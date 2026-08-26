"""Domain tests for canonical package closure and content identity."""

from __future__ import annotations

import hashlib
import importlib
import json
from typing import Any

import pytest
from pydantic import ValidationError


def _modules() -> tuple[Any, Any]:
    package = importlib.import_module("tsdaf.contracts.package")
    source = importlib.import_module("tsdaf.contracts.source")
    return source, package


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _artifact(source: Any, digit: str, path: str, media_type: str, size: int) -> Any:
    return source.ArtifactRef(
        sha256=digit * 64,
        media_type=media_type,
        byte_size=size,
        relative_path=path,
    )


def _content_projection(payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": payload["schema_version"],
        "manifest": payload["manifest"],
        "canonicalizer_version": payload["canonicalizer_version"],
        "normalized_package_sha256": payload["normalized_package_sha256"],
        "blocks": payload["blocks"],
        "visual_assets": payload["visual_assets"],
        "visual_groups": payload["visual_groups"],
        "visual_suppressions": payload["visual_suppressions"],
        "clean_view": payload["clean_view"],
    }


def _rehash(payload: dict[str, object]) -> dict[str, object]:
    payload["normalized_package_sha256"] = _sha(payload["manifest"])
    payload["canonical_content_sha256"] = _sha(_content_projection(payload))
    return payload


def _make_package(*, decorative_visual_ids: tuple[str, ...] = ()) -> Any:
    source, package = _modules()
    source_pdf = _artifact(source, "0", "source/paper.pdf", "application/pdf", 0)
    normalized_document = _artifact(source, "1", "normalized/document.json", "application/json", 11)
    normalized_blocks = _artifact(
        source, "2", "normalized/blocks.jsonl", "application/x-ndjson", 22
    )
    asset_0 = _artifact(source, "3", "assets/scheme-1a.png", "image/png", 100)
    asset_1 = _artifact(source, "4", "assets/scheme-1b.png", "image/png", 200)
    manifest = package.PaperManifest(
        schema_version="tsdaf.paper-package.v1",
        paper_id="paper-001",
        display_name="Paper One",
        source_pdf=source_pdf,
        normalized_document=normalized_document,
        normalized_blocks=normalized_blocks,
        assets=(asset_0, asset_1),
    )

    block_0 = package.CanonicalTextBlock(
        block_id="MAIN-P0000-B0000",
        page_index=0,
        reading_order=0,
        block_type=package.TextBlockType.PARAGRAPH,
        raw_text="The first route paragraph.",
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
        block_id="MAIN-P0000-B0001",
        page_index=0,
        reading_order=1,
        block_type=package.TextBlockType.FIGURE_CAPTION,
        raw_text="Scheme 1. First half.",
        section_path=("Results",),
        source=source.SourceLocator(
            document_id="paper-001",
            page_index=0,
            bbox=None,
            native_block_index=1,
            source_asset_ref=None,
        ),
    )
    block_2 = package.CanonicalTextBlock(
        block_id="MAIN-P0001-B0000",
        page_index=1,
        reading_order=0,
        block_type=package.TextBlockType.PARAGRAPH,
        raw_text="The second route paragraph.",
        section_path=("Results",),
        source=source.SourceLocator(
            document_id="paper-001",
            page_index=1,
            bbox=None,
            native_block_index=0,
            source_asset_ref=None,
        ),
    )
    block_3 = package.CanonicalTextBlock(
        block_id="MAIN-P0001-B0001",
        page_index=1,
        reading_order=1,
        block_type=package.TextBlockType.FIGURE_NOTE,
        raw_text="Scheme 1. Continued.",
        section_path=("Results",),
        source=source.SourceLocator(
            document_id="paper-001",
            page_index=1,
            bbox=None,
            native_block_index=1,
            source_asset_ref=None,
        ),
    )
    bbox = source.BoundingBox(
        x0=0,
        y0=100,
        x1=900,
        y1=900,
        coordinate_space="normalized_1000",
    )
    visual_0 = package.CanonicalVisualAsset(
        visual_id="MAIN-P0000-A0000",
        asset=asset_0,
        page_index=0,
        bbox=bbox,
        width=640,
        height=480,
        native_caption="Scheme 1",
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
        width=None,
        height=None,
        native_caption=None,
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
        caption_block_ids=(block_1.block_id, block_3.block_id),
        caption_text="Scheme 1. First half. Scheme 1. Continued.",
        association_status="CONFIRMED",
    )
    suppressions = tuple(
        package.VisualSuppressionRecord(
            visual_id=visual_id,
            reason="DECORATIVE_ASSET",
            rule_version="decorative-v1",
        )
        for visual_id in decorative_visual_ids
    )
    clean_view = package.CleanDocumentView(
        included_block_ids=(
            block_0.block_id,
            block_1.block_id,
            block_2.block_id,
            block_3.block_id,
        ),
        suppressed=(),
    )
    payload: dict[str, object] = {
        "schema_version": "tsdaf.canonical-paper-package.v1",
        "manifest": manifest,
        "canonicalizer_version": "canonicalizer-v1",
        "normalized_package_sha256": _sha(manifest.model_dump(mode="json")),
        "canonical_content_sha256": "f" * 64,
        "blocks": (block_0, block_1, block_2, block_3),
        "visual_assets": (visual_0, visual_1),
        "visual_groups": (group,),
        "visual_suppressions": suppressions,
        "clean_view": clean_view,
    }
    json_projection = {
        key: (
            value.model_dump(mode="json")
            if hasattr(value, "model_dump")
            else tuple(
                item.model_dump(mode="json") if hasattr(item, "model_dump") else item
                for item in value
            )
            if isinstance(value, tuple)
            else value
        )
        for key, value in _content_projection(payload).items()
    }
    payload["canonical_content_sha256"] = _sha(json_projection)
    return package.CanonicalPaperPackage.model_validate(payload)


def _payload(package_value: Any, *, rehash: bool = True) -> dict[str, object]:
    result = package_value.model_dump(mode="python")
    return _rehash(result) if rehash else result


def test_closed_cross_page_package_accepts_exact_hash_projections() -> None:
    value = _make_package()

    assert value.normalized_package_sha256 == _sha(value.manifest.model_dump(mode="json"))
    dumped = value.model_dump(mode="json")
    assert value.canonical_content_sha256 == _sha(_content_projection(dumped))
    assert value.visual_groups[0].member_visual_ids == (
        "MAIN-P0000-A0000",
        "MAIN-P0001-A0000",
    )
    assert value.visual_groups[0].visual_group_id == "MAIN-P0000-VG0000"


def test_decorative_visual_remains_in_every_canonical_inventory_and_changes_hash() -> None:
    plain = _make_package()
    decorated = _make_package(decorative_visual_ids=("MAIN-P0001-A0000",))

    assert decorated.manifest.assets == plain.manifest.assets
    assert decorated.visual_assets == plain.visual_assets
    assert decorated.visual_groups == plain.visual_groups
    assert decorated.visual_suppressions[0].visual_id == "MAIN-P0001-A0000"
    assert decorated.canonical_content_sha256 != plain.canonical_content_sha256


def test_text_and_visual_suppression_reasons_are_not_interchangeable() -> None:
    _, package = _modules()

    with pytest.raises(ValidationError):
        package.SuppressionRecord(
            block_id="MAIN-P0000-B0000",
            reason="DECORATIVE_ASSET",
            rule_version="v1",
        )
    with pytest.raises(ValidationError):
        package.VisualSuppressionRecord(
            visual_id="MAIN-P0000-A0000",
            reason="PAGE_HEADER",
            rule_version="v1",
        )


@pytest.mark.parametrize("mixed_inventory", [False, True])
def test_visual_group_must_have_an_anchor_even_if_other_inventory_exists(
    mixed_inventory: bool,
) -> None:
    _, package = _modules()
    group_payload = {
        "visual_group_id": "MAIN-P0000-VG0000",
        "member_visual_ids": (),
        "label_candidates": (),
        "caption_block_ids": (),
        "caption_text": None,
        "association_status": "NO_CAPTION",
    }

    with pytest.raises(ValidationError):
        package.VisualGroup.model_validate(group_payload)

    if mixed_inventory:
        value = _make_package()
        payload = _payload(value)
        payload["visual_groups"] = (*payload["visual_groups"], group_payload)
        _rehash(payload)
        with pytest.raises(ValidationError):
            package.CanonicalPaperPackage.model_validate(payload)


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_block_id",
        "manifest_asset_order",
        "group_member_order",
        "caption_wrong_type",
        "partial_clean_view",
        "source_page_mismatch",
        "visual_group_overlap",
    ],
)
def test_package_rejects_noncanonical_or_dangling_aggregate_facts(mutation: str) -> None:
    _, package = _modules()
    value = _make_package()
    payload = _payload(value)
    blocks = list(payload["blocks"])
    visuals = list(payload["visual_assets"])
    groups = list(payload["visual_groups"])
    manifest = dict(payload["manifest"])
    clean_view = dict(payload["clean_view"])

    if mutation == "bad_block_id":
        blocks[1] = {**blocks[1], "block_id": "MAIN-P0000-B0002"}
        clean_view["included_block_ids"] = (
            clean_view["included_block_ids"][0],
            "MAIN-P0000-B0002",
            *clean_view["included_block_ids"][2:],
        )
        groups[0] = {
            **groups[0],
            "caption_block_ids": ("MAIN-P0000-B0002", "MAIN-P0001-B0001"),
        }
    elif mutation == "manifest_asset_order":
        manifest["assets"] = tuple(reversed(manifest["assets"]))
    elif mutation == "group_member_order":
        groups[0] = {
            **groups[0],
            "member_visual_ids": tuple(reversed(groups[0]["member_visual_ids"])),
        }
    elif mutation == "caption_wrong_type":
        groups[0] = {**groups[0], "caption_block_ids": ("MAIN-P0000-B0000",)}
    elif mutation == "partial_clean_view":
        clean_view["included_block_ids"] = clean_view["included_block_ids"][:-1]
    elif mutation == "source_page_mismatch":
        visuals[1] = {
            **visuals[1],
            "source": {**visuals[1]["source"], "page_index": 0},
        }
    elif mutation == "visual_group_overlap":
        groups.append(
            {
                **groups[0],
                "visual_group_id": "MAIN-P0000-VG0001",
                "member_visual_ids": ("MAIN-P0000-A0000",),
                "caption_block_ids": (),
                "caption_text": None,
                "association_status": "NO_CAPTION",
            }
        )

    payload.update(
        manifest=manifest,
        blocks=tuple(blocks),
        visual_assets=tuple(visuals),
        visual_groups=tuple(groups),
        clean_view=clean_view,
    )
    _rehash(payload)

    with pytest.raises(ValidationError):
        package.CanonicalPaperPackage.model_validate(payload)


@pytest.mark.parametrize("tamper", ["manifest", "content"])
def test_package_rejects_stale_projection_hashes(tamper: str) -> None:
    _, package = _modules()
    value = _make_package()
    payload = _payload(value, rehash=False)

    if tamper == "manifest":
        payload["manifest"] = {**payload["manifest"], "display_name": "Changed Name"}
    else:
        payload["canonicalizer_version"] = "canonicalizer-v2"

    with pytest.raises(ValidationError):
        package.CanonicalPaperPackage.model_validate(payload)


def _nullable_visual_payload() -> dict[str, object]:
    source, _ = _modules()
    artifact = _artifact(source, "9", "assets/nullable.png", "image/png", 1)
    return {
        "visual_id": "MAIN-P0000-A0000",
        "asset": artifact.model_dump(mode="json"),
        "page_index": 0,
        "bbox": None,
        "width": None,
        "height": None,
        "native_caption": None,
        "source": {
            "document_id": "paper-001",
            "page_index": 0,
            "bbox": None,
            "native_block_index": None,
            "source_asset_ref": artifact.model_dump(mode="json"),
        },
        "visual_group_id": "MAIN-P0000-VG0000",
    }


@pytest.mark.parametrize("field", ["bbox", "width", "height", "native_caption"])
def test_visual_nullable_fields_are_required_and_accept_explicit_null(field: str) -> None:
    _, package = _modules()
    payload = _nullable_visual_payload()
    omitted = dict(payload)
    omitted.pop(field)

    with pytest.raises(ValidationError):
        package.CanonicalVisualAsset.model_validate(omitted)
    with pytest.raises(ValidationError):
        package.CanonicalVisualAsset.model_validate_json(json.dumps(omitted))
    assert getattr(package.CanonicalVisualAsset.model_validate(payload), field) is None
    assert (
        getattr(package.CanonicalVisualAsset.model_validate_json(json.dumps(payload)), field)
        is None
    )


def test_visual_group_caption_is_required_nullable_on_both_wires() -> None:
    _, package = _modules()
    payload = {
        "visual_group_id": "MAIN-P0000-VG0000",
        "member_visual_ids": ("MAIN-P0000-A0000",),
        "label_candidates": (),
        "caption_block_ids": (),
        "caption_text": None,
        "association_status": "NO_CAPTION",
    }
    json_payload = {**payload, "member_visual_ids": ["MAIN-P0000-A0000"]}
    omitted = dict(payload)
    omitted.pop("caption_text")
    json_omitted = dict(json_payload)
    json_omitted.pop("caption_text")

    with pytest.raises(ValidationError):
        package.VisualGroup.model_validate(omitted)
    with pytest.raises(ValidationError):
        package.VisualGroup.model_validate_json(json.dumps(json_omitted))
    assert package.VisualGroup.model_validate(payload).caption_text is None
    assert package.VisualGroup.model_validate_json(json.dumps(json_payload)).caption_text is None


def test_package_collections_reject_mutable_python_lists_but_json_arrays_become_tuples() -> None:
    _, package = _modules()
    payload = {
        "visual_group_id": "MAIN-P0000-VG0000",
        "member_visual_ids": ["MAIN-P0000-A0000"],
        "label_candidates": [],
        "caption_block_ids": [],
        "caption_text": None,
        "association_status": "NO_CAPTION",
    }

    with pytest.raises(ValidationError):
        package.VisualGroup.model_validate(payload)

    value = package.VisualGroup.model_validate_json(json.dumps(payload))
    assert value.member_visual_ids == ("MAIN-P0000-A0000",)
    with pytest.raises(TypeError):
        value.member_visual_ids[0] = "MAIN-P0000-A0001"


@pytest.mark.parametrize(
    ("model_name", "field", "value"),
    [
        ("CanonicalTextBlock", "section_path", ("Results", "Results")),
        ("VisualGroup", "label_candidates", ("Scheme 1", "Scheme 1")),
        (
            "VisualGroup",
            "member_visual_ids",
            ("MAIN-P0000-A0000", "MAIN-P0000-A0000"),
        ),
    ],
)
def test_package_leaf_collections_reject_exact_duplicates(
    model_name: str, field: str, value: tuple[str, ...]
) -> None:
    _, package = _modules()
    if model_name == "CanonicalTextBlock":
        source, _ = _modules()
        payload: dict[str, object] = {
            "block_id": "MAIN-P0000-B0000",
            "page_index": 0,
            "reading_order": 0,
            "block_type": "PARAGRAPH",
            "raw_text": "Exact text.",
            "section_path": value,
            "source": source.SourceLocator(
                document_id="paper-001",
                page_index=0,
                bbox=None,
                native_block_index=None,
                source_asset_ref=None,
            ),
        }
    else:
        payload = {
            "visual_group_id": "MAIN-P0000-VG0000",
            "member_visual_ids": ("MAIN-P0000-A0000",),
            "label_candidates": (),
            "caption_block_ids": (),
            "caption_text": None,
            "association_status": "NO_CAPTION",
        }
        payload[field] = value

    with pytest.raises(ValidationError):
        getattr(package, model_name).model_validate(payload)
