"""One sealed-record verification shared by final input and independent publication."""
from dataclasses import dataclass
from copy import deepcopy
import json

from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.investigation_kinds import investigation_kind
from tsdaf.workflow.naming_evidence import FrozenNaming, verify_accepted_turn_naming
from tsdaf.lanes.literature.enrichment_models import InvestigationReceipt
from tsdaf.lanes.literature.experimental_records import ExperimentalRecordAnswer
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.investigation_origins import InvestigationOrigins
from tsdaf.workflow.tool_execution import is_call_receipt


@dataclass(frozen=True)
class VerifiedRecordHandoff:
    receipt: InvestigationReceipt
    closure: tuple[ArtifactRef, ...]
    origins: dict
    source_bindings: dict | None = None

    def context(self):
        answer = self.receipt.raw_model()
        used = ({binding['submitted_name'] for bindings in self.source_bindings.values()
                 for binding in bindings if binding['status'] == 'BOUND'}
                if self.source_bindings is not None else
                {sid for record in answer.records for sid in record.selection_ids})
        observations = {self.receipt.selections[sid]['evidence_ref']['observation_id'] for sid in used}
        return {
            'scientific_records': [item.model_dump(mode='json') for item in answer.records],
            'record_status': answer.status,
            'record_explanation': answer.explanation,
            **({'record_source_bindings': self.source_bindings} if self.source_bindings is not None else {}),
            'record_selections': {sid: self.receipt.selections[sid] for sid in sorted(used)},
            'record_observations': [o for o in self.receipt.observations if o['observation_id'] in observations],
        }


def verify_record_handoff(sealed, *, store, runner, sources, baseline_ref):
    receipt = InvestigationReceipt.model_validate_json(read_artifact(store.read_bytes, sealed.receipt_ref))
    if receipt != sealed.receipt or receipt.kind != 'EXPERIMENTAL_RECORDING':
        raise ValueError('record handoff differs from its sealed recording receipt')
    if receipt.paper_id != sources.paper_id or receipt.baseline_machine_ref != baseline_ref:
        raise ValueError('record handoff belongs to a different paper or baseline')
    answer = receipt.raw_model()
    if not isinstance(answer, ExperimentalRecordAnswer) or receipt.failure or receipt.closed_out is not None:
        raise ValueError('record handoff requires an explicit scientific submission, not a host stop')
    closure = tuple(runner.verified_execution_artifact_closure(receipt.execution_id, sealed.terminal_manifest_ref))
    if not {sealed.receipt_ref, receipt.input_ref, baseline_ref} <= set(closure):
        raise ValueError('record handoff lacks its producer input or baseline closure')
    original = json.loads(read_artifact(store.read_bytes, receipt.input_ref))
    if original.get('record_source_version') != receipt.record_source_version:
        raise ValueError('record source capability differs from its sealed input')
    from tsdaf.contracts.literature.schema import machine_profile
    baseline = json.loads(read_artifact(store.read_bytes, baseline_ref))
    if machine_profile(baseline).faithful_mapping and receipt.record_source_version != 1:
        raise ValueError('faithful mapping requires independent record source feedback')
    if original.get('baseline_machine_sha256') != baseline_ref.sha256:
        raise ValueError('record input baseline differs from its receipt')
    identities = {d.registration.document_revision_id: d.identity() for d in sources.documents}
    if any(identity != identities.get(identity['document_revision_id']) for identity in receipt.source_identities):
        raise ValueError('record handoff source revision is stale')
    finished = False
    for ref in receipt.audit_refs:
        if ref not in closure:
            raise ValueError('record audit lies outside producer closure')
        if ref.media_type == 'application/vnd.tsdaf.investigation-interaction+json':
            event = json.loads(read_artifact(store.read_bytes, ref))
            if is_call_receipt(event) and event.get('outcome') == 'FINISHED':
                from tsdaf.workflow.tool_turns import response_calls
                turn_ref = ArtifactRef.model_validate(event['interaction_ref'])
                if turn_ref not in closure:
                    raise ValueError('record finish has no sealed interaction')
                turn = json.loads(read_artifact(store.read_bytes, turn_ref))
                response_ref = ArtifactRef.model_validate(turn['response_ref'])
                if response_ref not in closure:
                    raise ValueError('record finish has no sealed model response')
                calls = response_calls(read_artifact(store.read_bytes, response_ref))
                call = calls[event['call_index']]
                if call.get('name') != 'finish_investigation' or call.get('call_id') != event.get('call_id'):
                    raise ValueError('record finish does not correspond to a model finish call')
                from tsdaf.workflow.source_investigation import restore_investigation_domain
                naming = verify_accepted_turn_naming(store.read_bytes, turn, closure,
                    expected_binding=turn['binding'], expected_iteration=turn['iteration'])
                if naming is None:
                    raise ValueError('record finish lacks its accepted naming view')
                raw = FrozenNaming.from_snapshot(naming, restore_investigation_domain).resolve(decode_tool_arguments(call['arguments']))
                finish = investigation_kind(receipt.kind).finish_model().model_validate_json(canonical_json_bytes(raw))
                if any(receipt.raw_answer.get(k) != v for k,v in finish.model_dump(exclude={'disposition'}).items()):
                    raise ValueError('record scientific status or explanation differs from the model finish')
                if event.get('note') != finish.explanation or event['delivery'].get('disposition') != finish.disposition:
                    raise ValueError('record delivery differs from the model finish')
                result_ref = ArtifactRef.model_validate(event['delivery']['result_ref'])
                if result_ref not in closure or json.loads(read_artifact(store.read_bytes, result_ref)) != receipt.raw_answer:
                    raise ValueError('record finish does not seal the latest scientific account')
                finished = True
    if not finished:
        raise ValueError('record handoff has no sealed model finish')
    from tsdaf.lanes.literature.source_investigation import InvestigationSourceReader
    def forbid_seal(*args):
        raise AssertionError("record verification must not create source evidence")
    reader = InvestigationSourceReader(kind=receipt.kind, sources=sources, execution_id=receipt.execution_id,
        input_ref=receipt.input_ref,
        prompt_ref=next(r for r in closure if r.media_type == "text/vnd.tsdaf.investigation-prompt+markdown"),
        schema_ref=next(r for r in closure if r.media_type == "application/vnd.tsdaf.investigation-tools+json"),
        seal=forbid_seal, store=store, allowed_main_visual_ids=())
    observations = {o['observation_id']: o for o in receipt.observations}
    by_hash = {ref.sha256: ref for ref in closure}
    bindings = None
    if receipt.record_source_version == 1:
        from tsdaf.workflow.record_source_feedback import verified_record_bindings
        from tsdaf.workflow.source_investigation import restore_investigation_domain
        bindings = verified_record_bindings(receipt, read_bytes=store.read_bytes,
            accepted_refs=closure, restore_domain=restore_investigation_domain)
        used = {b['submitted_name'] for values in bindings.values() for b in values if b['status'] == 'BOUND'}
    else:
        used = {sid for record in answer.records for sid in record.selection_ids}
    for sid in used:
        binding = receipt.selections.get(sid)
        if binding is None:
            raise ValueError('record cites an unbound selection')
        identity = binding['source_identity']
        doc = sources.document(identity['document_revision_id'])
        if doc.package is None or identity['package_sha256'] != doc.package.canonical_content_sha256:
            raise ValueError('record selection belongs to a different source package')
        evidence = binding['evidence_ref']
        ref = by_hash.get(evidence['source_observation_sha256'])
        if ref is None or json.loads(read_artifact(store.read_bytes, ref)) != observations.get(evidence['observation_id']):
            raise ValueError('record selection lacks its immutable original observation')
        reader.verify_sealed_selection(deepcopy(binding), observations[evidence['observation_id']])
    origins = InvestigationOrigins(store.read_bytes).derive(receipt, strict=True)
    if any(('records', item.item_id) not in origins for item in answer.records):
        raise ValueError('record lacks a verified birth action')
    return VerifiedRecordHandoff(receipt, closure, origins, bindings)
