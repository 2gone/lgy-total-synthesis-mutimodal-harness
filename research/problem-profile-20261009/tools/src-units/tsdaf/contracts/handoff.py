"""Immutable visual/text handoffs, receipts, and image-job data shapes."""

from __future__ import annotations

import hashlib
import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from ._base import (
    ContractModel,
    UtcDateTime,
    _canonical_json_bytes,
    _sha256,
    _stable_string,
    _unique,
)
from .package import (
    _BLOCK_ID_RE,
    _GROUP_ID_RE,
    CanonicalPaperPackage,
    CanonicalTextBlock,
    CanonicalVisualAsset,
)
from .routing import (
    ArticleRoutingResult,
    ArticleType,
    CoarseTextSelection,
    RouteGroup,
    RouterProvenance,
    _RouteGroupId,
)
from .source import ArtifactRef, BoundingBox

_ROUTE_IMAGE_ITEM_ID_RE = re.compile(r"RIMG([0-9]{3})")


class VisualRoutingHandoff(ContractModel):
    schema_version: Literal["tsdaf.visual-routing-handoff.v1"]
    paper_id: str
    execution_id: str
    article_type: ArticleType
    route_groups: tuple[RouteGroup, ...]
    items: tuple[RouteImageItem, ...]
    provenance: RouterProvenance
    status: Literal["PROPOSED"]

    @field_validator("paper_id", "execution_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="visual handoff identity")

    def validate_against(
        self,
        package: CanonicalPaperPackage,
        routing_result: ArticleRoutingResult,
    ) -> None:
        routing_result.validate_against(package)
        if (
            self.paper_id != routing_result.paper_id
            or self.execution_id != routing_result.execution_id
            or self.article_type is not routing_result.article_type
            or self.route_groups != routing_result.route_groups
            or self.provenance != routing_result.provenance
        ):
            raise ValueError("visual handoff does not copy routing identity and provenance")
        visual_by_id = {visual.visual_id: visual for visual in package.visual_assets}
        expected: list[tuple[RouteGroup, int, CanonicalVisualAsset]] = []
        for route_group in routing_result.route_groups:
            for sequence_index, visual_id in enumerate(route_group.ordered_visual_ids):
                expected.append((route_group, sequence_index, visual_by_id[visual_id]))
        if len(self.items) != len(expected):
            raise ValueError("visual handoff item inventory is incomplete")
        for global_index, (item, expected_row) in enumerate(
            zip(self.items, expected, strict=True), 1
        ):
            route_group, sequence_index, visual = expected_row
            item._validate_related_shape()
            if item.route_image_item_id != f"RIMG{global_index:03d}":
                raise ValueError("route image item ids are not handoff-global and contiguous")
            if item.route_group_id != route_group.route_group_id:
                raise ValueError("route image item does not match flattened route group order")
            if item.sequence_index != sequence_index:
                raise ValueError("route image sequence is not route-local and contiguous")
            if item.router_execution_id != routing_result.execution_id:
                raise ValueError("route image item has a stale router execution id")
            expected_dimensions = None if visual.width is None else (visual.width, visual.height)
            if (
                item.visual_group_id != visual.visual_group_id
                or item.source_asset != visual.asset
                or item.page_index != visual.page_index
                or item.bbox != visual.bbox
                or item.dimensions != expected_dimensions
            ):
                raise ValueError("route image item is not an exact canonical visual copy")
            self._validate_related_paragraphs(item, package)

    @staticmethod
    def _validate_related_paragraphs(item: RouteImageItem, package: CanonicalPaperPackage) -> None:
        block_order = {block.block_id: index for index, block in enumerate(package.blocks)}
        block_by_id = {block.block_id: block for block in package.blocks}
        if any(block_id not in block_by_id for block_id in item.related_block_ids):
            raise ValueError("route image item has a dangling related block")
        indices = tuple(block_order[block_id] for block_id in item.related_block_ids)
        if indices != tuple(sorted(indices)):
            raise ValueError("related blocks are not in canonical text order")
        expected_text = tuple(block_by_id[block_id].raw_text for block_id in item.related_block_ids)
        if item.related_original_paragraphs != expected_text:
            raise ValueError("route image paragraphs are not byte-for-byte canonical text")


class RouteImageItem(ContractModel):
    route_image_item_id: str
    visual_group_id: str
    source_asset: ArtifactRef
    page_index: int = Field(ge=0, le=9999)
    bbox: BoundingBox | None
    dimensions: tuple[int, int] | None
    figure_or_scheme_label: str | None
    caption: str | None
    related_block_ids: tuple[str, ...]
    related_original_paragraphs: tuple[str, ...]
    route_group_id: _RouteGroupId
    sequence_index: int = Field(ge=0)
    router_execution_id: str

    @field_validator("route_image_item_id")
    @classmethod
    def _validate_item_id(cls, value: str) -> str:
        match = _ROUTE_IMAGE_ITEM_ID_RE.fullmatch(value)
        if match is None or int(match.group(1)) == 0:
            raise ValueError("invalid route image item id")
        return value

    @field_validator("visual_group_id")
    @classmethod
    def _validate_visual_group_id(cls, value: str) -> str:
        if _GROUP_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid route image visual group id")
        return value

    @field_validator("router_execution_id")
    @classmethod
    def _validate_execution_id(cls, value: str) -> str:
        return _stable_string(value, label="router execution id")

    @field_validator("related_block_ids")
    @classmethod
    def _validate_related_block_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="related block ids")
        if any(_BLOCK_ID_RE.fullmatch(block_id) is None for block_id in value):
            raise ValueError("route image item contains an invalid related block id")
        return value

    @field_validator("related_original_paragraphs")
    @classmethod
    def _validate_related_paragraphs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="related original paragraphs")
        if any(paragraph == "" for paragraph in value):
            raise ValueError("related original paragraphs cannot be empty")
        return value

    @field_validator("dimensions")
    @classmethod
    def _validate_dimensions(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is not None and (value[0] <= 0 or value[1] <= 0):
            raise ValueError("route image dimensions must be positive")
        return value

    @model_validator(mode="after")
    def _validate_related_fields(self) -> Self:
        self._validate_related_shape()
        return self

    def _validate_related_shape(self) -> None:
        if len(self.related_block_ids) > 2:
            raise ValueError("route image item permits at most two explicitly related blocks")
        if len(self.related_block_ids) != len(self.related_original_paragraphs):
            raise ValueError("related blocks and paragraphs must have equal length")


class ReviewReceipt(ContractModel):
    schema_version: Literal["tsdaf.review-receipt.v1"]
    review_id: str
    handoff_sha256: str
    reviewer_id: str
    decision: Literal["APPROVE", "REJECT", "REQUEST_CHANGES"]
    reviewed_at: UtcDateTime
    notes: str | None

    @field_validator("review_id", "reviewer_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="review receipt identity")

    @field_validator("handoff_sha256")
    @classmethod
    def _validate_handoff_hash(cls, value: str) -> str:
        return _sha256(value, label="reviewed handoff hash")


class ReleaseReceipt(ContractModel):
    schema_version: Literal["tsdaf.release-receipt.v1"]
    release_id: str
    handoff_sha256: str
    authority: Literal["HUMAN_APPROVED", "POLICY_APPROVED"]
    policy_version: str | None
    issued_at: UtcDateTime

    @field_validator("release_id")
    @classmethod
    def _validate_release_id(cls, value: str) -> str:
        return _stable_string(value, label="release id")

    @field_validator("handoff_sha256")
    @classmethod
    def _validate_handoff_hash(cls, value: str) -> str:
        return _sha256(value, label="released handoff hash")

    @model_validator(mode="after")
    def _validate_authority(self) -> Self:
        if self.authority == "POLICY_APPROVED":
            if self.policy_version is None:
                raise ValueError("policy approval requires a policy version")
            _stable_string(self.policy_version, label="policy version")
        elif self.policy_version is not None:
            raise ValueError("human approval cannot carry a policy version")
        return self


class ImageProcessingJob(ContractModel):
    job_id: str
    route_image_item: RouteImageItem
    release_receipt: ArtifactRef
    requested_pipeline: Literal["YOLO_MOLDET_OCSR_V1"]

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return _stable_string(value, label="image processing job id")


class CoarseTextHandoffItem(ContractModel):
    selection: CoarseTextSelection
    block: CanonicalTextBlock


class CoarseTextHandoff(ContractModel):
    schema_version: Literal["tsdaf.coarse-text-handoff.v1"]
    paper_id: str
    execution_id: str
    canonical_package_sha256: str
    routing_result_sha256: str
    items: tuple[CoarseTextHandoffItem, ...]
    provenance: RouterProvenance

    @field_validator("paper_id", "execution_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="coarse handoff identity")

    @field_validator("canonical_package_sha256", "routing_result_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        return _sha256(value, label="coarse handoff hash")

    def validate_against(
        self,
        package: CanonicalPaperPackage,
        routing_result: ArticleRoutingResult,
    ) -> None:
        routing_result.validate_against(package)
        if (
            self.paper_id != routing_result.paper_id
            or self.execution_id != routing_result.execution_id
            or self.canonical_package_sha256 != package.canonical_content_sha256
            or self.provenance != routing_result.provenance
        ):
            raise ValueError("coarse handoff does not copy routing/package identity")
        routing_bytes = _canonical_json_bytes(
            routing_result.model_dump(mode="json", by_alias=True, exclude_none=False)
        )
        if hashlib.sha256(routing_bytes).hexdigest() != self.routing_result_sha256:
            raise ValueError("coarse handoff routing result hash is stale")
        if len(self.items) != len(routing_result.coarse_text_selections):
            raise ValueError("coarse handoff item inventory is incomplete")
        blocks = {block.block_id: block for block in package.blocks}
        for item, selection in zip(self.items, routing_result.coarse_text_selections, strict=True):
            if item.selection != selection or item.block != blocks[selection.block_id]:
                raise ValueError("coarse handoff item is not an exact canonical copy")


VisualRoutingHandoff.model_rebuild()
