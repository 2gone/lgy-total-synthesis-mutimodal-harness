"""What a task writes for a container field, and what the host makes of it.

The public shape of a recorded value is the host's bookkeeping: a quantity carries nine
keys (the words, the parsed number or range, the unit, and the same three normalized),
and an operation's intermediate carries a whole resolved value with its evidence uses
and its candidate list. None of that is something a reader of a paper knows. Asking a
model for it costs 6,492 bytes of shapes on every turn of every task, and every one of
those keys is a chance to be refused for a wrapper it guessed wrong.

So the task writes what the author wrote -- "16 h", "-78 degrees C", "1.2 equiv" -- and
this module turns it into the public shape, with the evidence the host already holds.
The reduced shapes here are what the task is shown (``written_value_definitions``); the
projection (``project_written_value``) is the same table read the other way.

A value that already arrives in the public shape is kept as it is. That is not
politeness: the replay corpus is real calls a real model made under the old shapes, and
the day this module refused them we would have lost the only evidence that today's
contract still accepts what a model actually wrote.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from tsdaf.contracts.literature.schema import fact_definition
from tsdaf.lanes.literature.fact_projection import pending
from tsdaf.lanes.literature.material_meanings import (
    MATERIAL_AMOUNT_MEANING,
    MATERIAL_ROLE_GUIDANCE,
    MATERIAL_ROLES,
)
from tsdaf.lanes.literature.reported_scalars import (
    parse_material_quantity,
    parse_reported_outcome,
    parse_reported_scalar,
    parse_written_material_parts,
    parse_written_material_quantity,
)

__all__ = (
    "WRITTEN_SHAPES",
    "project_written_value",
    "written_value_definitions",
)

# Reuse the public representation vocabulary; a description alone declares no format.
STRUCTURE_FORMATS = tuple(
    cast(Any, fact_definition("StructureValue"))["properties"]["input_format"]["enum"]
)


_SCALAR_KEYS = (
    "raw_text",
    "numeric_value_decimal",
    "range_min_decimal",
    "range_max_decimal",
    "unit",
    "normalized_value_decimal",
    "normalized_range_min_decimal",
    "normalized_range_max_decimal",
    "normalized_unit",
)

# The shape a task is shown for a value the host parses. Each replaces the contract
# definition of the same name in what travels to the model; everything the replaced
# definition reached (evidence refs, coverage scopes, candidate lists) leaves with it.
WRITTEN_SHAPES: dict[str, dict[str, Any]] = {
    "ReportedScalarValue": {
        "type": "string",
        "minLength": 1,
        "description": (
            "The quantity in the author's own words, with his unit and his precision: "
            '"16 h", "-78 degrees C", "1.2 equiv", "0.05 M". The host parses and '
            "normalizes it; do not convert, round or restate it as a number."
        ),
    },
    "ReportedScalarArrayValue": {
        "type": "array",
        "items": {"$ref": "#/$defs/ReportedScalarValue"},
        "minItems": 0,
    },
    "MaterialArrayValue": {
        "type": "array",
        "items": {"$ref": "#/$defs/Material"},
        "minItems": 0,
    },
    # No task is shown this shape since the retired container patch; it keeps no wording
    # of its own. What a role means and what an amount is come from the one table.
    "Material": {
        "type": "object",
        "additionalProperties": False,
        "required": ["role", "verbatim_name"],
        "properties": {
            "role": {"enum": list(MATERIAL_ROLES), "description": MATERIAL_ROLE_GUIDANCE},
            "verbatim_name": {"type": "string", "minLength": 1},
            "amount": {
                "anyOf": [{"$ref": "#/$defs/ReportedScalarValue"}, {"type": "null"}],
                "description": "For this material, " + MATERIAL_AMOUNT_MEANING + ".",
            },
            "compound_id": {"anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}]},
            # `sequence_index` is not here and is not asked for: list them in the order
            # the author wrote them and the host numbers them from that order.
        },
    },
    "OperationIntermediateValue": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "local_intermediate_id",
            "produced_by_operation_id",
            "consumed_by_operation_id",
        ],
        "properties": {
            "local_intermediate_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Your name for the species this operation makes and the next consumes."
                ),
            },
            "produced_by_operation_id": {"type": "string", "minLength": 1},
            "consumed_by_operation_id": {"type": "string", "minLength": 1},
            "author_label": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": "What the author calls it, if he names it at all.",
            },
            "structure": {
                "anyOf": [{"type": "string", "minLength": 1}, {"type": "null"}],
                "description": "A SMILES string, only when the author gives one; else null.",
            },
        },
    },
}


def written_value_definitions(fields: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    """The value shapes one task is shown: the reduced ones where there is one.

    Walks the same way the contract's own ``value_definitions`` walks, but a name with a
    written shape resolves to that shape, and only that shape's references are followed.
    """
    definitions: dict[str, object] = {}

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            reference = value.get("$ref")
            if isinstance(reference, str):
                name = reference.rsplit("/", 1)[-1]
                if name not in definitions:
                    definitions[name] = WRITTEN_SHAPES.get(name) or fact_definition(name)
                    visit(definitions[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for field in fields:
        visit(field["value_schema"])
    return definitions


# Which parser a phrase takes is decided by what the quantity IS, not by the JSON shape
# it lands in -- the host has three, and the route lane has always chosen between them by
# field (`fact_projection`). A yield is "1.58 g (57% over 2 steps)", where the percentage
# is the value and the mass stays in the words; a material's amount is "2.00 g, 13.3
# mmol", where the moles are the amount. Read with the strict parser, both are a phrase
# with no single quantity in it, and every number in them is lost -- which is exactly
# what happened while this table did not exist.
_OUTCOME_VALUE_FIELDS = frozenset({"reported_value"})


def _phrase(text: str, field_key: str | None) -> dict[str, Any]:
    if field_key in _OUTCOME_VALUE_FIELDS:
        return dict(parse_reported_outcome(text)[0])
    return dict(parse_reported_scalar(text))


def _scalar(value: Any, field_key: str | None = None) -> Any:
    """One quantity: the author's words parsed, or a public scalar left as it came."""
    if isinstance(value, str):
        return _phrase(value, field_key)
    if isinstance(value, Mapping) and isinstance(value.get("raw_text"), str):
        if all(key in value for key in _SCALAR_KEYS):
            return dict(value)
        # Not the public shape, whatever else it carries: the words are the value, and
        # reading them is the host's job. Keeping the writer's own half-filled keys once
        # made a quantity with a number, a normalized unit, and no unit.
        return _phrase(str(value["raw_text"]), field_key)
    return value


def _scalar_array(value: Any, field_key: str | None = None) -> Any:
    """A profile is a list of quantities; one quantity written alone is a list of one.

    "150 degrees C" is the whole temperature profile of most steps, and a task that
    writes it is not making a shape mistake -- the list is the host's.
    """
    if isinstance(value, str | Mapping):
        return [_scalar(value, field_key)]
    return [_scalar(item, field_key) for item in value] if isinstance(value, list) else value


def _materials(value: Any) -> Any:
    """One material: what it is, what it does, and the author's phrase for how much.

    `sequence_index` is the order the author wrote them in (E53), which is the order the
    task listed them in: the host numbers them, and the published set sorts by it. A
    material without one degrades to a sort by role and name, and a later SAME on the
    field is refused for not matching the value it is preserving.
    """
    if not isinstance(value, list):
        return value
    materials = []
    for position, item in enumerate(value, 1):
        if not isinstance(item, Mapping):
            materials.append(item)
            continue
        material = dict(item)
        amount, equivalents = material.get("amount"), material.get("equivalents")
        stated = None
        if isinstance(amount, str):
            # The author's one phrase for how much, read once: moles, mass or volume are
            # the amount, equivalents or mol % the equivalents ("26 mg (0.25 mmol, 1
            # equiv)" states both), and words stating neither stay the amount unread.
            material["amount"], stated = parse_written_material_quantity(amount)
        elif isinstance(amount, list):
            # These public SourceParts belong to this amount field, not an
            # untyped reading-wide list. Source authorization precedes projection.
            material["amount"], stated = parse_written_material_parts(
                [part["text"] for part in amount]
            )
        else:
            material["amount"] = _scalar(amount)
        if isinstance(equivalents, str):
            # Only a retired detail-task patch wrote a phrase here: what it states as
            # equivalents, or nothing. Its first quantity whatever it was ("carried or
            # found") published 38 of b4-a4's moles as equivalents.
            stated = parse_material_quantity(equivalents)[1]
        elif equivalents is not None:
            stated = _scalar(equivalents)
        material["equivalents"] = stated
        for key in ("role", "compound_id", "verbatim_name"):
            material.setdefault(key, None)
        material.setdefault("sequence_index", position)
        materials.append(material)
    return materials


def project_structure(
    raw: Any, refs: Sequence[Mapping[str, str]], input_format: str | None = None
) -> Any:
    """A structure the author gave, as the public resolved value; else an open field."""
    if isinstance(raw, Mapping):
        return dict(raw)
    if input_format is None:
        return pending()
    if input_format not in STRUCTURE_FORMATS or not isinstance(raw, str) or not raw.strip():
        raise ValueError(
            "A declared structure format requires the author's nonempty representation."
        )
    return {
        "state": "PRESENT",
        "value": {
            "input_format": input_format,
            "raw_value": raw,
            "canonical_smiles": None,
            "isomeric_smiles": None,
            "inchi": None,
            "inchi_key": None,
            "normalizer_name": None,
            "normalizer_version": None,
        },
        "reason_code": None,
        "coverage_scope_ref": None,
        "evidence_uses": [{"evidence_ref": ref, "role": "SUPPORT"} for ref in refs],
        "candidates": [],
    }


def _operation_intermediate(value: Any, refs: Sequence[Mapping[str, str]]) -> Any:
    if not isinstance(value, Mapping):
        return value
    intermediate = dict(value)
    intermediate.setdefault("author_label", None)
    raw = intermediate.get("structure")
    # This legacy written shape explicitly declares SMILES; no format inference.
    intermediate["structure"] = project_structure(
        raw, refs, input_format="SMILES" if raw is not None else None
    )
    # The passages the task cited, bound to the record: the task names its evidence
    # once, in selection_ids, and never restates it inside a value.
    intermediate.setdefault("evidence_refs", [dict(ref) for ref in refs])
    return intermediate


def project_written_value(
    schema_name: str,
    value: Any,
    refs: Sequence[Mapping[str, str]] = (),
    field_key: str | None = None,
) -> Any:
    """What the host records for what a task wrote, under one field's value schema.

    ``schema_name`` is the name a container field's schema carries, which is the name of
    the whole recorded field (`ResolvedValue_X`), and an optional field's is
    `ResolvedValue_NullableX`. Both prefixes are the host's bookkeeping around the value
    -- what state it is in, what evidence holds it up, what else was a candidate -- and
    `X` is the value itself, which is all a task writes. Reading the name literally
    matched nothing at all, and every value a task wrote went in exactly as it came.
    """
    if value is None:
        return None
    schema_name = schema_name.removeprefix("ResolvedValue_").removeprefix("Nullable")
    if schema_name == "ReportedScalarValue":
        return _scalar(value, field_key)
    if schema_name == "ReportedScalarArrayValue":
        return _scalar_array(value, field_key)
    if schema_name == "MaterialArrayValue":
        return _materials(value)
    if schema_name == "OperationIntermediateValue":
        return _operation_intermediate(value, refs)
    return value
