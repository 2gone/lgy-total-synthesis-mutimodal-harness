"""What a task is shown, every turn, of what it has saved and of what is still open.

The progress view used to show a task's saved items in one of two storage shapes: the
whole list while it stayed under 8,000 bytes, a directory of 20 heads (collection,
handle, location) above that. Batch 4's reading task crossed the line at its 21st item,
on its fifth turn; the heads named no operation and no aspect, so the task spent 11 of
its 22 turns paging and reading its own saved items, and finished claiming conditions
and materials it never wrote. The same shape had been hit six times before (E14, E44,
E55, E93, E108/E109, E146), each time repaired for the task kind that exposed it, and
the host's own derivations of what was missing reached a task only as a refusal at
finish, a publication report, or once before dispatch.

So each kind gives its own account, organised by the route the task was handed and
recomputed every turn: what it has saved, by the identity it would write again to
change it, and what the host can derive is still open -- by the same functions
publication applies to the sealed receipt. Nothing here refuses anything; an empty cell
says "nothing yet" and is no obligation. The view grows with the route and with the
concise meaning and identity of each saved item, never by a byte cliff. Saved entries
are not a scientific completeness checklist.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tsdaf.contracts.literature.schema import machine_profile
from tsdaf.lanes.literature.completion_guard import (
    cited_sites,
    finish_left,
)
from tsdaf.lanes.literature.enrichment_values import cited_selection_ids
from tsdaf.lanes.literature.fact_projection import is_route_step
from tsdaf.lanes.literature.gate_models import CURRENT_LOCATION
from tsdaf.lanes.literature.model_view import (
    first_step,
    passage_steps,
    round_left_view,
    route_steps,
)
from tsdaf.lanes.literature.round_handoff import paragraph_scopes, source_handoff

if TYPE_CHECKING:
    from tsdaf.lanes.literature.enrichment_values import InvestigationAnswer
    from tsdaf.lanes.literature.gate_models import FusionResult, ScientificFact
    from tsdaf.lanes.literature.middle_layer import ParagraphSet, ReadingAnswer

# What sending an item again does is said once, by each collection's declared identity
# (`item_identity`); this said it a third time.
UPDATE_RULE = (
    "Every item you have saved is named here by the identity its collection declares; "
    "an item not named here is not saved."
)
NOTHING_YET = "nothing yet"


@dataclass(frozen=True)
class ProgressState:
    """What a kind's view reads besides the running answer: the task's own inputs, fixed
    at dispatch, which the host already holds."""

    # The route as the task's sheet shows it (`container_view(...)["containers"]`).
    route: Sequence[Mapping[str, Any]] = ()
    machine: Mapping[str, Any] | None = None
    material_bindings: Mapping[str, str | None] | None = None
    # Accepted new-experiment names bound by the scientific compiler.
    object_bindings: Mapping[str, str] = field(default_factory=dict)
    # Passages the host delivered with the task: selection id -> binding.
    delivered: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Every selection this task holds, delivered or read: selection id -> binding. What
    # a citation points at is looked up here.
    bindings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # What a round's step tasks left, in the host's rows, each located where the paper's
    # own task reads it; the task is shown them through `model_view.round_left_view`.
    # Absent for any other task.
    round_left: Mapping[str, Any] | None = None
    # Actual source ranges delivered so far in this task, from its read ledger.
    read_scopes: Sequence[Mapping[str, Any]] = ()
    # The locators' delivery ledger a reading task was handed.
    ledger: Sequence[Mapping[str, Any]] = ()
    # The host's guessed passages a locator must answer (`candidate_step_sources`).
    candidates: Sequence[Mapping[str, Any]] = ()
    # The document revisions the task assesses: those it can read (`documents`).
    documents: Sequence[str] = ()
    # Explicit dispatch scope; one route step alone does not imply a block task.
    task_scope: str = "WHOLE"
    # Same application as publication, cached by the workflow's accepted snapshot.
    procedure_source_preview: Mapping[str, Any] = field(default_factory=dict)


def _step_label(route: Sequence[Mapping[str, Any]], index: int | None) -> str:
    return "paper-wide" if index is None else str(route[index].get("step") or "")


def _not_cited(answer: Any, state: ProgressState) -> list[dict[str, Any]]:
    """Delivered passages whose block no saved item cites, under the step each belongs to."""
    sites = cited_sites(
        cited_selection_ids(answer, state.bindings or state.delivered),
        state.bindings or state.delivered,
    )
    shown_under = passage_steps(state.ledger, state.route)
    groups: dict[int | None, list[str]] = {}
    for sid, binding in state.delivered.items():
        identity = binding.get("source_identity") or {}
        site = (str(identity.get("document_revision_id")), str(identity.get("source_id")))
        if site not in sites:
            groups.setdefault(shown_under.get(sid), []).append(sid)
    return [
        {"step": _step_label(state.route, index), "selection_ids": sids}
        for index, sids in sorted(groups.items(), key=lambda g: (g[0] is None, g[0] or 0))
    ]


def reading_view(answer: ReadingAnswer, state: ProgressState) -> dict[str, Any]:
    """The whole-paper reading: each operation, measurement and compound of the route
    with the findings saved about it, what no saved item cites yet, and the yield
    candidates."""
    from tsdaf.lanes.literature.container_enrichment import _readable, content_hash
    from tsdaf.lanes.literature.middle_layer import (
        READING_COVERAGE_NOTE,
        current_ruling_view,
        material_value_decision,
        measurement_bindings,
        reading_candidates,
        skeleton_materials,
        whole_condition_decisions,
    )

    Cells = dict[str, dict[str, list[dict[str, Any]]]]
    operations: Cells = {
        str(o["target_id"]): {} for step in state.route for o in step.get("operations", ())
    }
    blocks: Cells = {
        str(step.get("reaction_block_id", step.get("account_id"))): {} for step in state.route
    }
    compounds: Cells = (
        {str(c["compound_id"]): {} for c in state.machine["domain_payload"]["compounds"]}
        if state.machine is not None
        else {}
    )
    elsewhere: dict[str, list[dict[str, Any]]] = {}
    material_rows = {
        key: row["value"]
        for key, row in skeleton_materials(state.machine or {}, state.material_bindings).items()
    }
    rulings = {r.finding: r for r in answer.conflicts}
    for finding in answer.findings:
        owners = [
            state.object_bindings.get(owner, owner)
            for owner in (*finding.about_ids, *([finding.block_id] if finding.block_id else []))
        ]
        home = next(
            (
                table[owner]
                for owner in owners
                for table in (operations, blocks, compounds)
                if owner in table
            ),
            elsewhere,
        )
        saved = finding.model_dump(
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
            exclude={"selection_ids", "value_selection_ids", "about_ids"},
        )
        saved.update(scope=finding.scope, relation=finding.relation)
        publication = state.procedure_source_preview.get("finding_publication", {}).get(
            finding.finding_id
        )
        raw_only = bool(publication and publication.get("state") in {"SAVED_ONLY", "NOT_APPLIED"})
        if publication:
            saved["publication"] = {k: v for k, v in publication.items() if k != "public_targets"}
        else:
            saved["publication"] = {
                "state": "NOT_CHECKED",
                "note": "Saved reading; public application has not been checked in this view.",
            }
        if finding.aspect == "material" and finding.scope != "ANOTHER_EXPERIMENT" and not raw_only:
            position = finding.skeleton_material_id
            if position:
                saved["skeleton_material_id"] = position
            previous = material_rows.get(position or "", {})
            try:
                _, primary, identity_changed, keep = material_value_decision(
                    finding,
                    previous,
                    machine_profile(state.machine or {}),
                    rulings.get(finding.finding_id),
                    selections=state.bindings,
                )
            except ValueError as error:
                saved["compilation_problem"] = str(error)
            else:
                saved["primary_after_this_finding"] = {
                    key: _readable(primary.get(key))
                    for key in ("verbatim_name", "role", "amount", "equivalents", "compound_id")
                }
                saved["effect_of_this_finding"] = (
                    "Existing scientific values retained; supplied additions are not applied."
                    if previous and keep
                    else "New material recorded."
                    if not previous
                    else "Chemical identity replaced; omitted old values are not inherited."
                    if identity_changed
                    else "Supplied values applied; omitted values inherited."
                )
                if position:
                    material_rows[position] = primary
        home.setdefault(finding.aspect, []).append(saved)
    segments: dict[str, list[dict[str, Any]]] = {}
    for segment in answer.segments:
        segments.setdefault(segment.operation_id, []).append(
            segment.model_dump(
                mode="json", exclude={"operation_id", "selection_ids"}, exclude_none=True
            )
        )
    paired: dict[str, list[str]] = {}
    # Show accepted declarations even when a public preview is temporarily unavailable.
    # Progress is not another validator against a possibly fallback skeleton.
    for same in measurement_bindings(answer).values():
        paired.setdefault(same.outcome_id, []).append(same.finding)
    # Exact successful application is a mechanical link, not a model's judgment
    # that two independently reported measurements are scientifically identical.
    applied_outcomes = {
        finding: {t["object_id"] for t in application.get("public_targets", ())
                  if t["object_type"] == "REPORTED_OUTCOME"}
        for finding, application in state.procedure_source_preview.get("finding_publication", {}).items()
        if application.get("state") in {"APPLIED", "PARTLY_APPLIED"}
    }
    recorded = {}
    for finding, targets in applied_outcomes.items():
        for target in targets:
            recorded.setdefault(target, []).append(finding)

    def measurement_progress(target):
        return {
            "outcome_id": target,
            **({"saved_findings": recorded[target]} if target in recorded else {}),
            **({"paired_with": paired[target]} if target in paired else {}),
        }

    steps = []
    experiments = []
    for step in state.route:
        identity_field = "reaction_block_id" if "reaction_block_id" in step else "account_id"
        block_id = str(step[identity_field])
        entries = []
        for operation in step.get("operations", ()):
            operation_id = str(operation["target_id"])
            entry: dict[str, Any] = {"operation_id": operation_id}
            if operation_id in segments:
                entry["segments"] = segments[operation_id]
            entry.update(operations[operation_id])
            entries.append(entry)
        (steps if identity_field == "reaction_block_id" else experiments).append(
            {
                "step": step.get("step"),
                identity_field: block_id,
                "operations": entries,
                "measurements": [
                    measurement_progress(str(m["target_id"]))
                    for m in step.get("measurements", ())
                ],
                **blocks[block_id],
            }
        )
    return {
        "update_rule": UPDATE_RULE,
        **(
            {
                "procedure_source_preview": {
                    k: v
                    for k, v in state.procedure_source_preview.items()
                    if k not in {"finding_publication", "narrative_publication"}
                }
            }
            if state.procedure_source_preview
            else {}
        ),
        "steps": steps,
        **({"experiments": experiments} if experiments else {}),
        **({"unassigned_measurements": [
            measurement_progress(str(outcome["outcome_id"]))
            for outcome in state.machine["domain_payload"].get("unassigned_outcomes", ())
        ]} if state.machine and state.machine["domain_payload"].get("unassigned_outcomes") else {}),
        "narratives": _narrative_items(answer, state),
        "compounds": [
            {"compound_id": compound_id, **found}
            for compound_id, found in compounds.items()
            if found
        ],
        "compounds_with_nothing_yet": [c for c, found in compounds.items() if not found],
        **({"about_nothing_on_the_route": elsewhere} if elsewhere else {}),
        "rulings": {
            "names": [current_ruling_view(r, answer) for r in answer.names],
            "conflicts": [current_ruling_view(r, answer) for r in answer.conflicts],
            "two_transformations": [t.operation_id for t in answer.two_transformations],
        },
        "citation_coverage": {
            "note": READING_COVERAGE_NOTE,
            "without_saved_citation": _not_cited(answer, state),
        },
        "characterization_links": [
            {"finding": f.finding_id, "same_measurement_as": f.words.same_measurement_as}
            for f in answer.findings
            if f.aspect == "characterization" and f.words.same_measurement_as
        ],
        "condition_values_needing_review": [
            {
                "operation_id": operation_id,
                "field": field,
                "blocking": False,
                "reason": "No unique primary value; the recorded field will be retained.",
                "findings": [f.finding_id for f, _, _ in options],
            }
            for (operation_id, field), options in whole_condition_decisions(
                answer, state.machine or {}, selections=state.bindings
            ).items()
            if len({content_hash(value) for _, value, _ in options}) > 1
        ],
        "yield_candidates": reading_candidates(
            answer, state.machine or {}, applied_outcomes=applied_outcomes
        ),
    }


def paragraph_view(answer: ParagraphSet, state: ProgressState) -> dict[str, Any]:
    """Actual saved step associations, without a citation-based scientific todo.

    Empty route steps are no paragraph assignment and do not describe this task's
    remaining work; its actual remaining source ranges travel in ``still_left``.
    """
    step_of = route_steps(state.route)
    placed: dict[int | None, list[dict[str, Any]]] = {}
    for paragraph in answer.paragraphs:
        placed.setdefault(first_step(paragraph.about_ids, step_of), []).append(
            {
                "selection_ids": list(paragraph.selection_ids),
                "role": paragraph.role,
                "about_ids": list(paragraph.about_ids),
            }
        )
    steps = []
    for index, step in enumerate(state.route):
        if index not in placed:
            continue
        entry: dict[str, Any] = {
            "step": step.get("step"),
            "reaction_block_id": str(step["reaction_block_id"]),
            "paragraphs": placed[index],
        }
        steps.append(entry)
    still_left: dict[str, Any] = {}
    if state.round_left is not None:
        covered = {
            str(state.route[index]["reaction_block_id"]) for index in placed if index is not None
        }
        prior = state.round_left.get("source_handoff") or {}
        handoff = source_handoff(
            prior.get("catalog", ()),
            [*prior.get("delivered", ()), *state.read_scopes],
            [
                *prior.get("handed_off", ()),
                *paragraph_scopes(answer, state.bindings),
            ],
        )
        still_left = round_left_view(
            {
                "steps_unaccounted": [
                    row
                    for row in state.round_left.get("steps_unaccounted", ())
                    if str(row.get("reaction_block_id")) not in covered
                ],
                **handoff,
            }
        )
    return {
        "update_rule": UPDATE_RULE,
        "steps": steps,
        **(
            {"unassigned_paragraphs" if state.task_scope == "BLOCK" else "paper_wide": placed[None]}
            if None in placed
            else {}
        ),
        **({"still_left": still_left} if state.round_left is not None else {}),
        "coverage": [
            {"document_revision_id": c.document_revision_id, "status": c.status}
            for c in answer.coverage
        ],
        "limitations": [limitation.scope for limitation in answer.limitations],
    }


def _narrative_items(answer: Any, state: ProgressState) -> list[dict[str, Any]]:
    return [
            {"title": n.title, "kind": n.kind,
             **({"publication": publication} if (publication :=
                state.procedure_source_preview.get("narrative_publication", {}).get(n.item_id)
                ) else {})}
            for n in answer.narratives
        ]


def narrative_view(answer: InvestigationAnswer, state: ProgressState) -> dict[str, Any]:
    """The supplement task: its saved narratives, its scope assessments, and the
    registered documents it has not assessed yet."""
    assessed = {coverage.document_revision_id for coverage in answer.coverage}
    return {
        "update_rule": UPDATE_RULE,
        "narratives": _narrative_items(answer, state),
        "coverage": [
            {"document_revision_id": c.document_revision_id, "status": c.status}
            for c in answer.coverage
        ],
        "limitations": [limitation.scope for limitation in answer.limitations],
        "documents_not_assessed": [d for d in state.documents if d not in assessed],
    }


# How a fusion task reads what its saved items say: whole, a large result answers as a
# directory (`resource_reads.READ_BYTES`); chosen by the ids its progress lists, it
# answers with those facts, and a pointer answers with the one part it names.
FUSION_READ_RULE = (
    " What a saved fact says -- its text, values and citations -- is read at location "
    f"'{CURRENT_LOCATION}' with select "
    '{"kind": "record_items", "item_ids": [...]} naming the '
    "facts by the ids listed here; a conflicting field's candidate values at "
    f"'{CURRENT_LOCATION}#/field_conflicts'; the overview and the note at "
    f"'{CURRENT_LOCATION}#/overview' and '{CURRENT_LOCATION}#/note'."
)


def fusion_view(result: FusionResult) -> dict[str, Any]:
    """The fusion task: its saved result by the route, every fact by the id it would
    name again to change it.

    Each step is a route REACTION (`is_route_step`, the finish's definition) in saved
    order -- the order publication reads as the author's -- with its input and output
    compounds; a reaction that is context and no step is listed apart, in the same shape.
    Every other fact is named under the fact that owns it, grouped by kind and category:
    a fact that owns others in turn carries its own ``has``, and that rule is the same at
    every depth, operations included. A fact owned twice is named under both owners and
    spelled out at the first; compounds are keyed by id with their labels; what nothing
    owns is ``unowned``. What the finish would still refuse is ``still_left``, in the
    finish's own words (`finish_left`).
    """
    facts = {fact.fact_id: fact for fact in result.facts}
    owned: dict[str, list[ScientificFact]] = {}
    for fact in result.facts:
        for owner in dict.fromkeys(fact.owners):
            if owner in facts:
                owned.setdefault(owner, []).append(fact)

    def first_owner(fact: ScientificFact) -> str | None:
        if fact.kind in {"REACTION", "COMPOUND"}:
            return None
        return next((owner for owner in fact.owners if owner in facts), None)

    def home(fact: ScientificFact) -> str | None:
        # Its first owner, when following first owners up reaches a step, a compound or
        # a fact nothing owns. Nothing refuses an owner cycle; a fact whose owners only
        # own each other is listed as unowned rather than named nowhere.
        seen: set[str] = set()
        owner = first_owner(fact)
        while owner is not None:
            if owner in seen or owner == fact.fact_id:
                return None
            seen.add(owner)
            owner = first_owner(facts[owner])
        return first_owner(fact)

    def group_key(fact: ScientificFact) -> str:
        return f"{fact.kind}:{fact.category}" if fact.category else fact.kind

    expanded: set[str] = set()

    def has(owner: str) -> dict[str, list[Any]]:
        groups: dict[str, list[Any]] = {}
        for child in owned.get(owner, ()):
            groups.setdefault(group_key(child), []).append(
                node(child, at_home=home(child) == owner)
            )
        return groups

    def node(fact: ScientificFact, *, at_home: bool) -> Any:
        if not at_home or fact.fact_id in expanded or fact.fact_id not in owned:
            return fact.fact_id
        expanded.add(fact.fact_id)
        return {"id": fact.fact_id, "has": has(fact.fact_id)}

    def with_has(fact: ScientificFact, value: dict[str, Any]) -> dict[str, Any]:
        if fact.fact_id in owned and fact.fact_id not in expanded:
            expanded.add(fact.fact_id)
            value["has"] = has(fact.fact_id)
        return value

    def reaction(fact: ScientificFact) -> dict[str, Any]:
        return with_has(
            fact,
            {
                "reaction": fact.fact_id,
                **({"category": fact.category} if fact.category else {}),
                "inputs": list(fact.inputs),
                "outputs": list(fact.outputs),
            },
        )

    reactions = [fact for fact in result.facts if fact.kind == "REACTION"]
    steps = [reaction(fact) for fact in reactions if is_route_step(result, fact, facts)]
    context = [reaction(fact) for fact in reactions if not is_route_step(result, fact, facts)]
    compounds = {
        fact.fact_id: with_has(fact, {"labels": list(fact.labels)})
        for fact in result.facts
        if fact.kind == "COMPOUND"
    }
    unowned: dict[str, list[Any]] = {}
    for fact in result.facts:
        if fact.kind not in {"REACTION", "COMPOUND"} and home(fact) is None:
            unowned.setdefault(group_key(fact), []).append(node(fact, at_home=True))
    counts: dict[str, int] = {}
    for fact in result.facts:
        counts[fact.kind] = counts.get(fact.kind, 0) + 1
    not_accepted = {f.fact_id: f.status for f in result.facts if f.status != "ACCEPTED"}
    conflicting = {fid: sorted(c.candidates) for fid, c in result.conflicts.items()}
    still_left = finish_left(result)
    return {
        "update_rule": UPDATE_RULE + FUSION_READ_RULE,
        "steps": steps,
        **({"context_reactions": context} if context else {}),
        "compounds": compounds,
        **({"unowned": unowned} if unowned else {}),
        **({"not_accepted": not_accepted} if not_accepted else {}),
        **({"conflicting_fields": conflicting} if conflicting else {}),
        "counts": counts,
        "segments": [
            {"segment_id": s.segment_id, "fact_count": len(s.fact_ids)} for s in result.segments
        ],
        "questions": [{"question_id": q.question_id, "status": q.status} for q in result.questions],
        "experiment_assessments": [
            {"subject_ids": list(a.subject_ids), "status": a.status}
            for a in result.experiment_assessments
        ],
        **({"still_left": still_left} if still_left else {}),
    }
