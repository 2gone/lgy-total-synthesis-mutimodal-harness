"""Source links fixed by accepted record writes, independent of scientific edit state.

The existing call transcript is the authority. Runtime reads and sealed handoff
use the same verified fold; no binding is reconstructed using later read access.
"""
from copy import deepcopy
import json

from tsdaf.contracts.literature.record_provenance import RecordSourceBinding
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.investigation_edits import InvestigationEdits
from tsdaf.workflow.investigation_origins import (
    InvestigationOrigins, accepted_write_batches, delivered_selections_in,
)
from tsdaf.workflow.write_delivery import submitted_item, write_feedback_items


def sources_submitted(arguments, item):
    proposal = submitted_item(arguments, item)
    if isinstance(proposal, dict) and "edit" in proposal:
        proposal = proposal["edit"].get("changes", {})
    return isinstance(proposal, dict) and "selection_ids" in proposal


def bind_record_sources(names, *, selections, authorized):
    """Bind only explicit proposals to already delivered immutable selections."""
    result = []
    for name in names:
        source = selections.get(name)
        bound = source is not None and name in authorized
        result.append(RecordSourceBinding(
            submitted_name=name, status="BOUND" if bound else "UNBOUND",
            evidence_refs=(source["evidence_ref"],) if bound else (),
            error_code=None if bound else "SOURCE_NOT_DELIVERED",
        ).model_dump(mode="json"))
    return result


def source_binding_view(bindings):
    """Readable link results, not the private observation/hash transportation."""
    return [{
        'submitted_name': binding['submitted_name'], 'status': binding['status'],
        **({'location': 'selection:' + binding['submitted_name']}
           if binding['status'] == 'BOUND' else {
               'error_code': binding['error_code'],
               'explanation': 'This source was not delivered when saved; the account is retained.',
           }),
    } for binding in bindings]


def record_binding_view(node):
    if isinstance(node, list):
        return [record_binding_view(item) for item in node]
    if not isinstance(node, dict) or node.get('type') == 'function_call':
        return node
    # Scientific payloads and submitted model arguments are opaque here.
    result = {key: value if key in {"value", "arguments", "submitted"}
              else record_binding_view(value) for key, value in node.items()}
    if (node.get('source_binding_version') == 1 and node.get('collection') == 'records'
            and 'item_id' in node and 'source_bindings' in node):
        result['source_bindings'] = source_binding_view(node['source_bindings'])
        result.pop('source_binding_version', None)
    if ('record_source_bindings' in node and
            ('saved' in node or 'scientific_records' in node)):
        result['record_source_bindings'] = {
            key: source_binding_view(value)
            for key, value in node['record_source_bindings'].items()
        }
    return result


def project_record_binding_feedback(visible):
    """Project only known host envelopes at the final model-view seam.

    Accepted receipts and function-call arguments stay intact. Prose is never
    parsed for keys; a model may freely discuss any of these field names.
    """

    for index, step in enumerate(visible):
        if step.get('type') not in {'function_result', 'user_input'}:
            continue
        field = 'result' if step['type'] == 'function_result' else 'content'
        parts = []
        for part in step.get(field, ()):
            parts.append(part)
            if part.get('type') != 'text' or not isinstance(part.get('text'), str):
                continue
            try:
                value = json.loads(part['text'])
            except ValueError:
                continue
            changed = record_binding_view(value)
            if changed != value:
                parts[-1] = {**part, 'text': canonical_json_bytes(changed).decode()}
        # Projection can share nested objects with the recorded history. Replace
        # only the visible envelope; never mutate an original history part.
        visible[index] = {**step, field: parts}


def verified_record_bindings(receipt, *, read_bytes, accepted_refs, restore_domain,
                             allow_call_prefix=False):
    """Verify accepted calls and fold only their record-source results.

    Returns a disposable index by active record identity. The source state is not
    part of the model's semantic_state or its saved-base equality check.
    """
    original = json.loads(read_artifact(read_bytes, receipt.input_ref))
    if receipt.record_source_version != 1 or original.get("record_source_version") != 1:
        raise ValueError("record source feedback requires its explicit input capability")
    answer = receipt.raw_model()
    if not receipt.request_refs:
        if answer is not None and answer.records:
            raise ValueError("saved records have no accepted source history")
        return {}
    index = InvestigationOrigins(read_bytes)
    origins = index.derive(receipt, strict=True, allow_call_prefix=allow_call_prefix)
    calls = index.verified_calls()
    closure = tuple(accepted_refs)
    turns = {}
    for ref in dict.fromkeys(receipt.audit_refs):
        if ref.media_type != "application/vnd.tsdaf.investigation-interaction+json":
            continue
        record = json.loads(read_artifact(read_bytes, ref))
        if "response_ref" not in record:
            continue
        if ref not in closure:
            raise ValueError("record source interaction is outside accepted closure")
        turns[ref] = record
    # Delivery deltas belong to every verified response, including one without tools.
    binding = next(iter(turns.values()))["binding"] if turns else None
    edits = InvestigationEdits(read_bytes=read_bytes, audits=lambda: receipt.audit_refs,
        accepted=lambda: closure, binding=lambda: binding, restore_domain=restore_domain)
    authorization, delivered = {}, set()
    for ref, turn in sorted(turns.items(), key=lambda pair: pair[1]["iteration"]):
        delivered |= delivered_selections_in(turn)
        authorization[ref] = set(delivered)
    active = {}
    for turn_ref, call in calls:
        if "write_results" not in call:
            continue
        rows = {row["item"]: row for row in write_feedback_items(call)}
        for batch in accepted_write_batches(call, required=True):
            touched = {}
            for key in batch:
                row = rows[key]
                collection = key.split(":")[0]
                if collection not in {"records", "withdrawals"}:
                    raise ValueError("unexpected collection in recording source history")
                submitted, _ = edits.canonical(row["action_id"], collection)
                if collection == "withdrawals":
                    target = json.loads(row["target"])
                    matches = [handle for handle, value in active.items()
                        if (target.get("item_id") is None or handle == target["item_id"])
                        and (target.get("location") is None or value["location"] == target["location"])]
                    if len(matches) != 1 or any(submitted.get(k) != target.get(k)
                                               for k in ("item_id", "location")):
                        raise ValueError("record source withdrawal lacks its actual target")
                    del active[matches[0]]
                    continue
                _, _, _, original_call = edits.action(row["action_id"], collection)
                explicit = sources_submitted(original_call["arguments"], key)
                handle = row["item_id"]
                previous = active.get(handle)
                names = submitted.get("selection_ids", ()) if explicit or previous is None else (
                    binding["submitted_name"] for binding in previous["source_bindings"])
                names = [name.removeprefix("selection:") for name in names]
                if explicit or previous is None:
                    expected = bind_record_sources(names, selections=receipt.selections,
                                                   authorized=authorization[turn_ref])
                else:
                    expected = previous["source_bindings"]
                if row.get("source_binding_version") != 1 or row.get("source_bindings") != expected:
                    raise ValueError("record source feedback differs from its accepted proposal/history")
                for value in expected:
                    RecordSourceBinding.model_validate_json(canonical_json_bytes(value))
                birth = previous["birth"] if previous else row["action_id"]
                active[handle] = {"birth": birth, "location": row["location"],
                                  "source_bindings": deepcopy(expected)}
                touched[handle] = row
            for handle, row in touched.items():
                if handle in active and list(row["value"]["selection_ids"]) != [
                    b["submitted_name"] for b in active[handle]["source_bindings"]]:
                    raise ValueError("saved record sources differ from the accepted source proposals")
    expected_handles = {r.item_id for r in answer.records} if answer is not None else set()
    if set(active) != expected_handles or any(
        origins[("records", handle)]["creation_action_id"] != value["birth"]
        for handle, value in active.items()
    ):
        raise ValueError("record source history differs from the active record births")
    return {handle: value["source_bindings"] for handle, value in active.items()}
