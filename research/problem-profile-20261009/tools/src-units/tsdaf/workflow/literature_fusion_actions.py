"""Host-bound read locations and sparse writes for both fusion consumers."""

from __future__ import annotations

import functools
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, get_origin

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_write import item_diagnostic, scalar_text, write_shape
from tsdaf.lanes.literature.author_words import find_quote, matching_text
from tsdaf.lanes.literature.characterization_methods import (
    CHARACTERIZATION_CATEGORY,
    validate_characterization_statement,
)
from tsdaf.lanes.literature.citation_policy import INHERITED_CITATION_POLICY, SOURCE_CITATION_POLICY
from tsdaf.lanes.literature.enrichment_models import MainExperimentAssessment
from tsdaf.lanes.literature.evidence_binding import unit_tag
from tsdaf.lanes.literature.fusion_state import (
    StaleFactChange,
    apply_proposal,
    fact_references,
    fold_verified_fact_commands,
    linked_change_batches,
    validate_read_versions,
)
from tsdaf.lanes.literature.gate_models import (
    CURRENT_LOCATION,
    FUSION_WRITE_REFERENCES,
    FUSION_WRITE_SCALARS,
    VIEW_RULE,
    FactChange,
    FactChangeGroup,
    FactOrigin,
    FusionProposal,
    FusionQuestion,
    FusionResult,
    InlineFactChangeGroup,
    RouteSegment,
    ViewControl,
    fusion_write_collections,
    read_fusion_result,
)
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.item_identity import Identity, identity_of
from tsdaf.lanes.literature.model_names import UnknownReference
from tsdaf.lanes.literature.skeleton_meanings import requires_author_words
from tsdaf.workflow.action_progress import ACTION_PROGRESS_ORIGIN
from tsdaf.workflow.write_registry import WriteRegistry, WriteTarget


def fusion_write_registry(
    base: FusionResult, locations: dict[str, dict[str, Any]],
    *, unified_expression: bool = False,
) -> WriteRegistry:
    """The collections a fusion write changes, and how each item names its subject.

    The names are the registered table's; only a fact or a linked group needs the
    executor to name it, because a fact is addressed through a read location. Every
    item is written and reported on its own. No data is accepted here.
    """
    models = fusion_write_collections(unified_expression=unified_expression)
    change_model = models["changes"]
    bound_name = name_binder(base, locations)

    def fact_id(value: Any) -> str | None:
        token = value.get("fact_id") if isinstance(value, dict) else None
        if not isinstance(token, str) or not token:
            return None
        if token in locations:
            return locations[token].get("fact_id")
        return bound_name(token)

    def change_target(value: Any) -> str | None:
        identity = fact_id(value)
        if identity is None or not isinstance(value, dict):
            return None
        fields = set(value) & change_model.model_fields.keys() - {"fact_id", "clear"}
        # Append and replacement are different intents. Defaults cannot repair a
        # rejected mutation; retain other supplied fields even when their values
        # are invalid so the failed intent cannot disappear during validation.
        if value.get("add_source_ids") == []:
            fields.discard("add_source_ids")
        if value.get("withdraw") is False:
            fields.discard("withdraw")
        cleared = value.get("clear", [])
        if isinstance(cleared, list):
            fields.update(
                k for k in cleared if isinstance(k, str) and k in change_model.model_fields
            )
        return canonical_json_bytes({"subjects": [identity], "fields": sorted(fields)}).decode()

    def group_target(value: Any) -> str | None:
        children = value.get("changes") if isinstance(value, dict) else None
        ids = [change_target(child) for child in children] if isinstance(children, list) else []
        if not ids or not all(ids):
            return None
        parts = [json.loads(i) for i in ids if i is not None]
        return canonical_json_bytes(
            {
                "subjects": sorted(v["subjects"][0] for v in parts),
                "fields": sorted(
                    v["subjects"][0] + ":" + name for v in parts for name in v["fields"]
                ),
            }
        ).decode()

    def named(collection: str) -> Callable[[Any], str | None]:
        """What an item names by the field its identity is (`item_identity`), each name
        bound as this task binds it; a list the identity takes in any order is sorted."""
        identity = METADATA_IDENTITY[collection]
        (field,) = identity.keys
        declared = METADATA_MODELS[collection].model_fields[field].annotation
        listed = get_origin(declared) is tuple

        def target(value: Any) -> str | None:
            token = value.get(field) if isinstance(value, dict) else None
            if not listed:
                return bound_name(token) if isinstance(token, str) and token else None
            if not isinstance(token, list) or not token:
                return None
            if any(not isinstance(i, str) or not i for i in token):
                return None
            names = [bound_name(i) for i in token]
            return canonical_json_bytes(
                sorted(names) if field in identity.unordered else names
            ).decode()

        return target

    identities: dict[str, Callable[[Any], str | None]] = {
        "changes": change_target,
        "groups": group_target,
        **{key: named(key) for key in METADATA_MODELS},
    }
    targets = [
        WriteTarget(name, "per_item", identities[name]) for name in models
    ]
    targets.extend(
        WriteTarget(name, "envelope", _constant(name), (name,)) for name, _ in FUSION_WRITE_SCALARS
    )
    return WriteRegistry(targets)


def _constant(name: str) -> Callable[[Any], str | None]:
    return lambda raw: name


_REFERENCE_FIELDS = ("fact_id", "same_as", "owners", "inputs", "outputs", "after")


def fold_fact_prefix(raw: dict[str, Any]) -> dict[str, Any]:
    """A ``fact:`` prefix on a name means nothing but the name; it is folded, not refused.

    The host never mints a ``fact:`` handle (read locations are ``seen:``, ``source:``,
    ``state:`` ...), yet a live model wrote every new fact and every reference as
    ``fact:rxn_1`` and lost sixty rows to "unknown read location" (kalmanol, step 6
    close-out). The one place that reads a model's tokens folds it, so the repair
    targets and the executor see the same names.
    """

    def name(value: Any) -> Any:
        return value[5:] if isinstance(value, str) and value.startswith("fact:") else value

    def change(item: Any) -> Any:
        if not isinstance(item, dict):
            return item
        folded = dict(item)
        for field in _REFERENCE_FIELDS:
            value = folded.get(field)
            if isinstance(value, list):
                folded[field] = [name(v) for v in value]
            elif isinstance(value, str):
                folded[field] = name(value)
        measurement = folded.get("measurement")
        if isinstance(measurement, dict) and isinstance(measurement.get("same_as"), str):
            folded["measurement"] = {**measurement, "same_as": name(measurement["same_as"])}
        return folded

    folded_raw = dict(raw)
    if isinstance(raw.get("changes"), list):
        folded_raw["changes"] = [change(item) for item in raw["changes"]]
    if isinstance(raw.get("groups"), list):
        groups = []
        for group in raw["groups"]:
            if isinstance(group, dict):
                group = dict(group)
                if isinstance(group.get("changes"), list):
                    group["changes"] = [change(item) for item in group["changes"]]
                if isinstance(group.get("fact_ids"), list):
                    group["fact_ids"] = [name(v) for v in group["fact_ids"]]
            groups.append(group)
        folded_raw["groups"] = groups
    return folded_raw


def submission_targets(
    raw: dict[str, Any], base: FusionResult, locations: dict[str, dict[str, Any]],
    *, unified_expression: bool = False,
) -> dict[str, str | None]:
    """Identify repair targets without accepting data or changing scientific identities."""
    return fusion_write_registry(
        base, locations, unified_expression=unified_expression
    ).item_targets(fold_fact_prefix(raw))


def diagnostic(
    error: ValueError,
    *,
    model: type[ContractModel] | None = None,
    collection: str | None = None,
) -> object:
    """One row's diagnostic: the item renderer's, plus what a stale change saw and sees."""
    if isinstance(error, StaleFactChange):
        return {
            "message": str(error),
            "subject": error.fact_id,
            "seen": error.seen,
            "current": error.current,
        }
    return item_diagnostic(error, model=model, collection=collection)


def remember_location(locations: dict[str, dict[str, Any]], **binding: Any) -> str:
    handle = "seen:" + hashlib.sha256(canonical_json_bytes(binding)).hexdigest()[:32]
    locations[handle] = binding
    return handle


def result_view(
    result: FusionResult,
    ref: ArtifactRef,
    locations: dict[str, dict[str, Any]],
    *,
    historical: bool = False,
    fact_ids: set[str] | None = None,
    include_metadata: bool = False,
) -> dict[str, Any]:
    visible = {f.fact_id for f in result.facts}
    while True:
        expanded = visible | {
            r
            for f in result.facts
            if f.fact_id in visible
            for r in (
                *f.owners,
                *f.inputs,
                *f.outputs,
                *f.after,
                *((f.same_as,) if f.same_as else ()),
            )
        }
        if expanded == visible:
            break
        visible = expanded
    kind = "HISTORICAL_RESULT" if historical else "CURRENT_RESULT"
    view = remember_location(locations, kind=kind, result_ref=ref.model_dump(mode="json"))
    facts = [
        f.model_dump(mode="json")
        for f in result.facts
        if f.fact_id in visible and (fact_ids is None or f.fact_id in fact_ids)
    ]
    fact_locations = {
        f["fact_id"]: remember_location(
            locations,
            kind=kind,
            result_ref=ref.model_dump(mode="json"),
            fact_id=f["fact_id"],
            writable=not historical,
        )
        for f in facts
    }
    output = {"kind": kind, "view_location": view, "fact_locations": fact_locations, "facts": facts}
    field_conflicts = {
        fid: {
            field: [candidate.model_dump(mode="json", exclude={"origins"})
                    for candidate in candidates]
            for field, candidates in conflict.candidates.items()
        }
        for fid, conflict in result.conflicts.items()
        if fid in fact_locations
    }
    if field_conflicts:
        output["field_conflicts"] = field_conflicts
    if include_metadata:
        output.update(
            overview=result.overview,
            segments=[s.model_dump(mode="json") for s in result.segments],
            questions=[q.model_dump(mode="json") for q in result.questions],
            experiment_assessments=[
                a.model_dump(mode="json") for a in result.experiment_assessments
            ],
            note=result.note,
        )
    return output


def inherited_references(value: Any) -> set[str]:
    """References accompanying actual accepted findings, never navigation alone."""
    from tsdaf.lanes.literature.source_parts import fact_source_ids

    if isinstance(value, list):
        return set().union(*(inherited_references(item) for item in value))
    if (
        not isinstance(value, dict)
        or value.get("outcome") in {"DIRECTORY", "REJECTED"}
        or value.get("origin") == ACTION_PROGRESS_ORIGIN
    ):
        return set()
    own = (
        set(fact_source_ids(value))
        if any(value.get(key) for key in ("text", "labels", "rationale"))
        else set()
    )
    return own | set().union(*(inherited_references(item) for item in value.values()))


def name_binder(
    base: FusionResult, locations: Mapping[str, Mapping[str, Any]] | None = None,
) -> Callable[[str], str]:
    """What a name the model wrote means in this task.

    It is an id this result already binds, or a local name of this work unit. The write
    ledger identifies items by the same rule, so a rejected item and its corrected resend
    name one subject whether the model spelled it locally or as the bound id a reply showed.
    Since batch 2A (2d) the model is shown the local name (`fusion_names`), so the local
    spelling is the one it writes.
    """
    prefix, bound = binder_spec(base)
    local_name = binder_from(prefix, bound)

    def located(token: str) -> str:
        binding = locations.get(token) if locations is not None else None
        fact_id = binding.get("fact_id") if isinstance(binding, Mapping) else None
        return fact_id if isinstance(fact_id, str) else local_name(token)

    return located


def binder_spec(base: FusionResult) -> tuple[str, set[str]]:
    """The existing local-name authority as plain data for a request snapshot."""
    return unit_tag(base.work_unit_id), (
        {f.fact_id for f in base.facts}
        | {s.segment_id for s in base.segments}
        | {q.question_id for q in base.questions}
    )


def binder_from(prefix: str, bound: set[str]) -> Callable[[str], str]:
    """Restore the same local-name binding from its sealed prefix and bound ids."""

    def bound_name(token: str) -> str:
        if token.startswith(prefix) or token in bound:
            return token
        return prefix + token

    return bound_name


def bind_fact_change_names(
    data: dict[str, Any], *, subject: Callable[[str], str],
    reference: Callable[[str], str], source: Callable[[str], str] | None = None,
) -> None:
    """Bind only declared identity slots in place, preserving scientific words/presence.

    Fusion location resolution, group-local binding and Reading's public-object
    binding share this mechanical walk. Callers retain their own authority checks.
    """
    def named(value: Any, binding: Callable[[str], str]) -> Any:
        return binding(value) if isinstance(value, str) else value

    if isinstance(data.get("fact_id"), str):
        data["fact_id"] = subject(data["fact_id"])
    for key in ("owners", "inputs", "outputs", "after"):
        if isinstance(data.get(key), (list, tuple)):
            data[key] = [named(value, reference) for value in data[key]]
    if isinstance(data.get("same_as"), str):
        data["same_as"] = reference(data["same_as"])
    measurement = data.get("measurement")
    if isinstance(measurement, dict) and isinstance(measurement.get("same_as"), str):
        measurement["same_as"] = reference(measurement["same_as"])
    if source is not None:
        for key in ("source_ids", "add_source_ids"):
            if isinstance(data.get(key), (list, tuple)):
                data[key] = [named(value, source) for value in data[key]]
        for part in data.get("source_parts", ()) or ():
            if isinstance(part, dict) and isinstance(part.get("selection_id"), str):
                part["selection_id"] = source(part["selection_id"])


def bind_local_names(
    base: FusionResult,
    proposal: FusionProposal,
    locations: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    bind_group_locations: bool = False,
) -> FusionProposal:
    """Bind names once across the whole submission, including atomic groups.

    A fact change names its subject through a read location and is resolved later,
    with the location's version checked (``normalize``). A segment, question or
    assessment names facts too, and the prompt tells the model it may use the same
    read locations for them; those are resolved here to the fact the location shows,
    so a segment written from a view is not refused as "expanding authority" for
    naming its own facts the way the view named them (E54).
    """
    mapping = name_binder(base, locations)
    group_name = mapping if bind_group_locations else name_binder(base)

    data = proposal.model_dump(exclude_unset=True)
    changes = [*data.get("changes", []), *(c for g in data.get("groups", []) for c in g["changes"])]
    metadata = [
        item
        for key in ("segments", "questions", "experiment_assessments")
        for item in data.get(key, [])
    ]
    for item in changes:
        bind_fact_change_names(item, subject=mapping, reference=mapping)
    for group in data.get("groups", []):
        if group.get("fact_ids"):
            group["fact_ids"] = [group_name(value) for value in group["fact_ids"]]
    for item in metadata:
        for key in ("segment_id", "question_id"):
            if item.get(key) is not None:
                item[key] = mapping(item[key])
        for key in ("subject_ids", "fact_ids"):
            if item.get(key) is not None:
                item[key] = [mapping(value) for value in item[key]]
    return FusionProposal.model_validate_json(canonical_json_bytes(data))


METADATA_MODELS: dict[str, type[ContractModel]] = {
    "segments": RouteSegment,
    "questions": FusionQuestion,
    "experiment_assessments": MainExperimentAssessment,
}
# Read from the one table: this was a table of its own, and compared an assessment's
# subject_ids in the order written, where the registry above and the state sort them.
METADATA_IDENTITY: dict[str, Identity] = {
    key: identity_of(model) for key, model in METADATA_MODELS.items()
}


def _identified(identity: Identity, item: object) -> str:
    """The identity an item names, as a refusal says it."""
    return ", ".join(str(getattr(item, name)) for name in identity.keys)


def compared_version(
    view: str | None,
    *,
    locations: Mapping[str, Mapping[str, Any]],
    store: PrivateArtifactStore,
    last_shown: FusionResult,
) -> FusionResult:
    """The version `VIEW_RULE` names: the one reading of a write's view.

    The result's own name and the whole-result handles a read or a write registered name
    versions; nothing else does. The fact changes and the metadata of one write used to
    read the same view twice, and differently: a view naming no version fell back to the
    version last shown for a new fact and was refused for the overview beside it.
    """
    if view is None or view == CURRENT_LOCATION:
        return last_shown
    binding = locations.get(view, {})
    if binding.get("kind") != "CURRENT_RESULT" or "fact_id" in binding:
        raise ValueError(f"view {view} names no version of this result. {VIEW_RULE}")
    ref = ArtifactRef.model_validate(binding["result_ref"])
    store.verify(ref)
    return read_fusion_result(store.read_bytes(ref))


def validate_changed_fact_words(
    base: FusionResult, updated: FusionResult, changes: Sequence[FactChange],
    delivered_text: Mapping[str, Mapping[str, Any]] | None,
) -> None:
    """The existing author-word rule over the reducer's actual result."""
    from tsdaf.lanes.literature.source_parts import selected_source_part

    for change in changes:
        if "source_parts" not in change.model_fields_set:
            continue
        for index, part in enumerate(getattr(change, "source_parts", ()) or ()):
            selected_source_part(part, delivered_text or {},
                                 field_path=f"{change.fact_id}.source_parts[{index}]")
    if delivered_text is not None:
        quote_fields = {"kind", "category", "text", "source_ids", "add_source_ids"}
        by_id = {fact.fact_id: fact for fact in updated.facts}
        before = {fact.fact_id: fact for fact in base.facts}
        for change in changes:
            fact = by_id[change.fact_id]
            prior_fact = before.get(change.fact_id)
            if (
                not requires_author_words(fact.kind, fact.category)
                or fact.status == "REJECTED"
                or not fact.text
                or not updated.fields_known(fact, "kind", "category", "text", "source_ids")
                or (
                    prior_fact is not None
                    and prior_fact.status != "REJECTED"
                    and not quote_fields.intersection(
                        change.model_fields_set | set(change.clear)
                    )
                )
            ):
                continue
            cited = fact.source_ids
            # A visual or an inherited citation may support the words even
            # when the text read here does not. Absence is not disproof.
            if not cited or not all(
                sid in delivered_text and matching_text(delivered_text[sid]) is not None
                for sid in cited
            ):
                continue
            quoted = {sid: delivered_text[sid] for sid in cited}
            if find_quote(fact.text, cited, quoted) is None:
                places = ", ".join(
                    dict.fromkeys(str(v["location"]) for v in quoted.values())
                )
                raise ValueError(
                    f"{fact.fact_id}.text is not verbatim in its cited, delivered TEXT. "
                    f"Read the author's wording at {places}; revise text or its citations."
                )


def validate_characterization_change(
    change: FactChange,
    *,
    kind: str | None,
    category: str | None,
    profile: MachineProfile,
    complete_statement: bool,
) -> None:
    validate_characterization_words(
        kind=kind,
        category=category,
        text=change.text,
        profile=profile,
        complete_statement=complete_statement,
    )


def validate_characterization_words(
    *,
    kind: str | None,
    category: str | None,
    text: str | None,
    profile: MachineProfile,
    complete_statement: bool,
) -> None:
    if kind == "NOTE" and category == CHARACTERIZATION_CATEGORY and text is not None:
        validate_characterization_statement(
            text,
            profile=profile,
            complete_statement=complete_statement or profile.expression,
        )


def apply_bound_fact_changes(
    base: FusionResult, changes: Sequence[FactChange], *,
    origins: Mapping[str, FactOrigin], proven_births: Mapping[str, FactOrigin],
    delivered_text: Mapping[str, Mapping[str, Any]] | None,
    machine_profile: MachineProfile, complete_characterization: bool = False,
) -> FusionResult:
    """Fold one previously accepted, identity-bound atomic batch.

    Caller must establish the original acceptance and binding before using this
    seam. It does not authorize a new write or pretend a counterfactual state is
    the model's old read version. Scientific mutation and result checks remain
    the same ones used by ordinary writes.
    """
    before = {fact.fact_id: fact for fact in base.facts}
    for change in changes:
        prior = before.get(change.fact_id)
        validate_characterization_change(
            change, kind=change.kind or (prior.kind if prior else None),
            category=change.category or (prior.category if prior else None),
            profile=machine_profile, complete_statement=complete_characterization,
        )
    updated = apply_proposal(
        base, FusionProposal(changes=tuple(changes)),
        origins=origins, proven_births=proven_births,
    )
    validate_changed_fact_words(base, updated, changes, delivered_text)
    return updated


def fold_bound_fact_commands(
    base: FusionResult,
    commands: Sequence[tuple[Sequence[FactChange], Mapping[str, FactOrigin]]],
    *,
    proven_births: Mapping[str, FactOrigin],
    delivered_texts: Sequence[Mapping[str, Mapping[str, Any]]],
    machine_profile: MachineProfile,
    complete_characterization: bool = False,
) -> FusionResult:
    """One complete counterfactual candidate, from already proved surviving writes.

    Original acceptance stays separate. Only the final surviving wording is
    checked with its final citations, drawn from surviving authorized TEXT; an
    overwritten historical sentence is not mistaken for the final statement.
    """
    texts: dict[str, Mapping[str, Any]] = {}
    for source_texts in delivered_texts:
        for sid, value in source_texts.items():
            if sid in texts and canonical_json_bytes(texts[sid]) != canonical_json_bytes(value):
                raise ValueError("surviving source text differs from its sealed identity")
            texts[sid] = value
    updated = fold_verified_fact_commands(base, commands, proven_births=proven_births)
    changes = tuple(change for command, _ in commands for change in command)
    by_id = {fact.fact_id: fact for fact in updated.facts}
    touched = {
        change.fact_id
        for change in changes
        if {"kind", "category", "text"} & (change.model_fields_set | set(change.clear))
    }
    for fid in touched:
        fact = by_id[fid]
        validate_characterization_words(
            kind=fact.kind,
            category=fact.category,
            text=fact.text,
            profile=machine_profile,
            complete_statement=complete_characterization,
        )
    validate_changed_fact_words(base, updated, changes, texts)
    return updated


def explicit_fact_edits(item: Any, *, creates: Callable[[Any], bool]) -> Any:
    """The existing empty-default/explicit-clear rule, shared with command proof.

    A new object's empty defaults may be omitted. For an existing object an
    empty value is ambiguous, so the author must omit it or explicitly clear it.
    The caller proves existence; this function does not infer scientific intent.
    """
    if not isinstance(item, dict):
        return item
    if "changes" in item and isinstance(item["changes"], list):
        return {**item, "changes": [explicit_fact_edits(c, creates=creates) for c in item["changes"]]}
    controls = {"clear", "withdraw", "add_source_ids"}
    empties = [name for name, value in item.items()
               if name not in controls and value in (None, "", [])]
    if not empties:
        return item
    if creates(item):
        return {name: value for name, value in item.items() if name not in empties}
    raise ValueError(f"{empties[0]}: omit unchanged properties or use explicit clear")


def creates_fact_edit(
    item: Any, *, known: set[str], bound_name: Callable[[str], str],
    locations: Mapping[str, Any],
) -> bool:
    """A new local fact, excluding an existing object or a read address."""
    token = item.get("fact_id") if isinstance(item, dict) else None
    if not isinstance(token, str) or not token or token in locations:
        return False
    return not token.startswith(("seen:", "source:")) and bound_name(token) not in known


def unlocated_fact_reference(token: str) -> str:
    if token.startswith(("seen:", "source:")):
        raise ValueError(
            f"unknown read location {token}; name an existing fact by its id, "
            "or by a location a view actually returned"
        )
    return token


def normalize_fact_change(
    change: FactChange, *, subject: Callable[[str], str], reference: Callable[[str], str],
    delivered_source_ids: set[str] | None, inherited_source_ids: frozenset[str],
) -> FactChange:
    """Pure identity/source normalization; no scientific field is inferred or added."""
    from tsdaf.lanes.literature.source_parts import fact_source_ids

    if delivered_source_ids is not None and (
        undelivered := sorted(
            set(fact_source_ids(change))
            - (delivered_source_ids | inherited_source_ids)
        )
    ):
        raise UnknownReference(
            "source_ids", undelivered[0], sorted(delivered_source_ids | inherited_source_ids),
            SOURCE_CITATION_POLICY + " " + INHERITED_CITATION_POLICY,
        )
    data = change.model_dump(exclude_unset=True)
    bind_fact_change_names(data, subject=subject, reference=reference)
    return type(change).model_validate_json(canonical_json_bytes(data))


def normalized_repair_changes(
    group: InlineFactChangeGroup, *, prefix: str, bound: set[str], known: set[str],
    delivered_source_ids: set[str], inherited_source_ids: frozenset[str],
) -> tuple[FactChange, ...]:
    """Reproduce a repair's mechanical command, from previously proved bindings.

    Public names and source names must already be resolved through the original
    request. The caller verifies group/existence/source authority; this helper is
    not a scientific acceptance path or a replacement for ordinary write checks.
    Reading repairs have no Fusion read-location table, matching their ordinary
    apply_sparse_submission call. Their original child order is retained.
    """
    bind = binder_from(prefix, bound)
    raw = fold_fact_prefix({"groups": [group.model_dump(mode="json", exclude_unset=True)]})["groups"][0]
    parsed = type(group).model_validate_json(canonical_json_bytes(explicit_fact_edits(
        raw, creates=lambda item: creates_fact_edit(item, known=known, bound_name=bind, locations={}),
    )))
    changes = []
    for change in parsed.changes:
        normalized = normalize_fact_change(
            change,
            subject=lambda token: token if token in known else unlocated_fact_reference(token),
            reference=unlocated_fact_reference, delivered_source_ids=delivered_source_ids,
            inherited_source_ids=inherited_source_ids,
        )
        data = normalized.model_dump(mode="json", exclude_unset=True)
        bind_fact_change_names(data, subject=bind, reference=bind)
        changes.append(type(normalized).model_validate_json(canonical_json_bytes(data)))
    return tuple(changes)


def accepted_fact_command(
    changes: Sequence[FactChange],
    *,
    authors: Mapping[str, Mapping[str, Any]],
    members: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Serialize the actual command and original item addresses without defaults."""
    return {
        "changes": [c.model_dump(mode="json", exclude_unset=True) for c in changes],
        "children": [{"fact_id": c.fact_id, **authors[c.fact_id]} for c in changes],
        "members": [dict(member) for member in members],
    }


def reconstructed_repair_command(
    group: InlineFactChangeGroup,
    *,
    prefix: str,
    bound: set[str],
    known: set[str],
    delivered_source_ids: set[str],
    inherited_source_ids: frozenset[str],
) -> tuple[tuple[FactChange, ...], dict[str, Any]]:
    """Reconstruct a Reading command from the original mechanically bound input.

    The caller must first prove the request's target map and existence/source
    context. This proves correspondence, not scientific acceptance. No host
    compound bridge or named-group members may be hidden in the command. Child
    positions belong to the one internal Fusion group; the outer Reading action
    address is separately proved by its accepted receipt.
    """
    if not group.changes or getattr(group, "fact_ids", ()):
        raise ValueError("repair command requires the original inline group")
    changes = normalized_repair_changes(
        group,
        prefix=prefix,
        bound=bound,
        known=known,
        delivered_source_ids=delivered_source_ids,
        inherited_source_ids=inherited_source_ids,
    )
    if len({c.fact_id for c in changes}) != len(changes):
        raise ValueError("repair command has duplicate targets")
    command = accepted_fact_command(
        changes,
        authors={
            c.fact_id: {"collection": "groups", "index": 0, "change_index": i}
            for i, c in enumerate(changes)
        },
        members=({"collection": "groups", "index": 0},),
    )
    return changes, command


def verified_repair_command(
    group: InlineFactChangeGroup,
    evidence: Mapping[str, Any],
    *,
    prefix: str,
    bound: set[str],
    known: set[str],
    delivered_source_ids: set[str],
    inherited_source_ids: frozenset[str],
) -> tuple[FactChange, ...]:
    """Compare an accepted witness to the same mechanically reconstructed command."""
    changes, expected = reconstructed_repair_command(
        group, prefix=prefix, bound=bound, known=known,
        delivered_source_ids=delivered_source_ids,
        inherited_source_ids=inherited_source_ids,
    )
    # Compare raw JSON values, before a DTO could erase presence, extra fields or
    # coerce booleans into numeric child addresses. Return only reconstructed data.
    if canonical_json_bytes(evidence) != canonical_json_bytes(expected):
        raise ValueError("repair command differs from its original accepted input")
    return changes


def apply_sparse_submission(
    base: FusionResult,
    raw: dict[str, Any],
    *,
    locations: dict[str, dict[str, Any]],
    store: PrivateArtifactStore,
    last_shown: FusionResult,
    validate_questions: Callable[[FusionProposal], None],
    delivered_source_ids: set[str] | None = None,
    inherited_source_ids: frozenset[str] = frozenset(),
    delivered_text: Mapping[str, Mapping[str, Any]] | None = None,
    render_reference: Callable[[UnknownReference], Any] | None = None,
    complete_characterization: bool = False,
    machine_profile: MachineProfile = MachineProfile.V1,
    accepted_origin: Callable[[str, int, int], FactOrigin] | None = None,
    accepted_commands: list[dict[str, Any]] | None = None,
    public_targets: bool = False,
) -> tuple[FusionResult, list[dict[str, Any]], list[tuple[FactChange, ...]]]:
    """Accept independent valid items without losing them to another item's error.

    Every registered collection is written one item at a time and reported one row at a
    time, in registration order. Local names are bound once across the whole write, so an
    item may name another item created in the same call.

    ``last_shown`` is the version the host compares a write with when its view names
    none other (`compared_version`; the rule is stated once, to the model, by
    `VIEW_RULE`), and an existing fact named by its bare id is bound to it whatever the
    view (step 6 (1)(3)).
    """
    models = fusion_write_collections(unified_expression=machine_profile.unified_expression,
                                     public_targets=public_targets)
    collections = tuple(models)
    scalars = tuple(key for key, _ in FUSION_WRITE_SCALARS)
    write_shape(raw, collections=collections, scalars=scalars, controls=("view",))
    raw = fold_fact_prefix(raw)
    view = ViewControl.model_validate({"view": raw.get("view")}).view

    @functools.cache
    def compared() -> FusionResult:
        # Every item the view governs is compared with this one version; a view naming
        # none refuses each of them with the same sentence.
        return compared_version(view, locations=locations, store=store, last_shown=last_shown)

    replies: list[dict[str, Any]] = []

    def reference_fields(error: ValueError) -> dict[str, Any]:
        return (
            render_reference(error)
            if isinstance(error, UnknownReference) and render_reference is not None
            else {}
        )

    accepted_batches: list[tuple[FactChange, ...]] = []
    singles: list[FactChange] = []
    single_rows: list[int] = []
    groups: list[FactChangeGroup] = []
    group_rows: list[int] = []
    seen: dict[str, dict[str, FusionResult]] = {}
    snapshots: dict[str, FusionResult] = {}
    known = {f.fact_id for f in base.facts}

    def normalize(change: FactChange, item_seen: dict[str, dict[str, FusionResult]]) -> FactChange:
        reads: dict[str, FusionResult] = {}

        def resolve(token: str, *, editable: bool = False) -> str:
            binding = locations.get(token)
            if binding is None:
                if editable and token in known:
                    # A fact this task may edit is named by its id; the host binds it to
                    # the version it last showed. The location table is a convenience
                    # for binding an older read, not a requirement (audit L1-02).
                    reads[token] = last_shown
                    return token
                return unlocated_fact_reference(token)
            if "fact_id" not in binding or binding.get("kind") not in {
                "CURRENT_RESULT",
                "HISTORICAL_RESULT",
            }:
                raise ValueError(f"relationship requires an actual fact read location: {token}")
            if editable and (not binding.get("writable") or binding["kind"] != "CURRENT_RESULT"):
                raise ValueError(
                    "this read location is not an editable current fact: "
                    f"{token} ({binding['fact_id']})"
                )
            ref = ArtifactRef.model_validate(binding["result_ref"])
            if ref.sha256 not in snapshots:
                payload = store.read_bytes(ref)
                if (
                    len(payload) != ref.byte_size
                    or hashlib.sha256(payload).hexdigest() != ref.sha256
                ):
                    raise ValueError("read snapshot bytes differ from their sealed identity")
                snapshots[ref.sha256] = read_fusion_result(payload)
            snapshot = snapshots[ref.sha256]
            if snapshot.work_unit_id != base.work_unit_id:
                raise ValueError("read location belongs to another task")
            fid: str = binding["fact_id"]
            if fid in reads and reads[fid] != snapshot:
                raise ValueError(
                    f"one change references different read versions of the same fact: {fid}"
                )
            reads[fid] = snapshot
            return fid

        def subject(token: str) -> str:
            fid = resolve(token, editable=True)
            if fid not in reads:
                # A new/local subject still validates the supplied comparison view.
                compared()
            return fid

        normalized = normalize_fact_change(
            change, subject=subject, reference=resolve,
            delivered_source_ids=delivered_source_ids, inherited_source_ids=inherited_source_ids,
        )
        item_seen[normalized.fact_id] = reads
        return normalized

    bound_name = name_binder(base, locations)
    kinds = {f.fact_id: f.kind for f in base.facts}
    categories = {f.fact_id: f.category for f in base.facts}

    def cite_not_transcribe(change: FactChange) -> FactChange:
        """A characterization note is one short sentence naming the measurement.

        A peak table or a full spectral line never enters the fusion state or a later
        reader's view (ledger E47): the card in the public result cites the spectrum, and
        the full text stays in the observation the card points at.
        """
        kind = change.kind or kinds.get(change.fact_id)
        category = change.category or categories.get(change.fact_id)
        validate_characterization_change(
            change, kind=kind, category=category, profile=machine_profile,
            complete_statement=complete_characterization,
        )
        return change

    def creates(item: Any) -> bool:
        return creates_fact_edit(item, known=known, bound_name=bound_name, locations=locations)

    for key in ("changes", "groups"):
        model = models[key]
        for index, item in enumerate(raw.get(key, [])):
            row: dict[str, Any] = {"collection": key, "index": index}
            replies.append(row)
            try:
                if key == "groups":
                    # A group naming this write's changes by id must find them here: the
                    # declaration's shape rule, applied to this one group at its own
                    # index, so the executor refuses with the declaration's sentence and
                    # the other items of the write stand (E55).
                    write_shape(
                        {"changes": raw.get("changes", []), "groups": [*([{}] * index), item]},
                        collections=("changes", "groups"),
                        references=FUSION_WRITE_REFERENCES,
                    )
                item_seen: dict[str, dict[str, FusionResult]] = {}
                parsed = model.model_validate_json(canonical_json_bytes(explicit_fact_edits(item, creates=creates)))
                if isinstance(parsed, FactChange):
                    singles.append(cite_not_transcribe(normalize(parsed, item_seen)))
                    single_rows.append(len(replies) - 1)
                else:
                    normalized = tuple(
                        cite_not_transcribe(normalize(c, item_seen)) for c in parsed.changes
                    )
                    groups.append(parsed.model_copy(update={"changes": normalized}))
                    group_rows.append(len(replies) - 1)
                seen.update(item_seen)
            except ValueError as error:
                row.update(
                    outcome="REJECTED",
                    diagnostic=diagnostic(error, model=model, collection=row["collection"]),
                    **reference_fields(error),
                )
    # Attempted declarations retain the link to a rejected parent's actual row.
    # They prove no fact and allocate no identity; only successful apply_proposal does.
    attempted: dict[str, set[int]] = {}
    attempted_counts: Counter[str] = Counter()
    if machine_profile.unified_expression:
        for row_index, row in enumerate(replies):
            if row.get("collection") not in {"changes", "groups"}:
                continue
            supplied = raw[row["collection"]][row["index"]]
            children = supplied.get("changes", ()) if (
                row["collection"] == "groups" and isinstance(supplied, dict)
            ) else (supplied,)
            for child in children if isinstance(children, (list, tuple)) else ():
                token = child.get("fact_id") if isinstance(child, dict) else None
                if isinstance(token, str) and token:
                    identity = bound_name(token)
                    attempted.setdefault(identity, set()).add(row_index)
                    attempted_counts[identity] += 1
    counts = Counter(c.fact_id for c in (*singles, *(c for g in groups for c in g.changes)))
    if machine_profile.unified_expression:
        counts = attempted_counts
    duplicates = {fid for fid, count in counts.items() if count > 1}
    # Duplicate target writes are ambiguous; only the affected items are rejected.
    for change_sets, rows in (
        ([(c,) for c in singles], single_rows),
        ([g.changes for g in groups], group_rows),
    ):
        for batch, row_index in zip(change_sets, rows, strict=True):
            if doubled := sorted({bound_name(c.fact_id) if machine_profile.unified_expression else c.fact_id
                                   for c in batch} & duplicates):
                replies[row_index].update(
                    outcome="REJECTED",
                    diagnostic="duplicate target in submission: " + ", ".join(doubled),
                )
    if machine_profile.unified_expression:
        # A named atomic group still includes members rejected during parsing.
        # Propagate across overlapping groups before filtering any rows out.
        while True:
            rejected_before = {i for i, row in enumerate(replies) if row.get("outcome") == "REJECTED"}
            for group, row in zip(groups, group_rows, strict=True):
                if not group.fact_ids:
                    continue
                members = {row} | {
                    member for token in group.fact_ids
                    for member in attempted.get(bound_name(token), ())
                }
                failed = members & rejected_before
                if failed:
                    for member in members - rejected_before:
                        replies[member].update(
                            outcome="REJECTED",
                            diagnostic="An explicit atomic group contains a rejected record.",
                            blocked_by=[f"{replies[r]['collection']}:{replies[r]['index']}"
                                        for r in sorted(failed)],
                        )
            if rejected_before == {i for i, row in enumerate(replies) if row.get("outcome") == "REJECTED"}:
                break
    # Navigation, questions and assessments: each item on its own shape, then duplicates.
    metadata: dict[str, list[tuple[Any, int]]] = {key: [] for key in METADATA_MODELS}
    for key, item_model in METADATA_MODELS.items():
        for index, item in enumerate(raw.get(key, [])):
            row = {"collection": key, "index": index}
            replies.append(row)
            try:
                entry = item_model.model_validate_json(canonical_json_bytes(item))
                metadata[key].append((entry, len(replies) - 1))
            except ValueError as error:
                row.update(
                    outcome="REJECTED",
                    diagnostic=diagnostic(error, model=item_model, collection=key),
                    **reference_fields(error),
                )
        identity = METADATA_IDENTITY[key]
        repeated = Counter(identity.key(parsed) for parsed, _ in metadata[key])
        for parsed, row_index in metadata[key]:
            if repeated[identity.key(parsed)] > 1:
                replies[row_index].update(
                    outcome="REJECTED",
                    diagnostic="duplicate target in submission: " + _identified(identity, parsed),
                )
        metadata[key] = [(p, r) for p, r in metadata[key] if "outcome" not in replies[r]]
    values: list[tuple[str, str, int]] = []
    for key in scalars:
        if raw.get(key) is None:
            continue
        row = {"collection": key}
        replies.append(row)
        try:
            values.append((key, scalar_text(raw[key]), len(replies) - 1))
        except ValueError as error:
            row.update(outcome="REJECTED", diagnostic=diagnostic(error))
    bound = bind_local_names(
        base,
        FusionProposal(
            changes=tuple(
                c for c, r in zip(singles, single_rows, strict=True) if "outcome" not in replies[r]
            ),
            groups=tuple(
                g for g, r in zip(groups, group_rows, strict=True) if "outcome" not in replies[r]
            ),
            segments=tuple(p for p, _ in metadata["segments"]),
            questions=tuple(p for p, _ in metadata["questions"]),
            experiment_assessments=tuple(p for p, _ in metadata["experiment_assessments"]),
        ),
        locations,
        bind_group_locations=machine_profile.unified_expression,
    )
    for original, mapped in zip(
        (
            *[c for c, r in zip(singles, single_rows, strict=True) if "outcome" not in replies[r]],
            *[
                c
                for g, r in zip(groups, group_rows, strict=True)
                if "outcome" not in replies[r]
                for c in g.changes
            ],
        ),
        (*bound.changes, *(c for g in bound.groups for c in g.changes)),
        strict=True,
    ):
        if mapped.fact_id not in seen or mapped.fact_id not in seen[mapped.fact_id]:
            seen[mapped.fact_id] = {**seen.get(original.fact_id, {}), mapped.fact_id: compared()}
    single_rows = [r for r in single_rows if "outcome" not in replies[r]]
    group_rows = [r for r in group_rows if "outcome" not in replies[r]]
    batches = [
        (tuple(bound.changes[i] for i in indexes), [single_rows[i] for i in indexes])
        for indexes in (
            tuple((i,) for i in range(len(bound.changes)))
            if machine_profile.unified_expression
            else linked_change_batches(bound.changes, known)
        )
    ]
    batches.extend(
        (g.changes, [r]) for g, r in zip(bound.groups, group_rows, strict=True) if g.changes
    )
    row_facts = {r: [c.fact_id] for c, r in zip(bound.changes, single_rows, strict=True)}
    row_facts.update(
        {r: [c.fact_id for c in g.changes] for g, r in zip(bound.groups, group_rows, strict=True)}
    )
    # Preserve the actual item/child position before grouping changes into atomic
    # batches. A group naming other rows does not become those rows' author.
    authors = {}
    for _changes, rows in batches:
        for r in rows:
            row = replies[r]
            for change_index, fid in enumerate(row_facts[r]):
                authors[fid] = {"collection": row["collection"], "index": row["index"],
                                "change_index": change_index}
    origins = {
        fid: accepted_origin(a["collection"], a["index"], a["change_index"])
        for fid, a in authors.items()
    } if accepted_origin is not None else {}
    # A group that names this write's changes by fact_ids (E54; E92 renamed the field)
    # binds their batches into one:
    # the items stay where the model sent them, under changes, each with its own row,
    # and the group's row reports the joint outcome.
    for group, r in zip(bound.groups, group_rows, strict=True):
        if not group.fact_ids:
            continue
        by_fact = {c.fact_id: i for i, (changes, _) in enumerate(batches) for c in changes}
        indexes = sorted({by_fact[f] for f in group.fact_ids if f in by_fact})
        if not indexes:
            replies[r].update(
                outcome="REJECTED",
                diagnostic="none of the named changes was accepted for saving in this write",
            )
            continue
        merged_changes = tuple(c for i in indexes for c in batches[i][0])
        merged_rows = [row for i in indexes for row in batches[i][1]] + [r]
        batches = [b for i, b in enumerate(batches) if i not in indexes]
        batches.insert(indexes[0], (merged_changes, merged_rows))
        row_facts[r] = [c.fact_id for c in merged_changes]
    row_batch = {row: i for i, (_, rows) in enumerate(batches) for row in rows}
    prerequisites: dict[int, set[int]] = {}
    for i, (changes, rows) in enumerate(batches):
        own = {change.fact_id for change in changes}
        prerequisites[i] = {
            row
            for change in changes
            for ref in fact_references(change) - known - own
            for row in attempted.get(ref, ())
            if row not in rows
        } if machine_profile.unified_expression else set()
    done: set[int] = set()
    visiting: set[int] = set()

    def apply_batch(index: int) -> None:
        nonlocal base
        if index in done:
            return
        changes, rows = batches[index]
        if index in visiting:
            for row in rows:
                replies[row].update(
                    outcome="REJECTED",
                    diagnostic="Cyclic new references require an explicit atomic group.",
                    fact_ids=row_facts[row],
                )
            done.add(index)
            return
        visiting.add(index)
        for row in sorted(prerequisites[index]):
            if row in row_batch:
                apply_batch(row_batch[row])
        visiting.remove(index)
        if index in done:
            return
        failed = [r for r in sorted(prerequisites[index]) if replies[r].get("outcome") == "REJECTED"]
        if failed:
            for row in rows:
                replies[row].update(
                    outcome="REJECTED",
                    diagnostic="A required record in this submission was not saved.",
                    blocked_by=[f"{replies[r]['collection']}:{replies[r]['index']}" for r in failed],
                    fact_ids=row_facts[row],
                )
            done.add(index)
            return
        try:
            validate_read_versions(
                base, changes, seen,
                created_in_submission=tuple(f for f in base.facts if f.fact_id not in known)
                if machine_profile.unified_expression else (),
            )
            updated = apply_proposal(
                base, FusionProposal(changes=changes), origins=origins
            )
            validate_changed_fact_words(base, updated, changes, delivered_text)
            # Recording another accepted address does not turn a scientific no-op
            # into a changed fact. It still belongs to the immutable accepted audit.
            scientific_change = (
                updated.model_copy(update={"field_origins": base.field_origins}) != base
            )
            outcome = "SAVED" if scientific_change else "NO_CHANGE"
            # This is the actual normalized command, including a successful no-op.
            # A caller may retain it as private execution evidence only after its
            # outer transaction succeeds; it is not a public fact or a receipt.
            if accepted_commands is not None:
                accepted_commands.append(accepted_fact_command(
                    changes, authors=authors, members=[
                        {"collection": replies[r]["collection"], "index": replies[r]["index"]}
                        for r in rows
                    ],
                ))
            if updated != base:
                accepted_batches.append(tuple(changes))
            base = updated
            for r in rows:
                replies[r].update(outcome=outcome, fact_ids=row_facts[r])
        except ValueError as error:
            # Every row of a linked batch is refused by the one fault; each says which
            # facts it carried and which of the batch's facts the fault names, so a
            # model resends the one at fault rather than the whole batch (E54).
            reason = diagnostic(error)
            batch_facts = dict.fromkeys(f for r in rows for f in row_facts[r])
            named = [f for f in batch_facts if isinstance(reason, str) and f in reason]
            for r in rows:
                refused: dict[str, Any] = {
                    "outcome": "REJECTED",
                    "diagnostic": reason,
                    "fact_ids": row_facts[r],
                    **reference_fields(error),
                }
                if len(rows) > 1:
                    refused["linked_batch"] = {"rows": len(rows), "at_fault": named}
                replies[r].update(refused)
        done.add(index)

    for batch_index in range(len(batches)):
        apply_batch(batch_index)
    # Metadata is checked against the view the model wrote it from, item by item.
    bound_items = {
        "segments": bound.segments,
        "questions": bound.questions,
        "experiment_assessments": bound.experiment_assessments,
    }
    for key in METADATA_MODELS:
        identity = METADATA_IDENTITY[key]
        for item, (_, r) in zip(bound_items[key], metadata[key], strict=True):
            row = replies[r]
            try:
                snapshot = compared()
                old = {identity.key(v): v for v in getattr(snapshot, key)}
                current = {identity.key(v): v for v in getattr(base, key)}
                token = identity.key(item)
                if current.get(token) not in (old.get(token), item, None):
                    raise StaleFactChange(
                        _identified(identity, item),
                        old[token].model_dump(mode="json") if token in old else None,
                        current[token].model_dump(mode="json"),
                    )
                if (
                    isinstance(item, RouteSegment | MainExperimentAssessment)
                    and delivered_source_ids is not None
                    and not set(item.source_ids) <= (delivered_source_ids | inherited_source_ids)
                ):
                    raise ValueError(SOURCE_CITATION_POLICY + " " + INHERITED_CITATION_POLICY)
                proposal = FusionProposal.model_validate({key: (item,)})
                if isinstance(item, FusionQuestion):
                    validate_questions(proposal)
                updated = apply_proposal(base, proposal)
                # The identity it landed on, as a change row names its fact_ids; not the
                # item echoed back to the model that just wrote it (E187).
                landed = item.model_dump(mode="json")
                row.update(
                    outcome="NO_CHANGE" if updated == base else "SAVED",
                    **{name: landed[name] for name in identity.keys},
                )
                base = updated
            except ValueError as error:
                row.update(
                    outcome="REJECTED",
                    diagnostic=diagnostic(error),
                    **reference_fields(error),
                )
    for key, value, r in values:
        row = replies[r]
        try:
            snapshot = compared()
            if getattr(snapshot, key) != getattr(base, key) and value != getattr(base, key):
                raise StaleFactChange(key, getattr(snapshot, key), getattr(base, key))
            updated = apply_proposal(base, FusionProposal.model_validate({key: value}))
            row.update(outcome="NO_CHANGE" if updated == base else "SAVED")
            base = updated
        except ValueError as error:
            row.update(outcome="REJECTED", diagnostic=diagnostic(error))
    return base, replies, accepted_batches


def reading_candidates(
    observations: Sequence[dict[str, Any]],
    selected: Sequence[Mapping[str, Any]],
    sources: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Navigation within one verified work unit/document, never scientific ownership.

    Source keys can differ when Fusion rereads a reader's original passage. Compare
    the canonical anchors too; an overlap offers every candidate, not a winner.
    """
    by_id = {source["source_id"]: source for source in sources}

    def overlaps(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        if (left.get("canonical_source_id"), left.get("modality")) != (
            right.get("canonical_source_id"),
            right.get("modality"),
        ):
            return False
        if left.get("modality") == "TEXT":
            return bool(
                {s["index"] for s in left.get("sentences", ())}
                & {s["index"] for s in right.get("sentences", ())}
            )
        a, b = left.get("bbox_px"), right.get("bbox_px")
        return bool(
            a
            and b
            and max(a["x_min_px"], b["x_min_px"]) < min(a["x_max_px"], b["x_max_px"])
            and max(a["y_min_px"], b["y_min_px"]) < min(a["y_max_px"], b["y_max_px"])
        )

    result = []
    for observation in observations:
        matches = []
        for sid in observation["source_ids"]:
            for source in selected:
                if sid == source["source_id"]:
                    reason = "same source reference"
                elif sid in by_id and overlaps(by_id[sid], source):
                    reason = "overlapping original range"
                else:
                    continue
                matches.append({"source_ids": [sid, source["source_id"]], "basis": reason})
        if matches:
            result.append({**observation, "matches": matches})
    return result


def reading_navigation_page(
    entries: Sequence[dict[str, Any]],
    location: str,
    *,
    offset: int = 0,
    limit: int = 20,
    max_bytes: int,
) -> dict[str, Any]:
    """Bound the actual navigation rows, not a different record-directory head."""
    page: dict[str, Any] = {
        "outcome": "DIRECTORY",
        "location": location,
        "entries": [],
        "entry_count": len(entries),
        "offset": offset,
        "notice": "Navigation only; shared evidence does not establish reaction ownership.",
        "next": None,
    }
    # Reserve space for the cursor and the surrounding read receipt.
    budget = max_bytes - 1024
    for entry in entries[offset : offset + limit]:
        shown = entry
        if len(canonical_json_bytes(entry)) > budget // 2:
            shown = {
                "location": entry["location"],
                "scope": str(entry["scope"])[:240],
                "metadata_omitted": True,
            }
        page["entries"].append(shown)
        if len(canonical_json_bytes(page)) > budget:
            page["entries"].pop()
            break
    end = offset + len(page["entries"])
    if end < len(entries):
        page["next"] = {
            "location": location,
            "select": {"kind": "directory", "offset": end, "limit": limit},
        }
    return page


def reading_text_parts(value: dict[str, Any], *, max_bytes: int) -> list[dict[str, Any]]:
    """Exact contiguous interpretation text; continuation never invents a sentence cut.

    Budget serialized UTF-8 (including JSON escaping), not characters. Metadata stays
    with each part; exceptionally large metadata remains a normal record directory.
    """
    budget = max_bytes - 2048  # Receipt and the Host-bound next location.
    if len(canonical_json_bytes(value)) <= budget:
        return [value]
    text = value.get("text")
    if not isinstance(text, str) or not text:
        return [value]

    def part(start: int, end: int) -> dict[str, Any]:
        return {
            **value,
            "text": text[start:end],
            "coverage": {
                "field": "text",
                "char_start": start,
                "char_end": end,
                "total_chars": len(text),
                "complete": start == 0 and end == len(text),
            },
        }

    if len(canonical_json_bytes(part(0, 1))) > budget:
        return [value]
    parts = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        while low < high:
            end = (low + high + 1) // 2
            if len(canonical_json_bytes(part(start, end))) <= budget:
                low = end
            else:
                high = end - 1
        parts.append(part(start, low))
        start = low
    return parts
