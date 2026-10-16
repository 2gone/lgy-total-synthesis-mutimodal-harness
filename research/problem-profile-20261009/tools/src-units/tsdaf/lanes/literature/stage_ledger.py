"""The Host's private ledgers of one publication: the stretches each operation's
procedure was declared in, the procedures that elide to another, and the
whole-operation values a stretch left unassigned (ledger E157).

None of this is on the operation record. `fact_schema.json` closes every fact root
(`additionalProperties: false`), so a private key on an operation is refused the
moment the operation is validated, and the working key `_stage_key` survives only
between a receipt's routing and its finalize, where `public_note` strips it (E141).
What has to outlive that -- which stage a name in a later receipt refers to, whose
procedure an operation follows, what a stretch displaced -- lives here, on one
mutable object the publication builds per revision, hands to every receipt's
application, and hands to the public assembly last (batch 10 compiles it into the
v1.1 stages; under the v1 profile it is not published, as before).

A stage's identity is its boundary, not the name a task gave it: the site where
the author's words begin in the cited observation. Two tasks that name the same
stretch differently -- f07 wrote `post_addition` and `stage-78C` for one sentence --
are one stage, because they begin at one place; two stretches a task declared from
one paragraph are two, because the boundary is where the words begin, not which
selection they were read in (both of B000's stages cite one selection). Equality of
the boundary, not overlap of the quotes: a quote that spans two stretches (f07's
paper task quoted B002's whole sentence) would otherwise fold both into one when it
arrived first and only one when it arrived second, and an identity that depends on
the order of arrival is not a function of content.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsdaf.contracts.literature.expressions import expressed_value, merge_interpretations
from tsdaf.contracts.literature.stage_semantics import boundary_hash_of
from tsdaf.contracts.reading_units import ReadingUnits, UnitKind
from tsdaf.lanes.literature.author_words import comparison_text, find_quote, matching_text
from tsdaf.lanes.literature.evidence import observation_anchor_key

__all__ = (
    "EnrichmentLedgers",
    "StageBoundary",
    "general_methods_first_blocks",
    "lookup_stage",
    "recall_note_stages",
    "register_elision",
    "register_stage",
    "register_unassigned",
    "remember_note_stages",
    "remount_published_stages",
    "stage_boundary",
)

VERIFIED = "VERIFIED"
SOURCE_MISMATCH = "SOURCE_MISMATCH"


@dataclass
class EnrichmentLedgers:
    """What one publication keeps beside the machine while its receipts are applied.

    `stages`: operation id -> the stages accepted for it, each a dict of `receipt`
    and `stage_key` (the first declaration's), `names` (every (receipt, stage_key)
    that declared it), `anchor_hash`, `stage_verbatim`, `verification`,
    `boundary_evidence_refs` and `sites`. `elisions`: operation id -> its elision.
    `unassigned`: the whole-operation values a stretch displaced, in the order they
    were displaced. `note_stages`: operation id -> {content hash of a published
    condition note -> the anchor_hash of each staged note with that content, in the
    operation's own note order}, which is how a note keeps its stage between receipts:
    the working key comes off the note at every receipt's finalize, and the next
    receipt puts it back from here before it routes anything
    (`remember_note_stages`, `recall_note_stages`). A list and not one anchor because
    two stages of one operation can carry the same words -- two stretches that each ran
    forty minutes publish the same note twice (E159). `furniture`: package
    content hash -> the page's own printing (`evidence.furniture_by_package`).
    `general_methods_first_blocks`: the (revision, first block id) of every
    GENERAL_METHODS reading unit, so an elision to one binds by the unit's name.
    """

    material_sources: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    elisions: dict[str, dict[str, Any]] = field(default_factory=dict)
    unassigned: list[dict[str, Any]] = field(default_factory=list)
    condition_readings: list[dict[str, Any]] = field(default_factory=list)
    measurement_baseline: dict[str, Any] | None = None
    measurement_contributions: dict[str, Any] = field(default_factory=dict)
    accepted_review_markers: list[dict[str, Any]] = field(default_factory=list)
    note_stages: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    furniture: Mapping[str, str | None] = field(default_factory=dict)
    general_methods_first_blocks: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True)
class StageBoundary:
    """Where a declared stretch begins, as content: the cited observations' anchor
    keys, and -- when the author's words were found in one of them -- the site of
    those words (the package, the block, the character span in the block's own
    coordinates, which is what a cited fragment's `char_start` is in).
    """

    anchor_keys: tuple[str, ...]
    verification: str
    site: tuple[str, str, int, int] | None = None

    words: str = ""

    @property
    def anchor_hash(self) -> str:
        """The stage's identity before its operation has a stable key (batch 10 joins
        the two). A verified boundary is the site the words begin at, so a longer
        quote of the same sentence is the same stage; an unverified one is the
        cited anchors and the words themselves, so it never collides with a
        verified stage of the same paragraph, and two tasks quoting one unfound
        boundary the same way are still one.
        """
        if self.site is not None:
            package, block, start, _ = self.site
            basis = f"{package}|{block}|{start}"
        else:
            basis = ",".join(sorted(self.anchor_keys)) + "|" + self.words
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _site_of(observation: Mapping[str, Any]) -> tuple[str, str, int] | None:
    """The package, block and character start of a TEXT observation's first fragment."""
    if observation.get("kind") != "TEXT":
        return None
    verification = observation["verification"]
    anchor = observation["anchor"]
    assert isinstance(verification, Mapping) and isinstance(anchor, Mapping)
    fragments = anchor["fragments"]
    assert isinstance(fragments, Sequence)
    first = next((f for f in fragments if isinstance(f, Mapping)), None)
    if first is None:
        return None
    return (
        str(verification["verified_package_sha256"]),
        str(first["block_id"]),
        int(first["char_start"]),
    )


def stage_boundary(
    stage_verbatim: str,
    selection_ids: Sequence[str],
    selections: Mapping[str, Mapping[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
    *,
    furniture: Mapping[str, str | None],
    start_at: tuple[str, str, int, int] | None = None,
) -> StageBoundary:
    """The boundary a stage declaration has: verified where the author's words are
    found among the cited selections, on the terms every quote is held to
    (`find_quote`: one selection first, else across the two halves of a paragraph the
    layout cut); otherwise unverified, kept and not refused.

    `observations` holds each selection's observation, by id. `char_start` is a raw
    block coordinate, so the offset added to it is `find_quote`'s raw one (E159): the
    same boundary cited through a whole-block selection and through a sub-span of it
    lands on one site, and therefore on one `anchor_hash`. Words that run across a cut
    begin in the upper half, so the site is in its block. The end is the author's words
    as they stand in the block, not the folded form's length.
    """
    anchor_keys = tuple(
        observation_anchor_key(observations[str(selections[sid]["evidence_ref"]["observation_id"])])
        for sid in dict.fromkeys(selection_ids)
    )
    words = comparison_text(stage_verbatim)
    offsets: dict[str, int] = {}
    search_selections = dict(selections)
    if start_at is not None:
        for sid in selection_ids:
            source = _site_of(observations[str(selections[sid]["evidence_ref"]["observation_id"])])
            if source is None or source[0] != start_at[0] or source[1] < start_at[1]:
                search_selections[sid] = {**selections[sid], "exact_text": "", "match_text": ""}
                continue
            cut = max(0, start_at[2] - source[2]) if source[1] == start_at[1] else 0
            offsets[sid] = cut
            search_selections[sid] = {
                **selections[sid],
                "match_text": (matching_text(selections[sid]) or "")[cut:],
            }
    found = find_quote(stage_verbatim, selection_ids, search_selections, furniture=furniture)
    site = (
        None
        if found is None
        else _site_of(
            observations[str(selections[found.selection_ids[0]]["evidence_ref"]["observation_id"])]
        )
    )
    if found is None or site is None:
        return StageBoundary(anchor_keys, SOURCE_MISMATCH, None, words)
    binding = selections[found.selection_ids[0]]
    observation = observations[str(binding["evidence_ref"]["observation_id"])]
    fragment = observation["anchor"]["fragments"][0]
    raw = matching_text(binding) or ""
    if hashlib.sha256(raw.encode()).hexdigest() != fragment["fragment_sha256"]:
        # Legacy cleaned-only receipts remain readable, but cannot manufacture raw coordinates.
        return StageBoundary(anchor_keys, SOURCE_MISMATCH, None, words)
    package, block, char_start = site
    start = char_start + offsets.get(found.selection_ids[0], 0) + found.offset
    return StageBoundary(
        anchor_keys,
        VERIFIED,
        (
            package,
            block,
            start,
            char_start + offsets.get(found.selection_ids[0], 0) + found.spans[0][1],
        ),
        words,
    )


def _fullness(text: str) -> tuple[int, str]:
    return (len(text.split()), " ".join(text.split()))


def _fuller(existing: str, incoming: str) -> str:
    """The wording that survives when two declarations are one stage: the fuller one,
    on `merge_condition`'s terms (more words, then the folded text)."""
    return incoming if _fullness(incoming) > _fullness(existing) else existing


def register_stage(
    ledgers: EnrichmentLedgers,
    operation_id: str,
    *,
    receipt: str,
    stage_key: str,
    stage_verbatim: str,
    boundary: StageBoundary,
    evidence_refs: Sequence[Mapping[str, str]],
    model_interpretations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Enter a stage declaration on its operation's ledger and return the entry the
    name now refers to.

    A declaration whose boundary is a stage already on the ledger is that stage: the
    name joins its names, the fuller wording survives, the evidence is united, and
    the `anchor_hash` is what it was. An unverified declaration is a stage too --
    its own receipt's conditions may name it -- identified by what it cited and
    said, since it has no site.
    """
    entries = ledgers.stages.setdefault(operation_id, [])
    name = {"receipt": receipt, "stage_key": stage_key}
    for entry in entries:
        if entry["anchor_hash"] == boundary.anchor_hash:
            if name not in entry["names"]:
                entry["names"].append(name)
            entry["stage_verbatim"] = _fuller(entry["stage_verbatim"], stage_verbatim)
            if model_interpretations:
                entry["model_interpretations"] = merge_interpretations(
                    entry.get("model_interpretations", ()), model_interpretations
                )
            for ref in evidence_refs:
                if ref not in entry["boundary_evidence_refs"]:
                    entry["boundary_evidence_refs"].append(dict(ref))
            if boundary.site is not None and boundary.site not in entry["sites"]:
                entry["sites"].append(boundary.site)
            return entry
    entry = {
        **name,
        "names": [name],
        "anchor_hash": boundary.anchor_hash,
        "stage_verbatim": stage_verbatim,
        "verification": boundary.verification,
        "boundary_evidence_refs": [dict(ref) for ref in evidence_refs],
        "sites": [boundary.site] if boundary.site is not None else [],
        **(
            {"model_interpretations": merge_interpretations((), model_interpretations)}
            if model_interpretations
            else {}
        ),
    }
    entries.append(entry)
    return entry


def lookup_stage(
    ledgers: EnrichmentLedgers, operation_id: str, receipt: str, stage_key: str
) -> dict[str, Any] | None:
    """The stage a receipt's `stage_key` names on this operation, or None.

    Looked up by receipt: `stage_key` is a name local to the answer that wrote it,
    and two tasks' names for one stretch meet only through the boundary
    (`register_stage`), never through the spelling.
    """
    name = {"receipt": receipt, "stage_key": stage_key}
    return next(
        (entry for entry in ledgers.stages.get(operation_id, ()) if name in entry["names"]),
        None,
    )


def register_elision(
    ledgers: EnrichmentLedgers,
    operation_id: str,
    *,
    receipt: str,
    target: Mapping[str, Any],
    evidence_refs: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    """Record whose procedure an operation follows, by identity: the operation, the
    general-methods unit's first block, or a source outside this document.

    An operation following itself says nothing, and is refused rather than kept as a
    record a consumer would have to learn to ignore (kalmanol's "used as the same
    procedure for 2 cycles again" is a condition of that operation, not an elision).
    """
    if target.get("referenced_operation_id") == operation_id:
        raise ValueError(
            "an operation cannot elide its own procedure; a procedure run again on the "
            "same operation is a condition of it, not an elision"
        )
    entry = {
        "receipt": receipt,
        "operation_id": operation_id,
        **{key: value for key, value in target.items() if value not in (None, False)},
        "evidence_refs": [dict(ref) for ref in evidence_refs],
    }
    ledgers.elisions[operation_id] = entry
    return entry


def register_unassigned(
    ledgers: EnrichmentLedgers,
    operation_id: str,
    note: Mapping[str, Any],
    *,
    receipt: str,
    source: str,
) -> dict[str, Any]:
    """Keep a whole-operation value a stretch displaced: the author's words, the kind,
    the evidence and where it came from (`source`: the collection or field it was
    written through), visibly, rather than filing it over what the stretches say or
    dropping it.
    """
    category_word = expressed_value(note.get("category_verbatim"))
    text = expressed_value(note.get("text"))
    if not isinstance(category_word, str) or not isinstance(text, str):
        raise ValueError("unassigned fact requires known condition words; preserve its source note")
    entry = {
        "receipt": receipt,
        "operation_id": operation_id,
        "source": source,
        "category": note.get("category"),
        "category_verbatim": category_word,
        "text": text,
        "about": note.get("about"),
        "evidence_refs": [dict(ref) for ref in note.get("evidence_refs", ())],
    }
    ledgers.unassigned.append(entry)
    return entry


def remember_note_stages(
    ledgers: EnrichmentLedgers,
    operation_id: str,
    notes: Sequence[Mapping[str, Any]],
    *,
    stage_key: str,
    public_form: Any,
    content_hash: Any,
) -> None:
    """Before the working key comes off an operation's notes, keep which stage each
    staged note is on, by the content of the note as it will be published.

    The operation's table is rebuilt here, once per finalize, from the notes as they
    now stand: notes are only appended and only a WHOLE_OWNERS match is removed, so
    their order is the one `recall_note_stages` will meet them in, and two notes whose
    published content is identical keep an anchor each instead of collapsing into one
    (E159).
    """
    table: dict[str, list[str]] = {}
    for note in notes:
        anchor = note.get(stage_key)
        if anchor is not None:
            table.setdefault(content_hash(public_form(note)), []).append(str(anchor))
    ledgers.note_stages[operation_id] = table


def recall_note_stages(
    ledgers: EnrichmentLedgers,
    operation_id: str,
    notes: Sequence[dict[str, Any]],
    *,
    stage_key: str,
    content_hash: Any,
) -> None:
    """Put the working key back on the notes an earlier receipt staged, so a later
    receipt's condition on the same stage meets them as the same stretch (E114's
    first rule) and not as an unplaced reading.

    Notes of identical published content are handed their anchors in the order both
    sides recorded them: the table's list is consumed from the front, so the first
    such note gets the first stage and the second the second (E159).
    """
    remembered = {
        key: list(anchors) for key, anchors in ledgers.note_stages.get(operation_id, {}).items()
    }
    if not remembered:
        return
    for note in notes:
        if stage_key not in note:
            anchors = remembered.get(content_hash(note))
            if anchors:
                note[stage_key] = anchors.pop(0)


def remount_published_stages(ledgers: EnrichmentLedgers, machine: Mapping[str, Any]) -> None:
    """Put back on this round's ledger what the last revision published (E158).

    A publication's private record dies with the round that made it; what outlives it
    is the public v1.1 result, which is where the next round reads its stages,
    elisions and displaced values from. Nothing is recomputed: the boundary hash a
    stage is identified by is the tail of the key it published, so a stretch declared
    again this round meets the one already there instead of minting a second. A v1
    result carries none of this and leaves the ledger empty, exactly as before.

    The names are not restored: a `stage_key` is local to the receipt that wrote it
    (`lookup_stage`), and no receipt of this round wrote any of these.
    """
    domain = machine.get("domain_payload")
    if not isinstance(domain, Mapping):
        return
    revision = str(machine.get("machine_result_id", "published"))
    from tsdaf.contracts.literature.objects import experiment_containers

    for block in experiment_containers(machine):
        for operation in block.get("operations", ()):
            operation_id = str(operation["operation_id"])
            for stage in operation.get("stages") or ():
                if any(
                    entry["anchor_hash"] == boundary_hash_of(stage)
                    for entry in ledgers.stages.get(operation_id, [])
                ):
                    continue
                boundary = stage.get("boundary")
                sites = (
                    [
                        (
                            str(boundary["package_sha256"]),
                            str(boundary["block_id"]),
                            int(boundary["char_start"]),
                            int(boundary["char_start"]) + len(str(stage["description"])),
                        )
                    ]
                    if isinstance(boundary, Mapping)
                    else []
                )
                ledgers.stages.setdefault(operation_id, []).append(
                    {
                        "receipt": revision,
                        "stage_key": str(stage["stable_key"]),
                        "names": [],
                        "anchor_hash": boundary_hash_of(stage),
                        "stage_verbatim": str(stage["description"]),
                        "verification": str(stage["verification"]),
                        "boundary_evidence_refs": [
                            dict(ref) for ref in stage["boundary_evidence_refs"]
                        ],
                        "sites": sites,
                        **(
                            {
                                "model_interpretations": merge_interpretations(
                                    (), stage["model_interpretations"]
                                )
                            }
                            if stage.get("model_interpretations")
                            else {}
                        ),
                    }
                )
            source = operation.get("procedure_source")
            if isinstance(source, Mapping) and operation_id not in ledgers.elisions:
                target = {
                    "referenced_operation_id": source.get("operation_id"),
                    "referenced_unit_block_id": source.get("unit_block_id"),
                    "external_citation": source.get("kind") == "EXTERNAL",
                }
                ledgers.elisions[operation_id] = {
                    "receipt": revision,
                    "operation_id": operation_id,
                    **{key: value for key, value in target.items() if value not in (None, False)},
                    "evidence_refs": [dict(ref) for ref in source.get("evidence_refs") or ()],
                }
            for fact in operation.get("unassigned_facts") or ():
                if any(
                    all(entry.get(k) == v for k, v in fact.items())
                    and entry.get("operation_id") == operation_id
                    for entry in ledgers.unassigned
                ):
                    continue
                ledgers.unassigned.append(
                    {"receipt": revision, "operation_id": operation_id, **dict(fact)}
                )


def general_methods_first_blocks(
    documents: Iterable[tuple[str, ReadingUnits | None]],
) -> frozenset[tuple[str, str]]:
    """The (revision, first block id) of every GENERAL_METHODS reading unit: the name
    an elision binds to a unit by (`ReadingUnit.first_block_id`). One construction
    for the publication and for the task loop, so both accept the same names.
    """
    return frozenset(
        (revision, unit.first_block_id)
        for revision, units in documents
        if units is not None
        for unit in units.units
        if unit.kind is UnitKind.GENERAL_METHODS
    )
