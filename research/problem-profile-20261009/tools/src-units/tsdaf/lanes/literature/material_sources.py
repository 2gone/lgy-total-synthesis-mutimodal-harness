"""Private, derived material-field sources; never part of the public Material contract.

Rows are carried by the projection/accepted compilation that chose their values, not
inferred from a table's aggregate citations. A publication binds them to its exact
machine; old publications without this metadata offer no false row-level precision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from typing import Any

from tsdaf.contracts.literature.expressions import rekey_source_readings
from tsdaf.contracts.schema import canonical_json_bytes

MATERIAL_SOURCE_FIELDS = ("verbatim_name", "amount", "equivalents")
MATERIAL_RELATION_FIELDS = ("reporting_level", "summary_material_index", "extent", "stage_id")


def bound_material_sources(machine: Mapping[str, Any], rows: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "machine_sha256": sha256(canonical_json_bytes(machine)).hexdigest(),
        "operations": deepcopy(dict(rows)),
    }


def material_sources_for(machine: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    if binding.get("machine_sha256") != sha256(canonical_json_bytes(machine)).hexdigest():
        return {}
    return deepcopy(dict(binding.get("operations") or {}))


def matching_material_sources(
    rows: Sequence[Mapping[str, Any]], index: int, value: Mapping[str, Any]
) -> dict[str, Any]:
    if index >= len(rows) or rows[index].get("value") != value:
        return {}
    return deepcopy(dict(rows[index].get("fields") or {}))


def rekey_material_sources(
    sources: Mapping[str, Sequence[Mapping[str, Any]]],
    id_map: Mapping[str, str],
    blocks: Sequence[Mapping[str, Any]],
    *,
    stage_id_map: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Each identity pass once. Match the full remapped row, never its name alone."""
    operations = {op["operation_id"]: op for b in blocks for op in b["operations"]}
    result: dict[str, Any] = {}
    for old_id, rows in sources.items():
        oid = id_map.get(old_id, old_id)
        op = operations.get(oid)
        if op is None or op["materials"]["state"] != "PRESENT":
            continue
        offered = []
        from tsdaf.lanes.literature.stable_keys import _material_order

        order = sorted(range(len(rows)), key=lambda index: _material_order(rows[index]["value"]))
        positions = {old + 1: new + 1 for new, old in enumerate(order)}
        for index in order:
            entry = deepcopy(dict(rows[index]))
            value = entry["value"]
            rekey_source_readings(value, id_map)
            cid = value.get("compound_id")
            if cid is not None:
                value["compound_id"] = id_map.get(cid, cid)
            if value.get("stage_id") is not None:
                value["stage_id"] = (stage_id_map or {}).get(value["stage_id"], value["stage_id"])
            if "summary_material_index" in value:
                value["summary_material_index"] = positions.get(
                    value["summary_material_index"], value["summary_material_index"]
                )
            offered.append(entry)
        # Canonical sorting is stable even with equal sequence values and identical rows.
        # Consume each source row once; no dict keyed by a non-unique sequence number.
        selected = []
        for row in op["materials"]["value"]:
            match = next((i for i, entry in enumerate(offered) if entry["value"] == row), None)
            selected.append(
                offered.pop(match) if match is not None else {"value": deepcopy(row), "fields": {}}
            )
        if oid in result:  # Never choose arbitrarily between collapsed private identities.
            if result[oid] != selected:
                result[oid] = [
                    {"value": deepcopy(r), "fields": {}} for r in op["materials"]["value"]
                ]
        else:
            result[oid] = selected
    return result


def apply_material_sources(
    previous: Sequence[Mapping[str, Any]],
    old_value: Sequence[Mapping[str, Any]],
    new_value: Sequence[Mapping[str, Any]],
    updates: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Replay only host-compiled, accepted row decisions, in their compilation order."""
    current = [
        {"value": deepcopy(row), "fields": matching_material_sources(previous, i, row)}
        for i, row in enumerate(old_value)
    ]
    for update in updates:
        index = update["index"]
        while len(current) <= index:
            current.append({"value": {}, "fields": {}})
        row = current[index]
        fields = row["fields"] if row["value"] == update["before"] else {}
        if update["reset_identity"]:
            fields = {}
        for key in (*MATERIAL_SOURCE_FIELDS, *MATERIAL_RELATION_FIELDS):
            if update["before"].get(key) != update["after"].get(key):
                fields.pop(key, None)
            if key in update["fields"]:
                refs = [*fields.get(key, []), *update["fields"][key]]
                fields[key] = list({canonical_json_bytes(ref): ref for ref in refs}.values())
        current[index] = {"value": deepcopy(update["after"]), "fields": fields}
    return [
        {"value": deepcopy(row), "fields": matching_material_sources(current, i, row)}
        for i, row in enumerate(new_value)
    ]
