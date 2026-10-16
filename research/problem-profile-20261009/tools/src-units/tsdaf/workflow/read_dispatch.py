"""Shared read dispatch: one location becomes one ReadPlan under rules kept in one place.

Consumers resolve a location root into a ``SourceTarget`` or ``RecordTarget`` and keep
their own location families, source readers, result rendering and accept bookkeeping.
The selector rules, image pixel estimate, text preview short-circuit and record
projection live here so a third read consumer cannot drift from the first two.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from tsdaf.contracts.tool_read import (
    DirectorySelection,
    ImageSelection,
    RecordSelection,
    Selection,
    TextSelection,
    WholeSelection,
)
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.workflow.resource_reads import (
    READ_BYTES,
    ReadFailure,
    ReadPlan,
    ReadValue,
    fragment_binding,
    record_value,
    text_value,
)
from tsdaf.workflow.state_resources import STATE_PREFIX, resolve_state_read

Region = tuple[int, int, int, int]
SentenceSpans = Callable[[str], list[tuple[int, int]]]
PREVIEW_MAX_BYTES = 8_000


@dataclass(frozen=True)
class SourceTarget:
    """An authorized source location; the fields carry the consumers' known differences."""

    medium: Literal["text", "visuals"]
    plan: Callable[[Selection | None, Region | None, int], Callable[[], ReadValue]]
    saved: bool = False
    preview_location: str = ""
    preview_extra: Mapping[str, Any] = field(default_factory=dict)
    allow_adjacent: bool = False
    # Whether a region of this picture may be asked for. A lane that answers a picture
    # whole and never crops it has to say so here rather than in its prompt: ledger E100
    # deleted the prompt sentence pointing at existing_public_containers and the model
    # went there 74 times anyway in the measured 8-task decumbenine_B run (d77b930).
    # True by default, because that is how all 80 image_region reads in the production
    # replay corpus were answered, every one of them from the window lane.
    allow_region: bool = True
    region: Region | None = None
    display_pixels: int | None = None
    dimensions: Callable[[], tuple[int, int]] | None = None
    raw_text: Callable[[], str] | None = None


@dataclass(frozen=True)
class RecordTarget:
    """A pinned record location; the consumer keeps its loading, minting and acceptance."""

    perform: Callable[[str, Selection | None], ReadValue]
    with_budget: Callable[[str, Selection | None, int], ReadValue] | None = None


def estimate_image_pixels(region: Region | None, width: int, height: int) -> int:
    """Pixels a normalized-1000 region maps to on the original; a bounded overview otherwise."""
    if region is None:
        return min(width, 1024) * min(height, 1024)
    x0, y0, x1, y1 = region
    return ((width * x1 + 999) // 1000 - width * x0 // 1000) * (
        (height * y1 + 999) // 1000 - height * y0 // 1000
    )


def project_record_value(
    value: Any,
    selection: Selection | None,
    *,
    location: str,
    item_key: str | None,
    sentence_spans: SentenceSpans,
    max_bytes: int = READ_BYTES,
) -> dict[str, Any]:
    """Project an already-pointed record value; ``location`` already carries the pointer."""
    selected = value
    if (
        isinstance(selection, RecordSelection)
        and selection.item_ids
        and isinstance(value, dict)
        and "facts" in value
    ):
        selected = value["facts"]
    if isinstance(selected, str):
        return text_value(
            selected,
            selection,
            location=location,
            spans=sentence_spans(selected),
            allow_parts=True, max_bytes=max_bytes, part_bytes=READ_BYTES,
        )
    return record_value(selected, selection, location=location, item_key=item_key,
                        max_bytes=max_bytes, part_bytes=READ_BYTES)


def fragment_of(
    parent: dict[str, Any],
    projected: Mapping[str, Any],
    *,
    seal_value: Callable[[Any], dict[str, Any]],
    pointer: str,
    selection: Selection | None,
) -> dict[str, Any] | None:
    """Bind a READ projection to its own sealed bytes; directories keep the parent binding."""
    if projected["outcome"] != "READ":
        return None
    return fragment_binding(
        parent, seal_value(projected["value"]), pointer=pointer, selection=selection
    )


# Existing location families to check for an exact, authorized prefix correction.
_KNOWN_PREFIXES = (
    "source:",
    STATE_PREFIX,
    "selection:",
    "finding:",
    "seen:",
    "result:",
    "search:",
    "main:",
)


def _source_option(
    target: SourceTarget,
    location: str,
    selection: Selection | None,
    pointer: str,
    sentence_spans: SentenceSpans,
) -> list[dict[str, Any]]:
    """Check a source option with the real planner, without performing a read."""
    try:
        _source_plan(target, selection, pointer, sentence_spans)
    except (ValueError, KeyError, TypeError, OSError):
        return []
    return [
        {
            "location": location,
            **({"select": selection.model_dump(exclude_none=True)} if selection else {}),
        }
    ]


def _unavailable(
    root: str,
    resolve_root: Callable[[str], Any],
    selection: Selection | None,
    pointer: str,
    sentence_spans: SentenceSpans,
) -> ReadFailure:
    """Offer a correction only when the resolver actually has that target."""
    if ":" not in root:
        for prefix in _KNOWN_PREFIXES:
            try:
                target = resolve_root(prefix + root)
                if target is not None:
                    return ReadFailure(
                        "UNAVAILABLE",
                        f"No location '{root}'. '{prefix}{root}' exists -- name it in full.",
                        available_reads=(
                            _source_option(
                                target,
                                prefix + root + ("#" + pointer if pointer else ""),
                                selection,
                                pointer,
                                sentence_spans,
                            )
                            if isinstance(target, SourceTarget)
                            else []
                        ),
                    )
            except (ValueError, KeyError, TypeError, OSError):
                continue
    return ReadFailure("UNAVAILABLE", "Unknown or unauthorized read location.")


def resolve_read_location(
    location: str,
    selection: Selection | None,
    *,
    task_id: str,
    store: PrivateArtifactStore | None,
    state_resources: Mapping[str, Any],
    persisted_locations: Mapping[str, Any],
    resolve_root: Callable[[str], SourceTarget | RecordTarget | None],
    sentence_spans: SentenceSpans,
) -> ReadPlan:
    """State views first, then the consumer's own root; rules below apply to every consumer."""
    if location.startswith(STATE_PREFIX):
        state_plan = resolve_state_read(
            location,
            selection,
            state_resources=state_resources,
            current_task_id=task_id,
            store=store,
            persisted_locations=persisted_locations,
        )
        if state_plan is not None:
            return state_plan
    root, _, pointer = location.partition("#")
    target = resolve_root(root)
    if target is None:
        raise _unavailable(root, resolve_root, selection, pointer, sentence_spans)
    if isinstance(target, RecordTarget):
        return ReadPlan(lambda: target.perform(pointer, selection),
                        with_budget=(lambda capacity: target.with_budget(pointer, selection, capacity))
                        if target.with_budget is not None else None)
    try:
        return _source_plan(target, selection, pointer, sentence_spans)
    except ReadFailure as error:
        if error.code in {"UNSUPPORTED_SELECTION", "INVALID_SELECTION", "TOO_LARGE"}:
            # The root has already resolved to an authorized source. Offer its own
            # default read, not a guessed range or a different source: for saved
            # selections the default preserves the accepted bounds. Omission also
            # lets a large source expose its ranges instead of refusing `whole`.
            error.available_reads = _source_option(target, root, None, "", sentence_spans)
        raise


def _source_plan(
    target: SourceTarget,
    selection: Selection | None,
    pointer: str,
    sentence_spans: SentenceSpans,
) -> ReadPlan:
    if (
        target.saved
        and isinstance(selection, WholeSelection)
        and selection.model_dump(exclude_defaults=True) == {"kind": "whole"}
    ):
        selection = None
    if pointer:
        raise ReadFailure("UNSUPPORTED_SELECTION", "Source locations use source selectors.")
    region: Region | None = None
    pixels = 0
    if target.medium == "visuals":
        if selection is not None and not isinstance(selection, (ImageSelection, WholeSelection)):
            raise ReadFailure("UNSUPPORTED_SELECTION", "This location supports image selections.")
        if isinstance(selection, ImageSelection) and not target.allow_region:
            # A lane that answers a picture whole has no crop to give. Refusing here, on
            # the side that hands the bytes over, is the only place the rule holds: the
            # declaration is byte-identical across every lane (22,115 bytes, unchanged in
            # all 199 requests of the measured run) and a prompt sentence does not hold
            # -- E100 deleted one and the model went to the deleted place 74 times.
            raise ReadFailure(
                "UNSUPPORTED_SELECTION", "This location is read whole and has no regions."
            )
        if target.dimensions is None:
            raise ReadFailure("UNAVAILABLE", "Source image dimensions are unavailable.")
        if isinstance(selection, ImageSelection):
            region = selection.region.xyxy()
        elif isinstance(selection, WholeSelection):
            # An extension may keep a saved whole selector alive (e.g. a new
            # figure question). It still names the saved source's geometry.
            region = target.region if target.saved else (0, 0, 1000, 1000)
        else:
            region = target.region
        width, height = target.dimensions()
        if region is None and target.display_pixels is not None and selection is None:
            pixels = target.display_pixels
        else:
            pixels = estimate_image_pixels(region, width, height)
    else:
        if selection is not None and not isinstance(
            selection, (TextSelection, WholeSelection, DirectorySelection)
        ):
            raise ReadFailure(
                "UNSUPPORTED_SELECTION", "This location supports sentence selections."
            )
        if (
            isinstance(selection, TextSelection)
            and selection.adjacent_blocks
            and not target.allow_adjacent
        ):
            raise ReadFailure(
                "UNSUPPORTED_SELECTION", "Select neighboring catalog blocks explicitly."
            )
        if not target.saved or selection is not None:
            if target.raw_text is None:
                raise ReadFailure("UNAVAILABLE", "No accepted readable source is available.")
            text = target.raw_text()
            preview = text_value(
                text,
                selection.model_copy(update={"adjacent_blocks": 0})
                if isinstance(selection, TextSelection)
                else selection,
                location=target.preview_location,
                spans=sentence_spans(text),
                max_bytes=PREVIEW_MAX_BYTES,
            )
            if preview["outcome"] == "DIRECTORY":
                preview.update(origin="SOURCE", **target.preview_extra)
                return ReadPlan(lambda: ReadValue(preview))
    # No receipt offers a closer look any more. It said "Read this location again with a
    # smaller region for exact pixels", and on a target whose `allow_region` is False the
    # gate above refuses exactly that read -- the Host was spending the model's turn on a
    # promise it had already decided not to keep.
    return ReadPlan(target.plan(selection, region, pixels), image_pixels=pixels)
