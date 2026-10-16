"""Accepted action progression derived from tool history and host interaction bindings.

Execution records maintain chronological action order and item-level outcomes
without echoing document bodies, raw images, or unaccepted writes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.workflow.tool_turns import recorded_reply
from tsdaf.workflow.write_delivery import write_action_id, write_feedback_items

ACTION_PROGRESS_ORIGIN = "EXECUTION_RECORD"


@dataclass
class ActionRecord:
    sequence: int
    action_id: str
    tool: str
    outcome: str
    item: str | None = None
    target: str | None = None
    location: str | None = None
    # Only a refused read carries this: what a successful read said is in the content
    # window, while a refusal leaves no trace there at all (`CONTENT_OUTCOMES`), so the
    # one record that outlives its turn must name the select as well as the location.
    select: Any = None
    version: str | None = None
    diagnostic: Any = None
    summary: str = ""
    call_id: str | None = None
    submission_read: dict[str, Any] | None = None
    feedback: dict[str, Any] | None = None
    origin: str = ACTION_PROGRESS_ORIGIN

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "origin": self.origin,
            "sequence": self.sequence,
            "action_id": self.action_id,
            "tool": self.tool,
            "outcome": self.outcome,
            "summary": self.summary,
        }
        if self.feedback is not None:
            result["feedback"] = self.feedback
        if self.item is not None:
            result["item"] = self.item
        if self.target is not None:
            result["target"] = self.target
        if self.location is not None:
            result["location"] = self.location
        if self.select is not None:
            result["select"] = self.select
        if self.version is not None:
            result["version"] = self.version
        if self.diagnostic is not None:
            result["diagnostic"] = self.diagnostic
        if self.call_id is not None:
            result["call_id"] = self.call_id
        if self.submission_read is not None:
            result["submission_read"] = dict(self.submission_read)
        return result


class ActionProgress:
    """Deterministic accepted action order derived from accepted tool transcript and bindings.

    Folding is incremental: a history that extends the steps already folded (the same
    step objects, in order) only parses the new steps. Any other history replays from
    the start; ``_seen_ids`` keeps the replay idempotent, exactly as a fresh fold would.
    """

    def __init__(self, history: Sequence[dict[str, Any]] = ()) -> None:
        self.records: list[ActionRecord] = []
        self._seen_ids: set[str] = set()
        self._reset_cursor()
        if history:
            self.fold(history)

    def _reset_cursor(self) -> None:
        self._consumed: list[dict[str, Any]] = []
        self._pending_calls: dict[str, tuple[dict[str, Any], int, int]] = {}
        self._turn_index = 0
        self._call_index = 0
        self._in_turn = False

    def fold(self, history: Sequence[dict[str, Any]]) -> None:
        consumed = self._consumed
        if len(history) < len(consumed) or any(
            step is not folded for step, folded in zip(history, consumed, strict=False)
        ):
            self._reset_cursor()
            consumed = self._consumed

        for position in range(len(consumed), len(history)):
            step = history[position]
            consumed.append(step)
            step_type = step.get("type")
            if step_type == "function_call":
                if step.get("turn_start") or not self._in_turn:
                    self._turn_index += 1
                    self._call_index = 0
                    self._in_turn = True
                else:
                    self._call_index += 1
                call_id = step.get("call_id")
                if isinstance(call_id, str):
                    self._pending_calls[call_id] = (step, self._turn_index, self._call_index)
            elif step_type == "function_result":
                call_id = step.get("call_id")
                call_info = (
                    self._pending_calls.pop(call_id, None) if isinstance(call_id, str) else None
                )
                if call_info is not None:
                    call, call_turn, call_idx = call_info
                else:
                    call, call_turn, call_idx = {}, self._turn_index, self._call_index
                self._fold_result(step, call, call_turn, call_idx)
            elif step_type == "user_input":
                self._in_turn = False

    def _fold_result(
        self,
        step: dict[str, Any],
        call: dict[str, Any],
        turn_index: int,
        fallback_call_index: int,
    ) -> None:
        call_id_raw = step.get("call_id")
        call_id = call_id_raw if isinstance(call_id_raw, str) else call.get("call_id")

        interaction_ref = step.get("interaction_ref") or call.get("interaction_ref")
        interaction_sha256 = (
            step.get("interaction_sha256")
            or call.get("interaction_sha256")
            or (
                interaction_ref.get("sha256")
                if isinstance(interaction_ref, dict)
                and isinstance(interaction_ref.get("sha256"), str)
                else None
            )
        )

        step_call_idx = step.get("call_index")
        if isinstance(step_call_idx, int):
            call_index = step_call_idx
        else:
            call_index = fallback_call_index

        if interaction_sha256 is not None:
            host_sha = interaction_sha256
        else:
            host_sha = hashlib.sha256(canonical_json_bytes(["turn", turn_index])).hexdigest()

        try:
            reply = recorded_reply(step)
        except (ValueError, TypeError):
            raw_hash = hashlib.sha256(
                canonical_json_bytes([host_sha, call_index, "malformed"])
            ).hexdigest()[:24]
            aid = f"action:{raw_hash}"
            if aid not in self._seen_ids:
                self._seen_ids.add(aid)
                self.records.append(
                    ActionRecord(
                        sequence=len(self.records) + 1,
                        action_id=aid,
                        tool=str(call.get("name", "unknown")),
                        outcome="REJECTED",
                        diagnostic="Malformed function result payload",
                        summary=f"{call.get('name', 'unknown')} rejected: malformed payload",
                        call_id=call_id if isinstance(call_id, str) else None,
                    )
                )
            return

        if not isinstance(reply, dict):
            return

        # 1. Tool with write_results (Write tools)
        write_results = reply.get("write_results")
        if isinstance(write_results, list) and write_results:
            for row in write_feedback_items(reply):
                if not isinstance(row, dict):
                    continue
                raw_aid = row.get("action_id")
                if isinstance(raw_aid, str) and raw_aid:
                    aid = raw_aid
                else:
                    item_key = str(row.get("item", "item"))
                    aid = write_action_id(host_sha, call_index, item_key)
                if aid in self._seen_ids:
                    continue
                self._seen_ids.add(aid)
                tool = str(row.get("tool") or call.get("name") or "write")
                outcome = str(row.get("outcome", "UNKNOWN"))
                item = str(row["item"]) if row.get("item") is not None else None
                target = str(row["target"]) if row.get("target") is not None else None
                diag = row.get("diagnostic")
                summary_parts = [f"Write {outcome.lower()}: {tool}"]
                if item:
                    summary_parts.append(f"({item})")
                if diag:
                    summary_parts.append(f": {diag}")
                self.records.append(
                    ActionRecord(
                        sequence=len(self.records) + 1,
                        action_id=aid,
                        tool=tool,
                        outcome=outcome,
                        item=item,
                        target=target,
                        diagnostic=diag,
                        summary=" ".join(summary_parts),
                        call_id=call_id if isinstance(call_id, str) else None,
                        submission_read=row.get("submission_read"),
                        feedback=row,
                    )
                )
            return

        tool_name = str(call.get("name", ""))

        # 2. Read tool or read receipt items
        items = reply.get("items")
        if tool_name in {"read", "read_many"} or (
            not tool_name and isinstance(items, list) and "outcome" in reply
        ):
            tool = tool_name or "read"
            if isinstance(items, list) and items:
                for idx, item_data in enumerate(items):
                    if not isinstance(item_data, dict):
                        continue
                    raw_aid = item_data.get("action_id")
                    item_key = str(item_data.get("item", f"item:{idx}"))
                    if isinstance(raw_aid, str) and raw_aid:
                        aid = raw_aid
                    else:
                        aid = (
                            f"{tool}:"
                            + hashlib.sha256(
                                canonical_json_bytes([host_sha, call_index, item_key])
                            ).hexdigest()[:24]
                        )
                    if aid in self._seen_ids:
                        continue
                    self._seen_ids.add(aid)
                    outcome = str(item_data.get("outcome", "UNKNOWN"))
                    location = (
                        str(item_data["location"])
                        if item_data.get("location") is not None
                        else None
                    )
                    version = (
                        str(item_data["content_version"])
                        if item_data.get("content_version") is not None
                        else str(item_data["version"])
                        if item_data.get("version") is not None
                        else None
                    )
                    diag = item_data.get("diagnostic") or item_data.get("code")
                    # Measured on the eight-task decumbenine_B run at d77b930: one task
                    # resent the same location with a byte-identical select five turns
                    # running, because this view named neither.
                    select = item_data.get("select") if outcome == "REJECTED" else None
                    if outcome == "READ":
                        summary = f"Read succeeded: {location or item_key}"
                    elif outcome == "DIRECTORY":
                        summary = f"Directory listed: {location or item_key}"
                    elif outcome == "REJECTED":
                        diag_str = f" ({diag})" if diag else ""
                        summary = f"Read rejected: {location or item_key}{diag_str}"
                    else:
                        summary = f"Read {outcome.lower()}: {location or item_key}"
                    self.records.append(
                        ActionRecord(
                            sequence=len(self.records) + 1,
                            action_id=aid,
                            tool=tool,
                            outcome=outcome,
                            item=item_key,
                            target=location,
                            location=location,
                            select=select,
                            version=version,
                            diagnostic=diag,
                            summary=summary,
                            call_id=call_id if isinstance(call_id, str) else None,
                        )
                    )
            else:
                item_key = tool
                aid = (
                    f"{tool}:"
                    + hashlib.sha256(
                        canonical_json_bytes([host_sha, call_index, item_key])
                    ).hexdigest()[:24]
                )
                if aid not in self._seen_ids:
                    self._seen_ids.add(aid)
                    outcome = str(reply.get("outcome", "REJECTED"))
                    diag = reply.get("diagnostic") or reply.get("code")
                    diag_msg = diag or "no items"
                    summary = f"Read {outcome.lower()}: {diag_msg}"
                    self.records.append(
                        ActionRecord(
                            sequence=len(self.records) + 1,
                            action_id=aid,
                            tool=tool,
                            outcome=outcome,
                            item=item_key,
                            diagnostic=diag,
                            summary=summary,
                            call_id=call_id if isinstance(call_id, str) else None,
                        )
                    )
            return

        # 3. Finish tools
        if reply.get("outcome") == "FINISHED":
            aid = (
                "finish:"
                + hashlib.sha256(
                    canonical_json_bytes([host_sha, call_index, "finish"])
                ).hexdigest()[:24]
            )
            if aid not in self._seen_ids:
                self._seen_ids.add(aid)
                delivery = reply.get("delivery")
                disp = (
                    delivery.get("disposition", "COMPLETE")
                    if isinstance(delivery, dict)
                    else "COMPLETE"
                )
                note = delivery.get("note", "") if isinstance(delivery, dict) else ""
                version = (
                    reply["result_ref"]["sha256"]
                    if (
                        isinstance(reply.get("result_ref"), dict)
                        and "sha256" in reply["result_ref"]
                    )
                    else None
                )
                summary = f"Finished ({disp}): {note}".strip() if note else f"Finished ({disp})"
                self.records.append(
                    ActionRecord(
                        sequence=len(self.records) + 1,
                        action_id=aid,
                        tool=tool_name or "finish",
                        outcome="FINISHED",
                        item="finish",
                        version=version,
                        summary=summary,
                        call_id=call_id if isinstance(call_id, str) else None,
                    )
                )
            return

        # 4. Other / rejected action without write_results
        aid = (
            "action:"
            + hashlib.sha256(canonical_json_bytes([host_sha, call_index, "call"])).hexdigest()[:24]
        )
        if aid not in self._seen_ids:
            self._seen_ids.add(aid)
            outcome = str(reply.get("outcome", "UNKNOWN"))
            diag = reply.get("diagnostic")
            label = tool_name or "action"
            diag_msg = diag or "action performed"
            summary = f"{label} {outcome.lower()}: {diag_msg}"
            self.records.append(
                ActionRecord(
                    sequence=len(self.records) + 1,
                    action_id=aid,
                    tool=tool_name or "action",
                    outcome=outcome,
                    item="call",
                    diagnostic=diag,
                    summary=summary,
                    call_id=call_id if isinstance(call_id, str) else None,
                )
            )

    def visible(self, *, max_recent: int = 20) -> dict[str, Any]:
        return bounded_action_view(
            [record.as_dict() for record in self.records], max_recent=max_recent
        )


def bounded_action_view(
    records: Sequence[Mapping[str, Any]], *, max_recent: int = 20
) -> dict[str, Any]:
    """Bound already derived action records (the ``state:history`` value) for the model."""
    total = len(records)
    if total <= max_recent:
        omitted = 0
        shown = records
    else:
        omitted = total - max_recent
        shown = records[-max_recent:]
    # Keep original inputs reachable in the full history and immediate receipt,
    # without repeatedly advertising a successful submission as a current-result read.
    visible = [
        {
            key: value
            for key, value in record.items()
            if not (
                key == "submission_read"
                and record.get("tool") == "write"
                and record.get("outcome") != "REJECTED"
            )
        }
        for record in shown
    ]
    return {
        "origin": ACTION_PROGRESS_ORIGIN,
        "meaning": (
            "Past actions; submission_read opens the original submitted inputs, not current "
            "saved contents. Inspect the current result or saved-item view for current contents."
        ),
        "total_actions": total,
        "omitted_actions": omitted,
        "actions": visible,
    }


def bounded_action_progress(
    history: Sequence[dict[str, Any]], *, max_recent: int = 20
) -> dict[str, Any]:
    """Derive bounded visible action progress from accepted tool history."""
    return ActionProgress(history).visible(max_recent=max_recent)
