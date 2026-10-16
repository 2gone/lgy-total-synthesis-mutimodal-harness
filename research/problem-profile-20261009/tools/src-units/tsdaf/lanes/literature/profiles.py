"""Fixed private lane profiles and immutable prepared interaction calls."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
from dataclasses import dataclass
from typing import Literal, cast

from tsdaf.contracts.tool_read import READ_DELIVERY_VERSION
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.citation_policy import INHERITED_CITATION_POLICY, SOURCE_CITATION_POLICY
from tsdaf.lanes.literature.skeleton_meanings import fusion_fact_meanings, skeleton_guidance

FUSION_PROFILE = "tsdaf.literature.observation-fusion.v59"


def fusion_profile(*, unified_expression: bool = False) -> str:
    """Select the resource revision for the actual model-facing capability."""
    return "tsdaf.literature.observation-fusion.v62" if unified_expression else FUSION_PROFILE


def fusion_prompt_text(*, unified_expression: bool = False) -> str:
    from tsdaf.lanes.literature.condition_categories import CONDITION_KEYS

    return (
        importlib.resources.files("tsdaf.lanes.literature")
        .joinpath("prompts/fusion_v1.md")
        .read_text(encoding="utf-8")
        .replace("{{condition_keys}}", ", ".join(CONDITION_KEYS))
        .replace("{{fact_meanings}}", fusion_fact_meanings(unified_expression=unified_expression))
        .replace("{{fusion_reference_rule}}", importlib.resources.files("tsdaf.lanes.literature").joinpath("prompts/fusion_references_" + ("unified" if unified_expression else "legacy") + ".md").read_text())
        .replace("{{fusion_limits}}", importlib.resources.files("tsdaf.lanes.literature").joinpath("prompts/fusion_limits_" + ("unified" if unified_expression else "legacy") + ".md").read_text())
        .replace(
            "{{source_citation_policy}}", SOURCE_CITATION_POLICY + " " + INHERITED_CITATION_POLICY
        )
        + "\n"
        + skeleton_guidance("FUSION")
        + "\n"
    )


def fusion_resource_manifest_sha256(*, unified_expression: bool = False) -> str:
    from tsdaf.lanes.literature.gate_models import fusion_tools
    from tsdaf.lanes.literature.model_names import NAMING_VERSION

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "profile": fusion_profile(unified_expression=unified_expression),
                "prompt": hashlib.sha256(fusion_prompt_text(unified_expression=unified_expression).encode()).hexdigest(),
                "tools": hashlib.sha256(
                    canonical_json_bytes(fusion_tools(unified_expression=unified_expression))
                ).hexdigest(),
                # The fusion is shown names, not the host's ids (batch 2A, 2d): a change
                # to the names is a change to what it is asked.
                "naming": NAMING_VERSION, "read_delivery": READ_DELIVERY_VERSION,
            }
        )
    ).hexdigest()


# V3 is the figure reader a detail task dispatches instead of being handed an image
# (E100 and the d77b930 run: 116 of 223 turns landed where no evidence could be minted,
# and the prompt sentence that sent them there had already been deleted). It answers one
# question about one figure in words, marking regions of that figure without naming it.
LaneStage = Literal["T1", "V1", "V3"]

STAGE_PROFILES = {
    "T1": "tsdaf.literature.text-observations.v9",
    "V1": "tsdaf.literature.visual-observations.v8",
    "V3": "tsdaf.literature.visual-detail-reading.v2",
}
_RESOURCE_SPECS = {
    "T1": (
        "prompts/text_route_v1.md",
        "c62cb746675675d28756869dffbd5b07d796e0c3927f239da75cb1d7e8ce8993",
        "schemas/text_route_v1.json",
        "4f90dc34e58ce7d85d889c31ab51693255c7c6bc5ea2c3662a7d379394ca1da2",
    ),
    "V1": (
        "prompts/visual_route_v1.md",
        "91e2c7737a73341d5ba1e9e3a3f7c8d6134c7272085d36ab7f08a3ebc68e25d2",
        "schemas/visual_route_v1.json",
        "2c6303430d0e465181ad8b5446503936d59f2f64c57fd099170493ec9d0e2dac",
    ),
    # V3 has an answer shape of its own. It once reused V1's, and with it V1's rule that
    # a selection names a MAIN image -- a rule about what V1 is shown, carried to a reader
    # that is also sent supplement figures: 93 of 95 readings of one were refused (f09).
    # The two shapes share one definition of a region (`lane_models._ImageRegion`); what
    # differs is only that V3 names no image, because the Host dispatched it.
    "V3": (
        "prompts/visual_detail_v1.md",
        "3a5ce2954cadacfb0c57f5b82f304f39abdd4d14380f4a23ace8c10d0fe9ef90",
        "schemas/visual_detail_v1.json",
        "68a63479941130fb43befc5583e5b40e9fcbd2504fe4f01072bb85de23f1c461",
    ),
}


@dataclass(frozen=True, slots=True)
class LaneResources:
    stage: LaneStage
    stage_profile: str
    prompt_path: str
    prompt_bytes: bytes
    prompt_text: str
    prompt_sha256: str
    schema_path: str
    schema_bytes: bytes
    schema_sha256: str
    resource_manifest_bytes: bytes
    resource_manifest_sha256: str

    @property
    def provider_schema(self) -> dict[str, object]:
        value: object = json.loads(self.schema_bytes)
        if not isinstance(value, dict):
            raise AssertionError("provider schema lost its object shape")
        return cast(dict[str, object], value)


@dataclass(frozen=True, slots=True)
class PreparedLaneCall:
    """Canonical lane call inputs whose mutable view is reconstructed on demand."""

    stage: LaneStage
    call_kwargs_bytes: bytes
    resources: LaneResources

    @property
    def call_kwargs(self) -> dict[str, object]:
        value: object = json.loads(self.call_kwargs_bytes)
        if not isinstance(value, dict):
            raise AssertionError("prepared lane kwargs lost their object shape")
        return cast(dict[str, object], value)


def load_lane_resources(stage: LaneStage) -> LaneResources:
    if stage not in _RESOURCE_SPECS:
        raise ValueError("unknown literature lane stage")
    prompt_path, expected_prompt_hash, schema_path, expected_schema_hash = _RESOURCE_SPECS[stage]
    package_root = importlib.resources.files("tsdaf.lanes.literature")
    prompt_bytes = package_root.joinpath(*prompt_path.split("/")).read_bytes()
    try:
        prompt_text = prompt_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("literature lane prompt is not strict UTF-8") from None
    if not prompt_text or prompt_text.startswith("\ufeff") or "\x00" in prompt_text:
        raise ValueError("literature lane prompt is empty or noncanonical")
    if stage in {"T1", "V1"}:
        prompt_text += "\n" + skeleton_guidance(stage) + "\n"
        prompt_bytes = prompt_text.encode("utf-8")
    schema_bytes = package_root.joinpath(*schema_path.split("/")).read_bytes()
    try:
        raw_schema: object = json.loads(schema_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("literature provider schema is not canonical JSON") from None
    if not isinstance(raw_schema, dict) or canonical_json_bytes(raw_schema) != schema_bytes:
        raise ValueError("literature provider schema is not canonical JSON")
    prompt_hash = hashlib.sha256(prompt_bytes).hexdigest()
    schema_hash = hashlib.sha256(schema_bytes).hexdigest()
    if prompt_hash != expected_prompt_hash or schema_hash != expected_schema_hash:
        raise ValueError("literature lane request resource hash does not match its frozen profile")
    manifest_bytes = canonical_json_bytes(
        {
            "schema_version": "tsdaf.literature-lane-resource-manifest.v1",
            "stage": stage,
            "stage_profile": STAGE_PROFILES[stage],
            "prompt": {"resource_path": prompt_path, "sha256": prompt_hash},
            "provider_schema": {"resource_path": schema_path, "sha256": schema_hash},
        }
    )
    return LaneResources(
        stage=stage,
        stage_profile=STAGE_PROFILES[stage],
        prompt_path=prompt_path,
        prompt_bytes=prompt_bytes,
        prompt_text=prompt_text,
        prompt_sha256=prompt_hash,
        schema_path=schema_path,
        schema_bytes=schema_bytes,
        schema_sha256=schema_hash,
        resource_manifest_bytes=manifest_bytes,
        resource_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def generated_schema_bytes(stage: LaneStage) -> bytes:
    """Explicitly reproduce one provider schema from the private Pydantic authority."""

    if stage not in _RESOURCE_SPECS:
        raise ValueError("unknown literature lane stage")
    from tsdaf.lanes.literature.lane_models import TextReading, VisualDetailReading, VisualReading

    model = (
        TextReading if stage == "T1" else VisualDetailReading if stage == "V3" else VisualReading
    )
    return canonical_json_bytes(model.model_json_schema(mode="validation"))


def prepare_lane_call(
    *,
    stage: LaneStage,
    call_kwargs: dict[str, object],
    resources: LaneResources,
) -> PreparedLaneCall:
    if resources.stage != stage:
        raise ValueError("prepared lane resources do not match the stage")
    call_bytes = canonical_json_bytes(call_kwargs)
    return PreparedLaneCall(
        stage=stage,
        call_kwargs_bytes=call_bytes,
        resources=resources,
    )
