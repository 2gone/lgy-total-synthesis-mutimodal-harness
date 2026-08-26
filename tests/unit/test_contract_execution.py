"""Domain tests for typed failures and sealed terminal stage results."""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError


def _modules() -> tuple[Any, Any]:
    execution = importlib.import_module("tsdaf.contracts.execution")
    source = importlib.import_module("tsdaf.contracts.source")
    return source, execution


def _artifact(source: Any, digit: str = "a") -> Any:
    return source.ArtifactRef(
        sha256=digit * 64,
        media_type="application/json",
        byte_size=10,
        relative_path=f"artifacts/{digit}.json",
    )


def _failure(execution: Any, code: Any) -> Any:
    return execution.TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message="Stage failed.",
        request_id=None,
        details=(),
    )


def _stage(
    status_name: str,
    *,
    review_failure: bool = False,
    offset_hours: int = 0,
) -> Any:
    source, execution = _modules()
    status = execution.ExecutionStatus[status_name]
    output = _artifact(source, "a")
    cached = _artifact(source, "b")
    attempt_id: str | None = "attempt-001"
    output_artifact: Any | None = output
    failure: Any | None = None
    cached_from_result: Any | None = None
    if status is execution.ExecutionStatus.REVIEW_REQUIRED and review_failure:
        failure = _failure(execution, execution.FailureCode.VISUAL_VERIFICATION_UNRESOLVED)
    elif status is execution.ExecutionStatus.FAILED_RETRYABLE:
        output_artifact = None
        failure = _failure(execution, execution.FailureCode.PROVIDER_TRANSIENT)
    elif status is execution.ExecutionStatus.FAILED_TERMINAL:
        output_artifact = None
        failure = _failure(execution, execution.FailureCode.PACKAGE_INVALID)
    elif status is execution.ExecutionStatus.CACHED:
        attempt_id = None
        cached_from_result = cached
    offset = timezone(timedelta(hours=offset_hours))
    return execution.StageResult(
        schema_version="tsdaf.stage-result.v1",
        execution_id="execution-001",
        attempt_id=attempt_id,
        stage_name="article-routing",
        stage_version="1.0.0",
        input_content_key="c" * 64,
        status=status,
        output_artifact=output_artifact,
        failure=failure,
        cached_from_result=cached_from_result,
        started_at=datetime(2026, 8, 26, 12, 0, 0, 1, tzinfo=offset),
        finished_at=datetime(2026, 8, 26, 12, 0, 1, 2, tzinfo=offset),
    )


@pytest.mark.parametrize(
    "status_name",
    ["SUCCEEDED", "REVIEW_REQUIRED", "FAILED_RETRYABLE", "FAILED_TERMINAL", "CACHED"],
)
def test_each_terminal_stage_result_row_is_accepted(status_name: str) -> None:
    value = _stage(status_name)

    assert value.status.value == status_name
    assert value.model_dump(mode="json")["schema_version"] == "tsdaf.stage-result.v1"


def test_review_result_accepts_only_optional_triage_failure() -> None:
    _, execution = _modules()
    assert _stage("REVIEW_REQUIRED", review_failure=True).failure.code is (
        execution.FailureCode.VISUAL_VERIFICATION_UNRESOLVED
    )


def test_aware_offset_datetimes_normalize_to_fixed_utc_json() -> None:
    value = _stage("SUCCEEDED", offset_hours=8)
    dumped = value.model_dump(mode="json")

    assert dumped["started_at"] == "2026-08-26T04:00:00.000001Z"
    assert dumped["finished_at"] == "2026-08-26T04:00:01.000002Z"


def test_failure_mapping_is_total_and_exact_for_all_sixteen_codes() -> None:
    _, execution = _modules()
    rows = {
        "PACKAGE_INVALID": ("INPUT", False),
        "ASSET_MISSING": ("INPUT", False),
        "HASH_MISMATCH": ("INPUT", False),
        "CONTEXT_TOO_LARGE": ("INPUT", False),
        "PROVIDER_RATE_LIMIT": ("PROVIDER", True),
        "PROVIDER_TRANSIENT": ("PROVIDER", True),
        "PROVIDER_REJECTED": ("PROVIDER", False),
        "ROUTER_NO_SUBMIT": ("ROUTER", False),
        "ROUTER_SCHEMA_INVALID": ("ROUTER", False),
        "ROUTER_REFERENCE_INVALID": ("ROUTER", False),
        "TOOL_BUDGET_EXHAUSTED": ("ROUTER", False),
        "VISUAL_VERIFICATION_REQUIRED": ("TRIAGE", False),
        "VISUAL_VERIFICATION_UNRESOLVED": ("TRIAGE", False),
        "HANDOFF_INVALID": ("MATERIALIZATION", False),
        "ARTIFACT_WRITE_FAILED": ("MATERIALIZATION", False),
        "LIVE_REQUIRED": ("CONTROL", False),
    }

    assert tuple(code.name for code in execution.FailureCode) == tuple(rows)
    for name, (category, retryable) in rows.items():
        code = execution.FailureCode[name]
        assert execution.failure_category(code).value == category
        assert execution.failure_is_retryable(code) is retryable


@pytest.mark.parametrize("status_name", ["PENDING", "RUNNING"])
def test_stage_result_rejects_nonterminal_status(status_name: str) -> None:
    _, execution = _modules()
    payload = _stage("SUCCEEDED").model_dump(mode="python")
    payload["status"] = execution.ExecutionStatus[status_name]

    with pytest.raises(ValidationError):
        execution.StageResult.model_validate(payload)


def test_stage_result_rejects_end_before_start_and_naive_time() -> None:
    _, execution = _modules()
    payload = _stage("SUCCEEDED").model_dump(mode="python")
    payload["finished_at"] = payload["started_at"] - timedelta(seconds=1)
    with pytest.raises(ValidationError):
        execution.StageResult.model_validate(payload)

    payload = _stage("SUCCEEDED").model_dump(mode="python")
    payload["started_at"] = datetime(2026, 8, 26, 12, 0, 0)
    with pytest.raises(ValidationError):
        execution.StageResult.model_validate(payload)


@pytest.mark.parametrize("keys", [("b", "a"), ("a", "a")])
def test_failure_details_require_unique_increasing_keys(keys: tuple[str, str]) -> None:
    _, execution = _modules()
    details = tuple(execution.FailureDetail(key=key, value="value") for key in keys)

    with pytest.raises(ValidationError):
        execution.TypedFailure(
            schema_version="tsdaf.typed-failure.v1",
            code=execution.FailureCode.PACKAGE_INVALID,
            message="Invalid package.",
            request_id=None,
            details=details,
        )


@pytest.mark.parametrize(
    ("valid_status", "field", "replacement"),
    [
        ("SUCCEEDED", "attempt_id", None),
        ("SUCCEEDED", "output_artifact", None),
        ("SUCCEEDED", "failure", "terminal"),
        ("SUCCEEDED", "cached_from_result", "artifact"),
        ("FAILED_RETRYABLE", "output_artifact", "artifact"),
        ("FAILED_RETRYABLE", "failure", None),
        ("FAILED_RETRYABLE", "cached_from_result", "artifact"),
        ("FAILED_TERMINAL", "output_artifact", "artifact"),
        ("FAILED_TERMINAL", "failure", None),
        ("CACHED", "attempt_id", "attempt-001"),
        ("CACHED", "failure", "terminal"),
        ("CACHED", "cached_from_result", None),
    ],
)
def test_stage_result_rejects_status_matrix_contradictions(
    valid_status: str, field: str, replacement: object
) -> None:
    source, execution = _modules()
    payload = _stage(valid_status).model_dump(mode="python")
    if replacement == "artifact":
        replacement = _artifact(source, "d")
    elif replacement == "terminal":
        replacement = _failure(execution, execution.FailureCode.PACKAGE_INVALID)
    payload[field] = replacement

    with pytest.raises(ValidationError):
        execution.StageResult.model_validate(payload)


@pytest.mark.parametrize(
    ("status_name", "code_name"),
    [
        ("FAILED_RETRYABLE", "PACKAGE_INVALID"),
        ("FAILED_TERMINAL", "PROVIDER_RATE_LIMIT"),
        ("REVIEW_REQUIRED", "PROVIDER_REJECTED"),
    ],
)
def test_stage_result_rejects_failure_category_or_retryability_mismatch(
    status_name: str, code_name: str
) -> None:
    _, execution = _modules()
    payload = _stage(status_name).model_dump(mode="python")
    payload["failure"] = _failure(execution, execution.FailureCode[code_name])

    with pytest.raises(ValidationError):
        execution.StageResult.model_validate(payload)


def test_live_required_is_valid_standalone_but_never_a_stage_result() -> None:
    _, execution = _modules()
    failure = _failure(execution, execution.FailureCode.LIVE_REQUIRED)
    assert failure.code is execution.FailureCode.LIVE_REQUIRED

    payload = _stage("FAILED_TERMINAL").model_dump(mode="python")
    payload["failure"] = failure
    with pytest.raises(ValidationError):
        execution.StageResult.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "status_name"),
    [
        ("attempt_id", "CACHED"),
        ("output_artifact", "FAILED_TERMINAL"),
        ("failure", "SUCCEEDED"),
        ("cached_from_result", "SUCCEEDED"),
    ],
)
def test_stage_nullable_fields_are_required_and_accept_explicit_null(
    field: str, status_name: str
) -> None:
    _, execution = _modules()
    valid = _stage(status_name)
    assert getattr(valid, field) is None
    python_payload = valid.model_dump(mode="python")
    json_payload = valid.model_dump(mode="json")
    python_omitted = dict(python_payload)
    json_omitted = dict(json_payload)
    python_omitted.pop(field)
    json_omitted.pop(field)

    with pytest.raises(ValidationError):
        execution.StageResult.model_validate(python_omitted)
    with pytest.raises(ValidationError):
        execution.StageResult.model_validate_json(json.dumps(json_omitted))
    assert getattr(execution.StageResult.model_validate(python_payload), field) is None
    assert (
        getattr(execution.StageResult.model_validate_json(json.dumps(json_payload)), field) is None
    )
