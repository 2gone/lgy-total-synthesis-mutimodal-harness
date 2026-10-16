"""Earlier reads a model can still see, derived from the transcript under a character budget.

A read used to be shown in the one request after it and in no other. A model that has
no memory of its own then read the same location again, and again, and the loop counted
every repeat as progress. Here the transcript itself says what was read and when: the
newest of it is shown again in full until a budget is spent, the rest is degraded to a
pointer that names the location, a few words of what it said and how to read it again,
and whatever is still shown is "in context" -- a repeat of it is answered IN_CONTEXT
instead of the bytes, and is not progress.

Nothing here is a second store. The window is recomputed from ``history`` every time;
the turn split and the record shape are the driver's, a read item's name is the read
contract's, and a read reply is recognized by the shape the read executor gives it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.tool_read import ReadItem, item_name, read_batch
from tsdaf.workflow.resource_reads import (
    CONTENT_OUTCOMES,
    READ_CALL_KEY,
    READ_ITEM_KEY,
    SAME_HITS_KEY,
    InContext,
    is_read_reply,
)
from tsdaf.workflow.text_search import SEARCH_LOCATION
from tsdaf.workflow.tool_turns import recorded_reply, split_turns

# Characters, the unit the projection's own statistics count: content shown again per
# request, before the oldest reads become pointers. Independent of the read budgets,
# which bound one read in bytes.
DEFAULT_WINDOW_CHARS = 24_000
# Images shown again per request, newest first. A real paper's supplement lane read the
# same four figures five times over in twenty-six turns while they were shown for one
# request each; the figures are what it writes from. That is that lane's number, and it
# was every lane's number for as long as no lane declared one: a lane that never looks at
# a picture still inherited room for four of them. Every consumer now states its own
# `window_images`, and this is only what a consumer that states none would get.
DEFAULT_WINDOW_IMAGES = 4
HEAD_CHARS = 200
MAX_POINTERS = 20
EARLIER_READS_KEY = "earlier_reads"
READ_EARLIER_KEY = "read_earlier"
# The key under which the initial step carries reads the host delivered with the task
# (ledger E54): the items as a model would have asked for them and the rows a read
# would have answered with. They are not a turn -- no call, no result, no progress --
# and they age like the oldest reads: shown while the budget allows, then pointers.
DELIVERED_KEY = "delivered"
# Set on a delivery whose text travels inside the first message itself, which the
# projection resends whole on every request. Such a passage never leaves the model's
# context, so the window counts it as in context and never shows it again or points to
# it: on batch 4's second run a block task re-announced its 20 delivered passages, about
# 5,900 characters, on every turn, and the reading task would have re-announced 177.
# A delivery without it -- the fusion's, whose later requests strip the first message
# of its source text -- ages like the oldest reads, as the note above says.
IN_FIRST_MESSAGE = "in_first_message"
EARLIER_READS_NOTE = (
    "Previously delivered content outside this request's full-content window. "
    "The location and select identify the original request; its current value may differ. "
    "historical_read, when present, opens the recorded snapshot. A current version "
    "already shown is returned as IN_CONTEXT. first_turn counts "
    "from the first turn; 0 denotes content delivered with the task."
)

# Strings under these keys are what a source said, whichever handler delivered it; the
# rest of a payload is its envelope.
PROSE_KEYS = frozenset({"text", "caption", "value", "title"})

Versions = Callable[[str], str | None]
Identities = Callable[[Any], Any]


def no_versions(location: str) -> str | None:
    return None


def as_written(arguments: Any) -> Any:
    """Use recorded call arguments directly when a consumer has no names."""
    return arguments


@dataclass(frozen=True)
class SourceDelivery:
    """A consumer-verified view of source content actually present in a read.

    The source root is for search coverage, not a promise that a new whole-source
    request (which may include continuations) is already satisfied. Only the saved
    selection's default read is an alias of these exact delivered bytes.
    """

    location: str
    selection_location: str
    selection_id: str
    sentences: frozenset[int] = frozenset()
    complete: bool = False


EvidenceView = Callable[[dict[str, Any], Sequence[dict[str, Any]]], tuple[SourceDelivery, ...]]


def no_source_evidence(
    row: dict[str, Any], parts: Sequence[dict[str, Any]]
) -> tuple[SourceDelivery, ...]:
    return ()


def _shown_characters(part: dict[str, Any]) -> int:
    """What one transcript part will cost the request that shows it again.

    Text costs its own characters. An image travels through the transcript as an
    ``artifact_ref`` and becomes base64 only when the request is built
    (`context_projection`), so what showing it again costs is four characters for every
    three sealed bytes; a part that already carries its ``data`` is counted as it stands.
    """
    text = part.get("text")
    if isinstance(text, str):
        return len(text)
    data = part.get("data")
    if isinstance(data, str):
        return len(data)
    ref = part.get("artifact_ref")
    byte_size = ref.get("byte_size") if isinstance(ref, dict) else None
    return (byte_size + 2) // 3 * 4 if isinstance(byte_size, int) else 0


@dataclass(frozen=True)
class ReadEntry:
    """One read item as the host resolved it, and how far back it was read.

    Two different questions used to share one field. ``turns_ago`` answers "how long
    before now": it counts from the request in which the model reads this again, the
    newest turn in the history being 1, whether that turn is complete or still
    executing. Being relative, it changes every turn by design -- the control flow that
    lets the turn's own reads through, and the ``turns_ago`` a read's IN_CONTEXT reply
    and a search hit's ``read`` status quote, all mean that relative sense.

    ``first_turn`` answers "which turn read this": the turn's own position counted
    forward from the task's first turn, which is 1. Both are derived from the same
    place in the history, so as turns accrue the one rises exactly as fast as the other,
    and ``first_turn`` never changes once an entry exists. That is what the window
    renders, so an entry already shown is re-serialized byte for byte identically in
    every later request instead of having its label rewritten each turn.
    Host-delivered reads carry 0: they precede every real turn and always have.
    """

    turns_ago: int
    first_turn: int
    name: str
    location: str
    select: Any
    row: dict[str, Any]
    parts: tuple[dict[str, Any], ...]
    stale: bool
    delivered: bool = False
    # Delivered inside the first message, which every request carries whole.
    resent: bool = False
    evidence: tuple[SourceDelivery, ...] = ()

    @property
    def has_image(self) -> bool:
        return any(part.get("type") != "text" for part in self.parts)

    @property
    def image_count(self) -> int:
        return sum(part.get("type") != "text" for part in self.parts)

    @property
    def image_artifacts(self) -> frozenset[str]:
        """The sealed images this entry shows, by digest: the same crop reopened under
        another name is the same picture, and is not shown twice."""
        found = set()
        for part in self.parts:
            ref = part.get("artifact_ref")
            if isinstance(ref, dict) and isinstance(ref.get("sha256"), str):
                found.add(ref["sha256"])
        return frozenset(found)

    @property
    def is_navigation(self) -> bool:
        """A reply that says where to read rather than what a source says: a search's
        hits, or a directory page.

        Directory pages used to stay under the budget rule, as small and with nothing to
        stop a loop of re-reading them. A catalog page runs to 19,000 characters, and on
        a3 (2026-09-24) pages were 85% of everything re-sent; the same item read again
        at the same version is now marked like a search that finds the same places
        (`annotate_navigation_reply`), so a page can leave the window like the hits.
        """
        return self.location == SEARCH_LOCATION or self.row.get("outcome") == "DIRECTORY"

    def hit_contents(self) -> tuple[bytes, ...] | None:
        """Stable returned navigation content, not an assessment of scientific progress."""
        if self.location != SEARCH_LOCATION:
            return None
        value = self.row.get("value")
        hits = _navigation_hits(value)
        if not isinstance(hits, list):
            return None
        return _hit_contents(hits)

    @property
    def size(self) -> int:
        """Characters it costs to show this again, the unit the projection stats count.

        An image costs what the request will carry for it, not nothing. Only
        ``part["text"]`` used to be counted, so a supplement task that had spent all
        120,000 characters of its budget still carried four figures into the next
        request for free: decumbenine_B's nineteen overview PNGs weigh 26-207 KB each
        (mean 94 KB), so four of them are roughly 500 KB the budget said nothing about.
        """
        return len(canonical_json_bytes(self.row).decode()) + (
            len(canonical_json_bytes(snapshot_context(self)).decode()) if self.stale else 0
        ) + sum(
            _shown_characters(part) for part in self.parts
        )

    @property
    def text_size(self) -> int:
        """What this costs the character budget: its words, not its pictures.

        Pictures are budgeted by count (`max_images`). Charging a picture's base64
        characters to the same budget as prose let one figure -- hundreds of thousands
        of characters, a thousand-odd tokens -- outweigh every passage read before it and
        latch the window shut on them: hyperfirin's skeleton compile (2026-10-10), given
        room for four figures, still lost eighteen passages to pointers the moment it
        read four figures, and read them back one reply at a time.
        """
        return self.size - sum(
            _shown_characters(part) for part in self.parts if part.get("type") != "text"
        )

    @property
    def shown_as(self) -> str | None:
        """The location the reply named, when it is not the one the model asked for."""
        served = self.row.get("location")
        return served if isinstance(served, str) and served != self.location else None

    @property
    def selection_id(self) -> str | None:
        """The name this content is already citable by, when the delivery gave it one.

        A source read binds its content to a selection id and writes that id into the
        part it delivers. When the same content is asked for again the answer is
        IN_CONTEXT and delivers nothing, so without reading the id back out of what was
        delivered the model would be told "you have this already" and left with no name
        to cite it by -- which is exactly the case the Host's own pre-read creates for
        every step it reads on a task's behalf (E108).
        """
        named = self.row.get("selection_id")
        if isinstance(named, str):
            return named
        for part in self.parts:
            text = part.get("text")
            if not isinstance(text, str) or not text.startswith("{"):
                continue
            try:
                inside = json.loads(text).get("selection_id")
            except ValueError:
                continue
            if isinstance(inside, str):
                return inside
        return None

    def request(self) -> dict[str, Any]:
        """The location and select as the host read them."""
        # The shared identity retains declared lane fields such as reason/question.
        # Do not reconstruct a narrower location/select request here.
        try:
            request = json.loads(self.name)
        except ValueError:
            request = None
        if isinstance(request, dict) and request.get("location") == self.location:
            return {**request, **({"select": self.select} if self.select is not None else {})}
        select = {"select": self.select} if self.select is not None else {}
        return {"location": self.location, **select}

    def covers_matches(self, matches: Any) -> bool:
        """One recorded delivery must prove coverage; unknown is not an unread claim."""
        if self.stale or self.row.get("outcome") != "READ" or self.is_navigation:
            return False
        if not isinstance(matches, list) or not matches:
            return False
        ranges = []
        for match in matches:
            start = match.get("sentence") if isinstance(match, dict) else None
            if type(start) is not int or start < 0:
                return False
            end = match.get("end_sentence", start + 1)
            if type(end) is not int or end <= start:
                return False
            ranges.append((start, end))
        coverage = self.row.get("coverage")
        selection = self.select if isinstance(self.select, dict) else {}
        if isinstance(coverage, dict) and "start_sentence" in coverage:
            start, end = coverage.get("start_sentence"), coverage.get("end_sentence")
        elif selection.get("kind") == "text_range":
            start, end = selection.get("start_sentence", 0), selection.get("end_sentence")
        else:
            # A whole selector alone can replay a saved partial source. Only the
            # existing complete text receipt proves whole coverage here.
            value = self.row.get("value")
            return (
                isinstance(coverage, dict)
                and isinstance(value, str)
                and bool(value)
                and coverage.get("char_start") == 0
                and coverage.get("char_end") == len(value)
                and selection.get("kind") in (None, "whole")
            )
        return (
            type(start) is int
            and type(end) is int
            and 0 <= start < end
            and all(start <= first < last <= end for first, last in ranges)
        )


@dataclass(frozen=True)
class ContentWindow:
    shown: tuple[ReadEntry, ...]
    pointers: tuple[ReadEntry, ...]
    omitted_pointers: int
    in_context: dict[str, ReadEntry]
    entries: tuple[ReadEntry, ...] = ()

    def seen(self, name: str) -> InContext | None:
        """How the model can still see this item, if it can."""
        entry = self.in_context.get(name)
        if entry is not None:
            return InContext(entry.turns_ago, entry.shown_as, entry.selection_id)
        for held in self.in_context.values():
            for source in held.evidence:
                if source.complete and item_name({"location": source.selection_location}) == name:
                    return InContext(held.turns_ago, source.selection_location, source.selection_id)
        return None

    def read_status(self, location: str, matches: Any = None) -> dict[str, Any] | None:
        """Whether the model has read this location, and where that content is now.

        Anything read that is not in context counts as pointed to, whether or not its
        pointer made the bounded list: what matters to a search hit is "you read this".
        """

        def relevant(entry: ReadEntry) -> bool:
            if entry.stale:
                return False
            if isinstance(matches, list) and matches:
                required: set[int] = set()
                for match in matches:
                    first = match.get("sentence") if isinstance(match, dict) else None
                    last = match.get("end_sentence", first + 1) if type(first) is int else None
                    if type(first) is not int or type(last) is not int or not 0 <= first < last:
                        return False
                    required.update(range(first, last))
                if any(
                    source.location == location and required <= source.sentences
                    for source in entry.evidence
                ):
                    return True
            return (
                entry.location == location
                and not entry.stale
                and (matches is None or entry.covers_matches(matches))
            )

        held = [entry for entry in self.in_context.values() if relevant(entry)]
        if held:
            return {"turns_ago": min(entry.turns_ago for entry in held), "shown": True}
        read = [entry for entry in self.entries if relevant(entry)]
        if read:
            return {"turns_ago": min(entry.turns_ago for entry in read), "shown": False}
        return None


def read_entries(
    history: Sequence[dict[str, Any]],
    *,
    versions: Versions = no_versions,
    identities: Identities = as_written,
    evidence_view: EvidenceView = no_source_evidence,
    item_model: type[ContractModel] | None = None,
) -> list[ReadEntry]:
    """Every read item that delivered content, newest first: by turn, then by call;
    behind every turn, what the host delivered with the task."""
    entries: list[ReadEntry] = []
    turns = split_turns(history)
    reads = [
        (age, call, reply)
        for age, turn in enumerate(reversed(turns))
        for call in reversed(turn.calls)
        if (reply := _read_reply(call, turn.results)) is not None
    ]
    # Resolve one batch with the same Names snapshot used for the current dispatch.
    resolved = identities([call["arguments"] for _, call, _ in reads])
    tagged = [_parts_by_item(turn.parts) for turn in reversed(turns)]
    for (age, call, reply), arguments in zip(reads, resolved, strict=True):
        for item, row in _rows_of(arguments, reply, item_model=item_model):
            location = item.get("location")
            if not isinstance(location, str):
                continue
            name = item_name(item, item_model=item_model)
            parts: Sequence[dict[str, Any]] = tagged[age].get((call.get("call_id"), name), ())
            if not parts:
                parts = tagged[age].get((call.get("call_id"), item_name(item)), ())
            current = versions(location)
            entries.append(
                ReadEntry(
                    turns_ago=age + 1,
                    first_turn=len(turns) - age,
                    name=name,
                    location=location,
                    select=item.get("select"),
                    row=row,
                    parts=tuple(parts),
                    stale=current is not None and row.get("version") != current,
                    evidence=evidence_view(row, parts),
                )
            )
    delivered = history[0].get(DELIVERED_KEY) if history else None
    if isinstance(delivered, dict):
        resent = delivered.get(IN_FIRST_MESSAGE) is True
        reply = delivered.get("reply")
        try:
            batch: tuple[Any, ...] = read_batch(
                {"items": delivered.get("items")}, item_model=item_model
            )
        except ValueError:
            batch = ()
        rows = reply.get("items") if isinstance(reply, dict) else None
        if isinstance(rows, list) and len(rows) == len(batch):
            legacy_sources = resent and not any(
                READ_ITEM_KEY in part for part in history[0].get("content", ())
            )
            for item, row in reversed(list(zip(batch, rows, strict=True))):
                if not isinstance(item, dict) or not isinstance(row, dict):
                    continue
                location = item.get("location")
                if not isinstance(location, str) or row.get("outcome") not in CONTENT_OUTCOMES:
                    continue
                parts = _delivered_parts(history[0], item, row, item_model=item_model)
                if legacy_sources and row.get("origin") == "SOURCE":
                    # Old permanent first messages had no item tags. Their complete
                    # source parts are still physically shown together: project them
                    # once through verified bindings, not by guessing parent names.
                    parts = tuple(history[0].get("content", ()))
                    legacy_sources = False
                entries.append(
                    ReadEntry(
                        turns_ago=len(turns) + 1,
                        first_turn=0,
                        name=item_name(item, item_model=item_model),
                        location=location,
                        select=item.get("select"),
                        row=row,
                        parts=(),
                        stale=(current := versions(location)) is not None
                        and row.get("version") != current,
                        delivered=True,
                        resent=resent,
                        evidence=evidence_view(row, parts),
                    )
                )
    return entries


def _delivered_parts(
    initial: dict[str, Any],
    item: dict[str, Any],
    row: dict[str, Any],
    *,
    item_model: type[ContractModel] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Use the same item tags as tool reads; old receipts can identify their own part.

    An untagged historical continuation is not guessed from an adjacent name. The
    original selection's content can still be recovered from its recorded identity.
    """
    parts = initial.get("content", ())
    tagged = tuple(
        p
        for p in parts
        if p.get(READ_ITEM_KEY) in {item_name(item), item_name(item, item_model=item_model)}
    )
    if tagged:
        return tagged
    sid = row.get("selection_id")
    if not isinstance(sid, str):
        return ()
    found = []
    for part in parts:
        text = part.get("text")
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if (isinstance(value, dict) and value.get("selection_id") == sid) or text.startswith(
            f"[{sid}]"
        ):
            found.append(part)
    return tuple(found)


def _parts_by_item(parts: Sequence[dict[str, Any]]) -> dict[tuple[Any, str], list[dict[str, Any]]]:
    """Index delivered parts by recorded call and resolved read item."""
    tagged: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for part in parts:
        name = part.get(READ_ITEM_KEY)
        if isinstance(name, str):
            tagged.setdefault((part.get(READ_CALL_KEY), name), []).append(part)
    return tagged


def _read_reply(call: dict[str, Any], results: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Get the sealed read reply for a recorded call, if present."""
    call_id = call.get("call_id")
    result = results.get(call_id) if isinstance(call_id, str) else None
    if result is None or not isinstance(call.get("arguments"), dict):
        return None
    try:
        reply = recorded_reply(result)
    except ValueError:
        return None
    return reply if is_read_reply(reply) else None


def _rows_of(
    arguments: Any, reply: dict[str, Any], *, item_model: type[ContractModel] | None = None
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair resolved read items with their recorded rows, last item first."""
    try:
        batch = read_batch(
            arguments,
            item_model=item_model,
        )
    except ValueError:
        return
    rows = reply["items"]
    if len(rows) != len(batch):
        # Historical raw batches may predate canonical deduplication. Pair the
        # actual sealed rows first; never zip a newly collapsed batch to old rows.
        original = arguments.get("items") or [
            {key: arguments[key] for key in (item_model or ReadItem).model_fields if key in arguments}
        ]
        if len(rows) != len(original):
            return
        batch = tuple(original)
    for item, row in reversed(list(zip(batch, rows, strict=True))):
        if isinstance(item, dict) and isinstance(row, dict):
            if row.get("outcome") in CONTENT_OUTCOMES:
                yield item, row


def content_window(
    history: Sequence[dict[str, Any]],
    *,
    budget: int = DEFAULT_WINDOW_CHARS,
    versions: Versions = no_versions,
    max_pointers: int = MAX_POINTERS,
    max_images: int = DEFAULT_WINDOW_IMAGES,
    identities: Identities = as_written,
    evidence_view: EvidenceView = no_source_evidence,
    item_model: type[ContractModel] | None = None,
) -> ContentWindow:
    """Decide what is shown again, what is a pointer, and what is in context.

    The newest turn is shown whole by the projection and costs nothing here; when the
    executor asks mid-turn, that is the turn it is executing, whose reads the next
    request will show whole. What the host delivered with the task is the oldest read
    of all and is budgeted like any other, from the first request on. Behind it, reads
    are kept newest first until one does not fit in ``budget`` characters; from there
    on they are pointers. A read of the same
    item as a newer one is dropped altogether. Images are budgeted by count, not
    characters: the newest ``max_images`` stay, the rest become pointers. A view
    whose version has moved remains historical context under the same budget. It
    cannot satisfy a read of the current value, or cover current search matches.

    Asked mid-turn, this predicts the next request's window: the turn's own reads cost
    nothing, and a later one can only free budget by superseding an older read of the
    same item. The one thing a later call can change is a version -- a write, or in a
    lane whose reads reseal its result, a read; a repeat answered IN_CONTEXT before that
    finds its subject gone from the next request, not misdescribed.

    Navigation -- a search's hits, a directory page -- is shown in the one request
    after it was read and pointed to afterwards, like an image: the window holds
    evidence, and a place to read is cheap to ask for again -- and navigation that lists
    what it listed before is not progress.
    """
    seen: set[str] = set()
    in_context: dict[str, ReadEntry] = {}
    shown: list[ReadEntry] = []
    pointers: list[ReadEntry] = []
    spent, full, images = 0, False, 0
    pictured: set[str] = set()
    entries = tuple(
        read_entries(
            history,
            versions=versions,
            identities=identities,
            evidence_view=evidence_view,
            item_model=item_model,
        )
    )

    def hold(entry: ReadEntry) -> None:
        if entry.stale:
            return
        in_context.setdefault(entry.name, entry)
        # A selected read can be returned at a sealed derived-record location. That
        # location names exactly the content already shown, unlike its parent record
        # or another pointer. Use the verified read reply as the identity source.
        returned = entry.row.get("location")
        if (
            entry.row.get("outcome") == "READ"
            and isinstance(returned, str)
            and returned.startswith("result:")
            and returned != entry.location
        ):
            in_context.setdefault(item_name({"location": returned}), entry)

    for entry in entries:
        if entry.name in seen:
            continue
        seen.add(entry.name)
        if entry.resent:
            hold(entry)
            continue
        if entry.turns_ago == 1 and not entry.delivered:
            hold(entry)
            pictured |= entry.image_artifacts
            continue
        full = full or spent + entry.text_size > budget
        pictures = entry.image_artifacts
        already = bool(pictures) and pictures <= pictured
        crowded = entry.has_image and images + entry.image_count > max_images
        if entry.is_navigation or full or crowded or already:
            pointers.append(entry)
            continue
        spent += entry.text_size
        images += entry.image_count
        pictured |= pictures
        shown.append(entry)
        hold(entry)
    omitted = max(0, len(pointers) - max_pointers)
    return ContentWindow(
        shown=tuple(reversed(shown)),
        pointers=tuple(reversed(pointers[:max_pointers])),
        omitted_pointers=omitted,
        in_context=in_context,
        entries=entries,
    )


def _navigation_hits(value: Any) -> list[Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("hits"), list):
        return None
    return [
        *value["hits"],
        *value.get("named_sources", []),
        *([value["directory"]] if isinstance(value.get("directory"), dict) else []),
    ]


def _hit_contents(hits: list[Any]) -> tuple[bytes, ...]:
    """Compare the delivered evidence; read hints vary with the current window."""
    return tuple(
        sorted(
            canonical_json_bytes({key: value for key, value in hit.items() if key != "read"})
            for hit in hits
            if isinstance(hit, dict)
        )
    )


def annotate_navigation_reply(
    reply: dict[str, Any],
    window: ContentWindow,
    items: Sequence[Any],
    *,
    item_model: type[ContractModel] | None = None,
) -> None:
    """Say, on each navigation row, what the model already knows about what it lists.

    ``items`` are the read items as the model asked for them, one per row. Every search
    hit is a place; the ones the model has read are marked with how long ago and
    whether that content is still shown or has become a pointer, so a model that lost
    sight of a source reads it back rather than searching for it again. A search whose
    hits an earlier search already produced, or a directory page read again at the same
    version, is marked as such: it is shown, but nothing new was found, and progress is
    counted accordingly.
    """
    rows = reply.get("items", ())
    for row, item in zip(rows, items, strict=False):
        if (
            isinstance(row, dict)
            and isinstance(item, dict)
            and row.get("outcome") == "DIRECTORY"
            and row.get("location") != SEARCH_LOCATION
        ):
            name = item_name(item, item_model=item_model)
            for entry in window.entries:
                if (
                    entry.name == name
                    and entry.is_navigation
                    and entry.row.get("version") == row.get("version")
                ):
                    row[SAME_HITS_KEY] = {"turns_ago": entry.turns_ago}
                    break
    for row in rows:
        if not isinstance(row, dict) or row.get("location") != SEARCH_LOCATION:
            continue
        value = row.get("value")
        hits = _navigation_hits(value)
        if not isinstance(hits, list):
            continue
        for hit in hits:
            if isinstance(hit, dict) and isinstance(hit.get("location"), str):
                hit.pop("read", None)
                status = window.read_status(hit["location"], hit.get("matches") or [])
                if status is not None:
                    hit["read"] = status
        found = _hit_contents(hits)
        if not found:
            continue
        for entry in window.entries:
            if entry.hit_contents() == found:
                terms = entry.select.get("terms") if isinstance(entry.select, dict) else None
                row[SAME_HITS_KEY] = {"turns_ago": entry.turns_ago, "terms": terms}
                break


def snapshot_context(entry: ReadEntry) -> dict[str, Any]:
    """Describe an old read without changing its receipt or prescribing a new read."""
    if not entry.stale:
        return {}
    return {"snapshot": {
        "status": "PRIOR_VERSION",
        "note": "Content from the recorded read; the current target version is different.",
    }}


def window_steps(window: ContentWindow) -> list[dict[str, Any]]:
    """Render the window as the one user input that precedes the last turn's own steps."""
    content: list[dict[str, Any]] = []
    if window.pointers or window.omitted_pointers:
        block = {
            EARLIER_READS_KEY: {
                "note": EARLIER_READS_NOTE,
                "omitted": window.omitted_pointers,
                "items": [_pointer(entry) for entry in window.pointers],
            }
        }
        content.append({"type": "text", "text": canonical_json_bytes(block).decode()})
    noted = set()
    notes = []
    for entry in window.entries:
        event = (entry.first_turn, entry.name, entry.row.get("version"))
        if event in noted:
            continue
        noted.add(event)
        if entry.stale and (entry.resent or (entry.turns_ago == 1 and not entry.delivered)):
            notes.append({"first_turn": entry.first_turn, **entry.request(), **snapshot_context(entry)})
    if notes:
        content.append({"type": "text", "text": canonical_json_bytes({"read_snapshot_notes": notes}).decode()})
    for entry in window.shown:
        block = {
            READ_EARLIER_KEY: {
                "first_turn": entry.first_turn,
                # The host delivered this with the task; no model asked for it.
                **({"delivered_by_host": True} if entry.delivered else {}),
                **entry.request(),
                "reply": entry.row,
                **snapshot_context(entry),
            }
        }
        content.append({"type": "text", "text": canonical_json_bytes(block).decode()})
        content.extend(entry.parts)
    if not content:
        return []
    return [{"type": "user_input", "content": content}]


def _pointer(entry: ReadEntry) -> dict[str, Any]:
    pointer: dict[str, Any] = {
        "first_turn": entry.first_turn,
        **({"delivered_by_host": True} if entry.delivered else {}),
        **entry.request(),
        "outcome": entry.row.get("outcome"),
        **snapshot_context(entry),
    }
    if entry.shown_as:
        pointer["shown_as"] = entry.shown_as
        # Returned result fragments already incorporate the original selector.
        # Versioned state locations instead still name the original state object.
        if entry.shown_as.startswith("result:"):
            pointer["historical_read"] = (
                {"location": entry.shown_as}
                if entry.row.get("outcome") == "READ"
                else {**entry.request(), "location": entry.shown_as}
            )
        elif entry.shown_as.startswith("state:") and "@" in entry.shown_as:
            pointer["historical_read"] = {**entry.request(), "location": entry.shown_as}
    if entry.location == SEARCH_LOCATION:
        # The places a search found are what it was for; a pointer that kept only a few
        # words of them sent a live model back to search again for the same places.
        value = entry.row.get("value")
        hits = value.get("hits") if isinstance(value, dict) else None
        if isinstance(hits, list):
            pointer["hits"] = [
                hit["location"]
                for hit in hits
                if isinstance(hit, dict) and isinstance(hit.get("location"), str)
            ]
        if isinstance(value, dict) and value.get("named_sources"):
            pointer["named_sources"] = value["named_sources"]
            for key in ("named_sources_total", "directory", "navigation"):
                if key in value:
                    pointer[key] = value[key]
    head = _head(entry)
    if head:
        pointer["head"] = head
    return pointer


def _head(entry: ReadEntry) -> str:
    """A few words of what the content said, without knowing which handler said it."""
    words: list[str] = []
    for part in entry.parts:
        text = part.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
        words.extend(_strings(payload, "text"))
    if not words:
        words.extend(_strings(entry.row, None))
    if not words and entry.row.get("outcome") == "DIRECTORY":
        entries = entry.row.get("entries")
        words.append(f"directory of {len(entries) if isinstance(entries, list) else 0} entries")
    head = " ".join(word for word in words if word)
    if len(head) <= HEAD_CHARS:
        return head
    return head[:HEAD_CHARS].rstrip() + "…"


def _strings(value: Any, key: str | None) -> Iterator[str]:
    """The prose in a payload: every string that sits under a prose key, however deep."""
    if isinstance(value, str):
        if key in PROSE_KEYS:
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, key)
    elif isinstance(value, dict):
        for child_key, child in value.items():
            yield from _strings(child, child_key)
