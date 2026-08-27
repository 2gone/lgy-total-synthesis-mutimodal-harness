from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from tsdaf.contracts.execution import FailureCode
from tsdaf.contracts.handoff import ImageProcessingJob, RouteImageItem
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.source_loader import SourceImageLoadError, load_source_image
from tsdaf.workflow.local_runtime import LocalObjectStore


def _png_bytes(size: tuple[int, int] = (80, 60)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _job(
    payload: bytes,
    *,
    relative_path: str = "images/scheme.png",
    media_type: str = "image/png",
    sha256: str | None = None,
    byte_size: int | None = None,
    dimensions: tuple[int, int] | None = (80, 60),
) -> ImageProcessingJob:
    asset = ArtifactRef(
        sha256=sha256 or hashlib.sha256(payload).hexdigest(),
        media_type=media_type,
        byte_size=len(payload) if byte_size is None else byte_size,
        relative_path=relative_path,
    )
    item = RouteImageItem(
        route_image_item_id="RIMG001",
        visual_group_id="MAIN-P0001-VG0001",
        source_asset=asset,
        page_index=0,
        bbox=None,
        dimensions=dimensions,
        figure_or_scheme_label="Scheme 1",
        caption=None,
        related_block_ids=("MAIN-P0001-B0001",),
        related_original_paragraphs=("Scheme 1 shows the route.",),
        route_group_id="RG001",
        sequence_index=0,
        router_execution_id="exec_router",
    )
    receipt_bytes = b"release receipt"
    return ImageProcessingJob(
        job_id="image-job-001",
        route_image_item=item,
        release_receipt=ArtifactRef(
            sha256=hashlib.sha256(receipt_bytes).hexdigest(),
            media_type="application/json",
            byte_size=len(receipt_bytes),
            relative_path="receipts/release.json",
        ),
        requested_pipeline="YOLO_MOLDET_OCSR_V1",
    )


def test_load_source_image_verifies_and_materializes_exact_bytes(tmp_path: Path) -> None:
    payload = _png_bytes()
    package_dir = tmp_path / "package"
    source_path = package_dir / "images" / "scheme.png"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(payload)
    store = LocalObjectStore(tmp_path / "workspace")

    loaded = load_source_image(job=_job(payload), package_dir=package_dir, store=store)

    assert loaded.dimensions == (80, 60)
    assert loaded.detected_media_type == "image/png"
    assert loaded.verified_local_path == source_path.resolve()
    assert store.read_bytes(loaded.source_artifact) == payload
    assert loaded.source_artifact.sha256 == hashlib.sha256(payload).hexdigest()
    assert loaded.job_artifact.media_type == "application/vnd.tsdaf.image-processing-job+json"


@pytest.mark.parametrize(
    ("job_kwargs", "expected_code"),
    [
        ({"sha256": "0" * 64}, FailureCode.HASH_MISMATCH),
        ({"byte_size": 1}, FailureCode.HASH_MISMATCH),
        ({"media_type": "image/jpeg"}, FailureCode.PACKAGE_INVALID),
        ({"dimensions": (81, 60)}, FailureCode.PACKAGE_INVALID),
    ],
)
def test_load_source_image_rejects_identity_or_metadata_mismatch(
    tmp_path: Path,
    job_kwargs: dict[str, object],
    expected_code: FailureCode,
) -> None:
    payload = _png_bytes()
    package_dir = tmp_path / "package"
    source_path = package_dir / "images" / "scheme.png"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(payload)

    with pytest.raises(SourceImageLoadError) as caught:
        load_source_image(
            job=_job(payload, **job_kwargs),
            package_dir=package_dir,
            store=LocalObjectStore(tmp_path / "workspace"),
        )

    assert caught.value.code is expected_code
    assert caught.value.relative_path == "images/scheme.png"


def test_load_source_image_rejects_path_escape_even_for_unvalidated_job(tmp_path: Path) -> None:
    payload = _png_bytes()
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    escaped = tmp_path / "escaped.png"
    escaped.write_bytes(payload)
    valid_job = _job(payload)
    escaped_asset = valid_job.route_image_item.source_asset.model_copy(
        update={"relative_path": "../escaped.png"}
    )
    escaped_item = valid_job.route_image_item.model_copy(update={"source_asset": escaped_asset})
    escaped_job = valid_job.model_copy(update={"route_image_item": escaped_item})

    with pytest.raises(SourceImageLoadError) as caught:
        load_source_image(
            job=escaped_job,
            package_dir=package_dir,
            store=LocalObjectStore(tmp_path / "workspace"),
        )

    assert caught.value.code is FailureCode.PACKAGE_INVALID


def test_load_source_image_rejects_missing_file(tmp_path: Path) -> None:
    payload = _png_bytes()

    with pytest.raises(SourceImageLoadError) as caught:
        load_source_image(
            job=_job(payload),
            package_dir=tmp_path / "package",
            store=LocalObjectStore(tmp_path / "workspace"),
        )

    assert caught.value.code is FailureCode.ASSET_MISSING
