"""Integration acceptance tests for proposed handoffs and their release gate."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PAPER_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "paper_packages"
SEMANTIC_FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "providers" / "fake" / "article_router_v1.json"
)
PAPER_IDS = (
    "alchivemycin_A",
    "hyperfirin",
    "ineleganolide",
    "kalmanol",
    "principinol_B",
)
EXPECTED_ITEM_COUNTS = (3, 2, 1, 2, 1)
EXPECTED_FORWARD_IDS = (
    "MAIN-P0004-A0000",
    "MAIN-P0005-A0000",
    "MAIN-P0006-A0000",
    "MAIN-P0001-A0001",
    "MAIN-P0002-A0000",
    "MAIN-P0001-A0000",
    "MAIN-P0001-A0001",
    "MAIN-P0002-A0000",
    "MAIN-P0001-A0000",
)
EXPECTED_SOURCE_SHA256S = (
    "a3057bc392aca32054932b4677850e965f10627995a3b5e0df54f254cd158f17",
    "9335d15e09fc7d4889f316c95ba1db3a7418d230af8ddc6e3c307e58b4f9a45b",
    "4d64043f869c5417aa84c02b9634b0ec859ce4af4588dce9f431b9682beb58b6",
    "5983f3b72b704d3ad0f4d94c7f03287f2577dd0d356b22aba156293987468be0",
    "d706696f559b0075b106465a58ba6059d9085b0df5fead74b22b42751facb94a",
    "3d2eda8baea927ef6c5e8ffdde12a76a6653e3be28c15c27fb1bb15b96b61328",
    "f3b6874b62543babe547b945209cd1b7e78410f5468ddac64d44ba132e43bd6b",
    "0084dd0937d74c0401c6e243103bf47213015f9dbf9472b601f62554001cc007",
    "dc98f1c370c2b62e6c5ca9573ce00b65e709bb26cc6672f7e0ec126bb104f87c",
)


def _semantic_rows() -> dict[str, dict[str, Any]]:
    payload = json.loads(SEMANTIC_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "tsdaf.fake-article-router.v1"
    rows = {row["paper_id"]: row for row in payload["papers"]}
    assert tuple(rows) == PAPER_IDS
    return rows


def _router_provenance() -> Any:
    from tsdaf.contracts.routing import RouterProvenance

    return RouterProvenance(
        schema_version="tsdaf.router-provenance.v1",
        router_id="article-router",
        router_version="1.0.0",
        provider_id="fake",
        model_id="fake-v1",
        provider_profile_id="offline",
        provider_profile_version="1.0.0",
        provider_profile_sha256="1" * 64,
        prompt_id="article-router",
        prompt_version="1.0.0",
        prompt_sha256="2" * 64,
        contract_schema_bundle_version="1.0.0",
        contract_schema_sha256="3" * 64,
        taxonomy_sha256="4" * 64,
        tool_schema_version="1.0.0",
        tool_schema_sha256="5" * 64,
    )


def _routing_inputs(
    paper_ids: tuple[str, ...] = PAPER_IDS,
) -> tuple[tuple[Any, Any], ...]:
    from tsdaf.contracts.routing import (
        ArticleRoutingResult,
        ArticleType,
        CoarseTextRole,
        CoarseTextSelection,
        RouteAttribute,
        RouteGroup,
        RoutingDisposition,
        VisualAssetDecision,
        VisualDecision,
    )
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package

    rows = _semantic_rows()
    provenance = _router_provenance()
    inputs: list[tuple[Any, Any]] = []
    for paper_id in paper_ids:
        row = rows[paper_id]
        package = canonicalize_paper_package(PAPER_FIXTURES / paper_id)
        visuals_by_id = {visual.visual_id: visual for visual in package.visual_assets}
        route_group = RouteGroup(
            route_group_id=row["route_group_id"],
            target_names=tuple(row["target_names"]),
            synthesis_type=ArticleType(row["article_type"]),
            attributes=tuple(RouteAttribute(value) for value in row["attributes"]),
            ordered_visual_ids=tuple(row["ordered_visual_ids"]),
            core_text_block_ids=tuple(row["core_text_block_ids"]),
        )
        visual_decisions = tuple(
            VisualAssetDecision(
                visual_id=decision["visual_id"],
                visual_group_id=visuals_by_id[decision["visual_id"]].visual_group_id,
                initial_decision=VisualDecision(decision["initial_decision"]),
                triage_result=None,
                final_decision=VisualDecision(decision["final_decision"]),
            )
            for decision in row["visual_decisions"]
        )
        coarse_text_selections = tuple(
            CoarseTextSelection(
                route_group_id=route_group.route_group_id,
                block_id=block_id,
                role=CoarseTextRole.ROUTE_CORE,
            )
            for block_id in route_group.core_text_block_ids
        )
        routing_result = ArticleRoutingResult(
            schema_version="tsdaf.article-routing-result.v1",
            paper_id=paper_id,
            execution_id=f"task10-{paper_id}",
            canonical_package_sha256=package.canonical_content_sha256,
            article_type=ArticleType(row["article_type"]),
            disposition=RoutingDisposition(row["disposition"]),
            route_groups=(route_group,),
            visual_decisions=visual_decisions,
            coarse_text_selections=coarse_text_selections,
            provenance=provenance,
        )
        routing_result.validate_against(package)
        inputs.append((package, routing_result))
    return tuple(inputs)


def _two_route_result(package: Any, base_result: Any) -> Any:
    from tsdaf.contracts.routing import (
        ArticleRoutingResult,
        ArticleType,
        CoarseTextRole,
        CoarseTextSelection,
        RouteAttribute,
        RouteGroup,
        RoutingDisposition,
        VisualAssetDecision,
        VisualDecision,
    )

    route_groups = (
        RouteGroup(
            route_group_id="RG001",
            target_names=("route alpha",),
            synthesis_type=ArticleType.TOTAL_SYNTHESIS,
            attributes=(RouteAttribute.CHEMOENZYMATIC,),
            ordered_visual_ids=("MAIN-P0005-A0000", "MAIN-P0004-A0000"),
            core_text_block_ids=("MAIN-P0002-B0009", "MAIN-P0006-B0002"),
        ),
        RouteGroup(
            route_group_id="RG002",
            target_names=("route beta",),
            synthesis_type=ArticleType.TOTAL_SYNTHESIS,
            attributes=(),
            ordered_visual_ids=("MAIN-P0006-A0000",),
            core_text_block_ids=("MAIN-P0002-B0010",),
        ),
    )
    visual_decisions = tuple(
        VisualAssetDecision(
            visual_id=decision.visual_id,
            visual_group_id=decision.visual_group_id,
            initial_decision=VisualDecision.NEEDS_VISUAL_VERIFICATION,
            triage_result=VisualDecision.FORWARD_ROUTE,
            final_decision=VisualDecision.FORWARD_ROUTE,
        )
        if decision.visual_id == "MAIN-P0004-A0000"
        else decision
        for decision in base_result.visual_decisions
    )
    coarse_text_selections = (
        CoarseTextSelection(
            route_group_id="RG001",
            block_id="MAIN-P0002-B0009",
            role=CoarseTextRole.ROUTE_CORE,
        ),
        CoarseTextSelection(
            route_group_id="RG001",
            block_id="MAIN-P0006-B0002",
            role=CoarseTextRole.ROUTE_CORE,
        ),
        CoarseTextSelection(
            route_group_id="RG002",
            block_id="MAIN-P0002-B0010",
            role=CoarseTextRole.ROUTE_CORE,
        ),
    )
    result = ArticleRoutingResult(
        schema_version="tsdaf.article-routing-result.v1",
        paper_id=package.manifest.paper_id,
        execution_id="task10-multiroute-alchivemycin-A",
        canonical_package_sha256=package.canonical_content_sha256,
        article_type=ArticleType.TOTAL_SYNTHESIS,
        disposition=RoutingDisposition.ROUTED,
        route_groups=route_groups,
        visual_decisions=visual_decisions,
        coarse_text_selections=coarse_text_selections,
        provenance=base_result.provenance,
    )
    result.validate_against(package)
    return result


def _canonical_model_bytes(model: Any) -> bytes:
    from tsdaf.contracts.schema import canonical_json_bytes

    return canonical_json_bytes(model.model_dump(mode="json", by_alias=True, exclude_none=False))


def _nested_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _nested_keys(child)
    elif isinstance(value, list | tuple):
        for child in value:
            yield from _nested_keys(child)


def test_five_visual_handoffs_preserve_the_nine_original_assets() -> None:
    # Break caught: materialization drops, reorders, or transforms a routed source asset.
    from tsdaf.contracts.handoff import CoarseTextHandoff, VisualRoutingHandoff
    from tsdaf.lanes.document.routing import build_proposed_handoffs

    aggregate_forward_ids: list[str] = []
    aggregate_source_hashes: list[str] = []
    item_counts: list[int] = []

    for package, routing_result in _routing_inputs():
        outcome = build_proposed_handoffs(package=package, routing_result=routing_result)

        assert type(outcome) is tuple
        visual_handoff, coarse_handoff = outcome
        assert type(visual_handoff) is VisualRoutingHandoff
        assert type(coarse_handoff) is CoarseTextHandoff
        visual_handoff.validate_against(package, routing_result)
        coarse_handoff.validate_against(package, routing_result)
        assert visual_handoff.status == "PROPOSED"
        assert visual_handoff.route_groups == routing_result.route_groups

        flattened_ids = tuple(
            visual_id
            for route_group in routing_result.route_groups
            for visual_id in route_group.ordered_visual_ids
        )
        visuals_by_id = {visual.visual_id: visual for visual in package.visual_assets}
        groups_by_id = {group.visual_group_id: group for group in package.visual_groups}
        assert tuple(item.route_image_item_id for item in visual_handoff.items) == tuple(
            f"RIMG{index:03d}" for index in range(1, len(flattened_ids) + 1)
        )

        cursor = 0
        for route_group in routing_result.route_groups:
            route_items = visual_handoff.items[
                cursor : cursor + len(route_group.ordered_visual_ids)
            ]
            assert tuple(item.sequence_index for item in route_items) == tuple(
                range(len(route_items))
            )
            cursor += len(route_items)

        for item, visual_id in zip(visual_handoff.items, flattened_ids, strict=True):
            source_visual = visuals_by_id[visual_id]
            visual_group = groups_by_id[source_visual.visual_group_id]
            expected_caption = visual_group.caption_text or source_visual.native_caption or None
            expected_label = (
                visual_group.label_candidates[0] if visual_group.label_candidates else None
            )
            assert item.source_asset == source_visual.asset
            assert item.figure_or_scheme_label == expected_label
            assert item.caption == expected_caption

        serialized = visual_handoff.model_dump(mode="json", by_alias=True, exclude_none=False)
        forbidden_tokens = ("crop", "panel", "smiles", "reaction")
        assert not {
            key
            for key in _nested_keys(serialized)
            if any(token in key.casefold() for token in forbidden_tokens)
        }

        aggregate_forward_ids.extend(flattened_ids)
        aggregate_source_hashes.extend(item.source_asset.sha256 for item in visual_handoff.items)
        item_counts.append(len(visual_handoff.items))

    assert tuple(item_counts) == EXPECTED_ITEM_COUNTS
    assert tuple(aggregate_forward_ids) == EXPECTED_FORWARD_IDS
    assert tuple(aggregate_source_hashes) == EXPECTED_SOURCE_SHA256S


def test_multi_route_final_decisions_text_and_determinism_are_exact() -> None:
    # Break caught: materialization consults audit trace, sorts routes, or rewrites source text.
    from tsdaf.contracts.routing import VisualDecision
    from tsdaf.contracts.schema import canonical_json_bytes
    from tsdaf.lanes.document.routing import build_proposed_handoffs

    package, base_result = _routing_inputs(("alchivemycin_A",))[0]
    routing_result = _two_route_result(package, base_result)
    package_before = _canonical_model_bytes(package)
    routing_before = _canonical_model_bytes(routing_result)

    first = build_proposed_handoffs(package=package, routing_result=routing_result)
    second = build_proposed_handoffs(package=package, routing_result=routing_result)
    visual_handoff, coarse_handoff = first

    visual_handoff.validate_against(package, routing_result)
    coarse_handoff.validate_against(package, routing_result)
    expected_route_visual_ids = (
        "MAIN-P0005-A0000",
        "MAIN-P0004-A0000",
        "MAIN-P0006-A0000",
    )
    eligible_ids = tuple(
        decision.visual_id
        for decision in routing_result.visual_decisions
        if decision.final_decision is VisualDecision.FORWARD_ROUTE
    )
    overridden = next(
        decision
        for decision in routing_result.visual_decisions
        if decision.visual_id == "MAIN-P0004-A0000"
    )
    assert overridden.initial_decision is VisualDecision.NEEDS_VISUAL_VERIFICATION
    assert overridden.triage_result is VisualDecision.FORWARD_ROUTE
    assert overridden.final_decision is VisualDecision.FORWARD_ROUTE
    assert overridden.visual_id in expected_route_visual_ids
    assert set(expected_route_visual_ids) == set(eligible_ids)
    assert visual_handoff.route_groups == routing_result.route_groups

    visuals_by_id = {visual.visual_id: visual for visual in package.visual_assets}
    assert tuple(item.source_asset for item in visual_handoff.items) == tuple(
        visuals_by_id[visual_id].asset for visual_id in expected_route_visual_ids
    )
    assert tuple(item.route_image_item_id for item in visual_handoff.items) == (
        "RIMG001",
        "RIMG002",
        "RIMG003",
    )
    assert tuple(item.route_group_id for item in visual_handoff.items) == (
        "RG001",
        "RG001",
        "RG002",
    )
    assert tuple(item.sequence_index for item in visual_handoff.items) == (0, 1, 0)

    expected_related_ids = (
        "MAIN-P0006-B0002",
        "MAIN-P0002-B0009",
        "MAIN-P0002-B0010",
    )
    blocks_by_id = {block.block_id: block for block in package.blocks}
    assert tuple(item.related_block_ids for item in visual_handoff.items) == tuple(
        (block_id,) for block_id in expected_related_ids
    )
    assert tuple(item.related_original_paragraphs for item in visual_handoff.items) == tuple(
        (blocks_by_id[block_id].raw_text,) for block_id in expected_related_ids
    )
    assert tuple(item.selection for item in coarse_handoff.items) == (
        routing_result.coarse_text_selections
    )
    assert tuple(item.block for item in coarse_handoff.items) == tuple(
        blocks_by_id[selection.block_id] for selection in routing_result.coarse_text_selections
    )
    assert coarse_handoff.routing_result_sha256 == hashlib.sha256(routing_before).hexdigest()

    assert tuple(_canonical_model_bytes(model) for model in first) == tuple(
        _canonical_model_bytes(model) for model in second
    )
    assert _canonical_model_bytes(package) == package_before
    assert _canonical_model_bytes(routing_result) == routing_before
    assert routing_before == canonical_json_bytes(
        routing_result.model_dump(mode="json", by_alias=True, exclude_none=False)
    )


def test_invalid_typed_routing_and_wrong_python_types_are_rejected() -> None:
    # Break caught: the boundary trusts model identity without rechecking package invariants.
    from tsdaf.lanes.document.routing import build_proposed_handoffs
    from tsdaf.lanes.image import create_image_processing_job

    package, routing_result = _routing_inputs(("alchivemycin_A",))[0]
    route_group = routing_result.route_groups[0]
    forward_decision = next(
        decision
        for decision in routing_result.visual_decisions
        if decision.visual_id == route_group.ordered_visual_ids[0]
    )

    dangling_group = route_group.model_copy(
        update={
            "ordered_visual_ids": (
                *route_group.ordered_visual_ids,
                "MAIN-P9999-A0000",
            )
        }
    )
    dangling_result = routing_result.model_copy(update={"route_groups": (dangling_group,)})

    duplicated_group = route_group.model_copy(
        update={
            "ordered_visual_ids": (
                route_group.ordered_visual_ids[0],
                *route_group.ordered_visual_ids,
            )
        }
    )
    duplicated_result = routing_result.model_copy(
        update={
            "route_groups": (duplicated_group,),
            "visual_decisions": (*routing_result.visual_decisions, forward_decision),
        }
    )

    missing_core_group = route_group.model_copy(update={"core_text_block_ids": ()})
    missing_core_result = routing_result.model_copy(
        update={
            "route_groups": (missing_core_group,),
            "coarse_text_selections": (),
        }
    )

    for invalid_result in (dangling_result, duplicated_result, missing_core_result):
        with pytest.raises(ValueError):
            build_proposed_handoffs(package=package, routing_result=invalid_result)

    with pytest.raises(TypeError):
        build_proposed_handoffs(package=object(), routing_result=routing_result)
    with pytest.raises(TypeError):
        build_proposed_handoffs(package=package, routing_result=object())

    valid_handoff, _ = build_proposed_handoffs(package=package, routing_result=routing_result)
    with pytest.raises(TypeError):
        create_image_processing_job(
            job_id="task10-invalid-type",
            handoff=object(),
            route_image_item_id=valid_handoff.items[0].route_image_item_id,
            release_receipt=None,
            release_receipt_artifact=None,
        )


def test_not_in_scope_builds_empty_valid_handoffs() -> None:
    # Break caught: a rejected out-of-scope paper invents downstream image or text work.
    from tsdaf.contracts.routing import (
        ArticleRoutingResult,
        ArticleType,
        RoutingDisposition,
        VisualAssetDecision,
        VisualDecision,
    )
    from tsdaf.lanes.document.routing import build_proposed_handoffs

    package, base_result = _routing_inputs(("alchivemycin_A",))[0]
    routing_result = ArticleRoutingResult(
        schema_version="tsdaf.article-routing-result.v1",
        paper_id=package.manifest.paper_id,
        execution_id="task10-not-in-scope",
        canonical_package_sha256=package.canonical_content_sha256,
        article_type=ArticleType.NOT_IN_SCOPE,
        disposition=RoutingDisposition.REJECTED,
        route_groups=(),
        visual_decisions=tuple(
            VisualAssetDecision(
                visual_id=visual.visual_id,
                visual_group_id=visual.visual_group_id,
                initial_decision=VisualDecision.OTHER,
                triage_result=None,
                final_decision=VisualDecision.OTHER,
            )
            for visual in package.visual_assets
        ),
        coarse_text_selections=(),
        provenance=base_result.provenance,
    )
    routing_result.validate_against(package)

    visual_handoff, coarse_handoff = build_proposed_handoffs(
        package=package, routing_result=routing_result
    )

    assert visual_handoff.status == "PROPOSED"
    assert visual_handoff.route_groups == ()
    assert visual_handoff.items == ()
    assert coarse_handoff.items == ()
    visual_handoff.validate_against(package, routing_result)
    coarse_handoff.validate_against(package, routing_result)


def test_release_gate_requires_exact_current_handoff_and_receipt_artifact(
    tmp_path: Path,
) -> None:
    # Break caught: stale or inexact approval evidence can authorize an image job.
    from tsdaf.contracts.execution import FailureCode, TypedFailure
    from tsdaf.contracts.handoff import ImageProcessingJob, ReleaseReceipt
    from tsdaf.contracts.routing import ArticleRoutingResult
    from tsdaf.contracts.schema import canonical_json_bytes
    from tsdaf.contracts.source import ArtifactRef
    from tsdaf.lanes.document.routing import build_proposed_handoffs
    from tsdaf.lanes.image import create_image_processing_job
    from tsdaf.workflow.local_runtime import LocalObjectStore

    package, routing_result = _routing_inputs(("alchivemycin_A",))[0]
    visual_handoff, _ = build_proposed_handoffs(package=package, routing_result=routing_result)
    selected_item = visual_handoff.items[1]
    handoff_sha256 = hashlib.sha256(_canonical_model_bytes(visual_handoff)).hexdigest()
    release_receipt = ReleaseReceipt(
        schema_version="tsdaf.release-receipt.v1",
        release_id="release-task10-001",
        handoff_sha256=handoff_sha256,
        authority="HUMAN_APPROVED",
        policy_version=None,
        issued_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )
    store = LocalObjectStore(tmp_path / "store")
    receipt_payload = release_receipt.model_dump(mode="json", by_alias=True, exclude_none=False)
    receipt_bytes = canonical_json_bytes(receipt_payload)
    release_artifact = store.put_json(receipt_payload)
    assert release_artifact.sha256 == hashlib.sha256(receipt_bytes).hexdigest()
    assert release_artifact.byte_size == len(receipt_bytes)

    missing_receipt = create_image_processing_job(
        job_id="image-job-missing-receipt",
        handoff=visual_handoff,
        route_image_item_id=selected_item.route_image_item_id,
        release_receipt=None,
        release_receipt_artifact=release_artifact,
    )
    missing_artifact = create_image_processing_job(
        job_id="image-job-missing-artifact",
        handoff=visual_handoff,
        route_image_item_id=selected_item.route_image_item_id,
        release_receipt=release_receipt,
        release_receipt_artifact=None,
    )

    wrong_handoff_receipt = ReleaseReceipt(
        schema_version="tsdaf.release-receipt.v1",
        release_id="release-task10-wrong-handoff",
        handoff_sha256="0" * 64,
        authority="HUMAN_APPROVED",
        policy_version=None,
        issued_at=datetime(2026, 8, 26, 12, 1, tzinfo=UTC),
    )
    wrong_handoff_artifact = store.put_json(
        wrong_handoff_receipt.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    wrong_handoff = create_image_processing_job(
        job_id="image-job-wrong-handoff",
        handoff=visual_handoff,
        route_image_item_id=selected_item.route_image_item_id,
        release_receipt=wrong_handoff_receipt,
        release_receipt_artifact=wrong_handoff_artifact,
    )

    wrong_hash_artifact = ArtifactRef(
        sha256="f" * 64,
        media_type=release_artifact.media_type,
        byte_size=release_artifact.byte_size,
        relative_path=release_artifact.relative_path,
    )
    wrong_artifact_hash = create_image_processing_job(
        job_id="image-job-wrong-artifact-hash",
        handoff=visual_handoff,
        route_image_item_id=selected_item.route_image_item_id,
        release_receipt=release_receipt,
        release_receipt_artifact=wrong_hash_artifact,
    )
    wrong_size_artifact = ArtifactRef(
        sha256=release_artifact.sha256,
        media_type=release_artifact.media_type,
        byte_size=release_artifact.byte_size + 1,
        relative_path=release_artifact.relative_path,
    )
    wrong_artifact_size = create_image_processing_job(
        job_id="image-job-wrong-artifact-size",
        handoff=visual_handoff,
        route_image_item_id=selected_item.route_image_item_id,
        release_receipt=release_receipt,
        release_receipt_artifact=wrong_size_artifact,
    )
    unknown_item = create_image_processing_job(
        job_id="image-job-unknown-item",
        handoff=visual_handoff,
        route_image_item_id="RIMG999",
        release_receipt=release_receipt,
        release_receipt_artifact=release_artifact,
    )

    changed_payload = routing_result.model_dump(mode="python", by_alias=True, exclude_none=False)
    changed_payload["execution_id"] = "task10-release-content-change"
    changed_result = ArticleRoutingResult.model_validate(changed_payload)
    changed_result.validate_against(package)
    changed_handoff, _ = build_proposed_handoffs(package=package, routing_result=changed_result)
    assert _canonical_model_bytes(changed_handoff) != _canonical_model_bytes(visual_handoff)
    assert selected_item.route_image_item_id in {
        item.route_image_item_id for item in changed_handoff.items
    }
    stale_receipt = create_image_processing_job(
        job_id="image-job-stale-receipt",
        handoff=changed_handoff,
        route_image_item_id=selected_item.route_image_item_id,
        release_receipt=release_receipt,
        release_receipt_artifact=release_artifact,
    )

    failures = (
        missing_receipt,
        missing_artifact,
        wrong_handoff,
        wrong_artifact_hash,
        wrong_artifact_size,
        unknown_item,
        stale_receipt,
    )
    for failure in failures:
        assert type(failure) is TypedFailure
        assert failure.code is FailureCode.HANDOFF_INVALID
        assert release_artifact.relative_path not in failure.message

    job = create_image_processing_job(
        job_id="image-job-valid",
        handoff=visual_handoff,
        route_image_item_id=selected_item.route_image_item_id,
        release_receipt=release_receipt,
        release_receipt_artifact=release_artifact,
    )
    assert type(job) is ImageProcessingJob
    assert job.job_id == "image-job-valid"
    assert job.route_image_item == selected_item
    assert job.release_receipt == release_artifact
    assert job.requested_pipeline == "YOLO_MOLDET_OCSR_V1"
