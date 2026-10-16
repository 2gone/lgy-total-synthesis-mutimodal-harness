"""Task-local material selectors follow explicit row identity through repairs.

No persistent scientific identity is added. A selector is never reassigned when its
row disappears; new rows get new task-local selectors. Runtime and publication use
the same mapping, with projection fact IDs supplying actual repair correspondence.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from tsdaf.contracts.literature.objects import experiment_containers


def material_position(operation_id: str, index: int) -> str:
    return f"{operation_id}#m{index}"


def material_rows(machine: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        material_position(str(op["operation_id"]), index): {
            "operation_id": op["operation_id"],
            "index": index - 1,
            "value": deepcopy(row),
        }
        for block in experiment_containers(machine)
        for op in block["operations"]
        for index, row in enumerate(op["materials"].get("value") or (), 1)
    }


def initial_bindings(machine: Mapping[str, Any]) -> dict[str, str | None]:
    return {key: key for key in material_rows(machine)}


def bound_rows(
    machine: Mapping[str, Any], bindings: Mapping[str, str | None] | None = None
) -> dict[str, dict[str, Any]]:
    rows = material_rows(machine)
    if bindings is None:
        return rows
    return {alias: rows[current] for alias, current in bindings.items() if current in rows}


def advance_bindings(
    bindings: Mapping[str, str | None],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    transitions: Mapping[str, str | None],
) -> dict[str, str | None]:
    """Follow only explicit repair correspondence; unchanged tables retain identity."""
    old, new = material_rows(before), material_rows(after)
    operations = {row["operation_id"] for row in (*old.values(), *new.values())}
    changed = {
        op
        for op in operations
        if [v["value"] for v in old.values() if v["operation_id"] == op]
        != [v["value"] for v in new.values() if v["operation_id"] == op]
    }
    result: dict[str, str | None] = {}
    for alias, current in bindings.items():
        if current is None:
            result[alias] = None
        elif current in old and old[current]["operation_id"] not in changed:
            result[alias] = current if current in new else None
        else:
            target = transitions.get(current)
            result[alias] = target if target in new else None
    selected = [value for value in result.values() if value is not None]
    if len(selected) != len(set(selected)):
        raise ValueError("material repair has ambiguous row correspondence")
    # Fresh aliases cannot reuse a withdrawn task-time selector.
    claimed = {v for v in result.values() if v is not None}
    for target, row in new.items():
        if target in claimed:
            continue
        oid = row["operation_id"]
        used = [int(key.rsplit("#m", 1)[1]) for key in result if key.startswith(oid + "#m")]
        alias = material_position(oid, max(used, default=0) + 1)
        result[alias] = target
        claimed.add(target)
    return result


def unchanged_bindings(
    baseline: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, str | None]:
    """Without a repair/accepted-update map, do not reinterpret a moved selector."""
    old, new = material_rows(baseline), material_rows(current)
    return {key: key if new.get(key) == row else None for key, row in old.items()}
