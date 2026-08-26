"""Minimal live and recorded adapters with OpenAI-compatible Chat Completions."""

from __future__ import annotations

import hashlib as _hashlib
import json as _json
import math as _math
import re as _re
import time as _time
from collections.abc import Callable as _Callable
from collections.abc import Mapping as _Mapping
from dataclasses import dataclass as _dataclass
from dataclasses import field as _field
from pathlib import Path as _Path
from typing import Any as _Any
from typing import Self as _Self
from typing import cast as _cast

from tsdaf.contracts.execution import FailureCode as _FailureCode
from tsdaf.contracts.execution import TypedFailure as _TypedFailure
from tsdaf.contracts.schema import canonical_json_bytes as _canonical_json_bytes

__all__ = (
    "GEMINI_MODEL_ID",
    "GEMINI_MODEL_PROFILE",
    "GeminiAdapterError",
    "GeminiInteraction",
    "GeminiInteractions",
    "RecordedGeminiInteractions",
)

GEMINI_MODEL_ID = "gemini-3.7-flash-high"
GEMINI_MODEL_PROFILE = "gemini-3.7-flash-high-chat-completions-v1"

_FIXTURE_SCHEMA_VERSION = "tsdaf.recorded-gemini-interactions.v1"
_MAX_TRANSPORT_ATTEMPTS = 4
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


class GeminiAdapterError(Exception):
    """Safe adapter error carrying exactly one provider-neutral failure."""

    __slots__ = ("failure",)

    def __init__(self, failure: _TypedFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)

    def __str__(self) -> str:
        return self.failure.message

    def __repr__(self) -> str:
        return f"GeminiAdapterError({self.failure.message!r})"


def _failure(
    code: _FailureCode, message: str, *, request_id: str | None = None
) -> GeminiAdapterError:
    return GeminiAdapterError(
        _TypedFailure(
            schema_version="tsdaf.typed-failure.v1",
            code=code,
            message=message,
            request_id=request_id,
        )
    )


def _require_fixed_model(model: str) -> None:
    if model != GEMINI_MODEL_ID:
        raise _failure(
            _FailureCode.PROVIDER_REJECTED,
            "Gemini adapter requires the fixed model profile",
        ) from None


def _request_and_bytes(
    *,
    input: object,
    system_instruction: object | None,
    tools: object | None,
    response_format: object | None,
) -> tuple[dict[str, object], bytes]:
    request: dict[str, object] = {
        "input": input,
        "model": GEMINI_MODEL_ID,
        "store": False,
        "stream": False,
        "background": False,
        "generation_config": {"thinking_level": "high"},
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


def _failure_kind(error: BaseException) -> tuple[_FailureCode, bool]:
    if isinstance(error, TimeoutError | ConnectionError):
        return _FailureCode.PROVIDER_TRANSIENT, True
    status = _status_code(error)
    if status == 429:
        return _FailureCode.PROVIDER_RATE_LIMIT, True
    if status is not None and 500 <= status <= 599:
        return _FailureCode.PROVIDER_TRANSIENT, True
    return _FailureCode.PROVIDER_REJECTED, False


def _failure_message(code: _FailureCode) -> str:
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


def _retry_delay(error: BaseException, attempt: int) -> float:
    exponential = min(0.25 * (2.0 ** (attempt - 1)), 2.0)
    retry_after = _retry_after(error)
    if retry_after is None:
        return exponential
    return max(exponential, retry_after)


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
                    or not isinstance(arguments, _Mapping)
                ):
                    raise _failure(
                        _FailureCode.PROVIDER_REJECTED,
                        "Gemini interaction request is not JSON-compatible",
                    ) from None
                try:
                    argument_text = _canonical_json_bytes(dict(arguments)).decode("utf-8")
                except (TypeError, ValueError, UnicodeError):
                    raise _failure(
                        _FailureCode.PROVIDER_REJECTED,
                        "Gemini interaction request is not JSON-compatible",
                    ) from None
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
                    or not isinstance(arguments, _Mapping)
                ):
                    raise _failure(
                        _FailureCode.PROVIDER_REJECTED,
                        "Gemini interaction request is not JSON-compatible",
                    ) from None
                try:
                    argument_text = _canonical_json_bytes(dict(arguments)).decode("utf-8")
                except (TypeError, ValueError, UnicodeError):
                    raise _failure(
                        _FailureCode.PROVIDER_REJECTED,
                        "Gemini interaction request is not JSON-compatible",
                    ) from None
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


def _chat_request(request: _Mapping[str, object]) -> dict[str, object]:
    wire: dict[str, object] = {
        "model": GEMINI_MODEL_ID,
        "messages": _chat_messages(request),
        "reasoning_effort": "high",
        "store": False,
        "stream": False,
    }
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
    )


class GeminiInteractions:
    """Concrete fixed adapter with a private Chat Completions transport."""

    __slots__ = ("_chat_completions", "_client", "_connection_error_type", "_monotonic", "_sleep")

    def __init__(
        self,
        client: object,
        *,
        model: str = GEMINI_MODEL_ID,
        monotonic: _Callable[[], float] = _time.monotonic,
        sleep: _Callable[[float], None] = _time.sleep,
    ) -> None:
        _require_fixed_model(model)
        self._client: _Any = client
        self._monotonic = monotonic
        self._sleep = sleep
        self._chat_completions = False
        self._connection_error_type: type[BaseException] | None = None

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        *,
        base_url: str,
        model: str = GEMINI_MODEL_ID,
        monotonic: _Callable[[], float] = _time.monotonic,
        sleep: _Callable[[float], None] = _time.sleep,
    ) -> _Self:
        """Create the fixed OpenAI-compatible client from explicit credentials."""

        from openai import APIConnectionError as _APIConnectionError
        from openai import OpenAI as _OpenAI

        adapter = cls(
            _OpenAI(api_key=api_key, base_url=base_url, max_retries=0),
            model=model,
            monotonic=monotonic,
            sleep=sleep,
        )
        adapter._chat_completions = True
        adapter._connection_error_type = _APIConnectionError
        return adapter

    def create(
        self,
        *,
        input: object,
        system_instruction: object | None = None,
        tools: object | None = None,
        response_format: object | None = None,
    ) -> GeminiInteraction:
        """Create one nonstreaming stateless interaction with bounded retry."""

        request, request_bytes = _request_and_bytes(
            input=input,
            system_instruction=system_instruction,
            tools=tools,
            response_format=response_format,
        )
        started = self._monotonic()
        for attempt in range(1, _MAX_TRANSPORT_ATTEMPTS + 1):
            try:
                if self._chat_completions:
                    response = self._client.chat.completions.create(**_chat_request(request))
                    payload, response_bytes = _normalized_chat_completion(response)
                else:
                    response = self._client.interactions.create(**request)
                    payload, response_bytes = _normalized_response(response)
            except Exception as error:
                if self._connection_error_type is not None and isinstance(
                    error, self._connection_error_type
                ):
                    code, retryable = _FailureCode.PROVIDER_TRANSIENT, True
                else:
                    code, retryable = _failure_kind(error)
                if retryable and attempt < _MAX_TRANSPORT_ATTEMPTS:
                    self._sleep(_retry_delay(error, attempt))
                    continue
                raise _failure(
                    code,
                    _failure_message(code),
                    request_id=_request_id_from(error),
                ) from None
            elapsed_ms = max(0.0, (self._monotonic() - started) * 1000.0)
            return _interaction(
                request_bytes=request_bytes,
                response_bytes=response_bytes,
                payload=payload,
                request_id=_request_id_from(response, payload),
                elapsed_ms=elapsed_ms,
                transport_attempts=attempt,
            )
        raise AssertionError("unreachable")


class RecordedGeminiInteractions:
    """Deterministic sequential replay of one local Interactions fixture."""

    __slots__ = ("_index", "_records")

    def __init__(self, fixture_path: str | _Path, *, model: str = GEMINI_MODEL_ID) -> None:
        _require_fixed_model(model)
        try:
            fixture = _json.loads(_Path(fixture_path).read_text(encoding="utf-8"))
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
        self._records = tuple(records)
        self._index = 0

    def create(
        self,
        *,
        input: object,
        system_instruction: object | None = None,
        tools: object | None = None,
        response_format: object | None = None,
    ) -> GeminiInteraction:
        """Consume and verify the next recorded interaction."""

        request, request_bytes = _request_and_bytes(
            input=input,
            system_instruction=system_instruction,
            tools=tools,
            response_format=response_format,
        )
        del request
        if self._index >= len(self._records):
            raise _failure(
                _FailureCode.PROVIDER_REJECTED,
                "Gemini recorded interaction fixture is exhausted",
            ) from None
        raw_record = self._records[self._index]
        self._index += 1
        if not isinstance(raw_record, dict):
            raise _failure(
                _FailureCode.PACKAGE_INVALID,
                "Gemini recorded interaction fixture record is invalid",
            ) from None
        record = _cast(dict[str, object], raw_record)
        recorded_request = record.get("request")
        recorded_hash = record.get("request_sha256")
        try:
            recorded_request_bytes = _canonical_json_bytes(recorded_request)
        except (TypeError, ValueError, UnicodeError):
            raise _failure(
                _FailureCode.PACKAGE_INVALID,
                "Gemini recorded interaction fixture record is invalid",
            ) from None
        expected_hash = _hashlib.sha256(recorded_request_bytes).hexdigest()
        if (
            not isinstance(recorded_request, dict)
            or recorded_hash != expected_hash
            or request_bytes != recorded_request_bytes
        ):
            raise _failure(
                _FailureCode.HASH_MISMATCH,
                "Gemini recorded interaction request does not match",
            ) from None
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
