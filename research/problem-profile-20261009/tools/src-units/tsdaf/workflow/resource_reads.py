"""Content-neutral read execution, budgets and immutable structured projections."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast

from pydantic import ValidationError

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.tool_read import (
    DirectorySelection,
    ReadEnvelope,
    ReadItem,
    RecordSelection,
    Selection,
    TextPartSelection,
    TextSelection,
    WholeSelection,
    item_name,
    read_batch,
    canonical_read_item,
)
from tsdaf.contracts.tool_write import item_diagnostic

# What one read of a record carries, or of a text held in one. Above it a whole read of
# a record answers as a directory to page through, and of such a text as its sentence
# directory; a consumer that trims what it renders for a read that will be paged asks
# this same number. A source's own text is previewed under `read_dispatch`'s
# PREVIEW_MAX_BYTES instead.
READ_BYTES = 24_000


def _text_part_spans(text: str, max_bytes: int) -> list[tuple[int, int]]:
    """Bound exact JSON string payloads, including escaping, without changing text."""
    spans: list[tuple[int, int]] = []
    start, used = 0, 2  # JSON's enclosing quotes belong to each separate part.
    for index, char in enumerate(text):
        size = 2 if char in '"\\\b\f\n\r\t' else 6 if ord(char) < 32 else len(char.encode("utf-8"))
        if size + 2 > max_bytes:
            raise ReadFailure("TOO_LARGE", "This read budget cannot hold one text character.")
        if used + size > max_bytes:
            spans.append((start, index))
            start, used = index, 2
        used += size
    spans.append((start, len(text)))
    return spans


def _text_parts(
    text: str, selection: Selection | None, *, location: str, max_bytes: int, part_bytes: int | None = None
) -> dict[str, Any]:
    """One continuation rule for saved record fields and submitted scalar fields."""
    spans = _text_part_spans(text, max_bytes if part_bytes is None else part_bytes)

    def entry(part: int) -> dict[str, Any]:
        return {"location": location, "select": {"kind": "text_part", "part": part}}

    if isinstance(selection, TextPartSelection):
        if selection.part >= len(spans):
            raise ReadFailure(
                "INVALID_SELECTION",
                "This part is outside the offered text.",
                available_reads=[entry(0)],
            )
        left, right = spans[selection.part]
        return {
            "outcome": "READ",
            "location": location,
            "value": text[left:right],
            "coverage": {"char_start": left, "char_end": right, "char_total": len(text)},
            "part": selection.part,
            "part_count": len(spans),
            "next": entry(selection.part + 1) if selection.part + 1 < len(spans) else None,
        }
    if selection is not None and not isinstance(selection, DirectorySelection):
        raise ReadFailure(
            "TOO_LARGE",
            f"This text holds {len(canonical_json_bytes(text)):,} JSON bytes; "
            f"one part carries at most {max_bytes:,}. Follow the offered text part.",
            available_reads=[entry(0)],
        )
    page = (
        selection
        if isinstance(selection, DirectorySelection)
        else DirectorySelection(kind="directory")
    )
    entries = []
    used = 0
    for part in range(page.offset, min(len(spans), page.offset + page.limit)):
        left, right = spans[part]
        row = {
            **entry(part),
            "part": part,
            "bytes": len(canonical_json_bytes(text[left:right])),
            "coverage": {"char_start": left, "char_end": right},
        }
        size = len(canonical_json_bytes(row))
        if entries and used + size > max_bytes:
            break
        entries.append(row)
        used += size
    cursor = page.offset + len(entries)
    return {
        "outcome": "DIRECTORY",
        "location": location,
        "representation": "text",
        "part_count": len(spans),
        "entries": entries,
        "next": {
            "location": location,
            "select": {"kind": "directory", "offset": cursor, "limit": page.limit},
        }
        if cursor < len(spans)
        else None,
    }


def text_value(
    text: str,
    selection: Selection | None,
    *,
    location: str,
    spans: list[tuple[int, int]],
    max_bytes: int = READ_BYTES,
    allow_parts: bool = False,
    part_bytes: int | None = None,
) -> dict[str, Any]:
    """Read exact text using the installed content handler's versioned segmentation."""
    # Record text can be divided for transport; original sources keep their own
    # evidence-backed sentence selectors. Never offer a part their source reader
    # cannot actually deliver or cite.
    if allow_parts and (
        isinstance(selection, TextPartSelection)
        or (
            not isinstance(selection, TextSelection) and len(canonical_json_bytes(text)) > max_bytes
        )
    ):
        return _text_parts(text, selection, location=location, max_bytes=max_bytes, part_bytes=part_bytes)
    if isinstance(selection, TextSelection):
        if selection.adjacent_blocks:
            raise ReadFailure("UNSUPPORTED_SELECTION", "This text has no adjacent block catalog.")
        end = len(spans) if selection.end_sentence is None else selection.end_sentence
        start = selection.start_sentence
        if not 0 <= start < end <= len(spans):
            raise ReadFailure(
                "INVALID_SELECTION",
                f"Sentence range exceeds the offered text ({len(spans)} sentences; "
                "end is exclusive).",
                available_reads=[{"location": location}] if spans else [],
            )
        left, right = spans[start][0], spans[end - 1][1]
        value = text[left:right]
        if len(value.encode()) > max_bytes:
            raise ReadFailure(
                "TOO_LARGE",
                f"These {end - start} sentences are more than {max_bytes:,} bytes, more than "
                "one read carries; select a smaller text range.",
            )
        return {
            "outcome": "READ",
            "location": location,
            "value": value,
            "coverage": {
                "start_sentence": start,
                "end_sentence": end,
                "char_start": left,
                "char_end": right,
            },
        }
    if selection is not None and not isinstance(selection, (WholeSelection, DirectorySelection)):
        raise ReadFailure(
            "UNSUPPORTED_SELECTION",
            "This text supports sentence selections.",
            available_reads=[{"location": location}],
        )
    if isinstance(selection, DirectorySelection) or len(text.encode()) > max_bytes:
        if isinstance(selection, WholeSelection):
            raise ReadFailure(
                "TOO_LARGE",
                f"This text has {len(spans)} sentences and more than {max_bytes:,} bytes, more "
                "than one read carries; omit select to list its ranges, or select a smaller "
                "text range (kind text_range).",
            )
        page = (
            selection
            if isinstance(selection, DirectorySelection)
            else DirectorySelection(kind="directory")
        )
        entries: list[dict[str, Any]] = []
        cursor = page.offset
        while cursor < len(spans) and len(entries) < page.limit:
            end = cursor + 1
            if len(text[spans[cursor][0] : spans[cursor][1]].encode()) > max_bytes:
                entries.append(
                    {
                        "location": location,
                        "start_sentence": cursor,
                        "outcome": "UNAVAILABLE",
                        "code": "TOO_LARGE",
                        "diagnostic": (
                            "This single sentence exceeds this handler's budget; "
                            "no smaller sentence selection is available."
                        ),
                    }
                )
            else:
                while (
                    end < min(len(spans), cursor + 20)
                    and len(text[spans[cursor][0] : spans[end][1]].encode()) <= max_bytes
                ):
                    end += 1
                entries.append(
                    {
                        "location": location,
                        "select": {
                            "kind": "text_range",
                            "start_sentence": cursor,
                            "end_sentence": end,
                        },
                    }
                )
            cursor = end
        return {
            "outcome": "DIRECTORY",
            "location": location,
            "sentence_count": len(spans),
            "entries": entries,
            "next": {
                "location": location,
                "select": {"kind": "directory", "offset": cursor, "limit": page.limit},
            }
            if cursor < len(spans)
            else None,
        }
    return {
        "outcome": "READ",
        "location": location,
        "value": text,
        "coverage": {"char_start": 0, "char_end": len(text)},
    }


class ReadFailure(ValueError):
    def __init__(
        self, code: str, message: str, *, available_reads: list[dict[str, Any]] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.available_reads = available_reads or []


@dataclass(frozen=True)
class ReadBudget:
    text_bytes: int = 64_000
    image_pixels: int = 4_000_000


DEFAULT_READ_BUDGET = ReadBudget()


@dataclass
class ReadValue:
    receipt: dict[str, Any]
    content: list[dict[str, Any]] = field(default_factory=list)
    image_pixels: int = 0
    accept: Callable[[], None] = lambda: None


@dataclass
class ReadPlan:
    """Resolved and pinned before execution; never re-resolve a moving alias here."""

    perform: Callable[[], ReadValue]
    with_budget: Callable[[int], ReadValue] | None = None
    image_pixels: int = 0
    text_bytes: int = 0


# Row outcomes that delivered content the model can see; a repeat of one of these while
# it is still in the model's context adds nothing and is answered IN_CONTEXT instead.
CONTENT_OUTCOMES = frozenset({"READ", "DIRECTORY"})
# Navigation that lists what earlier navigation listed carries this key, naming it: a
# search whose hits an earlier search already produced, or a directory page read again
# at the same version. It is shown again, but nothing new was found.
SAME_HITS_KEY = "same_hits_as"


def delivered_content(row: Any) -> bool:
    """Whether one reply row put something new in front of the model.

    This is what progress means for a read, wherever it is counted: content came back,
    and it was not a search that found exactly what an earlier one had found.
    """
    return (
        isinstance(row, dict)
        and row.get("outcome") in CONTENT_OUTCOMES
        and SAME_HITS_KEY not in row
    )


# The keys under which every delivered content part names the read item it belongs to
# and the call that delivered it, so the transcript can be read back item by item without
# a second record of the batch. The executor sets the first; the loop, which knows the
# call, sets the second; the projection strips both before a model sees the part.
READ_ITEM_KEY = "read_item"
READ_CALL_KEY = "read_call"
TRANSCRIPT_TAGS = frozenset({READ_ITEM_KEY, READ_CALL_KEY})
IN_CONTEXT_MESSAGE = (
    "This content is already present in the current context and has not been resent."
)


class InContext(NamedTuple):
    """How the model can already see an item: how many requests back, and under what name.

    ``selection_id`` is the name the earlier delivery bound the content to, when it
    bound one. It travels back on the row so that "you have this already" is never also
    "and you have no way to cite it".
    """

    turns_ago: int
    shown_as: str | None
    selection_id: str | None = None


def is_read_reply(reply: Any) -> bool:
    """Whether a recorded reply is the shape ``read_many`` returns, whatever tool sent it."""
    return (
        isinstance(reply, dict)
        and isinstance(reply.get("items"), list)
        and ("write_results" not in reply)
    )


def _never_in_context(name: str) -> InContext | None:
    return None


def read_item_action_id(interaction_sha256: str, call_index: int, item_key: str) -> str:
    """Identity of one actual read action, never a scientific or cache identity."""
    return "read:" + hashlib.sha256(canonical_json_bytes(
        [interaction_sha256, call_index, item_key]
    )).hexdigest()


def read_many(
    raw: Any,
    *,
    resolve: Callable[[ContractModel], ReadPlan],
    item_model: type[ContractModel] = ReadItem,
    envelope_model: type[ContractModel] = ReadEnvelope,
    budget: ReadBudget = DEFAULT_READ_BUDGET,
    in_context: Callable[[str], InContext | None] = _never_in_context,
) -> ReadValue:
    """Validate each item independently, resolve the batch, then accept actual content.

    ``in_context`` answers, for an item's name, how the model can still see the same
    content if it can, else None. Such an item is not read again: its row says IN_CONTEXT
    and names what it was shown as, and a batch that delivered nothing is not progress.

    ``envelope_model`` parses the batch's own shape (``items``/``location``/``select``,
    plus any lane-installed ``reason``). It is generated by the same ``read_models()``
    call as the declaration the model reads and ``item_model`` validates each item
    against, so a request the declaration accepts is one this batch shape accepts too --
    there is no second, hand-written envelope to drift out of step with either.
    """
    # `envelope_model` is typed as the shared `ContractModel` base because a lane's own
    # envelope (`INVESTIGATION_READ_ENVELOPE`) is a distinct class generated at runtime;
    # `items`/`location`/`select` are the three fields every `read_models()` envelope
    # carries by construction, which mypy cannot see through the parameter's own type.
    envelope = cast(Any, envelope_model.model_validate_json(canonical_json_bytes(raw)))
    try:
        # The naming rule is the contract's, so a request accepted by the declaration is
        # accepted here, and refused with the same sentence rather than a schema complaint.
        batch = read_batch(envelope, item_model=item_model)
    except ValueError as error:
        raise ReadFailure("INVALID_READ", str(error)) from error
    plans: list[ReadPlan | None] = []
    results: list[dict[str, Any]] = []

    def failure(error: Exception, raw_item: Any = None) -> dict[str, Any]:
        """A refused row says what was refused, not only why.

        The row used to carry a code and a sentence and nothing else, so the ledger a
        task keeps of its own reads recorded a refusal with no location and no select in
        it. A model rereading that ledger could not see what it had already been turned
        down for: in the e106 round one task asked for the same location with a
        byte-identical select five times over. This row is the only place it could have
        learned otherwise, and it is the same pair the IN_CONTEXT row already names.
        """
        named = raw_item if isinstance(raw_item, dict) else {}
        location = named.get("location")
        return {
            "outcome": "REJECTED",
            **({"location": location} if isinstance(location, str) else {}),
            **({"select": named["select"]} if named.get("select") else {}),
            "code": error.code if isinstance(error, ReadFailure) else "INVALID_READ",
            **(
                {"available_reads": error.available_reads}
                if isinstance(error, ReadFailure) and error.available_reads
                else {}
            ),
            "diagnostic": (
                item_diagnostic(error, model=item_model, collection="items")
                if isinstance(error, ValidationError)
                else str(error)[:500]
            ),
        }

    names: list[str] = []
    derived_notes: list[str | None] = []
    for raw_item in batch:
        names.append(item_name(raw_item, item_model=item_model))
        derived_notes.append(None)
        try:
            item, derived = canonical_read_item(raw_item, item_model=item_model)
            derived_notes[-1] = derived
            parsed = cast(Any, item)
            seen = in_context(names[-1])
            if seen is not None:
                plans.append(None)
                results.append(
                    {
                        "outcome": "IN_CONTEXT",
                        "location": parsed.location,
                        **(
                            {"select": parsed.select.model_dump(mode="json")} if parsed.select else {}
                        ),
                        **({"shown_as": seen.shown_as} if seen.shown_as else {}),
                        **({"selection_id": seen.selection_id} if seen.selection_id else {}),
                        **({"derived": derived} if derived is not None else {}),
                        "turns_ago": seen.turns_ago,
                        "message": IN_CONTEXT_MESSAGE,
                    }
                )
                continue
            plan = resolve(item)
            plans.append(plan)
            results.append({})
        except (ValueError, KeyError, TypeError, OSError) as error:
            plans.append(None)
            results.append(failure(error, raw_item))
    def reply():
        rejected = sum(row.get("outcome") == "REJECTED" for row in results)
        held = sum(row.get("outcome") == "IN_CONTEXT" for row in results)
        outcome = ("REJECTED" if rejected == len(results) else
                   "IN_CONTEXT" if held == len(results) else
                   "PARTIAL" if rejected or held else "READ")
        receipt = {"outcome": outcome, "items": results}
        if held and not len(results)-rejected-held:
            receipt["message"] = IN_CONTEXT_MESSAGE
        return receipt

    pixels_left = budget.image_pixels
    content: list[dict[str, Any]] = []
    pending_values: dict[int, ReadValue] = {}
    for index, pending_plan in enumerate(plans):
        plan_or_none = pending_plan
        if plan_or_none is None:
            continue
        plan = plan_or_none
        # Replace this row's placeholder, rather than charging both the placeholder
        # and the delivered row. Final fit accounts for any later diagnostics.
        text_left = max(0, budget.text_bytes - len(canonical_json_bytes(reply()))
                        + len(canonical_json_bytes(results[index]))
                        - sum(len(part.get("text", "").encode()) for part in content))
        try:
            if plan.text_bytes > text_left or plan.image_pixels > pixels_left:
                # The batch's remaining budget, not the item's own size, is what refused
                # this: a model that is not told which number it overran cannot tell
                # whether to ask for less or to ask in a later turn (E108).
                raise ReadFailure(
                    "TOO_LARGE",
                    f"This item wants {plan.text_bytes:,} bytes and {plan.image_pixels:,} "
                    f"pixels; {text_left:,} bytes and {pixels_left:,} pixels are left in "
                    f"this read (total budget {budget.text_bytes:,} bytes). Select a smaller range or a directory page.",
                )
            value = plan.with_budget(text_left) if plan.with_budget is not None else plan.perform()
            receipt = value.receipt
            if derived_notes[index] is not None and "derived" not in receipt:
                receipt = {**receipt, "derived": derived_notes[index]}
            size = len(canonical_json_bytes(receipt)) + sum(
                len(part.get("text", "").encode()) for part in value.content
            )
            if size > text_left or value.image_pixels > pixels_left:
                raise ReadFailure(
                    "TOO_LARGE",
                    f"This item came back as {size:,} bytes and {value.image_pixels:,} "
                    f"pixels; {text_left:,} bytes and {pixels_left:,} pixels are left in "
                    f"this read (total budget {budget.text_bytes:,} bytes). Select fewer entries, fields or a smaller range.",
                )
            pending_values[index] = value
            results[index] = receipt
            content.extend({**part, READ_ITEM_KEY: names[index]} for part in value.content)
            text_left -= size
            pixels_left -= value.image_pixels
        except (ValueError, KeyError, TypeError, OSError) as error:
            results[index] = failure(error, batch[index])
    def assembled():
        receipt = reply()
        content = [{**part, READ_ITEM_KEY:names[i]}
                   for i, value in pending_values.items() for part in value.content]
        return ReadValue(receipt=receipt, content=content,
                         image_pixels=sum(value.image_pixels for value in pending_values.values()))

    def size_of(value):
        return len(canonical_json_bytes(value.receipt)) + sum(
            len(part.get("text", "").encode()) for part in value.content)

    def fit(committed=()):
        # Late diagnostics and selector annotations are part of the final envelope.
        # Discard only uncommitted deliveries; their immutable parts never change.
        value = assembled()
        while size_of(value) > budget.text_bytes:
            removable = [i for i in pending_values if i not in committed]
            if not removable:
                break  # Mandatory failure feedback may itself exceed a tiny budget.
            index = removable[-1]
            pending_values.pop(index)
            results[index] = failure(ReadFailure("TOO_LARGE",
                "The complete read reply exceeds its byte budget; read this item separately or select a smaller part."), batch[index])
            value = assembled()
        return value

    fit()
    committed = set()
    for index in list(pending_values):
        if index not in pending_values:
            continue
        try:
            pending_values[index].accept()
            committed.add(index)
        except (ValueError, KeyError, TypeError, OSError) as error:
            pending_values.pop(index)
            results[index] = failure(error, batch[index])
            fit(committed)
    answer = assembled()
    if size_of(answer) > budget.text_bytes:
        # Do not hide the operational error or claim the envelope met its bound.
        answer.receipt['diagnostic'] = 'Required error feedback exceeds the read byte budget.'
    return answer



def pointer_value(value: Any, path: str) -> Any:
    if (path and not path.startswith("/")) or re.search(r"~(?![01])", path):
        raise ReadFailure("INVALID_SELECTION", "Invalid JSON Pointer.")
    for part in path[1:].split("/") if path else ():
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ReadFailure("MISSING_PART", "The selected field or entry does not exist.")
    return value


# How much of a record a directory page shows of each entry. Scalars only, strings cut
# here: enough to tell a catalog entry's preview or a finding's id apart, never the
# nested body the entry's own location answers with.
HEAD_CHARS = 96


def _head(record: Mapping[str, Any]) -> dict[str, Any]:
    head: dict[str, Any] = {}
    for key, value in record.items():
        if isinstance(value, str):
            head[key] = value if len(value) <= HEAD_CHARS else value[: HEAD_CHARS - 1] + "…"
        elif isinstance(value, (int, float, bool)) or value is None:
            head[key] = value
    return head


def record_value(
    value: Any,
    selection: Selection | None,
    *,
    location: str,
    item_key: str | None = None,
    max_bytes: int = READ_BYTES,
    part_bytes: int | None = None,
) -> dict[str, Any]:
    """A reusable JSON handler; entry identity is supplied by its content consumer."""
    value = copy.deepcopy(value)
    if isinstance(selection, RecordSelection):
        if selection.path:
            value = pointer_value(value, selection.path)
            location += ("" if "#" in location else "#") + selection.path
        if selection.item_ids:
            if not isinstance(value, list) or item_key is None:
                raise ReadFailure("INVALID_SELECTION", "This location does not offer item IDs.")
            entries = {
                item[item_key]: item
                for item in value
                if isinstance(item, dict) and item_key in item
            }
            if not set(selection.item_ids) <= entries.keys():
                raise ReadFailure("MISSING_PART", "A selected entry is not offered here.")
            value = [entries[key] for key in selection.item_ids]
        if selection.fields:

            def fields(item: Any) -> dict[str, Any]:
                if not isinstance(item, dict) or not set(selection.fields) <= item.keys():
                    raise ReadFailure("MISSING_PART", "A selected field is not offered here.")
                return {key: item[key] for key in selection.fields}

            value = [fields(item) for item in value] if isinstance(value, list) else fields(value)
    elif selection is not None and not isinstance(
        selection, (WholeSelection, DirectorySelection, TextPartSelection)
    ):
        raise ReadFailure(
            "UNSUPPORTED_SELECTION",
            "This location supports record selections.",
            available_reads=[{"location": location}],
        )
    size = len(canonical_json_bytes(value))
    directory = isinstance(selection, DirectorySelection)
    if isinstance(value, str) and (
        size > max_bytes or directory or isinstance(selection, TextPartSelection)
    ):
        return _text_parts(value, selection, location=location, max_bytes=max_bytes, part_bytes=part_bytes)
    if isinstance(selection, TextPartSelection):
        raise ReadFailure(
            "UNSUPPORTED_SELECTION",
            "Text parts require an offered saved text field.",
            available_reads=[{"location": location}],
        )
    if size > max_bytes and selection is not None and not directory:
        raise ReadFailure(
            "TOO_LARGE",
            f"This holds {size:,} bytes and one read carries {max_bytes:,}; select a "
            "directory page, fewer entries or specific fields.",
        )
    if directory or size > max_bytes:
        if not isinstance(value, (dict, list)):
            raise ReadFailure(
                "TOO_LARGE",
                f"This scalar is {size:,} bytes against a {max_bytes:,}-byte read and "
                "cannot be split into record entries.",
            )
        page = (
            selection
            if isinstance(selection, DirectorySelection)
            else DirectorySelection(kind="directory")
        )
        keys = list(value) if isinstance(value, dict) else list(range(len(value)))
        directory_entries = []
        used = 0
        for key in keys[page.offset : page.offset + page.limit]:
            child = value[key]
            segment = str(key).replace("~", "~0").replace("/", "~1")
            child_location = location + ("/" if "#" in location else "#/") + segment
            # A directory exists to say what to open next, so it has to say it in the
            # words the read tool takes and say what opening it will cost. The text
            # directory beside this one has always given each entry a `select` to copy;
            # this one gave a bare location, and a model that guessed `whole` on an
            # oversized child got TOO_LARGE back with no number in it to correct by. All
            # 29 TOO_LARGE refusals of the e106 round landed on host `result:` locations
            # and not one landed on the paper's prose (E108).
            child_bytes = len(canonical_json_bytes(child))
            directory_entries.append(
                {
                    "key": key,
                    "location": child_location,
                    "bytes": child_bytes,
                    "select": {"kind": "directory"}
                    if child_bytes > max_bytes
                    else {"kind": "whole"},
                    "representation": "record"
                    if isinstance(child, (list, dict))
                    else "text"
                    if isinstance(child, str)
                    else "value",
                    **(
                        {"item_id": child[item_key]}
                        if item_key and isinstance(child, dict) and item_key in child
                        else {}
                    ),
                    # What the entry is, not only where it is: a page that listed 150
                    # catalog entries by key and byte count alone was opened one entry
                    # at a time -- 69 of the 106 catalog reads of one round (E144 ③).
                    **({"head": _head(child)} if isinstance(child, dict) else {}),
                }
            )
            # A page with heads has a size; it stops at what one read carries and
            # says where the next one starts.
            used += len(canonical_json_bytes(directory_entries[-1]))
            if used > max_bytes and len(directory_entries) > 1:
                directory_entries.pop()
                break
        shown = len(directory_entries)
        return {
            "outcome": "DIRECTORY",
            "location": location,
            "entries": directory_entries,
            "entry_count": len(keys),
            "offset": page.offset,
            "next": {
                "location": location,
                "select": {
                    "kind": "directory",
                    "offset": page.offset + shown,
                    "limit": page.limit,
                },
            }
            if page.offset + shown < len(keys)
            else None,
        }
    return {
        "outcome": "READ",
        "location": location,
        "value": value,
        "selection": selection.model_dump(mode="json") if selection else None,
    }


def project_source_directory(
    projected: dict[str, Any],
    entries: Any,
    *,
    readable_locations: Collection[str],
) -> None:
    """Keep source targets in a host-authorized navigation collection.

    Callers establish the collection's provenance; arbitrary JSON containing a
    `location` is not a source catalog. The sealed entries and generic record
    handler stay unchanged. Both explicit and size-triggered directories use this
    projection, and the read resolver still checks every target independently.
    """
    if projected["outcome"] != "DIRECTORY" or not isinstance(entries, list):
        return
    for row in projected["entries"]:
        child = entries[row["key"]]
        if (
            not isinstance(child, dict)
            or not isinstance(child.get("location"), str)
            or child["location"] not in readable_locations
            or child.get("unavailable")
        ):
            continue
        row["location"] = child["location"]
        row["select"] = None
        row["representation"] = "source"
        # Neither metadata size nor a host item handle describes this source.
        row.pop("bytes", None)
        row.pop("item_id", None)
        row.get("head", {}).pop("location", None)


def resource_binding(
    artifact_ref: dict[str, Any],
    *,
    origin: str,
    provided_to: str,
    owner_task: str | None = None,
) -> dict[str, Any]:
    """A content reference is not its provenance: preserve both independently."""
    return {
        "artifact_ref": artifact_ref,
        "root_ref": artifact_ref,
        "derivation": [],
        "origin": origin,
        "owner_task": owner_task,
        "provided_to": provided_to,
    }


def resource_location(binding: dict[str, Any]) -> str:
    return "result:" + hashlib.sha256(canonical_json_bytes(binding)).hexdigest()[:32]


def fragment_binding(
    parent: dict[str, Any],
    artifact_ref: dict[str, Any],
    *,
    pointer: str,
    selection: Selection | None,
) -> dict[str, Any]:
    steps = list(parent["derivation"])
    if pointer:
        steps.append({"path": pointer})
    if selection is not None and not isinstance(selection, WholeSelection):
        steps.append({"select": selection.model_dump(mode="json")})
    return {**parent, "artifact_ref": artifact_ref, "derivation": steps}


def resource_receipt(binding: dict[str, Any]) -> dict[str, Any]:
    return {key: binding[key] for key in ("origin", "owner_task", "provided_to", "derivation")} | {
        # A result view's version is the science of what it showed (the same digest
        # its consumer reports for "current"); anything else is versioned by its bytes.
        "version": binding.get("science_version") or binding["root_ref"]["sha256"],
        "content_version": binding["artifact_ref"]["sha256"],
    }
