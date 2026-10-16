"""One projection of the final scientific result onto the frozen public contract."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from dataclasses import field as _field
from typing import Any

from tsdaf.contracts.literature.expressions import (
    characterization_source_statement,
    material_source_reading,
    model_interpretation,
    source_statement,
)
from tsdaf.contracts.literature.expressions import (
    pending as pending,
)
from tsdaf.contracts.literature.expressions import (
    present as present,
)
from tsdaf.contracts.literature.schema import MachineProfile, fact_enum
from tsdaf.contracts.literature.objects import experiment_operations
from tsdaf.contracts.literature.validation import validate_preparation_target
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.author_labels import canonical_author_label
from tsdaf.lanes.literature.characterization_methods import (
    CHARACTERIZATION_CATEGORY,
    characterization_fields,
)
from tsdaf.lanes.literature.condition_categories import (
    FIXED_CONDITION_FIELDS,
    canonical_key,
    normalize_condition,
)
from tsdaf.lanes.literature.experiment_projection import (
    apply_measurement_contributions,
    build_experiment_operation,
    build_experimental_account,
    build_measurement,
)
from tsdaf.lanes.literature.fusion_state import validate_result
from tsdaf.lanes.literature.gate_models import FusionResult, ScientificFact
from tsdaf.lanes.literature.material_meanings import MATERIAL_ROLES
from tsdaf.lanes.literature.material_sources import rekey_material_sources
from tsdaf.lanes.literature.measurement_intent import MeasurementIntent
from tsdaf.lanes.literature.source_parts import SourceSelection, fact_source_ids, selected_source_part
from tsdaf.lanes.literature.outcome_meanings import OUTCOME_KINDS
from tsdaf.lanes.literature.reported_scalars import (
    parse_material_quantity,
    parse_reported_outcome,
    parse_reported_scalar,
    parse_step_count,
    parse_written_material_quantity,
    sourced_material_quantities,
)
from tsdaf.lanes.literature.skeleton_meanings import (
    LOCAL_INTERMEDIATE,
    NON_ROUTE_REACTION_CATEGORIES,
)
from tsdaf.lanes.literature.stable_keys import public_creation_key, public_id, rekey


class PublicationNotReady(ValueError):
    def __init__(self, route_id: str, reason: str) -> None:
        self.readiness_ref: ArtifactRef | None = None
        self.subject_id = route_id
        self.reason = reason
        super().__init__(f"{route_id}: {reason}")


@dataclass(frozen=True, slots=True)
class RouteGroupFactProjection:
    route_group_id: str
    compounds: tuple[dict[str, Any], ...]
    reaction_blocks: tuple[dict[str, Any], ...]
    route: dict[str, Any]
    observation_refs: tuple[dict[str, str], ...]
    unresolved_items: tuple[dict[str, Any], ...]
    experimental_accounts: tuple[dict[str, Any], ...] = ()
    unassigned_outcomes: tuple[dict[str, Any], ...] = ()
    field_conflicts: tuple[dict[str, Any], ...] = ()
    paper_narratives: tuple[dict[str, Any], ...] = ()
    characterization_records: tuple[dict[str, Any], ...] = ()
    # The investigator's assessments of how far the main experimental investigation
    # covers public subjects (E77 `coverage_assessments`, E83): subjects that project
    # to public compounds or blocks, in their public ids; the rest is not public.
    coverage_assessments: tuple[dict[str, Any], ...] = ()
    # Which public id each fusion fact became, in this group's own keyed ids. The
    # mapping is decided here and nowhere else -- it is `pid` plus this pass's own
    # rekey -- and a block task's context is filtered with it: without it the Host
    # cannot tell which of the fusion lane's questions are about which published step,
    # and hands every task the whole route's conclusions.
    fusion_public_ids: dict[str, str] = _field(default_factory=dict)
    # A relation has its own fact birth, but no separate public object ID. Its
    # current storage destination must never participate in object identity/rekey.
    relation_destinations: dict[str, str] = _field(default_factory=dict)
    material_sources: dict[str, list[dict[str, Any]]] = _field(default_factory=dict)
    # Derived at row selection, not inferred later from a material's spelling.
    material_fact_rows: dict[str, tuple[str, int]] = _field(default_factory=dict)


# A condition's kind is one table, `condition_categories`: the eight kinds with a public
# field are named there (`FIXED_CONDITION_FIELDS`, read below by that name -- an alias
# once let a test compare the table with itself), and everything else goes to the
# operation's open slot (E57).
# A MATERIAL's category is its public role, from the one table that says what each role
# means and follows the contract's vocabulary (a retyped copy once lacked QUENCH_WORKUP,
# and every drying agent, brine wash and quench a window recorded was left unmapped --
# ledger E48).
_MATERIAL_ROLES = frozenset(MATERIAL_ROLES)
# An OUTCOME's category is its public kind, from the one table of kinds and meanings. A
# retyped copy of four values left every dr, ee and ratio the model wrote with no kind
# (E52).
_OUTCOME_KINDS = frozenset(OUTCOME_KINDS)
_ASSERTION_SUBJECTS = frozenset(fact_enum("AssertionSubjectValue"))
_ASSERTION_OUTCOMES = frozenset(fact_enum("AssertionOutcomeValue"))
# The label an author writes after a material's name -- "acid 6", "imine (39)".
_TRAILING_LABEL = re.compile(r"(?:\(\s*(?P<paren>[0-9]+[a-z]?)\s*\)|\b(?P<bare>[0-9]+[a-z]?))\s*$")


def field_readings(result: FusionResult, fact: ScientificFact, field: str) -> list[Any]:
    """Every value a field of a fact is read as: its candidates while they conflict."""
    conflict = result.conflicts.get(fact.fact_id)
    return (
        [c.value for c in conflict.candidates[field]]
        if conflict and field in conflict.candidates
        else [getattr(fact, field)]
    )


def canonical_id(result: FusionResult, facts: Mapping[str, ScientificFact], fid: str) -> str:
    """The fact an accepted identity chain (`same_as`) ends at. Pending identity
    hypotheses remain private, even when the target is accepted."""
    known = result.fields_known
    while (
        known(facts[fid], "kind", "status", "same_as")
        and facts[fid].status in {"ACCEPTED", "CONTEXT"}
        and facts[fid].same_as
    ):
        target = facts[fid].same_as
        assert target is not None
        if not known(facts[target], "kind", "status", "same_as") or facts[target].status not in {
            "ACCEPTED",
            "CONTEXT",
        }:
            break
        fid = target
    return fid


# How a step is named to a model: what goes in, the arrow the author draws, what comes
# out. One rule, read by the paper's sheet (`container_enrichment.step_line`) and by the
# supplement's route result (`model_view.route_result_view`) -- the two places a step is
# named at all. The comma joins the compounds of one end ("9a, 9b"), which is why this
# lives here rather than beside either caller (batch 2A, 2d, E205).
STEP_ARROW = " → "


def compound_label(compound: Mapping[str, Any]) -> str | None:
    """One identity's complete author numbering, else its first supported name."""
    labels = [
        label.strip()
        for label in (compound.get("author_labels") or ())
        if isinstance(label, str) and label.strip()
    ]
    if labels:
        return "/".join(sorted(set(labels)))
    return next(
        (
            name.strip()
            for name in (compound.get("names") or ())
            if isinstance(name, str) and name.strip()
        ),
        None,
    )


def step_name(ends: Sequence[str]) -> str:
    """A step by what goes in and what comes out: "9a, 9b → 11a", "? → 11a" when the
    route does not say where it starts. An end is already the names of its compounds,
    joined by the caller the way the author lists them."""
    return STEP_ARROW.join(ends)


def is_route_step(
    result: FusionResult, fact: ScientificFact, facts: Mapping[str, ScientificFact]
) -> bool:
    """A REACTION the route may publish as a block: accepted, of a route category in
    some reading, and no alias of another. The one definition the projection's blocks,
    the fusion's finish and its progress's steps share (E187); ``facts`` is the result's
    facts by id."""
    return (
        "REACTION" in field_readings(result, fact, "kind")
        and "ACCEPTED" in field_readings(result, fact, "status")
        and any(
            category not in NON_ROUTE_REACTION_CATEGORIES
            for category in field_readings(result, fact, "category")
        )
        and canonical_id(result, facts, fact.fact_id) == fact.fact_id
    )


# A NOTE with one of these categories is a paper narrative; the three that describe a
# reaction's context also hang off the block they are owned by (ledger E47).
_NARRATIVE_KINDS = frozenset(fact_enum("NarrativeKindValue"))
_TARGET_NOTE_CATEGORIES = frozenset(
    {"AUTHOR_STEP_COUNT", "AUTHOR_OVERALL_YIELD", "ASSERTION_SUBJECT", "ASSERTION_OUTCOME"}
)
_BLOCK_CONTEXT_LISTS = {
    "MECHANISM": "mechanism_context_ids",
    "OPTIMIZATION": "optimization_context_ids",
    "FAILED_ATTEMPT": "failed_attempt_context_ids",
}


def longest_chain(blocks: Sequence[dict[str, Any]]) -> int:
    """The longest linear sequence through blocks linked product -> substrate: a
    chemist's step count for a convergent route."""

    def compounds(block: dict[str, Any], field: str) -> set[str]:
        field_value = block[field]
        return (
            set(field_value.get("value") or ()) if field_value.get("state") == "PRESENT" else set()
        )

    producers: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        for compound in compounds(block, "output_compound_ids"):
            producers.setdefault(compound, []).append(index)
    memo: dict[int, int] = {}

    def depth(index: int, seen: frozenset[int]) -> int:
        if index in memo:
            return memo[index]
        upstream = [
            producer
            for compound in compounds(blocks[index], "input_compound_ids")
            for producer in producers.get(compound, ())
            if producer not in seen
        ]
        value = 1 + max((depth(u, seen | {index}) for u in upstream), default=0)
        memo[index] = value
        return value

    return max((depth(i, frozenset()) for i in range(len(blocks))), default=0)


def project_route_group_facts(
    *,
    route_group_id: str,
    result: FusionResult,
    paper_id: str,
    observation_texts: Mapping[str, str] | None = None,
    characterization_texts: Mapping[str, str] | None = None,
    furniture: Sequence[str] = (),
    profile: MachineProfile = MachineProfile.V1,
    creation_keys: Mapping[str, str] | None = None,
    source_selections: Mapping[str, Mapping[str, Any]] | None = None,
    public_targets: Mapping[str, Any] | None = None,
) -> RouteGroupFactProjection:
    """Publishable facts only; readable partial state does not relax publication."""
    projection = project_route_group_state(
        route_group_id=route_group_id,
        result=result,
        paper_id=paper_id,
        observation_texts=observation_texts,
        characterization_texts=characterization_texts,
        furniture=furniture,
        profile=profile,
        creation_keys=creation_keys,
        source_selections=source_selections,
        public_targets=public_targets,
    )
    if not projection.reaction_blocks:
        raise PublicationNotReady("ROUTE-" + route_group_id, "NO_ROUTE_BLOCKS")
    if not projection.route["target_paths"]:
        raise PublicationNotReady("ROUTE-" + route_group_id, "NO_SOURCE_BOUND_TARGET")
    return projection


def project_route_group_state(
    *,
    route_group_id: str,
    result: FusionResult,
    paper_id: str,
    observation_texts: Mapping[str, str] | None = None,
    characterization_texts: Mapping[str, str] | None = None,
    furniture: Sequence[str] = (),
    profile: MachineProfile = MachineProfile.V1,
    creation_keys: Mapping[str, str] | None = None,
    source_selections: Mapping[str, Mapping[str, Any]] | None = None,
    public_targets: Mapping[str, Any] | None = None,
) -> RouteGroupFactProjection:
    """The interpreted state shared by publication and a partial-result reader.

    Reading known steps does not require a publishable target or route. Public facts
    use the guarded entry above; the reader only formats this same interpretation.
    Records carry ids derived from what the author
    wrote (`stable_keys.rekey`, E66). `observation_texts` checks compound/narrative
    words against cited sources. Cards use `characterization_texts` independently;
    without the corresponding input those records cannot earn AUTHOR_TEXT.
    `furniture` is the page printing of the packages those texts came from
    (`evidence.furniture_by_package`), set aside before a card is compared.
    `creation_keys` optionally supplies host birth keys by Fusion fact id; v1.6
    otherwise derives them from immutable `fact_creation_origins`. A legacy record
    address is stable but never represented as verified creation provenance."""
    validate_result(result)
    facts = {f.fact_id: f for f in result.facts}
    route_id = "ROUTE-" + route_group_id
    unresolved: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    consumed: set[str] = set()

    known = result.fields_known

    def readings(fact: ScientificFact, field: str) -> list[Any]:
        return field_readings(result, fact, field)

    def canonical(fid: str) -> str:
        return canonical_id(result, facts, fid)

    def identified(fact: ScientificFact) -> bool:
        while True:
            if not known(fact, "kind", "status", "same_as"):
                return False
            if not fact.same_as:
                return True
            fact = facts[fact.same_as]
            if fact.status not in {"ACCEPTED", "CONTEXT"}:
                return False

    minted: dict[str, str] = {}
    account_ids: set[str] = set()
    birth_keys = {
        fid: public_creation_key(paper_id, "EXPR", origin.model_dump(mode="json"))
        for fid, origin in result.fact_creation_origins.items() if profile.unified_expression
    }
    if profile.unified_expression:
        birth_keys.update(creation_keys or {})

    def creation_key(fid: str) -> str:
        # A legacy record address is not evidence of an accepted birth action.
        return birth_keys.get(
            canonical(fid), f"fusion-record:{result.work_unit_id}:{canonical(fid)}"
        )

    def pid(kind: str, fid: str) -> str:
        public = (
            public_id(kind, creation_key(fid))
            if profile.unified_expression and kind in {"ACCOUNT", "OP", "OUT", "INT"}
            else f"{kind}-{route_group_id}-"
            + hashlib.sha256(canonical(fid).encode()).hexdigest()[:16]
        )
        # Both spellings: a question names the fact the investigator wrote, which may be
        # the one that was later merged into another (`same_as`), and both have to reach
        # the object that was published.
        minted.setdefault(fid, public)
        minted.setdefault(canonical(fid), public)
        return public

    def cited_sources(fact: ScientificFact) -> tuple[str, ...]:
        conflict = result.conflicts.get(fact.fact_id)
        parts = tuple(part for reading in readings(fact, "source_parts") for part in reading)
        part_sources = fact_source_ids({"source_parts": parts})
        if conflict and "source_ids" in conflict.candidates:
            return tuple(
                dict.fromkeys(
                    [sid for option in conflict.candidates["source_ids"] for sid in option.source_ids]
                    + list(part_sources)
                )
            )
        return tuple(dict.fromkeys((*fact.source_ids, *part_sources)))

    def refs(*items: ScientificFact, exact: bool = False) -> list[dict[str, str]]:
        if not exact:
            ids = {canonical(item.fact_id) for item in items}
            items += tuple(
                f
                for f in result.facts
                if identified(f)
                and f.status in {"ACCEPTED", "CONTEXT"}
                and f.same_as
                and canonical(f.fact_id) in ids
            )
        values = [
            r
            for item in items
            for s in (cited_sources(item) if not exact else fact_source_ids(item))
            for r in result.sources[s]
        ]
        return list(
            {(r["observation_id"], r["source_observation_sha256"]): r for r in values}.values()
        )

    def aliases(fact: ScientificFact) -> list[ScientificFact]:
        return [
            f
            for f in result.facts
            if identified(f) and f.status == "ACCEPTED" and canonical(f.fact_id) == fact.fact_id
        ]

    def value(
        subject_type: str,
        subject_id: str,
        field: str,
        items: Sequence[ScientificFact],
        inputs: set[str],
        build: Callable[..., Any],
        *,
        individual: bool = False,
        register_conflict: bool = True,
    ) -> dict[str, Any]:
        """Map actual readings; never combine multiple unknown input points."""
        projected_readings = []
        evidence: dict[bytes, dict[str, Any]] = {}
        uncertain = False
        complete = True
        readable = False
        for group in ([item] for item in items) if individual else (items,):
            # Alternative citations of the same known data remain usable evidence.
            # A data candidate below always replaces this with its own exact sources.
            group = [fact.model_copy(update={"source_ids": cited_sources(fact)}) for fact in group]
            points = [
                (i, name, options)
                for i, fact in enumerate(group)
                if fact.fact_id in result.conflicts
                for name, options in result.conflicts[fact.fact_id].candidates.items()
                if name in inputs
            ]
            uncertain |= bool(points)
            versions = [group] if not points else []
            for _, _, options in points:
                for option in options:
                    for sid in option.source_ids:
                        for ref in result.sources[sid]:
                            evidence[canonical_json_bytes(ref)] = {
                                "evidence_ref": ref,
                                "role": "CONFLICT",
                            }
            if len(points) == 1:
                index, name, options = points[0]
                for option in options:
                    updated = list(group)
                    updated[index] = ScientificFact.model_validate_json(
                        canonical_json_bytes(
                            {
                                **group[index].model_dump(),
                                name: option.value,
                                "source_ids": option.source_ids,
                            }
                        )
                    )
                    versions.append(updated)
            complete &= bool(versions)
            readable |= bool(versions)
            for version in versions:
                projected = build(version)
                source_refs = refs(*version, exact=True)
                if projected is None or not source_refs:
                    complete = False
                    continue
                evidence.update(
                    {
                        canonical_json_bytes(ref): {"evidence_ref": ref, "role": "CONFLICT"}
                        for ref in source_refs
                    }
                )
                projected_readings.append((version[0].fact_id, projected, source_refs))
        same_value = len({canonical_json_bytes(v) for _, v, _ in projected_readings}) == 1
        same_identity = not individual or len({canonical(f.fact_id) for f in items}) == 1
        if complete and not uncertain and same_value and same_identity:
            return present(
                projected_readings[0][1], [use["evidence_ref"] for use in evidence.values()]
            )
        if not projected_readings:
            # Nothing to publish on any side. Two readings that could not be combined are
            # still a conflict; readings that all project to nothing are a gap, with the
            # conflict's evidence kept either way.
            reason = "CONFLICTING_CANDIDATES" if uncertain and not readable else "STAGE_PARTIAL"
            return {**pending(reason), "evidence_uses": list(evidence.values())}
        candidates = []
        for fid, projected, source_refs in projected_readings:
            key = hashlib.sha256(canonical_json_bytes([projected, source_refs])).hexdigest()[:16]
            candidates.append(
                {
                    "candidate_id": f"{fid}-{field}-{key}",
                    "value": projected,
                    "evidence_uses": [
                        {"evidence_ref": ref, "role": "CONFLICT"} for ref in source_refs
                    ],
                }
            )
        candidates = list({c["candidate_id"]: c for c in candidates}.values())
        if len(candidates) > 1 and register_conflict:
            conflicts.append(
                {
                    "conflict_id": f"CONF-{subject_id}-{field}",
                    "target_object_type": subject_type,
                    "target_object_id": subject_id,
                    "field_key": field,
                    "blocking": False,
                    "candidate_values": candidates,
                }
            )
        return {
            **pending("CONFLICTING_CANDIDATES"),
            "candidates": candidates,
            "evidence_uses": list(evidence.values()),
        }

    def expression_field(fact: ScientificFact, field: str, build: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Project one expressed field independently, retaining its actual candidates."""
        return value("REACTION_BLOCK", route_id, field, [fact], {field},
                     build or (lambda rows: getattr(rows[0], field)), register_conflict=False)

    def interpretations(fact: ScientificFact) -> list[dict[str, Any]]:
        if not any(readings(fact, "rationale")):
            return []
        if not profile.unified_expression:
            return [model_interpretation(fact.rationale, refs(fact))] if fact.rationale else []
        return [expression_field(fact, "rationale", lambda rows:
            model_interpretation(rows[0].rationale, refs(*rows, exact=True))
            if rows[0].rationale else None)]

    def gap(
        key: str,
        evidence: list[dict[str, str]],
        reason: str = "STAGE_PARTIAL",
        *,
        kind: str,
        block_id: str | None = None,
    ) -> None:
        # `key` tells the provisional records apart; the public id is derived later
        # from the subject's stable key and `kind` (E66), never from a fact name.
        account_subject = block_id in account_ids
        unresolved.append(
            {
                "unresolved_item_id": "UNR-" + route_group_id + "-" + key,
                "scope": "PAPER"
                if account_subject
                else "ROUTE"
                if block_id is None
                else "REACTION_BLOCK",
                "subject_id": paper_id
                if account_subject
                else route_id
                if block_id is None
                else block_id,
                **(
                    {"subjects": [{"object_type": "EXPERIMENTAL_ACCOUNT", "object_id": block_id}]}
                    if account_subject
                    else {}
                ),
                "reason_code": reason,
                "evidence_refs": evidence,
                "_kind": kind,
            }
        )

    def accepted(kind: str) -> list[ScientificFact]:
        return [
            f
            for f in result.facts
            if identified(f)
            and f.kind == kind
            and f.status == "ACCEPTED"
            and canonical(f.fact_id) == f.fact_id
        ]

    def owners(fact: ScientificFact) -> set[str]:
        return {canonical(i) for i in fact.owners}

    def possible_owners(fact: ScientificFact) -> set[str]:
        return {canonical(i) for reading in readings(fact, "owners") for i in reading}

    def is_local(fid: str) -> bool:
        """A species inside one reaction: so categorized, or a context compound an
        operation forms or consumes, or one owned by a reaction or operation."""
        head = facts[fid]
        if known(head, "category") and head.category == LOCAL_INTERMEDIATE:
            return True
        if head.status != "CONTEXT":
            return False
        referenced = any(
            fid
            in {
                canonical(i)
                for field in ("inputs", "outputs")
                for reading in readings(op, field)
                for i in reading
            }
            for op in accepted("OPERATION")
        )
        owned = known(head, "owners") and any(
            facts[o].kind
            in (
                {"REACTION", "OPERATION", "EXPERIMENT"}
                if profile.unified_expression
                else {"REACTION", "OPERATION"}
            )
            for o in owners(head)
            if o in facts
        )
        return referenced or owned

    compound_members: dict[str, list[ScientificFact]] = {}
    local_members: dict[str, list[ScientificFact]] = {}
    for fact in result.facts:
        if not identified(fact) or fact.kind != "COMPOUND" or fact.status == "REJECTED":
            continue
        table = local_members if is_local(canonical(fact.fact_id)) else compound_members
        table.setdefault(canonical(fact.fact_id), []).append(fact)
    compounds: dict[str, dict[str, Any]] = {}
    for fid, members in compound_members.items():
        compound = compounds[fid] = {
            "compound_id": pid("CMP", fid),
            "author_labels": [],
            "names": [],
            "structure": pending(),
            "predecessor_ids": [],
            "successor_ids": [],
            "evidence_refs": refs(*members),
        }
        labels = value(
            "COMPOUND",
            compound["compound_id"],
            "author_labels",
            members,
            {"labels"},
            lambda rows: list(dict.fromkeys(label for row in rows for label in row.labels)),
        )
        if labels["state"] == "PRESENT":
            compound["author_labels"] = labels["value"]
        named = [f for f in members if f.status in {"ACCEPTED", "CONTEXT"}]
        if named:
            names = value(
                "COMPOUND",
                compound["compound_id"],
                "names",
                named,
                {"text"},
                lambda rows: list(dict.fromkeys(row.text for row in rows if row.text.strip())),
            )
            if names["state"] == "PRESENT":
                compound["names"] = names["value"]
            consumed.update(f.fact_id for f in named)

    def endpoint_ids(ids: tuple[str, ...]) -> list[str] | None:
        selected = [canonical(i) for i in ids]
        if not selected or any(
            i not in compounds
            or not identified(facts[i])
            or facts[i].status != "ACCEPTED"
            or not known(facts[i], "category")
            or facts[i].category == LOCAL_INTERMEDIATE
            for i in selected
        ):
            return None
        return list(dict.fromkeys(pid("CMP", i) for i in selected))

    def material_quantities(material: ScientificFact) -> list[ScientificFact]:
        """The accepted CONDITION facts written on this material: its amounts."""
        return [
            c
            for c in accepted("CONDITION")
            if known(c, "owners", "text") and owners(c) == {material.fact_id}
        ]

    def material_compound(material: ScientificFact, reaction: ScientificFact) -> str | None:
        """The route compound a substrate material is, when the reaction says so.

        A ROUTE_SUBSTRATE is one of the reaction's inputs: the only one when there is
        one, or the one whose author label the material's name ends with. Anything
        the Host would have to guess stays unbound (contract 9.7).
        """
        if material.category != "ROUTE_SUBSTRATE" or not known(reaction, "inputs"):
            return None
        inputs = [canonical(i) for i in reaction.inputs if canonical(i) in compounds]
        if len(inputs) == 1:
            return str(compounds[inputs[0]]["compound_id"])
        label = _TRAILING_LABEL.search(material.text or "")
        if label is None:
            return None
        written = canonical_author_label(label.group("paren") or label.group("bare"))
        matches = [
            cid
            for cid in inputs
            if written
            in {canonical_author_label(lab) for f in compound_members[cid] for lab in f.labels}
        ]
        return str(compounds[matches[0]]["compound_id"]) if len(matches) == 1 else None

    def absorbed_quantities(material: ScientificFact) -> list[ScientificFact]:
        """The conditions on a material that its amount or equivalents were read from."""
        return [
            c
            for c in material_quantities(material)
            if any(parse_material_quantity(c.text)) or canonical_key(c.category) == "CONCENTRATION"
        ]

    material_sources: dict[str, list[dict[str, Any]]] = {}
    material_fact_rows: dict[str, tuple[str, int]] = {}
    material_fields: dict[str, dict[str, Any]] = {}

    def material_record(material: ScientificFact, reaction: ScientificFact) -> dict[str, Any]:
        amount = equivalents = None
        fields = {"verbatim_name": refs(material)}
        for quantity in material_quantities(material):
            found_amount, found_equivalents = parse_material_quantity(quantity.text)
            # Moles are the amount when any condition gives them; otherwise the first.
            if found_amount is not None and (
                amount is None
                or found_amount["normalized_unit"] == "mol" != amount["normalized_unit"]
            ):
                amount = found_amount
                fields["amount"] = refs(quantity)
            if equivalents is None and found_equivalents is not None:
                equivalents = found_equivalents
                fields["equivalents"] = refs(quantity)
        concentrations = [
            c for c in material_quantities(material) if canonical_key(c.category) == "CONCENTRATION"
        ]
        strengths = [c.text for c in concentrations]
        if strengths:
            phrases = list(
                dict.fromkeys([*([str(amount["raw_text"])] if amount else []), *strengths])
            )
            words = "; ".join(phrases)
            if amount is None:
                amount, _ = parse_written_material_quantity(words)
            else:
                amount = {**amount, "raw_text": words}
            fields["amount"] = list(
                {
                    canonical_json_bytes(ref): ref
                    for ref in [*fields.get("amount", []), *refs(*concentrations)]
                }.values()
            )
        material_fields[material.fact_id] = fields
        identity = {}
        if profile.unified_expression and canonical(material.fact_id) in birth_keys:
            material_id = public_id("MAT", creation_key(material.fact_id))
            minted[material.fact_id] = material_id
            identity = {"material_use_id": material_id}
        return {
            **identity,
            "role": material.category if material.category in _MATERIAL_ROLES else None,
            **(
                {"role_verbatim": material.category}
                if profile.unified_expression and material.category
                else {}
            ),
            "compound_id": material_compound(material, reaction),
            "verbatim_name": material.text,
            "amount": amount,
            "equivalents": equivalents,
            **(
                {
                    "source_readings": [
                        material_source_reading(
                            role=material.category
                            if material.category in _MATERIAL_ROLES
                            else None,
                            **(
                                {"role_verbatim": material.category}
                                if profile.unified_expression and material.category
                                else {}
                            ),
                            verbatim_name=material.text,
                            amount=q.text if q is not None else None,
                            compound_id=material_compound(material, reaction),
                            refs=refs(material, q) if q is not None else refs(material),
                            reported_quantities=(
                                sourced_material_quantities(
                                    [{"text": q.text, "evidence_refs": refs(q)}]
                                )
                                if profile.unified_expression and q is not None
                                else None
                            ),
                            interpretations=[
                                interpretation
                                for f in (material, q)
                                if f is not None
                                for interpretation in interpretations(f)
                            ],
                        )
                        for q in list(material_quantities(material)) or [None]
                    ]
                }
                if profile.preservation
                else {}
            ),
        }

    blocks: list[dict[str, Any]] = []
    accounts: list[dict[str, Any]] = []
    unassigned_outcomes: list[dict[str, Any]] = []
    operation_block: dict[str, str] = {}
    reactions = [f for f in result.facts if is_route_step(result, f, facts)]
    if profile.unified_expression:
        reactions += accepted("EXPERIMENT")
    for reaction in reactions:
        is_account = reaction.kind == "EXPERIMENT"
        container_type = "EXPERIMENTAL_ACCOUNT" if is_account else "REACTION_BLOCK"
        if not identified(reaction) or (not is_account and not known(reaction, "category")):
            gap(
                reaction.fact_id + "-BOUNDARY-OR-CONTEXT",
                refs(reaction),
                kind="BOUNDARY_OR_CONTEXT",
            )
            continue
        block_id = pid("ACCOUNT" if is_account else "RXB", reaction.fact_id)
        if is_account:
            account_ids.add(block_id)
        ops = [
            op
            for op in accepted("OPERATION")
            if known(op, "owners")
            and (profile.unified_expression or known(op, "after"))
            and owners(op) == {reaction.fact_id}
        ]
        if any(
            not known(op, "owners", "after") and reaction.fact_id in possible_owners(op)
            for op in accepted("OPERATION")
        ):
            gap(
                reaction.fact_id + "-UNRESOLVED-OPERATION-ORDER",
                refs(reaction),
                kind="UNRESOLVED_OPERATION_ORDER",
                **({"block_id": block_id} if profile.unified_expression else {}),
            )
            if not profile.unified_expression:
                continue
        if not ops and not is_account:
            # Whether a step is a block is the main text's call, not whether an operation
            # was written for it (2026-09-23 ruling): a step the route draws or tells is a
            # block, and the experimental detail lane is sent to it. No operation is
            # invented; the gap names this block, so the step stays on the route as one
            # whose operations are not known.
            gap(
                reaction.fact_id + "-NO-OPERATIONS",
                refs(reaction),
                kind="NO_OPERATIONS",
                block_id=block_id,
            )
        # Order within the reaction comes from `after` alone, and only from the part of
        # it that names this reaction's own operations: a predecessor in another reaction
        # (the previous step -- how kalmanol lost steps 9 and 10, ledger E47) is already
        # expressed by the reaction boundaries and says nothing about order in here.
        # Where `after` leaves the order undecided (nothing declared, a tie, a cycle)
        # the operations keep the order they were saved in and the block is published
        # with that fact recorded once; a single operation has no order to declare.
        own = {op.fact_id for op in ops}
        ordered_ops: list[ScientificFact] = []
        remaining = list(ops)
        order_undeclared = False
        while remaining:
            done = {op.fact_id for op in ordered_ops}
            ready_ops = [
                op
                for op in remaining
                if ({canonical(i) for i in op.after if known(op, "after")} & own) <= done
            ]
            if not ready_ops:
                ready_ops = remaining
            order_undeclared = order_undeclared or len(ready_ops) > 1
            ordered_ops.extend(ready_ops)
            remaining = [op for op in remaining if op not in ready_ops]
        ops = ordered_ops
        if order_undeclared and len(ops) > 1:
            gap(
                reaction.fact_id + "-OPERATION-ORDER-UNDECLARED",
                refs(reaction, *ops),
                kind="OPERATION_ORDER_UNDECLARED",
                block_id=block_id,
            )
        public_ops: list[dict[str, Any]] = []
        for index, op in enumerate(ops, 1):
            oid = pid("OP", op.fact_id)
            operation_block[op.fact_id] = block_id
            record: dict[str, Any] = {
                "operation_id": oid,
                "sequence_index": index,
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
                "evidence_refs": refs(op),
            }
            if profile.unified_expression:
                record = build_experiment_operation(
                    creation_key=creation_key(op.fact_id),
                    position=index,
                    description=op.text or op.fact_id,
                    refs=refs(op),
                )
                record["model_description"] = expression_field(op, "text")
                record["reaction_class"] = expression_field(op, "category")
                if any(readings(op, "text")):
                    record["model_description"] = expression_field(op, "text")
                if interpretations(op):
                    record["model_interpretations"] = interpretations(op)
            materials = [m for m in accepted("MATERIAL") if op.fact_id in possible_owners(m)]

            def unplaceable_material(m: ScientificFact, block_id: str = block_id) -> None:
                """A material the model wrote whose role has no public value: the gap
                names it on its block, and it is not a route-level leftover as well."""
                gap("MATERIAL-ROLE-" + m.fact_id, refs(m), kind="MATERIAL_ROLE", block_id=block_id)
                consumed.add(m.fact_id)

            valid_materials = [
                m
                for m in materials
                if profile.unified_expression or any(category in _MATERIAL_ROLES for category in readings(m, "category"))
            ]
            if valid_materials:
                record["materials"] = value(
                    "OPERATION",
                    oid,
                    "materials",
                    valid_materials,
                    {"category", "text", "outputs", "owners"},
                    lambda rows, op=op, reaction=reaction: (
                        # The saved order is the order the author wrote the materials
                        # in (E53); it is published as `sequence_index` so a canonical
                        # sort keeps it (E66).
                        [
                            {**material_record(m, reaction), "sequence_index": index}
                            for index, m in enumerate(rows, 1)
                        ]
                        if all(
                            (profile.unified_expression or m.category in _MATERIAL_ROLES) and owners(m) == {op.fact_id}
                            for m in rows
                        )
                        else None
                    ),
                )
                consumed.update(m.fact_id for m in valid_materials)
                if record["materials"]["state"] == "PRESENT":
                    # Amount/strength may have been read in different selections from
                    # the material name. Their words and SUPPORT evidence travel
                    # together into this field; unresolved material alternatives keep
                    # their unabsorbed conditions available as notes below.
                    material_sources[oid] = [
                        {"value": deepcopy(row), "fields": material_fields[m.fact_id]}
                        for m, row in zip(
                            valid_materials, record["materials"]["value"], strict=True
                        )
                    ]
                    material_fact_rows.update(
                        (m.fact_id, (oid, row["sequence_index"]))
                        for m, row in zip(
                            valid_materials, record["materials"]["value"], strict=True
                        )
                    )
                    quantities = [c for m in valid_materials for c in absorbed_quantities(m)]
                    uses = record["materials"]["evidence_uses"]
                    for ref in refs(*quantities):
                        use = {"evidence_ref": ref, "role": "SUPPORT"}
                        if use not in uses:
                            uses.append(use)
                    consumed.update(c.fact_id for c in quantities)
                for m in materials:
                    if m not in valid_materials:
                        unplaceable_material(m)
            elif materials:
                # The model wrote a material the public roles cannot hold. That is a
                # gap the block names, not an operation that says "nothing added"
                # (the deep audit read the E57 branch as doing the latter).
                record["materials"] = pending()
                for m in materials:
                    unplaceable_material(m)
            # No material naming an operation is not a finding that nothing was added:
            # the list stays unresolved, and the task sheet offers it as missing. That
            # an operation added nothing ("filtered", "concentrated") is something a
            # reader who saw the sentence says, as an ADD of an empty list citing it.
            # Inferring it here left 26 of alchivemycin_A's f09 operations reading as
            # finished, and refused the amounts the detail tasks then read in the SI.
            conditions = [c for c in accepted("CONDITION") if op.fact_id in possible_owners(c)]

            def condition_key(c: ScientificFact, about: str | None = None) -> str | None:
                if profile.unified_expression and not known(c, "category", "text"):
                    return None
                return normalize_condition(c.category or "", c.text, about=about)

            def note_of(
                c: ScientificFact, about: str | None, extent: str | None = None
            ) -> dict[str, Any] | None:
                """A condition as the open slot carries it: the author's words, the
                model's word for its kind, the Host's key or null, its extent."""
                evidence = refs(c)
                if not evidence or not any(readings(c, "text")):
                    return None
                if profile.unified_expression:
                    result_note = {
                        "text": expression_field(c, "text"),
                        "category_verbatim": expression_field(c, "category", lambda rows: rows[0].category),
                        "extent": expression_field(c, "extent"),
                        "category": condition_key(c, about), "about": about,
                        "evidence_refs": evidence,
                    }
                    if interpretations(c):
                        result_note["model_interpretations"] = interpretations(c)
                    return result_note
                return {
                    "category_verbatim": (c.category or "").strip() or "condition",
                    "text": " ".join(c.text.split()),
                    "category": condition_key(c, about),
                    "about": about,
                    "extent": extent or c.extent,
                    "evidence_refs": evidence,
                }

            if profile.unified_expression:
                for condition in conditions:
                    full_note = note_of(condition, None)
                    if full_note is not None:
                        record["other_conditions"].append(full_note)
            for field in dict.fromkeys(FIXED_CONDITION_FIELDS.values()):
                selected = [
                    c
                    for c in conditions
                    if FIXED_CONDITION_FIELDS.get(condition_key(c) or "") == field
                ]
                if not selected:
                    continue
                alternatives = [f for c in selected for f in aliases(c)]
                if all(known(f, "extent", "owners", "category") for f in alternatives) and any(
                    f.extent != "WHOLE_OWNERS" or owners(f) != {op.fact_id} for f in alternatives
                ):
                    # A condition of one stage of the operation, or shared with another
                    # operation, has no place in a whole-operation field; the open slot
                    # keeps it with its extent (E57).
                    record[field] = pending()
                    # The narrowest extent any witness states is the condition's.
                    narrow = any(f.extent != "WHOLE_OWNERS" for f in alternatives)
                    for c in selected:
                        entry = note_of(c, None, "WITHIN_OWNER" if narrow else None)
                        if entry is not None:
                            record["other_conditions"].append(entry)
                    consumed.update(c.fact_id for c in selected)
                    continue

                def whole(
                    rows: Sequence[ScientificFact], field: str = field, op: ScientificFact = op
                ) -> bool:
                    return all(
                        row.extent == "WHOLE_OWNERS"
                        and owners(row) == {op.fact_id}
                        and FIXED_CONDITION_FIELDS.get(condition_key(row) or "") == field
                        for row in rows
                    )

                if field == "temperature_profile":
                    # A profile is the sequence of temperatures the operation went
                    # through, in the order they were written: "-78 °C" then "warmed
                    # to room temperature" is one profile, not two candidates (E52).
                    record[field] = value(
                        "OPERATION",
                        oid,
                        field,
                        alternatives,
                        {"text", "category", "owners", "extent"},
                        lambda rows, whole=whole: (
                            [parse_reported_scalar(row.text) for row in rows]
                            if whole(rows)
                            else None
                        ),
                    )
                else:
                    record[field] = value(
                        "OPERATION",
                        oid,
                        field,
                        alternatives,
                        {"text", "category", "owners", "extent"},
                        lambda rows, field=field, whole=whole: (
                            (
                                rows[0].text
                                if field in {"atmosphere", "key_addition", "key_workup"}
                                else parse_reported_scalar(rows[0].text)
                            )
                            if whole(rows)
                            else None
                        ),
                        individual=True,
                    )
                consumed.update(c.fact_id for c in selected)
            for c in conditions:
                if c.fact_id in consumed or not known(c, "category", "text"):
                    continue
                # A condition the fixed fields have no place for (microwave, one-pot, a
                # word the table does not know): the open slot, never a gap (E57).
                entry = note_of(c, None)
                if entry is not None:
                    record["other_conditions"].append(entry)
                    consumed.add(c.fact_id)
            # Other material instructions retain their notes. Solution strength has
            # already joined the material amount phrase, per SOLUTION_STRENGTH.
            for m in valid_materials:
                for c in material_quantities(m):
                    if c.fact_id in consumed or not known(c, "category", "text"):
                        continue
                    entry = note_of(c, m.text)
                    if entry is not None:
                        record["other_conditions"].append(entry)
                        consumed.add(c.fact_id)
            record["other_conditions"] = list({
                canonical_json_bytes(note): note for note in record["other_conditions"]
            }.values())
            public_ops.append(record)
            consumed.add(op.fact_id)
        # A local intermediate is the species one operation forms and the next consumes.
        # The model writes it as a CONTEXT compound owned by that operation (or, less
        # usefully, by the reaction); the Host places it from the owner: formed by the
        # owning operation, consumed by the one after it. An explicit inputs/outputs
        # reading on the operations, when the model gave one, wins. What cannot be
        # placed is reported on the block, never published as a compound of the route.
        local_records: list[dict[str, Any]] = []
        local_by_producer: dict[str, list[tuple[ScientificFact, ScientificFact]]] = {}
        unplaced_locals: list[ScientificFact] = []
        op_ids = [op.fact_id for op in ops]
        for local_id in local_members:
            local_compound = facts[local_id]
            if not identified(local_compound) or (
                not profile.unified_expression and not known(local_compound, "owners")
            ):
                continue
            producers = [
                op
                for op in ops
                if local_id
                in {canonical(i) for reading in readings(op, "outputs") for i in reading}
            ]
            consumers = [
                op
                for op in ops
                if local_id in {canonical(i) for reading in readings(op, "inputs") for i in reading}
            ]
            local_owners = owners(local_compound) if known(local_compound, "owners") else set()
            here = (
                reaction.fact_id in local_owners
                or bool(local_owners & set(op_ids))
                or bool(producers or consumers)
            )
            if not here:
                continue
            if profile.unified_expression:
                # Only expressed links are known. Owning an operation states its
                # producer; neither operation order nor a two-operation container
                # establishes which later operation consumes this species.
                producing = list(
                    dict.fromkeys(
                        [op.fact_id for op in producers if known(op, "outputs")]
                        + [op.fact_id for op in ops if op.fact_id in local_owners]
                    )
                )
                consuming = [op.fact_id for op in consumers if known(op, "inputs")]
                labels = {label for local in local_members[local_id] for label in local.labels}
                local_records.append({
                    "local_intermediate_id": pid("INT", local_id),
                    "produced_by": {"known_operation_ids": [pid("OP", fid) for fid in producing]},
                    "consumed_by": {"known_operation_ids": [pid("OP", fid) for fid in consuming]},
                    "author_label": next(iter(labels)) if len(labels) == 1 else None,
                    "structure": pending(),
                    "evidence_refs": refs(*local_members[local_id]),
                    **({"model_interpretations": interpretations(local_compound)}
                       if interpretations(local_compound) else {}),
                })
                consumed.update(f.fact_id for f in local_members[local_id])
                continue
            if (
                len(producers) == len(consumers) == 1
                and producers[0] != consumers[0]
                and known(producers[0], "outputs")
                and known(consumers[0], "inputs")
            ):
                producer, consumer = producers[0], consumers[0]
                local_by_producer.setdefault(producer.fact_id, []).append(
                    (local_compound, consumer)
                )
                continue
            owning = [op for op in ops if op.fact_id in owners(local_compound)]
            if len(owning) == 1 and op_ids.index(owning[0].fact_id) + 1 < len(op_ids):
                consumer = ops[op_ids.index(owning[0].fact_id) + 1]
                local_by_producer.setdefault(owning[0].fact_id, []).append(
                    (local_compound, consumer)
                )
                continue
            if not owning and reaction.fact_id in owners(local_compound) and len(ops) == 2:
                # Owned by the reaction itself, in a reaction of exactly two operations:
                # the only place it can sit is between them (E57).
                local_by_producer.setdefault(ops[0].fact_id, []).append((local_compound, ops[1]))
                continue
            unplaced_locals.append(local_compound)
        for local_compound in unplaced_locals:
            gap(
                "LOCAL-INTERMEDIATE-" + local_compound.fact_id,
                refs(local_compound),
                kind="LOCAL_INTERMEDIATE",
                block_id=block_id,
            )
            consumed.update(f.fact_id for f in local_members[local_compound.fact_id])
        for producer_id, local_species in local_by_producer.items():
            if len(local_species) > 1:
                gap(
                    "CAPABILITY_LIMIT-MULTIPLE-LOCAL-SPECIES-" + producer_id,
                    refs(*(compound for compound, _ in local_species)),
                    kind="MULTIPLE_LOCAL_SPECIES",
                    block_id=block_id,
                )
                for local, _ in local_species:
                    consumed.update(f.fact_id for f in local_members[local.fact_id])
                continue
            local_compound, consumer = local_species[0]
            consumed.update(f.fact_id for f in local_members[local_compound.fact_id])
            record = next(r for r in public_ops if r["operation_id"] == pid("OP", producer_id))
            record["operation_intermediate"] = value(
                "OPERATION",
                pid("OP", producer_id),
                "operation_intermediate",
                local_members[local_compound.fact_id],
                {"labels"},
                lambda rows, local=local_compound, producer=producer_id, consumer=consumer: {
                    "local_intermediate_id": pid("INT", local.fact_id),
                    "produced_by_operation_id": pid("OP", producer),
                    "consumed_by_operation_id": pid("OP", consumer.fact_id),
                    "author_label": next(iter(labels))
                    if len(labels := {label for row in rows for label in row.labels}) == 1
                    else None,
                    "structure": pending(),
                    "evidence_refs": refs(*rows, exact=True),
                },
            )
        blocks.append(
            {
                "reaction_block_id": block_id,
                "source_order": len(blocks) + 1,
                "input_compound_ids": value(
                    container_type,
                    block_id,
                    "input_compound_ids",
                    [reaction],
                    {"inputs"},
                    lambda rows: endpoint_ids(rows[0].inputs),
                ),
                "output_compound_ids": value(
                    container_type,
                    block_id,
                    "output_compound_ids",
                    [reaction],
                    {"outputs"},
                    lambda rows: endpoint_ids(rows[0].outputs),
                ),
                "operations": public_ops,
                **(
                    {
                        "local_intermediates": local_records,
                        **(
                            {"model_summary": expression_field(reaction, "text")}
                            if any(readings(reaction, "text"))
                            else {}
                        ),
                        **(
                            {"model_interpretations": interpretations(reaction)}
                            if interpretations(reaction)
                            else {}
                        ),
                    }
                    if profile.unified_expression
                    else {}
                ),
                "reported_outcomes": [],
                "evidence_refs": refs(reaction, *ops),
                "optimization_context_ids": [],
                "failed_attempt_context_ids": [],
                "mechanism_context_ids": [],
            }
        )
        if is_account:
            boundary = blocks.pop()
            account = build_experimental_account(
                creation_key=creation_key(reaction.fact_id),
                title=reaction.text
                if known(reaction, "text") and reaction.text.strip()
                else reaction.fact_id,
                explanation=reaction.rationale or reaction.text or reaction.fact_id,
                refs=refs(reaction, *ops),
                operations=public_ops,
                source_parts=[
                    selected_source_part(
                        SourceSelection.model_validate(part), source_selections or {},
                        field_path=f"{reaction.fact_id}.source_parts",
                    )
                    for parts in readings(reaction, "source_parts") for part in parts
                ],
            )
            account["input_compound_ids"] = boundary["input_compound_ids"]
            account["output_compound_ids"] = boundary["output_compound_ids"]
            account["local_intermediates"] = local_records
            account["model_interpretations"] = [
                expression_field(
                    reaction,
                    "text",
                    lambda rows: (
                        model_interpretation(rows[0].text, refs(*rows, exact=True))
                        if rows[0].text
                        else None
                    ),
                ),
                *interpretations(reaction),
            ]
            accounts.append(account)
        consumed.add(reaction.fact_id)

    # A named NOTE is one relation, with its own lifetime and support. Targets
    # belong to the request's public inventory, not this independent fact group.
    operations = list(experiment_operations(public_targets)) if public_targets is not None else []
    operation_ids = {op["operation_id"] for op in operations}
    material_owners = {material["material_use_id"]: op["operation_id"]
                       for op in operations for material in op["materials"].get("value") or ()
                       if "material_use_id" in material}
    account_by_id = {account["account_id"]: account for account in accounts}
    preparation_destinations: dict[str, str] = {}
    for note in accepted("NOTE"):
        if note.preparation_use is None:
            continue
        consumed.add(note.fact_id)  # Never turn a relation into a duplicate narrative.
        if not known(note, "preparation_use", "owners"):
            continue  # Existing conflict projection retains the actual alternatives.
        target = note.preparation_use
        validate_preparation_target(target.operation, target.material,
                                    operation_ids=operation_ids, material_owners=material_owners)
        owner = minted.get(canonical(note.owners[0]))
        if owner not in account_by_id:
            raise ValueError("preparation-use owner has no projected experimental account")
        support = refs(note, exact=True)
        use = {"target_operation_id": target.operation, "target_material_use_id": target.material,
               "evidence_refs": support,
               "model_interpretations": [
                   expression_field(note, "text", lambda rows: model_interpretation(
                       rows[0].text, refs(*rows, exact=True))), *interpretations(note)]}
        account_by_id[owner].setdefault("preparation_uses", []).append(use)
        # A relation lands in an account but did not create that account. Keep
        # destination mappings out of the producer identity map used by rekey.
        preparation_destinations[note.fact_id] = owner

    def outcome_kind(outcome: ScientificFact) -> str | None:
        """The contract's kind for an outcome: its category when that is one, else the
        one its own words name -- "dr = 3:1" is a DR, "98% ee" an EE. Anything else has
        no kind rather than a guessed one."""
        if outcome.category in _OUTCOME_KINDS:
            return outcome.category
        words = outcome.text.casefold()
        if re.search(r"\b(?:dr|d\.r\.|diastereomeric ratio)\b", words):
            return "DR"
        if re.search(r"\b(?:ee|e\.e\.|enantiomeric excess)\b", words):
            return "EE"
        return None

    operation_order = {
        operation["operation_id"]: (index, operation["sequence_index"])
        for index, block in enumerate(blocks)
        for operation in block["operations"]
    }

    def outcome_block(outcome: ScientificFact, destinations: set[str]) -> dict[str, Any]:
        spanned = [b for b in blocks if b["reaction_block_id"] in destinations]
        if len(spanned) == 1:
            return spanned[0]
        products = set(endpoint_ids(outcome.outputs) or ())
        by_product = [b for b in spanned if products & set(b["output_compound_ids"]["value"] or ())]
        if len(by_product) == 1:
            return by_product[0]
        upstream = {
            a["reaction_block_id"]
            for a in spanned
            for b in spanned
            if a is not b
            and set(a["output_compound_ids"]["value"] or ())
            & set(b["input_compound_ids"]["value"] or ())
        }
        downstream = [b for b in spanned if b["reaction_block_id"] not in upstream]
        return downstream[0] if len(downstream) == 1 else spanned[0]

    if profile.unified_expression:
        containers = {
            c.get("reaction_block_id", c.get("account_id")): c for c in [*blocks, *accounts]
        }
        container_refs = {
            fid: {
                "object_type": "EXPERIMENTAL_ACCOUNT"
                if public in account_ids
                else "REACTION_BLOCK",
                "object_id": public,
            }
            for fid, public in minted.items()
            if public in containers
        }
        for fid, public in operation_block.items():
            container_refs[fid] = {
                "object_type": "EXPERIMENTAL_ACCOUNT"
                if public in account_ids
                else "REACTION_BLOCK",
                "object_id": public,
            }

        def experiment_reading(fact: ScientificFact) -> dict[str, Any]:
            def references(rows: Sequence[ScientificFact]) -> list[dict[str, str]]:
                return list(
                    {
                        canonical_json_bytes(container_refs[canonical(owner)]): container_refs[
                            canonical(owner)
                        ]
                        for owner in rows[0].owners
                        if canonical(owner) in container_refs
                    }.values()
                )

            reading = expression_field(fact, "owners", references)
            complete = all(
                group and all(canonical(owner) in container_refs for owner in group)
                for group in readings(fact, "owners")
            )
            if reading["state"] == "PRESENT" and len(reading["value"]) == 1 and complete:
                return {**reading, "value": reading["value"][0]}
            choices = (
                [{"value": reading["value"], "evidence_uses": reading["evidence_uses"]}]
                if reading["state"] == "PRESENT"
                else reading["candidates"]
            )
            candidates = [
                {
                    "candidate_id": public_id(
                        "CAND",
                        fact.fact_id
                        + "|experiment|"
                        + canonical_json_bytes([reference, row["evidence_uses"]]).decode(),
                    ),
                    "value": reference,
                    "evidence_uses": row["evidence_uses"],
                }
                for row in choices
                for reference in row["value"]
            ]
            return {
                **pending("CONFLICTING_CANDIDATES" if candidates else "INSUFFICIENT_EVIDENCE"),
                "candidates": list({c["candidate_id"]: c for c in candidates}.values()),
                "evidence_uses": reading["evidence_uses"],
            }

        outcome_facts = {f.fact_id: f for f in accepted("OUTCOME")}
        identities: dict[str, dict[str, Any]] = {}

        def measurement_identity(
            fact: ScientificFact, visiting: frozenset[str] = frozenset()
        ) -> dict[str, Any]:
            fid = canonical(fact.fact_id)
            if fid in identities:
                return identities[fid]
            if fid in visiting:
                raise ValueError("measurement same_as identity cycle cannot be projected")
            intent = fact.measurement if known(fact, "measurement") else None
            target = canonical(intent.same_as) if intent is not None and intent.same_as else None
            if target is not None and target in outcome_facts:
                identity = measurement_identity(outcome_facts[target], visiting | {fid})
            else:
                identity = {"outcome_id": pid("OUT", fid), "stable_key": creation_key(fid)}
            identities[fid] = identity
            for alias in aliases(outcome_facts[fid]):
                minted[alias.fact_id] = identity["outcome_id"]
            return identity

        contributions = {}
        for outcome in outcome_facts.values():
            identity = measurement_identity(outcome)
            for reading_fact in aliases(outcome):
                intent = reading_fact.measurement if known(reading_fact, "measurement") else None
                if intent is not None and intent.same_as is not None:
                    target = canonical(intent.same_as)
                    intent = MeasurementIntent.model_validate(
                        {"same_as": identity["outcome_id"]}
                        if target in outcome_facts
                        else {"unknown": True}
                    )
                intent = intent or MeasurementIntent.model_validate({"unknown": True})
                projected = build_measurement(
                    creation_key=identity["stable_key"],
                    text=reading_fact.text or reading_fact.fact_id,
                    kind=(reading_fact.category or None)
                    if known(reading_fact, "category")
                    else None,
                    product_ids=endpoint_ids(reading_fact.outputs) or ()
                    if known(reading_fact, "outputs")
                    else (),
                    operation_ids=[],
                    measurement=intent,
                    experiment=None,
                    experiment_reading=experiment_reading(reading_fact),
                    existing=identity if intent.same_as else None,
                    refs=refs(reading_fact),
                    explanation=reading_fact.rationale or reading_fact.text or reading_fact.fact_id,
                )
                record = projected.outcome
                record["outcome_id"] = identity["outcome_id"]
                record["kind"] = expression_field(
                    reading_fact,
                    "category",
                    lambda rows: rows[0].category if rows[0].category in _OUTCOME_KINDS else None,
                )
                record["reported_value"] = expression_field(
                    reading_fact,
                    "text",
                    lambda rows: parse_reported_outcome(rows[0].text)[0] if rows[0].text else None,
                )
                record["basis"] = expression_field(
                    reading_fact, "text", lambda rows: parse_reported_outcome(rows[0].text)[1]
                )
                record["product_compound_ids"] = expression_field(
                    reading_fact, "outputs", lambda rows: endpoint_ids(rows[0].outputs)
                )
                record["operation_ids"] = expression_field(
                    reading_fact,
                    "owners",
                    lambda rows: (
                        list(dict.fromkeys(
                            minted[canonical(owner)]
                            for owner in rows[0].owners
                            if canonical(owner) in operation_block
                        )) or None
                    ),
                )
                if interpretations(reading_fact):
                    record["model_interpretations"] = interpretations(reading_fact)
                else:
                    record.pop("model_interpretations", None)
                # Source word candidates keep their own citations; no cross product with
                # independent category/product/owner candidate sets is constructed.
                text_conflict = result.conflicts.get(reading_fact.fact_id)
                text_options = text_conflict.candidates.get("text") if text_conflict else None
                text_rows = (
                    [
                        (
                            option.value,
                            [ref for sid in option.source_ids for ref in result.sources[sid]],
                        )
                        for option in text_options
                    ]
                    if text_options is not None
                    else [(reading_fact.text, refs(reading_fact, exact=True))]
                )
                record["source_statements"] = [
                    source_statement(
                        text,
                        evidence,
                        interpretations(reading_fact),
                        kind=reading_fact.category
                        if known(reading_fact, "category")
                        and reading_fact.category in _OUTCOME_KINDS
                        else None,
                        product_ids=(endpoint_ids(reading_fact.outputs) or [])
                        if known(reading_fact, "outputs")
                        else [],
                        **(
                            {"kind_verbatim": reading_fact.category}
                            if known(reading_fact, "category") and reading_fact.category
                            else {}
                        ),
                    )
                    for text, evidence in text_rows
                    if text
                ]
                if not record["source_statements"]:
                    record.pop("source_statements")
                contributions[reading_fact.fact_id] = projected
                consumed.add(reading_fact.fact_id)
        applied = apply_measurement_contributions(
            {
                "domain_payload": {
                    "reaction_blocks": blocks,
                    "experimental_accounts": accounts,
                    "unassigned_outcomes": [],
                }
            },
            contributions,
        )["domain_payload"]
        blocks = applied["reaction_blocks"]
        accounts = applied["experimental_accounts"]
        unassigned_outcomes = applied["unassigned_outcomes"]

    for outcome in [] if profile.unified_expression else accepted("OUTCOME"):
        alternatives = aliases(outcome)
        owner_sets = {
            tuple(sorted(canonical(i) for i in reading))
            for f in alternatives
            for reading in readings(f, "owners")
        }
        destinations = {
            operation_block[o] for group in owner_sets for o in group if o in operation_block
        }
        # Unpublishable witnesses still participate in deciding the measurement's scope.
        if (
            any(not group or not set(group) <= operation_block.keys() for group in owner_sets)
            or not destinations
            or (len(owner_sets) > 1 and len(destinations) > 1)
        ):
            gap(
                outcome.fact_id + "-OUTCOME-SCOPE-CAPABILITY",
                refs(*alternatives),
                kind="OUTCOME_SCOPE",
            )
            consumed.add(outcome.fact_id)
            continue
        # Aggregate measurements spanning blocks cannot be placed in the frozen local
        # operation-ID field. Publish their reported value once, on the block that makes
        # the product the author reported it for -- "57% over 2 steps" belongs where
        # the 57% was isolated, not where the two steps began -- and retain scope
        # unresolved. Without a product, the downstream end of the span.
        block = outcome_block(outcome, destinations)
        block["reported_outcomes"].append(
            {
                "outcome_id": pid("OUT", outcome.fact_id),
                "kind": value(
                    "REPORTED_OUTCOME",
                    pid("OUT", outcome.fact_id),
                    "kind",
                    alternatives,
                    # The words decide the kind only when the category is not one of
                    # the contract's, so a conflict over the words leaves a stated kind
                    # untouched.
                    {"category"}
                    if any(f.category in _OUTCOME_KINDS for f in alternatives)
                    else {"category", "text"},
                    lambda rows: outcome_kind(rows[0]),
                    individual=True,
                ),
                "reported_value": value(
                    "REPORTED_OUTCOME",
                    pid("OUT", outcome.fact_id),
                    "reported_value",
                    alternatives,
                    {"text"},
                    lambda rows: parse_reported_outcome(rows[0].text)[0],
                    individual=True,
                ),
                "value_provenance": "REPORTED",
                "basis": value(
                    "REPORTED_OUTCOME",
                    pid("OUT", outcome.fact_id),
                    "basis",
                    alternatives,
                    {"text"},
                    lambda rows: parse_reported_outcome(rows[0].text)[1],
                    individual=True,
                ),
                "operation_ids": value(
                    "REPORTED_OUTCOME",
                    pid("OUT", outcome.fact_id),
                    "operation_ids",
                    alternatives,
                    {"owners", "extent"},
                    # The operations the value was measured over, in the order the
                    # route runs them, in whichever blocks of the route they lie (E57).
                    lambda rows: (
                        sorted(
                            (pid("OP", o) for o in owners(rows[0])),
                            key=lambda oid: operation_order.get(oid, (len(blocks), 0)),
                        )
                        if rows[0].extent == "WHOLE_OWNERS"
                        and owners(rows[0]) <= operation_block.keys()
                        else None
                    ),
                    individual=True,
                )
                if not any(known(f, "extent") and f.extent != "WHOLE_OWNERS" for f in alternatives)
                else pending(),
                "product_compound_ids": value(
                    "REPORTED_OUTCOME",
                    pid("OUT", outcome.fact_id),
                    "product_compound_ids",
                    alternatives,
                    {"outputs"},
                    lambda rows: endpoint_ids(rows[0].outputs),
                    individual=True,
                ),
                "evidence_refs": refs(outcome),
            }
        )
        if profile.preservation:
            block["reported_outcomes"][-1]["source_statements"] = [
                source_statement(f.text, refs(f),
                    interpretations(f),
                    kind=outcome_kind(f), product_ids=endpoint_ids(f.outputs),
                    **({"kind_verbatim": f.category} if profile.unified_expression and f.category else {}))
                for f in alternatives if f.text
            ]
        consumed.add(outcome.fact_id)
    # Topology is set arithmetic over accepted boundaries, never over author labels.
    deps = {
        b["reaction_block_id"]: {
            a["reaction_block_id"]
            for a in blocks
            if a is not b
            and set(a["output_compound_ids"]["value"] or ())
            & set(b["input_compound_ids"]["value"] or ())
        }
        for b in blocks
    }
    # One ready block at a time, the first in saved order: a chain continues before a
    # parallel branch starts, so two fragments of a convergent synthesis (or the two
    # halves of a chain with a missing step) are numbered each in its own run rather
    # than interleaved layer by layer (E52).
    ordered: list[dict[str, Any]] = []
    while len(ordered) < len(blocks):
        done = {b["reaction_block_id"] for b in ordered}
        ready = next(
            (
                b
                for b in blocks
                if b["reaction_block_id"] not in done and deps[b["reaction_block_id"]] <= done
            ),
            None,
        )
        if ready is None:
            gap(
                "CYCLIC-ADJACENCY",
                [r for b in blocks for r in b["evidence_refs"]],
                kind="CYCLIC_ADJACENCY",
            )
            ordered.extend(b for b in blocks if b["reaction_block_id"] not in done)
            break
        ordered.append(ready)
    blocks = ordered
    for i, block in enumerate(blocks, 1):
        block["source_order"] = i
        for start in block["input_compound_ids"]["value"] or ():
            for end in block["output_compound_ids"]["value"] or ():
                for compound in compounds.values():
                    if compound["compound_id"] == start and end not in compound["successor_ids"]:
                        compound["successor_ids"].append(end)
                    if compound["compound_id"] == end and start not in compound["predecessor_ids"]:
                        compound["predecessor_ids"].append(start)
    paths: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    route_compounds = {
        cid
        for b in blocks
        for cid in (
            *(b["input_compound_ids"]["value"] or ()),
            *(b["output_compound_ids"]["value"] or ()),
        )
    }
    for target in accepted("TARGET"):
        if (
            not known(target, "owners")
            or len(target.owners) != 1
            or endpoint_ids(target.owners) is None
        ):
            continue
        cid = pid("CMP", target.owners[0])
        needed = {cid}
        relevant: set[str] = set()
        path_known = True
        while True:
            previous = len(needed)
            for reaction in reactions:
                possible_outputs = {
                    pid("CMP", i) for candidate in readings(reaction, "outputs") for i in candidate
                }
                if not needed & possible_outputs:
                    continue
                path_block = next(
                    (b for b in blocks if b["reaction_block_id"] == pid("RXB", reaction.fact_id)),
                    None,
                )
                if path_block is None or any(
                    path_block[field]["state"] != "PRESENT"
                    for field in ("input_compound_ids", "output_compound_ids")
                ):
                    path_known = False
                if path_block is not None:
                    relevant.add(path_block["reaction_block_id"])
                needed.update(
                    pid("CMP", i) for candidate in readings(reaction, "inputs") for i in candidate
                )
            if len(needed) == previous:
                break
        # Only the four TARGET-owned categories are read here; a narrative the author
        # attached to the target (its scope, a conclusion) is projected with the others.
        reported = {
            f.category: f
            for f in accepted("NOTE")
            if known(f, "owners", "category")
            and owners(f) == {target.fact_id}
            and f.category in _TARGET_NOTE_CATEGORIES
        }
        count = reported.get("AUTHOR_STEP_COUNT")
        overall = reported.get("AUTHOR_OVERALL_YIELD")
        if count is not None and relevant and path_known:
            # The author's step count against the route that reached the target: a
            # chemist counts the longest linear sequence, a reader may count every
            # block, so either matching is agreement; neither matching is a gap that
            # says the route was merged or split somewhere (E63). The count is the
            # author's and the blocks are what was written -- the evidence is short,
            # not wrong.
            written = parse_step_count(count.text)
            path_blocks = [b for b in blocks if b["reaction_block_id"] in relevant]
            if written is not None and written not in {
                len(path_blocks),
                longest_chain(path_blocks),
            }:
                gap(
                    "STEP-COUNT-MISMATCH-" + target.fact_id,
                    refs(count),
                    "INSUFFICIENT_EVIDENCE",
                    kind="STEP_COUNT_MISMATCH",
                )
        paths.append(
            {
                "target_path_id": pid("TGT", target.fact_id),
                "target_compound_id": cid,
                "reaction_block_ids": present(
                    [b["reaction_block_id"] for b in blocks if b["reaction_block_id"] in relevant],
                    refs(target),
                )
                if relevant and path_known
                else pending(),
                "reported_step_count": value(
                    "TARGET_PATH",
                    pid("TGT", target.fact_id),
                    "reported_step_count",
                    [count],
                    {"text"},
                    lambda rows: parse_step_count(rows[0].text),
                )
                if count
                else pending(),
                "reported_overall_yield": value(
                    "TARGET_PATH",
                    pid("TGT", target.fact_id),
                    "reported_overall_yield",
                    [overall],
                    {"text"},
                    lambda rows: parse_reported_scalar(rows[0].text),
                )
                if overall
                else pending(),
                "evidence_refs": refs(target),
            }
        )
        subject, assertion_outcome = (
            reported.get("ASSERTION_SUBJECT"),
            reported.get("ASSERTION_OUTCOME"),
        )
        if (
            subject
            and assertion_outcome
            and any(text in _ASSERTION_SUBJECTS for text in readings(subject, "text"))
            and any(text in _ASSERTION_OUTCOMES for text in readings(assertion_outcome, "text"))
        ):
            assertions.append(
                {
                    "assertion_id": pid("ASSERT", target.fact_id),
                    "target_compound_id": cid,
                    "subject": value(
                        "TARGET_ASSERTION",
                        pid("ASSERT", target.fact_id),
                        "subject",
                        [subject],
                        {"text"},
                        lambda rows: rows[0].text if rows[0].text in _ASSERTION_SUBJECTS else None,
                    ),
                    "outcome": value(
                        "TARGET_ASSERTION",
                        pid("ASSERT", target.fact_id),
                        "outcome",
                        [assertion_outcome],
                        {"text"},
                        lambda rows: rows[0].text if rows[0].text in _ASSERTION_OUTCOMES else None,
                    ),
                    "author_statement": value(
                        "TARGET_ASSERTION",
                        pid("ASSERT", target.fact_id),
                        "author_statement",
                        [target],
                        {"text"},
                        lambda rows: rows[0].text,
                    ),
                    "evidence_refs": refs(target, subject, assertion_outcome),
                }
            )
        consumed.add(target.fact_id)
        consumed.update(f.fact_id for f in reported.values())
        route_compounds.add(cid)

    # NOTE facts with a public home (ledger E47). A characterization note is owned by
    # the compounds it characterizes and becomes one card: the author's words, the
    # Host's key for them, a number when the words carry one. A narrative note becomes
    # a paper narrative; when it describes a reaction's mechanism, optimization or a
    # failed attempt and is owned by that reaction or one of its operations, the block
    # lists it in the matching context list. The four TARGET-owned categories were
    # consumed above. Anything else stays unmapped, and says so.
    blocks_by_id = {block["reaction_block_id"]: block for block in blocks}
    characterizations: list[dict[str, Any]] = []
    narratives: list[dict[str, Any]] = []
    for note in accepted("NOTE"):
        if note.fact_id in consumed or (
            not profile.unified_expression and not known(note, "category", "text", "owners")
        ):
            continue
        evidence = refs(note)
        if not evidence:
            continue
        if note.category == CHARACTERIZATION_CATEGORY and known(note, "category", "text", "owners"):
            owner_ids = [canonical(i) for i in note.owners]
            if not owner_ids or any(i not in compounds for i in owner_ids):
                gap(
                    note.fact_id + "-CHARACTERIZATION-OWNER",
                    evidence,
                    kind="CHARACTERIZATION_OWNER",
                )
                consumed.add(note.fact_id)
                continue
            words = " ".join(note.text.split())
            if not words:
                continue
            cards = characterization_fields(words, profile=profile)
            for index, card in enumerate(cards, 1):
                characterizations.append(
                    {
                        "characterization_id": pid("CHAR", note.fact_id)
                        + (f"-{index}" if len(cards) > 1 else ""),
                        "compound_ids": list(
                            dict.fromkeys(compounds[i]["compound_id"] for i in owner_ids)
                        ),
                        **card,
                        "sample_context": None
                        if profile.expression
                        else " ".join(note.rationale.split()) or None,
                        "evidence_refs": evidence,
                        **(
                            {
                                "source_statements": [
                                    characterization_source_statement(note.text, None, evidence)
                                ],
                                **(
                                    {
                                        "model_interpretations": interpretations(note)
                                    }
                                    if note.rationale
                                    else {}
                                ),
                            }
                            if profile.expression
                            else {}
                        ),
                    }
                )
            consumed.add(note.fact_id)
            continue
        if note.category in _NARRATIVE_KINDS or profile.unified_expression:
            author_text = note.text.strip() or None if known(note, "text") else None
            explanation = note.rationale.strip() or None
            if (
                author_text is None
                and explanation is None
                and not (
                    profile.unified_expression
                    and (any(readings(note, "text")) or any(readings(note, "rationale")))
                )
            ):
                continue
            narrative_id = pid("NARR", note.fact_id)
            narrative: dict[str, Any] = {
                "narrative_id": narrative_id,
                "kind": note.category
                if known(note, "category") and note.category in _NARRATIVE_KINDS
                else None,
                **(
                    {"kind_verbatim": note.category}
                    if profile.unified_expression and known(note, "category") and note.category
                    else {}
                ),
                "author_text": author_text,
                "evidence_refs": evidence,
            }
            if explanation is not None or (
                profile.unified_expression
                and (author_text is None or any(readings(note, "rationale")))
            ):
                narrative["explanation"] = (
                    expression_field(note, "rationale")
                    if profile.unified_expression else explanation
                )
            if profile.unified_expression and not known(note, "text"):
                narrative["source_parts"] = [
                    {
                        "text": candidate.value,
                        "evidence_refs": [
                            ref for sid in candidate.source_ids for ref in result.sources[sid]
                        ],
                    }
                    for candidate in result.conflicts[note.fact_id].candidates["text"]
                    if candidate.value
                ]
                if not narrative["source_parts"]:
                    narrative.pop("source_parts")
            if profile.preservation:
                public_subjects = {
                    **{c["compound_id"]: "COMPOUND" for c in compounds.values()},
                    **{b["reaction_block_id"]: "REACTION_BLOCK" for b in blocks},
                    **{a["account_id"]: "EXPERIMENTAL_ACCOUNT" for a in accounts},
                    **{o["outcome_id"]: "REPORTED_OUTCOME" for o in unassigned_outcomes},
                    **{
                        o["operation_id"]: "OPERATION"
                        for b in [*blocks, *accounts]
                        for o in b["operations"]
                    },
                    **{
                        o["outcome_id"]: "REPORTED_OUTCOME"
                        for b in [*blocks, *accounts]
                        for o in b["reported_outcomes"]
                    },
                }
                # Only already projected, explicitly owned objects are eligible.
                narrative["subjects"] = [
                    {"object_type": public_subjects[minted[owner]], "object_id": minted[owner]}
                    for owner in sorted(
                        owners(note)
                        if known(note, "owners")
                        else set.intersection(
                            *(
                                set(canonical(i) for i in reading)
                                for reading in readings(note, "owners")
                            )
                        )
                    )
                    if owner in minted and minted[owner] in public_subjects
                ]
                narrative["subjects"] = list({
                    canonical_json_bytes(row): row for row in narrative["subjects"]
                }.values())
            narratives.append(narrative)
            consumed.add(note.fact_id)
            context_list = (
                _BLOCK_CONTEXT_LISTS.get(note.category)
                if known(note, "category", "owners")
                else None
            )
            if context_list is None:
                continue
            for owner in owners(note):
                block_id = operation_block.get(owner) or pid("RXB", owner)
                context_block = blocks_by_id.get(block_id)
                if context_block is not None and narrative_id not in context_block[context_list]:
                    context_block[context_list].append(narrative_id)

    for fact in result.facts:
        if (
            fact.fact_id in result.conflicts
            or fact.status == "UNRESOLVED"
            or (
                fact.status == "ACCEPTED"
                and fact.fact_id not in consumed
                and canonical(fact.fact_id) == fact.fact_id
            )
        ):
            on_material = (
                fact.status == "ACCEPTED"
                and fact.kind == "CONDITION"
                and any(facts[i].kind == "MATERIAL" for i in fact.owners if i in facts)
            )
            # A condition on a material the contract cannot hold (a solution's
            # concentration) is a limit of the block that material is used in.
            material_block = next(
                (
                    operation_block[op]
                    for i in fact.owners
                    if on_material and i in facts
                    for op in owners(facts[i])
                    if op in operation_block
                ),
                None,
            )
            leftover_kind = (
                "FIELD_CONFLICT"
                if fact.fact_id in result.conflicts
                else "CAPABILITY_LIMIT"
                if on_material
                else "UNMAPPED"
                if fact.status == "ACCEPTED"
                else "UNDECIDED"
            )
            gap(
                leftover_kind.replace("_", "-") + "-" + fact.fact_id,
                refs(fact),
                kind=leftover_kind,
                block_id=material_block,
            )
            if profile.unified_expression and fact.fact_id in result.conflicts:
                field_candidates = result.conflicts[fact.fact_id].candidates
                unresolved[-1]["statement"] = "\n".join(
                    fact.fact_id
                    + "."
                    + field
                    + " candidate: "
                    + canonical_json_bytes(option.value).decode()
                    + "; sources: "
                    + ", ".join(option.source_ids)
                    for field, options in field_candidates.items()
                    for option in options
                )
                unresolved[-1]["evidence_refs"] = list(
                    {
                        canonical_json_bytes(ref): ref
                        for options in field_candidates.values()
                        for option in options
                        for sid in option.source_ids
                        for ref in result.sources[sid]
                    }.values()
                )
                # Identity alternatives name measurements explicitly; never invent
                # a competing experiment owner from a measurement reference.
                identity_subjects = [
                    {
                        "object_type": "REPORTED_OUTCOME",
                        "object_id": minted[canonical(option.value["same_as"])],
                    }
                    for option in field_candidates.get("measurement", [])
                    if isinstance(option.value, dict)
                    and option.value.get("same_as") in facts
                    and canonical(option.value["same_as"]) in minted
                ]
                if identity_subjects:
                    unresolved[-1]["subjects"] = list(
                        {row["object_id"]: row for row in identity_subjects}.values()
                    )
    for question in result.questions:
        if question.status != "RESOLVED":
            gap(
                question.question_id if profile.unified_expression else question.status + "-" + question.question_id,
                refs(*(facts[i] for i in question.subject_ids if i in facts))
                + [
                    ref
                    for s in result.segments
                    if s.segment_id in question.subject_ids
                    for sid in s.source_ids
                    for ref in result.sources[sid]
                ],
                "WAIT_FOR_SI" if question.status == "WAIT_FOR_SI" else "STAGE_PARTIAL",
                kind="QUESTION" if profile.unified_expression else "QUESTION_" + question.status,
            )
            if profile.unified_expression:
                public_types = {
                    **{c["compound_id"]: "COMPOUND" for c in compounds.values()},
                    **{b["reaction_block_id"]: "REACTION_BLOCK" for b in blocks},
                    **{a["account_id"]: "EXPERIMENTAL_ACCOUNT" for a in accounts},
                    **{o["outcome_id"]: "REPORTED_OUTCOME" for o in unassigned_outcomes},
                    **{
                        o["operation_id"]: "OPERATION"
                        for b in [*blocks, *accounts]
                        for o in b["operations"]
                    },
                    **{
                        o["outcome_id"]: "REPORTED_OUTCOME"
                        for b in [*blocks, *accounts]
                        for o in b["reported_outcomes"]
                    },
                }
                subjects = [
                    {"object_type": public_types[minted[fid]], "object_id": minted[fid]}
                    for fid in question.subject_ids if minted.get(fid) in public_types
                ]
                subjects = list({canonical_json_bytes(row): row for row in subjects}.values())
                unresolved[-1].update(
                    creation_key=f"{result.work_unit_id}:{question.question_id}",
                    statement="Question: " + question.question + "\nModel explanation: " + question.explanation,
                    **({"subjects": subjects} if subjects else {}),
                )
    for error in result.source_errors:
        gap(
            "INVALID_SOURCE-" + hashlib.sha256(str(error).encode()).hexdigest()[:16],
            [],
            kind="INVALID_SOURCE",
        )
    all_refs = list(
        {
            (r["observation_id"], r["source_observation_sha256"]): r
            for items in result.sources.values()
            for r in items
        }.values()
    )
    # Coverage assessments (E83): the investigator's word on subjects that are public
    # compounds or blocks, keyed by the provisional ids `rekey` will rewrite; the
    # evidence is what the assessment's sources resolved to.
    public_subject_ids = {c["compound_id"] for c in compounds.values()} | {
        b["reaction_block_id"] for b in blocks
    }
    coverage_assessments: list[dict[str, Any]] = []
    for assessment in result.experiment_assessments:
        assessed: list[str] = []
        for subject_id in assessment.subject_ids:
            subject_fact = facts.get(subject_id)
            if subject_fact is None:
                continue
            candidate = pid("CMP" if subject_fact.kind == "COMPOUND" else "RXB", subject_id)
            if candidate in public_subject_ids and candidate not in assessed:
                assessed.append(candidate)
        if not assessed:
            continue
        cited = {
            (r["observation_id"], r["source_observation_sha256"]): dict(r)
            for sid in assessment.source_ids
            for r in result.sources.get(sid, ())
        }
        coverage_assessments.append(
            {
                "subject_ids": assessed,
                "status": assessment.status,
                "source_phase": "MAIN",
                "explanation": assessment.explanation,
                "evidence_refs": list(cited.values()),
            }
        )
    provisional = RouteGroupFactProjection(
        route_group_id=route_group_id,
        compounds=tuple(compounds.values()),
        reaction_blocks=tuple(blocks),
        experimental_accounts=tuple(accounts),
        unassigned_outcomes=tuple(unassigned_outcomes),
        route={
            "route_id": route_id,
            **(
                {
                    **({"model_overview": result.overview} if result.overview else {}),
                    **({"model_integration_note": result.note} if result.note else {}),
                }
                if profile.unified_expression
                else {}
            ),
            "compound_ids": [
                c["compound_id"] for c in compounds.values() if c["compound_id"] in route_compounds
            ],
            "reaction_block_ids": [b["reaction_block_id"] for b in blocks],
            "target_paths": paths,
            "target_assertions": assertions,
            "evidence_refs": all_refs,
        },
        observation_refs=tuple(all_refs),
        unresolved_items=tuple({g["unresolved_item_id"]: g for g in unresolved}.values()),
        field_conflicts=tuple(conflicts),
        paper_narratives=tuple(narratives),
        characterization_records=tuple(characterizations),
        coverage_assessments=tuple(coverage_assessments),
    )
    renamed: dict[str, str] = {}
    public_birth_keys = {minted[fid]: key for fid, key in birth_keys.items() if fid in minted}
    for container in [*blocks, *accounts, {"reported_outcomes": unassigned_outcomes}]:
        for outcome in container["reported_outcomes"]:
            # A same_as contribution does not become the birth of its target.
            if ":CREATED:" in outcome.get("stable_key", ""):
                public_birth_keys[outcome["outcome_id"]] = outcome["stable_key"]
            else:
                public_birth_keys.pop(outcome["outcome_id"], None)
    keyed: RouteGroupFactProjection = rekey(
        provisional,
        paper_id=paper_id,
        observation_texts=observation_texts,
        characterization_texts=characterization_texts,
        furniture=furniture,
        id_map=renamed,
        creation_keys=public_birth_keys,
    )
    # One lookup, not a chain: `rekey` can mint an id spelled like one `pid` handed out,
    # and following the map twice would move a fact into another fact's object (E111).
    return replace(
        keyed,
        material_sources=rekey_material_sources(
            material_sources, renamed, [*keyed.reaction_blocks, *keyed.experimental_accounts]
        ),
        material_fact_rows={
            fid: (renamed.get(oid, oid), index) for fid, (oid, index) in material_fact_rows.items()
        },
        fusion_public_ids={
            fact_id: renamed.get(public, public)
            for fact_id, public in minted.items()
        },
        relation_destinations={
            fact_id: renamed.get(public, public)
            for fact_id, public in preparation_destinations.items()
        },
    )
