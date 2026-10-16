"""The sealed task input alone chooses Reading repair semantics."""

from collections.abc import Mapping
from typing import Any, Literal

SNAPSHOT = "SNAPSHOT_V1"
SPARSE = "SPARSE_V1"


def reading_repair_mode(task_input: Mapping[str, Any]) -> Literal["SNAPSHOT_V1", "SPARSE_V1"]:
    value = task_input.get("reading_repair_mode", SNAPSHOT)
    if value not in (SNAPSHOT, SPARSE):
        raise ValueError("unknown sealed Reading repair mode")
    return value
