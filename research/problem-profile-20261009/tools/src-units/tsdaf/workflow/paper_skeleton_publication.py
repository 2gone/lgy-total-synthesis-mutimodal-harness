"""Publish the model's paper skeleton without the historical scientific projector."""
from tsdaf.contracts.literature.schema import MachineProfile, MACHINE_SCHEMA_VERSIONS
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.paper_skeleton_compilation import verify_paper_compilation
from tsdaf.workflow.literature_paper_facts import (
    PublishedPaperFactSet,finalize_public_result,public_observations,
    golden_schema_sha256,seal_assembly_record,resolve_public_ref,
)
from tsdaf.workflow.literature_fact_publication import FactPublisher,build_fact_publication_authority
from tsdaf.workflow.literature_enrichment_publication import public_ref,content_hash


def publish_paper_skeleton(*,workspace,package,package_dir,receipt_ref,provider_binding,
                          model_profile_versions,sources=None):
    store=LocalObjectStore(workspace); runner=LocalRunner(workspace,store)
    receipt,science,inputs,terminal=verify_paper_compilation(receipt_ref,package=package,
        package_root=package_dir,store=store,runner=runner,provider_binding=provider_binding)
    if science is None:
        from tsdaf.lanes.literature.fact_projection import PublicationNotReady
        raise PublicationNotReady(receipt.paper_id,'Paper Fusion did not deliver a compilable skeleton.')
    cohort=public_observations(inputs.observations,{})
    supporting_ids={group.receipt.literature_execution_id for group in inputs.groups}|set(cohort.execution_ids)
    supporting_ids.discard(receipt.literature_execution_id)
    supporting=tuple(runner.verified_terminal_manifest(eid) for eid in sorted(supporting_ids))
    authority=build_fact_publication_authority(package,package_dir,terminal,store,runner,
        supporting_terminal_manifests=supporting,sources=sources)
    profile=MachineProfile.V1_7
    machine={**science,'result_kind':'MACHINE_RESULT',
        'machine_result_id':'MPR-'+content_hash([receipt.compilation.answer.compiled_ref.sha256,receipt_ref.sha256]),
        'main_package_revision_id':package.stable_revision_id('MAIN'),
        'si_package_revision_ids':[d.package.stable_revision_id('SI') for d in sources.active_si] if sources else [],
        'supersedes_machine_result_sha256':None,'machine_revision':1,'coverage':'MAIN_ONLY',
        'source_observation_refs':[],
        'source_artifact_refs':[public_ref(ref,'source-artifact') for ref in authority.review_readable_artifacts],
        'stage_output_refs':[public_ref(receipt.compilation.answer.delivery_ref,'Paper skeleton delivery')],
        'schema_version':MACHINE_SCHEMA_VERSIONS[profile], 'schema_golden_sha256':golden_schema_sha256(profile),
        'model_profile_versions':[dict(item) for item in model_profile_versions],
        'execution_id':authority.execution_id,'execution_manifest_ref':public_ref(terminal,'execution-manifest')}
    def read(ref): return store.read_bytes(resolve_public_ref(store,ref))
    final=finalize_public_result(machine,inputs.observations,paper_id=receipt.paper_id,
        prior_provenance={},material_sources={},profile=profile,read_public=read,observation_set=cohort)
    published=FactPublisher().publish(final.machine,final.observations,authority,store,provenance=final.provenance)
    result=PublishedPaperFactSet(**published.__dict__,
        published_route_group_ids=tuple(g.receipt.route_group_id for g in inputs.groups),
        excluded_route_groups=receipt.group_failures,material_sources=final.material_sources,
        observation_provenance=dict(final.provenance),verifying_execution_ids=frozenset(supporting_ids|{receipt.literature_execution_id}),
        block_provenance={},skeleton_groups=())
    seal_assembly_record(workspace,'literature-publication-record',{
        'schema_version':'tsdaf.literature-publication-record.v1','paper_id':receipt.paper_id,
        'machine_result_id':result.machine['machine_result_id'],'machine_revision':1,
        'execution_id':receipt.literature_execution_id,'machine_artifact':dict(result.machine_artifact_ref),
        'material_sources':{},'observation_count':len(result.observations)},
        (receipt_ref,resolve_public_ref(store,result.machine_artifact_ref)))
    return result
