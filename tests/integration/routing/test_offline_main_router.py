"""Offline five-paper and local-runtime acceptance tests for Task 8."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PAPER_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "paper_packages"
SEMANTIC_FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "providers" / "fake" / "article_router_v1.json"
)
PROMPT_DIR = PROJECT_ROOT / "prompts" / "article_router" / "v1"
PAPER_IDS = (
    "alchivemycin_A",
    "hyperfirin",
    "ineleganolide",
    "kalmanol",
    "principinol_B",
)
EXPECTED_FORWARD_IDS = (
    "MAIN-P0004-A0000",
    "MAIN-P0005-A0000",
    "MAIN-P0006-A0000",
    "MAIN-P0001-A0001",
    "MAIN-P0002-A0000",
    "MAIN-P0001-A0000",
    "MAIN-P0001-A0001",
    "MAIN-P0002-A0000",
    "MAIN-P0001-A0000",
)
EXPECTED_FORWARD_ASSET_SHA256S = (
    "a3057bc392aca32054932b4677850e965f10627995a3b5e0df54f254cd158f17",
    "9335d15e09fc7d4889f316c95ba1db3a7418d230af8ddc6e3c307e58b4f9a45b",
    "4d64043f869c5417aa84c02b9634b0ec859ce4af4588dce9f431b9682beb58b6",
    "5983f3b72b704d3ad0f4d94c7f03287f2577dd0d356b22aba156293987468be0",
    "d706696f559b0075b106465a58ba6059d9085b0df5fead74b22b42751facb94a",
    "3d2eda8baea927ef6c5e8ffdde12a76a6653e3be28c15c27fb1bb15b96b61328",
    "f3b6874b62543babe547b945209cd1b7e78410f5468ddac64d44ba132e43bd6b",
    "0084dd0937d74c0401c6e243103bf47213015f9dbf9472b601f62554001cc007",
    "dc98f1c370c2b62e6c5ca9573ce00b65e709bb26cc6672f7e0ec126bb104f87c",
)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _semantic_rows() -> dict[str, dict[str, Any]]:
    payload = json.loads(SEMANTIC_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "tsdaf.fake-article-router.v1"
    papers = payload["papers"]
    assert isinstance(papers, list)
    rows = {paper["paper_id"]: paper for paper in papers}
    assert tuple(rows) == PAPER_IDS
    return rows


def _initial_context(request: dict[str, object]) -> dict[str, Any]:
    history = request["input"]
    assert isinstance(history, list) and history
    initial = history[0]
    assert initial["type"] == "user_input"
    content = initial["content"]
    assert isinstance(content, list) and content == [content[0]]
    assert content[0]["type"] == "text"
    context = json.loads(content[0]["text"])
    assert isinstance(context, dict)
    return context


def _routing_payload(context: dict[str, Any]) -> dict[str, object]:
    semantic = _semantic_rows()[context["paper_id"]]
    assert semantic["canonical_package_sha256"] == context["canonical_package_sha256"]
    visual_groups = {
        visual["visual_id"]: visual["visual_group_id"] for visual in context["visual_assets"]
    }
    decision_ids = [decision["visual_id"] for decision in semantic["visual_decisions"]]
    assert len(decision_ids) == len(set(decision_ids))
    assert decision_ids == list(visual_groups)
    assert all(decision["triage_result"] is None for decision in semantic["visual_decisions"])
    route_group_id = semantic["route_group_id"]
    return {
        "schema_version": "tsdaf.article-routing-result.v1",
        "paper_id": context["paper_id"],
        "execution_id": context["execution_id"],
        "canonical_package_sha256": context["canonical_package_sha256"],
        "article_type": semantic["article_type"],
        "disposition": semantic["disposition"],
        "route_groups": [
            {
                "route_group_id": route_group_id,
                "target_names": semantic["target_names"],
                "synthesis_type": semantic["article_type"],
                "attributes": semantic["attributes"],
                "ordered_visual_ids": semantic["ordered_visual_ids"],
                "core_text_block_ids": semantic["core_text_block_ids"],
            }
        ],
        "visual_decisions": [
            {
                **decision,
                "visual_group_id": visual_groups[decision["visual_id"]],
            }
            for decision in semantic["visual_decisions"]
        ],
        "coarse_text_selections": [
            {
                "route_group_id": route_group_id,
                "block_id": block_id,
                "role": "ROUTE_CORE",
            }
            for block_id in semantic["core_text_block_ids"]
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
        self.requests: list[dict[str, object]] = []
        self.interactions: list[_InteractionEvidence] = []

    def create(self, **request: object) -> _InteractionEvidence:
        request_dict = dict(request)
        self.requests.append(request_dict)
        turn = len(self.requests)
        context = _initial_context(request_dict)
        response: dict[str, object] = {
            "id": f"interaction-fake-{context['paper_id']}-{turn:03d}",
            "status": "completed",
            "steps": [
                {
                    "type": "function_call",
                    "name": "submit_article_routing",
                    "call_id": f"call-fake-{turn:03d}",
                    "arguments": _routing_payload(context),
                }
            ],
            "usage": {
                "total_input_tokens": 1000 + turn,
                "total_output_tokens": 200 + turn,
                "total_tokens": 1200 + 2 * turn,
                "total_cached_tokens": 0,
                "total_thought_tokens": 12,
                "total_tool_use_tokens": 18,
            },
            "request_id": f"req-fake-{context['paper_id']}-{turn:03d}",
        }
        usage = response["usage"]
        assert isinstance(usage, dict)
        interaction = _InteractionEvidence(
            request_bytes=_canonical_json_bytes(_adapter_request(request_dict)),
            response_bytes=_canonical_json_bytes(response),
            interaction_id=response["id"],
            output_text=None,
            total_input_tokens=usage["total_input_tokens"],
            total_output_tokens=usage["total_output_tokens"],
            total_tokens=usage["total_tokens"],
            total_cached_tokens=usage["total_cached_tokens"],
            total_thought_tokens=usage["total_thought_tokens"],
            total_tool_use_tokens=usage["total_tool_use_tokens"],
            request_id=response["request_id"],
            elapsed_ms=10.0 + turn,
            transport_attempts=1,
        )
        self.interactions.append(interaction)
        return interaction


def _event_rows(workspace: Path, execution_id: str) -> list[dict[str, Any]]:
    path = workspace / "executions" / execution_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_five_real_fixtures_route_offline_with_immutable_evidence_and_sqlite_projection(
    tmp_path: Path,
) -> None:
    # Break caught: the vertical slice drops a source block/visual or persists transcript text.
    from tsdaf.contracts.execution import ExecutionStatus, StageResult
    from tsdaf.contracts.routing import ArticleRoutingResult, ArticleType, VisualDecision
    from tsdaf.contracts.source import ArtifactRef
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.workflow.article_router_runtime import run_article_router_stage
    from tsdaf.workflow.local_runtime import LocalObjectStore

    workspace = tmp_path / "workspace"
    store = LocalObjectStore(workspace)
    all_forward_ids: list[str] = []
    all_forward_asset_hashes: list[str] = []
    total_source_blocks = 0
    total_decisions = 0
    result_artifact_hashes: set[str] = set()

    for paper_id in PAPER_IDS:
        package = canonicalize_paper_package(PAPER_FIXTURES / paper_id)
        source_snapshot = tuple(block.model_dump_json() for block in package.blocks)
        provider = _FakeSemanticProvider()
        execution_id = f"task8_{paper_id}"

        stage = run_article_router_stage(
            workspace=workspace,
            package=package,
            execution_id=execution_id,
            prompt_dir=PROMPT_DIR,
            interaction_create=provider.create,
            provider_is_live=False,
        )

        assert isinstance(stage, StageResult)
        assert stage.status is ExecutionStatus.SUCCEEDED
        assert stage.stage_name == "main-article-router"
        assert stage.stage_version == "1.0.0"
        assert stage.output_artifact is not None
        output_bytes = store.read_bytes(stage.output_artifact)
        result = ArticleRoutingResult.model_validate_json(output_bytes)
        assert output_bytes == _canonical_json_bytes(result.model_dump(mode="json"))
        result.validate_against(package)
        assert result.paper_id == paper_id
        assert result.execution_id == execution_id
        assert result.article_type is ArticleType.TOTAL_SYNTHESIS
        assert len(result.route_groups) == 1
        assert result.route_groups[0].route_group_id == "RG001"
        assert all(decision.triage_result is None for decision in result.visual_decisions)
        assert all(
            decision.initial_decision is not VisualDecision.NEEDS_VISUAL_VERIFICATION
            and decision.final_decision is not VisualDecision.UNCERTAIN
            for decision in result.visual_decisions
        )
        assert tuple(block.model_dump_json() for block in package.blocks) == source_snapshot

        forward_ids = tuple(
            decision.visual_id
            for decision in result.visual_decisions
            if decision.final_decision is VisualDecision.FORWARD_ROUTE
        )
        assert forward_ids == result.route_groups[0].ordered_visual_ids
        visuals_by_id = {visual.visual_id: visual for visual in package.visual_assets}
        all_forward_ids.extend(forward_ids)
        all_forward_asset_hashes.extend(visuals_by_id[item].asset.sha256 for item in forward_ids)
        total_source_blocks += len(package.blocks)
        total_decisions += len(result.visual_decisions)
        result_artifact_hashes.add(stage.output_artifact.sha256)

        assert len(provider.requests) == 1
        context = _initial_context(provider.requests[0])
        context_assets = {row["visual_id"]: row["asset"] for row in context["visual_assets"]}
        assert context_assets == {
            visual.visual_id: {
                "sha256": visual.asset.sha256,
                "media_type": visual.asset.media_type,
                "byte_size": visual.asset.byte_size,
            }
            for visual in package.visual_assets
        }

        events = _event_rows(workspace, execution_id)
        interaction_events = [
            event for event in events if event["event_type"] == "article_router_interaction"
        ]
        assert len(interaction_events) == 1
        event_data = interaction_events[0]["data"]
        assert set(event_data) == {
            "turn",
            "interaction_id",
            "request_id",
            "request_artifact",
            "response_artifact",
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "total_cached_tokens",
            "total_thought_tokens",
            "total_tool_use_tokens",
            "elapsed_ms",
            "transport_attempts",
        }
        request_ref = ArtifactRef.model_validate(event_data["request_artifact"])
        response_ref = ArtifactRef.model_validate(event_data["response_artifact"])
        store.verify(request_ref)
        store.verify(response_ref)
        assert store.read_bytes(request_ref) == provider.interactions[0].request_bytes
        assert store.read_bytes(response_ref) == provider.interactions[0].response_bytes
        assert event_data["turn"] == 1
        assert event_data["interaction_id"] == provider.interactions[0].interaction_id
        assert event_data["request_id"] == provider.interactions[0].request_id
        assert event_data["transport_attempts"] == 1
        safe_event_json = _canonical_json_bytes(event_data)
        assert provider.interactions[0].request_bytes not in safe_event_json
        assert provider.interactions[0].response_bytes not in safe_event_json

        execution_root = workspace / "executions" / execution_id
        assert (execution_root / "events.jsonl.seal.json").is_file()
        assert (execution_root / "manifest.json").is_file()
        assert (execution_root / "manifest.json.seal.json").is_file()

    assert total_source_blocks == 615
    assert total_decisions == 37
    assert tuple(all_forward_ids) == EXPECTED_FORWARD_IDS
    assert tuple(all_forward_asset_hashes) == EXPECTED_FORWARD_ASSET_SHA256S
    assert len(result_artifact_hashes) == 5

    with sqlite3.connect(workspace / "index.sqlite") as connection:
        executions = connection.execute(
            "SELECT execution_id, status FROM executions ORDER BY execution_id"
        ).fetchall()
        outputs = connection.execute(
            "SELECT stage_name, stage_version, status FROM stage_outputs ORDER BY execution_id"
        ).fetchall()
    assert executions == sorted((f"task8_{paper_id}", "SUCCEEDED") for paper_id in PAPER_IDS)
    assert outputs == [("main-article-router", "1.0.0", "SUCCEEDED")] * 5


def test_second_execution_reuses_sealed_cache_without_invoking_provider(tmp_path: Path) -> None:
    # Break caught: an identical package/profile replays the model instead of
    # reusing verified cache.
    from tsdaf.contracts.execution import ExecutionStatus, StageResult
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.workflow.article_router_runtime import run_article_router_stage

    package = canonicalize_paper_package(PAPER_FIXTURES / "hyperfirin")
    workspace = tmp_path / "workspace"
    provider = _FakeSemanticProvider()
    first = run_article_router_stage(
        workspace=workspace,
        package=package,
        execution_id="task8_cache_producer",
        prompt_dir=PROMPT_DIR,
        interaction_create=provider.create,
        provider_is_live=False,
    )
    assert isinstance(first, StageResult)
    assert first.status is ExecutionStatus.SUCCEEDED
    assert len(provider.requests) == 1

    class ExplodingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **request: object) -> object:
            del request
            self.calls += 1
            raise AssertionError("cached execution invoked provider")

    exploding = ExplodingProvider()
    cached = run_article_router_stage(
        workspace=workspace,
        package=package,
        execution_id="task8_cache_consumer",
        prompt_dir=PROMPT_DIR,
        interaction_create=exploding.create,
        provider_is_live=False,
    )

    assert isinstance(cached, StageResult)
    assert cached.status is ExecutionStatus.CACHED
    assert cached.attempt_id is None
    assert cached.output_artifact == first.output_artifact
    assert cached.cached_from_result is not None
    assert exploding.calls == 0
    for execution_id in ("task8_cache_producer", "task8_cache_consumer"):
        root = workspace / "executions" / execution_id
        assert (root / "events.jsonl.seal.json").is_file()
        assert (root / "manifest.json.seal.json").is_file()
    consumer_events = _event_rows(workspace, "task8_cache_consumer")
    assert [event["event_type"] for event in consumer_events] == [
        "stage_cached",
        "execution_finished",
    ]


def test_recorded_task7_replay_matches_exact_capture_and_live_gate_is_no_write(
    tmp_path: Path,
) -> None:
    # Break caught: Task 8 emits a request Task 7 cannot replay or writes before LIVE_REQUIRED.
    from tsdaf.adapters.gemini.interactions import (
        GEMINI_MODEL_PROFILE,
        RecordedGeminiInteractions,
    )
    from tsdaf.contracts.execution import FailureCode, TypedFailure
    from tsdaf.contracts.routing import ArticleRoutingResult
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.main_router import MainArticleRouter
    from tsdaf.workflow.article_router_runtime import run_article_router_stage

    package = canonicalize_paper_package(PAPER_FIXTURES / "principinol_B")
    captured_provider = _FakeSemanticProvider()
    captured = MainArticleRouter(PROMPT_DIR).route(
        package=package,
        execution_id="task8_recorded_replay",
        interaction_create=captured_provider.create,
    )
    assert isinstance(captured, ArticleRoutingResult)
    assert len(captured_provider.interactions) == 1
    interaction = captured_provider.interactions[0]
    request = json.loads(interaction.request_bytes)
    response = json.loads(interaction.response_bytes)
    recorded_fixture = {
        "schema_version": "tsdaf.recorded-gemini-interactions.v1",
        "model_profile": GEMINI_MODEL_PROFILE,
        "interactions": [
            {
                "request": request,
                "request_sha256": hashlib.sha256(interaction.request_bytes).hexdigest(),
                "response": response,
                "elapsed_ms": interaction.elapsed_ms,
            }
        ],
    }
    recorded_path = tmp_path / "captured-task8-interaction.json"
    recorded_path.write_bytes(_canonical_json_bytes(recorded_fixture))

    replay = RecordedGeminiInteractions(recorded_path)
    replayed = MainArticleRouter(PROMPT_DIR).route(
        package=package,
        execution_id="task8_recorded_replay",
        interaction_create=replay.create,
    )
    assert replayed == captured

    class ExplodingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **request: object) -> object:
            del request
            self.calls += 1
            raise AssertionError("LIVE_REQUIRED invoked provider")

    workspace = tmp_path / "live-disabled-workspace"
    exploding = ExplodingProvider()
    live_required = run_article_router_stage(
        workspace=workspace,
        package=package,
        execution_id="task8_live_disabled",
        prompt_dir=PROMPT_DIR,
        interaction_create=exploding.create,
        provider_is_live=True,
        live=False,
    )
    assert isinstance(live_required, TypedFailure)
    assert live_required.code is FailureCode.LIVE_REQUIRED
    assert exploding.calls == 0
    assert not workspace.exists()
