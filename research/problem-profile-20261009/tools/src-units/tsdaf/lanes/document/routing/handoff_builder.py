"""Deterministic materialization of proposed downstream handoffs."""

from __future__ import annotations

import hashlib

from tsdaf.contracts.handoff import (
    CoarseTextHandoff,
    CoarseTextHandoffItem,
    RouteImageItem,
    VisualRoutingHandoff,
)
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.routing import ArticleRoutingResult, VisualDecision
from tsdaf.contracts.schema import canonical_json_bytes

__all__ = ("build_proposed_handoffs",)


def build_proposed_handoffs(
    *,
    package: CanonicalPaperPackage,
    routing_result: ArticleRoutingResult,
) -> tuple[VisualRoutingHandoff, CoarseTextHandoff]:
    """Build immutable proposed visual and coarse-text handoffs."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if type(routing_result) is not ArticleRoutingResult:
        raise TypeError("routing_result must be an ArticleRoutingResult")

    routing_result.validate_against(package)

    eligible_ids = tuple(
        decision.visual_id
        for decision in routing_result.visual_decisions
        if decision.final_decision is VisualDecision.FORWARD_ROUTE
    )
    routed_ids = tuple(
        visual_id
        for route_group in routing_result.route_groups
        for visual_id in route_group.ordered_visual_ids
    )
    if len(routed_ids) != len(set(routed_ids)) or set(routed_ids) != set(eligible_ids):
        raise ValueError("route groups do not match final forward visual inventory")

    block_by_id = {block.block_id: block for block in package.blocks}
    visual_by_id = {visual.visual_id: visual for visual in package.visual_assets}
    group_by_id = {group.visual_group_id: group for group in package.visual_groups}

    visual_items: list[RouteImageItem] = []
    for route_group in routing_result.route_groups:
        for sequence_index, visual_id in enumerate(route_group.ordered_visual_ids):
            visual = visual_by_id[visual_id]
            visual_group = group_by_id[visual.visual_group_id]
            # Routing assigns the visual to a route, not to a nearest paragraph.
            dimensions = (
                None
                if visual.width is None or visual.height is None
                else (visual.width, visual.height)
            )
            visual_items.append(
                RouteImageItem(
                    route_image_item_id=f"RIMG{len(visual_items) + 1:03d}",
                    visual_group_id=visual.visual_group_id,
                    source_asset=visual.asset,
                    page_index=visual.page_index,
                    bbox=visual.bbox,
                    dimensions=dimensions,
                    figure_or_scheme_label=(
                        visual_group.label_candidates[0] if visual_group.label_candidates else None
                    ),
                    caption=visual_group.caption_text or visual.native_caption or None,
                    related_block_ids=(),
                    related_original_paragraphs=(),
                    route_group_id=route_group.route_group_id,
                    sequence_index=sequence_index,
                    router_execution_id=routing_result.execution_id,
                )
            )

    visual_handoff = VisualRoutingHandoff(
        schema_version="tsdaf.visual-routing-handoff.v1",
        paper_id=routing_result.paper_id,
        execution_id=routing_result.execution_id,
        article_type=routing_result.article_type,
        route_groups=routing_result.route_groups,
        items=tuple(visual_items),
        provenance=routing_result.provenance,
        status="PROPOSED",
    )

    routing_bytes = canonical_json_bytes(
        routing_result.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    coarse_handoff = CoarseTextHandoff(
        schema_version="tsdaf.coarse-text-handoff.v1",
        paper_id=routing_result.paper_id,
        execution_id=routing_result.execution_id,
        canonical_package_sha256=package.canonical_content_sha256,
        routing_result_sha256=hashlib.sha256(routing_bytes).hexdigest(),
        items=tuple(
            CoarseTextHandoffItem(
                selection=selection,
                block=block_by_id[selection.block_id],
            )
            for selection in routing_result.coarse_text_selections
        ),
        provenance=routing_result.provenance,
    )

    visual_handoff.validate_against(package, routing_result)
    coarse_handoff.validate_against(package, routing_result)
    return visual_handoff, coarse_handoff
