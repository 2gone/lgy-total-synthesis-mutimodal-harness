"""Request-bound measurement comparisons, derived from the existing write history.

Snapshots are private comparison inputs, never apply baselines or contributions.
The same decoder serves live restoration and independent publication.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy

from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts.source import ArtifactRef
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.investigation_origins import (
    INVESTIGATION_INTERACTION_MEDIA,
    accepted_write_batches,
    withdraw_item_state,
    write_item_state,
)
from tsdaf.workflow.naming_evidence import verify_accepted_turn_naming
from tsdaf.workflow.tool_turns import TOOL_REQUEST_INTENT_MEDIA, response_calls

MEASUREMENT_SNAPSHOT_MEDIA = "application/vnd.tsdaf.reading-comparison+json"


def selected_binding(value, previous, submitted, request_binding, *, current_updates=False, machine=None):
    """The single capability/transition rule used by writing and history decoding.

    No request snapshot means the established legacy path. A missing carrier is
    the canonical representation of no comparison, including explicit new/unknown.
    """
    if current_updates:
        from tsdaf.lanes.literature.finding_updates import update_binding
        return update_binding(value, previous, submitted, request_binding, machine)
    if request_binding is None or value.get("aspect") != "yield":
        return None
    changes = submitted.get("edit", {}).get("changes", {}) if "edit" in submitted else submitted
    changed_words = changes.get("words", {})
    explicit = isinstance(changed_words, Mapping) and "measurement" in changed_words
    if not explicit and previous is not None:
        return deepcopy(previous)
    words = value.get("words", {})
    if (
        not isinstance(words, Mapping)
        or not isinstance(words.get("measurement"), Mapping)
        or not words["measurement"].get("same_as")
    ):
        return None
    # Request authority is still verified for this action. Identical scientific
    # comparison content does not create a revision merely because a call advanced.
    if (
        previous is not None
        and request_binding is not None
        and (previous["snapshot_ref"] == request_binding["snapshot_ref"])
    ):
        return deepcopy(previous)
    return deepcopy(request_binding)


class MeasurementContexts:
    """A bounded task's immutable blobs; no cached authorization decisions."""

    def __init__(self, read_bytes):
        self.read_bytes = read_bytes
        self._values = {}

    def record(self, ref):
        ref = ArtifactRef.model_validate(ref) if isinstance(ref, Mapping) else ref
        if ref not in self._values:
            self._values[ref] = json.loads(read_artifact(self.read_bytes, ref))
        return self._values[ref]

    def request(self, turn, closure):
        refs = set(closure)
        intent_ref = ArtifactRef.model_validate(turn["intent_ref"])
        if intent_ref not in refs or intent_ref.media_type != TOOL_REQUEST_INTENT_MEDIA:
            raise ValueError("measurement comparison intent is outside the accepted closure")
        intent = self.record(intent_ref)
        if any(intent.get(k) != turn.get(k) for k in ("binding", "iteration", "request_ref")):
            raise ValueError("measurement comparison belongs to another request")
        ref = intent.get("science_snapshot_ref")
        if ref is None:
            return None
        artifact = ArtifactRef.model_validate(ref)
        if artifact not in refs or artifact.media_type != MEASUREMENT_SNAPSHOT_MEDIA:
            raise ValueError("measurement comparison snapshot is outside the accepted closure")
        self.record(artifact)
        return {"snapshot_ref": ref, "request_ref": turn["request_ref"]}

    def machines(self, bindings, closure):
        refs = set(closure)
        result = {}
        for item, binding in bindings.items():
            if binding is None:
                continue
            ref = ArtifactRef.model_validate(binding["snapshot_ref"])
            if ref not in refs or ref.media_type != MEASUREMENT_SNAPSHOT_MEDIA:
                raise ValueError("measurement comparison snapshot is not authorized")
            result[item] = self.record(ref)
            if "resolved_intent" in binding:
                result[item] = {**result[item], "_finding_binding": deepcopy(binding)}
        return result

    def bindings(self, receipt, *, origins, closure, restore_domain):
        """Verify the original actions and their bindings, not a final-value heuristic."""
        if origins._verified_calls is None:
            # Only receipts predating this private capability retain the legacy path.
            for ref in receipt.audit_refs:
                if ref.media_type != INVESTIGATION_INTERACTION_MEDIA:
                    continue
                turn = self.record(ref)
                intent = turn.get("intent_ref")
                if (
                    intent is not None
                    and self.record(intent).get("science_snapshot_ref") is not None
                ):
                    raise ValueError("measurement comparison requires verified accepted history")
            return {}
        from tsdaf.workflow.investigation_edits import InvestigationEdits

        from tsdaf.lanes.literature.middle_layer import (
            CandidateFindingV2, UnifiedReadingAnswerV2, inherit_finding_scope,
        )
        from tsdaf.lanes.literature.local_edits import restore_semantic_state
        current_updates = isinstance(receipt.raw_model(), UnifiedReadingAnswerV2)
        typed_active = {}
        calls = origins._verified_calls
        active, bindings = {}, {}
        turns = {}
        for interaction, call in calls:
            if interaction not in turns:
                turn = {
                    **self.record(interaction),
                    "interaction_ref": interaction.model_dump(mode="json"),
                }
                if turn.get("intent_ref") is None:
                    turns[interaction] = (turn, None)
                else:
                    verify_accepted_turn_naming(
                        self.read_bytes,
                        turn,
                        closure,
                        expected_binding=turn["binding"],
                        expected_iteration=turn["iteration"],
                    )
                    turns[interaction] = (turn, self.request(turn, closure))
            turn, request_binding = turns[interaction]
            if "write_results" not in call:
                if call.get("measurement_bindings"):
                    raise ValueError("comparison binding has no accepted write row")
                continue
            batches = accepted_write_batches(call)
            if not batches:
                if call.get("measurement_bindings"):
                    raise ValueError("comparison binding has no accepted write row")
                continue
            commands = response_calls(
                read_artifact(self.read_bytes, ArtifactRef.model_validate(turn["response_ref"]))
            )
            raw = commands[call["call_index"]]["arguments"]
            if isinstance(raw, str):
                raw = decode_tool_arguments(raw)
            rows = {v["item"]: v for v in call["items"]}
            delivered_bindings = call.get("measurement_bindings", {})
            edits = InvestigationEdits(
                read_bytes=self.read_bytes,
                audits=lambda: receipt.audit_refs,
                accepted=lambda: closure,
                binding=lambda binding=turn["binding"]: binding,
                restore_domain=restore_domain,
            )
            for batch in batches:
                for key in batch:
                    row = rows[key]
                    collection, index = key.split(":")
                    if row["outcome"] == "WITHDRAWN":
                        written = next(r for r in call["write_results"] if r["item"] == key)
                        removed = withdraw_item_state(active, json.loads(written["target"]))
                        bindings.pop(removed, None)
                        typed_active.pop(removed, None)
                        continue
                    target = (collection, row["item_id"])
                    if collection == "findings":
                        # Legacy requests never enabled this capability. Do not force
                        # their historical actions through the newer edit decoder.
                        proposed = {}
                        if request_binding is not None:
                            action = next(
                                r["action_id"] for r in call["write_results"] if r["item"] == key
                            )
                            proposed, semantics = edits.canonical(action, collection)
                        previous = bindings.get(target)
                        if current_updates:
                            item = CandidateFindingV2.model_validate_json(json.dumps(proposed))
                            prior_item = typed_active.get(target)
                            if prior_item is not None:
                                item = inherit_finding_scope(item, prior_item, CandidateFindingV2)
                                previous = previous or {"target": None, "resolved_intent": "RECORD"}
                            if semantics is not None:
                                restore_semantic_state(item, semantics, exact=False)
                            from tsdaf.lanes.literature.finding_updates import inherit_measurement_context
                            item = inherit_measurement_context(item, prior_item)
                            typed_active[target] = item
                            proposed = item.model_dump(mode="json")
                        expected = selected_binding(
                            proposed,
                            previous,
                            raw[collection][int(index)],
                            request_binding,
                            current_updates=current_updates,
                            machine=self.record(request_binding["snapshot_ref"])
                            if current_updates and request_binding else None,
                        )
                        if delivered_bindings.get(key) != expected:
                            raise ValueError(
                                "accepted measurement comparison differs from its "
                                "original request selection"
                            )
                        bindings[target] = expected
                    write_item_state(
                        active,
                        collection=collection,
                        item_id=row["item_id"],
                        location=row["location"],
                        value=row["value"],
                        new_origin=None,
                    )
                # Atomic groups publish their final wrapper once; per-action bindings above
                # remain distinct, and only the final surviving action owns its comparison.
            allowed_bindings = {
                key
                for batch in batches
                for key in batch
                if key.startswith("findings:")
            }
            if set(delivered_bindings) - allowed_bindings:
                raise ValueError("comparison binding has no accepted write row")
        actual = {f.item_id for f in receipt.raw_model().findings}
        result = {
            key[1]: value
            for key, value in bindings.items()
            if key[0] == "findings" and key[1] in actual
        }
        return result

    def restore(self, receipt, *, origins, closure, restore_domain):
        bindings = self.bindings(
            receipt, origins=origins, closure=closure, restore_domain=restore_domain
        )
        return self.machines(bindings, closure)
