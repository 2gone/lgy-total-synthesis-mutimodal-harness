"""Which collections a task can write, and how one written item names its subject.

Neutral mechanics. The registry knows a collection's name, whether it is written
one element at a time or as one envelope, and the order receipts appear in. What
an element means, and how its subject identity is derived, stays with the site
that registers it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal


def item_key(collection: str, index: int | None = None) -> str:
    """The one name a written item answers to, in receipts and in repairs alike."""
    return collection if index is None else f"{collection}:{index}"


def collection_of(key: str) -> str:
    """The registered collection an item key belongs to."""
    return key.split(":", 1)[0]


@dataclass(frozen=True)
class WriteTarget:
    """One writable collection of a task.

    ``per_item`` reads a list under ``name`` and gives every element its own
    ``name:index`` item key, its own acceptance and its own receipt. ``envelope``
    covers ``fields`` as a single item keyed by ``name``: they are validated
    together, so they are accepted or rejected together.

    ``identity`` reads what an item targets without accepting it: a rejected write
    must still name what it tried to change, or its repair cannot be matched later.
    It returns ``None`` when the item does not identify a subject.
    """

    name: str
    mode: Literal["per_item", "envelope"]
    identity: Callable[[Any], str | None]
    fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode == "envelope" and not self.fields:
            raise ValueError("an envelope target must name the payload fields it covers")
        if self.mode == "per_item" and self.fields:
            raise ValueError("a per-item target is keyed by its own name, not by fields")


class WriteRegistry:
    """The write targets one task site offers, in the order their receipts appear."""

    def __init__(self, targets: Sequence[WriteTarget]) -> None:
        names = [target.name for target in targets]
        if len(set(names)) != len(names):
            raise ValueError("a write target name is registered twice")
        self._targets = tuple(targets)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(target.name for target in self._targets)

    def item_targets(self, raw: Mapping[str, Any]) -> dict[str, str | None]:
        """Item key to the identity of what it targets; ``None`` when unidentifiable."""
        found: dict[str, str | None] = {}
        for target in self._targets:
            if target.mode == "per_item":
                values = raw.get(target.name, [])
                if not isinstance(values, list):
                    continue
                for index, value in enumerate(values):
                    found[item_key(target.name, index)] = target.identity(value)
            elif any(name in raw for name in target.fields):
                found[target.name] = target.identity(raw)
        return found
