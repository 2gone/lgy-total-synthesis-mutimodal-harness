"""The containers of public experiment facts, shared by validation and application.

No object is moved or assigned here. Collection membership is supplied scientific
structure; an independent account is never returned as a route reaction block.
"""

from collections.abc import Mapping
from typing import Any


# Public review/conflict target names and their scientific definitions.
PUBLIC_TARGET_DEFINITIONS = {
    'COMPOUND':'CompoundRecord','REACTION_BLOCK':'ReactionBlock','OPERATION':'Operation',
    'REPORTED_OUTCOME':'ReportedOutcome','TARGET_PATH':'TargetPath','TARGET_ASSERTION':'TargetAssertion',
    'PAPER_NARRATIVE':'PaperNarrative','CHARACTERIZATION':'CharacterizationRecord',
    'BIOLOGICAL_FINDING':'BiologicalFinding','EXPERIMENTAL_ACCOUNT':'ExperimentalAccount',
}


def registered_conflict_fields(definition):
    """Exact registered pairs; a field-name union is not a per-target contract."""
    allowed=definition('FieldConflict')['properties']
    return {(p['target_object_type']['const'],p['field_key']['const']):
            PUBLIC_TARGET_DEFINITIONS[p['target_object_type']['const']]
        for branch in definition('FieldReviewView')['oneOf'] for p in [branch['properties']]
        if p['target_object_type']['const'] in allowed['target_object_type']['enum']
        and p['field_key']['const'] in allowed['field_key']['enum']}


def registered_field_value_schema(definition, object_definition, field):
    from copy import deepcopy
    from .resolved_rules import AUTHORITY_KEYS
    fields=definition(object_definition)['properties']
    if object_definition=='ReportedOutcome' and field not in fields:
        return deepcopy(definition('LegacyReportedOutcomeConflictValues')['properties'][field])
    schema=deepcopy(fields[field])
    while '$ref' in schema or 'allOf' in schema and 'properties' not in schema:
        schema=(deepcopy(definition(schema['$ref'].rsplit('/',1)[-1])) if '$ref' in schema
                else deepcopy(schema['allOf'][0]))
    return schema['properties']['value'] if set(AUTHORITY_KEYS)<=schema.get('properties',{}).keys() else schema


def experiment_containers(machine: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    domain = machine["domain_payload"]
    return (*domain.get("reaction_blocks", ()), *domain.get("experimental_accounts", ()))


def experiment_operations(machine: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        op for container in experiment_containers(machine) for op in container["operations"]
    )


def reported_outcomes(machine: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return (
        *(outcome for c in experiment_containers(machine) for outcome in c["reported_outcomes"]),
        *machine["domain_payload"].get("unassigned_outcomes", ()),
    )


def local_intermediates(machine: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """The v1.6 collection is authoritative; older scalar values remain readable."""
    records = []
    for container in experiment_containers(machine):
        if "local_intermediates" in container:
            records.extend(container["local_intermediates"])
        else:
            for operation in container["operations"]:
                field = operation.get("operation_intermediate") or {}
                if field.get("state") == "PRESENT" and isinstance(field.get("value"), Mapping):
                    records.append(field["value"])
    return tuple(records)


def initialize_open_collections(domain: dict[str, Any]) -> None:
    """Fresh and upgraded v1.5 domains declare empty collections, never invented facts.

    Existing contents must survive profile-compatible publications. Callers select
    this only for the explicit open-expression profile; older roots stay unchanged.
    """
    domain.setdefault("experimental_accounts", [])
    domain.setdefault("unassigned_outcomes", [])
