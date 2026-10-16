"""Nonblocking review annotations of accepted values; no scientific inference."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from tsdaf.contracts.literature.schema import machine_profile


def append_review_markers(
    machine: dict[str, Any],
    markers: Iterable[Mapping[str, Any]],
    *,
    target_type: str,
    target_id: str,
    field_key: str,
) -> None:
    """Called only after a write succeeds, naming its actual public home."""
    if not machine_profile(machine).wrapping:
        return
    for marker in markers:
        value = deepcopy(dict(marker))
        value.update(
            target_object_type=target_type, target_object_id=target_id, field_key=field_key
        )
        machine["domain_payload"].setdefault("review_markers", []).append(value)
    deduplicate_review_markers(machine)


def deduplicate_review_markers(machine: dict[str, Any]) -> None:
    """Also run after evidence remapping: distinct sealed refs can become one ref."""
    domain = machine["domain_payload"]
    if "review_markers" not in domain:
        return
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for marker in domain["review_markers"]:
        key = tuple(
            marker.get(k)
            for k in (
                "target_object_type",
                "target_object_id",
                "field_key",
                "material_index",
                "reason",
            )
        )
        merged = unique.setdefault(key, {**marker, "evidence_refs": []})
        for ref in marker["evidence_refs"]:
            if ref not in merged["evidence_refs"]:
                merged["evidence_refs"].append(ref)
    domain["review_markers"] = list(unique.values())
