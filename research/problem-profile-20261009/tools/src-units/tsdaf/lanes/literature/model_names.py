"""What a model calls the route's objects: one name each, given out and taken back here.

The host identifies a compound, a step, an operation, a measurement and a material's
place by ids that are functions of content (E66): `CMP-…`, `RXB-…`, `OP-…`, `OUT-…`,
`OP-…#m1`. The views showed the model those ids where it needed to know what they were
-- a4's middle layer was shown 297 compound hashes and only a compound table at the top
to decode them with, and it never went back to a source (E195). The owner's rule
(E196): a name is whatever the model understands; the host takes back only the names it
gave; the identities stay the host's, in every receipt and every published record.

One table per task (`task_names`) gives each object the task is shown its name: the
route's compounds, steps, operations, measurements, materials and intermediates; the
documents; the blocks it may read; the passages it has been handed. The shared tool loop
shows every request through it (`Names.present`) and reads every call through it
(`Names.resolve`), so the views, the progress, the replies and the refusals speak names
without each renderer learning them, and the receipts keep identities without each
executor translating. A task recorded before names existed was shown identities: its
table is empty, and what it wrote back reads as it did.

A name is plain printed text, never a quote or a backslash, so it stands inside a JSON
string exactly as it stands in prose.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsdaf.contracts.literature.objects import experiment_containers
from tsdaf.lanes.literature.author_words import author_rendering
from tsdaf.lanes.literature.container_enrichment import (
    material_position,
    step_line,
)
from tsdaf.lanes.literature.fact_projection import compound_label
from tsdaf.lanes.literature.outcome_meanings import outcome_kind_word
from tsdaf.lanes.literature.public_objects import public_object_records

__all__ = (
    "FIELD_FAMILIES",
    "NAMING_VERSION",
    "OTHER_ID_FIELDS",
    "REFERENCE_FIELDS",
    "Namer",
    "Names",
    "UnknownReference",
    "handle",
    "minted",
    "route_names",
    "selection_name",
    "task_names",
)


class UnknownReference(ValueError):
    """An existing domain check found an unknown closed-set reference."""

    def __init__(self, field: str, value: str, allowed: Sequence[str], message: str) -> None:
        self.field = field
        self.value = value
        self.allowed = tuple(allowed)
        super().__init__(message)


# Enters every task's resources hash: a change to what the model is shown is a new stage.
# Bumped once for steps 2b, 2c and 2d together (E199): the fusion's own reference fields
# are classified here too, so this is the last naming change before a real run.
# v4 changes how recorded names are resolved back through the content window, while
# retaining the same readable strings. Recovery must not reuse a v3 transcript view.
NAMING_VERSION = "task-names-v21"

# What a name can stand for.
ROUTE, DOCUMENT, SOURCE, SELECTION = "route", "document", "source", "selection"
_ROUTE_FIELDS = (
    "about_ids",
    "object_id",
    "block_id",
    "compound_id",
    "operation_id",
    "outcome_id",
    "product_ids",
    "produced_by",
    "consumed_by",
    "referenced_operation_id",
    "scope",
    "skeleton_material_id",
    "detail_of",
    "same_measurement_as",
    "same_as",
    "known_operation_ids",
    "experiment",
    "input_compound_ids",
    "output_compound_ids",
    "operation",
    "material",
    "existing_stage_id",
    "span_block_ids",
)
# The fields a model writes a name into, and what each may name. A place to read is a
# block or a passage, read at the location the host reads it at; every other field is
# the identity itself. Every other id field a write declares belongs to another family
# (OTHER_ID_FIELDS); a test holds both to the declarations.
FIELD_FAMILIES: Mapping[str, frozenset[str]] = {
    **{name: frozenset({ROUTE}) for name in _ROUTE_FIELDS},
    "selection_ids": frozenset({SELECTION}),
    "selection_id": frozenset({SELECTION}),
    "value_selection_ids": frozenset({SELECTION}),
    "author_selection_ids": frozenset({SELECTION}),
    "document_revision_id": frozenset({DOCUMENT}),
    # Public objects are read through the same stable identity used for writes.
    # Resolve the frozen task name before read-cache/version checks, not only
    # later in the source consumer's read planner.
    "location": frozenset({SOURCE, SELECTION, ROUTE}),
    "referenced_method_location": frozenset({SOURCE}),
}
REFERENCE_FIELDS = frozenset(_ROUTE_FIELDS)
OTHER_ID_FIELDS: Mapping[str, str] = {
    # A package block id: already the block's name.
    "referenced_unit_block_id": "block",
    "text_block_ids": "block",
    "visual_ids": "block",
    # The fusion's own facts and the sources its readers cited.
    "route_group_id": "fusion",
    "fact_id": "fusion",
    "fact_ids": "fusion",
    "owners": "fusion",
    "inputs": "fusion",
    "outputs": "fusion",
    "after": "fusion",
    "subject_ids": "fusion",
    "source_ids": "fusion",
    "add_source_ids": "fusion",
    # A handle the host returned on a write or a receipt row.
    "item_id": "handle",
    "action_id": "handle",
    # A name the model chose for its own item.
    "finding_id": "chosen",
    "question_id": "chosen",
    "segment_id": "chosen",
}
_SELECTION_ROOT = "selection:"

# A host-minted identity carries a run of hex (a content hash). Only those are named: an
# id with none -- a block id, a test's "s", a name the model chose -- already reads as
# itself, is shown as itself and is taken back as itself.
_MINTED = re.compile(r"[0-9a-f]{12,}")


def minted(identity: str) -> bool:
    """Whether an id is a host-minted hash, the only kind of id that is given a name."""
    return bool(_MINTED.search(identity))


# Values under these keys are media, not text a model reads.
_MEDIA_KEYS = frozenset({"data", "image_url", "inline_data", "file_data", "file_uri"})
_UNSAFE = re.compile(r'["\\\x00-\x1f]')


def handle(text: str) -> str:
    """Printed text on one line, safe inside a JSON string: the author's words as they
    print (`author_rendering`), with a double quote as a single one and no backslash."""
    printed = " ".join(author_rendering(text).split())
    return _UNSAFE.sub("", printed.replace('"', "'"))


@dataclass(frozen=True)
class Names:
    """Identity -> name for one task, and back, each identity of one family."""

    names: Mapping[str, str]
    identities: Mapping[str, str] = field(default_factory=dict)
    families: Mapping[str, str] = field(default_factory=dict)
    _pattern: re.Pattern[str] | None = None
    # The fields a model writes a name into and what each may name: a task's own
    # families (FIELD_FAMILIES) unless the table says otherwise.
    field_families: Mapping[str, frozenset[str]] = field(default_factory=lambda: FIELD_FAMILIES)
    reserved: frozenset[str] = frozenset()

    @classmethod
    def of(
        cls,
        names: Mapping[str, str],
        families: Mapping[str, str] | None = None,
        *,
        field_families: Mapping[str, frozenset[str]] | None = None,
        reserved: Sequence[str] | None = None,
    ) -> Names:
        identities: dict[str, str] = {}
        for identity, name in names.items():
            if not name or (identity != name and _UNSAFE.search(name)):
                raise ValueError(f"not a name a model can be shown: {name!r}")
            if identities.setdefault(name, identity) != identity:
                raise ValueError(f"two objects share the name {name!r}")
        pattern = None
        # Literal public identities map to themselves only for reference lookup.
        # They need no text substitution, and may contain characters that would
        # not be safe to insert as a new alias in an already encoded JSON string.
        renamed = [identity for identity, name in names.items() if identity != name]
        if renamed:
            ordered = sorted(renamed, key=len, reverse=True)
            pattern = re.compile(
                r"(?<![\w-])(?:" + "|".join(map(re.escape, ordered)) + r")(?![\w#-])"
            )
        kinds = {identity: (families or {}).get(identity, ROUTE) for identity in names}
        return cls(
            dict(names),
            identities,
            kinds,
            pattern,
            FIELD_FAMILIES if field_families is None else field_families,
            frozenset(reserved if reserved is not None else identities),
        )

    def name_of(self, identity: str) -> str:
        return self.names[identity]

    def present(self, value: Any) -> Any:
        """``value`` with every identity this table holds shown by its name: in text, in
        keys and in values, a JSON text included. An identity it does not hold is left
        as it is."""
        if self._pattern is None:
            return value
        return self._present(value)

    def _present(self, value: Any) -> Any:
        if isinstance(value, Mapping) and value.get("type") == "function_call":
            # The model's own call, shown back as it wrote it.
            return value
        if isinstance(value, str):
            assert self._pattern is not None
            return self._pattern.sub(lambda m: self.names[m.group(0)], value)
        if isinstance(value, Mapping):
            return {
                self._present(key) if isinstance(key, str) else key: (
                    item if key in _MEDIA_KEYS else self._present(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._present(item) for item in value]
        return value

    def resolve(self, value: Any) -> Any:
        """``value`` with every name this table gave, in a field that may name it, read
        as the identity or the location the field expects. The author's words and every
        other field are left as written, and so is a name the host did not give: the
        field's own check refuses it."""
        if not self.identities:
            return value
        local_findings = (
            frozenset(
                f["finding_id"]
                for f in value.get("findings", [])
                if isinstance(f, dict) and isinstance(f.get("finding_id"), str)
            )
            if isinstance(value, Mapping)
            else frozenset()
        )
        return self._resolve(value, local_findings)

    def source_for_unregistered_selection(self, location: str) -> str | None:
        """Suggest a named source, without registering or claiming a delivered selection.

        Only this table's authorized source names and its own selection-name shape
        qualify. Saved selections continue through normal identity resolution first.
        """
        if not location.startswith(_SELECTION_ROOT):
            return None
        name = location.removeprefix(_SELECTION_ROOT)
        if name in self.identities:
            return None
        matches = [
            identity
            for identity, source_name in self.names.items()
            if self.families[identity] == SOURCE
            and re.fullmatch(
                re.escape(source_name) + r" (?:sentence [0-9]+|sentences [0-9]+-[0-9]+|whole|"
                r"region [0-9]+,[0-9]+,[0-9]+,[0-9]+)",
                name,
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _resolve(self, value: Any, local_findings: frozenset[str] = frozenset()) -> Any:
        if isinstance(value, Mapping):
            return {
                key: (
                    [self._reference(entry, frozenset({ROUTE}))
                     if isinstance(entry, str) else self._resolve(entry, local_findings)
                     for entry in item]
                    if key == "subjects" and isinstance(item, list)
                    else [self._resolve_limitation(entry) for entry in item]
                    if key == "limitations" and isinstance(item, list)
                    else item
                    if key == "scope"
                    or (key == "detail_of" and isinstance(item, str) and item in local_findings)
                    else self._reference(item, self.field_families[key])
                    if key in self.field_families
                    else self._resolve(item, local_findings)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._resolve(item, local_findings) for item in value]
        return value

    def _resolve_limitation(self, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return self._resolve(value)
        return {
            key: self._reference(item, frozenset({ROUTE, DOCUMENT, SELECTION}))
            if key == "scope"
            else self._resolve({key: item})[key]
            for key, item in value.items()
        }

    def _reference(self, value: Any, families: frozenset[str]) -> Any:
        if isinstance(value, list):
            return [self._reference(item, families) for item in value]
        if not isinstance(value, str):
            return value
        name, rooted = value, False
        if SELECTION in families and value.startswith(_SELECTION_ROOT):
            # A passage named at its location, where a place to read or a citation is
            # expected: the host reads the location's selection as the selection.
            name, rooted = value.removeprefix(_SELECTION_ROOT), True
        identity = self.identities.get(name)
        if identity is None or self.families[identity] not in families:
            return value
        if self.families[identity] == SELECTION and (rooted or SOURCE in families):
            # A place to read, or a citation written as one: the passage's location.
            return _SELECTION_ROOT + identity
        return identity


class Namer:
    """Gives each identity a name no other identity has: a name two would share takes
    its occurrence in brackets."""

    def __init__(self, seed: Names | None = None) -> None:
        self.names: dict[str, str] = dict(seed.names) if seed is not None else {}
        self.families: dict[str, str] = dict(seed.families) if seed is not None else {}
        self._taken: set[str] = set(seed.reserved) if seed is not None else set()

    @property
    def reserved(self) -> frozenset[str]:
        return frozenset(self._taken)

    def give(self, identity: str, name: str, family: str) -> str:
        if identity in self.names:
            return self.names[identity]
        if not _MINTED.search(identity):
            if identity in self.names.values():
                raise ValueError("literal identity collides with a previously issued name")
            # A literal public object still needs an entry for the read resolver.
            # Keep its spelling: replacing arbitrary literal IDs in prose would
            # also rewrite the author's words. Source/selection families retain
            # their existing path semantics rather than becoming route objects.
            self._taken.add(identity)
            if family == ROUTE:
                self.names[identity] = identity
                self.families[identity] = family
            return identity
        unique, count = name, 1
        while unique in self._taken:
            count += 1
            unique = f"{name} ({count})"
        self._taken.add(unique)
        self.names[identity] = unique
        self.families[identity] = family
        return unique


def _route(
    namer: Namer,
    machine: Mapping[str, Any],
    material_bindings: Mapping[str, str | None] | None = None,
) -> None:
    payload = machine["domain_payload"]
    unnamed = 0
    labels: dict[str, str] = {}
    for compound in payload["compounds"]:
        label = compound_label(compound)
        printed = handle(label) if label else ""
        if not printed:
            unnamed += 1
            printed = f"unnamed compound {unnamed}"
        identity = str(compound["compound_id"])
        labels[identity] = namer.give(identity, printed, ROUTE)
    for block in experiment_containers(machine):
        if "reaction_block_id" in block:
            step = namer.give(
                str(block["reaction_block_id"]), "step " + step_line(block, labels), ROUTE
            )
        else:
            step = namer.give(
                str(block["account_id"]), "experiment " + handle(str(block["title"])), ROUTE
            )
        for index, operation in enumerate(block["operations"], 1):
            operation_id = str(operation["operation_id"])
            name = namer.give(
                operation_id,
                f"{step} / operation {operation.get('sequence_index') or index}",
                ROUTE,
            )
            for stage_index, stage in enumerate(operation.get("stages", []), 1):
                namer.give(str(stage["stage_id"]), f"{name} / stage {stage_index}", ROUTE)
            materials = (operation.get("materials") or {}).get("value") or ()
            for position in range(1, len(materials) + 1):
                namer.give(
                    next(
                        (
                            alias
                            for alias, target in (material_bindings or {}).items()
                            if target == material_position(operation_id, position)
                        ),
                        material_position(operation_id, position),
                    ),
                    f"{name} / material {position}",
                    ROUTE,
                )
            intermediate = (operation.get("operation_intermediate") or {}).get("value")
            if isinstance(intermediate, Mapping) and intermediate.get("local_intermediate_id"):
                label = intermediate.get("author_label")
                printed = handle(label) if isinstance(label, str) and label.strip() else ""
                namer.give(
                    str(intermediate["local_intermediate_id"]),
                    printed or f"{name} / intermediate",
                    ROUTE,
                )
        for index, outcome in enumerate(block["reported_outcomes"], 1):
            # What it is by its kind's own word (`outcome_kind_word`), never what it
            # holds: a name holding the value moves the moment the middle layer corrects
            # it, and a model copying the old name is refused (owner ruling, 2026-09-26).
            kind = outcome.get("kind")
            word = outcome_kind_word(kind.get("value") if isinstance(kind, Mapping) else None)
            namer.give(str(outcome["outcome_id"]), f"{step} / {word} {index}", ROUTE)

    for index, card in enumerate(payload.get("characterization_records", ()), 1):
        owners = ", ".join(labels.get(identity, identity) for identity in card["compound_ids"])
        method = card.get("method") or "measurement"
        namer.give(str(card["characterization_id"]), f"{owners} / {method} card {index}", ROUTE)

    _public_object_names(namer, machine, labels)


def _public_object_names(namer: Namer, value: Any, labels: Mapping[str, str]) -> None:
    # Finish the exact inventory the read resolver uses. Established route names
    # above stay stable; titles/values are never used to identify or merge objects.
    for identity, (_, record) in public_object_records(value).items():
        if identity in namer.names:
            continue
        if "narrative_id" in record:
            title = record.get("title") or record.get("kind") or "source explanation"
            label = "account of " + handle(str(title))
        elif "route_id" in record:
            label = "route " + ", ".join(
                labels.get(c, c) for c in record.get("compound_ids", ())[-2:]
            )
        elif "target_path_id" in record:
            label = "path to " + labels.get(
                record["target_compound_id"], record["target_compound_id"]
            )
        elif "assertion_id" in record:
            label = "assertion about " + labels.get(
                record["target_compound_id"], record["target_compound_id"]
            )
        elif "outcome_id" in record:
            kind = (record.get("kind") or {}).get("value")
            label = "unassigned " + outcome_kind_word(kind)
        elif "local_intermediate_id" in record:
            label = "intermediate " + handle(str(
                record.get("author_label") or record.get("name") or "unnamed species"
            ))
        elif "unresolved_item_id" in record:
            label = "unresolved " + handle(str(
                record.get("statement") or record.get("reason_code") or "source question"
            ))
        elif "material_use_id" in record:
            label = "material use " + handle(str(record["verbatim_name"]))
        elif "conflict_id" in record:
            label = handle(
                "conflict about "
                + namer.names.get(record["target_object_id"], "reported value")
                + " " + record["field_key"].replace("_", " ")
            )
        else:
            # Current operations, stages and legacy material aliases were named
            # above. A future unhandled definition must not silently get a hash alias.
            continue
        namer.give(identity, label, ROUTE)


def selection_name(block: str, identity: Mapping[str, Any]) -> str:
    """A passage by its block and the sentences it covers, numbered as a passage shows
    them; a figure's region by its corners."""
    region = identity.get("region")
    if isinstance(region, Mapping):
        region = [region[k] for k in ("left", "top", "right", "bottom")]
    if isinstance(region, (list, tuple)):
        # The host records a region as (left, top, right, bottom).
        return f"{block} region " + ",".join(str(corner) for corner in region)
    if "start_sentence" not in identity and "end_sentence" not in identity:
        # A text passage always carries its sentences; with neither these nor a region it
        # is a figure, read whole.
        return f"{block} whole"
    start, end = int(identity.get("start_sentence", 0)), int(identity.get("end_sentence", 0))
    if end - start <= 1:
        return f"{block} sentence {start}"
    return f"{block} sentences {start}-{end - 1}"


def task_names(
    machine: Mapping[str, Any] | None,
    *,
    documents: Sequence[tuple[str, str]] = (),
    sources: Mapping[str, Mapping[str, Any]] | None = None,
    selections: Mapping[str, Mapping[str, Any]] | None = None,
    figure_names: Mapping[tuple[str, str], str] | None = None,
    seed: Names | None = None,
    material_bindings: Mapping[str, str | None] | None = None,
    public_objects: Any = None,
) -> Names:
    """The name of every object a task is shown.

    The route's: a compound as the author calls it (their number, else their name); a
    step by what goes in and what comes out, in those names (`step_line`); an operation,
    a measurement, a material's place and an intermediate by their place in their step.
    A document by its phase (``documents`` is (revision, phase) in order). A block the
    task may read (``sources``, location -> binding) by its block id. A passage it was
    handed (``selections``, id -> binding) by its block and sentences. A figure the
    route's evidence labels (``figure_names``, (revision, visual id) -> the author's
    label, "Scheme 2") by that label.
    """
    namer = Namer(seed)
    if machine is not None:
        _route(namer, machine, material_bindings)
    if public_objects is not None:
        _public_object_names(namer, public_objects, namer.names)
    phases = [phase for _, phase in documents]
    counted: dict[str, int] = {}
    for revision, phase in documents:
        counted[phase] = counted.get(phase, 0) + 1
        several = phases.count(phase) > 1
        namer.give(revision, f"{phase} {counted[phase]}" if several else phase, DOCUMENT)
    blocks: dict[tuple[str, str], str] = {}
    for location, binding in (sources or {}).items():
        key = (str(binding["document_revision_id"]), str(binding["source_id"]))
        label = (figure_names or {}).get(key)
        blocks[key] = namer.give(location, (handle(label) if label else "") or key[1], SOURCE)
    for selection_id, binding in (selections or {}).items():
        identity = binding.get("source_identity") or {}
        key = (str(identity.get("document_revision_id")), str(identity.get("source_id")))
        block = blocks.get(key, key[1])
        namer.give(selection_id, selection_name(block, identity), SELECTION)
    return Names.of(namer.names, namer.families, reserved=tuple(namer.reserved))


def route_names(machine: Mapping[str, Any] | None) -> Names:
    """The names of the route's objects alone (`task_names` with nothing else)."""
    return task_names(machine)
