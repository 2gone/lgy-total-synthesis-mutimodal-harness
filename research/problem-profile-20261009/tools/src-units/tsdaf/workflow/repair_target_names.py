"""Request-local mechanical correspondence for Reading's repair references.

This is naming data, not evidence that a repair was accepted. A history consumer
must check it against the task baseline and prior accepted command projections.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from tsdaf.lanes.literature.model_names import UnknownReference
from tsdaf.workflow.literature_fusion_actions import bind_fact_change_names


@dataclass(frozen=True)
class RepairTargetNames:
    groups: Mapping[str, Mapping[str, str]]

    @classmethod
    def of(cls, groups: Mapping[str, Mapping[str, str]]) -> RepairTargetNames:
        if not isinstance(groups, Mapping) or any(
            not isinstance(gid, str) or not gid
            or not isinstance(facts, Mapping)
            or any(not isinstance(fid, str) or not fid
                   or not isinstance(pid, str) or not pid for fid, pid in facts.items())
            for gid, facts in groups.items()
        ):
            raise ValueError("invalid repair target names")
        # The two namespaces must not give the same token conflicting meanings.
        for facts in groups.values():
            for fid, pid in facts.items():
                if pid in facts and pid != fid:
                    raise ValueError("colliding repair target namespaces")
        return cls(MappingProxyType({
            gid: MappingProxyType(dict(facts)) for gid, facts in groups.items()
        }))

    def component(self) -> dict[str, Any]:
        return {"version": 1, "groups": {gid: dict(facts) for gid, facts in self.groups.items()}}

    @classmethod
    def from_component(cls, value: Any) -> RepairTargetNames:
        if not isinstance(value, dict) or set(value) != {"version", "groups"}:
            raise ValueError("invalid repair target names component")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("unsupported repair target names version")
        return cls.of(value["groups"])

    def _target(self, gid: str, token: str, field: str, *, strict: bool) -> str:
        facts = self.groups.get(gid)
        candidates = tuple(fid for fid, pid in (facts or {}).items() if pid == token)
        if len(candidates) == 1:
            return candidates[0]
        if strict and len(candidates) > 1:
            raise UnknownReference(field, token, candidates,
                                   "This public object contains several facts; select the specific group fact.")
        if strict and facts is None:
            raise UnknownReference("route_group_id", gid, tuple(self.groups),
                                   "Select an exact supplied repair group.")
        if strict and not candidates and any(token in group.values() for group in self.groups.values()):
            raise UnknownReference(field, token, tuple(facts or ()),
                                   "This public object does not belong to the selected repair group.")
        # Concrete group facts and new local aliases keep the existing Fusion binder.
        return token

    def resolve(self, value: Any, *, strict: bool = False) -> Any:
        """Bind repair-only slots; malformed values remain for the declared DTO check.

        Request decoding is non-strict so one ambiguous item cannot reject sibling
        writes. Its per-item validator uses strict=True before accepting a command.
        """
        return self._walk(value, strict=strict, present=False)

    def present(self, value: Any) -> Any:
        return self._walk(value, strict=False, present=True)

    def _walk(self, value: Any, *, strict: bool, present: bool) -> Any:
        if isinstance(value, list):
            return [self._walk(item, strict=strict, present=present) for item in value]
        if not isinstance(value, Mapping):
            return value
        if present and value.get("type") == "function_call":
            return value  # The model's original call is shown exactly as authored.
        gid, repair = value.get("route_group_id"), value.get("repair")
        if isinstance(gid, str) and isinstance(repair, Mapping):
            result = deepcopy(dict(value))
            facts = self.groups.get(gid, {})

            def convert(token: str, field: str) -> str:
                if not present:
                    return self._target(gid, token, field, strict=strict)
                pid = facts.get(token)
                # A collapsed public object is not a uniquely writable fact name.
                return pid if pid is not None and sum(p == pid for p in facts.values()) == 1 else token

            changes = result["repair"].get("changes")
            for change in changes if isinstance(changes, list) else ():
                if isinstance(change, dict):
                    bind_fact_change_names(
                        change, subject=lambda token: convert(token, "fact_id"),
                        reference=lambda token: convert(token, "repair reference"),
                    )
            return result
        return {key: self._walk(item, strict=strict, present=present) for key, item in value.items()}
