"""Direct deterministic canonicalization of validated paper packages."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tsdaf.contracts.execution import FailureCode
from tsdaf.contracts.package import (
    CanonicalPaperPackage,
    CanonicalTextBlock,
    SourcePhase,
    TextBlockType,
    format_block_id,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import BoundingBox, SourceLocator

from .captions import _figure_note_indices, _native_caption, build_visuals_and_groups
from .cleaning import _reference_flags, build_clean_view, build_visual_suppressions
from .package_loader import PICTURE_TYPES, PackageLoadError, load_paper_package

_CANONICALIZER_VERSION = "document-canonicalizer.v1"
_FIGURE_CAPTION_RE = re.compile(
    r"^\s*(?:Fig(?:ure)?\.?|Scheme)\s+\d+[A-Za-z]?\s*(?:\.|:|\|)",
    re.IGNORECASE,
)
_TABLE_CAPTION_RE = re.compile(
    r"^\s*Table\s+\d+[A-Za-z]?\s*(?:\.|:|\|)",
    re.IGNORECASE,
)
_DIRECT_TYPES = {
    "header": TextBlockType.PAGE_HEADER,
    "footer": TextBlockType.PAGE_FOOTER,
    "page_number": TextBlockType.PAGE_NUMBER,
    "page_footnote": TextBlockType.FOOTNOTE,
    "table": TextBlockType.TABLE_TEXT,
    "image": TextBlockType.OTHER,
    "chart": TextBlockType.OTHER,
    "equation": TextBlockType.DISPLAY_EQUATION,
    "code": TextBlockType.OTHER,
    "aside_text": TextBlockType.OTHER,
}


def _heading_indices(rows: tuple[dict[str, Any], ...]) -> frozenset[int]:
    headings: set[int] = set()
    previous_path: tuple[str, ...] | None = None
    index = 0
    while index < len(rows):
        path = tuple(rows[index]["section_path"])
        if path != previous_path:
            cursor = index
            while cursor < len(rows) and tuple(rows[cursor]["section_path"]) == path:
                if rows[cursor]["type"] == "text":
                    if path and rows[cursor]["raw_text"] == path[-1]:
                        headings.add(cursor)
                    break
                cursor += 1
        previous_path = path
        index += 1
    return frozenset(headings)


def _title_index(rows: tuple[dict[str, Any], ...]) -> int | None:
    """The block that is the document's title, or None when no block is.

    A supplement that is only spectra has no title block, and that is what the document is;
    which paper it belongs to is the package's identity, not a block's label.
    """
    for index, row in enumerate(rows):
        path = row["section_path"]
        if row["type"] == "text" and path and row["raw_text"] == path[0]:
            return index
    return None


def _block_type(
    index: int,
    row: dict[str, Any],
    *,
    title_index: int | None,
    headings: frozenset[int],
    notes: frozenset[int],
    references: tuple[bool, ...],
) -> TextBlockType:
    normalized_type = row["type"]
    if normalized_type in _DIRECT_TYPES:
        return _DIRECT_TYPES[normalized_type]
    if normalized_type == "ref_text":
        return TextBlockType.REFERENCE_ENTRY if references[index] else TextBlockType.PARAGRAPH
    raw_text = row["raw_text"]
    if index == title_index:
        return TextBlockType.DOCUMENT_TITLE
    if _FIGURE_CAPTION_RE.match(raw_text):
        return TextBlockType.FIGURE_CAPTION
    if _TABLE_CAPTION_RE.match(raw_text):
        return TextBlockType.TABLE_CAPTION
    if index in notes:
        return TextBlockType.FIGURE_NOTE
    if index in headings:
        return TextBlockType.SECTION_HEADING
    if references[index]:
        return TextBlockType.REFERENCE_ENTRY
    if raw_text == "":
        return TextBlockType.OTHER
    return TextBlockType.PARAGRAPH


def _block_text(row: dict[str, Any], phase: SourcePhase) -> str:
    """The text a reader finds, reads and cites for this row.

    A supplement's figure row is given its own caption. MinerU puts whole procedures
    there -- 33 across the five SI fixtures, none of them in any text block -- and the
    row's block is otherwise empty, while search, the reading units, the delivery scan
    and text citation read only block text: a procedure whose only copy was a caption
    reached none of them (f09 B004, compound 17 -> 18). In the main text a figure is read
    with its caption by the visual route reader, and its row keeps the empty text it is
    normalized with. The block's type is still judged from the row as normalized.
    """
    text: str = row["raw_text"]
    if phase == "SI" and row["type"] in PICTURE_TYPES and not text.strip():
        return _native_caption(row) or text
    return text


def _canonical_blocks(
    manifest_assets_by_path: dict[str, Any],
    paper_id: str,
    rows: tuple[dict[str, Any], ...],
    *,
    phase: SourcePhase = "MAIN",
) -> tuple[CanonicalTextBlock, ...]:
    references = _reference_flags(rows)
    notes = _figure_note_indices(rows)
    headings = _heading_indices(rows)
    title_index = _title_index(rows)
    blocks: list[CanonicalTextBlock] = []
    for index, row in enumerate(rows):
        x0, y0, x1, y1 = row["bbox_norm_1000"]
        bbox = BoundingBox(
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            coordinate_space="normalized_1000",
        )
        source = SourceLocator(
            document_id=paper_id,
            page_index=row["page_idx"],
            bbox=bbox,
            native_block_index=row["mineru_native_ref"]["native_index"],
            source_asset_ref=manifest_assets_by_path.get(row["asset_ref"]),
        )
        blocks.append(
            CanonicalTextBlock(
                block_id=format_block_id(phase, row["page_idx"], row["reading_order"]),
                page_index=row["page_idx"],
                reading_order=row["reading_order"],
                block_type=_block_type(
                    index,
                    row,
                    title_index=title_index,
                    headings=headings,
                    notes=notes,
                    references=references,
                ),
                raw_text=_block_text(row, phase),
                section_path=tuple(row["section_path"]),
                source=source,
            )
        )
    return tuple(blocks)


def _json_models(values: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [value.model_dump(mode="json", by_alias=True, exclude_none=False) for value in values]


def canonicalize_paper_package(package_dir: Path) -> CanonicalPaperPackage:
    """Load and deterministically canonicalize one normalized package."""

    loaded = load_paper_package(package_dir)
    manifest = loaded.manifest
    try:
        assets_by_path = {asset.relative_path: asset for asset in manifest.assets}
        blocks = _canonical_blocks(
            assets_by_path, manifest.paper_id, loaded.blocks, phase=manifest.source_phase
        )
        visuals, groups, visual_rows = build_visuals_and_groups(
            loaded.package_dir,
            manifest,
            blocks,
            loaded.blocks,
        )
        clean_view = build_clean_view(blocks, loaded.blocks)
        visual_suppressions = build_visual_suppressions(
            visuals,
            groups,
            visual_rows,
            loaded.blocks,
        )
        manifest_json = manifest.content_projection()
        normalized_hash = hashlib.sha256(canonical_json_bytes(manifest_json)).hexdigest()
        projection: dict[str, Any] = {
            "schema_version": "tsdaf.canonical-paper-package.v1",
            "manifest": manifest_json,
            "canonicalizer_version": _CANONICALIZER_VERSION,
            "normalized_package_sha256": normalized_hash,
            "blocks": _json_models(blocks),
            "visual_assets": _json_models(visuals),
            "visual_groups": _json_models(groups),
            "visual_suppressions": _json_models(visual_suppressions),
            "clean_view": clean_view.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=False,
            ),
        }
        canonical_hash = hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
        return CanonicalPaperPackage(
            schema_version="tsdaf.canonical-paper-package.v1",
            manifest=manifest,
            canonicalizer_version=_CANONICALIZER_VERSION,
            normalized_package_sha256=normalized_hash,
            canonical_content_sha256=canonical_hash,
            blocks=blocks,
            visual_assets=visuals,
            visual_groups=groups,
            visual_suppressions=visual_suppressions,
            clean_view=clean_view,
        )
    except PackageLoadError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError) as error:
        raise PackageLoadError(
            FailureCode.PACKAGE_INVALID,
            "normalized/blocks.jsonl",
            f"canonical construction failed: {error}",
        ) from error
