"""What the model is shown of the host's records: one projection, in one place.

A host record carries what the host needs to verify, seal and attribute -- a source
binding with its package hash, revision and sentence range, an observation's evidence
reference, the producing task's execution id, a compound's stable key -- and what the
model needs to read, cite and place a passage: its selection id, its phase, the roles
and owners the locators gave it, and its words. Until batch 4 each place that showed the
model a record trimmed it its own way, and the reading task's paragraphs were not
trimmed at all: on the second live run their headers (128,652 chars) outweighed the
paragraphs (59,090). The rule "the host's bookkeeping is not the model's input" was a
comment (`9040191`), not a function, so each new kind of record escaped it.

Each view here is an allowlist: a field the host adds to a record later stays with the
host unless it is named here, because the model can act on it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.fact_projection import (
    compound_label,
    project_route_group_state,
    step_name,
)
from tsdaf.lanes.literature.gate_models import FusionResult

# A passage as a read reply or a delivery shows it. The location is where it can be
# read again; the rest is what it says and how it was cut.
PASSAGE_FIELDS = (
    "selection_id",
    "location",
    "origin",
    "source_phase",
    "numbered_sentences",
    "caption",
    "display",
    "figure_reading",
)
# A visual passage's "display": the geometry the model reads and acts on to pick a
# region. image_ref and mapping_ref are the host's own storage bookkeeping (each an
# ArtifactRef -- sha256, relative_path under objects/sha256/.., byte_size, media_type)
# for a derivative the model is never handed the hash of; they stay with the host
# (measured: 30 of 133 supplement-task and 19 of 381 detail-task requests carried them
# -- batch 2A, step 2d).
DISPLAY_FIELDS = ("bbox_px", "dimensions", "source_frame")
# A compound as a sheet shows it: the id the model writes about, what the author calls
# it, and where it sits in the route. Its stable key, key basis and structure state are
# the host's.
COMPOUND_FIELDS = (
    "compound_id",
    "author_labels",
    "names",
    "predecessor_ids",
    "successor_ids",
    "source_statements",
    "model_interpretations",
)


# A registered document: which one, its phase, and whether it can be read. Its package
# hash is the host's integrity check.
DOCUMENT_FIELDS = (
    "attachment_id",
    "document_revision_id",
    "source_phase",
    "active",
    "failure",
    "unavailable_reason",
)
# A catalog entry: where it reads, what it is, how it may be read. A figure's asset hash
# is the host's.
CATALOG_ENTRY_FIELDS = {
    "text": (
        "source_id",
        "page",
        "section_path",
        "sentence_count",
        "preview",
        "location",
        "selectors",
    ),
    "visuals": ("source_id", "page", "caption", "location", "selectors", "unavailable"),
}
# An observation the route rests on: which passage it came from, what it was read as,
# and where to read that passage. Its id, anchor hashes and payload are the host's; a
# citation is minted on the read path, never out of an observation record (E108). The
# passage is named by the location this task reads it at, never by its package's block
# id: a4's paper task read one such id bare and with `source:`, refused both times.
OBSERVATION_FIELDS = (
    "document_revision_id",
    "source_phase",
    "kind",
    "semantic_role",
    "location",
)


# The paper task's actionable remainder (`literature_composition.round_left`): each
# source range names a place to read. Execution ids and source bindings stay in the
# host ledger; accepted ranges are offered separately through a versioned read.
ROUND_LEFT_FIELDS = {
    "steps_unaccounted": ("reaction_block_id", "step", "reason", "failure_code"),
    "remaining_sources": (
        "document_revision_id",
        "location",
        "start_sentence",
        "end_sentence",
        "status",
        "head",
    ),
}


# The router's sealed decision as a task after it is shown it: the kind of article, its
# route groups with their core blocks and figures, which blocks it selected in what
# role, and what it decided each figure is. Its package hash, execution id and model
# provenance are the host's.
ROUTING_FIELDS = ("article_type", "route_groups", "coarse_text_selections", "visual_decisions")
ROUTE_GROUP_FIELDS = (
    "route_group_id",
    "synthesis_type",
    "target_class",
    "target_names",
    "core_text_block_ids",
    "ordered_visual_ids",
)
VISUAL_DECISION_FIELDS = ("visual_id", "final_decision")

# The fusion result's scientific account and source keys are readable at main:<i>.
# Its verification refs, package hash and source-key bindings belong to the Host.
GROUP_RESULT_FIELDS = (
    "overview",
    "segments",
    "facts",
    "questions",
    "experiment_assessments",
    "conflicts",
    "source_errors",
    "note",
)
FIGURE_SOURCE_FIELDS = ("dimensions", "native_caption", "group_caption", "label_candidates")


def _pick(record: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {key: record[key] for key in fields if key in record}


def document_view(document: Mapping[str, Any], *, media: Sequence[str] = ()) -> dict[str, Any]:
    """One registered document as the model is shown it, with the catalog entries of
    each medium in ``media``."""
    view = _pick(document, DOCUMENT_FIELDS)
    for medium in media:
        view[medium] = [
            _pick(entry, CATALOG_ENTRY_FIELDS[medium]) for entry in document.get(medium) or ()
        ]
    return view


def _placed(row: Mapping[str, Any], fields: Sequence[str]) -> bool:
    """Whether the model can act on a row: one that names a place in a source does only
    where this task can read it. A row with no location names a block this task cannot
    read, and stays with the host."""
    return "location" not in fields or "location" in row


def round_left_view(round_left: Mapping[str, Any]) -> dict[str, Any]:
    """The same compact handoff for the paper task's first sheet and progress."""
    view: dict[str, Any] = {}
    for key, fields in ROUND_LEFT_FIELDS.items():
        if key not in round_left:
            continue
        rows: list[dict[str, Any]] = []
        for row in round_left[key] or ():
            picked = _pick(row, fields)
            if _placed(row, fields) and picked not in rows:
                rows.append(picked)
        view[key] = rows
    if "handled_sources" in round_left:
        view["handled_sources"] = {
            "count": len(round_left["handled_sources"] or ()),
            "location": "handled",
        }
    return view


def observation_views(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The observations as the model is shown them: each one this task can read the
    passage of."""
    return [
        _pick(observation, OBSERVATION_FIELDS)
        for observation in observations
        if _placed(observation, OBSERVATION_FIELDS)
    ]


def passage_view(passage: Mapping[str, Any]) -> dict[str, Any]:
    """One passage as the model is shown it, its display trimmed to the geometry the
    model acts on -- the host's storage refs stay with the host (batch 2A, 2d)."""
    view = _pick(passage, PASSAGE_FIELDS)
    if "display" in view:
        view["display"] = _pick(view["display"], DISPLAY_FIELDS)
    return view


def compound_view(compound: Mapping[str, Any]) -> dict[str, Any]:
    """One compound record as the model is shown it."""
    view = _pick(compound, COMPOUND_FIELDS)
    def expression(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: expression(item) for key, item in value.items()
                    if key in {"statement", "model_interpretations", "text", "relation", "primary",
                               "state", "value", "reason_code", "candidates"}}
        if isinstance(value, list):
            return [expression(item) for item in value]
        return value
    for field in ("source_statements", "model_interpretations"):
        if field in view:
            view[field] = expression(view[field])
    return view


DetailContextPurpose = Literal["READING", "RECORDING", "FINAL_MAPPING"]


def detail_context_view(
    value: Mapping[str, Any], purpose: DetailContextPurpose = "READING"
) -> dict[str, Any]:
    """Separate prior investigation metadata from the new task's inputs.

    Applies only to a group/public-root view, never recursively to saved scientific
    expressions. Sealed records and the publication machine remain unchanged.
    """
    excluded = set()
    # Recording retains prior scientific assessments as historical context. A
    # field name cannot tell us that the model's conclusion is an obsolete task.
    # The legacy typed final mapper keeps its historical input policy; the new
    # whole-document mapper has its own faithful scientific view.
    if purpose == "FINAL_MAPPING":
        excluded.update(("experiment_assessments", "coverage_assessments",
                         "overview", "note", "questions", "shared_question_ids",
                         "unmapped_question_ids", "source_errors", "conflicts",
                         "unresolved_items", "blocking_conflicts"))
    return {key: item for key, item in value.items() if key not in excluded}


def group_result_view(
    result: Mapping[str, Any], *, purpose: DetailContextPurpose = "READING"
) -> dict[str, Any]:
    """One readable group record, shared by whole, pointer and selected reads."""
    view = detail_context_view(_pick(result, GROUP_RESULT_FIELDS), purpose)
    if "sources" in result:
        view["sources"] = list(result["sources"])
    if "conflicts" in view:
        # This consumer has no authorized submission resolver. Private provenance
        # remains in the sealed result; only scientific candidate data is offered.
        view["conflicts"] = {
            fid: {**conflict, "candidates": {
                field: [{key: value for key, value in candidate.items() if key != "origins"}
                        for candidate in candidates]
                for field, candidates in conflict.get("candidates", {}).items()
            }}
            for fid, conflict in view["conflicts"].items()
        }
    return view


def figure_source_view(source: Mapping[str, Any]) -> dict[str, Any]:
    """The author-facing source card beside the original figure image."""
    return _pick(source, FIGURE_SOURCE_FIELDS)


def route_steps(route: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Each route object's step, by its position in the route: the step itself, its
    operations and measurements, and the compounds it makes."""
    step_of: dict[str, int] = {}
    for index, step in enumerate(route):
        owned = [
            step.get("reaction_block_id", step.get("account_id")),
            *(operation["target_id"] for operation in step.get("operations", ())),
            *(measurement["target_id"] for measurement in step.get("measurements", ())),
            *((step.get("known") or {}).get("output_compound_ids") or ()),
        ]
        for object_id in owned:
            step_of.setdefault(str(object_id), index)
    return step_of


def first_step(owners: Sequence[str], step_of: Mapping[str, int]) -> int | None:
    """The first route step any of ``owners`` belongs to; None when none does. A passage
    is shown under it, and the progress view counts it there."""
    steps = [step_of[owner] for owner in owners if owner in step_of]
    return min(steps) if steps else None


def passage_steps(
    ledger: Sequence[Mapping[str, Any]], route: Sequence[Mapping[str, Any]]
) -> dict[str, int | None]:
    """The route step each delivered passage is shown under: the first step any object
    the locators said it concerns belongs to, None when none does. One answer for the
    delivery, the progress view and the receipt's read scopes."""
    owners: dict[str, list[str]] = {}
    for entry in ledger:
        for sid in entry["selection_ids"]:
            known = owners.setdefault(sid, [])
            known.extend(o for o in entry.get("about_ids") or () if o not in known)
    step_of = route_steps(route)
    return {sid: first_step(about, step_of) for sid, about in owners.items()}


def delivered_paragraphs(
    ledger: Sequence[Mapping[str, Any]],
    selections: Mapping[str, Mapping[str, Any]],
    route: Sequence[Mapping[str, Any]],
    *,
    references: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[tuple[str | None, str]]:
    """The located paragraphs as the reading task is shown them.

    ``ledger`` is every locator's delivery, one entry per paragraph it located; the same
    passage located by two tasks appears in both, under one content-derived selection id
    (E108 ①). Each passage is shown once, under the first route step it concerns, headed
    by its id, its phase, every role the locators gave it and every object they said it
    concerns. A passage no route object claims is shown after the steps, as paper-wide.

    Returns ``(selection_id, text)`` parts in delivery order; a step's heading is a part
    of its own, with no selection id.
    """
    references = references or {}
    attributions: dict[str, list[dict[str, Any]]] = {}
    for entry in ledger:
        attribution = {
            key: entry[key] for key in ("role", "about_ids", "explanation") if key in entry
        }
        unit_sources: dict[str, list[str]] = {}
        for sid in entry["selection_ids"]:
            if sid in references:
                unit = str(references[sid]["location"])
                locations = unit_sources.setdefault(unit, [])
                location = str(references[sid]["locator_source"])
                if location not in locations:
                    locations.append(location)
        for unit, locations in unit_sources.items():
            linked = {**attribution, "source_locations": locations}
            items = attributions.setdefault(unit, [])
            if linked not in items:
                items.append(linked)
    shown_references: set[str] = set()
    roles: dict[str, list[str]] = {}
    about: dict[str, list[str]] = {}
    for entry in ledger:
        for sid in entry["selection_ids"]:
            if entry["role"] not in roles.setdefault(sid, []):
                roles[sid].append(entry["role"])
            owners = about.setdefault(sid, [])
            owners.extend(o for o in entry.get("about_ids") or () if o not in owners)
    shown_under = passage_steps(ledger, route)
    groups: dict[int | None, list[str]] = {}
    for sid in roles:
        groups.setdefault(shown_under[sid], []).append(sid)
    parts: list[tuple[str | None, str]] = []
    for group in sorted(groups, key=lambda i: (i is None, i if i is not None else 0)):
        if group is None:
            heading = "PAPER-WIDE (no route step named)"
        else:
            step = route[group]
            # The step by its name alone: the loop shows it through the task's names.
            heading = f"STEP {group + 1}: {step['reaction_block_id']}"
        parts.append((None, heading))
        for sid in groups[group]:
            if sid in references:
                reference = references[sid]
                unit = str(reference["location"])
                if unit not in shown_references:
                    shown_references.add(unit)
                    parts.append(
                        (
                            None,
                            canonical_json_bytes(
                                {
                                    "technical_attachment": {
                                        k: v for k, v in reference.items() if k != "locator_source"
                                    },
                                    "upstream_attributions": attributions[unit],
                                }
                            ).decode(),
                        )
                    )
                continue
            binding = selections[sid]
            text = binding.get("exact_text") or binding.get("figure_reading") or ""
            if not isinstance(text, str):
                text = canonical_json_bytes(text).decode()
            header = " · ".join(
                piece
                for piece in (
                    f"[{sid}]",
                    str(binding.get("source_phase") or ""),
                    ", ".join(roles[sid]),
                    # A name can hold a comma (step 9a, 9b -> 11a); names are
                    # separated by semicolons.
                    "about " + "; ".join(about[sid]) if about[sid] else "",
                )
                if piece
            )
            parts.append((sid, f"{header}\n{text}"))
    return parts


def routing_view(routing: Mapping[str, Any]) -> dict[str, Any]:
    """The router's sealed decision as a later task is shown it."""
    view = _pick(routing, ROUTING_FIELDS)
    view["route_groups"] = [_pick(g, ROUTE_GROUP_FIELDS) for g in view.get("route_groups", ())]
    view["visual_decisions"] = [
        _pick(d, VISUAL_DECISION_FIELDS) for d in view.get("visual_decisions", ())
    ]
    return view


def route_result_view(
    result: FusionResult, *, profile: MachineProfile = MachineProfile.V1,
    source_selections: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Format the same interpreted state publication uses, including partial routes.

    This summary precedes publication. It names no private/public ids and never picks
    the stale head value of a conflicting endpoint or an alias's partial identity.
    """
    projected = project_route_group_state(
        route_group_id="reading", paper_id="reading", result=result, profile=profile,
        source_selections=source_selections,
    )
    labels = {c["compound_id"]: compound_label(c) or "?" for c in projected.compounds}

    def names(ids: Sequence[str]) -> list[str]:
        return [labels.get(identity, "?") for identity in ids]

    steps = []
    for block in projected.reaction_blocks:
        ends = {
            key: names(block[field]["value"] or ())
            for key, field in (("from", "input_compound_ids"), ("to", "output_compound_ids"))
        }
        step: dict[str, Any] = {
            "step": step_name([", ".join(ends[key]) or "?" for key in ("from", "to")]),
            **ends,
        }
        unresolved = {
            key: [names(candidate["value"] or ()) for candidate in block[field]["candidates"]]
            for key, field in (("from", "input_compound_ids"), ("to", "output_compound_ids"))
            if block[field]["state"] != "PRESENT"
        }
        if unresolved:
            step["unresolved"] = unresolved
        steps.append(step)
    from tsdaf.lanes.literature.container_enrichment import _scientific_view

    return {
        "overview": result.overview,
        "target": names([path["target_compound_id"] for path in projected.route["target_paths"]]),
        "steps": steps,
        **({"experiments": [
            {"title": account["title"],
             "description": _scientific_view(account.get("model_description"), None),
             "operations": [
                 {"description": _scientific_view(operation.get("model_description"), None)}
                 for operation in account["operations"]
             ]}
            for account in projected.experimental_accounts
        ]} if projected.experimental_accounts else {}),
        **({"unassigned_measurements": [
            {key: _scientific_view(outcome.get(key), None)
             for key in ("kind", "reported_value", "source_statements", "model_interpretations")}
            for outcome in projected.unassigned_outcomes
        ]} if projected.unassigned_outcomes else {}),
    }
