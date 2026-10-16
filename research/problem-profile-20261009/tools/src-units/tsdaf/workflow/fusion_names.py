"""What the fusion is shown of the host's ids, and how its names are read back.

Every one of the 272 fusion requests of the offline suite showed host hashes (batch 2A,
2d census): the unit's tag on each fact, `seen:` tokens for each place it could read,
the work unit id. The rule is 2A's (E196): the model is shown names, the host takes
back only the names it gave, and the identities stay the host's, in every receipt and
every sealed result. Each name here is derived from what the host recorded -- the result,
the place table, the history of what the task was shown -- never from where it happened
to be shown, so it does not move between turns or when a resumed task is shown less:

- This unit's own ids. A fact, a segment, a question and a reader's citation key are
  ``<tag>:<name>``, the unit's tag (`evidence_binding.unit_tag`) on a name a model
  chose. They are the model's own names, never the host's to give: the model is shown
  each as it was written, "Yfinal", "TEXT:early", quotes and all, and a bare name written
  back is bound by the unit's tag where it is written (`name_binder`), as it has been
  since E54.
- A source the fusion read itself. Its key carries the request and the call that minted
  it; it is cited by what was read, in the form a reader's key has:
  "TEXT:MAIN-P0002-B0005 sentences 6-7".
- A place the host recorded (``seen:``), by what it holds: a reader's view by its reader,
  "text reading"; a version of the result by the order the task was first shown it,
  "version 2", and read as history, "version 2 (history)"; a fact read in a version,
  "Yfinal in version 2"; a passage the fusion read by its block and sentences; a record
  it read by what it read, "version 2 as read". A place read at a pointer is its name and
  the pointer. Two places that would share a name take their order in brackets.
- The work unit, which read receipts name as owner and recipient: its route group.

A name built from a model's text passes through the one sanitiser (`model_names.handle`):
a fact id with a quote in it is the model's own name, shown as written, but the name of
the place it was read at is a name the host gives, and a name never holds a quote.

A later task is handed a group's conclusions and may read its result (`group_names`).
There every id of the unit is what the fusion was shown, the fusion's own name for it,
and the unit is its route group; a read that selects facts of a group's result names
them so, and is bound to the group whose result it reads.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.evidence_binding import unit_tag
from tsdaf.lanes.literature.gate_models import (
    FUSION_RESULT_MEDIA,
    FusionResult,
    read_fusion_result,
)
from tsdaf.lanes.literature.model_names import Namer, Names, handle, minted, selection_name
from tsdaf.workflow.literature_fusion_actions import binder_spec, name_binder
from tsdaf.workflow.tool_turns import recorded_reply

__all__ = (
    "FIELD_FAMILIES",
    "POINTER",
    "FusionNames",
    "GroupNames",
    "at_pointer",
    "first_shown",
    "fusion_names",
    "fusion_reads",
    "group_names",
    "pointed",
    "receipt_versions",
)

SOURCE, PLACE, TASK = "fusion_source", "fusion_place", "fusion_task"
# The fields a fusion call names a source or a place in. A fact may be named at a place it
# was read, which binds the version read (E54); every such field also takes a bare name,
# which `name_binder` binds where it is written.
FIELD_FAMILIES: Mapping[str, frozenset[str]] = {
    "source_ids": frozenset({SOURCE}),
    "add_source_ids": frozenset({SOURCE}),
    "selection_id": frozenset({SOURCE}),
    "location": frozenset({PLACE}),
    "view": frozenset({PLACE}),
    **{
        name: frozenset({PLACE})
        for name in ("fact_id", "same_as", "owners", "inputs", "outputs", "after")
    },
    "fact_ids": frozenset({PLACE}),
    "subject_ids": frozenset({PLACE}),
}
# Where a bare name of this unit is bound as it is read, not where it is written: the
# items a read of a result selects are its facts (`FUSION_READ_RULE`), and a citation is
# a reader's key or a source the fusion read, each under the unit's tag.
_BOUND_ON_READ = frozenset({"item_ids", "source_ids", "add_source_ids"})
_PLACE = re.compile(r"seen:[0-9a-f]{32}")
POINTER = "#"


@dataclass(frozen=True)
class FusionNames:
    """The fusion's names: places, the sources it read and the work unit by the table;
    its own ids as it wrote them, without the unit's tag."""

    table: Names
    tag: re.Pattern[str]
    bind: Callable[[str], str]
    # Each digest a read's receipt reports, by the place it is the content of: the whole
    # read by the whole's name, what came back by the name of the place read at (`CONTENT`).
    digests: Mapping[str, str] = dataclasses.field(default_factory=dict)

    @property
    def _digest_pattern(self) -> re.Pattern[str] | None:
        if not self.digests:
            return None
        return re.compile("|".join(map(re.escape, sorted(self.digests, key=len, reverse=True))))

    def present(self, value: Any) -> Any:
        """``value`` as the model is shown it: every name the table gives, this unit's ids
        without the unit's tag, and a read's digests by the place they are the content of.

        Both passes come after the table has named what it holds: a place read at a pointer
        is the place's name and the pointer (`pointed` gave the table that name), and a
        digest a receipt reports is named by the place it belongs to, never shown raw.
        """
        named = self.table.present(value)
        return _untagged(self._digests(named), self.tag)

    def _digests(self, value: Any) -> Any:
        if not self.digests or self._digest_pattern is None:
            return value
        pattern = self._digest_pattern
        return _texts(value, lambda text: pattern.sub(lambda m: self.digests[m[0]], text))

    def resolve(self, value: Any) -> Any:
        """``value`` as the host acts on it: every name the table gave, in a field that
        may name it, read as its identity; a place named with a pointer read at that
        pointer; a bare name of this unit, where it is bound as it is read, bound."""
        return self._resolve(self.table.resolve(value))

    def _resolve(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            if key == "location" and isinstance(item, str):
                item = at_pointer(self.table, item)
            elif key in _BOUND_ON_READ and isinstance(item, list):
                item = [self.bind(name) if isinstance(name, str) else name for name in item]
            elif key == "selection_id" and isinstance(item, str):
                item = self.bind(item)
            else:
                item = self._resolve(item)
            resolved[key] = item
        return resolved


def at_pointer(table: Names, location: str) -> str:
    """A place named with a pointer, as the host reads it: the place the name was given
    to, at that pointer. A name the table did not give is left as written."""
    root, at, pointer = location.partition(POINTER)
    if not at:
        return location
    place = table.resolve({"location": root})["location"]
    return place + POINTER + pointer if place != root else location


def pointed(namer: Namer, family: str) -> None:
    """Each place of ``family`` read at a pointer: the pointer follows the name as it
    followed the place."""
    for identity in [i for i, f in namer.families.items() if f == family]:
        namer.give(identity + POINTER, namer.names[identity] + POINTER, family)


def _texts(value: Any, rewrite: Callable[[str], str]) -> Any:
    """``value`` with every key and every text rewritten, apart from the model's own
    calls, which are shown back as it wrote them."""
    if isinstance(value, Mapping) and value.get("type") == "function_call":
        return value
    if isinstance(value, str):
        return rewrite(value)
    if isinstance(value, Mapping):
        return {
            (rewrite(key) if isinstance(key, str) else key): _texts(item, rewrite)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_texts(item, rewrite) for item in value]
    return value


def _untagged(value: Any, tag: re.Pattern[str]) -> Any:
    return _texts(value, lambda text: tag.sub("", text))


def first_shown(history: Sequence[Mapping[str, Any]], token: re.Pattern[str]) -> list[str]:
    """Every ``token`` in the order the host first showed it: the task input and the
    results it recorded, each in canonical form, so a resumed history reads in the same
    order."""
    order: dict[str, None] = {}
    for step in history:
        if step.get("type") in {"user_input", "function_result"}:
            order.update(dict.fromkeys(token.findall(canonical_json_bytes(step).decode())))
    return list(order)


def _passage(selection: Mapping[str, Any]) -> str:
    """A passage the fusion read, in the words the investigation's passages are named in
    (`model_names.selection_name`): the fusion records a region by its named edges and a
    text passage by the sentences it holds, numbered as a passage shows them."""
    region = selection.get("region")
    indexes = [int(sentence["index"]) for sentence in selection.get("sentences") or ()]
    identity: Mapping[str, Any] = (
        {"region": region}
        if isinstance(region, Mapping)
        else {"start_sentence": min(indexes), "end_sentence": max(indexes) + 1}
        if indexes
        else selection
    )
    return selection_name(str(selection["canonical_source_id"]), identity)


def fusion_reads(selections: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Every source the fusion read itself, by the key its read minted, in the order it
    read them: the passage each cites.

    A source the fusion read is shown by what was read, in the form a reader's key has
    ("TEXT:MAIN-P0002-B0005 sentences 6-7"), never by the request and the call that
    minted its key. The host records each such selection as it happens
    (`evidence_binding.source_location`), so the one derivation names them here and in a
    later task (`group_names`), and a key shown once reads back to the same identity.
    """
    return {str(selection["source_id"]): selection for selection in selections}


def receipt_versions(
    resource: Mapping[str, Any], *, whole: str, read_as: str
) -> tuple[tuple[str, str], ...]:
    """The digests a read's receipt reports, each named after the place it is the content
    of: the version of the whole read by the whole's name, what came back by the name of
    the place it was read at.

    ``resource_receipt`` reports ``version`` as the science a result view showed, or the
    bytes of anything else, and ``content_version`` as the bytes that came back. Both are
    digests the model would otherwise be shown raw; they are the same two rules the
    investigation names its records' digests by, so both read this one.
    """
    digests: dict[str, str] = {str(resource["root_ref"]["sha256"]): whole}
    if resource.get("science_version") is not None:
        digests[str(resource["science_version"])] = whole
    digests[str(resource["artifact_ref"]["sha256"])] = read_as
    return tuple(digests.items())


def fusion_names(
    result: FusionResult,
    *,
    locations: Mapping[str, Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
    readers: Sequence[str],
    route_group_id: str,
    read: Mapping[str, Mapping[str, Any]] | None = None,
    seed: Names | None = None,
) -> FusionNames:
    """The fusion's names as the task stands: its result, its places, what it was shown.

    ``readers`` is the modality of each reading the task was given, by its ordinal.
    ``read`` is every source the fusion read itself (`fusion_reads`), by the key its read
    minted, in the order it read them: a key is cited by what was read, in the form a
    reader's key has, and the place it was read at takes that name without its modality.
    """
    tag = unit_tag(result.work_unit_id)
    namer = Namer(seed)
    passages = {
        str(binding["selection"]["source_id"])
        for binding in locations.values()
        if binding.get("kind") == "STANDARD_SOURCE"
    }
    # A reader's key is shown as the reader wrote it, without the unit's tag: it reads as
    # itself, and no name given here may copy it. (None of these names has a run of hex
    # in it, so none can copy a key that does.)
    for key in sorted(set(result.sources) - passages):
        own = key.removeprefix(tag)
        if not minted(own):
            namer.give(own, own, SOURCE)
    namer.give(result.work_unit_id, f"route group {route_group_id}", TASK)
    # A source the fusion read itself, in the order it read them: named by what was read,
    # in the form a reader's key has, and the place it was read at by that name without
    # its modality -- one name, given once, so both read the same and neither moves.
    for key, selection in (read or {}).items():
        modality = str(selection["modality"])
        name = namer.give(key, f"{modality}:{_passage(selection)}", SOURCE)
        for token, binding in locations.items():
            if (
                binding.get("kind") == "STANDARD_SOURCE"
                and str(binding["selection"]["source_id"]) == key
                and token not in namer.names
            ):
                namer.give(token, name.removeprefix(f"{modality}:"), PLACE)
    # A reader's view by its reader, in the order the task input lists them.
    views: dict[str, str] = {}
    for token, binding in sorted(
        (
            (token, binding)
            for token, binding in locations.items()
            if binding.get("kind") == "MODEL_WORK"
        ),
        key=lambda pair: (
            int(pair[1]["ordinal"]),
            int(pair[1].get("observation", -1)),
            int(pair[1].get("part", 0)),
            pair[0],
        ),
    ):
        reader = f"{readers[int(binding['ordinal'])].lower()} reading"
        if "scope" in binding:
            reader += ": " + handle(str(binding["scope"]))
        if "part" in binding:
            reader += f" (part {binding['part']} of {binding['parts']})"
        named = namer.give(token, reader, PLACE)
        views[str(binding["artifact_ref"]["sha256"])] = named
        if "original_ref" in binding:
            views.setdefault(str(binding["original_ref"]["sha256"]), named)
    for token, binding in locations.items():
        if binding.get("kind") == "DIRECTORY" and binding.get("label"):
            namer.give(token, str(binding["label"]), PLACE)
    numbers: dict[str, int] = {}
    # Each version was numbered where the host made it (`register_version`), so a version
    # reads the same number whatever order the task is shown them in, and a resumed task
    # reads the number back with the place. Only a place that carries no number falls back
    # to how many the names have already numbered, which keeps an older result readable.
    for binding in locations.values():
        if binding.get("kind") in {"CURRENT_RESULT", "HISTORICAL_RESULT"} and (
            "sequence" in binding
        ):
            numbers.setdefault(str(binding["result_ref"]["sha256"]), int(binding["sequence"]))
    if seed is not None:
        for token, name in seed.names.items():
            place = locations.get(token)
            found = re.fullmatch(r"version (\d+)(?: \(history\))?", name)
            if place is not None and found and "result_ref" in place:
                numbers.setdefault(str(place["result_ref"]["sha256"]), int(found.group(1)))

    def version(ref: Mapping[str, Any]) -> str:
        index = numbers.setdefault(str(ref["sha256"]), max(numbers.values(), default=0) + 1)
        return f"version {index}"

    # A state snapshot actually returned to the model may point to the immutable
    # result that existed then. Give only that registered place its ordinary version
    # name. An unshown registered result must not consume a version number.
    snapshot_refs: set[str] = set()
    for step in history:
        if step.get("type") != "function_result":
            continue
        reply = recorded_reply(dict(step))
        if not isinstance(reply, Mapping):
            continue
        for row in reply.get("items", ()):
            if not isinstance(row, Mapping) or row.get("outcome") != "READ":
                continue
            location = row.get("location")
            value = row.get("value")
            if not isinstance(location, str) or not location.startswith("state:snapshot"):
                continue
            if isinstance(value, Mapping) and isinstance(value.get("result_ref"), Mapping):
                snapshot_refs.add(str(value["result_ref"].get("sha256")))
    for token, place in locations.items():
        if (
            place.get("kind") == "CURRENT_RESULT"
            and "fact_id" not in place
            and str(place["result_ref"]["sha256"]) in snapshot_refs
        ):
            namer.give(token, version(place["result_ref"]), PLACE)

    for token in first_shown(history, _PLACE):
        place = locations.get(token)
        if place is None or token in namer.names:
            continue
        kind = place["kind"]
        if kind in {"CURRENT_RESULT", "HISTORICAL_RESULT"}:
            name = version(place["result_ref"])
            if kind == "HISTORICAL_RESULT":
                name += " (history)"
            if "fact_id" in place:
                # The fact by the name the model wrote it with, printed as a name prints.
                name = f"{handle(str(place['fact_id']).removeprefix(tag))} in {name}"
            namer.give(token, name, PLACE)
        elif kind == "STANDARD_SOURCE":
            selection = place["selection"]
            name = namer.give(token, _passage(selection), PLACE)
            namer.give(str(selection["source_id"]), f"{selection['modality']}:{name}", SOURCE)
        elif kind == "CONTENT":
            root = place["resource"]["root_ref"]
            if root["media_type"] == FUSION_RESULT_MEDIA:
                what = version(root)
            else:
                # A record read is of a result version, a reader's view or the history.
                what = views.get(str(root["sha256"]), "history")
            namer.give(token, f"{what} as read", PLACE)
    pointed(namer, PLACE)
    return FusionNames(
        Names.of(
            namer.names,
            namer.families,
            field_families=FIELD_FAMILIES,
            reserved=tuple(namer.reserved),
        ),
        re.compile(r"(?<![\w-])" + re.escape(tag)),
        name_binder(result),
        _receipt_digests(namer, locations, views, version),
    )


def _receipt_digests(
    namer: Namer,
    locations: Mapping[str, Mapping[str, Any]],
    views: Mapping[str, str],
    version: Callable[[Mapping[str, Any]], str],
) -> dict[str, str]:
    """A read's receipt reports two digests: the version of what was read, and the bytes
    that came back (`resource_reads.resource_receipt`). Each is named by the place it is
    the content of -- the same words the investigation names its records' digests by --
    so neither reaches the model as a digest. A place the host did not name is left out,
    and passes through as written."""
    digests: dict[str, str] = {}
    for token, place in locations.items():
        if place.get("kind") != "CONTENT" or token not in namer.names:
            continue
        root = place["resource"]["root_ref"]
        whole = (
            version(root)
            if root["media_type"] == FUSION_RESULT_MEDIA
            else views.get(str(root["sha256"]))
        )
        if whole is not None:
            digests[str(root["sha256"])] = whole
        if place["resource"].get("science_version") is not None:
            digests[str(place["resource"]["science_version"])] = whole or str(
                place["resource"]["science_version"]
            )
        digests[str(place["resource"]["artifact_ref"]["sha256"])] = namer.names[token]
    return digests


@dataclass(frozen=True)
class GroupNames:
    """What a later task is shown of the route groups' ids, and how it names them back."""

    units: Names
    tags: re.Pattern[str] | None
    binders: Mapping[str, Callable[[str], str]]
    binder_specs: Mapping[str, tuple[str, tuple[str, ...]]]

    def present(self, value: Any) -> Any:
        """``value`` with each unit by its route group, and every other id of a unit by
        the fusion's own name for it, without the unit's tag."""
        value = self.units.present(value)
        return value if self.tags is None else _untagged(value, self.tags)

    def resolve(self, value: Any, owner: Callable[[str], str | None]) -> Any:
        """``value`` with the facts a read selects of a group's result named as the task
        was shown them and bound to that group's (`name_binder`). ``owner`` is the unit
        whose record a location holds, if a unit's: the same name in two groups' results
        is two facts, and the location read says which."""
        value = self.units.resolve(value)
        if isinstance(value, list):
            return [self.resolve(item, owner) for item in value]
        if not isinstance(value, Mapping):
            return value
        location, select = value.get("location"), value.get("select")
        bind = self.binders.get(owner(location) or "") if isinstance(location, str) else None
        if (
            bind is not None
            and isinstance(select, Mapping)
            and isinstance(select.get("item_ids"), list)
        ):
            items = [bind(name) if isinstance(name, str) else name for name in select["item_ids"]]
            return {**value, "select": {**select, "item_ids": items}}
        return {key: self.resolve(item, owner) for key, item in value.items()}


def group_names(
    groups: Sequence[Mapping[str, Any]],
    read: Callable[[FusionResult], Mapping[str, Mapping[str, Any]]],
    *, seed: Names | None = None,
) -> GroupNames:
    """The names a later task is shown the route groups' ids by (batch 2A, 2d).

    ``groups`` are the route groups the task is handed, each with the result its fusion
    sealed. Each unit is its route group; every other id of a unit is the fusion's own
    name for it, as the fusion was shown it. A fact that became a route object is not
    shown by that object's name: several facts may have become one object, and its name
    would read back to none of them. Only host-minted units are named (`minted`).

    ``read`` is what a unit's own result says it read (`fusion_reads`): a source one
    fusion read is named by what was read, exactly as that fusion showed it, so a key
    shown in the fusion and in a later task is one name, and a later task reads it back.
    """
    units: dict[str, str] = {}
    binders: dict[str, Callable[[str], str]] = {}
    binder_specs: dict[str, tuple[str, tuple[str, ...]]] = {}
    names = Namer(seed)
    for group in groups:
        result = group.get("result")
        if not isinstance(result, Mapping) or not minted(str(result["work_unit_id"])):
            continue
        unit = str(result["work_unit_id"])
        units[unit] = names.give(
            unit, group.get("display_name") or f"route group {group['route_group_id']}", TASK
        )
        sealed = read_fusion_result(canonical_json_bytes(result))
        binders[unit] = name_binder(sealed)
        prefix, bound = binder_spec(sealed)
        binder_specs[unit] = (prefix, tuple(sorted(bound)))
        # A reader's key is the reader's own name under the unit's tag: shown without the
        # tag, and never put in a name. It is registered first, so a source the fusion
        # read that would copy its name takes its order instead -- the same order the
        # fusion itself gives them, so one key has one name in both tasks.
        for key in sorted(set(sealed.sources)):
            own = key.removeprefix(unit_tag(sealed.work_unit_id))
            if not minted(own):
                names.give(own, own, SOURCE)
        for key, selection in read(sealed).items():
            names.give(key, f"{selection['modality']}:{_passage(selection)}", SOURCE)
    tags = sorted(unit_tag(unit) for unit in units)
    named = {**units, **names.names}
    return GroupNames(
        Names.of(named, {identity: (TASK if identity in units else SOURCE) for identity in named},
                 field_families={"route_group_id": frozenset({TASK})}),
        re.compile(r"(?<![\w-])(?:" + "|".join(map(re.escape, tags)) + ")") if tags else None,
        binders,
        binder_specs,
    )
