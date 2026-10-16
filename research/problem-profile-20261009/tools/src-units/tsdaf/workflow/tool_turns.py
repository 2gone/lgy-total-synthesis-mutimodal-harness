"""One recoverable tool-turn driver, independent of content and task roles.

Consumers provide source/write validation and bind their result reference together
with this cursor in the existing accepted artifact chain. No second state store.
"""

from __future__ import annotations

import json
from copy import deepcopy
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.workflow.accepted_artifacts import read_artifact

TOOL_REQUEST_INTENT_MEDIA = "application/vnd.tsdaf.tool-request-intent+json"


def decode_tool_arguments(arguments: Any) -> Any:
    """Decode only the transport spelling; validation belongs to the declared tool."""
    return json.loads(arguments) if isinstance(arguments, str) else arguments


class UncertainToolResponse(RuntimeError):
    """A request may have executed, but no associated response was accepted."""


@dataclass
class ToolTurnState:
    history: list[dict[str, Any]] = field(default_factory=list)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    next_call: int = 0
    iteration: int = 0
    finished: bool = False


def function_result(call: dict[str, Any], reply: Any, **binding: Any) -> dict[str, Any]:
    """The transcript's record of one reply, in the one shape ``recorded_reply`` reads back."""
    return {
        "type": "function_result",
        "call_id": call["call_id"],
        **binding,
        "result": [{"type": "text", "text": canonical_json_bytes(reply).decode()}],
    }


def recorded_reply(step: dict[str, Any]) -> Any:
    """The reply a ``function_result`` step carries; None without one, ValueError if malformed."""
    parts = step.get("result")
    if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
        return None
    text = parts[0].get("text")
    if not isinstance(text, str):
        return None
    return json.loads(text)


@dataclass(frozen=True)
class Turn:
    """One model turn as this driver records it: its calls, their results, its content."""

    start: int
    calls: tuple[dict[str, Any], ...]
    results: dict[str, dict[str, Any]]
    parts: tuple[dict[str, Any], ...]


def split_turns(history: Sequence[dict[str, Any]]) -> list[Turn]:
    """Group the transcript by the ``turn_start`` marks written below.

    The first step is the task's initial context and never a turn, whatever it carries.
    A turn runs from one mark to the next: the response's calls, the result recorded for
    each, and the content its reads delivered. The projection and the content window
    split the transcript here; the action fold keeps its own incremental cursor over the
    same marks, and only its fallback identity for an unbound result depends on it.
    """
    starts = [index for index in range(1, len(history)) if history[index].get("turn_start")]
    turns: list[Turn] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(history)
        calls: list[dict[str, Any]] = []
        results: dict[str, dict[str, Any]] = {}
        parts: list[dict[str, Any]] = []
        for step in history[start:end]:
            kind = step.get("type")
            if kind == "function_call":
                calls.append(step)
            elif kind == "function_result":
                call_id = step.get("call_id")
                if isinstance(call_id, str):
                    results[call_id] = step
            elif kind == "user_input":
                content = step.get("content")
                if isinstance(content, list):
                    parts.extend(part for part in content if isinstance(part, dict))
        turns.append(Turn(start, tuple(calls), results, tuple(parts)))
    return turns


def find_returned_response(
    refs: tuple[ArtifactRef, ...],
    *,
    media_type: str,
    binding: dict[str, Any],
    iteration: int,
    read_bytes: Callable[[ArtifactRef], bytes],
) -> dict[str, Any] | None:
    """Recover a durably bound response when its pending cursor was not yet accepted.

    ``refs`` must come from the caller's authenticated stage artifact closure. Loose
    objects and an unbound response body cannot establish a request/response pair.
    """
    matches = {}
    requested = False
    for ref in refs:
        if ref.media_type not in {media_type, TOOL_REQUEST_INTENT_MEDIA}:
            continue
        payload = read_artifact(read_bytes, ref)
        value = json.loads(payload)
        if value.get("binding") != binding or value.get("iteration") != iteration:
            continue
        if ref.media_type == TOOL_REQUEST_INTENT_MEDIA:
            requested = True
            continue
        if "response_ref" not in value:
            # Failed-request accounting is not a model answer to replay.
            continue
        for key in ("request_ref", "response_ref"):
            artifact = ArtifactRef.model_validate(value[key])
            read_artifact(read_bytes, artifact)
        matches[ref] = {**value, "interaction_ref": ref.model_dump(mode="json")}
    if len(matches) > 1:
        raise ValueError("more than one accepted response claims the same tool turn")
    if not matches and requested:
        raise UncertainToolResponse(
            "A prior request may have executed without an accepted associated response. "
            "Automatic replay is disabled; the accepted task state is preserved."
        )
    return next(iter(matches.values()), None)


def response_calls(payload: bytes) -> list[dict[str, Any]]:
    """The executor's call indices, also used to verify sealed call coverage."""
    return [step for _, step in response_call_steps(payload)]


def response_call_steps(payload: bytes) -> list[tuple[int, dict[str, Any]]]:
    """Retain the actual JSON step position when linking a submitted tool item."""
    return [
        (index, step)
        for index, step in enumerate(json.loads(payload).get("steps", []))
        if step.get("type") == "function_call"
    ]


def tool_turns(
    state: ToolTurnState,
    *,
    request: Callable[[], dict[str, Any] | None],
    checkpoint: Callable[[], None],
    read_bytes: Callable[[ArtifactRef], bytes],
) -> Iterator[tuple[dict[str, Any], int, dict[str, Any]]]:
    """Yield each recorded action and accept its cursor after the consumer applies it.

    A crash before acceptance replays from the prior state; after acceptance it
    resumes the next action. Consumers must not hide external side effects in an
    action. Finish drains the recorded response and never asks for confirmation.
    """
    while not state.finished or state.pending is not None:
        fresh = state.pending is None
        record = request() if fresh else state.pending
        if record is None:
            return
        ref = ArtifactRef.model_validate(record["response_ref"])
        payload = read_artifact(read_bytes, ref)
        calls = response_calls(payload)
        if fresh:
            # Preserve the visible response, not just its actions. Private provider
            # reasoning is not a transcript step and is never copied here.
            normalized = []
            for step in json.loads(payload).get("steps", []):
                if step.get("type") == "model_output":
                    content = [deepcopy(part) for part in step.get("content", [])
                               if part.get("type") == "text"
                               and isinstance(part.get("text"), str) and part["text"].strip()]
                    if content:
                        normalized.append({"type": "model_output", "content": content})
                elif step.get("type") == "function_call":
                    call = dict(step)
                    if isinstance(call.get("arguments"), str):
                        try:
                            call["arguments"] = decode_tool_arguments(call["arguments"])
                        except ValueError:
                            pass
                    normalized.append(call)
            if normalized:
                normalized[0]["turn_start"] = True
                state.history.extend(normalized)
            if not calls:
                state.history.append(
                    {
                        "type": "user_input",
                        **({} if normalized else {"turn_start": True}),
                        "content": [
                            {
                                "type": "text",
                                "text": "No tool action was supplied. Use the declared tools; "
                                "delivery requires an explicit accepted finish action.",
                            }
                        ],
                    }
                )
                state.iteration += 1
                checkpoint()
                continue
            state.pending, state.next_call = record, 0
            checkpoint()
        if not 0 <= state.next_call <= len(calls):
            raise ValueError("tool cursor exceeds its recorded response")
        for index in range(state.next_call, len(calls)):
            yield record, index, calls[index]
            state.next_call = index + 1
            checkpoint()
        if state.outbox:
            state.history.append({"type": "user_input", "content": list(state.outbox)})
        state.outbox.clear()
        state.pending, state.next_call = None, 0
        state.iteration += 1
        checkpoint()
