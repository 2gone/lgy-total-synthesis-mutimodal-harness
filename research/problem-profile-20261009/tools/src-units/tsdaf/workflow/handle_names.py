"""What a model calls the handles the shared loop hands it: its actions and its saves.

The loop answers a write with an action id per item (`write:…`), a read with one per
location (`read:…`), and a save with the item's handle (`item:…`) and the location of
the version saved (`finding:…`). Each is a hash; the model copies one back to repair a
refused write, to rename a saved item or to
read and withdraw a saved version (batch 2A, E195/E196). Here each is named from what
the host records, never from where it happened to be shown, so a name does not move
when a resumed task is shown less of its past:

- an action by its place in the task's own record of actions (`ActionProgress`):
  "action 3";
- a saved item by the order its handle first came back: "item 2";
- a saved version by its item and the order it came back: "item 2 version 1".

The loop shows every request through these names before the consumer's own, and reads
every call through them first; a name it did not give passes to the consumer as written.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from tsdaf.lanes.literature.model_names import Names, minted
from tsdaf.workflow.action_progress import ActionRecord
from tsdaf.workflow.tool_turns import recorded_reply

__all__ = ("HANDLE_FIELDS", "handle_names")

ACTION, ITEM, VERSION = "action", "item", "version"
# Where a model writes a handle back, and what it names there.
HANDLE_FIELDS: Mapping[str, frozenset[str]] = {
    "action_id": frozenset({ACTION}),
    "item_id": frozenset({ITEM}),
    # Record selectors address saved items or execution-history action rows.
    "item_ids": frozenset({ITEM, ACTION}),
    "location": frozenset({VERSION}),
}


def _dicts(value: Any) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _dicts(item)


def handle_names(
    records: Sequence[ActionRecord], history: Sequence[dict[str, Any]], *, seed: Names | None = None
) -> Names:
    """The name of every handle the loop has returned in ``history``."""
    names: dict[str, str] = dict(seed.names) if seed is not None else {}
    families: dict[str, str] = dict(seed.families) if seed is not None else {}
    for record in records:
        if minted(record.action_id):
            names[record.action_id] = f"action {record.sequence}"
            families[record.action_id] = ACTION
    items: dict[str, int] = {
        identity: int(match.group(1))
        for identity, name in names.items()
        if families.get(identity) == ITEM and (match := re.fullmatch(r"item (\d+)", name))
    }
    versions: dict[str, int] = {}
    for location, name in names.items():
        if families.get(location) != VERSION:
            continue
        match = re.fullmatch(r"item (\d+) version (\d+)", name)
        if match is None:
            continue
        owner = next(
            (identity for identity, number in items.items() if number == int(match[1])), None
        )
        if owner is not None:
            versions[owner] = max(versions.get(owner, 0), int(match[2]))
    for step in history:
        if step.get("type") != "function_result":
            continue
        try:
            reply = recorded_reply(step)
        except (ValueError, TypeError):
            continue
        for row in _dicts(reply):
            item_handle, item_location = row.get("item_id"), row.get("location")
            if not isinstance(item_handle, str) or not minted(item_handle):
                continue
            number = items.setdefault(item_handle, max(items.values(), default=0) + 1)
            names.setdefault(item_handle, f"item {number}")
            families[item_handle] = ITEM
            if (
                isinstance(item_location, str)
                and minted(item_location)
                and item_location not in names
            ):
                versions[item_handle] = versions.get(item_handle, 0) + 1
                names[item_location] = f"item {number} version {versions[item_handle]}"
                families[item_location] = VERSION
    return Names.of(
        names,
        families,
        field_families=HANDLE_FIELDS,
        reserved=tuple(set(names.values()) | (set(seed.reserved) if seed is not None else set())),
    )
