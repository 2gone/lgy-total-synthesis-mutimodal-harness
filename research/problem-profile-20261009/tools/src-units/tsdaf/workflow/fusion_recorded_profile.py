"""Recover a Fusion call's contract from its sealed request resources."""

from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.contracts.source import ArtifactRef


def fusion_call_profile(call, read):
    # Old calls predate request resource bindings and used the original contract.
    turn = read(ArtifactRef.model_validate(call["interaction_ref"])) if "interaction_ref" in call else call
    resources = read(ArtifactRef.model_validate(turn["resources_ref"])) if "resources_ref" in turn else {}
    return MachineProfile(resources.get("machine_profile", MachineProfile.V1.value))
