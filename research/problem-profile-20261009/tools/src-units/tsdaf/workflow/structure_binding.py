"""Bind structures read from a paper's schemes to the compounds of its published result.

The image branch (detector boxes, the printed label beside each box, one structure per
crop) was built and tuned as the review workbench's display overlay
(`e293-expression-review/tools/display_overlay`: `build.py`, `rebind.py`). This is its
binding rule brought into the harness unchanged, so a published result can carry the
structures that branch read:

    a printed label binds when exactly one compound carries it as an author label;
    more than one is AMBIGUOUS, none is UNBOUND, and a box with no label is NO_LABEL.

Nothing is guessed: an unresolved box keeps its structure and gets no compound, and every
unresolved label is listed. The two sides are compared after folding only surrounding
whitespace and full-width characters (ruled 2026-09-20): anything folded binds silently,
so the list is that short, and signs and stereodescriptors stay differences for a person.

The output is a sidecar, not part of the contract: every structure in it is a model
reading of a drawing, and says so.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SCHEMA_VERSION = 'tsdaf.structure-binding.v0'
AUTHORITY = 'MODEL_READING_NOT_CONFIRMED'
# Full-width ASCII (U+FF01..U+FF5E) and the ideographic space, to their half-width forms.
_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)} | {0x3000: 0x20}


def normalize_label(label: str) -> str:
    """The form a printed label and an author label are compared in."""
    return str(label).translate(_FULLWIDTH).strip()


def author_labels(compound: Mapping[str, Any]) -> list[str]:
    """Both shapes the contract has used: a bare list, or a resolved value around one."""
    value = compound.get('author_labels')
    if isinstance(value, Mapping):
        value = value.get('value')
    return [str(item) for item in value or []]


def label_index(compounds: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for compound in compounds:
        for label in author_labels(compound):
            found = index.setdefault(normalize_label(label), [])
            if compound['compound_id'] not in found:
                found.append(compound['compound_id'])
    return index


def bind_structures(machine: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """The overlay's detections bound to this result's compounds, with what did not bind."""
    compounds = list(machine['domain_payload']['compounds'])
    index = label_index(compounds)
    by_compound: dict[str, list[dict[str, Any]]] = {}
    unresolved: list[dict[str, Any]] = []
    counts = {'BOUND': 0, 'AMBIGUOUS': 0, 'UNBOUND': 0, 'NO_LABEL': 0}
    for scheme in overlay.get('schemes') or []:
        for item in scheme.get('detections') or []:
            label = str(item.get('printed_label') or '').strip()
            matches = index.get(normalize_label(label), []) if label else []
            status = 'NO_LABEL' if not label else 'BOUND' if len(matches) == 1 else 'AMBIGUOUS' if matches else 'UNBOUND'
            counts[status] += 1
            read = item.get('ocsr') or {}
            structure = {
                'detection_id': item.get('detection_id'), 'figure': scheme.get('logical_name'),
                'figure_asset_sha256': scheme.get('asset_sha256'), 'crop_sha256': item.get('crop_sha256'),
                'bbox_px': item.get('bbox_px'), 'printed_label': label or None, 'binding': status,
                'readable': read.get('readable'), 'smiles': read.get('isomeric_smiles') or read.get('smiles') or None,
                'inchi_key': read.get('inchi_key'), 'formula': read.get('formula'),
                'is_abstract': read.get('is_abstract'), 'reader_confidence': read.get('confidence'),
            }
            if status == 'BOUND':
                by_compound.setdefault(matches[0], []).append(structure)
            else:
                unresolved.append({**structure, 'candidates': matches})
    rows = []
    for compound in compounds:
        structures = by_compound.get(compound['compound_id'], [])
        keys = {s['inchi_key'] for s in structures if s['inchi_key']}
        rows.append({
            'compound_id': compound['compound_id'], 'author_labels': author_labels(compound), 'structures': structures,
            # Several drawings of one compound that read differently are shown, not chosen between.
            'readings_agree': None if len(structures) < 2 else len(keys) == 1,
        })
    with_structure = sum(1 for row in rows if any(s['smiles'] for s in row['structures']))
    return {
        'schema_version': SCHEMA_VERSION, 'authority': AUTHORITY, 'paper_id': machine.get('paper_id'),
        'machine_result_id': machine.get('machine_result_id'),
        'notice': ('Structures were read from drawings by a model and bound to compounds by printed label only. '
                   'They are readings, not confirmed structures.'),
        'reader': (overlay.get('provenance') or {}),
        'compounds': rows, 'unresolved': unresolved,
        'summary': {'compounds': len(rows), 'compounds_with_a_structure': with_structure,
                    'compounds_with_an_author_label': sum(1 for row in rows if row['author_labels']),
                    'compounds_whose_drawings_disagree': sum(1 for row in rows if row['readings_agree'] is False),
                    'detections': sum(counts.values()), **{key.lower(): value for key, value in counts.items()}},
    }
