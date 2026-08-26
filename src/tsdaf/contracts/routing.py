"""Asset-level article routing contracts and package-aware validation."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import field_validator, model_validator

from ._base import ContractModel, _sha256, _stable_string, _unique
from .package import (
    _BLOCK_ID_RE,
    _GROUP_ID_RE,
    _VISUAL_ID_RE,
    CanonicalPaperPackage,
)

_ROUTE_GROUP_ID_RE = re.compile(r"RG([0-9]{3})")


class ArticleType(StrEnum):
    TOTAL_SYNTHESIS = "TOTAL_SYNTHESIS"
    FORMAL_SYNTHESIS = "FORMAL_SYNTHESIS"
    SEMISYNTHESIS = "SEMISYNTHESIS"
    SYNTHETIC_APPROACH = "SYNTHETIC_APPROACH"
    NOT_IN_SCOPE = "NOT_IN_SCOPE"


class RoutingDisposition(StrEnum):
    ROUTED = "ROUTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class RouteAttribute(StrEnum):
    ASYMMETRIC = "ASYMMETRIC"
    RACEMIC = "RACEMIC"
    BIOMIMETIC = "BIOMIMETIC"
    CHEMOENZYMATIC = "CHEMOENZYMATIC"
    DIVERGENT = "DIVERGENT"
    COLLECTIVE = "COLLECTIVE"


class VisualDecision(StrEnum):
    FORWARD_ROUTE = "FORWARD_ROUTE"
    RETROSYNTHESIS = "RETROSYNTHESIS"
    MECHANISM = "MECHANISM"
    OPTIMIZATION_SCREENING = "OPTIMIZATION_SCREENING"
    FAILED_ONLY_ROUTE = "FAILED_ONLY_ROUTE"
    BIOSYNTHESIS = "BIOSYNTHESIS"
    ANALYTICAL = "ANALYTICAL"
    OTHER = "OTHER"
    NEEDS_VISUAL_VERIFICATION = "NEEDS_VISUAL_VERIFICATION"
    UNCERTAIN = "UNCERTAIN"


class CoarseTextRole(StrEnum):
    ROUTE_CORE = "ROUTE_CORE"
    DIRECT_OPTIMIZATION_CONTEXT = "DIRECT_OPTIMIZATION_CONTEXT"
    DIRECT_MECHANISM_CONTEXT = "DIRECT_MECHANISM_CONTEXT"
    DIRECT_FAILED_ATTEMPT_CONTEXT = "DIRECT_FAILED_ATTEMPT_CONTEXT"


InitialVisualDecision = Literal[
    VisualDecision.FORWARD_ROUTE,
    VisualDecision.RETROSYNTHESIS,
    VisualDecision.MECHANISM,
    VisualDecision.OPTIMIZATION_SCREENING,
    VisualDecision.FAILED_ONLY_ROUTE,
    VisualDecision.BIOSYNTHESIS,
    VisualDecision.ANALYTICAL,
    VisualDecision.OTHER,
    VisualDecision.NEEDS_VISUAL_VERIFICATION,
]
TriageVisualDecision = Literal[
    VisualDecision.FORWARD_ROUTE,
    VisualDecision.RETROSYNTHESIS,
    VisualDecision.MECHANISM,
    VisualDecision.OPTIMIZATION_SCREENING,
    VisualDecision.FAILED_ONLY_ROUTE,
    VisualDecision.BIOSYNTHESIS,
    VisualDecision.ANALYTICAL,
    VisualDecision.OTHER,
    VisualDecision.UNCERTAIN,
]
FinalVisualDecision = TriageVisualDecision


class RouteGroup(ContractModel):
    route_group_id: str
    target_names: tuple[str, ...]
    synthesis_type: ArticleType
    attributes: tuple[RouteAttribute, ...]
    ordered_visual_ids: tuple[str, ...]
    core_text_block_ids: tuple[str, ...]

    @field_validator("route_group_id")
    @classmethod
    def _validate_route_group_id(cls, value: str) -> str:
        match = _ROUTE_GROUP_ID_RE.fullmatch(value)
        if match is None or int(match.group(1)) == 0:
            raise ValueError("invalid route group id")
        return value

    @field_validator("target_names")
    @classmethod
    def _validate_target_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="route target names")
        for name in value:
            _stable_string(name, label="route target name")
        return value

    @field_validator("attributes")
    @classmethod
    def _validate_attributes(cls, value: tuple[RouteAttribute, ...]) -> tuple[RouteAttribute, ...]:
        return _unique(value, label="route attributes")

    @field_validator("ordered_visual_ids")
    @classmethod
    def _validate_visual_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="route visual ids")
        if any(_VISUAL_ID_RE.fullmatch(visual_id) is None for visual_id in value):
            raise ValueError("route group contains an invalid visual id")
        return value

    @field_validator("core_text_block_ids")
    @classmethod
    def _validate_core_block_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="route core block ids")
        if any(_BLOCK_ID_RE.fullmatch(block_id) is None for block_id in value):
            raise ValueError("route group contains an invalid core block id")
        return value


class RouterProvenance(ContractModel):
    schema_version: Literal["tsdaf.router-provenance.v1"]
    router_id: str
    router_version: str
    provider_id: str
    model_id: str
    provider_profile_id: str
    provider_profile_version: str
    provider_profile_sha256: str
    prompt_id: str
    prompt_version: str
    prompt_sha256: str
    contract_schema_bundle_version: str
    contract_schema_sha256: str
    taxonomy_sha256: str
    tool_schema_version: str
    tool_schema_sha256: str

    @field_validator(
        "router_id",
        "router_version",
        "provider_id",
        "model_id",
        "provider_profile_id",
        "provider_profile_version",
        "prompt_id",
        "prompt_version",
        "contract_schema_bundle_version",
        "tool_schema_version",
    )
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="router provenance identity")

    @field_validator(
        "provider_profile_sha256",
        "prompt_sha256",
        "contract_schema_sha256",
        "taxonomy_sha256",
        "tool_schema_sha256",
    )
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="router provenance hash")


class VisualAssetDecision(ContractModel):
    visual_id: str
    visual_group_id: str
    initial_decision: InitialVisualDecision
    triage_result: TriageVisualDecision | None = None
    final_decision: FinalVisualDecision

    @field_validator("visual_id")
    @classmethod
    def _validate_visual_id(cls, value: str) -> str:
        if _VISUAL_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid visual decision id")
        return value

    @field_validator("visual_group_id")
    @classmethod
    def _validate_visual_group_id(cls, value: str) -> str:
        if _GROUP_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid visual decision group id")
        return value

    @model_validator(mode="after")
    def _validate_trace(self) -> Self:
        if self.initial_decision is VisualDecision.NEEDS_VISUAL_VERIFICATION:
            if self.triage_result is None or self.final_decision is not self.triage_result:
                raise ValueError("visual verification requires a persisted matching triage result")
        elif self.triage_result is not None or self.final_decision is not self.initial_decision:
            raise ValueError("resolved initial decision cannot have triage or a changed final")
        return self


class CoarseTextSelection(ContractModel):
    route_group_id: str
    block_id: str
    role: CoarseTextRole

    @field_validator("route_group_id")
    @classmethod
    def _validate_route_group_id(cls, value: str) -> str:
        match = _ROUTE_GROUP_ID_RE.fullmatch(value)
        if match is None or int(match.group(1)) == 0:
            raise ValueError("invalid selection route group id")
        return value

    @field_validator("block_id")
    @classmethod
    def _validate_block_id(cls, value: str) -> str:
        if _BLOCK_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid selected block id")
        return value


class ArticleRoutingResult(ContractModel):
    schema_version: Literal["tsdaf.article-routing-result.v1"]
    paper_id: str
    execution_id: str
    canonical_package_sha256: str
    article_type: ArticleType
    disposition: RoutingDisposition
    route_groups: tuple[RouteGroup, ...]
    visual_decisions: tuple[VisualAssetDecision, ...]
    coarse_text_selections: tuple[CoarseTextSelection, ...]
    provenance: RouterProvenance

    @field_validator("paper_id", "execution_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="routing result identity")

    @field_validator("canonical_package_sha256")
    @classmethod
    def _validate_package_hash(cls, value: str) -> str:
        return _sha256(value, label="canonical package hash")

    @model_validator(mode="after")
    def _validate_result_shape(self) -> Self:
        self._validate_shape()
        return self

    def _validate_shape(self) -> None:
        expected_route_ids = tuple(
            f"RG{index:03d}" for index in range(1, len(self.route_groups) + 1)
        )
        actual_route_ids = tuple(group.route_group_id for group in self.route_groups)
        if actual_route_ids != expected_route_ids:
            raise ValueError("route group ids are not tuple-order contiguous")
        decision_ids = tuple(decision.visual_id for decision in self.visual_decisions)
        _unique(decision_ids, label="visual decision ids")
        route_ids = set(actual_route_ids)
        triples = tuple(
            (selection.route_group_id, selection.block_id, selection.role)
            for selection in self.coarse_text_selections
        )
        _unique(triples, label="coarse text selections")
        if any(
            selection.route_group_id not in route_ids for selection in self.coarse_text_selections
        ):
            raise ValueError("coarse selection references an unknown route group")
        route_positions = {route_id: index for index, route_id in enumerate(actual_route_ids)}
        selection_route_order = tuple(
            route_positions[selection.route_group_id] for selection in self.coarse_text_selections
        )
        if selection_route_order != tuple(sorted(selection_route_order)):
            raise ValueError("coarse selections are not grouped by route order")
        for group in self.route_groups:
            if not group.ordered_visual_ids:
                raise ValueError("each route group must contain at least one visual")
            route_core = tuple(
                selection.block_id
                for selection in self.coarse_text_selections
                if selection.route_group_id == group.route_group_id
                and selection.role is CoarseTextRole.ROUTE_CORE
            )
            if not route_core or route_core != group.core_text_block_ids:
                raise ValueError("route core selections do not match route core block ids")
        routed_visual_ids = tuple(
            visual_id for group in self.route_groups for visual_id in group.ordered_visual_ids
        )
        _unique(routed_visual_ids, label="routed visual ids")
        forward_ids = tuple(
            decision.visual_id
            for decision in self.visual_decisions
            if decision.final_decision is VisualDecision.FORWARD_ROUTE
        )
        if set(routed_visual_ids) != set(forward_ids):
            raise ValueError("routed visual inventory does not equal final forward inventory")
        has_uncertain = any(
            decision.final_decision is VisualDecision.UNCERTAIN
            for decision in self.visual_decisions
        )
        if self.article_type is ArticleType.NOT_IN_SCOPE:
            valid = (
                self.disposition is RoutingDisposition.REJECTED
                and not self.route_groups
                and not self.coarse_text_selections
                and not forward_ids
                and not has_uncertain
            )
        elif has_uncertain:
            valid = self.disposition is RoutingDisposition.REVIEW_REQUIRED
        elif self.route_groups and forward_ids:
            valid = self.disposition is RoutingDisposition.ROUTED
        elif not forward_ids and not self.route_groups and not self.coarse_text_selections:
            valid = self.disposition is RoutingDisposition.REJECTED
        else:
            valid = False
        if not valid:
            raise ValueError("routing disposition contradicts the completed routing facts")

    def validate_against(self, package: CanonicalPaperPackage) -> None:
        self._validate_shape()
        if self.paper_id != package.manifest.paper_id:
            raise ValueError("routing paper id does not match canonical package")
        if self.canonical_package_sha256 != package.canonical_content_sha256:
            raise ValueError("routing package hash does not match canonical package")
        package_visual_pairs = tuple(
            (visual.visual_id, visual.visual_group_id) for visual in package.visual_assets
        )
        decision_pairs = tuple(
            (decision.visual_id, decision.visual_group_id) for decision in self.visual_decisions
        )
        if decision_pairs != package_visual_pairs:
            raise ValueError("visual decisions do not match package visual inventory and order")
        visual_ids = {visual.visual_id for visual in package.visual_assets}
        block_order = {block.block_id: index for index, block in enumerate(package.blocks)}
        for group in self.route_groups:
            if any(visual_id not in visual_ids for visual_id in group.ordered_visual_ids):
                raise ValueError("route group has a dangling visual reference")
            if any(block_id not in block_order for block_id in group.core_text_block_ids):
                raise ValueError("route group has a dangling core block reference")
        if any(selection.block_id not in block_order for selection in self.coarse_text_selections):
            raise ValueError("coarse selection has a dangling block reference")
        role_order = {role: index for index, role in enumerate(CoarseTextRole)}
        route_order = {group.route_group_id: index for index, group in enumerate(self.route_groups)}
        selection_keys = tuple(
            (
                route_order[selection.route_group_id],
                block_order[selection.block_id],
                role_order[selection.role],
            )
            for selection in self.coarse_text_selections
        )
        if selection_keys != tuple(sorted(selection_keys)):
            raise ValueError("coarse selections are not in canonical package order")
        decorative_ids = {record.visual_id for record in package.visual_suppressions}
        routed_ids = {
            visual_id for group in self.route_groups for visual_id in group.ordered_visual_ids
        }
        for decision in self.visual_decisions:
            if decision.visual_id not in decorative_ids:
                continue
            if decision.initial_decision is VisualDecision.FORWARD_ROUTE:
                raise ValueError("main router cannot directly route a decorative visual")
            if decision.visual_id in routed_ids and not (
                decision.initial_decision is VisualDecision.NEEDS_VISUAL_VERIFICATION
                and decision.triage_result is VisualDecision.FORWARD_ROUTE
                and decision.final_decision is VisualDecision.FORWARD_ROUTE
            ):
                raise ValueError("decorative visual lacks the complete forward override trace")
