"""Semantic closure checks for frozen public literature facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any, cast

from tsdaf.contracts.package import STABLE_REVISION_PATTERN

from .expressions import expressed_value
from .objects import (
    experiment_containers,
    experiment_operations,
    local_intermediates,
    reported_outcomes,
)
from .schema import FactRootKind, machine_profile, validate_fact_root


def validate_preparation_target(operation, material, *, operation_ids, material_owners) -> None:
    """The same explicit target relation at write projection and public closure."""
    if operation not in operation_ids:
        raise ValueError("preparation use names an unknown operation")
    if material is not None and material_owners.get(material) != operation:
        raise ValueError("preparation use material must belong to its target operation")


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _walk(value: object) -> Iterable[Mapping[str, object]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list | tuple):
        for child in value:
            yield from _walk(child)


def evidence_refs_in_order(value: object) -> list[tuple[str, str]]:
    """Every observation reference under ``value``, once each, in the order they stand.

    What counts as a reference is decided here and only here; the closure below is the
    set of these, and a caller that hands passages over in route order reads the same
    references in the order the facts give them.
    """
    refs: dict[tuple[str, str], None] = {}
    for item in _walk(value):
        if set(item) == {"observation_id", "source_observation_sha256"}:
            observation_id = item["observation_id"]
            digest = item["source_observation_sha256"]
            if isinstance(observation_id, str) and isinstance(digest, str):
                refs[(observation_id, digest)] = None
    return list(refs)


def _evidence_refs(value: object) -> set[tuple[str, str]]:
    return set(evidence_refs_in_order(value))


def _resolved_value_refs(value: object) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for item in _walk(value):
        if {"state", "evidence_uses"} <= set(item):
            uses = item["evidence_uses"]
            if isinstance(uses, list):
                refs.update(_evidence_refs(uses))
    return refs


def _validate_resolved_values(value: object, *, faithful_mapping: bool = False, typed_validated: bool = False) -> None:
    from .resolved_rules import AUTHORITY_KEYS, resolved_errors
    for item in _walk(value):
        if not set(AUTHORITY_KEYS) <= set(item):
            continue
        errors = resolved_errors(item, faithful_mapping=faithful_mapping,
                                 nullable=faithful_mapping and typed_validated)
        if errors:
            messages = {
                "RESOLVED_VALUE_HASH_MISMATCH": "resolved value hash does not match authority fields",
                "RESOLVED_VALUE_STATE": f"resolved value {item['state']} state is inconsistent",
                "CORE_EVIDENCE_REQUIRED": "resolved core value requires SUPPORT evidence",
            }
            raise ValueError(messages.get(errors[0], "resolved value contract: " + errors[0]))


def _validate_observation(observation: Mapping[str, object]) -> None:
    from .observation_roles import ROLE_PAYLOADS as role_payloads
    role = observation["semantic_role"]
    payload = observation["payload"]
    anchor = observation["anchor"]
    verification = observation["verification"]
    if role in role_payloads and payload["payload_kind"] not in role_payloads[role]:  # type: ignore[index]
        raise ValueError("observation role and payload kind are inconsistent")
    if role in {"OTHER_TEXT", "OTHER_VISUAL"} and not observation["producer_note"]:
        raise ValueError("OTHER observation requires producer_note")
    if anchor["document_revision_id"] != observation["document_revision_id"]:  # type: ignore[index]
        raise ValueError("observation anchor document revision is inconsistent")
    if verification["verified_anchor_sha256"] != _canonical_sha256(anchor):  # type: ignore[index]
        raise ValueError("observation verified anchor hash is inconsistent")
    # E83 (the E77 audit condition): an observation keyed by its anchor is the same
    # bytes in every run that reads its site, so nothing about a run may be in it --
    # its document revision is the stable one and no execution is named.
    if "anchor_key" in observation:
        revision = str(observation["document_revision_id"])
        if STABLE_REVISION_PATTERN.fullmatch(revision) is None:
            raise ValueError("keyed observation must carry a stable document revision")
        if "originator" in observation or "execution_id" in verification:  # type: ignore[operator]
            raise ValueError("keyed observation must not carry a run coordinate")


def required_fact_observation_refs(machine: Mapping[str, object], *, public_science=None) -> set[tuple[str, str]]:
    """The exact evidence closure consumed by both assembly and validation."""
    domain = machine["domain_payload"]
    assert isinstance(domain, Mapping)
    nested_domain = dict(domain)
    nested_domain.pop("source_observation_refs", None)
    from .public_science import record_observation_refs
    return (
        _evidence_refs(nested_domain)
        | _evidence_refs(machine["blocking_conflicts"])
        | _evidence_refs(machine["unresolved_items"])
        | _evidence_refs(machine.get("coverage_assessments", []))
        | record_observation_refs(public_science or {})
    )


def _ensure_reference_closure(machine: Mapping[str, object], *, public_science=None) -> None:
    domain = machine["domain_payload"]
    assert isinstance(domain, Mapping)
    if machine["paper_id"] != domain["paper_id"]:
        raise ValueError("machine paper_id does not match domain paper_id")
    top_refs = _evidence_refs(machine["source_observation_refs"])
    domain_refs = _evidence_refs(domain["source_observation_refs"])
    expected_refs = required_fact_observation_refs(machine, public_science=public_science)
    if top_refs != domain_refs or top_refs != expected_refs:
        raise ValueError("machine source observation refs do not close the fact bundle")
    validate_scientific_structure(machine)


def validate_scientific_structure(machine: Mapping[str, object], *, profile=None) -> None:
    """Public object and relation closure shared by preview and publication.

    Evidence artifact closure is checked separately at publication. This function
    neither supplies scientific values nor judges completeness of model reading.
    The caller has already validated the public field shapes.
    """
    profile = profile or machine_profile(machine)
    domain = machine["domain_payload"]
    assert isinstance(domain, Mapping)
    if machine["paper_id"] != domain["paper_id"]:
        raise ValueError("machine paper_id does not match domain paper_id")
    compounds = {
        item["compound_id"]
        for item in domain["compounds"]
        if isinstance(item, Mapping) and isinstance(item.get("compound_id"), str)
    }
    blocks = {
        item["reaction_block_id"]: item
        for item in domain["reaction_blocks"]
        if isinstance(item, Mapping) and isinstance(item.get("reaction_block_id"), str)
    }
    if len(compounds) != len(domain["compounds"]) or len(blocks) != len(domain["reaction_blocks"]):
        raise ValueError("machine contains duplicate stable IDs")
    outcome_ids: set[str] = set()
    all_operation_ids: set[str] = set()
    outcome_operation_refs: list[set[str]] = []
    for block in experiment_containers(machine):
        for endpoint in (block["input_compound_ids"], block["output_compound_ids"]):
            if isinstance(endpoint, Mapping) and endpoint.get("state") == "PRESENT":
                values = endpoint.get("value")
                if not isinstance(values, list) or not set(values) <= compounds:
                    raise ValueError("machine has a dangling compound reference")
        operation_ids = {
            item["operation_id"]
            for item in block["operations"]
            if isinstance(item, Mapping) and isinstance(item.get("operation_id"), str)
        }
        if len(operation_ids) != len(block["operations"]):
            raise ValueError("machine contains duplicate operation IDs")
        if all_operation_ids.intersection(operation_ids):
            raise ValueError("machine contains duplicate paper-global operation IDs")
        all_operation_ids.update(operation_ids)
        sequence = [item["sequence_index"] for item in block["operations"]]
        if sequence != list(range(1, len(sequence) + 1)):
            raise ValueError("machine operation sequence indexes are not contiguous")
        for outcome in block["reported_outcomes"]:
            if not isinstance(outcome, Mapping):
                continue
            outcome_id = outcome.get("outcome_id")
            if not isinstance(outcome_id, str) or outcome_id in outcome_ids:
                raise ValueError("machine contains duplicate outcome IDs")
            outcome_ids.add(outcome_id)
            operation_refs = outcome.get("operation_ids")
            if isinstance(operation_refs, Mapping) and operation_refs.get("state") == "PRESENT":
                # An aggregate yield names the operations it was measured over, in
                # any block of the paper (E57): the reference closes over every
                # operation, checked once all blocks are known.
                values = operation_refs.get("value")
                if not isinstance(values, list):
                    raise ValueError("machine has a dangling operation reference")
                outcome_operation_refs.append(set(values))
            product_refs = outcome.get("product_compound_ids")
            if isinstance(product_refs, Mapping) and product_refs.get("state") == "PRESENT":
                values = product_refs.get("value")
                if not isinstance(values, list) or not set(values) <= compounds:
                    raise ValueError("machine has a dangling outcome product reference")
    for referenced in outcome_operation_refs:
        if not referenced <= all_operation_ids:
            raise ValueError("machine has a dangling operation reference")
    route_ids: set[str] = set()
    for route in domain["routes"]:
        assert isinstance(route, Mapping)
        route_id = route["route_id"]
        if not isinstance(route_id, str) or route_id in route_ids:
            raise ValueError("machine contains duplicate route IDs")
        route_ids.add(route_id)
        if not set(route["compound_ids"]) <= compounds or not set(
            route["reaction_block_ids"]
        ) <= set(blocks):
            raise ValueError("machine has a dangling route reference")
        route_blocks = set(route["reaction_block_ids"])
        for target_path in route["target_paths"]:
            assert isinstance(target_path, Mapping)
            if target_path["target_compound_id"] not in compounds:
                raise ValueError("machine has a dangling target-path compound reference")
            path_blocks = target_path["reaction_block_ids"]
            if isinstance(path_blocks, Mapping) and path_blocks.get("state") == "PRESENT":
                values = path_blocks.get("value")
                if not isinstance(values, list) or not set(values) <= route_blocks:
                    raise ValueError("machine has a dangling target-path block reference")
        for assertion in route["target_assertions"]:
            assert isinstance(assertion, Mapping)
            if assertion["target_compound_id"] not in compounds:
                raise ValueError("machine has a dangling target assertion compound reference")
    _ensure_context_closure(machine, compounds, blocks, all_operation_ids)
    _ensure_coverage_closure(machine)
    _validate_resolved_values(domain, faithful_mapping=profile.faithful_mapping, typed_validated=True)
    validate_wrapping(machine, profile=profile)
    if profile.expression:
        validate_expression_relations(machine, profile=profile)
    if "experimental_accounts" in domain:
        validate_open_expression_relations(machine, profile=profile)


def _unique_ids(items: object, key: str, label: str) -> set[str]:
    rows = list(items) if isinstance(items, list) else []
    ids = [
        str(item[key])
        for item in rows
        if isinstance(item, Mapping) and isinstance(item.get(key), str)
    ]
    if len(ids) != len(rows) or len(set(ids)) != len(ids):
        raise ValueError(f"machine contains duplicate {label} IDs")
    return set(ids)


def _ensure_context_closure(
    machine: Mapping[str, object],
    compounds: set[str],
    blocks: Mapping[str, Mapping[str, object]],
    operation_ids: set[str],
) -> None:
    """The references ledger E47 added close too: a block's context lists name
    narratives, a card names compounds, a conflict and an unresolved item name the
    object they are about, and every one of those IDs is unique (E52)."""
    domain = machine["domain_payload"]
    assert isinstance(domain, Mapping)
    narratives = _unique_ids(domain.get("paper_narratives", []), "narrative_id", "narrative")
    cards = domain.get("characterization_records", [])
    _unique_ids(cards, "characterization_id", "characterization")
    for card in cards:
        if isinstance(card, Mapping) and not set(card.get("compound_ids", ())) <= compounds:
            raise ValueError("machine has a dangling characterization compound reference")
    for block in blocks.values():
        for field in (
            "mechanism_context_ids",
            "optimization_context_ids",
            "failed_attempt_context_ids",
        ):
            listed = block.get(field, [])
            if not isinstance(listed, list) or not set(listed) <= narratives:
                raise ValueError(f"machine has a dangling {field} reference")
            if len(set(listed)) != len(listed):
                raise ValueError(f"machine repeats a {field} reference")
    for compound in domain["compounds"]:
        assert isinstance(compound, Mapping)
        for field in ("predecessor_ids", "successor_ids"):
            if not set(compound.get(field, ())) <= compounds:
                raise ValueError(f"machine has a dangling compound {field} reference")
    assertion_ids = [
        assertion["assertion_id"]
        for route in domain["routes"]
        if isinstance(route, Mapping)
        for assertion in route["target_assertions"]
        if isinstance(assertion, Mapping)
    ]
    if len(set(assertion_ids)) != len(assertion_ids):
        raise ValueError("machine contains duplicate assertion IDs")
    known = {
        "COMPOUND": compounds,
        "REACTION_BLOCK": set(blocks),
        "OPERATION": operation_ids,
        "ROUTE": {route["route_id"] for route in domain["routes"] if isinstance(route, Mapping)},
        "PAPER": {machine["paper_id"]},
    }
    unresolved = machine["unresolved_items"]
    assert isinstance(unresolved, list)
    _unique_ids(unresolved, "unresolved_item_id", "unresolved item")
    for item in unresolved:
        assert isinstance(item, Mapping)
        if item["subject_id"] not in known.get(str(item["scope"]), set()):
            raise ValueError("machine has an unresolved item about an unknown subject")
    validate_narrative_subjects(unresolved, machine)
    # The conflicts live in the domain payload (the contract puts them there); E52 read
    # the root, where there are none, so this check ran on an empty list for a week.
    conflicts = domain["field_conflicts"]
    assert isinstance(conflicts, list)
    _unique_ids(conflicts, "conflict_id", "conflict")
    targets = conflict_target_ids(machine)
    for conflict in conflicts:
        assert isinstance(conflict, Mapping)
        target_type = str(conflict["target_object_type"])
        if target_type not in targets:
            raise ValueError(
                f"machine has a field conflict about a {target_type}, which has no ids"
            )
        if conflict["target_object_id"] not in targets[target_type]:
            raise ValueError("machine has a field conflict about an unknown object")
    # The root's blocking list is a view of the domain's blocking conflicts, exactly.
    blocking = [c for c in conflicts if isinstance(c, Mapping) and c["blocking"]]
    if machine["blocking_conflicts"] != blocking:
        raise ValueError("machine blocking_conflicts differ from the domain's blocking conflicts")


# What each SI state says about the result that carries it (E77, step 5): whether an SI
# package is named, and whether `coverage` may say it was read. NONE and NOT_APPLICABLE
# name none; a registered SI is named, and only a read one is covered.
SI_STATES: dict[str, tuple[bool, str]] = {
    "NONE": (False, "MAIN_ONLY"),
    "REGISTERED_UNREAD": (True, "MAIN_ONLY"),
    "REGISTERED_READ": (True, "MAIN_SI"),
    "NOT_APPLICABLE": (False, "MAIN_ONLY"),
}
# An unresolved reason that speaks of the SI is only true in one SI state: an item can
# wait for an SI the Host has, and only lack one the Host does not have.
SI_REASON_STATES: dict[str, str] = {
    "WAIT_FOR_SI": "REGISTERED_UNREAD",
    "SI_UNAVAILABLE": "NONE",
    "NO_EXPERIMENTAL_SOURCE": "NONE",
}


def _ensure_coverage_closure(machine: Mapping[str, object]) -> None:
    """E77: a coverage assessment is about public objects of the result, and the three
    SI states agree with what the result says it read. Both are absent from results
    sealed before E77, which stay valid."""
    domain = machine["domain_payload"]
    assert isinstance(domain, Mapping)
    public_ids: set[str] = {str(machine["paper_id"])}
    for ids in conflict_target_ids(machine).values():
        public_ids |= ids
    public_ids |= {
        str(route["route_id"])
        for route in cast(list[object], domain["routes"])
        if isinstance(route, Mapping)
    }
    for assessment in cast(list[object], machine.get("coverage_assessments", [])):
        assert isinstance(assessment, Mapping)
        if not set(cast(list[str], assessment["subject_ids"])) <= public_ids:
            raise ValueError("machine has a coverage assessment about an unknown subject")
    state = machine.get("si_registration")
    if state is None:
        return
    names_si, coverage = SI_STATES[str(state)]
    if bool(machine["si_package_revision_ids"]) != names_si:
        raise ValueError(f"machine si_registration {state} disagrees with its SI packages")
    if machine["coverage"] != coverage:
        raise ValueError(f"machine si_registration {state} disagrees with its coverage")
    for item in cast(list[object], machine["unresolved_items"]):
        assert isinstance(item, Mapping)
        reason = str(item["reason_code"])
        expected = SI_REASON_STATES.get(reason)
        if not machine_profile(machine).faithful_mapping and expected is not None and expected != state:
            raise ValueError(f"machine has a {reason} item but si_registration is {state}")
        if reason == "NO_EXPERIMENTAL_SOURCE" and item["scope"] != "PAPER":
            raise ValueError("machine NO_EXPERIMENTAL_SOURCE is a PAPER-scoped item")


def fact_target_records(
    machine: Mapping[str, object],
) -> dict[str, dict[str, Mapping[str, object]]]:
    """The ids a field conflict may be about, per target type the contract lists.

    The nine types are the contract's enum; each resolves here to the ids the result
    carries for it, so a type added to the contract without a resolver fails the seam
    test rather than passing unchecked.
    """
    domain = machine["domain_payload"]
    assert isinstance(domain, Mapping)
    blocks = [b for b in cast(list[object], domain["reaction_blocks"]) if isinstance(b, Mapping)]
    routes = [r for r in cast(list[object], domain["routes"]) if isinstance(r, Mapping)]

    def ids(items: object, key: str) -> dict[str, Mapping[str, object]]:
        return {
            str(item[key]): item for item in cast(list[object], items) if isinstance(item, Mapping)
        }

    return {
        "COMPOUND": ids(domain["compounds"], "compound_id"),
        "REACTION_BLOCK": ids(blocks, "reaction_block_id"),
        "OPERATION": {
            str(op["operation_id"]): op for op in experiment_operations(machine)
        },
        "REPORTED_OUTCOME": {
            str(outcome["outcome_id"]): outcome for outcome in reported_outcomes(machine)
        },
        "CHARACTERIZATION": ids(domain.get("characterization_records", []), "characterization_id"),
        "PAPER_NARRATIVE": ids(domain.get("paper_narratives", []), "narrative_id"),
        "BIOLOGICAL_FINDING": ids(domain.get("biological_findings", []), "biological_finding_id"),
        "TARGET_PATH": {
            str(path["target_path_id"]): path
            for route in routes
            for path in cast(list[object], route["target_paths"])
            if isinstance(path, Mapping)
        },
        "TARGET_ASSERTION": {
            str(assertion["assertion_id"]): assertion
            for route in routes
            for assertion in cast(list[object], route["target_assertions"])
            if isinstance(assertion, Mapping)
        },
    }


def conflict_target_ids(machine: Mapping[str, object]) -> dict[str, set[str]]:
    return {kind: set(records) for kind, records in fact_target_records(machine).items()}


def validate_fact_bundle(
    machine: Mapping[str, object], observations: Iterable[Mapping[str, object]], *, read_public=None
) -> None:
    """Validate the strict schema plus evidence and stable-ID closure for public facts."""

    if validate_fact_root(machine) is not FactRootKind.MACHINE:
        raise ValueError("machine must be a MachinePaperResult")
    public_science = None
    if machine_profile(machine).faithful_mapping:
        from .public_science import load_public_science
        def unavailable(ref):
            raise ValueError('public scientific artifacts require their immutable bytes')
        public_science = load_public_science(machine, read_public or unavailable)
    checked_observations = tuple(observations)
    observation_map: dict[str, tuple[str, Mapping[str, object]]] = {}
    for observation in checked_observations:
        if validate_fact_root(observation) is not FactRootKind.OBSERVATION:
            raise ValueError("observations must contain only SourceObservation roots")
        _validate_observation(observation)
        observation_id = observation["observation_id"]
        assert isinstance(observation_id, str)
        if observation_id in observation_map:
            raise ValueError("observations contain duplicate observation IDs")
        observation_map[observation_id] = (_canonical_sha256(observation), observation)

    expected = _evidence_refs(machine)
    source_observation_refs = machine["source_observation_refs"]
    assert isinstance(source_observation_refs, list)
    listed = {
        (item["observation_id"], item["source_observation_sha256"])
        for item in source_observation_refs
        if isinstance(item, Mapping)
    }
    if expected != listed:
        raise ValueError("machine source observation refs do not close nested evidence")
    actual = {(identifier, digest) for identifier, (digest, _) in observation_map.items()}
    if listed != actual:
        if listed - actual:
            raise ValueError("machine has dangling observation references")
        raise ValueError("observations include an extra unreferenced observation")
    if _resolved_value_refs(machine) - actual:
        raise ValueError("resolved value evidence does not close to observations")
    _ensure_reference_closure(machine, public_science=public_science)


def _declared_values(field: Mapping[str, Any]) -> tuple[Any, ...]:
    """Validate known values and actual alternatives separately, without choosing one."""
    return (
        *((field["value"],) if field.get("state") == "PRESENT" else ()),
        *(candidate["value"] for candidate in field.get("candidates", ())),
    )


def validate_open_expression_relations(machine: Mapping[str, Any], *, profile=None) -> None:
    """Structural references for the forward expression surface, not scientific support.

    The design generator and publication use this same rule. No temporal edges,
    same-measurement claims or experiment assignments are inferred here.
    """
    profile = profile or machine_profile(machine)
    domain = machine["domain_payload"]
    containers = experiment_containers(machine)
    operations = experiment_operations(machine)
    outcomes = reported_outcomes(machine)
    compound_ids = {row["compound_id"] for row in domain["compounds"]}
    block_ids = {row["reaction_block_id"] for row in domain["reaction_blocks"]}
    account_ids = {row["account_id"] for row in domain.get("experimental_accounts", ())}
    operation_ids = {op["operation_id"] for op in operations}
    from .resolved_rules import PublicIdentityRegistry
    identities = PublicIdentityRegistry()
    define = identities.define

    for row in domain["compounds"]:
        define(row["compound_id"])
    for container in containers:
        define(container.get("reaction_block_id", container.get("account_id")))
        for key in ("input_compound_ids", "output_compound_ids"):
            field = container[key]
            for values in _declared_values(field):
                if values is not None and not set(values) <= compound_ids:
                    raise ValueError("experiment endpoint has a dangling compound reference")
    material_owners: dict[str, str] = {}
    for op in operations:
        define(op["operation_id"])
        stages = {row["stage_id"] for row in op.get("stages", ())}
        for stage in op.get("stages", ()):
            define(stage["stage_id"])
        for material in op["materials"].get("value") or ():
            if "material_use_id" in material:
                if not profile.faithful_mapping:
                    define(material["material_use_id"])
                material_owners[material["material_use_id"]] = op["operation_id"]
        from .resolved_rules import validate_declared_precedence
        validate_declared_precedence(stages, op.get("stage_precedence", ()),
            before_key="before_stage_id", after_key="after_stage_id", label="stage")
    if profile.faithful_mapping:
        for route in domain["routes"]:
            define(route["route_id"])
            for path in route.get("target_paths", ()):
                define(path["target_path_id"])
            for assertion in route.get("target_assertions", ()):
                define(assertion["assertion_id"])
        for collection, key in (("paper_narratives", "narrative_id"),
                                ("characterization_records", "characterization_id")):
            for row in domain.get(collection, ()):
                define(row[key])
        for container in containers:
            own = {op["operation_id"] for op in container["operations"]}
            from .resolved_rules import validate_declared_precedence
            validate_declared_precedence(own, container.get("operation_precedence", ()),
                before_key="before_operation_id", after_key="after_operation_id", label="operation")
        for op in operations:
            from .resolved_rules import material_occurrences
            for branch, rows in material_occurrences(op["materials"]):
                for row in rows:
                    material = row.get("material_use_id")
                    if material is not None:
                        define(material, exclusive_owner=("MATERIAL", op["operation_id"]), branch=branch)
                    for quantity in row.get("additional_reported_quantities", ()):
                        # Without an explicit material identity no cross-candidate
                        # sameness can be established from row position or content.
                        owner = ("QUANTITY", op["operation_id"], material) if material else None
                        define(quantity["quantity_id"], exclusive_owner=owner, branch=branch)
    for outcome in outcomes:
        define(outcome["outcome_id"])
        if profile.faithful_mapping:
            for quantity in outcome["quantities"]:
                define(quantity["quantity_id"])
        for key, allowed in (
            ("operation_ids", operation_ids),
            ("product_compound_ids", compound_ids),
        ):
            field = outcome[key]
            for values in _declared_values(field):
                if values is not None and not set(values) <= allowed:
                    raise ValueError("outcome has a dangling scientific reference")
    for outcome in domain.get("unassigned_outcomes", ()):
        assignment = outcome["assignment"]
        expression = assignment["experiment"]
        unified = profile.unified_expression
        experiments = _declared_values(expression) if unified else (
            (expression,) if expression is not None else ()
        )
        for experiment in experiments:
            allowed = {"REACTION_BLOCK": block_ids, "EXPERIMENTAL_ACCOUNT": account_ids}.get(
                experiment["object_type"], set()
            )
            if experiment["object_id"] not in allowed:
                raise ValueError("outcome assignment names an unknown experiment")
        known_experiment = expressed_value(expression) if unified else expression
        if (
            known_experiment is not None
            and known_experiment["object_type"] == "EXPERIMENTAL_ACCOUNT"
        ):
            account = next(
                a for a in domain["experimental_accounts"]
                if a["account_id"] == known_experiment["object_id"]
            )
            own = {op["operation_id"] for op in account["operations"]}
            for values in _declared_values(outcome["operation_ids"]):
                if values is not None and not set(values) <= own:
                    raise ValueError("outcome operations must belong to its assigned account")
    for account in domain.get("experimental_accounts", ()):
        own_operations = {op["operation_id"] for op in account["operations"]}
        for outcome in account["reported_outcomes"]:
            field = outcome["operation_ids"]
            for values in _declared_values(field):
                if not set(values) <= own_operations:
                    raise ValueError("account outcome operations must belong to that experiment")
        for use in account.get("preparation_uses", ()):
            validate_preparation_target(use["target_operation_id"], use.get("target_material_use_id"),
                                        operation_ids=operation_ids, material_owners=material_owners)
    for route in domain["routes"]:
        if not set(route["reaction_block_ids"]) <= block_ids:
            raise ValueError("independent accounts are not route reaction blocks")
    if profile.unified_expression:
        for intermediate in local_intermediates(machine):
            define(intermediate["local_intermediate_id"])
        for container in containers:
            own = {op["operation_id"] for op in container["operations"]}
            for intermediate in container.get("local_intermediates", ()):
                for relation in ("produced_by", "consumed_by"):
                    if not set(intermediate[relation]["known_operation_ids"]) <= own:
                        raise ValueError("local intermediate links must stay within its experiment")


def validate_wrapping(machine: Mapping[str, object], *, profile=None) -> None:
    profile = profile or machine_profile(machine)
    domain = cast(Mapping[str, object], machine["domain_payload"])
    targets = fact_target_records(machine)
    for operation in targets["OPERATION"].values():
        field = cast(Mapping[str, object], operation["materials"])
        values = [
            field.get("value"),
            *(
                candidate.get("value")
                for candidate in cast(list[Mapping[str, object]], field.get("candidates", []))
            ),
        ]
        for value in values:
            for material in cast(list[Mapping[str, object]], value or []):
                # v1.7 separates model names from literal author wording. Never
                # fabricate a verbatim name just to satisfy the legacy coupling.
                if not profile.faithful_mapping and "names" in material and (
                    material.get("compound_id") is not None
                    or material.get("verbatim_name") not in cast(list[str], material["names"])
                ):
                    raise ValueError(
                        "material names require an unbound identity and its public name"
                    )
    for marker in cast(list[Mapping[str, object]], domain.get("review_markers", [])):
        kind = str(marker["target_object_type"])
        record = targets.get(kind, {}).get(str(marker["target_object_id"]))
        key = str(marker["field_key"])
        if record is None or key not in record:
            raise ValueError("review marker names an unknown target or field")
        if "material_index" in marker:
            if kind != "OPERATION" or key != "materials":
                raise ValueError("review material_index requires OPERATION materials")
            field = cast(Mapping[str, object], record[key])
            values = cast(list[object], field.get("value") or [])
            if not 1 <= cast(int, marker["material_index"]) <= len(values):
                raise ValueError("review marker material_index is outside the main value")


def validate_expression_relations(machine: Mapping[str, object], *, profile=None) -> None:
    profile = profile or machine_profile(machine)
    """Reference closure for v1.3, shared by the pack and production validator.

    These checks resolve declared relationships; they never sum amounts or judge
    whether two source readings are scientifically consistent.
    """
    domain = cast(dict[str, Any], machine["domain_payload"])
    operation_ids = {op["operation_id"] for op in experiment_operations(machine)}
    subject_ids = {
        "COMPOUND": {c["compound_id"] for c in domain["compounds"]},
        "REACTION_BLOCK": {b["reaction_block_id"] for b in domain["reaction_blocks"]},
        "OPERATION": operation_ids,
        "REPORTED_OUTCOME": {
            o["outcome_id"] for b in domain["reaction_blocks"] for o in b["reported_outcomes"]
        },
        "ROUTE": {r["route_id"] for r in domain["routes"]},
    }
    for outcome in reported_outcomes(machine):
        for source in outcome.get("source_statements", []):
            if not set(source.get("product_ids", [])) <= subject_ids["COMPOUND"]:
                raise ValueError("outcome source statement has a dangling product reference")
    validate_narrative_subjects(domain.get("paper_narratives", []), machine)
    for block in experiment_containers(machine):
        for operation in block["operations"]:
            field = operation["materials"]
            for rows in [
                field.get("value"),
                *(c.get("value") for c in field.get("candidates", [])),
            ]:
                for material in rows if isinstance(rows, list) else []:
                    for reading in material.get("source_readings", []):
                        if (
                            reading.get("compound_id") is not None
                            and reading["compound_id"] not in subject_ids["COMPOUND"]
                        ):
                            raise ValueError(
                                "material source reading has a dangling compound reference"
                            )
            stages = {s["stage_id"] for s in operation.get("stages", [])}
            if len(stages) != len(operation.get("stages", [])):
                raise ValueError("duplicate stage IDs within operation")

            def check_stage(
                row: Mapping[str, Any], stage_ids: set[str] = stages, *, condition: bool = False
            ) -> None:
                if "stage_id" in row:
                    if row["stage_id"] not in stage_ids:
                        raise ValueError("stage reference must belong to the same operation")
                    extent = row.get("extent")
                    if condition and profile.unified_expression:
                        extent = expressed_value(extent)
                        if extent in (None, "UNRESOLVED"):
                            return
                    if extent != "WITHIN_OWNER":
                        raise ValueError("a stage reference requires WITHIN_OWNER extent")

            for note in operation.get("other_conditions", []):
                check_stage(note, condition=True)
                if "shared_unit_text" in note and "sequence" not in note:
                    raise ValueError("shared unit requires an explicit sequence")
            for source in operation.get("procedure_sources", []):
                check_stage(source)
                target = source.get("operation_id")
                unit = source.get("unit_block_id")
                if source["kind"] == "OPERATION":
                    if (
                        target not in operation_ids
                        or target == operation["operation_id"]
                        or unit is not None
                    ):
                        raise ValueError("procedure source requires another existing operation")
                elif source["kind"] == "GENERAL_METHODS_UNIT":
                    if target is not None or not unit:
                        raise ValueError("general-methods source requires only its unit")
                elif target is not None or unit is not None:
                    raise ValueError("external procedure source cannot name a local target")
            field = operation["materials"]
            arrays = [field.get("value"), *(c.get("value") for c in field.get("candidates", []))]
            for rows in arrays:
                if not isinstance(rows, list):
                    continue
                validate_material_relations(rows, stages, faithful_mapping=profile.faithful_mapping)


def validate_material_relations(rows: list[dict[str, Any]], stage_ids: set[str], *, faithful_mapping: bool = False) -> None:
    """Validate only declared material links, without inferring scientific correspondence."""
    from .material_relation_basis import (
        MATERIAL_STAGE_EXTENT, MATERIAL_TOTAL_LEVEL, MATERIAL_USE_LEVEL,
    )
    for index, row in enumerate(rows, 1):
        if faithful_mapping and 'relation_field_bases' in row:
            from jsonschema import Draft202012Validator
            from .material_relation_basis import material_relation_rules
            if not Draft202012Validator(material_relation_rules()).is_valid(row):
                raise ValueError('relation provenance does not match the declared fields')
        if "stage_id" in row:
            if row["stage_id"] not in stage_ids:
                raise ValueError("stage reference must belong to the same operation")
            if row.get("extent") != MATERIAL_STAGE_EXTENT:
                raise ValueError(f"a stage reference requires WITHIN_OWNER extent; material {index} names a stage")
        if any(
            k in row for k in ("reporting_level", "extent", "stage_id", "summary_material_index")
        ):
            if not faithful_mapping and not row.get("relation_evidence_refs"):
                raise ValueError("material relationship requires its source evidence")
        target = row.get("summary_material_index")
        if target is None:
            continue
        if not isinstance(target, int) or not 1 <= target <= len(rows) or target == index:
            raise ValueError("material summary must name another row in this operation")
        summary = rows[target - 1]
        if row.get("reporting_level") != MATERIAL_USE_LEVEL or summary.get("reporting_level") != MATERIAL_TOTAL_LEVEL:
            raise ValueError(
                f"material summary relationship must link {MATERIAL_USE_LEVEL} to {MATERIAL_TOTAL_LEVEL}; "
                f"material {index} declares a summary link but its source or target level does not match. "
                "This link means actual use, not an arbitrary quantity relationship; the host cannot infer use."
            )
        if "summary_material_index" in summary:
            raise ValueError("a material summary cannot itself be a detail")


def validate_narrative_subjects(narratives: Any, machine: Mapping[str, Any]) -> None:
    """Validate explicit ownership identically before save and after publication."""
    domain = machine["domain_payload"]
    identifiers = {
        "COMPOUND": {c["compound_id"] for c in domain["compounds"]},
        "REACTION_BLOCK": {b["reaction_block_id"] for b in domain["reaction_blocks"]},
        "OPERATION": {o["operation_id"] for o in experiment_operations(machine)},
        "REPORTED_OUTCOME": {o["outcome_id"] for o in reported_outcomes(machine)},
        "EXPERIMENTAL_ACCOUNT": {a["account_id"] for a in domain.get("experimental_accounts", ())},
        "ROUTE": {r["route_id"] for r in domain["routes"]},
    }
    for narrative in narratives:
        for subject in narrative.get("subjects", []):
            if subject["object_id"] not in identifiers.get(subject["object_type"], set()):
                raise ValueError(
                    "narrative subject must name an existing object of its declared type"
                )
