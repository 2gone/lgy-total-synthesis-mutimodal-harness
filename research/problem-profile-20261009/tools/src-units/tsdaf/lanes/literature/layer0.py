"""Deterministic Layer 0 ownership reconstructed from sealed stage evidence."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string, _unique
from tsdaf.contracts.execution import ExecutionStatus, FailureCode, StageResult, TypedFailure
from tsdaf.contracts.package import _BLOCK_ID_RE, _VISUAL_ID_RE, CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.host_ports import (
    ExecutionClosureVerifier,
    VerifiedArtifactStore,
)

__all__ = (
    "CoverageGap",
    "CoverageGapReason",
    "CoverageLedger",
    "CoverageOwner",
    "DocumentSurveyOutput",
    "Layer0Shard",
    "Layer0ShardKind",
    "SurveyFailed",
    "SurveyFailureClass",
    "SurveyProviderProvenance",
    "SurveySucceeded",
    "SurveyTerminal",
    "SurveyTerminalProof",
    "build_coverage_ledger",
    "plan_layer0_shards",
)

_SHARD_ID_RE = re.compile(r"L0-[TV][0-9]{4}")
_TEXT_SHARD_SIZE = 32
_VISUAL_SHARD_SIZE = 16
_STAGE_NAME = "literature-layer0-survey"
_STAGE_VERSION = "1.0.0"
_PACKAGE_MEDIA_TYPE = "application/vnd.tsdaf.canonical-paper-package+json"
_SHARD_MEDIA_TYPE = "application/vnd.tsdaf.layer0-shard+json"
_SURVEY_OUTPUT_MEDIA_TYPE = "application/vnd.tsdaf.document-survey-output+json"
_STAGE_RESULT_MEDIA_TYPE = "application/vnd.tsdaf.stage-result+json"
_MANIFEST_MEDIA_TYPE = "application/json"


class Layer0ShardKind(StrEnum):
    TEXT = "TEXT"
    VISUAL_INVENTORY = "VISUAL_INVENTORY"


class SurveyFailureClass(StrEnum):
    CONTRACT = "CONTRACT"
    INPUT = "INPUT"
    PROVIDER = "PROVIDER"
    MODEL_SEMANTIC = "MODEL_SEMANTIC"
    HOST_VALIDATION = "HOST_VALIDATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"


class CoverageGapReason(StrEnum):
    REFERENCE_UNRESOLVED = "REFERENCE_UNRESOLVED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    MODEL_UNRESOLVED = "MODEL_UNRESOLVED"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"


def _shard_digest(
    *,
    package_sha256: str,
    shard_id: str,
    shard_kind: Layer0ShardKind | str,
    owned_block_ids: tuple[str, ...],
    owned_visual_ids: tuple[str, ...],
    read_only_overlap_block_ids: tuple[str, ...],
    read_authority_block_ids: tuple[str, ...],
    shard_ordinal: int,
) -> str:
    projection = {
        "schema_version": "tsdaf.layer0-shard-identity.v1",
        "package_sha256": package_sha256,
        "shard_id": shard_id,
        "shard_kind": str(shard_kind),
        "owned_block_ids": list(owned_block_ids),
        "owned_visual_ids": list(owned_visual_ids),
        "read_only_overlap_block_ids": list(read_only_overlap_block_ids),
        "read_authority_block_ids": list(read_authority_block_ids),
        "shard_ordinal": shard_ordinal,
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _validate_block_ids(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    _unique(value, label=label)
    if any(_BLOCK_ID_RE.fullmatch(item) is None for item in value):
        raise ValueError(f"{label} contains an invalid block id")
    return value


def _validate_visual_ids(value: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    _unique(value, label=label)
    if any(_VISUAL_ID_RE.fullmatch(item) is None for item in value):
        raise ValueError(f"{label} contains an invalid visual id")
    return value


class Layer0Shard(ContractModel):
    schema_version: Literal["tsdaf.layer0-shard.v1"]
    shard_id: str
    package_sha256: str
    shard_kind: Layer0ShardKind
    owned_block_ids: tuple[str, ...]
    owned_visual_ids: tuple[str, ...]
    read_only_overlap_block_ids: tuple[str, ...]
    read_authority_block_ids: tuple[str, ...]
    shard_ordinal: int = Field(ge=1, le=9999)
    shard_sha256: str

    @field_validator("shard_id")
    @classmethod
    def _validate_shard_id(cls, value: str) -> str:
        if _SHARD_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid Layer 0 shard id")
        return value

    @field_validator("package_sha256", "shard_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="Layer 0 shard hash")

    @field_validator(
        "owned_block_ids",
        "read_only_overlap_block_ids",
        "read_authority_block_ids",
    )
    @classmethod
    def _validate_blocks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_block_ids(value, label="Layer 0 block ids")

    @field_validator("owned_visual_ids")
    @classmethod
    def _validate_visuals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_visual_ids(value, label="Layer 0 visual ids")

    @model_validator(mode="after")
    def _validate_scope_and_identity(self) -> Self:
        if self.shard_kind is Layer0ShardKind.TEXT:
            if not self.owned_block_ids or self.owned_visual_ids:
                raise ValueError("TEXT shard must own blocks and no visuals")
            if self.read_authority_block_ids != (
                *self.read_only_overlap_block_ids,
                *self.owned_block_ids,
            ):
                raise ValueError("TEXT shard read authority must be overlap followed by ownership")
            expected_prefix = "L0-T"
        else:
            if not self.owned_visual_ids or self.owned_block_ids:
                raise ValueError("VISUAL_INVENTORY shard must own visuals and no blocks")
            if self.read_only_overlap_block_ids or self.read_authority_block_ids:
                raise ValueError("visual inventory shard cannot carry text read authority")
            expected_prefix = "L0-V"
        if not self.shard_id.startswith(expected_prefix):
            raise ValueError("shard id prefix contradicts shard kind")
        if set(self.owned_block_ids).intersection(self.read_only_overlap_block_ids):
            raise ValueError("owned and read-only blocks overlap")
        expected = _shard_digest(
            package_sha256=self.package_sha256,
            shard_id=self.shard_id,
            shard_kind=self.shard_kind,
            owned_block_ids=self.owned_block_ids,
            owned_visual_ids=self.owned_visual_ids,
            read_only_overlap_block_ids=self.read_only_overlap_block_ids,
            read_authority_block_ids=self.read_authority_block_ids,
            shard_ordinal=self.shard_ordinal,
        )
        if self.shard_sha256 != expected:
            raise ValueError("Layer 0 shard hash does not match its deterministic scope")
        return self


class CoverageGap(ContractModel):
    gap_id: str
    reason: CoverageGapReason
    block_ids: tuple[str, ...]
    visual_ids: tuple[str, ...]

    @field_validator("gap_id")
    @classmethod
    def _validate_gap_id(cls, value: str) -> str:
        return _stable_string(value, label="coverage gap id")

    @field_validator("block_ids")
    @classmethod
    def _validate_blocks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_block_ids(value, label="coverage gap block ids")

    @field_validator("visual_ids")
    @classmethod
    def _validate_visuals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_visual_ids(value, label="coverage gap visual ids")

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        if not self.block_ids and not self.visual_ids:
            raise ValueError("coverage gap must identify a source scope")
        return self


class SurveyProviderProvenance(ContractModel):
    provider_id: str
    model_id: str
    model_profile: str
    provider_profile_version: str
    provider_profile_sha256: str
    prompt_sha256: str
    schema_sha256: str

    @field_validator(
        "provider_id",
        "model_id",
        "model_profile",
        "provider_profile_version",
    )
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="survey provider identity")

    @field_validator("provider_profile_sha256", "prompt_sha256", "schema_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="survey provider provenance hash")


class DocumentSurveyOutput(ContractModel):
    """Canonical typed result owned by the Layer 0 survey stage."""

    schema_version: Literal["tsdaf.document-survey-output.v1"]
    package_sha256: str
    shard_id: str
    shard_sha256: str
    seed_block_ids: tuple[str, ...]
    figure_or_scheme_refs: tuple[str, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    provider_provenance: SurveyProviderProvenance | None

    @field_validator("package_sha256", "shard_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="document survey hash")

    @field_validator("shard_id")
    @classmethod
    def _validate_shard_id(cls, value: str) -> str:
        if _SHARD_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid document survey shard id")
        return value

    @field_validator("seed_block_ids")
    @classmethod
    def _validate_seeds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_block_ids(value, label="document survey seed ids")

    @field_validator("figure_or_scheme_refs")
    @classmethod
    def _validate_figures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_visual_ids(value, label="document survey figure refs")

    @model_validator(mode="after")
    def _reserve_terminal_failure(self) -> Self:
        if any(gap.reason is CoverageGapReason.TERMINAL_FAILURE for gap in self.coverage_gaps):
            raise ValueError("TERMINAL_FAILURE is reserved for Host-derived terminal evidence")
        return self


class SurveyTerminalProof(ContractModel):
    """Proof-only caller boundary; scientific facts are read from the sealed output."""

    schema_version: Literal["tsdaf.layer0-survey-terminal-proof.v1"]
    package_sha256: str
    shard_id: str
    shard_sha256: str
    execution_id: str
    terminal_manifest_ref: ArtifactRef
    prompt_sha256: str | None
    schema_sha256: str | None
    model_profile: str | None

    @field_validator("package_sha256", "shard_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="survey proof hash")

    @field_validator("prompt_sha256", "schema_sha256")
    @classmethod
    def _validate_optional_hash(cls, value: str | None) -> str | None:
        if value is not None:
            return _sha256(value, label="survey proof runtime hash")
        return value

    @field_validator("shard_id")
    @classmethod
    def _validate_shard_id(cls, value: str) -> str:
        if _SHARD_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid survey proof shard id")
        return value

    @field_validator("execution_id")
    @classmethod
    def _validate_execution_id(cls, value: str) -> str:
        return _stable_string(value, label="survey proof execution id")

    @field_validator("model_profile")
    @classmethod
    def _validate_model_profile(cls, value: str | None) -> str | None:
        if value is not None:
            return _stable_string(value, label="survey proof model profile")
        return value

    @model_validator(mode="after")
    def _validate_runtime_tuple(self) -> Self:
        present = tuple(
            value is not None
            for value in (self.prompt_sha256, self.schema_sha256, self.model_profile)
        )
        if len(set(present)) != 1:
            raise ValueError("survey proof runtime identity must be wholly present or absent")
        return self


class _SurveyTerminalBase(ContractModel):
    schema_version: Literal["tsdaf.layer0-survey-terminal.v1"]
    package_sha256: str
    shard_id: str
    shard_sha256: str
    owned_block_ids: tuple[str, ...]
    owned_visual_ids: tuple[str, ...]
    read_only_overlap_block_ids: tuple[str, ...]
    read_authority_block_ids: tuple[str, ...]
    execution_id: str
    attempt_id: str
    stage_name: Literal["literature-layer0-survey"]
    stage_version: Literal["1.0.0"]
    input_content_key: str
    terminal_manifest_ref: ArtifactRef
    stage_result_artifact: ArtifactRef

    @field_validator("package_sha256", "shard_sha256", "input_content_key")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="survey terminal hash")

    @field_validator("shard_id")
    @classmethod
    def _validate_shard_id(cls, value: str) -> str:
        if _SHARD_ID_RE.fullmatch(value) is None:
            raise ValueError("invalid survey terminal shard id")
        return value

    @field_validator("execution_id", "attempt_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="survey terminal identity")

    @field_validator(
        "owned_block_ids",
        "read_only_overlap_block_ids",
        "read_authority_block_ids",
    )
    @classmethod
    def _validate_blocks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_block_ids(value, label="survey terminal block ids")

    @field_validator("owned_visual_ids")
    @classmethod
    def _validate_visuals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_visual_ids(value, label="survey terminal visual ids")

    @model_validator(mode="after")
    def _validate_authority(self) -> Self:
        if self.owned_block_ids:
            expected = (*self.read_only_overlap_block_ids, *self.owned_block_ids)
            if self.read_authority_block_ids != expected or self.owned_visual_ids:
                raise ValueError("survey terminal text authority is inconsistent")
        elif self.read_only_overlap_block_ids or self.read_authority_block_ids:
            raise ValueError("survey terminal visual scope cannot carry text authority")
        return self


class SurveySucceeded(_SurveyTerminalBase):
    terminal_kind: Literal["SUCCEEDED"]
    seed_block_ids: tuple[str, ...]
    figure_or_scheme_refs: tuple[str, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    output_artifact: ArtifactRef
    provider_provenance: SurveyProviderProvenance | None

    @field_validator("seed_block_ids")
    @classmethod
    def _validate_seeds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_block_ids(value, label="survey terminal seed ids")

    @field_validator("figure_or_scheme_refs")
    @classmethod
    def _validate_figures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_visual_ids(value, label="survey terminal figure refs")


class SurveyFailed(_SurveyTerminalBase):
    terminal_kind: Literal["FAILED"]
    failure_class: SurveyFailureClass
    failure: TypedFailure


SurveyTerminal = Annotated[SurveySucceeded | SurveyFailed, Field(discriminator="terminal_kind")]


class CoverageOwner(ContractModel):
    source_id: str
    source_kind: Literal["TEXT_BLOCK", "VISUAL_ASSET"]
    shard_id: str
    shard_sha256: str
    terminal_kind: Literal["SUCCEEDED", "FAILED"]

    @field_validator("shard_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="coverage owner shard hash")


class CoverageLedger(ContractModel):
    schema_version: Literal["tsdaf.layer0-coverage-ledger.v1"]
    paper_id: str
    package_sha256: str
    completeness: Literal["COMPLETE", "PARTIAL"]
    planned_shard_ids: tuple[str, ...]
    terminal_shard_ids: tuple[str, ...]
    terminals: tuple[SurveyTerminal, ...]
    block_owners: tuple[CoverageOwner, ...]
    visual_owners: tuple[CoverageOwner, ...]
    seed_block_ids: tuple[str, ...]
    figure_or_scheme_refs: tuple[str, ...]
    coverage_gaps: tuple[CoverageGap, ...]
    failed_shard_ids: tuple[str, ...]
    ledger_sha256: str

    @field_validator("paper_id")
    @classmethod
    def _validate_paper_id(cls, value: str) -> str:
        return _stable_string(value, label="coverage ledger paper id")

    @field_validator("package_sha256", "ledger_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="coverage ledger hash")

    @model_validator(mode="after")
    def _validate_projection(self) -> Self:
        if self.planned_shard_ids != self.terminal_shard_ids:
            raise ValueError("coverage ledger lacks an exact terminal inventory")
        if tuple(item.shard_id for item in self.terminals) != self.terminal_shard_ids:
            raise ValueError("coverage ledger terminal order contradicts its inventory")
        _unique(self.planned_shard_ids, label="coverage ledger shard ids")
        _unique(
            tuple(owner.source_id for owner in self.block_owners),
            label="coverage ledger block ownership",
        )
        _unique(
            tuple(owner.source_id for owner in self.visual_owners),
            label="coverage ledger visual ownership",
        )
        _unique(self.failed_shard_ids, label="coverage ledger failed shard ids")
        if (
            tuple(item.shard_id for item in self.terminals if isinstance(item, SurveyFailed))
            != self.failed_shard_ids
        ):
            raise ValueError("coverage ledger failed shard inventory is inconsistent")
        if _canonical_gaps(self.coverage_gaps) != self.coverage_gaps:
            raise ValueError("coverage ledger gaps are not canonical")
        expected = "PARTIAL" if self.failed_shard_ids or self.coverage_gaps else "COMPLETE"
        if self.completeness != expected:
            raise ValueError("coverage ledger completeness contradicts terminal evidence")
        projection = self.model_dump(mode="json", exclude={"ledger_sha256"})
        if hashlib.sha256(canonical_json_bytes(projection)).hexdigest() != self.ledger_sha256:
            raise ValueError("coverage ledger hash does not match its projection")
        return self


def _make_shard(
    *,
    package_sha256: str,
    shard_id: str,
    shard_kind: Layer0ShardKind,
    owned_block_ids: tuple[str, ...],
    owned_visual_ids: tuple[str, ...],
    overlap_block_ids: tuple[str, ...],
    shard_ordinal: int,
) -> Layer0Shard:
    authority = (*overlap_block_ids, *owned_block_ids)
    digest = _shard_digest(
        package_sha256=package_sha256,
        shard_id=shard_id,
        shard_kind=shard_kind,
        owned_block_ids=owned_block_ids,
        owned_visual_ids=owned_visual_ids,
        read_only_overlap_block_ids=overlap_block_ids,
        read_authority_block_ids=authority,
        shard_ordinal=shard_ordinal,
    )
    return Layer0Shard(
        schema_version="tsdaf.layer0-shard.v1",
        shard_id=shard_id,
        package_sha256=package_sha256,
        shard_kind=shard_kind,
        owned_block_ids=owned_block_ids,
        owned_visual_ids=owned_visual_ids,
        read_only_overlap_block_ids=overlap_block_ids,
        read_authority_block_ids=authority,
        shard_ordinal=shard_ordinal,
        shard_sha256=digest,
    )


def plan_layer0_shards(
    package: CanonicalPaperPackage,
    *,
    text_shard_size: int = _TEXT_SHARD_SIZE,
    visual_shard_size: int = _VISUAL_SHARD_SIZE,
    text_overlap_blocks: int = 1,
) -> tuple[Layer0Shard, ...]:
    """Assign every canonical source once and grant deterministic readonly text overlap."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    for value, label, allow_zero in (
        (text_shard_size, "text_shard_size", False),
        (visual_shard_size, "visual_shard_size", False),
        (text_overlap_blocks, "text_overlap_blocks", True),
    ):
        minimum = 0 if allow_zero else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{label} must be an integer >= {minimum}")

    planned: list[Layer0Shard] = []
    block_ids = tuple(block.block_id for block in package.blocks)
    for index, start in enumerate(range(0, len(block_ids), text_shard_size), start=1):
        overlap_start = max(0, start - text_overlap_blocks)
        planned.append(
            _make_shard(
                package_sha256=package.canonical_content_sha256,
                shard_id=f"L0-T{index:04d}",
                shard_kind=Layer0ShardKind.TEXT,
                owned_block_ids=block_ids[start : start + text_shard_size],
                owned_visual_ids=(),
                overlap_block_ids=block_ids[overlap_start:start],
                shard_ordinal=index,
            )
        )
    visual_ids = tuple(visual.visual_id for visual in package.visual_assets)
    for index, start in enumerate(range(0, len(visual_ids), visual_shard_size), start=1):
        planned.append(
            _make_shard(
                package_sha256=package.canonical_content_sha256,
                shard_id=f"L0-V{index:04d}",
                shard_kind=Layer0ShardKind.VISUAL_INVENTORY,
                owned_block_ids=(),
                owned_visual_ids=visual_ids[start : start + visual_shard_size],
                overlap_block_ids=(),
                shard_ordinal=index,
            )
        )
    return tuple(planned)


def _canonical_model_bytes(model: ContractModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json", by_alias=True, exclude_none=False))


def _canonical_json_object(store: VerifiedArtifactStore, ref: ArtifactRef) -> dict[str, object]:
    store.verify(ref)
    payload = store.read_bytes(ref)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Layer 0 manifest is not valid JSON") from error
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise ValueError("Layer 0 manifest is not canonical JSON")
    return value


def _artifact(value: object, *, label: str) -> ArtifactRef:
    try:
        return ArtifactRef.model_validate(value)
    except ValueError as error:
        raise ValueError(f"{label} is not a typed artifact reference") from error


def _canonical_stage_result(store: VerifiedArtifactStore, ref: ArtifactRef) -> StageResult:
    if ref.media_type != _STAGE_RESULT_MEDIA_TYPE:
        raise ValueError("Layer 0 stage result has the wrong media type")
    store.verify(ref)
    payload = store.read_bytes(ref)
    result = StageResult.model_validate_json(payload)
    if _canonical_model_bytes(result) != payload:
        raise ValueError("Layer 0 stage result is not canonical JSON")
    return result


def _canonical_survey_output(
    store: VerifiedArtifactStore,
    ref: ArtifactRef,
) -> DocumentSurveyOutput:
    if ref.media_type != _SURVEY_OUTPUT_MEDIA_TYPE:
        raise ValueError("Layer 0 survey output has the wrong media type")
    store.verify(ref)
    payload = store.read_bytes(ref)
    output = DocumentSurveyOutput.model_validate_json(payload)
    if _canonical_model_bytes(output) != payload:
        raise ValueError("Layer 0 survey output is not canonical JSON")
    return output


def _identity(payload: bytes, media_type: str) -> tuple[str, str, int]:
    return hashlib.sha256(payload).hexdigest(), media_type, len(payload)


def _require_artifact_identity(
    closure: tuple[ArtifactRef, ...],
    expected: tuple[str, str, int],
    *,
    label: str,
) -> ArtifactRef:
    matches = tuple(
        ref for ref in closure if (ref.sha256, ref.media_type, ref.byte_size) == expected
    )
    if len(matches) != 1:
        raise ValueError(f"sealed Layer 0 execution does not bind exact {label}")
    return matches[0]


def _content_key(
    *,
    package_ref: ArtifactRef,
    shard_ref: ArtifactRef,
    shard: Layer0Shard,
    proof: SurveyTerminalProof,
) -> str:
    projection = {
        "schema_version": "tsdaf.stage-content-key.v1",
        "stage_name": _STAGE_NAME,
        "stage_version": _STAGE_VERSION,
        "input_sha256s": [package_ref.sha256, shard_ref.sha256],
        "settings": {
            "schema_version": "tsdaf.layer0-survey-settings.v1",
            "shard_kind": shard.shard_kind.value,
            "shard_sha256": shard.shard_sha256,
        },
        "prompt_sha256": proof.prompt_sha256,
        "schema_sha256": proof.schema_sha256,
        "model_profile": proof.model_profile,
    }
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _canonical_gaps(gaps: tuple[CoverageGap, ...]) -> tuple[CoverageGap, ...]:
    by_value = {(gap.gap_id, gap.reason.value, gap.block_ids, gap.visual_ids): gap for gap in gaps}
    ordered = tuple(by_value[key] for key in sorted(by_value))
    _unique(tuple(gap.gap_id for gap in ordered), label="coverage gap ids")
    return ordered


def _validate_plan(package: CanonicalPaperPackage, shards: tuple[Layer0Shard, ...]) -> None:
    if not shards:
        raise ValueError("Layer 0 requires at least one planned shard")
    for shard in shards:
        if not isinstance(shard, Layer0Shard):
            raise TypeError("planned_shards must contain Layer0Shard values")
        Layer0Shard.model_validate(shard.model_dump(mode="python"))
    _unique(tuple(shard.shard_id for shard in shards), label="Layer 0 planned shard ids")
    if any(shard.package_sha256 != package.canonical_content_sha256 for shard in shards):
        raise ValueError("planned shard package hash does not match canonical package")

    package_blocks = tuple(block.block_id for block in package.blocks)
    package_visuals = tuple(visual.visual_id for visual in package.visual_assets)
    owned_blocks = tuple(block for shard in shards for block in shard.owned_block_ids)
    owned_visuals = tuple(visual for shard in shards for visual in shard.owned_visual_ids)
    if owned_blocks != package_blocks:
        raise ValueError("Layer 0 block ownership is not exact canonical coverage")
    if owned_visuals != package_visuals:
        raise ValueError("Layer 0 visual ownership is not exact canonical coverage")

    block_positions = {block_id: index for index, block_id in enumerate(package_blocks)}
    for shard in shards:
        if shard.shard_kind is Layer0ShardKind.VISUAL_INVENTORY:
            continue
        if any(block_id not in block_positions for block_id in shard.read_authority_block_ids):
            raise ValueError("Layer 0 read authority names a foreign text block")
        first = block_positions[shard.owned_block_ids[0]]
        overlap_size = len(shard.read_only_overlap_block_ids)
        expected_overlap = package_blocks[max(0, first - overlap_size) : first]
        if shard.read_only_overlap_block_ids != expected_overlap:
            raise ValueError("Layer 0 readonly overlap is not the adjacent canonical context")


def _validate_proof_for_shard(
    proof: SurveyTerminalProof,
    shard: Layer0Shard,
) -> SurveyTerminalProof:
    checked = SurveyTerminalProof.model_validate(proof.model_dump(mode="python"))
    if (
        checked.package_sha256 != shard.package_sha256
        or checked.shard_id != shard.shard_id
        or checked.shard_sha256 != shard.shard_sha256
    ):
        raise ValueError("survey terminal proof identity contradicts its planned shard")
    runtime_values = (checked.prompt_sha256, checked.schema_sha256, checked.model_profile)
    if shard.shard_kind is Layer0ShardKind.TEXT and any(value is None for value in runtime_values):
        raise ValueError("TEXT survey proof must bind prompt, schema, and model profile")
    if shard.shard_kind is Layer0ShardKind.VISUAL_INVENTORY and any(
        value is not None for value in runtime_values
    ):
        raise ValueError("visual inventory proof cannot claim a model runtime")
    return checked


def _failure_class(code: FailureCode) -> SurveyFailureClass:
    mapping = {
        FailureCode.PACKAGE_INVALID: SurveyFailureClass.INPUT,
        FailureCode.ASSET_MISSING: SurveyFailureClass.INPUT,
        FailureCode.HASH_MISMATCH: SurveyFailureClass.INPUT,
        FailureCode.CONTEXT_TOO_LARGE: SurveyFailureClass.INPUT,
        FailureCode.PROVIDER_RATE_LIMIT: SurveyFailureClass.PROVIDER,
        FailureCode.PROVIDER_TRANSIENT: SurveyFailureClass.PROVIDER,
        FailureCode.PROVIDER_REJECTED: SurveyFailureClass.PROVIDER,
        FailureCode.ROUTER_NO_SUBMIT: SurveyFailureClass.MODEL_SEMANTIC,
        FailureCode.ROUTER_SCHEMA_INVALID: SurveyFailureClass.MODEL_SEMANTIC,
        FailureCode.ROUTER_REFERENCE_INVALID: SurveyFailureClass.MODEL_SEMANTIC,
        FailureCode.TOOL_BUDGET_EXHAUSTED: SurveyFailureClass.MODEL_SEMANTIC,
        FailureCode.VISUAL_VERIFICATION_REQUIRED: SurveyFailureClass.MODEL_SEMANTIC,
        FailureCode.VISUAL_VERIFICATION_UNRESOLVED: SurveyFailureClass.MODEL_SEMANTIC,
        FailureCode.HANDOFF_INVALID: SurveyFailureClass.HOST_VALIDATION,
        FailureCode.ARTIFACT_WRITE_FAILED: SurveyFailureClass.INFRASTRUCTURE,
        FailureCode.LIVE_REQUIRED: SurveyFailureClass.INFRASTRUCTURE,
    }
    return mapping[code]


def _validate_output_scope(
    *,
    output: DocumentSurveyOutput,
    shard: Layer0Shard,
    package_visual_ids: frozenset[str],
) -> tuple[CoverageGap, ...]:
    if not set(output.figure_or_scheme_refs).issubset(package_visual_ids):
        raise ValueError("survey figure or scheme reference is outside the canonical package")
    if shard.shard_kind is Layer0ShardKind.TEXT:
        if not set(output.seed_block_ids).issubset(shard.owned_block_ids):
            raise ValueError("survey seed block is outside its owned text scope")
        for gap in output.coverage_gaps:
            if gap.visual_ids or not set(gap.block_ids).issubset(shard.owned_block_ids):
                raise ValueError("survey coverage gap is outside its owned text scope")
    else:
        if output.seed_block_ids:
            raise ValueError("visual inventory survey cannot publish text seeds")
        if not set(output.figure_or_scheme_refs).issubset(shard.owned_visual_ids):
            raise ValueError("visual survey figure reference is outside its owned visual scope")
        for gap in output.coverage_gaps:
            if gap.block_ids or not set(gap.visual_ids).issubset(shard.owned_visual_ids):
                raise ValueError("survey coverage gap is outside its owned visual scope")
    return _canonical_gaps(output.coverage_gaps)


def _derive_terminal(
    *,
    package: CanonicalPaperPackage,
    shard: Layer0Shard,
    proof: SurveyTerminalProof,
    store: VerifiedArtifactStore,
    verifier: ExecutionClosureVerifier,
) -> SurveySucceeded | SurveyFailed:
    checked_proof = _validate_proof_for_shard(proof, shard)
    try:
        closure = verifier.verified_execution_artifact_closure(
            checked_proof.execution_id,
            checked_proof.terminal_manifest_ref,
        )
    except Exception as error:
        raise ValueError("Layer 0 execution manifest did not verify as a sealed closure") from error
    if checked_proof.terminal_manifest_ref not in closure:
        raise ValueError("Layer 0 terminal manifest is outside its verified execution closure")
    if checked_proof.terminal_manifest_ref.media_type != _MANIFEST_MEDIA_TYPE:
        raise ValueError("Layer 0 terminal manifest has the wrong media type")
    manifest = _canonical_json_object(store, checked_proof.terminal_manifest_ref)
    if (
        manifest.get("schema_version") != "tsdaf.local-execution-manifest.v1"
        or manifest.get("record_kind") != "terminal_execution_manifest"
        or manifest.get("execution_id") != checked_proof.execution_id
    ):
        raise ValueError("Layer 0 terminal manifest identity is invalid")

    records = manifest.get("stage_records")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("terminal execution must bind exactly one completed Layer 0 stage")
    record = records[0]
    if record.get("stage_name") != _STAGE_NAME or record.get("stage_version") != _STAGE_VERSION:
        raise ValueError("terminal execution does not bind the expected Layer 0 stage")
    stage_ref = _artifact(record.get("result_ref"), label="Layer 0 stage result")
    if stage_ref not in closure:
        raise ValueError("Layer 0 stage result is outside its terminal execution closure")
    result = _canonical_stage_result(store, stage_ref)
    if result.status is ExecutionStatus.CACHED:
        raise ValueError("CACHED Layer 0 survey has no supported producer lineage in v1")
    if result.status is ExecutionStatus.FAILED_RETRYABLE:
        raise ValueError("FAILED_RETRYABLE survey cannot cross the Layer 0 terminal barrier")
    if result.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED_TERMINAL}:
        raise ValueError("completed Layer 0 stage has an unsupported terminal status")
    if (
        result.execution_id != checked_proof.execution_id
        or result.stage_name != _STAGE_NAME
        or result.stage_version != _STAGE_VERSION
        or result.input_content_key != record.get("content_key")
        or result.status.value != record.get("status")
    ):
        raise ValueError("Layer 0 stage result contradicts its terminal manifest binding")
    expected_manifest_status = (
        ExecutionStatus.SUCCEEDED.value
        if result.status is ExecutionStatus.SUCCEEDED
        else ExecutionStatus.FAILED_TERMINAL.value
    )
    if manifest.get("status") != expected_manifest_status:
        raise ValueError("Layer 0 terminal manifest status contradicts its stage result")

    attempts = manifest.get("attempt_records")
    if not isinstance(attempts, list) or result.attempt_id is None:
        raise ValueError("terminal Layer 0 stage has no sealed attempt binding")
    matching_attempts = tuple(
        item
        for item in attempts
        if isinstance(item, dict) and item.get("attempt_id") == result.attempt_id
    )
    if len(matching_attempts) != 1:
        raise ValueError("terminal Layer 0 stage must bind one exact attempt")
    attempt_result_ref = _artifact(
        matching_attempts[0].get("result_ref"),
        label="Layer 0 attempt result",
    )
    if attempt_result_ref != stage_ref:
        raise ValueError("Layer 0 attempt and completed-stage result references diverge")

    package_payload = _canonical_model_bytes(package)
    shard_payload = _canonical_model_bytes(shard)
    package_ref = _require_artifact_identity(
        closure,
        _identity(package_payload, _PACKAGE_MEDIA_TYPE),
        label="canonical package input",
    )
    shard_ref = _require_artifact_identity(
        closure,
        _identity(shard_payload, _SHARD_MEDIA_TYPE),
        label="Layer 0 shard input",
    )
    expected_key = _content_key(
        package_ref=package_ref,
        shard_ref=shard_ref,
        shard=shard,
        proof=checked_proof,
    )
    if result.input_content_key != expected_key:
        raise ValueError("Layer 0 content key does not bind package, shard, and runtime identity")

    if result.status is ExecutionStatus.FAILED_TERMINAL:
        assert result.failure is not None
        assert result.attempt_id is not None
        return SurveyFailed(
            schema_version="tsdaf.layer0-survey-terminal.v1",
            terminal_kind="FAILED",
            package_sha256=shard.package_sha256,
            shard_id=shard.shard_id,
            shard_sha256=shard.shard_sha256,
            owned_block_ids=shard.owned_block_ids,
            owned_visual_ids=shard.owned_visual_ids,
            read_only_overlap_block_ids=shard.read_only_overlap_block_ids,
            read_authority_block_ids=shard.read_authority_block_ids,
            execution_id=checked_proof.execution_id,
            attempt_id=result.attempt_id,
            stage_name="literature-layer0-survey",
            stage_version="1.0.0",
            input_content_key=result.input_content_key,
            terminal_manifest_ref=checked_proof.terminal_manifest_ref,
            stage_result_artifact=stage_ref,
            failure_class=_failure_class(result.failure.code),
            failure=result.failure,
        )

    assert result.output_artifact is not None
    if result.output_artifact not in closure:
        raise ValueError("Layer 0 survey output is outside its terminal execution closure")
    output = _canonical_survey_output(store, result.output_artifact)
    if (
        output.package_sha256 != shard.package_sha256
        or output.shard_id != shard.shard_id
        or output.shard_sha256 != shard.shard_sha256
    ):
        raise ValueError("Layer 0 survey output identity contradicts its sealed shard")
    gaps = _validate_output_scope(
        output=output,
        shard=shard,
        package_visual_ids=frozenset(visual.visual_id for visual in package.visual_assets),
    )
    if shard.shard_kind is Layer0ShardKind.TEXT:
        provenance = output.provider_provenance
        if provenance is None or (
            provenance.prompt_sha256 != checked_proof.prompt_sha256
            or provenance.schema_sha256 != checked_proof.schema_sha256
            or provenance.model_profile != checked_proof.model_profile
        ):
            raise ValueError("TEXT survey provenance contradicts its runtime proof")
    elif output.provider_provenance is not None:
        raise ValueError("visual inventory output cannot claim model provider provenance")
    assert result.attempt_id is not None
    return SurveySucceeded(
        schema_version="tsdaf.layer0-survey-terminal.v1",
        terminal_kind="SUCCEEDED",
        package_sha256=shard.package_sha256,
        shard_id=shard.shard_id,
        shard_sha256=shard.shard_sha256,
        owned_block_ids=shard.owned_block_ids,
        owned_visual_ids=shard.owned_visual_ids,
        read_only_overlap_block_ids=shard.read_only_overlap_block_ids,
        read_authority_block_ids=shard.read_authority_block_ids,
        execution_id=checked_proof.execution_id,
        attempt_id=result.attempt_id,
        stage_name="literature-layer0-survey",
        stage_version="1.0.0",
        input_content_key=result.input_content_key,
        terminal_manifest_ref=checked_proof.terminal_manifest_ref,
        stage_result_artifact=stage_ref,
        seed_block_ids=output.seed_block_ids,
        figure_or_scheme_refs=output.figure_or_scheme_refs,
        coverage_gaps=gaps,
        output_artifact=result.output_artifact,
        provider_provenance=output.provider_provenance,
    )


def _terminal_failure_gap(
    terminal: SurveyFailed,
) -> CoverageGap:
    return CoverageGap(
        gap_id=f"FAIL-{terminal.shard_id}",
        reason=CoverageGapReason.TERMINAL_FAILURE,
        block_ids=terminal.owned_block_ids,
        visual_ids=terminal.owned_visual_ids,
    )


def build_coverage_ledger(
    *,
    package: CanonicalPaperPackage,
    planned_shards: tuple[Layer0Shard, ...],
    terminal_proofs: tuple[SurveyTerminalProof, ...],
    object_store: VerifiedArtifactStore,
    closure_verifier: ExecutionClosureVerifier,
) -> CoverageLedger:
    """Cross Layer 0 only after Host reconstruction of every sealed terminal proof."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(planned_shards, tuple) or not isinstance(terminal_proofs, tuple):
        raise TypeError("planned_shards and terminal_proofs must be tuples")
    if not isinstance(object_store, VerifiedArtifactStore) or not isinstance(
        closure_verifier, ExecutionClosureVerifier
    ):
        raise TypeError("Layer 0 barrier requires verified store and execution closure reads")
    _validate_plan(package, planned_shards)

    proof_by_shard: dict[str, SurveyTerminalProof] = {}
    for proof in terminal_proofs:
        if not isinstance(proof, SurveyTerminalProof):
            raise TypeError("terminal_proofs must contain SurveyTerminalProof values")
        if proof.shard_id in proof_by_shard:
            raise ValueError("Layer 0 terminal proof inventory contains duplicate shard ids")
        proof_by_shard[proof.shard_id] = proof
    planned_ids = tuple(shard.shard_id for shard in planned_shards)
    if set(proof_by_shard) != set(planned_ids):
        raise ValueError("Layer 0 terminal proof inventory is not exact")

    terminals = tuple(
        _derive_terminal(
            package=package,
            shard=shard,
            proof=proof_by_shard[shard.shard_id],
            store=object_store,
            verifier=closure_verifier,
        )
        for shard in planned_shards
    )
    terminal_by_shard = {terminal.shard_id: terminal for terminal in terminals}
    block_owners = tuple(
        CoverageOwner(
            source_id=block_id,
            source_kind="TEXT_BLOCK",
            shard_id=terminal.shard_id,
            shard_sha256=terminal.shard_sha256,
            terminal_kind=terminal.terminal_kind,
        )
        for terminal in terminals
        for block_id in terminal.owned_block_ids
    )
    visual_owners = tuple(
        CoverageOwner(
            source_id=visual_id,
            source_kind="VISUAL_ASSET",
            shard_id=terminal.shard_id,
            shard_sha256=terminal.shard_sha256,
            terminal_kind=terminal.terminal_kind,
        )
        for terminal in terminals
        for visual_id in terminal.owned_visual_ids
    )

    success_gaps = tuple(
        gap
        for terminal in terminals
        if isinstance(terminal, SurveySucceeded)
        for gap in terminal.coverage_gaps
    )
    failure_gaps = tuple(
        _terminal_failure_gap(terminal)
        for terminal in terminals
        if isinstance(terminal, SurveyFailed)
    )
    gaps = _canonical_gaps((*success_gaps, *failure_gaps))
    failed_ids = tuple(
        shard_id
        for shard_id in planned_ids
        if isinstance(terminal_by_shard[shard_id], SurveyFailed)
    )
    seed_set = {
        block_id
        for terminal in terminals
        if isinstance(terminal, SurveySucceeded)
        for block_id in terminal.seed_block_ids
    }
    figure_set = {
        visual_id
        for terminal in terminals
        if isinstance(terminal, SurveySucceeded)
        for visual_id in terminal.figure_or_scheme_refs
    }
    completeness: Literal["COMPLETE", "PARTIAL"] = "PARTIAL" if failed_ids or gaps else "COMPLETE"
    terminal_ids = tuple(terminal.shard_id for terminal in terminals)
    seed_ids = tuple(block.block_id for block in package.blocks if block.block_id in seed_set)
    figure_ids = tuple(
        visual.visual_id for visual in package.visual_assets if visual.visual_id in figure_set
    )
    serialized = {
        "schema_version": "tsdaf.layer0-coverage-ledger.v1",
        "paper_id": package.manifest.paper_id,
        "package_sha256": package.canonical_content_sha256,
        "completeness": completeness,
        "planned_shard_ids": list(planned_ids),
        "terminal_shard_ids": list(terminal_ids),
        "terminals": [item.model_dump(mode="json") for item in terminals],
        "block_owners": [item.model_dump(mode="json") for item in block_owners],
        "visual_owners": [item.model_dump(mode="json") for item in visual_owners],
        "seed_block_ids": list(seed_ids),
        "figure_or_scheme_refs": list(figure_ids),
        "coverage_gaps": [item.model_dump(mode="json") for item in gaps],
        "failed_shard_ids": list(failed_ids),
    }
    digest = hashlib.sha256(canonical_json_bytes(serialized)).hexdigest()
    return CoverageLedger(
        schema_version="tsdaf.layer0-coverage-ledger.v1",
        paper_id=package.manifest.paper_id,
        package_sha256=package.canonical_content_sha256,
        completeness=completeness,
        planned_shard_ids=planned_ids,
        terminal_shard_ids=terminal_ids,
        terminals=terminals,
        block_owners=block_owners,
        visual_owners=visual_owners,
        seed_block_ids=seed_ids,
        figure_or_scheme_refs=figure_ids,
        coverage_gaps=gaps,
        failed_shard_ids=failed_ids,
        ledger_sha256=digest,
    )
