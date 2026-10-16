"""Derived read-only workflow execution state resources for task isolation and pagination.

Exposes bounded, deterministic snapshots of workflow execution state (snapshot summary,
action progress history, task catalog, and pending writes) as read-only resources under
the 'state:' namespace with immutable content-addressed versions ('state:<kind>@<sha256>').
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_read import DirectorySelection, Selection
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.workflow.action_progress import ACTION_PROGRESS_ORIGIN, ActionProgress
from tsdaf.workflow.resource_reads import (
    READ_BYTES,
    ReadFailure,
    ReadPlan,
    ReadValue,
    fragment_binding,
    pointer_value,
    project_source_directory,
    record_value,
    resource_binding,
    resource_receipt,
)
from tsdaf.workflow.write_delivery import WriteDelivery

STATE_RESOURCE_ORIGIN = ACTION_PROGRESS_ORIGIN
STATE_RESOURCE_MEDIA_TYPE = "application/vnd.tsdaf.readable-record+json"

STATE_LOCATION_SNAPSHOT = "state:snapshot"
STATE_LOCATION_HISTORY = "state:history"
STATE_LOCATION_CATALOG = "state:catalog"
STATE_LOCATION_WRITES = "state:writes"
# Immutable submitted inputs have no current-turn alias: they are not a snapshot
# of current saved facts or a version of the action-history summary.
STATE_LOCATION_SUBMISSION = "state:submission"

STATE_LOCATIONS = (
    STATE_LOCATION_SNAPSHOT,
    STATE_LOCATION_HISTORY,
    STATE_LOCATION_CATALOG,
    STATE_LOCATION_WRITES,
)
STATE_PREFIX = "state:"


@dataclass(frozen=True)
class StateLocation:
    """The parts of ``state:<alias>[@<digest>][#<pointer>]``, split in one place."""

    alias: str
    digest: str
    pointer: str

    @property
    def root(self) -> str:
        return self.alias + ("@" + self.digest if self.digest else "")


def split_state_location(location: str) -> StateLocation | None:
    """Split a state location into alias, pinned digest and pointer; None if not one."""
    if not location.startswith(STATE_PREFIX):
        return None
    root, _, pointer = location.partition("#")
    alias, _, digest = root.partition("@")
    return StateLocation(alias, digest, pointer)


def _make_artifact_ref(payload: Any, name: str) -> dict[str, Any]:
    """Derive an immutable ArtifactRef descriptor from canonical JSON bytes."""
    raw_bytes = canonical_json_bytes(payload)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    ref = ArtifactRef(
        sha256=digest,
        media_type=STATE_RESOURCE_MEDIA_TYPE,
        byte_size=len(raw_bytes),
        relative_path=f"objects/sha256/{digest[:2]}/{digest}",
    )
    return ref.model_dump(mode="json")


@dataclass(frozen=True)
class StateResource:
    """A registered state resource with its payload, binding metadata, and entry item key."""

    location: str
    value: Any
    binding: dict[str, Any]
    item_key: str | None = None

    def __iter__(self) -> Iterator[Any]:
        """Support 2-tuple unpacking as (value, binding) for backward compatibility."""
        return iter((self.value, self.binding))


def build_state_resources(
    *,
    task_id: str,
    history: Sequence[dict[str, Any]] | ActionProgress = (),
    write_delivery: WriteDelivery | None = None,
    result_ref: ArtifactRef | dict[str, Any] | str | None = None,
    catalog_data: dict[str, Any] | list[Any] | None = None,
    catalog_origin: str = STATE_RESOURCE_ORIGIN,
    owner_task: str | None = None,
    store: PrivateArtifactStore | None = None,
    feedback_view: Callable[[Any], Any] = lambda value: value,
) -> dict[str, StateResource]:
    """Derive the standard read-only state views and their bindings for a task.

    Returns a mapping containing both current-turn aliases ('state:<kind>') and
    immutable content-addressed versioned locations ('state:<kind>@<sha256>').
    If a store is provided, state resource payloads are persisted to the store immediately.
    """
    if catalog_origin != STATE_RESOURCE_ORIGIN:
        raise ValueError(
            f"catalog_origin must be '{STATE_RESOURCE_ORIGIN}', got '{catalog_origin}'. "
            "State resources cannot be assigned scientific evidence origin."
        )

    if isinstance(catalog_data, dict):
        custom_origin = catalog_data.get("origin")
        if custom_origin is not None and custom_origin != STATE_RESOURCE_ORIGIN:
            raise ValueError(
                f"catalog_data origin cannot be '{custom_origin}', "
                f"must be '{STATE_RESOURCE_ORIGIN}'. "
                "State resources cannot be assigned scientific evidence origin."
            )

    owner = owner_task if owner_task is not None else task_id

    # 1. History
    if isinstance(history, ActionProgress):
        history_records = [record.as_dict() for record in history.records]
    else:
        history_records = [record.as_dict() for record in ActionProgress(history).records]
    history_records = copy.deepcopy(history_records)
    # Project complete feedback before field/path selection. The original accepted
    # transcript remains the authority; these are independently sealed read views.
    for row in history_records:
        if "feedback" in row:
            row["feedback"] = feedback_view(row["feedback"])
    if store is not None:
        raw_hist = canonical_json_bytes(history_records)
        hist_art_ref = store.put_bytes(raw_hist, media_type=STATE_RESOURCE_MEDIA_TYPE)
        hist_ref_dict = hist_art_ref.model_dump(mode="json")
    else:
        hist_ref_dict = _make_artifact_ref(history_records, "history")
    hist_digest = hist_ref_dict["sha256"]
    hist_versioned_location = f"{STATE_LOCATION_HISTORY}@{hist_digest}"
    history_binding = resource_binding(
        hist_ref_dict,
        origin=STATE_RESOURCE_ORIGIN,
        provided_to=task_id,
        owner_task=owner,
    )
    history_res = StateResource(
        location=hist_versioned_location,
        value=history_records,
        binding=history_binding,
        item_key="action_id",
    )

    # 2. Writes
    if write_delivery is not None:
        pending_writes = [
            {**copy.deepcopy(row), "origin": STATE_RESOURCE_ORIGIN}
            for row in write_delivery.pending()
        ]
    else:
        pending_writes = []
    if store is not None:
        raw_writes = canonical_json_bytes(pending_writes)
        writes_art_ref = store.put_bytes(raw_writes, media_type=STATE_RESOURCE_MEDIA_TYPE)
        writes_ref_dict = writes_art_ref.model_dump(mode="json")
    else:
        writes_ref_dict = _make_artifact_ref(pending_writes, "writes")
    writes_digest = writes_ref_dict["sha256"]
    writes_versioned_location = f"{STATE_LOCATION_WRITES}@{writes_digest}"
    writes_binding = resource_binding(
        writes_ref_dict,
        origin=STATE_RESOURCE_ORIGIN,
        provided_to=task_id,
        owner_task=owner,
    )
    writes_res = StateResource(
        location=writes_versioned_location,
        value=pending_writes,
        binding=writes_binding,
        item_key="action_id",
    )

    # 3. Catalog
    catalog_copy = copy.deepcopy(catalog_data)
    if catalog_copy is None:
        catalog_payload: dict[str, Any] | list[Any] = {
            "origin": STATE_RESOURCE_ORIGIN,
            "entries": [],
        }
    elif isinstance(catalog_copy, dict):
        catalog_payload = {
            **catalog_copy,
            "origin": STATE_RESOURCE_ORIGIN,
        }
    else:
        catalog_payload = catalog_copy
    if store is not None:
        raw_cat = canonical_json_bytes(catalog_payload)
        cat_art_ref = store.put_bytes(raw_cat, media_type=STATE_RESOURCE_MEDIA_TYPE)
        cat_ref_dict = cat_art_ref.model_dump(mode="json")
    else:
        cat_ref_dict = _make_artifact_ref(catalog_payload, "catalog")
    cat_digest = cat_ref_dict["sha256"]
    cat_versioned_location = f"{STATE_LOCATION_CATALOG}@{cat_digest}"
    catalog_binding = resource_binding(
        cat_ref_dict,
        origin=STATE_RESOURCE_ORIGIN,
        provided_to=task_id,
        owner_task=owner,
    )
    catalog_res = StateResource(
        location=cat_versioned_location,
        value=catalog_payload,
        binding=catalog_binding,
    )

    # 4. Snapshot summary
    result_ref_payload: dict[str, Any] | str | None
    if isinstance(result_ref, ArtifactRef):
        result_ref_payload = result_ref.model_dump(mode="json")
    else:
        result_ref_payload = copy.deepcopy(result_ref)

    snapshot_payload: dict[str, Any] = {
        "origin": STATE_RESOURCE_ORIGIN,
        "task_id": task_id,
        "result_ref": result_ref_payload,
        "total_actions": len(history_records),
        "pending_writes_count": len(pending_writes),
        "state_locations": {
            "snapshot": STATE_LOCATION_SNAPSHOT,
            "history": hist_versioned_location,
            "catalog": cat_versioned_location,
            "writes": writes_versioned_location,
        },
    }
    if store is not None:
        raw_snap = canonical_json_bytes(snapshot_payload)
        snap_art_ref = store.put_bytes(raw_snap, media_type=STATE_RESOURCE_MEDIA_TYPE)
        snap_ref_dict = snap_art_ref.model_dump(mode="json")
    else:
        snap_ref_dict = _make_artifact_ref(snapshot_payload, "snapshot")
    snap_digest = snap_ref_dict["sha256"]
    snap_versioned_location = f"{STATE_LOCATION_SNAPSHOT}@{snap_digest}"
    snapshot_binding = resource_binding(
        snap_ref_dict,
        origin=STATE_RESOURCE_ORIGIN,
        provided_to=task_id,
        owner_task=owner,
    )
    snapshot_res = StateResource(
        location=snap_versioned_location,
        value=snapshot_payload,
        binding=snapshot_binding,
    )

    return {
        STATE_LOCATION_SNAPSHOT: snapshot_res,
        STATE_LOCATION_HISTORY: history_res,
        STATE_LOCATION_CATALOG: catalog_res,
        STATE_LOCATION_WRITES: writes_res,
        snap_versioned_location: snapshot_res,
        hist_versioned_location: history_res,
        cat_versioned_location: catalog_res,
        writes_versioned_location: writes_res,
    }


def state_version(state_resources: Mapping[str, Any], location: str) -> str | None:
    """The version a read of ``location`` would carry now; None for what never changes.

    A state view's version is the digest in its content-addressed location, the same
    digest ``resource_receipt`` reports as ``version``. A location that names a digest is
    pinned to it; an alias follows the current turn's view; anything outside ``state:`` is
    immutable once read and has no version to move.
    """
    parts = split_state_location(location)
    if parts is None:
        return None
    if parts.digest:
        return parts.digest
    entry = state_resources.get(parts.alias)
    if isinstance(entry, StateResource):
        own = split_state_location(entry.location)
        return own.digest or None if own is not None else None
    return None


def persist_state_resources(
    state_resources: Mapping[str, StateResource | Any],
    store: PrivateArtifactStore,
    *,
    wrap_content: bool = False,
) -> dict[str, dict[str, Any]]:
    """Persist immutable versioned state resources to store.

    Returns a mapping of location -> binding (or wrapped CONTENT dict if wrap_content
    is True) suitable for registering directly into checkpoint incremental location
    tables (InvestigationCheckpoint.resource_locations_added or FusionCheckpoint.locations_added).

    Current-turn aliases ('state:history', etc.) are strictly excluded so checkpoints
    only record immutable, content-addressed locations.
    """
    persisted: dict[str, dict[str, Any]] = {}
    for location, entry in state_resources.items():
        parts = split_state_location(location)
        if parts is None or not parts.digest:
            continue

        if isinstance(entry, StateResource):
            value = entry.value
            binding = dict(entry.binding)
        elif isinstance(entry, tuple) and len(entry) >= 2:
            value = entry[0]
            binding = dict(entry[1])
        elif isinstance(entry, dict):
            if (
                "resource" in entry
                and isinstance(entry["resource"], dict)
                and "artifact_ref" in entry["resource"]
            ):
                binding = dict(entry["resource"])
            elif "artifact_ref" in entry:
                binding = dict(entry)
            else:
                continue
            value = None
        else:
            continue

        if value is not None:
            raw_bytes = canonical_json_bytes(value)
            ref = store.put_bytes(raw_bytes, media_type=STATE_RESOURCE_MEDIA_TYPE)
            binding["artifact_ref"] = ref.model_dump(mode="json")
            binding["root_ref"] = ref.model_dump(mode="json")

        if wrap_content:
            persisted[location] = {"kind": "CONTENT", "resource": binding}
        else:
            persisted[location] = binding

    return persisted


def catalog_recovery_reads(catalog: StateResource) -> list[dict[str, Any]]:
    """Existing authorized catalog pages, ready to read without walking host fields.

    Both document investigations and fusion use this recovery feedback. It is derived
    from the current sealed catalog, never from a guess about the refused location.
    """
    options = []
    value = catalog.value
    if isinstance(value, list):
        for index, document in enumerate(value):
            if not isinstance(document, dict) or document.get("active") is False:
                continue
            label = (
                " ".join(
                    str(document[key])
                    for key in ("source_phase", "attachment_id")
                    if document.get(key)
                )
                or f"Document {index + 1}"
            )
            for medium in ("text", "visuals"):
                if isinstance(document.get(medium), list) and document[medium]:
                    options.append(
                        {
                            "label": f"{label} {medium}",
                            "location": f"{catalog.location}#/{index}/{medium}",
                        }
                    )
    elif isinstance(value, dict) and isinstance(value.get("entries"), list):
        if value["entries"]:
            options.append(
                {"label": "Available content", "location": catalog.location + "#/entries"}
            )
    # The ordinary directory remains a bounded fallback, including unusual/empty catalogs.
    options = options[: DirectorySelection(kind="directory").limit]
    options.append({"label": "All available locations", "location": catalog.location})
    return [{**option, "select": {"kind": "directory"}} for option in options]


def resolve_state_read(
    location: str,
    selection: Selection | None = None,
    *,
    state_resources: Mapping[str, Any],
    current_task_id: str,
    store: PrivateArtifactStore | None = None,
    persisted_locations: Mapping[str, Any] | None = None,
) -> ReadPlan | None:
    """Resolve a read request against registered state resources with host task verification."""
    parts = split_state_location(location)
    if parts is None:
        return None

    root, pointer = parts.root, parts.pointer
    is_versioned = bool(parts.digest)

    # 1. Look up entry:
    # Aliases MUST come from active state_resources (current turn snapshot).
    # Versioned locations may come from active state_resources OR persisted checkpoint locations.
    if not is_versioned:
        if root not in state_resources:
            raise ReadFailure("UNAVAILABLE", f"Unknown state resource location: {root}")
        entry = state_resources[root]
    else:
        if root in state_resources:
            entry = state_resources[root]
        elif persisted_locations is not None and root in persisted_locations:
            entry = persisted_locations[root]
        else:
            raise ReadFailure("UNAVAILABLE", f"Unknown state resource location: {root}")

    # 2. Extract binding, in-memory value, and metadata
    if isinstance(entry, StateResource):
        value: Any = entry.value
        binding = entry.binding
        item_key = entry.item_key
        canonical_root = entry.location
    elif isinstance(entry, tuple) and len(entry) >= 2:
        value = entry[0]
        binding = entry[1]
        item_key = (
            "action_id"
            if root.startswith(STATE_LOCATION_HISTORY) or root.startswith(STATE_LOCATION_WRITES)
            else None
        )
        binding_ref = binding.get("artifact_ref") or binding.get("root_ref") or {}
        ref_sha = binding_ref.get("sha256")
        canonical_root = f"{root.partition('@')[0]}@{ref_sha}" if ref_sha else root
    elif isinstance(entry, dict):
        if (
            "resource" in entry
            and isinstance(entry["resource"], dict)
            and "artifact_ref" in entry["resource"]
        ):
            binding = entry["resource"]
        elif "artifact_ref" in entry:
            binding = entry
        else:
            raise ReadFailure("INVALID_RESOURCE", f"Malformed state resource entry for: {root}")
        value = None
        item_key = (
            "action_id"
            if root.startswith(STATE_LOCATION_HISTORY) or root.startswith(STATE_LOCATION_WRITES)
            else None
        )
        canonical_root = root
    else:
        raise ReadFailure("INVALID_RESOURCE", f"Malformed state resource entry for: {root}")

    # 3. Host validation: task isolation and anti-escalation
    authorized_to = binding.get("provided_to")
    if authorized_to is not None and authorized_to != current_task_id:
        raise ReadFailure(
            "UNAUTHORIZED",
            (
                f"State resource '{root}' is authorized for task '{authorized_to}', "
                f"not '{current_task_id}'."
            ),
        )

    # 4. Content-addressed digest verification for versioned locations
    if is_versioned:
        uri_digest = parts.digest
        binding_ref = binding.get("artifact_ref") or binding.get("root_ref") or {}
        expected_digest = binding_ref.get("sha256")
        if expected_digest is not None and uri_digest != expected_digest:
            raise ReadFailure(
                "UNAVAILABLE",
                (
                    f"State resource URI digest '{uri_digest}' does not match "
                    f"registered digest '{expected_digest}'."
                ),
            )

    # 5. Restore value from store if not already in memory
    if value is None:
        if store is None:
            raise ReadFailure(
                "UNAVAILABLE",
                f"Artifact store is required to read stored state resource: {root}",
            )
        binding_ref = binding.get("artifact_ref") or binding.get("root_ref")
        if not binding_ref:
            raise ReadFailure("INVALID_RESOURCE", f"State resource '{root}' has no artifact_ref.")
        ref = ArtifactRef.model_validate(binding_ref)
        if hasattr(store, "verify"):
            try:
                store.verify(ref)
            except Exception as err:
                raise ReadFailure(
                    "UNAVAILABLE",
                    f"Artifact verification failed for '{root}': {err}",
                ) from err
        try:
            raw_bytes = store.read_bytes(ref)
        except Exception as err:
            raise ReadFailure(
                "UNAVAILABLE",
                f"Failed to read artifact bytes for '{root}': {err}",
            ) from err
        computed_digest = hashlib.sha256(raw_bytes).hexdigest()
        if computed_digest != ref.sha256:
            raise ReadFailure(
                "UNAVAILABLE",
                (
                    f"Stored artifact digest mismatch for '{root}': "
                    f"expected {ref.sha256}, got {computed_digest}"
                ),
            )
        try:
            value = json.loads(raw_bytes.decode("utf-8"))
        except Exception as err:
            raise ReadFailure(
                "INVALID_RESOURCE",
                f"Corrupt JSON artifact for '{root}': {err}",
            ) from err

    def perform(max_bytes=READ_BYTES) -> ReadValue:
        target_value = value
        if pointer:
            target_value = pointer_value(target_value, pointer)

        actual_root = canonical_root if canonical_root else root
        actual_location = actual_root + ("#" + pointer if pointer else "")
        projected = record_value(
            target_value,
            selection,
            location=actual_location,
            item_key=item_key, max_bytes=max_bytes, part_bytes=READ_BYTES,
        )

        # A source catalog lists sources, not the host records describing them.
        # Keep generic record navigation for document roots and result/history rows.
        path = pointer.strip("/").split("/")
        source_page = (
            isinstance(value, list)
            and len(path) == 2
            and path[0].isdecimal()
            and path[1] in {"text", "visuals"}
        )
        if (
            actual_root.partition("@")[0] == STATE_LOCATION_CATALOG
            and projected["outcome"] == "DIRECTORY"
            and isinstance(target_value, list)
        ):
            project_source_directory(
                projected,
                target_value,
                readable_locations={
                    child["location"]
                    for child in target_value
                    if isinstance(child, dict)
                    and isinstance(child.get("location"), str)
                    and (
                        (source_page and child.get("selectors"))
                        or (pointer == "/entries" and child.get("kind") == "STANDARD_SOURCE")
                    )
                },
            )

        if projected["outcome"] == "READ":
            prefix_name = actual_root.partition("@")[0].replace(":", "_")
            selected_ref = _make_artifact_ref(projected["value"], prefix_name)
            selected_binding = fragment_binding(
                binding,
                selected_ref,
                pointer=pointer,
                selection=selection,
            )
        else:
            selected_binding = binding

        receipt = {
            **projected,
            **resource_receipt(selected_binding),
        }
        return ReadValue(receipt=receipt)

    return ReadPlan(perform, with_budget=perform)
