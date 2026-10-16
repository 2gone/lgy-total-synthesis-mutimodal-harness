"""Minimal live and recorded adapters with OpenAI-compatible Chat Completions."""

from __future__ import annotations

import hashlib as _hashlib
import json as _json
import math as _math
import re as _re
import threading as _threading
import time as _time
from collections import deque as _deque
from collections.abc import Callable as _Callable
from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from dataclasses import dataclass as _dataclass
from dataclasses import field as _field
from pathlib import Path as _Path
from typing import Any as _Any
from typing import Self as _Self
from typing import cast as _cast

from tsdaf.contracts.execution import (
    INVALID_HTTP_400_REASON,
    UNKNOWN_HTTP_400_MAX_ATTEMPTS,
    UNKNOWN_HTTP_400_REASON,
)
from tsdaf.contracts.execution import PROVIDER_ATTEMPT_AUDIT_VERSION as _ATTEMPT_AUDIT_VERSION
from tsdaf.contracts.execution import FailureCode as _FailureCode
from tsdaf.contracts.execution import FailureDetail as _FailureDetail
from tsdaf.contracts.execution import ProviderInteractionError as _ProviderInteractionError
from tsdaf.contracts.execution import TypedFailure as _TypedFailure
from tsdaf.contracts.schema import canonical_json_bytes as _canonical_json_bytes

__all__ = (
    "GEMINI_MAX_OUTPUT_TOKENS",
    "GEMINI_MODEL_ID",
    "GEMINI_MODEL_PROFILE",
    "GEMINI_MEDIUM_EFFORT_PROFILE",
    "GEMINI_OCSR_MODEL_PROFILE",
    "GeminiAdapterError",
    "GeminiInteraction",
    "GeminiInteractions",
    "PreparedGeminiInteraction",
    "RecordedGeminiInteractions",
)

GEMINI_MODEL_ID = "gemini-3.8-flash-high"
GEMINI_MAX_OUTPUT_TOKENS = 131072
# Deployment information supplied by the owner on 2026-09-27, not a discovered
# provider capability. Input/output sharing is unknown, so reserve requested output.
GEMINI_CONTEXT_TOKENS = 1_000_000
_CONTEXT_ESTIMATOR_VERSION = "wire-utf8-proxy-v1"
GEMINI_MODEL_PROFILE = "gemini-3.8-flash-high-chat-completions-v1"
GEMINI_MEDIUM_EFFORT_PROFILE = "gemini-3.8-flash-high-medium-effort-chat-completions-v1"
GEMINI_OCSR_MODEL_PROFILE = "gemini-3.8-flash-high-molecule-ocsr-chat-completions-v1"

_FIXTURE_SCHEMA_VERSION = "tsdaf.recorded-gemini-interactions.v1"
_MAX_TRANSPORT_ATTEMPTS = 4
_MAIN_GENERATION_CONFIG: dict[str, object] = {"thinking_level": "high"}
_MEDIUM_GENERATION_CONFIG: dict[str, object] = {"thinking_level": "medium"}
_OCSR_GENERATION_CONFIG: dict[str, object] = {"temperature": 0}
_REQUEST_ID_RE = _re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


@_dataclass(frozen=True, slots=True)
class GeminiInteraction:
    """Immutable provider-neutral evidence from one interaction."""

    request_bytes: bytes = _field(repr=False)
    response_bytes: bytes = _field(repr=False)
    interaction_id: str | None
    output_text: str | None
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_tokens: int | None
    total_cached_tokens: int | None
    total_thought_tokens: int | None
    total_tool_use_tokens: int | None
    request_id: str | None
    elapsed_ms: float
    transport_attempts: int
    attempt_audit_bytes: bytes | None = _field(default=None, repr=False)


@_dataclass(frozen=True, slots=True)
class PreparedGeminiInteraction:
    """Immutable exact request bytes bound to the adapter that prepared them."""

    request_bytes: bytes = _field(repr=False)
    model_profile: str
    _adapter_token: object = _field(repr=False, compare=False)
    context_capacity: _Mapping[str, object] = _field(repr=False, compare=False)


class GeminiAdapterError(_ProviderInteractionError):
    """Backward-compatible adapter name for the provider-neutral failure boundary."""

    __slots__ = ()


def _failure(
    code: _FailureCode,
    message: str,
    *,
    request_id: str | None = None,
    details: tuple[_FailureDetail, ...] = (),
    attempt_audit_bytes: bytes | None = None,
) -> GeminiAdapterError:
    return GeminiAdapterError(
        _TypedFailure(
            schema_version="tsdaf.typed-failure.v1",
            code=code,
            message=message,
            request_id=request_id,
            details=details,
        ),
        attempt_audit_bytes=attempt_audit_bytes,
    )


def _require_fixed_model(model: str) -> None:
    if model != GEMINI_MODEL_ID:
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini adapter requires the fixed model profile",
        ) from None


def _request_profile(
    effort: object,
    default_profile: str,
    default_config: _Mapping[str, object],
) -> tuple[str, _Mapping[str, object]]:
    """Select immutable per-request settings without changing a shared adapter."""

    if effort is None:
        return default_profile, default_config
    if (
        default_profile != GEMINI_MODEL_PROFILE
        or not isinstance(effort, str)
        or effort not in {"medium", "high"}
    ):
        raise ValueError("reasoning effort requires the main high/medium request profile")
    if effort == "medium":
        return GEMINI_MEDIUM_EFFORT_PROFILE, _MEDIUM_GENERATION_CONFIG
    return GEMINI_MODEL_PROFILE, _MAIN_GENERATION_CONFIG


def _request_and_bytes(
    *,
    input: object,
    system_instruction: object | None,
    tools: object | None,
    response_format: object | None,
    generation_config: _Mapping[str, object] | None = None,
) -> tuple[dict[str, object], bytes]:
    request: dict[str, object] = {
        "input": input,
        "model": GEMINI_MODEL_ID,
        "store": False,
        "stream": False,
        "background": False,
        "generation_config": dict(generation_config or _MAIN_GENERATION_CONFIG),
    }
    if system_instruction is not None:
        request["system_instruction"] = system_instruction
    if tools is not None:
        request["tools"] = tools
    if response_format is not None:
        request["response_format"] = response_format
    try:
        request_bytes = _canonical_json_bytes(request)
        json_request = _json.loads(request_bytes)
    except (TypeError, ValueError, UnicodeError):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini interaction request is not JSON-compatible",
        ) from None
    if not isinstance(json_request, dict):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini interaction request is not JSON-compatible",
        ) from None
    return _cast(dict[str, object], json_request), request_bytes


def _prepared_request(
    prepared: PreparedGeminiInteraction,
    *,
    adapter_token: object,
    model_profile: str,
    generation_config: _Mapping[str, object],
) -> dict[str, object]:
    if type(prepared) is not PreparedGeminiInteraction:
        raise TypeError("prepared interaction must be a PreparedGeminiInteraction")
    if prepared._adapter_token is not adapter_token:
        raise ValueError("prepared interaction belongs to a different adapter")
    if prepared.model_profile == GEMINI_MEDIUM_EFFORT_PROFILE:
        model_profile, generation_config = _request_profile(
            "medium", model_profile, generation_config
        )
    if prepared.model_profile != model_profile:
        raise ValueError("prepared interaction model profile does not match its adapter")
    try:
        request: object = _json.loads(prepared.request_bytes)
        if _canonical_json_bytes(request) != prepared.request_bytes:
            raise ValueError
    except (TypeError, ValueError, UnicodeError, _json.JSONDecodeError):
        raise ValueError("prepared interaction request bytes are not canonical JSON") from None
    if (
        not isinstance(request, dict)
        or request.get("model") != GEMINI_MODEL_ID
        or request.get("generation_config") != dict(generation_config)
    ):
        raise ValueError("prepared interaction request does not match its adapter profile")
    return _cast(dict[str, object], request)


def _safe_request_id(value: object) -> str | None:
    if isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None:
        return value
    return None


def _header_value(headers: object, names: tuple[str, ...]) -> object | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    for name in names:
        value = _cast(object | None, getter(name))
        if value is not None:
            return value
    return None


def _response_headers(value: object) -> object | None:
    response = getattr(value, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            return _cast(object, headers)
    return _cast(object | None, getattr(value, "headers", None))


def _request_id_from(value: object, payload: _Mapping[str, object] | None = None) -> str | None:
    direct = _safe_request_id(getattr(value, "request_id", None))
    if direct is not None:
        return direct
    if payload is not None:
        mapped = _safe_request_id(payload.get("request_id"))
        if mapped is not None:
            return mapped
    headers = _response_headers(value)
    return _safe_request_id(
        _header_value(
            headers,
            (
                "x-request-id",
                "X-Request-Id",
                "x-goog-request-id",
                "X-Goog-Request-Id",
                "request-id",
                "Request-Id",
            ),
        )
    )


def _status_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, int) and not isinstance(enum_value, bool):
        return enum_value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    return None


def _status_code(error: BaseException) -> int | None:
    for candidate in (getattr(error, "status_code", None), getattr(error, "code", None)):
        status = _status_integer(candidate)
        if status is not None:
            return status
    response = getattr(error, "response", None)
    if response is None:
        return None
    for candidate in (getattr(response, "status_code", None), getattr(response, "status", None)):
        status = _status_integer(candidate)
        if status is not None:
            return status
    return None


def _sdk_connection_errors() -> tuple[type[BaseException], ...]:
    """The exceptions the fixed SDK raises for a connection that broke.

    Before the response starts the SDK wraps every transport failure as
    `APIConnectionError`. Once a streamed response is being iterated it does not: the
    transport library's own error surfaces raw, `httpx.RemoteProtocolError` ("peer closed
    connection without sending complete message body") when the gateway closes a stream
    that has carried no bytes for about 40 s (measured 2026-09-15: silences of 34 s and
    38 s survived, 39 s did not, while the model thought without emitting a delta). Both
    are the same event, a cut, and the same verdict, transient; the raw family used to fall
    through `_failure_kind` as a provider rejection that was never retried.
    """
    import httpx as _httpx
    from openai import APIConnectionError as _APIConnectionError

    return (_APIConnectionError, _httpx.TransportError)


def _failure_kind(error: BaseException, *, phase: str | None = None) -> tuple[_FailureCode, bool]:
    if isinstance(error, GeminiAdapterError):
        code = error.failure.code
        return code, code in {_FailureCode.PROVIDER_TRANSIENT, _FailureCode.PROVIDER_RATE_LIMIT}
    if isinstance(error, TimeoutError | ConnectionError):
        return _FailureCode.PROVIDER_TRANSIENT, True
    status = _status_code(error)
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, _Mapping) else None
    # An SSE error arrives after HTTP headers, so the SDK may expose no HTTP
    # status. Trust its structured category, never error prose or a partial answer.
    if (
        status is None
        and phase == "STREAM_ASSEMBLY"
        and isinstance(detail, _Mapping)
        and any(
            str(detail.get(key, "")).lower() == "server_error" for key in ("type", "code", "status")
        )
    ):
        return _FailureCode.PROVIDER_TRANSIENT, True
    if (
        status == 400
        and isinstance(detail, _Mapping)
        and detail.get("code") == "context_length_exceeded"
    ):
        return _FailureCode.CONTEXT_TOO_LARGE, False
    if status == 429:
        return _FailureCode.PROVIDER_RATE_LIMIT, True
    if status is not None and 500 <= status <= 599:
        return _FailureCode.PROVIDER_TRANSIENT, True
    if status == 400:
        # Only explicit machine-readable parameter failures stop here. Generic
        # HTTP rejection text is not a diagnosis; retry the unchanged request.
        invalid_codes = {
            "invalid_argument",
            "invalid_request",
            "invalid_parameter",
            "invalid_value",
            "invalid_json",
            "invalid_json_schema",
            "invalid_schema",
            "invalid_model",
            "unsupported_parameter",
            "missing_required_parameter",
            "unknown_parameter",
            "unsupported_value",
        }
        invalid = isinstance(detail, _Mapping) and any(
            str(detail.get(key, "")).lower() in invalid_codes for key in ("code", "status", "type")
        )
        return _FailureCode.PROVIDER_REJECTED, not invalid
    return _FailureCode.PROVIDER_REJECTED, False


def _require_requested_json(request: _Mapping[str, object], payload: _Mapping[str, object]) -> None:
    """Refuse an answer that is not the JSON the request asked for.

    Keep the existing bounded retry policy, without interpreting invalid JSON as
    proof of truncation, network failure or exhausted generation budget. The attempt
    audit retains the answer and separate provider usage counters for inspection.
    """

    response_format = request.get("response_format")
    if (
        not isinstance(response_format, _Mapping)
        or response_format.get("mime_type") != "application/json"
    ):
        return
    output_text = payload.get("output_text")
    if not isinstance(output_text, str):
        return
    try:
        _json.loads(output_text)
    except ValueError:
        raise _failure(
            _FailureCode.PROVIDER_TRANSIENT,
            "Gemini provider returned an answer that is not the requested JSON",
        ) from None


def _status_detail(error: BaseException) -> tuple[_FailureDetail, ...]:
    """Seal the provider's HTTP status, which is the one safe thing its refusal carries.

    A rejection's own text carries URLs, headers and an echo of the request, so it never
    crosses this boundary. That left a refused call saying only that it was refused: a
    live Gate 1 rejection cost a whole diagnosis cycle because nothing recorded whether
    the provider had said the request was malformed, too large, or unauthorized. A status
    code is an integer and cannot leak any of that, so it is kept.
    """

    status = _status_code(error)
    if status is None:
        return ()
    return (_FailureDetail(key="provider_status", value=str(status)),)


def _failure_message(code: _FailureCode, error: BaseException) -> str:
    """The message this failure crosses the boundary with.

    Provider exceptions get a fixed sentence, because their own text carries URLs,
    headers and echoed request bodies that must not escape. A `GeminiAdapterError` is
    ours: every one of them is raised here with a message this module wrote, so it says
    what was actually wrong instead of being flattened into "failed transiently" at the
    exact moment -- the last attempt -- when a person needs to know why.

    Anything added to one of those messages has to stay Host-authored. Interpolating
    provider text into one would defeat the rule above by the back door.
    """

    if isinstance(error, GeminiAdapterError) and isinstance(error.failure, _TypedFailure):
        return error.failure.message
    if code is _FailureCode.PROVIDER_RATE_LIMIT:
        return "Gemini provider rate limited the interaction"
    if code is _FailureCode.PROVIDER_TRANSIENT:
        return "Gemini provider interaction failed transiently"
    return "Gemini provider rejected the interaction"


def _retry_after(error: BaseException) -> float | None:
    raw_value = _header_value(
        _response_headers(error),
        ("Retry-After", "retry-after"),
    )
    if isinstance(raw_value, bool) or not isinstance(raw_value, int | float | str):
        return None
    try:
        value = float(raw_value)
    except ValueError:
        return None
    if not _math.isfinite(value) or value < 0.0:
        return None
    return value


# A gateway's Retry-After is honoured up to this: one call may not park a chain (E64).
_RETRY_AFTER_CAP_S = 60.0
# The client default when a caller names none; the literature lanes name 300 s.
_DEFAULT_TIMEOUT_S = 120.0


def _retry_delay(error: BaseException, attempt: int) -> float:
    exponential = min(5.0 * (2.0 ** (attempt - 1)), _RETRY_AFTER_CAP_S)
    retry_after = _retry_after(error)
    if retry_after is None:
        return exponential
    return min(max(exponential, retry_after), _RETRY_AFTER_CAP_S)


def _normalized_response(response: object) -> tuple[dict[str, object], bytes]:
    try:
        if isinstance(response, _Mapping):
            raw_payload: object = dict(response)
        else:
            model_dump = getattr(response, "model_dump", None)
            if not callable(model_dump):
                raise TypeError
            raw_payload = model_dump(mode="json", by_alias=True, exclude_none=True)
        response_bytes = _canonical_json_bytes(raw_payload)
        payload = _json.loads(response_bytes)
    except (TypeError, ValueError, UnicodeError):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    if not isinstance(payload, dict):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    steps = payload.get("steps")
    if (
        not isinstance(payload.get("id"), str)
        or not payload["id"]
        or not isinstance(payload.get("status"), str)
        or not payload["status"]
        or not isinstance(steps, list)
        or not steps
        or any(
            not isinstance(step, _Mapping) or not isinstance(step.get("type"), str)
            for step in steps
        )
    ):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    return _cast(dict[str, object], payload), response_bytes


def _chat_content(value: object) -> str | list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini interaction request is not JSON-compatible",
        ) from None
    parts: list[dict[str, object]] = []
    texts: list[str] = []
    for item in value:
        if not isinstance(item, _Mapping):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini interaction request is not JSON-compatible",
            ) from None
        item_type = item.get("type")
        if item_type == "text" and isinstance(item.get("text"), str):
            text = item["text"]
            texts.append(text)
            parts.append({"type": "text", "text": text})
            continue
        if (
            item_type == "image"
            and isinstance(item.get("mime_type"), str)
            and isinstance(item.get("data"), str)
            and isinstance(item.get("resolution"), str)
        ):
            detail = {"low": "low", "medium": "auto", "high": "high"}.get(item["resolution"])
            if detail is None:
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini interaction request is not JSON-compatible",
                ) from None
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{item['mime_type']};base64,{item['data']}",
                        "detail": detail,
                    },
                }
            )
            continue
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini interaction request is not JSON-compatible",
        ) from None
    if len(parts) == 1 and texts:
        return texts[0]
    return parts


def _chat_tool_arguments(arguments: object) -> str:
    # A malformed model call is still valid transcript evidence. Chat's wire
    # transports argument text; its matching tool result explains the rejection.
    if isinstance(arguments, str):
        return arguments
    if not isinstance(arguments, _Mapping):
        raise _failure(_FailureCode.PROVIDER_REJECTED,
                       "Gemini interaction request is not JSON-compatible")
    try:
        return _canonical_json_bytes(dict(arguments)).decode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise _failure(_FailureCode.PROVIDER_REJECTED,
                       "Gemini interaction request is not JSON-compatible") from None


def _chat_messages(request: _Mapping[str, object]) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    system_instruction = request.get("system_instruction")
    if system_instruction is not None:
        if not isinstance(system_instruction, str):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini interaction request is not JSON-compatible",
            ) from None
        messages.append({"role": "system", "content": system_instruction})
    history = request.get("input")
    if not isinstance(history, list):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini interaction request is not JSON-compatible",
        ) from None
    index = 0
    while index < len(history):
        step = history[index]
        if not isinstance(step, _Mapping) or not isinstance(step.get("type"), str):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini interaction request is not JSON-compatible",
            ) from None
        step_type = step["type"]
        if step_type == "user_input":
            messages.append({"role": "user", "content": _chat_content(step.get("content"))})
        elif step_type == "model_output":
            content = _chat_content(step.get("content"))
            if isinstance(content, list):
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini interaction request is not JSON-compatible",
                ) from None
            tool_calls: list[dict[str, object]] = []
            while index + 1 < len(history):
                next_step = history[index + 1]
                if not isinstance(next_step, _Mapping) or next_step.get("type") != "function_call":
                    break
                call_id = next_step.get("call_id")
                name = next_step.get("name")
                arguments = next_step.get("arguments")
                if (
                    not isinstance(call_id, str)
                    or not isinstance(name, str)
                ):
                    raise _failure(
                        _FailureCode.PROVIDER_REJECTED,
                        "Gemini interaction request is not JSON-compatible",
                    ) from None
                argument_text = _chat_tool_arguments(arguments)
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": argument_text},
                    }
                )
                index += 1
            assistant: dict[str, object] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)
        elif step_type == "function_result":
            call_id = step.get("call_id")
            result = step.get("result")
            if (
                not isinstance(call_id, str)
                or not isinstance(result, list)
                or len(result) != 1
                or not isinstance(result[0], _Mapping)
                or result[0].get("type") != "text"
                or not isinstance(result[0].get("text"), str)
            ):
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini interaction request is not JSON-compatible",
                ) from None
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result[0]["text"]})
        elif step_type == "function_call":
            standalone_tool_calls: list[dict[str, object]] = []
            while index < len(history):
                next_step = history[index]
                if not isinstance(next_step, _Mapping) or next_step.get("type") != "function_call":
                    break
                call_id = next_step.get("call_id")
                name = next_step.get("name")
                arguments = next_step.get("arguments")
                if (
                    not isinstance(call_id, str)
                    or not isinstance(name, str)
                ):
                    raise _failure(
                        _FailureCode.PROVIDER_REJECTED,
                        "Gemini interaction request is not JSON-compatible",
                    ) from None
                argument_text = _chat_tool_arguments(arguments)
                standalone_tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": argument_text},
                    }
                )
                index += 1
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": standalone_tool_calls,
                }
            )
            continue
        elif step_type != "function_call":
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini interaction request is not JSON-compatible",
            ) from None
        index += 1
    return messages


def _context_policy() -> dict[str, object]:
    return {
        "version": _CONTEXT_ESTIMATOR_VERSION,
        "capacity_source": "owner-provided-deployment-2026-09-27",
        "context_tokens": GEMINI_CONTEXT_TOKENS,
        "output_reserve_tokens": GEMINI_MAX_OUTPUT_TOKENS,
    }


def _context_capacity(
    request_bytes: bytes, model_profile: str, *, stream: bool = False
) -> dict[str, object]:
    """Measure the canonical protocol parameters, not internal input characters.

    One UTF-8 byte per input token is a conservative planning proxy, not a tokenizer
    or a mathematical bound. Image payloads have no numerical estimate here.
    """
    wire = _chat_request(_json.loads(request_bytes), stream=stream)
    messages = _cast(list[dict[str, object]], wire["messages"])
    text_only = all(
        not isinstance(content, list) or all(part.get("type") == "text" for part in content)
        for message in messages
        for content in [message.get("content")]
    )
    wire_bytes = len(_canonical_json_bytes(wire))
    return {
        "request_sha256": _hashlib.sha256(request_bytes).hexdigest(),
        "model_profile": model_profile,
        **_context_policy(),
        "method": "wire_utf8_bytes_as_input_token_proxy" if text_only else "unknown_multimodal",
        "exact": False,
        "wire_utf8_bytes": wire_bytes,
        "estimated_input_tokens": wire_bytes if text_only else None,
    }


def _chat_request(request: _Mapping[str, object], *, stream: bool = False) -> dict[str, object]:
    wire: dict[str, object] = {
        "model": GEMINI_MODEL_ID,
        "messages": _chat_messages(request),
        "store": False,
        "stream": stream,
        # Pin the requested limit in the sealed request. Provider usage counters may
        # overlap; their sum is not evidence of remaining budget or truncation.
        "max_tokens": GEMINI_MAX_OUTPUT_TOKENS,
    }
    if stream:
        # Usage arrives only on the final chunk, and the interaction record needs it.
        wire["stream_options"] = {"include_usage": True}
    generation_config = request.get("generation_config")
    if not isinstance(generation_config, _Mapping):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED, "Gemini interaction request is not JSON-compatible"
        ) from None
    thinking_level = generation_config.get("thinking_level")
    temperature = generation_config.get("temperature")
    if thinking_level is not None:
        if thinking_level not in {"high", "medium"} or len(generation_config) != 1:
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini interaction request is not JSON-compatible",
            ) from None
        wire["reasoning_effort"] = thinking_level
    elif temperature == 0 and len(generation_config) == 1:
        wire["temperature"] = 0
    else:
        raise _failure(
            _FailureCode.PROVIDER_REJECTED, "Gemini interaction request is not JSON-compatible"
        ) from None
    tools = request.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED, "Gemini interaction request is not JSON-compatible"
            ) from None
        wrapped: list[dict[str, object]] = []
        for declaration in tools:
            if not isinstance(declaration, _Mapping) or declaration.get("type") != "function":
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini interaction request is not JSON-compatible",
                ) from None
            function = {key: value for key, value in declaration.items() if key != "type"}
            wrapped.append({"type": "function", "function": function})
        wire["tools"] = wrapped
        if wrapped:
            wire["parallel_tool_calls"] = True
    response_format = request.get("response_format")
    if response_format is not None:
        if not isinstance(response_format, _Mapping) or not isinstance(
            response_format.get("schema"), _Mapping
        ):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED, "Gemini interaction request is not JSON-compatible"
            ) from None
        wire["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "tsdaf_response",
                "strict": True,
                "schema": dict(_cast(_Mapping[str, object], response_format["schema"])),
            },
        }
    return wire


_Watchdog = _Callable[[float, _Callable[[], None]], _Callable[[], None]]


def _silence_watchdog(seconds: float, on_expiry: _Callable[[], None]) -> _Callable[[], None]:
    """Arm `on_expiry` to run after `seconds` on a daemon timer; returns its cancel."""
    timer = _threading.Timer(seconds, on_expiry)
    timer.daemon = True
    timer.start()
    return timer.cancel


def _delta_wrote(delta: _Mapping[str, object]) -> bool:
    """Whether this delta is the model writing: an answer, its thinking, or a call."""
    return bool(delta.get("content") or delta.get("reasoning_content") or delta.get("tool_calls"))


def _assembled_chat_completion(
    stream: _Iterable[_Any],
    *,
    monotonic: _Callable[[], float] = _time.monotonic,
    silence_s: float | None = None,
    watchdog: _Watchdog = _silence_watchdog,
) -> dict[str, object]:
    """Rebuild the completion a non-streamed call would have returned.

    Streaming is a transport choice, not a different answer, so the chunks are
    reassembled into the exact mapping `_normalized_chat_completion` already accepts.
    Nothing downstream -- the normalized response bytes, the sealed provenance, the
    recorded fixture format -- learns that the bytes arrived in pieces.

    A stream that ends without its terminal chunk is not an answer (E64): the request
    asks for usage, which arrives only on the final chunk, so a stream with no usage
    was cut -- by the gateway closing an idle connection or an upstream EOF. Taking
    the prefix as complete gave two bad outcomes: a half JSON refused as "not JSON"
    and resent at full context, or a parseable prefix accepted with every token count
    null. The cut is transient; the loop refreshes the connection and retries it.

    Nothing bounds how long a stream that is being written may take. A wall-clock bound
    cannot tell a model that is still thinking from a connection that has stalled, and
    cutting a call that is still being answered throws away the answer and resends the
    whole context to ask for it again. So the only stream this gives up on is one that
    has written nothing at all -- no answer, no thinking, no tool call -- for
    `silence_s`, and giving up means a transient failure the loop refreshes and retries,
    never a kill of the work (E96).

    The read timeout alone cannot see that silence: a supplement-lane call of
    2026-09-15 sat two hours on a socket that kept returning bytes within its 300 s read
    timeout while no chunk ever completed, which is how an SSE heartbeat reads -- the
    decoder swallows it and yields nothing. Hence a timer, disarmed by the first thing
    the model writes.
    """

    identity: str | None = None
    model: str | None = None
    finish_reason: str | None = None
    content: list[str] = []
    calls: dict[int, dict[str, object]] = {}
    usage: object = None
    started = monotonic()
    silent = _threading.Event()

    def cancel() -> None:
        return None

    if silence_s is not None:
        close = getattr(stream, "close", None)

        def on_expiry() -> None:
            silent.set()
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - the flag is what the reader needs
                    pass

        cancel = watchdog(silence_s, on_expiry)

    def silence_failure(cause: BaseException | None) -> GeminiAdapterError:
        failure = _failure(
            _FailureCode.PROVIDER_TRANSIENT,
            "Gemini provider stream wrote nothing for "
            f"{monotonic() - started:.0f} s and was refreshed",
        )
        if cause is not None:
            failure.__cause__ = cause
        return failure

    try:
        for chunk in stream:
            if silent.is_set():
                raise silence_failure(None)
            raw = chunk.model_dump(mode="json", by_alias=True, exclude_none=True)
            if not isinstance(raw, _Mapping):
                raise TypeError
            identity = identity or _cast(str | None, raw.get("id"))
            model = model or _cast(str | None, raw.get("model"))
            if isinstance(raw.get("usage"), _Mapping):
                usage = raw["usage"]
            choices = raw.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, _Mapping):
                raise TypeError
            finish_reason = _cast(str | None, choice.get("finish_reason")) or finish_reason
            delta = choice.get("delta")
            if not isinstance(delta, _Mapping):
                continue
            if _delta_wrote(delta):
                # The model is answering; from here the call is on no clock of ours.
                cancel()
            piece = delta.get("content")
            if isinstance(piece, str):
                content.append(piece)
            partial_calls = delta.get("tool_calls")
            if isinstance(partial_calls, list):
                for partial in partial_calls:
                    if not isinstance(partial, _Mapping):
                        raise TypeError
                    index = partial.get("index")
                    if not isinstance(index, int):
                        raise TypeError
                    call = calls.setdefault(index, {"id": "", "type": "function"})
                    if isinstance(partial.get("id"), str):
                        call["id"] = partial["id"]
                    function = partial.get("function")
                    if isinstance(function, _Mapping):
                        merged = _cast(
                            dict[str, object],
                            call.setdefault("function", {"name": "", "arguments": ""}),
                        )
                        if isinstance(function.get("name"), str):
                            merged["name"] = function["name"]
                        if isinstance(function.get("arguments"), str):
                            merged["arguments"] = (
                                _cast(str, merged.get("arguments", "")) + function["arguments"]
                            )
    except GeminiAdapterError:
        raise
    except Exception as error:
        # The watchdog closed a stream that had written nothing: whatever the woken
        # read raises is that silence, not the provider's answer.
        if silent.is_set():
            raise silence_failure(error) from error
        if isinstance(error, AttributeError | TypeError | ValueError):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini provider returned an invalid interaction response",
            ) from error
        raise
    finally:
        cancel()
    if usage is None:
        raise _failure(
            _FailureCode.PROVIDER_TRANSIENT,
            "Gemini provider stream ended without its terminal chunk",
        )
    message: dict[str, object] = {"role": "assistant"}
    if content:
        message["content"] = "".join(content)
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    completion: dict[str, object] = {
        "id": identity or "",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    }
    if model is not None:
        completion["model"] = model
    if usage is not None:
        completion["usage"] = usage
    return completion


# How a provider says an answer stopped at the token limit. The OpenAI spelling is
# `length`; the lpz gateway passes Gemini's own `max_tokens` through. Until 2026-10-10
# only the first was known here, so an answer cut at the 65,536-token ceiling (thought
# plus answer) failed two layers later as invalid JSON and was sent again unchanged:
# Kalmanol's supplement spent four attempts of five to seven minutes each that way.
_TOKEN_LIMIT_FINISHES = frozenset({"length", "max_tokens", "MAX_TOKENS"})


def _normalized_chat_completion(response: object) -> tuple[dict[str, object], bytes]:
    try:
        if isinstance(response, _Mapping):
            raw_payload: object = dict(response)
        else:
            model_dump = getattr(response, "model_dump", None)
            if not callable(model_dump):
                raise TypeError
            raw_payload = model_dump(mode="json", by_alias=True, exclude_none=True)
        completion = _json.loads(_canonical_json_bytes(raw_payload))
    except (TypeError, ValueError, UnicodeError):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    if (
        not isinstance(completion, dict)
        or not isinstance(completion.get("id"), str)
        or not completion["id"]
    ):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    choices = completion.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], _Mapping):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    if choices[0].get("finish_reason") in _TOKEN_LIMIT_FINISHES:
        # An answer cut off at the token limit is still valid JSON at the wire level and
        # only fails two layers later, as an unparseable contract. Saying so here names
        # the actual cause -- and its own code: resending the same request cannot mend
        # the model's own answer, so this loop does not try again (E64).
        raise _failure(
            _FailureCode.PROVIDER_OUTPUT_TRUNCATED,
            "Gemini provider stopped at the output token limit before finishing its answer",
        ) from None
    message = choices[0].get("message")
    if not isinstance(message, _Mapping):
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    steps: list[dict[str, object]] = []
    content = message.get("content")
    if content is not None:
        if not isinstance(content, str):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini provider returned an invalid interaction response",
            ) from None
        steps.append({"type": "model_output", "content": [{"type": "text", "text": content}]})
    tool_calls = message.get("tool_calls")
    if tool_calls is not None:
        if not isinstance(tool_calls, list):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini provider returned an invalid interaction response",
            ) from None
        for tool_call in tool_calls:
            if not isinstance(tool_call, _Mapping) or not isinstance(tool_call.get("id"), str):
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini provider returned an invalid interaction response",
                ) from None
            function = tool_call.get("function")
            if (
                not isinstance(function, _Mapping)
                or not isinstance(function.get("name"), str)
                or not isinstance(function.get("arguments"), str)
            ):
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini provider returned an invalid interaction response",
                ) from None
            try:
                arguments = _json.loads(function["arguments"])
            except _json.JSONDecodeError:
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini provider returned an invalid interaction response",
                ) from None
            if not isinstance(arguments, dict):
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini provider returned an invalid interaction response",
                ) from None
            steps.append(
                {
                    "type": "function_call",
                    "name": function["name"],
                    "call_id": tool_call["id"],
                    "arguments": arguments,
                }
            )
    if not steps:
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini provider returned an invalid interaction response",
        ) from None
    usage_value = completion.get("usage")
    usage: _Mapping[str, object] = (
        _cast(_Mapping[str, object], usage_value) if isinstance(usage_value, _Mapping) else {}
    )
    raw_prompt_details = usage.get("prompt_tokens_details")
    prompt_details: _Mapping[str, object] = (
        _cast(_Mapping[str, object], raw_prompt_details)
        if isinstance(raw_prompt_details, _Mapping)
        else {}
    )
    raw_completion_details = usage.get("completion_tokens_details")
    completion_details: _Mapping[str, object] = (
        _cast(_Mapping[str, object], raw_completion_details)
        if isinstance(raw_completion_details, _Mapping)
        else {}
    )
    payload: dict[str, object] = {
        "id": completion["id"],
        "status": "completed",
        "steps": steps,
        "usage": {
            "total_input_tokens": usage.get("prompt_tokens"),
            "total_output_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "total_cached_tokens": prompt_details.get("cached_tokens"),
            "total_thought_tokens": completion_details.get("reasoning_tokens"),
            "total_tool_use_tokens": None,
        },
    }
    if isinstance(content, str):
        payload["output_text"] = content
    if isinstance(completion.get("model"), str):
        payload["provider_model"] = completion["model"]
    response_bytes = _canonical_json_bytes(payload)
    return payload, response_bytes


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_token_total(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _interaction(
    *,
    request_bytes: bytes,
    response_bytes: bytes,
    payload: dict[str, object],
    request_id: str | None,
    elapsed_ms: float,
    transport_attempts: int,
    attempt_audit_bytes: bytes | None = None,
) -> GeminiInteraction:
    raw_usage = payload.get("usage")
    usage: _Mapping[str, object]
    if isinstance(raw_usage, _Mapping):
        usage = _cast(_Mapping[str, object], raw_usage)
    else:
        usage = {}
    return GeminiInteraction(
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        interaction_id=_optional_string(payload.get("id")),
        output_text=_optional_string(payload.get("output_text")),
        total_input_tokens=_optional_token_total(usage.get("total_input_tokens")),
        total_output_tokens=_optional_token_total(usage.get("total_output_tokens")),
        total_tokens=_optional_token_total(usage.get("total_tokens")),
        total_cached_tokens=_optional_token_total(usage.get("total_cached_tokens")),
        total_thought_tokens=_optional_token_total(usage.get("total_thought_tokens")),
        total_tool_use_tokens=_optional_token_total(usage.get("total_tool_use_tokens")),
        request_id=request_id,
        elapsed_ms=elapsed_ms,
        transport_attempts=transport_attempts,
        attempt_audit_bytes=attempt_audit_bytes,
    )


def _safe_attempt_metadata(response: object) -> dict[str, object]:
    """Keep named numeric usage fields and a bounded finish reason, not SDK metadata."""
    try:
        raw = (
            response
            if isinstance(response, _Mapping)
            else response.model_dump(  # type: ignore[attr-defined]
                mode="json", by_alias=True, exclude_none=True
            )
        )
    except Exception:
        return {}
    if not isinstance(raw, _Mapping):
        return {}
    usage = raw.get("usage")
    safe_usage: dict[str, object] = {}
    if isinstance(usage, _Mapping):
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "total_input_tokens",
            "total_output_tokens",
            "total_cached_tokens",
            "total_thought_tokens",
            "total_tool_use_tokens",
        ):
            if key in usage:
                safe_usage[key] = _optional_token_total(usage[key])
        for group, keys in (
            ("prompt_tokens_details", ("cached_tokens", "audio_tokens")),
            (
                "completion_tokens_details",
                (
                    "reasoning_tokens",
                    "audio_tokens",
                    "accepted_prediction_tokens",
                    "rejected_prediction_tokens",
                ),
            ),
        ):
            values = usage.get(group)
            if isinstance(values, _Mapping):
                safe_usage[group] = {
                    key: _optional_token_total(values[key]) for key in keys if key in values
                }
    metadata: dict[str, object] = {"reported_usage": safe_usage}
    choices = raw.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], _Mapping):
        finish = choices[0].get("finish_reason")
        metadata["finish_reason"] = (
            finish
            if finish
            in (None, "stop", "length", "max_tokens", "tool_calls", "content_filter", "function_call")
            else "UNRECOGNIZED"
        )
    return metadata


def _attempt_audit_bytes(request_bytes: bytes, attempts: list[dict[str, object]]) -> bytes:
    return _canonical_json_bytes(
        {
            "schema_version": _ATTEMPT_AUDIT_VERSION,
            "request_sha256": _hashlib.sha256(request_bytes).hexdigest(),
            # The normalized provider request contains no endpoint, API key or headers.
            # Keeping it once also covers a Router failure before on_interaction exists.
            "request": _json.loads(request_bytes),
            "attempts": attempts,
        }
    )


def _safe_provider_diagnostic(
    error: BaseException, request: _Mapping[str, object]
) -> dict[str, object]:
    """Keep structured error categories and declared field paths, never raw prose.

    Gateways may echo credentials, URLs or the whole request in error messages.
    Unknown categories stay unknown; vocabulary mentions are clues, not causes.
    """
    # The exception's class is structure, not prose: it says which layer gave up
    # (the SDK before the response, the transport mid-stream, this module's own checks).
    exception = f"{type(error).__module__}.{type(error).__name__}"
    body = getattr(error, "body", None)
    if not isinstance(body, _Mapping):
        return {"exception": exception, "detail_available": False}
    detail = body.get("error", body)
    if not isinstance(detail, _Mapping):
        return {"exception": exception, "detail_available": False}
    categories = {
        "invalid_request_error",
        "invalid_argument",
        "invalid_json",
        "invalid_schema",
        "unsupported_parameter",
        "unsupported_value",
        "unknown_parameter",
        "missing_required_parameter",
        "context_length_exceeded",
        "resource_exhausted",
        "authentication_error",
        "permission_denied",
        "rate_limit_exceeded",
        "server_error",
    }
    result: dict[str, object] = {"exception": exception, "detail_available": True}
    for key in ("type", "code", "status"):
        value = detail.get(key)
        if isinstance(value, str) and value.lower() in categories:
            result[key] = value.lower()
    names = {
        "tools",
        "function",
        "parameters",
        "response_format",
        "json_schema",
        "schema",
        "messages",
        "content",
        "model",
        "parallel_tool_calls",
        "strict",
        "stream",
        "max_tokens",
        "reasoning_effort",
        "generation_config",
        "generationConfig",
        "responseJsonSchema",
        "responseSchema",
        "responseMimeType",
        "functionDeclarations",
    }

    def schema_names(value: object) -> None:
        if isinstance(value, _Mapping):
            names.update(str(key) for key in value)
            for child in value.values():
                schema_names(child)
        elif isinstance(value, list):
            for child in value:
                schema_names(child)

    schema_names(request.get("tools"))
    schema_names(request.get("response_format"))
    fields = []
    for key in ("param", "field"):
        field = detail.get(key)
        if isinstance(field, str) and _re.fullmatch(r"[A-Za-z0-9_$./#\[\]-]{1,160}", field):
            parts = _re.findall(r"[A-Za-z_$][A-Za-z0-9_$-]*", field)
            if parts and all(part in names for part in parts):
                fields.append(field)
    result["fields"] = fields
    message = detail.get("message")
    if isinstance(message, str):
        # This bounded list preserves useful request vocabulary without retaining
        # an arbitrary provider value or pretending the mentioned field is at fault.
        result["message_mentions"] = [
            name
            for name in (
                "tools",
                "response_format",
                "json_schema",
                "strict",
                "$ref",
                "$defs",
                "parameters",
                "max_tokens",
                "reasoning_effort",
                "unsupported",
                "schema",
            )
            if name in message[:8192]
        ]
    return result


class GeminiInteractions:
    """Concrete fixed adapter with a private Chat Completions transport."""

    __slots__ = (
        "_chat_completions",
        "_client",
        "_connection_error_type",
        "_generation_config",
        "_max_transport_attempts",
        "_model_profile",
        "_monotonic",
        "_prepare_token",
        "_rebuild_client",
        "_sleep",
        "_stream",
        "_timeout",
    )

    def __init__(
        self,
        client: object,
        *,
        model: str = GEMINI_MODEL_ID,
        monotonic: _Callable[[], float] = _time.monotonic,
        sleep: _Callable[[float], None] = _time.sleep,
        generation_config: _Mapping[str, object] | None = None,
        max_transport_attempts: int = _MAX_TRANSPORT_ATTEMPTS,
        stream: bool = False,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        _require_fixed_model(model)
        if not isinstance(stream, bool):
            raise TypeError("stream must be a bool")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if (
            isinstance(max_transport_attempts, bool)
            or not isinstance(max_transport_attempts, int)
            or max_transport_attempts < 1
        ):
            raise ValueError("max_transport_attempts must be a positive integer")
        self._client: _Any = client
        self._monotonic = monotonic
        self._sleep = sleep
        self._generation_config = dict(generation_config or _MAIN_GENERATION_CONFIG)
        if self._generation_config == _MAIN_GENERATION_CONFIG:
            self._model_profile = GEMINI_MODEL_PROFILE
        elif self._generation_config == _OCSR_GENERATION_CONFIG:
            self._model_profile = GEMINI_OCSR_MODEL_PROFILE
        else:
            self._model_profile = "gemini-custom-generation-config-v1"
        self._prepare_token = object()
        self._max_transport_attempts = max_transport_attempts
        self._chat_completions = False
        self._stream = stream
        # The client's per-request timeout: how long one read may wait for bytes, and
        # how long a stream may write nothing at all. Not how long an answer may take
        # -- a stream being written is on no clock (E96).
        self._timeout = float(timeout)
        self._connection_error_type: (
            type[BaseException] | tuple[type[BaseException], ...] | None
        ) = None
        # Set by the live constructors: build a new SDK client, so a retry does not
        # reuse the pooled connection whose stream just died (E96).
        self._rebuild_client: _Callable[[], object] | None = None

    @property
    def max_transport_attempts(self) -> int:
        """Maximum sends behind one prepared interaction, for host admission."""
        return self._max_transport_attempts

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        *,
        base_url: str,
        model: str = GEMINI_MODEL_ID,
        monotonic: _Callable[[], float] = _time.monotonic,
        sleep: _Callable[[float], None] = _time.sleep,
        stream: bool = False,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_transport_attempts: int = _MAX_TRANSPORT_ATTEMPTS,
    ) -> _Self:
        """Create the fixed OpenAI-compatible client from explicit credentials.

        `timeout` is the client's per-request limit -- for a streamed call, how long one
        read may wait for bytes and how long the stream may write nothing at all. The
        answer itself is not on a clock: a turn that keeps writing keeps going, and a
        silent stream is refreshed and retried, not abandoned (E96).
        """

        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        from openai import OpenAI as _OpenAI

        def build() -> object:
            return _OpenAI(
                api_key=api_key, base_url=base_url, max_retries=0, timeout=float(timeout)
            )

        adapter = cls(
            build(),
            model=model,
            monotonic=monotonic,
            sleep=sleep,
            stream=stream,
            timeout=timeout,
            max_transport_attempts=max_transport_attempts,
        )
        adapter._chat_completions = True
        adapter._connection_error_type = _sdk_connection_errors()
        adapter._rebuild_client = build
        return adapter

    @classmethod
    def for_molecule_ocsr(
        cls,
        api_key: str,
        *,
        base_url: str,
        model: str = GEMINI_MODEL_ID,
        timeout: float = 120.0,
        retries: int = 2,
        monotonic: _Callable[[], float] = _time.monotonic,
        sleep: _Callable[[float], None] = _time.sleep,
    ) -> _Self:
        """Create the fixed one-crop OCSR transport profile."""

        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
            raise ValueError("retries must be a nonnegative integer")
        from openai import OpenAI as _OpenAI

        def build() -> object:
            return _OpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                timeout=float(timeout),
            )

        adapter = cls(
            build(),
            model=model,
            monotonic=monotonic,
            sleep=sleep,
            generation_config=_OCSR_GENERATION_CONFIG,
            max_transport_attempts=retries + 1,
            timeout=timeout,
        )
        adapter._chat_completions = True
        adapter._connection_error_type = _sdk_connection_errors()
        adapter._rebuild_client = build
        return adapter

    @property
    def interaction_stream_owner(self) -> object:
        """Identity of the mutable transport stream owned by this adapter."""

        return self._client

    def _refresh_transport(self) -> None:
        """Drop the connection a failed attempt used and get a new one.

        A stream that died mid-answer leaves a pooled connection whose state nobody
        can vouch for, and the supplement-lane hang of 2026-09-15 sat two hours on one
        such socket. Retrying on a fresh client is what "refresh" means here; when the
        client was injected (the fixtures, the tests) there is nothing to rebuild and
        the retry goes out on the client it was given.
        """
        if self._rebuild_client is None:
            return
        closer = getattr(self._client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - the old connection is being discarded
                pass
        self._client = self._rebuild_client()

    def create(
        self,
        *,
        input: object,
        system_instruction: object | None = None,
        tools: object | None = None,
        response_format: object | None = None,
    ) -> GeminiInteraction:
        """Create one nonstreaming stateless interaction with bounded retry."""

        return self.execute_prepared(
            self.prepare(
                input=input,
                system_instruction=system_instruction,
                tools=tools,
                response_format=response_format,
            )
        )

    @property
    def context_policy(self) -> _Mapping[str, object]:
        """A fresh value for hashing into existing task/cache identities."""
        return _context_policy()

    def prepare(
        self,
        *,
        input: object,
        system_instruction: object | None = None,
        tools: object | None = None,
        response_format: object | None = None,
        reasoning_effort: object = None,
    ) -> PreparedGeminiInteraction:
        """Serialize one request without consuming or calling the transport."""

        model_profile, generation_config = _request_profile(
            reasoning_effort, self._model_profile, self._generation_config
        )
        _, request_bytes = _request_and_bytes(
            input=input,
            system_instruction=system_instruction,
            tools=tools,
            response_format=response_format,
            generation_config=generation_config,
        )
        return PreparedGeminiInteraction(
            request_bytes=request_bytes,
            model_profile=model_profile,
            _adapter_token=self._prepare_token,
            context_capacity=_context_capacity(
                request_bytes,
                model_profile,
                stream=self._stream if self._chat_completions else False,
            ),
        )

    def execute_prepared(
        self,
        prepared: PreparedGeminiInteraction,
    ) -> GeminiInteraction:
        """Send exactly one immutable request previously prepared by this adapter."""

        request = _prepared_request(
            prepared,
            adapter_token=self._prepare_token,
            model_profile=self._model_profile,
            generation_config=self._generation_config,
        )
        request_bytes = prepared.request_bytes
        started = self._monotonic()
        audits: list[dict[str, object]] = []
        attempt_limit = self._max_transport_attempts
        for attempt in range(1, self._max_transport_attempts + 1):
            attempt_started = started if attempt == 1 else self._monotonic()
            phase = "REQUEST_ENCODING"
            payload: dict[str, object] | None = None
            response: object = None
            try:
                if self._chat_completions:
                    wire = _chat_request(request, stream=self._stream)
                    phase = "TRANSPORT"
                    response = self._client.chat.completions.create(**wire)
                    if self._stream:
                        phase = "STREAM_ASSEMBLY"
                        response = _assembled_chat_completion(
                            _cast(_Iterable[_Any], response),
                            monotonic=self._monotonic,
                            silence_s=self._timeout,
                        )
                    phase = "RESPONSE_NORMALIZATION"
                    payload, response_bytes = _normalized_chat_completion(response)
                else:
                    phase = "TRANSPORT"
                    response = self._client.interactions.create(**request)
                    phase = "RESPONSE_NORMALIZATION"
                    payload, response_bytes = _normalized_response(response)
                phase = "REQUESTED_JSON_VALIDATION"
                _require_requested_json(request, payload)
            except Exception as error:
                if self._connection_error_type is not None and isinstance(
                    error, self._connection_error_type
                ):
                    code, retryable = _FailureCode.PROVIDER_TRANSIENT, True
                else:
                    code, retryable = _failure_kind(error, phase=phase)
                finished = self._monotonic()
                unknown_400 = (
                    _status_code(error) == 400
                    and retryable
                    and code is _FailureCode.PROVIDER_REJECTED
                )
                if unknown_400:
                    attempt_limit = min(attempt_limit, UNKNOWN_HTTP_400_MAX_ATTEMPTS)
                will_retry = retryable and attempt < attempt_limit
                audits.append(
                    {
                        "attempt_index": attempt,
                        "phase": phase,
                        "outcome": "FAILED",
                        "failure_code": code.value,
                        "retry_delay_s": _retry_delay(error, attempt) if will_retry else None,
                        "reason": (
                            "NON_JSON_RESPONSE"
                            if phase == "REQUESTED_JSON_VALIDATION"
                            else "INVALID_RESPONSE_ENVELOPE"
                            if phase == "RESPONSE_NORMALIZATION"
                            else UNKNOWN_HTTP_400_REASON
                            if unknown_400
                            else INVALID_HTTP_400_REASON
                            if _status_code(error) == 400 and code is _FailureCode.PROVIDER_REJECTED
                            else code.value
                        ),
                        "elapsed_ms": max(0.0, (finished - attempt_started) * 1000.0),
                        "retry_scheduled": will_retry,
                        "normalized_response": payload,
                        "http_status": _status_code(error),
                        "provider_diagnostic": _safe_provider_diagnostic(error, request),
                        **_safe_attempt_metadata(
                            payload
                            if not self._chat_completions and payload is not None
                            else response
                        ),
                    }
                )
                if will_retry:
                    self._refresh_transport()
                    self._sleep(_retry_delay(error, attempt))
                    continue
                raise _failure(
                    code,
                    _failure_message(code, error),
                    request_id=_request_id_from(error),
                    details=_status_detail(error),
                    attempt_audit_bytes=_attempt_audit_bytes(request_bytes, audits),
                ) from None
            finished = self._monotonic()
            audits.append(
                {
                    "attempt_index": attempt,
                    "phase": "COMPLETED",
                    "outcome": "SUCCEEDED",
                    "failure_code": None,
                    "reason": None,
                    "elapsed_ms": max(0.0, (finished - attempt_started) * 1000.0),
                    "retry_scheduled": False,
                    "normalized_response": payload,
                    **_safe_attempt_metadata(
                        payload if not self._chat_completions and payload is not None else response
                    ),
                }
            )
            elapsed_ms = max(0.0, (finished - started) * 1000.0)
            return _interaction(
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                payload=payload,
                request_id=_request_id_from(response, payload),
                elapsed_ms=elapsed_ms,
                transport_attempts=attempt,
                attempt_audit_bytes=_attempt_audit_bytes(request_bytes, audits),
            )
        raise AssertionError("unreachable")


class RecordedGeminiInteractions:
    """Deterministic replay of one local Interactions fixture, keyed by request.

    Replay used to consume records by position, which made an offline run's answers
    depend on the order its callers happened to ask in. That was fine while every layer
    was sequential and became the one thing stopping the window layer (reaction windows,
    retired 2026-09-23) from running its windows concurrently -- the windows were
    independent, but their replay was not.

    A record is now found by the hash of the request being made, so a caller gets the
    answer recorded for its own question whatever order it asks in. Records sharing a
    request stay in recorded order behind it, which is what a retry sequence needs, and
    the cursor is guarded because a route group's two readers are still dispatched
    concurrently and share this one replay.
    """

    __slots__ = ("_by_request", "_cursor_lock", "_prepare_token", "_fixture_sha256")

    def __init__(self, fixture_path: str | _Path, *, model: str = GEMINI_MODEL_ID) -> None:
        _require_fixed_model(model)
        try:
            fixture_bytes = _Path(fixture_path).read_bytes()
            fixture = _json.loads(fixture_bytes.decode("utf-8"))
            self._fixture_sha256 = _hashlib.sha256(fixture_bytes).hexdigest()
        except (OSError, UnicodeError, _json.JSONDecodeError):
            raise _failure(
                _FailureCode.PACKAGE_INVALID,
                "Gemini recorded interaction fixture is invalid",
            ) from None
        if (
            not isinstance(fixture, dict)
            or fixture.get("schema_version") != _FIXTURE_SCHEMA_VERSION
            or fixture.get("model_profile") != GEMINI_MODEL_PROFILE
        ):
            raise _failure(
                _FailureCode.PACKAGE_INVALID,
                "Gemini recorded interaction fixture is invalid",
            ) from None
        records = fixture.get("interactions")
        if not isinstance(records, list) or not records:
            raise _failure(
                _FailureCode.PACKAGE_INVALID,
                "Gemini recorded interaction fixture is invalid",
            ) from None
        # Index the request half of every record now: it is what a lookup needs, and a
        # fixture whose request cannot be canonicalized is broken however it is read.
        # The response half stays unread until consumed, so a bad response still surfaces
        # as the failure of the call that asked for it.
        by_request: dict[str, _deque[dict[str, object]]] = {}
        for raw_record in records:
            if not isinstance(raw_record, dict):
                raise _failure(
                    _FailureCode.PACKAGE_INVALID,
                    "Gemini recorded interaction fixture record is invalid",
                ) from None
            record = _cast(dict[str, object], raw_record)
            recorded_request = record.get("request")
            try:
                recorded_request_bytes = _canonical_json_bytes(recorded_request)
            except (TypeError, ValueError, UnicodeError):
                raise _failure(
                    _FailureCode.PACKAGE_INVALID,
                    "Gemini recorded interaction fixture record is invalid",
                ) from None
            digest = _hashlib.sha256(recorded_request_bytes).hexdigest()
            if not isinstance(recorded_request, dict) or record.get("request_sha256") != digest:
                raise _failure(
                    _FailureCode.PACKAGE_INVALID,
                    "Gemini recorded interaction fixture record is invalid",
                ) from None
            by_request.setdefault(digest, _deque()).append(record)
        self._by_request = by_request
        self._cursor_lock = _threading.Lock()
        self._prepare_token = object()

    @property
    def fixture_sha256(self) -> str:
        """Identity of the exact fixture snapshot loaded into this replay client."""
        return self._fixture_sha256

    @property
    def interaction_stream_owner(self) -> object:
        """Identity of this replay instance's sequential record cursor."""

        return self

    def create(
        self,
        *,
        input: object,
        system_instruction: object | None = None,
        tools: object | None = None,
        response_format: object | None = None,
    ) -> GeminiInteraction:
        """Consume and verify the next recorded interaction."""

        return self.execute_prepared(
            self.prepare(
                input=input,
                system_instruction=system_instruction,
                tools=tools,
                response_format=response_format,
            )
        )

    @property
    def context_policy(self) -> _Mapping[str, object]:
        """A fresh value for hashing into existing task/cache identities."""
        return _context_policy()

    def prepare(
        self,
        *,
        input: object,
        system_instruction: object | None = None,
        tools: object | None = None,
        response_format: object | None = None,
        reasoning_effort: object = None,
    ) -> PreparedGeminiInteraction:
        """Serialize a replay request without advancing the fixture cursor."""

        model_profile, generation_config = _request_profile(
            reasoning_effort, GEMINI_MODEL_PROFILE, _MAIN_GENERATION_CONFIG
        )
        _, request_bytes = _request_and_bytes(
            input=input,
            system_instruction=system_instruction,
            tools=tools,
            response_format=response_format,
            generation_config=generation_config,
        )
        return PreparedGeminiInteraction(
            request_bytes=request_bytes,
            model_profile=model_profile,
            _adapter_token=self._prepare_token,
            context_capacity=_context_capacity(request_bytes, model_profile),
        )

    def execute_prepared(
        self,
        prepared: PreparedGeminiInteraction,
    ) -> GeminiInteraction:
        """Consume the next fixture record for a request prepared by this replay."""

        _prepared_request(
            prepared,
            adapter_token=self._prepare_token,
            model_profile=GEMINI_MODEL_PROFILE,
            generation_config=_MAIN_GENERATION_CONFIG,
        )
        request_bytes = prepared.request_bytes
        digest = _hashlib.sha256(request_bytes).hexdigest()
        pending = self._by_request.get(digest)
        if pending is None:
            # Nothing in this fixture was recorded for the question being asked.
            raise _failure(
                _FailureCode.HASH_MISMATCH,
                "Gemini recorded interaction request does not match",
            ) from None
        with self._cursor_lock:
            if not pending:
                raise _failure(
                    _FailureCode.PROVIDER_REJECTED,
                    "Gemini recorded interaction fixture is exhausted",
                ) from None
            record = pending.popleft()
        response = record.get("response")
        try:
            payload, response_bytes = _normalized_response(response)
        except GeminiAdapterError:
            raise _failure(
                _FailureCode.PACKAGE_INVALID,
                "Gemini recorded interaction fixture response is invalid",
            ) from None
        raw_elapsed_ms = record.get("elapsed_ms")
        if (
            isinstance(raw_elapsed_ms, bool)
            or not isinstance(raw_elapsed_ms, int | float)
            or not _math.isfinite(raw_elapsed_ms)
            or raw_elapsed_ms < 0
        ):
            raise _failure(
                _FailureCode.PACKAGE_INVALID,
                "Gemini recorded interaction fixture timing is invalid",
            ) from None
        return _interaction(
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            payload=payload,
            request_id=_request_id_from(response, payload),
            elapsed_ms=float(raw_elapsed_ms),
            transport_attempts=1,
        )
