"""Domain tests for the explicit deterministic contract schema catalog."""

from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

EXPORTS = (
    "ArtifactRef",
    "BoundingBox",
    "SourceLocator",
    "PaperManifest",
    "CanonicalTextBlock",
    "CanonicalVisualAsset",
    "VisualGroup",
    "SuppressionRecord",
    "VisualSuppressionRecord",
    "CleanDocumentView",
    "TextBlockType",
    "CanonicalPaperPackage",
    "ArticleType",
    "RoutingDisposition",
    "RouteAttribute",
    "VisualDecision",
    "CoarseTextRole",
    "RouteGroup",
    "RouterProvenance",
    "VisualAssetDecision",
    "CoarseTextSelection",
    "ArticleRoutingResult",
    "VisualRoutingHandoff",
    "RouteImageItem",
    "ReviewReceipt",
    "ReleaseReceipt",
    "ImageProcessingJob",
    "CoarseTextHandoffItem",
    "CoarseTextHandoff",
    "ExecutionStatus",
    "FailureCode",
    "FailureCategory",
    "FailureDetail",
    "TypedFailure",
    "StageResult",
    "failure_category",
    "failure_is_retryable",
    "taxonomy_document",
    "contract_schema_bundle",
    "canonical_json_bytes",
    "contract_schema_bytes",
    "contract_schema_sha256",
)

MODEL_NAMES = (
    "ArticleRoutingResult",
    "ArtifactRef",
    "BoundingBox",
    "CanonicalPaperPackage",
    "CanonicalTextBlock",
    "CanonicalVisualAsset",
    "CleanDocumentView",
    "CoarseTextHandoff",
    "CoarseTextHandoffItem",
    "CoarseTextSelection",
    "FailureDetail",
    "ImageProcessingJob",
    "PaperManifest",
    "ReleaseReceipt",
    "ReviewReceipt",
    "RouteGroup",
    "RouteImageItem",
    "RouterProvenance",
    "SourceLocator",
    "StageResult",
    "SuppressionRecord",
    "TypedFailure",
    "VisualAssetDecision",
    "VisualGroup",
    "VisualRoutingHandoff",
    "VisualSuppressionRecord",
)

TAXONOMY = {
    "ArticleType": [
        "TOTAL_SYNTHESIS",
        "FORMAL_SYNTHESIS",
        "SEMISYNTHESIS",
        "SYNTHETIC_APPROACH",
        "NOT_IN_SCOPE",
    ],
    "CoarseTextRole": [
        "ROUTE_CORE",
        "DIRECT_OPTIMIZATION_CONTEXT",
        "DIRECT_MECHANISM_CONTEXT",
        "DIRECT_FAILED_ATTEMPT_CONTEXT",
    ],
    "ExecutionStatus": [
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "REVIEW_REQUIRED",
        "FAILED_RETRYABLE",
        "FAILED_TERMINAL",
        "CACHED",
    ],
    "FailureCategory": ["INPUT", "PROVIDER", "ROUTER", "TRIAGE", "MATERIALIZATION", "CONTROL"],
    "FailureCode": [
        "PACKAGE_INVALID",
        "ASSET_MISSING",
        "HASH_MISMATCH",
        "CONTEXT_TOO_LARGE",
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_TRANSIENT",
        "PROVIDER_REJECTED",
        "ROUTER_NO_SUBMIT",
        "ROUTER_SCHEMA_INVALID",
        "ROUTER_REFERENCE_INVALID",
        "TOOL_BUDGET_EXHAUSTED",
        "VISUAL_VERIFICATION_REQUIRED",
        "VISUAL_VERIFICATION_UNRESOLVED",
        "HANDOFF_INVALID",
        "ARTIFACT_WRITE_FAILED",
        "LIVE_REQUIRED",
    ],
    "RouteAttribute": [
        "ASYMMETRIC",
        "RACEMIC",
        "BIOMIMETIC",
        "CHEMOENZYMATIC",
        "DIVERGENT",
        "COLLECTIVE",
    ],
    "RoutingDisposition": ["ROUTED", "REVIEW_REQUIRED", "REJECTED"],
    "TextBlockType": [
        "DOCUMENT_TITLE",
        "SECTION_HEADING",
        "PARAGRAPH",
        "LIST_ITEM",
        "FIGURE_CAPTION",
        "TABLE_CAPTION",
        "FIGURE_NOTE",
        "TABLE_NOTE",
        "DISPLAY_EQUATION",
        "TABLE_TEXT",
        "FOOTNOTE",
        "PAGE_HEADER",
        "PAGE_FOOTER",
        "PAGE_NUMBER",
        "REFERENCE_ENTRY",
        "OTHER",
    ],
    "VisualDecision": [
        "FORWARD_ROUTE",
        "RETROSYNTHESIS",
        "MECHANISM",
        "OPTIMIZATION_SCREENING",
        "FAILED_ONLY_ROUTE",
        "BIOSYNTHESIS",
        "ANALYTICAL",
        "OTHER",
        "NEEDS_VISUAL_VERIFICATION",
        "UNCERTAIN",
    ],
}

REQUIRED_NULLABLE = {
    "SourceLocator": ("bbox", "native_block_index", "source_asset_ref"),
    "CanonicalVisualAsset": ("bbox", "width", "height", "native_caption"),
    "VisualGroup": ("caption_text",),
    "RouteImageItem": ("bbox", "dimensions", "figure_or_scheme_label", "caption"),
    "ReviewReceipt": ("notes",),
    "ReleaseReceipt": ("policy_version",),
    "StageResult": ("attempt_id", "output_artifact", "failure", "cached_from_result"),
}

GOLDEN = Path(__file__).parents[1] / "golden" / "contracts" / "tsdaf-contracts-v1.schema.json"
REVIEWED_GOLDEN_SHA256 = "dcd7fe3ad5b16955c33d6bc20ed272dce211fdc03d7659b2ea2137390743a155"


def _contracts() -> tuple[Any, Any]:
    root = importlib.import_module("tsdaf.contracts")
    schema = importlib.import_module("tsdaf.contracts.schema")
    return root, schema


def _refs(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "$ref":
                assert isinstance(child, str)
                yield child
            yield from _refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _refs(child)


def _assert_local_ref_closure(resource: dict[str, object]) -> None:
    definitions = resource.get("$defs", {})
    assert isinstance(definitions, dict)
    for ref in _refs(resource):
        assert ref.startswith("#/$defs/")
        name = ref.removeprefix("#/$defs/")
        assert name in definitions, f"dangling local reference: {ref}"


def test_public_exports_are_exact_unique_and_owned_by_leaf_modules() -> None:
    root = importlib.import_module("tsdaf.contracts")
    assert root.__all__ == EXPORTS
    assert len(root.__all__) == len(set(root.__all__)) == 42
    for name in EXPORTS:
        assert getattr(root, name) is not None
    assert not hasattr(root, "ContractModel")


def test_explicit_model_and_taxonomy_catalogs_have_exact_membership_and_order() -> None:
    _, schema = _contracts()
    bundle = schema.contract_schema_bundle()

    assert tuple(bundle) == (
        "bundle_schema_version",
        "contract_api_version",
        "json_schema_dialect",
        "models",
        "taxonomy",
    )
    assert bundle["bundle_schema_version"] == "tsdaf.contract-schema-bundle.v1"
    assert bundle["contract_api_version"] == "tsdaf.contracts.v1"
    assert bundle["json_schema_dialect"] == "https://json-schema.org/draft/2020-12/schema"
    assert tuple(bundle["models"]) == MODEL_NAMES
    assert tuple(bundle["taxonomy"]) == tuple(sorted(TAXONOMY))
    assert bundle["taxonomy"] == TAXONOMY
    assert "VisualSuppressionRecord" in bundle["models"]


def test_bundle_calls_are_fresh_and_subprocess_bytes_are_identical() -> None:
    _, schema = _contracts()
    first = schema.contract_schema_bundle()
    second = schema.contract_schema_bundle()
    assert first == second
    assert first is not second
    first["models"].pop("ArtifactRef")
    first["taxonomy"]["ArticleType"].append("MUTATED")
    third = schema.contract_schema_bundle()
    assert "ArtifactRef" in third["models"]
    assert "MUTATED" not in third["taxonomy"]["ArticleType"]

    command = (
        "import hashlib; from tsdaf.contracts import contract_schema_bytes; "
        "print(hashlib.sha256(contract_schema_bytes()).hexdigest())"
    )
    completed = subprocess.run(
        [sys.executable, "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stderr == ""
    assert completed.stdout.strip() == schema.contract_schema_sha256()


def test_canonical_json_bytes_reject_non_json_and_nan() -> None:
    _, schema = _contracts()
    assert schema.canonical_json_bytes({"β": (2, 1)}) == '{"β":[2,1]}'.encode()
    with pytest.raises(ValueError):
        schema.canonical_json_bytes({"value": float("nan")})
    with pytest.raises(TypeError):
        schema.canonical_json_bytes(object())


def test_every_selected_model_schema_is_its_own_closed_resource() -> None:
    _, schema = _contracts()
    bundle = schema.contract_schema_bundle()
    assert "$defs" not in bundle
    for name, resource in bundle["models"].items():
        assert "$id" not in resource, name
        _assert_local_ref_closure(resource)


def test_missing_definition_and_outer_catalog_resolution_are_rejected() -> None:
    _, schema = _contracts()
    bundle = schema.contract_schema_bundle()
    resource = json.loads(json.dumps(bundle["models"]["ArticleRoutingResult"]))
    refs = tuple(_refs(resource))
    assert refs
    first_name = refs[0].removeprefix("#/$defs/")
    resource["$defs"].pop(first_name)
    with pytest.raises(AssertionError, match="dangling local reference"):
        _assert_local_ref_closure(resource)

    with pytest.raises(AssertionError):
        _assert_local_ref_closure(bundle)


def test_required_nullable_and_default_sets_are_exact() -> None:
    root, schema = _contracts()
    bundle = schema.contract_schema_bundle()
    assert sum(len(fields) for fields in REQUIRED_NULLABLE.values()) == 18
    for model_name, fields in REQUIRED_NULLABLE.items():
        required = bundle["models"][model_name]["required"]
        for field in fields:
            assert field in required

    observed: dict[tuple[str, str], object] = {}
    for model_name in MODEL_NAMES:
        model = getattr(root, model_name)
        for field_name, field in model.model_fields.items():
            if not field.is_required():
                observed[(model_name, field_name)] = field.default
    assert observed == {
        ("VisualAssetDecision", "triage_result"): None,
        ("TypedFailure", "request_id"): None,
        ("TypedFailure", "details"): (),
    }


def test_unknown_fields_fail_in_runtime_and_generated_validation_schema() -> None:
    root, schema = _contracts()
    payload = {
        "sha256": "a" * 64,
        "media_type": "application/pdf",
        "byte_size": 0,
        "relative_path": "source/paper.pdf",
        "filename": "paper.pdf",
    }
    with pytest.raises(ValidationError):
        root.ArtifactRef.model_validate(payload)
    artifact_schema = schema.contract_schema_bundle()["models"]["ArtifactRef"]
    assert artifact_schema["additionalProperties"] is False
    assert "filename" not in artifact_schema["properties"]


def test_private_base_model_does_not_silently_join_explicit_catalog() -> None:
    _, schema = _contracts()

    class UnrelatedPrivateModel(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str

    assert UnrelatedPrivateModel(value="x").value == "x"
    assert "UnrelatedPrivateModel" not in schema.contract_schema_bundle()["models"]


def test_schema_bytes_have_no_runtime_leaks_bom_or_trailing_newline() -> None:
    _, schema = _contracts()
    payload = schema.contract_schema_bytes()
    text = payload.decode("utf-8")

    assert not payload.startswith(b"\xef\xbb\xbf")
    assert not payload.endswith(b"\n")
    assert not re.search(r"[\"'][A-Za-z]:[/\\]", text)
    assert not re.search(r"0x[0-9a-fA-F]{6,}", text)
    assert schema.contract_schema_sha256() == hashlib.sha256(payload).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", schema.contract_schema_sha256())


def test_checked_in_golden_is_exact_reviewed_derivative() -> None:
    _, schema = _contracts()
    expected = GOLDEN.read_bytes()
    actual = schema.contract_schema_bytes()
    old_sha = hashlib.sha256(expected).hexdigest()
    new_sha = hashlib.sha256(actual).hexdigest()
    if expected != actual:
        old_value = json.dumps(json.loads(expected), indent=2, sort_keys=True).splitlines()
        new_value = json.dumps(json.loads(actual), indent=2, sort_keys=True).splitlines()
        structural_diff = "\n".join(
            difflib.unified_diff(old_value, new_value, fromfile="golden", tofile="generated")
        )
        pytest.fail(f"schema golden drift: old_sha={old_sha} new_sha={new_sha}\n{structural_diff}")
    assert new_sha == REVIEWED_GOLDEN_SHA256
