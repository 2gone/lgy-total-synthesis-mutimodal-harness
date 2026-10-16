"""Mechanical procedure-reference alternatives, shared by input and projection."""

import hashlib
from collections.abc import Mapping
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes

TARGET_FIELDS = ("referenced_operation_id", "referenced_unit_block_id", "external_citation")
LOCATION_TARGET_FIELDS = (*TARGET_FIELDS[:2], "referenced_method_location", TARGET_FIELDS[-1])


def procedure_target_kind(
    value: Mapping[str, Any], *, allow_unresolved: bool, allow_location: bool = False
) -> str:
    fields = LOCATION_TARGET_FIELDS if allow_location else TARGET_FIELDS
    for field in fields[:-1]:
        if value.get(field) is not None and (not isinstance(value[field], str) or not value[field]):
            raise ValueError(f"{field} must be a nonempty string or null")
    active = [
        field
        for field in fields
        if (
            value.get(field) is True
            if field == "external_citation"
            else value.get(field) is not None
        )
    ]
    if len(active) > 1:
        raise ValueError(
            "procedure target: choose "
            + ("at most one" if allow_unresolved else "exactly one")
            + " of "
            + ", ".join(fields)
        )
    if not active:
        if not allow_unresolved:
            raise ValueError(
                "procedure target: this profile requires exactly one identified operation, "
                "general-methods unit or explicit external citation; "
                "unresolved target needs the candidate profile"
            )
        return "UNRESOLVED"
    return {
        "referenced_operation_id": "OPERATION",
        "referenced_unit_block_id": "GENERAL_METHODS_UNIT",
        "referenced_method_location": "GENERAL_METHODS_UNIT",
        "external_citation": "EXTERNAL",
    }[active[0]]


def procedure_target_schema(
    *, allow_unresolved: bool, allow_location: bool = False
) -> dict[str, Any]:
    fields = LOCATION_TARGET_FIELDS if allow_location else TARGET_FIELDS
    branches = []
    choices = (None, *fields) if allow_unresolved else fields
    for chosen in choices:
        props = {
            field: (
                {"const": field == chosen}
                if field == "external_citation"
                else {"type": "string", "minLength": 1}
                if field == chosen
                else {"type": "null"}
            )
            for field in fields
        }
        branches.append(
            {"properties": props, **({"required": [chosen]} if chosen is not None else {})}
        )
    return {"oneOf": branches}


def source_catalog_location(revision: str, block_id: str) -> str:
    """The existing source catalog identity, shared by live and sealed reconstruction."""
    return "source:" + hashlib.sha256(canonical_json_bytes([revision, block_id])).hexdigest()[:24]


class AmbiguousMethodUnit(ValueError):
    """A legacy unit name has more than one actual cited source target."""

    def __init__(self, targets: set[tuple[str, str]]) -> None:
        self.targets = targets
        super().__init__(
            "referenced_unit_block_id names GENERAL_METHODS in multiple cited documents. "
            "Choose the intended referenced_method_location from the supplied source locations, "
            "or leave the target unresolved; do not discard valid evidence or guess a document."
        )


def resolve_method_target(
    value: Mapping[str, Any],
    *,
    method_targets: Mapping[str, Mapping[str, str]],
    cited_revisions: set[str],
    legacy_units: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    require_document: bool = True,
) -> dict[str, str] | None:
    """Resolve explicit identity only; a unit name never selects a document by order."""
    if (
        procedure_target_kind(value, allow_unresolved=True, allow_location=True)
        != "GENERAL_METHODS_UNIT"
    ):
        return None
    location = value.get("referenced_method_location")
    allowed = {
        key: dict(target)
        for key, target in method_targets.items()
        if location is not None or target["document_revision_id"] in cited_revisions
    }
    if location is not None:
        if location not in allowed:
            raise ValueError(
                "referenced_method_location must name an authorized GENERAL_METHODS "
                "source; available locations: " + ", ".join(sorted(allowed))
            )
        result = allowed[location]
    else:
        unit = value.get("referenced_unit_block_id")
        candidates = [target for target in allowed.values() if target["unit_block_id"] == unit]
        pairs = {(target["document_revision_id"], target["unit_block_id"]) for target in candidates}
        pairs.update(
            (revision, block)
            for revision, block in legacy_units
            if revision in cited_revisions and block == unit
        )
        if len(pairs) > 1:
            raise AmbiguousMethodUnit(pairs)
        if not pairs:
            raise ValueError(
                "referenced_unit_block_id must name a GENERAL_METHODS first block "
                "in its cited document revision"
            )
        if not candidates:
            if require_document:
                raise ValueError(
                    "general-methods target needs its verified source document binding"
                )
            return None
        result = candidates[0]
    if any(
        not isinstance(result.get(key), str) or not result[key]
        for key in ("document_id", "document_revision_id", "unit_block_id")
    ):
        raise ValueError("general-methods target has an incomplete host source binding")
    return result
