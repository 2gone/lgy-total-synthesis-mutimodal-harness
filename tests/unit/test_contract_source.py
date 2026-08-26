"""Domain tests for immutable source and artifact contracts."""

from __future__ import annotations

import importlib
import json
from typing import Any

import pytest
from pydantic import ValidationError


def _source() -> Any:
    return importlib.import_module("tsdaf.contracts.source")


def _artifact_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sha256": "a" * 64,
        "media_type": "application/pdf",
        "byte_size": 0,
        "relative_path": "corpus/paper.pdf",
    }
    payload.update(updates)
    return payload


def _locator_payload() -> dict[str, object]:
    return {
        "document_id": "paper-001",
        "page_index": 0,
        "bbox": None,
        "native_block_index": None,
        "source_asset_ref": None,
    }


def test_artifact_ref_accepts_zero_bytes_and_has_deterministic_json() -> None:
    source = _source()

    artifact = source.ArtifactRef.model_validate(_artifact_payload())

    assert artifact.model_dump(mode="json") == _artifact_payload()
    assert artifact.model_dump_json() == (
        '{"sha256":"' + "a" * 64 + '","media_type":"application/pdf",'
        '"byte_size":0,"relative_path":"corpus/paper.pdf"}'
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sha256", "A" * 64),
        ("sha256", "sha256:" + "a" * 64),
        ("sha256", "a" * 63),
        ("byte_size", -1),
        ("relative_path", "/corpus/paper.pdf"),
        ("relative_path", "C:/corpus/paper.pdf"),
        ("relative_path", "corpus\\paper.pdf"),
        ("relative_path", "corpus/../paper.pdf"),
        ("relative_path", "corpus//paper.pdf"),
        ("relative_path", "./paper.pdf"),
        ("relative_path", "corpus/./paper.pdf"),
        ("relative_path", "corpus/paper.pdf\x00"),
        ("media_type", " application/pdf"),
        ("media_type", "application/pdf\x00"),
    ],
)
def test_artifact_ref_rejects_noncanonical_scalar_values(field: str, value: object) -> None:
    source = _source()
    payload = _artifact_payload(**{field: value})

    with pytest.raises(ValidationError):
        source.ArtifactRef.model_validate(payload)


def test_source_locator_preserves_boundary_bbox_and_nullable_provenance() -> None:
    source = _source()
    bbox = source.BoundingBox(
        x0=0,
        y0=0,
        x1=1000,
        y1=1000,
        coordinate_space="normalized_1000",
    )
    artifact = source.ArtifactRef.model_validate(_artifact_payload())

    locator = source.SourceLocator(
        document_id="paper-001",
        page_index=9999,
        bbox=bbox,
        native_block_index=0,
        source_asset_ref=artifact,
    )

    assert locator.bbox == bbox
    assert locator.source_asset_ref is artifact
    assert locator.page_index == 9999


@pytest.mark.parametrize(
    "updates",
    [
        {"x0": -1},
        {"x0": 1, "x1": 1},
        {"x0": 2, "x1": 1},
        {"x1": 1001},
        {"y0": -1},
        {"y0": 1, "y1": 1},
        {"y0": 2, "y1": 1},
        {"y1": 1001},
        {"coordinate_space": "pixels"},
        {"x0": True},
        {"x0": "0"},
    ],
)
def test_bounding_box_rejects_invalid_or_coerced_coordinates(updates: dict[str, object]) -> None:
    source = _source()
    payload: dict[str, object] = {
        "x0": 0,
        "y0": 0,
        "x1": 1000,
        "y1": 1000,
        "coordinate_space": "normalized_1000",
    }
    payload.update(updates)

    with pytest.raises(ValidationError):
        source.BoundingBox.model_validate(payload)


@pytest.mark.parametrize("field", ["bbox", "native_block_index", "source_asset_ref"])
def test_source_locator_nullable_fields_are_required_on_python_wire(field: str) -> None:
    source = _source()
    payload = _locator_payload()
    payload.pop(field)

    with pytest.raises(ValidationError):
        source.SourceLocator.model_validate(payload)


@pytest.mark.parametrize("field", ["bbox", "native_block_index", "source_asset_ref"])
def test_source_locator_nullable_fields_are_required_on_json_wire(field: str) -> None:
    source = _source()
    payload = _locator_payload()
    payload.pop(field)

    with pytest.raises(ValidationError):
        source.SourceLocator.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("field", ["bbox", "native_block_index", "source_asset_ref"])
def test_source_locator_explicit_null_is_valid_on_both_wires(field: str) -> None:
    source = _source()
    payload = _locator_payload()

    assert getattr(source.SourceLocator.model_validate(payload), field) is None
    assert getattr(source.SourceLocator.model_validate_json(json.dumps(payload)), field) is None


@pytest.mark.parametrize(
    "updates",
    [
        {"document_id": " paper-001"},
        {"document_id": ""},
        {"document_id": "paper\x00001"},
        {"page_index": -1},
        {"page_index": 10000},
        {"page_index": True},
        {"native_block_index": -1},
        {"native_block_index": "0"},
    ],
)
def test_source_locator_rejects_noncanonical_identity_and_indices(
    updates: dict[str, object],
) -> None:
    source = _source()
    payload = _locator_payload()
    payload.update(updates)

    with pytest.raises(ValidationError):
        source.SourceLocator.model_validate(payload)


def test_contract_models_forbid_extra_fields_and_are_frozen() -> None:
    source = _source()

    with pytest.raises(ValidationError):
        source.ArtifactRef.model_validate({**_artifact_payload(), "filename": "paper.pdf"})

    artifact = source.ArtifactRef.model_validate(_artifact_payload())
    with pytest.raises(ValidationError):
        artifact.byte_size = 1
