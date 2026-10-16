"""Deterministic taxonomy and independent JSON Schema resource catalog."""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum

from ._base import ContractModel, _canonical_json_bytes
from .execution import (
    ExecutionStatus,
    FailureCategory,
    FailureCode,
    FailureDetail,
    StageResult,
    TypedFailure,
)
from .handoff import (
    CoarseTextHandoff,
    CoarseTextHandoffItem,
    ImageProcessingJob,
    ReleaseReceipt,
    ReviewReceipt,
    RouteImageItem,
    VisualRoutingHandoff,
)
from .package import (
    CanonicalPaperPackage,
    CanonicalTextBlock,
    CanonicalVisualAsset,
    CleanDocumentView,
    PaperManifest,
    SuppressionRecord,
    TextBlockType,
    VisualGroup,
    VisualSuppressionRecord,
)
from .routing import (
    ArticleRoutingResult,
    ArticleType,
    CoarseTextRole,
    CoarseTextSelection,
    RouteAttribute,
    RouteGroup,
    RouterProvenance,
    RoutingDisposition,
    SynthesisType,
    TargetClass,
    VisualAssetDecision,
    VisualDecision,
)
from .source import ArtifactRef, BoundingBox, SourceLocator

_ROOT_MODELS: tuple[type[ContractModel], ...] = (
    ArticleRoutingResult,
    ArtifactRef,
    BoundingBox,
    CanonicalPaperPackage,
    CanonicalTextBlock,
    CanonicalVisualAsset,
    CleanDocumentView,
    CoarseTextHandoff,
    CoarseTextHandoffItem,
    CoarseTextSelection,
    FailureDetail,
    ImageProcessingJob,
    PaperManifest,
    ReleaseReceipt,
    ReviewReceipt,
    RouteGroup,
    RouteImageItem,
    RouterProvenance,
    SourceLocator,
    StageResult,
    SuppressionRecord,
    TypedFailure,
    VisualAssetDecision,
    VisualGroup,
    VisualRoutingHandoff,
    VisualSuppressionRecord,
)

_TAXONOMY_ENUMS: tuple[type[StrEnum], ...] = (
    ArticleType,
    CoarseTextRole,
    ExecutionStatus,
    FailureCategory,
    FailureCode,
    RouteAttribute,
    RoutingDisposition,
    SynthesisType,
    TargetClass,
    TextBlockType,
    VisualDecision,
)

_DISPLAY_KEYS = frozenset({"title", "description", "examples", "$comment"})
_RUNTIME_LEAK_RE = re.compile(
    r"(?:[\"'][A-Za-z]:[/\\]|file://|/(?:home|Users|tmp)/|0x[0-9a-fA-F]{6,}|"
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
)


def _strip_display_keys(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _strip_display_keys(child)
            for key, child in value.items()
            if key not in _DISPLAY_KEYS
        }
    if isinstance(value, list):
        return [_strip_display_keys(child) for child in value]
    return value


def taxonomy_document() -> dict[str, object]:
    return {
        enum_type.__name__: [member.value for member in enum_type] for enum_type in _TAXONOMY_ENUMS
    }


def contract_schema_bundle() -> dict[str, object]:
    models: dict[str, object] = {}
    for model in _ROOT_MODELS:
        schema = model.model_json_schema(
            mode="validation",
            ref_template="#/$defs/{model}",
        )
        models[model.__name__] = _strip_display_keys(schema)
    bundle: dict[str, object] = {
        "bundle_schema_version": "tsdaf.contract-schema-bundle.v1",
        "contract_api_version": "tsdaf.contracts.v1",
        "json_schema_dialect": "https://json-schema.org/draft/2020-12/schema",
        "models": models,
        "taxonomy": taxonomy_document(),
    }
    if _RUNTIME_LEAK_RE.search(_canonical_json_bytes(bundle).decode("utf-8")):
        raise ValueError("contract schema bundle contains runtime-specific data")
    return bundle


def canonical_json_bytes(value: object) -> bytes:
    return _canonical_json_bytes(value)


def contract_schema_bytes() -> bytes:
    return canonical_json_bytes(contract_schema_bundle())


def contract_schema_sha256() -> str:
    return hashlib.sha256(contract_schema_bytes()).hexdigest()
