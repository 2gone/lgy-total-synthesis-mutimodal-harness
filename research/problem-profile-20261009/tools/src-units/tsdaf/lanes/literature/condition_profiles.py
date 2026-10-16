"""Compile model-declared condition relations using verified source positions.

The middle layer decides points versus interval and procedural ownership. This pass
runs once after the receipts have been applied, before public rekeying. It never
infers a temporal relation from a word, receipt arrival, or numeric magnitude.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from tsdaf.contracts.literature.expressions import expressed_value
from tsdaf.contracts.literature.schema import machine_profile
from tsdaf.lanes.literature.condition_categories import normalize_condition
from tsdaf.lanes.literature.fact_projection import present
from tsdaf.lanes.literature.reported_scalars import parse_reported_scalar
from tsdaf.lanes.literature.review_markers import append_review_markers
from tsdaf.lanes.literature.stage_ledger import EnrichmentLedgers


def condition_values(text: str, relation: str) -> list[dict[str, Any]] | None:
    """The existing scalar parser reads units; the model determines their relation.

    The range parser retains written endpoint order (despite the public min/max
    names). Only an explicit ORDERED_POINTS decision turns them into successive
    points, retaining the author's original phrase on both values.
    """
    scalar = dict(parse_reported_scalar(text))
    if relation == "VALUE":
        return [scalar]
    if scalar["range_min_decimal"] is None or scalar["range_max_decimal"] is None:
        return None
    points = []
    for end in ("min", "max"):
        point = dict(scalar)
        point["numeric_value_decimal"] = scalar[f"range_{end}_decimal"]
        point["normalized_value_decimal"] = scalar[f"normalized_range_{end}_decimal"]
        for key in (
            "range_min_decimal",
            "range_max_decimal",
            "normalized_range_min_decimal",
            "normalized_range_max_decimal",
        ):
            point[key] = None
        points.append(point)
    return points


def condition_source_value(note: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Project only explicit member order or one source phrase; never pick a part."""

    def single_text(part: Mapping[str, Any]) -> str | None:
        if "text" in part:
            text = expressed_value(part["text"])
            return text if isinstance(text, str) else None
        parts = part.get("source_parts", ())
        return parts[0]["text"] if len(parts) == 1 else None

    if "text" in note and expressed_value(note["text"]) is None:
        return None
    sequence = note.get("sequence")
    if not sequence:
        text = single_text(note)
        if text is None:
            return None
        return condition_values(text, note.get("value_relation", "VALUE")) or [
            dict(parse_reported_scalar(text))
        ]
    values = []
    for member in sequence:
        typed = member.get("values", ())
        words = [
            v["text"]
            for v in typed
            if normalize_condition(v["category_word"], v["text"]) == "TEMPERATURE"
        ]
        if not typed:
            text = single_text(member)
            if text is None:
                return None
            words = [text]
        for word in words:
            scalar = dict(parse_reported_scalar(word))
            if scalar["unit"] is None and note.get("shared_unit_text"):
                with_unit = dict(parse_reported_scalar(word + " " + note["shared_unit_text"]))
                if with_unit["unit"] is not None:
                    scalar = with_unit
                    scalar["raw_text"] = word
            values.append(scalar)
    return values or None


def compile_condition_profiles(
    machine: Mapping[str, Any], ledgers: EnrichmentLedgers | None
) -> dict[str, Any]:
    """Fill only an absent curve from reliably ordered readings of this operation.

    Unknown stage/value placement or incomparable packages keep their raw notes.
    Duplicate receipts at the same source site are one reading; equal temperatures
    at different sites remain separate points. No broad operation conflict is made.
    """
    if ledgers is None:
        return dict(machine)
    result = deepcopy(dict(machine))
    for marker in ledgers.accepted_review_markers:
        append_review_markers(
            result,
            [marker],
            target_type=marker["target_object_type"],
            target_id=marker["target_object_id"],
            field_key=marker["field_key"],
        )
    if machine_profile(result).expression:
        return result
    absorbed: set[int] = set()
    for block in result["domain_payload"]["reaction_blocks"]:
        for operation in block["operations"]:
            field = operation["temperature_profile"]
            if (
                field.get("state") == "PRESENT"
                or field.get("reason_code") == "CONFLICTING_CANDIDATES"
            ):
                continue
            operation_id = operation["operation_id"]
            entries = [
                row
                for row in ledgers.condition_readings
                if row["operation_id"] == operation_id and row["category"] == "TEMPERATURE"
            ]
            if not entries:
                continue
            stages = {stage["anchor_hash"]: stage for stage in ledgers.stages.get(operation_id, ())}
            # A partial or SKELETON reading cannot become an invented whole curve.
            if any(
                row["primary"] == "SKELETON"
                or row["site"] is None
                or (row["extent"] == "WITHIN_OWNER" and row["stage_anchor"] is None)
                or (row["stage_anchor"] is not None and not stages[row["stage_anchor"]]["sites"])
                for row in entries
            ):
                continue
            if len({row["site"][0] for row in entries}) != 1:
                continue
            outside_stage = False
            for row in entries:
                if row["stage_anchor"] is None:
                    continue
                start = min(stages[row["stage_anchor"]]["sites"])
                later = [
                    site
                    for stage in stages.values()
                    for site in stage["sites"]
                    if site[0] == start[0] and (site[1], site[2]) > (start[1], start[2])
                ]
                stop = min(later) if later else None
                at = (row["site"][1], row["site"][2])
                if (
                    row["site"][0] != start[0]
                    or at < (start[1], start[2])
                    or (stop is not None and at >= (stop[1], stop[2]))
                ):
                    outside_stage = True
                    break
            if outside_stage:
                continue
            ordered: dict[tuple[str, int], dict[str, Any]] = {}
            ambiguous = False
            for row in entries:
                site = (row["site"][1], row["site"][2])
                prior = ordered.get(site)
                if prior is not None and (
                    prior["condition_id"] != row["condition_id"]
                    or (prior["text"], prior["value_relation"])
                    != (row["text"], row["value_relation"])
                ):
                    ambiguous = True
                    break
                if prior is None:
                    ordered[site] = deepcopy(row)
                else:
                    for ref in row["evidence_refs"]:
                        if ref not in prior["evidence_refs"]:
                            prior["evidence_refs"].append(ref)
            if ambiguous:
                continue
            values, refs = [], []
            for _, row in sorted(ordered.items()):
                parsed = condition_values(row["text"], row["value_relation"])
                if parsed is None:
                    ambiguous = True
                    break
                values.extend(parsed)
                for ref in row["evidence_refs"]:
                    if ref not in refs:
                        refs.append(ref)
            if not ambiguous and values:
                operation["temperature_profile"] = present(values, refs)
                # Whole values are now carried by the fixed field and its evidence.
                # Scoped notes keep the procedural wrapping; avoid a duplicate whole
                # note for the same successfully compiled value.
                whole_words = {row["text"] for row in entries if row["extent"] == "WHOLE_OWNERS"}
                absorbed.update(id(row) for row in entries if row["extent"] == "WHOLE_OWNERS")
                operation["other_conditions"] = [
                    note
                    for note in operation["other_conditions"]
                    if not (
                        note.get("category") == "TEMPERATURE"
                        and note["extent"] == "WHOLE_OWNERS"
                        and note["text"] in whole_words
                        and all(ref in refs for ref in note["evidence_refs"])
                    )
                ]

    # Every row here was accepted by apply. A whole temperature note absorbed by
    # this pass now lives on the curve; all other readings retain their notes.
    for row in ledgers.condition_readings:
        if not row["review_reason"]:
            continue
        field = "temperature_profile" if id(row) in absorbed else "other_conditions"
        append_review_markers(
            result,
            [{"reason": row["review_reason"], "evidence_refs": row["evidence_refs"]}],
            target_type="OPERATION",
            target_id=row["operation_id"],
            field_key=field,
        )
    return result
