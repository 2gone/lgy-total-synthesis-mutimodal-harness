"""Freeze verified model accounts only after their source IDs become public."""
import hashlib
from dataclasses import dataclass

from tsdaf.contracts.literature.record_provenance import PublicScientificRecords, RECORDS_MEDIA
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.workflow.literature_paper_facts import public_observations, remap_evidence_refs


@dataclass(frozen=True)
class PublishedScientificRecords:
    artifact: ArtifactRef
    public_ref: dict
    value: dict
    observations: list
    provenance: dict
    execution_ids: frozenset


def seal_public_records(handoff, *, producer_receipt_ref, store, observation_set):
    if handoff.source_bindings is None or handoff.receipt.record_source_version != 1:
        raise ValueError('public records require verified independent source feedback')
    if producer_receipt_ref not in handoff.closure:
        raise ValueError('public record producer receipt is outside verified closure')
    if store.read_bytes(producer_receipt_ref) != canonical_json_bytes(handoff.receipt.model_dump(mode='json')):
        raise ValueError('public record producer differs from its verified receipt')
    original = handoff.receipt.raw_model()
    # Assembly freezes the full cohort once (baseline and all producer receipts).
    # Re-keying this producer alone can bind a hash changed by a later same-site merge.
    public = observation_set
    original_refs = {(str(o["observation_id"]), hashlib.sha256(
        canonical_json_bytes(o)).hexdigest()) for o in handoff.receipt.observations}
    if not original_refs <= public.remap.keys():
        raise ValueError('public observation cohort omits the recording producer')
    recomputed = public_observations((*public.observations, *handoff.receipt.observations))
    if any(public.remap[key] != recomputed.remap[key] for key in original_refs):
        raise ValueError('public observation cohort does not preserve the recording sources')
    public_refs = {(str(o["observation_id"]), hashlib.sha256(
        canonical_json_bytes(o)).hexdigest()) for o in public.observations}
    if any((r['observation_id'],r['source_observation_sha256']) not in public_refs
           for r in public.remap.values()):
        raise ValueError('public observation cohort has a dangling remap')
    records = []
    used = set()
    for record in original.records:
        bindings = remap_evidence_refs(handoff.source_bindings[record.item_id], public.remap)
        refs = {(e['observation_id'],e['source_observation_sha256']): e
                for binding in bindings for e in binding['evidence_refs']}
        used.update(key[0] for key in refs)
        origin = handoff.origins[('records',record.item_id)]
        records.append({'item_id':record.item_id,'title':record.title,'explanation':record.explanation,
            'birth':{'execution_id':origin['execution_id'],'action_id':origin['creation_action_id']},
            'source_bindings':bindings,'evidence_refs':[refs[key] for key in sorted(refs)]})
    value = PublicScientificRecords.model_validate_json(canonical_json_bytes({
        'schema_version':'tsdaf.public-scientific-records.v1', 'paper_id':handoff.receipt.paper_id,
        'recording_execution_id':handoff.receipt.execution_id,
        'producer_receipt_sha256':producer_receipt_ref.sha256,
        'scientific_status':original.status,'scientific_explanation':original.explanation,'records':records,
    })).model_dump(mode='json')
    artifact = store.put_bytes(canonical_json_bytes(value),media_type=RECORDS_MEDIA)
    ref = {'sha256':artifact.sha256,'byte_size':artifact.byte_size,'media_type':artifact.media_type,
           'logical_name':'Scientific records:'+handoff.receipt.execution_id}
    return PublishedScientificRecords(artifact,ref,value,
        [o for o in public.observations if o['observation_id'] in used],
        {k:v for k,v in public.provenance.items() if k in used}, public.execution_ids)


@dataclass(frozen=True)
class ReboundPublicScience:
    machine: dict
    artifacts: tuple[ArtifactRef, ...]


def rebind_public_science(machine, *, observation_set, previous_observations, read_public, store, source_machine=None):
    """Create a new revision's provenance derivatives without editing old artifacts.

    All readers use the current revision's full observation cohort. Older record and
    delivery artifacts remain immutable, reachable from their original Machine root.
    Only explicit reference fields move; scientific prose and submitted payloads do not.
    """
    from copy import deepcopy
    from tsdaf.contracts.literature.public_science import load_public_science, record_observation_refs
    from tsdaf.contracts.literature.record_provenance import PublicMappingDelivery, MAPPING_DELIVERY_MEDIA

    bundle = load_public_science(source_machine if source_machine is not None else machine, read_public)
    if not record_observation_refs(bundle) <= observation_set.remap.keys():
        raise ValueError('public observation cohort omits a previous record source')
    public_refs = {(str(o['observation_id']), hashlib.sha256(canonical_json_bytes(o)).hexdigest())
                   for o in observation_set.observations}
    if any((r['observation_id'],r['source_observation_sha256']) not in public_refs
           for r in observation_set.remap.values()):
        raise ValueError('public observation cohort has a dangling remap')
    previous_by_ref = {(str(o['observation_id']), hashlib.sha256(canonical_json_bytes(o)).hexdigest()):o
                       for o in previous_observations}
    if not record_observation_refs(bundle) <= previous_by_ref.keys():
        raise ValueError('previous record source lacks its exact immutable observation')
    checked = public_observations((*observation_set.observations, *previous_observations))
    if any(observation_set.remap[key] != checked.remap[key]
           for key in record_observation_refs(bundle)):
        raise ValueError('public observation cohort changes a previous record source')
    replaced, artifacts = {}, []

    def seal(old_sha, value, model, media):
        model.model_validate_json(canonical_json_bytes(value))
        artifact = store.put_bytes(canonical_json_bytes(value), media_type=media)
        artifacts.append(artifact)
        original = bundle['artifacts'][old_sha]
        replaced[old_sha] = {**original, 'sha256':artifact.sha256, 'byte_size':artifact.byte_size}

    def record_ref(ref):
        return {**deepcopy(ref), 'artifact_ref':deepcopy(replaced.get(ref['artifact_ref']['sha256'],ref['artifact_ref']))}

    for sha, value in bundle['records'].items():
        seal(sha, remap_evidence_refs(value, observation_set.remap), PublicScientificRecords, RECORDS_MEDIA)
    for sha, value in bundle['deliveries'].items():
        value = deepcopy(value)
        if 'record_artifact_refs' in value:
            value['record_artifact_refs'] = [deepcopy(replaced[r['sha256']])
                                            for r in value['record_artifact_refs']]
        for item in value.get('items', ()):
            if 'model_record_refs' in item:
                item['model_record_refs'] = [record_ref(r) for r in item['model_record_refs']]
        seal(sha, value, PublicMappingDelivery, MAPPING_DELIVERY_MEDIA)

    def transport(value):
        if isinstance(value, dict):
            return {k: ([record_ref(r) for r in v]
                        if k in {'model_record_refs','relation_record_refs'} else transport(v))
                    for k,v in value.items()}
        if isinstance(value, list):
            return [transport(v) for v in value]
        return deepcopy(value)
    changed = transport(machine)
    changed['stage_output_refs'] = [deepcopy(replaced.get(r['sha256'],r))
                                     for r in machine.get('stage_output_refs', ())]
    return ReboundPublicScience(changed, tuple(artifacts))
