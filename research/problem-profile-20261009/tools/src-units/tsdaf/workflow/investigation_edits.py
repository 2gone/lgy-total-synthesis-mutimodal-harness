"""Exact recorded edit bases and pre-validation expansion for investigation writes."""

from __future__ import annotations

import json
from copy import deepcopy

from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.local_edits import (
    edit_model,
    edited_semantics,
    merge_edit,
    restore_semantic_state,
    semantic_state,
)
from tsdaf.lanes.literature.model_names import NAMING_VERSION
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.naming_evidence import FrozenNaming, verify_accepted_turn_naming
from tsdaf.workflow.tool_execution import PreparedWrite, is_call_receipt
from tsdaf.workflow.tool_turns import response_calls
from tsdaf.workflow.write_delivery import covers_target, write_action_id

EFFECTIVE_WRITE_MEDIA = "application/vnd.tsdaf.investigation-effective-write+json"
EFFECTIVE_WRITE_VERSION = 1


def read_effective(read_bytes, call, *, interaction_sha256, call_index, accepted=None):
    """An advertised derived body is mandatory, bound to this one immutable call."""
    if call.get("effective_write_version") != EFFECTIVE_WRITE_VERSION:
        raise ValueError("effective write format is missing or unsupported")
    ref = ArtifactRef.model_validate(call["effective_body_ref"])
    if accepted is not None and ref not in accepted:
        raise ValueError("effective write is outside the accepted artifact closure")
    if ref.media_type != EFFECTIVE_WRITE_MEDIA:
        raise ValueError("effective write has the wrong media type")
    body = json.loads(read_artifact(read_bytes, ref))
    if (body.get("version"), body.get("interaction_sha256"), body.get("call_index")) != (
        EFFECTIVE_WRITE_VERSION,
        interaction_sha256,
        call_index,
    ):
        raise ValueError("effective write belongs to another call")
    candidates, errors = body.get("candidates"), body.get("errors")
    if (
        not isinstance(candidates, dict)
        or not isinstance(errors, dict)
        or candidates.keys() & errors.keys()
    ):
        raise ValueError("effective write has an invalid candidate index")
    row_keys = {r["item"] for r in call.get("write_results", ())}
    if not (candidates.keys() | errors.keys()) <= row_keys:
        raise ValueError("effective write contains items outside its call receipt")
    for row in call.get("write_results", ()):
        if row["outcome"] in {"SAVED", "NO_CHANGE", "WITHDRAWN"} and row["item"] not in candidates:
            raise ValueError("accepted edit call item has no effective candidate")
    if any(isinstance(v, dict) and "edit" in v for v in candidates.values()):
        raise ValueError("effective write contains an unexpanded wrapper")
    return body


def verify_effective_history(read_bytes, records, *, accepted):
    """New receipt markers are binding; legacy unknown input keys are not markers."""
    for _, call in records:
        if isinstance(call, dict) and (
            "effective_write_version" in call or "effective_body_ref" in call
        ):
            read_effective(
                read_bytes,
                call,
                interaction_sha256=call["interaction_ref"]["sha256"],
                call_index=call["call_index"],
                accepted=accepted,
            )


class InvestigationEdits:
    """Disposable indexes over the existing accepted audit, not another write ledger."""

    def __init__(self, *, read_bytes, audits, accepted, binding, restore_domain):
        self.read_bytes = read_bytes
        self.audits = audits
        self.accepted = accepted
        self.binding = binding
        self.restore_domain = restore_domain
        self._records = {}
        self._actions = {}
        self._parsed = set()
        self._canonical = {}

    def record(self, ref):
        if ref not in self._records:
            self._records[ref] = json.loads(read_artifact(self.read_bytes, ref))
        return self._records[ref]

    def index(self):
        for ref in self.audits():
            if (
                ref in self._parsed
                or ref.media_type != "application/vnd.tsdaf.investigation-interaction+json"
            ):
                continue
            record = self.record(ref)
            if is_call_receipt(record):
                for row in record.get("write_results", ()):
                    action = row["action_id"]
                    if action in self._actions:
                        raise ValueError("ambiguous action history")
                    self._actions[action] = (record, row)
            self._parsed.add(ref)

    def action(self, action_id, collection):
        self.index()
        if action_id not in self._actions:
            raise ValueError("unknown edit action; use a supplied action or submit the full item")
        call, row = self._actions[action_id]
        if not isinstance(row.get("item"), str) or row["item"].split(":")[0] != collection:
            raise ValueError("edit action must belong to this collection")
        turn_ref = ArtifactRef.model_validate(call["interaction_ref"])
        if turn_ref not in set(self.audits()):
            raise ValueError("edit action has no accepted interaction")
        turn = {**self.record(turn_ref), "interaction_ref": turn_ref.model_dump(mode="json")}
        index = call["call_index"]
        if type(index) is not int or index < 0:
            raise ValueError("invalid edit call index")
        calls = response_calls(
            read_artifact(self.read_bytes, ArtifactRef.model_validate(turn["response_ref"]))
        )
        if index >= len(calls):
            raise ValueError("edit action is outside its response")
        original = calls[index]
        if call["call_id"] != original.get("call_id") or row["tool"] != original.get("name"):
            raise ValueError("edit action does not identify its original call")
        if row["action_id"] != write_action_id(turn_ref.sha256, index, row["item"]):
            raise ValueError("edit action identity mismatch")
        if turn["binding"] != self.binding():
            raise ValueError("edit base belongs to another task")
        return call, row, turn, original

    def canonical(self, action_id, collection):
        call, row, turn, original = self.action(action_id, collection)
        if action_id in self._canonical:
            return deepcopy(self._canonical[action_id])
        arguments = original.get("arguments")
        if isinstance(arguments, str):
            arguments = decode_tool_arguments(arguments)
        index = int(row["item"].split(":")[1])
        raw = arguments[collection][index]
        if isinstance(raw, dict) and "edit" in raw:
            if "effective_body_ref" not in call:
                raise ValueError("edit history lacks its effective body; submit the full item")
            body = read_effective(
                self.read_bytes,
                call,
                interaction_sha256=turn["interaction_ref"]["sha256"],
                call_index=call["call_index"],
                accepted=self.accepted(),
            )
            if row["item"] not in body["candidates"]:
                raise ValueError(
                    "that edit never produced a complete candidate; submit the full item"
                )
            candidate = body["candidates"][row["item"]]
            state = body.get("candidate_semantics", {}).get(row["item"])
        else:
            snapshot = verify_accepted_turn_naming(
                self.read_bytes,
                turn,
                tuple(self.accepted()),
                expected_naming_version=NAMING_VERSION,
                expected_binding=self.binding(),
                expected_iteration=turn["iteration"],
            )
            if snapshot is None:
                raise ValueError(
                    "historical names cannot be proved; read the proposal and submit a full item"
                )
            candidate = FrozenNaming.from_snapshot(snapshot, self.restore_domain).resolve(raw)
            state = None
        if not isinstance(candidate, dict):
            raise ValueError("base is not an object; submit the full item")
        result = (candidate, state)
        self._canonical[action_id] = deepcopy(result)
        return result

    def restore_delivery(self, delivery):
        """Verify new derived evidence before it can serve as a restored comparison/base."""
        self.index()
        checked = set()
        for action, (call, row) in self._actions.items():
            if not isinstance(row.get("item"), str):
                continue
            collection = row["item"].split(":")[0]
            _, _, turn, original = self.action(action, collection)
            call_key = (turn["interaction_ref"]["sha256"], call["call_index"])
            if call_key not in checked:
                if "effective_write_version" in call or "effective_body_ref" in call:
                    read_effective(
                        self.read_bytes,
                        call,
                        interaction_sha256=call_key[0],
                        call_index=call_key[1],
                        accepted=self.accepted(),
                    )
                checked.add(call_key)
            if row["outcome"] == "REJECTED":
                try:
                    proposal, _ = self.canonical(action, collection)
                except ValueError:
                    # Unexpandable/legacy rejected wrappers remain readable, not edit bases.
                    if 'effective_write_version' in call:
                        delivery.forget_candidate(action)
                    continue
                delivery.remember_candidate(action, proposal)

    def restore_current(self, writer):
        """JSON values and host semantic evidence are restored only when they agree."""
        from tsdaf.lanes.literature.local_edits import supports_edit
        editable = {key for key, (model, _) in writer.collections.items() if supports_edit(model)}
        self.index()
        latest = {}
        for call, row in self._actions.values():
            if row["outcome"] in {"SAVED", "NO_CHANGE"}:
                reply = next((r for r in call.get("items", ()) if r["item"] == row["item"]), None)
                if reply and reply["collection"] in editable:
                    latest[reply["collection"], reply["item_id"]] = (reply, call, row)
        answer = writer.current().model_copy(deep=True)
        for collection in editable:
            for item in getattr(answer, collection, ()):
                record = latest.get((collection, item.item_id))
                if record is None:
                    continue
                reply, call, row = record
                state = call.get("item_semantics", {}).get(row["item"])
                if state is not None:
                    if reply["value"] != item.model_dump(mode="json"):
                        raise ValueError(
                            "restored value disagrees with its recorded semantic evidence"
                        )
                    restore_semantic_state(item, state)
        writer.answer = answer

    def prepare(self, raw, ctx, *, writer, delivery, origins, action_origins, seal, register):
        if not isinstance(raw, dict):
            return None
        edits = [
            (c, i, v)
            for c, values in raw.items()
            if c in writer.write_collections and isinstance(values, list)
            for i, v in enumerate(values)
            if isinstance(v, dict) and "edit" in v
        ]
        if not edits:
            return None
        body = deepcopy(raw)
        errors, before, choices, states = {}, {}, {}, {}
        for collection, index, wrapper in edits:
            key = f"{collection}:{index}"
            base = None
            try:
                from tsdaf.lanes.literature.local_edits import supports_edit
                model = writer.write_collections[collection][0]
                if not supports_edit(model):
                    raise ValueError("local edits are not offered for this collection")
                # Locate an explicit, verified action before validating its changes.
                # A malformed correction still belongs to its connected write group.
                payload = wrapper.get("edit")
                action = payload.get("action_id") if isinstance(payload, dict) else None
                if not isinstance(action, str):
                    raise ValueError("edit requires a supplied action name")
                call, row, turn, _ = self.action(action, collection)
                state = None
                if row["outcome"] == "REJECTED":
                    base, state = self.canonical(action, collection)
                elif row["outcome"] in {"SAVED", "NO_CHANGE"}:
                    reply = next(r for r in call["items"] if r["item"] == row["item"])
                    base = deepcopy(reply["value"])
                entry = edit_model(model).model_validate_json(canonical_json_bytes(wrapper)).edit
                changes = entry.changes.model_dump(mode="json", exclude_unset=True)
                if row["outcome"] == "REJECTED":
                    if action not in {r["action_id"] for r in delivery.pending()}:
                        raise ValueError(
                            "rejected edit base is already resolved; use its current saved action"
                        )
                    target = writer.targets("write", {collection: [base]}).get(f"{collection}:0")
                    recorded_target = json.loads(row["target"]) if row.get("target") else {}
                    historical_locations = {
                        reply["location"]: reply["item_id"]
                        for previous_call, _ in self._actions.values()
                        for reply in previous_call.get("items", ())
                        if reply.get("outcome") in {"SAVED", "NO_CHANGE"}
                    }
                    after = False
                    for later_action, (later_call, later) in self._actions.items():
                        if later_action == action:
                            after = True
                            continue
                        if not after or later["outcome"] not in {"SAVED", "NO_CHANGE", "WITHDRAWN"}:
                            continue
                        if later["outcome"] == "WITHDRAWN":
                            retired = json.loads(later["target"]) if later.get("target") else {}
                            retired_id = retired.get("item_id") or historical_locations.get(
                                retired.get("location")
                            )
                            base_id = base.get("item_id") or recorded_target.get("item_id")
                            if base_id and retired_id == base_id:
                                raise ValueError("a later withdrawal supersedes this rejected base")
                        later_reply = next(
                            (
                                r
                                for r in later_call.get("items", ())
                                if r.get("item") == later["item"]
                            ),
                            {},
                        )
                        if later_reply.get("collection") == collection and "value" in later_reply:
                            later_value = {
                                k: v for k, v in later_reply["value"].items() if k != "item_id"
                            }
                            later_target = writer.targets("write", {collection: [later_value]}).get(
                                f"{collection}:0"
                            )
                            if target is not None and covers_target(later_target, target):
                                raise ValueError(
                                    "a later accepted write supersedes this rejected base"
                                )
                    if state is not None:
                        states[key] = state
                elif row["outcome"] in {"SAVED", "NO_CHANGE"}:
                    current = next(
                        (
                            f
                            for f in getattr(writer.current(), collection)
                            if f.item_id == reply["item_id"]
                        ),
                        None,
                    )
                    if current is None or current.model_dump(mode="json") != reply["value"]:
                        raise ValueError(
                            "saved edit base is stale; read current and use its action"
                        )
                    origin = origins.get((collection, current.item_id))
                    if origin is None or action_origins.get(action) != origin:
                        raise ValueError("saved base creation cannot be proved or has changed")
                    state = call.get("item_semantics", {}).get(row["item"])
                    if state is None or state != semantic_state(current):
                        raise ValueError(
                            "saved base field presence cannot be proved; submit a full item"
                        )
                    base = current.model_dump(mode="json", exclude_unset=True)
                    states[key] = state
                else:
                    raise ValueError("edit requires a saved or pending rejected proposal")
                # Retain the identified base on preparation failure for dependency grouping.
                body[collection][index] = deepcopy(base)
                candidate = merge_edit(model, base, changes)
                body[collection][index] = candidate
                if key in states:
                    states[key] = edited_semantics(states[key], changes)
                before[action] = deepcopy(base)
                choices[key] = dict(
                    action_id=action,
                    outcome=row["outcome"],
                    interaction_ref=turn["interaction_ref"],
                    call_index=call["call_index"],
                    item=row["item"],
                    naming_ref=turn.get("naming_ref"),
                )
            except (ValueError, KeyError, TypeError, StopIteration) as error:
                errors[key] = str(error) or "edit base cannot be verified; submit the full item"
                if base is not None:
                    body[collection][index] = deepcopy(base)
        targets = writer.targets("write", body)
        touched = {}
        edit_keys = {f"{c}:{i}" for c, i, _ in edits}
        for key, target in targets.items():
            if target is not None:
                # Location-only withdrawals and handle-based edits name one saved
                # object. Compare that exact saved identity, not address spellings.
                target_value = json.loads(target)
                if target_value.get("location"):
                    saved = next(
                        (r for r in writer.findings() if r["location"] == target_value["location"]),
                        None,
                    )
                    if saved and target_value.get("item_id", saved["item_id"]) == saved["item_id"]:
                        target_value = {"item_id": saved["item_id"]}
                target_key = canonical_json_bytes(target_value)
                touched.setdefault(target_key, []).append(key)
        for keys in touched.values():
            if len(keys) > 1 and edit_keys.intersection(keys):
                for key in keys:
                    errors[key] = (
                        "same target submitted more than once with edit; send one correction"
                    )
        declared = []
        for repair in body.get("repairs", ()) if isinstance(body.get("repairs", []), list) else ():
            if repair not in declared:
                declared.append(repair)
        for key, choice in choices.items():
            if key in errors or choice["outcome"] != "REJECTED":
                continue
            repair = {"action_id": choice["action_id"], "item": key}
            conflicts = [
                r
                for r in declared
                if isinstance(r, dict) and r.get("action_id") == repair["action_id"] and r != repair
            ]
            if conflicts:
                errors[key] = "edit and repairs disagree about the replacement item"
            elif repair not in declared:
                declared.append(repair)
        if isinstance(body.get("repairs", []), list):
            body["repairs"] = declared
        candidates = {
            f"{c}:{i}": v
            for c, values in body.items()
            if c in writer.write_collections and isinstance(values, list)
            for i, v in enumerate(values)
            if f"{c}:{i}" not in errors
        }
        artifact = {
            "version": EFFECTIVE_WRITE_VERSION,
            "interaction_sha256": ctx.interaction_sha256,
            "call_index": ctx.call_index,
            "candidates": candidates,
            "errors": errors,
            "bases": choices,
            "candidate_semantics": states,
        }
        # Sealing failure aborts before any scientific write or resolution.
        try:
            ref = seal(artifact, EFFECTIVE_WRITE_MEDIA)
            root = register(
                artifact,
                origin="EXECUTION_RECORD",
                ref=ref,
                source_navigation="expanded write candidates",
            )
        except Exception as error:
            raise RuntimeError(
                "unable to seal and register the effective write; no item applied"
            ) from error
        reads = {key: {"location": root + "#/candidates/" + key} for key in candidates}
        return PreparedWrite(
            body=body,
            errors=errors,
            comparison_before=before,
            item_reads=reads,
            candidate_semantics=states,
            audit={
                "effective_body_ref": ref.model_dump(mode="json"),
                "effective_write_version": EFFECTIVE_WRITE_VERSION,
            },
        )
