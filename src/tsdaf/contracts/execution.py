"""Provider-neutral failures and sealed terminal stage-result contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import field_validator, model_validator

from ._base import (
    ContractModel,
    UtcDateTime,
    _sha256,
    _stable_string,
    _strictly_increasing,
)
from .source import ArtifactRef


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    CACHED = "CACHED"


class FailureCode(StrEnum):
    PACKAGE_INVALID = "PACKAGE_INVALID"
    ASSET_MISSING = "ASSET_MISSING"
    HASH_MISMATCH = "HASH_MISMATCH"
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    ROUTER_NO_SUBMIT = "ROUTER_NO_SUBMIT"
    ROUTER_SCHEMA_INVALID = "ROUTER_SCHEMA_INVALID"
    ROUTER_REFERENCE_INVALID = "ROUTER_REFERENCE_INVALID"
    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"
    VISUAL_VERIFICATION_REQUIRED = "VISUAL_VERIFICATION_REQUIRED"
    VISUAL_VERIFICATION_UNRESOLVED = "VISUAL_VERIFICATION_UNRESOLVED"
    HANDOFF_INVALID = "HANDOFF_INVALID"
    ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"
    LIVE_REQUIRED = "LIVE_REQUIRED"


class FailureCategory(StrEnum):
    INPUT = "INPUT"
    PROVIDER = "PROVIDER"
    ROUTER = "ROUTER"
    TRIAGE = "TRIAGE"
    MATERIALIZATION = "MATERIALIZATION"
    CONTROL = "CONTROL"


_FAILURE_CATEGORIES = {
    FailureCode.PACKAGE_INVALID: FailureCategory.INPUT,
    FailureCode.ASSET_MISSING: FailureCategory.INPUT,
    FailureCode.HASH_MISMATCH: FailureCategory.INPUT,
    FailureCode.CONTEXT_TOO_LARGE: FailureCategory.INPUT,
    FailureCode.PROVIDER_RATE_LIMIT: FailureCategory.PROVIDER,
    FailureCode.PROVIDER_TRANSIENT: FailureCategory.PROVIDER,
    FailureCode.PROVIDER_REJECTED: FailureCategory.PROVIDER,
    FailureCode.ROUTER_NO_SUBMIT: FailureCategory.ROUTER,
    FailureCode.ROUTER_SCHEMA_INVALID: FailureCategory.ROUTER,
    FailureCode.ROUTER_REFERENCE_INVALID: FailureCategory.ROUTER,
    FailureCode.TOOL_BUDGET_EXHAUSTED: FailureCategory.ROUTER,
    FailureCode.VISUAL_VERIFICATION_REQUIRED: FailureCategory.TRIAGE,
    FailureCode.VISUAL_VERIFICATION_UNRESOLVED: FailureCategory.TRIAGE,
    FailureCode.HANDOFF_INVALID: FailureCategory.MATERIALIZATION,
    FailureCode.ARTIFACT_WRITE_FAILED: FailureCategory.MATERIALIZATION,
    FailureCode.LIVE_REQUIRED: FailureCategory.CONTROL,
}

_RETRYABLE_FAILURES = frozenset({FailureCode.PROVIDER_RATE_LIMIT, FailureCode.PROVIDER_TRANSIENT})


def failure_category(code: FailureCode) -> FailureCategory:
    return _FAILURE_CATEGORIES[code]


def failure_is_retryable(code: FailureCode) -> bool:
    return code in _RETRYABLE_FAILURES


class FailureDetail(ContractModel):
    key: str
    value: str

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _stable_string(value, label="failure detail key")


class TypedFailure(ContractModel):
    schema_version: Literal["tsdaf.typed-failure.v1"]
    code: FailureCode
    message: str
    request_id: str | None = None
    details: tuple[FailureDetail, ...] = ()

    @field_validator("request_id")
    @classmethod
    def _validate_request_id(cls, value: str | None) -> str | None:
        if value is not None:
            _stable_string(value, label="provider request id")
        return value

    @model_validator(mode="after")
    def _validate_details(self) -> Self:
        keys = tuple(detail.key for detail in self.details)
        _strictly_increasing(keys, label="failure detail keys")
        return self


class StageResult(ContractModel):
    schema_version: Literal["tsdaf.stage-result.v1"]
    execution_id: str
    attempt_id: str | None
    stage_name: str
    stage_version: str
    input_content_key: str
    status: ExecutionStatus
    output_artifact: ArtifactRef | None
    failure: TypedFailure | None
    cached_from_result: ArtifactRef | None
    started_at: UtcDateTime
    finished_at: UtcDateTime

    @field_validator("execution_id", "stage_name", "stage_version")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="stage result identity")

    @field_validator("attempt_id")
    @classmethod
    def _validate_attempt_id(cls, value: str | None) -> str | None:
        if value is not None:
            _stable_string(value, label="stage attempt id")
        return value

    @field_validator("input_content_key")
    @classmethod
    def _validate_input_content_key(cls, value: str) -> str:
        return _sha256(value, label="stage input content key")

    @model_validator(mode="after")
    def _validate_terminal_matrix(self) -> Self:
        if self.status in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
            raise ValueError("stage result must be terminal")
        if self.finished_at < self.started_at:
            raise ValueError("stage result cannot finish before it starts")
        if self.failure is not None and self.failure.code is FailureCode.LIVE_REQUIRED:
            raise ValueError("LIVE_REQUIRED is not a persistent stage result")
        if self.status is ExecutionStatus.SUCCEEDED:
            valid = (
                self.attempt_id is not None
                and self.output_artifact is not None
                and self.failure is None
                and self.cached_from_result is None
            )
        elif self.status is ExecutionStatus.REVIEW_REQUIRED:
            valid = (
                self.attempt_id is not None
                and self.output_artifact is not None
                and self.cached_from_result is None
                and (
                    self.failure is None
                    or failure_category(self.failure.code) is FailureCategory.TRIAGE
                )
            )
        elif self.status is ExecutionStatus.FAILED_RETRYABLE:
            valid = (
                self.attempt_id is not None
                and self.output_artifact is None
                and self.failure is not None
                and failure_is_retryable(self.failure.code)
                and self.cached_from_result is None
            )
        elif self.status is ExecutionStatus.FAILED_TERMINAL:
            valid = (
                self.attempt_id is not None
                and self.output_artifact is None
                and self.failure is not None
                and not failure_is_retryable(self.failure.code)
                and self.cached_from_result is None
            )
        else:
            valid = (
                self.attempt_id is None
                and self.output_artifact is not None
                and self.failure is None
                and self.cached_from_result is not None
            )
        if not valid:
            raise ValueError("stage result fields contradict the terminal status matrix")
        return self
