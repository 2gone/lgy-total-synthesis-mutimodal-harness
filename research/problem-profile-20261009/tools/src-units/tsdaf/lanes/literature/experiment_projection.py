"""Shared v1.6 experiment construction and deterministic measurement adoption.

The caller supplies verified source wording, resolved public references and host
creation keys. Contributions are projections of accepted receipts, indexed by the
caller's persistent item identity; they are never another public fact store. Replay
always starts from the immutable baseline, with the current effective contributions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from tsdaf.contracts.literature.expressions import (
    SourcePrimary,
    SourceRelation,
    merge_interpretations,
    model_interpretation,
    pending,
    present,
    source_statement,
)
from tsdaf.contracts.literature.objects import experiment_containers
from tsdaf.contracts.literature.schema import (
    MachineProfile,
    fact_definition,
    validate_fact_definition,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.outcome_meanings import OUTCOME_KINDS
from tsdaf.lanes.literature.reported_scalars import parse_reported_outcome
from tsdaf.lanes.literature.stable_keys import DERIVED_ORDINAL, public_id

if TYPE_CHECKING:
    from tsdaf.lanes.literature.experiment_intents import MeasurementIntent

_PROFILE = MachineProfile.V1_6


def _refs(refs: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    unique = {canonical_json_bytes(ref): dict(ref) for ref in refs}
    return [unique[key] for key in sorted(unique)]


def _interpretation(explanation: str, refs: list[dict[str, str]], **choices: Any) -> dict[str, Any]:
    return present(model_interpretation(explanation, refs, **choices), refs)


def build_experiment_operation(
    *, creation_key: str, position: int, description: str, refs: Sequence[Mapping[str, str]]
) -> dict[str, Any]:
    """A model-organized operation with schema-declared unknown scientific fields."""
    evidence = _refs(refs)
    record: dict[str, Any] = {
        "operation_id": public_id("OP", creation_key),
        "stable_key": creation_key,
        "key_basis": DERIVED_ORDINAL,
        "sequence_index": position,
        "other_conditions": [],
        "evidence_refs": evidence,
        "model_description": present(description, evidence),
    }
    schema = cast(dict[str, Any], fact_definition("Operation", _PROFILE))
    for field in schema["required"]:
        prop = schema["properties"][field]
        if field not in record and prop.get("$ref", "").startswith("#/$defs/ResolvedValue_"):
            record[field] = pending("INSUFFICIENT_EVIDENCE")
    validate_fact_definition("Operation", record, _PROFILE)
    return record


def build_experimental_account(
    *,
    creation_key: str,
    title: str,
    explanation: str,
    refs: Sequence[Mapping[str, str]],
    source_parts: Sequence[Mapping[str, Any]] = (),
    input_compound_ids: Sequence[str] = (),
    output_compound_ids: Sequence[str] = (),
    operations: Sequence[Mapping[str, Any]] = (),
    preparation_uses: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Construct an independent account without changing any route or source words."""
    evidence = _refs(refs)
    record = {
        "account_id": public_id("ACCOUNT", creation_key),
        "stable_key": creation_key,
        "title": title,
        "input_compound_ids": present(list(input_compound_ids), evidence)
        if input_compound_ids
        else pending("INSUFFICIENT_EVIDENCE"),
        "output_compound_ids": present(list(output_compound_ids), evidence)
        if output_compound_ids
        else pending("INSUFFICIENT_EVIDENCE"),
        "operations": deepcopy(list(operations)),
        "reported_outcomes": [],
        "evidence_refs": evidence,
        "model_interpretations": [_interpretation(explanation, evidence)],
        **({"source_parts": deepcopy(list(source_parts))} if source_parts else {}),
        **({"preparation_uses": deepcopy(list(preparation_uses))} if preparation_uses else {}),
    }
    validate_fact_definition("ExperimentalAccount", record, _PROFILE)
    return record


@dataclass(frozen=True, slots=True)
class MeasurementProjection:
    """Host-only projection of one effective accepted contribution, never model wire."""

    outcome: dict[str, Any]
    experiment: dict[str, str] | None
    identity_known: bool
    explanation: str
    refs: tuple[dict[str, str], ...]
    relation: SourceRelation = "ADDS"
    primary: SourcePrimary | None = None
    skeleton: dict[str, Any] | None = None
    experiment_reading: dict[str, Any] | None = None


def _validate_skeleton(
    skeleton: Mapping[str, Any] | None, outcome_id: str, primary: SourcePrimary | None
) -> None:
    if primary != "SKELETON":
        return
    if skeleton is None:
        raise ValueError("SKELETON requires the exact accepted prior snapshot")
    if skeleton.get("outcome", {}).get("outcome_id") != outcome_id:
        raise ValueError("SKELETON snapshot must name the same measurement")
    if skeleton.get("measurement_identity") not in {"CONFIRMED", "UNRESOLVED"}:
        raise ValueError("SKELETON snapshot must retain the prior measurement identity state")
    validate_fact_definition(
        "ResolvedValue_ExperimentReference", skeleton.get("experiment"), _PROFILE
    )


def build_measurement(
    *,
    creation_key: str,
    text: str,
    kind: str | None,
    product_ids: Sequence[str],
    operation_ids: Sequence[str],
    measurement: MeasurementIntent,
    experiment: Mapping[str, str] | None,
    refs: Sequence[Mapping[str, str]],
    explanation: str,
    existing: Mapping[str, Any] | None = None,
    relation: SourceRelation = "ADDS",
    primary: SourcePrimary | None = None,
    skeleton: Mapping[str, Any] | None = None,
    experiment_reading: Mapping[str, Any] | None = None,
) -> MeasurementProjection:
    """Compile explicit scientific choices; quantity equality never binds identity.

    Omission/inheritance and named-reference resolution are performed by the caller
    before this shared builder. An unknown identity still has a host-created record,
    while its scientific relationship to other measurements remains unresolved.
    `skeleton`, for a SKELETON ruling, is the accepted prior snapshot with `outcome`,
    `experiment` (a ResolvedValue) and `measurement_identity`. It is not inferred
    from which contributions happen to be replayed alongside this one.
    `experiment_reading` is the authoritative public wrapper when supplied. A raw
    `experiment` may accompany it only if it names the same PRESENT reference.
    """
    if measurement.same_as is not None:
        if existing is None or existing.get("outcome_id") != measurement.same_as:
            raise ValueError("same_as must resolve to the exact existing measurement")
        identity = {
            field: deepcopy(existing[field])
            for field in ("outcome_id", "stable_key", "key_basis")
            if field in existing
        }
    else:
        identity = {
            "outcome_id": public_id("OUT", creation_key),
            "stable_key": creation_key,
            "key_basis": DERIVED_ORDINAL,
        }
    _validate_skeleton(skeleton, str(identity["outcome_id"]), primary)
    if experiment is not None:
        validate_fact_definition("ExperimentReference", dict(experiment), _PROFILE)
    if experiment_reading is not None:
        validate_fact_definition(
            "ResolvedValue_ExperimentReference", dict(experiment_reading), _PROFILE
        )
        if experiment is not None and (
            experiment_reading.get("state") != "PRESENT"
            or experiment_reading.get("value") != dict(experiment)
        ):
            raise ValueError("experiment and experiment_reading disagree")
    evidence = _refs(refs)
    scalar, basis = parse_reported_outcome(text)
    standard_kind = kind if kind in OUTCOME_KINDS else None
    interpretation = _interpretation(explanation, evidence, relation=relation, primary=primary)
    statement = source_statement(
        text,
        evidence,
        [interpretation],
        kind=standard_kind,
        product_ids=list(product_ids),
        **({"kind_verbatim": kind} if kind is not None else {}),
    )
    record = {
        **identity,
        "kind": present(standard_kind, evidence) if standard_kind else pending("UNPARSED_SOURCE"),
        "reported_value": present(scalar, evidence),
        "value_provenance": "REPORTED",
        "basis": present(basis, evidence) if basis else pending("INSUFFICIENT_EVIDENCE"),
        "operation_ids": present(list(operation_ids), evidence)
        if operation_ids
        else pending("INSUFFICIENT_EVIDENCE"),
        "product_compound_ids": present(list(product_ids), evidence)
        if product_ids
        else pending("INSUFFICIENT_EVIDENCE"),
        "evidence_refs": evidence,
        "source_statements": [statement],
        "model_interpretations": [interpretation],
    }
    validate_fact_definition("ReportedOutcome", record, _PROFILE)
    return MeasurementProjection(
        outcome=record,
        experiment=dict(experiment) if experiment is not None else None,
        identity_known=measurement.identity_known,
        explanation=explanation,
        refs=tuple(evidence),
        relation=relation,
        primary=primary,
        skeleton=deepcopy(dict(skeleton)) if skeleton is not None else None,
        experiment_reading=deepcopy(dict(experiment_reading))
        if experiment_reading is not None
        else None,
    )


def _choices(field: Mapping[str, Any]) -> list[dict[str, Any]]:
    if field.get("state") == "PRESENT":
        return [{"value": field["value"], "evidence_uses": field["evidence_uses"]}]
    return list(field.get("candidates", []))


def _resolve(
    identity: str,
    field_name: str,
    baseline: Mapping[str, Any] | None,
    contributions: Sequence[tuple[Mapping[str, Any], SourceRelation, SourcePrimary | None]],
    *,
    skeletons: Sequence[Mapping[str, Any]] = (),
    preserve_baseline_primary: bool = True,
) -> dict[str, Any]:
    """One explicit-primary rule for value fields and experiment ownership.

    Independent FINDING rulings compete with each other. Without a ruling, ADDS
    contributes without replacing an adopted value; SAME keeps that value too.
    Experiment ownership passes preserve_baseline_primary=False: independent owner
    assertions compete even when a value contribution says SAME or ADDS. An empty
    baseline field can be filled, while actual unresolved candidates stay unresolved.
    This function compares public field values only after identity was supplied.
    """
    original = baseline or pending("INSUFFICIENT_EVIDENCE")
    all_fields = [original, *skeletons, *(field for field, _, _ in contributions)]
    finding = [field for field, _, primary in contributions if primary == "FINDING"]
    if finding:
        selected = finding
    elif skeletons:
        # A ruling keeps the precise prior adopted value, not a guessed owner.
        # Other independent contributions still compete with that selected value.
        selected = [
            *skeletons,
            *(
                field
                for field, relation, primary in contributions
                if primary != "SKELETON" and relation != "SAME"
            ),
        ]
    elif preserve_baseline_primary and baseline is not None and baseline.get("state") == "PRESENT":
        selected = [baseline]
    else:
        selected = [
            *([baseline] if baseline is not None and _choices(baseline) else []),
            *(field for field, relation, _ in contributions if relation != "SAME"),
        ]
        if not selected:
            selected = [field for field, _, _ in contributions]

    values: dict[bytes, dict[str, Any]] = {}
    for field in all_fields:
        for candidate in [*_choices(field), *field.get("candidates", [])]:
            key = canonical_json_bytes(candidate["value"])
            row = values.setdefault(
                key, {"value": deepcopy(candidate["value"]), "evidence_uses": []}
            )
            row["evidence_uses"] = merge_interpretations(
                row["evidence_uses"], candidate["evidence_uses"]
            )
    adopted = {
        canonical_json_bytes(field["value"])
        for field in selected
        if field.get("state") == "PRESENT"
    }
    # A candidate is a possible reading, even when it is the only candidate.
    # Only explicitly PRESENT selected readings can establish adoption.
    unresolved_selected = any(field.get("state") != "PRESENT" for field in selected)
    if len(adopted) == 1 and not unresolved_selected:
        chosen = values[next(iter(adopted))]
        refs = _refs([use["evidence_ref"] for use in chosen["evidence_uses"]])
        resolved = present(deepcopy(chosen["value"]), refs)
    else:
        resolved = pending("CONFLICTING_CANDIDATES" if len(values) > 1 else "INSUFFICIENT_EVIDENCE")
    if resolved["state"] == "UNRESOLVED":
        resolved["evidence_uses"] = merge_interpretations(
            [], [use for field in all_fields for use in field.get("evidence_uses", [])]
        )
    if len(values) > 1 or resolved["state"] == "UNRESOLVED":
        resolved["candidates"] = [
            {
                "candidate_id": public_id("CAND", identity + "|" + field_name + "|" + key.decode()),
                **values[key],
            }
            for key in sorted(values)
        ]
    return resolved


def _baseline_assignment(
    outcome: Mapping[str, Any], container: Mapping[str, Any] | None
) -> dict[str, Any]:
    if "assignment" in outcome:
        return deepcopy(outcome["assignment"])
    if container is None:
        raise ValueError("unassigned measurement is missing its assignment")
    field, object_type = (
        ("reaction_block_id", "REACTION_BLOCK")
        if "reaction_block_id" in container
        else ("account_id", "EXPERIMENTAL_ACCOUNT")
    )
    return {
        "experiment": present(
            {"object_type": object_type, "object_id": container[field]},
            list(outcome["evidence_refs"]),
        ),
        "measurement_identity": "CONFIRMED",
        "explanation": "",
        "evidence_refs": list(outcome["evidence_refs"]),
    }


def apply_measurement_contributions(
    baseline: Mapping[str, Any], contributions: Mapping[str, MeasurementProjection]
) -> dict[str, Any]:
    """Replay selected accepted contributions once, atomically moving each outcome.

    Keys identify the effective producer/item, not wording or value. To revise one's
    own contribution replace its entry before replay; to withdraw B replay only A.
    Never pass a previous projection as the immutable baseline of the same replay.
    """
    result = deepcopy(dict(baseline))
    domain = result["domain_payload"]
    domain.setdefault("unassigned_outcomes", [])
    containers = {
        c.get("reaction_block_id", c.get("account_id")): cast(dict[str, Any], c)
        for c in experiment_containers(result)
    }
    originals: dict[str, tuple[dict[str, Any], Mapping[str, Any] | None]] = {}
    for container in [*containers.values(), None]:
        outcomes = (
            container["reported_outcomes"]
            if container is not None
            else domain["unassigned_outcomes"]
        )
        for outcome in outcomes:
            oid = outcome["outcome_id"]
            if oid in originals:
                raise ValueError("baseline measurement has more than one public location")
            originals[oid] = (outcome, container)
    groups: dict[str, list[MeasurementProjection]] = {}
    for contribution_id in sorted(contributions):
        row = contributions[contribution_id]
        _validate_skeleton(row.skeleton, row.outcome["outcome_id"], row.primary)
        reading = row.experiment_reading
        if reading is not None:
            validate_fact_definition("ResolvedValue_ExperimentReference", reading, _PROFILE)
            if row.experiment is not None and (
                reading.get("state") != "PRESENT" or reading.get("value") != row.experiment
            ):
                raise ValueError("experiment and experiment_reading disagree")
        references = (
            [choice["value"] for choice in _choices(reading)]
            + [choice["value"] for choice in reading.get("candidates", [])]
            if reading is not None
            else [row.experiment]
            if row.experiment is not None
            else []
        )
        for reference in references:
            target = containers.get(reference["object_id"])
            target_type = (
                "REACTION_BLOCK"
                if target is not None and "reaction_block_id" in target
                else "EXPERIMENTAL_ACCOUNT"
            )
            if target is None or reference["object_type"] != target_type:
                raise ValueError("measurement experiment is not an available matching container")
        groups.setdefault(row.outcome["outcome_id"], []).append(row)
    for oid, rows in sorted(groups.items()):
        old, old_container = originals.get(oid, ({}, None))
        assignment = _baseline_assignment(old, old_container) if old else {}
        outcome = deepcopy(old or rows[0].outcome)
        outcome.pop("assignment", None)
        # The contract identifies precisely which outcome fields are ResolvedValues.
        properties = cast(
            dict[str, Any], fact_definition("ReportedOutcome", _PROFILE)["properties"]
        )
        for field, declaration in properties.items():
            if declaration.get("$ref", "").startswith("#/$defs/ResolvedValue_"):
                outcome[field] = _resolve(
                    oid,
                    field,
                    old.get(field),
                    [(r.outcome[field], r.relation, r.primary) for r in rows],
                    skeletons=[
                        r.skeleton["outcome"][field]
                        for r in rows
                        if r.primary == "SKELETON" and r.skeleton is not None
                    ],
                )
        for field in ("source_statements", "model_interpretations", "evidence_refs"):
            combined = list(old.get(field, []))
            for row in rows:
                combined = merge_interpretations(combined, row.outcome.get(field, []))
            if combined:
                outcome[field] = combined
        experiment = _resolve(
            oid,
            "experiment",
            assignment.get("experiment"),
            [
                (
                    r.experiment_reading
                    if r.experiment_reading is not None
                    else (
                        present(r.experiment, list(r.refs))
                        if r.experiment is not None
                        else pending("INSUFFICIENT_EVIDENCE")
                    ),
                    "ADDS",
                    r.primary,
                )
                for r in rows
            ],
            skeletons=[
                r.skeleton["experiment"]
                for r in rows
                if r.primary == "SKELETON" and r.skeleton is not None
            ],
            preserve_baseline_primary=False,
        )
        finding = [row for row in rows if row.primary == "FINDING"]
        if finding:
            identity_known = all(row.identity_known for row in finding)
        elif assignment.get("measurement_identity") == "CONFIRMED":
            identity_known = True
        else:
            identity_known = any(row.identity_known for row in rows)
        # Remove the old location and add the one new location only after constructing
        # the complete replacement. The input baseline is never modified.
        for container in containers.values():
            container["reported_outcomes"] = [
                o for o in container["reported_outcomes"] if o["outcome_id"] != oid
            ]
        domain["unassigned_outcomes"] = [
            o for o in domain["unassigned_outcomes"] if o["outcome_id"] != oid
        ]
        if identity_known and experiment["state"] == "PRESENT":
            containers[experiment["value"]["object_id"]]["reported_outcomes"].append(outcome)
        else:
            explanations = [assignment.get("explanation", ""), *(r.explanation for r in rows)]
            outcome["assignment"] = {
                "experiment": experiment,
                "measurement_identity": "CONFIRMED" if identity_known else "UNRESOLVED",
                "explanation": "\n".join(dict.fromkeys(text for text in explanations if text)),
                "evidence_refs": _refs(
                    [*assignment.get("evidence_refs", []), *(ref for r in rows for ref in r.refs)]
                ),
            }
            domain["unassigned_outcomes"].append(outcome)
        validate_fact_definition("ReportedOutcome", outcome, _PROFILE)
    return result
