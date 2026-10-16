"""Marks on a figure before it is read: the boxes of the molecule detector in reading order, each drawn with its mark (M001, M002, ...).
[Copied from e293-expression-review/tools/display_overlay/build.py (2026-10-15), the functions the five reference papers' figures were marked with.]"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PIL import Image, ImageDraw, ImageFont

MARK_COLOR = (214, 0, 170)


def reading_order(boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows top to bottom, left to right within a row -- the order a reader scans."""

    rows: list[list[dict[str, Any]]] = []
    for item in sorted(boxes, key=lambda value: (value["bbox_px"][1], value["bbox_px"][0])):
        box = item["bbox_px"]
        centre = (box[1] + box[3]) / 2
        for row in rows:
            ref = row[0]["bbox_px"]
            tolerance = 0.55 * max(ref[3] - ref[1], box[3] - box[1])
            if abs(centre - (ref[1] + ref[3]) / 2) <= tolerance:
                row.append(item)
                break
        else:
            rows.append([item])
    return [item for row in rows for item in sorted(row, key=lambda value: value["bbox_px"][0])]


def marked_image(image: Image.Image, marks: list[tuple[str, Sequence[float]]]) -> Image.Image:
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas)
    size = max(12, round(canvas.height / 55))
    font = ImageFont.load_default(size=size)
    for mark, box in marks:
        draw.rectangle(tuple(box), outline=MARK_COLOR, width=3)
        left, top, right, bottom = draw.textbbox((box[0], box[1]), mark, font=font)
        draw.rectangle((left - 2, top - 1, right + 2, bottom + 1), fill=(255, 255, 255))
        draw.text((box[0], box[1]), mark, fill=MARK_COLOR, font=font)
    return canvas
