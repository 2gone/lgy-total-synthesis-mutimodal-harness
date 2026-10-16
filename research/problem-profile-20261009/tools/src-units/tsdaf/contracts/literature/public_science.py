"""Runtime adapter for the shared public-science contract rules."""
from .public_science_rules import record_observation_refs, load_public_science as _load
from .schema import MachineProfile, validate_fact_definition


def load_public_science(machine, read_public):
    return _load(machine, read_public, lambda name, value:
                 validate_fact_definition(name, value, MachineProfile.V1_7))
