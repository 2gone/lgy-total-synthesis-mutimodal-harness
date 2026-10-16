"""Derive creation actions from sealed writes; never infer ownership from wording.

This is a disposable index of existing audit evidence, not a second fact store.
Incomplete/legacy histories yield no origins. Artifact integrity failures still
raise. The current preview and final assembly use the same derivation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.enrichment_models import InvestigationReceipt
from tsdaf.lanes.literature.gate_models import FactOrigin
from tsdaf.lanes.literature.source_investigation import INVESTIGATION_TOOL_KINDS
from tsdaf.lanes.literature.stable_keys import public_creation_key
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.investigation_edits import InvestigationEdits, verify_effective_history
from tsdaf.workflow.tool_execution import is_call_receipt
from tsdaf.workflow.tool_turns import response_calls
from tsdaf.workflow.write_delivery import write_action_id

INVESTIGATION_INTERACTION_MEDIA = "application/vnd.tsdaf.investigation-interaction+json"
REPAIR_COMMAND_VERSION = 1


def delivered_selections_in(record: Mapping[str, Any]) -> set[str]:
    """Selections presented before this response, in old full or current delta form."""
    return set(record.get("delivered_selections", ())) | set(record.get("delivered_added", ()))


class IncompleteOrigins(ValueError):
    """Existing evidence does not establish an unbroken item lifecycle."""


def _require(condition: bool) -> None:
    if not condition:
        raise IncompleteOrigins("creation history is incomplete or ambiguous")


def withdraw_item_state(active, target):
    """Apply an already decoded explicit wrapper withdrawal, without inference."""
    _require(bool(target.get("item_id") or target.get("location")))
    matches = [
        key for key, saved in active.items()
        if (target.get("item_id") is None or target["item_id"] == key[1])
        and (target.get("location") is None or target["location"] == saved["location"])
    ]
    _require(len(matches) == 1)
    key = matches[0]
    del active[key]
    return key


def write_item_state(active, *, collection, item_id, location, value, new_origin,
                     repair_births=None):
    """One wrapper identity transition for verified history and live candidates.

    Callers prove their own evidence. A provisional origin is never sealed here.
    An existing incarnation keeps its birth even when the saved value changes.
    """
    _require(bool(item_id) and value["item_id"] == item_id)
    key = (collection, item_id)
    origin = active[key]["origin"] if key in active else new_origin
    active[key] = {
        "origin": origin, "location": location, "value": value,
        "repair_births": repair_births or {},
    }
    return origin


def accepted_write_batches(
    call: Mapping[str, Any], *, required: bool = False
) -> tuple[tuple[str, ...], ...]:
    """One authority for actual acceptance order, independent of reply display order.

    Legacy receipts without an order retain their original row order. Advertising
    an order is binding even for an empty/rejected write; a malformed advertised
    order must not silently fall back to the legacy interpretation.
    """
    rows = call.get("write_results", ())
    keys = [row["item"] for row in rows]
    _require(all(isinstance(key, str) for key in keys) and len(keys) == len(set(keys)))
    accepted = {
        row["item"] for row in rows if row["outcome"] in {"SAVED", "NO_CHANGE", "WITHDRAWN"}
    }
    if "accepted_item_batches" not in call:
        _require(not required or not accepted)
        return tuple((key,) for key in keys if key in accepted)
    batches = call["accepted_item_batches"]
    _require(
        isinstance(batches, list) and all(isinstance(batch, list) and batch for batch in batches)
    )
    flat = [key for batch in batches for key in batch]
    _require(all(isinstance(key, str) for key in flat))
    _require(len(flat) == len(set(flat)) and set(flat) == accepted)
    return tuple(tuple(batch) for batch in batches)


class InvestigationOrigins:
    def __init__(self, read_bytes: Callable[[ArtifactRef], bytes]) -> None:
        self.read_bytes = read_bytes
        self._bytes: dict[ArtifactRef, bytes] = {}
        self._records: dict[ArtifactRef, Any] = {}
        self.creation_origins: dict[tuple[str, str], dict[str, str]] = {}
        self.action_origins: dict[str, dict[str, str]] = {}
        self.repair_birth_origins: dict[str, dict[str, FactOrigin]] = {}
        self._verified_calls: tuple[tuple[ArtifactRef, dict[str, Any]], ...] | None = None

    def _read(self, ref: ArtifactRef) -> bytes:
        if ref not in self._bytes:
            self._bytes[ref] = read_artifact(self.read_bytes, ref)
        return self._bytes[ref]

    def _record(self, ref: ArtifactRef) -> Any:
        if ref not in self._records:
            self._records[ref] = json.loads(self._read(ref))
        return self._records[ref]

    def derive(
        self, receipt: InvestigationReceipt, *, allow_call_prefix: bool = False, strict: bool = False
    ) -> dict[tuple[str, str], dict[str, str]]:
        """Only the last live response may have a still-unexecuted suffix.

        Values must also equal the current accepted answer. Host reconciliation
        that changed another item without a corresponding row cannot prove that
        item's history; it is not reconstructed here with another scientific rule.
        """
        # Never let a failed/incomplete derivation retain a previous verified generation.
        self._verified_calls = None
        self.creation_origins = {}
        self.action_origins = {}
        self.repair_birth_origins = {}
        # Verify bytes outside the compatibility fallback: corruption is not legacy.
        records = [
            (ref, self._record(ref))
            for ref in dict.fromkeys(receipt.audit_refs)
            if ref.media_type == INVESTIGATION_INTERACTION_MEDIA
        ]
        for _, record in records:
            if isinstance(record, dict) and "response_ref" in record:
                self._read(ArtifactRef.model_validate(record["response_ref"]))
        verify_effective_history(self._read, records, accepted=set(receipt.audit_refs))
        try:
            self.creation_origins = self._derive(receipt, records, allow_call_prefix)
            return self.creation_origins
        except (ValueError, KeyError, TypeError, AttributeError):
            if strict:
                raise IncompleteOrigins("record history requires complete accepted calls") from None
            self.action_origins = {}
            self.repair_birth_origins = {}
            return {}

    def verified_repair_history(
        self, receipt: InvestigationReceipt, *, restore_domain, allow_call_prefix: bool = False,
        require_command_receipts: bool = False,
        accepted_refs: Sequence[ArtifactRef] | None = None,
        include_request_context: bool = False,
    ) -> dict[str, Any]:
        """Derive bound repair commands from existing receipts, never from latest values.

        This strict opt-in index does not reinterpret legacy snapshot answers. It
        retains historical actions separately from current wrapper incarnations;
        the fact reducer alone will determine their scientific consequences.
        """
        from tsdaf.lanes.literature.middle_layer import UnifiedSkeletonRepair

        self.derive(receipt, allow_call_prefix=allow_call_prefix)
        # Before the first interaction there are no commands to recover. This
        # exception proves only an empty answer, never an unaudited saved repair.
        if not receipt.request_refs and not any(
            ref.media_type == INVESTIGATION_INTERACTION_MEDIA for ref in receipt.audit_refs
        ):
            answer = receipt.raw_model()
            _require(not any(isinstance(value, list) and value for value in (
                answer.model_dump(mode="json") if answer is not None else {}
            ).values()))
            return {"actions": [], "active": {}, "locations": {}, "events": [],
                    **({"request_contexts": []} if include_request_context else {})}
        if self._verified_calls is None:
            raise IncompleteOrigins("sparse repair history requires complete accepted calls")
        turns = [{**record, "interaction_ref": ref.model_dump(mode="json")}
                 for ref in dict.fromkeys(receipt.audit_refs)
                 if ref.media_type == INVESTIGATION_INTERACTION_MEDIA
                 if "response_ref" in (record := self._record(ref))]
        binding = turns[0]["binding"]
        presented: set[str] = set()
        authorization = {}
        task_input = self._record(receipt.input_ref)
        inherited = (set(task_input.get("context", {}).get("record_selections", {}))
                     if task_input.get("record_receipt_ref") else set())
        for turn in sorted(turns, key=lambda turn: turn["iteration"]):
            presented |= delivered_selections_in(turn)
            authorization[turn["iteration"]] = tuple(sorted(presented))
        # Naming snapshots and request intents live in the existing accepted
        # artifact closure, not necessarily in the receipt's compact audit list.
        # Live callers supply their accepted set; publication supplies the verified
        # terminal closure. A smaller set must fail rather than infer authorization.
        closure = tuple(accepted_refs) if accepted_refs is not None else (
            *receipt.audit_refs, *receipt.request_refs,
        )
        edits = InvestigationEdits(
            read_bytes=self._read, audits=lambda: receipt.audit_refs,
            accepted=lambda: closure,
            binding=lambda: binding, restore_domain=restore_domain,
        )
        active: dict[str, dict[str, Any]] = {}
        actions: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        for interaction_ref, call in self._verified_calls:
            if "write_results" not in call:
                continue  # READ/FINISH have different item shapes and no repair commands.
            rows = {row["item"]: row for row in call.get("write_results", ())}
            replies = {row["item"]: row for row in call.get("items", ())}
            accepted = {key for key, row in rows.items()
                        if row["outcome"] in {"SAVED", "NO_CHANGE", "WITHDRAWN"}}
            repair_keys = {key for key in accepted if key.startswith("skeleton_repairs:")}
            has_commands = "repair_command_version" in call or "repair_commands" in call
            if has_commands or (require_command_receipts and repair_keys):
                _require(type(call.get("repair_command_version")) is int
                         and call["repair_command_version"] == REPAIR_COMMAND_VERSION)
                _require(isinstance(call.get("repair_commands"), dict))
                _require(set(call["repair_commands"]) == repair_keys)
                _require(all(isinstance(value, dict) for value in call["repair_commands"].values()))
            relevant = any(key.startswith("skeleton_repairs:") for key in accepted)
            relevant |= bool(active) and any(key.startswith("withdrawals:") for key in accepted)
            if not relevant:
                continue
            batches = accepted_write_batches(call, required=True)
            for batch_index, batch in enumerate(batches):
                final_submissions: dict[str, dict[str, Any]] = {}
                saved_repairs: list[dict[str, Any]] = []
                for key in batch:
                    row = rows[key]
                    if key.startswith("withdrawals:"):
                        target = json.loads(row["target"])
                        matches = [handle for handle, value in active.items()
                                   if (target.get("item_id") is None or target["item_id"] == handle)
                                   and (target.get("location") is None or target["location"] == value["location"])]
                        if matches:
                            _require(len(matches) == 1)
                            submitted, _ = edits.canonical(row["action_id"], "withdrawals")
                            _require(all(submitted.get(name) == target.get(name)
                                         for name in ("item_id", "location")))
                            handle = matches[0]
                            events.append({"kind": "WITHDRAW", "item_id": handle,
                                           "incarnation": active[handle]["incarnation"],
                                           "action_id": row["action_id"],
                                           "batch": [interaction_ref.sha256, call["call_index"], batch_index]})
                            del active[handle]
                        continue
                    if not key.startswith("skeleton_repairs:"):
                        continue
                    reply = replies[key]
                    submitted, _ = edits.canonical(row["action_id"], "skeleton_repairs")
                    parsed = UnifiedSkeletonRepair.model_validate_json(canonical_json_bytes(submitted))
                    handle = reply["item_id"]
                    _require(parsed.item_id in (None, handle))
                    bound = parsed.model_copy(update={"item_id": handle}).model_dump(mode="json")
                    final_submissions[handle] = bound
                    saved_repairs.append(reply)
                    previous = active.get(handle)
                    _require(previous is not None or row["outcome"] == "SAVED")
                    incarnation = previous["incarnation"] if previous else row["action_id"]
                    active[handle] = {"incarnation": incarnation, "location": reply["location"]}
                    actions.append({
                        "value": bound, "incarnation": incarnation, "outcome": row["outcome"],
                        "batch": [interaction_ref.sha256, call["call_index"], batch_index],
                        "origin": FactOrigin(
                            interaction_ref=interaction_ref, call_index=call["call_index"],
                            action_id=row["action_id"], item=key, change_index=0,
                        ).model_dump(mode="json"),
                        "delivered_selection_ids": list(authorization[self._record(interaction_ref)["iteration"]]),
                        "inherited_selection_ids": sorted(inherited),
                        # This is bound execution evidence, not yet a validated
                        # scientific command. Its consumer must check normalization.
                        **({"command_evidence": deepcopy(call["repair_commands"][key])}
                           if has_commands else {}),
                    })
                    events.append({"kind": "WRITE", "action_index": len(actions) - 1})
                # The writer reports the complete atomic candidate for every
                # same-handle row, even an earlier NO_CHANGE. Preserve each
                # original command above; compare saved views only to the last
                # submitted wrapper at this actual acceptance boundary.
                for reply in saved_repairs:
                    _require(final_submissions[reply["item_id"]] == reply["value"])
        expected = {item.item_id for item in getattr(receipt.raw_model(), "skeleton_repairs", ())}
        _require(set(active) == expected)
        result = {
            "actions": actions,
            "active": {key: value["incarnation"] for key, value in active.items()},
            "locations": {key: value["location"] for key, value in active.items()},
            "events": events,
        }
        if include_request_context:
            result["request_contexts"] = self._request_contexts(
                receipt, turns=turns, edits=edits, closure=closure,
                authorization=authorization,
            )
        return result

    def _request_contexts(self, receipt, *, turns, edits, closure, authorization):
        """Disposable whole-batch wrapper states from the already verified calls.

        Submitted canonical values and accepted saved values are deliberately
        separate: ordinary scope inheritance may legitimately distinguish them.
        This records no new fact or journal, and makes no scientific projection.
        A consumer must check each request map against its prior verified state
        before using any batch belonging to that response.
        """
        from tsdaf.lanes.literature.model_names import NAMING_VERSION
        from tsdaf.workflow.naming_evidence import verify_accepted_turn_naming

        task_input = self._record(receipt.input_ref)
        inherited = (set(task_input.get("context", {}).get("record_selections", {}))
                     if task_input.get("record_receipt_ref") else set())
        active = {}
        contexts = []
        calls_by_turn = {}
        for interaction_ref, call in self._verified_calls or ():
            iteration = self._record(interaction_ref)["iteration"]
            calls_by_turn.setdefault(iteration, []).append((interaction_ref, call))
        for turn in sorted(turns, key=lambda value: value["iteration"]):
            snapshot = verify_accepted_turn_naming(
                self._read,
                turn,
                closure,
                expected_naming_version=NAMING_VERSION,
                expected_binding=turn["binding"],
                expected_iteration=turn["iteration"],
            )
            _require(snapshot is not None)
            context = {
                "iteration": turn["iteration"],
                "naming": snapshot,
                "delivered_selection_ids": list(authorization[turn["iteration"]]),
                "inherited_selection_ids": sorted(inherited),
                "batches": [],
            }
            contexts.append(context)
            for interaction_ref, call in calls_by_turn.get(turn["iteration"], ()):
                if "write_results" not in call:
                    continue
                if not accepted_write_batches(call, required=True):
                    # A rejected envelope/finish has no saved item payload. It is
                    # evidence of a failed attempt, not an accepted state transition.
                    continue
                rows = {row["item"]: row for row in call["write_results"]}
                replies = {row["item"]: row for row in call["items"]}
                touched, removed = set(), {}

                def changed(key, withdrawn):
                    touched.add(key)
                    if withdrawn:
                        removed[key] = None

                def completed(
                    batch_index,
                    keys,
                    *,
                    rows=rows,
                    replies=replies,
                    context=context,
                    interaction_ref=interaction_ref,
                    call_index=call["call_index"],
                ):
                    items = []
                    for key in keys:
                        row, reply = rows[key], replies[key]
                        submitted, semantics = edits.canonical(row["action_id"], key.split(":")[0])
                        items.append(
                            {
                                "item": key,
                                "action_id": row["action_id"],
                                "submitted": submitted,
                                "semantics": semantics,
                                "saved": deepcopy(reply),
                            }
                        )
                    context["batches"].append(
                        {
                            "batch": [interaction_ref.sha256, call_index, batch_index],
                            "items": items,
                            "updates": {key: deepcopy(value) for key, value in active.items()
                                        if key in touched},
                            "removed": tuple(removed),
                        }
                    )
                    touched.clear()
                    removed.clear()

                self._apply(
                    active,
                    call,
                    interaction_ref.sha256,
                    call["call_index"],
                    receipt.execution_id,
                    next(iter(rows.values()))["tool"] if rows else "write",
                    completed_batch=completed,
                    changed_item=changed,
                )
        return contexts

    def verified_calls(self):
        """Actual calls from the latest successful strict lifecycle derivation."""
        if self._verified_calls is None:
            raise IncompleteOrigins("accepted calls require a successful lifecycle derivation")
        return self._verified_calls

    def generation(self, content: Any) -> str:
        """Content plus proven live births; reads and rejected calls are not revisions.

        Call derive for the current accepted history before using this index. A
        proposed repair may supply different content for cache isolation; doing so
        does not prove any new birth or make that content accepted. Empty origins
        preserve legacy content-only identity without inventing a creation event.
        """
        return hashlib.sha256(canonical_json_bytes({
            "content": content,
            "creations": [
                [*key, self.creation_origins[key]] for key in sorted(self.creation_origins)
            ],
            "repair_children": {
                item: {fid: origin.model_dump(mode="json") for fid, origin in sorted(births.items())}
                for item, births in sorted(self.repair_birth_origins.items())
            },
        })).hexdigest()

    def _derive(self, receipt, records, allow_call_prefix):
        turns = {}
        calls = {}
        binding = None
        for ref, record in records:
            _require(isinstance(record, dict))
            if is_call_receipt(record):
                key = (record["interaction_ref"]["sha256"], record["call_index"])
                _require(key not in calls)
                calls[key] = record
            elif "response_ref" in record:
                current = record["binding"]
                _require(current["execution_id"] == receipt.execution_id)
                _require(current["input_ref"] == receipt.input_ref.model_dump(mode="json"))
                _require(isinstance(current.get("scope_sha256"), str))
                binding = current if binding is None else binding
                _require(binding == current)
                iteration = record["iteration"]
                _require(type(iteration) is int and iteration not in turns)
                turns[iteration] = (ref, record)
        _require(bool(turns) and sorted(turns) == list(range(len(turns))))
        _require(
            [turns[i][1]["request_ref"] for i in range(len(turns))]
            == [ref.model_dump(mode="json") for ref in receipt.request_refs]
        )
        active = {}
        verified_calls = []
        action_origins = {}
        consumed = set()
        for iteration in range(len(turns)):
            ref, record = turns[iteration]
            expected = response_calls(
                self._read(ArtifactRef.model_validate(record["response_ref"]))
            )
            actual = sorted(i for sha, i in calls if sha == ref.sha256)
            _require(actual == list(range(len(actual))))
            _require(len(actual) <= len(expected))
            _require(
                len(actual) == len(expected) or (allow_call_prefix and iteration == len(turns) - 1)
            )
            for index in actual:
                key = (ref.sha256, index)
                call = calls[key]
                consumed.add(key)
                _require(call["interaction_ref"] == ref.model_dump(mode="json"))
                _require(call["call_id"] == expected[index].get("call_id"))
                verified_calls.append((ref, call))
                self._apply(
                    active,
                    call,
                    ref.sha256,
                    index,
                    receipt.execution_id,
                    expected[index].get("name"),
                    action_origins,
                )
        _require(consumed == set(calls))
        answer = receipt.raw_model()
        expected_items = {}
        for collection, values in (answer.model_dump(mode="json") if answer else {}).items():
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict) and value.get("item_id"):
                        key = (collection, value["item_id"])
                        _require(key not in expected_items)
                        expected_items[key] = value
        _require({key: row["value"] for key, row in active.items()} == expected_items)
        self._verified_calls = tuple(verified_calls)
        self.action_origins = action_origins
        self.repair_birth_origins = {
            key[1]: row["repair_births"] for key, row in active.items()
            if key[0] == "skeleton_repairs" and row.get("repair_births")
        }
        return {key: row["origin"] for key, row in active.items() if row["origin"] is not None}

    @staticmethod
    def _apply(
        active,
        call,
        sha,
        index,
        execution_id,
        tool_name,
        action_origins=None,
        *,
        completed_batch=None,
        changed_item=None,
    ):
        if "write_results" not in call:
            return
        replies = {row["item"]: row for row in call.get("items", [])}
        _require(len(replies) == len(call.get("items", [])))
        # The displayed rows retain original submission order. Prerequisites and
        # atomic groups can have executed in a different order, recorded here.
        batches = accepted_write_batches(call)
        by_key = {row["item"]: row for row in call["write_results"]}
        ordered = [by_key[key] for batch in batches for key in batch]
        ordered.extend(
            row
            for row in call["write_results"]
            if row["outcome"] not in {"SAVED", "NO_CHANGE", "WITHDRAWN"}
        )
        batch_ends = {batch[-1]: (i, batch) for i, batch in enumerate(batches)}
        seen = set()
        for row in ordered:
            item = row["item"]
            _require(item not in seen)
            seen.add(item)
            _require(row["action_id"] == write_action_id(sha, index, item))
            _require(row["tool"] == tool_name)
            if item in replies:
                _require(row["outcome"] == replies[item]["outcome"])
            if row["outcome"] == "REJECTED":
                continue
            _require(INVESTIGATION_TOOL_KINDS.get(tool_name) == "write")
            reply = replies[item]
            collection = reply["collection"]
            _require(item.split(":")[0] == collection)
            _require(row["outcome"] == reply["outcome"])
            if row["outcome"] == "WITHDRAWN":
                target = json.loads(row["target"])
                removed = withdraw_item_state(active, target)
                if changed_item is not None:
                    changed_item(removed, True)
                if completed_batch is not None and item in batch_ends:
                    completed_batch(*batch_ends[item])
                continue
            _require(row["outcome"] in {"SAVED", "NO_CHANGE"})
            key = (collection, reply["item_id"])
            repair_births = {}
            if collection == "skeleton_repairs":
                previous_row = active.get(key, {})
                previous = previous_row.get("repair_births", {})
                previous_children = {
                    c["fact_id"]
                    for c in previous_row.get("value", {}).get("repair", {}).get("changes", ())
                }
                for change_index, change in enumerate(reply["value"]["repair"]["changes"]):
                    fid = change["fact_id"]
                    if fid in previous:
                        repair_births[fid] = previous[fid]
                    elif row["outcome"] == "SAVED" and fid not in previous_children:
                        repair_births[fid] = FactOrigin(
                            interaction_ref=ArtifactRef.model_validate(call["interaction_ref"]),
                            call_index=index,
                            action_id=row["action_id"],
                            item=item,
                            change_index=change_index,
                        )
            origin = write_item_state(
                active, collection=collection, item_id=key[1],
                location=reply["location"], value=reply["value"],
                new_origin={
                    "execution_id": execution_id, "collection": collection,
                    "item_id": key[1], "creation_action_id": row["action_id"],
                } if row["outcome"] == "SAVED" else None,
                repair_births=repair_births,
            )
            if action_origins is not None and origin is not None:
                action_origins[row["action_id"]] = dict(origin)
            if changed_item is not None:
                changed_item(key, False)
            if completed_batch is not None and item in batch_ends:
                completed_batch(*batch_ends[item])
        _require(set(replies) <= seen)


def bind_creation_origins(
    applications: list[dict[str, Any]], origins: Mapping[tuple[str, str], Mapping[str, str]]
) -> list[dict[str, Any]]:
    """Attach only proven host provenance; leave legacy contributions readable."""
    return [
        {**row, **({"creation_origin": dict(origins[key])} if key in origins else {})}
        for row in applications
        for key in [(row["collection"], row["item_id"])]
    ]


def narrative_creation_keys(
    paper_id: str, origins: Mapping[tuple[str, str], Mapping[str, str]]
) -> dict[str, str]:
    """Same host key for the current preview and sealed narrative publication."""
    return {
        item_id: public_creation_key(paper_id, "NARR", origin)
        for (collection, item_id), origin in origins.items()
        if collection in {"narratives", "findings", "records"}
    }


def expression_creation_keys(
    paper_id: str, origins: Mapping[tuple[str, str], Mapping[str, str]]
) -> dict[str, str]:
    """Accepted item birth, shared by preview and publication for new public objects."""
    return {
        f"{collection}:{item_id}": public_creation_key(paper_id, "EXPR", origin)
        for (collection, item_id), origin in origins.items()
    }
