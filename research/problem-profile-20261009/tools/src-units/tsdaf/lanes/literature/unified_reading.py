"""Compile explicit experimental relationships, with host-owned identity and evidence.

These projections share the production experiment builders with Fusion. They do
not choose scientific owners from products, words, order or numeric equality.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict

from tsdaf.contracts.literature.expressions import (
    expressed_value,
    model_interpretation,
    pending,
    present,
)
from tsdaf.contracts.literature.objects import experiment_containers, reported_outcomes
from tsdaf.contracts.literature.schema import MachineProfile, validate_fact_definition
from tsdaf.lanes.literature.enrichment_values import CompiledMeasurement
from tsdaf.lanes.literature.experiment_intents import MeasurementIntent
from tsdaf.lanes.literature.experiment_projection import (
    build_measurement,
)


def creation_key(collection, item, keys):
    key = f"{collection}:{item.item_id}"
    if key not in (keys or {}):
        raise ValueError(f"{collection} has no verified accepted creation origin")
    return keys[key]


def references(receipt, selections):
    from tsdaf.lanes.literature.container_enrichment import _refs

    return _refs(receipt, tuple(selections))


def resolve_experiment_reference(selected, containers):
    """An explicit container selection; unknown ownership remains unknown."""
    if selected is None:
        return None
    if selected not in containers:
        from tsdaf.lanes.literature.model_names import UnknownReference

        raise UnknownReference(
            "words.experiment",
            selected,
            sorted(containers),
            "experiment names an unknown experimental container",
        )
    return {
        "object_type": "REACTION_BLOCK"
        if "reaction_block_id" in containers[selected]
        else "EXPERIMENTAL_ACCOUNT",
        "object_id": selected,
    }


def compile_measurement(finding, receipt, machine, keys, ruling=None, *, comparison_machine=None):
    from tsdaf.lanes.literature.finding_updates import resolved_intent
    intent = resolved_intent(finding)
    primary = ("FINDING" if intent == "UPDATE_CURRENT" else None) if intent is not None else (ruling.primary if ruling else None)
    words = finding.words
    containers = {
        c.get("reaction_block_id", c.get("account_id")): c for c in experiment_containers(machine)
    }
    op_owner = {o["operation_id"]: cid for cid, c in containers.items() for o in c["operations"]}
    comparison = machine if comparison_machine is None else comparison_machine
    comparison_containers = {
        c.get("reaction_block_id", c.get("account_id")): c
        for c in experiment_containers(comparison)
    }
    outcomes = {o["outcome_id"]: o for o in reported_outcomes(comparison)}
    choice = words.measurement or MeasurementIntent.model_validate({"unknown": True})
    existing = outcomes.get(choice.same_as) if choice.same_as else None
    if choice.same_as is not None and existing is None:
        raise ValueError("same_as names an unknown measurement")
    old_owner = next(
        (
            cid
            for c in experiment_containers(comparison)
            for cid in (c.get("reaction_block_id", c.get("account_id")),)
            if existing is not None
            and any(o["outcome_id"] == existing["outcome_id"] for o in c["reported_outcomes"])
        ),
        None,
    )
    if existing is not None and old_owner is None:
        prior = expressed_value(existing.get("assignment", {}).get("experiment"))
        if isinstance(prior, Mapping):
            old_owner = prior["object_id"]
    experiment = None
    inherited_experiment = None
    if existing is not None and "experiment" not in words.model_fields_set:
        inherited_experiment = deepcopy(existing.get("assignment", {}).get("experiment"))
        if inherited_experiment is None and old_owner is not None:
            inherited_experiment = present(
                {
                    "object_type": "REACTION_BLOCK"
                    if "reaction_block_id" in comparison_containers[old_owner]
                    else "EXPERIMENTAL_ACCOUNT",
                    "object_id": old_owner,
                },
                existing["evidence_refs"],
            )
    selected = words.experiment if "experiment" in words.model_fields_set else old_owner
    if ("experiment" not in words.model_fields_set and selected is None and existing is None
            and finding.scope != "ANOTHER_EXPERIMENT"):
        owners = {op_owner[oid] for oid in finding.about_ids if oid in op_owner}
        owners.update(oid for oid in finding.about_ids if oid in containers)
        if len(owners) == 1:
            selected = next(iter(owners))
    experiment = resolve_experiment_reference(selected, containers)
    if not (
        set(words.product_ids) <= {c["compound_id"] for c in machine["domain_payload"]["compounds"]}
    ):
        raise ValueError("measurement names an unknown product")
    evidence = references(receipt, finding.value_selection_ids or finding.selection_ids)
    skeleton = None
    if ruling is not None and ruling.primary == "SKELETON":
        if existing is None:
            raise ValueError("SKELETON requires an explicitly identified existing measurement")
        assignment = existing.get("assignment", {})
        prior_experiment = assignment.get("experiment")
        if prior_experiment is None and old_owner is not None:
            prior_experiment = present(
                {
                    "object_type": "REACTION_BLOCK"
                    if "reaction_block_id" in comparison_containers[old_owner]
                    else "EXPERIMENTAL_ACCOUNT",
                    "object_id": old_owner,
                },
                existing["evidence_refs"],
            )
        skeleton = {
            "outcome": existing,
            "experiment": prior_experiment or pending(),
            "measurement_identity": assignment.get("measurement_identity", "CONFIRMED"),
        }
    projected = build_measurement(
        creation_key=creation_key("findings", finding, keys),
        text=words.text,
        kind=words.kind,
        product_ids=words.product_ids,
        # ANOTHER describes a different run; its about objects are comparison
        # context, not an assertion that those operations produced this value.
        operation_ids=[] if finding.scope == "ANOTHER_EXPERIMENT" else [
            oid for oid in finding.about_ids if oid in op_owner
        ],
        measurement=choice,
        experiment=experiment,
        refs=evidence,
        explanation=finding.explanation,
        existing=existing,
        relation=finding.relation,
        primary=primary,
        skeleton=skeleton,
        experiment_reading=inherited_experiment,
    )
    return CompiledMeasurement(
        **asdict(projected),
        contribution_key=creation_key("findings", finding, keys),
        selection_ids=finding.selection_ids,
    )


def resolve_intermediate_links(finding, machine):
    """Resolve only expressed operation links; validation and compilation share scope."""
    words = finding.words
    owners = {
        op["operation_id"]: c.get("reaction_block_id", c.get("account_id"))
        for c in experiment_containers(machine)
        for op in c["operations"]
    }
    producer = (
        words.produced_by.model_dump(mode="json")
        if words.produced_by is not None
        else {"known_operation_ids": list(finding.about_ids)}
    )
    consumer = words.consumed_by.model_dump(mode="json")
    links = [*producer["known_operation_ids"], *consumer["known_operation_ids"], *finding.about_ids]
    if set(links) - owners.keys():
        raise ValueError("local intermediate names an unknown operation")
    containers = {owners[oid] for oid in links}
    if len(containers) != 1:
        raise ValueError("local intermediate requires one explicit experimental container")
    return next(iter(containers)), producer, consumer


def compile_intermediate(finding, receipt, machine, keys):
    from tsdaf.lanes.literature.enrichment_values import CompiledLocalIntermediate
    from tsdaf.lanes.literature.middle_layer import project_structure
    from tsdaf.lanes.literature.stable_keys import public_id

    words = finding.words
    container_id, producer, consumer = resolve_intermediate_links(finding, machine)
    evidence = references(receipt, finding.value_selection_ids or finding.selection_ids)
    key = creation_key("findings", finding, keys)
    value = {
        "local_intermediate_id": public_id("INT", key),
        "produced_by": producer,
        "consumed_by": consumer,
        "author_label": words.label,
        "structure": project_structure(words.structure, evidence, words.structure_format),
        "evidence_refs": evidence,
        "model_interpretations": [
            present(
                model_interpretation(
                    finding.explanation, references(receipt, finding.selection_ids)
                ),
                references(receipt, finding.selection_ids),
            )
        ],
    }
    validate_fact_definition("LocalIntermediate", value, MachineProfile.V1_6)
    return CompiledLocalIntermediate(
        container_id=container_id, value=value, selection_ids=finding.selection_ids
    )
