"""Shared execution of an authorized literature task, from readers through fusion.

The route entry point proves its authority before reaching this module. The common
stage owns caching, recovery, evidence, receipts and failure isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string
from tsdaf.contracts.execution import (
    PROVIDER_ATTEMPT_AUDIT_VERSION,
    ExecutionStatus,
    FailureCode,
    FailureDetail,
    ProviderInteractionError,
    StageResult,
    TypedFailure,
    failure_is_retryable,
)
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.evidence_binding import bind_work_unit_evidence
from tsdaf.lanes.literature.lane_dispatch import LanePairProviderError, dispatch_literature_lanes
from tsdaf.lanes.literature.lane_proposals import seal_initial_route_lane_proposals
from tsdaf.lanes.literature.profiles import fusion_resource_manifest_sha256, load_lane_resources
from tsdaf.lanes.literature.provider_port import (
    AUTHORIZED_MODEL_PROFILE,
    LaneInteractionClient,
    ProviderBinding,
)
from tsdaf.lanes.literature.source_inventory_models import LiteratureSourceInventory
from tsdaf.lanes.literature.source_manifest import UNKNOWN_SOURCE_STATUS, PaperSourceStatus
from tsdaf.lanes.literature.source_reads import SourceReader
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput
from tsdaf.workflow.literature_fusion_progress import restore_fusion_inputs, save_fusion_inputs
from tsdaf.workflow.literature_fusion_runtime import (
    SealedFusionOutcome,
    run_fusion,
)
from tsdaf.workflow.literature_reader_runtime import cached_reader_executor, reader_reuse_verifier
from tsdaf.workflow.local_runtime import (
    LocalObjectStore,
    LocalRunner,
    LocalRuntimeError,
    stage_content_key,
)
from tsdaf.workflow.tool_execution import diagnostic_text
from tsdaf.workflow.unit_decisions import UNIT_DECISIONS

LITERATURE_ROUTE_STAGE = "literature-route-group"
LITERATURE_ROUTE_STAGE_VERSION = "4.3.0"
PACKAGE_MEDIA_TYPE = "application/vnd.tsdaf.canonical-paper-package+json"
ROUTE_RECEIPT_MEDIA = "application/vnd.tsdaf.literature-route-group-receipt+json"
READINGS_RECEIPT_MEDIA = "application/vnd.tsdaf.literature-group-readings+json"


class GroupReadingBindings(ContractModel):
    """Original group authority and reader provenance shared by both execution modes."""
    route_execution_id: str
    literature_execution_id: str
    document_revision_id: str
    route_group_id: str
    work_unit_id: str
    work_unit_sha256: str
    source_view_sha256: str
    text_proposal_ref: ArtifactRef
    text_proposal_provenance_ref: ArtifactRef
    visual_proposal_ref: ArtifactRef
    visual_proposal_provenance_ref: ArtifactRef
    evidence_registry_ref: ArtifactRef
    evidence_entry_count: int = Field(ge=0)
    work_unit_ref: ArtifactRef | None = None
    source_inventory_ref: ArtifactRef | None = None

    @field_validator(
        "route_execution_id",
        "literature_execution_id",
        "document_revision_id",
        "route_group_id",
        "work_unit_id",
    )
    @classmethod
    def _identities(cls, value: str) -> str:
        return _stable_string(value, label="literature route receipt identity")

    @field_validator("work_unit_sha256", "source_view_sha256")
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _sha256(value, label="literature route receipt hash")


class LiteratureGroupReadingsReceipt(GroupReadingBindings):
    """T/V readings, never a fabricated Gate result or a scientific completion claim."""

    schema_version: Literal["tsdaf.literature-group-readings.v1"]
    work_unit_ref: ArtifactRef
    source_inventory_ref: ArtifactRef


class LiteratureRouteGroupReceipt(GroupReadingBindings):
    """Historical group-sized Fusion receipt; independent of the new reading receipt."""

    schema_version: Literal["tsdaf.literature-route-group-receipt.v4"]
    gate_proposal_ref: ArtifactRef
    gate_provenance_ref: ArtifactRef
    # Historical field name. New runs record STOPPED, not scientific completion.
    gate_decision: str
    child_work_unit_refs: tuple[ArtifactRef, ...]

    @field_validator("gate_decision")
    @classmethod
    def _gate_identity(cls, value: str) -> str:
        return _stable_string(value, label="literature route receipt identity")

    @model_validator(mode="after")
    def _distinct_children(self) -> Self:
        if self.child_work_unit_refs and self.gate_decision not in UNIT_DECISIONS:
            raise ValueError("literature route receipt decision and child inventory disagree")
        if self.gate_decision == "DEEPEN" and not self.child_work_unit_refs:
            raise ValueError("literature route receipt decision and child inventory disagree")
        digests = tuple(ref.sha256 for ref in self.child_work_unit_refs)
        if len(set(digests)) != len(digests):
            raise ValueError("literature route receipt repeats a child work unit")
        return self


@dataclass(frozen=True, slots=True)
class LiteratureRouteGroupRun:
    """One completed route-group run.

    `outcome` carries the in-memory Gate 1 result only for a freshly executed stage.
    A run served from the stage cache returns `None` there; `receipt` is always present
    and its refs remain the durable, content-addressed truth for the whole run.
    """

    stage: StageResult
    receipt: LiteratureRouteGroupReceipt
    outcome: SealedFusionOutcome | None


@dataclass(frozen=True, slots=True)
class LiteratureGroupReadingsRun:
    """Verified reader output awaiting the paper's single Fusion stage."""

    stage: StageResult
    receipt: LiteratureGroupReadingsReceipt


def failure(code: FailureCode, message: str) -> TypedFailure:
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message=message,
        request_id=None,
        details=(),
    )


def at_phase(value: TypedFailure, phase: str) -> TypedFailure:
    details = {item.key: item for item in value.details}
    details["unit_phase"] = FailureDetail(key="unit_phase", value=phase)
    return value.model_copy(update={"details": tuple(details[key] for key in sorted(details))})


def unit_failure(error: Exception, *, phase: str) -> TypedFailure:
    if isinstance(error, LocalRuntimeError):
        value = failure(
            FailureCode.HASH_MISMATCH, "The literature runtime could not persist verified evidence."
        )
    elif isinstance(error, ProviderInteractionError):
        value = error.failure
    else:
        cause = diagnostic_text(error) if isinstance(error, ValidationError) else str(error)
        value = failure(
            FailureCode.PROVIDER_REJECTED,
            "This task's sealed inputs or provider answer were refused.",
        )
        value = value.model_copy(
            update={
                "details": (
                    FailureDetail(key="cause", value=f"{type(error).__name__}: {cause}"[:2000]),
                )
            }
        )
    return at_phase(value, phase)


def resource_settings(machine_profile: MachineProfile = MachineProfile.V1) -> dict[str, object]:
    return {
        "provider_attempt_audit_version": PROVIDER_ATTEMPT_AUDIT_VERSION,
        "t1_resource_manifest_sha256": load_lane_resources("T1").resource_manifest_sha256,
        "v1_resource_manifest_sha256": load_lane_resources("V1").resource_manifest_sha256,
        "gate1_resource_manifest_sha256": fusion_resource_manifest_sha256(
            unified_expression=machine_profile.unified_expression
        ),
    }


def run_verified_literature_unit(
    *,
    workspace: Path,
    store: LocalObjectStore,
    runner: LocalRunner,
    package: CanonicalPaperPackage,
    package_root: Path,
    authority_execution_id: str,
    execution_id: str,
    work_unit: VerifiedWorkUnitInput,
    work_unit_ref: ArtifactRef,
    inventory: LiteratureSourceInventory | None,
    inventory_ref: ArtifactRef | None,
    input_artifacts: tuple[ArtifactRef, ...],
    settings: dict[str, object],
    provider_binding: ProviderBinding,
    text_client: LaneInteractionClient,
    visual_client: LaneInteractionClient,
    gate_client: LaneInteractionClient,
    provider_is_live: bool,
    live: bool,
    finish_execution: bool,
    machine_profile: MachineProfile = MachineProfile.V1,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
    readers_only: bool = False,
) -> LiteratureRouteGroupRun | LiteratureGroupReadingsRun | TypedFailure:
    # Every route entry (paper, direct and recovery) shares this cache boundary.
    # Override any caller-supplied identity; only composition's binding is authority.
    settings = {
        **settings,
        "provider_backend_namespaces": dict(provider_binding.backend_namespaces),
        "machine_profile": machine_profile.value,
    }
    if readers_only:
        if not machine_profile.faithful_mapping:
            raise ValueError("separate group readings require the faithful compilation profile")
        settings["execution_mode"] = "GROUP_READINGS"
    stage_name = "literature-group-readings" if readers_only else LITERATURE_ROUTE_STAGE
    stage_version = LITERATURE_ROUTE_STAGE_VERSION
    content_key = stage_content_key(
        stage_name=stage_name,
        stage_version=stage_version,
        input_sha256s=tuple(ref.sha256 for ref in input_artifacts),
        settings=settings,
        model_profile=AUTHORIZED_MODEL_PROFILE,
    )
    if (
        provider_is_live
        and not live
        and runner.stage_is_uncached(
            execution_id=execution_id, stage_name=stage_name, content_key=content_key
        )
    ):
        return failure(
            FailureCode.LIVE_REQUIRED,
            "This uncached literature task requires an explicitly enabled live invocation.",
        )
    reuse_verifier = reader_reuse_verifier(
        runner=runner, store=store, provider_binding=provider_binding, work_unit=work_unit
    )
    executed: SealedFusionOutcome | None = None

    def invoke(attempt_id: str, invoked_content_key: str) -> StageResult:
        nonlocal executed
        started_at = datetime.now(UTC)

        def bind(*refs: ArtifactRef | None) -> None:
            runner.bind_attempt_artifacts(
                execution_id,
                attempt_id,
                tuple(ref for ref in refs if ref is not None),
                stage_name=stage_name,
                content_key=invoked_content_key,
            )

        def emit_event(event_type: str, data: dict[str, Any]) -> None:
            runner.append_event(
                execution_id,
                event_type,
                data,
                attempt_id=attempt_id,
                stage_name=stage_name,
                content_key=invoked_content_key,
            )

        def event(name: str, values: dict[str, Any]) -> None:
            runner.append_event(
                execution_id,
                name,
                values,
                attempt_id=attempt_id,
                stage_name=stage_name,
                content_key=invoked_content_key,
            )

        phase = "lane_dispatch"
        try:
            source_reader = SourceReader(package, package_root)
            recovery_artifacts = runner.verified_stage_artifacts(
                execution_id,
                stage_name=stage_name,
                content_key=invoked_content_key,
            )
            restored = restore_fusion_inputs(
                recovery_artifacts,
                work_unit_id=work_unit.work_unit_id,
                store=store,
            )
            if restored is not None:
                proposals, evidence = restored
                if evidence is None:
                    raise ValueError("root fusion recovery requires sealed readings and evidence")
            else:
                source_reader.verify_text()
                visual_payloads = source_reader.read_visuals(
                    work_unit=work_unit,
                    visual_ids=tuple(s.visual_id for s in work_unit.visual_sources),
                )
                exchanges = dispatch_literature_lanes(
                    work_unit=work_unit,
                    visual_payloads=visual_payloads,
                    provider_binding=provider_binding,
                    text_client=text_client,
                    visual_client=visual_client,
                    artifact_store=store,
                    reader_executor=cached_reader_executor(
                        runner=runner,
                        store=store,
                        work_unit=work_unit,
                        provider_binding=provider_binding,
                        provider_is_live=provider_is_live,
                        live=live,
                        bind=bind,
                        emit_event=emit_event,
                    ),
                )
                bind(
                    exchanges.preparation_ref,
                    exchanges.text.record_ref,
                    exchanges.visual.record_ref,
                )
                event(
                    "literature_route_pair_dispatched",
                    {
                        "route_group_id": work_unit.route_group_id,
                        "work_unit_id": work_unit.work_unit_id,
                        "authorized_text_block_ids": list(work_unit.read_authority.text_block_ids),
                        "preparation_artifact": exchanges.preparation_ref.model_dump(mode="json"),
                        "text_reused": exchanges.text.reuse_binding_ref is not None,
                        "visual_reused": exchanges.visual.reuse_binding_ref is not None,
                        "text_raw_record_artifact": exchanges.text.record_ref.model_dump(
                            mode="json"
                        ),
                        "visual_raw_record_artifact": exchanges.visual.record_ref.model_dump(
                            mode="json"
                        ),
                    },
                )
                phase = "lane_proposal_validation"
                proposals = seal_initial_route_lane_proposals(
                    exchanges=exchanges, artifact_store=store, reuse_verifier=reuse_verifier
                )
                bind(
                    proposals.text.proposal_ref,
                    proposals.text.provenance_ref,
                    proposals.visual.proposal_ref,
                    proposals.visual.provenance_ref,
                )
                phase = "evidence_binding"
                evidence = bind_work_unit_evidence(
                    work_unit=work_unit,
                    source_reader=source_reader,
                    visual_payloads=visual_payloads,
                    execution_id=execution_id,
                    proposals=proposals,
                    artifact_store=store,
                    isolate_invalid_selections=True,
                    reuse_verifier=reuse_verifier,
                )
                bind(evidence.registry_ref)
                event(
                    "literature_evidence_materialized",
                    {
                        "work_unit_id": work_unit.work_unit_id,
                        "registry_artifact": evidence.registry_ref.model_dump(mode="json"),
                        "entry_count": len(evidence.registry.entries),
                    },
                )
                save_fusion_inputs(
                    work_unit_id=work_unit.work_unit_id,
                    proposals=proposals,
                    evidence=evidence,
                    store=store,
                    bind=bind,
                )
            reading_bindings = GroupReadingBindings(
                route_execution_id=authority_execution_id,
                literature_execution_id=execution_id,
                document_revision_id=work_unit.document_revision_id,
                route_group_id=work_unit.route_group_id,
                work_unit_id=work_unit.work_unit_id,
                work_unit_sha256=work_unit.work_unit_sha256,
                source_view_sha256=work_unit.source_view_sha256,
                text_proposal_ref=proposals.text.proposal_ref,
                text_proposal_provenance_ref=proposals.text.provenance_ref,
                visual_proposal_ref=proposals.visual.proposal_ref,
                visual_proposal_provenance_ref=proposals.visual.provenance_ref,
                evidence_registry_ref=evidence.registry_ref,
                evidence_entry_count=len(evidence.registry.entries),
                work_unit_ref=work_unit_ref,
                source_inventory_ref=inventory_ref,
            )
            if readers_only:
                receipt = LiteratureGroupReadingsReceipt(
                    schema_version="tsdaf.literature-group-readings.v1",
                    **reading_bindings.model_dump(mode="python"),
                )
                outcome = None
            else:
                phase = "gate_fusion"
                outcome = run_fusion(
                    work_unit=work_unit,
                    source_reader=source_reader,
                    proposals=proposals,
                    evidence=evidence,
                    execution_id=execution_id,
                    provider_binding=provider_binding,
                    interaction=gate_client,
                    artifact_store=store,
                    source_inventory=inventory,
                    bind_artifacts=bind,
                    recovery_artifacts=recovery_artifacts,
                    source_status=source_status,
                    machine_profile=machine_profile,
                    reuse_verifier=reuse_verifier,
                    emit_event=emit_event,
                )
                bind(outcome.provenance_ref)
                phase = "receipt_persistence"
                receipt = LiteratureRouteGroupReceipt(
                    schema_version="tsdaf.literature-route-group-receipt.v4",
                    **reading_bindings.model_dump(mode="python"),
                    gate_proposal_ref=outcome.proposal_ref,
                    gate_provenance_ref=outcome.provenance_ref,
                    gate_decision=outcome.decision,
                    child_work_unit_refs=(),
                )
                event(
                    "literature_gate1_decided",
                    {
                        "work_unit_id": work_unit.work_unit_id,
                        "gate_decision": receipt.gate_decision,
                        "gate_provenance_artifact": outcome.provenance_ref.model_dump(mode="json"),
                        "child_work_unit_count": 0,
                    },
                )
            output = store.put_bytes(
                canonical_json_bytes(receipt.model_dump(mode="json")),
                media_type=READINGS_RECEIPT_MEDIA if readers_only else ROUTE_RECEIPT_MEDIA,
            )
            executed = outcome
            status, refused = ExecutionStatus.SUCCEEDED, None
        except (LocalRuntimeError, ProviderInteractionError, ValueError) as error:
            if (
                isinstance(error, LocalRuntimeError)
                and error.code is FailureCode.ARTIFACT_WRITE_FAILED
            ):
                raise  # Recovery determines whether the interrupted acceptance committed.
            refused = unit_failure(error, phase=phase)
            output = None
            status = (
                ExecutionStatus.FAILED_RETRYABLE
                if failure_is_retryable(refused.code)
                else ExecutionStatus.FAILED_TERMINAL
            )
            if isinstance(error, LanePairProviderError):
                bind(error.audit_ref)
        return StageResult(
            schema_version="tsdaf.stage-result.v1",
            execution_id=execution_id,
            attempt_id=attempt_id,
            stage_name=stage_name,
            stage_version=stage_version,
            input_content_key=invoked_content_key,
            status=status,
            output_artifact=output,
            failure=refused,
            cached_from_result=None,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    result = runner.run_stage(
        execution_id=execution_id,
        stage_name=stage_name,
        stage_version=stage_version,
        input_artifacts=input_artifacts,
        settings=settings,
        invoke=invoke,
        model_profile=AUTHORIZED_MODEL_PROFILE,
        requires_live=provider_is_live,
        live=live,
    )
    if isinstance(result, TypedFailure):
        return result
    if result.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
        if finish_execution and result.status is not ExecutionStatus.FAILED_RETRYABLE:
            runner.finish_execution(execution_id, ExecutionStatus.FAILED_TERMINAL)
        return result.failure or failure(
            FailureCode.HASH_MISMATCH, "The literature stage failed without a verified receipt."
        )
    ref = result.output_artifact
    receipt_media = READINGS_RECEIPT_MEDIA if readers_only else ROUTE_RECEIPT_MEDIA
    if ref is None or ref.media_type != receipt_media:
        raise ValueError("a successful literature task must seal the expected receipt")
    receipt_type = LiteratureGroupReadingsReceipt if readers_only else LiteratureRouteGroupReceipt
    receipt = receipt_type.model_validate_json(store.read_bytes(ref))
    references = (
        receipt.text_proposal_ref,
        receipt.text_proposal_provenance_ref,
        receipt.visual_proposal_ref,
        receipt.visual_proposal_provenance_ref,
        receipt.evidence_registry_ref,
        *((receipt.gate_proposal_ref, receipt.gate_provenance_ref, *receipt.child_work_unit_refs)
          if isinstance(receipt, LiteratureRouteGroupReceipt) else ()),
        receipt.work_unit_ref,
        receipt.source_inventory_ref,
    )
    for reference in references:
        if reference is not None:
            store.verify(reference)
    if finish_execution:
        runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
    if isinstance(receipt, LiteratureGroupReadingsReceipt):
        return LiteratureGroupReadingsRun(result, receipt)
    return LiteratureRouteGroupRun(result, receipt, executed)
