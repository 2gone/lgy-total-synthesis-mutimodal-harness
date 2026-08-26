"""Domain tests for exact-copy handoffs and receipt-shaped release data."""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError


def _modules() -> tuple[Any, Any, Any, Any, Any]:
    handoff = importlib.import_module("tsdaf.contracts.handoff")
    source = importlib.import_module("tsdaf.contracts.source")
    package = importlib.import_module("tsdaf.contracts.package")
    routing = importlib.import_module("tsdaf.contracts.routing")
    routing_tests = importlib.import_module("test_contract_routing")
    return source, package, routing, handoff, routing_tests


def _sha(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _domain() -> tuple[Any, Any, Any, Any]:
    _, _, _, handoff, routing_tests = _modules()
    package_value = routing_tests._package()
    routing_result = routing_tests._forward_result(package_value)
    return handoff, routing_tests, package_value, routing_result


def _visual_handoff() -> tuple[Any, Any, Any]:
    handoff, _, package_value, routing_result = _domain()
    items = tuple(
        handoff.RouteImageItem(
            route_image_item_id=f"RIMG{index + 1:03d}",
            visual_group_id=visual.visual_group_id,
            source_asset=visual.asset,
            page_index=visual.page_index,
            bbox=visual.bbox,
            dimensions=(visual.width, visual.height),
            figure_or_scheme_label="Scheme 1",
            caption=visual.native_caption,
            related_block_ids=(package_value.blocks[index].block_id,),
            related_original_paragraphs=(package_value.blocks[index].raw_text,),
            route_group_id="RG001",
            sequence_index=index,
            router_execution_id=routing_result.execution_id,
        )
        for index, visual in enumerate(package_value.visual_assets)
    )
    value = handoff.VisualRoutingHandoff(
        schema_version="tsdaf.visual-routing-handoff.v1",
        paper_id=package_value.manifest.paper_id,
        execution_id=routing_result.execution_id,
        article_type=routing_result.article_type,
        route_groups=routing_result.route_groups,
        items=items,
        provenance=routing_result.provenance,
        status="PROPOSED",
    )
    return value, package_value, routing_result


def _coarse_handoff() -> tuple[Any, Any, Any]:
    handoff, _, package_value, routing_result = _domain()
    by_id = {block.block_id: block for block in package_value.blocks}
    items = tuple(
        handoff.CoarseTextHandoffItem(selection=selection, block=by_id[selection.block_id])
        for selection in routing_result.coarse_text_selections
    )
    value = handoff.CoarseTextHandoff(
        schema_version="tsdaf.coarse-text-handoff.v1",
        paper_id=package_value.manifest.paper_id,
        execution_id=routing_result.execution_id,
        canonical_package_sha256=package_value.canonical_content_sha256,
        routing_result_sha256=_sha(routing_result.model_dump(mode="json")),
        items=items,
        provenance=routing_result.provenance,
    )
    return value, package_value, routing_result


def test_visual_handoff_materializes_every_forward_asset_in_route_order() -> None:
    value, package_value, routing_result = _visual_handoff()

    assert value.validate_against(package_value, routing_result) is None
    assert tuple(item.route_image_item_id for item in value.items) == ("RIMG001", "RIMG002")
    assert tuple(item.sequence_index for item in value.items) == (0, 1)
    assert value.items[0].visual_group_id == value.items[1].visual_group_id
    assert tuple(item.source_asset for item in value.items) == package_value.manifest.assets


@pytest.mark.parametrize(
    "mutation",
    [
        "asset",
        "page",
        "bbox",
        "dimensions",
        "collapsed",
        "duplicate",
        "reordered",
        "item_id_gap",
        "sequence_gap",
    ],
)
def test_visual_handoff_rejects_copy_inventory_and_order_breaks(mutation: str) -> None:
    value, package_value, routing_result = _visual_handoff()
    items = list(value.items)
    if mutation == "asset":
        items[0] = items[0].model_copy(update={"source_asset": package_value.manifest.assets[1]})
    elif mutation == "page":
        items[0] = items[0].model_copy(update={"page_index": 1})
    elif mutation == "bbox":
        items[0] = items[0].model_copy(update={"bbox": None})
    elif mutation == "dimensions":
        items[0] = items[0].model_copy(update={"dimensions": (1, 1)})
    elif mutation == "collapsed":
        items = items[:1]
    elif mutation == "duplicate":
        items[1] = items[0]
    elif mutation == "reordered":
        items.reverse()
    elif mutation == "item_id_gap":
        items[1] = items[1].model_copy(update={"route_image_item_id": "RIMG003"})
    elif mutation == "sequence_gap":
        items[1] = items[1].model_copy(update={"sequence_index": 2})
    candidate = value.model_copy(update={"items": tuple(items)})

    with pytest.raises(ValueError):
        candidate.validate_against(package_value, routing_result)


@pytest.mark.parametrize(
    "mutation", ["zero", "three", "length_mismatch", "dangling", "paraphrase", "wrong_order"]
)
def test_visual_handoff_requires_one_or_two_exact_canonical_paragraphs(mutation: str) -> None:
    value, package_value, routing_result = _visual_handoff()
    first = value.items[0]
    if mutation == "zero":
        changed = first.model_copy(
            update={"related_block_ids": (), "related_original_paragraphs": ()}
        )
    elif mutation == "three":
        changed = first.model_copy(
            update={
                "related_block_ids": (
                    "MAIN-P0000-B0000",
                    "MAIN-P0001-B0000",
                    "MAIN-P0000-B0000",
                ),
                "related_original_paragraphs": (
                    "First route paragraph.",
                    "Second route paragraph.",
                    "First route paragraph.",
                ),
            }
        )
    elif mutation == "length_mismatch":
        changed = first.model_copy(
            update={
                "related_block_ids": ("MAIN-P0000-B0000",),
                "related_original_paragraphs": (
                    "First route paragraph.",
                    "Second route paragraph.",
                ),
            }
        )
    elif mutation == "dangling":
        changed = first.model_copy(update={"related_block_ids": ("MAIN-P0000-B0001",)})
    elif mutation == "paraphrase":
        changed = first.model_copy(
            update={"related_original_paragraphs": ("A paraphrased route paragraph.",)}
        )
    else:
        changed = first.model_copy(
            update={
                "related_block_ids": ("MAIN-P0001-B0000", "MAIN-P0000-B0000"),
                "related_original_paragraphs": (
                    "Second route paragraph.",
                    "First route paragraph.",
                ),
            }
        )
    candidate = value.model_copy(update={"items": (changed, value.items[1])})

    with pytest.raises(ValueError):
        candidate.validate_against(package_value, routing_result)


def test_coarse_handoff_copies_selections_and_blocks_field_for_field() -> None:
    value, package_value, routing_result = _coarse_handoff()

    assert value.validate_against(package_value, routing_result) is None
    assert tuple(item.selection for item in value.items) == routing_result.coarse_text_selections
    assert tuple(item.block.raw_text for item in value.items) == tuple(
        block.raw_text for block in package_value.blocks
    )


@pytest.mark.parametrize(
    "mutation", ["package_hash", "routing_hash", "provenance", "reordered", "changed_block"]
)
def test_coarse_handoff_rejects_stale_or_nonexact_copies(mutation: str) -> None:
    value, package_value, routing_result = _coarse_handoff()
    candidate = value
    if mutation == "package_hash":
        candidate = value.model_copy(update={"canonical_package_sha256": "a" * 64})
    elif mutation == "routing_hash":
        candidate = value.model_copy(update={"routing_result_sha256": "b" * 64})
    elif mutation == "provenance":
        changed = value.provenance.model_copy(update={"router_version": "2.0.0"})
        candidate = value.model_copy(update={"provenance": changed})
    elif mutation == "reordered":
        candidate = value.model_copy(update={"items": tuple(reversed(value.items))})
    elif mutation == "changed_block":
        changed_block = value.items[0].block.model_copy(update={"raw_text": "Changed text."})
        changed_item = value.items[0].model_copy(update={"block": changed_block})
        candidate = value.model_copy(update={"items": (changed_item, value.items[1])})

    with pytest.raises(ValueError):
        candidate.validate_against(package_value, routing_result)


def test_receipts_accept_both_authorities_and_serialize_fixed_utc_microseconds() -> None:
    _, _, _, handoff, _ = _modules()
    offset = timezone(timedelta(hours=8))
    reviewed = handoff.ReviewReceipt(
        schema_version="tsdaf.review-receipt.v1",
        review_id="review-001",
        handoff_sha256="a" * 64,
        reviewer_id="reviewer-001",
        decision="APPROVE",
        reviewed_at=datetime(2026, 8, 26, 12, 34, 56, 123, tzinfo=offset),
        notes=None,
    )
    human = handoff.ReleaseReceipt(
        schema_version="tsdaf.release-receipt.v1",
        release_id="release-human",
        handoff_sha256="a" * 64,
        authority="HUMAN_APPROVED",
        policy_version=None,
        issued_at=datetime(2026, 8, 26, 12, 34, 56, 123, tzinfo=offset),
    )
    policy = handoff.ReleaseReceipt(
        schema_version="tsdaf.release-receipt.v1",
        release_id="release-policy",
        handoff_sha256="a" * 64,
        authority="POLICY_APPROVED",
        policy_version="gold-100-v1",
        issued_at=datetime(2026, 8, 26, 12, 34, 56, 123, tzinfo=offset),
    )

    assert reviewed.model_dump(mode="json")["reviewed_at"] == "2026-08-26T04:34:56.000123Z"
    assert human.model_dump(mode="json")["issued_at"] == "2026-08-26T04:34:56.000123Z"
    assert policy.policy_version == "gold-100-v1"


@pytest.mark.parametrize(
    ("authority", "policy_version"),
    [("HUMAN_APPROVED", "policy-v1"), ("POLICY_APPROVED", None), ("POLICY_APPROVED", "")],
)
def test_release_receipt_enforces_authority_policy_relationship(
    authority: str, policy_version: str | None
) -> None:
    _, _, _, handoff, _ = _modules()

    with pytest.raises(ValidationError):
        handoff.ReleaseReceipt(
            schema_version="tsdaf.release-receipt.v1",
            release_id="release-001",
            handoff_sha256="a" * 64,
            authority=authority,
            policy_version=policy_version,
            issued_at=datetime(2026, 8, 26, tzinfo=UTC),
        )


def test_image_job_requires_receipt_artifact_but_makes_no_authorization_claim() -> None:
    source, _, _, handoff, _ = _modules()
    visual_handoff, _, _ = _visual_handoff()
    receipt_ref = source.ArtifactRef(
        sha256="d" * 64,
        media_type="application/json",
        byte_size=256,
        relative_path="receipts/release.json",
    )
    payload = {
        "job_id": "job-001",
        "route_image_item": visual_handoff.items[0],
        "release_receipt": receipt_ref,
        "requested_pipeline": "YOLO_MOLDET_OCSR_V1",
    }

    job = handoff.ImageProcessingJob.model_validate(payload)
    assert job.release_receipt == receipt_ref

    missing = dict(payload)
    missing.pop("release_receipt")
    with pytest.raises(ValidationError):
        handoff.ImageProcessingJob.model_validate(missing)


@pytest.mark.parametrize(
    "extra_field", ["crop_bbox", "crop_sha256", "smiles", "reaction_arrow", "panel_coordinates"]
)
def test_route_items_and_jobs_reject_image_analysis_overreach(extra_field: str) -> None:
    _, _, _, handoff, _ = _modules()
    visual_handoff, _, _ = _visual_handoff()
    payload = visual_handoff.items[0].model_dump(mode="python")
    payload[extra_field] = "forbidden"

    with pytest.raises(ValidationError):
        handoff.RouteImageItem.model_validate(payload)


def _nullable_item_payload() -> dict[str, object]:
    source, _, _, _, _ = _modules()
    artifact = source.ArtifactRef(
        sha256="e" * 64,
        media_type="image/png",
        byte_size=1,
        relative_path="assets/item.png",
    )
    return {
        "route_image_item_id": "RIMG001",
        "visual_group_id": "MAIN-P0000-VG0000",
        "source_asset": artifact.model_dump(mode="json"),
        "page_index": 0,
        "bbox": None,
        "dimensions": None,
        "figure_or_scheme_label": None,
        "caption": None,
        "related_block_ids": ("MAIN-P0000-B0000",),
        "related_original_paragraphs": ("Exact paragraph.",),
        "route_group_id": "RG001",
        "sequence_index": 0,
        "router_execution_id": "execution-001",
    }


@pytest.mark.parametrize("field", ["bbox", "dimensions", "figure_or_scheme_label", "caption"])
def test_route_item_nullable_fields_are_required_and_accept_explicit_null(field: str) -> None:
    _, _, _, handoff, _ = _modules()
    payload = _nullable_item_payload()
    omitted = dict(payload)
    omitted.pop(field)

    with pytest.raises(ValidationError):
        handoff.RouteImageItem.model_validate(omitted)
    with pytest.raises(ValidationError):
        handoff.RouteImageItem.model_validate_json(json.dumps(omitted))
    assert getattr(handoff.RouteImageItem.model_validate(payload), field) is None
    assert getattr(handoff.RouteImageItem.model_validate_json(json.dumps(payload)), field) is None


@pytest.mark.parametrize("kind", ["review", "release"])
def test_receipt_nullable_fields_are_required_and_accept_explicit_null(kind: str) -> None:
    _, _, _, handoff, _ = _modules()
    if kind == "review":
        model = handoff.ReviewReceipt
        field = "notes"
        payload: dict[str, object] = {
            "schema_version": "tsdaf.review-receipt.v1",
            "review_id": "review-001",
            "handoff_sha256": "a" * 64,
            "reviewer_id": "reviewer-001",
            "decision": "APPROVE",
            "reviewed_at": "2026-08-26T00:00:00Z",
            "notes": None,
        }
    else:
        model = handoff.ReleaseReceipt
        field = "policy_version"
        payload = {
            "schema_version": "tsdaf.release-receipt.v1",
            "release_id": "release-001",
            "handoff_sha256": "a" * 64,
            "authority": "HUMAN_APPROVED",
            "policy_version": None,
            "issued_at": "2026-08-26T00:00:00Z",
        }
    omitted = dict(payload)
    omitted.pop(field)

    with pytest.raises(ValidationError):
        model.model_validate_json(json.dumps(omitted))
    python_payload = dict(payload)
    time_field = "reviewed_at" if kind == "review" else "issued_at"
    python_payload[time_field] = datetime(2026, 8, 26, tzinfo=UTC)
    python_omitted = dict(python_payload)
    python_omitted.pop(field)
    with pytest.raises(ValidationError):
        model.model_validate(python_omitted)
    assert getattr(model.model_validate(python_payload), field) is None
    assert getattr(model.model_validate_json(json.dumps(payload)), field) is None
