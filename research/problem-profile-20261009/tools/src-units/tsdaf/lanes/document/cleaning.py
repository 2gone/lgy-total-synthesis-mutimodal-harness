"""Reversible text and visual suppression rules for canonical packages."""

from __future__ import annotations

import re
from typing import Any, Literal

from tsdaf.contracts.package import (
    CanonicalTextBlock,
    CanonicalVisualAsset,
    CleanDocumentView,
    SuppressionRecord,
    VisualGroup,
    VisualSuppressionRecord,
)

_NUMBERED_REFERENCE_START_RE = re.compile(r"^\s*\[\s*1\s*\]")
_TEXT_RULE_VERSION = "document-cleaning.v1"
_VISUAL_RULE_VERSION = "visual-cleaning.v1"


def _is_explicit_reference_path(section_path: object) -> bool:
    if not isinstance(section_path, list):
        return False
    for component in section_path:
        if not isinstance(component, str):
            continue
        normalized = re.sub(r"^[^A-Za-z]+", "", component).strip().casefold()
        if normalized == "references":
            return True
    return False


def _reference_flags(rows: tuple[dict[str, Any], ...]) -> tuple[bool, ...]:
    flags: list[bool] = []
    numbered_run_started = False
    for row in rows:
        normalized_type = row["type"]
        raw_text = row["raw_text"]
        if normalized_type == "ref_text" and _NUMBERED_REFERENCE_START_RE.match(raw_text):
            numbered_run_started = True
        flags.append(
            _is_explicit_reference_path(row["section_path"])
            or (numbered_run_started and normalized_type == "ref_text")
        )
    return tuple(flags)


def build_clean_view(
    blocks: tuple[CanonicalTextBlock, ...],
    rows: tuple[dict[str, Any], ...],
) -> CleanDocumentView:
    reference_flags = _reference_flags(rows)
    included: list[str] = []
    suppressed: list[SuppressionRecord] = []
    for block, row, is_reference in zip(blocks, rows, reference_flags, strict=True):
        normalized_type = row["type"]
        reason: (
            Literal[
                "PAGE_HEADER",
                "PAGE_FOOTER",
                "PAGE_NUMBER",
                "RUNNING_TEXT",
                "REFERENCES_SECTION",
            ]
            | None
        ) = None
        if normalized_type == "header":
            reason = "PAGE_HEADER"
        elif normalized_type == "footer":
            reason = "PAGE_FOOTER"
        elif normalized_type == "page_number":
            reason = "PAGE_NUMBER"
        elif normalized_type == "aside_text" and "downloaded from" in row["raw_text"].casefold():
            reason = "RUNNING_TEXT"
        elif is_reference:
            reason = "REFERENCES_SECTION"
        if reason is None:
            included.append(block.block_id)
        else:
            suppressed.append(
                SuppressionRecord(
                    block_id=block.block_id,
                    reason=reason,
                    rule_version=_TEXT_RULE_VERSION,
                )
            )
    return CleanDocumentView(
        included_block_ids=tuple(included),
        suppressed=tuple(suppressed),
    )


def _bbox_is_paired(visual_bbox: object, text_bbox: object) -> bool:
    if not isinstance(visual_bbox, list) or not isinstance(text_bbox, list):
        return False
    vx0, vy0, vx1, vy1 = visual_bbox
    tx0, ty0, tx1, ty1 = text_bbox
    vertical_overlap = min(vy1, ty1) > max(vy0, ty0)
    horizontal_gap = max(tx0 - vx1, vx0 - tx1, 0)
    return bool(vertical_overlap and horizontal_gap <= 5)


def build_visual_suppressions(
    visuals: tuple[CanonicalVisualAsset, ...],
    groups: tuple[VisualGroup, ...],
    visual_rows: tuple[dict[str, Any], ...],
    rows: tuple[dict[str, Any], ...],
) -> tuple[VisualSuppressionRecord, ...]:
    group_by_id = {group.visual_group_id: group for group in groups}
    controls = tuple(
        row
        for row in rows
        if row["page_idx"] == 0
        and row["type"] == "text"
        and (
            row["raw_text"].startswith("Cite This")
            or row["raw_text"] == "Read Online"
            or row["raw_text"].endswith("Supporting Information")
        )
    )
    records: list[VisualSuppressionRecord] = []
    for visual, row in zip(visuals, visual_rows, strict=True):
        group = group_by_id[visual.visual_group_id]
        if visual.page_index != 0 or group.association_status != "NO_CAPTION":
            continue
        bbox = row["bbox_norm_1000"]
        beside_control = any(
            abs(control["reading_order"] - row["reading_order"]) <= 2
            and _bbox_is_paired(bbox, control["bbox_norm_1000"])
            for control in controls
        )
        lower_right_cover = bbox[0] >= 800 and bbox[1] >= 800
        if beside_control or lower_right_cover:
            records.append(
                VisualSuppressionRecord(
                    visual_id=visual.visual_id,
                    reason="DECORATIVE_ASSET",
                    rule_version=_VISUAL_RULE_VERSION,
                )
            )
    return tuple(records)
