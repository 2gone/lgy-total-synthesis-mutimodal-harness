"""Shared turn protocol and action dispatch for tool-using consumers.

``tool_turns`` owns the recoverable cursor. This module owns what surrounds it:
sealing one request/response pair into an accepted interaction record, recovering
a returned response, routing each recorded action to the consumer's content
handlers, and recording the model-facing result. Consumers keep content processing,
checkpoint models and scientific validation in their own closures.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.execution import ProviderInteractionError
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_delivery import PARTIAL_DELIVERY_GUIDANCE, WriteControl
from tsdaf.contracts.tool_read import ReadEnvelope, ReadItem, Selection, read_batch
from tsdaf.contracts.tool_write import NamesNothing, WriteShapeError, item_diagnostic
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.model_names import NAMING_VERSION, Names, minted
from tsdaf.lanes.literature.provider_port import (
    AUTHORIZED_MODEL_PROFILE,
    LaneCompletedInteraction,
    LaneInteractionClient,
    LanePreparedInteraction,
    ProviderBinding,
)
from tsdaf.lanes.provider_attempts import seal_provider_attempts
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.action_progress import ActionProgress
from tsdaf.workflow.call_budget import CallBudget
from tsdaf.workflow.content_window import (
    DEFAULT_WINDOW_CHARS,
    DEFAULT_WINDOW_IMAGES,
    READ_EARLIER_KEY,
    EvidenceView,
    Identities,
    Versions,
    annotate_navigation_reply,
    content_window,
    no_source_evidence,
    no_versions,
)
from tsdaf.workflow.context_projection import project_context
from tsdaf.workflow.handle_names import handle_names
from tsdaf.workflow.naming_evidence import (
    NAMING_SNAPSHOT_MEDIA,
    DomainNaming,
    FrozenNaming,
    verify_accepted_turn_naming,
)
from tsdaf.workflow.resource_reads import (
    READ_CALL_KEY,
    ReadPlan,
    delivered_content,
    is_read_reply,
    read_many,
    resource_binding,
)
from tsdaf.workflow.state_resource_names import ReadVersionNames, StateResourceNames
from tsdaf.workflow.state_resources import (
    STATE_LOCATION_CATALOG,
    STATE_LOCATION_HISTORY,
    STATE_LOCATION_SUBMISSION,
    STATE_RESOURCE_ORIGIN,
    build_state_resources,
    catalog_recovery_reads,
    persist_state_resources,
    state_version,
)
from tsdaf.workflow.tool_turns import (
    TOOL_REQUEST_INTENT_MEDIA,
    ToolTurnState,
    find_returned_response,
    function_result,
    recorded_reply,
    response_call_steps,
    split_turns,
    tool_turns,
)
from tsdaf.workflow.write_delivery import NO_ITEM_WRITE, WriteDelivery, split_write, write_result
from tsdaf.workflow.write_registry import collection_of, item_key


def seal_artifact(
    store: PrivateArtifactStore,
    bind: Callable[..., None],
    value: object,
    media: str,
    *,
    accept: bool = True,
) -> ArtifactRef:
    """Store exact bytes and optionally accept them through the Host binding.

    The store verifies what it wrote before returning the reference, and a binding
    verifies it again; a third check here hashed the same bytes once more for nothing
    (batch 4 redundancy, 2026-09-24).
    """
    payload = value if isinstance(value, bytes) else canonical_json_bytes(value)
    ref = store.put_bytes(payload, media_type=media)
    if accept:
        bind(ref)
    return ref


def diagnostic_text(
    error: BaseException, *, model: type[BaseModel] | None = None, collection: str | None = None
) -> str:
    """The item diagnostic as text: validation errors as canonical JSON, the rest as is."""
    said = item_diagnostic(error, model=model, collection=collection)
    return said if isinstance(said, str) else canonical_json_bytes(said).decode()


def diagnostic_value(
    error: BaseException, *, model: type[BaseModel] | None = None, collection: str | None = None
) -> Any:
    """The item diagnostic as a value: validation errors structured, the rest by message."""
    if isinstance(error, ValidationError):
        return item_diagnostic(error, model=model, collection=collection)
    return str(error)


@dataclass(frozen=True)
class CallContext:
    """Host-bound identity of one recorded action within one accepted interaction."""

    record: dict[str, Any]
    call_index: int
    call: dict[str, Any]
    request_ref: ArtifactRef
    response_ref: ArtifactRef
    interaction_sha256: str
    state_resources: Mapping[str, Any]
    naming: FrozenNaming | None = None
    arguments_location: str | None = None
    arguments_are_object: bool = False
    prepared_write: PreparedWrite | None = None
    read_item: BaseModel | None = None


@dataclass(frozen=True)
class PreparedWrite:
    """Host expansion before target identification; the recorded call stays untouched."""

    body: dict[str, Any]
    errors: dict[str, str] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)
    comparison_before: dict[str, dict[str, Any]] = field(default_factory=dict)
    item_reads: dict[str, dict[str, str]] = field(default_factory=dict)
    candidate_semantics: dict[str, Any] = field(default_factory=dict)


# What one model turn cost, read off the provider's completed interaction and written
# into every lane's interaction record under these names (one place; the lanes used to
# copy some of them and the investigation lanes none -- ledger E75).
USAGE_FIELDS = (
    "total_input_tokens",
    "total_output_tokens",
    "total_tokens",
    "total_cached_tokens",
    "total_thought_tokens",
    "total_tool_use_tokens",
    "elapsed_ms",
    "transport_attempts",
    "request_id",
)


def interaction_usage(completed: Any, *, model_profile: str | None) -> dict[str, Any]:
    """The usage fields of one completed interaction, None where the provider gave none."""
    usage: dict[str, Any] = {name: getattr(completed, name, None) for name in USAGE_FIELDS}
    usage["model_profile"] = model_profile
    return usage


def lane_interaction_event(lane: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """The runner event of one model turn: which lane, which turn, what it cost, where
    the sealed record is. Shaped like the Router's `article_router_interaction` so
    `inspect_execution` totals every lane's tokens the same way."""
    return {
        "lane": lane,
        "turn": record["iteration"],
        "interaction_artifact": record["interaction_ref"],
        "request_artifact": record["request_ref"],
        "response_artifact": record["response_ref"],
        **{name: record.get(name) for name in USAGE_FIELDS},
        "model_profile": record.get("model_profile"),
    }


def call_receipt(ctx: CallContext, event: Mapping[str, Any]) -> dict[str, Any]:
    """The Host's receipt for one call, in the one shape every lane seals (ledger E70).

    The executor emits one event per call (READ / WRITE / FINISH / REJECTED, below) and
    each lane seals what the Host answered. The shape is spelled here and nowhere else:
    a lane adds its own fields beside these, and the export of a run's calls
    (`model_call_export`) recognises a receipt by `is_call_receipt`, so the two lanes
    cannot drift apart without this function changing.
    """
    receipt: dict[str, Any] = {
        "interaction_ref": ctx.record["interaction_ref"],
        "call_index": ctx.call_index,
        "call_id": ctx.call.get("call_id"),
    }
    kind = event["kind"]
    if kind == "READ":
        receipt.update(outcome=event["reply"]["outcome"], items=event["items"])
    elif kind == "WRITE":
        receipt.update(
            outcome=event["reply"]["outcome"],
            items=event["reply"]["items"],
            write_results=event["rows"],
        )
    elif kind == "FINISH":
        receipt.update(outcome="FINISHED", note=event["note"], delivery=event["completion"])
    elif kind == "REJECTED":
        receipt.update(
            outcome="REJECTED", diagnostic=event["diagnostic"], write_results=event["rows"]
        )
    else:
        raise ValueError(f"unknown call event kind {kind!r}")
    for key in ("effective_body_ref", "effective_write_version", "item_semantics", "accepted_item_batches",
                "repair_command_version", "repair_commands", "measurement_bindings"):
        if key in event:
            receipt[key] = event[key]
    return receipt


def is_call_receipt(value: Mapping[str, Any]) -> bool:
    """Whether a sealed record is a call receipt (rather than the turn it answers)."""
    return "interaction_ref" in value and "call_index" in value


@dataclass(frozen=True)
class FinishIntent:
    """Explicit stop intent read from the consumer's finish model."""

    disposition: str
    note: str
    accept: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class WriteOutcome:
    """Reply for the model plus the per-item write rows the Host will accept."""

    reply: dict[str, Any]
    rows: list[dict[str, Any]]
    audit: dict[str, Any] = field(default_factory=dict)


class ToolLoopStalled(RuntimeError):
    """Consecutive turns produced no accepted action; the Host closes out instead of paying on."""


def _no_initial() -> dict[str, Any] | None:
    return None


def _partial_close_out(reason: str) -> FinishIntent:
    """The finish the Host declares on a task's behalf: what was accepted, delivered PARTIAL."""
    return FinishIntent(disposition="PARTIAL", note=reason)


def _ignore(*_: Any) -> None:
    return None


def _same(value: Any) -> Any:
    return value


# What the host keeps on a recorded result to pair it with its call and interaction; the
# provider pairs by call_id alone.
_RESULT_BOOKKEEPING = frozenset({"interaction_sha256", "call_index"})


def _without_bookkeeping(
    visible: list[dict[str, Any]], *, catalog_locations: set[str]
) -> list[dict[str, Any]]:
    def receipt_view(node: Any) -> Any:
        if isinstance(node, list):
            return [receipt_view(item) for item in node]
        if not isinstance(node, Mapping):
            return node
        location = node.get("location")
        catalog = (
            isinstance(location, str)
            and location.partition("#")[0] in catalog_locations
            and node.get("outcome") in {"READ", "DIRECTORY"}
            and node.get("origin") == STATE_RESOURCE_ORIGIN
        )
        return {
            key: item if key == "value" else receipt_view(item)
            for key, item in node.items()
            if not (catalog and key in {"owner_task", "provided_to"})
        }

    result = []
    for step in visible:
        is_reply = step.get("type") == "function_result"
        if not is_reply and step.get("type") != "user_input":
            result.append(step)
            continue
        shown = {
            key: value
            for key, value in step.items()
            if not (is_reply and key in _RESULT_BOOKKEEPING)
        }
        part_key = "result" if is_reply else "content"
        parts = []
        for part in step.get(part_key, []):
            raw = part.get("text") if isinstance(part, Mapping) else None
            if isinstance(raw, str):
                try:
                    value = json.loads(raw)
                except ValueError:
                    pass
                else:
                    projected = value
                    if is_reply:
                        projected = receipt_view(value)
                    elif (
                        isinstance(value, dict)
                        and isinstance(earlier := value.get(READ_EARLIER_KEY), dict)
                        and isinstance(earlier.get("reply"), dict)
                    ):
                        # Only the host's older-read wrapper, never ordinary user text.
                        projected = {
                            **value,
                            READ_EARLIER_KEY: {**earlier, "reply": receipt_view(earlier["reply"])},
                        }
                    if projected != value:
                        part = {**part, "text": canonical_json_bytes(projected).decode()}
            parts.append(part)
        shown[part_key] = parts
        result.append(shown)
    return result


def _finish_reply(completion: dict[str, Any]) -> dict[str, Any]:
    return {"outcome": "FINISHED", "delivery": completion}


@dataclass(frozen=True, kw_only=True)
class ToolConsumer:
    """Everything one consumer contributes to the shared loop; each hook has one job.

    The hook count is the inventory of what is not yet unified across consumers.
    Later capability packages remove hooks as write and delivery converge.
    """

    # identity and sealing
    label: str
    name: str
    task_id: str
    turn_binding: dict[str, Any]
    request_media: str
    response_media: str
    interaction_media: str
    # runtime state shared with the loop
    delivery: WriteDelivery
    catalog_data: Any
    locations: dict[str, dict[str, Any]]
    wrap_state_content: bool
    result_ref: Callable[[], ArtifactRef | None]
    # model-facing resources
    system_instruction: str
    tools: list[dict[str, Any]]
    tool_kinds: Mapping[str, str]
    # Only explicitly configured consumers change transport parsing.
    decode_arguments: Callable[[str, Any], Any] = lambda name, raw: decode_tool_arguments(raw)
    # A consumer whose write is always one known target (a whole document) names it here.
    # A write that cannot even be decoded is then still a failure of that target, which a
    # later accepted write of the same target replaces. Without it such a failure has no
    # item, and nothing but omission resolves it. Item-wise writers leave this unset.
    fixed_write_targets: Mapping[str, str] | None = None
    # turn protocol hooks
    progress: Callable[[Mapping[str, Any]], dict[str, Any]]
    initial: Callable[[], dict[str, Any] | None] = _no_initial
    retained_call_ids: Callable[[], tuple[str, ...]] = lambda: ()
    after_projection: Callable[[list[dict[str, Any]]], None] = _ignore
    feedback_view: Callable[[Any], Any] = _same
    interaction_extras: Callable[
        [LanePreparedInteraction, LaneCompletedInteraction, dict[str, Any]], dict[str, Any]
    ]
    # Admission is after naming and prepare, but before request/intent sealing.
    check_prepared: Callable[[LanePreparedInteraction], None] = _ignore
    on_request_sealed: Callable[[ArtifactRef], None] = _ignore
    request_intent_extras: Callable[[ArtifactRef], dict[str, Any]] = lambda ref: {}
    on_response_sealed: Callable[[ArtifactRef], None] = _ignore
    on_provider_error: Callable[[ProviderInteractionError, ArtifactRef | None], None]
    turn_errors: tuple[type[BaseException], ...] = ()
    on_turn_error: Callable[[BaseException], None] = _ignore
    on_turn_recorded: Callable[[ArtifactRef, ArtifactRef | None], None] = _ignore
    # The sealed interaction record, with its ref: a lane that keeps a runner emits the
    # `literature_lane_interaction` event from it, so `inspect_execution` can total tokens.
    on_interaction: Callable[[dict[str, Any]], None] = _ignore
    on_recovered: Callable[[dict[str, Any]], None] = _ignore
    # Turns in which nothing was accepted count as stalled; at the limit the Host closes
    # out with what was accepted, as a PARTIAL delivery, and tells on_stalled. Turns are
    # also budgeted outright: at turn_limit the same close-out happens, so a task that
    # keeps doing things without finishing still ends with a sealed result. 0 disables
    # either limit. The first unresolved write gets one feedback opportunity if the
    # stall limit would otherwise hide its refusal; the hard turn limit still applies.
    stall_limit: int = 3
    turn_limit: int = 40
    # How this consumer finishes on the Host's behalf, given the reason;
    # the intent's accept seals the consumer's own result, as a finish
    # from the model would. on_close_out then records the delivery.
    close_out: Callable[[str], FinishIntent] = _partial_close_out
    on_close_out: Callable[[dict[str, Any]], None] = _ignore
    # Characters of earlier reads shown again each request before the oldest become
    # pointers, and images shown again by count; a repeat of what is still shown is
    # IN_CONTEXT, which is not progress.
    window_chars: int = DEFAULT_WINDOW_CHARS
    window_images: int = DEFAULT_WINDOW_IMAGES
    evidence_view: EvidenceView = no_source_evidence
    # The current version of a consumer-owned location whose content moves under the
    # same name, so an earlier read of it is pointed to rather than shown again. State
    # views are versioned by the loop itself; None is for what never changes once read.
    versions: Versions = no_versions
    on_stalled: Callable[[ToolLoopStalled], None] = _ignore
    # The item model a read's items are validated against: the one the consumer's read
    # declaration was generated from. A lane that installs reasons on its reads (the
    # detail lane, E130) declares `reason` on every item, and the executor validated
    # items against the base model until f07 -- refusing as "unknown field reason" the
    # very field the declaration offered, on every batch read that used it (E147).
    read_item_model: type[ContractModel] = ReadItem
    # The envelope a read's batch shape is validated against: generated by the same
    # `read_models()` call as `read_item_model`, so a lane that installs a top-level
    # `reason` on its declaration (E130) is understood by the envelope that parses it,
    # not refused as an unknown field the way the hand-written envelope refused it.
    read_envelope_model: type[ContractModel] = ReadEnvelope
    # What the model calls the host's objects (batch 2A): every request is shown through
    # `present` and every call is read through `resolve`, so the model reads and writes
    # names while the host and its receipts keep identities. The history keeps the call as
    # the model wrote it. The window resolves that history through the same callback.
    # Both default to the identity: a consumer that names nothing.
    present: Callable[[Any], Any] = _same
    resolve: Callable[[Any], Any] = _same
    make_domain_names: Callable[[dict[str, Any] | None], DomainNaming] | None = None
    restore_domain_names: Callable[[dict[str, Any]], DomainNaming] | None = None
    # action dispatch hooks
    on_call: Callable[[CallContext], None] = _ignore
    targets: Callable[[str, Any], dict[str, str | None]]
    prepare_write: Callable[[str, Any, CallContext], PreparedWrite | None] = lambda *args: None
    resolve_read: Callable[[str, Selection | None, CallContext], ReadPlan]
    apply_write: Callable[
        [str, dict[str, Any], CallContext, dict[str, str | None], dict[str, list[str]]],
        WriteOutcome,
    ]
    finish: Callable[[str, dict[str, Any], CallContext], FinishIntent]
    finish_reply: Callable[[dict[str, Any]], dict[str, Any]] = _finish_reply
    audit: Callable[[CallContext, dict[str, Any]], None] = _ignore
    diagnose: Callable[[BaseException], Any]
    rejected_errors: tuple[type[BaseException], ...]
    finished_message: str
    unknown_tool_message: str
    reject_message: str


def execute_tool_turns(
    loop: ToolTurnState,
    consumer: ToolConsumer,
    *,
    store: PrivateArtifactStore,
    bind: Callable[..., None],
    client: LaneInteractionClient,
    provider_binding: ProviderBinding,
    recovery_artifacts: tuple[ArtifactRef, ...],
    checkpoint: Callable[[], None],
    call_budget: CallBudget | None = None,
) -> None:
    """Run the consumer's task to its explicit finish or to the consumer's stop policy."""

    accepted_refs = list(recovery_artifacts)

    def seal(value: object, media: str, *, accept: bool = True) -> ArtifactRef:
        ref = seal_artifact(store, bind, value, media, accept=accept)
        if accept:
            accepted_refs.append(ref)
        return ref

    # One derived progress per run: each turn folds only the results appended since.
    progress = ActionProgress()
    submission_response: tuple[str, list[tuple[int, dict[str, Any]]]] | None = None

    def build_states() -> dict[str, Any]:
        progress.fold(loop.history)
        return build_state_resources(
            task_id=consumer.task_id,
            history=progress,
            write_delivery=consumer.delivery,
            result_ref=consumer.result_ref(),
            catalog_data=consumer.catalog_data,
            store=store,
            feedback_view=consumer.feedback_view,
        )

    # A pending response replays without a new request; its state reads still resolve.
    current_states: dict[str, Any] = build_states() if loop.pending is not None else {}
    state_names = StateResourceNames.registered(current_states, consumer.locations)
    read_versions = ReadVersionNames({})
    active_naming: FrozenNaming | None = None
    verified_turn: tuple[str, FrozenNaming | None] | None = None
    returned = (
        find_returned_response(
            recovery_artifacts,
            media_type=consumer.interaction_media,
            binding=consumer.turn_binding,
            iteration=loop.iteration,
            read_bytes=store.read_bytes,
        )
        if loop.pending is None
        else None
    )

    def version_of(location: str) -> str | None:
        return state_version(current_states, location) or consumer.versions(location)

    def names_now() -> tuple[Names, Identities]:
        """Resolve the current and recorded calls through one Names snapshot."""
        handles = handle_names(
            progress.records,
            loop.history,
            seed=active_naming.handles if active_naming is not None else None,
        )
        return handles, lambda arguments: consumer.resolve(
            handles.resolve(state_names.resolve(arguments))
        )

    def restore_names(
        record: Mapping[str, Any], *, expected_iteration: int | None = None
    ) -> FrozenNaming | None:
        nonlocal verified_turn
        identity = ArtifactRef.model_validate(record["interaction_ref"]).sha256
        if verified_turn is not None and verified_turn[0] == identity:
            return verified_turn[1]
        snapshot = verify_accepted_turn_naming(
            store.read_bytes,
            record,
            accepted_refs,
            expected_naming_version=NAMING_VERSION,
            expected_binding=consumer.turn_binding,
            expected_iteration=expected_iteration,
        )
        if snapshot is None:
            verified_turn = (identity, None)
            return None
        if consumer.restore_domain_names is None:
            raise ValueError("named turn has no domain restorer")
        view = FrozenNaming.from_snapshot(snapshot, consumer.restore_domain_names)
        verified_turn = (identity, view)
        return view

    def install_names(view: FrozenNaming | None) -> None:
        nonlocal active_naming, state_names, read_versions
        active_naming = view
        if view is not None:
            state_names = StateResourceNames.registered(
                current_states, consumer.locations, previous=view.state.roots
            )
            read_versions = view.reads

    if loop.iteration:
        previous: tuple[int, Mapping[str, Any]] | None = None
        prior_iterations: set[int] = set()
        for ref in recovery_artifacts:
            if ref.media_type != consumer.interaction_media:
                continue
            record = json.loads(read_artifact(store.read_bytes, ref))
            if "response_ref" not in record:
                continue
            iteration = record.get("iteration")
            if record.get("binding") != consumer.turn_binding or not isinstance(iteration, int):
                continue
            if iteration >= loop.iteration:
                continue
            if iteration in prior_iterations:
                raise ValueError("competing accepted interactions claim one tool turn")
            prior_iterations.add(iteration)
            if previous is None or iteration > previous[0]:
                previous = (iteration, {**record, "interaction_ref": ref.model_dump(mode="json")})
        if previous is not None:
            install_names(restore_names(previous[1]))

    def request_turn() -> dict[str, Any] | None:
        nonlocal returned, current_states, state_names, read_versions, active_naming
        # Derive this from accepted history so restarting cannot reset either the
        # stall count or the first refusal's opportunity to reach the model.
        turns = split_turns(loop.history)
        stalled, first_refusal = 0, None
        for index, turn in enumerate(turns):
            replies = [recorded_reply(step) for step in turn.results.values()]
            replies = [reply for reply in replies if isinstance(reply, dict)]
            stalled = 0 if any(_accepted_anything(reply) for reply in replies) else stalled + 1
            if first_refusal is None and any(reply.get("unresolved_writes") for reply in replies):
                first_refusal = index
        feedback_due = (
            first_refusal is not None
            and first_refusal == len(turns) - 1
            and bool(consumer.delivery.pending())
        )
        # An authenticated response already paid for is consumed before stop policy;
        # no new request is admitted by this recovery branch.
        if returned is None and consumer.turn_limit and loop.iteration >= consumer.turn_limit:
            _close_out(
                loop,
                consumer,
                reason=f"{consumer.name} reached its limit of {consumer.turn_limit} turns",
            )
            return None
        if (
            returned is None
            and consumer.stall_limit
            and stalled >= consumer.stall_limit
            and not feedback_due
        ):
            error = ToolLoopStalled(
                f"{consumer.name} made no accepted progress in {stalled} consecutive turns"
            )
            _close_out(loop, consumer, reason=str(error))
            consumer.on_stalled(error)
            return None
        current_states = build_states()
        consumer.locations.update(
            persist_state_resources(current_states, store, wrap_content=consumer.wrap_state_content)
        )
        state_names = StateResourceNames.registered(
            current_states,
            consumer.locations,
            previous=state_names.roots,
        )
        if returned is not None:
            recovered, returned = returned, None
            install_names(restore_names(recovered, expected_iteration=loop.iteration))
            consumer.on_recovered(recovered)
            return recovered
        progress_view = consumer.progress(current_states)
        full_feedback = {record.action_id: record.feedback for record in progress.records
                         if record.feedback is not None}
        if "unresolved_writes" in progress_view:
            progress_view["unresolved_writes"] = [
                {**full_feedback.get(row["action_id"], {}), **row}
                for row in progress_view["unresolved_writes"]
            ]
        if consumer.turn_limit or consumer.stall_limit:
            # The budget is the model's to spend; a model that does not know how much is
            # left reads until the host closes the task out with nothing saved.
            turn_budget: dict[str, Any] = {"used": loop.iteration}
            if consumer.turn_limit:
                turn_budget["budget"] = consumer.turn_limit
            if consumer.stall_limit and stalled:
                # The other way a task ends: turns in which nothing was accepted. Five
                # of hyperfirin's fourteen step tasks went this way on f06 (E142),
                # searching the same words a fourth and fifth time with the count
                # never in front of them.
                turn_budget["without_progress"] = stalled
                turn_budget["closes_after"] = consumer.stall_limit
            if (consumer.turn_limit and loop.iteration == consumer.turn_limit - 1) or (
                consumer.stall_limit and stalled >= consumer.stall_limit - 1
            ):
                # Knowing the budget is not knowing which turn is the last one. On the
                # e106 run of decumbenine_B, task B003 spent turn 40 writing two
                # characterization cards and was closed out with more still to write: it
                # had the count in front of it and still could not tell that this request
                # was its last chance to save. The host knows, so the host says so --
                # and the same flag, for the same reason, when one more turn without
                # accepted progress would close the task out.
                turn_budget["closing"] = True
            progress_view["turns"] = turn_budget
        handles = handle_names(
            progress.records,
            loop.history,
            seed=active_naming.handles if active_naming is not None else None,
        )
        domain = (
            consumer.make_domain_names(
                active_naming.domain.components if active_naming is not None else None
            )
            if consumer.make_domain_names is not None
            else DomainNaming(consumer.present, consumer.resolve, {"kind": "identity"})
        )
        read_versions = ReadVersionNames.registered(
            domain.present(state_names.present(handles.present(loop.history))),
            previous=read_versions.versions,
        )
        active_naming = FrozenNaming(handles, domain, state_names, read_versions)
        visible = project_context(
            loop.history,
            retained_call_ids=consumer.retained_call_ids(),
            initial=consumer.initial(),
            progress=progress_view,
            read_bytes=store.read_bytes,
            window_chars=consumer.window_chars,
            window_images=consumer.window_images,
            versions=version_of,
            identities=active_naming.resolve,
            evidence_view=consumer.evidence_view,
            item_model=consumer.read_item_model,
            feedback_reads={
                record.action_id: {
                    "location": current_states[STATE_LOCATION_HISTORY].location
                    + f"#/{index}/feedback"
                }
                for index, record in enumerate(progress.records)
                if record.feedback is not None
            },
        )
        # The host's own accounting reads what is shown in its identities; the model is
        # then shown names, and none of what the host keeps on a result to pair it.
        consumer.after_projection(visible)
        # Version naming needs the host receipt first; only the final model view
        # drops catalog ownership. Reuse the frozen table instead of parsing names.
        model_input = _without_bookkeeping(
            active_naming.present(visible),
            catalog_locations={STATE_LOCATION_CATALOG}
            | {
                name
                for root, name in state_names.roots.items()
                if root.startswith(STATE_LOCATION_CATALOG + "@")
            },
        )
        prepared = client.prepare(
            input=model_input, system_instruction=consumer.system_instruction, tools=consumer.tools
        )
        if prepared.model_profile != AUTHORIZED_MODEL_PROFILE:
            raise ValueError(f"{consumer.name} uses an unauthorized model profile")
        consumer.check_prepared(prepared)
        attempt_limit = call_budget.require_client(client) if call_budget is not None else 1
        with (
            call_budget.reserve(attempt_limit)
            if call_budget is not None
            else nullcontext() as reservation
        ):
            request_ref = seal(prepared.request_bytes, consumer.request_media)
            naming_ref = (
                seal(active_naming.snapshot(), NAMING_SNAPSHOT_MEDIA)
                if consumer.make_domain_names is not None
                else None
            )
            intent_extras = consumer.request_intent_extras(request_ref)
            consumer.on_request_sealed(request_ref)
            intent_ref = seal(
                {
                    "binding": consumer.turn_binding,
                    "iteration": loop.iteration,
                    "request_ref": request_ref.model_dump(mode="json"),
                    "transport_attempt_limit": getattr(client, "max_transport_attempts", None),
                    **({"naming_ref": naming_ref.model_dump(mode="json")} if naming_ref else {}),
                    **intent_extras,
                },
                TOOL_REQUEST_INTENT_MEDIA,
            )
            if reservation is not None:
                reservation.started(intent_ref, attempts=attempt_limit)
            try:
                completed = provider_binding.require_interaction(
                    client.execute_prepared(prepared), label=consumer.label
                )
                audit = seal_provider_attempts(
                    store, completed, request_bytes=prepared.request_bytes
                )
                if completed.request_bytes != prepared.request_bytes:
                    raise ValueError(f"{consumer.name} response is not bound to its exact request")
                response_ref = seal(completed.response_bytes, consumer.response_media, accept=False)
                consumer.on_response_sealed(response_ref)
                response = json.loads(completed.response_bytes)
            except ProviderInteractionError as error:
                audit = seal_provider_attempts(store, error, request_bytes=prepared.request_bytes)
                if audit is not None:
                    failed_record = seal(
                        {
                            "binding": consumer.turn_binding,
                            "iteration": loop.iteration,
                            "request_ref": request_ref.model_dump(mode="json"),
                            "intent_ref": intent_ref.model_dump(mode="json"),
                            "provider_attempt_ref": audit.model_dump(mode="json"),
                            "failure_code": error.failure.code.value,
                        },
                        consumer.interaction_media,
                        accept=False,
                    )
                    bind(audit, failed_record)
                    if reservation is not None:
                        reservation.settled(intent_ref, audit)
                consumer.on_provider_error(error, audit)
                return None
            except consumer.turn_errors as error:
                consumer.on_turn_error(error)
                return None
            record: dict[str, Any] = {
                "binding": consumer.turn_binding,
                "iteration": loop.iteration,
                "request_ref": request_ref.model_dump(mode="json"),
                "intent_ref": intent_ref.model_dump(mode="json"),
                **({"naming_ref": naming_ref.model_dump(mode="json")} if naming_ref else {}),
                "response_ref": response_ref.model_dump(mode="json"),
                "provider_attempt_ref": audit.model_dump(mode="json")
                if audit is not None
                else None,
                **interaction_usage(completed, model_profile=prepared.model_profile),
                **consumer.interaction_extras(prepared, completed, response),
            }
            interaction_ref = seal(record, consumer.interaction_media, accept=False)
            bind(response_ref, *((audit,) if audit is not None else ()), interaction_ref)
            if reservation is not None and audit is not None:
                reservation.settled(intent_ref, audit)
            accepted_refs.extend((response_ref, interaction_ref))
            consumer.on_turn_recorded(interaction_ref, audit)
            recorded = {**record, "interaction_ref": interaction_ref.model_dump(mode="json")}
            consumer.on_interaction(recorded)
            return recorded

    for record, call_index, call in tool_turns(
        loop, request=request_turn, checkpoint=checkpoint, read_bytes=store.read_bytes
    ):
        restored_names = restore_names(record, expected_iteration=loop.iteration)
        if restored_names is not None:
            install_names(restored_names)
        response_ref = ArtifactRef.model_validate(record["response_ref"])
        arguments_location = None
        arguments_are_object = False
        if isinstance(call.get("name"), str) and consumer.tool_kinds.get(call["name"]) == "write":
            if submission_response is None or submission_response[0] != response_ref.sha256:
                submission_response = (
                    response_ref.sha256,
                    response_call_steps(read_artifact(store.read_bytes, response_ref)),
                )
            step_index, submitted_call = submission_response[1][call_index]
            # Reuse the immutable response, not one new artifact per refused item.
            # This is execution history, never a source selection or scientific evidence.
            root = STATE_LOCATION_SUBMISSION + "@" + response_ref.sha256
            binding = resource_binding(
                response_ref.model_dump(mode="json"),
                origin=STATE_RESOURCE_ORIGIN,
                provided_to=consumer.task_id,
                owner_task=consumer.task_id,
            )
            consumer.locations[root] = (
                {"kind": "CONTENT", "resource": binding} if consumer.wrap_state_content else binding
            )
            arguments_location = root + f"#/steps/{step_index}/arguments"
            arguments_are_object = isinstance(submitted_call.get("arguments"), dict)
        ctx = CallContext(
            record=record,
            call_index=call_index,
            call=call,
            request_ref=ArtifactRef.model_validate(record["request_ref"]),
            response_ref=response_ref,
            interaction_sha256=record["interaction_ref"]["sha256"],
            state_resources=current_states,
            naming=active_naming,
            arguments_location=arguments_location,
            arguments_are_object=arguments_are_object,
        )
        consumer.on_call(ctx)
        _, fallback_identities = names_now()
        reply = _dispatch_call(
            loop,
            consumer,
            ctx,
            versions=version_of,
            identities=active_naming.resolve if active_naming is not None else fallback_identities,
        )
        loop.history.append(
            function_result(
                ctx.call,
                reply,
                interaction_sha256=ctx.interaction_sha256,
                call_index=ctx.call_index,
            )
        )


def _hollow_items(raw: Mapping[str, Any]) -> set[str]:
    """Items that say nothing: answered with their fields, never owed as failures.

    A model sends {} to learn what an item needs, and the refusal lists the fields. An
    item carrying only a handle says as little. Nothing it meant can vanish with an item
    that says nothing, so it is not a rejected write that blocks COMPLETE delivery.
    """
    return {
        item_key(name, index)
        for name, values in raw.items()
        if isinstance(values, list)
        for index, value in enumerate(values)
        if isinstance(value, dict) and set(value) <= {"item_id"}
    }


def _close_out(loop: ToolTurnState, consumer: ToolConsumer, *, reason: str) -> None:
    """Finish on the task's behalf: everything accepted so far is delivered, as PARTIAL.

    A limit is a budget, not a verdict on the work. What the model saved stands; what it
    did not resolve is named in the delivery; the reason is the delivery's note. The same
    sealing a model's own finish would do happens here, so a closed-out run leaves the
    same kind of result behind, traceable to the turn that ended it.
    """
    intent = consumer.close_out(reason)
    completion = consumer.delivery.finish(disposition=intent.disposition, note=intent.note)
    intent.accept()
    result_ref = consumer.result_ref()
    completion["result_ref"] = (
        result_ref.model_dump(mode="json") if result_ref is not None else None
    )
    completion["closed_out"] = reason
    consumer.delivery.completion = completion
    loop.finished = True
    consumer.on_close_out(completion)


UNRESOLVED_WRITES_MESSAGE = (
    "Unresolved writes block COMPLETE delivery: resend a corrected item with the same "
    "identity to resolve one. " + PARTIAL_DELIVERY_GUIDANCE
)


def _unresolved_message(pending: list[dict[str, Any]]) -> str:
    """What resolves the writes still pending, said of the ones that are there."""
    if any(row["item"] is None for row in pending):
        return UNRESOLVED_WRITES_MESSAGE + " " + NO_ITEM_WRITE
    return UNRESOLVED_WRITES_MESSAGE


def _accepted_anything(reply: dict[str, Any]) -> bool:
    """Whether the Host accepted something, which is what the stall limit counts.

    A write naming no collection at all is answered without a single row. Treating that
    as progress let a task with nothing left to say hold the loop open indefinitely,
    which is the one thing the limit exists to prevent. A read that delivered no content,
    because everything it named was refused or already on screen, is the same emptiness
    with a different tool.
    """
    if reply["outcome"] == "REJECTED":
        return False
    rows = reply.get("write_results")
    if rows is not None:
        return bool(rows)
    if is_read_reply(reply):
        return any(delivered_content(row) for row in reply["items"])
    return True


def _dispatch_call(
    loop: ToolTurnState,
    consumer: ToolConsumer,
    ctx: CallContext,
    *,
    versions: Versions = no_versions,
    identities: Identities,
) -> dict[str, Any]:
    """Apply one recorded action; a rejection never discards earlier accepted actions."""
    name = ctx.call.get("name")
    kind = consumer.tool_kinds.get(name) if isinstance(name, str) else None
    targets: dict[str, str | None] = {}
    repairs: dict[str, list[str]] = {}
    refused_repairs: list[dict[str, Any]] = []

    def describe_submission(row: dict[str, Any]) -> None:
        if ctx.arguments_location is None:
            return
        location = ctx.arguments_location
        key = row["item"]
        if ctx.arguments_are_object and isinstance(key, str):
            collection, mark, index = key.partition(":")
            arguments = ctx.call.get("arguments")
            values = arguments.get(collection) if isinstance(arguments, dict) else None
            if mark and index.isdecimal() and isinstance(values, list) and int(index) < len(values):
                escaped = collection.replace("~", "~0").replace("/", "~1")
                location += f"/{escaped}/{index}"
        row["submission_read"] = {"location": location}

    def action_candidates(item: str | None = None) -> dict[str, Any]:
        pending = consumer.delivery.pending()
        if item is not None:
            pending = [
                row
                for row in pending
                if row.get("item") is not None and collection_of(row["item"]) == collection_of(item)
            ]
        names = sorted(
            {
                shown
                for row in pending
                if isinstance(
                    (
                        shown := (
                            ctx.naming.present(row["action_id"])
                            if ctx.naming is not None
                            else row["action_id"]
                        )
                    ),
                    str,
                )
                and not minted(shown)
            }
        )
        return {"available_names": names[:8], "more_available": max(0, len(names) - 8)}

    try:
        if loop.finished:
            raise ValueError(consumer.finished_message)
        if kind is None or not isinstance(name, str):
            raise ValueError(consumer.unknown_tool_message)
        raw = ctx.call["arguments"]
        decoded = consumer.decode_arguments(name, raw)
        if isinstance(raw, str):
            ctx.call["arguments"] = decoded
        raw = decoded
        # The host acts on identities; the recorded call keeps the model's own names.
        raw = identities(raw)
        if kind == "write":
            prepared = consumer.prepare_write(name, raw, ctx)
            if prepared is not None:
                ctx = replace(ctx, prepared_write=prepared)
                raw = prepared.body
                for action, proposal in prepared.comparison_before.items():
                    consumer.delivery.remember_candidate(action, proposal)
            targets = consumer.targets(name, raw)
            raw, control, refused_repairs = split_write(raw)
            repairs, refused = consumer.delivery.check_repairs(control, tool=name, targets=targets)
            for row in refused:
                row.update(action_candidates(row.get("item")))
            refused_repairs.extend(refused)
        if kind == "read":

            def resolve(item) -> ReadPlan:
                return consumer.resolve_read(item.location, item.select, replace(ctx, read_item=item))

            # What the model can already see is what the next request will show: this
            # turn's reads whole, and behind them the window the projection will build
            # from the same history, so the same computation answers both.
            window = content_window(
                loop.history,
                budget=consumer.window_chars,
                max_images=consumer.window_images,
                versions=versions,
                identities=identities,
                evidence_view=consumer.evidence_view,
                item_model=consumer.read_item_model,
            )
            reading = read_many(
                raw,
                resolve=resolve,
                item_model=consumer.read_item_model,
                envelope_model=consumer.read_envelope_model,
                in_context=window.seen,
            )
            # Offer actual authorized pages, including the selector they accept. The
            # failed name cannot tell us which scientific source the model intended.
            for row in reading.receipt["items"]:
                if (
                    row.get("outcome") == "REJECTED"
                    and row.get("code") == "UNAVAILABLE"
                    and not row.get("available_reads")
                ):
                    row["available_reads"] = catalog_recovery_reads(
                        ctx.state_resources[STATE_LOCATION_CATALOG]
                    )
            loop.outbox.extend(
                {**part, READ_CALL_KEY: ctx.call["call_id"]} for part in reading.content
            )
            reply: dict[str, Any] = reading.receipt
            # Navigation lists places: say which the model has already read and where that
            # content is now, and whether earlier navigation listed exactly these.
            annotate_navigation_reply(
                reply,
                window,
                read_batch(
                    raw,
                    item_model=consumer.read_item_model,
                ),
                item_model=consumer.read_item_model,
            )
            rejected_rows = [row for row in reply["items"] if row.get("outcome") == "REJECTED"]
            if reply["outcome"] == "REJECTED":
                # Nothing came back. The turn counter already calls this no progress, so
                # calling it a read here left each consumer to find the reason its own way
                # -- and left one of them recording no reason at all.
                consumer.audit(
                    ctx,
                    {
                        "kind": "REJECTED",
                        "diagnostic": rejected_rows,
                        "rows": [],
                        "items": reply["items"],
                    },
                )
                return {**reply, "message": consumer.reject_message}
            consumer.audit(ctx, {"kind": "READ", "reply": reply, "items": reply["items"]})
            return reply
        if kind == "write":
            outcome = consumer.apply_write(name, raw, ctx, targets, repairs)
            hollow = _hollow_items(raw)
            for row in outcome.rows:
                describe_submission(row)
                submitted = raw if ctx.prepared_write else ctx.call["arguments"]
                if ctx.prepared_write and row["item"] in ctx.prepared_write.errors:
                    submitted = None
                consumer.delivery.describe_resolutions(
                    row, control, submitted=submitted
                )
                if ctx.prepared_write and row["item"] in ctx.prepared_write.item_reads:
                    row["effective_candidate_read"] = ctx.prepared_write.item_reads[row["item"]]
                if row["item"] not in hollow:
                    consumer.delivery.accept(row)
            reply = {**outcome.reply, "write_results": outcome.rows}
            # What a model needs after a write it cannot see the state of: which repair
            # entries were not repairs, and which failures still block complete delivery.
            if refused_repairs:
                reply["repairs"] = refused_repairs
            if consumer.delivery.pending():
                reply["unresolved_writes"] = consumer.delivery.pending()
                reply["message"] = _unresolved_message(reply["unresolved_writes"])
            consumer.audit(
                ctx, {"kind": "WRITE", "reply": reply, "rows": outcome.rows, **outcome.audit,
                      **(ctx.prepared_write.audit if ctx.prepared_write else {})}
            )
            return reply
        if kind == "finish":
            intent = consumer.finish(name, raw, ctx)
            completion = consumer.delivery.finish(disposition=intent.disposition, note=intent.note)
            intent.accept()
            result_ref = consumer.result_ref()
            completion["result_ref"] = (
                result_ref.model_dump(mode="json") if result_ref is not None else None
            )
            consumer.audit(ctx, {"kind": "FINISH", "completion": completion, "note": intent.note})
            consumer.delivery.completion = completion
            loop.finished = True
            return consumer.finish_reply(completion)
        raise ValueError(consumer.unknown_tool_message)
    except consumer.rejected_errors as error:
        diagnostic = consumer.diagnose(error)
        rows: list[dict[str, Any]] = []
        if kind == "write" and isinstance(name, str) and not loop.finished:
            # What a refused write leaves to deliver is the items it carried, each under
            # its own identity. One that carried none leaves nothing (a3's B009 was
            # handed a placeholder obligation for an empty write, which no write could
            # replace). One the Host could not read as items is an obligation of its
            # own, with no item, which the model is told only omitting resolves.
            carried: list[tuple[str | None, str | None]] = [*targets.items()]
            if not carried and consumer.fixed_write_targets:
                carried = [*consumer.fixed_write_targets.items()]
            if not carried and not isinstance(error, NamesNothing):
                carried.append((None, None))
            for item, target in carried:
                rows.append(
                    write_result(
                        interaction_sha256=ctx.interaction_sha256,
                        call_index=ctx.call_index,
                        item=item,
                        tool=name,
                        target=target,
                        outcome="REJECTED",
                        diagnostic=diagnostic,
                    )
                )
            for row in rows:
                describe_submission(row)
                submitted = raw if ctx.prepared_write else ctx.call.get("arguments")
                if ctx.prepared_write and row["item"] in ctx.prepared_write.errors:
                    submitted = None
                consumer.delivery.describe_resolutions(
                    row, WriteControl(),
                    submitted=submitted,
                )
                if ctx.prepared_write and row["item"] in ctx.prepared_write.item_reads:
                    row["effective_candidate_read"] = ctx.prepared_write.item_reads[row["item"]]
                consumer.delivery.accept(row)
        consumer.audit(ctx, {"kind": "REJECTED", "diagnostic": diagnostic, "rows": rows,
                             **(ctx.prepared_write.audit if ctx.prepared_write else {})})
        no_item = any(row["item"] is None for row in rows)
        return {
            "outcome": "REJECTED",
            "diagnostic": diagnostic,
            **({"diagnostic_scope": "call"} if kind == "write" else {}),
            **(
                {"validation_scope": "envelope", "items_validated": False}
                if isinstance(error, WriteShapeError)
                else {}
            ),
            "message": consumer.reject_message + (" " + NO_ITEM_WRITE if no_item else ""),
            "write_results": rows,
            **({"repairs": refused_repairs} if refused_repairs else {}),
            "unresolved_writes": consumer.delivery.pending(),
            **(action_candidates() if kind == "finish" else {}),
        }
