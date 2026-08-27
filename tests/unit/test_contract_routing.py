"""Domain tests for asset-level routing and derived disposition."""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any

import pytest
from pydantic import ValidationError


def _modules() -> tuple[Any, Any, Any]:
    routing = importlib.import_module("tsdaf.contracts.routing")
    source = importlib.import_module("tsdaf.contracts.source")
    package = importlib.import_module("tsdaf.contracts.package")
    return source, package, routing


def _sha(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_direct_routing_schema_exposes_route_group_contract() -> None:
    _, _, routing = _modules()
    handoff = importlib.import_module("tsdaf.contracts.handoff")

    schema = routing.ArticleRoutingResult.model_json_schema(mode="validation")
    route_group_schema = schema["$defs"]["RouteGroup"]
    selection_schema = schema["$defs"]["CoarseTextSelection"]
    image_item_schema = handoff.RouteImageItem.model_json_schema(mode="validation")
    pattern = r"^RG(?:00[1-9]|0[1-9][0-9]|[1-9][0-9]{2})$"

    assert route_group_schema["properties"]["route_group_id"]["pattern"] == pattern
    assert selection_schema["properties"]["route_group_id"]["pattern"] == pattern
    assert image_item_schema["properties"]["route_group_id"]["pattern"] == pattern
    assert (
        schema["properties"]["route_groups"]["description"]
        == "Route groups start at RG001 and increment contiguously by one in tuple order."
    )
    assert (
        schema["properties"]["coarse_text_selections"]["description"]
        == "Each coarse text selection reuses the exact route-group ID declared in route_groups."
    )


@pytest.mark.parametrize(
    ("route_group_id", "valid"),
    [("RG001", True), ("RG999", True), ("RG000", False), ("rg001", False), ("RG1000", False)],
)
def test_route_group_id_runtime_boundary_is_exact(route_group_id: str, valid: bool) -> None:
    _, _, routing = _modules()
    payload = {
        "route_group_id": route_group_id,
        "target_names": ("Target A",),
        "synthesis_type": routing.ArticleType.TOTAL_SYNTHESIS,
        "attributes": (),
        "ordered_visual_ids": ("MAIN-P0000-A0000",),
        "core_text_block_ids": ("MAIN-P0000-B0000",),
    }

    if valid:
        assert routing.RouteGroup.model_validate(payload).route_group_id == route_group_id
    else:
        with pytest.raises(ValidationError):
            routing.RouteGroup.model_validate(payload)


def _artifact(source: Any, digit: str, path: str, media_type: str) -> Any:
    return source.ArtifactRef(
        sha256=digit * 64,
        media_type=media_type,
        byte_size=10,
        relative_path=path,
    )


def _package(*, decorative: tuple[str, ...] = ()) -> Any:
    source, package, _ = _modules()
    source_pdf = _artifact(source, "0", "source/paper.pdf", "application/pdf")
    document = _artifact(source, "1", "normalized/document.json", "application/json")
    blocks_ref = _artifact(source, "2", "normalized/blocks.jsonl", "application/x-ndjson")
    asset_0 = _artifact(source, "3", "assets/route-a.png", "image/png")
    asset_1 = _artifact(source, "4", "assets/route-b.png", "image/png")
    manifest = package.PaperManifest(
        schema_version="tsdaf.paper-package.v1",
        paper_id="paper-001",
        display_name="Paper One",
        source_pdf=source_pdf,
        normalized_document=document,
        normalized_blocks=blocks_ref,
        assets=(asset_0, asset_1),
    )
    block_0 = package.CanonicalTextBlock(
        block_id="MAIN-P0000-B0000",
        page_index=0,
        reading_order=0,
        block_type=package.TextBlockType.PARAGRAPH,
        raw_text="First route paragraph.",
        section_path=("Results",),
        source=source.SourceLocator(
            document_id="paper-001",
            page_index=0,
            bbox=None,
            native_block_index=0,
            source_asset_ref=None,
        ),
    )
    block_1 = package.CanonicalTextBlock(
        block_id="MAIN-P0001-B0000",
        page_index=1,
        reading_order=0,
        block_type=package.TextBlockType.PARAGRAPH,
        raw_text="Second route paragraph.",
        section_path=("Results",),
        source=source.SourceLocator(
            document_id="paper-001",
            page_index=1,
            bbox=None,
            native_block_index=0,
            source_asset_ref=None,
        ),
    )
    bbox = source.BoundingBox(
        x0=10,
        y0=20,
        x1=900,
        y1=950,
        coordinate_space="normalized_1000",
    )
    visual_0 = package.CanonicalVisualAsset(
        visual_id="MAIN-P0000-A0000",
        asset=asset_0,
        page_index=0,
        bbox=bbox,
        width=640,
        height=480,
        native_caption="Scheme 1a",
        source=source.SourceLocator(
            document_id="paper-001",
            page_index=0,
            bbox=bbox,
            native_block_index=None,
            source_asset_ref=asset_0,
        ),
        visual_group_id="MAIN-P0000-VG0000",
    )
    visual_1 = package.CanonicalVisualAsset(
        visual_id="MAIN-P0001-A0000",
        asset=asset_1,
        page_index=1,
        bbox=None,
        width=800,
        height=600,
        native_caption="Scheme 1b",
        source=source.SourceLocator(
            document_id="paper-001",
            page_index=1,
            bbox=None,
            native_block_index=None,
            source_asset_ref=asset_1,
        ),
        visual_group_id="MAIN-P0000-VG0000",
    )
    group = package.VisualGroup(
        visual_group_id="MAIN-P0000-VG0000",
        member_visual_ids=(visual_0.visual_id, visual_1.visual_id),
        label_candidates=("Scheme 1",),
        caption_block_ids=(),
        caption_text=None,
        association_status="NO_CAPTION",
    )
    suppressions = tuple(
        package.VisualSuppressionRecord(
            visual_id=visual_id,
            reason="DECORATIVE_ASSET",
            rule_version="decorative-v1",
        )
        for visual_id in decorative
    )
    clean_view = package.CleanDocumentView(
        included_block_ids=(block_0.block_id, block_1.block_id),
        suppressed=(),
    )
    normalized_hash = _sha(manifest.model_dump(mode="json"))
    projection = {
        "schema_version": "tsdaf.canonical-paper-package.v1",
        "manifest": manifest.model_dump(mode="json"),
        "canonicalizer_version": "canonicalizer-v1",
        "normalized_package_sha256": normalized_hash,
        "blocks": tuple(block.model_dump(mode="json") for block in (block_0, block_1)),
        "visual_assets": tuple(visual.model_dump(mode="json") for visual in (visual_0, visual_1)),
        "visual_groups": (group.model_dump(mode="json"),),
        "visual_suppressions": tuple(item.model_dump(mode="json") for item in suppressions),
        "clean_view": clean_view.model_dump(mode="json"),
    }
    return package.CanonicalPaperPackage(
        schema_version="tsdaf.canonical-paper-package.v1",
        manifest=manifest,
        canonicalizer_version="canonicalizer-v1",
        normalized_package_sha256=normalized_hash,
        canonical_content_sha256=_sha(projection),
        blocks=(block_0, block_1),
        visual_assets=(visual_0, visual_1),
        visual_groups=(group,),
        visual_suppressions=suppressions,
        clean_view=clean_view,
    )


def _provenance(routing: Any) -> Any:
    return routing.RouterProvenance(
        schema_version="tsdaf.router-provenance.v1",
        router_id="article-router",
        router_version="1.0.0",
        provider_id="provider",
        model_id="model",
        provider_profile_id="profile",
        provider_profile_version="1.0.0",
        provider_profile_sha256="5" * 64,
        prompt_id="article-router",
        prompt_version="1.0.0",
        prompt_sha256="6" * 64,
        contract_schema_bundle_version="tsdaf.contract-schema-bundle.v1",
        contract_schema_sha256="7" * 64,
        taxonomy_sha256="8" * 64,
        tool_schema_version="tsdaf.article-router-tool.v1",
        tool_schema_sha256="9" * 64,
    )


def _decision(routing: Any, visual_id: str, initial: Any, final: Any, triage: Any = None) -> Any:
    return routing.VisualAssetDecision(
        visual_id=visual_id,
        visual_group_id="MAIN-P0000-VG0000",
        initial_decision=initial,
        triage_result=triage,
        final_decision=final,
    )


def _forward_result(package_value: Any) -> Any:
    _, _, routing = _modules()
    group = routing.RouteGroup(
        route_group_id="RG001",
        target_names=("Target A",),
        synthesis_type=routing.ArticleType.TOTAL_SYNTHESIS,
        attributes=(routing.RouteAttribute.ASYMMETRIC,),
        ordered_visual_ids=("MAIN-P0000-A0000", "MAIN-P0001-A0000"),
        core_text_block_ids=("MAIN-P0000-B0000", "MAIN-P0001-B0000"),
    )
    selections = (
        routing.CoarseTextSelection(
            route_group_id="RG001",
            block_id="MAIN-P0000-B0000",
            role=routing.CoarseTextRole.ROUTE_CORE,
        ),
        routing.CoarseTextSelection(
            route_group_id="RG001",
            block_id="MAIN-P0001-B0000",
            role=routing.CoarseTextRole.ROUTE_CORE,
        ),
    )
    decisions = tuple(
        _decision(
            routing,
            visual.visual_id,
            routing.VisualDecision.FORWARD_ROUTE,
            routing.VisualDecision.FORWARD_ROUTE,
        )
        for visual in package_value.visual_assets
    )
    return routing.ArticleRoutingResult(
        schema_version="tsdaf.article-routing-result.v1",
        paper_id=package_value.manifest.paper_id,
        execution_id="execution-001",
        canonical_package_sha256=package_value.canonical_content_sha256,
        article_type=routing.ArticleType.TOTAL_SYNTHESIS,
        disposition=routing.RoutingDisposition.ROUTED,
        route_groups=(group,),
        visual_decisions=decisions,
        coarse_text_selections=selections,
        provenance=_provenance(routing),
    )


def _empty_result(
    package_value: Any,
    decisions: tuple[Any, ...],
    disposition: Any,
    *,
    article_type: Any | None = None,
) -> Any:
    _, _, routing = _modules()
    return routing.ArticleRoutingResult(
        schema_version="tsdaf.article-routing-result.v1",
        paper_id=package_value.manifest.paper_id,
        execution_id="execution-001",
        canonical_package_sha256=package_value.canonical_content_sha256,
        article_type=article_type or routing.ArticleType.TOTAL_SYNTHESIS,
        disposition=disposition,
        route_groups=(),
        visual_decisions=decisions,
        coarse_text_selections=(),
        provenance=_provenance(routing),
    )


def test_complete_forward_routing_closes_against_package() -> None:
    package_value = _package()
    result = _forward_result(package_value)

    assert result.validate_against(package_value) is None


@pytest.mark.parametrize(
    ("initial", "triage", "final"),
    [
        ("UNCERTAIN", None, "UNCERTAIN"),
        ("NEEDS_VISUAL_VERIFICATION", None, "OTHER"),
        ("OTHER", "OTHER", "OTHER"),
        ("NEEDS_VISUAL_VERIFICATION", "NEEDS_VISUAL_VERIFICATION", "OTHER"),
        ("OTHER", None, "MECHANISM"),
        ("NEEDS_VISUAL_VERIFICATION", "OTHER", "MECHANISM"),
    ],
)
def test_visual_decision_rejects_invalid_initial_triage_final_trace(
    initial: str, triage: str | None, final: str
) -> None:
    _, _, routing = _modules()
    payload = {
        "visual_id": "MAIN-P0000-A0000",
        "visual_group_id": "MAIN-P0000-VG0000",
        "initial_decision": initial,
        "triage_result": triage,
        "final_decision": final,
    }

    with pytest.raises(ValidationError):
        routing.VisualAssetDecision.model_validate_json(json.dumps(payload))


def test_decorative_resolved_nonforward_path_needs_no_paid_triage() -> None:
    package_value = _package(decorative=("MAIN-P0000-A0000",))
    _, _, routing = _modules()
    decisions = (
        _decision(
            routing,
            "MAIN-P0000-A0000",
            routing.VisualDecision.MECHANISM,
            routing.VisualDecision.MECHANISM,
        ),
        _decision(
            routing,
            "MAIN-P0001-A0000",
            routing.VisualDecision.OTHER,
            routing.VisualDecision.OTHER,
        ),
    )
    result = _empty_result(package_value, decisions, routing.RoutingDisposition.REJECTED)

    assert result.validate_against(package_value) is None


def test_decorative_forward_route_requires_and_accepts_complete_override_trace() -> None:
    package_value = _package(decorative=("MAIN-P0000-A0000",))
    _, _, routing = _modules()
    group = routing.RouteGroup(
        route_group_id="RG001",
        target_names=("Target A",),
        synthesis_type=routing.ArticleType.TOTAL_SYNTHESIS,
        attributes=(),
        ordered_visual_ids=("MAIN-P0000-A0000",),
        core_text_block_ids=("MAIN-P0000-B0000",),
    )
    decisions = (
        _decision(
            routing,
            "MAIN-P0000-A0000",
            routing.VisualDecision.NEEDS_VISUAL_VERIFICATION,
            routing.VisualDecision.FORWARD_ROUTE,
            routing.VisualDecision.FORWARD_ROUTE,
        ),
        _decision(
            routing,
            "MAIN-P0001-A0000",
            routing.VisualDecision.OTHER,
            routing.VisualDecision.OTHER,
        ),
    )
    result = routing.ArticleRoutingResult(
        schema_version="tsdaf.article-routing-result.v1",
        paper_id="paper-001",
        execution_id="execution-001",
        canonical_package_sha256=package_value.canonical_content_sha256,
        article_type=routing.ArticleType.TOTAL_SYNTHESIS,
        disposition=routing.RoutingDisposition.ROUTED,
        route_groups=(group,),
        visual_decisions=decisions,
        coarse_text_selections=(
            routing.CoarseTextSelection(
                route_group_id="RG001",
                block_id="MAIN-P0000-B0000",
                role=routing.CoarseTextRole.ROUTE_CORE,
            ),
        ),
        provenance=_provenance(routing),
    )

    assert result.validate_against(package_value) is None


def test_unresolved_triage_is_preserved_as_review_without_route() -> None:
    package_value = _package(decorative=("MAIN-P0000-A0000",))
    _, _, routing = _modules()
    decisions = (
        _decision(
            routing,
            "MAIN-P0000-A0000",
            routing.VisualDecision.NEEDS_VISUAL_VERIFICATION,
            routing.VisualDecision.UNCERTAIN,
            routing.VisualDecision.UNCERTAIN,
        ),
        _decision(
            routing,
            "MAIN-P0001-A0000",
            routing.VisualDecision.OTHER,
            routing.VisualDecision.OTHER,
        ),
    )
    result = _empty_result(package_value, decisions, routing.RoutingDisposition.REVIEW_REQUIRED)

    assert result.validate_against(package_value) is None


@pytest.mark.parametrize(
    "mutation", ["direct_forward", "nonforward_in_route", "uncertain_in_route"]
)
def test_decorative_assets_cannot_enter_routes_without_complete_override(mutation: str) -> None:
    package_value = _package(decorative=("MAIN-P0000-A0000",))
    _, _, routing = _modules()
    base = _forward_result(package_value)
    if mutation == "direct_forward":
        decisions = base.visual_decisions
    elif mutation == "nonforward_in_route":
        decisions = (
            _decision(
                routing,
                "MAIN-P0000-A0000",
                routing.VisualDecision.OTHER,
                routing.VisualDecision.OTHER,
            ),
            base.visual_decisions[1],
        )
    else:
        decisions = (
            _decision(
                routing,
                "MAIN-P0000-A0000",
                routing.VisualDecision.NEEDS_VISUAL_VERIFICATION,
                routing.VisualDecision.UNCERTAIN,
                routing.VisualDecision.UNCERTAIN,
            ),
            base.visual_decisions[1],
        )
    candidate = base.model_copy(update={"visual_decisions": decisions})

    with pytest.raises(ValueError):
        candidate.validate_against(package_value)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_decision",
        "duplicate_decision",
        "reordered_decisions",
        "group_mismatch",
        "stale_hash",
        "bad_route_sequence",
        "dangling_visual",
        "dangling_block",
        "route_without_core",
        "core_mismatch",
        "selection_order",
    ],
)
def test_routing_rejects_inventory_reference_and_order_breaks(mutation: str) -> None:
    package_value = _package()
    _, _, routing = _modules()
    base = _forward_result(package_value)
    candidate = base

    if mutation == "missing_decision":
        candidate = base.model_copy(update={"visual_decisions": base.visual_decisions[:-1]})
    elif mutation == "duplicate_decision":
        candidate = base.model_copy(
            update={"visual_decisions": (base.visual_decisions[0], base.visual_decisions[0])}
        )
    elif mutation == "reordered_decisions":
        candidate = base.model_copy(
            update={"visual_decisions": tuple(reversed(base.visual_decisions))}
        )
    elif mutation == "group_mismatch":
        bad = base.visual_decisions[0].model_copy(update={"visual_group_id": "MAIN-P0000-VG0001"})
        candidate = base.model_copy(update={"visual_decisions": (bad, base.visual_decisions[1])})
    elif mutation == "stale_hash":
        candidate = base.model_copy(update={"canonical_package_sha256": "a" * 64})
    elif mutation == "bad_route_sequence":
        bad_group = base.route_groups[0].model_copy(update={"route_group_id": "RG002"})
        candidate = base.model_copy(update={"route_groups": (bad_group,)})
    elif mutation == "dangling_visual":
        bad_group = base.route_groups[0].model_copy(
            update={"ordered_visual_ids": ("MAIN-P0000-A0001",)}
        )
        candidate = base.model_copy(update={"route_groups": (bad_group,)})
    elif mutation == "dangling_block":
        bad_group = base.route_groups[0].model_copy(
            update={"core_text_block_ids": ("MAIN-P0000-B0001",)}
        )
        candidate = base.model_copy(update={"route_groups": (bad_group,)})
    elif mutation == "route_without_core":
        candidate = base.model_copy(update={"coarse_text_selections": ()})
    elif mutation == "core_mismatch":
        bad_group = base.route_groups[0].model_copy(
            update={"core_text_block_ids": ("MAIN-P0000-B0000",)}
        )
        candidate = base.model_copy(update={"route_groups": (bad_group,)})
    elif mutation == "selection_order":
        candidate = base.model_copy(
            update={"coarse_text_selections": tuple(reversed(base.coarse_text_selections))}
        )

    with pytest.raises(ValueError):
        candidate.validate_against(package_value)


@pytest.mark.parametrize("field", ["target_names", "attributes", "ordered_visual_ids"])
def test_route_group_rejects_duplicate_semantic_values(field: str) -> None:
    _, _, routing = _modules()
    payload: dict[str, object] = {
        "route_group_id": "RG001",
        "target_names": ("Target A",),
        "synthesis_type": routing.ArticleType.TOTAL_SYNTHESIS,
        "attributes": (routing.RouteAttribute.ASYMMETRIC,),
        "ordered_visual_ids": ("MAIN-P0000-A0000",),
        "core_text_block_ids": ("MAIN-P0000-B0000",),
    }
    payload[field] = (payload[field][0], payload[field][0])

    with pytest.raises(ValidationError):
        routing.RouteGroup.model_validate(payload)


def test_duplicate_coarse_selection_is_rejected() -> None:
    package_value = _package()
    _, _, routing = _modules()
    base = _forward_result(package_value)
    duplicate = (base.coarse_text_selections[0], base.coarse_text_selections[0])

    with pytest.raises(ValidationError):
        routing.ArticleRoutingResult(
            **{
                **base.model_dump(mode="python"),
                "coarse_text_selections": duplicate,
            }
        )


def test_not_in_scope_allows_only_resolved_empty_rejection() -> None:
    package_value = _package()
    _, _, routing = _modules()
    resolved = tuple(
        _decision(
            routing,
            visual.visual_id,
            routing.VisualDecision.OTHER,
            routing.VisualDecision.OTHER,
        )
        for visual in package_value.visual_assets
    )
    valid = _empty_result(
        package_value,
        resolved,
        routing.RoutingDisposition.REJECTED,
        article_type=routing.ArticleType.NOT_IN_SCOPE,
    )
    assert valid.validate_against(package_value) is None

    with pytest.raises(ValidationError):
        routing.ArticleRoutingResult(
            **{
                **_forward_result(package_value).model_dump(mode="python"),
                "article_type": routing.ArticleType.NOT_IN_SCOPE,
            }
        )


def test_caller_cannot_contradict_derived_disposition() -> None:
    package_value = _package()
    _, _, routing = _modules()
    payload = _forward_result(package_value).model_dump(mode="python")
    payload["disposition"] = routing.RoutingDisposition.REJECTED

    with pytest.raises(ValidationError):
        routing.ArticleRoutingResult.model_validate(payload)
