"""Publish an accepted complete snapshot, bypassing the historical scientific fold."""
from copy import deepcopy
import json

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.literature.public_science import load_public_science
from tsdaf.workflow.final_mapping_handoff import verify_final_mapping
from tsdaf.workflow.final_science_mapping import final_mapping_input, mapping_assignment
from tsdaf.lanes.literature.final_science_view import FinalScienceView
from tsdaf.lanes.literature.final_science_document import FinalDocumentCompiler, validate_mapping_scope
from tsdaf.lanes.literature.scientific_field_bindings import iter_bound_objects, iter_declarations
from tsdaf.contracts.literature.record_provenance import submission_origin
from tsdaf.workflow.experimental_record_handoff import verify_record_handoff
from tsdaf.workflow.public_scientific_records import seal_public_records,rebind_public_science
from tsdaf.workflow.literature_paper_facts import (
    ASSEMBLY_VERSION,PublishedPaperFactSet,public_observations,finalize_public_result,seal_assembly_record,
)
from tsdaf.workflow.literature_fact_publication import FactPublisher,build_fact_publication_authority


def _binding_check(baseline, current, original_names, current_names):
    """Reject positional aliases whose original object has moved or been replaced."""
    def signatures(machine):
        from tsdaf.lanes.literature.faithful_baseline import compilation_baseline
        # Compare the same deterministic optional identities as the compiler.
        # An untouched legacy member gaining that identity is not a replacement.
        machine = compilation_baseline(machine)
        result = {row.path:(row.binding.definition,
                           row.identity if row.identity is not None else row.value)
                  for row in iter_bound_objects(machine)}
        result.update({path:(definition,
            value.get('unresolved_item_id', value.get('conflict_id', value)))
            for _, definition, path, value in iter_declarations(machine)})
        return result
    before, after = signatures(baseline), signatures(current)
    def check(name):
        path = original_names.get(name)
        if (path is None or current_names.get(name) != path
                or before.get(path) != after.get(path)):
            raise ValueError(f'offered object changed before assembly: {name}')
    return check


def publish_final_snapshot(*,workspace,sources,baseline,previous,details,record_receipts,narrative,failed_details):
    # Imports reuse publication transport only, never its old scientific application.
    from tsdaf.workflow.literature_enrichment_publication import (
        EnrichmentPublication,_load_investigations,resolve_public_ref,public_ref,content_hash,
    )
    from tsdaf.workflow.local_runtime import LocalObjectStore,LocalRunner
    store=LocalObjectStore(workspace);runner=LocalRunner(workspace,store)
    original_ref=resolve_public_ref(store,baseline.machine_artifact_ref)
    prior=previous.published if previous else baseline
    prior_ref=resolve_public_ref(store,prior.machine_artifact_ref)
    if previous and previous.original_main_ref!=original_ref:
        raise ValueError('previous publication belongs to a different original baseline')
    if json.loads(store.read_bytes(prior_ref))!=prior.machine:
        raise ValueError('previous machine differs from its immutable artifact')
    def read_public(ref): return store.read_bytes(resolve_public_ref(store,ref))
    mappings=[s for s in details if s.receipt.kind=='EXPERIMENTAL_MAPPING']
    if narrative is not None or any(s.receipt.kind not in {'EXPERIMENTAL_MAPPING','EXPERIMENTAL_DETAIL'} for s in details):
        raise ValueError('faithful publication consumes final documents; other science must first enter model notes')
    if len(record_receipts)!=1:
        raise ValueError('one faithful publication requires one recording')
    recording=record_receipts[0]
    handoff=verify_record_handoff(recording,store=store,runner=runner,sources=sources,baseline_ref=prior_ref)
    closures={}
    additions=[recording,*mappings]
    receipts=_load_investigations(store,runner,additions,closures_by_receipt=closures)
    cohort=public_observations((*prior.observations,*handoff.receipt.observations),prior.observation_provenance)
    public_records=seal_public_records(handoff,producer_receipt_ref=recording.receipt_ref,store=store,observation_set=cohort)
    machine=deepcopy(prior.machine); public_artifacts=[public_records.artifact]
    failures=list(failed_details)
    verified = []
    from tsdaf.workflow.final_mapping_sources import mapping_source_choices
    expected_sources = mapping_source_choices(prior.machine, handoff, sources)
    for final in mappings:
        compiled,delivery,task_input=verify_final_mapping(final.receipt,baseline=prior.machine,baseline_ref=prior_ref,
                                                       closure=closures[final.receipt_ref],store=store)
        context=task_input['context']
        if context.get('mapping_sources')!=expected_sources:
            raise ValueError('final source choices differ from their verified upstream bindings')
        if (task_input.get('record_receipt_ref')!=recording.receipt_ref.model_dump(mode='json')
                or context.get('mapping_records')!=public_records.value
                or context.get('mapping_record_ref')!=public_records.public_ref):
            raise ValueError('final document input differs from its independently verified public notes')
        assigned = mapping_assignment(prior.machine, context)
        verified.append((final, delivery, context, assigned, compiled))
    if len(verified) > 1 and any(row[3] is None for row in verified):
        raise ValueError('legacy whole-document mapping cannot be mixed with assigned tasks')
    scopes = [row[3] for row in verified]
    if len(scopes) != len(set(scopes)):
        raise ValueError('multiple final mapping tasks claim the same assignment')
    original_view = FinalScienceView(prior.machine)
    def order(row):
        assigned = row[3]
        return original_view.existing[assigned[0]][-1] if assigned else len(prior.machine['domain_payload']['reaction_blocks'])
    verified.sort(key=order)
    for final, delivery, context, assigned, compiled in verified:
        public_artifacts.append(final.receipt.raw_model().delivery_ref)
        if compiled is None:
            if assigned is not None:
                failures.append({'execution_id':final.receipt.execution_id,
                    'reaction_block_ids':context['mapping_assignment'],
                    'failure_code':'FINAL_MAPPING_NOT_DELIVERED'})
            continue
        # Replay already proved which submission was accepted against the fixed
        # input. Apply that exact document, not its baseline-sized output snapshot.
        item = next(item for item in delivery.items if item.status == 'APPLIED')
        view, _, _, _, records, _, _, _, _, citations = final_mapping_input(prior.machine, context)
        current_view = FinalScienceView(machine)
        try:
            validate_mapping_scope(item.submitted, assigned=assigned, existing=view.existing)
            compiler = FinalDocumentCompiler(machine,
                submission=final.receipt.execution_id+'/'+item.submission_action,
                existing=current_view.existing, records=records, sources=context.get('mapping_sources',{}),
                offered_names=view.existing,
                citations=citations,
                submission_refs=[submission_origin(final.receipt.execution_id,item.submission_action)],
                check_offered_reference=_binding_check(prior.machine, machine, view.existing, current_view.existing))
            updated = compiler.compile(item.submitted)
        except (ValueError, TypeError, KeyError) as error:
            failures.append({'execution_id':final.receipt.execution_id,
                'reaction_block_ids':context.get('mapping_assignment'),
                'failure_code':'FINAL_MAPPING_ASSEMBLY_FAILED', 'message':str(error)})
            continue
        machine = updated
    # Historical records need new source bytes only if the full observation cohort
    # changed. This modifies technical references, never old immutable artifacts.
    old_bundle=load_public_science(prior.machine,read_public)
    if old_bundle['artifacts']:
        rebound=rebind_public_science(machine,observation_set=cohort,previous_observations=prior.observations,
                                      read_public=read_public,store=store,source_machine=prior.machine)
        machine=rebound.machine;public_artifacts.extend(rebound.artifacts)
    machine['stage_output_refs']=[*machine['stage_output_refs'],public_records.public_ref,
        public_ref(prior_ref,'Final mapping fixed baseline'),*deepcopy(list(prior.observation_artifact_refs))]
    for final, *_ in verified:
        machine['stage_output_refs'].append(public_ref(final.receipt.raw_model().delivery_ref,'Final mapping delivery'))
    # These are execution/provenance declarations, not inferred scientific conclusions.
    machine['machine_revision']=prior.machine['machine_revision']+1
    machine['supersedes_machine_result_sha256']=prior_ref.sha256
    finalized=finalize_public_result(machine,(*prior.observations,*handoff.receipt.observations),
        paper_id=sources.paper_id,prior_provenance=prior.observation_provenance,
        material_sources=prior.material_sources,profile=MachineProfile.V1_7,read_public=read_public,
        observation_set=cohort)
    machine=finalized.machine
    from tsdaf.contracts.literature.public_science_rules import baseline_public_artifacts
    from tsdaf.contracts.literature.schema import validate_fact_definition
    historical = baseline_public_artifacts(machine,read_public,
        lambda name,value:validate_fact_definition(name,value,MachineProfile.V1_7))
    if not mappings: failures.append({'failure_code':'FINAL_MAPPING_NOT_DELIVERED'})
    payload={'schema_version':'tsdaf.literature-enrichment-assembly.v1',
        'machine_schema_version':machine['schema_version'],'schema_golden_sha256':machine['schema_golden_sha256'],
        'original_main_ref':original_ref.model_dump(mode='json'),'previous_machine_ref':prior_ref.model_dump(mode='json'),
        'investigation_refs':[s.receipt_ref.model_dump(mode='json') for s in [recording, *(row[0] for row in verified)]],
        'source_identities':[d.identity() for d in sources.documents],
        'assembly_failures':failures,'applications':[],'creation_keys':{},
        'skeleton_groups':list(prior.skeleton_groups),'block_provenance':dict(prior.block_provenance),
        'domain_sha256':content_hash(machine['domain_payload']),'mode':'FINAL_SNAPSHOT'}
    package_refs=tuple(store.put_bytes(canonical_json_bytes(d.package.model_dump(mode='json')),
        media_type='application/vnd.tsdaf.canonical-paper-package+json') for d in sources.active() if d.package is not None)
    inputs=(original_ref,prior_ref,*(s.receipt_ref for s in [recording, *(row[0] for row in verified)]),*public_artifacts,*package_refs,
            *(resolve_public_ref(store,ref) for ref in prior.observation_artifact_refs),
            *(resolve_public_ref(store,ref) for ref in historical.values()))
    _,terminal=seal_assembly_record(workspace,'literature-enrichment-assembly',payload,inputs)
    supporting_ids=set(prior.verifying_execution_ids)|set(finalized.execution_ids)
    supporting_ids.update((str(prior.machine['execution_id']),str(baseline.machine['execution_id'])))
    supporting_ids.update(r.execution_id for r in receipts)
    terminals={s.receipt.execution_id:s.terminal_manifest_ref for s in additions}
    assembly_id=json.loads(store.read_bytes(terminal))['execution_id']
    supporting=tuple(terminals[eid] if eid in terminals else runner.verified_terminal_manifest(eid)
                     for eid in sorted(supporting_ids) if eid!=assembly_id)
    authority=build_fact_publication_authority(sources.main.package,sources.main.package_root,terminal,store,runner,
                                              supporting_terminal_manifests=supporting,sources=sources)
    machine['execution_id']=authority.execution_id
    machine['execution_manifest_ref']=public_ref(terminal,'execution-manifest')
    machine['source_artifact_refs']=[public_ref(r,'source-artifact') for r in authority.review_readable_artifacts]
    machine['machine_result_id']='MPR-'+content_hash([ASSEMBLY_VERSION,prior_ref.sha256,payload])
    published=FactPublisher().publish(machine,finalized.observations,authority,store,provenance=finalized.provenance)
    result=PublishedPaperFactSet(**published.__dict__,published_route_group_ids=prior.published_route_group_ids,
        excluded_route_groups=prior.excluded_route_groups,material_sources=finalized.material_sources,
        observation_provenance=dict(finalized.provenance),verifying_execution_ids=frozenset(supporting_ids),
        block_provenance=prior.block_provenance,skeleton_groups=prior.skeleton_groups)
    publication={'schema_version':'tsdaf.literature-enrichment-publication.v1','assembly_version':ASSEMBLY_VERSION,
        'applications':[],'creation_keys':{},'original_main_ref':original_ref.model_dump(mode='json'),
        'material_sources':dict(result.material_sources),'skeleton_groups':list(result.skeleton_groups),
        'block_provenance':dict(result.block_provenance),'observation_provenance':dict(result.observation_provenance),
        'verifying_execution_ids':sorted(result.verifying_execution_ids),'machine_ref':dict(result.machine_artifact_ref),
        'observation_refs':list(result.observation_artifact_refs),'published_route_group_ids':list(result.published_route_group_ids),
        'excluded_route_groups':dict(result.excluded_route_groups),'execution_manifest_ref':terminal.model_dump(mode='json')}
    ref,_=seal_assembly_record(workspace,'literature-enrichment-publication',publication,
        (resolve_public_ref(store,result.machine_artifact_ref),terminal,original_ref,
         *(resolve_public_ref(store,r) for r in result.observation_artifact_refs)))
    return EnrichmentPublication(result,ref,original_ref,assembly_failures=tuple(failures))
