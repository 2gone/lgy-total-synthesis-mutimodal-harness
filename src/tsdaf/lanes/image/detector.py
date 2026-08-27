"""Provider-neutral molecule detector boundary and deterministic box preparation."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from tsdaf.lanes.image.models import PixelBox


@dataclass(frozen=True, slots=True)
class RawMoleculeDetection:
    bbox_xyxy: tuple[float, float, float, float]
    confidence: float
    class_id: int
    class_name: str


@dataclass(frozen=True, slots=True)
class PreparedMoleculeDetection:
    molecule_id: str
    detection_index: int
    raw: RawMoleculeDetection
    bbox_xyxy_px: PixelBox


@dataclass(frozen=True, slots=True)
class _Candidate:
    raw: RawMoleculeDetection
    box: PixelBox


def _overlap(a: PixelBox, b: PixelBox) -> tuple[float, float]:
    x0, y0 = max(a.x0, b.x0), max(a.y0, b.y0)
    x1, y1 = min(a.x1, b.x1), min(a.y1, b.y1)
    if x1 <= x0 or y1 <= y0:
        return 0.0, 0.0
    intersection = float((x1 - x0) * (y1 - y0))
    area_a = float((a.x1 - a.x0) * (a.y1 - a.y0))
    area_b = float((b.x1 - b.x0) * (b.y1 - b.y0))
    union = area_a + area_b - intersection
    return intersection / union, intersection / min(area_a, area_b)


def _candidate_key(candidate: _Candidate) -> tuple[float | int | str, ...]:
    box = candidate.box
    return (
        -candidate.raw.confidence,
        box.y0,
        box.x0,
        box.y1,
        box.x1,
        candidate.raw.class_id,
        candidate.raw.class_name,
    )


def _reading_order(candidates: list[_Candidate]) -> list[_Candidate]:
    rows: list[list[_Candidate]] = []
    for candidate in sorted(
        candidates,
        key=lambda value: (
            value.box.y0,
            value.box.x0,
            value.box.y1,
            value.box.x1,
            -value.raw.confidence,
        ),
    ):
        center_y = (candidate.box.y0 + candidate.box.y1) / 2.0
        for row in rows:
            reference = row[0].box
            reference_y = (reference.y0 + reference.y1) / 2.0
            tolerance = (
                max(
                    reference.y1 - reference.y0,
                    candidate.box.y1 - candidate.box.y0,
                )
                / 2.0
            )
            if abs(center_y - reference_y) <= tolerance:
                row.append(candidate)
                break
        else:
            rows.append([candidate])
    return [
        candidate
        for row in rows
        for candidate in sorted(
            row,
            key=lambda value: (
                value.box.x0,
                value.box.y0,
                value.box.x1,
                value.box.y1,
                -value.raw.confidence,
            ),
        )
    ]


def prepare_detections(
    *,
    route_image_item_id: str,
    width: int,
    height: int,
    raw: Sequence[RawMoleculeDetection],
    iou_threshold: float = 0.5,
    containment_threshold: float = 0.7,
) -> tuple[PreparedMoleculeDetection, ...]:
    """Normalize, suppress, and order raw detector outputs."""

    if not route_image_item_id or not route_image_item_id.startswith("RIMG"):
        raise ValueError("route_image_item_id must be a route image ID")
    if width <= 0 or height <= 0:
        raise ValueError("source image dimensions must be positive")
    if not 0.0 <= iou_threshold <= 1.0 or not 0.0 <= containment_threshold <= 1.0:
        raise ValueError("overlap thresholds must be between zero and one")

    candidates: list[_Candidate] = []
    for detection in raw:
        if not isinstance(detection, RawMoleculeDetection):
            raise TypeError("raw detections must be RawMoleculeDetection values")
        values = detection.bbox_xyxy
        if not all(math.isfinite(value) for value in values):
            continue
        if not 0.0 <= detection.confidence <= 1.0:
            raise ValueError("detection confidence must be between zero and one")
        if not detection.class_name:
            raise ValueError("detection class name cannot be empty")
        x0 = max(0, min(width, math.floor(values[0])))
        y0 = max(0, min(height, math.floor(values[1])))
        x1 = max(0, min(width, math.ceil(values[2])))
        y1 = max(0, min(height, math.ceil(values[3])))
        if x0 >= x1 or y0 >= y1:
            continue
        candidates.append(_Candidate(detection, PixelBox(x0=x0, y0=y0, x1=x1, y1=y1)))

    kept: list[_Candidate] = []
    for candidate in sorted(candidates, key=_candidate_key):
        if any(
            (lambda overlap: overlap[0] > iou_threshold or overlap[1] > containment_threshold)(
                _overlap(candidate.box, existing.box)
            )
            for existing in kept
        ):
            continue
        kept.append(candidate)

    ordered = _reading_order(kept)
    return tuple(
        PreparedMoleculeDetection(
            molecule_id=f"{route_image_item_id}:M{index:03d}",
            detection_index=index,
            raw=candidate.raw,
            bbox_xyxy_px=candidate.box,
        )
        for index, candidate in enumerate(ordered, start=1)
    )


__all__ = (
    "PreparedMoleculeDetection",
    "RawMoleculeDetection",
    "prepare_detections",
)
