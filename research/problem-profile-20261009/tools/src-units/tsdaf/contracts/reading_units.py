"""A document read once and divided into the units its readers are handed.

One model call over a cleaned document -- every kept block's full text -- answers with
a list of units: contiguous runs of blocks, each of one kind, and where the kind is a
compound's preparation, characterization or crystal structure, the author's own label
for that compound. That list is what every later reader is given: the Router the
narrative, the detail lane a step's procedure and its data by the label the route
resolved, nobody the bibliography.

The Host keeps only what it can check. A unit is a range over the package's cleaned
block order, so the block ids stay the document's own and the sentence indices inside
them stay on the original text; a unit is named by its first block and has no identity
of its own (ledger E134). The experimental cut every earlier consumer was built on is
derived from the units by one function and is not stored again.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Self, get_args

from pydantic import Field, field_validator, model_validator

from ._base import ContractModel, _sha256, _stable_string
from .package import CanonicalPaperPackage
from .partition import ExperimentalPartition, ExperimentalPartitionProvenance

READING_UNITS_MEDIA_TYPE = "application/vnd.tsdaf.reading-units+json"


class UnitKind(StrEnum):
    """What a run of blocks is for. Only the kinds a reader is chosen by are named;
    everything else is OTHER, and the model chooses from this list rather than
    inventing (ledger E61)."""

    # Introduction, results, discussion, conclusion: what the Router reads.
    NARRATIVE = "NARRATIVE"
    # Instruments, solvents, how spectra were recorded: the experimental part's preface.
    GENERAL_METHODS = "GENERAL_METHODS"
    # One compound's preparation: amounts, conditions, workup, yield.
    PROCEDURE = "PROCEDURE"
    # One compound's data: Rf, mp, rotation, IR, NMR, HRMS.
    CHARACTERIZATION = "CHARACTERIZATION"
    # X-ray: cell, refinement, coordinates, bond tables.
    CRYSTALLOGRAPHY = "CRYSTALLOGRAPHY"
    # The reference list, however the journal prints it.
    BIBLIOGRAPHY = "BIBLIOGRAPHY"
    # Front matter, contents, acknowledgements, copies of spectra, and anything else.
    OTHER = "OTHER"


# The kinds that make up a document's experimental part; the cut every earlier consumer
# reads falls at the first unit of one of these. General methods count as experimental,
# as the split stage's schema said before this contract replaced it.
EXPERIMENTAL_KINDS = frozenset(
    {
        UnitKind.GENERAL_METHODS,
        UnitKind.PROCEDURE,
        UnitKind.CHARACTERIZATION,
        UnitKind.CRYSTALLOGRAPHY,
    }
)


# The kinds a reader is handed whole: a run of prose, a compound's procedure or its
# data is one passage, and one block of it is not what the author wrote. A crystal
# structure is offered by reference and read whole only on explicit request. A
# bibliography and OTHER are not passages authorized by the experimental cut.
# Default candidate anchors and explicit read groups derive from these unit kinds.
PASSAGE_KINDS = frozenset(
    {
        UnitKind.NARRATIVE,
        UnitKind.GENERAL_METHODS,
        UnitKind.PROCEDURE,
        UnitKind.CHARACTERIZATION,
    }
)


# How an author writes two or more compounds' labels against one procedure: `9a and
# 9b`, `14a, 14b`, `14a/14b`, `14a, 14b, and 14c`. The separators, and nothing else
# about the label, are what `compound_labels` splits on; a comma absorbs the Oxford
# `and` after it (E159), so the last of three is a label and not `and 14c`.
_LABEL_SEPARATORS = re.compile(r"\s*,\s*(?:and\s+)?|\s+and\s+|\s*/\s*")


class ReadingUnit(ContractModel):
    """A contiguous run of cleaned blocks read as one thing."""

    # Every kept block from first to last in the package's cleaned order, expanded by
    # the Host from the range the model answered with. A unit's boundary is a block's:
    # if it ever had to fall on a sentence instead (first_sentence / last_sentence,
    # named after `TextSelection.start_sentence/end_sentence`), that is where it would
    # go -- not needed today (sub-block report 2026-09-21: 1 of 892 blocks, and that
    # one should be merged, not cut), so this sentence holds the place and no field does.
    block_ids: tuple[str, ...] = Field(min_length=1)
    kind: UnitKind
    # The author's printed label for the compound this unit makes, characterizes or
    # crystallizes, as printed -- `13`, `14a`, `(±)-1`; compared through
    # `canonical_author_label`, never by string. None for a kind that names none.
    # A pair the author wrote up together stays one string here, `9a and 9b`, and
    # `compound_labels` below is how it is read as two.
    compound_label: str | None = None

    @field_validator("block_ids")
    @classmethod
    def _validate_block_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for block_id in value:
            _stable_string(block_id, label="reading unit block id")
        if len(set(value)) != len(value):
            raise ValueError("reading unit block ids must be distinct")
        return value

    @field_validator("compound_label")
    @classmethod
    def _validate_label(cls, value: str | None) -> str | None:
        return None if value is None else _stable_string(value, label="reading unit label")

    @property
    def first_block_id(self) -> str:
        """The unit's name: the id of its first block (ledger E134, decision 2)."""
        return self.block_ids[0]

    @property
    def reader_block_ids(self) -> tuple[str, ...]:
        """Default passage/candidate anchors, not the complete readable scope.

        A technical attachment offers one stable entry point, not a presumed title
        and not a candidate for every table. Its original text is read explicitly.
        """
        if self.kind in PASSAGE_KINDS:
            return self.block_ids
        if self.kind is UnitKind.CRYSTALLOGRAPHY:
            return self.block_ids[:1]
        return ()

    @property
    def is_attachment(self) -> bool:
        """An upstream technical unit offered by reference rather than pre-read."""
        return self.kind is UnitKind.CRYSTALLOGRAPHY

    @property
    def readable_block_ids(self) -> tuple[str, ...]:
        """Complete authorized unit, independent of its default presentation."""
        return self.block_ids if self.kind in PASSAGE_KINDS or self.is_attachment else ()

    @property
    def compound_labels(self) -> tuple[str, ...]:
        """Every label the unit's `compound_label` names, each as printed: one for a
        unit about one compound, two or more for a pair the author wrote up together
        (`9a and 9b`, hyperfirin's cyclooctadienones). Derived from the one string the
        model answered with: the prompt and `UnitAnswer.compound_label` tell the model to
        copy a pair's whole printed string, and this reads it as the several labels it
        names. E156 ruling 12 left both saying only "null when the unit names none"
        rather than spend a stage identity on the distinction; E161's shape hash had
        spent it by the time that wording changed. The caller compares each through
        `canonical_author_label`, as it does the whole. Empty when the unit names none."""
        if self.compound_label is None:
            return ()
        parts = tuple(part.strip() for part in _LABEL_SEPARATORS.split(self.compound_label))
        return tuple(part for part in parts if part) or (self.compound_label,)


class ReadingUnitsProvenance(ContractModel):
    stage_profile: str
    model_profile: str
    prompt_sha256: str
    schema_sha256: str
    # The cleaning the delivered text went through; part of the stage's cache identity.
    cleaning_version: str

    @field_validator("stage_profile", "model_profile", "cleaning_version")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="reading units provenance identity")

    @field_validator("prompt_sha256", "schema_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="reading units provenance hash")


class ReadingUnits(ContractModel):
    """One document's units, sealed once per package and cleaning version."""

    schema_version: Literal["tsdaf.reading-units.v1"]
    paper_id: str
    source_phase: Literal["MAIN", "SI"]
    canonical_package_sha256: str
    # In the package's cleaned order, non-overlapping, and together every kept block
    # (the Host fills what the model left out as OTHER and lists it below).
    units: tuple[ReadingUnit, ...]
    # Blocks the model's answer did not cover, filled as OTHER: an omission the Host
    # tolerates and records, unlike an overlap, which it refuses.
    uncovered_block_ids: tuple[str, ...]
    # The subset of uncovered_block_ids the request never showed the model (empty, or
    # a bare page marker; the skip in build_reading_units_request is the only place
    # that decides this). A never-shown block a claimed unit's range absorbed --
    # sandwiched between two blocks the model did claim -- is covered, not uncovered,
    # and is not named here; the unit it fell into already accounts for it. A sealed
    # run from before this field existed loads it as empty, so every one of its
    # uncovered blocks reads as unclaimed, which is the true default -- an old run
    # recorded nothing was withheld.
    not_shown_block_ids: tuple[str, ...] = ()
    # What the model was shown, so a reviewer can reproduce the decision.
    request_sha256: str
    # The model's one sentence; recorded for review, authoritative for nothing.
    rationale: str = Field(min_length=1)
    provenance: ReadingUnitsProvenance

    @field_validator("paper_id")
    @classmethod
    def _validate_paper_id(cls, value: str) -> str:
        return _stable_string(value, label="reading units paper id")

    @field_validator("canonical_package_sha256", "request_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="reading units hash")

    @model_validator(mode="after")
    def _validate_units(self) -> Self:
        if not self.rationale.strip():
            raise ValueError("reading units rationale must not be blank")
        seen: set[str] = set()
        for unit in self.units:
            if seen.intersection(unit.block_ids):
                raise ValueError("reading units overlap")
            seen.update(unit.block_ids)
        if set(self.uncovered_block_ids) - seen:
            raise ValueError("uncovered block ids must belong to a unit")
        if set(self.not_shown_block_ids) - set(self.uncovered_block_ids):
            raise ValueError("not-shown block ids must be a subset of uncovered block ids")
        return self

    @property
    def unclaimed_block_ids(self) -> tuple[str, ...]:
        """Uncovered blocks the model was shown and did not claim: the rest of
        `uncovered_block_ids` once the never-shown ones are set aside. Derived, not
        stored -- `uncovered = not_shown ∪ unclaimed` holds by construction, not by
        a second field that could drift from the first (ledger E154)."""
        not_shown = set(self.not_shown_block_ids)
        return tuple(block_id for block_id in self.uncovered_block_ids if block_id not in not_shown)

    @property
    def has_experimental_part(self) -> bool | None:
        """Known model classification; uncovered blocks do not prove absence."""
        if any(unit.kind in EXPERIMENTAL_KINDS for unit in self.units):
            return True
        return None if self.uncovered_block_ids else False

    @property
    def experimental_begins_at(self) -> str | None:
        """The first block of the first experimental unit, or None when there is none.

        Derived, never answered: one fact, one place (ledger E136, decision 1).
        """
        for unit in self.units:
            if unit.kind in EXPERIMENTAL_KINDS:
                return unit.first_block_id
        return None

    def units_for(self, compound_label: str) -> tuple[ReadingUnit, ...]:
        """Every unit claiming this label, exactly as printed, in reading order. Callers
        that compare labels the author printed differently go through
        `canonical_author_label` first."""
        return tuple(unit for unit in self.units if unit.compound_label == compound_label)

    def unit_containing(self, block_id: str) -> ReadingUnit | None:
        """The unit this block belongs to, or None for a block no unit holds. The one
        place that question is answered: the detail lane's authorization and delivery
        and the Router's replies all ask here (ledger E156)."""
        for unit in self.units:
            if block_id in unit.block_ids:
                return unit
        return None

    def validate_against(self, package: CanonicalPaperPackage) -> None:
        """Refuse units that name another package, a block cleaning dropped, or an
        order that is not the package's."""

        if type(package) is not CanonicalPaperPackage:
            raise TypeError("package must be a CanonicalPaperPackage")
        if (
            self.paper_id != package.manifest.paper_id
            or self.canonical_package_sha256 != package.canonical_content_sha256
        ):
            raise ValueError("reading units do not belong to this package")
        included = package.clean_view.included_block_ids
        position = {block_id: index for index, block_id in enumerate(included)}
        covered: list[int] = []
        for unit in self.units:
            for block_id in unit.block_ids:
                if block_id not in position:
                    raise ValueError("reading unit names a block that is not a cleaned block")
                covered.append(position[block_id])
        if covered != sorted(covered):
            raise ValueError("reading units are not in the package's cleaned order")
        if len(covered) != len(included):
            raise ValueError("reading units do not cover every cleaned block")

    def partition(self) -> ExperimentalPartition:
        """The cut every earlier consumer reads, derived from the units.

        The partition's own provenance names this stage, and its `skeleton_sha256`
        carries the request hash: the field keeps its name so partitions sealed by the
        split stage stay readable, and for a derived partition it is the hash of what
        the model was shown, which is all it ever meant.
        """
        return ExperimentalPartition(
            schema_version="tsdaf.experimental-partition.v1",
            paper_id=self.paper_id,
            source_phase=self.source_phase,
            canonical_package_sha256=self.canonical_package_sha256,
            cut_block_id=self.experimental_begins_at,
            skeleton_sha256=self.request_sha256,
            rationale=self.rationale,
            provenance=ExperimentalPartitionProvenance(
                stage_profile=self.provenance.stage_profile,
                model_profile=self.provenance.model_profile,
                prompt_sha256=self.provenance.prompt_sha256,
                schema_sha256=self.provenance.schema_sha256,
            ),
        )


def answer_shape_sha256() -> str:
    """The shape of a sealed answer, so a stage that changed it cannot serve an old one.

    A reading-units execution is sealed as ``ReadingUnits`` bytes and read back through
    the same model, and the Host verifies that the bytes are exactly what the model
    writes today (``bootstrap._canonical_model``). Adding a field therefore invalidates
    every sealed answer: it loads, but it no longer round-trips. The stage's content key
    carries this hash so that a change of shape is a change of stage -- the cached answer
    is not served, the stage runs again, and what it seals round-trips. The hash is over
    field names and their annotations, recursively; wording in a description is not part
    of it (ledger E161).
    """

    def walk(model: type[ContractModel], seen: frozenset[str]) -> list[str]:
        name = model.__name__
        if name in seen:
            return []
        rows: list[str] = []
        for field, info in model.model_fields.items():
            annotation = info.annotation
            rows.append(f"{name}.{field}:{annotation!s}")
            for nested in _contract_models(annotation):
                rows.extend(walk(nested, seen | {name}))
        return rows

    return hashlib.sha256(
        "\n".join(sorted(set(walk(ReadingUnits, frozenset())))).encode()
    ).hexdigest()


def _contract_models(annotation: object) -> tuple[type[ContractModel], ...]:
    """Every contract model reachable from one field's annotation."""

    if isinstance(annotation, type) and issubclass(annotation, ContractModel):
        return (annotation,)
    found: list[type[ContractModel]] = []
    for argument in get_args(annotation):
        found.extend(_contract_models(argument))
    return tuple(found)


def unit_reading_group(
    units: ReadingUnits | None,
    block_id: str,
    *,
    partners: Mapping[str, tuple[str, ...]] = MappingProxyType({}),
    readable: Collection[str],
    explicit: bool = False,
) -> tuple[str, ...]:
    """Which blocks go with this one when it is delivered or read: the block itself
    first, then the rest of its unit, then authorized layout-linked context.

    A step reads its passage whole (ledger E156): when the block's unit is a passage
    (`PASSAGE_KINDS`) of more than one block and every one of them is in `readable`,
    the group is the unit's whole range in the package's order. Explicit unit reads
    include all authorized members regardless of kind; default attachments stay compact.
    A unit of one block, a unit of another kind, a block no unit holds, a document never read into
    units (`units` is None) or a unit this reader may not see entire falls back to the
    block alone. Then layout context: `partners` is
    the candidate table `page_furniture.package_continuity` computed. It offers
    nearby text for reading without establishing that two blocks are one sentence
    or that any text is missing. A partner already in the group, or not readable,
    adds nothing. Whether the block may be
    read at all is not decided here: that is `readable_with_step_sources`' rule, and
    this function assumes it.
    """
    group: list[str] = [block_id]
    unit = units.unit_containing(block_id) if units is not None else None
    if (
        unit is not None
        and (unit.kind in PASSAGE_KINDS or explicit)
        and len(unit.block_ids) > 1
        and all(b in readable for b in unit.block_ids)
    ):
        group.extend(b for b in unit.block_ids if b != block_id)
    group.extend(
        half for half in partners.get(block_id, ()) if half in readable and half not in group
    )
    return tuple(group)
