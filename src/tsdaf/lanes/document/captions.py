"""Conservative visual construction, caption association, and grouping."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from tsdaf.contracts.execution import FailureCode
from tsdaf.contracts.package import (
    CanonicalTextBlock,
    CanonicalVisualAsset,
    PaperManifest,
    TextBlockType,
    VisualGroup,
)

from .package_loader import PackageLoadError

_LABEL_RE = re.compile(
    r"(?:^|\n)\s*(Fig(?:ure)?\.?|Scheme|Table)\s+(\d+[A-Za-z]?)\b",
    re.IGNORECASE,
)
_FIGURE_CAPTION_RE = re.compile(
    r"^\s*(?:Fig(?:ure)?\.?|Scheme)\s+\d+[A-Za-z]?\s*(?:\.|:|\|)",
    re.IGNORECASE,
)
_PANEL_START_RE = re.compile(r"^\s*([A-Za-z])\.")
_PAREN_PANEL_RE = re.compile(r"\(([A-Za-z])\)")
_HTML_PANEL_RE = re.compile(r"(?:^|>)\s*([A-Za-z])\.")
_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


def _native_caption(row: dict[str, Any]) -> str | None:
    captions = row["captions"]
    return "\n".join(captions) if captions else None


def _extract_labels(text: str | None) -> tuple[str, ...]:
    if text is None:
        return ()
    labels: list[str] = []
    for match in _LABEL_RE.finditer(text):
        kind = match.group(1).casefold()
        prefix = "Figure" if kind.startswith("fig") else kind.title()
        label = f"{prefix} {match.group(2)}"
        if label not in labels:
            labels.append(label)
    return tuple(labels)


def _explicit_panel_segment(
    rows: tuple[dict[str, Any], ...],
    caption_row_index: int,
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    caption = _native_caption(rows[caption_row_index])
    if caption is None or not ({"A", "B"} <= set(_PAREN_PANEL_RE.findall(caption))):
        return None
    page_index = rows[caption_row_index]["page_idx"]
    candidates: list[int] = []
    marker_found = False
    index = caption_row_index - 1
    while index >= 0 and rows[index]["page_idx"] == page_index:
        row = rows[index]
        if row["asset_ref"] is not None:
            index -= 1
            continue
        if row["type"] == "text":
            candidates.append(index)
            if _PANEL_START_RE.match(row["raw_text"]):
                marker_found = True
                break
            index -= 1
            continue
        break
    if not marker_found:
        return None
    start_index = min(candidates)
    asset_indices = tuple(
        row_index
        for row_index in range(start_index, caption_row_index + 1)
        if rows[row_index]["asset_ref"] is not None
    )
    if len(asset_indices) < 2:
        return None
    note_indices = tuple(
        row_index
        for row_index in range(start_index, caption_row_index)
        if rows[row_index]["type"] == "text"
    )
    return asset_indices, note_indices


def _figure_note_indices(rows: tuple[dict[str, Any], ...]) -> frozenset[int]:
    notes: set[int] = set()
    for index, row in enumerate(rows[:-1]):
        following = rows[index + 1]
        if (
            row["type"] == "text"
            and _FIGURE_CAPTION_RE.match(row["raw_text"])
            and following["type"] == "text"
            and following["page_idx"] == row["page_idx"]
        ):
            notes.add(index + 1)
    for index, row in enumerate(rows):
        if row["asset_ref"] is not None:
            segment = _explicit_panel_segment(rows, index)
            if segment is not None:
                notes.update(segment[1])
        if row["type"] != "table" or not _extract_labels(_native_caption(row)):
            continue
        if index < 2 or rows[index - 1]["type"] != "image":
            continue
        panel_note = rows[index - 2]
        if (
            panel_note["page_idx"] == row["page_idx"]
            and panel_note["type"] == "text"
            and _PANEL_START_RE.match(panel_note["raw_text"])
            and {"b", "c"}
            <= {letter.casefold() for letter in _HTML_PANEL_RE.findall(row["raw_text"])}
        ):
            notes.add(index - 2)
    return frozenset(notes)


def _jpeg_dimensions(path: Path, relative_path: str) -> tuple[int, int]:
    try:
        with path.open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                raise ValueError("missing JPEG SOI marker")
            while True:
                prefix = stream.read(1)
                if not prefix:
                    raise ValueError("JPEG has no SOF marker")
                if prefix != b"\xff":
                    continue
                marker_byte = stream.read(1)
                while marker_byte == b"\xff":
                    marker_byte = stream.read(1)
                if not marker_byte:
                    raise ValueError("truncated JPEG marker")
                marker = marker_byte[0]
                if marker in {0x01, *range(0xD0, 0xDA)}:
                    continue
                length_bytes = stream.read(2)
                if len(length_bytes) != 2:
                    raise ValueError("truncated JPEG segment length")
                segment_length = int.from_bytes(length_bytes, "big")
                if segment_length < 2:
                    raise ValueError("invalid JPEG segment length")
                if marker in _SOF_MARKERS:
                    payload = stream.read(5)
                    if len(payload) != 5:
                        raise ValueError("truncated JPEG SOF segment")
                    height = int.from_bytes(payload[1:3], "big")
                    width = int.from_bytes(payload[3:5], "big")
                    if width <= 0 or height <= 0:
                        raise ValueError("non-positive JPEG dimensions")
                    return width, height
                stream.seek(segment_length - 2, 1)
    except (OSError, ValueError) as error:
        raise PackageLoadError(FailureCode.PACKAGE_INVALID, relative_path, str(error)) from error


def _x_overlaps(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_bbox = left["bbox_norm_1000"]
    right_bbox = right["bbox_norm_1000"]
    return bool(min(left_bbox[2], right_bbox[2]) > max(left_bbox[0], right_bbox[0]))


def _adjacent_caption_indices(
    rows: tuple[dict[str, Any], ...],
    blocks: tuple[CanonicalTextBlock, ...],
    member_row_indices: tuple[int, ...],
) -> tuple[int, ...]:
    allowed = {TextBlockType.FIGURE_CAPTION, TextBlockType.TABLE_CAPTION}
    found: list[int] = []
    for cursor, step in ((member_row_indices[0] - 1, -1), (member_row_indices[-1] + 1, 1)):
        while 0 <= cursor < len(rows):
            row = rows[cursor]
            if row["page_idx"] != rows[member_row_indices[0]]["page_idx"]:
                break
            if row["type"] == "text" and row["raw_text"] == "":
                cursor += step
                continue
            if blocks[cursor].block_type in allowed and any(
                _x_overlaps(row, rows[index]) for index in member_row_indices
            ):
                found.append(cursor)
            break
    return tuple(sorted(set(found)))


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def build_visuals_and_groups(
    package_root: Path,
    manifest: PaperManifest,
    blocks: tuple[CanonicalTextBlock, ...],
    rows: tuple[dict[str, Any], ...],
) -> tuple[
    tuple[CanonicalVisualAsset, ...],
    tuple[VisualGroup, ...],
    tuple[dict[str, Any], ...],
]:
    note_indices = _figure_note_indices(rows)
    visual_row_indices = tuple(
        index for index, row in enumerate(rows) if row["asset_ref"] is not None
    )
    visual_rows = tuple(rows[index] for index in visual_row_indices)
    row_to_visual = {
        row_index: visual_index for visual_index, row_index in enumerate(visual_row_indices)
    }

    multi_groups: dict[int, tuple[tuple[int, ...], tuple[int, ...]]] = {}
    occupied: set[int] = set()
    for row_index in visual_row_indices:
        segment = _explicit_panel_segment(rows, row_index)
        if segment is None:
            continue
        member_visuals = tuple(row_to_visual[index] for index in segment[0])
        if not occupied.intersection(member_visuals):
            multi_groups[member_visuals[0]] = (member_visuals, segment[1])
            occupied.update(member_visuals)
    for visual_index in range(len(visual_row_indices) - 1):
        if visual_index in occupied or visual_index + 1 in occupied:
            continue
        first_row_index = visual_row_indices[visual_index]
        second_row_index = visual_row_indices[visual_index + 1]
        first = rows[first_row_index]
        second = rows[second_row_index]
        if (
            second_row_index != first_row_index + 1
            or first["page_idx"] != second["page_idx"]
            or {first["type"], second["type"]} != {"image", "table"}
        ):
            continue
        table = first if first["type"] == "table" else second
        image = first if first["type"] == "image" else second
        scheme_labels = tuple(
            label
            for label in _extract_labels(_native_caption(table))
            if label.startswith("Scheme ")
        )
        table_panel_letters = {
            letter.casefold() for letter in _HTML_PANEL_RE.findall(table["raw_text"])
        }
        preceding_note = first_row_index - 1
        has_panel_a = bool(_PANEL_START_RE.match(_native_caption(image) or "")) or (
            preceding_note in note_indices
            and _PANEL_START_RE.match(rows[preceding_note]["raw_text"]) is not None
        )
        if scheme_labels and {"b", "c"} <= table_panel_letters and has_panel_a:
            note_rows = (preceding_note,) if preceding_note in note_indices else ()
            multi_groups[visual_index] = ((visual_index, visual_index + 1), note_rows)
            occupied.update((visual_index, visual_index + 1))

    group_members: list[tuple[int, ...]] = []
    group_notes: list[tuple[int, ...]] = []
    visual_index = 0
    while visual_index < len(visual_row_indices):
        if visual_index in multi_groups:
            members, panel_notes = multi_groups[visual_index]
            group_members.append(members)
            group_notes.append(panel_notes)
            visual_index = members[-1] + 1
        else:
            group_members.append((visual_index,))
            group_notes.append(())
            visual_index += 1

    visual_ids: list[str] = []
    page_visual_ordinals: dict[int, int] = {}
    for row in visual_rows:
        page_index = row["page_idx"]
        ordinal = page_visual_ordinals.get(page_index, 0)
        visual_ids.append(f"MAIN-P{page_index:04d}-A{ordinal:04d}")
        page_visual_ordinals[page_index] = ordinal + 1

    group_ids: list[str] = []
    group_id_by_visual: dict[int, str] = {}
    page_group_ordinals: dict[int, int] = {}
    for members in group_members:
        page_index = visual_rows[members[0]]["page_idx"]
        ordinal = page_group_ordinals.get(page_index, 0)
        group_id = f"MAIN-P{page_index:04d}-VG{ordinal:04d}"
        group_ids.append(group_id)
        page_group_ordinals[page_index] = ordinal + 1
        for member in members:
            group_id_by_visual[member] = group_id

    assets_by_path = {asset.relative_path: asset for asset in manifest.assets}
    visuals: list[CanonicalVisualAsset] = []
    for index, (row, row_index) in enumerate(zip(visual_rows, visual_row_indices, strict=True)):
        asset = assets_by_path[row["asset_ref"]]
        width, height = _jpeg_dimensions(
            package_root.joinpath(*PurePosixPath(asset.relative_path).parts),
            asset.relative_path,
        )
        source = blocks[row_index].source
        visuals.append(
            CanonicalVisualAsset(
                visual_id=visual_ids[index],
                asset=asset,
                page_index=row["page_idx"],
                bbox=source.bbox,
                width=width,
                height=height,
                native_caption=_native_caption(row),
                source=source,
                visual_group_id=group_id_by_visual[index],
            )
        )

    specs: list[dict[str, Any]] = []
    for members, explicit_notes in zip(group_members, group_notes, strict=True):
        member_row_indices = tuple(visual_row_indices[index] for index in members)
        adjacent = _adjacent_caption_indices(rows, blocks, member_row_indices)
        caption_note_indices = set(explicit_notes)
        for caption_index in adjacent:
            cursor = caption_index + 1
            while (
                cursor < len(rows)
                and rows[cursor]["page_idx"] == rows[caption_index]["page_idx"]
                and cursor in note_indices
            ):
                caption_note_indices.add(cursor)
                cursor += 1
        caption_indices = tuple(sorted((*adjacent, *caption_note_indices)))
        adjacent_labels = [
            label for index in adjacent for label in _extract_labels(rows[index]["raw_text"])
        ]
        native_texts = [_native_caption(visual_rows[index]) for index in members]
        native_labels = [label for text in native_texts for label in _extract_labels(text)]
        labels = list(_unique([*adjacent_labels, *native_labels]))
        if len(members) > 1 and labels:
            base = next(
                (label for label in labels if re.fullmatch(r"(?:Figure|Scheme) \d+", label)),
                labels[0],
            )
            group_panel_letters: list[str] = []
            for text in native_texts:
                if text is not None:
                    group_panel_letters.extend(_PAREN_PANEL_RE.findall(text))
                    group_panel_letters.extend(_PANEL_START_RE.findall(text))
            for index in members:
                group_panel_letters.extend(_HTML_PANEL_RE.findall(visual_rows[index]["raw_text"]))
            for note_index in caption_note_indices:
                group_panel_letters.extend(_PANEL_START_RE.findall(rows[note_index]["raw_text"]))
            unique_letters: dict[str, str] = {}
            for letter in group_panel_letters:
                unique_letters.setdefault(letter.casefold(), letter)
            labels = [
                base,
                *(f"{base}{unique_letters[key]}" for key in sorted(unique_letters)),
            ]

        if len(members) > 1:
            caption_text = next(
                (text for text in reversed(native_texts) if _extract_labels(text)),
                None,
            )
            status = "CONFIRMED"
        elif adjacent:
            caption_text = "\n".join(rows[index]["raw_text"] for index in caption_indices)
            status = (
                "AMBIGUOUS"
                if adjacent_labels and native_labels and set(adjacent_labels) != set(native_labels)
                else "CONFIRMED"
            )
        else:
            caption_text = next((text for text in native_texts if text is not None), None)
            status = "CONFIRMED" if labels else "NO_CAPTION"
        specs.append(
            {
                "labels": labels,
                "caption_indices": caption_indices,
                "caption_text": caption_text,
                "status": status,
                "adjacent_labels": adjacent_labels,
                "native_labels": native_labels,
                "members": members,
                "member_row_indices": member_row_indices,
            }
        )

    for index, spec in enumerate(specs):
        if spec["status"] == "AMBIGUOUS" and spec["native_labels"]:
            previous = specs[index - 1] if index > 0 else None
            if previous is not None and not previous["labels"]:
                current_page = visual_rows[spec["members"][0]]["page_idx"]
                previous_page = visual_rows[previous["members"][0]]["page_idx"]
                if current_page == previous_page:
                    previous["labels"] = list(spec["native_labels"])
                    previous["status"] = "AMBIGUOUS"
        if spec["adjacent_labels"] or not spec["native_labels"] or len(spec["members"]) != 1:
            continue
        previous = specs[index - 1] if index > 0 else None
        if previous is None or previous["labels"] or len(previous["members"]) != 1:
            continue
        current_row = spec["member_row_indices"][0]
        previous_row = previous["member_row_indices"][0]
        if (
            rows[current_row]["page_idx"] == rows[previous_row]["page_idx"]
            and current_row - previous_row <= 2
        ):
            previous["labels"] = list(spec["native_labels"])
            previous["status"] = "AMBIGUOUS"
            spec["status"] = "AMBIGUOUS"

    groups = tuple(
        VisualGroup(
            visual_group_id=group_id,
            member_visual_ids=tuple(visual_ids[index] for index in members),
            label_candidates=tuple(spec["labels"]),
            caption_block_ids=tuple(blocks[index].block_id for index in spec["caption_indices"]),
            caption_text=spec["caption_text"],
            association_status=spec["status"],
        )
        for group_id, members, spec in zip(group_ids, group_members, specs, strict=True)
    )
    return tuple(visuals), groups, visual_rows
