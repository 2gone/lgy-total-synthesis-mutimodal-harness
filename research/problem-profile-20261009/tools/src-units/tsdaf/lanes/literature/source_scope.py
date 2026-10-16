"""Host-compiled, pathless source views for bounded literature work units."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string, _unique
from tsdaf.contracts.package import (
    BLOCK_ID_PATTERN,
    GROUP_ID_PATTERN,
    VISUAL_ID_PATTERN,
    CanonicalPaperPackage,
    TextBlockType,
)
from tsdaf.contracts.routing import VisualDecision
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import BoundingBox
from tsdaf.lanes.document.reading_units import partition_clean_view
from tsdaf.lanes.literature.citation_markers import citation_marker_spans
from tsdaf.lanes.literature.lane_models import TextFragmentSelection, VisualRegionSelection
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans
from tsdaf.lanes.literature.source_inventory_models import (
    EXPERIMENTAL_PART_EXCLUSION,
    LiteratureSourceInventory,
)
from tsdaf.lanes.literature.text_extent import resolve_text_read_extent

from .bootstrap import VerifiedRouterHandoffs

__all__ = (
    "WORK_UNIT_MEDIA_TYPE",
    "ArtifactIdentity",
    "ReadAuthority",
    "RouteGroupAnchor",
    "RouteGroupAssignment",
    "RouteSeedScope",
    "ScientificSelectorKind",
    "SelectedSubjectScope",
    "TextSourceSlice",
    "VerifiedWorkUnitInput",
    "VisualSourceSlice",
    "WorkUnitKind",
    "compile_root_work_unit",
    "numbered_text_source",
    "resolve_route_scope",
)

_ROUTE_GROUP_RE = re.compile(r"RG([0-9]{3})")
# The package-local id syntax is the contract's, written once (E83): a block of an SI
# package is SI-P####-B####, and a work unit's ids are checked by the same patterns
# the canonicalizer spells them with.
_BLOCK_ID_RE = BLOCK_ID_PATTERN
_VISUAL_ID_RE = VISUAL_ID_PATTERN
_WORK_UNIT_ID_RE = re.compile(r"LWU-[0-9a-f]{64}")
_BOOTSTRAP_ID_RE = re.compile(r"LBOOT-[0-9a-f]{64}")
WORK_UNIT_MEDIA_TYPE = "application/vnd.tsdaf.literature-work-unit-input+json"
_CONTEXT_VISUAL_DECISIONS = frozenset(
    {
        VisualDecision.MECHANISM,
        VisualDecision.RETROSYNTHESIS,
        VisualDecision.OPTIMIZATION_SCREENING,
        VisualDecision.FAILED_ONLY_ROUTE,
    }
)


class WorkUnitKind(StrEnum):
    ROUTE_GROUP = "ROUTE_GROUP"
    REACTION_WINDOW = "REACTION_WINDOW"
    REACTION_UNIT = "REACTION_UNIT"


class ScientificSelectorKind(StrEnum):
    ROUTE_GROUP = "ROUTE_GROUP"
    AUTHOR_LABEL_WINDOW = "AUTHOR_LABEL_WINDOW"
    LOCAL_SUBJECT_SET = "LOCAL_SUBJECT_SET"


class ArtifactIdentity(ContractModel):
    """Public-safe artifact facts without a locator."""

    sha256: str
    media_type: str
    byte_size: int = Field(ge=0)

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, label="source asset hash")

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, value: str) -> str:
        return _stable_string(value, label="source asset media type")


class RouteGroupAnchor(ContractModel):
    """One source the Router assigned to this route group, and what it assigned it as."""

    source_id: str
    role: str

    @field_validator("source_id", "role")
    @classmethod
    def _stable(cls, value: str) -> str:
        return _stable_string(value, label="route group anchor")


class RouteGroupAssignment(ContractModel):
    """What the Router decided this route group is *for*.

    Read authority answers what a lane may read; it does not answer what the lane is
    supposed to do with it. Those were the same question only while authority was the
    Router's handful of blocks -- once a group may read the whole cleaned article, a
    lane given nothing but a route group id has no way to tell which synthesis it is
    tracing, and two groups over one document would receive identical instructions.

    So the Router's own judgement travels with the work unit: the targets this route
    makes, how it makes them, and which sources it thought each part of the story
    lived in. None of it bounds what may be read. It says what to look for.
    """

    target_names: tuple[str, ...]
    synthesis_type: str
    attributes: tuple[str, ...]
    text_anchors: tuple[RouteGroupAnchor, ...]
    visual_anchors: tuple[RouteGroupAnchor, ...]

    @field_validator("synthesis_type")
    @classmethod
    def _kind(cls, value: str) -> str:
        return _stable_string(value, label="route group synthesis type")

    @field_validator("target_names", "attributes")
    @classmethod
    def _names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="route group assignment values")
        for item in value:
            _stable_string(item, label="route group assignment value")
        return value

    @model_validator(mode="after")
    def _anchor_identity(self) -> Self:
        for anchors, label in ((self.text_anchors, "text"), (self.visual_anchors, "visual")):
            _unique(
                tuple((item.source_id, item.role) for item in anchors),
                label=f"route group {label} anchors",
            )
        return self


class ReadingPlan(ContractModel):
    """A scoped evidence question, not a required workflow round."""

    lanes: Literal["TEXT", "VISUAL", "BOTH"] = Field(
        description="Which readers the plan asks for: the text reader, the image reader or both."
    )
    question: str = Field(min_length=1, description="The precise question the reader answers.")
    reasoning_effort: Literal["medium", "high"] = Field(
        default="medium",
        description=(
            "medium when the answer is a lookup the source states outright, high when "
            "it has to be interpreted against other evidence."
        ),
    )
    text_selections: tuple[TextFragmentSelection, ...] = Field(
        default=(),
        description=(
            "Optional sentence ranges to focus on, including a new view of a block already seen."
        ),
    )
    visual_selections: tuple[VisualRegionSelection, ...] = Field(
        default=(),
        description=(
            "Optional image regions to focus on, including a new view of an image already seen."
        ),
    )


class RouteSeedScope(ContractModel):
    bootstrap_id: str
    bootstrap_sha256: str
    route_group_id: str
    ordered_text_seed_ids: tuple[str, ...]
    ordered_visual_seed_ids: tuple[str, ...]
    ordered_context_visual_ids: tuple[str, ...] = ()
    assignment: RouteGroupAssignment

    @field_validator("bootstrap_id")
    @classmethod
    def _validate_bootstrap_id(cls, value: str) -> str:
        if _BOOTSTRAP_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid literature bootstrap id")
        return value

    @field_validator("bootstrap_sha256")
    @classmethod
    def _validate_bootstrap_hash(cls, value: str) -> str:
        return _sha256(value, label="route seed bootstrap hash")

    @field_validator("route_group_id")
    @classmethod
    def _validate_route_group(cls, value: str) -> str:
        match = _ROUTE_GROUP_RE.fullmatch(value)
        if match is None or int(match.group(1)) == 0:
            raise ValueError("invalid route group id")
        return value

    @field_validator("ordered_text_seed_ids")
    @classmethod
    def _validate_text_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="route text seed ids")
        if any(_BLOCK_ID_RE.fullmatch(item) is None for item in value):
            raise ValueError("route scope contains an invalid text seed id")
        return value

    @field_validator("ordered_visual_seed_ids", "ordered_context_visual_ids")
    @classmethod
    def _validate_visual_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="route visual seed ids")
        if any(_VISUAL_ID_RE.fullmatch(item) is None for item in value):
            raise ValueError("route scope contains an invalid visual seed id")
        return value


class ReadAuthority(ContractModel):
    text_block_ids: tuple[str, ...]
    visual_ids: tuple[str, ...]

    @field_validator("text_block_ids")
    @classmethod
    def _validate_text_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="read authority text ids")
        if any(_BLOCK_ID_RE.fullmatch(item) is None for item in value):
            raise ValueError("read authority contains an invalid text id")
        return value

    @field_validator("visual_ids")
    @classmethod
    def _validate_visual_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="read authority visual ids")
        if any(_VISUAL_ID_RE.fullmatch(item) is None for item in value):
            raise ValueError("read authority contains an invalid visual id")
        return value

    @model_validator(mode="after")
    def _validate_nonempty(self) -> Self:
        if not self.text_block_ids and not self.visual_ids:
            raise ValueError("read authority requires at least one source")
        return self


class SelectedSubjectScope(ContractModel):
    work_unit_kind: WorkUnitKind
    selector_kind: ScientificSelectorKind
    selector_values: tuple[str, ...]
    local_subject_keys: tuple[str, ...]

    @field_validator("selector_values")
    @classmethod
    def _validate_selector_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("scientific selector values must be nonempty")
        _unique(value, label="scientific selector values")
        for item in value:
            _stable_string(item, label="scientific selector value")
        return value

    @field_validator("local_subject_keys")
    @classmethod
    def _validate_local_subject_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="local subject keys")
        for item in value:
            _stable_string(item, label="local subject key")
        return value


class TextSourceSlice(ContractModel):
    block_id: str
    page_index: int = Field(ge=0, le=9999)
    reading_order: int = Field(ge=0, le=9999)
    block_type: TextBlockType
    section_path: tuple[str, ...]
    raw_text: str
    raw_text_sha256: str
    # Where this block's reference markers are, as half-open offsets into `raw_text`.
    # The text itself is never rewritten: an observation quotes `raw_text` by offset and
    # a published quote has to be the paper's own bytes.
    citation_marker_spans: tuple[tuple[int, int], ...] = ()

    @field_validator("block_id")
    @classmethod
    def _validate_block_id(cls, value: str) -> str:
        if _BLOCK_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid text source block id")
        return value

    @field_validator("section_path")
    @classmethod
    def _validate_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="text source section path")
        for item in value:
            _stable_string(item, label="text source section path element")
        return value

    @field_validator("raw_text_sha256")
    @classmethod
    def _validate_text_hash(cls, value: str) -> str:
        return _sha256(value, label="raw text hash")

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        expected = hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()
        if self.raw_text_sha256 != expected:
            raise ValueError("raw text hash does not match canonical UTF-8 text")
        previous = 0
        for start, end in self.citation_marker_spans:
            if not 0 <= start < end <= len(self.raw_text) or start < previous:
                raise ValueError("citation marker spans must be ordered, disjoint and in range")
            previous = end
        return self


def numbered_text_source(source: TextSourceSlice) -> dict[str, object]:
    """Project exact source identity and the numbered sentences a reader can cite."""
    value = source.model_dump(mode="json", exclude={"raw_text", "raw_text_sha256"})
    value["sentences"] = [
        {"sentence_index": index, "text": span.text}
        for index, span in enumerate(block_sentence_spans(source.raw_text))
    ]
    return value


class VisualSourceSlice(ContractModel):
    visual_id: str
    visual_group_id: str
    page_index: int = Field(ge=0, le=9999)
    bbox: BoundingBox | None
    dimensions: tuple[int, int]
    source_asset: ArtifactIdentity
    # Opaque canonical source text: preserve whitespace and control characters.
    native_caption: str | None
    group_caption: str | None
    label_candidates: tuple[str, ...]
    caption_block_ids: tuple[str, ...]

    @field_validator("visual_id")
    @classmethod
    def _validate_visual_id(cls, value: str) -> str:
        if _VISUAL_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid visual source id")
        return value

    @field_validator("visual_group_id")
    @classmethod
    def _validate_visual_group_id(cls, value: str) -> str:
        if GROUP_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid visual source group id")
        return value

    @field_validator("dimensions")
    @classmethod
    def _validate_dimensions(cls, value: tuple[int, int]) -> tuple[int, int]:
        if value[0] <= 0 or value[1] <= 0:
            raise ValueError("visual source dimensions must be positive")
        return value

    @field_validator("label_candidates")
    @classmethod
    def _validate_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="visual source labels")
        for item in value:
            _stable_string(item, label="visual source label")
        return value

    @field_validator("caption_block_ids")
    @classmethod
    def _validate_caption_blocks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, label="visual source caption block ids")
        if any(_BLOCK_ID_RE.fullmatch(item) is None for item in value):
            raise ValueError("visual source contains an invalid caption block id")
        return value


def _work_unit_projection(value: VerifiedWorkUnitInput) -> dict[str, object]:
    projection: dict[str, object] = {
        "paper_id": value.paper_id,
        "document_revision_id": value.document_revision_id,
        "source_phase": value.source_phase,
        "package_sha256": value.package_sha256,
        "route_group_id": value.route_group_id,
        "depth": value.depth,
        "parent_work_unit_id": value.parent_work_unit_id,
        "read_authority": value.read_authority.model_dump(mode="json"),
        "route_group_assignment": value.route_group_assignment.model_dump(mode="json"),
        "selected_subject_scope": value.selected_subject_scope.model_dump(mode="json"),
        "source_order": value.source_order,
        "ordinal": value.ordinal,
    }
    return projection


def _source_view_projection(value: VerifiedWorkUnitInput) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "work_unit_sha256": value.work_unit_sha256,
        "bootstrap_id": value.bootstrap_id,
        "bootstrap_sha256": value.bootstrap_sha256,
        "text_sources": [source.model_dump(mode="json") for source in value.text_sources],
        "visual_sources": [source.model_dump(mode="json") for source in value.visual_sources],
    }


class VerifiedWorkUnitInput(ContractModel):
    schema_version: Literal["tsdaf.verified-work-unit-input.v1"]
    work_unit_id: str
    work_unit_sha256: str
    source_view_sha256: str
    paper_id: str
    document_revision_id: str
    source_phase: Literal["MAIN"]
    package_sha256: str
    bootstrap_id: str
    bootstrap_sha256: str
    route_group_id: str
    depth: int = Field(ge=1)
    parent_work_unit_id: str | None
    read_authority: ReadAuthority
    route_group_assignment: RouteGroupAssignment
    selected_subject_scope: SelectedSubjectScope
    source_order: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    text_sources: tuple[TextSourceSlice, ...]
    visual_sources: tuple[VisualSourceSlice, ...]

    @field_validator("work_unit_id")
    @classmethod
    def _validate_work_unit_id(cls, value: str) -> str:
        if _WORK_UNIT_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid full literature work-unit id")
        return value

    @field_validator("work_unit_sha256", "source_view_sha256", "package_sha256", "bootstrap_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="literature work-unit hash")

    @field_validator("paper_id", "document_revision_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="literature work-unit identity")

    @field_validator("bootstrap_id")
    @classmethod
    def _validate_bootstrap_id(cls, value: str) -> str:
        if _BOOTSTRAP_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid literature work-unit bootstrap id")
        return value

    @field_validator("route_group_id")
    @classmethod
    def _validate_route_group_id(cls, value: str) -> str:
        match = _ROUTE_GROUP_RE.fullmatch(value)
        if match is None or int(match.group(1)) == 0:
            raise ValueError("invalid literature work-unit route group")
        return value

    @field_validator("parent_work_unit_id")
    @classmethod
    def _validate_parent_id(cls, value: str | None) -> str | None:
        if value is not None and _WORK_UNIT_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid parent literature work-unit id")
        return value

    @model_validator(mode="after")
    def _validate_inventory_and_hashes(self) -> Self:
        if (self.depth == 1) != (self.parent_work_unit_id is None):
            raise ValueError("work-unit depth and parent relationship are invalid")
        if tuple(source.block_id for source in self.text_sources) != (
            self.read_authority.text_block_ids
        ):
            raise ValueError("text slices do not exactly match read authority ids and order")
        if tuple(source.visual_id for source in self.visual_sources) != (
            self.read_authority.visual_ids
        ):
            raise ValueError("visual slices do not exactly match read authority ids and order")
        work_digest = hashlib.sha256(canonical_json_bytes(_work_unit_projection(self))).hexdigest()
        if self.work_unit_sha256 != work_digest or self.work_unit_id != f"LWU-{work_digest}":
            raise ValueError("literature work-unit identity does not match its projection")
        view_digest = hashlib.sha256(
            canonical_json_bytes(_source_view_projection(self))
        ).hexdigest()
        if self.source_view_sha256 != view_digest:
            raise ValueError("literature source-view hash does not match its projection")
        return self


def _validated_package(package: CanonicalPaperPackage) -> CanonicalPaperPackage:
    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    return CanonicalPaperPackage.model_validate(
        package.model_dump(mode="python", by_alias=True, exclude_none=False)
    )


def _validated_subject_scope(scope: SelectedSubjectScope) -> SelectedSubjectScope:
    if type(scope) is not SelectedSubjectScope:
        raise TypeError("selected_subject_scope must be a SelectedSubjectScope")
    return SelectedSubjectScope.model_validate(
        scope.model_dump(mode="python", by_alias=True, exclude_none=False)
    )


def resolve_route_scope(
    *,
    package: CanonicalPaperPackage,
    handoffs: VerifiedRouterHandoffs,
    route_group_id: str,
) -> RouteSeedScope:
    """Recover one route's authority from exact typed Router handoff artifacts."""

    match = _ROUTE_GROUP_RE.fullmatch(route_group_id) if isinstance(route_group_id, str) else None
    if match is None or int(match.group(1)) == 0:
        raise ValueError("invalid route group id")
    bootstrap = handoffs.bootstrap
    if (
        bootstrap.paper_id != package.manifest.paper_id
        or bootstrap.package_sha256 != package.canonical_content_sha256
    ):
        raise ValueError("bootstrap paper/package identity does not match canonical package")
    visual, coarse = handoffs.visual, handoffs.coarse

    matching_groups = tuple(
        group for group in visual.route_groups if group.route_group_id == route_group_id
    )
    if len(matching_groups) != 1:
        raise ValueError("typed handoffs must contain exactly one requested route group")
    text_ids = tuple(
        dict.fromkeys(
            item.block.block_id
            for item in coarse.items
            if item.selection.route_group_id == route_group_id
        )
    )
    if not text_ids:
        raise ValueError("typed coarse handoff is missing the requested route group")
    visual_ids = matching_groups[0].ordered_visual_ids
    routed_group = next(
        group for group in handoffs.routing.route_groups if group.route_group_id == route_group_id
    )
    visual_roles = {
        decision.visual_id: str(decision.final_decision)
        for decision in handoffs.routing.visual_decisions
    }
    context_ids = tuple(
        decision.visual_id
        for decision in handoffs.routing.visual_decisions
        if decision.final_decision in _CONTEXT_VISUAL_DECISIONS
    )
    return RouteSeedScope(
        bootstrap_id=bootstrap.bootstrap_id,
        bootstrap_sha256=bootstrap.bootstrap_sha256,
        route_group_id=route_group_id,
        ordered_text_seed_ids=text_ids,
        ordered_visual_seed_ids=visual_ids,
        ordered_context_visual_ids=context_ids,
        assignment=RouteGroupAssignment(
            target_names=routed_group.target_names,
            synthesis_type=str(routed_group.synthesis_type),
            attributes=tuple(str(item) for item in routed_group.attributes),
            text_anchors=tuple(
                RouteGroupAnchor(source_id=item.block_id, role=str(item.role))
                for item in handoffs.routing.coarse_text_selections
                if item.route_group_id == route_group_id
            ),
            visual_anchors=tuple(
                RouteGroupAnchor(source_id=visual_id, role=visual_roles[visual_id])
                for visual_id in visual_ids
            )
            + tuple(
                RouteGroupAnchor(source_id=visual_id, role=f"CONTEXT_{visual_roles[visual_id]}")
                for visual_id in context_ids
            ),
        ),
    )


def _ordered_subset(
    requested: tuple[str, ...],
    allowed: tuple[str, ...],
    *,
    label: str,
) -> tuple[str, ...]:
    if type(requested) is not tuple or any(type(item) is not str for item in requested):
        raise TypeError(f"{label} must be a tuple of strings")
    if len(requested) != len(set(requested)):
        raise ValueError(f"{label} contains duplicates")
    positions = {item: index for index, item in enumerate(allowed)}
    if any(item not in positions for item in requested):
        raise ValueError(f"{label} contains ids outside authorized route seeds")
    indices = tuple(positions[item] for item in requested)
    if indices != tuple(sorted(indices)):
        raise ValueError(f"{label} must be an ordered subset of authorized route seeds")
    return requested


def _text_slices(
    package: CanonicalPaperPackage,
    block_ids: tuple[str, ...],
) -> tuple[TextSourceSlice, ...]:
    by_id = {block.block_id: block for block in package.blocks}
    return tuple(
        TextSourceSlice(
            block_id=by_id[block_id].block_id,
            page_index=by_id[block_id].page_index,
            reading_order=by_id[block_id].reading_order,
            block_type=by_id[block_id].block_type,
            section_path=by_id[block_id].section_path,
            raw_text=by_id[block_id].raw_text,
            raw_text_sha256=hashlib.sha256(by_id[block_id].raw_text.encode("utf-8")).hexdigest(),
            citation_marker_spans=citation_marker_spans(by_id[block_id].raw_text),
        )
        for block_id in block_ids
    )


def _visual_slices(
    package: CanonicalPaperPackage,
    visual_ids: tuple[str, ...],
) -> tuple[VisualSourceSlice, ...]:
    visual_by_id = {visual.visual_id: visual for visual in package.visual_assets}
    group_by_id = {group.visual_group_id: group for group in package.visual_groups}
    slices: list[VisualSourceSlice] = []
    for visual_id in visual_ids:
        visual = visual_by_id[visual_id]
        if visual.width is None or visual.height is None:
            raise ValueError("authorized visual source has no canonical dimensions")
        group = group_by_id[visual.visual_group_id]
        slices.append(
            VisualSourceSlice(
                visual_id=visual.visual_id,
                visual_group_id=visual.visual_group_id,
                page_index=visual.page_index,
                bbox=visual.bbox,
                dimensions=(visual.width, visual.height),
                source_asset=ArtifactIdentity(
                    sha256=visual.asset.sha256,
                    media_type=visual.asset.media_type,
                    byte_size=visual.asset.byte_size,
                ),
                native_caption=visual.native_caption,
                group_caption=group.caption_text,
                label_candidates=group.label_candidates,
                caption_block_ids=group.caption_block_ids,
            )
        )
    return tuple(slices)


def _build_work_unit(
    *,
    package: CanonicalPaperPackage,
    bootstrap_id: str,
    bootstrap_sha256: str,
    document_revision_id: str,
    route_group_id: str,
    depth: int,
    parent_work_unit_id: str | None,
    read_authority: ReadAuthority,
    route_group_assignment: RouteGroupAssignment,
    selected_subject_scope: SelectedSubjectScope,
    source_order: int,
    ordinal: int,
) -> VerifiedWorkUnitInput:
    text_sources = _text_slices(package, read_authority.text_block_ids)
    visual_sources = _visual_slices(package, read_authority.visual_ids)
    provisional = VerifiedWorkUnitInput.model_construct(
        schema_version="tsdaf.verified-work-unit-input.v1",
        work_unit_id="LWU-" + "0" * 64,
        work_unit_sha256="0" * 64,
        source_view_sha256="0" * 64,
        paper_id=package.manifest.paper_id,
        document_revision_id=document_revision_id,
        source_phase="MAIN",
        package_sha256=package.canonical_content_sha256,
        bootstrap_id=bootstrap_id,
        bootstrap_sha256=bootstrap_sha256,
        route_group_id=route_group_id,
        depth=depth,
        parent_work_unit_id=parent_work_unit_id,
        read_authority=read_authority,
        route_group_assignment=route_group_assignment,
        selected_subject_scope=selected_subject_scope,
        source_order=source_order,
        ordinal=ordinal,
        text_sources=text_sources,
        visual_sources=visual_sources,
    )
    work_digest = hashlib.sha256(
        canonical_json_bytes(_work_unit_projection(provisional))
    ).hexdigest()
    provisional = provisional.model_copy(
        update={
            "work_unit_id": f"LWU-{work_digest}",
            "work_unit_sha256": work_digest,
        }
    )
    source_digest = hashlib.sha256(
        canonical_json_bytes(_source_view_projection(provisional))
    ).hexdigest()
    return VerifiedWorkUnitInput.model_validate(
        {
            **provisional.model_dump(mode="python", by_alias=True, exclude_none=False),
            "source_view_sha256": source_digest,
        }
    )


def compile_root_work_unit(
    *,
    package: CanonicalPaperPackage,
    handoffs: VerifiedRouterHandoffs,
    document_revision_id: str,
    route_group_id: str,
    requested_text_block_ids: tuple[str, ...],
    requested_visual_ids: tuple[str, ...],
    selected_subject_scope: SelectedSubjectScope,
    source_order: int,
    ordinal: int,
) -> VerifiedWorkUnitInput:
    """Compile one depth-1 work unit exclusively from canonical package facts."""

    exact_package = _validated_package(package)
    exact_scope = _validated_subject_scope(selected_subject_scope)
    route_scope = resolve_route_scope(
        package=exact_package,
        handoffs=handoffs,
        route_group_id=route_group_id,
    )
    # See `text_extent` for what a root may read and why. Anything outside it is still
    # refused, and the extent is re-derived here rather than trusted from the caller.
    # The narrative part is the cleaned document before the experimental split's cut
    # (ledger E47); a Router run sealed without the split reads all of it.
    narrative_block_ids, _ = partition_clean_view(exact_package, handoffs.partition)
    extent = resolve_text_read_extent(
        package=exact_package,
        anchor_block_ids=route_scope.ordered_text_seed_ids,
        readable_block_ids=narrative_block_ids,
    )
    text_ids = _ordered_subset(
        requested_text_block_ids,
        extent.ordered_block_ids,
        label="requested text ids",
    )
    visual_ids = _ordered_subset(
        requested_visual_ids,
        (*route_scope.ordered_visual_seed_ids, *route_scope.ordered_context_visual_ids),
        label="requested visual ids",
    )
    authority = ReadAuthority(text_block_ids=text_ids, visual_ids=visual_ids)
    return _build_work_unit(
        package=exact_package,
        bootstrap_id=route_scope.bootstrap_id,
        bootstrap_sha256=route_scope.bootstrap_sha256,
        document_revision_id=document_revision_id,
        route_group_id=route_group_id,
        depth=1,
        parent_work_unit_id=None,
        read_authority=authority,
        route_group_assignment=route_scope.assignment,
        selected_subject_scope=exact_scope,
        source_order=source_order,
        ordinal=ordinal,
    )


def resolve_fusion_read_authority(
    *,
    work_unit: VerifiedWorkUnitInput,
    package: CanonicalPaperPackage,
    source_inventory: LiteratureSourceInventory | None = None,
) -> ReadAuthority:
    """Use the runtime-verified paper catalogue for ordinary source reads.

    The initial lane input and scientific edit scope do not change. This catalogue
    contains original sources only, never other investigators' findings.
    """
    if (
        work_unit.paper_id != package.manifest.paper_id
        or work_unit.package_sha256 != package.canonical_content_sha256
    ):
        raise ValueError("source reading changes work-unit package provenance")
    if source_inventory is None:
        return work_unit.read_authority
    inventory = LiteratureSourceInventory.model_validate(source_inventory.model_dump())
    if (
        inventory.paper_id != work_unit.paper_id
        or inventory.package_sha256 != work_unit.package_sha256
        or inventory.bootstrap_id != work_unit.bootstrap_id
        or inventory.bootstrap_sha256 != work_unit.bootstrap_sha256
        or work_unit.route_group_id not in inventory.route_group_ids
    ):
        raise ValueError("source catalogue changes package or Router provenance")
    expected = {
        b.block_id: (
            "TEXT_BLOCK",
            hashlib.sha256(b.raw_text.encode("utf-8")).hexdigest(),
            bool(block_sentence_spans(b.raw_text)),
            len(block_sentence_spans(b.raw_text)),
        )
        for b in package.blocks
    } | {v.visual_id: ("VISUAL_ASSET", v.asset.sha256, True, None) for v in package.visual_assets}
    actual = {
        e.source_id: (e.source_kind, e.source_sha256, e.readable, e.sentence_count)
        for e in inventory.entries
    }
    if actual != expected:
        raise ValueError("source catalogue differs from canonical source content")
    # The initial selection already has an authorized order (route seeds followed by
    # context). Add catalogue sources without invalidating that existing selection.
    # A route group's on-demand reads stay within the narrative part: the catalogue
    # marks the experimental part excluded for it (ledger E49, JOC r3: a group that
    # read the experimental section itself finished with reactions and no operations).
    withheld = {
        e.source_id
        for e in inventory.entries
        if e.initially_excluded and e.exclusion_reason == EXPERIMENTAL_PART_EXCLUSION
    }
    return ReadAuthority(
        text_block_ids=tuple(
            dict.fromkeys(
                (
                    *work_unit.read_authority.text_block_ids,
                    *(
                        b.block_id
                        for b in package.blocks
                        if expected[b.block_id][2] and b.block_id not in withheld
                    ),
                )
            )
        ),
        visual_ids=tuple(
            dict.fromkeys(
                (
                    *work_unit.read_authority.visual_ids,
                    *(v.visual_id for v in package.visual_assets),
                )
            )
        ),
    )
