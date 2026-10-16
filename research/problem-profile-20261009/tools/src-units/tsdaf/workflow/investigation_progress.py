"""Accepted deltas for a source investigation's content and shared tool cursor."""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import Field

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.tool_turns import ToolTurnState

INVESTIGATION_PROGRESS_MEDIA = "application/vnd.tsdaf.investigation-checkpoint+json"


class InvestigationCheckpoint(ContractModel):
    schema_version: Literal["tsdaf.investigation-checkpoint.v2"] = (
        "tsdaf.investigation-checkpoint.v2"
    )
    execution_id: str
    input_ref: ArtifactRef
    resources_sha256: str
    previous_ref: ArtifactRef | None = None
    answer_ref: ArtifactRef | None = None
    history_added: tuple[dict[str, Any], ...] = ()
    selections_added: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resource_locations_added: dict[str, dict[str, Any]] = Field(default_factory=dict)
    observations_added: tuple[dict[str, Any], ...] = ()
    read_errors_added: tuple[dict[str, Any], ...] = ()
    request_refs_added: tuple[ArtifactRef, ...] = ()
    audit_refs_added: tuple[ArtifactRef, ...] = ()
    outbox_added: tuple[dict[str, Any], ...] = ()
    pending_ref: ArtifactRef | None = None
    next_call: int = Field(default=0, ge=0)
    iteration: int = Field(default=0, ge=0)
    finished: bool = False
    # The delivered set only grows. A checkpoint records the selections it added to it;
    # restoring takes the union over the chain. Checkpoints before 2026-09-24 wrote the
    # whole set each time (66 of a2's 67 repeated the one before), and the union reads
    # them unchanged.
    delivered_selections: tuple[str, ...] = ()
    delivered_added: tuple[str, ...] = ()


@dataclass
class RestoredInvestigation:
    loop: ToolTurnState = field(default_factory=ToolTurnState)
    ref: ArtifactRef | None = None
    answer_ref: ArtifactRef | None = None
    selections: dict[str, dict[str, Any]] = field(default_factory=dict)
    resource_locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    requests: list[ArtifactRef] = field(default_factory=list)
    audits: list[ArtifactRef] = field(default_factory=list)
    delivered: set[str] = field(default_factory=set)


def restore_investigation(
    refs: tuple[ArtifactRef, ...],
    *,
    execution_id: str,
    input_ref: ArtifactRef,
    resources_sha256: str,
    store: PrivateArtifactStore,
) -> RestoredInvestigation:
    state = RestoredInvestigation()
    verified: set[ArtifactRef] = set()
    for ref in refs:
        if ref.media_type != INVESTIGATION_PROGRESS_MEDIA or ref == state.ref:
            continue
        record = InvestigationCheckpoint.model_validate_json(read_artifact(store.read_bytes, ref))
        if (record.execution_id, record.input_ref, record.resources_sha256) != (
            execution_id,
            input_ref,
            resources_sha256,
        ):
            raise ValueError("investigation checkpoint belongs to another task or resource version")
        if record.previous_ref != state.ref:
            raise ValueError("investigation checkpoint lacks its accepted predecessor")
        for artifact in (
            *record.request_refs_added,
            *record.audit_refs_added,
            *((record.answer_ref,) if record.answer_ref else ()),
        ):
            if artifact not in verified:
                read_artifact(store.read_bytes, artifact)
                verified.add(artifact)
        state.audits.extend(record.audit_refs_added)
        if record.pending_ref is not None and record.pending_ref not in state.audits:
            raise ValueError("pending investigation response lacks an accepted audit binding")
        if record.pending_ref is None:
            if record.outbox_added:
                raise ValueError("queued content requires a pending investigation response")
            state.loop.outbox.clear()
        state.loop.outbox.extend(record.outbox_added)
        state.loop.history.extend(record.history_added)
        for name, binding in record.selections_added.items():
            if name in state.selections and state.selections[name] != binding:
                raise ValueError("a saved source selection cannot be rebound")
            state.selections[name] = binding
        for name, binding in record.resource_locations_added.items():
            if name in state.resource_locations and state.resource_locations[name] != binding:
                raise ValueError("a saved resource location cannot be rebound")
            for field_name in ("artifact_ref", "root_ref"):
                artifact = ArtifactRef.model_validate(binding[field_name])
                read_artifact(store.read_bytes, artifact)
            state.resource_locations[name] = binding
        state.observations.extend(record.observations_added)
        state.errors.extend(record.read_errors_added)
        state.requests.extend(record.request_refs_added)
        state.loop.pending = (
            {
                **json.loads(read_artifact(store.read_bytes, record.pending_ref)),
                "interaction_ref": record.pending_ref.model_dump(mode="json"),
            }
            if record.pending_ref
            else None
        )
        state.loop.next_call, state.loop.iteration = record.next_call, record.iteration
        state.loop.finished = record.finished
        state.delivered |= set(record.delivered_selections) | set(record.delivered_added)
        if not state.delivered <= state.selections.keys():
            raise ValueError("delivered selection is absent from accepted source bindings")
        state.ref, state.answer_ref = ref, record.answer_ref
    return state
