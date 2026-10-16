"""Apply source-bound increments to existing public experimental containers.

Scientific correspondence and ADD/REFINE/CONFLICT are model decisions. The host
checks exact old values, allowed field schemas, evidence closure and MAIN priority.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any

from tsdaf.contracts.literature.expressions import (
    characterization_source_statement,
    expressed_value,
    resolved_condition_note,
    merge_interpretations,
    model_interpretation,
)
from tsdaf.contracts.literature.objects import experiment_containers, reported_outcomes
from tsdaf.contracts.literature.schema import (
    MachineProfile,
    fact_definition,
    fact_enum,
    machine_profile,
    validate_fact_definition,
)
from tsdaf.contracts.literature.stage_semantics import (
    apply_procedure_reading,
    boundary_hash_of,
    compile_operation_stages,
    procedure_readings,
    procedure_source_value,
    stage_id_for,
)
from tsdaf.contracts.literature.validation import (
    evidence_refs_in_order,
    required_fact_observation_refs,
    validate_fact_bundle,
    validate_material_relations,
    validate_narrative_subjects,
)
from tsdaf.contracts.reading_units import ReadingUnits, UnitKind
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.author_labels import (
    author_label_signs_disagree,
    canonical_author_label,
    canonical_author_labels,
)
from tsdaf.lanes.literature.characterization_methods import (
    characterization_fields,
    validate_characterization_statement,
)
from tsdaf.lanes.literature.condition_categories import (
    FIXED_CONDITION_FIELDS,
    STAGE_KEY,
    merge_condition,
    normalize_condition,
    public_note,
    same_condition,
    stage_covers,
    stage_of,
)
from tsdaf.lanes.literature.enrichment_values import (
    CompiledCharacterization,
    CompiledCompound,
    CompiledCondition,
    CompiledElision,
    CompiledReadingPatch,
    CompoundAddition,
    ContainerPatch,
    HoldsSelections,
    ReceiptEvidence,
)
from tsdaf.lanes.literature.fact_projection import compound_label, pending, present, step_name
from tsdaf.lanes.literature.material_bindings import material_position as material_position
from tsdaf.lanes.literature.material_sources import (
    MATERIAL_RELATION_FIELDS,
    MATERIAL_SOURCE_FIELDS,
    apply_material_sources,
    matching_material_sources,
)
from tsdaf.lanes.literature.model_view import (
    DetailContextPurpose, compound_view, detail_context_view,
)
from tsdaf.lanes.literature.source_manifest import LiteratureSources
from tsdaf.lanes.literature.stage_ledger import (
    EnrichmentLedgers,
    lookup_stage,
    recall_note_stages,
    register_elision,
    register_stage,
    register_unassigned,
    remember_note_stages,
    remount_published_stages,
    stage_boundary,
)
from tsdaf.lanes.literature.written_values import project_written_value

OUTCOME_REQUIRED_FIELDS = ("kind", "reported_value", "operation_ids", "product_compound_ids")
OUTCOME_OPTIONAL_FIELDS = ("basis",)


def content_hash(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def same_compound_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Reuse numbered-identity rules; an unnumbered saved object keeps its first name."""
    labels = left.get("author_labels") or []
    other = right.get("author_labels") or []
    if labels or other:
        return canonical_author_labels(labels) == canonical_author_labels(
            other
        ) and not author_label_signs_disagree(labels, other)
    return (left.get("names") or [])[:1] == list(right.get("names") or [])[:1]


def register_compound_addition(
    domain: dict[str, Any],
    paper_id: str,
    added: CompoundAddition,
    refs: list[dict[str, Any]],
    *,
    profile: MachineProfile,
) -> str:
    """One identity/merge rule for accepted reading objects and publication.

    The caller owns this mutable projection. Validate before replacing a record so
    a refused addition cannot partially change the compound it matched.
    """
    labels = list(dict.fromkeys(added.author_labels))
    compound_id = "CMP-ENR-" + content_hash([paper_id, added.compound_key])[:24]
    same = next(
        (
            c
            for c in domain["compounds"]
            if labels
            and canonical_author_labels(c["author_labels"]) == canonical_author_labels(labels)
            and not author_label_signs_disagree(c["author_labels"], labels)
        ),
        None,
    )
    prior_key = next((c for c in domain["compounds"] if c["compound_id"] == compound_id), None)
    if prior_key is not None:
        if not same_compound_identity(prior_key, {"author_labels": labels, "names": added.names}):
            raise ValueError(
                "compound key already belongs to a different identity; use a distinct finding"
            )
        same = prior_key
    if same is not None:
        record = copy.deepcopy(same)
        record["names"] = list(dict.fromkeys([*record["names"], *added.names]))
        record["evidence_refs"] = list(
            {content_hash(ref): ref for ref in (*record["evidence_refs"], *refs)}.values()
        )
        compound_id = str(record["compound_id"])
    else:
        record = {
            "compound_id": compound_id,
            "author_labels": labels,
            "names": list(dict.fromkeys(added.names)),
            "structure": pending(),
            "predecessor_ids": [],
            "successor_ids": [],
            "evidence_refs": refs,
        }
    validate_fact_definition("CompoundRecord", record, profile=profile)
    domain["compounds"] = (
        [record if c["compound_id"] == compound_id else c for c in domain["compounds"]]
        if any(c["compound_id"] == compound_id for c in domain["compounds"])
        else [*domain["compounds"], record]
    )
    return compound_id


# What a detail investigation may do to each container type's offered fields: every
# action on an operation's or an outcome's own fields; on a reaction block only a
# CONFLICT, because its endpoints are the route's topology (ledger E76 ⑥).
CONTAINER_ACTIONS: dict[str, tuple[str, ...] | None] = {
    "Operation": None,
    "ReportedOutcome": None,
    "ReactionBlock": ("CONFLICT",),
    "ExperimentalAccount": ("CONFLICT",),
}
CONFLICT_TARGET_TYPES = {
    "Operation": "OPERATION",
    "ReportedOutcome": "REPORTED_OUTCOME",
    "ReactionBlock": "REACTION_BLOCK",
    "ExperimentalAccount": "EXPERIMENTAL_ACCOUNT",
}


def _containers(machine: Mapping[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    for block in experiment_containers(machine):
        if "reaction_block_id" in block:
            result[block["reaction_block_id"]] = ("ReactionBlock", block)
        else:
            result[block["account_id"]] = ("ExperimentalAccount", block)
        for type_name, items, id_field in (
            ("Operation", block["operations"], "operation_id"),
            ("ReportedOutcome", block["reported_outcomes"], "outcome_id"),
        ):
            for item in items:
                result[item[id_field]] = (type_name, item)
    for outcome in reported_outcomes(machine):
        result[outcome["outcome_id"]] = ("ReportedOutcome", outcome)
    return result


def scoped_blocks(
    machine: Mapping[str, Any] | None, block_ids: tuple[str, ...] | None,
    *, include_accounts: bool = False,
) -> list[dict[str, Any]]:
    """The reaction blocks one task works on: the ones it was given, else every one.

    A task scoped to blocks is given those blocks and nothing else -- not their sheet,
    not their shapes, not the steps its neighbours are working on. The one place that
    decides what "this task's blocks" means, so the sheet, the value definitions and
    the Host's own reconciliation cannot disagree about it.
    """
    if machine is None:
        return []
    blocks = list(experiment_containers(machine)) if include_accounts else list(
        machine["domain_payload"]["reaction_blocks"]
    )
    if block_ids is None:
        return blocks
    wanted = set(block_ids)
    return [
        block for block in blocks
        if str(block.get("reaction_block_id", block.get("account_id"))) in wanted
    ]


def anchored_block_id(observation: Mapping[str, Any]) -> str | None:
    """The one source block an observation was read out of, when it names one.

    A text anchor carries the fragments it was cut from, and every text observation in
    three real papers (106 of them) came from a single block -- which is the passage a
    task can go and read. An image anchor carries a page and a box instead and names no
    block at all (15 of 37 on decumbenine_B, 16 of 74 on alchivemycin_A); `None` says so
    rather than inventing one, and that figure stays reachable through the catalog like
    any other visual.

    Public: the paper-level and block-level context an EXPERIMENTAL_DETAIL task is
    handed both name a block this same way, so a workflow layer offering the model a
    readable ``location`` for either shape derives it from the same site.
    """
    anchor = observation.get("anchor")
    fragments = anchor.get("fragments") if isinstance(anchor, Mapping) else None
    named = list(
        dict.fromkeys(
            str(fragment["block_id"])
            for fragment in (fragments or ())
            if isinstance(fragment, Mapping) and "block_id" in fragment
        )
    )
    return named[0] if len(named) == 1 else None


def anchored_asset(observation: Mapping[str, Any]) -> str | None:
    """The figure an image observation was read from, by its asset's content hash: an
    image anchor names no block, and its asset is what the package knows the figure by."""
    anchor = observation.get("anchor")
    asset = anchor.get("source_asset") if isinstance(anchor, Mapping) else None
    sha256 = asset.get("sha256") if isinstance(asset, Mapping) else None
    return str(sha256) if isinstance(sha256, str) else None


def figure_sources(
    observations: Sequence[Mapping[str, Any]], sources: LiteratureSources
) -> list[tuple[str, str]]:
    """The figures the route was read from: (registered revision, visual id), each once,
    found by the asset its observation cites. A task may read them whatever its own part
    (owner ruling, batch 2A: an upstream value's source stays readable downstream)."""
    by_asset: dict[str, tuple[str, str]] = {}
    for document in sources.active():
        if document.package is None:
            continue
        for visual in document.package.visual_assets:
            by_asset[visual.asset.sha256] = (
                document.registration.document_revision_id,
                visual.visual_id,
            )
    found: dict[tuple[str, str], None] = {}
    for observation in observations:
        sha256 = anchored_asset(observation)
        if sha256 is not None and sha256 in by_asset:
            found[by_asset[sha256]] = None
    return list(found)


def observations_these_blocks_cite(
    observations: tuple[Mapping[str, Any], ...],
    machine: Mapping[str, Any] | None,
    block_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    """The observations a block task's own steps already rest on, named and no more.

    A block task was given every observation of the paper with its anchor and its
    payload: 29,957 bytes on a seven-step paper, 65,799 on an eighteen-step one. That is
    far past the task context limit, so it arrived as a menu, and the e106 round spent
    21 of its 223 turns walking it. Nothing in it could ever have been evidence: a
    citation is minted on the `source:` read path, out of the passage, never out of an
    observation record. What is worth naming is which observations this step rests on,
    what each was read as, and which passage it came from so the task can go read that
    passage -- three keys, and 315 bytes for the same seven steps.

    The closure is the one the Host's own validation already computes for a fact
    subtree, handed the scoped blocks in the shape it takes, so "which observations does
    this step rest on" has one answer here and in the checks that publish the result.
    """
    subtree: dict[str, object] = {
        "domain_payload": {"reaction_blocks": scoped_blocks(machine, block_ids)},
        "blocking_conflicts": [],
        "unresolved_items": [],
    }
    cited = {observation_id for observation_id, _ in required_fact_observation_refs(subtree)}
    return [
        {
            "observation_id": str(observation["observation_id"]),
            "block_id": anchored_block_id(observation),
            # An image observation's figure, by its asset; a text one has none to carry.
            **(
                {"asset_sha256": anchored_asset(observation)}
                if anchored_asset(observation) is not None
                else {}
            ),
            "semantic_role": observation.get("semantic_role"),
            "document_revision_id": str(observation["anchor"]["document_revision_id"]),
        }
        for observation in observations
        if observation.get("observation_id") in cited
    ]


def step_sources(
    machine: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
    block_ids: tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """Where the author wrote this task's steps: (document revision, block), in route order.

    A step carries the ``evidence_refs`` of the observations it was built from, each
    observation's anchor carries the fragments it was read from, and a text fragment
    names its block. The route therefore already knows which paragraphs of the paper its
    steps came out of, and the Host can hand those over instead of leaving the model to
    search for them. Measured on the sealed baselines of two papers, 23 steps in all:
    every step reached at least one paragraph, and on decumbenine_B every one of the
    seven reached a paragraph inside the experimental part the detail lane may read.

    A visual observation has no ``fragments`` and is skipped; this answers with text
    sites only. The revision is the one the observation names, which is the stable
    revision of its package and need not be the id the caller registered the document
    under. Which of these sites a task is authorized to read is the caller's to decide.

    A step rests on every observation under it -- its own, its operations', their
    materials' and its measurements' -- which is the closure
    `observations_these_blocks_cite` names to the task. This walked the block's own
    references alone, so the two answered "what does this step rest on" differently:
    ineleganolide f09 B008 was told of an observation hung only on its materials and was
    never handed the paragraph it came from (the conclusion, read in none of its twenty
    turns). The block's own references still come first, in their order.
    """
    anchors = {
        str(observation["observation_id"]): observation
        for observation in observations
        if isinstance(observation.get("anchor"), Mapping)
    }
    found: dict[tuple[str, str], None] = {}
    for block in scoped_blocks(machine, block_ids):
        own = [str(ref.get("observation_id")) for ref in block.get("evidence_refs", ())]
        under = [observation_id for observation_id, _ in evidence_refs_in_order(block)]
        for observation_id in dict.fromkeys((*own, *under)):
            observation = anchors.get(observation_id)
            if observation is None:
                continue
            revision = str(observation["document_revision_id"])
            for fragment in observation["anchor"].get("fragments", ()):
                found[(revision, str(fragment["block_id"]))] = None
    return list(found)


def readable_with_step_sources(
    sources: LiteratureSources,
    machine: Mapping[str, Any] | None,
    observations: Sequence[Mapping[str, Any]],
    block_ids: tuple[str, ...] | None,
) -> LiteratureSources:
    """The same sources, with the passages this task's steps are about readable, whole.

    A step task reads its upstream experimental units and is handed, at turn
    zero, the paragraphs its own steps came out of -- when those lie inside that part.
    When the experimental part is a supplement, they never do: every observation the
    hyperfirin route was built from is main-text narrative, on the far side of the cut,
    and E100 left them unread there. The context still names them, as the observations
    the step carries, and on f06 three of fourteen tasks spent every turn asking for
    them by that name and wrote nothing (ledger E142). What a step was built from is
    the step's to read, wherever in the paper it stands.

    A block is read as part of the unit the document's own reading placed it in
    (E136), and a step that may read one block of a unit may read the unit (E156, R8):
    on f07 four of the main text's seventeen units were cut across three to five
    single-block tasks. Four rules, each a call to a function that already existed,
    and only this function decides what a step may read:

    * the PROCEDURE, CHARACTERIZATION and CRYSTALLOGRAPHY units claimed by the
      products' labels, exactly as `candidate_step_units` hands them over -- whole,
      but a crystal structure by its first block only (E139) -- asked over the whole
      document rather than the part already readable;
    * the same kinds of unit claimed by the labels of the step's neighbours on the
      route (E163), through the same function, the labels from
      `step_neighbour_labels`;
    * a NARRATIVE unit one of the step's own observations is anchored in, whole; an
      anchor in a BIBLIOGRAPHY or OTHER unit, or in a document never read into units,
      grants that block alone, as before;
    * every document's GENERAL_METHODS units (E144), whole.

    The grant only adds: the composition's semantic reading scope stays available
    across routes (`readable_ids` is the union of the part and these blocks).

    A task over the whole route (``block_ids`` None: the paper's own task, the middle
    layer) is shown every step, so what every step was read from stays readable to it
    (owner ruling, batch 2A, 2026-09-25: an upstream value's source is always readable
    downstream). It is granted the paragraphs the route cites and the procedure and
    data units no step claims (`unclaimed_step_units`) -- no units claimed by a label
    and no general methods, which are a step task's reading.
    An empty ``block_ids`` names no step and grants nothing.
    """
    loaded = [(document, document.package) for document in sources.active() if document.package]
    by_stable = {p.stable_revision_id(d.registration.source_phase): (d, p) for d, p in loaded}
    by_registered = {d.registration.document_revision_id: (d, p) for d, p in loaded}
    whole = [
        (d.registration.document_revision_id, d.units, frozenset(p.clean_view.included_block_ids))
        for d, p in loaded
    ]
    granted: list[tuple[str, str]] = []
    if block_ids is not None:
        labels = step_author_labels(machine, block_ids)
        granted.extend(
            (row["document_revision_id"], row["source_id"])
            for row in candidate_step_units(labels, whole)
        )
        granted.extend(
            (row["document_revision_id"], row["source_id"])
            for row in candidate_step_units(step_neighbour_labels(machine, block_ids), whole)
        )
    for observed_revision, block_id in step_sources(machine, observations, block_ids):
        found = by_stable.get(observed_revision) or by_registered.get(observed_revision)
        if found is None:
            continue
        document, package = found
        if block_id not in package.clean_view.included_block_ids:
            continue
        registered = document.registration.document_revision_id
        unit = document.units.unit_containing(block_id) if document.units is not None else None
        if unit is not None and unit.kind is UnitKind.NARRATIVE and len(unit.block_ids) > 1:
            granted.extend((registered, member) for member in unit.block_ids)
        else:
            granted.append((registered, block_id))
    if block_ids is not None:
        granted.extend(general_methods_blocks(whole))
    else:
        # The units no step claims belong to no step task; the whole-route task reads them.
        reached: set[tuple[str, str]] = set()
        for observed, block_id in step_sources(machine, observations, None):
            owner = by_stable.get(observed) or by_registered.get(observed)
            if owner is not None:
                reached.add((owner[0].registration.document_revision_id, block_id))
        granted.extend(
            (row["document_revision_id"], row["source_id"])
            for row in unclaimed_step_units(machine, whole, reached)
        )
    widened: dict[str, list[str]] = {}
    for registered, block_id in granted:
        document = by_registered[registered][0]
        if block_id in document.readable_ids:
            continue
        widened.setdefault(registered, list(document.step_block_ids)).append(block_id)
    if not widened:
        return sources
    return sources.with_step_blocks(
        {revision: tuple(dict.fromkeys(ids)) for revision, ids in widened.items()}
    )


# How far into a block the Host will look for a compound's number. An experimental entry
# is titled by what it makes, so the number stands in the title; past the title a number
# is prose, and prose is what the model reads for itself.
CANDIDATE_HEAD_CHARS = 240
# How many blocks a number may stand in and still be a way to find one passage. On
# alchivemycin_A the natural product's label `1` heads eleven blocks -- the
# introduction, the discussion, four figure captions -- and eleven rows is not a
# shortcut to anything. Past this the Host offers none and the task uses the directory,
# exactly as it does today.
CANDIDATE_LIMIT = 3
# A number printed on its own, not one written against an element symbol: `dione (22)`
# is compound 22, `C(22)` is an atom of a crystal structure. Seven of the eight blocks
# matching `(22)` on the hyperfirin supplement were bond tables and atomic coordinates.
_STANDS_ALONE = r"(?:(?<=\s)|^)"
# `14a` and `14b` when the supplement titles the pair `(14)`: the route separates the
# diastereomers, the author wrote one entry for both.
_ISOMER_SUFFIX = re.compile(r"^(\d{1,3})[a-z]$")
# `exo-19` when the route makes `19`: the author characterized one isomer and wrote
# its descriptor into the label. E66 keeps a word prefix as part of the identity --
# ent-5 is not 5 -- and the delivery does not say otherwise: the label under the
# descriptor is handed over as the weaker match, as a suffix's stem is, and the row
# says so. On f06 the step making 19 was never handed the data marked `exo-19`
# (ledger E142). The descriptors are the stereo- and isomer words chemists put in
# front of a number, listed; a protecting group written the same way (`Boc-12`) or
# the supplement's own namespace (`SI-3`) is not one, and stays a different label.
_DESCRIPTOR_PREFIX = re.compile(
    r"^(?:exo|endo|ent|epi|rac|iso|syn|anti|cis|trans|nor|seco|neo|homo|allo|pseudo|"
    r"alpha|beta|[EZRS])-(\d{1,3}[a-z]?)$",
    re.IGNORECASE,
)


def _under_descriptor(label: str) -> str | None:
    """The label under a descriptor -- `19` under `exo-19`, `14a` under `exo-14a` -- or
    None when the label carries none."""
    prefixed = _DESCRIPTOR_PREFIX.match(label)
    return prefixed.group(1) if prefixed else None


def _number_under(label: str) -> str | None:
    """The number under a decorated label -- `14` under `14a`, `19` under `exo-19`,
    `14` under `exo-14a` -- or None when the label is the number itself."""
    bare = _under_descriptor(label) or label
    suffixed = _ISOMER_SUFFIX.match(bare)
    if suffixed:
        bare = suffixed.group(1)
    return None if bare == label else bare


def _label_forms(label: str) -> tuple[re.Pattern[str], ...]:
    """How an author prints a compound's number where they title its procedure."""
    printed = re.escape(label)
    return (
        re.compile(_STANDS_ALONE + rf"\({printed}\)"),
        re.compile(rf"\bcompounds?\s+{printed}(?![0-9A-Za-z])", re.IGNORECASE),
    )


def _blocks_titled(label: str, blocks: Sequence[tuple[str, str, str]]) -> list[tuple[str, str]]:
    forms = _label_forms(label)
    return [
        (revision, block_id)
        for revision, block_id, text in blocks
        if any(form.search(text[:CANDIDATE_HEAD_CHARS]) for form in forms)
    ]


def _author_labels_of(compound_id: str, known: Mapping[str, Mapping[str, Any]]) -> Iterator[str]:
    """The numbers the author printed for one compound of the route, in their order.

    What counts as a usable author label is decided here and nowhere else, because two
    callers now ask it of different compounds -- `step_author_labels` of what the task's
    own steps make, `step_neighbour_labels` of what stands one hop away. A compound the
    catalog does not carry, a field the projection left unresolved, and an entry that is
    blank or not a string all answer the same: nothing, rather than a guess.
    """
    for label in known.get(compound_id, {}).get("author_labels") or ():
        if isinstance(label, str) and label.strip():
            yield label.strip()


def step_author_labels(
    machine: Mapping[str, Any] | None, block_ids: tuple[str, ...] | None = None
) -> list[str]:
    """What the author calls the products of this task's steps, in route order.

    The experimental part titles a procedure by the compound it makes (E96), so the
    products' numbers are what a title would say. An endpoint the route could not
    resolve names no compound and contributes nothing rather than a guess.
    """
    if machine is None:
        return []
    labels: dict[str, None] = {}
    known = {
        str(compound["compound_id"]): compound
        for compound in machine["domain_payload"]["compounds"]
    }
    for block in scoped_blocks(machine, block_ids):
        for compound_id in _endpoint_ids(block, "output_compound_ids"):
            for label in _author_labels_of(compound_id, known):
                labels[label] = None
    return list(labels)


def step_neighbour_labels(
    machine: Mapping[str, Any] | None, block_ids: tuple[str, ...] | None = None
) -> list[str]:
    """What the author calls the compounds one step away from this task's own.

    A step is one reaction of a route, and the route is why the author's write-up of
    the compound before it and the compound after it answers questions about it: which
    material went in, what the thing it makes was carried into, whether the passage in
    front of the task is this step or its neighbour's. Across the seven sealed f07/f08
    chains 150 of 1578 turns (9.5%) were spent reading another compound's PROCEDURE,
    CHARACTERIZATION or CRYSTALLOGRAPHY unit, and 34 of those read a compound standing
    in a `predecessor_ids`/`successor_ids` list of a compound this task's own block
    names -- one hop, which is what this answers.

    The compounds the block itself names, at either end, are not neighbours: their
    labels are the task's own (`step_author_labels` for what it makes) or are already
    in its container. Two hops -- a predecessor's predecessor -- is the other 116 of
    those 150 turns and is deliberately not answered here: nothing has measured what
    delivering a whole branch of the route would cost against what it saves.
    """
    if machine is None:
        return []
    known = {
        str(compound["compound_id"]): compound
        for compound in machine["domain_payload"]["compounds"]
    }
    mine = dict.fromkeys(
        compound_id
        for block in scoped_blocks(machine, block_ids)
        for key in ENDPOINT_FIELDS
        for compound_id in _endpoint_ids(block, key)
    )
    labels: dict[str, None] = {}
    for compound_id in mine:
        compound = known.get(compound_id, {})
        for field in ADJACENCY_FIELDS:
            for neighbour_id in compound.get(field) or ():
                if str(neighbour_id) in mine:
                    continue
                for label in _author_labels_of(str(neighbour_id), known):
                    labels[label] = None
    return list(labels)


def candidate_step_sources(
    labels: Sequence[str], blocks: Sequence[tuple[str, str, str]]
) -> list[dict[str, str]]:
    """Where this task's steps are probably written up, by the author's own numbering.

    `blocks` are the authorized (document revision, block id, text) a task may read.
    The answer is candidates and only candidates: the Host has not decided that block
    B is step S, and says so on every row it returns. What it has done is the
    deterministic part -- the author printed a number, and that number stands in one
    block's title -- which is the part the model should not be spending turns on. On
    hyperfirin's thirteen published steps against its supplement, nine reach one entry,
    three reach a small set the model settles, and compound 21, which the supplement
    never titles, reaches none.

    Missing is the cheap failure and guessing is the expensive one, so every rule here
    narrows: the number must stand alone, must sit in the title, and must not be one
    the prose repeats. A paper that numbers nothing gets no rows and its tasks are as
    well off as they are today.
    """
    hits: dict[tuple[str, str], str] = {}
    for label in labels:
        for site in _blocks_titled(label, blocks):
            hits.setdefault(site, label)
    match = "AUTHOR_LABEL"
    if not hits:
        # Two names for one thing: the printed number found nothing, so try the number
        # underneath it. A stem is a weaker claim than what the author wrote, and the
        # row says so.
        for label in labels:
            stem = _ISOMER_SUFFIX.match(label)
            if stem is None:
                continue
            for site in _blocks_titled(stem.group(1), blocks):
                hits.setdefault(site, stem.group(1))
        match = "LABEL_STEM"
    if not hits or len(hits) > CANDIDATE_LIMIT:
        return []
    status = "UNIQUE" if len(hits) == 1 and match == "AUTHOR_LABEL" else "AMBIGUOUS"
    return [
        {
            "document_revision_id": revision,
            "source_id": block_id,
            "matched_label": label,
            "match": match,
            "status": status,
        }
        for (revision, block_id), label in hits.items()
    ]


# The kinds of unit that are about one compound, and so can be claimed by its label.
_CLAIMABLE = frozenset({UnitKind.PROCEDURE, UnitKind.CHARACTERIZATION, UnitKind.CRYSTALLOGRAPHY})


def _canonical(label: str) -> str | None:
    try:
        return canonical_author_label(label)
    except ValueError:
        return None


def candidate_step_units(
    labels: Sequence[str],
    documents: Sequence[tuple[str, ReadingUnits | None, frozenset[str]]],
) -> list[dict[str, str]]:
    """Where this task's steps are written up, by what the document's own reading said.

    `documents` are `(document revision, its reading units or None, the block ids this
    task may read)`. The reading-units stage (E136) read each document once and marked
    every procedure, every set of data and every crystal structure with the label of
    the compound it is about; a step whose product carries that label is handed those
    units. This is the model's claim, made with the whole document in view, and it
    supersedes the title scan of `candidate_step_sources`, which now answers only for a
    label no unit claims (ledger E138: on hyperfirin's thirteen steps the units claim
    thirteen, the scan reached nine).

    One row per block, never one per unit: everything downstream -- delivery, the
    selection a task cites, the siblings an ATTRIBUTION_CONFLICT read brings back -- is
    keyed by (revision, block id). Each row carries the unit's kind and the id of its
    first block, which is the unit's name, so the rows of one unit can be told apart
    from the rows of another under the same label. Labels are compared through
    `canonical_author_label` (E66): the route may write the label with a sign or a
    spacing the supplement did not. A unit marked with the label's stem -- `14` for
    a step that makes `14a` -- is handed over as well, because the author who wrote
    one procedure for the pair titled it by the stem, and the row says it is the
    weaker match: on hyperfirin, 14a has its own data under `14a` and shares its
    procedure under `14`.
    Blocks outside what the task may read are left out; a unit entirely outside
    contributes nothing.

    A label matching more than `CANDIDATE_LIMIT` units is not a way to find a passage
    and is handed to the fallback instead, as the scan does with blocks.

    Two of a step's labels can reach one block -- 14a and 14b both reach the pair's
    procedure under 14 -- and the block is still handed over once (f05 read it twice,
    ledger E141): the exact claim keeps it over a stem's, else the first label's.
    """
    rows: list[dict[str, str]] = []
    for label in labels:
        wanted = _canonical(label)
        if wanted is None:
            continue
        stem = _number_under(wanted)
        matched: list[tuple[str, frozenset[str], Any, str]] = []
        for revision, units, readable in documents:
            if units is None:
                continue
            for unit in units.units:
                if unit.kind not in _CLAIMABLE:
                    continue
                # A pair written up together -- `9a and 9b` -- is matched by either
                # label (E156, ruling 12); an exact claim on any of them keeps it.
                printed_labels = [
                    printed
                    for printed in map(_canonical, unit.compound_labels)
                    if printed is not None
                ]
                if any(printed == wanted for printed in printed_labels):
                    matched.append((revision, readable, unit, "UNIQUE"))
                elif any(
                    (stem is not None and printed == stem) or _under_descriptor(printed) == wanted
                    for printed in printed_labels
                ):
                    # the route's `14a` reaches the pair's `14`; the route's `19`
                    # reaches the author's `exo-19` -- the label under a decoration,
                    # and never decoration to decoration: `14a` does not reach `14b`,
                    # `endo-19` does not reach `exo-19`, and a bare `19` does not reach
                    # `19a` and `19b`, which are two compounds.
                    matched.append((revision, readable, unit, "AMBIGUOUS"))
        if len(matched) > CANDIDATE_LIMIT:
            # Too many is not a way to find a passage -- but the reading's exact claims
            # are not guesses, and the weaker matches are what made it too many.
            matched = [m for m in matched if m[3] == "UNIQUE"]
        if not matched or len(matched) > CANDIDATE_LIMIT:
            continue
        for revision, readable, unit, status in matched:
            for block_id in unit.reader_block_ids:
                if block_id not in readable:
                    continue
                rows.append(
                    {
                        "document_revision_id": revision,
                        "source_id": block_id,
                        "matched_label": label,
                        "match": "UNIT_LABEL",
                        "status": status,
                        "unit_kind": unit.kind.value,
                        "unit_first_block_id": unit.first_block_id,
                    }
                )
    once: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        site = (row["document_revision_id"], row["source_id"])
        held = once.get(site)
        if held is None or (held["status"] == "AMBIGUOUS" and row["status"] == "UNIQUE"):
            once[site] = row
    return list(once.values())


def unclaimed_step_units(
    machine: Mapping[str, Any] | None,
    documents: Sequence[tuple[str, ReadingUnits | None, frozenset[str]]],
    reached: Collection[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """The procedure and data units no step of the route claims, one row per block.

    A unit is handed to the step whose product carries its label. A unit whose compound
    no step makes -- the preparation of the route's starting material is the plain case:
    on hyperfirin the supplement's first procedure makes ketone 9, which the route only
    consumes -- matches no step, was handed to no task, and its experiment reached
    neither the accounts nor the result (three independent runs, 2026-10-09). It was
    classified correctly; it had no owner. These rows are the whole-route task's to read.

    Claimed means exactly what `candidate_step_units` hands to the steps over the whole
    route, so nothing is both delivered to a step and listed here. A step also reaches
    passages through the observations it was built from (`step_sources`); `reached` names
    those (revision, block) sites, and a unit holding one of them is some step's as well. Crystal structures
    are left out: they are attachments read on demand, and so are units the reading gave
    no compound label. No decision is made here about whether such an experiment belongs
    to the route.
    """
    if machine is None:
        return []
    claimed = {
        (row["document_revision_id"], row["unit_first_block_id"])
        for row in candidate_step_units(step_author_labels(machine, None), documents)
    }
    rows: list[dict[str, str]] = []
    for revision, units, readable in documents:
        if units is None:
            continue
        for unit in units.units:
            if unit.kind not in (UnitKind.PROCEDURE, UnitKind.CHARACTERIZATION):
                continue
            if (revision, unit.first_block_id) in claimed:
                continue
            if any((revision, block_id) in reached for block_id in unit.block_ids):
                continue
            # Only a unit the reading marked with a compound: that label is what says
            # nobody makes it. A unit with no label was never offered by label to anyone
            # and stays found the way it is found today.
            if not any(_canonical(printed) for printed in unit.compound_labels):
                continue
            for block_id in unit.reader_block_ids:
                if block_id in readable:
                    rows.append(
                        {
                            "document_revision_id": revision,
                            "source_id": block_id,
                            "matched_label": unit.compound_label or "",
                            "match": "UNCLAIMED_UNIT",
                            "unit_kind": unit.kind.value,
                            "unit_first_block_id": unit.first_block_id,
                        }
                    )
    return rows


def general_methods_blocks(
    documents: Sequence[tuple[str, ReadingUnits | None, frozenset[str]]],
) -> list[tuple[str, str]]:
    """The (revision, block) sites of every document's general methods, in order.

    A procedure refers to the general methods -- the solvents, the drying, the
    instruments -- without repeating them, and a task reading one procedure goes
    looking for them: on f07 eleven of the sixty-six searches across fourteen step
    tasks were for "general experimental (procedure)", and the two GENERAL_METHODS
    units of the supplement, 3 KB together, had been handed to nobody (ledger E144).
    They are available beside each step's own passages. Applicability and any
    compound-specific limits follow the source, not the delivery grouping.
    """
    return [
        (revision, block_id)
        for revision, units, readable in documents
        if units is not None
        for unit in units.units
        if unit.kind is UnitKind.GENERAL_METHODS
        for block_id in unit.block_ids
        if block_id in readable
    ]


def _blocks_naming(machine: Mapping[str, Any] | None) -> list[tuple[str, set[str]]]:
    """Each published step and every public id that step names, in the route's order.

    A step names itself, its operations, its outcomes and the compounds at its ends. The
    compounds are why this is a list and not a lookup: two steps name the same compound,
    so a subject that reaches one has reached both steps, and belongs to neither alone.
    """
    if machine is None:
        return []
    naming: list[tuple[str, set[str]]] = []
    for block in machine["domain_payload"]["reaction_blocks"]:
        ids = {str(block["reaction_block_id"])}
        ids.update(str(o["operation_id"]) for o in block.get("operations", ()))
        ids.update(str(o["outcome_id"]) for o in block.get("reported_outcomes", ()))
        for key in ENDPOINT_FIELDS:
            ids.update(_endpoint_ids(block, key))
        naming.append((str(block["reaction_block_id"]), ids))
    return naming


def _placed(
    subject_ids: Sequence[str],
    segments: Mapping[str, Sequence[str]],
    group_map: Mapping[str, str],
    naming: Sequence[tuple[str, set[str]]],
    mine: set[str],
) -> tuple[str, list[str]]:
    """Which of the route's steps a fusion subject reaches, and whose work that makes it.

    LOCAL: only this task's steps. SHARED: this task's and others', so it is registered
    once and answered by the paper's task. ELSEWHERE: other steps only, so it does not
    travel here. UNPLACED: nothing published, which is named rather than passed over.
    """
    reached: set[str] = set()
    for subject in subject_ids:
        for fact_id in segments.get(str(subject), [str(subject)]):
            public = group_map.get(fact_id)
            if public is not None:
                reached.add(public)
    touched = [block_id for block_id, ids in naming if ids & reached] if reached else []
    if not touched:
        return "UNPLACED", []
    if not set(touched) & mine:
        return "ELSEWHERE", touched
    return ("LOCAL" if set(touched) <= mine else "SHARED"), touched


def project_detail_group_context(
    groups: Sequence[Mapping[str, Any]],
    identity_map: Mapping[str, Mapping[str, str]],
    machine: Mapping[str, Any] | None,
    block_ids: tuple[str, ...] | None,
    *,
    purpose: DetailContextPurpose = "READING",
) -> list[dict[str, Any]]:
    """The fusion lane's conclusions, placed: each question and assessment with the task
    whose steps it is about.

    A block task was given all of them. On hyperfirin that is 9,267 bytes in one context
    key against the Host's own 8,000-byte limit, and the five parts were byte-identical
    across all thirteen tasks -- the whole route's conclusions, thirteen times, none of
    it scoped. Every one of those tasks was refused before a request existed.

    A subject is placed by the steps its facts reach. Reaching only this task's steps
    makes it this task's. Reaching this task's and others makes it shared: registered
    once, with the steps it concerns, and answered by the task that answers for the
    paper. Reaching only other steps makes it theirs, and it does not travel. Reaching
    nothing published makes it unplaced, and it is named rather than dropped -- a
    subject nobody was asked about is the failure this scoping exists to avoid (E111).

    The paper's own task answers for every step, so nothing is placed for it: it keeps
    the group's questions, assessments and the route map its neighbours have no use for.
    """
    naming = _blocks_naming(machine)
    mine = set(block_ids or ())
    projected: list[dict[str, Any]] = []
    for group in groups:
        if "result" not in group:
            projected.append(dict(group))
            continue
        result = group["result"]
        keys = ("overview", "questions", "experiment_assessments", "source_errors", "note")
        common: dict[str, Any] = {
            "route_group_id": group["route_group_id"],
            "fact_count": len(result.get("facts", ())),
            "context_role": (
                "Historical MAIN investigation, before the current experimental source delivery. "
                "Its assessments and unresolved notes are upstream background, not current "
                "read/write status or a new completion checklist."
            ),
        }
        if group.get("scope_kind") == "INDEPENDENT":
            common["context_role"] = (
                "Editable independent experiments, separate from the route. "
                "Create or edit accounts and their operations here; source evidence is still required."
            )
        if block_ids is None:
            # `segments` is the fusion lane's map of the whole paper: the paper's task
            # is the one with a use for it.
            projected.append(
                {
                    **common,
                    **{key: result[key] for key in ("overview", "segments") if key in result},
                    **{key: result[key] for key in keys[1:] if key in result},
                }
            )
            continue
        group_map = identity_map.get(str(group["route_group_id"]), {})
        segments = {
            str(segment["segment_id"]): [str(f) for f in segment.get("fact_ids", ())]
            for segment in result.get("segments", ())
        }

        local_questions: list[dict[str, Any]] = []
        shared: list[dict[str, Any]] = []
        unplaced: list[str] = []
        for question in result.get("questions", ()):
            where, touched = _placed(
                question.get("subject_ids", ()), segments, group_map, naming, mine
            )
            if where == "LOCAL":
                local_questions.append(dict(question))
            elif where == "SHARED":
                shared.append(
                    {
                        "question_id": str(question["question_id"]),
                        "related_public_block_ids": touched,
                    }
                )
            elif where == "UNPLACED":
                unplaced.append(str(question["question_id"]))
        local_assessments = [
            dict(assessment)
            for assessment in result.get("experiment_assessments", ())
            if _placed(assessment.get("subject_ids", ()), segments, group_map, naming, mine)[0]
            == "LOCAL"
        ]
        projected.append(
            {
                **common,
                **{
                    key: result[key]
                    for key in ("overview", "source_errors", "note")
                    if key in result
                },
                "questions": local_questions,
                "experiment_assessments": local_assessments,
                "shared_question_ids": shared,
                "unmapped_question_ids": unplaced,
            }
        )
    views = [detail_context_view(group, purpose) for group in projected]
    if purpose == "FINAL_MAPPING":
        for group in views:
            group["context_role"] = (
                "Existing group identity and route map. Map the supplied scientific "
                "records; this is not a new source investigation."
            )
    return views


def _field_schema(
    type_name: str, field_key: str, profile: MachineProfile = MachineProfile.V1
) -> str:
    properties: Any = fact_definition(type_name, profile)["properties"]
    schema = properties.get(field_key, {})
    reference = schema.get("$ref", "")
    if not reference.startswith("#/$defs/ResolvedValue_"):
        raise ValueError("only offered experimental container fields are writable")
    return str(reference).rsplit("/", 1)[-1]


def container_fields(
    machine: Mapping[str, Any] | None, block_ids: tuple[str, ...] | None = None
) -> list[dict[str, Any]]:
    if machine is None:
        return []
    containers = scoped_blocks(machine, block_ids, include_accounts=True)
    scoped = {"domain_payload": {
        "reaction_blocks": [c for c in containers if "reaction_block_id" in c],
        "experimental_accounts": [c for c in containers if "account_id" in c],
        "unassigned_outcomes": machine["domain_payload"].get("unassigned_outcomes", ())
        if block_ids is None else (),
    }}
    result = []
    for identity, (kind, container) in _containers(scoped).items():
        for key, value in container.items():
            if not isinstance(value, dict) or "evidence_uses" not in value or "state" not in value:
                continue
            schema_name = _field_schema(kind, key, machine_profile(machine))
            schema: Any = fact_definition(schema_name, machine_profile(machine))
            offered: dict[str, Any] = {
                "target_id": identity,
                "object_type": kind,
                "field_key": key,
                "current": value,
                "value_schema": schema["properties"]["value"],
            }
            actions = CONTAINER_ACTIONS[kind]
            if actions is not None:
                offered["actions"] = list(actions)
            result.append(offered)
    return result


def _compound_label(compound: Mapping[str, Any]) -> str:
    return compound_label(compound) or str(compound.get("compound_id") or "?")


ENDPOINT_FIELDS = ("input_compound_ids", "output_compound_ids")
# The two ends of a compound's own place in the route, written by the projection from
# the blocks' endpoints (`fact_projection.py`) and read back here as one hop.
ADJACENCY_FIELDS = ("predecessor_ids", "successor_ids")


def _endpoint_ids(block: Mapping[str, Any], field_key: str) -> list[str]:
    """The compounds one end of a step names, in the route's own ids.

    An endpoint is a ResolvedValue like every other offered field, so an unresolved one
    names no compound and this returns nothing rather than a guess -- the step's
    `missing` is where that shows. Read out here once because two things now depend on
    it: the step line the author would recognize, and the compound records a block task
    is handed in place of the whole route's catalog (E108).
    """
    value = block.get(field_key)
    ids = value.get("value") if isinstance(value, Mapping) else None
    return [str(item) for item in (ids or ())]


def step_line(block: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    """The step in the author's own numbers: what goes in, what comes out.

    The experimental part titles its procedures by the compound the step makes
    ("Compound 39: ..."), so naming the step the way the author does is what lets the
    investigation find the passage instead of searching for it (E96). The arrow is the
    one the author draws (owner ruling of 2026-09-26): "step 9a, 9b → 11a".
    """
    return step_name(
        [
            ", ".join(labels.get(i, i) for i in _endpoint_ids(block, key)) or "?"
            for key in ENDPOINT_FIELDS
        ]
    )


def _readable(value: Any, route: RouteAccount | None = None) -> Any:
    """A recorded value as the author wrote it, not as the host normalized it.

    The investigation reads this to decide whether a field is already right, needs
    refining or contradicts the source; the normalized decimals, units and evidence
    hashes behind it are the host's bookkeeping and say nothing it can act on.
    """
    # Project provenance once, before recursively formatting scientific values.
    # Projecting already-readable evidence uses would discard their original links.
    return _format_readable(_scientific_view(value, route))


def _format_readable(value: Any) -> Any:
    if isinstance(value, Mapping):
        if "verbatim_name" in value:
            if set(value) - {
                "verbatim_name",
                "names",
                "amount",
                "equivalents",
                "role",
                "compound_id",
                "sequence_index",
            }:
                return value
            names = list(
                dict.fromkeys(
                    name for name in (value.get("verbatim_name"), *value.get("names", [])) if name
                )
            )
            parts = [" / ".join(names) or "?"]
            for key in ("amount", "equivalents"):
                rendered = _format_readable(value.get(key))
                if rendered:
                    parts.append(str(rendered))
            role = value.get("role")
            return " ".join(parts) + (f" [{role}]" if role else "")
        if "raw_text" in value and set(value) <= {
            "raw_text",
            "numeric_value_decimal",
            "range_min_decimal",
            "range_max_decimal",
            "unit",
            "normalized_value_decimal",
            "normalized_range_min_decimal",
            "normalized_range_max_decimal",
            "normalized_unit",
        }:
            return value.get("raw_text")
        return {
            key: _format_readable(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list | tuple):
        return [_format_readable(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RouteAccount:
    """What a block task is told of the route beyond its offered fields.

    The detail lane is the route's muscle: it fills in what the main text left open and
    must not contradict what the main text already says. A block task saw each field's
    value and which fields were empty, and nothing else (windows-muscle, 2026-09-23):
    not how the route put the operation (136 operations on six f09 runs, their wording
    on no task sheet), not the conditions the route kept without a field of their own,
    not whether an empty field was empty or two readings the route had not settled, and
    not where a recorded value was read. All four are the Host's to derive, so they are
    handed over rather than asked for.
    """

    # Public operation id -> how the route reading put it, in its own words. Those are
    # the fusion's words about the author's text, not the author's words.
    wording: Mapping[str, tuple[str, ...]]
    # Observation id -> where in the paper it was read (`OBSERVATION_SITES`).
    read_in: Mapping[str, str]
    # Actual authorized read targets, supplied by the task's catalog, not inferred
    # from a source category or reconstructed from a public observation id.
    read_at: Mapping[str, tuple[Mapping[str, Any], ...]]
    material_sources: Mapping[str, Sequence[Mapping[str, Any]]] = dataclass_field(
        default_factory=dict
    )


# Where a recorded value was read, as a block task is told it. The main text's
# narrative and its figures are what the route was built from; the experimental part
# and the supplement are what the detail lane reads.
OBSERVATION_SITES = ("NARRATIVE", "FIGURE", "EXPERIMENTAL_PART", "SUPPLEMENT")


def route_account(
    groups: Sequence[Mapping[str, Any]],
    identity_map: Mapping[str, Mapping[str, str]],
    observations: Sequence[Mapping[str, Any]],
    experimental_part: Sequence[str],
    *,
    read_at: Mapping[str, tuple[Mapping[str, Any], ...]] | None = None,
    material_sources: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> RouteAccount:
    """The route's own account of each public operation, and where each observation was
    read -- derived from what the round was dispatched with, nothing asked of a model.

    The wording goes through the Host's map from fusion fact to published id, the same
    map that places the route's questions (`project_detail_group_context`), so an
    operation is matched to the fusion fact it was published from and never to a
    look-alike. Where an observation was read follows from its own record: a supplement
    is the supplement, a main-text image is a figure, and a main-text passage is the
    experimental part when its block stands past the cut, the narrative otherwise.
    """
    wording: dict[str, list[str]] = {}
    for group in groups:
        result = group.get("result")
        if not isinstance(result, Mapping):
            continue
        public_ids = identity_map.get(str(group["route_group_id"]), {})
        for fact in result.get("facts", ()):
            if fact.get("kind") != "OPERATION" or fact.get("status") != "ACCEPTED":
                continue
            public = public_ids.get(str(fact["fact_id"]))
            text = fact.get("text")
            if public is not None and isinstance(text, str) and text.strip():
                wording.setdefault(public, []).append(text.strip())
    part = frozenset(experimental_part)
    read_in: dict[str, str] = {}
    for observation in observations:
        if observation.get("source_phase") == "SI":
            site = "SUPPLEMENT"
        elif observation.get("kind") == "VISUAL":
            site = "FIGURE"
        else:
            anchor = observation.get("anchor")
            fragments = anchor.get("fragments", ()) if isinstance(anchor, Mapping) else ()
            blocks = {str(f.get("block_id")) for f in fragments if isinstance(f, Mapping)}
            site = "EXPERIMENTAL_PART" if blocks & part else "NARRATIVE"
        read_in[str(observation["observation_id"])] = site
    return RouteAccount(
        wording={key: tuple(texts) for key, texts in wording.items()},
        read_in=read_in,
        read_at=read_at if read_at is not None else {},
        material_sources=material_sources or {},
    )


def _read_where(uses: Sequence[Mapping[str, Any]], read_in: Mapping[str, str]) -> list[str]:
    """Where the evidence a value stands on was read, each site once, in citation order."""
    return list(
        dict.fromkeys(
            read_in[str(ref["observation_id"])]
            for ref in (
                use.get("evidence_ref", use) if isinstance(use, Mapping) else {} for use in uses
            )
            if str(ref.get("observation_id")) in read_in
        )
    )


def _read_at(uses: Sequence[Mapping[str, Any]], route: RouteAccount | None) -> list[dict[str, Any]]:
    """The same support citations as the value, with each actual read target once."""
    missing = {"unavailable": "The supporting source is not readable in this task."}
    found: dict[bytes, dict[str, Any]] = {}
    available = route.read_at if route is not None else {}
    for use in uses:
        ref = use.get("evidence_ref", use)
        for entry in available.get(str(ref.get("observation_id"))) or (missing,):
            found.setdefault(canonical_json_bytes(entry), dict(entry))
    return list(found.values()) or [missing]


def _offered_state(
    container: Mapping[str, Any],
    offered: Mapping[str, Any],
    route: RouteAccount | None = None,
    material_ids: bool = False,
    *,
    science_questions: bool = True,
    final_mapping: bool = False,
    recording: bool = False,
    material_bindings: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """What a sheet says of a container's offered fields: the values recorded, and the
    fields still open.

    Given the route's account (a block task's sheet), it says two more things. A field
    whose readings the route holds and has not settled is under `in_dispute` with those
    readings, not under `missing`: it is not empty, and "nobody has written this yet"
    and "the route has two answers" are different jobs. `known_from` names the source
    categories; `known_at` offers the actual supporting passages or figures to read.
    """
    known: dict[str, Any] = {}
    missing: list[str] = []
    in_dispute: dict[str, list[Any]] = {}
    recorded_states: dict[str, Any] = {}
    known_from: dict[str, list[str]] = {}
    known_at: dict[str, list[dict[str, Any]]] = {}
    material_known_at: dict[str, Any] = {}
    for key in offered:
        value = container.get(key)
        if not isinstance(value, Mapping):
            continue
        if recording and value.get("candidates"):
            # PRESENT can retain competing interpretations too. Keep the whole
            # explicit declaration alongside its selected-value summary.
            recorded_states[key] = _scientific_view(value, route)
        if value.get("state") == "PRESENT":
            known[key] = _readable(value.get("value"), route)
            inverse = {
                position: alias
                for alias, position in (material_bindings or {}).items()
                if position is not None
            }

            def named_position(index: int, names: dict[str, str] = inverse) -> str:
                position = material_position(str(container["operation_id"]), index)
                return names.get(position, position)

            if key == "materials" and route is not None and "operation_id" in container:
                rows = route.material_sources.get(str(container["operation_id"]), ())
                for index, raw in enumerate(value["value"]):
                    fields = matching_material_sources(rows, index, raw)
                    material_known_at[named_position(index + 1)] = {
                        field: _read_at(fields.get(field, []), route)
                        for field in (*MATERIAL_SOURCE_FIELDS, *MATERIAL_RELATION_FIELDS)
                        if raw.get(field) is not None
                    }
            if key == "materials" and material_ids and "operation_id" in container:
                for shown in known[key]:
                    if isinstance(shown, dict) and "summary_material_index" in shown:
                        shown["detail_of"] = named_position(shown.pop("summary_material_index"))
                known[key] = {
                    named_position(index): shown for index, shown in enumerate(known[key], 1)
                }
            if route is not None:
                support = [
                    use for use in value.get("evidence_uses", ()) if use.get("role") == "SUPPORT"
                ]
                if where := _read_where(support, route.read_in):
                    known_from[key] = where
                known_at[key] = _read_at(support, route)
        elif recording:
            if value.get("state") != "UNSTATED":
                recorded_states[key] = _scientific_view(value, route)
        elif route is not None and value.get("reason_code") == "CONFLICTING_CANDIDATES":
            in_dispute[key] = [
                _readable(c.get("value"), route) for c in value.get("candidates", ())
            ]
        elif science_questions:
            missing.append(key)
    state: dict[str, Any] = {"known": known}
    if science_questions and not (final_mapping or recording):
        state["missing"] = missing
    if recorded_states:
        state["recorded_states"] = recorded_states
    if in_dispute:
        state["recorded_alternatives" if final_mapping else "in_dispute"] = in_dispute
    if known_from:
        state["known_from"] = known_from
    if known_at:
        state["known_at"] = known_at
    if material_known_at:
        state["material_known_at"] = material_known_at
    return state


def _scientific_view(value: Any, route: RouteAccount | None) -> Any:
    """Keep scientific structure intact; turn each record's evidence into read links."""
    if isinstance(value, Mapping):
        result = {
            key: _scientific_view(item, route)
            for key, item in value.items()
            if key
            not in {
                "stable_key",
                "boundary",
                "evidence_refs",
                "boundary_evidence_refs",
                "relation_evidence_refs",
                "evidence_uses",
                "source_observation_refs",
                "model_record_refs",
                "model_submission_refs",
                "relation_record_refs",
                "relation_submission_refs",
                "relation_field_bases",
            }
            and item is not None
        }
        # Each use retains its own role and source. The aggregate known_at view
        # must not collapse SUPPORT and CONFLICT into an undifferentiated union.
        if "evidence_uses" in value:
            result["evidence_uses"] = [
                {**{key: _scientific_view(item, route) for key, item in use.items()
                    if key != "evidence_ref"}, "known_at": _read_at([use], route)}
                for use in value["evidence_uses"]
            ]
        if "source_observation_refs" in value:
            result["source_links"] = [
                {"known_at": _read_at([ref], route)}
                for ref in value["source_observation_refs"]
            ]
        refs = value.get("evidence_refs", value.get("boundary_evidence_refs", ()))
        if refs and route is not None:
            result["known_at"] = _read_at(refs, route)
            if where := _read_where(refs, route.read_in):
                result["known_from"] = where
        relation_refs = value.get("relation_evidence_refs", ())
        if relation_refs and route is not None:
            result["relation_known_at"] = _read_at(relation_refs, route)
        if value.get("relation_field_bases"):
            result["relation_sources"] = {
                field: {
                    "precision": basis["precision"],
                    **({"original_fields": list(basis["original_fields"])}
                       if "original_fields" in basis else {}),
                    "known_at": _read_at(basis.get("evidence_refs", ()), route),
                }
                for field, basis in value["relation_field_bases"].items()
            }
        return result
    if isinstance(value, (list, tuple)):
        return [_scientific_view(item, route) for item in value]
    return value


def _route_notes(operation: Mapping[str, Any], route: RouteAccount | None) -> dict[str, Any]:
    """Public condition/source meanings are visible with or without a route account."""
    notes: dict[str, Any] = {}
    if route and (wording := route.wording.get(str(operation["operation_id"]))):
        notes["route_wording"] = list(wording)
    for key in ("other_conditions", "procedure_sources"):
        if operation.get(key):
            notes[key] = _scientific_view(operation[key], route)
    return notes


def characterization_view(
    machine: Mapping[str, Any] | None,
    route: RouteAccount | None = None,
    compound_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Actionable baseline cards, without hashes or a second scientific identity key."""
    if machine is None:
        return []
    return [
        _scientific_view(card, route)
        for card in machine["domain_payload"].get("characterization_records", ())
        if compound_ids is None or set(card["compound_ids"]) <= compound_ids
    ]


def container_view(
    machine: Mapping[str, Any] | None,
    block_ids: tuple[str, ...] | None = None,
    route: RouteAccount | None = None,
    *,
    material_ids: bool = False,
    paragraph_locator: bool = False,
    final_mapping: bool = False,
    recording: bool = False,
    material_bindings: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """The task an experimental investigation is given: the route, step by step.

    One entry per reaction block -- what the step makes, its operations in order, and
    its measurements -- each saying what is already recorded and which offered fields
    are still empty. The field-by-field dump this replaced sent every offered field as
    its own record with the whole ResolvedValue, its evidence hashes and a copy of its
    JSON schema: 385 records and 221 KB on one paper, two thirds of it fields that were
    already answered, and not one line saying what any step was (E96). The shapes now
    travel once under `field_shapes`, and the ids the investigation writes back --
    `target_id`, `field_key` -- are unchanged.

    A step of a block task also carries `compounds`: the two or three compound records
    its own endpoints name, as the one compound projection shows them (`model_view`).
    That is the whole of what the paper's compound catalog was ever consulted for, and
    handing it over here is what lets a block task be given no
    `existing_public_containers` at all (E108).

    Given the route's account, a block task's sheet also says what the route already
    holds and how (`RouteAccount`): each operation's wording in the route, the conditions
    kept without a field, the fields in dispute and where each recorded value was read.
    """
    if machine is None:
        return {"containers": [], "field_shapes": {}}
    offered_by_id: dict[str, dict[str, Any]] = {}
    shapes: dict[str, Any] = {}
    actions: dict[str, list[str]] = {}
    for field in container_fields(machine, block_ids):
        offered_by_id.setdefault(field["target_id"], {})[field["field_key"]] = field
        shapes.setdefault(field["field_key"], field["value_schema"])
        if field.get("actions"):
            actions[field["target_id"]] = list(field["actions"])
    labels = {
        str(compound["compound_id"]): _compound_label(compound)
        for compound in machine["domain_payload"]["compounds"]
    }
    # The compound records a step's own endpoints name, with the host's evidence hashes
    # taken out of each. A block task used to be handed the paper's entire compound
    # catalog, its routes and their evidence as `existing_public_containers`, and
    # because one step's slice of that still weighs 21-40 KB it arrived over the task
    # context limit and became a menu: on the e106 round the model spent 74 of its 223
    # turns walking that menu, every one of them landing on a host `result:` location,
    # which by construction mints no selection_id and so can never answer with citable
    # evidence. Two or three records of about 330 bytes are what the step actually
    # refers to, and they now travel with the step (E108). Wording cannot fix this:
    # E100 deleted the prompt sentence that sent the model there and it went anyway,
    # so the records are handed over rather than made fetchable.
    known_compounds = {
        str(compound["compound_id"]): compound_view(compound)
        for compound in machine["domain_payload"]["compounds"]
    }
    # Whoever is handed the account gets it on its sheet: a block task for its own steps,
    # and a task reading the whole paper for the whole route (batch 2A, 2d, E203: the
    # middle layer was shown the route bare while the layers under it saw the account,
    # and "the sheet must stay byte-for-byte what the recorded export measured" was a
    # reason to record a new export, not a reason to keep a downstream task blind).
    account = route
    containers = []
    for block in scoped_blocks(machine, block_ids, include_accounts=True):
        identity_field = "reaction_block_id" if "reaction_block_id" in block else "account_id"
        block_id = str(block[identity_field])
        entry: dict[str, Any] = {
            identity_field: block_id,
            "step": step_line(block, labels) if identity_field == "reaction_block_id"
            else "experiment " + str(block["title"]),
            **_offered_state(
                block,
                offered_by_id.get(block_id, {}),
                account,
                science_questions=not paragraph_locator, final_mapping=final_mapping,
                recording=recording,
            ),
        }
        # Only a block task: the paper's sheet keeps the containers themselves, and the
        # records a step's own endpoints name travel with the step.
        if block_ids is not None:
            cited = dict.fromkeys(
                compound_id
                for key in ENDPOINT_FIELDS
                for compound_id in _endpoint_ids(block, key)
                if compound_id in known_compounds
            )
            entry["compounds"] = [known_compounds[compound_id] for compound_id in cited]
        if not (paragraph_locator or recording) and block_id in actions:
            entry["actions"] = actions[block_id]
        entry["operations"] = [
            {
                "target_id": operation["operation_id"],
                "order": operation.get("sequence_index"),
                **(
                    {"stages": [_scientific_view(stage, account) for stage in operation["stages"]]}
                    if operation.get("stages")
                    else {}
                ),
                **_route_notes(operation, account),
                **_offered_state(
                    operation,
                    offered_by_id.get(operation["operation_id"], {}),
                    account,
                    material_ids,
                    material_bindings=material_bindings,
                    science_questions=not paragraph_locator, final_mapping=final_mapping,
                    recording=recording,
                ),
            }
            for operation in block["operations"]
        ]
        entry["measurements"] = [
            {
                "target_id": outcome["outcome_id"],
                **(
                    {"source_statements": _scientific_view(outcome["source_statements"], account)}
                    if outcome.get("source_statements")
                    else {}
                ),
                **(
                    {
                        "model_interpretations": _scientific_view(
                            outcome["model_interpretations"], account
                        )
                    }
                    if outcome.get("model_interpretations")
                    else {}
                ),
                **_offered_state(
                    outcome,
                    offered_by_id.get(outcome["outcome_id"], {}),
                    account,
                    science_questions=not paragraph_locator, final_mapping=final_mapping,
                    recording=recording,
                ),
            }
            for outcome in block["reported_outcomes"]
        ]
        for operation in entry["operations"]:
            competing = [
                c
                for c in machine["domain_payload"].get("field_conflicts", ())
                if c["target_object_id"] == operation["target_id"]
                and not c["blocking"]
                and c["target_object_type"] == "OPERATION"
            ]
            if competing and recording:
                operation["recorded_conflicts"] = [
                    {key: _scientific_view(c[key], account)
                     for key in ("field_key", "blocking", "candidate_values") if key in c}
                    for c in competing
                ]
            elif competing and not final_mapping:
                operation["needs_review"] = [
                    {
                        "field": c["field_key"],
                        "blocking": False,
                        "reason": (
                            "Nonblocking field review: inspect the recorded value "
                            "and candidate evidence."
                        ),
                        "candidates": [
                            {
                                "value": _readable(v["value"]),
                                **(
                                    {"known_at": _read_at(v["evidence_uses"], account)}
                                    if account
                                    else {}
                                ),
                            }
                            for v in c["candidate_values"]
                        ],
                    }
                    for c in competing
                ]
        containers.append(entry)
    return {
        "containers": containers,
        "field_shapes": shapes,
        **({"unassigned_measurements": [
            {"target_id": outcome["outcome_id"],
             **_offered_state(outcome, offered_by_id.get(outcome["outcome_id"], {}), account,
                              science_questions=not paragraph_locator, final_mapping=final_mapping,
                              recording=recording)}
            for outcome in machine["domain_payload"].get("unassigned_outcomes", ())
        ]} if block_ids is None and machine["domain_payload"].get("unassigned_outcomes") else {}),
        "paper_narratives": [
            _scientific_view(n, route)
            for n in machine["domain_payload"].get("paper_narratives", [])
        ],
    }


def outcome_value_schema() -> dict[str, Any]:
    """The offered new measurement's values, without unrelated ResolvedValue metadata."""
    properties = {}
    for key in (*OUTCOME_REQUIRED_FIELDS, *OUTCOME_OPTIONAL_FIELDS):
        definition: Any = fact_definition(_field_schema("ReportedOutcome", key))
        properties[key] = definition["properties"]["value"]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(OUTCOME_REQUIRED_FIELDS),
        "properties": properties,
    }


def _refs(receipt: HoldsSelections, selection_ids: tuple[str, ...]) -> list[dict[str, str]]:
    refs = []
    for sid in selection_ids:
        if sid not in receipt.selections:
            raise ValueError("increment cites a source that was not read and verified")
        reference: Any = receipt.selections[sid]["evidence_ref"]
        if reference not in refs:
            refs.append(reference)
    return refs


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def characterization_key(record: Mapping[str, Any]) -> tuple[tuple[str, ...], str, str | None]:
    """Comparable method/value content, never sufficient to establish measurement identity."""
    compounds = tuple(sorted(dict.fromkeys(record["compound_ids"])))
    method = record.get("method") or "verbatim:" + _fold(record["method_verbatim"])
    value = record.get("reported_value")
    text = value.get("raw_text") if isinstance(value, dict) else None
    return compounds, str(method), _fold(text) if isinstance(text, str) and text else None


def matching_card(
    cards: Mapping[str, Mapping[str, Any]],
    candidate: Mapping[str, Any],
    same_measurement_as: str | None = None,
    *, explicit_identity: bool = False,
) -> Mapping[str, Any] | None:
    """Follow an explicit scientific association; content similarity never creates one."""
    if same_measurement_as is None:
        return None
    record = cards.get(same_measurement_as)
    if record is None:
        raise ValueError("same_measurement_as must name an existing characterization card")
    if explicit_identity:
        if set(record["compound_ids"]) != set(candidate["compound_ids"]):
            raise ValueError("selected characterization and submitted compounds differ")
        return record
    if characterization_key(record)[:2] != characterization_key(candidate)[:2]:
        raise ValueError("same_measurement_as must have the same compounds and method")
    for field in ("reported_value", "reported_formula", "reported_ion", "comparator"):
        old, new = record.get(field), candidate.get(field)
        if old is not None and new is not None and old != new:
            # Author renderings of one parsed scalar may differ; compare the full
            # parsed value only when a numeric interpretation actually exists.
            if field == "reported_value" and isinstance(old, Mapping) and isinstance(new, Mapping):
                numeric = ("numeric_value_decimal", "range_min_decimal", "range_max_decimal")
                if any(old.get(k) is not None for k in numeric) and {
                    k: v for k, v in old.items() if k != "raw_text"
                } == {k: v for k, v in new.items() if k != "raw_text"}:
                    continue
            raise ValueError(
                f"same_measurement_as has a conflicting {field}; keep the reading independent"
            )
    return record


def merge_card(existing: Mapping[str, Any], incoming: Mapping[str, Any], *, update_current: bool = False, replace_sample_context: bool = False) -> dict[str, Any]:
    """Keep the existing card's identity, filled values and original words; fill empty
    slots and unite evidence and accession ids. Replace words only to gain a value."""
    merged: dict[str, Any] = dict(existing)
    same_method = existing.get("method") == incoming.get("method")
    for field in (
        "reported_value",
        "reported_formula",
        "reported_ion",
        "comparator",
        "sample_context",
    ):
        if update_current and (field != "sample_context" or replace_sample_context):
            merged[field] = incoming.get(field)
        elif same_method and merged.get(field) is None and incoming.get(field) is not None:
            merged[field] = incoming[field]
    # A newly readable value can replace a stub's words. Otherwise length says nothing
    # about scientific detail; preserve the skeleton's wording and fill its empty slots.
    reads = incoming.get("reported_value") is not None
    read_before = existing.get("reported_value") is not None
    if update_current:
        merged["method"] = incoming.get("method")
    if update_current or (same_method and reads and not read_before):
        merged["method_verbatim"] = incoming["method_verbatim"]
    merged["evidence_refs"] = list(
        {
            content_hash(ref): ref
            for ref in (*existing.get("evidence_refs", []), *incoming.get("evidence_refs", []))
        }.values()
    )
    for key in ("source_statements", "model_interpretations"):
        joined = merge_interpretations(existing.get(key, []), incoming.get(key, []))
        if joined:
            merged[key] = joined
    accessions = [*existing.get("accession_ids", []), *incoming.get("accession_ids", [])]
    if accessions:
        merged["accession_ids"] = list(dict.fromkeys(accessions))
    return merged


def _new_operation(
    operation_id: str, position: int, description: str, refs: list[dict[str, str]]
) -> dict[str, Any]:
    """A public operation the detail investigation adds: every field pending until
    patched, its evidence the step's, its description the author's words."""
    record: dict[str, Any] = {
        "operation_id": operation_id,
        "sequence_index": position,
        "materials": pending(),
        "temperature_profile": pending(),
        "duration": pending(),
        "pressure": pending(),
        "concentration": pending(),
        "scale": pending(),
        "atmosphere": pending(),
        "key_addition": pending(),
        "key_workup": pending(),
        "operation_intermediate": pending(),
        "reaction_class": None,
        "other_conditions": [],
        "evidence_refs": refs,
        "description": description,
    }
    validate_fact_definition("Operation", record)
    return record


def apply_container_enrichment(
    machine: Mapping[str, Any],
    observations: tuple[Mapping[str, Any], ...],
    receipt: ReceiptEvidence,
    *,
    ledgers: EnrichmentLedgers | None = None,
) -> dict[str, Any]:
    """The strict form: the first increment the projection refuses is the error.

    The write loop accepts one item at a time, so the refusal is that item's diagnostic.
    """
    result, failures, _ = apply_container_enrichment_items(
        machine, observations, receipt, ledgers=ledgers
    )
    if failures:
        raise ValueError(failures[0]["reason"])
    return result


def _spoken(value: Any) -> str | None:
    """The author's words a public field value carries: `raw_text` of a parsed scalar,
    the string itself, each entry of a profile joined; None when it carries none."""
    if isinstance(value, list):
        parts = [_spoken(entry) for entry in value]
        return "; ".join(part for part in parts if part is not None) or None
    if isinstance(value, Mapping):
        text = value.get("raw_text")
        return text if isinstance(text, str) else None
    return value if isinstance(value, str) else None


def _condition_kind_of(type_name: str, field_key: str) -> str | None:
    """The kind of condition an operation's fixed field holds, from the one table;
    None for any other field of any container."""
    if type_name != "Operation":
        return None
    return next((key for key, field in FIXED_CONDITION_FIELDS.items() if field == field_key), None)


def _field_as_condition(
    category: str, field: Mapping[str, Any], *, about: str | None = None
) -> dict[str, Any]:
    """A filled fixed field, or a value bound for one, in the shape of a condition note
    -- the one shape `same_condition`, `register_unassigned` and the finalize scan
    reason about, whichever path the value came through."""
    return {
        "category_verbatim": category,
        "category": category,
        "text": _spoken(field.get("value")) or "",
        "about": about,
        "extent": "WHOLE_OWNERS",
        "evidence_refs": [dict(use["evidence_ref"]) for use in field.get("evidence_uses") or ()],
    }


def _updated_condition_sources(
    operation: Mapping[str, Any],
    patch: ContainerPatch,
    profile: MachineProfile,
    *,
    replace_primary: bool = False,
) -> list[dict[str, Any]]:
    """Validate the full source side before committing either side of a field write."""
    notes: list[dict[str, Any]] = copy.deepcopy(operation.get("other_conditions", []))
    sources = getattr(patch, "condition_sources", ())
    if not sources:
        return notes
    if not profile.expression:
        raise ValueError("condition sources require the expression profile")
    category = next(
        (key for key, field in FIXED_CONDITION_FIELDS.items() if field == patch.field_key), None
    )
    if category is None:
        raise ValueError("condition sources require a fixed condition target")
    for note in sources:
        validate_fact_definition("ConditionNote", note, profile=profile)
        if note["category"] != category or expressed_value(note["extent"]) != "WHOLE_OWNERS" or note.get("stage_id"):
            raise ValueError("condition source does not match the whole fixed field")
    if replace_primary and any(n.get("adoption") == "ADOPTED" for n in sources):
        for note in notes:
            if (
                note.get("category") == category
                and expressed_value(note["extent"]) == "WHOLE_OWNERS"
                and note.get("adoption") in {"ADOPTED", "SUPPORTING"}
            ):
                note["adoption"] = "ALTERNATIVE"
    for note in sources:
        if note not in notes:
            notes.append(copy.deepcopy(note))
    return notes


def place_whole_operation_value(
    operation: dict[str, Any],
    category: str,
    incoming: dict[str, Any],
    *,
    baseline_present: bool = False,
) -> str:
    """Where a whole-operation value of a fixed kind goes on this operation: MERGED
    into a note that already says it, UNASSIGNED because a stretch owns the kind, or
    FILE in its named field. One judgement for both write paths (a patch of the
    field and a WHOLE_OWNERS condition of the kind), ledger E157.

    1. `same_condition` (E114, one rule): the incoming value is unplaced, so it can
       meet only a note that is unplaced or whole, never one already on a stage. A
       WITHIN_OWNER note that says the same is where the words belong -- the value
       joins it and the field stays as it was (E117, ruling two); a WHOLE_OWNERS note
       that says the same is absorbed into the named field with its evidence.
    2. `stage_covers`: a stretch of this operation already owns this kind, so the
       value is not filed over what the stretches say; the caller keeps it
       unassigned, visibly.
    3. Otherwise the field is its home.

    `incoming` is a note-shaped dict; its `evidence_refs` are extended by what an
    absorbed whole note cited, so the caller files the value with that evidence.
    """
    if baseline_present:
        return "FILE"
    matches = [
        note
        for note in operation["other_conditions"]
        if incoming["text"] and note.get("category") == category and same_condition(note, incoming)
    ]
    narrow = [note for note in matches if expressed_value(note["extent"]) == "WITHIN_OWNER"]
    if narrow:
        for note in narrow:
            note.update(merge_condition(note, incoming))
        return "MERGED"
    for note in matches:
        for ref in note["evidence_refs"]:
            if ref not in incoming["evidence_refs"]:
                incoming["evidence_refs"].append(ref)
        operation["other_conditions"].remove(note)
    if stage_covers(operation, category):
        return "UNASSIGNED"
    return "FILE"


def _absorbed_uses(field: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """Cite on the field what the note it absorbed cited."""
    for ref in incoming["evidence_refs"]:
        use = {"evidence_ref": ref, "role": "SUPPORT"}
        if not any(existing["evidence_ref"] == ref for existing in field["evidence_uses"]):
            field["evidence_uses"].append(use)


def apply_container_enrichment_items(
    machine: Mapping[str, Any],
    observations: tuple[Mapping[str, Any], ...],
    receipt: ReceiptEvidence,
    *,
    bound_to: Mapping[str, Any] | None = None,
    ledgers: EnrichmentLedgers | None = None,
    applied_locations: dict[tuple[str, int], list[dict[str, str]]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[str]]]:
    """Apply a receipt's increments one by one; a refused one is a failure, not a lost receipt.

    Audit L8-08: one patch the projection refused used to discard the whole receipt,
    characterization cards included. Every accepted increment lands; each refused one is
    returned with the refusal sentence and the public subject it concerned, for the
    publication to record as an unresolved item. A stale baseline is not an increment's
    fault and still refuses the receipt whole. The third value maps each accepted
    characterization card to the public record ids it landed in (a merged card lands in
    the fusion card's id, audit L6-03), for the per-compound packages.

    `bound_to` is the fact set the receipt's task was given, for when that is not
    `machine` itself. A round that runs one task per reaction step dispatches every task
    against one published baseline and applies their receipts in the route's order, so
    every receipt after the first is applied to a machine that already carries the
    increments of the ones before it. The check below asks whether the task read the ids
    and values it wrote against, so it is asked of that common starting point; asked of
    the preceding receipt's output it refused every receipt but the first.

    `ledgers` is the publication's private record of the stages, elisions and
    unassigned values its receipts declare (E157), kept across the receipts of one
    revision; a caller with no publication -- the task loop's preview of its own
    answer -- gets one for this application alone.
    """
    if ledgers is None:
        ledgers = EnrichmentLedgers()
    if machine_profile(machine).expression:
        remount_published_stages(ledgers, machine)
    baseline = machine if bound_to is None else bound_to
    if receipt.baseline_machine_ref is None or receipt.baseline_machine_ref.sha256 != content_hash(
        baseline
    ):
        raise ValueError("experimental increment is bound to a stale or absent machine baseline")
    if receipt.answer is None:
        return copy.deepcopy(dict(machine)), [], {}
    profile = machine_profile(machine)
    result = copy.deepcopy(dict(machine))
    # The notes an earlier receipt of this publication staged get their stage back
    # before anything is routed against them: the working key is not in the published
    # shape and came off at that receipt's finalize; the ledger kept it (E157).
    for block in experiment_containers(result):
        for operation in block["operations"]:
            if profile.expression:
                if "procedure_source" in operation or "procedure_sources" in operation:
                    operation["procedure_sources"] = procedure_readings(operation)
                    operation.pop("procedure_source", None)
                continue
            recall_note_stages(
                ledgers,
                operation["operation_id"],
                operation["other_conditions"],
                stage_key=STAGE_KEY,
                content_hash=content_hash,
            )
    containers = _containers(result)
    baseline_containers = _containers(baseline)

    def baseline_present(operation_id: str, field_key: str) -> bool:
        entry = baseline_containers.get(operation_id)
        return bool(entry and (entry[1].get(field_key) or {}).get("state") == "PRESENT")

    failures: list[dict[str, Any]] = []
    placed: dict[str, list[str]] = {}
    # The compiler's item identity, not its words, connects the actual application
    # to both the current-task feedback and the sealed publication provenance.
    entries = {
        id(item): (collection, index)
        for collection in type(receipt.answer).model_fields
        for values in (getattr(receipt.answer, collection),)
        if isinstance(values, tuple)
        for index, item in enumerate(values)
    }
    locations: dict[tuple[str, int], list[dict[str, str]]] = {}

    def locate(item: Any, target_type: str, target_id: str, field_key: str | None = None) -> None:
        key = entries[id(item)]
        target = {"object_type": target_type, "object_id": target_id}
        if field_key is not None:
            target["field_key"] = field_key
        values = locations.setdefault(key, [])
        if target not in values:
            values.append(target)
    block_of: dict[str, str] = {
        member[id_field]: block["reaction_block_id"]
        for block in result["domain_payload"]["reaction_blocks"]
        for members, id_field in (
            (block["operations"], "operation_id"),
            (block["reported_outcomes"], "outcome_id"),
        )
        for member in members
    }
    public_compounds = {c["compound_id"] for c in result["domain_payload"]["compounds"]}

    def subject(block_id: str | None = None, compound_id: str | None = None) -> tuple[str, str]:
        if block_id is not None:
            return "REACTION_BLOCK", block_id
        if compound_id is not None:
            return "COMPOUND", compound_id
        return "PAPER", receipt.paper_id

    def failed(
        collection: str, index: int, error: ValueError, scope: str, subject_id: str, sids: Any
    ) -> None:
        try:
            refs = _refs(receipt, tuple(sids))
        except ValueError:
            refs = []
        failures.append(
            {
                "collection": collection,
                "index": index,
                "reason": str(error),
                "scope": scope,
                "subject_id": subject_id,
                "evidence_refs": refs,
            }
        )

    observation_phases = {o["observation_id"]: o["source_phase"] for o in observations}
    observation_phases.update(
        {o["observation_id"]: o["source_phase"] for o in receipt.observations}
    )
    touched: set[tuple[str, str]] = set()

    def mark_accepted(item: Any, target_type: str, target_id: str, field_key: str) -> None:
        if isinstance(item, CompiledCondition) and item.review_reason and profile.wrapping:
            ledgers.accepted_review_markers.append(
                {
                    "reason": item.review_reason,
                    "evidence_refs": _refs(receipt, item.value_selection_ids or item.selection_ids),
                    "target_object_type": target_type,
                    "target_object_id": target_id,
                    "field_key": field_key,
                }
            )
        if (
            isinstance(
                item,
                (CompiledReadingPatch, CompiledCharacterization, CompiledCompound, CompiledElision),
            )
            and profile.wrapping
        ):
            ledgers.accepted_review_markers.extend(
                {
                    **marker,
                    "target_object_type": target_type,
                    "target_object_id": target_id,
                    "field_key": field_key,
                }
                for marker in item.review_markers
            )

    def mark_patch(patch: Any, kind: str, field_key: str) -> None:
        mark_accepted(patch, CONFLICT_TARGET_TYPES[kind], patch.target_id, field_key)

    # Make explicitly declared compounds available before dependent material/card
    # writes. This is the same projection used by the reading loop, not a second
    # identity table. Failed additions still leave their dependants unbound/refused.
    domain = result["domain_payload"]
    new_compound_ids: dict[str, str] = {}
    for index, added in enumerate(receipt.answer.new_compounds):
        try:
            compound_id = register_compound_addition(
                domain,
                receipt.paper_id,
                added,
                _refs(receipt, added.selection_ids),
                profile=profile,
            )
            new_compound_ids[added.compound_key] = compound_id
            locate(added, "COMPOUND", compound_id)
            mark_accepted(
                added, "COMPOUND", compound_id, "names" if added.names else "author_labels"
            )
        except ValueError as error:
            failed("new_compounds", index, error, "PAPER", receipt.paper_id, added.selection_ids)
    public_compounds = {c["compound_id"] for c in domain["compounds"]}
    public_compound_ids = public_compounds

    containers = _containers(result)

    for index, intermediate in enumerate(getattr(receipt.answer, "local_intermediates", ())):
        try:
            if not profile.unified_expression:
                raise ValueError("local intermediate collection requires the unified profile")
            validate_fact_definition("LocalIntermediate", intermediate.value, profile)
            target = containers.get(intermediate.container_id)
            if target is None or target[0] not in {"ReactionBlock", "ExperimentalAccount"}:
                raise ValueError("local intermediate has no experimental container")
            rows = target[1].setdefault("local_intermediates", [])
            iid = intermediate.value["local_intermediate_id"]
            old = next((i for i, row in enumerate(rows) if row["local_intermediate_id"] == iid), None)
            if old is None:
                rows.append(copy.deepcopy(intermediate.value))
            else:
                rows[old] = copy.deepcopy(intermediate.value)
            locate(intermediate, "LOCAL_INTERMEDIATE", iid)
        except ValueError as error:
            failed("local_intermediates", index, error, "PAPER", receipt.paper_id,
                   intermediate.selection_ids)

    measurements = getattr(receipt.answer, "measurements", ())
    if measurements:
        from tsdaf.contracts.literature.validation import validate_open_expression_relations
        from tsdaf.lanes.literature.experiment_projection import (
            MeasurementProjection, apply_measurement_contributions,
        )
        if ledgers.measurement_baseline is None:
            ledgers.measurement_baseline = copy.deepcopy(result)
        for index, measurement in enumerate(measurements):
            try:
                if not profile.unified_expression:
                    raise ValueError("explicit measurement identity requires the unified profile")
                proposed = dict(ledgers.measurement_contributions)
                proposed[measurement.contribution_key] = MeasurementProjection(**{
                    key: value for key, value in measurement.model_dump().items()
                    if key not in {"contribution_key", "selection_ids"}
                })
                # Keep current non-measurement edits, but replay scientific values
                # from the immutable starting pools and effective accepted items.
                replay = copy.deepcopy(result)
                originals = {
                    c.get("reaction_block_id", c.get("account_id")): c["reported_outcomes"]
                    for c in experiment_containers(ledgers.measurement_baseline)
                }
                for container in experiment_containers(replay):
                    cid = container.get("reaction_block_id", container.get("account_id"))
                    container["reported_outcomes"] = copy.deepcopy(originals.get(cid, []))
                replay["domain_payload"]["unassigned_outcomes"] = copy.deepcopy(
                    ledgers.measurement_baseline["domain_payload"].get("unassigned_outcomes", [])
                )
                candidate = apply_measurement_contributions(replay, proposed)
                validate_open_expression_relations(candidate)
                result.clear()
                result.update(candidate)
                domain = result["domain_payload"]
                ledgers.measurement_contributions = proposed
                locate(measurement, "REPORTED_OUTCOME", measurement.outcome["outcome_id"])
            except ValueError as error:
                failed("measurements", index, error, "PAPER", receipt.paper_id, measurement.selection_ids)
        containers = _containers(result)

    for index, expression in enumerate(getattr(receipt.answer, "compound_expressions", ())):
        try:
            if not profile.preservation:
                raise ValueError("compound source expressions require v1.4")
            compound = next(
                (c for c in domain["compounds"] if c["compound_id"] == expression.compound_id), None
            )
            if compound is None:
                raise ValueError("compound expression has no existing owner")
            changed = copy.deepcopy(compound)
            changed["names"] = list(dict.fromkeys([*changed["names"], *expression.adopted_names]))
            changed["source_statements"] = merge_interpretations(
                changed.get("source_statements", []), [expression.source_statement])
            validate_fact_definition("CompoundRecord", changed, profile=profile)
            compound.clear()
            compound.update(changed)
            locate(expression, "COMPOUND", expression.compound_id, "source_statements")
            if expression.adopted_names:
                locate(expression, "COMPOUND", expression.compound_id, "names")
        except ValueError as error:
            failed(
                "compound_expressions",
                index,
                error,
                "COMPOUND",
                expression.compound_id,
                expression.selection_ids,
            )

    blocks = {b["reaction_block_id"]: b for b in result["domain_payload"]["reaction_blocks"]}
    for index, addition in enumerate(receipt.answer.new_outcomes):
        try:
            block = blocks.get(addition.reaction_block_id)
            if block is None:
                raise ValueError("new outcome must bind an existing reaction container")
            values: dict[str, Any] = addition.value
            required = set(OUTCOME_REQUIRED_FIELDS)
            if (
                not isinstance(values, dict)
                or not required <= values.keys()
                or values.keys() - required - set(OUTCOME_OPTIONAL_FIELDS)
            ):
                raise ValueError(
                    "new outcome must supply the offered measurement and attribution fields"
                )
            operations = {o["operation_id"] for o in block["operations"]}
            products = set(block["output_compound_ids"]["value"] or [])
            if (
                not values["operation_ids"]
                or not set(values["operation_ids"]) <= operations
                or not values["product_compound_ids"]
                or not set(values["product_compound_ids"]) <= products
            ):
                raise ValueError(
                    "new outcome must bind existing operations and products in this reaction"
                )
            for old in block["reported_outcomes"]:
                if (
                    old["kind"]["value"] == values["kind"]
                    and old["operation_ids"]["value"] == values["operation_ids"]
                    and old["product_compound_ids"]["value"] == values["product_compound_ids"]
                    and old["basis"]["value"] == values.get("basis")
                ):
                    raise ValueError(
                        "this measurement scope already exists; use SAME, REFINE or CONFLICT"
                    )
            references = _refs(receipt, addition.selection_ids)
            outcome: dict[str, Any] = {
                "outcome_id": "OUT-ENR-"
                + content_hash([receipt.paper_id, addition.reaction_block_id, values])[:24],
                "value_provenance": "REPORTED",
                "evidence_refs": references,
            }
            for field_name in required | set(OUTCOME_OPTIONAL_FIELDS):
                # Each of a measurement's fields is written the way a patch of that same
                # field is written, and read back the same way (E106).
                written = project_written_value(
                    _field_schema("ReportedOutcome", field_name, profile=profile),
                    values.get(field_name),
                    references,
                    field_key=field_name,
                )
                outcome[field_name] = (
                    present(written, references)
                    if written is not None
                    else {
                        "state": "UNRESOLVED",
                        "value": None,
                        "reason_code": "STAGE_PARTIAL",
                        "coverage_scope_ref": None,
                        "evidence_uses": [],
                        "candidates": [],
                    }
                )
            validate_fact_definition("ReportedOutcome", outcome, profile=profile)
            block["reported_outcomes"].append(outcome)
            locate(addition, CONFLICT_TARGET_TYPES["ReportedOutcome"], outcome["outcome_id"])
        except ValueError as error:
            scope, subject_id = subject(
                addition.reaction_block_id if addition.reaction_block_id in blocks else None
            )
            failed("new_outcomes", index, error, scope, subject_id, addition.selection_ids)

    def compound_ref(token: str) -> str:
        if token in new_compound_ids:
            return new_compound_ids[token]
        if token in public_compound_ids:
            return token
        raise ValueError(f"{token} is no public compound and no compound_key of this answer")

    new_block_ids: dict[str, str] = {}
    new_operation_ids: dict[str, str] = {}
    for index, added_block in enumerate(receipt.answer.new_reaction_blocks):
        try:
            inputs = [compound_ref(t) for t in dict.fromkeys(added_block.input_compound_ids)]
            outputs = [compound_ref(t) for t in dict.fromkeys(added_block.output_compound_ids)]
            block_id = "RXB-ENR-" + content_hash([receipt.paper_id, added_block.block_key])[:24]
            refs = _refs(receipt, added_block.selection_ids)
            prior_block: dict[str, Any] | None = blocks.get(block_id)
            block_operations = []
            for position, step in enumerate(added_block.operations, 1):
                operation_id = "OP-ENR-" + content_hash([receipt.paper_id, step.operation_key])[:24]
                block_operations.append(
                    _new_operation(operation_id, position, step.description, refs)
                )
                new_operation_ids[step.operation_key] = operation_id
            block_record: dict[str, Any] = {
                "reaction_block_id": block_id,
                "source_order": prior_block["source_order"]
                if prior_block is not None
                else len(domain["reaction_blocks"]) + 1,
                "input_compound_ids": present(inputs, refs),
                "output_compound_ids": present(outputs, refs),
                "operations": block_operations,
                "reported_outcomes": prior_block["reported_outcomes"]
                if prior_block is not None
                else [],
                "evidence_refs": refs,
                "optimization_context_ids": [],
                "failed_attempt_context_ids": [],
                "mechanism_context_ids": [],
                "summary": added_block.summary,
            }
            validate_fact_definition("ReactionBlock", block_record, profile=profile)
            domain["reaction_blocks"] = [
                b for b in domain["reaction_blocks"] if b["reaction_block_id"] != block_id
            ] + [block_record]
            blocks[block_id] = block_record
            new_block_ids[added_block.block_key] = block_id
            locate(added_block, "REACTION_BLOCK", block_id)
        except ValueError as error:
            failed(
                "new_reaction_blocks",
                index,
                error,
                "PAPER",
                receipt.paper_id,
                added_block.selection_ids,
            )
    for index, added_op in enumerate(receipt.answer.new_operations):
        try:
            block_id = added_op.reaction_block_id
            if block_id in new_block_ids:
                raise ValueError(
                    f"{block_id} is a block written in this same answer: write its "
                    "operations inside that block, not under new_operations"
                )
            if block_id not in blocks:
                raise ValueError(f"{block_id} is no public reaction block")
            block = blocks[block_id]
            operation_id = "OP-ENR-" + content_hash([receipt.paper_id, added_op.operation_key])[:24]
            refs = _refs(receipt, added_op.selection_ids)
            position = next(
                (
                    o["sequence_index"]
                    for o in block["operations"]
                    if o["operation_id"] == operation_id
                ),
                len(block["operations"]) + 1,
            )
            operation_record = _new_operation(operation_id, position, added_op.description, refs)
            block["operations"] = [
                o for o in block["operations"] if o["operation_id"] != operation_id
            ] + [operation_record]
            block["operations"].sort(key=lambda o: o["sequence_index"])
            new_operation_ids[added_op.operation_key] = operation_id
            locate(added_op, "OPERATION", operation_id)
        except ValueError as error:
            failed(
                "new_operations", index, error, "PAPER", receipt.paper_id, added_op.selection_ids
            )
    public_operations: dict[str, dict[str, Any]] = {
        o["operation_id"]: o
        for block in experiment_containers(result)
        for o in block["operations"]
    }

    def bound_operation(token: str, what: str) -> dict[str, Any]:
        operation = public_operations.get(new_operation_ids.get(token, token))
        if operation is None:
            raise ValueError(
                f"{what} must bind an existing public operation or the "
                "operation_key of an operation written in this same answer"
            )
        return operation

    def bound_stage(
        operation: Mapping[str, Any], stage_key: str | None, existing_stage_id: str | None
    ) -> dict[str, Any] | None:
        resolved_stage: dict[str, Any] | None = None
        if existing_stage_id is not None:
            published = next(
                (s for s in operation.get("stages", []) if s["stage_id"] == existing_stage_id),
                None,
            )
            if published is None or stage_key is not None:
                raise ValueError(
                    "existing_stage_id must name this operation's stage, without stage_key"
                )
            resolved_stage = {"anchor_hash": boundary_hash_of(published), "sites": []}
        if stage_key is not None:
            resolved_stage = lookup_stage(
                ledgers, operation["operation_id"], receipt.execution_id, stage_key
            )
            if resolved_stage is None:
                raise ValueError(
                    f"stage_key {stage_key!r} names no stage declared for this "
                    "operation; declare it with new_stages first"
                )
        return resolved_stage

    available = {o["observation_id"]: o for o in (*observations, *receipt.observations)}
    # The stretches of a procedure, declared before any condition hangs on them (E157):
    # the author's words where each begins, checked against the cited selection the way
    # a card's method statement is (E120), and entered on the publication's ledger under
    # the boundary they name -- so two tasks' names for one stretch are one stage, and
    # an unverifiable boundary is kept as such rather than refused.
    for index, stage in enumerate(receipt.answer.new_stages):
        try:
            operation = bound_operation(stage.operation_id, "a new stage")
            refs = _refs(receipt, stage.selection_ids)
            for sid in dict.fromkeys(stage.selection_ids):
                observation_id = receipt.selections[sid]["evidence_ref"]["observation_id"]  # type: ignore[index]
                if str(observation_id) not in available:
                    raise ValueError("new stage cites a selection whose observation is unavailable")
            register_stage(
                ledgers,
                operation["operation_id"],
                receipt=receipt.execution_id,
                stage_key=stage.stage_key,
                stage_verbatim=stage.stage_verbatim,
                boundary=stage_boundary(
                    stage.stage_verbatim,
                    stage.selection_ids,
                    receipt.selections,
                    available,
                    furniture=ledgers.furniture,
                ),
                evidence_refs=refs,
                model_interpretations=(
                    [present(model_interpretation(stage.explanation, refs), refs)]
                    if profile.unified_expression
                    else [model_interpretation(stage.explanation, refs)]
                    if profile.expression else ()
                ),
            )
            locate(stage, "OPERATION", operation["operation_id"], "stages")
        except ValueError as error:
            scope, subject_id = subject(block_of.get(stage.operation_id))
            failed("new_stages", index, error, scope, subject_id, stage.selection_ids)
    outcome_snapshot = None
    for index, patch in enumerate(receipt.answer.patches):
        group = getattr(patch, "outcome_group", None)
        if group and outcome_snapshot is None:
            entry = containers.get(patch.target_id)
            outcome_snapshot = {
                "start": index, "failures": len(failures), "touched": set(touched),
                "record": copy.deepcopy(entry[1]) if entry else None,
                "conflicts": copy.deepcopy(result["domain_payload"]["field_conflicts"]),
                "blocking": copy.deepcopy(result["blocking_conflicts"]),
                "markers": len(ledgers.accepted_review_markers),
            }
        try:
            experimental_reading = isinstance(patch, CompiledReadingPatch)
            key = (patch.target_id, patch.field_key)
            if key in touched and not (group and patch.action == "SAME"):
                raise ValueError("submit one integrated increment per container field")
            touched.add(key)
            if patch.target_id not in containers:
                raise ValueError("experimental increment has no existing public container")
            kind, container = containers[patch.target_id]
            allowed = CONTAINER_ACTIONS[kind]
            if allowed is not None and patch.action not in allowed:
                raise ValueError(
                    "a reaction block's endpoints are the route's topology: only CONFLICT "
                    "may be recorded on input_compound_ids or output_compound_ids; "
                    "correcting them is the route's work, not an increment"
                )
            schema_name = _field_schema(kind, patch.field_key, profile=profile)
            old = container[patch.field_key]
            old_sha256 = content_hash(old)
            if isinstance(patch, CompiledReadingPatch) and patch.competing_values:
                if kind != "Operation" or patch.field_key not in {
                    field
                    for category, field in FIXED_CONDITION_FIELDS.items()
                    if category != "TEMPERATURE" or profile.expression
                }:
                    raise ValueError(
                        "competing readings require one whole-operation fixed condition"
                    )
                baseline_field = baseline_containers[patch.target_id][1][patch.field_key]
                candidates: dict[str, dict[str, Any]] = {}
                distinct = set()
                for proposed in patch.competing_values:
                    ids = tuple(proposed["selection_ids"])
                    if not set(ids) <= set(patch.selection_ids):
                        raise ValueError("competing value evidence is outside this reading")
                    uses: list[dict[str, Any]] = (
                        copy.deepcopy(baseline_field["evidence_uses"])
                        if proposed["baseline"]
                        else [
                            {"evidence_ref": ref, "role": "CONFLICT"} for ref in _refs(receipt, ids)
                        ]
                    )
                    if proposed["baseline"] and (
                        baseline_field["value"] is None
                        or proposed["value"] != baseline_field["value"]
                    ):
                        raise ValueError("a retained primary must be the actual baseline value")
                    if not uses:
                        raise ValueError("each competing value needs its own evidence")
                    value = proposed["value"]
                    validate_fact_definition(
                        schema_name,
                        present(value, [u["evidence_ref"] for u in uses]),
                        profile=profile,
                    )
                    distinct.add(content_hash(value))
                    identity = content_hash([value, uses])
                    candidates[identity] = {
                        "candidate_id": "READING-" + identity[:24],
                        "value": value,
                        "evidence_uses": uses,
                    }
                if len(distinct) < 2:
                    raise ValueError("competing readings require at least two distinct values")
                candidate_values = [candidates[k] for k in sorted(candidates)]
                conflict = {
                    "conflict_id": "READING-MULTI-" + content_hash([key, candidate_values])[:24],
                    "target_object_type": "OPERATION",
                    "target_object_id": patch.target_id,
                    "field_key": patch.field_key,
                    "blocking": False,
                    "candidate_values": candidate_values,
                }
                validate_fact_definition("FieldConflict", conflict, profile=profile)
                source_notes = _updated_condition_sources(container, patch, profile)
                if conflict not in result["domain_payload"]["field_conflicts"]:
                    result["domain_payload"]["field_conflicts"].append(conflict)
                if patch.condition_sources:
                    container["other_conditions"] = source_notes
                # The baseline field (including pending/candidates) stays byte-for-byte.
                # This is a local review item, never an entry in blocking_conflicts.
                mark_patch(patch, kind, patch.field_key)
                if patch.condition_sources:
                    locate(patch, "OPERATION", patch.target_id, "other_conditions")
                locate(patch, "FIELD_CONFLICT", conflict["conflict_id"])
                continue
            references = _refs(receipt, patch.selection_ids)
            primary_ids = patch.primary_selection_ids or patch.selection_ids
            if not set(primary_ids) <= set(patch.selection_ids):
                raise ValueError(
                    "primary value evidence must belong to the patch's inspected selections"
                )
            primary_phases = {receipt.selections[s]["source_phase"] for s in primary_ids}
            # Main text and supplement cited together, no primary named: the host takes
            # the main text's side (MAIN has primary-value priority), and keeps that only
            # if the value is the one already recorded -- then which side is primary has
            # one answer, and asking the model to name it was asking it to enumerate what
            # the host can derive (12 of the 40 such refusals on the sealed rounds were
            # SAME on the route's own yield, word for word). A value that differs still
            # needs the model to say which reading it is.
            derived_primary = (
                len(primary_phases) != 1
                and not patch.primary_selection_ids
                and not experimental_reading
            )
            if derived_primary:
                primary_ids = tuple(
                    s for s in primary_ids if receipt.selections[s]["source_phase"] == "MAIN"
                )
                primary_phases = {"MAIN"}
            if len(primary_phases) != 1 and not experimental_reading:
                raise ValueError(
                    "mixed MAIN/SI evidence requires explicit primary_selection_ids from one phase"
                )
            primary_refs = _refs(receipt, primary_ids)
            # What the task wrote, in the shape the host records: the author's words
            # parsed into the public scalar, the evidence bound into an intermediate's
            # structure. A value already in the public shape passes through unchanged.
            value = project_written_value(
                schema_name, patch.value, primary_refs, field_key=patch.field_key
            )
            if (
                experimental_reading
                and kind == "ReportedOutcome"
                and patch.field_key == "product_compound_ids"
            ):
                owner = containers[block_of[patch.target_id]][1]
                permitted = (
                    owner["output_compound_ids"]["value"] or []
                    if patch.field_key == "product_compound_ids"
                    else [op["operation_id"] for op in owner["operations"]]
                )
                if not isinstance(value, list) or not value or not set(value) <= set(permitted):
                    raise ValueError("measurement attribution must stay within its route block")
            candidate = present(value, primary_refs)
            candidate["evidence_uses"].extend(
                {"evidence_ref": ref, "role": "REFINE"}
                for ref in references
                if ref not in primary_refs
            )
            if isinstance(patch, CompiledReadingPatch) and patch.support_selection_ids is not None:
                support_refs = _refs(receipt, patch.support_selection_ids)
                candidate["evidence_uses"] = [
                    use for use in candidate["evidence_uses"] if use["evidence_ref"] in support_refs
                ]
            validate_fact_definition(schema_name, candidate, profile=profile)
            if value is None:
                raise ValueError("an experimental increment requires an evidenced value")
            if derived_primary and (old["state"] != "PRESENT" or old["value"] != value):
                raise ValueError(
                    "mixed MAIN/SI evidence requires explicit primary_selection_ids from one phase"
                )
            incoming_main = primary_phases == {"MAIN"}
            existing_main = any(
                observation_phases.get(e["evidence_ref"]["observation_id"]) == "MAIN"
                and e["role"] == "SUPPORT"
                for e in old["evidence_uses"]
            )
            if patch.action == "CORRECT_MAIN" and not incoming_main and not experimental_reading:
                raise ValueError("correcting a MAIN interpretation requires MAIN evidence")
            if patch.action == "SAME" and (old["state"] != "PRESENT" or old["value"] != value):
                raise ValueError("SAME must preserve the existing field value exactly")
            if patch.action == "ADD" and old["reason_code"] == "CONFLICTING_CANDIDATES":
                raise ValueError("ADD cannot settle CONFLICTING_CANDIDATES as an empty field")
            if patch.action == "ADD" and old["state"] == "PRESENT" and old["value"] != value:
                raise ValueError("ADD cannot replace an existing container value")
            if patch.action == "CONFLICT" and old["value"] is None:
                raise ValueError("CONFLICT requires an existing value for comparison")
            if patch.action == "CONFLICT":
                role = "CONFLICT"
                alternative: dict[str, Any] = {
                    "candidate_id": "EC-" + content_hash([key, value, references])[:24],
                    "value": value,
                    "evidence_uses": [{"evidence_ref": ref, "role": role} for ref in references],
                }
                updated = copy.deepcopy(old)
                if alternative not in updated["candidates"]:
                    updated["candidates"].append(alternative)
                if patch.action == "CONFLICT":
                    conflict = {
                        "conflict_id": "ECONF-" + content_hash([key, old["value"], value])[:24],
                        "target_object_type": CONFLICT_TARGET_TYPES[kind],
                        "target_object_id": patch.target_id,
                        "field_key": patch.field_key,
                        "blocking": True,
                        "candidate_values": [
                            {
                                "candidate_id": "PRIMARY-" + old_sha256[:24],
                                "value": old["value"],
                                "evidence_uses": old["evidence_uses"],
                            },
                            alternative,
                        ],
                    }
                    validate_fact_definition("FieldConflict", conflict, profile=profile)
                    if conflict not in result["domain_payload"]["field_conflicts"]:
                        result["domain_payload"]["field_conflicts"].append(conflict)
                        result["blocking_conflicts"].append(conflict)
                    locate(patch, "FIELD_CONFLICT", conflict["conflict_id"])
                if incoming_main and not existing_main:
                    # The model identified a conflict with a previously SI-only value.
                    # Preserve the new MAIN reading as primary, with both alternatives.
                    updated = candidate
                    updated["candidates"] = [
                        *old["candidates"],
                        {
                            "candidate_id": "PREVIOUS-" + old_sha256[:24],
                            "value": old["value"],
                            "evidence_uses": old["evidence_uses"],
                        },
                    ]
            elif old["state"] == "PRESENT" and old["value"] == value:
                updated = copy.deepcopy(old)
                updated["evidence_uses"] = list(
                    {
                        content_hash(e): e
                        for e in (*old["evidence_uses"], *candidate["evidence_uses"])
                    }.values()
                )
            else:
                updated = candidate
                if patch.action == "REFINE" and not (
                    profile.expression
                    and isinstance(patch, CompiledReadingPatch)
                    and bool(patch.condition_sources)
                ):
                    updated["evidence_uses"] = list(
                        {
                            content_hash(e): e
                            for e in (*old["evidence_uses"], *candidate["evidence_uses"])
                        }.values()
                    )
                if old["value"] is not None:
                    updated["candidates"] = [
                        *old["candidates"],
                        {
                            "candidate_id": "PREVIOUS-" + old_sha256[:24],
                            "value": old["value"],
                            "evidence_uses": old["evidence_uses"],
                        },
                    ]
            if isinstance(patch, CompiledReadingPatch) and patch.alternative_value is not None:
                alternative_value = project_written_value(
                    schema_name, patch.alternative_value, primary_refs, field_key=patch.field_key
                )
                if alternative_value != updated["value"]:
                    alternative = {
                        "candidate_id": "READING-"
                        + content_hash([key, alternative_value, references])[:24],
                        "value": alternative_value,
                        "evidence_uses": [
                            {"evidence_ref": ref, "role": "CONFLICT"}
                            for ref in _refs(
                                receipt, patch.alternative_selection_ids or patch.selection_ids
                            )
                        ],
                    }
                    if alternative not in updated["candidates"]:
                        updated["candidates"].append(alternative)
            validate_fact_definition(schema_name, updated, profile=profile)
            alias_updates = []
            if isinstance(patch, CompiledReadingPatch) and patch.compound_aliases:
                if kind != "Operation" or patch.field_key != "materials":
                    raise ValueError("compound aliases require an applied material identity link")
                bound = {row.get("compound_id") for row in updated["value"]}
                for compound_id, names in patch.compound_aliases.items():
                    if compound_id not in bound or compound_id not in public_compounds:
                        raise ValueError("compound alias has no existing material identity link")
                    compound = next(
                        c
                        for c in result["domain_payload"]["compounds"]
                        if c["compound_id"] == compound_id
                    )
                    changed = copy.deepcopy(compound)
                    changed["names"] = list(dict.fromkeys([*compound["names"], *names]))
                    changed["evidence_refs"] = list(
                        {
                            content_hash(ref): ref
                            for ref in (*compound["evidence_refs"], *references)
                        }.values()
                    )
                    validate_fact_definition("CompoundRecord", changed, profile=profile)
                    alias_updates.append((compound, changed))
            source_notes = _updated_condition_sources(
                container, patch, profile, replace_primary=old.get("value") != updated.get("value")
            )
            category = _condition_kind_of(kind, patch.field_key)
            if category is not None and patch.action != "CONFLICT" and not profile.expression:
                incoming = _field_as_condition(category, updated)
                placed_at = place_whole_operation_value(
                    container,
                    category,
                    incoming,
                    baseline_present=baseline_present(patch.target_id, patch.field_key),
                )
                if placed_at == "MERGED":
                    locate(patch, "OPERATION", patch.target_id, "other_conditions")
                    mark_patch(patch, kind, "other_conditions")
                    continue
                if placed_at == "UNASSIGNED":
                    register_unassigned(
                        ledgers,
                        container["operation_id"],
                        incoming,
                        receipt=receipt.execution_id,
                        source="patches",
                    )
                    mark_patch(patch, kind, "unassigned_facts")
                    locate(patch, "OPERATION", patch.target_id, "unassigned_facts")
                    continue
                _absorbed_uses(updated, incoming)
            if kind == "Operation" and patch.field_key == "materials":
                source_updates = copy.deepcopy(getattr(patch, "material_source_updates", ()))
                resolved_material_stages: set[str] = set()
                for relation in getattr(patch, "material_relation_updates", ()):
                    position = relation["index"]
                    material = updated["value"][position]
                    resolved = bound_stage(
                        container, relation["segment_key"], relation["existing_stage_id"]
                    )
                    material.pop("stage_id", None)
                    material["extent"] = (
                        "WHOLE_OWNERS" if relation["scope"] == "WHOLE" else "WITHIN_OWNER"
                    )
                    if resolved is not None:
                        material["stage_id"] = stage_id_for(
                            str(container.get("stable_key") or container["operation_id"]),
                            resolved["anchor_hash"],
                        )
                    if material.get("stage_id") is not None:
                        resolved_material_stages.add(material["stage_id"])
                    matching = [u for u in source_updates if u["index"] == position]
                    if matching:
                        update = matching[-1]
                        update["after"] = copy.deepcopy(material)
                        for material_field in ("extent", "stage_id"):
                            if material_field in material:
                                update["fields"][material_field] = copy.deepcopy(
                                    relation["evidence_refs"]
                                )
                # Resolve the entire value before committing any row or its source index.
                validate_fact_definition(schema_name, updated, profile=profile)
                if profile.expression:
                    stage_ids = {s["stage_id"] for s in container.get("stages", [])}
                    stage_ids.update(resolved_material_stages)
                    for value in (
                        updated.get("value"),
                        *(c.get("value") for c in updated.get("candidates", [])),
                    ):
                        if isinstance(value, list):
                            validate_material_relations(value, stage_ids)
                # Coarse historical table writes have no row correspondence. Never
                # borrow table evidence or an old identity merely because words match.
                ledgers.material_sources[patch.target_id] = (
                    apply_material_sources(
                        ledgers.material_sources.get(patch.target_id, []),
                        old.get("value") or [],
                        updated.get("value") or [],
                        source_updates,
                    )
                    if isinstance(patch, CompiledReadingPatch) and patch.material_source_updates
                    else []
                )
            container[patch.field_key] = updated
            locate(patch, CONFLICT_TARGET_TYPES[kind], patch.target_id, patch.field_key)
            if getattr(patch, "condition_sources", ()):
                container["other_conditions"] = source_notes
                locate(patch, "OPERATION", patch.target_id, "other_conditions")
            for compound, changed in alias_updates:
                compound.update(changed)
                locate(patch, "COMPOUND", compound["compound_id"], "names")
            mark_patch(patch, kind, patch.field_key)
        except ValueError as error:
            scope, subject_id = subject(block_of.get(patch.target_id))
            failed("patches", index, error, scope, subject_id, patch.selection_ids)
        finally:
            next_group = (
                getattr(receipt.answer.patches[index + 1], "outcome_group", None)
                if index + 1 < len(receipt.answer.patches)
                else None
            )
            if group and group != next_group:
                assert outcome_snapshot is not None
                entry = containers.get(patch.target_id)
                if len(failures) == outcome_snapshot["failures"]:
                    try:
                        if (
                            not profile.preservation
                            or entry is None
                            or entry[0] != "ReportedOutcome"
                        ):
                            raise ValueError("complete outcome source requires a v1.4 outcome")
                        source = patch.outcome_source
                        if (
                            source is None
                            or not set(source.get("product_ids", [])) <= public_compounds
                        ):
                            raise ValueError("outcome source requires existing product identities")
                        updated_outcome = copy.deepcopy(entry[1])
                        updated_outcome["source_statements"] = merge_interpretations(
                            updated_outcome.get("source_statements", []), [source])
                        validate_fact_definition(
                            "ReportedOutcome", updated_outcome, profile=profile
                        )
                        entry[1].update(updated_outcome)
                        for sibling in range(outcome_snapshot["start"], index + 1):
                            locate(
                                receipt.answer.patches[sibling],
                                CONFLICT_TARGET_TYPES["ReportedOutcome"],
                                patch.target_id, "source_statements",
                            )
                    except ValueError as error:
                        scope, subject_id = subject(block_of.get(patch.target_id))
                        failed("patches", index, error, scope, subject_id, patch.selection_ids)
                if len(failures) != outcome_snapshot["failures"]:
                    if entry is not None and outcome_snapshot["record"] is not None:
                        entry[1].clear()
                        entry[1].update(outcome_snapshot["record"])
                    touched = outcome_snapshot["touched"]
                    result["domain_payload"]["field_conflicts"] = outcome_snapshot["conflicts"]
                    result["blocking_conflicts"] = outcome_snapshot["blocking"]
                    del ledgers.accepted_review_markers[outcome_snapshot["markers"]:]
                    failed_indices = {f["index"] for f in failures[outcome_snapshot["failures"]:]}
                    reason = failures[outcome_snapshot["failures"]]["reason"]
                    for sibling in range(outcome_snapshot["start"], index + 1):
                        locations.pop(("patches", sibling), None)
                        if sibling not in failed_indices:
                            scope, subject_id = subject(block_of.get(patch.target_id))
                            failed(
                                "patches",
                                sibling,
                                ValueError("same finding was not applied: " + reason),
                                scope,
                                subject_id,
                                patch.selection_ids,
                            )
                outcome_snapshot = None
    # Whose procedure an operation follows, by identity (E117 principle one, R4): an
    # operation, a general-methods unit's first block, or a source outside this
    # document. Recorded, never derived from.
    for index, elision in enumerate(receipt.answer.procedure_elisions):
        try:
            operation = bound_operation(elision.operation_id, "a procedure elision")
            target: dict[str, Any] = {
                "referenced_operation_id": None,
                "referenced_unit_block_id": elision.referenced_unit_block_id,
                "external_citation": elision.external_citation,
            }
            if elision.referenced_operation_id is not None:
                followed = public_operations.get(
                    new_operation_ids.get(
                        elision.referenced_operation_id, elision.referenced_operation_id
                    )
                )
                if followed is None:
                    raise ValueError(
                        "referenced_operation_id names no existing public operation and "
                        "no operation_key of this answer"
                    )
                target["referenced_operation_id"] = followed["operation_id"]
            elif elision.referenced_unit_block_id is not None:
                revisions = {
                    str(receipt.selections[sid]["source_identity"]["document_revision_id"])  # type: ignore[index]
                    for sid in elision.selection_ids
                }
                matching = {
                    (revision, elision.referenced_unit_block_id) for revision in revisions
                    if (revision, elision.referenced_unit_block_id)
                    in ledgers.general_methods_first_blocks
                }
                target_revision = getattr(elision, "target_source_revision_id", None)
                target_document = getattr(elision, "target_source_document_id", None)
                if profile.unified_expression:
                    if not target_document or (
                        target_revision, elision.referenced_unit_block_id
                    ) not in ledgers.general_methods_first_blocks:
                        raise ValueError(
                            "general-methods source needs its exact verified document/revision/unit binding"
                        )
                    target.update(target_source_document_id=target_document,
                                  target_source_revision_id=target_revision)
                elif len(matching) > 1:
                    raise ValueError("general-methods unit is ambiguous across cited documents")
                if not matching and not profile.unified_expression:
                    raise ValueError(
                        "referenced_unit_block_id names no GENERAL_METHODS unit's first "
                        "block in the documents the elision cites"
                    )
            refs = _refs(receipt, elision.selection_ids)
            if profile.expression:
                if target["referenced_operation_id"] == operation["operation_id"]:
                    raise ValueError("a procedure source cannot refer to its own operation")
                row = procedure_source_value({**target, "evidence_refs": refs})
                row["extent"] = getattr(elision, "extent", "WHOLE_OWNERS")
                row["model_interpretations"] = [model_interpretation(elision.explanation, refs)]
                if ruling_text := getattr(elision, "ruling_explanation", None):
                    row["model_interpretations"] = merge_interpretations(
                        row["model_interpretations"], [model_interpretation(ruling_text, refs)]
                    )
                if profile.unified_expression:
                    row["model_interpretations"] = [present(value, value["evidence_refs"])
                                                    for value in row["model_interpretations"]]
                    if parts := getattr(elision, "source_parts", ()):
                        row["source_parts"] = copy.deepcopy(list(parts))
                resolved_stage = bound_stage(
                    operation,
                    getattr(elision, "stage_key", None),
                    getattr(elision, "existing_stage_id", None),
                )
                if resolved_stage is not None:
                    if row["extent"] != "WITHIN_OWNER":
                        raise ValueError("a named-stage procedure source must be WITHIN_OWNER")
                    row["stage_id"] = stage_id_for(
                        str(operation.get("stable_key") or operation["operation_id"]),
                        resolved_stage["anchor_hash"],
                    )
                rows = apply_procedure_reading(
                    operation,
                    row,
                    relation=getattr(elision, "relation", "ADDS"),
                    primary=getattr(elision, "primary", None),
                    against=baseline_containers.get(operation["operation_id"], (None, {}))[1],
                )
                if profile.unified_expression:
                    for reading in rows:
                        reading["model_interpretations"] = [
                            interpretation
                            if "state" in interpretation
                            else present(interpretation, interpretation["evidence_refs"])
                            for interpretation in reading.get("model_interpretations", [])
                        ]
                for reading in rows:
                    validate_fact_definition("ProcedureSourceReading", reading, profile=profile)
                operation["procedure_sources"] = rows
                operation.pop("procedure_source", None)
                mark_accepted(elision, "OPERATION", operation["operation_id"], "procedure_sources")
                locate(elision, "OPERATION", operation["operation_id"], "procedure_sources")
            else:
                register_elision(
                    ledgers,
                    operation["operation_id"],
                    receipt=receipt.execution_id,
                    target=target,
                    evidence_refs=refs,
                )
                mark_accepted(elision, "OPERATION", operation["operation_id"], "procedure_source")
                locate(elision, "OPERATION", operation["operation_id"], "procedure_source")
        except ValueError as error:
            scope, subject_id = subject(block_of.get(elision.operation_id))
            failed("procedure_elisions", index, error, scope, subject_id, elision.selection_ids)
    # Conditions no fixed field holds (ledger E57): the model's word and the author's
    # words, keyed by the Host's one table, appended to the operation's open slot.
    for index, condition in enumerate(receipt.answer.new_conditions):
        try:
            operation = bound_operation(condition.operation_id, "a new condition")
            condition_key = normalize_condition(
                condition.category_verbatim, condition.text or "", about=condition.about
            )
            stage_key = stage_of(condition)
            resolved_stage = bound_stage(
                operation, stage_key, getattr(condition, "existing_stage_id", None)
            )
            if (
                condition_key in FIXED_CONDITION_FIELDS
                and not (isinstance(condition, CompiledCondition) and condition.retain_as_note)
                and not (
                    isinstance(condition, CompiledCondition) and condition_key == "TEMPERATURE"
                )
            ) and (condition.extent == "WHOLE_OWNERS" and resolved_stage is None):
                # The host holds the table that says which kinds have a field of their
                # own, so the host routes them: this used to be a refusal that told the
                # task to resend the same words under `patches`, which is the host
                # asking a model to apply a rule the host is the only one holding.
                # A field that already has a value is not overwritten -- the note keeps
                # the second reading's words and evidence instead.
                field_key = FIXED_CONDITION_FIELDS[condition_key]
                field = operation.get(field_key)
                if (
                    isinstance(field, Mapping)
                    and field.get("reason_code") != "CONFLICTING_CANDIDATES"
                ):
                    refs = _refs(receipt, condition.selection_ids)
                    routed = present(
                        project_written_value(
                            _field_schema("Operation", field_key, profile=profile),
                            condition.text,
                            refs,
                            field_key=field_key,
                        ),
                        refs,
                    )
                    validate_fact_definition(
                        _field_schema("Operation", field_key, profile=profile),
                        routed,
                        profile=profile,
                    )
                    if field.get("state") != "PRESENT" or field["value"] == routed["value"]:
                        if field.get("state") == "PRESENT":
                            incoming_uses = routed["evidence_uses"]
                            routed = copy.deepcopy(dict(field))
                            routed["evidence_uses"] = list(
                                {
                                    content_hash(use): use
                                    for use in (*field["evidence_uses"], *incoming_uses)
                                }.values()
                            )
                        incoming = _field_as_condition(condition_key, routed, about=condition.about)
                        placed_at = (
                            "FIELD"
                            if profile.expression
                            else place_whole_operation_value(
                                operation,
                                condition_key,
                                incoming,
                                baseline_present=baseline_present(
                                    operation["operation_id"], field_key
                                ),
                            )
                        )
                        if placed_at == "MERGED":
                            locate(
                                condition, "OPERATION", operation["operation_id"],
                                "other_conditions",
                            )
                            mark_accepted(
                                condition,
                                "OPERATION",
                                operation["operation_id"],
                                "other_conditions",
                            )
                            continue
                        if placed_at == "UNASSIGNED":
                            register_unassigned(
                                ledgers,
                                operation["operation_id"],
                                incoming,
                                receipt=receipt.execution_id,
                                source="new_conditions",
                            )
                            mark_accepted(
                                condition,
                                "OPERATION",
                                operation["operation_id"],
                                "unassigned_facts",
                            )
                            locate(
                                condition, "OPERATION", operation["operation_id"],
                                "unassigned_facts",
                            )
                            continue
                        _absorbed_uses(routed, incoming)
                        operation[field_key] = routed
                        validate_fact_definition("Operation", operation, profile=profile)
                        mark_accepted(condition, "OPERATION", operation["operation_id"], field_key)
                        locate(condition, "OPERATION", operation["operation_id"], field_key)
                        continue
            note = {
                "category_verbatim": condition.category_verbatim,
                "text": condition.text,
                "category": condition_key,
                "about": condition.about,
                # A write that names the stretch it read this in has said something the
                # Host can act on, and `WHOLE_OWNERS` beside it is a claim about the
                # same words that cannot also be true.
                "extent": "WITHIN_OWNER" if resolved_stage is not None else condition.extent,
                "evidence_refs": _refs(receipt, condition.selection_ids),
            }
            if (
                profile.expression
                and isinstance(condition, CompiledCondition)
                and condition.source_note is not None
            ):
                note = copy.deepcopy(condition.source_note)
            if profile.expression:
                note.setdefault(
                    "model_interpretations",
                    [
                        model_interpretation(
                            condition.explanation, _refs(receipt, condition.selection_ids)
                        )
                    ],
                )
            if resolved_stage is not None and profile.expression:
                note["stage_id"] = stage_id_for(
                    str(operation.get("stable_key") or operation["operation_id"]),
                    resolved_stage["anchor_hash"],
                )
            if profile.unified_expression:
                note = resolved_condition_note(note)
            validate_fact_definition("ConditionNote", note, profile=profile)
            if resolved_stage is not None and not profile.expression:
                # The Host's working key holds the stage's content-derived identity,
                # never the name the task wrote: two tasks' names for one stretch met
                # on the ledger, and their conditions meet here.
                note[STAGE_KEY] = resolved_stage["anchor_hash"]
            condition_reading = None
            if isinstance(condition, CompiledCondition) and not profile.expression:
                value_ids = condition.value_selection_ids or condition.selection_ids
                value_boundary = stage_boundary(
                    condition.text,
                    value_ids,
                    receipt.selections,
                    available,
                    furniture=ledgers.furniture,
                    start_at=min(resolved_stage["sites"])
                    if resolved_stage and resolved_stage["sites"]
                    else None,
                )
                condition_reading = {
                    "operation_id": operation["operation_id"],
                    "receipt": receipt.execution_id,
                    "condition_id": condition.condition_id,
                    "category": condition_key,
                    "text": condition.text,
                    "extent": condition.extent,
                    "stage_anchor": resolved_stage["anchor_hash"] if resolved_stage else None,
                    "site": value_boundary.site,
                    "relation": condition.relation,
                    "primary": condition.primary,
                    "value_relation": condition.value_relation,
                    "review_reason": condition.review_reason,
                    "explanation": condition.explanation,
                    "evidence_refs": _refs(receipt, value_ids),
                }
            # This condition, already on this operation. Two tasks reading one procedure
            # -- a general method serving several steps, a telescoped sequence written as
            # one paragraph -- both see it, and refusing the second was a rejection that
            # blocked its task's COMPLETE and, through the partial receipt, lowered the
            # whole paper's completeness. Recording it twice is a second fact for one
            # condition: the e111 round published `2 dram vial` beside `in a 2 dram vial`
            # and `1:1` beside `1:1 hexanes/EtOAc` on one operation. What counts as this
            # condition again, and which wording then survives, is `same_condition` and
            # `merge_condition` -- one place, so the projection can adopt the same rule
            # the day its own lane produces a second reading (ledger E114).
            if (profile.unified_expression and isinstance(condition, CompiledCondition)
                    and condition.update_current and condition.existing_stage_id):
                # The selected slot is stage + category, not a guessed matching sentence.
                # Previous source reports remain readable but are no longer primary.
                for prior_note in operation["other_conditions"]:
                    if (prior_note.get("stage_id") == note.get("stage_id")
                            and prior_note.get("category") == condition_key
                            and prior_note.get("adoption") == "ADOPTED"):
                        prior_note["adoption"] = "ALTERNATIVE"
            recorded = (
                next((c for c in operation["other_conditions"] if c == note), None)
                if profile.expression
                else None
                if isinstance(condition, CompiledCondition) and condition.retain_as_note
                else next(
                    (c for c in operation["other_conditions"] if same_condition(c, note)), None
                )
            )
            if recorded is not None:
                merged_note = note if profile.expression else merge_condition(recorded, note)
                # The merged note may carry the working key; the published form is
                # what the definition is of.
                validate_fact_definition("ConditionNote", public_note(merged_note), profile=profile)
                recorded.update(merged_note)
            else:
                operation["other_conditions"].append(note)
            if condition_reading is not None:
                ledgers.condition_readings.append(condition_reading)
            if profile.expression:
                mark_accepted(condition, "OPERATION", operation["operation_id"], "other_conditions")
            locate(condition, "OPERATION", operation["operation_id"], "other_conditions")
        except ValueError as error:
            scope, subject_id = subject(block_of.get(condition.operation_id))
            failed("new_conditions", index, error, scope, subject_id, condition.selection_ids)
    # Characterization cards (ledger E47): the author's one-line statement on existing
    # public compounds, keyed and parsed by the Host, never transcribed. A card that
    # progress updates resubmit keeps its id and replaces its earlier form.
    compounds = {c["compound_id"] for c in result["domain_payload"]["compounds"]}
    cards = {
        c["characterization_id"]: c for c in result["domain_payload"]["characterization_records"]
    }
    baseline_cards = {
        c["characterization_id"]: c for c in baseline["domain_payload"]["characterization_records"]
    }
    for position, card in enumerate(receipt.answer.characterizations):
        try:
            if not set(card.compound_ids) <= compounds:
                raise ValueError("characterization card has no existing public compound")
            words = " ".join(card.method_verbatim.split())
            validate_characterization_statement(words, profile=profile)
            fields = characterization_fields(words, profile=profile)
            if card.same_measurement_as is not None and len(fields) != 1:
                raise ValueError("Link one measured result to one existing characterization card")
            for index, read in enumerate(fields, 1):
                record: dict[str, Any] = {
                    "characterization_id": "CHAR-ENR-"
                    + content_hash(
                        [receipt.paper_id, receipt.execution_id, card.characterization_id]
                    )[:24]
                    + (f"-{index}" if len(fields) > 1 else ""),
                    "compound_ids": list(dict.fromkeys(card.compound_ids)),
                    **read,
                    "sample_context": card.sample_context,
                    "evidence_refs": _refs(receipt, card.selection_ids),
                }
                if profile.expression:
                    record["source_statements"] = [
                        characterization_source_statement(
                            card.method_verbatim,
                            card.sample_context,
                            _refs(receipt, card.selection_ids),
                        )
                    ]
                    if getattr(card, "model_interpretations", ()):
                        record["model_interpretations"] = list(
                            getattr(card, "model_interpretations", ())
                        )
                if getattr(card, "explicit_identity", False):
                    hit = matching_card(cards, record, card.same_measurement_as, explicit_identity=True)
                else:
                    hit = matching_card(baseline_cards, record, card.same_measurement_as)
                    if hit is not None:
                        hit = matching_card(cards, record, card.same_measurement_as)
                if hit is not None:
                    record = merge_card(hit, record, update_current=getattr(card, "update_current", False),
                                        replace_sample_context=getattr(card, "replace_sample_context", False))
                validate_fact_definition("CharacterizationRecord", record, profile=profile)
                cards[record["characterization_id"]] = record
                placed.setdefault(card.characterization_id, []).append(
                    record["characterization_id"]
                )
                mark_accepted(
                    card, "CHARACTERIZATION", record["characterization_id"], "method_verbatim"
                )
                if hit is not None and profile.expression:
                    locate(
                        card, "CHARACTERIZATION", record["characterization_id"], "source_statements"
                    )
                    if getattr(card, "model_interpretations", ()):
                        locate(
                            card, "CHARACTERIZATION", record["characterization_id"],
                            "model_interpretations",
                        )
                else:
                    locate(card, "CHARACTERIZATION", record["characterization_id"])
        except ValueError as error:
            scope, subject_id = subject(
                compound_id=next((c for c in card.compound_ids if c in public_compounds), None)
            )
            failed("characterizations", position, error, scope, subject_id, card.selection_ids)
    result["domain_payload"]["characterization_records"] = list(cards.values())
    # Two passes the receipt's own order must not decide. A filled field of a kind a
    # stretch of the procedure owns is withdrawn, whichever of the two the model wrote
    # first: `stage_covers` is the same predicate the write paths route by, so a value
    # that arrived before the stretch is withdrawn here and one that arrives after is
    # kept unassigned there, and both end in the same place (E157; E119 held only one
    # way round, and E129's text guard missed the five takeovers of f07). Then the
    # Host's working key for the stretch comes off: what it decided is decided, and
    # the published shape is the shape it was before this slice. Both run before the
    # bundle is validated, because the bundle check is of the published shape and the
    # working key is not in it: with the check ahead of them, every write that named a
    # stretch was refused for carrying the key (f05, ledger E141).
    for block in experiment_containers(result):
        for operation in block["operations"]:
            for category, field_key in FIXED_CONDITION_FIELDS.items():
                field = operation.get(field_key)
                if (
                    isinstance(field, Mapping)
                    and field.get("state") == "PRESENT"
                    and not profile.expression
                    and stage_covers(operation, category)
                    and not baseline_present(operation["operation_id"], field_key)
                ):
                    register_unassigned(
                        ledgers,
                        operation["operation_id"],
                        _field_as_condition(category, field),
                        receipt=receipt.execution_id,
                        source=field_key,
                    )
                    # The withdrawn field is the pending field it was before anything
                    # filled it: the v1 shape has no reason code for "a stretch owns
                    # this" (`fact_schema.json` ResolvedValue.reason_code), so the
                    # displaced value and why it was displaced are the ledger's to
                    # say, and the field's bytes are the same whichever write came
                    # first.
                    operation[field_key] = pending()
                    for marker in [
                        *result["domain_payload"].get("review_markers", []),
                        *ledgers.accepted_review_markers,
                    ]:
                        if (
                            marker["target_object_id"] == operation["operation_id"]
                            and marker["field_key"] == field_key
                        ):
                            marker["field_key"] = "unassigned_facts"
                    for targets in locations.values():
                        for target in targets:
                            if (
                                target["object_id"] == operation["operation_id"]
                                and target.get("field_key") == field_key
                            ):
                                target["field_key"] = "unassigned_facts"
            if not profile.expression:
                remember_note_stages(
                    ledgers,
                    operation["operation_id"],
                    operation["other_conditions"],
                    stage_key=STAGE_KEY,
                    public_form=public_note,
                    content_hash=content_hash,
                )
            operation["other_conditions"] = [
                public_note(note) for note in operation["other_conditions"]
            ]
            validate_fact_definition("Operation", operation, profile=profile)
    if profile.expression:
        result = compile_operation_stages(result, ledgers, {}, rekey_relations=True)
    evidence = required_fact_observation_refs(result)
    refs = [
        {"observation_id": oid, "source_observation_sha256": digest}
        for oid, digest in sorted(evidence)
    ]
    result["source_observation_refs"] = refs
    result["domain_payload"]["source_observation_refs"] = refs
    validate_fact_bundle(
        result,
        tuple(o for o in available.values() if (o["observation_id"], content_hash(o)) in evidence),
    )
    if applied_locations is not None:
        # A later failure may roll back a group. Never publish a success locator
        # for a refused item, or expose this output before whole-bundle validation.
        refused = {(f["collection"], f["index"]) for f in failures}
        applied_locations.update(
            {key: value for key, value in locations.items() if key not in refused}
        )
    return result, failures, placed


def withheld_narratives(
    receipt: ReceiptEvidence, machine: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The receipt's narratives whose kind the public result has no place for.

    A biological finding the supplement channel recorded stays in the channel's own
    receipt (ledger E47); the publication reports each such narrative as unresolved so
    the result never reads as if the paper had none (E52).
    """
    if machine is not None and machine_profile(machine).unified_expression:
        return []
    public_kinds = set(fact_enum("NarrativeKindValue"))
    return [
        {
            "kind": narrative.kind,
            "title": narrative.title,
            "evidence_refs": _refs(receipt, narrative.selection_ids),
        }
        for narrative in _receipt_narratives(receipt)
        if narrative.kind not in public_kinds
    ]


def _receipt_narratives(receipt: ReceiptEvidence) -> tuple[Any, ...]:
    """The same declared collection before and after the reading's typed compile."""
    answer = receipt.answer if receipt.answer is not None else receipt.raw_model()
    narratives = getattr(answer, "narratives", ())
    if hasattr(answer, "findings") and getattr(answer, "new_experiments", None) is not None:
        from tsdaf.lanes.literature.middle_layer import other_experiment_narratives
        narratives = (*narratives, *other_experiment_narratives(answer, receipt))
    return narratives


def _public_narrative(
    narrative: Any, receipt: ReceiptEvidence, machine: Mapping[str, Any] | None,
    creation_keys: Mapping[str, str] | None,
) -> dict[str, Any]:
    """One strict source/subject projection shared by admission and materialization."""
    if not set(narrative.author_selection_ids) <= set(narrative.selection_ids):
        raise ValueError("author quotations must belong to the narrative evidence")
    quotes = []
    for sid in narrative.author_selection_ids:
        selection = receipt.selections[sid]
        if selection.get("exact_text") is None:
            raise ValueError("a pure image cannot supply author_text")
        quotes.append(str(selection["exact_text"]))
    value = {
        "narrative_id": "NARR-"
        + content_hash(
            [
                receipt.paper_id,
                narrative.title,
                narrative.kind,
                [receipt.selections[s]["source_identity"] for s in narrative.selection_ids],
            ]
        )[:24],
        "title": narrative.title,
        "kind": narrative.kind,
        "author_text": "\n\n".join(quotes) or None,
        "explanation": narrative.explanation,
        "evidence_refs": _refs(receipt, narrative.selection_ids),
    }
    if key := (creation_keys or {}).get(narrative.item_id):
        from tsdaf.contracts.literature.public_ids import public_id

        value["narrative_id"] = public_id("NARR", key)
    profile = machine_profile(machine) if machine is not None else MachineProfile.V1
    if profile.unified_expression:
        value["kind"] = narrative.kind if narrative.kind in fact_enum("NarrativeKindValue") else None
        value["kind_verbatim"] = narrative.kind
        value["explanation"] = present(narrative.explanation, value["evidence_refs"])
        if parts := getattr(narrative, "source_parts", ()):
            value["source_parts"] = copy.deepcopy(list(parts))
    if narrative.subjects:
        if not profile.preservation or machine is None:
            raise ValueError("narrative subjects require a v1.4 machine context")
        value["subjects"] = [subject.model_dump() for subject in narrative.subjects]
        validate_narrative_subjects([value], machine)
    validate_fact_definition("PaperNarrative", value, profile=profile)
    return value


def public_narratives(
    receipt: ReceiptEvidence, machine: Mapping[str, Any] | None = None,
    *, creation_keys: Mapping[str, str] | None = None,
    applications: list[dict[str, Any]] | None = None,
    failures: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The receipt's narratives that have a public kind; see `withheld_narratives`."""
    public_kinds = set(fact_enum("NarrativeKindValue"))
    result = []
    for index, narrative in enumerate(_receipt_narratives(receipt)):
        source_finding = getattr(narrative, "source_finding", None)
        identity = ({"collection": "findings", "finding_id": source_finding}
                    if source_finding is not None else {"collection": "narratives"})
        if narrative.kind not in public_kinds and not (
            machine is not None and machine_profile(machine).unified_expression
        ):
            if applications is not None:
                applications.append({
                    **identity, "item_id": narrative.item_id,
                    "state": "SAVED_ONLY", "public_targets": [],
                    "reasons": ["Narrative kind has no public definition in this profile."],
                })
            continue
        try:
            value = _public_narrative(narrative, receipt, machine, creation_keys)
        except ValueError as error:
            if failures is None:
                raise
            reason = str(error)
            failures.append({
                **identity, "index": index, "item_id": narrative.item_id,
                "reason": reason, "evidence_refs": _refs(receipt, narrative.selection_ids),
            })
            if applications is not None:
                applications.append({
                    **identity, "item_id": narrative.item_id,
                    "state": "NOT_APPLIED", "public_targets": [], "reasons": [reason],
                })
            continue
        result.append(value)
        if applications is not None:
            applications.append({
                **identity, "item_id": narrative.item_id,
                "state": "APPLIED", "public_targets": [{
                    "object_type": "PAPER_NARRATIVE", "object_id": value["narrative_id"]
                }],
            })
    return result
