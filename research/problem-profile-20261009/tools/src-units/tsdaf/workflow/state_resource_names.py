"""Readable, reversible names for registered versions of shared state resources."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.workflow.state_resources import (
    STATE_LOCATION_SUBMISSION,
    STATE_LOCATIONS,
    StateResource,
    split_state_location,
)


@dataclass(frozen=True)
class StateResourceNames:
    roots: Mapping[str, str]
    result_by_ref: Mapping[str, str]

    def __post_init__(self) -> None:
        if len(set(self.roots.values())) != len(self.roots):
            raise ValueError("two state roots share one readable name")

    @classmethod
    def registered(
        cls,
        current: Mapping[str, Any],
        historical: Mapping[str, Any],
        *,
        previous: Mapping[str, str] | None = None,
    ) -> StateResourceNames:
        names = dict(previous or {})
        for alias in (*STATE_LOCATIONS, STATE_LOCATION_SUBMISSION):
            occupied = {name for name in names.values() if name.startswith(alias + " version ")}
            for location in (*historical, *current):
                parts = split_state_location(location)
                if parts is None or parts.alias != alias or not parts.digest or parts.pointer:
                    continue
                if location in names:
                    continue
                entry = current.get(location) or historical.get(location)
                if not isinstance(entry, (StateResource, dict)):
                    continue
                number = 1
                while f"{alias} version {number}" in occupied:
                    number += 1
                name = f"{alias} version {number}"
                names[location] = name
                occupied.add(name)
        result_by_ref: dict[str, str] = {}
        for location, binding in historical.items():
            if (
                not isinstance(binding, Mapping)
                or binding.get("kind") not in {"CURRENT_RESULT", "HISTORICAL_RESULT"}
                or "fact_id" in binding
            ):
                continue
            ref = binding.get("result_ref")
            if not isinstance(ref, Mapping) or not isinstance(ref.get("sha256"), str):
                continue
            result_by_ref.setdefault(ref["sha256"], location)
        return cls(names, result_by_ref)

    def resolve(self, value: Any) -> Any:
        inverse = {name: root for root, name in self.roots.items()}

        def walk(node: Any) -> Any:
            if isinstance(node, list):
                return [walk(item) for item in node]
            if not isinstance(node, Mapping):
                return node
            result = {key: walk(item) for key, item in node.items()}
            location = result.get("location")
            if isinstance(location, str):
                root, mark, pointer = location.partition("#")
                if root in inverse:
                    result["location"] = inverse[root] + (mark + pointer if mark else "")
            return result

        return walk(value)

    def present(self, value: Any) -> Any:
        if not self.roots:
            return value
        roots = re.compile(
            "|".join(re.escape(root) for root in sorted(self.roots, key=len, reverse=True))
        )

        def named_locations(text: str) -> str:
            return roots.sub(lambda match: self.roots[match.group(0)], text)

        def walk(node: Any) -> Any:
            if isinstance(node, list):
                return [walk(item) for item in node]
            if not isinstance(node, Mapping):
                return node
            location = node.get("location")
            state = split_state_location(location) if isinstance(location, str) else None
            result: dict[str, Any] = {}
            for key, item in node.items():
                if key == "value" and state is not None and state.alias == "state:snapshot":
                    if isinstance(item, Mapping):
                        item = {k: v for k, v in item.items() if k != "result_ref"}
                        ref = node["value"].get("result_ref")
                        if isinstance(ref, Mapping):
                            token = self.result_by_ref.get(str(ref.get("sha256")))
                            if token is not None:
                                item["result_location"] = token
                if (
                    key in {"version", "content_version"}
                    and state is not None
                    and isinstance(item, str)
                    and (named := self.roots.get(f"{state.alias}@{item}")) is not None
                ):
                    result[key] = named
                elif isinstance(item, str) and key in {
                    "location",
                    "target",
                    "summary",
                    "snapshot",
                    "history",
                    "catalog",
                    "writes",
                }:
                    result[key] = named_locations(item)
                else:
                    result[key] = walk(item)
            return result

        def step(node: Any) -> Any:
            if not isinstance(node, Mapping) or node.get("type") not in {
                "function_result",
                "user_input",
            }:
                return walk(node)
            result = dict(node)
            part_key = "result" if node["type"] == "function_result" else "content"
            parts = []
            for part in node.get(part_key, []):
                if not isinstance(part, Mapping) or part.get("type") != "text":
                    parts.append(part)
                    continue
                raw = part.get("text")
                if not isinstance(raw, str) or not raw.startswith(("{", "[")):
                    parts.append(part)
                    continue
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parts.append(part)
                    continue
                projected = walk(parsed)
                parts.append(
                    {**part, "text": canonical_json_bytes(projected).decode()}
                    if projected != parsed
                    else part
                )
            result[part_key] = parts
            return result

        return [step(item) for item in value] if isinstance(value, list) else step(value)


@dataclass(frozen=True)
class ReadVersionNames:
    """Name only versions proved by actual read receipts, in their first-seen order."""

    versions: Mapping[str, str]

    @classmethod
    def registered(
        cls, history: Any, *, previous: Mapping[str, str] | None = None
    ) -> ReadVersionNames:
        names = dict(previous or {})
        used = set(names.values())

        def scan(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    scan(item)
            elif isinstance(node, Mapping):
                location = node.get("location")
                if (
                    node.get("outcome") in {"READ", "IN_CONTEXT"}
                    and isinstance(location, str)
                    and isinstance(node.get("origin"), str)
                    and isinstance(node.get("provided_to"), str)
                ):
                    for key in ("version", "content_version"):
                        digest = node.get(key)
                        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                            continue
                        if digest not in names:
                            index = 1
                            while f"{location} version {index}" in used:
                                index += 1
                            label = f"{location} version {index}"
                            names[digest] = label
                            used.add(label)
                for item in node.values():
                    scan(item)
            elif isinstance(node, str) and node.startswith(("{", "[")):
                try:
                    scan(json.loads(node))
                except ValueError:
                    pass

        if isinstance(history, list):
            for step in history:
                if isinstance(step, Mapping) and step.get("type") == "function_result":
                    scan(step.get("result", ()))
        return cls(names)

    def present(self, value: Any) -> Any:
        if not self.versions:
            return value

        def walk(node: Any) -> Any:
            if isinstance(node, list):
                return [walk(item) for item in node]
            if isinstance(node, Mapping):
                receipt = (
                    isinstance(node.get("location"), str)
                    and node.get("outcome") in {"READ", "IN_CONTEXT", "DIRECTORY"}
                    and isinstance(node.get("origin"), str)
                    and isinstance(node.get("provided_to"), str)
                )
                action = isinstance(node.get("action_id"), str) and node.get("tool") == "read"
                return {
                    key: self.versions[item]
                    if (receipt or action)
                    and key in {"version", "content_version"}
                    and isinstance(item, str)
                    and item in self.versions
                    else walk(item)
                    for key, item in node.items()
                }
            return node

        if not isinstance(value, list):
            return value
        projected = []
        for step in value:
            if not isinstance(step, Mapping) or step.get("type") not in {
                "function_result",
                "user_input",
            }:
                projected.append(step)
                continue
            part_key = "result" if step["type"] == "function_result" else "content"
            parts = []
            for part in step.get(part_key, []):
                if not isinstance(part, Mapping) or part.get("type") != "text":
                    parts.append(part)
                    continue
                raw = part.get("text")
                if not isinstance(raw, str) or not raw.startswith(("{", "[")):
                    parts.append(part)
                    continue
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parts.append(part)
                    continue
                named = walk(parsed)
                parts.append(
                    {**part, "text": canonical_json_bytes(named).decode()}
                    if named != parsed
                    else part
                )
            projected.append({**step, part_key: parts})
        return projected
