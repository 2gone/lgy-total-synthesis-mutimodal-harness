"""Write acceptance dependencies derived from the existing accepted tool transcript.

Both fusion and source investigations use this guard. No general action history,
scientific completion inference, or additional persistence store lives here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.tool_delivery import (
    _REPAIR_ITEM_DESCRIPTION,
    PARTIAL_DELIVERY_GUIDANCE,
    WriteControl,
    repair_shape,
)
from tsdaf.contracts.tool_write import DIAGNOSTIC_CHARS
from tsdaf.workflow.write_registry import collection_of

# A write the Host could not read as items -- not an object, or keyed by a collection
# this task does not have -- may have carried what the model meant to save, so it stays
# an obligation; but no later write can name an item of it, and no resend covers it. It
# is recorded with no item, and this is what the model is told about it wherever it
# meets it: in the refusal, in a repair that names it, and among unresolved writes.
NO_ITEM_WRITE = (
    "A write with no item could not be read as items, so no later write corrects it. "
    + PARTIAL_DELIVERY_GUIDANCE
)


def write_feedback_items(reply: dict[str, Any]) -> list[dict[str, Any]]:
    """Join one call's acceptance metadata to its action rows without losing either.

    Item keys are local to this reply; action identities are never inferred from
    matching diagnostics, targets or scientific values. The transcript stays intact.
    """
    details = [entry for entry in reply.get("items", ()) if isinstance(entry, dict)]
    by_action = {entry["action_id"]: entry for entry in details
                 if isinstance(entry.get("action_id"), str)}
    by_item = {entry["item"]: entry for entry in details
               if isinstance(entry.get("item"), str)}
    rows = [{**deepcopy(by_action.get(row.get("action_id"), by_item.get(row.get("item"), {}))),
             **deepcopy(row)}
            for row in reply.get("write_results", ()) if isinstance(row, dict)]
    envelope = reply.get("validation_scope") == "envelope" and reply.get("items_validated") is False
    if rows and (envelope or reply.get("diagnostic_scope") == "call"):
        for row in rows:
            row["envelope_action"] = rows[0]["action_id"]
            row["diagnostic_scope"] = "call"
            if envelope:
                row["validation_scope"] = "envelope"
                row["items_validated"] = False
    return rows



def write_action_id(interaction_sha256: str, call_index: int, item: str | None) -> str:
    return (
        "write:"
        + hashlib.sha256(canonical_json_bytes([interaction_sha256, call_index, item])).hexdigest()[
            :24
        ]
    )


def split_write(raw: Any) -> tuple[dict[str, Any], WriteControl, list[dict[str, Any]]]:
    """The body, the repairs that are repairs, and one refusal per entry that is not.

    A repair entry of the wrong shape used to refuse the whole write, taking a corrected
    item down with it. It is now refused on its own; the items are written regardless.
    """
    if not isinstance(raw, dict):
        raise ValueError("write arguments must be an object")
    body = {key: value for key, value in raw.items() if key != "repairs"}
    example = next((f"{key}:0" for key, value in body.items() if isinstance(value, list)), None)
    entries = raw.get("repairs", [])
    refused: list[dict[str, Any]] = []
    valid: list[Any] = []
    if not isinstance(entries, list):
        refused.append(
            {
                "index": None,
                "outcome": "REJECTED",
                "diagnostic": "repairs is a list of entries, each naming a rejected row's "
                "action_id and " + _REPAIR_ITEM_DESCRIPTION + ".",
            }
        )
        entries = []
    for index, entry in enumerate(entries):
        try:
            valid.append(repair_shape(entry, example_key=example or "<collection>:0"))
        except ValueError as error:
            refused.append({"index": index, "outcome": "REJECTED", "diagnostic": str(error)})
    return body, WriteControl(repairs=tuple(valid)), refused


def covers_target(replacement: str | None, failed: str | None) -> bool:
    """Exact item identity, or the same subjects with all failed write parts covered."""
    if replacement is None or failed is None:
        return False
    if replacement == failed:
        return True
    try:
        old, new = json.loads(failed), json.loads(replacement)
    except ValueError:
        return False
    if not all(isinstance(v, dict) and set(v) == {"subjects", "fields"} for v in (old, new)):
        return False
    return old["subjects"] == new["subjects"] and set(old["fields"]) <= set(new["fields"])


class WriteDelivery:
    def __init__(self, history: Sequence[dict[str, Any]] = ()) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.resolved: dict[str, str] = {}
        self.completion: dict[str, Any] | None = None
        self._submissions: dict[str, Any] = {}
        calls: list[dict[str, Any]] = []
        for step in history:
            if step.get("turn_start"):
                calls = []
            if step.get("type") == "function_call":
                calls.append(step)
            if step.get("type") != "function_result":
                continue
            for part in step.get("result", []):
                value = json.loads(part["text"])
                for row in value.get("write_results", []):
                    self.accept(row)
                    sha, index = step.get("interaction_sha256"), step.get("call_index")
                    call = calls[index] if type(index) is int and 0 <= index < len(calls) else None
                    if (
                        call is not None
                        and call.get("call_id") == step.get("call_id")
                        and call.get("name") == row["tool"]
                        and isinstance(sha, str)
                        and isinstance(index, int)
                        and row["action_id"] == write_action_id(sha, index, row["item"])
                    ):
                        self._remember_submission(row, call.get("arguments"))
                if value.get("outcome") == "FINISHED":
                    self.completion = value.get("delivery")

    def pending(self) -> list[dict[str, Any]]:
        return [
            row
            for key, row in self.records.items()
            if row["outcome"] == "REJECTED" and key not in self.resolved
        ]

    def remember_candidate(self, action_id: str, proposal: dict[str, Any]) -> None:
        """Replace the comparison cache with the verified canonical effective proposal."""
        if action_id not in self.records:
            raise ValueError("candidate comparison requires an existing write action")
        self._submissions[action_id] = deepcopy(proposal)

    def forget_candidate(self, action_id: str) -> None:
        """An unexpandable edit has original evidence, but no full proposal to compare."""
        self._submissions.pop(action_id, None)

    def check_repairs(
        self, control: WriteControl, *, tool: str, targets: dict[str, str | None]
    ) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
        """Which items of this write repair which failures, and the explicit repairs refused.

        An explicit repair is the model's declared intent: it must name a known, unresolved
        failure and an identifiable item of this write in the same collection, and no more.
        A corrected identity is then a repair too, with both ids kept in the audit. The
        automatic link stays strict: an item resent without naming anything resolves only
        the failures it covers, subject and fields alike.
        """
        repairs: dict[str, list[str]] = {}
        refused: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, repair in enumerate(control.repairs):
            try:
                failed = self.records.get(repair.action_id)
                if repair.action_id in seen or repair.action_id in self.resolved:
                    raise ValueError("write repair is duplicate or already resolved")
                if failed is None or failed["outcome"] != "REJECTED":
                    raise ValueError("write repair requires a known rejected action")
                if failed["item"] is None:
                    raise ValueError(NO_ITEM_WRITE)
                # One write tool covers every collection, so its name no longer separates
                # them; the item's collection is what a repair may not cross.
                if collection_of(repair.item) != collection_of(failed["item"]):
                    raise ValueError("a write repair must replace an item of the same collection")
                if failed["tool"] != tool:
                    raise ValueError("write repair must match the rejected tool")
                if repair.item not in targets:
                    raise ValueError("a write repair must name an item of this write")
                if targets[repair.item] is None:
                    raise ValueError("a write repair must name an identifiable item of this write")
            except ValueError as error:
                refused.append(
                    {
                        "index": index,
                        "action_id": repair.action_id,
                        "item": repair.item,
                        "outcome": "REJECTED",
                        "diagnostic": str(error),
                    }
                )
                continue
            seen.add(repair.action_id)
            repairs.setdefault(repair.item, []).append(repair.action_id)
        for key, target in targets.items():
            if target is None:
                continue
            for failed in self.pending():
                action_id = failed["action_id"]
                if (
                    action_id in seen
                    or failed["item"] is None
                    or failed["tool"] != tool
                    or collection_of(failed["item"]) != collection_of(key)
                    or not covers_target(target, failed["target"])
                ):
                    continue
                seen.add(action_id)
                repairs.setdefault(key, []).append(action_id)
        return repairs, refused

    def accept(self, row: dict[str, Any]) -> None:
        key = row["action_id"]
        if key in self.records:
            if self.records[key] != row:
                raise ValueError("an accepted write action cannot be rebound")
            return
        relations = row.get("resolutions")
        if relations is None:
            # Old sealed rows recorded a mechanical/declared relationship under
            # repairs. Preserve that history; do not certify a scientific correction.
            relations = [
                {"action_id": action, "kind": "LEGACY"} for action in row.get("repairs", ())
            ]
        elif "repairs" in row:
            raise ValueError("write resolution has two competing representations")
        if row["outcome"] != "REJECTED":
            ids: list[str] = []
            for relation in relations:
                failed_id = relation["action_id"]
                if relation["kind"] not in {"SUPERSEDED", "DECLARED", "LEGACY"} or failed_id in ids:
                    raise ValueError("invalid or duplicate write resolution")
                ids.append(failed_id)
                failed = self.records.get(failed_id)
                if failed is not None and collection_of(failed["item"]) != collection_of(
                    row["item"]
                ):
                    raise ValueError("a write repair must replace an item of the same collection")
                if (
                    failed is None
                    or failed["outcome"] != "REJECTED"
                    or failed_id in self.resolved
                    or row["target"] is None
                    or failed["tool"] != row["tool"]
                ):
                    raise ValueError(
                        "accepted write repair lacks its unresolved matching predecessor"
                    )
                if relation["kind"] == "SUPERSEDED" and not covers_target(
                    row["target"], failed["target"]
                ):
                    raise ValueError("supersession requires the same covered item target")
            for failed_id in ids:
                self.resolved[failed_id] = key
        self.records[key] = row

    def finish(self, *, disposition: str, note: str) -> dict[str, Any]:
        pending = self.pending()
        if disposition != "PARTIAL" and pending:
            raise ValueError("complete delivery requires no active rejected write proposals")
        return {"disposition": disposition, "omitted_writes": pending, "note": note}

    def describe_resolutions(
        self, row: dict[str, Any], control: WriteControl, *, submitted: Any = None
    ) -> None:
        """New receipt spelling: accepted replacement is not a scientific proof.

        Failed versions stay in records and the immutable submitted-call transcript.
        Explicit relationships are model declarations; automatic ones cover identity.
        """
        linked = row.pop("repairs", ())
        row["resolutions"] = (
            [
                {
                    "action_id": action,
                    "kind": "DECLARED"
                    if any(r.action_id == action and r.item == row["item"] for r in control.repairs)
                    else "SUPERSEDED",
                }
                for action in linked
            ]
            if row["outcome"] != "REJECTED"
            else []
        )
        if row["resolutions"]:
            row["resolution_note"] = (
                "Accepted new version replaces the linked rejected proposal. This records "
                "item identity or your declared relationship, not scientific equivalence or "
                "proof that the old claim was saved; its original rejection is retained."
            )
            current = submitted_item(submitted, row["item"])
            comparisons = []
            linked_ids = {r["action_id"] for r in row["resolutions"]}
            # The nearest prior submission is usually the most useful comparison.
            # Explicit repair order is not chronology; the accepted record order is.
            for action in reversed(self.records):
                if action not in linked_ids:
                    continue
                if current is None or action not in self._submissions:
                    continue
                changes = submission_changes(self._submissions[action], current)
                comparisons.append(
                    {
                        "action_id": action,
                        **changes,
                        **(
                            {"original_submission_read": self.records[action]["submission_read"]}
                            if "submission_read" in self.records[action]
                            else {}
                        ),
                    }
                )
            if comparisons:
                description = {
                    "meaning": "Rejected submission versus this successful submission, not "
                    "a diff of saved facts. Omitted fields may inherit saved values. "
                    "These changes do not establish scientific equivalence; check whether "
                    "your intended supported meaning is retained.",
                    "comparisons": [],
                    "total_comparisons": len(comparisons),
                    "omitted_comparisons": len(comparisons),
                    "unavailable_comparisons": len(linked_ids) - len(comparisons),
                }
                for comparison in comparisons:
                    trial = deepcopy(comparison)
                    description["comparisons"].append(trial)
                    description["omitted_comparisons"] -= 1
                    # Bound the whole feedback, not each old proposal independently.
                    # Keep paths when possible; complete values remain at the READ link.
                    while len(canonical_json_bytes(description)) > DIAGNOSTIC_CHARS:
                        with_values = next(
                            (f for f in reversed(trial["fields"]) if not f.get("values_omitted")),
                            None,
                        )
                        if with_values is not None:
                            with_values.pop("before", None)
                            with_values.pop("after", None)
                            with_values["values_omitted"] = True
                        elif trial["fields"]:
                            trial["fields"].pop()
                            trial["omitted_fields"] += 1
                        else:
                            description["comparisons"].pop()
                            description["omitted_comparisons"] += 1
                            break
                    if (
                        description["omitted_comparisons"]
                        and trial not in description["comparisons"]
                    ):
                        break
                row["submission_changes"] = description
        self._remember_submission(row, submitted)

    def _remember_submission(self, row: dict[str, Any], submitted: Any) -> None:
        if row["outcome"] != "REJECTED":
            return
        item = submitted_item(submitted, row["item"])
        if item is not None:
            self._submissions[row["action_id"]] = deepcopy(item)


def submitted_item(raw: Any, key: str | None) -> Any:
    """A per-item submission as written, never an inferred or accepted scientific value."""
    if isinstance(raw, str):
        try:
            raw = decode_tool_arguments(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict) or not isinstance(key, str):
        return None
    collection, mark, index = key.partition(":")
    values = raw.get(collection)
    if mark and index.isdecimal() and isinstance(values, list) and int(index) < len(values):
        return values[int(index)]
    return None


def submission_changes(before: Any, after: Any) -> dict[str, Any]:
    """Exact JSON field changes; arrays are whole values, never guessed item matches."""
    changes: list[dict[str, Any]] = []

    def visit(old: Any, new: Any, path: str) -> None:
        if canonical_json_bytes(old) == canonical_json_bytes(new):
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                child = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in old:
                    changes.append({"path": child, "change": "ADDED", "after": new[key]})
                elif key not in new:
                    changes.append({"path": child, "change": "OMITTED", "before": old[key]})
                else:
                    visit(old[key], new[key], child)
        else:
            changes.append({"path": path, "change": "CHANGED", "before": old, "after": new})

    visit(before, after, "")
    # Show emptied fields first without asserting they lost a scientific meaning.
    changes.sort(
        key=lambda c: not (c["change"] == "OMITTED" or c.get("after") in (None, "", [], {}))
    )
    shown = []
    for change in changes:
        candidate = change
        if len(canonical_json_bytes([*shown, candidate])) > DIAGNOSTIC_CHARS:
            candidate = {"path": change["path"], "change": change["change"], "values_omitted": True}
        if len(canonical_json_bytes([*shown, candidate])) > DIAGNOSTIC_CHARS:
            break
        shown.append(candidate)
    return {
        "fields": shown,
        "total_fields": len(changes),
        "omitted_fields": len(changes) - len(shown),
    }


def write_result(
    *,
    interaction_sha256: str,
    call_index: int,
    item: str | None,
    tool: str,
    target: str | None,
    outcome: str,
    repairs: Sequence[str] = (),
    diagnostic: Any = None,
) -> dict[str, Any]:
    return {
        "action_id": write_action_id(interaction_sha256, call_index, item),
        "item": item,
        "tool": tool,
        "target": target,
        "outcome": outcome,
        "repairs": list(repairs),
        "diagnostic": diagnostic,
    }


def named_target(
    raw: Any, keys: tuple[str, ...], *, content_keys: frozenset[str] = frozenset()
) -> str | None:
    def identifying(value: Any) -> bool:
        return (isinstance(value, str) and bool(value)) or (
            isinstance(value, (list, tuple))
            and all(isinstance(part, str) and bool(part) for part in value)
        )

    if not isinstance(raw, dict) or any(
        not identifying(raw.get(k))
        and not (k in content_keys and isinstance(raw.get(k), (dict, list, tuple)))
        for k in keys
    ):
        return None
    return canonical_json_bytes({key: raw[key] for key in keys}).decode()
