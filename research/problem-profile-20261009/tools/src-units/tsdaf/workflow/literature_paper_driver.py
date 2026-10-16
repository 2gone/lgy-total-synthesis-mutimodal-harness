"""One paper through its route groups.

The driver runs each named route group, records every outcome in one paper receipt and
seals that receipt under the paper execution. Publication reads the receipt; it is not
attempted here. Until 2026-09-23 a group could hand parts of its route on as reaction
windows, which this driver then ran as executions of their own; the fusion now writes
the whole route the main text tells and hands on nothing (ledger E172), so the groups
are the whole run. A receipt sealed before then still records its windows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tsdaf.contracts.execution import ExecutionStatus, StageResult, TypedFailure
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.provider_port import LaneInteractionClient, ProviderBinding
from tsdaf.lanes.literature.source_manifest import UNKNOWN_SOURCE_STATUS, PaperSourceStatus
from tsdaf.workflow.literature_paper_runtime import LiteraturePaperReceipt, RouteGroupOutcome
from tsdaf.workflow.literature_route_runtime import (
    LiteratureRouteGroupRun,
    run_literature_route_group,
)
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.literature_unit_runtime import LiteratureGroupReadingsRun
from tsdaf.workflow.paper_skeleton_compilation import PaperCompilationReceipt, run_paper_compilation

PAPER_RECEIPT_MEDIA_TYPE = "application/vnd.tsdaf.literature-paper-receipt+json"
PAPER_RECEIPT_STAGE = "literature_paper_receipt"


@dataclass(frozen=True, slots=True)
class LiteraturePaperRun:
    """The sealed paper receipt with the runs it records, in execution order."""

    receipt: LiteraturePaperReceipt | PaperCompilationReceipt
    receipt_ref: ArtifactRef
    groups: tuple[LiteratureRouteGroupRun | LiteratureGroupReadingsRun, ...]
    # The typed failure of each group that never reached a decision, by route group id.
    group_failures: dict[str, TypedFailure]


def group_execution_id(execution_id: str, ordinal: int) -> str:
    return f"{execution_id}-G{ordinal:03d}"


def run_literature_paper(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    package_root: Path,
    route_execution_id: str,
    execution_id: str,
    document_revision_id: str,
    route_group_ids: Sequence[str],
    provider_binding: ProviderBinding,
    text_client: LaneInteractionClient,
    visual_client: LaneInteractionClient,
    gate_client: LaneInteractionClient,
    provider_is_live: bool,
    live: bool = False,
    machine_profile: MachineProfile = MachineProfile.V1,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
) -> LiteraturePaperRun:
    """Run the named route groups. A failed group is recorded and its siblings run."""

    machine_profile.require_production()
    if not isinstance(workspace, Path) or not isinstance(package_root, Path):
        raise TypeError("workspace and package_root must be pathlib.Path values")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ValueError("execution_id must be a nonempty string")
    if not route_group_ids and not machine_profile.faithful_mapping:
        raise ValueError("a paper run needs at least one route group")

    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    if machine_profile.faithful_mapping:
        if provider_is_live and not live:
            raise ValueError('paper Fusion requires explicitly enabled live invocation')
        from tsdaf.lanes.literature.route_root import resolve_verified_route_group_ids
        route_group_ids = resolve_verified_route_group_ids(package=package,
            router_execution_id=route_execution_id,
            terminal_manifest_ref=runner.verified_terminal_manifest(route_execution_id),
            object_store=store,runner=runner)
    groups: list[LiteratureRouteGroupRun] = []
    group_outcomes: list[RouteGroupOutcome] = []
    group_failures: dict[str, TypedFailure] = {}
    for ordinal, group_id in enumerate(route_group_ids, 1):
        root = run_literature_route_group(
            workspace=workspace,
            package=package,
            package_root=package_root,
            route_execution_id=route_execution_id,
            literature_execution_id=group_execution_id(execution_id, ordinal),
            document_revision_id=document_revision_id,
            route_group_id=group_id,
            provider_binding=provider_binding,
            text_client=text_client,
            visual_client=visual_client,
            gate_client=gate_client,
            provider_is_live=provider_is_live,
            live=live,
            source_status=source_status,
            machine_profile=machine_profile,
            readers_only=machine_profile.faithful_mapping,
        )
        if isinstance(root, TypedFailure):
            group_failures[group_id] = root
            group_outcomes.append(
                RouteGroupOutcome(
                    route_group_id=group_id,
                    gate_decision=None,
                    receipt_ref=None,
                    failure_code=str(root.code),
                    failure=root,
                    child_work_unit_count=0,
                )
            )
            continue
        groups.append(root)
        if machine_profile.faithful_mapping:
            continue
        group_outcomes.append(
            RouteGroupOutcome(
                route_group_id=group_id,
                gate_decision=root.receipt.gate_decision,
                receipt_ref=root.stage.output_artifact,
                failure_code=None,
                child_work_unit_count=len(root.receipt.child_work_unit_refs),
            )
        )

    if machine_profile.faithful_mapping:
        receipt,receipt_ref=run_paper_compilation(package=package,package_root=package_root,
            router_execution_id=route_execution_id,execution_id=execution_id,
            document_revision_id=document_revision_id,
            receipt_refs=tuple(group.stage.output_artifact for group in groups),
            failures={key:str(value.code) for key,value in group_failures.items()},
            store=store,runner=runner,client=gate_client,provider_binding=provider_binding)
        return LiteraturePaperRun(receipt,receipt_ref,tuple(groups),group_failures)

    receipt = LiteraturePaperReceipt(
        schema_version="tsdaf.literature-paper-receipt.v2",
        route_execution_id=route_execution_id,
        literature_execution_id=execution_id,
        paper_id=package.manifest.paper_id,
        document_revision_id=document_revision_id,
        package_sha256=package.canonical_content_sha256,
        route_group_outcomes=tuple(group_outcomes),
    )
    receipt_ref = store.put_bytes(
        canonical_json_bytes(receipt.model_dump(mode="json")), media_type=PAPER_RECEIPT_MEDIA_TYPE
    )

    def seal(attempt_id: str, content_key: str) -> StageResult:
        now = datetime.now(UTC)
        return StageResult(
            schema_version="tsdaf.stage-result.v1",
            execution_id=execution_id,
            attempt_id=attempt_id,
            stage_name=PAPER_RECEIPT_STAGE,
            stage_version="1.0.0",
            input_content_key=content_key,
            status=ExecutionStatus.SUCCEEDED,
            output_artifact=receipt_ref,
            failure=None,
            cached_from_result=None,
            started_at=now,
            finished_at=now,
        )

    runner.run_stage(
        execution_id=execution_id,
        stage_name=PAPER_RECEIPT_STAGE,
        stage_version="1.0.0",
        input_artifacts=(receipt_ref,),
        settings={},
        invoke=seal,
    )
    runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
    return LiteraturePaperRun(receipt, receipt_ref, tuple(groups), group_failures)
