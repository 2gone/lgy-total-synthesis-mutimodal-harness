"""Aggregated CLI composition acceptance tests for Task 11."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPOSITORY_ROOT / "tests" / "fixtures" / "paper_packages" / "alchivemycin_A"
PROMPT_DIR = REPOSITORY_ROOT / "prompts" / "article_router" / "v1"
WORKSPACE_NAME = ".tsdaf-workspace"
PRODUCER_EXECUTION_ID = "exec_11111111111111111111111111111111"
RETRY_EXECUTION_ID = "exec_22222222222222222222222222222222"
CRASH_EXECUTION_ID = "exec_33333333333333333333333333333333"
SECRET_SENTINEL = "task11-secret-sentinel-must-not-appear"

EXPECTED_PACKAGE_SHA256 = "cd8704c9f7c2b7aafaadf7c955011c79fbe5afac5a94fa7ebc6e30659811578e"
EXPECTED_ASSET_SHA256S = (
    "a3057bc392aca32054932b4677850e965f10627995a3b5e0df54f254cd158f17",
    "9335d15e09fc7d4889f316c95ba1db3a7418d230af8ddc6e3c307e58b4f9a45b",
    "4d64043f869c5417aa84c02b9634b0ec859ce4af4588dce9f431b9682beb58b6",
)
EXPECTED_FORWARD_IDS = (
    "MAIN-P0004-A0000",
    "MAIN-P0005-A0000",
    "MAIN-P0006-A0000",
)
EXPECTED_PROMPT_SHA256 = "804ef4b26e3c0a43169ce243d67ce8a1b6c2ba88d52350526ed08b20f81d4243"
EXPECTED_SCHEMA_SHA256 = "dcd7fe3ad5b16955c33d6bc20ed272dce211fdc03d7659b2ea2137390743a155"
EXPECTED_MODEL_PROFILE = "gemini-3.7-flash-high-chat-completions-v1"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _run_cli(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tsdaf", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _compact_payload(text: str) -> dict[str, Any]:
    assert text.endswith("\n")
    assert text.count("\n") == 1
    value = json.loads(text)
    assert isinstance(value, dict)
    assert text == _canonical_json_bytes(value).decode("utf-8") + "\n"
    return value


def _initial_context(request: dict[str, object]) -> dict[str, Any]:
    history = request["input"]
    assert isinstance(history, list) and history
    initial = history[0]
    assert initial["type"] == "user_input"
    content = initial["content"]
    assert isinstance(content, list) and len(content) == 1
    assert content[0]["type"] == "text"
    context = json.loads(content[0]["text"])
    assert isinstance(context, dict)
    return context


def _routing_payload(context: dict[str, Any]) -> dict[str, object]:
    assert context["paper_id"] == "alchivemycin_A"
    assert context["canonical_package_sha256"] == EXPECTED_PACKAGE_SHA256
    visual_groups = {
        visual["visual_id"]: visual["visual_group_id"] for visual in context["visual_assets"]
    }
    decisions = (
        ("MAIN-P0001-A0000", "OTHER"),
        ("MAIN-P0003-A0000", "RETROSYNTHESIS"),
        ("MAIN-P0004-A0000", "FORWARD_ROUTE"),
        ("MAIN-P0005-A0000", "FORWARD_ROUTE"),
        ("MAIN-P0006-A0000", "FORWARD_ROUTE"),
    )
    assert tuple(visual_groups) == tuple(visual_id for visual_id, _ in decisions)
    return {
        "schema_version": "tsdaf.article-routing-result.v1",
        "paper_id": "alchivemycin_A",
        "execution_id": context["execution_id"],
        "canonical_package_sha256": EXPECTED_PACKAGE_SHA256,
        "article_type": "TOTAL_SYNTHESIS",
        "disposition": "ROUTED",
        "route_groups": [
            {
                "route_group_id": "RG001",
                "target_names": ["alchivemycin A"],
                "synthesis_type": "TOTAL_SYNTHESIS",
                "attributes": ["CHEMOENZYMATIC"],
                "ordered_visual_ids": list(EXPECTED_FORWARD_IDS),
                "core_text_block_ids": [
                    "MAIN-P0002-B0009",
                    "MAIN-P0002-B0010",
                    "MAIN-P0006-B0002",
                ],
            }
        ],
        "visual_decisions": [
            {
                "visual_id": visual_id,
                "visual_group_id": visual_groups[visual_id],
                "initial_decision": decision,
                "triage_result": None,
                "final_decision": decision,
            }
            for visual_id, decision in decisions
        ],
        "coarse_text_selections": [
            {
                "route_group_id": "RG001",
                "block_id": block_id,
                "role": "ROUTE_CORE",
            }
            for block_id in (
                "MAIN-P0002-B0009",
                "MAIN-P0002-B0010",
                "MAIN-P0006-B0002",
            )
        ],
        "provenance": context["expected_provenance"],
    }


def _adapter_request(request: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {
        "input": request["input"],
        "model": "gemini-3.7-flash-high",
        "store": False,
        "stream": False,
        "background": False,
        "generation_config": {"thinking_level": "high"},
    }
    for name in ("system_instruction", "tools", "response_format"):
        value = request.get(name)
        if value is not None:
            payload[name] = value
    return payload


@dataclass(frozen=True, slots=True)
class _InteractionEvidence:
    request_bytes: bytes
    response_bytes: bytes
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


class _FakeSemanticProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.interactions: list[_InteractionEvidence] = []

    def create(self, **request: object) -> _InteractionEvidence:
        self.calls += 1
        request_dict = dict(request)
        context = _initial_context(request_dict)
        response: dict[str, object] = {
            "id": "interaction-task11-001",
            "status": "completed",
            "steps": [
                {
                    "type": "function_call",
                    "name": "submit_article_routing",
                    "call_id": "call-task11-001",
                    "arguments": _routing_payload(context),
                }
            ],
            "usage": {
                "total_input_tokens": 101,
                "total_output_tokens": 23,
                "total_tokens": 124,
                "total_cached_tokens": 7,
                "total_thought_tokens": 5,
                "total_tool_use_tokens": 3,
            },
            "request_id": "request-task11-001",
            "opaque_provider_payload": SECRET_SENTINEL,
        }
        interaction = _InteractionEvidence(
            request_bytes=_canonical_json_bytes(_adapter_request(request_dict)),
            response_bytes=_canonical_json_bytes(response),
            interaction_id="interaction-task11-001",
            output_text=None,
            total_input_tokens=101,
            total_output_tokens=23,
            total_tokens=124,
            total_cached_tokens=7,
            total_thought_tokens=5,
            total_tool_use_tokens=3,
            request_id="request-task11-001",
            elapsed_ms=12.5,
            transport_attempts=1,
        )
        self.interactions.append(interaction)
        return interaction


class _RetryableProvider:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **request: object) -> object:
        del request
        from tsdaf.contracts.execution import FailureCode, TypedFailure

        self.calls += 1
        failure = TypedFailure(
            schema_version="tsdaf.typed-failure.v1",
            code=FailureCode.PROVIDER_TRANSIENT,
            message="The fake provider is temporarily unavailable.",
            request_id=None,
            details=(),
        )
        error = RuntimeError(SECRET_SENTINEL)
        error.failure = failure  # type: ignore[attr-defined]
        raise error


class _NeverProvider:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **request: object) -> object:
        del request
        self.calls += 1
        raise AssertionError("offline resume invoked the provider")


def _seed_success(
    workspace: Path, execution_id: str
) -> tuple[_FakeSemanticProvider, dict[str, Any]]:
    from tsdaf.apps.composition import execute_route_workflow

    provider = _FakeSemanticProvider()
    code, payload = execute_route_workflow(
        package_dir=PACKAGE_DIR,
        workspace=workspace,
        execution_id=execution_id,
        interaction_create=provider.create,
        provider_is_live=False,
        live=False,
    )
    assert code == 0
    assert payload["execution_id"] == execution_id
    assert payload["status"] == "SUCCEEDED"
    assert payload["stage_status"] == "SUCCEEDED"
    assert payload["cache_hit"] is False
    assert provider.calls == 1
    return provider, payload


def _event_rows(workspace: Path, execution_id: str) -> list[dict[str, Any]]:
    path = workspace / "executions" / execution_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _artifact_ref(value: object) -> Any:
    from tsdaf.contracts.source import ArtifactRef

    return ArtifactRef.model_validate(value)


def _nested_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            keys.append(str(key))
            keys.extend(_nested_keys(child))
    elif isinstance(value, list | tuple):
        for child in value:
            keys.extend(_nested_keys(child))
    return keys


def _terminal_evidence_snapshot(execution_root: Path) -> dict[str, bytes]:
    names = (
        "execution.json",
        "execution.json.seal.json",
        "events.jsonl",
        "events.jsonl.seal.json",
        "manifest.json",
        "manifest.json.seal.json",
    )
    return {name: (execution_root / name).read_bytes() for name in names}


def _retry_evidence_snapshot(execution_root: Path) -> dict[str, bytes]:
    selected = (
        path
        for path in execution_root.rglob("*")
        if path.is_file()
        and (path.name in {"execution.json", "events.jsonl"} or "attempts" in path.parts)
    )
    return {
        path.relative_to(execution_root).as_posix(): path.read_bytes() for path in sorted(selected)
    }


def _sdk_module_names() -> set[str]:
    return {name for name in sys.modules if name == "openai" or name.startswith("openai.")}


def _forbid_live_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    from tsdaf.adapters.gemini.interactions import GeminiInteractions

    def fail_from_api_key(cls: type[object], *args: object, **kwargs: object) -> object:
        del cls, args, kwargs
        raise AssertionError("missing-key guard constructed the live SDK adapter")

    monkeypatch.setattr(GeminiInteractions, "from_api_key", classmethod(fail_from_api_key))


def test_live_provider_uses_only_fixed_openai_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: the CLI accepts legacy variables or lets them select a live model.
    from tsdaf.adapters.gemini.interactions import GeminiInteractions
    from tsdaf.apps.commands import route as route_commands

    calls: list[dict[str, object]] = []

    class _Adapter:
        def create(self, **request: object) -> object:
            del request
            raise AssertionError("the provider factory must not make a live request")

    def capture_factory(cls: type[object], **kwargs: object) -> _Adapter:
        del cls
        calls.append(kwargs)
        return _Adapter()

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "gemini-legacy-must-not-select-live")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-task12-openai-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.example.invalid/v1")
    monkeypatch.setattr(GeminiInteractions, "from_api_key", classmethod(capture_factory))

    interaction_create, profile = route_commands._provider(True)

    assert callable(interaction_create)
    assert profile is None
    assert calls == [
        {
            "api_key": "sk-task12-openai-secret",
            "base_url": "https://provider.example.invalid/v1",
        }
    ]


@pytest.mark.parametrize(
    "base_url",
    (
        "http://127.0.0.1:8317/v1",
        "http://localhost:8317/v1",
        "http://[::1]:8317/v1",
    ),
)
def test_live_provider_allows_loopback_http_base_urls(
    base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break caught: explicit live routing rejects a local OpenAI-compatible endpoint.
    from tsdaf.adapters.gemini.interactions import GeminiInteractions
    from tsdaf.apps.commands import route as route_commands

    calls: list[dict[str, object]] = []

    class _Adapter:
        def create(self, **request: object) -> object:
            del request
            raise AssertionError("the provider factory must not make a live request")

    def capture_factory(cls: type[object], **kwargs: object) -> _Adapter:
        del cls
        calls.append(kwargs)
        return _Adapter()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-task12-loopback-test")
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setattr(GeminiInteractions, "from_api_key", classmethod(capture_factory))

    interaction_create, profile = route_commands._provider(True)

    assert callable(interaction_create)
    assert profile is None
    assert calls == [{"api_key": "sk-task12-loopback-test", "base_url": base_url}]


def test_validation_and_live_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Break caught: validation drifts or either live guard mutates state/loads the SDK.
    workspace = tmp_path / WORKSPACE_NAME
    validated = _run_cli(["package", "validate", str(PACKAGE_DIR)], tmp_path)

    assert validated.returncode == 0
    assert validated.stderr == ""
    assert _compact_payload(validated.stdout) == {
        "block_count": 176,
        "canonical_package_sha256": EXPECTED_PACKAGE_SHA256,
        "paper_id": "alchivemycin_A",
        "source_schema_version": "tsdaf.paper-package.v1",
        "status": "VALID",
        "total_source_asset_bytes": 804_509,
        "visual_count": 5,
    }
    assert not workspace.exists()

    offline = _run_cli(["route", "run", str(PACKAGE_DIR)], tmp_path)

    assert offline.returncode == 3
    assert offline.stderr == ""
    offline_payload = _compact_payload(offline.stdout)
    assert offline_payload["code"] == "LIVE_REQUIRED"
    assert "traceback" not in offline.stdout.casefold()
    assert str(PACKAGE_DIR) not in offline.stdout
    assert not workspace.exists()

    from tsdaf.apps import cli as cli_app
    from tsdaf.apps.commands import route as route_commands

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET_SENTINEL)
    monkeypatch.setenv("GEMINI_MODEL", SECRET_SENTINEL)
    _forbid_live_adapter(monkeypatch)
    sdk_before = _sdk_module_names()

    exit_code = cli_app.main(["route", "run", str(PACKAGE_DIR), "--live"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    _compact_payload(captured.err)
    assert SECRET_SENTINEL not in captured.err
    assert "traceback" not in captured.err.casefold()
    assert _sdk_module_names() == sdk_before
    assert not workspace.exists()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-task12-config-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://unsafe.example.invalid/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "old-variable-must-not-select-live")
    unsafe_exit_code = cli_app.main(["route", "run", str(PACKAGE_DIR), "--live"])
    unsafe_captured = capsys.readouterr()

    assert unsafe_exit_code == 2
    assert unsafe_captured.out == ""
    _compact_payload(unsafe_captured.err)
    assert "unsafe.example.invalid" not in unsafe_captured.err
    assert "old-variable-must-not-select-live" not in unsafe_captured.err
    assert not workspace.exists()

    provider_paths = 0

    def forbidden_provider(live: bool) -> tuple[object, None]:
        nonlocal provider_paths
        del live
        provider_paths += 1
        return _NeverProvider().create, None

    def invalid_command(arguments: list[str]) -> tuple[int | None, str | None, str, str]:
        try:
            code = cli_app.main(arguments)
            error_type = None
        except Exception as error:
            code = None
            error_type = type(error).__name__
        output = capsys.readouterr()
        return code, error_type, output.out, output.err

    monkeypatch.setattr(route_commands, "_provider", forbidden_provider)
    invalid_id = f"../{SECRET_SENTINEL}"
    resume_outcome = invalid_command(["route", "resume", invalid_id, "--live"])
    inspect_outcome = invalid_command(["run", "inspect", invalid_id])
    expected_failure = {
        "status": "ERROR",
        "code": "PACKAGE_INVALID",
        "message": "The execution identifier is invalid.",
    }

    assert resume_outcome[:2] == (1, None)
    assert resume_outcome[2] == ""
    assert _compact_payload(resume_outcome[3]) == expected_failure
    assert inspect_outcome[:2] == (1, None)
    assert inspect_outcome[2] == ""
    assert _compact_payload(inspect_outcome[3]) == expected_failure
    assert provider_paths == 0
    assert SECRET_SENTINEL not in resume_outcome[3] + inspect_outcome[3]
    assert "traceback" not in (resume_outcome[3] + inspect_outcome[3]).casefold()
    assert _sdk_module_names() == sdk_before
    assert not workspace.exists()


def test_cached_offline_cli_vertical_path(tmp_path: Path) -> None:
    # Break caught: offline cache reuse omits exact immutable handoffs or mutates terminal resume.
    from datetime import UTC, datetime

    from tsdaf.apps.composition import execute_route_workflow, inspect_execution
    from tsdaf.contracts.execution import ExecutionStatus, StageResult
    from tsdaf.contracts.handoff import CoarseTextHandoff, VisualRoutingHandoff
    from tsdaf.contracts.routing import ArticleRoutingResult
    from tsdaf.contracts.schema import canonical_json_bytes
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing import build_proposed_handoffs
    from tsdaf.workflow.article_router_runtime import run_article_router_stage
    from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner

    workspace = tmp_path / WORKSPACE_NAME
    producer, producer_payload = _seed_success(workspace, PRODUCER_EXECUTION_ID)
    assert producer.calls == 1

    cached = _run_cli(["route", "run", str(PACKAGE_DIR)], tmp_path)

    assert cached.returncode == 0
    assert cached.stderr == ""
    payload = _compact_payload(cached.stdout)
    execution_id = payload["execution_id"]
    assert isinstance(execution_id, str)
    assert re.fullmatch(r"exec_[0-9a-f]{32}", execution_id)
    assert execution_id != PRODUCER_EXECUTION_ID
    assert payload["status"] == "SUCCEEDED"
    assert payload["stage_status"] == "CACHED"
    assert payload["cache_hit"] is True
    assert payload["stage_name"] == "main-article-router"
    assert payload["stage_version"] == "1.0.0"
    assert isinstance(payload["content_key"], str) and len(payload["content_key"]) == 64
    assert payload["visual_item_count"] == 3
    assert payload["coarse_text_item_count"] == 3

    store = LocalObjectStore(workspace)
    routing_ref = _artifact_ref(payload["routing_result_artifact"])
    visual_ref = _artifact_ref(payload["visual_handoff_artifact"])
    coarse_ref = _artifact_ref(payload["coarse_text_handoff_artifact"])
    assert visual_ref.media_type == "application/vnd.tsdaf.visual-routing-handoff+json"
    assert coarse_ref.media_type == "application/vnd.tsdaf.coarse-text-handoff+json"
    for artifact in (routing_ref, visual_ref, coarse_ref):
        store.verify(artifact)

    package = canonicalize_paper_package(PACKAGE_DIR)
    routing_result = ArticleRoutingResult.model_validate_json(store.read_bytes(routing_ref))
    routing_result.validate_against(package)
    expected_visual, expected_coarse = build_proposed_handoffs(
        package=package,
        routing_result=routing_result,
    )
    visual_bytes = store.read_bytes(visual_ref)
    coarse_bytes = store.read_bytes(coarse_ref)
    assert visual_bytes == canonical_json_bytes(
        expected_visual.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    assert coarse_bytes == canonical_json_bytes(
        expected_coarse.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    visual_handoff = VisualRoutingHandoff.model_validate_json(visual_bytes)
    coarse_handoff = CoarseTextHandoff.model_validate_json(coarse_bytes)
    assert tuple(item.source_asset.sha256 for item in visual_handoff.items) == (
        EXPECTED_ASSET_SHA256S
    )
    assert (
        tuple(
            decision.visual_id
            for decision in routing_result.visual_decisions
            if decision.final_decision == "FORWARD_ROUTE"
        )
        == EXPECTED_FORWARD_IDS
    )
    assert len(coarse_handoff.items) == 3
    forbidden = ("crop", "panel", "smiles", "reaction")
    assert not {
        key
        for key in _nested_keys(visual_handoff.model_dump(mode="json", exclude_none=False))
        if any(token in key.casefold() for token in forbidden)
    }

    events = _event_rows(workspace, execution_id)
    assert [event["event_type"] for event in events] == [
        "stage_cached",
        "handoffs_materialized",
        "execution_finished",
    ]
    handoff_events = [event for event in events if event["event_type"] == "handoffs_materialized"]
    assert len(handoff_events) == 1
    handoff_event = handoff_events[0]
    assert handoff_event["stage_name"] == "main-article-router"
    assert handoff_event["content_key"] == payload["content_key"]
    assert handoff_event["data"] == {
        "routing_result_artifact": routing_ref.model_dump(mode="json"),
        "visual_handoff_artifact": visual_ref.model_dump(mode="json"),
        "coarse_text_handoff_artifact": coarse_ref.model_dump(mode="json"),
        "visual_item_count": 3,
        "coarse_text_item_count": 3,
    }

    execution_root = workspace / "executions" / execution_id
    before_inspect = _terminal_evidence_snapshot(execution_root)
    inspected = _run_cli(["run", "inspect", execution_id], tmp_path)
    assert inspected.returncode == 0
    assert inspected.stderr == ""
    inspected_payload = _compact_payload(inspected.stdout)
    assert inspected_payload["execution_id"] == execution_id
    assert inspected_payload["status"] == "SUCCEEDED"
    assert _terminal_evidence_snapshot(execution_root) == before_inspect

    producer_interaction = next(
        event
        for event in _event_rows(workspace, PRODUCER_EXECUTION_ID)
        if event["event_type"] == "article_router_interaction"
    )
    producer_only_interaction_ref = _artifact_ref(producer_interaction["data"]["request_artifact"])
    scope_runner = LocalRunner(workspace, store)

    def bind_producer_only_artifact(attempt_id: str, content_key: str) -> StageResult:
        started_at = datetime.now(UTC)
        return StageResult(
            schema_version="tsdaf.stage-result.v1",
            execution_id="exec_44444444444444444444444444444444",
            attempt_id=attempt_id,
            stage_name="producer-only-evidence",
            stage_version="1.0.0",
            input_content_key=content_key,
            status=ExecutionStatus.SUCCEEDED,
            output_artifact=producer_only_interaction_ref,
            failure=None,
            cached_from_result=None,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    scope_result = scope_runner.run_stage(
        execution_id="exec_44444444444444444444444444444444",
        stage_name="producer-only-evidence",
        stage_version="1.0.0",
        input_artifacts=(producer_only_interaction_ref,),
        settings={"scope": "producer-only"},
        invoke=bind_producer_only_artifact,
    )
    assert isinstance(scope_result, StageResult)
    reinspect_code, reinspected = inspect_execution(
        workspace=workspace,
        execution_id=execution_id,
    )
    assert reinspect_code == 0
    consumer_refs = {_artifact_ref(value) for value in reinspected["artifact_refs"]}
    consumer_bound_values = [
        reinspected["stage_outputs"][0]["result_artifact"],
        reinspected["stage_outputs"][0]["output_artifact"],
        reinspected["stage_outputs"][0]["cached_from_result"],
        reinspected["handoffs"]["routing_result_artifact"],
        reinspected["handoffs"]["visual_handoff_artifact"],
        reinspected["handoffs"]["coarse_text_handoff_artifact"],
    ]
    required_consumer_refs = {_artifact_ref(value) for value in consumer_bound_values}
    for artifact in consumer_refs:
        store.verify(artifact)
    artifact_scope_is_exact = (
        producer_only_interaction_ref not in consumer_refs
        and required_consumer_refs <= consumer_refs
    )

    before_resume = _terminal_evidence_snapshot(execution_root)
    resumed = _run_cli(["route", "resume", execution_id], tmp_path)
    assert resumed.returncode == 0
    assert resumed.stderr == ""
    resumed_payload = _compact_payload(resumed.stdout)
    assert resumed_payload["execution_id"] == execution_id
    assert resumed_payload["status"] == "SUCCEEDED"
    assert resumed_payload["resume"] == "ALREADY_TERMINAL"
    assert _terminal_evidence_snapshot(execution_root) == before_resume

    crash_provider = _NeverProvider()
    crash_stage = run_article_router_stage(
        workspace=workspace,
        package=package,
        execution_id=CRASH_EXECUTION_ID,
        prompt_dir=PROMPT_DIR,
        interaction_create=crash_provider.create,
        provider_is_live=True,
        live=False,
        package_dir=PACKAGE_DIR,
        finish_execution=False,
    )
    assert isinstance(crash_stage, StageResult)
    assert crash_stage.status is ExecutionStatus.CACHED
    crash_event_data = {
        "routing_result_artifact": producer_payload["routing_result_artifact"],
        "visual_handoff_artifact": producer_payload["visual_handoff_artifact"],
        "coarse_text_handoff_artifact": producer_payload["coarse_text_handoff_artifact"],
        "visual_item_count": 3,
        "coarse_text_item_count": 3,
    }
    scope_runner.append_event(
        CRASH_EXECUTION_ID,
        "handoffs_materialized",
        crash_event_data,
        attempt_id=crash_stage.attempt_id,
        stage_name="wrong-stage-binding",
        content_key=crash_stage.input_content_key,
    )
    crash_code, crash_payload = execute_route_workflow(
        package_dir=PACKAGE_DIR,
        workspace=workspace,
        execution_id=CRASH_EXECUTION_ID,
        interaction_create=crash_provider.create,
        provider_is_live=True,
        live=False,
    )
    crash_root = workspace / "executions" / CRASH_EXECUTION_ID
    crash_handoff_count = sum(
        event["event_type"] == "handoffs_materialized"
        for event in _event_rows(workspace, CRASH_EXECUTION_ID)
    )

    assert (
        artifact_scope_is_exact,
        crash_code,
        crash_payload.get("code"),
        crash_handoff_count,
        (crash_root / "manifest.json").exists(),
        crash_provider.calls,
    ) == (True, 1, "HASH_MISMATCH", 1, False, 0)


def test_provider_observability(tmp_path: Path) -> None:
    # Break caught: inspect invents metrics or leaks provider request/response evidence.
    from tsdaf.apps.composition import inspect_execution
    from tsdaf.workflow.local_runtime import LocalObjectStore

    workspace = tmp_path / WORKSPACE_NAME
    provider, execution_payload = _seed_success(workspace, PRODUCER_EXECUTION_ID)
    assert provider.calls == 1
    assert len(provider.interactions) == 1

    code, payload = inspect_execution(workspace=workspace, execution_id=PRODUCER_EXECUTION_ID)

    assert code == 0
    assert payload["execution_id"] == PRODUCER_EXECUTION_ID
    assert payload["status"] == "SUCCEEDED"
    assert len(payload["stage_outputs"]) == 1
    assert len(payload["attempts"]) == 1
    assert payload["handoffs"] == {
        "routing_result_artifact": execution_payload["routing_result_artifact"],
        "visual_handoff_artifact": execution_payload["visual_handoff_artifact"],
        "coarse_text_handoff_artifact": execution_payload["coarse_text_handoff_artifact"],
        "visual_item_count": 3,
        "coarse_text_item_count": 3,
    }
    assert payload["observability"] == {
        "cache_hit": False,
        "model_profile": EXPECTED_MODEL_PROFILE,
        "prompt_sha256": EXPECTED_PROMPT_SHA256,
        "schema_sha256": EXPECTED_SCHEMA_SHA256,
        "router_turn_count": 1,
        "visual_triage_batch_count": 0,
        "function_call_count": 1,
        "total_input_tokens": 101,
        "total_output_tokens": 23,
        "total_tokens": 124,
        "total_cached_tokens": 7,
        "total_thought_tokens": 5,
        "total_tool_use_tokens": 3,
        "total_latency_ms": 12.5,
        "visual_tokens": None,
        "cost_estimate": {
            "amount_usd": None,
            "currency": "USD",
            "pricing_snapshot_version": "unconfigured",
        },
        "errors": [],
    }

    interaction_events = [
        event for event in payload["events"] if event["event_type"] == "article_router_interaction"
    ]
    assert len(interaction_events) == 1
    evidence = interaction_events[0]["data"]
    request_ref = _artifact_ref(evidence["request_artifact"])
    response_ref = _artifact_ref(evidence["response_artifact"])
    store = LocalObjectStore(workspace)
    store.verify(request_ref)
    store.verify(response_ref)
    assert store.read_bytes(request_ref) == provider.interactions[0].request_bytes
    assert store.read_bytes(response_ref) == provider.interactions[0].response_bytes
    artifact_refs = {_artifact_ref(value) for value in payload["artifact_refs"]}
    assert request_ref in artifact_refs
    assert response_ref in artifact_refs
    assert _artifact_ref(execution_payload["routing_result_artifact"]) in artifact_refs
    assert _artifact_ref(execution_payload["visual_handoff_artifact"]) in artifact_refs
    assert _artifact_ref(execution_payload["coarse_text_handoff_artifact"]) in artifact_refs

    serialized = _canonical_json_bytes(payload)
    assert SECRET_SENTINEL.encode() in provider.interactions[0].response_bytes
    assert SECRET_SENTINEL.encode() not in serialized
    assert "request_bytes" not in _nested_keys(payload)
    assert "response_bytes" not in _nested_keys(payload)


def test_retryable_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Break caught: retry resumes bypass live/config guards or alter sealed attempt evidence.
    from tsdaf.apps.cli import main
    from tsdaf.apps.composition import execute_route_workflow, resume_route_workflow

    workspace = tmp_path / WORKSPACE_NAME
    retryable = _RetryableProvider()
    code, failed = execute_route_workflow(
        package_dir=PACKAGE_DIR,
        workspace=workspace,
        execution_id=RETRY_EXECUTION_ID,
        interaction_create=retryable.create,
        provider_is_live=True,
        live=True,
    )

    assert code == 1
    assert failed["execution_id"] == RETRY_EXECUTION_ID
    assert failed["status"] == "FAILED_RETRYABLE"
    assert failed["code"] == "PROVIDER_TRANSIENT"
    assert SECRET_SENTINEL not in _canonical_json_bytes(failed).decode("utf-8")
    assert retryable.calls == 1
    execution_root = workspace / "executions" / RETRY_EXECUTION_ID
    attempt_roots = tuple((execution_root / "attempts").iterdir())
    assert len(attempt_roots) == 1
    before_offline = _retry_evidence_snapshot(execution_root)

    never = _NeverProvider()
    resume_code, offline = resume_route_workflow(
        workspace=workspace,
        execution_id=RETRY_EXECUTION_ID,
        interaction_create=never.create,
        provider_is_live=True,
        live=False,
    )

    assert resume_code == 3
    assert offline["execution_id"] == RETRY_EXECUTION_ID
    assert offline["code"] == "LIVE_REQUIRED"
    assert never.calls == 0
    assert len(tuple((execution_root / "attempts").iterdir())) == 1
    assert _retry_evidence_snapshot(execution_root) == before_offline

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET_SENTINEL)
    monkeypatch.setenv("GEMINI_MODEL", SECRET_SENTINEL)
    _forbid_live_adapter(monkeypatch)
    before_missing_key = _retry_evidence_snapshot(execution_root)

    exit_code = main(["route", "resume", RETRY_EXECUTION_ID, "--live"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    _compact_payload(captured.err)
    assert SECRET_SENTINEL not in captured.err
    assert "traceback" not in captured.err.casefold()
    assert len(tuple((execution_root / "attempts").iterdir())) == 1
    assert _retry_evidence_snapshot(execution_root) == before_missing_key
