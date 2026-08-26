"""Canonical paper package contracts and aggregate closure validation."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from ._base import ContractModel, _canonical_json_bytes, _sha256, _stable_string, _unique
from .source import ArtifactRef, BoundingBox, SourceLocator

_BLOCK_ID_RE = re.compile(r"MAIN-P([0-9]{4})-B([0-9]{4})")
_VISUAL_ID_RE = re.compile(r"MAIN-P([0-9]{4})-A([0-9]{4})")
_GROUP_ID_RE = re.compile(r"MAIN-P([0-9]{4})-VG([0-9]{4})")


class TextBlockType(StrEnum):
    DOCUMENT_TITLE = "DOCUMENT_TITLE"
    SECTION_HEADING = "SECTION_HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    FIGURE_CAPTION = "FIGURE_CAPTION"
    TABLE_CAPTION = "TABLE_CAPTION"
    FIGURE_NOTE = "FIGURE_NOTE"
    TABLE_NOTE = "TABLE_NOTE"
    DISPLAY_EQUATION = "DISPLAY_EQUATION"
    TABLE_TEXT = "TABLE_TEXT"
    FOOTNOTE = "FOOTNOTE"
    PAGE_HEADER = "PAGE_HEADER"
    PAGE_FOOTER = "PAGE_FOOTER"
    PAGE_NUMBER = "PAGE_NUMBER"
    REFERENCE_ENTRY = "REFERENCE_ENTRY"
    OTHER = "OTHER"


class PaperManifest(ContractModel):
    schema_version: Literal["tsdaf.paper-package.v1"]
    paper_id: str
    display_name: str
    source_pdf: ArtifactRef
    normalized_document: ArtifactRef
    normalized_blocks: ArtifactRef
    assets: tuple[ArtifactRef, ...]

    @field_validator("paper_id", "display_name")
    @classmethod
    def _validate_names(cls, value: str) -> str:
        return _stable_string(value, label="manifest identity")

    @model_validator(mode="after")
    def _validate_artifact_inventory(self) -> Self:
        references = (
            self.source_pdf,
            self.normalized_document,
            self.normalized_blocks,
            *self.assets,
        )
        if len(references) != len(set(references)):
            raise ValueError("manifest contains duplicate artifact references")
        paths = tuple(reference.relative_path for reference in references)
        _unique(paths, label="manifest artifact paths")
        return self


class CanonicalTextBlock(ContractModel):
    block_id: str
    page_index: int = Field(ge=0, le=9999)
    reading_order: int = Field(ge=0, le=9999)
    block_type: TextBlockType
    raw_text: str
    section_path: tuple[str, ...]
    source: SourceLocator

    @field_validator("block_id")
    @classmethod
    def _validate_block_id(cls, value: str) -> str:
        if _BLOCK_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid canonical text block id")
        return value

    @field_validator("section_path")
    @classmethod
    def _validate_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="section path")
        for element in value:
            _stable_string(element, label="section path element")
        return value

    @model_validator(mode="after")
    def _validate_source_and_id(self) -> Self:
        match = _BLOCK_ID_RE.fullmatch(self.block_id)
        assert match is not None
        if int(match.group(1)) != self.page_index or int(match.group(2)) != self.reading_order:
            raise ValueError("block id does not match page and reading order")
        if self.source.page_index != self.page_index:
            raise ValueError("block source page does not match block page")
        return self


class CanonicalVisualAsset(ContractModel):
    visual_id: str
    asset: ArtifactRef
    page_index: int = Field(ge=0, le=9999)
    bbox: BoundingBox | None
    width: int | None = Field(gt=0)
    height: int | None = Field(gt=0)
    native_caption: str | None
    source: SourceLocator
    visual_group_id: str

    @field_validator("visual_id")
    @classmethod
    def _validate_visual_id(cls, value: str) -> str:
        if _VISUAL_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid canonical visual id")
        return value

    @field_validator("visual_group_id")
    @classmethod
    def _validate_group_id(cls, value: str) -> str:
        if _GROUP_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid canonical visual group id")
        return value

    @model_validator(mode="after")
    def _validate_dimensions_source_and_id(self) -> Self:
        if (self.width is None) != (self.height is None):
            raise ValueError("visual width and height must both be null or both be present")
        match = _VISUAL_ID_RE.fullmatch(self.visual_id)
        assert match is not None
        if int(match.group(1)) != self.page_index:
            raise ValueError("visual id page does not match visual page")
        if self.source.page_index != self.page_index:
            raise ValueError("visual source page does not match visual page")
        if self.source.bbox != self.bbox:
            raise ValueError("visual source bounding box does not match visual bounding box")
        if self.source.source_asset_ref != self.asset:
            raise ValueError("visual source artifact does not match visual artifact")
        return self


class VisualGroup(ContractModel):
    visual_group_id: str
    member_visual_ids: Annotated[tuple[str, ...], Field(min_length=1)]
    label_candidates: tuple[str, ...]
    caption_block_ids: tuple[str, ...]
    caption_text: str | None
    association_status: Literal["CONFIRMED", "AMBIGUOUS", "NO_CAPTION"]

    @field_validator("visual_group_id")
    @classmethod
    def _validate_group_id(cls, value: str) -> str:
        if _GROUP_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid canonical visual group id")
        return value

    @field_validator("member_visual_ids")
    @classmethod
    def _validate_members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="visual group members")
        if any(_VISUAL_ID_RE.fullmatch(member) is None for member in value):
            raise ValueError("visual group contains an invalid visual id")
        return value

    @field_validator("caption_block_ids")
    @classmethod
    def _validate_caption_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="caption block ids")
        if any(_BLOCK_ID_RE.fullmatch(block_id) is None for block_id in value):
            raise ValueError("visual group contains an invalid caption block id")
        return value

    @field_validator("label_candidates")
    @classmethod
    def _validate_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="label candidates")
        for label in value:
            _stable_string(label, label="label candidate")
        return value


class SuppressionRecord(ContractModel):
    block_id: str
    reason: Literal[
        "PAGE_HEADER",
        "PAGE_FOOTER",
        "PAGE_NUMBER",
        "RUNNING_TEXT",
        "REFERENCES_SECTION",
    ]
    rule_version: str

    @field_validator("block_id")
    @classmethod
    def _validate_block_id(cls, value: str) -> str:
        if _BLOCK_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid suppressed block id")
        return value

    @field_validator("rule_version")
    @classmethod
    def _validate_rule_version(cls, value: str) -> str:
        return _stable_string(value, label="suppression rule version")


class VisualSuppressionRecord(ContractModel):
    visual_id: str
    reason: Literal["DECORATIVE_ASSET"]
    rule_version: str

    @field_validator("visual_id")
    @classmethod
    def _validate_visual_id(cls, value: str) -> str:
        if _VISUAL_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid suppressed visual id")
        return value

    @field_validator("rule_version")
    @classmethod
    def _validate_rule_version(cls, value: str) -> str:
        return _stable_string(value, label="visual suppression rule version")


class CleanDocumentView(ContractModel):
    included_block_ids: tuple[str, ...]
    suppressed: tuple[SuppressionRecord, ...]

    @field_validator("included_block_ids")
    @classmethod
    def _validate_included_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="included block ids")
        if any(_BLOCK_ID_RE.fullmatch(block_id) is None for block_id in value):
            raise ValueError("clean view contains an invalid block id")
        return value


class CanonicalPaperPackage(ContractModel):
    schema_version: Literal["tsdaf.canonical-paper-package.v1"]
    manifest: PaperManifest
    canonicalizer_version: str
    normalized_package_sha256: str
    canonical_content_sha256: str
    blocks: tuple[CanonicalTextBlock, ...]
    visual_assets: tuple[CanonicalVisualAsset, ...]
    visual_groups: tuple[VisualGroup, ...]
    visual_suppressions: tuple[VisualSuppressionRecord, ...]
    clean_view: CleanDocumentView

    @field_validator("canonicalizer_version")
    @classmethod
    def _validate_canonicalizer_version(cls, value: str) -> str:
        return _stable_string(value, label="canonicalizer version")

    @field_validator("normalized_package_sha256", "canonical_content_sha256")
    @classmethod
    def _validate_hashes(cls, value: str) -> str:
        return _sha256(value, label="package hash")

    @model_validator(mode="after")
    def _validate_package(self) -> Self:
        self._validate_hash_projections()
        block_order = self._validate_blocks()
        visual_order = self._validate_visuals()
        self._validate_groups(block_order, visual_order)
        self._validate_clean_view(block_order)
        self._validate_visual_suppressions(visual_order)
        return self

    def _validate_hash_projections(self) -> None:
        manifest_bytes = _canonical_json_bytes(
            self.manifest.model_dump(mode="json", by_alias=True, exclude_none=False)
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != self.normalized_package_sha256:
            raise ValueError("normalized package hash does not match manifest")
        projection = self.model_dump(
            mode="json",
            by_alias=True,
            exclude_none=False,
            exclude={"canonical_content_sha256"},
        )
        if (
            hashlib.sha256(_canonical_json_bytes(projection)).hexdigest()
            != self.canonical_content_sha256
        ):
            raise ValueError("canonical content hash does not match package projection")

    def _validate_blocks(self) -> dict[str, int]:
        block_ids = tuple(block.block_id for block in self.blocks)
        _unique(block_ids, label="canonical block ids")
        keys = tuple((block.page_index, block.reading_order) for block in self.blocks)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("canonical blocks are not strictly ordered")
        by_page: dict[int, list[int]] = {}
        for block in self.blocks:
            by_page.setdefault(block.page_index, []).append(block.reading_order)
        if any(orders != list(range(len(orders))) for orders in by_page.values()):
            raise ValueError("block reading order is not page-local and contiguous")
        return {block_id: index for index, block_id in enumerate(block_ids)}

    def _validate_visuals(self) -> dict[str, int]:
        visual_ids = tuple(visual.visual_id for visual in self.visual_assets)
        _unique(visual_ids, label="canonical visual ids")
        keys: list[tuple[int, int]] = []
        by_page: dict[int, list[int]] = {}
        for visual in self.visual_assets:
            match = _VISUAL_ID_RE.fullmatch(visual.visual_id)
            assert match is not None
            ordinal = int(match.group(2))
            keys.append((visual.page_index, ordinal))
            by_page.setdefault(visual.page_index, []).append(ordinal)
        if tuple(keys) != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("canonical visuals are not strictly ordered")
        if any(ordinals != list(range(len(ordinals))) for ordinals in by_page.values()):
            raise ValueError("visual asset ordinals are not page-local and contiguous")
        if tuple(visual.asset for visual in self.visual_assets) != self.manifest.assets:
            raise ValueError("visual assets do not match manifest asset inventory")
        return {visual_id: index for index, visual_id in enumerate(visual_ids)}

    def _validate_groups(self, block_order: dict[str, int], visual_order: dict[str, int]) -> None:
        group_ids = tuple(group.visual_group_id for group in self.visual_groups)
        _unique(group_ids, label="visual group ids")
        block_by_id = {block.block_id: block for block in self.blocks}
        allowed_caption_types = {
            TextBlockType.FIGURE_CAPTION,
            TextBlockType.TABLE_CAPTION,
            TextBlockType.FIGURE_NOTE,
            TextBlockType.TABLE_NOTE,
        }
        seen_members: set[str] = set()
        anchor_indices: list[int] = []
        group_ordinals_by_page: dict[int, list[int]] = {}
        inverse: dict[str, str] = {}
        for group in self.visual_groups:
            if any(member not in visual_order for member in group.member_visual_ids):
                raise ValueError("visual group has a dangling member")
            member_indices = tuple(visual_order[member] for member in group.member_visual_ids)
            if member_indices != tuple(sorted(member_indices)):
                raise ValueError("visual group members are not in canonical visual order")
            if seen_members.intersection(group.member_visual_ids):
                raise ValueError("visual groups overlap")
            seen_members.update(group.member_visual_ids)
            inverse.update(dict.fromkeys(group.member_visual_ids, group.visual_group_id))
            anchor_index = member_indices[0]
            anchor_indices.append(anchor_index)
            anchor_page = self.visual_assets[anchor_index].page_index
            match = _GROUP_ID_RE.fullmatch(group.visual_group_id)
            assert match is not None
            if int(match.group(1)) != anchor_page:
                raise ValueError("visual group id page does not match its anchor")
            group_ordinals_by_page.setdefault(anchor_page, []).append(int(match.group(2)))
            if any(block_id not in block_by_id for block_id in group.caption_block_ids):
                raise ValueError("visual group has a dangling caption block")
            caption_indices = tuple(block_order[block_id] for block_id in group.caption_block_ids)
            if caption_indices != tuple(sorted(caption_indices)):
                raise ValueError("caption blocks are not in canonical text order")
            if any(
                block_by_id[block_id].block_type not in allowed_caption_types
                for block_id in group.caption_block_ids
            ):
                raise ValueError("visual group references a non-caption block")
        if anchor_indices != sorted(anchor_indices) or len(anchor_indices) != len(
            set(anchor_indices)
        ):
            raise ValueError("visual groups are not ordered by distinct anchors")
        if any(
            ordinals != list(range(len(ordinals))) for ordinals in group_ordinals_by_page.values()
        ):
            raise ValueError("visual group ordinals are not anchor-page local and contiguous")
        if seen_members != set(visual_order):
            raise ValueError("visual groups do not partition the visual inventory")
        if any(
            inverse[visual.visual_id] != visual.visual_group_id for visual in self.visual_assets
        ):
            raise ValueError("visual group inverse membership does not match visual assets")

    def _validate_clean_view(self, block_order: dict[str, int]) -> None:
        included = self.clean_view.included_block_ids
        suppressed = tuple(record.block_id for record in self.clean_view.suppressed)
        _unique(suppressed, label="suppressed block ids")
        if any(block_id not in block_order for block_id in (*included, *suppressed)):
            raise ValueError("clean view has a dangling block reference")
        if tuple(block_order[block_id] for block_id in included) != tuple(
            sorted(block_order[block_id] for block_id in included)
        ):
            raise ValueError("included blocks are not in canonical text order")
        if tuple(block_order[block_id] for block_id in suppressed) != tuple(
            sorted(block_order[block_id] for block_id in suppressed)
        ):
            raise ValueError("suppressed blocks are not in canonical text order")
        included_set = set(included)
        suppressed_set = set(suppressed)
        if included_set.intersection(suppressed_set):
            raise ValueError("clean view included and suppressed sets overlap")
        if included_set.union(suppressed_set) != set(block_order):
            raise ValueError("clean view is not a complete block partition")

    def _validate_visual_suppressions(self, visual_order: dict[str, int]) -> None:
        visual_ids = tuple(record.visual_id for record in self.visual_suppressions)
        _unique(visual_ids, label="visual suppression ids")
        if any(visual_id not in visual_order for visual_id in visual_ids):
            raise ValueError("visual suppression has a dangling visual reference")
        indices = tuple(visual_order[visual_id] for visual_id in visual_ids)
        if indices != tuple(sorted(indices)):
            raise ValueError("visual suppressions are not in canonical visual order")
