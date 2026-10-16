"""Verify original group reading packages without inventing a paper work unit."""
from dataclasses import dataclass

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.evidence_binding import WorkUnitEvidenceRegistry
from tsdaf.lanes.literature.lane_models import TextReading, VisualReading
from tsdaf.lanes.literature.lane_proposals import (
    RouteProposalProvenance, SealedTextProposal, SealedVisualProposal,
    SealedInitialRouteLaneProposals, verify_sealed_initial_route_lane_proposals,
)
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput
from tsdaf.workflow.literature_fusion_progress import read_exact
from tsdaf.workflow.literature_reader_runtime import reader_reuse_verifier
from tsdaf.workflow.literature_unit_runtime import (
    LiteratureGroupReadingsReceipt, READINGS_RECEIPT_MEDIA,
)


@dataclass(frozen=True)
class VerifiedGroupReadings:
    receipt_ref: ArtifactRef
    receipt: LiteratureGroupReadingsReceipt
    terminal_ref: ArtifactRef
    work_unit: VerifiedWorkUnitInput
    proposals: SealedInitialRouteLaneProposals
    evidence: WorkUnitEvidenceRegistry


def verify_group_readings(receipt_ref, *, package, router_execution_id,
                          store, runner, provider_binding):
    """Used at Fusion input and publication replay; hashes alone do not grant scope."""
    if receipt_ref.media_type!=READINGS_RECEIPT_MEDIA:
        raise ValueError('paper Fusion expects an actual group reading receipt')
    receipt=LiteratureGroupReadingsReceipt.model_validate_json(read_exact(store,receipt_ref))
    if receipt.route_execution_id!=router_execution_id:
        raise ValueError('group readings belong to a different Router execution')
    terminal=runner.verified_terminal_manifest(receipt.literature_execution_id)
    closure=runner.verified_execution_artifact_closure(receipt.literature_execution_id,terminal)
    required=(receipt_ref,receipt.work_unit_ref,receipt.source_inventory_ref,
              receipt.text_proposal_ref,receipt.text_proposal_provenance_ref,
              receipt.visual_proposal_ref,receipt.visual_proposal_provenance_ref,
              receipt.evidence_registry_ref)
    if any(ref not in closure for ref in required):
        raise ValueError('group readings are outside their actual producer execution')
    work=VerifiedWorkUnitInput.model_validate_json(read_exact(store,receipt.work_unit_ref))
    if (work.package_sha256!=package.canonical_content_sha256 or
        (work.work_unit_id,work.work_unit_sha256,work.source_view_sha256,
         work.route_group_id,work.document_revision_id)!=
        (receipt.work_unit_id,receipt.work_unit_sha256,receipt.source_view_sha256,
         receipt.route_group_id,receipt.document_revision_id)):
        raise ValueError('group readings changed their original source authority')
    # Reprove Router authority, rather than accepting a self-consistent invented
    # work unit whose hashes happen to match one another.
    from tsdaf.lanes.literature.bootstrap import verify_router_handoff_bundle
    from tsdaf.lanes.literature.route_root import compile_route_group_root
    router_terminal=runner.verified_terminal_manifest(router_execution_id)
    handoffs=verify_router_handoff_bundle(package=package,router_execution_id=router_execution_id,
        terminal_manifest_ref=router_terminal,object_store=store,runner=runner)
    expected=compile_route_group_root(package=package,handoffs=handoffs,
        document_revision_id=receipt.document_revision_id,route_group_id=receipt.route_group_id)
    if work!=expected:
        raise ValueError('group reading scope differs from the sealed Router authority')
    lanes=[]
    for model,container,proposal_ref,provenance_ref in (
        (TextReading,SealedTextProposal,receipt.text_proposal_ref,receipt.text_proposal_provenance_ref),
        (VisualReading,SealedVisualProposal,receipt.visual_proposal_ref,receipt.visual_proposal_provenance_ref),
    ):
        provenance=RouteProposalProvenance.model_validate_json(read_exact(store,provenance_ref))
        if provenance.proposal_ref!=proposal_ref or provenance.work_unit_id!=work.work_unit_id:
            raise ValueError('reader proposal identity differs from its producer provenance')
        lanes.append(container(model.model_validate_json(read_exact(store,proposal_ref)),
                               proposal_ref,provenance,provenance_ref))
    proposals=SealedInitialRouteLaneProposals(*lanes)
    preparation=verify_sealed_initial_route_lane_proposals(proposals=proposals,artifact_store=store,
        reuse_verifier=reader_reuse_verifier(runner=runner,store=store,
                                            provider_binding=provider_binding,work_unit=work))
    if (preparation.work_unit_id,preparation.work_unit_sha256,preparation.source_view_sha256)!= (
        work.work_unit_id,work.work_unit_sha256,work.source_view_sha256):
        raise ValueError('sealed reader request differs from the original group')
    registry=WorkUnitEvidenceRegistry.model_validate_json(read_exact(store,receipt.evidence_registry_ref))
    if (registry.execution_id,registry.package_sha256,registry.work_unit_id,
        registry.work_unit_sha256,registry.source_view_sha256,registry.document_revision_id)!= (
        receipt.literature_execution_id,work.package_sha256,work.work_unit_id,
        work.work_unit_sha256,work.source_view_sha256,work.document_revision_id):
        raise ValueError('group evidence differs from its reading execution or scope')
    if len(registry.entries)!=receipt.evidence_entry_count:
        raise ValueError('group receipt changed the evidence inventory')
    for entry in registry.entries:
        authority=work.read_authority.text_block_ids if entry.modality=='TEXT' else work.read_authority.visual_ids
        if entry.canonical_source_id not in authority:
            raise ValueError('group evidence names an unauthorized source')
        if read_exact(store,entry.observation_ref)!=canonical_json_bytes(entry.source_observation):
            raise ValueError('group observation differs from its immutable source evidence')
    return VerifiedGroupReadings(receipt_ref,receipt,terminal,work,proposals,registry)
