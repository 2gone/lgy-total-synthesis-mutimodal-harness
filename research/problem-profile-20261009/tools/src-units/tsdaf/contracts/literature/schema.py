"""Schema-backed validation for the published fact roots, one profile at a time.

The frozen v1/v1.1 profiles remain readable; v1.2 adds material names and nonblocking
review markers while retaining v1.1 stages. Which one a root is read against is
not a caller's option -- it is the root's own `schema_version`, so a result cannot be
validated against a contract it does not claim.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from enum import StrEnum
from importlib.resources import files

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import best_match  # type: ignore[import-untyped]

# A refusal is a sentence, not the document. jsonschema's message for a root that
# matches none of its alternatives quotes the whole instance -- for a machine result
# that is the paper -- and a write refused that way put 191 KB into every diagnostic
# the model was handed, until the request itself was refused (f05, ledger E141).
REFUSAL_CHARS = 300


class FactRootKind(StrEnum):
    MACHINE = "MACHINE"
    OBSERVATION = "OBSERVATION"


class MachineProfile(StrEnum):
    """A published version of the machine fact surface. v1 is the default everywhere:
    Newer profiles are produced only when a run asks for them (`--machine-profile`)."""

    V1 = "v1"
    V1_1 = "v1.1"
    V1_2 = "v1.2"
    V1_3 = "v1.3"
    V1_4 = "v1.4"
    V1_5 = "v1.5"
    V1_6 = "v1.6"
    V1_7 = "v1.7"

    @property
    def stages(self) -> bool:
        return self is not MachineProfile.V1

    @property
    def wrapping(self) -> bool:
        return self not in (MachineProfile.V1, MachineProfile.V1_1)

    @property
    def expression(self) -> bool:
        return self in (
            MachineProfile.V1_3,
            MachineProfile.V1_4,
            MachineProfile.V1_5,
            MachineProfile.V1_6,
            MachineProfile.V1_7,
        )


    @property
    def preservation(self) -> bool:
        return self in (MachineProfile.V1_4, MachineProfile.V1_5, MachineProfile.V1_6, MachineProfile.V1_7)


    @property
    def open_expression(self) -> bool:
        return self in (MachineProfile.V1_5, MachineProfile.V1_6, MachineProfile.V1_7)


    @property
    def unified_expression(self) -> bool:
        return self in (MachineProfile.V1_6, MachineProfile.V1_7)


    @property
    def faithful_mapping(self) -> bool:
        return self is MachineProfile.V1_7

    @property
    def production_enabled(self) -> bool:
        """Candidate contracts can be checked internally, never selected for a run."""
        return self not in (MachineProfile.V1_6, MachineProfile.V1_7)


    def require_production(self) -> None:
        if not self.production_enabled:
            raise ValueError(
                f"{self.value} is an internal candidate; production consumers are not enabled"
            )
# One table, read four ways: which resources a profile's contract lives in, and what
# its machine root calls itself. Nothing else in the runtime spells these names.
_RESOURCES: dict[MachineProfile, tuple[str, str]] = {
    MachineProfile.V1: ("fact_schema.json", "contract_pack_pins.json"),
    MachineProfile.V1_1: ("fact_schema_v1_1.json", "contract_pack_pins_v1_1.json"),
    MachineProfile.V1_2: ("fact_schema_v1_2.json", "contract_pack_pins_v1_2.json"),
    MachineProfile.V1_3: ("fact_schema_v1_3.json", "contract_pack_pins_v1_3.json"),
    MachineProfile.V1_4: ("fact_schema_v1_4.json", "contract_pack_pins_v1_4.json"),
    MachineProfile.V1_5: ("fact_schema_v1_5.json", "contract_pack_pins_v1_5.json"),
    MachineProfile.V1_6: ("fact_schema_v1_6.json", "contract_pack_pins_v1_6.json"),
    MachineProfile.V1_7: ("fact_schema_v1_7.json", "contract_pack_pins_v1_7.json"),
}
MACHINE_SCHEMA_VERSIONS: dict[MachineProfile, str] = {
    profile: f"tsdaf.machine-paper-result.{profile.value}" for profile in MachineProfile
}
OBSERVATION_SCHEMA_VERSION = "tsdaf.source-observation.v1"


def _read(name: str) -> dict[str, object]:
    path = files("tsdaf.contracts.literature").joinpath("resources/" + name)
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _validator(profile: MachineProfile) -> Draft202012Validator:
    schema = _read(_RESOURCES[profile][0])
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


_FACT_VALIDATORS: dict[MachineProfile, Draft202012Validator] = {
    profile: _validator(profile) for profile in MachineProfile
}
_FACT_VALIDATOR = _FACT_VALIDATORS[MachineProfile.V1]
_PROFILE_BY_VERSION = {
    **{version: profile for profile, version in MACHINE_SCHEMA_VERSIONS.items()},
    OBSERVATION_SCHEMA_VERSION: MachineProfile.V1,
}


def contract_pack_pins(profile: MachineProfile = MachineProfile.V1) -> Mapping[str, str]:
    """The pack's own hashes for this profile: the shared schema candidate a result
    names as its golden contract, and the fact schema it was validated against."""
    return {key: str(value) for key, value in _read(_RESOURCES[profile][1]).items()}


def machine_profile(machine: Mapping[str, object]) -> MachineProfile:
    """The root's declared profile; never guess a newer contract for old facts."""
    profile = _PROFILE_BY_VERSION.get(str(machine.get("schema_version")))
    if profile is None or machine.get("schema_version") == OBSERVATION_SCHEMA_VERSION:
        raise ValueError("machine has an unknown schema_version")
    return profile


def validate_profile_transition(machine: Mapping[str, object], profile: MachineProfile) -> None:
    """A newer derived result may add capabilities; never silently discard them."""
    profiles = tuple(MachineProfile)
    previous = machine_profile(machine)
    if profile.faithful_mapping and not previous.faithful_mapping:
        raise ValueError("v1.7 requires explicit mechanical baseline upgrade, not version relabeling")
    if profile.unified_expression and not previous.unified_expression:
        raise ValueError(
            "v1.6 changes expression representations; reproject the sealed source facts "
            "under v1.6 instead of relabeling an older public result"
        )
    if profiles.index(profile) < profiles.index(previous):
        raise ValueError("machine profile downgrade is not supported")


def fact_definition(name: str, profile: MachineProfile = MachineProfile.V1) -> dict[str, object]:
    """Copy one authoritative public definition for producer tools and validation."""
    return copy.deepcopy(_FACT_VALIDATORS[profile].schema["$defs"][name])


def fact_enum(name: str) -> tuple[str, ...]:
    """The values of one public enum definition, in the order the contract lists them.

    Every consumer that maps a private category onto a public enum reads it here, so
    the vocabulary is written once -- in the shared contract pack -- and a prompt that
    lists it is checked against this, not retyped.
    """
    definition = _FACT_VALIDATOR.schema["$defs"][name]
    values = definition.get("enum") if isinstance(definition, dict) else None
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ValueError(f"{name} is not an enum definition of the public contract")
    return tuple(values)


def _refusal(validator: Draft202012Validator, value: object) -> str | None:
    """The deepest error and where it is, in at most REFUSAL_CHARS -- or None if valid.

    `best_match` walks into the alternatives of a oneOf/anyOf and picks the error most
    likely to be the actual one, so a stray key deep in a machine result is reported
    as that key at that path rather than as the root matching no alternative.
    """
    error = best_match(validator.iter_errors(value))
    if error is None:
        return None
    return _error_refusal(error)


def _error_refusal(error, *, alternatives: bool = True) -> str:
    """Describe validator failures, never select a scientific interpretation."""
    where = "/".join(str(part) for part in error.absolute_path) or "<root>"
    # jsonschema may begin its message with the entire offending instance. The
    # actionable rule and path must survive the bound, without dumping source
    # statements or evidence identities back to the model.
    keyword = str(error.validator)
    if keyword == "uniqueItems":
        reason = "duplicate items are not allowed"
    elif keyword == "type":
        reason = f"expected type {error.validator_value}"
    elif keyword in {"required", "additionalProperties"}:
        # These messages identify missing/unexpected keys, not their values.
        reason = error.message
    elif keyword == "enum":
        choices = json.dumps(error.validator_value, ensure_ascii=False)
        reason = (
            f"expected one of {choices}"
            if len(choices) <= 100
            else "value must be one of the declared choices"
        )
    elif keyword == "const":
        reason = "value must equal the declared constant"
    elif keyword in {"oneOf", "anyOf"} and alternatives and error.context:
        # A tied union error has no single best branch. Report concrete failures
        # under the original alternatives rather than guessing which was intended.
        # Keep the same global size bound and never include the submitted instance.
        details = []
        for child in error.context:
            branch = next(iter(child.schema_path), "?")
            detail = f"branch {branch}: " + _error_refusal(child, alternatives=False)
            if detail not in details:
                details.append(detail)
            if len(details) == 3:
                break
        reason = "no alternative matched; " + "; ".join(details)
    elif keyword in {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "minProperties",
        "maxProperties",
    }:
        reason = f"required constraint {keyword}={error.validator_value}"
    else:
        reason = "value does not satisfy the declared constraint"
    prefix = f"{keyword}: {reason}"
    # Reserve room for the path even when an error names many unexpected keys.
    if len(where) > 150:
        where = "…" + where[-149:]
    prefix_limit = REFUSAL_CHARS - len(where) - len(" at ")
    if len(prefix) > prefix_limit:
        prefix = prefix[: prefix_limit - 1] + "…"
    said = f"{prefix} at {where}"
    if len(said) > REFUSAL_CHARS:
        said = said[: REFUSAL_CHARS - 1] + "…"
    return said


def validate_fact_definition(
    name: str, value: object, profile: MachineProfile = MachineProfile.V1
) -> None:
    schema = {"$ref": "#/$defs/" + name, "$defs": _FACT_VALIDATORS[profile].schema["$defs"]}
    refusal = _refusal(Draft202012Validator(schema), value)
    if refusal is not None:
        raise ValueError(f"{name}: {refusal}")


def validate_fact_root(value: Mapping[str, object]) -> FactRootKind:
    """Validate one published fact root against the contract it claims, and return its
    discriminated kind.

    The version is read first because it chooses the validator: a v1.1 result held to
    the v1 schema would be refused for the version string rather than for anything
    that is wrong with it, and a v1 result held to v1.1 would be accepted with stage
    items v1 never allowed."""

    if not isinstance(value, Mapping):
        raise TypeError("fact root must be a mapping")
    schema_version = str(value.get("schema_version"))
    profile = _PROFILE_BY_VERSION.get(schema_version)
    if profile is None:
        raise ValueError("fact root has an unknown schema_version")
    refusal = _refusal(_FACT_VALIDATORS[profile], dict(value))
    if refusal is not None:
        raise ValueError(f"fact root fails schema validation: {refusal}")
    if schema_version == OBSERVATION_SCHEMA_VERSION:
        return FactRootKind.OBSERVATION
    return FactRootKind.MACHINE
