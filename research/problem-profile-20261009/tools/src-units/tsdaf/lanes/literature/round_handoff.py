"""A round's source handoff, derived from authorized text and accepted paragraphs.

These ranges describe delivery and ownership, not how much science was extracted.
The same derivation serves the paper task's first sheet and its changing progress.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tsdaf.contracts.reading_units import UnitKind
from tsdaf.lanes.literature.completion_guard import unread_experimental_prose
from tsdaf.lanes.literature.middle_layer import ParagraphSet
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans
from tsdaf.lanes.literature.source_manifest import LiteratureSources

_DATA_KINDS = frozenset(
    {UnitKind.CHARACTERIZATION, UnitKind.CRYSTALLOGRAPHY, UnitKind.BIBLIOGRAPHY, UnitKind.OTHER}
)
_SHORT_PROSE_KINDS = frozenset({UnitKind.GENERAL_METHODS, UnitKind.PROCEDURE})
_IDENTITY = ("document_revision_id", "package_sha256", "source_phase", "source_id")


def source_catalog(sources: LiteratureSources) -> list[dict[str, Any]]:
    """The current detail round's processable text, never inferred from what was read.

    The existing unread-prose rule selects long experimental paragraphs, and reading
    units keep short procedures/general methods in scope. Data and closing matter keep
    their existing owners instead of becoming paper-task work.
    """
    rows: list[dict[str, Any]] = []
    for document in sources.active():
        if document.package is None:
            continue
        part = set(document.part_ids)
        prose = {
            row["source_id"]
            for row in unread_experimental_prose(
                document.package.blocks, readable_ids=part, delivered_scopes=()
            )
        }
        kind_of = (
            {block_id: unit.kind for unit in document.units.units for block_id in unit.block_ids}
            if document.units is not None
            else {}
        )
        for block in document.package.blocks:
            if block.block_id not in part:
                continue
            kind = kind_of.get(block.block_id)
            if kind in _DATA_KINDS:
                continue
            if block.block_id not in prose and kind not in _SHORT_PROSE_KINDS:
                continue
            count = len(block_sentence_spans(block.raw_text))
            if count == 0:
                continue
            rows.append(
                {
                    "document_revision_id": document.registration.document_revision_id,
                    "package_sha256": document.package.canonical_content_sha256,
                    "source_phase": document.registration.source_phase,
                    "source_id": block.block_id,
                    "start_sentence": 0,
                    "end_sentence": count,
                    "head": " ".join(block.raw_text.split())[:80],
                }
            )
    return rows


def paragraph_scopes(
    answer: ParagraphSet | None, selections: Mapping[str, Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Only accepted paragraph handoffs count, never a limitation's citation."""
    if answer is None:
        return []
    return [
        identity
        for paragraph in answer.paragraphs
        for sid in paragraph.selection_ids
        if (selection := selections.get(sid)) is not None
        if isinstance(identity := selection.get("source_identity"), Mapping)
    ]


def _bucket(row: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    parts = tuple(row.get(key) for key in _IDENTITY)
    return parts if all(isinstance(part, str) for part in parts) else None  # type: ignore[return-value]


def _range(row: Mapping[str, Any], limit: int) -> range | None:
    start, end = row.get("start_sentence"), row.get("end_sentence")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start < end <= limit
    ):
        return None
    return range(start, end)


def source_handoff(
    catalog: Sequence[Mapping[str, Any]],
    delivered: Sequence[Mapping[str, Any]],
    handed_off: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Partition every eligible sentence into accepted, delivered, or undelivered."""
    delivered_by: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    handed_by: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    for rows, grouped in ((delivered, delivered_by), (handed_off, handed_by)):
        for row in rows:
            if (key := _bucket(row)) is not None:
                grouped.setdefault(key, []).append(row)
    handled: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    for source in catalog:
        key = _bucket(source)
        if key is None:
            continue
        limit = source.get("end_sentence")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            continue
        read = {
            index
            for row in delivered_by.get(key, ())
            for span in (_range(row, limit),)
            if span is not None
            for index in span
        }
        owned = {
            index
            for row in handed_by.get(key, ())
            for span in (_range(row, limit),)
            if span is not None
            for index in span
        } & read
        statuses = [
            "HANDLED"
            if index in owned
            else "DELIVERED_UNHANDLED"
            if index in read
            else "UNDELIVERED"
            for index in range(limit)
        ]
        begin = 0
        for end in range(1, limit + 1):
            if end < limit and statuses[end] == statuses[begin]:
                continue
            row = {
                **source,
                "start_sentence": begin,
                "end_sentence": end,
                "status": statuses[begin],
            }
            (handled if statuses[begin] == "HANDLED" else remaining).append(row)
            begin = end
    return {"handled_sources": handled, "remaining_sources": remaining}
