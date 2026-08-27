"""Direct OpenAI-compatible transport contract tests for immutable interactions evidence."""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import FrozenInstanceError, fields, is_dataclass
from pathlib import Path
from types import ModuleType

import pytest

from tsdaf.contracts.execution import FailureCode, TypedFailure
from tsdaf.contracts.schema import canonical_json_bytes

FIXTURE_PATH = (
    Path(__file__).parents[2] / "fixtures" / "providers" / "gemini" / "task7_interactions.json"
)
RAW_SECRET = "sk-task7-raw-secret"

STATELESS_INPUT: list[dict[str, object]] = [
    {
        "type": "user_input",
        "content": [{"type": "text", "text": "Classify the article."}],
    }
]
SYSTEM_INSTRUCTION = "Return only a submit_routing tool result."
TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "name": "submit_routing",
        "description": "Submit the routing result.",
        "parameters": {
            "type": "object",
            "properties": {"article_type": {"type": "string"}},
            "required": ["article_type"],
            "additionalProperties": False,
        },
    }
]
RESPONSE_FORMAT: dict[str, object] = {
    "type": "text",
    "mime_type": "application/json",
    "schema": {
        "type": "object",
        "properties": {"article_type": {"type": "string"}},
        "required": ["article_type"],
        "additionalProperties": False,
    },
}

EXPECTED_REQUEST: dict[str, object] = {
    "input": STATELESS_INPUT,
    "model": "gemini-3.7-flash-high",
    "store": False,
    "stream": False,
    "background": False,
    "generation_config": {"thinking_level": "high"},
    "system_instruction": SYSTEM_INSTRUCTION,
    "tools": TOOLS,
    "response_format": RESPONSE_FORMAT,
}

NORMALIZED_RESPONSE: dict[str, object] = {
    "id": "interaction-task7-001",
    "status": "completed",
    "steps": [
        {
            "type": "model_output",
            "content": [
                {
                    "type": "text",
                    "text": '{"article_type":"research_article"}',
                }
            ],
        }
    ],
    "output_text": '{"article_type":"research_article"}',
    "usage": {
        "total_input_tokens": 101,
        "total_output_tokens": 29,
        "total_tokens": 130,
        "total_cached_tokens": 17,
        "total_thought_tokens": 11,
        "total_tool_use_tokens": 7,
    },
}


def _gemini_module() -> ModuleType:
    try:
        return importlib.import_module("tsdaf.adapters.gemini.interactions")
    except ModuleNotFoundError as exc:
        if exc.name in {
            "tsdaf.adapters.gemini",
            "tsdaf.adapters.gemini.interactions",
        }:
            pytest.fail(
                "Task 7 adapter module is missing: tsdaf.adapters.gemini.interactions",
                pytrace=False,
            )
        raise


class _FakeResponse:
    def __init__(self, payload: dict[str, object], *, request_id: str | None = None) -> None:
        self._payload = payload
        self.request_id = request_id
        self.raw_sdk_secret = RAW_SECRET
        self.dump_calls: list[dict[str, object]] = []

    def model_dump(
        self,
        *,
        mode: str,
        by_alias: bool,
        exclude_none: bool,
    ) -> dict[str, object]:
        self.dump_calls.append({"mode": mode, "by_alias": by_alias, "exclude_none": exclude_none})
        return self._payload


class _FakeHttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self.headers = headers


class _FakeHttpError(Exception):
    def __init__(
        self,
        status_code: int,
        *,
        raw_text: str,
        retry_after: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(raw_text)
        self.code = status_code
        headers: dict[str, str] = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        if request_id is not None:
            headers["x-request-id"] = request_id
        self.response = _FakeHttpResponse(status_code, headers)


class _ScriptedInteractions:
    def __init__(self, actions: list[object]) -> None:
        self._actions = list(actions)
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> object:
        self.requests.append(request)
        action = self._actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class _FakeClient:
    def __init__(self, actions: list[object]) -> None:
        self.interactions = _ScriptedInteractions(actions)


class _ScriptedChatCompletions:
    def __init__(self, response: object) -> None:
        self.actions = [response]
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> object:
        self.requests.append(request)
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class _FakeChatClient:
    def __init__(self, response: object) -> None:
        self.chat = type("_Chat", (), {"completions": _ScriptedChatCompletions(response)})()


class _FakeOpenAI:
    def __init__(self, client: _FakeChatClient) -> None:
        self.client = client
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _FakeChatClient:
        self.calls.append(kwargs)
        return self.client


class _FakeAPIConnectionError(Exception):
    pass


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def _assert_safe_failure(error: BaseException, *, absent: tuple[str, ...]) -> TypedFailure:
    failure = error.failure
    assert isinstance(failure, TypedFailure)
    rendered = "\n".join((str(error), repr(error), failure.model_dump_json()))
    for value in absent:
        assert value not in rendered
    assert str(error) == failure.message
    return failure


def test_live_create_uses_fixed_v2_request_and_returns_neutral_evidence() -> None:
    # Break caught: an adapter sends a legacy/stateful request or leaks an SDK response object.
    gemini = _gemini_module()
    response = _FakeResponse(NORMALIZED_RESPONSE, request_id="req-live-001")
    client = _FakeClient([response])
    adapter = gemini.GeminiInteractions(
        client,
        monotonic=_Clock(12.0, 12.125),
        sleep=lambda _: None,
    )

    interaction = adapter.create(
        input=STATELESS_INPUT,
        system_instruction=SYSTEM_INSTRUCTION,
        tools=TOOLS,
        response_format=RESPONSE_FORMAT,
    )

    assert client.interactions.requests == [EXPECTED_REQUEST]
    assert "previous_interaction_id" not in client.interactions.requests[0]
    assert response.dump_calls == [{"mode": "json", "by_alias": True, "exclude_none": True}]
    assert is_dataclass(interaction)
    assert hasattr(type(interaction), "__slots__")
    with pytest.raises(FrozenInstanceError):
        interaction.output_text = "mutated"
    assert interaction.request_bytes == canonical_json_bytes(EXPECTED_REQUEST)
    assert interaction.response_bytes == canonical_json_bytes(NORMALIZED_RESPONSE)
    assert interaction.interaction_id == "interaction-task7-001"
    assert interaction.output_text == '{"article_type":"research_article"}'
    assert interaction.total_input_tokens == 101
    assert interaction.total_output_tokens == 29
    assert interaction.total_tokens == 130
    assert interaction.total_cached_tokens == 17
    assert interaction.total_thought_tokens == 11
    assert interaction.total_tool_use_tokens == 7
    assert interaction.request_id == "req-live-001"
    assert interaction.elapsed_ms == pytest.approx(125.0)
    assert interaction.transport_attempts == 1
    assert all(
        not isinstance(getattr(interaction, field.name), _FakeClient | _FakeResponse)
        for field in fields(interaction)
    )
    assert RAW_SECRET not in repr(interaction)


def test_recorded_replay_matches_live_shape_then_exhausts_without_sdk_or_network(
    tmp_path: Path,
) -> None:
    # Break caught: replay accepts a response without the required 2.x envelope.
    gemini = _gemini_module()
    adapter = gemini.RecordedGeminiInteractions(FIXTURE_PATH)

    interaction = adapter.create(
        input=STATELESS_INPUT,
        system_instruction=SYSTEM_INSTRUCTION,
        tools=TOOLS,
        response_format=RESPONSE_FORMAT,
    )

    assert isinstance(interaction, gemini.GeminiInteraction)
    assert interaction.request_bytes == canonical_json_bytes(EXPECTED_REQUEST)
    assert interaction.response_bytes == canonical_json_bytes(NORMALIZED_RESPONSE)
    assert interaction.interaction_id == "interaction-task7-001"
    assert interaction.output_text == '{"article_type":"research_article"}'
    assert interaction.elapsed_ms == pytest.approx(37.5)
    assert interaction.transport_attempts == 1
    with pytest.raises(gemini.GeminiAdapterError) as caught:
        adapter.create(
            input=STATELESS_INPUT,
            system_instruction=SYSTEM_INSTRUCTION,
            tools=TOOLS,
            response_format=RESPONSE_FORMAT,
        )
    _assert_safe_failure(caught.value, absent=(RAW_SECRET, str(FIXTURE_PATH)))

    invalid_fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    invalid_fixture["interactions"][0]["response"] = {}
    invalid_fixture_path = tmp_path / "missing-response-envelope.json"
    invalid_fixture_path.write_text(
        json.dumps(invalid_fixture, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    invalid_adapter = gemini.RecordedGeminiInteractions(invalid_fixture_path)

    with pytest.raises(gemini.GeminiAdapterError) as invalid_caught:
        invalid_adapter.create(
            input=STATELESS_INPUT,
            system_instruction=SYSTEM_INSTRUCTION,
            tools=TOOLS,
            response_format=RESPONSE_FORMAT,
        )
    failure = _assert_safe_failure(
        invalid_caught.value,
        absent=(RAW_SECRET, str(FIXTURE_PATH), str(invalid_fixture_path)),
    )
    assert failure.code is FailureCode.PACKAGE_INVALID


def test_rate_limit_honors_numeric_retry_after_then_succeeds() -> None:
    # Break caught: HTTP 429 is not retried or sleeps for less than Retry-After.
    gemini = _gemini_module()
    response = _FakeResponse(NORMALIZED_RESPONSE)
    client = _FakeClient(
        [
            _FakeHttpError(
                429,
                raw_text=f"Authorization: Bearer {RAW_SECRET}",
                retry_after="1.75",
                request_id="req-rate-limit-001",
            ),
            response,
        ]
    )
    sleeps: list[float] = []
    adapter = gemini.GeminiInteractions(
        client,
        monotonic=lambda: 4.0,
        sleep=sleeps.append,
    )

    interaction = adapter.create(input=STATELESS_INPUT)

    assert len(client.interactions.requests) == 2
    assert len(sleeps) == 1
    assert 1.75 <= sleeps[0] <= 4.0
    assert interaction.transport_attempts == 2
    assert RAW_SECRET not in repr(interaction)


def test_transient_exhaustion_stops_after_four_total_attempts() -> None:
    # Break caught: retryable 5xx failures exceed the three-retry transport budget.
    gemini = _gemini_module()
    unsafe_request_id = f"bad\nAuthorization: Bearer {RAW_SECRET}"
    client = _FakeClient(
        [
            _FakeHttpError(
                503,
                raw_text=f"https://private.invalid body={RAW_SECRET}",
                request_id=unsafe_request_id,
            )
            for _ in range(4)
        ]
    )
    sleeps: list[float] = []
    adapter = gemini.GeminiInteractions(
        client,
        monotonic=lambda: 8.0,
        sleep=sleeps.append,
    )

    with pytest.raises(gemini.GeminiAdapterError) as caught:
        adapter.create(input=STATELESS_INPUT)

    assert len(client.interactions.requests) == 4
    assert len(sleeps) == 3
    assert 0.0 < sleeps[0] < sleeps[1] < sleeps[2] <= 4.0
    failure = _assert_safe_failure(
        caught.value,
        absent=(RAW_SECRET, "private.invalid", "Authorization", unsafe_request_id),
    )
    assert failure.code is FailureCode.PROVIDER_TRANSIENT
    assert failure.request_id is None


def test_provider_rejection_is_not_retried_and_preserves_only_safe_request_id() -> None:
    # Break caught: a terminal 4xx rejection is retried or raw provider text escapes the boundary.
    gemini = _gemini_module()
    client = _FakeClient(
        [
            _FakeHttpError(
                400,
                raw_text=f"request body={RAW_SECRET}",
                request_id="req-rejected-001",
            )
        ]
    )
    sleeps: list[float] = []
    adapter = gemini.GeminiInteractions(
        client,
        monotonic=lambda: 9.0,
        sleep=sleeps.append,
    )

    with pytest.raises(gemini.GeminiAdapterError) as caught:
        adapter.create(input=STATELESS_INPUT)

    assert len(client.interactions.requests) == 1
    assert sleeps == []
    failure = _assert_safe_failure(caught.value, absent=(RAW_SECRET, "request body"))
    assert failure.code is FailureCode.PROVIDER_REJECTED
    assert failure.request_id == "req-rejected-001"


def test_nonfixed_model_fails_before_provider_call_without_echoing_secret() -> None:
    # Break caught: callers can select a different model or see the rejected value in safe text.
    gemini = _gemini_module()
    client = _FakeClient([_FakeResponse(NORMALIZED_RESPONSE)])
    rejected_model = f"gemini-unfixed-{RAW_SECRET}"

    with pytest.raises(gemini.GeminiAdapterError) as caught:
        gemini.GeminiInteractions(client, model=rejected_model)

    assert client.interactions.requests == []
    failure = _assert_safe_failure(
        caught.value,
        absent=(RAW_SECRET, rejected_model, "Authorization", "api_key"),
    )
    assert failure.code is FailureCode.PROVIDER_REJECTED


def test_from_api_key_translates_chat_completions_history_and_normalizes_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: the fixed remote transport bypasses Chat Completions or leaks SDK evidence.
    gemini = _gemini_module()
    completion = _FakeResponse(
        {
            "id": "chatcmpl-task12-001",
            "model": "gemini-3.7-flash-high-provider-revision",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-result-001",
                                "type": "function",
                                "function": {
                                    "name": "submit_routing",
                                    "arguments": '{"article_type":"research_article"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 101,
                "completion_tokens": 29,
                "total_tokens": 130,
                "prompt_tokens_details": {"cached_tokens": 17},
                "completion_tokens_details": {"reasoning_tokens": 11},
            },
        },
        request_id="req-chat-001",
    )
    client = _FakeChatClient(completion)
    factory = _FakeOpenAI(client)
    monkeypatch.setitem(
        sys.modules,
        "openai",
        type(
            "_OpenAIModule",
            (),
            {"APIConnectionError": _FakeAPIConnectionError, "OpenAI": factory},
        )(),
    )

    adapter = gemini.GeminiInteractions.from_api_key(
        api_key=RAW_SECRET,
        base_url="https://provider.example.invalid/v1",
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )
    client.chat.completions.actions.insert(0, _FakeAPIConnectionError())
    first = adapter.create(input=STATELESS_INPUT, tools=TOOLS)
    first_response = json.loads(first.response_bytes)
    assert first.transport_attempts == 2
    assert first_response["steps"] == [
        {
            "type": "function_call",
            "name": "submit_routing",
            "call_id": "call-result-001",
            "arguments": {"article_type": "research_article"},
        }
    ]
    assert first_response["usage"]["total_tool_use_tokens"] is None
    assert first.total_tool_use_tokens is None
    completion._payload["choices"] = [
        {
            "message": {
                "content": '{"article_type":"research_article"}',
                "tool_calls": [
                    {
                        "id": "call-result-002",
                        "type": "function",
                        "function": {
                            "name": "submit_routing",
                            "arguments": '{"article_type":"research_article"}',
                        },
                    }
                ],
            }
        }
    ]
    client.chat.completions.actions.append(completion)
    interaction = adapter.create(
        input=[
            *STATELESS_INPUT,
            *first_response["steps"],
            {
                "type": "function_result",
                "call_id": "call-result-001",
                "name": "submit_routing",
                "result": [{"type": "text", "text": '{"accepted":true}'}],
            },
        ],
        system_instruction=SYSTEM_INSTRUCTION,
        tools=TOOLS,
        response_format=RESPONSE_FORMAT,
    )

    assert factory.calls == [
        {
            "api_key": RAW_SECRET,
            "base_url": "https://provider.example.invalid/v1",
            "max_retries": 0,
        }
    ]
    assert len(client.chat.completions.requests) == 3
    assert client.chat.completions.requests[-1] == {
        "model": "gemini-3.7-flash-high",
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": "Classify the article."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-result-001",
                        "type": "function",
                        "function": {
                            "name": "submit_routing",
                            "arguments": '{"article_type":"research_article"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-result-001",
                "content": '{"accepted":true}',
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {key: value for key, value in TOOLS[0].items() if key != "type"},
            }
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tsdaf_response",
                "strict": True,
                "schema": RESPONSE_FORMAT["schema"],
            },
        },
        "reasoning_effort": "high",
        "parallel_tool_calls": True,
        "store": False,
        "stream": False,
    }
    normalized = json.loads(interaction.response_bytes)
    assert normalized["provider_model"] == "gemini-3.7-flash-high-provider-revision"
    assert normalized["status"] == "completed"
    assert normalized["output_text"] == '{"article_type":"research_article"}'
    assert normalized["usage"]["total_cached_tokens"] == 17
    assert normalized["usage"]["total_thought_tokens"] == 11
    function_call = next(step for step in normalized["steps"] if step["type"] == "function_call")
    assert function_call["arguments"] == {"article_type": "research_article"}
    assert interaction.request_id == "req-chat-001"
    assert RAW_SECRET not in repr(interaction)


def test_chat_completions_preserves_image_bytes_and_uses_strict_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: image evidence is cropped or response JSON is not constrained at the wire.
    gemini = _gemini_module()
    response = _FakeResponse(
        {
            "id": "chatcmpl-task12-image-001",
            "model": "gemini-3.7-flash-high-provider-revision",
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    client = _FakeChatClient(response)
    factory = _FakeOpenAI(client)
    monkeypatch.setitem(
        sys.modules,
        "openai",
        type(
            "_OpenAIModule",
            (),
            {"APIConnectionError": _FakeAPIConnectionError, "OpenAI": factory},
        )(),
    )
    adapter = gemini.GeminiInteractions.from_api_key(
        api_key=RAW_SECRET,
        base_url="https://provider.example.invalid/v1",
    )

    adapter.create(
        input=[
            {
                "type": "user_input",
                "content": [
                    {"type": "text", "text": "Read this image."},
                    {
                        "type": "image",
                        "mime_type": "image/png",
                        "data": "AAEC/2Z1bGw=",
                        "resolution": "medium",
                    },
                ],
            }
        ],
        response_format=RESPONSE_FORMAT,
    )

    request = client.chat.completions.requests[0]
    assert request["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Read this image."},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,AAEC/2Z1bGw=",
                        "detail": "auto",
                    },
                },
            ],
        }
    ]
    assert request["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "tsdaf_response",
            "strict": True,
            "schema": RESPONSE_FORMAT["schema"],
        },
    }
    assert "parallel_tool_calls" not in request


def test_molecule_ocsr_profile_uses_temperature_zero_timeout_and_three_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: OCSR silently inherits the article-router reasoning or retry profile.
    gemini = _gemini_module()
    response = _FakeResponse(
        {
            "id": "chatcmpl-ocsr-001",
            "model": "gemini-3.7-flash-high",
            "choices": [{"message": {"content": '{"smiles":"CCO"}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    client = _FakeChatClient(response)
    factory = _FakeOpenAI(client)
    monkeypatch.setitem(
        sys.modules,
        "openai",
        type(
            "_OpenAIModule",
            (),
            {"APIConnectionError": _FakeAPIConnectionError, "OpenAI": factory},
        )(),
    )
    adapter = gemini.GeminiInteractions.for_molecule_ocsr(
        api_key=RAW_SECRET,
        base_url="https://provider.example.invalid/v1",
        timeout=120.0,
        retries=2,
    )

    interaction = adapter.create(input=STATELESS_INPUT)

    assert factory.calls == [
        {
            "api_key": RAW_SECRET,
            "base_url": "https://provider.example.invalid/v1",
            "max_retries": 0,
            "timeout": 120.0,
        }
    ]
    request = client.chat.completions.requests[0]
    assert request["temperature"] == 0
    assert "reasoning_effort" not in request
    assert interaction.transport_attempts == 1

    failing_client = _FakeChatClient(_FakeAPIConnectionError())
    failing_client.chat.completions.actions.extend(
        [_FakeAPIConnectionError(), _FakeAPIConnectionError()]
    )
    failing_factory = _FakeOpenAI(failing_client)
    monkeypatch.setitem(
        sys.modules,
        "openai",
        type(
            "_OpenAIModule",
            (),
            {"APIConnectionError": _FakeAPIConnectionError, "OpenAI": failing_factory},
        )(),
    )
    failing = gemini.GeminiInteractions.for_molecule_ocsr(
        api_key=RAW_SECRET,
        base_url="https://provider.example.invalid/v1",
        timeout=120.0,
        retries=2,
        sleep=lambda _: None,
    )

    with pytest.raises(gemini.GeminiAdapterError):
        failing.create(input=STATELESS_INPUT)

    assert len(failing_client.chat.completions.requests) == 3
