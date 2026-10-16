"""Host-owned recovery records for the route and local fusion consumers.

Scientific state remains a reference to the single FusionResult. These records
remember accepted inputs and interaction progress; no model fills their fields.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.evidence_binding import (
    SealedWorkUnitEvidenceRegistry,
    WorkUnitEvidenceRegistry,
)
from tsdaf.lanes.literature.host_ports import ArtifactReader, PrivateArtifactStore
from tsdaf.lanes.literature.lane_models import TextReading, VisualReading
from tsdaf.lanes.literature.lane_proposals import (
    RouteProposalProvenance,
    SealedInitialRouteLaneProposals,
    SealedTextProposal,
    SealedVisualProposal,
)
from tsdaf.workflow.accepted_artifacts import accept_artifact, read_artifact

FUSION_INPUTS_MEDIA = "application/vnd.tsdaf.fusion-inputs+json"
FUSION_PROGRESS_MEDIA = "application/vnd.tsdaf.fusion-progress+json"


class FusionInputRecord(ContractModel):
    schema_version: Literal["tsdaf.fusion-inputs.v2"] = "tsdaf.fusion-inputs.v2"
    work_unit_id: str
    root: bool
    text_provenance_ref: ArtifactRef | None = None
    visual_provenance_ref: ArtifactRef | None = None
    evidence_ref: ArtifactRef | None = None


class FusionCheckpoint(ContractModel):
    """Accepted cursor plus additions since its predecessor; no cumulative state copies."""

    schema_version: Literal["tsdaf.fusion-progress.v2"] = "tsdaf.fusion-progress.v2"
    execution_id: str
    work_unit_id: str
    resources_sha256: str
    previous_ref: ArtifactRef | None = None
    initial_result_ref: ArtifactRef
    result_ref: ArtifactRef
    history_added: tuple[dict[str, Any], ...] = ()
    locations_added: dict[str, dict[str, Any]] = Field(default_factory=dict)
    pending_ref: ArtifactRef | None = None
    image_parts_added: tuple[dict[str, Any], ...] = ()
    next_call: int = Field(default=0, ge=0)
    iteration: int = Field(default=0, ge=0)
    audit_refs_added: tuple[ArtifactRef, ...] = ()
    dispatch_questions_added: tuple[str, ...] = ()
    finished: bool = False


@dataclass
class RestoredFusionProgress:
    checkpoint: FusionCheckpoint
    ref: ArtifactRef
    history: list[dict[str, Any]]
    locations: dict[str, dict[str, Any]]
    image_parts: list[dict[str, Any]]
    audit_refs: list[ArtifactRef]


def read_exact(store: ArtifactReader, ref: ArtifactRef) -> bytes:
    """Verify the actual returned bytes, including for stores whose reads do not self-verify."""
    return read_artifact(store.read_bytes, ref)


def accept_record(
    record: ContractModel,
    *,
    media_type: str,
    store: PrivateArtifactStore,
    bind: Callable[..., None],
) -> ArtifactRef:
    """A stored object becomes accepted only through the authenticated Host binding."""

    return accept_artifact(
        record,
        media_type=media_type,
        put_bytes=lambda payload, media: store.put_bytes(payload, media_type=media),
        read_bytes=store.read_bytes,
        bind=bind,
    )


def save_fusion_inputs(
    *,
    work_unit_id: str,
    proposals: SealedInitialRouteLaneProposals,
    evidence: SealedWorkUnitEvidenceRegistry | None,
    store: PrivateArtifactStore,
    bind: Callable[..., None],
) -> ArtifactRef:
    record = FusionInputRecord(
        work_unit_id=work_unit_id,
        # Historical field name: every task has been a route group since 2026-09-23.
        root=True,
        text_provenance_ref=proposals.text.provenance_ref,
        visual_provenance_ref=proposals.visual.provenance_ref,
        evidence_ref=evidence.registry_ref if evidence is not None else None,
    )
    return accept_record(record, media_type=FUSION_INPUTS_MEDIA, store=store, bind=bind)


def restore_fusion_inputs(
    refs: Sequence[ArtifactRef],
    *,
    work_unit_id: str,
    store: PrivateArtifactStore,
) -> tuple[SealedInitialRouteLaneProposals, SealedWorkUnitEvidenceRegistry | None] | None:
    """Load accepted inputs; run_fusion verifies their full closure before consumption."""

    selected = [ref for ref in refs if ref.media_type == FUSION_INPUTS_MEDIA]
    if not selected:
        return None
    record = FusionInputRecord.model_validate_json(read_exact(store, selected[-1]))
    if (record.work_unit_id, record.root) != (work_unit_id, True):
        raise ValueError("fusion input recovery belongs to another task")
    lanes: list[Any] = []
    for modality, ref in (
        ("TEXT", record.text_provenance_ref),
        ("VISUAL", record.visual_provenance_ref),
    ):
        if ref is None:
            lanes.append(None)
            continue
        provenance = RouteProposalProvenance.model_validate_json(read_exact(store, ref))
        if provenance.work_unit_id != work_unit_id:
            raise ValueError("recovered reading belongs to another work unit")
        if modality == "TEXT":
            lanes.append(
                SealedTextProposal(
                    TextReading.model_validate_json(read_exact(store, provenance.proposal_ref)),
                    provenance.proposal_ref,
                    provenance,
                    ref,
                )
            )
        else:
            lanes.append(
                SealedVisualProposal(
                    VisualReading.model_validate_json(read_exact(store, provenance.proposal_ref)),
                    provenance.proposal_ref,
                    provenance,
                    ref,
                )
            )
    if any(lane is None for lane in lanes):
        raise ValueError("root fusion recovery requires both independent readings")
    proposals = SealedInitialRouteLaneProposals(*lanes)
    evidence = None
    if record.evidence_ref is not None:
        registry = WorkUnitEvidenceRegistry.model_validate_json(
            read_exact(store, record.evidence_ref)
        )
        if registry.work_unit_id != work_unit_id:
            raise ValueError("recovered evidence belongs to another work unit")
        evidence = SealedWorkUnitEvidenceRegistry(registry, record.evidence_ref)
    return proposals, evidence


def restore_fusion_progress(
    refs: Sequence[ArtifactRef],
    *,
    execution_id: str,
    work_unit_id: str,
    resources_sha256: str,
    store: PrivateArtifactStore,
) -> RestoredFusionProgress | None:
    """Fold the accepted chain once, verifying each referenced object once per restore."""

    head: RestoredFusionProgress | None = None
    verified: set[ArtifactRef] = set()

    def verify(ref: ArtifactRef) -> None:
        if ref not in verified:
            store.verify(ref)
            verified.add(ref)

    history: list[dict[str, Any]] = []
    locations: dict[str, dict[str, Any]] = {}
    images: list[dict[str, Any]] = []
    audits: list[ArtifactRef] = []
    accepted_audits: set[ArtifactRef] = set()
    for ref in refs:
        if ref.media_type != FUSION_PROGRESS_MEDIA or head is not None and ref == head.ref:
            continue
        payload = read_exact(store, ref)
        verified.add(ref)
        record = FusionCheckpoint.model_validate_json(payload)
        if (record.execution_id, record.work_unit_id, record.resources_sha256) != (
            execution_id,
            work_unit_id,
            resources_sha256,
        ):
            raise ValueError("fusion progress belongs to another task or resource version")
        if record.previous_ref != (head.ref if head else None):
            raise ValueError("fusion progress does not follow its accepted predecessor")
        verify(record.initial_result_ref)
        verify(record.result_ref)
        for audit_ref in record.audit_refs_added:
            verify(audit_ref)
        audits.extend(record.audit_refs_added)
        accepted_audits.update(record.audit_refs_added)
        if record.pending_ref is not None and record.pending_ref not in accepted_audits:
            raise ValueError("pending fusion interaction was not accepted in its audit trail")
        if record.pending_ref is None:
            if record.image_parts_added:
                raise ValueError("queued images require a pending fusion interaction")
            images = []
        images.extend(record.image_parts_added)
        history.extend(record.history_added)
        for key, value in record.locations_added.items():
            if key in locations and locations[key] != value:
                raise ValueError("a saved fusion location cannot be rebound")
            resource = value.get("resource")
            if resource is not None:
                for name in ("root_ref", "artifact_ref"):
                    read_exact(store, ArtifactRef.model_validate(resource[name]))
            locations[key] = value
        head = RestoredFusionProgress(record, ref, history, locations, images, audits)
    return head
