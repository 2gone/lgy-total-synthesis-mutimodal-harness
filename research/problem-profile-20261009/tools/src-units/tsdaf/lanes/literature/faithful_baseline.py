"""Explicit representation-only baseline upgrade before a new recording task.

The caller seals this derivative as the new execution input. Existing sealed
receipts continue to name their original baseline; they are never relabelled.
"""
from copy import deepcopy

from tsdaf.contracts.literature.objects import reported_outcomes
from tsdaf.contracts.literature.public_ids import public_id
from tsdaf.contracts.literature.schema import (
    MACHINE_SCHEMA_VERSIONS, MachineProfile, machine_profile, validate_fact_definition, contract_pack_pins,
)
from tsdaf.contracts.literature.validation import validate_open_expression_relations


def faithful_baseline(machine):
    profile=machine_profile(machine)
    if profile not in (MachineProfile.V1_6,MachineProfile.V1_7):
        raise ValueError('faithful baseline requires an explicit v1.6 or v1.7 input')
    validate_fact_definition('MachinePaperResult',machine,profile)
    result=deepcopy(machine)
    if profile==MachineProfile.V1_6:
        for outcome in reported_outcomes(result):
            # One old typed quantity becomes one new typed quantity. Every wrapper
            # and qualification is copied exactly; no outcome prose is parsed.
            outcome['quantities']=[{
                'quantity_id':public_id('QTY',machine['paper_id']+'|v1.6-reported-quantity|'+outcome['outcome_id']),
                **{key:outcome.pop(key) for key in ('kind','reported_value','basis')},
            }]
        result['schema_version']=MACHINE_SCHEMA_VERSIONS[MachineProfile.V1_7]
    result['schema_golden_sha256']=contract_pack_pins(MachineProfile.V1_7)['schema_candidate_sha256']
    validate_fact_definition('MachinePaperResult',result,MachineProfile.V1_7)
    validate_open_expression_relations(result)
    return result


def compilation_baseline(machine):
    """Prepare identities only inside a structural compilation, not while taking notes.

    The sealed input remains the original Machine. A legacy optional identity is
    derived from that exact snapshot and occurrence, never from scientific content
    similarity or the new submission's birth. The resulting candidate is published
    only if the complete explicit compilation succeeds.
    """
    import hashlib
    from tsdaf.contracts.schema import canonical_json_bytes
    from tsdaf.lanes.literature.scientific_field_bindings import iter_bound_objects
    validate_fact_definition("MachinePaperResult", machine, MachineProfile.V1_7)
    result = deepcopy(machine)
    original_sha = hashlib.sha256(canonical_json_bytes(machine)).hexdigest()
    for row in iter_bound_objects(result):
        if row.identity is None:
            key = canonical_json_bytes([original_sha,row.binding.definition,row.path]).decode()
            row.value[row.binding.identity] = public_id(row.binding.family,key)
    return result
