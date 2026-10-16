"""Build a model input from accepted state, a window of earlier reads, and the last turn.

The audit transcript stays intact and is the only memory there is: consumers supply
their accepted progress, and what the model read earlier is shown again only as far as
a character budget allows, degraded to pointers beyond it. This module knows neither a
scientific schema nor a provider implementation.
"""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.content_window import (
    DEFAULT_WINDOW_CHARS,
    DEFAULT_WINDOW_IMAGES,
    EvidenceView,
    Identities,
    Versions,
    as_written,
    content_window,
    no_source_evidence,
    no_versions,
    window_steps,
)
from tsdaf.workflow.resource_reads import TRANSCRIPT_TAGS
from tsdaf.workflow.tool_turns import split_turns
from tsdaf.workflow.write_delivery import write_feedback_items


@dataclass(frozen=True)
class ContextProjectionStats:
    """Observable statistics for projected context.

    Counts raw text characters (not tokens), actual message count,
    actual image count, and observable image pixels.
    If image dimensions/pixels are unknown, image_pixels remains None.
    """

    message_count: int = 0
    text_characters: int = 0
    image_count: int = 0
    image_pixels: int | None = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert stats to dictionary representation."""
        return {
            "message_count": self.message_count,
            "text_characters": self.text_characters,
            "image_count": self.image_count,
            "image_pixels": self.image_pixels,
        }


def _extract_observable_pixels(item: dict[str, Any]) -> int | None:
    """Extract observable pixel count from an image part or item dict.

    Returns None if pixels cannot be determined.
    """
    pixels = item.get("pixels")
    if isinstance(pixels, int) and pixels >= 0:
        return pixels

    width = item.get("width")
    height = item.get("height")
    if isinstance(width, int) and isinstance(height, int) and width >= 0 and height >= 0:
        return width * height

    dims = item.get("dimensions")
    if isinstance(dims, dict):
        w = dims.get("width")
        h = dims.get("height")
        if isinstance(w, int) and isinstance(h, int) and w >= 0 and h >= 0:
            return w * h
    elif isinstance(dims, (tuple, list)) and len(dims) == 2:
        w, h = dims[0], dims[1]
        if isinstance(w, int) and isinstance(h, int) and w >= 0 and h >= 0:
            return w * h

    nested = item.get("image")
    if isinstance(nested, dict):
        return _extract_observable_pixels(nested)

    return None


def compute_context_stats(
    messages: Sequence[dict[str, Any]],
) -> ContextProjectionStats:
    """Compute observable projection statistics from a sequence of messages.

    Counts raw text characters (not tokens), actual message count,
    actual image count, and observable image pixels.
    If image dimensions/pixels are unknown, image_pixels remains None.
    """
    message_count = len(messages)
    text_characters = 0
    image_count = 0
    total_pixels = 0
    has_unknown_pixels = False

    for step in messages:
        content = step.get("content")
        has_content_images = False
        if isinstance(content, str):
            text_characters += len(content)
        elif isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
            for part in content:
                if isinstance(part, str):
                    text_characters += len(part)
                elif isinstance(part, dict):
                    text_val = part.get("text")
                    if isinstance(text_val, str):
                        text_characters += len(text_val)

                    part_type = part.get("type")
                    mime_type = part.get("mime_type", "")
                    is_image = (
                        part_type in ("image", "image_url")
                        or (isinstance(mime_type, str) and mime_type.startswith("image/"))
                        or "image" in part
                    )
                    if is_image:
                        has_content_images = True
                        image_count += 1
                        px = _extract_observable_pixels(part)
                        if px is None:
                            has_unknown_pixels = True
                        else:
                            total_pixels += px
        else:
            if isinstance(step.get("text"), str):
                text_characters += len(step["text"])

        if isinstance(step.get("thought"), str):
            text_characters += len(step["thought"])

        args = step.get("arguments")
        if isinstance(args, str):
            text_characters += len(args)
        elif isinstance(args, dict):
            text_characters += len(canonical_json_bytes(args).decode("utf-8"))

        # In function_result, content is in result list
        step_result = step.get("result")
        if isinstance(step_result, Sequence) and not isinstance(step_result, (bytes, bytearray)):
            for r_part in step_result:
                if isinstance(r_part, dict) and isinstance(r_part.get("text"), str):
                    text_characters += len(r_part["text"])

        step_type = step.get("type")
        step_mime = step.get("mime_type", "")
        if (
            step_type in ("image", "image_url")
            or (isinstance(step_mime, str) and step_mime.startswith("image/"))
        ) and not has_content_images:
            image_count += 1
            px = _extract_observable_pixels(step)
            if px is None:
                has_unknown_pixels = True
            else:
                total_pixels += px

    if image_count == 0:
        final_pixels: int | None = 0
    elif has_unknown_pixels:
        final_pixels = None
    else:
        final_pixels = total_pixels

    return ContextProjectionStats(
        message_count=message_count,
        text_characters=text_characters,
        image_count=image_count,
        image_pixels=final_pixels,
    )


class ProjectedContext(list[dict[str, Any]]):
    """Projected context messages list with attached observable stats."""

    stats: ContextProjectionStats

    def __init__(
        self,
        messages: Sequence[dict[str, Any]] = (),
        *,
        stats: ContextProjectionStats | None = None,
    ) -> None:
        super().__init__(messages)
        self.stats = stats if stats is not None else compute_context_stats(messages)


def _write_feedback(
    value: dict[str, Any], seen: set[str], feedback_reads: dict[str, Any],
    *, progress: bool = False,
) -> dict[str, Any]:
    """One complete action row per visible result; current status is not history."""
    value = copy.deepcopy(value)
    if "write_results" in value:
        rows = write_feedback_items(value)
        value.pop("items", None)
        value.pop("unresolved_writes", None)
        value["write_results"] = rows
        for row in rows:
            action = row.get("action_id")
            if isinstance(action, str):
                seen.add(action)
            if action in feedback_reads:
                row["feedback_read"] = feedback_reads[action]
            if row.pop("envelope_action", None) is not None:
                row.pop("diagnostic", None)
                row["diagnostic_from"] = (
                    "write envelope" if value.get("validation_scope") == "envelope"
                    else "write call"
                )
    if progress and "unresolved_writes" in value:
        actions = value.get("action_progress", {}).get("actions", [])
        complete = {row["action_id"]: row["feedback"] for row in actions
                    if isinstance(row.get("feedback"), dict)}
        pending = [{**complete.get(row.get("action_id"), {}), **row}
                   for row in value.get("unresolved_writes", [])]
        active = {row.get("action_id") for row in pending}
        shown_envelopes: dict[str, str] = {}
        visible = []
        for row in pending:
            action = row["action_id"]
            if action in seen:
                visible.append({"action_id": action, "item": row.get("item"),
                                "feedback_from": "latest write_results"})
                continue
            if action in feedback_reads:
                row["feedback_read"] = feedback_reads[action]
            envelope = row.pop("envelope_action", None)
            if envelope is not None:
                if envelope in shown_envelopes:
                    row.pop("diagnostic", None)
                    row["diagnostic_from"] = shown_envelopes[envelope]
                else:
                    shown_envelopes[envelope] = action
            visible.append(row)
        value["unresolved_writes"] = visible
        for row in actions:
            action = row.get("action_id")
            if action not in feedback_reads and not isinstance(row.get("feedback"), dict):
                continue
            row.pop("feedback", None)
            row.pop("diagnostic", None)
            row["summary"] = " ".join(str(row[k]) for k in ("tool", "outcome", "item") if k in row)
            row["pending"] = action in active
            if action in feedback_reads:
                row["feedback_read"] = feedback_reads[action]
    return value


def project_context(
    history: Sequence[dict[str, Any]],
    *,
    progress: dict[str, Any],
    read_bytes: Callable[[ArtifactRef], bytes],
    initial: dict[str, Any] | None = None,
    window_chars: int = DEFAULT_WINDOW_CHARS,
    window_images: int = DEFAULT_WINDOW_IMAGES,
    versions: Versions = no_versions,
    identities: Identities = as_written,
    evidence_view: EvidenceView = no_source_evidence,
    item_model: type[ContractModel] | None = None,
    feedback_reads: dict[str, Any] | None = None,
    retained_call_ids: Sequence[str] = (),
) -> ProjectedContext:
    """Expand only this request's selected payloads; never mutate the audit history.

    A Host-owned ``turn_start`` marks a response (including an empty/rejected
    response). The last turn is shown whole. Reads from earlier turns are shown again
    while ``window_chars`` allows, newest first, and as pointers beyond that; an image
    is shown in the one request after its read and pointed to afterwards. An unrelated
    later write never resends anything.
    """
    if not history:
        raise ValueError("a task requires an initial context")
    turns = split_turns(history)
    start = turns[-1].start if turns else len(history)
    selected = [initial if initial is not None else history[0]]
    # A consumer may keep its one current proposal in the working context. Reuse
    # the actual call/result; do not manufacture a summary or another write ledger.
    # Last-turn calls are already shown below. Capacity admission still measures
    # this complete request, independently of the historical-read window.
    retained = set(retained_call_ids)
    last_ids = {step.get("call_id") for step in history[start:]
                if step.get("type") == "function_call"}
    retained.difference_update(last_ids)
    selected.extend(step for step in history[1:start]
                    if step.get("type") in {"function_call", "function_result"}
                    and step.get("call_id") in retained)
    selected.extend(
        window_steps(
            content_window(
                history,
                budget=window_chars,
                max_images=window_images,
                versions=versions,
                identities=identities,
                evidence_view=evidence_view,
                item_model=item_model,
            )
        )
    )
    selected.extend(history[start:])
    # Merge each write action once for the provider. Audit history is unchanged;
    # current pending references point to the complete row in this same request.
    seen_feedback: set[str] = set()
    feedback_reads = feedback_reads or {}
    for index, step in enumerate(selected):
        if step.get("type") != "function_result":
            continue
        parts = []
        for part in step.get("result", []):
            if isinstance(part.get("text"), str):
                try:
                    reply = json.loads(part["text"])
                except ValueError:
                    reply = None
                if isinstance(reply, dict) and "write_results" in reply:
                    part = {
                        **part,
                        "text": canonical_json_bytes(
                            _write_feedback(reply, seen_feedback, feedback_reads)
                        ).decode(),
                    }
            parts.append(part)
        selected[index] = {**step, "result": parts}
    if progress:
        # Last, not second. Everything ahead of the progress block only ever grows by
        # appending -- the task input, then the window, then the turn just taken -- while
        # the progress block is rewritten every turn, so in front it is a volatile prefix
        # and at the end it is the one part that changed. It also reads in the order the
        # turn happened: here is the task, here is what you saw, here is what you just
        # did, here is where that leaves you. Caching is not the reason and must not be
        # claimed as one: a live probe of ten calls, prefixes from 9,728 to 129,885
        # tokens, each prefix sent twice, got cached_tokens null every single time. This
        # gateway does no implicit caching today; the ordering is free and is the shape a
        # cache would need if one ever arrives.
        selected.append(
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": canonical_json_bytes(
                            _write_feedback(progress, seen_feedback, feedback_reads, progress=True)
                        ).decode(),
                    }
                ],
            }
        )
    output: list[dict[str, Any]] = []
    payloads: dict[ArtifactRef, bytes] = {}
    for step in selected:
        step = {key: value for key, value in step.items() if key != "turn_start"}
        if step["type"] == "user_input":
            content = []
            for part in step["content"]:
                # The transcript tags each part with the read that delivered it, for
                # the window's sake; the model is shown the part, not the bookkeeping.
                part = {key: value for key, value in part.items() if key not in TRANSCRIPT_TAGS}
                if "artifact_ref" not in part:
                    content.append(dict(part))
                    continue
                ref = ArtifactRef.model_validate(part["artifact_ref"])
                if part.get("mime_type") != ref.media_type:
                    raise ValueError("display MIME type differs from the selected artifact")
                if ref not in payloads:
                    payloads[ref] = read_artifact(read_bytes, ref)
                content.append(
                    {
                        **{key: value for key, value in part.items() if key != "artifact_ref"},
                        "data": base64.b64encode(payloads[ref]).decode(),
                    }
                )
            step = {**step, "content": content}
        output.append(step)

    stats = compute_context_stats(output)
    return ProjectedContext(output, stats=stats)


def project_context_with_stats(
    history: Sequence[dict[str, Any]],
    *,
    progress: dict[str, Any],
    read_bytes: Callable[[ArtifactRef], bytes],
    initial: dict[str, Any] | None = None,
    window_chars: int = DEFAULT_WINDOW_CHARS,
    window_images: int = DEFAULT_WINDOW_IMAGES,
    versions: Versions = no_versions,
    identities: Identities = as_written,
    evidence_view: EvidenceView = no_source_evidence,
    item_model: type[ContractModel] | None = None,
) -> tuple[ProjectedContext, ContextProjectionStats]:
    """Project context and return both the messages list and projection stats."""
    projected = project_context(
        history,
        progress=progress,
        read_bytes=read_bytes,
        initial=initial,
        window_chars=window_chars,
        window_images=window_images,
        versions=versions,
        identities=identities,
        evidence_view=evidence_view,
        item_model=item_model,
    )
    return projected, projected.stats
