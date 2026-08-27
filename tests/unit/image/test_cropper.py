from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

from PIL import Image

from tsdaf.contracts.handoff import ImageProcessingJob, RouteImageItem
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.cropper import materialize_crop_set
from tsdaf.lanes.image.detector import RawMoleculeDetection, prepare_detections
from tsdaf.lanes.image.models import CropPolicy, DetectorProvenance
from tsdaf.lanes.image.source_loader import LoadedSourceImage
from tsdaf.workflow.local_runtime import LocalObjectStore


def _loaded_source(tmp_path: Path) -> tuple[LoadedSourceImage, LocalObjectStore]:
    source = tmp_path / "source.png"
    Image.new("RGB", (20, 20), "red").save(source)
    payload = source.read_bytes()
    source_ref = ArtifactRef(
        sha256=hashlib.sha256(payload).hexdigest(),
        media_type="image/png",
        byte_size=len(payload),
        relative_path="images/source.png",
    )
    item = RouteImageItem(
        route_image_item_id="RIMG001",
        visual_group_id="MAIN-P0001-VG0001",
        source_asset=source_ref,
        page_index=0,
        bbox=None,
        dimensions=(20, 20),
        figure_or_scheme_label="Scheme 1",
        caption=None,
        related_block_ids=("MAIN-P0001-B0001",),
        related_original_paragraphs=("Scheme 1.",),
        route_group_id="RG001",
        sequence_index=0,
        router_execution_id="exec_router",
    )
    receipt = b"receipt"
    job = ImageProcessingJob(
        job_id="job-001",
        route_image_item=item,
        release_receipt=ArtifactRef(
            sha256=hashlib.sha256(receipt).hexdigest(),
            media_type="application/json",
            byte_size=len(receipt),
            relative_path="receipts/release.json",
        ),
        requested_pipeline="YOLO_MOLDET_OCSR_V1",
    )
    store = LocalObjectStore(tmp_path / "workspace")
    stored_source = store.put_bytes(payload, media_type="image/png")
    job_ref = store.put_json(job.model_dump(mode="json"))
    return (
        LoadedSourceImage(
            job=job,
            job_artifact=job_ref,
            source_artifact=stored_source,
            verified_local_path=source.resolve(),
            dimensions=(20, 20),
            detected_media_type="image/png",
        ),
        store,
    )


def _provenance() -> DetectorProvenance:
    return DetectorProvenance(
        detector_id="molyolo:yolov10",
        adapter_version="1.0.0",
        weights_sha256="1" * 64,
        runtime_bundle_sha256="2" * 64,
        ultralytics_version="8.1.34",
        image_size=1024,
        confidence_threshold=0.25,
        iou_threshold=0.5,
        device="cpu",
    )


def test_materialize_crop_set_adds_white_padding_at_image_edge(tmp_path: Path) -> None:
    loaded, store = _loaded_source(tmp_path)
    prepared = prepare_detections(
        route_image_item_id="RIMG001",
        width=20,
        height=20,
        raw=(RawMoleculeDetection((0.0, 0.0, 10.0, 10.0), 0.9, 0, "mol"),),
    )

    result = materialize_crop_set(
        source=loaded,
        detections=prepared,
        raw_detection_count=1,
        detector=_provenance(),
        store=store,
        policy=CropPolicy(
            horizontal_padding_fraction=0.1,
            vertical_padding_fraction=0.1,
            minimum_padding_pixels=4,
        ),
    )

    molecule = result.molecules[0]
    assert molecule.crop_requested_xyxy_px.as_xyxy() == (-4, -4, 14, 14)
    assert molecule.crop_sampled_xyxy_px.as_xyxy() == (0, 0, 14, 14)
    assert (molecule.crop_width, molecule.crop_height) == (18, 18)
    with Image.open(BytesIO(store.read_bytes(molecule.crop_artifact))) as crop:
        assert crop.mode == "RGB"
        assert crop.size == (18, 18)
        assert crop.getpixel((0, 0)) == (255, 255, 255)
        assert crop.getpixel((4, 4)) == (255, 0, 0)


def test_materialize_crop_set_marks_zero_detection_images(tmp_path: Path) -> None:
    loaded, store = _loaded_source(tmp_path)

    result = materialize_crop_set(
        source=loaded,
        detections=(),
        raw_detection_count=0,
        detector=_provenance(),
        store=store,
    )

    assert result.molecules == ()
    assert [flag.value for flag in result.quality_flags] == ["ZERO_DETECTIONS"]
    assert store.read_bytes(result.review_image_artifact).startswith(b"\x89PNG")
