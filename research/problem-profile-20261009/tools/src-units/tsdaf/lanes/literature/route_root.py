"""Sealed Router execution boundary for a complete private route-group root."""

from __future__ import annotations

from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.reading_units import partition_clean_view
from tsdaf.lanes.literature.bootstrap import VerifiedRouterHandoffs, verify_router_handoff_bundle
from tsdaf.lanes.literature.host_ports import (
    ExecutionClosureVerifier,
    VerifiedArtifactStore,
)
from tsdaf.lanes.literature.source_scope import (
    ScientificSelectorKind,
    SelectedSubjectScope,
    VerifiedWorkUnitInput,
    WorkUnitKind,
    compile_root_work_unit,
    resolve_route_scope,
)
from tsdaf.lanes.literature.text_extent import resolve_text_read_extent

__all__ = (
    "compile_route_group_root",
    "resolve_verified_route_group_ids",
    "validate_route_group_root_shape",
)


def validate_route_group_root_shape(work_unit: VerifiedWorkUnitInput) -> VerifiedWorkUnitInput:
    """Revalidate root shape; only the sealed compiler proves complete source authority."""

    if type(work_unit) is not VerifiedWorkUnitInput:
        raise TypeError("work_unit must be a VerifiedWorkUnitInput")
    exact = VerifiedWorkUnitInput.model_validate(
        work_unit.model_dump(mode="python", by_alias=True, exclude_none=False)
    )
    scope = exact.selected_subject_scope
    if (
        exact.depth != 1
        or exact.parent_work_unit_id is not None
        or scope.work_unit_kind is not WorkUnitKind.ROUTE_GROUP
        or scope.selector_kind is not ScientificSelectorKind.ROUTE_GROUP
        or scope.selector_values != (exact.route_group_id,)
        or scope.local_subject_keys
    ):
        raise ValueError("initial literature lanes require an unprefilled route-group root")
    return exact


def compile_route_group_root(
    *,
    package: CanonicalPaperPackage,
    handoffs: VerifiedRouterHandoffs,
    document_revision_id: str,
    route_group_id: str,
) -> VerifiedWorkUnitInput:
    """Verify one sealed Router execution and compile its complete unprefilled route root."""

    route_scope = resolve_route_scope(
        package=package,
        handoffs=handoffs,
        route_group_id=route_group_id,
    )
    # The root reads the narrative part: the cleaned document before the experimental
    # split's cut. `compile_root_work_unit` re-derives the same extent and refuses
    # anything outside it, so the request here must be built from the same rule.
    narrative_block_ids, _ = partition_clean_view(package, handoffs.partition)
    extent = resolve_text_read_extent(
        package=package,
        anchor_block_ids=route_scope.ordered_text_seed_ids,
        readable_block_ids=narrative_block_ids,
    )
    return compile_root_work_unit(
        package=package,
        handoffs=handoffs,
        document_revision_id=document_revision_id,
        route_group_id=route_group_id,
        requested_text_block_ids=extent.ordered_block_ids,
        requested_visual_ids=(
            *route_scope.ordered_visual_seed_ids,
            *route_scope.ordered_context_visual_ids,
        ),
        selected_subject_scope=SelectedSubjectScope(
            work_unit_kind=WorkUnitKind.ROUTE_GROUP,
            selector_kind=ScientificSelectorKind.ROUTE_GROUP,
            selector_values=(route_group_id,),
            local_subject_keys=(),
        ),
        source_order=0,
        ordinal=0,
    )


def resolve_verified_route_group_ids(
    *,
    package: CanonicalPaperPackage,
    router_execution_id: str,
    terminal_manifest_ref: ArtifactRef,
    object_store: VerifiedArtifactStore,
    runner: ExecutionClosureVerifier,
) -> tuple[str, ...]:
    """Enumerate the route groups one sealed Router execution authorized.

    A paper-level run needs the same proof a single root needs -- the Router execution
    is terminal and its handoffs close -- before it may believe how many routes the
    paper has, so this goes through the same bundle verification.
    """

    handoffs = verify_router_handoff_bundle(
        package=package,
        router_execution_id=router_execution_id,
        terminal_manifest_ref=terminal_manifest_ref,
        object_store=object_store,
        runner=runner,
    )
    return tuple(group.route_group_id for group in handoffs.routing.route_groups)
