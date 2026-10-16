"""Exact closure of the two public model-work artifacts, independent of private logs."""
from collections.abc import Mapping
import hashlib
import json

# This standard-library-only contract source is copied byte-for-byte by the pack
# generator. Runtime and standalone review execute the same rules.
RECORDS_MEDIA = "application/vnd.tsdaf.public-scientific-records+json"
MAPPING_DELIVERY_MEDIA = "application/vnd.tsdaf.public-mapping-delivery+json"
PUBLIC_SCIENCE_MEDIA = frozenset({RECORDS_MEDIA, MAPPING_DELIVERY_MEDIA})


def validate_record_identity(value):
    records = value.get('records', ())
    if len({r['item_id'] for r in records}) != len(records):
        raise ValueError('duplicate public record identity')
    for record in records:
        if record['birth']['execution_id'] != value['recording_execution_id']:
            raise ValueError('record birth belongs to another execution')
        bound = {(e['observation_id'], e['source_observation_sha256'])
                 for b in record.get('source_bindings', ()) if b['status'] == 'BOUND'
                 for e in b.get('evidence_refs', ())}
        if bound != {(e['observation_id'], e['source_observation_sha256'])
                     for e in record.get('evidence_refs', ())}:
            raise ValueError('record evidence differs from its exact bound sources')


def validate_mapping_identity(value):
    """Acceptance belongs to this mapping execution, not the target object's birth."""
    actions=[item['submission_action'] for item in value.get('items',())]
    if len(actions)!=len(set(actions)):
        raise ValueError('duplicate public submission action')
    for item in value.get('items', ()):
        birth = item.get('accepted_birth')
        if birth is not None and birth['execution_id'] != value['mapping_execution_id']:
            raise ValueError('mapping entry acceptance belongs to another execution')


def _walk(value):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def load_public_science(machine, read_public, validate):
    """Only explicitly listed public media; hashes are checked before decoding.

    read_public receives a public ArtifactRef. Its caller owns access control;
    this contract verifies bytes, paper identity and all adopted record references.
    validate receives the definition name and decoded public JSON. A failed mapping's
    original submitted payload never grants a reference.
    """
    listed = [ref for ref in machine.get('stage_output_refs', ())
              if ref.get('media_type') in PUBLIC_SCIENCE_MEDIA]
    records, deliveries, identities = {}, {}, {}
    for ref in listed:
        sha = ref['sha256']
        if sha in identities:
            if identities[sha] != ref:
                raise ValueError('public scientific artifact has inconsistent reference metadata')
            continue
        payload = read_public(ref)
        if len(payload) != ref['byte_size'] or hashlib.sha256(payload).hexdigest() != sha:
            raise ValueError('public scientific artifact differs from its hash or size')
        definition = 'PublicScientificRecords' if ref['media_type']==RECORDS_MEDIA else 'PublicMappingDelivery'
        value = json.loads(payload)
        validate(definition, value)
        if definition == 'PublicScientificRecords':
            validate_record_identity(value)
        else:
            validate_mapping_identity(value)
        if value['paper_id'] != machine['paper_id']:
            raise ValueError('public scientific artifact belongs to another paper')
        identities[sha] = dict(ref)
        (records if ref['media_type']==RECORDS_MEDIA else deliveries)[sha] = value

    def verify_record_ref(ref):
        artifact = ref['artifact_ref']
        sha = artifact['sha256']
        if artifact != identities.get(sha) or sha not in records:
            raise ValueError('model record reference lacks its listed public artifact')
        if not any(item['item_id']==ref['item_id'] for item in records[sha].get('records', ())):
            raise ValueError('model record reference names an absent record item')

    for node in _walk(machine):
        for key in ('model_record_refs', 'relation_record_refs'):
            for ref in node.get(key, ()):
                verify_record_ref(ref)
    for delivery in deliveries.values():
        for ref in delivery.get('record_artifact_refs', ()):
            if ref != identities.get(ref['sha256']) or ref['sha256'] not in records:
                raise ValueError('mapping delivery lacks its listed public recording derivative')
        for item in delivery.get('items', ()):
            for ref in item.get('model_record_refs', ()):
                verify_record_ref(ref)
    # Resolve the complete listed delivery set before checking references. Historical
    # accepted submissions remain declarations even if replaced, not current output.
    submissions={}
    for delivery in deliveries.values():
        for item in delivery.get('items',()):
            birth=item.get('accepted_birth')
            if birth is None:
                continue
            key=(delivery['mapping_execution_id'],item['submission_action'])
            identity=(birth,item['submitted'],delivery.get('kind') or 'DETAIL',
                      delivery.get('baseline_sha256'),delivery.get('input_sha256'))
            if key in submissions and submissions[key]!=identity:
                raise ValueError('accepted submission has conflicting public copies')
            submissions[key]=identity
    for node in _walk(machine):
        for key in ('model_submission_refs','relation_submission_refs'):
            for ref in node.get(key,()):
                identity=submissions.get((ref['execution_id'],ref['submission_action']))
                if identity is None or identity[0]!=dict(execution_id=ref['execution_id'],action_id=ref['submission_action']):
                    raise ValueError('model submission reference lacks its exact accepted declaration')
    return {'records': records, 'deliveries': deliveries, 'artifacts': identities}


def record_observation_refs(bundle):
    """References retained by published records, whether or not a field cites them."""
    return {(ref['observation_id'],ref['source_observation_sha256'])
            for artifact in bundle.get('records', {}).values()
            for record in artifact.get('records', ()) for ref in record.get('evidence_refs', ())}


def baseline_public_artifacts(machine, read_public, validate):
    """Exact old public inputs of final mappings, without private stage artifacts.

    A baseline is already a public Machine, not a new audit ledger. Its observations
    and assets are copied only by explicit references. The current Machine lists the
    actual observation ArtifactRefs needed when an old fact was replaced.
    """
    available={r['sha256']:r for r in machine.get('stage_output_refs',())}
    result={};visited=set()
    def read(ref):
        data=read_public(ref)
        if len(data)!=ref['byte_size'] or hashlib.sha256(data).hexdigest()!=ref['sha256']:
            raise ValueError('historical public artifact differs from its identity')
        return data
    def visit(value):
        bundle=load_public_science(value,read,validate)
        if value is not machine:
            result.update(bundle['artifacts'])
            for ref in value.get('source_artifact_refs',()):
                read(ref);result[ref['sha256']]=ref
            for evidence in value.get('source_observation_refs',()):
                ref=available.get(evidence['source_observation_sha256'])
                if ref is None or ref['media_type']!='application/vnd.tsdaf.source-observation.v1+json':
                    raise ValueError('historical baseline observation lacks its public artifact')
                observation=json.loads(read(ref))
                validate('SourceObservation',observation)
                if observation['observation_id']!=evidence['observation_id']:
                    raise ValueError('historical observation identity differs from its baseline')
                result[ref['sha256']]=ref
        for delivery in bundle['deliveries'].values():
            if delivery.get('kind')=='SKELETON':
                continue  # Its real input is a reading package, never a fake Machine baseline.
            sha=delivery['baseline_sha256']
            if sha in visited: continue
            ref=available.get(sha)
            if ref is None or not ref['media_type'].startswith('application/vnd.tsdaf.machine-paper-result.'):
                raise ValueError('mapping baseline lacks its exact public Machine artifact')
            baseline=json.loads(read(ref))
            validate('MachinePaperResult',baseline)
            if baseline['paper_id']!=machine['paper_id']:
                raise ValueError('mapping baseline belongs to a different paper')
            visited.add(sha);result[sha]=ref
            available.update((r['sha256'],r) for r in baseline.get('stage_output_refs',()))
            visit(baseline)
    visit(machine)
    return result
