"""Immutable canonical source catalogue; actual supply is recorded by reader requests."""

from __future__ import annotations

import hashlib

from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.lanes.document.reading_units import partition_clean_view
from tsdaf.lanes.literature.bootstrap import VerifiedRouterHandoffs
from tsdaf.lanes.literature.layer0 import plan_layer0_shards
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans
from tsdaf.lanes.literature.source_inventory_models import (
    EXPERIMENTAL_PART_EXCLUSION,
    SourceInventoryEntry,
    _digest,
)
from tsdaf.lanes.literature.source_inventory_models import (
    LiteratureSourceInventory as LiteratureSourceInventory,
)
from tsdaf.lanes.literature.source_scope import (
    _validated_package,
    resolve_route_scope,
)
from tsdaf.lanes.literature.source_scope import (
    resolve_fusion_read_authority as resolve_fusion_read_authority,
)
from tsdaf.lanes.literature.text_extent import resolve_text_read_extent

SOURCE_INVENTORY_MEDIA_TYPE = "application/vnd.tsdaf.literature-source-inventory+json"


def compile_source_inventory(
    *,
    package: CanonicalPaperPackage,
    handoffs: VerifiedRouterHandoffs,
) -> LiteratureSourceInventory:
    """Read exact Router authority and index every canonical source without dropping any."""

    package = _validated_package(package)
    bootstrap = handoffs.bootstrap
    routing = handoffs.routing
    groups = tuple(group.route_group_id for group in routing.route_groups)
    shards = plan_layer0_shards(package)
    owners = {
        source_id: shard.shard_id
        for shard in shards
        for source_id in (*shard.owned_block_ids, *shard.owned_visual_ids)
    }
    text_groups: dict[str, list[str]] = {}
    visual_groups: dict[str, list[str]] = {}
    # A root compiler is the authority owner; inventory reports exactly that policy:
    # a route group's initial selection is the narrative part, the cleaned document
    # before the experimental split's cut (ledger E47). The experimental part stays in
    # the inventory, excluded for the route group (2026-09-23: unconditionally now that
    # reaction windows, which once reopened it, are retired).
    narrative_block_ids, experimental_block_ids = partition_clean_view(package, handoffs.partition)
    experimental = set(experimental_block_ids)
    for group in groups:
        scope = resolve_route_scope(
            package=package,
            handoffs=handoffs,
            route_group_id=group,
        )
        extent = resolve_text_read_extent(
            package=package,
            anchor_block_ids=scope.ordered_text_seed_ids,
            readable_block_ids=narrative_block_ids,
        )
        for source_id in extent.ordered_block_ids:
            text_groups.setdefault(source_id, []).append(group)
        for source_id in (*scope.ordered_visual_seed_ids, *scope.ordered_context_visual_ids):
            visual_groups.setdefault(source_id, []).append(group)
    entries: list[SourceInventoryEntry] = []
    for block in package.blocks:
        initial = tuple(text_groups.get(block.block_id, ()))
        sentence_count = len(block_sentence_spans(block.raw_text))
        referenceable = bool(sentence_count)
        excluded = not initial
        entries.append(
            SourceInventoryEntry(
                source_id=block.block_id,
                source_kind="TEXT_BLOCK",
                source_sha256=hashlib.sha256(block.raw_text.encode("utf-8")).hexdigest(),
                shard_id=owners[block.block_id],
                router_role=None,
                initial_route_group_ids=initial,
                initially_excluded=excluded,
                exclusion_reason=(
                    (
                        EXPERIMENTAL_PART_EXCLUSION
                        if block.block_id in experimental
                        else "CLEANING_EXCLUDED"
                        if referenceable
                        else "NO_REFERENCEABLE_TEXT"
                    )
                    if excluded
                    else None
                ),
                readable=referenceable,
                sentence_count=sentence_count,
            )
        )
    decisions = {item.visual_id: item.final_decision for item in routing.visual_decisions}
    for visual in package.visual_assets:
        entries.append(
            SourceInventoryEntry(
                source_id=visual.visual_id,
                source_kind="VISUAL_ASSET",
                source_sha256=visual.asset.sha256,
                shard_id=owners[visual.visual_id],
                router_role=str(decisions[visual.visual_id]),
                initial_route_group_ids=tuple(visual_groups.get(visual.visual_id, ())),
                initially_excluded=False,
                exclusion_reason=None,
                readable=True,
            )
        )
    provisional = LiteratureSourceInventory.model_construct(
        schema_version="tsdaf.literature-source-inventory.v1",
        paper_id=package.manifest.paper_id,
        package_sha256=package.canonical_content_sha256,
        bootstrap_id=bootstrap.bootstrap_id,
        bootstrap_sha256=bootstrap.bootstrap_sha256,
        route_group_ids=groups,
        shards=shards,
        entries=tuple(entries),
        inventory_sha256="0" * 64,
    )
    return LiteratureSourceInventory.model_validate(
        {**provisional.model_dump(), "inventory_sha256": _digest(provisional, "inventory_sha256")}
    )
