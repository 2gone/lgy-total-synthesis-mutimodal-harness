"""Host-private, content-addressed evidence for a model request's naming view."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.model_names import NAMING_VERSION, Names, UnknownReference, minted
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.fusion_names import FusionNames
from tsdaf.workflow.literature_fusion_actions import binder_from
from tsdaf.workflow.repair_target_names import RepairTargetNames
from tsdaf.workflow.state_resource_names import ReadVersionNames, StateResourceNames

NAMING_SNAPSHOT_MEDIA = "application/vnd.tsdaf.task-names+json"
NAMING_SCHEMA_VERSION = "tsdaf.task-names.v1"


def names_component(table: Names) -> dict[str, Any]:
    return {
        "names": dict(table.names),
        "families": dict(table.families),
        "field_families": {key: sorted(value) for key, value in table.field_families.items()},
        "reserved": sorted(table.reserved),
    }


def names_from_component(value: Mapping[str, Any]) -> Names:
    return Names.of(
        value["names"],
        value["families"],
        field_families={
            key: frozenset(families) for key, families in value["field_families"].items()
        },
        reserved=value.get("reserved"),
    )


@dataclass(frozen=True)
class DomainNaming:
    present: Callable[[Any], Any]
    resolve: Callable[[Any], Any]
    components: dict[str, Any]


def fusion_domain(names: FusionNames, *, prefix: str, bound: set[str]) -> DomainNaming:
    return DomainNaming(
        names.present,
        names.resolve,
        {
            "kind": "fusion",
            "table": names_component(names.table),
            "tag_pattern": names.tag.pattern,
            "prefix": prefix,
            "bound": sorted(bound),
            "digests": dict(names.digests),
        },
    )


def restore_fusion_domain(value: dict[str, Any]) -> DomainNaming:
    import re

    if value.get("kind") != "fusion":
        raise ValueError("not a fusion naming view")
    prefix, bound = value["prefix"], set(value["bound"])
    names = FusionNames(
        names_from_component(value["table"]),
        re.compile(value["tag_pattern"]),
        binder_from(prefix, bound),
        value["digests"],
    )
    return fusion_domain(names, prefix=prefix, bound=bound)


@dataclass(frozen=True)
class FrozenNaming:
    """Exactly one table for presenting a request and interpreting its response."""

    handles: Names
    domain: DomainNaming
    state: StateResourceNames
    reads: ReadVersionNames

    def present(self, value: Any) -> Any:
        return self.reads.present(
            self.domain.present(self.state.present(self.handles.present(value)))
        )

    def resolve(self, value: Any) -> Any:
        return self.domain.resolve(self.handles.resolve(self.state.resolve(value)))

    def diagnose_reference(self, error: UnknownReference) -> dict[str, Any]:
        """Render the original validator's allowed identities through this request's table."""
        names = sorted(
            {
                shown
                for identity in error.allowed
                if isinstance((shown := self.present(identity)), str) and not minted(shown)
            }
        )
        shown_value = self.present(error.value)
        return {
            "message": str(error),
            "field": error.field,
            "value": shown_value
            if isinstance(shown_value, str) and not minted(shown_value)
            else "unknown name",
            "available_names": names[:8],
            "more_available": max(0, len(names) - 8),
            "catalog_location": "state:catalog" if len(names) > 8 else None,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": NAMING_SCHEMA_VERSION,
            "naming_version": NAMING_VERSION,
            "components": {
                "handles": names_component(self.handles),
                "state": {
                    "roots": dict(self.state.roots),
                    "result_by_ref": dict(self.state.result_by_ref),
                },
                "read_versions": dict(self.reads.versions),
                "domain": self.domain.components,
            },
        }

    @classmethod
    def from_snapshot(
        cls, snapshot: dict[str, Any], restore_domain: Callable[[dict[str, Any]], DomainNaming]
    ) -> FrozenNaming:
        parts = snapshot["components"]
        state = parts["state"]
        return cls(
            names_from_component(parts["handles"]),
            restore_domain(parts["domain"]),
            StateResourceNames(state["roots"], state["result_by_ref"]),
            ReadVersionNames(parts["read_versions"]),
        )


def read_naming_snapshot(
    read_bytes: Callable[[ArtifactRef], bytes], ref: ArtifactRef
) -> dict[str, Any]:
    """Read an already-authorized snapshot and verify its exact media and schema."""
    if ref.media_type != NAMING_SNAPSHOT_MEDIA:
        raise ValueError("not a task-names snapshot")
    value = json.loads(read_artifact(read_bytes, ref))
    if not isinstance(value, dict) or value.get("schema_version") != NAMING_SCHEMA_VERSION:
        raise ValueError("unknown task-names snapshot schema")
    if not isinstance(value.get("naming_version"), str) or not isinstance(
        value.get("components"), dict
    ):
        raise ValueError("invalid task-names snapshot")
    _validate_components(value["components"])
    return value


def _str_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _names_table(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    names, families, fields, reserved = (
        value.get("names"),
        value.get("families"),
        value.get("field_families"),
        value.get("reserved"),
    )
    if not (
        isinstance(names, dict)
        and isinstance(families, dict)
        and isinstance(fields, dict)
        and isinstance(reserved, list)
    ):
        return False
    valid = (
        _str_map(names)
        and _str_map(families)
        and set(families) == set(names)
        and isinstance(fields, dict)
        and all(
            isinstance(key, str)
            and isinstance(options, list)
            and all(isinstance(option, str) for option in options)
            for key, options in fields.items()
        )
        and isinstance(reserved, list)
        and all(isinstance(name, str) for name in reserved)
        and set(names.values()) <= set(reserved)
    )
    if not valid:
        return False
    try:
        names_from_component(value)
    except ValueError:
        return False
    return True


def _validate_components(parts: dict[str, Any]) -> None:
    state, domain = parts.get("state"), parts.get("domain")
    if (
        not _names_table(parts.get("handles"))
        or not isinstance(state, Mapping)
        or not _str_map(state.get("roots"))
        or not _str_map(state.get("result_by_ref"))
        or not _str_map(parts.get("read_versions"))
        or not isinstance(domain, Mapping)
    ):
        raise ValueError("invalid task-names components")
    StateResourceNames(state["roots"], state["result_by_ref"])
    kind = domain.get("kind")
    if kind == "fusion":
        valid = (
            _names_table(domain.get("table"))
            and isinstance(domain.get("tag_pattern"), str)
            and isinstance(domain.get("prefix"), str)
            and isinstance(domain.get("bound"), list)
            and all(isinstance(item, str) for item in domain["bound"])
            and _str_map(domain.get("digests"))
        )
    elif kind == "investigation":
        valid = all(
            _names_table(domain.get(key))
            for key in ("task", "records_locations", "records_versions", "groups_units")
        )
        valid = valid and (
            domain.get("groups_tag_pattern") is None
            or isinstance(domain.get("groups_tag_pattern"), str)
        )
        binders = domain.get("groups_binders")
        valid = (
            valid
            and isinstance(binders, dict)
            and all(
                isinstance(unit, str)
                and isinstance(entry, Mapping)
                and isinstance(entry.get("prefix"), str)
                and isinstance(entry.get("bound"), list)
                and all(isinstance(item, str) for item in entry["bound"])
                for unit, entry in binders.items()
            )
        )
        owners = domain.get("owner_by_location")
        valid = (
            valid
            and isinstance(owners, dict)
            and all(
                isinstance(location, str) and (owner is None or isinstance(owner, str))
                for location, owner in owners.items()
            )
        )
        if "repair_targets" in domain:
            RepairTargetNames.from_component(domain["repair_targets"])
    elif kind == "identity":
        valid = domain == {"kind": "identity"}
    else:
        valid = False
    if not valid:
        raise ValueError("invalid task-names domain")
    pattern = domain.get("tag_pattern") if kind == "fusion" else domain.get("groups_tag_pattern")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError("invalid task-names domain pattern") from error


def verify_accepted_turn_naming(
    read_bytes: Callable[[ArtifactRef], bytes],
    turn: Mapping[str, Any],
    accepted_refs: Sequence[ArtifactRef],
    *,
    expected_naming_version: str | None = None,
    expected_binding: Mapping[str, Any] | None = None,
    expected_iteration: int | None = None,
) -> dict[str, Any] | None:
    """Verify one interaction's exact intent and naming table in its owner closure.

    A genuinely old turn with neither side declaring a table returns None. A missing,
    conflicting, or malformed declaration never falls back to an older table.
    """
    from tsdaf.workflow.tool_turns import TOOL_REQUEST_INTENT_MEDIA

    closure = set(accepted_refs)
    if expected_binding is not None and turn.get("binding") != expected_binding:
        raise ValueError("turn belongs to a different task binding")
    if expected_iteration is not None and turn.get("iteration") != expected_iteration:
        raise ValueError("turn belongs to a different iteration")
    interaction = turn.get("interaction_ref")
    if interaction is not None:
        interaction_ref = ArtifactRef.model_validate(interaction)
        if interaction_ref not in closure:
            raise ValueError("turn interaction is outside its accepted artifact closure")
        sealed = json.loads(read_artifact(read_bytes, interaction_ref))
        if any(
            sealed.get(key) != turn.get(key)
            for key in ("binding", "iteration", "request_ref", "response_ref", "naming_ref")
        ):
            raise ValueError("turn differs from its accepted interaction")
    request = ArtifactRef.model_validate(turn["request_ref"])
    response = ArtifactRef.model_validate(turn["response_ref"])
    if request not in closure or response not in closure:
        raise ValueError("turn request or response is outside its accepted artifact closure")
    read_artifact(read_bytes, request)
    read_artifact(read_bytes, response)
    intents = []
    for ref in closure:
        if ref.media_type != TOOL_REQUEST_INTENT_MEDIA:
            continue
        intent = json.loads(read_artifact(read_bytes, ref))
        if intent.get("binding") == turn.get("binding") and intent.get("iteration") == turn.get(
            "iteration"
        ):
            intents.append(intent)
    if len(intents) > 1:
        raise ValueError("competing request intents claim the same tool turn")
    intent = intents[0] if intents else None
    if intent is not None and intent.get("request_ref") != request.model_dump(mode="json"):
        raise ValueError("turn request differs from its accepted intent")
    named = turn.get("naming_ref")
    intended = intent.get("naming_ref") if intent is not None else None
    if "naming_ref" not in turn and (intent is None or "naming_ref" not in intent):
        return None
    if not isinstance(named, Mapping) or not isinstance(intended, Mapping) or named != intended:
        raise ValueError("request and response naming snapshots differ")
    ref = ArtifactRef.model_validate(named)
    if ref not in closure:
        raise ValueError("naming snapshot is outside its accepted artifact closure")
    snapshot = read_naming_snapshot(read_bytes, ref)
    if (
        expected_naming_version is not None
        and snapshot["naming_version"] != expected_naming_version
    ):
        raise ValueError("naming snapshot belongs to a different execution version")
    return snapshot
