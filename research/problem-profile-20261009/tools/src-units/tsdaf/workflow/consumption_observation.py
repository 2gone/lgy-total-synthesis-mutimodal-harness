"""Pure derived, deterministic observation view of model interaction consumption.

This module provides consumption observability across host interactions and
provider attempts without imposing token/call/cost budget cutoffs or maintaining
authoritative ledgers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef


def _clean_token_count(value: object) -> int | None:
    """Return positive integer or zero; reject bools, floats, strings and negatives."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _clean_elapsed_ms(value: object) -> float | None:
    """Return non-negative float/int latency; reject bools, strings and negatives."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def _ref_to_dict(ref: ArtifactRef | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if ref is None:
        return None
    if isinstance(ref, ArtifactRef):
        return ref.model_dump(mode="json")
    return dict(ref)


def _ref_sha256(ref: ArtifactRef | Mapping[str, Any] | None) -> str | None:
    if ref is None:
        return None
    if isinstance(ref, ArtifactRef):
        return ref.sha256
    val = ref.get("sha256")
    return str(val) if isinstance(val, str) else None


def _resolve_bytes(
    ref: ArtifactRef | Mapping[str, Any] | None,
    read_bytes: Callable[[ArtifactRef], bytes] | None,
) -> bytes | None:
    if ref is None or read_bytes is None:
        return None
    try:
        artifact_ref = ref if isinstance(ref, ArtifactRef) else ArtifactRef.model_validate(ref)
        return read_bytes(artifact_ref)
    except Exception:
        return None


def _extract_token_with_fallback(
    source: Mapping[str, Any],
    keys: Iterable[str],
    extra_sources: Iterable[Mapping[str, Any]] = (),
) -> int | None:
    """Extract first non-None token field from source or extra sources, cleaned.

    If a primary candidate field exists with a value of None, fallback continues
    to subsequent candidate aliases.
    Explicit 0 is preserved as known 0.
    Invalid types (bool, negative, float, string) are rejected and return None.
    """
    for k in keys:
        if k in source:
            raw = source[k]
            if raw is not None:
                return _clean_token_count(raw)
    for extra in extra_sources:
        for k in keys:
            if k in extra:
                raw = extra[k]
                if raw is not None:
                    return _clean_token_count(raw)
    return None


@dataclass(frozen=True, slots=True)
class TokenUsageObservation:
    """Explicit token usage reported directly by the provider.

    Missing or unreported fields remain None (null). Partial counts are never
    summed to synthesize artificial totals, nor are character/byte counts
    substituted for token counts.
    """

    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_tokens: int | None = None
    total_cached_tokens: int | None = None
    total_thought_tokens: int | None = None
    total_tool_use_tokens: int | None = None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_cached_tokens": self.total_cached_tokens,
            "total_thought_tokens": self.total_thought_tokens,
            "total_tool_use_tokens": self.total_tool_use_tokens,
        }


@dataclass(frozen=True, slots=True)
class ObservedAttempt:
    """A distinct transport attempt within a model interaction."""

    attempt_id: str
    attempt_index: int
    outcome: str | None = None  # e.g. "SUCCEEDED", "FAILED"
    phase: str | None = None
    failure_code: str | None = None
    reason: str | None = None
    elapsed_ms: float | None = None
    retry_scheduled: bool | None = None
    finish_reason: str | None = None
    reported_usage: TokenUsageObservation | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "attempt_index": self.attempt_index,
            "outcome": self.outcome,
            "phase": self.phase,
            "failure_code": self.failure_code,
            "reason": self.reason,
            "elapsed_ms": self.elapsed_ms,
            "retry_scheduled": self.retry_scheduled,
            "finish_reason": self.finish_reason,
            "reported_usage": (
                self.reported_usage.to_dict() if self.reported_usage is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ObservedRequest:
    """A distinct model interaction request with its associated attempts and usage."""

    request_key: str
    request_id: str | None = None
    request_sha256: str | None = None
    binding: dict[str, Any] | None = None
    iteration: int | None = None
    interaction_ref: dict[str, Any] | None = None
    request_ref: dict[str, Any] | None = None
    request_artifact: dict[str, Any] | None = None
    response_ref: dict[str, Any] | None = None
    response_artifact: dict[str, Any] | None = None
    provider_attempt_ref: dict[str, Any] | None = None
    provider_attempt_audit_ref: dict[str, Any] | None = None
    elapsed_ms: float | None = None
    transport_attempts: int = 1
    has_attempt_audit: bool = False
    reported_usage: TokenUsageObservation = field(default_factory=TokenUsageObservation)
    known_usage: TokenUsageObservation = field(default_factory=TokenUsageObservation)
    attempts: tuple[ObservedAttempt, ...] = ()
    audit_gap_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_key": self.request_key,
            "request_id": self.request_id,
            "request_sha256": self.request_sha256,
            "binding": self.binding,
            "iteration": self.iteration,
            "interaction_ref": self.interaction_ref,
            "request_ref": self.request_ref,
            "request_artifact": self.request_artifact,
            "response_ref": self.response_ref,
            "response_artifact": self.response_artifact,
            "provider_attempt_ref": self.provider_attempt_ref,
            "provider_attempt_audit_ref": self.provider_attempt_audit_ref,
            "elapsed_ms": self.elapsed_ms,
            "transport_attempts": self.transport_attempts,
            "has_attempt_audit": self.has_attempt_audit,
            "reported_usage": self.reported_usage.to_dict(),
            "known_usage": self.known_usage.to_dict(),
            "attempts": [att.to_dict() for att in self.attempts],
            "audit_gap_note": self.audit_gap_note,
        }


@dataclass(frozen=True, slots=True)
class AggregatedTokenUsage:
    """Aggregated usage across all unique requests.

    If any request has an unknown/null token field, the aggregated total for
    that field remains None (not an incomplete sum masquerading as complete).
    Cost is strictly None: guessing prices is forbidden.
    """

    total_input_tokens: int | None = None
    is_input_tokens_complete: bool = True
    known_input_tokens_sum: int = 0

    total_output_tokens: int | None = None
    is_output_tokens_complete: bool = True
    known_output_tokens_sum: int = 0

    total_tokens: int | None = None
    is_total_tokens_complete: bool = True
    known_total_tokens_sum: int = 0

    total_cached_tokens: int | None = None
    is_cached_tokens_complete: bool = True
    known_cached_tokens_sum: int = 0

    total_thought_tokens: int | None = None
    is_thought_tokens_complete: bool = True
    known_thought_tokens_sum: int = 0

    total_tool_use_tokens: int | None = None
    is_tool_use_tokens_complete: bool = True
    known_tool_use_tokens_sum: int = 0

    total_elapsed_ms: float | None = None
    is_elapsed_ms_complete: bool = True

    total_cost_usd: None = None
    cost_currency: str = "USD"
    cost_note: str = (
        "Cost is null: pricing snapshot is unconfigured and guessing prices is prohibited."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_input_tokens": self.total_input_tokens,
            "is_input_tokens_complete": self.is_input_tokens_complete,
            "known_input_tokens_sum": self.known_input_tokens_sum,
            "total_output_tokens": self.total_output_tokens,
            "is_output_tokens_complete": self.is_output_tokens_complete,
            "known_output_tokens_sum": self.known_output_tokens_sum,
            "total_tokens": self.total_tokens,
            "is_total_tokens_complete": self.is_total_tokens_complete,
            "known_total_tokens_sum": self.known_total_tokens_sum,
            "total_cached_tokens": self.total_cached_tokens,
            "is_cached_tokens_complete": self.is_cached_tokens_complete,
            "known_cached_tokens_sum": self.known_cached_tokens_sum,
            "total_thought_tokens": self.total_thought_tokens,
            "is_thought_tokens_complete": self.is_thought_tokens_complete,
            "known_thought_tokens_sum": self.known_thought_tokens_sum,
            "total_tool_use_tokens": self.total_tool_use_tokens,
            "is_tool_use_tokens_complete": self.is_tool_use_tokens_complete,
            "known_tool_use_tokens_sum": self.known_tool_use_tokens_sum,
            "total_elapsed_ms": self.total_elapsed_ms,
            "is_elapsed_ms_complete": self.is_elapsed_ms_complete,
            "total_cost_usd": self.total_cost_usd,
            "cost_currency": self.cost_currency,
            "cost_note": self.cost_note,
        }


@dataclass(frozen=True, slots=True)
class ConsumptionObservationView:
    """Pure derived, reconstructible consumption view of observed interactions."""

    total_requests: int
    total_attempts: int
    replayed_requests_ignored: int
    attempt_coverage: (
        str  # "EXACT_ATTEMPTS", "ACCEPTED_ONLY", "PARTIAL_ATTEMPTS", "NO_INTERACTIONS"
    )
    coverage_notes: tuple[str, ...]
    aggregated_usage: AggregatedTokenUsage
    requests: tuple[ObservedRequest, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_requests": self.total_requests,
            "total_attempts": self.total_attempts,
            "replayed_requests_ignored": self.replayed_requests_ignored,
            "attempt_coverage": self.attempt_coverage,
            "coverage_notes": list(self.coverage_notes),
            "aggregated_usage": self.aggregated_usage.to_dict(),
            "requests": [r.to_dict() for r in self.requests],
        }


def _extract_usage_from_mapping(mapping: Mapping[str, Any]) -> TokenUsageObservation:
    """Extract explicit token usage fields from a mapping without guessing or summing."""
    usage_dict = mapping.get("usage")
    if isinstance(usage_dict, Mapping):
        source: Mapping[str, Any] = usage_dict
    elif isinstance(mapping.get("reported_usage"), Mapping):
        source = mapping["reported_usage"]
    else:
        source = mapping

    prompt_details = source.get("prompt_tokens_details")
    prompt_details_map = prompt_details if isinstance(prompt_details, Mapping) else {}

    completion_details = source.get("completion_tokens_details")
    completion_details_map = completion_details if isinstance(completion_details, Mapping) else {}

    input_tokens = _extract_token_with_fallback(
        source, ("total_input_tokens", "prompt_tokens", "input_tokens")
    )
    output_tokens = _extract_token_with_fallback(
        source, ("total_output_tokens", "completion_tokens", "output_tokens")
    )
    total_tokens = _extract_token_with_fallback(source, ("total_tokens",))

    cached_tokens = _extract_token_with_fallback(
        source,
        ("total_cached_tokens", "cached_tokens"),
        extra_sources=(prompt_details_map,),
    )
    thought_tokens = _extract_token_with_fallback(
        source,
        ("total_thought_tokens", "reasoning_tokens", "thought_tokens"),
        extra_sources=(completion_details_map,),
    )
    tool_use_tokens = _extract_token_with_fallback(
        source, ("total_tool_use_tokens", "tool_use_tokens")
    )

    return TokenUsageObservation(
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_tokens=total_tokens,
        total_cached_tokens=cached_tokens,
        total_thought_tokens=thought_tokens,
        total_tool_use_tokens=tool_use_tokens,
    )


def _extract_usage_from_object(obj: object) -> TokenUsageObservation:
    """Extract token fields from an object (e.g. LaneCompletedInteraction)."""
    if isinstance(obj, Mapping):
        return _extract_usage_from_mapping(obj)

    def _get_cleaned(attrs: Iterable[str]) -> int | None:
        for attr in attrs:
            if hasattr(obj, attr):
                val = getattr(obj, attr)
                if val is not None:
                    return _clean_token_count(val)
        return None

    input_tokens = _get_cleaned(("total_input_tokens", "prompt_tokens", "input_tokens"))
    output_tokens = _get_cleaned(("total_output_tokens", "completion_tokens", "output_tokens"))
    total_tokens = _get_cleaned(("total_tokens",))
    cached_tokens = _get_cleaned(("total_cached_tokens", "cached_tokens"))
    thought_tokens = _get_cleaned(("total_thought_tokens", "reasoning_tokens", "thought_tokens"))
    tool_use_tokens = _get_cleaned(("total_tool_use_tokens", "tool_use_tokens"))

    return TokenUsageObservation(
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_tokens=total_tokens,
        total_cached_tokens=cached_tokens,
        total_thought_tokens=thought_tokens,
        total_tool_use_tokens=tool_use_tokens,
    )


class ConsumptionObserver:
    """Stateful aggregator that observes interactions and derives consumption views.

    Tracks request and attempt identities. Repeated loads of the same request
    (such as accepted replays or checkpoint restorations) are idempotent and
    produce zero additional consumption.
    """

    def __init__(
        self,
        *,
        read_bytes: Callable[[ArtifactRef], bytes] | None = None,
    ) -> None:
        self._read_bytes = read_bytes
        self._observed_requests: list[ObservedRequest] = []
        self._seen_request_keys: set[str] = set()
        self._replayed_requests_ignored: int = 0
        self._indeterminate_counter: int = 0

    @property
    def replayed_requests_ignored(self) -> int:
        return self._replayed_requests_ignored

    def observe(self, record: Any) -> bool:
        """Observe one interaction record, attempt audit, or completed interaction.

        Returns True if newly added, or False if ignored as an accepted replay / duplicate.
        """
        if record is None:
            return False

        if isinstance(record, (bytes, bytearray)):
            return self._observe_audit_bytes(bytes(record))

        request_key, request_sha256 = self._derive_request_key(record)
        if request_key in self._seen_request_keys:
            self._replayed_requests_ignored += 1
            return False

        self._seen_request_keys.add(request_key)
        observed_request = self._parse_record(record, request_key, request_sha256)
        self._observed_requests.append(observed_request)
        return True

    def _derive_request_key(self, record: Any) -> tuple[str, str | None]:
        """Derive deterministic unique request key and sha256 hash.

        Identity is derived from host accepted interaction (interaction_ref,
        interaction_id) or host binding + iteration. Request payload hash alone
        must never treat distinct calls with identical prompt payload as replays.
        When host identity is missing, the request is explicitly marked indeterminate
        and not deduplicated. Original request payload ref/hash is preserved for traceability.
        """
        # 1. Extract request payload sha256 for traceability
        req_ref = getattr(record, "request_ref", None)
        if req_ref is None:
            req_ref = getattr(record, "request_artifact", None)
        if req_ref is None and isinstance(record, Mapping):
            req_ref = record.get("request_ref")
            if req_ref is None:
                req_ref = record.get("request_artifact")
        sha = _ref_sha256(req_ref)

        if sha is None:
            req_bytes = getattr(record, "request_bytes", None)
            if req_bytes is None and isinstance(record, Mapping):
                req_bytes = record.get("request_bytes")
            if isinstance(req_bytes, (bytes, bytearray)):
                sha = hashlib.sha256(req_bytes).hexdigest()

        if sha is None and isinstance(record, Mapping):
            val = record.get("request_sha256")
            if isinstance(val, str) and val.strip():
                sha = val.strip()

        # 2. Host accepted interaction_ref identity
        inter_ref = getattr(record, "interaction_ref", None)
        if inter_ref is None and isinstance(record, Mapping):
            inter_ref = record.get("interaction_ref")
        inter_sha = _ref_sha256(inter_ref)
        if inter_sha:
            return f"interaction:{inter_sha}", sha

        # 3. Host accepted interaction_id identity
        inter_id = getattr(record, "interaction_id", None)
        if inter_id is None and isinstance(record, Mapping):
            inter_id = record.get("interaction_id")
        if isinstance(inter_id, str) and inter_id.strip():
            return f"interaction_id:{inter_id.strip()}", sha

        # 4. Host binding + iteration identity
        binding = getattr(record, "binding", None)
        iteration = getattr(record, "iteration", None)
        if isinstance(record, Mapping):
            binding = record.get("binding", binding)
            iteration = record.get("iteration", iteration)
        if binding is not None and iteration is not None:
            try:
                ident = canonical_json_bytes({"binding": binding, "iteration": iteration})
                binding_sha = hashlib.sha256(ident).hexdigest()
                return f"binding:{binding_sha}", sha
            except Exception:
                pass

        # 5. Host identity is missing: explicitly indeterminate
        self._indeterminate_counter += 1
        return f"indeterminate:{self._indeterminate_counter}", sha

    def _observe_audit_bytes(self, audit_bytes: bytes) -> bool:
        try:
            audit_dict = json.loads(audit_bytes.decode("utf-8"))
            if isinstance(audit_dict, Mapping):
                return self.observe(audit_dict)
        except Exception:
            pass
        return False

    def _parse_record(
        self,
        record: Any,
        request_key: str,
        request_sha256: str | None,
    ) -> ObservedRequest:
        binding = getattr(record, "binding", None)
        iteration = getattr(record, "iteration", None)
        request_id = getattr(record, "request_id", None)
        transport_attempts = getattr(record, "transport_attempts", 1)
        elapsed_ms = _clean_elapsed_ms(getattr(record, "elapsed_ms", None))

        interaction_ref = _ref_to_dict(getattr(record, "interaction_ref", None))
        request_ref = _ref_to_dict(getattr(record, "request_ref", None))
        request_artifact = _ref_to_dict(getattr(record, "request_artifact", None))
        response_ref = _ref_to_dict(getattr(record, "response_ref", None))
        response_artifact = _ref_to_dict(getattr(record, "response_artifact", None))
        provider_attempt_ref = _ref_to_dict(getattr(record, "provider_attempt_ref", None))
        provider_attempt_audit_ref = _ref_to_dict(
            getattr(record, "provider_attempt_audit_ref", None)
        )

        if isinstance(record, Mapping):
            binding = record.get("binding", binding)
            iteration = record.get("iteration", iteration)
            request_id = record.get("request_id", request_id)
            transport_attempts = record.get("transport_attempts", transport_attempts)
            if elapsed_ms is None:
                elapsed_ms = _clean_elapsed_ms(record.get("elapsed_ms"))
            if interaction_ref is None:
                interaction_ref = _ref_to_dict(record.get("interaction_ref"))
            if request_ref is None:
                request_ref = _ref_to_dict(record.get("request_ref"))
            if request_artifact is None:
                request_artifact = _ref_to_dict(record.get("request_artifact"))
            if response_ref is None:
                response_ref = _ref_to_dict(record.get("response_ref"))
            if response_artifact is None:
                response_artifact = _ref_to_dict(record.get("response_artifact"))
            if provider_attempt_ref is None:
                provider_attempt_ref = _ref_to_dict(record.get("provider_attempt_ref"))
            if provider_attempt_audit_ref is None:
                provider_attempt_audit_ref = _ref_to_dict(record.get("provider_attempt_audit_ref"))

        if request_ref is None and request_artifact is not None:
            request_ref = request_artifact
        if request_artifact is None and request_ref is not None:
            request_artifact = request_ref

        if response_ref is None and response_artifact is not None:
            response_ref = response_artifact
        if response_artifact is None and response_ref is not None:
            response_artifact = response_ref

        if provider_attempt_ref is None and provider_attempt_audit_ref is not None:
            provider_attempt_ref = provider_attempt_audit_ref
        if provider_attempt_audit_ref is None and provider_attempt_ref is not None:
            provider_attempt_audit_ref = provider_attempt_ref

        if not isinstance(transport_attempts, int) or transport_attempts < 1:
            transport_attempts = 1

        audit_payload: dict[str, Any] | None = None
        attempt_audit_bytes = getattr(record, "attempt_audit_bytes", None)
        if isinstance(attempt_audit_bytes, (bytes, bytearray)):
            try:
                audit_payload = json.loads(bytes(attempt_audit_bytes).decode("utf-8"))
            except Exception:
                audit_payload = None

        audit_ref = provider_attempt_audit_ref or provider_attempt_ref
        if audit_payload is None and audit_ref is not None and self._read_bytes:
            raw_audit = _resolve_bytes(audit_ref, self._read_bytes)
            if raw_audit is not None:
                try:
                    audit_payload = json.loads(raw_audit.decode("utf-8"))
                except Exception:
                    audit_payload = None

        if (
            audit_payload is None
            and isinstance(record, Mapping)
            and isinstance(record.get("attempts"), list)
        ):
            audit_payload = dict(record)

        TOKEN_FIELDS = (
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "total_cached_tokens",
            "total_thought_tokens",
            "total_tool_use_tokens",
        )

        attempts: list[ObservedAttempt] = []
        has_attempt_audit = False
        audit_gap_notes: list[str] = []

        att_prefix = f"request:{request_sha256}" if request_sha256 else request_key

        if (
            audit_payload is not None
            and isinstance(audit_payload.get("attempts"), list)
            and audit_payload["attempts"]
        ):
            has_attempt_audit = True
            raw_attempts = audit_payload["attempts"]
            for raw_att in raw_attempts:
                if not isinstance(raw_att, Mapping):
                    continue
                att_idx = raw_att.get("attempt_index")
                idx = att_idx if isinstance(att_idx, int) and att_idx > 0 else len(attempts) + 1
                att_id = f"{att_prefix}:attempt:{idx}"
                att_usage = _extract_usage_from_mapping(raw_att)
                att = ObservedAttempt(
                    attempt_id=att_id,
                    attempt_index=idx,
                    outcome=str(raw_att.get("outcome")) if raw_att.get("outcome") else None,
                    phase=str(raw_att.get("phase")) if raw_att.get("phase") else None,
                    failure_code=(
                        str(raw_att.get("failure_code")) if raw_att.get("failure_code") else None
                    ),
                    reason=str(raw_att.get("reason")) if raw_att.get("reason") else None,
                    elapsed_ms=_clean_elapsed_ms(raw_att.get("elapsed_ms")),
                    retry_scheduled=(
                        bool(raw_att["retry_scheduled"]) if "retry_scheduled" in raw_att else None
                    ),
                    finish_reason=(
                        str(raw_att.get("finish_reason")) if raw_att.get("finish_reason") else None
                    ),
                    reported_usage=att_usage,
                )
                attempts.append(att)

            if elapsed_ms is None and attempts:
                att_elapseds = [a.elapsed_ms for a in attempts]
                if all(e is not None for e in att_elapseds):
                    elapsed_ms = sum(e for e in att_elapseds if e is not None)

            is_audit_truncated = transport_attempts > len(attempts)
            if is_audit_truncated:
                audit_gap_notes.append(
                    f"Interaction declared {transport_attempts} transport attempts, but provider "
                    f"audit contains only {len(attempts)} attempts; incomplete attempt audit."
                )

            unreported_attempts = [
                str(a.attempt_index)
                for a in attempts
                if a.reported_usage is None
                or all(getattr(a.reported_usage, f) is None for f in TOKEN_FIELDS)
            ]
            if unreported_attempts:
                unreported_str = ", ".join(unreported_attempts)
                audit_gap_notes.append(
                    f"Provider attempt audit contains attempts with unreported token usage "
                    f"(attempts: {unreported_str})."
                )

            reported_kwargs: dict[str, int | None] = {}
            known_kwargs: dict[str, int | None] = {}

            for f in TOKEN_FIELDS:
                vals = [
                    getattr(a.reported_usage, f)
                    for a in attempts
                    if a.reported_usage is not None and getattr(a.reported_usage, f) is not None
                ]
                if vals:
                    known_kwargs[f] = sum(vals)
                else:
                    known_kwargs[f] = None

                is_field_complete = (
                    (not is_audit_truncated)
                    and (len(attempts) > 0)
                    and (len(vals) == len(attempts))
                )
                if is_field_complete:
                    reported_kwargs[f] = sum(vals)
                else:
                    reported_kwargs[f] = None

            reported_usage = TokenUsageObservation(**reported_kwargs)
            known_usage = TokenUsageObservation(**known_kwargs)

            transport_attempts = max(transport_attempts, len(attempts))
        else:
            has_attempt_audit = False
            audit_gap_notes.append(
                "Only accepted interaction is recorded; failed transport attempts cannot "
                "be observed without provider attempt audit records."
            )

            resp_ref = response_ref or response_artifact
            raw_usage = TokenUsageObservation()
            if resp_ref is not None and self._read_bytes:
                raw_response = _resolve_bytes(resp_ref, self._read_bytes)
                if raw_response is not None:
                    try:
                        resp_payload = json.loads(raw_response.decode("utf-8"))
                        if isinstance(resp_payload, Mapping):
                            raw_usage = _extract_usage_from_mapping(resp_payload)
                    except Exception:
                        pass

            if raw_usage == TokenUsageObservation():
                raw_usage = _extract_usage_from_object(record)

            att_id = f"{att_prefix}:accepted"
            attempts.append(
                ObservedAttempt(
                    attempt_id=att_id,
                    attempt_index=transport_attempts,
                    outcome="SUCCEEDED",
                    phase="COMPLETED",
                    elapsed_ms=elapsed_ms,
                    reported_usage=raw_usage,
                )
            )

            if transport_attempts > 1:
                audit_gap_notes.append(
                    f"Interaction reports {transport_attempts} transport attempts, but provider "
                    "attempt audit records are missing; retry details are unobserved."
                )
                reported_kwargs = {f: None for f in TOKEN_FIELDS}
                known_kwargs = {f: getattr(raw_usage, f) for f in TOKEN_FIELDS}
                reported_usage = TokenUsageObservation(**reported_kwargs)
                known_usage = TokenUsageObservation(**known_kwargs)
            else:
                reported_usage = raw_usage
                known_usage = raw_usage

        if request_key.startswith("indeterminate:"):
            audit_gap_notes.append(
                "Host interaction identity is indeterminate; lacking interaction_ref, "
                "interaction_id, or binding+iteration."
            )

        audit_gap_note = "; ".join(audit_gap_notes) if audit_gap_notes else None

        return ObservedRequest(
            request_key=request_key,
            request_id=str(request_id) if request_id is not None else None,
            request_sha256=request_sha256,
            binding=binding if isinstance(binding, dict) else None,
            iteration=iteration if isinstance(iteration, int) else None,
            interaction_ref=interaction_ref,
            request_ref=request_ref,
            request_artifact=request_artifact,
            response_ref=response_ref,
            response_artifact=response_artifact,
            provider_attempt_ref=provider_attempt_ref,
            provider_attempt_audit_ref=provider_attempt_audit_ref,
            elapsed_ms=elapsed_ms,
            transport_attempts=transport_attempts,
            has_attempt_audit=has_attempt_audit,
            reported_usage=reported_usage,
            known_usage=known_usage,
            attempts=tuple(attempts),
            audit_gap_note=audit_gap_note,
        )

    def build_view(self) -> ConsumptionObservationView:
        """Construct deterministic, reconstructible consumption observation view."""
        requests = tuple(self._observed_requests)
        total_requests = len(requests)
        total_attempts = sum(max(len(r.attempts), r.transport_attempts) for r in requests)

        coverage_notes: list[str] = []
        if total_requests == 0:
            attempt_coverage = "NO_INTERACTIONS"
            coverage_notes.append("No interactions observed.")
        elif all(r.has_attempt_audit and len(r.attempts) >= r.transport_attempts for r in requests):
            attempt_coverage = "EXACT_ATTEMPTS"
            coverage_notes.append(
                "All attempts are verified by explicit provider attempt audit records."
            )
        elif not any(r.has_attempt_audit for r in requests):
            attempt_coverage = "ACCEPTED_ONLY"
            coverage_notes.append(
                "Only accepted interactions are recorded; failed transport attempts cannot "
                "be observed without provider attempt audit records."
            )
        else:
            attempt_coverage = "PARTIAL_ATTEMPTS"
            coverage_notes.append(
                "Some interactions lack provider attempt audit records; unaccepted or failed "
                "retry attempts for those interactions cannot be observed."
            )

        for r in requests:
            if r.audit_gap_note:
                note_str = f"Request {r.request_key}: {r.audit_gap_note}"
                if note_str not in coverage_notes:
                    coverage_notes.append(note_str)

        def _aggregate_metric(
            reported_extractor: Callable[[ObservedRequest], int | None],
            known_extractor: Callable[[ObservedRequest], int | None],
        ) -> tuple[int | None, bool, int]:
            if not requests:
                return 0, True, 0
            is_complete = all(reported_extractor(r) is not None for r in requests)
            known_values = [known_extractor(r) for r in requests]
            known_sum = sum(v for v in known_values if v is not None)
            if is_complete:
                rep_values = [reported_extractor(r) for r in requests]
                total = sum(v for v in rep_values if v is not None)
                return total, True, known_sum
            return None, False, known_sum

        in_tot, in_comp, in_sum = _aggregate_metric(
            lambda r: r.reported_usage.total_input_tokens,
            lambda r: r.known_usage.total_input_tokens,
        )
        out_tot, out_comp, out_sum = _aggregate_metric(
            lambda r: r.reported_usage.total_output_tokens,
            lambda r: r.known_usage.total_output_tokens,
        )
        tot_tot, tot_comp, tot_sum = _aggregate_metric(
            lambda r: r.reported_usage.total_tokens,
            lambda r: r.known_usage.total_tokens,
        )
        cache_tot, cache_comp, cache_sum = _aggregate_metric(
            lambda r: r.reported_usage.total_cached_tokens,
            lambda r: r.known_usage.total_cached_tokens,
        )
        th_tot, th_comp, th_sum = _aggregate_metric(
            lambda r: r.reported_usage.total_thought_tokens,
            lambda r: r.known_usage.total_thought_tokens,
        )
        tool_tot, tool_comp, tool_sum = _aggregate_metric(
            lambda r: r.reported_usage.total_tool_use_tokens,
            lambda r: r.known_usage.total_tool_use_tokens,
        )

        elapsed_values = [r.elapsed_ms for r in requests]
        if not requests:
            tot_elapsed, is_elapsed_comp = 0.0, True
        elif any(e is None for e in elapsed_values):
            tot_elapsed, is_elapsed_comp = None, False
        else:
            tot_elapsed = sum(e for e in elapsed_values if e is not None)
            is_elapsed_comp = True

        aggregated_usage = AggregatedTokenUsage(
            total_input_tokens=in_tot,
            is_input_tokens_complete=in_comp,
            known_input_tokens_sum=in_sum,
            total_output_tokens=out_tot,
            is_output_tokens_complete=out_comp,
            known_output_tokens_sum=out_sum,
            total_tokens=tot_tot,
            is_total_tokens_complete=tot_comp,
            known_total_tokens_sum=tot_sum,
            total_cached_tokens=cache_tot,
            is_cached_tokens_complete=cache_comp,
            known_cached_tokens_sum=cache_sum,
            total_thought_tokens=th_tot,
            is_thought_tokens_complete=th_comp,
            known_thought_tokens_sum=th_sum,
            total_tool_use_tokens=tool_tot,
            is_tool_use_tokens_complete=tool_comp,
            known_tool_use_tokens_sum=tool_sum,
            total_elapsed_ms=tot_elapsed,
            is_elapsed_ms_complete=is_elapsed_comp,
            total_cost_usd=None,
            cost_currency="USD",
        )

        return ConsumptionObservationView(
            total_requests=total_requests,
            total_attempts=total_attempts,
            replayed_requests_ignored=self._replayed_requests_ignored,
            attempt_coverage=attempt_coverage,
            coverage_notes=tuple(coverage_notes),
            aggregated_usage=aggregated_usage,
            requests=requests,
        )


def observe_consumption(
    records: Iterable[Any],
    *,
    read_bytes: Callable[[ArtifactRef], bytes] | None = None,
) -> ConsumptionObservationView:
    """Derive deterministic consumption observation view from interaction records.

    Loading is idempotent: replayed or duplicate records sharing the same request
    identity are deduplicated and add zero new consumption.
    """
    observer = ConsumptionObserver(read_bytes=read_bytes)
    for record in records:
        observer.observe(record)
    return observer.build_view()


def observe_interaction(
    interaction: Any,
    *,
    read_bytes: Callable[[ArtifactRef], bytes] | None = None,
) -> ConsumptionObservationView:
    """Derive consumption observation view from a single interaction or record."""
    return observe_consumption([interaction], read_bytes=read_bytes)
