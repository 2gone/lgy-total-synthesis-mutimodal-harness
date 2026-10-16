"""What can be checked about a division into units without reading its chemistry.

A division is the model's; these checks decide nothing about it. Each one names a unit
that deserves a second look and says why, in terms a person could verify by eye: a
label that is not printed where the unit begins, a procedure with no label, the same
label on two procedures, a heading in the middle of a unit. A finding sends that one
unit to a small, local review (`unit_review_request`) instead of asking for the whole
division again -- the whole division is the heavy task (hyperfirin, kalmanol,
principinol B, 2026-10-10: 45-63K tokens of thought for 2-6K of answer), and what a
lighter pass gets wrong is local: across five supplements, a label written as null or
borrowed from a neighbour.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tsdaf.contracts.reading_units import ReadingUnits, UnitKind

LABELLED = frozenset({UnitKind.PROCEDURE, UnitKind.CHARACTERIZATION, UnitKind.CRYSTALLOGRAPHY})
# How much of a unit's beginning its label is looked for in: a title line and the sentence after it.
HEAD_CHARS = 400
_SPACE = re.compile(r'\s+')
_MARKUP = re.compile(r'<[^>]+>|\$|\\mathbf|\\textbf|[{}^_~]')


def _plain(text: str) -> str:
    return _SPACE.sub(' ', _MARKUP.sub('', text)).strip()


def _printed(label: str, text: str) -> bool:
    """Whether the label stands in the text as a token of its own, not inside a longer one."""
    found = re.search(r'(?<![0-9A-Za-z])' + re.escape(_plain(label)) + r'(?![0-9A-Za-z])', text)
    return found is not None


def unit_findings(units: ReadingUnits, texts: Mapping[str, str], block_types: Mapping[str, str]) -> list[dict[str, Any]]:
    """One row per thing worth a second look: the unit's position, a code, and what was seen."""
    out: list[dict[str, Any]] = []
    procedures: dict[str, list[int]] = {}
    labelled_procedures = {label for unit in units.units if unit.kind is UnitKind.PROCEDURE for label in unit.compound_labels}

    def note(index: int, code: str, detail: str) -> None:
        out.append({'unit': index, 'first_block_id': units.units[index].block_ids[0], 'code': code, 'detail': detail})

    for index, unit in enumerate(units.units):
        if unit.kind not in LABELLED:
            if unit.kind is UnitKind.OTHER and sum(len(texts.get(b, '')) for b in unit.block_ids) > 600 and any(
                    b in units.uncovered_block_ids and b not in units.not_shown_block_ids for b in unit.block_ids):
                note(index, 'UNCLAIMED_TEXT', 'rows the division named no unit for carry more than 600 characters')
            continue
        head = _plain(' '.join(texts.get(b, '') for b in unit.block_ids[:2]))[:HEAD_CHARS]
        if unit.compound_label is None:
            if unit.kind is not UnitKind.CRYSTALLOGRAPHY:
                note(index, 'NO_LABEL', f'a {unit.kind.value} unit carries no compound label')
        elif unit.kind is UnitKind.PROCEDURE:
            # A procedure is titled with what it makes. Data seldom repeats the label -- it
            # begins "Rf = 0.3" -- so data is held to the procedure before it instead (below).
            missing = [label for label in unit.compound_labels if not _printed(label, head)]
            if missing:
                note(index, 'LABEL_NOT_AT_START', 'not printed where the unit begins: ' + ', '.join(missing))
        elif unit.kind is UnitKind.CHARACTERIZATION:
            before = next((earlier for earlier in reversed(units.units[:index]) if earlier.kind is UnitKind.PROCEDURE), None)
            if before is not None and set(unit.compound_labels) != set(before.compound_labels) and not all(
                    _printed(label, head) for label in unit.compound_labels):
                note(index, 'DATA_LABEL_DIFFERS', f'data labelled {unit.compound_label}; the procedure before it is labelled '
                     f'{before.compound_label}, and the data does not print its own label where it begins')
        if unit.kind is UnitKind.PROCEDURE:
            for label in unit.compound_labels:
                procedures.setdefault(label, []).append(index)
        elif unit.kind is UnitKind.CHARACTERIZATION:
            for label in unit.compound_labels:
                if label not in labelled_procedures:
                    note(index, 'DATA_WITHOUT_PROCEDURE', f'data labelled {label}, and no procedure unit carries that label')
        inside = [b for b in unit.block_ids[1:] if 'HEADING' in block_types.get(b, '') or 'TITLE' in block_types.get(b, '')]
        if inside:
            note(index, 'HEADING_INSIDE', 'a heading block lies inside the unit: ' + ', '.join(inside[:3]))
    for label, where in procedures.items():
        if len(where) > 1:
            for index in where:
                note(index, 'LABEL_ON_TWO_PROCEDURES', f'{len(where)} procedure units carry the label {label}')
    return sorted(out, key=lambda row: (row['unit'], row['code']))
