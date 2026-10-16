"""Source expressions shared by model writes and the forward public contract."""

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field

from tsdaf.contracts._base import ContractModel

ConditionAdoption = Literal["ADOPTED", "SUPPORTING", "ALTERNATIVE", "UNRESOLVED"]
ValueRelation = Literal["VALUE", "ORDERED_POINTS"]
SourceRelation = Literal["SAME", "ADDS", "CONTRADICTS"]
SourcePrimary = Literal["SKELETON", "FINDING"]


# These arrays preserve contributions, not competing adopted values or ordered steps.
EXPRESSION_SETS = frozenset({"source_statements", "source_readings", "model_interpretations"})


def expressed_value(field: Any) -> Any:
    """Read only a known expression; never select a candidate or retained baseline."""
    if isinstance(field, Mapping) and "state" in field and "value" in field:
        return field["value"] if field["state"] == "PRESENT" else None
    return field


def pending(reason: str = "STAGE_PARTIAL") -> dict[str, Any]:
    return {
        "state": "UNRESOLVED",
        "value": None,
        "reason_code": reason,
        "coverage_scope_ref": None,
        "evidence_uses": [],
        "candidates": [],
    }


def unstated() -> dict[str, Any]:
    """No submitted claim; not a negative or a scientific unresolved item."""
    return {"state": "UNSTATED", "value": None, "reason_code": None,
            "coverage_scope_ref": None, "evidence_uses": [], "candidates": [],
            "model_record_refs": []}


def record_derived(value: object, record_refs: list[dict[str, Any]]) -> dict[str, Any]:
    if not record_refs:
        raise ValueError("record-derived value requires an explicit record reference")
    return {"state": "PRESENT", "value": deepcopy(value), "reason_code": None,
            "coverage_scope_ref": None, "evidence_uses": [], "candidates": [],
            "model_record_refs": deepcopy(record_refs)}


def present(value: object, refs: list[dict[str, str]]) -> dict[str, Any]:
    if not refs:
        raise ValueError("present public fact requires verified evidence")
    return {
        "state": "PRESENT",
        "value": value,
        "reason_code": None,
        "coverage_scope_ref": None,
        "evidence_uses": [{"evidence_ref": ref, "role": "SUPPORT"} for ref in refs],
        "candidates": [],
    }


def remap_expression_field(key: str, value: Any, remap: Callable[[Any], Any]) -> Any:
    """Identity remapping never edits quoted words or explanations in source records."""
    if key in {"author_text", "explanation"}:
        return deepcopy(value)
    if key not in EXPRESSION_SETS:
        return remap(value)
    copied = deepcopy(value)
    if key == "model_interpretations":
        return copied
    for record in copied:
        for field in ("compound_id", "product_ids"):
            if field in record:
                record[field] = remap(record[field])
    return copied


def merge_expression_contributions(before: list, after: list, current: list) -> list:
    """A revised producer removes its exact old contributions, keeping independent ones."""
    removed = [item for item in before if item not in after]
    retained = [item for item in current if item not in removed]
    return merge_interpretations(retained, [item for item in after if item not in before])


def model_interpretation(
    text: str,
    refs: Sequence[Mapping[str, str]],
    *,
    relation: SourceRelation | None = None,
    primary: SourcePrimary | None = None,
) -> dict[str, Any]:
    """Preserve submitted scientific reasoning, distinct from verified author wording.

    Its public shape is defined once in the forward contract generator. References
    belong to this explanation's context, not merely the narrower value quotation.
    """
    return {
        "text": text,
        "evidence_refs": [dict(ref) for ref in refs],
        **({"relation": relation} if relation is not None else {}),
        **({"primary": primary} if primary is not None else {}),
    }


def source_statement(
    statement: str, refs: Sequence[Mapping[str, str]],
    interpretations: Sequence[Mapping[str, Any]] = (),
    **declared: Any,
) -> dict[str, Any]:
    """Keep the author's complete words and explanations attached to this reading."""
    return {
        "statement": statement,
        "evidence_refs": [dict(ref) for ref in refs],
        **declared,
        **(
            {"model_interpretations": merge_interpretations([], interpretations)}
            if interpretations
            else {}
        ),
    }


def material_source_reading(
    *, role: str | None, verbatim_name: str | None, amount: str | list[dict[str, Any]] | None,
    compound_id: str | None,
    refs: Sequence[Mapping[str, str]], identity_relation: str = "SAME",
    interpretations: Sequence[Mapping[str, Any]] = (),
    role_verbatim: str | None = None,
    reported_quantities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Keep source fields separate; joining them would manufacture an author quote."""
    return {
        "role": role,
        **({"role_verbatim": role_verbatim} if role_verbatim else {}),
        "verbatim_name": verbatim_name,
        "amount": deepcopy(amount),
        "compound_id": compound_id,
        "identity_relation": identity_relation,
        **({"reported_quantities": deepcopy(reported_quantities)} if reported_quantities else {}),
        "evidence_refs": [dict(ref) for ref in refs],
        **(
            {"model_interpretations": merge_interpretations([], interpretations)}
            if interpretations
            else {}
        ),
    }


def rekey_source_readings(record: dict[str, Any], ids: Mapping[str, str]) -> None:
    """Remap only declared identities, never strings inside source wording."""
    for reading in record.get("source_readings", []):
        if reading.get("compound_id") is not None:
            reading["compound_id"] = ids.get(reading["compound_id"], reading["compound_id"])
    for reading in record.get("source_statements", []):
        if "product_ids" in reading:
            reading["product_ids"] = [ids.get(value, value) for value in reading["product_ids"]]
    for subject in record.get("subjects", []):
        subject["object_id"] = ids.get(subject["object_id"], subject["object_id"])


def characterization_source_statement(
    statement: str, sample_context: str | None, refs: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """The complete submitted author expression, before method/value projection."""
    return {
        "statement": statement,
        "sample_context": sample_context,
        "evidence_refs": [dict(ref) for ref in refs],
    }


def merge_interpretations(
    existing: Sequence[Mapping[str, Any]], incoming: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Only exact records are idempotent; no wording or source wins by arrival order."""
    result = [dict(item) for item in existing]
    for item in incoming:
        if item not in result:
            result.append(dict(item))
    return result


class ConditionSequenceValue(ContractModel):
    category_word: str = Field(
        min_length=1, description="Kind of this value in this sequence member."
    )
    text: str = Field(min_length=1, description="This value in the author's words.")


class ConditionSequenceMember(ContractModel):
    text: str = Field(
        min_length=1, description="The author's complete phrase for this ordered member."
    )
    values: tuple[ConditionSequenceValue, ...] = Field(
        default=(),
        description=(
            "Optional values belonging together in this member; omit when not individually typed."
        ),
    )


def deduplicate_public_expressions(value: Any) -> None:
    """After evidence rekeying, exact public sets are idempotent; sequences are not sets."""
    if isinstance(value, dict):
        for key, child in value.items():
            deduplicate_public_expressions(child)
            if key in {
                "model_interpretations",
                "procedure_sources",
                "source_statements",
                "source_readings",
            } and isinstance(child, list):
                value[key] = merge_interpretations((), child)
    elif isinstance(value, list):
        for child in value:
            deduplicate_public_expressions(child)


def resolved_condition_note(note: Mapping[str, Any]) -> dict[str, Any]:
    """Wrap directly compiled fields; preserve existing unresolved/candidate states."""
    result = deepcopy(dict(note))
    refs = result["evidence_refs"]
    for key in ("text", "category_verbatim", "extent"):
        if key in result and not isinstance(result[key], Mapping):
            if result[key] is None:
                result.pop(key)
            else:
                result[key] = present(result[key], refs)
    if result.get("model_interpretations"):
        result["model_interpretations"] = [
            deepcopy(entry) if "state" in entry else present(entry, entry["evidence_refs"])
            for entry in result["model_interpretations"]
        ]
    return result
