"""Reading write prerequisites and only the joint changes that preserve references.

A malformed optional ruling must not invalidate its Finding. References resolve to
this call's attempted parent version, including failed updates of a saved parent.
The writer validates each resulting candidate; this planner never judges chemistry.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ReadingWritePlan:
    atomic_groups: tuple[tuple[str, ...], ...]
    prerequisites: dict[str, tuple[str, ...]]


def reading_write_groups(
    raw: Mapping[str, Any], saved: Sequence[Mapping[str, Any]]
) -> ReadingWritePlan:
    relevant = {"findings", "conflicts", "names", "same_measurements"}
    old_rows = [e for e in saved if e["collection"] in relevant]
    by_handle = {e["item_id"]: e for e in old_rows}
    by_identity = {
        (
            e["collection"],
            e["value"].get("finding_id" if e["collection"] == "findings" else "finding"),
        ): e
        for e in old_rows
    }
    actions: dict[str, tuple[str, Mapping[str, Any] | None, Mapping[str, Any] | None, str]] = {}
    submitted: dict[str, list[str]] = {}
    for collection, values in raw.items():
        if collection not in relevant | {"withdrawals"} or not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            if not isinstance(value, Mapping):
                continue
            key = f"{collection}:{index}"
            if collection == "withdrawals":
                matches = [
                    e
                    for e in old_rows
                    if (value.get("item_id") is not None or value.get("location") is not None)
                    and (value.get("item_id") is None or value["item_id"] == e["item_id"])
                    and (value.get("location") is None or value["location"] == e["location"])
                ]
                if len(matches) != 1:
                    continue
                old = matches[0]
                actions[key] = (old["collection"], old["value"], None, old["item_id"])
            else:
                identity = value.get("finding_id" if collection == "findings" else "finding")
                handle = value.get("item_id")
                old = (by_handle.get(handle) if isinstance(handle, str) else None) or (
                    by_identity.get((collection, identity)) if isinstance(identity, str) else None
                )
                node = old["item_id"] if old else key
                before = old["value"] if old else None
                actions[key] = (collection, before, {**(before or {}), **value}, node)
            submitted.setdefault(actions[key][3], []).append(key)

    aliases: dict[str, set[str]] = {}
    versions = [(e["item_id"], e["collection"], e["value"]) for e in old_rows]
    versions += [(node, c, after) for c, _, after, node in actions.values() if after is not None]
    for node, collection, value in versions:
        if collection != "findings":
            continue
        for field in ("finding_id", "skeleton_material_id"):
            alias = value.get(field)
            if isinstance(alias, str):
                aliases.setdefault(alias, set()).add(node)

    def targets(collection: str, value: Mapping[str, Any] | None) -> set[str]:
        if value is None:
            return set()
        reference = value.get("detail_of") if collection == "findings" else value.get("finding")
        return aliases.get(reference, set()) if isinstance(reference, str) else set()

    parent: dict[str, str] = {key: key for key in actions}

    def root(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(keys: list[str]) -> None:
        for key in keys[1:]:
            parent[root(key)] = root(keys[0])

    dependencies: dict[str, set[str]] = {}
    for key, (collection, before, after, _node) in actions.items():
        for target in targets(collection, after):
            for prerequisite in submitted.get(target, ()):
                if prerequisite != key:
                    dependencies.setdefault(key, set()).add(prerequisite)
        # Existing references need a joint candidate only when their parent's
        # structural/identity fields change, or either end is explicitly removed.
        if before is not None:
            for target in targets(collection, before):
                for prerequisite in submitted.get(target, ()):
                    pc, old, new, _ = actions[prerequisite]
                    critical = (
                        "finding_id",
                        "aspect",
                        "about_ids",
                        "scope",
                        "segment_key",
                        "existing_stage_id",
                        "skeleton_material_id",
                        "reporting_level",
                        "detail_of",
                    )
                    changed = new is None or any((old or {}).get(k) != new.get(k) for k in critical)
                    if changed:
                        union([prerequisite, key])
        # Two explicit forms of one measurement intent in this call are one
        # scientific declaration. Neither syntax may win just by arrival order.
        if collection == "same_measurements" and after is not None:
            for target in targets(collection, after):
                for prerequisite in submitted.get(target, ()):
                    _, _, new, _ = actions[prerequisite]
                    if (
                        new
                        and new.get("aspect") == "yield"
                        and isinstance(new.get("words"), Mapping)
                        and "same_measurement_as" in new["words"]
                    ):
                        union([prerequisite, key])
    declarations: dict[str, list[str]] = {}
    for key, (collection, _before, after, _node) in actions.items():
        if after is None:
            continue
        raw_collection, raw_index = key.split(":")
        supplied = raw[raw_collection][int(raw_index)]
        if collection == "same_measurements":
            identity = after.get("finding")
        elif (
            collection == "findings"
            and after.get("aspect") == "yield"
            and isinstance(supplied.get("words"), Mapping)
            and "same_measurement_as" in supplied["words"]
        ):
            identity = after.get("finding_id")
        else:
            continue
        if isinstance(identity, str):
            declarations.setdefault(identity, []).append(key)
    for keys in declarations.values():
        if len(keys) > 1:
            union(keys)
    groups: dict[str, list[str]] = {}
    for key in actions:
        groups.setdefault(root(key), []).append(key)
    return ReadingWritePlan(
        atomic_groups=tuple(tuple(group) for group in groups.values() if len(group) > 1),
        prerequisites={key: tuple(sorted(deps)) for key, deps in dependencies.items()},
    )


# Both entry sites consume the same dependency rule; there is no material-only
# union implementation that can diverge from the production Reading writer.
material_write_groups = reading_write_groups
ruling_write_groups = reading_write_groups
