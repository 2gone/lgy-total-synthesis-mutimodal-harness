"""Route entry: verify sealed Router authority before the shared literature stage."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tsdaf.contracts.execution import ExecutionStatus, TypedFailure
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.bootstrap import verify_router_handoff_bundle
from tsdaf.lanes.literature.provider_port import LaneInteractionClient, ProviderBinding
from tsdaf.lanes.literature.route_root import compile_route_group_root
from tsdaf.lanes.literature.source_inventory import (
    SOURCE_INVENTORY_MEDIA_TYPE,
    compile_source_inventory,
)
from tsdaf.lanes.literature.source_manifest import UNKNOWN_SOURCE_STATUS, PaperSourceStatus
from tsdaf.lanes.literature.source_scope import WORK_UNIT_MEDIA_TYPE
from tsdaf.workflow.literature_unit_runtime import (
    LITERATURE_ROUTE_STAGE,
    LITERATURE_ROUTE_STAGE_VERSION,
    PACKAGE_MEDIA_TYPE,
    LiteratureRouteGroupReceipt,
    LiteratureRouteGroupRun,
    LiteratureGroupReadingsRun,
    resource_settings,
    run_verified_literature_unit,
    unit_failure,
)
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner, LocalRuntimeError

__all__ = (
    "LITERATURE_ROUTE_STAGE",
    "LITERATURE_ROUTE_STAGE_VERSION",
    "LiteratureRouteGroupReceipt",
    "LiteratureRouteGroupRun",
    "run_literature_route_group",
)


def run_literature_route_group(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    package_root: Path,
    route_execution_id: str,
    literature_execution_id: str,
    document_revision_id: str,
    route_group_id: str,
    provider_binding: ProviderBinding,
    text_client: LaneInteractionClient,
    visual_client: LaneInteractionClient,
    gate_client: LaneInteractionClient,
    provider_is_live: bool,
    live: bool = False,
    finish_execution: bool = True,
    machine_profile: MachineProfile = MachineProfile.V1,
    source_status: PaperSourceStatus = UNKNOWN_SOURCE_STATUS,
    readers_only: bool = False,
) -> LiteratureRouteGroupRun | LiteratureGroupReadingsRun | TypedFailure:
    """Run or reuse one literature route group from sealed Router authority to Gate 1.

    `source_status` says whether a supporting information is registered (E83); whether
    the main text has an experimental part is decided here from the sealed cut when the
    Router execution has one, and remains unknown when it has none.
    """

    machine_profile.require_production()
    if not isinstance(workspace, Path) or not isinstance(package_root, Path):
        raise TypeError("workspace and package_root must be pathlib.Path values")
    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    for label, value in (
        ("route_execution_id", route_execution_id),
        ("literature_execution_id", literature_execution_id),
        ("document_revision_id", document_revision_id),
        ("route_group_id", route_group_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a nonempty string")
    if type(provider_binding) is not ProviderBinding:
        raise TypeError("provider_binding must be a ProviderBinding")
    if not isinstance(provider_is_live, bool) or not isinstance(live, bool):
        raise TypeError("provider_is_live and live must be bool values")
    if not isinstance(finish_execution, bool):
        raise TypeError("finish_execution must be a bool value")

    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)

    package_bytes = canonical_json_bytes(
        package.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    settings = {
        "document_revision_id": document_revision_id,
        "route_group_id": route_group_id,
        **resource_settings(machine_profile),
    }

    phase = "router_verification"
    try:
        terminal_manifest_ref = runner.finish_execution(
            route_execution_id, ExecutionStatus.SUCCEEDED
        )
        package_artifact = store.put_bytes(package_bytes, media_type=PACKAGE_MEDIA_TYPE)
        handoffs = verify_router_handoff_bundle(
            package=package,
            router_execution_id=route_execution_id,
            terminal_manifest_ref=terminal_manifest_ref,
            object_store=store,
            runner=runner,
        )
        phase = "source_compilation"
        work_unit = compile_route_group_root(
            package=package,
            handoffs=handoffs,
            document_revision_id=document_revision_id,
            route_group_id=route_group_id,
        )
        phase = "source_inventory"
        inventory = compile_source_inventory(package=package, handoffs=handoffs)
        phase = "source_persistence"
        inventory_ref = store.put_bytes(
            canonical_json_bytes(inventory.model_dump(mode="json")),
            media_type=SOURCE_INVENTORY_MEDIA_TYPE,
        )
        work_unit_ref = store.put_bytes(
            canonical_json_bytes(work_unit.model_dump(mode="json")), media_type=WORK_UNIT_MEDIA_TYPE
        )
    except (LocalRuntimeError, ValueError) as error:
        return unit_failure(error, phase=phase)
    input_artifacts: tuple[ArtifactRef, ...] = (
        package_artifact,
        terminal_manifest_ref,
        inventory_ref,
        work_unit_ref,
    )

    return run_verified_literature_unit(
        workspace=workspace,
        store=store,
        runner=runner,
        package=package,
        package_root=package_root,
        authority_execution_id=route_execution_id,
        execution_id=literature_execution_id,
        work_unit=work_unit,
        work_unit_ref=work_unit_ref,
        inventory=inventory,
        inventory_ref=inventory_ref,
        input_artifacts=input_artifacts,
        settings=settings,
        machine_profile=machine_profile,
        provider_binding=provider_binding,
        text_client=text_client,
        visual_client=visual_client,
        gate_client=gate_client,
        provider_is_live=provider_is_live,
        live=live,
        finish_execution=finish_execution,
        readers_only=readers_only,
        source_status=replace(
            source_status,
            has_experimental_part=(
                handoffs.units.has_experimental_part if handoffs.units is not None
                else (handoffs.partition.cut_block_id is not None
                      if handoffs.partition is not None else source_status.has_experimental_part)
            ),
        ),
    )
