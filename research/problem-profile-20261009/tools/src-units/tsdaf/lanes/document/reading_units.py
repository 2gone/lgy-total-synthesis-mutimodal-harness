"""Read a cleaned document once and divide it into the units its readers are handed.

The stage this replaces asked one question of a skeleton -- every kept block's first
140 characters -- and got one handle back: where the experimental part begins. This
asks a model that has read every kept block's full text, cleaned, to say what each
run of blocks is and, where it is a compound's preparation, data or crystal
structure, whose. The cut the old stage answered is derived from that (the first
experimental unit's first block), so every consumer of the cut is unchanged.

What the model is shown: every kept block as one row with its full cleaned text --
the page's footer removed (`remove_page_furniture`), possible page-boundary
continuations marked as layout hints (`page_break_joins`) -- and no row at all for a block that is
empty or a bare page number, which are the Host's to recognise and would each cost
the model a unit to account for. What it answers: units as ranges over block ids, so
the answer is as long as the number of units and not the number of blocks, which is
what lets one call cover a supplement of nine hundred blocks.

What the Host checks: that every block a unit names is a kept block, in the
package's order, in no more than one unit. What it does not demand: that the model
account for every block -- an omission is filled as OTHER and recorded, an overlap is
refused (ledger E136, decision 5).
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import Field

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.execution import FailureCode, TypedFailure
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.partition import ExperimentalPartition
from tsdaf.contracts.reading_units import (
    ReadingUnit,
    ReadingUnits,
    ReadingUnitsProvenance,
    UnitKind,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.document.page_furniture import (
    PAGE_FURNITURE_VERSION,
    is_page_marker,
    page_break_joins,
    page_furniture,
    remove_page_furniture,
)

__all__ = (
    "EXPERIMENTAL_HEADING_RE",
    "MAX_REQUEST_CHARS",
    "READING_UNITS_STAGE_PROFILE",
    "PreparedReadingUnitsCall",
    "ReadingUnitsAnswer",
    "ReadingUnitsResources",
    "UnitAnswer",
    "accept_reading_units_part",
    "build_reading_units_part",
    "build_reading_units_request",
    "generated_reading_units_schema_bytes",
    "has_experimental_heading",
    "load_reading_units_resources",
    "partition_clean_view",
    "resolve_reading_units_answer",
)

READING_UNITS_STAGE_PROFILE = "tsdaf.document.reading-units.v3"
REQUEST_SCHEMA_VERSION = "tsdaf.reading-units-request.v1"
# Past this many characters of cleaned text the document is refused rather than sent:
# a splitter for such a document is its own batch (E136, decision 10). The bound is
# conservative against what has been measured -- 198 main texts at most 52K tokens, five
# real supplements at most 147K characters -- and is revisited once the supplement side
# of the 200-paper corpus is measured.
MAX_REQUEST_CHARS = 400_000
_RESOURCES = (
    "prompts/reading_units_v1.md",
    "fcc2748599e19a3c115c97fbf8d60102f2b02271cbdfe39689f430792aef911f",
    "schemas/reading_units_v1.json",
    "6e3ec8a1b5175825654ae73026dc1b423490701158c0140cd8c492aa4739e0c1",
)
# How the stage is run. WHOLE_FIRST is the form every recorded answer was made in: one
# answer for the whole document, parts only when the provider cannot deliver it. WINDOWS
# divides every document part by part, each part told the units already saved, under the
# second prompt and kind definitions: the rules and principles that decide what the first
# left open (2026-10-10/11: with them five supplements divide the same way three times in
# three, the judged cases are all right, and no call runs past four minutes; under
# WHOLE_FIRST the same documents gave up to ten answers in eleven runs and Kalmanol's
# stopped at the output ceiling). A test-side switch until a whole-chain run confirms it.
READING_UNITS_FORM = "WHOLE_FIRST"
_RESOURCES_WINDOWS = (
    "prompts/reading_units_v2.md",
    "2965bec3f25293cb755bbbecd9840745ba15290d67d7cb81c078e1b0f652fa8a",
    "schemas/reading_units_v2.json",
    "97e69f12788d16efeec92d8079222750b7d002542d500d1057700370e4d4fc13",
)
# The kind definitions of the first form that contradict the second prompt's principles,
# and what they say instead. The schema stays generated from the Pydantic model; these
# sentences are the only difference.
_WINDOWS_DEFINITIONS = (
    (
        ", copies of spectra, and a screening table dumped as text.",
        ", and copies of spectra. A table of conditions screened for a reaction is not "
        "OTHER; it is PROCEDURE.",
    ),
    (
        "PROCEDURE is one compound's preparation.",
        "PROCEDURE is how something was made or tried: one compound's preparation, the "
        "conditions screened for a reaction, a protocol that prepares no labelled compound.",
    ),
    (
        "CHARACTERIZATION is one compound's data.",
        "CHARACTERIZATION is one compound's data, including a comparison of the natural "
        "and the synthetic sample of it.",
    ),
    (
        "copied as printed -- 13, 14a, (±)-1, SI-6. When",
        "copied as printed -- 7, 12b, (±)-3, SI-4. When",
    ),
    (
        "string as it stands -- `9a and 9b` -- rather than choosing one. null when the unit is "
        "about no compound the author named,",
        "string as it stands -- `5a and 5b` -- rather than choosing one. null when the unit is "
        "about no compound the author labelled,",
    ),
    (
        "diastereomers were separable, but only separated once",
        "the two isomers were separated only once",
    ),
    (
        "each run of consecutive citation blocks is a unit of its own, because the prose between "
        "them belongs to other units.",
        "each run of consecutive citation blocks that stands between two units is a unit of its "
        "own; a run that falls inside a unit (between rows of one procedure, in the middle of a "
        "sentence) is not given, and that unit's range runs across it.",
    ),
)
# What a figure row says in place of text, by block: the labels a figure-reading step
# read in it. Empty unless that step ran; a row for a figure is then shown, where an
# empty block is otherwise left out.
FIGURE_ROWS: dict[str, str] = {}


def reading_units_form() -> str:
    if READING_UNITS_FORM not in {"WHOLE_FIRST", "WINDOWS"}:
        raise ValueError("unknown reading-units form")
    return READING_UNITS_FORM


# One answer for a whole supplement asked the model to hold all of it in one reply. On
# 2026-10-10 Kalmanol's supplement (237 rows) failed four times identically: 62.9K tokens
# of thought plus 2.6K of answer reached the 65,536 output ceiling and the JSON was cut;
# Principinol B's passed within 3% of it. A document whose one answer the provider could
# not deliver is therefore divided part by part instead of being asked again unchanged.
# A part is this many characters or rows, whichever comes first, and is never cut by the
# Host inside a unit: see `accept_reading_units_part`. The one-answer call stays the first
# attempt and is sent exactly as before, so every recorded answer still replays.
PART_CHARS = 40_000
PART_ROWS = 60
# Rows alone do not say what a part costs. Over 78 parts of 21 supplements (2026-10-12) the
# thought a part took did not follow its rows (r = -0.01) or its characters (0.01); of what
# can be counted beforehand only the number of long rows did, weakly (0.46): a long row is
# a procedure or a block of data, and each is a unit to decide. Parts with more than twelve
# took 25.6K thought tokens on average and 30% of them over 35K; the others 13.9K and 11%.
# None disables the limit (the first form never had it).
PART_LONG_ROWS: int | None = None
LONG_ROW_CHARS = 600
# Said in the request of a part, not in the prompt: the prompt is the stage's identity and
# a document read in one answer never sees this.
PART_NOTE = (
    "These rows are one stretch of a longer document, in reading order: rows_before says "
    "how many rows came earlier and more_rows_follow whether the document goes on after "
    "the last row shown. Divide the rows shown exactly as instructed. When more rows "
    "follow, the last unit you give may have been cut off by the end of this stretch; give "
    "it anyway as far as the rows go -- the Host sets that last unit aside and shows its "
    "rows again at the head of the next stretch. The rationale says where the experimental "
    "part begins if it begins in these rows, or that it does not begin in them."
)
# What a later stretch is told about the earlier ones. Each stretch used to be a call
# with no memory: it could not know which compound the crystal data it opened on belonged
# to, or how this author had been labelling entries. Only what the Host has saved is
# carried -- kind, label, range -- never a reading of the text.
BEFORE_NOTE = (
    "units_before lists, in reading order, the units the earlier stretches of this document "
    "were divided into and the Host has saved: their kind, their label and their range. They "
    "are settled: do not give them again and do not change them. Use them to see what "
    "precedes the rows shown -- which compound the last entries were about, how this author "
    "labels and lays out an entry. They are not precedents: the kind and the range of each "
    "unit in the rows shown follow from the instructions, also where an earlier unit of a "
    "similar passage was given differently."
)
# The survey's own description of how this document is laid out, carried to every part.
# It is this step's working note, made from an outline, and the rows outrank it.
LAYOUT_NOTE = (
    "layout is a note made earlier from an outline of this whole document (each row's "
    "beginning only): how its entries are laid out. Use it to recognise where an entry "
    "begins and ends. It was written without the full text: where the rows shown disagree "
    "with it, the rows are right."
)
_WHITESPACE = re.compile(r"\s+")


class UnitAnswer(ContractModel):
    """One unit as the model answers it: a range, a kind, and whose it is."""

    first_block_id: str = Field(
        description="The handle (block_id) of the unit's first block, copied from the rows."
    )
    last_block_id: str = Field(
        description=(
            "The handle of the unit's last block, copied from the rows; the same as "
            "first_block_id for a unit of one block."
        )
    )
    # The response schema is the sole definition of kinds; the prompt refers to it.
    kind: UnitKind = Field(
        description=(
            "NARRATIVE is the argument: introduction, results and discussion, conclusion, and the "
            "abstract. GENERAL_METHODS is the experimental part's preface: instruments, solvents, "
            "purification, how spectra were recorded. PROCEDURE is one compound's preparation. "
            "CHARACTERIZATION is one compound's data. CRYSTALLOGRAPHY is "
            "X-ray work -- crystal data, "
            "refinement, atomic coordinates, bond lengths and angles, "
            "displacement parameters -- which "
            "usually runs for pages and is one unit per structure, labelled with the compound "
            "crystallized. BIBLIOGRAPHY is the reference list, whatever the journal calls it and "
            "however it numbers it: where the author printed it as one run it is one unit from its "
            "first entry to its last, and where the journal prints the "
            "citations as numbered notes at "
            "the foot of each page, each run of consecutive citation blocks is a unit of its own, "
            "because the prose between them belongs to other units. What "
            "makes one of those notes part "
            "of it is that it cites a work -- authors, title, journal, year. A numbered note that "
            'instead says something about this work ("diastereomers were '
            "separable, but only separated "
            'once") is not a reference and is not BIBLIOGRAPHY, whatever '
            "number it carries. Do not read "
            "past the references for the document's chemistry unless there is "
            "a procedure printed after "
            "them, which does happen when a supplement is bound into the same "
            "file. OTHER is what none "
            "of these are: title and authors, affiliations, dates received, table of contents, "
            "acknowledgements, notes on the supporting information, copies of "
            "spectra, and a screening "
            "table dumped as text."
        )
    )
    # A pair the author wrote up together arrives as the model copied it, `9a and 9b`,
    # and the contract's `ReadingUnit.compound_labels` reads it as two. Both this
    # description and the prompt used to say only "null when the unit names none",
    # which covered two different cases with one word: a general procedure written for
    # a class, where null is right, and a pair the author labelled, where the whole
    # printed string is wanted. Answering null for the second loses the pair's
    # procedure to every step that makes one of them -- `candidate_step_units` finds it
    # by label or not at all. Saying so costs a stage identity, which E156 ruling 12
    # declined to spend; the shape hash (E161) had already spent it by the time this
    # landed. A unit's boundary is a block's: a sentence-level one (first_sentence /
    # last_sentence) would be declared here, and is not needed today.
    compound_label: str | None = Field(
        description=(
            "For PROCEDURE, CHARACTERIZATION and CRYSTALLOGRAPHY: the author's own label "
            "for the compound this unit makes, characterizes or crystallizes, copied as "
            "printed -- 13, 14a, (±)-1, SI-6. When the author wrote one entry for two or "
            "more compounds and printed each of their labels, copy the whole printed "
            "string as it stands -- `9a and 9b` -- rather than choosing one. null when "
            "the unit is about no compound the author named, and always null for the "
            "other kinds."
        ),
    )


class ReadingUnitsAnswer(ContractModel):
    """The model's answer: the document as a list of units, in reading order."""

    units: list[UnitAnswer] = Field(
        min_length=1,
        description=(
            "Every unit of the document, in reading order, with no two overlapping. A "
            "block the rows do not show (empty, or a bare page number) may fall inside a "
            "unit's range without being named."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description=(
            "One sentence: where the experimental part begins and what marks it, or "
            "that there is none."
        ),
    )


@dataclass(frozen=True, slots=True)
class ReadingUnitsResources:
    prompt_text: str
    prompt_sha256: str
    schema_bytes: bytes
    schema_sha256: str

    @property
    def provider_schema(self) -> dict[str, object]:
        value: object = json.loads(self.schema_bytes)
        if not isinstance(value, dict):
            raise AssertionError("reading units provider schema lost its object shape")
        return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class PreparedReadingUnitsCall:
    """The exact call, with the rows it carries identified for the record."""

    call_kwargs_bytes: bytes
    request_sha256: str
    request_chars: int
    resources: ReadingUnitsResources
    # Kept blocks the request never showed the model (empty, or a bare page marker).
    # Not part of the payload -- request_sha256 hashes payload alone, so recorded
    # calls keep replaying byte for byte -- but the one place that decides "shown"
    # (the loop below), so a reader of an OTHER unit can tell an omission from a
    # block the model saw and did not claim.
    not_shown_block_ids: tuple[str, ...] = ()

    @property
    def call_kwargs(self) -> dict[str, object]:
        value: object = json.loads(self.call_kwargs_bytes)
        if not isinstance(value, dict):
            raise AssertionError("prepared reading units call lost its object shape")
        return cast(dict[str, object], value)


def generated_reading_units_schema_bytes(form: str = "WHOLE_FIRST") -> bytes:
    generated = canonical_json_bytes(ReadingUnitsAnswer.model_json_schema(mode="validation"))
    if form == "WHOLE_FIRST":
        return generated
    text = generated.decode("utf-8")
    for old, new in _WINDOWS_DEFINITIONS:
        if old not in text:
            raise ValueError("a kind definition the second form rewrites is no longer there")
        text = text.replace(old, new)
    return text.encode("utf-8")


def load_reading_units_resources(form: str | None = None) -> ReadingUnitsResources:
    form = reading_units_form() if form is None else form
    prompt_path, expected_prompt, schema_path, expected_schema = (
        _RESOURCES if form == "WHOLE_FIRST" else _RESOURCES_WINDOWS
    )
    root = importlib.resources.files("tsdaf.lanes.document")
    prompt_bytes = root.joinpath(*prompt_path.split("/")).read_bytes()
    try:
        prompt_text = prompt_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("reading units prompt is not strict UTF-8") from None
    if not prompt_text.strip() or prompt_text.startswith("﻿") or "\x00" in prompt_text:
        raise ValueError("reading units prompt is empty or noncanonical")
    schema_bytes = root.joinpath(*schema_path.split("/")).read_bytes()
    prompt_sha256 = hashlib.sha256(prompt_bytes).hexdigest()
    schema_sha256 = hashlib.sha256(schema_bytes).hexdigest()
    if prompt_sha256 != expected_prompt or schema_sha256 != expected_schema:
        raise ValueError("reading units resource hash does not match its frozen profile")
    if schema_bytes != generated_reading_units_schema_bytes(form):
        raise ValueError("reading units schema drifted from its Pydantic authority")
    return ReadingUnitsResources(
        prompt_text=prompt_text,
        prompt_sha256=prompt_sha256,
        schema_bytes=schema_bytes,
        schema_sha256=schema_sha256,
    )


def _collapsed(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def build_reading_units_request(
    package: CanonicalPaperPackage,
    *,
    source_phase: Literal["MAIN", "SI"],
    resources: ReadingUnitsResources | None = None,
) -> PreparedReadingUnitsCall | TypedFailure:
    """Every kept block as one row of cleaned text, or a typed refusal when there is
    too much of it for one call."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if source_phase not in {"MAIN", "SI"}:
        raise ValueError("source_phase must be MAIN or SI")
    loaded = load_reading_units_resources() if resources is None else resources
    blocks = {block.block_id: block for block in package.blocks}
    furniture = page_furniture([(b.block_id, b.raw_text) for b in package.blocks])
    cleaned = {
        b.block_id: remove_page_furniture(b.raw_text, furniture).text for b in package.blocks
    }
    continues = {
        join.upper
        for join in page_break_joins(
            [
                (b.block_id, b.page_index, b.block_type.value, cleaned[b.block_id])
                for b in package.blocks
            ]
        )
    }
    rows: list[dict[str, object]] = []
    not_shown: list[str] = []
    for block_id in package.clean_view.included_block_ids:
        text = _collapsed(cleaned[block_id])
        if not text and block_id in FIGURE_ROWS:
            text = FIGURE_ROWS[block_id]
        if not text or is_page_marker(text):
            not_shown.append(block_id)
            continue
        block = blocks[block_id]
        rows.append(
            {
                "block_id": block_id,
                "block_type": block.block_type.value,
                "page_index": block.page_index,
                "section": block.section_path[-1] if len(block.section_path) > 1 else None,
                "text": text,
                "continues_on_next": block_id in continues,
            }
        )
    request_chars = sum(len(str(row["text"])) for row in rows)
    if request_chars > MAX_REQUEST_CHARS:
        return TypedFailure(
            schema_version="tsdaf.typed-failure.v1",
            code=FailureCode.CONTEXT_TOO_LARGE,
            message=(
                f"The cleaned document is {request_chars:,} characters, past the "
                f"{MAX_REQUEST_CHARS:,} one reading-units call is allowed."
            ),
            request_id=None,
            details=(),
        )
    payload = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "paper_id": package.manifest.paper_id,
        "source_phase": source_phase,
        "furniture_removed": furniture,
        "blocks": rows,
    }
    call_kwargs = {
        "input": [
            {
                "type": "user_input",
                "content": [
                    {"type": "text", "text": canonical_json_bytes(payload).decode("utf-8")}
                ],
            }
        ],
        "system_instruction": loaded.prompt_text,
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": loaded.provider_schema,
        },
    }
    return PreparedReadingUnitsCall(
        call_kwargs_bytes=canonical_json_bytes(call_kwargs),
        request_sha256=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        request_chars=request_chars,
        resources=loaded,
        not_shown_block_ids=tuple(not_shown),
    )


def build_reading_units_part(
    prepared: PreparedReadingUnitsCall,
    start: int,
    *,
    scale: int = 1,
    whole: bool = False,
    rows_limit: int | None = None,
    before: "list[UnitAnswer] | None" = None,
    layout: str | None = None,
) -> tuple[dict[str, object], list[str], bool]:
    """The call for the rows from `start` on, the handles it shows, and whether it is the last.

    `whole` is the one-answer call for the entire document, exactly as it always was; a
    document that fits one part is the same call. `scale` widens the part, for the rare
    unit longer than one. `rows_limit` replaces the fixed number of rows a part shows (the
    caller sizes the next part from what the last one cost). `before` is the units already
    saved from earlier parts, shown so that a part is not divided without memory.
    """

    kwargs = prepared.call_kwargs
    first = cast(list[dict[str, object]], kwargs["input"])[0]
    content = cast(list[dict[str, object]], first["content"])
    payload = cast(dict[str, object], json.loads(cast(str, content[0]["text"])))
    rows = cast(list[dict[str, object]], payload["blocks"])
    if whole:
        return kwargs, [str(row["block_id"]) for row in rows], True
    end, chars, long_rows = start, 0, 0
    while end < len(rows):
        size = len(str(rows[end]["text"]))
        is_long = size >= LONG_ROW_CHARS
        if end > start and (
            end - start >= (rows_limit or PART_ROWS) * scale
            or chars + size > PART_CHARS * scale
            or (
                PART_LONG_ROWS is not None
                and rows_limit is None
                and is_long
                and long_rows >= PART_LONG_ROWS * scale
            )
        ):
            break
        chars += size
        long_rows += is_long
        end += 1
    final = end >= len(rows)
    shown = [str(row["block_id"]) for row in rows[start:end]]
    if start == 0 and final:
        return kwargs, shown, True
    said: dict[str, object] = {"rows_before": start, "more_rows_follow": not final, "note": PART_NOTE}
    if before:
        said["units_before"] = [
            {
                "kind": unit.kind.value,
                "compound_label": unit.compound_label,
                "first_block_id": unit.first_block_id,
                "last_block_id": unit.last_block_id,
            }
            for unit in before
        ]
        said["units_before_note"] = BEFORE_NOTE
    if layout:
        said["layout"] = layout
        said["layout_note"] = LAYOUT_NOTE
    part = {**payload, "blocks": rows[start:end], "part": said}
    text = canonical_json_bytes(part).decode("utf-8")
    return (
        {**kwargs, "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}]},
        shown,
        final,
    )


def accept_reading_units_part(
    shown: list[str], answer: ReadingUnitsAnswer, *, final: bool
) -> tuple[list[UnitAnswer], int] | str | None:
    """The units of one part that are whole, and how many of its rows they settle.

    A sentence when the answer cannot be used (shown to the model for its corrective
    turn). None when the part held less than one whole unit and must be widened. In a
    part that is not the last, the final unit may have been cut by the end of the rows,
    so it is set aside and its rows open the next part; the Host never decides where a
    unit ends.
    """

    position = {handle: index for index, handle in enumerate(shown)}
    ranges: list[tuple[int, int, UnitAnswer]] = []
    for unit in answer.units:
        for handle in (unit.first_block_id, unit.last_block_id):
            if handle not in position:
                return (
                    f"{handle} is not a handle in the rows shown; every first_block_id and "
                    "last_block_id must be copied from a row of this part"
                )
        first, last = position[unit.first_block_id], position[unit.last_block_id]
        if first > last:
            return (
                f"unit {unit.first_block_id}..{unit.last_block_id} ends before it begins; "
                "last_block_id must not precede first_block_id in reading order"
            )
        ranges.append((first, last, unit))
    ranges.sort(key=lambda item: item[0])
    for (_, previous_last, previous), (first, _, current) in zip(ranges, ranges[1:], strict=False):
        if first <= previous_last:
            return (
                f"units {previous.first_block_id}..{previous.last_block_id} and "
                f"{current.first_block_id}..{current.last_block_id} overlap; every block "
                "belongs to at most one unit"
            )
    if final:
        return [unit for _, _, unit in ranges], len(shown)
    if len(ranges) < 2:
        return None
    return [unit for _, _, unit in ranges[:-1]], ranges[-1][0]


def resolve_reading_units_answer(
    package: CanonicalPaperPackage,
    *,
    source_phase: Literal["MAIN", "SI"],
    answer: ReadingUnitsAnswer,
    request_sha256: str,
    model_profile: str,
    resources: ReadingUnitsResources,
    not_shown_block_ids: tuple[str, ...] = (),
) -> ReadingUnits | str:
    """Bind a validated answer to the package, or say in one sentence why it cannot be.

    The refusal sentence is what the model is shown for its one corrective turn, so it
    names the offending handle rather than a code. A gap is not a refusal: the blocks
    no unit named become OTHER units and are listed as uncovered -- some of them
    because the request never showed them (`not_shown_block_ids`, carried straight
    from the request that produced `request_sha256`), the rest because the model saw
    them and claimed none (`ReadingUnits.unclaimed_block_ids`, derived).
    """

    included = package.clean_view.included_block_ids
    position = {block_id: index for index, block_id in enumerate(included)}
    ranges: list[tuple[int, int, UnitAnswer]] = []
    for unit in answer.units:
        for handle in (unit.first_block_id, unit.last_block_id):
            if handle not in position:
                return (
                    f"{handle} is not a handle in the rows; every first_block_id and "
                    "last_block_id must be copied from a row"
                )
        first, last = position[unit.first_block_id], position[unit.last_block_id]
        if first > last:
            return (
                f"unit {unit.first_block_id}..{unit.last_block_id} ends before it begins; "
                "last_block_id must not precede first_block_id in reading order"
            )
        ranges.append((first, last, unit))
    ranges.sort(key=lambda item: item[0])
    for (_, previous_last, previous), (first, _, current) in zip(ranges, ranges[1:], strict=False):
        if first <= previous_last:
            return (
                f"units {previous.first_block_id}..{previous.last_block_id} and "
                f"{current.first_block_id}..{current.last_block_id} overlap; every block "
                "belongs to at most one unit"
            )
    units: list[ReadingUnit] = []
    uncovered: list[str] = []
    cursor = 0

    def fill(upto: int) -> None:
        nonlocal cursor
        if upto > cursor:
            gap = included[cursor:upto]
            uncovered.extend(gap)
            units.append(ReadingUnit(block_ids=gap, kind=UnitKind.OTHER, compound_label=None))
        cursor = upto

    for first, last, unit in ranges:
        fill(first)
        label = None if unit.compound_label is None else unit.compound_label.strip() or None
        units.append(
            ReadingUnit(
                block_ids=included[first : last + 1],
                kind=unit.kind,
                compound_label=label,
            )
        )
        cursor = last + 1
    fill(len(included))
    # A never-shown block a claimed unit's range absorbed is covered, not uncovered
    # (`fill` above never touched it); recording it under `not_shown_block_ids` too
    # would break the ⊆ `uncovered_block_ids` invariant the contract checks, so only
    # the ones that actually landed in a gap are kept.
    never_shown = set(not_shown_block_ids)
    not_shown_and_uncovered = tuple(block_id for block_id in uncovered if block_id in never_shown)
    return ReadingUnits(
        schema_version="tsdaf.reading-units.v1",
        paper_id=package.manifest.paper_id,
        source_phase=source_phase,
        canonical_package_sha256=package.canonical_content_sha256,
        units=tuple(units),
        uncovered_block_ids=tuple(uncovered),
        not_shown_block_ids=not_shown_and_uncovered,
        request_sha256=request_sha256,
        rationale=answer.rationale,
        provenance=ReadingUnitsProvenance(
            stage_profile=READING_UNITS_STAGE_PROFILE,
            model_profile=model_profile,
            prompt_sha256=resources.prompt_sha256,
            schema_sha256=resources.schema_sha256,
            cleaning_version=PAGE_FURNITURE_VERSION,
        ),
    )


def partition_clean_view(
    package: CanonicalPaperPackage,
    partition: ExperimentalPartition | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The cleaned blocks before the cut and from the cut on, in reading order.

    No partition (a run sealed before the cut existed, or a package no stage has
    seen) retains the historical full-package reading scope. It says nothing about
    whether the source contains experimental material. Written here once
    for every consumer; a derived partition (`ReadingUnits.partition`) and one sealed by
    the retired split stage read the same way.
    """

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    included = package.clean_view.included_block_ids
    if partition is None or partition.cut_block_id is None:
        return included, ()
    partition.validate_against(package)
    position = included.index(partition.cut_block_id)
    return included[:position], included[position:]


# A heading-derived navigation hint (ledger E76 (5), E83), not a scientific
# conclusion that a paper lacks experimental material when no heading matches.
# The sealed model cut, when present, supplies the actual reading partition.
EXPERIMENTAL_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s*)?(?:■\s*)?"
    r"(?:experimental(?: section| procedures?| details| part)?|"
    r"general (?:procedures?|methods|information|experimental(?: procedures?)?|remarks)|"
    r"materials and methods|methods|synthetic procedures?)\s*[.:]?\s*$",
    re.IGNORECASE,
)
_MARKUP_RE = re.compile(r"<[^>]+>")


def has_experimental_heading(package: CanonicalPaperPackage) -> bool:
    """Whether a short block or a section name of the cleaned document reads as an
    experimental-section heading."""
    included = set(package.clean_view.included_block_ids)
    names: set[str] = set()
    for block in package.blocks:
        if block.block_id not in included:
            continue
        names.update(name.strip() for name in block.section_path[1:])
        text = block.raw_text.strip()
        if len(text) <= 60:
            names.add(_MARKUP_RE.sub(" ", text))
    return any(EXPERIMENTAL_HEADING_RE.match(name) for name in names)
