"""Deterministic molecule crop and review-overlay materialization."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from tsdaf.lanes.image.detector import PreparedMoleculeDetection
from tsdaf.lanes.image.models import (
    CropPolicy,
    DetectorProvenance,
    ImageQualityFlag,
    MoleculeCrop,
    MoleculeCropSet,
    PixelBox,
)
from tsdaf.lanes.image.source_loader import ImageObjectStore, LoadedSourceImage

_DEFAULT_CROP_POLICY = CropPolicy(
    horizontal_padding_fraction=0.15,
    vertical_padding_fraction=0.20,
    minimum_padding_pixels=16,
)


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def materialize_crop_set(
    *,
    source: LoadedSourceImage,
    detections: Sequence[PreparedMoleculeDetection],
    raw_detection_count: int,
    detector: DetectorProvenance,
    store: ImageObjectStore,
    policy: CropPolicy = _DEFAULT_CROP_POLICY,
) -> MoleculeCropSet:
    """Materialize padded RGB crops and one lightweight review overlay."""

    if not isinstance(source, LoadedSourceImage):
        raise TypeError("source must be a LoadedSourceImage")
    if not isinstance(store, ImageObjectStore):
        raise TypeError("store must implement ImageObjectStore")
    if raw_detection_count < len(detections):
        raise ValueError("raw detection count cannot be smaller than retained detections")

    source_bytes = store.read_bytes(source.source_artifact)
    with Image.open(BytesIO(source_bytes)) as opened:
        image = opened.convert("RGB")
    if image.size != source.dimensions:
        raise ValueError("stored source dimensions changed after verification")

    review = image.copy()
    draw = ImageDraw.Draw(review)
    font = ImageFont.load_default()
    molecules: list[MoleculeCrop] = []
    for prepared in detections:
        box = prepared.bbox_xyxy_px
        pad_x = max(
            policy.minimum_padding_pixels,
            round((box.x1 - box.x0) * policy.horizontal_padding_fraction),
        )
        pad_y = max(
            policy.minimum_padding_pixels,
            round((box.y1 - box.y0) * policy.vertical_padding_fraction),
        )
        requested = PixelBox(
            x0=box.x0 - pad_x,
            y0=box.y0 - pad_y,
            x1=box.x1 + pad_x,
            y1=box.y1 + pad_y,
        )
        sampled = PixelBox(
            x0=max(0, requested.x0),
            y0=max(0, requested.y0),
            x1=min(image.width, requested.x1),
            y1=min(image.height, requested.y1),
        )
        source_crop = image.crop(sampled.as_xyxy())
        canvas = Image.new(
            "RGB",
            (requested.x1 - requested.x0, requested.y1 - requested.y0),
            policy.background,
        )
        canvas.paste(source_crop, (sampled.x0 - requested.x0, sampled.y0 - requested.y0))
        crop_ref = store.put_bytes(_png_bytes(canvas), media_type="image/png")
        molecules.append(
            MoleculeCrop(
                molecule_id=prepared.molecule_id,
                detection_index=prepared.detection_index,
                class_name=prepared.raw.class_name,
                detector_score=prepared.raw.confidence,
                detector_bbox_raw_xyxy=prepared.raw.bbox_xyxy,
                bbox_xyxy_px=box,
                crop_requested_xyxy_px=requested,
                crop_sampled_xyxy_px=sampled,
                crop_width=canvas.width,
                crop_height=canvas.height,
                crop_artifact=crop_ref,
            )
        )
        draw.rectangle(box.as_xyxy(), outline="#0066ff", width=2)
        draw.text((box.x0, box.y0), str(prepared.detection_index), fill="#0066ff", font=font)

    review_ref = store.put_bytes(_png_bytes(review), media_type="image/png")
    quality_flags = (ImageQualityFlag.ZERO_DETECTIONS,) if not molecules else ()
    return MoleculeCropSet(
        schema_version="tsdaf.molecule-crop-set.v1",
        job_id=source.job.job_id,
        route_image_item_id=source.job.route_image_item.route_image_item_id,
        source_image_artifact=source.source_artifact,
        source_width=image.width,
        source_height=image.height,
        detector=detector,
        crop_policy=policy,
        raw_detection_count=raw_detection_count,
        molecules=tuple(molecules),
        review_image_artifact=review_ref,
        quality_flags=quality_flags,
    )


__all__ = ("materialize_crop_set",)
