"""Isolated visual-verification acceptance tests for Task 9."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
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
TRIAGE_DECISIONS = (
    "FORWARD_ROUTE",
    "RETROSYNTHESIS",
    "MECHANISM",
    "OPTIMIZATION_SCREENING",
    "FAILED_ONLY_ROUTE",
    "BIOSYNTHESIS",
    "ANALYTICAL",
    "OTHER",
    "UNCERTAIN",
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
    rows = payload["papers"]
    assert isinstance(rows, list)
    by_id = {row["paper_id"]: row for row in rows}
    assert tuple(by_id) == PAPER_IDS
    return by_id


def _test_provenance() -> dict[str, object]:
    digest = "0" * 64
    return {
        "schema_version": "tsdaf.router-provenance.v1",
        "router_id": "main-article-router",
        "router_version": "1.0.0",
        "provider_id": "test-provider",
        "model_id": "test-model",
        "provider_profile_id": "test-profile",
        "provider_profile_version": "1.0.0",
        "provider_profile_sha256": digest,
        "prompt_id": "article-router",
        "prompt_version": "1.0.0",
        "prompt_sha256": digest,
        "contract_schema_bundle_version": "tsdaf.contract-schema-bundle.v1",
        "contract_schema_sha256": digest,
        "taxonomy_sha256": digest,
        "tool_schema_version": "tsdaf.article-router-tools.v1",
        "tool_schema_sha256": digest,
    }


def _routing_payload(
    package: Any,
    execution_id: str,
    *,
    provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    semantic = _semantic_rows()[package.manifest.paper_id]
    assert semantic["canonical_package_sha256"] == package.canonical_content_sha256
    visual_groups = {visual.visual_id: visual.visual_group_id for visual in package.visual_assets}
    decision_ids = [decision["visual_id"] for decision in semantic["visual_decisions"]]
    assert decision_ids == list(visual_groups)
    route_group_id = semantic["route_group_id"]
    return {
        "schema_version": "tsdaf.article-routing-result.v1",
        "paper_id": package.manifest.paper_id,
        "execution_id": execution_id,
        "canonical_package_sha256": package.canonical_content_sha256,
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
        "provenance": dict(provenance) if provenance is not None else _test_provenance(),
    }


def _flagged_submission(
    completed: Mapping[str, object], flagged_ids: tuple[str, ...]
) -> dict[str, object]:
    submission = copy.deepcopy(dict(completed))
    decisions = submission["visual_decisions"]
    assert isinstance(decisions, list)
    flagged = set(flagged_ids)
    for decision in decisions:
        assert isinstance(decision, dict)
        if decision["visual_id"] in flagged:
            decision["initial_decision"] = "NEEDS_VISUAL_VERIFICATION"
            decision["triage_result"] = None
    assert sum(decision["visual_id"] in flagged for decision in decisions) == len(flagged_ids)
    return submission


def _adapter_request(request: Mapping[str, object]) -> dict[str, object]:
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


def _interaction_evidence(
    request: Mapping[str, object],
    response: dict[str, object],
    *,
    call_index: int,
) -> _InteractionEvidence:
    usage = response["usage"]
    assert isinstance(usage, dict)
    output_text = response.get("output_text")
    assert output_text is None or isinstance(output_text, str)
    return _InteractionEvidence(
        request_bytes=_canonical_json_bytes(_adapter_request(request)),
        response_bytes=_canonical_json_bytes(response),
        interaction_id=str(response["id"]),
        output_text=output_text,
        total_input_tokens=usage["total_input_tokens"],
        total_output_tokens=usage["total_output_tokens"],
        total_tokens=usage["total_tokens"],
        total_cached_tokens=usage["total_cached_tokens"],
        total_thought_tokens=usage["total_thought_tokens"],
        total_tool_use_tokens=usage["total_tool_use_tokens"],
        request_id=str(response["request_id"]),
        elapsed_ms=10.0 + call_index,
        transport_attempts=1,
    )


def _model_output_response(output: object, *, call_index: int) -> dict[str, object]:
    output_text = _canonical_json_bytes(output).decode("utf-8")
    return {
        "id": f"interaction-triage-{call_index:03d}",
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": output_text}],
            }
        ],
        "output_text": output_text,
        "usage": {
            "total_input_tokens": 200 + call_index,
            "total_output_tokens": 20 + call_index,
            "total_tokens": 220 + 2 * call_index,
            "total_cached_tokens": 0,
            "total_thought_tokens": 2,
            "total_tool_use_tokens": 0,
        },
        "request_id": f"req-triage-{call_index:03d}",
    }


class _ScriptedTriageProvider:
    def __init__(self, outputs: list[object]) -> None:
        self._outputs = list(outputs)
        self.requests: list[dict[str, object]] = []
        self.interactions: list[_InteractionEvidence] = []

    @property
    def remaining(self) -> int:
        return len(self._outputs)

    def create(self, **request: object) -> _InteractionEvidence:
        self.requests.append(dict(request))
        if not self._outputs:
            raise AssertionError("visual triage exceeded the scripted interaction count")
        call_index = len(self.requests)
        response = _model_output_response(self._outputs.pop(0), call_index=call_index)
        interaction = _interaction_evidence(request, response, call_index=call_index)
        self.interactions.append(interaction)
        return interaction


def _initial_context(request: Mapping[str, object]) -> dict[str, Any]:
    history = request["input"]
    assert isinstance(history, list) and history
    initial = history[0]
    assert isinstance(initial, dict) and initial["type"] == "user_input"
    content = initial["content"]
    assert isinstance(content, list) and content == [content[0]]
    text = content[0]
    assert isinstance(text, dict) and text["type"] == "text"
    context = json.loads(text["text"])
    assert isinstance(context, dict)
    return context


class _MainUnresolvedProvider:
    def __init__(self, package: Any, visual_id: str) -> None:
        self._package = package
        self._visual_id = visual_id
        self.requests: list[dict[str, object]] = []
        self.interactions: list[_InteractionEvidence] = []

    def create(self, **request: object) -> _InteractionEvidence:
        request_dict = dict(request)
        self.requests.append(request_dict)
        call_index = len(self.requests)
        context = _initial_context(request_dict)
        payload = _routing_payload(
            self._package,
            context["execution_id"],
            provenance=context["expected_provenance"],
        )
        payload = _flagged_submission(payload, (self._visual_id,))
        response: dict[str, object] = {
            "id": f"interaction-main-{call_index:03d}",
            "status": "completed",
            "steps": [
                {
                    "type": "function_call",
                    "name": "submit_article_routing",
                    "call_id": f"call-main-{call_index:03d}",
                    "arguments": payload,
                }
            ],
            "usage": {
                "total_input_tokens": 1000 + call_index,
                "total_output_tokens": 100 + call_index,
                "total_tokens": 1100 + 2 * call_index,
                "total_cached_tokens": 0,
                "total_thought_tokens": 5,
                "total_tool_use_tokens": 7,
            },
            "request_id": f"req-main-{call_index:03d}",
        }
        interaction = _interaction_evidence(request_dict, response, call_index=call_index)
        self.interactions.append(interaction)
        return interaction


def _artifact_path(package_dir: Path, relative_path: str) -> Path:
    return package_dir.joinpath(*PurePosixPath(relative_path).parts)


def _source_hashes(package_dir: Path, package: Any) -> dict[str, str]:
    manifest = package.manifest
    references = (
        manifest.source_pdf,
        manifest.normalized_document,
        manifest.normalized_blocks,
        *manifest.assets,
    )
    return {
        reference.relative_path: hashlib.sha256(
            _artifact_path(package_dir, reference.relative_path).read_bytes()
        ).hexdigest()
        for reference in references
    }


def _expected_neighbor_rows(package: Any, visual: Any) -> list[dict[str, str]]:
    groups = {group.visual_group_id: group for group in package.visual_groups}
    group = groups[visual.visual_group_id]
    blocks_by_id = {block.block_id: block for block in package.blocks}
    if group.caption_block_ids:
        selected = [blocks_by_id[block_id] for block_id in group.caption_block_ids[:2]]
    else:
        included = set(package.clean_view.included_block_ids)
        canonical_order = {block.block_id: index for index, block in enumerate(package.blocks)}
        candidates = [
            block
            for block in package.blocks
            if block.page_index == visual.page_index
            and block.block_id in included
            and block.source.source_asset_ref is None
            and block.raw_text
        ]
        nearest = sorted(
            candidates,
            key=lambda block: (
                abs(block.source.native_block_index - visual.source.native_block_index),
                block.source.native_block_index,
                canonical_order[block.block_id],
            ),
        )[:2]
        selected = sorted(nearest, key=lambda block: canonical_order[block.block_id])
    return [{"block_id": block.block_id, "raw_text": block.raw_text} for block in selected]


def _triage_content(request: Mapping[str, object]) -> list[dict[str, object]]:
    assert set(request) == {"input", "response_format"}
    history = request["input"]
    assert isinstance(history, list) and len(history) == 1
    user_input = history[0]
    assert isinstance(user_input, dict)
    assert set(user_input) == {"type", "content"}
    assert user_input["type"] == "user_input"
    content = user_input["content"]
    assert isinstance(content, list)
    assert all(isinstance(item, dict) for item in content)
    return content


def _assert_response_format(request: Mapping[str, object]) -> None:
    response_format = request["response_format"]
    assert isinstance(response_format, dict)
    assert set(response_format) == {"type", "mime_type", "schema"}
    assert response_format["type"] == "text"
    assert response_format["mime_type"] == "application/json"
    schema = response_format["schema"]
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert schema["required"] == ["decisions"]
    assert schema["additionalProperties"] is False
    decisions = schema["properties"]["decisions"]
    assert decisions["type"] == "array"
    item = decisions["items"]
    assert item["type"] == "object"
    assert item["required"] == ["visual_id", "decision"]
    assert item["additionalProperties"] is False
    assert tuple(item["properties"]["decision"]["enum"]) == TRIAGE_DECISIONS


def _assert_triage_request(
    request: Mapping[str, object],
    *,
    package_dir: Path,
    package: Any,
    expected_ids: tuple[str, ...],
) -> None:
    content = _triage_content(request)
    _assert_response_format(request)
    assert len(content) == 1 + 2 * len(expected_ids)
    instruction_item = content[0]
    assert set(instruction_item) == {"type", "text"}
    assert instruction_item["type"] == "text"
    instruction = instruction_item["text"]
    assert isinstance(instruction, str) and len(instruction.encode("utf-8")) <= 300
    lowered = instruction.lower()
    assert "high-level visual category verification" in lowered
    assert "reaction" in lowered
    assert "crop" in lowered and "panel" in lowered
    assert "smiles" in lowered
    assert "do not" in lowered or "forbid" in lowered

    visuals = {visual.visual_id: visual for visual in package.visual_assets}
    groups = {group.visual_group_id: group for group in package.visual_groups}
    metadata_ids: list[str] = []
    for offset, visual_id in enumerate(expected_ids):
        metadata_item = content[1 + 2 * offset]
        image_item = content[2 + 2 * offset]
        assert set(metadata_item) == {"type", "text"}
        assert metadata_item["type"] == "text"
        metadata = json.loads(metadata_item["text"])
        assert set(metadata) == {
            "visual_id",
            "candidate_category",
            "group_caption",
            "native_caption",
            "page_index",
            "neighboring_blocks",
        }
        metadata_ids.append(metadata["visual_id"])
        visual = visuals[visual_id]
        group = groups[visual.visual_group_id]
        assert metadata["candidate_category"] in TRIAGE_DECISIONS[:-1]
        assert metadata["group_caption"] == group.caption_text
        assert metadata["native_caption"] == visual.native_caption
        assert metadata["page_index"] == visual.page_index
        assert metadata["neighboring_blocks"] == _expected_neighbor_rows(package, visual)
        assert len(metadata["neighboring_blocks"]) <= 2

        assert set(image_item) == {"type", "mime_type", "data", "resolution"}
        assert image_item["type"] == "image"
        assert image_item["mime_type"] == visual.asset.media_type
        assert image_item["resolution"] == "medium"
        encoded = image_item["data"]
        assert isinstance(encoded, str) and not encoded.startswith("data:")
        expected_bytes = _artifact_path(package_dir, visual.asset.relative_path).read_bytes()
        assert base64.b64decode(encoded, validate=True) == expected_bytes
    assert tuple(metadata_ids) == expected_ids

    serialized = _canonical_json_bytes(request).decode("utf-8")
    expected_set = set(expected_ids)
    for visual in package.visual_assets:
        if visual.visual_id not in expected_set:
            assert visual.visual_id not in serialized
        assert visual.asset.relative_path not in serialized
        assert visual.asset.sha256 not in serialized
    assert str(package_dir) not in serialized


def _assert_main_request_is_text_only(
    request: Mapping[str, object], package_dir: Path, package: Any
) -> None:
    assert set(request) == {"input", "system_instruction", "tools"}
    context = _initial_context(request)
    forbidden_keys = {"bytes", "base64", "data_url", "pixels", "crop", "relative_path"}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert forbidden_keys.isdisjoint(value)
            assert value.get("type") != "image"
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            assert not value.startswith("data:image/")

    visit(context)
    serialized = _canonical_json_bytes(request).decode("utf-8")
    assert str(package_dir) not in serialized
    for visual in package.visual_assets:
        assert visual.asset.relative_path not in serialized


def test_completed_five_paper_results_return_without_visual_io(tmp_path: Path) -> None:
    # Break caught: the visual lane reads assets or calls a provider for an already-complete result.
    from tsdaf.contracts.routing import ArticleRoutingResult, VisualDecision
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.visual_triage import resolve_visual_verification

    provider_calls = 0
    interaction_calls: list[tuple[int, object]] = []

    def exploding_provider(**request: object) -> object:
        nonlocal provider_calls
        del request
        provider_calls += 1
        raise AssertionError("completed routing result invoked visual provider")

    def record_interaction(batch_index: int, evidence: object) -> None:
        interaction_calls.append((batch_index, evidence))
        raise AssertionError("completed routing result emitted visual interaction")

    unusable_package_dir = tmp_path / "missing-package-that-must-not-be-read"
    assert not unusable_package_dir.exists()
    total_decisions = 0
    total_forwards = 0
    for paper_id in PAPER_IDS:
        package = canonicalize_paper_package(PAPER_FIXTURES / paper_id)
        payload = _routing_payload(package, f"task9-complete-{paper_id}")
        completed = ArticleRoutingResult.model_validate(payload, strict=False)
        completed.validate_against(package)

        result = resolve_visual_verification(
            package_dir=unusable_package_dir,
            package=package,
            routing_submission=payload,
            interaction_create=exploding_provider,
            on_interaction=record_interaction,
        )

        assert isinstance(result, ArticleRoutingResult)
        assert result == completed
        total_decisions += len(result.visual_decisions)
        total_forwards += sum(
            decision.final_decision is VisualDecision.FORWARD_ROUTE
            for decision in result.visual_decisions
        )

    assert total_decisions == 37
    assert total_forwards == 9
    assert provider_calls == 0
    assert interaction_calls == []


def test_flagged_only_batches_use_original_assets_and_preserve_source_bytes() -> None:
    # Break caught: triage leaks an unflagged asset/article context, exceeds four
    # images, or mutates input.
    from tsdaf.contracts.routing import ArticleRoutingResult, VisualDecision
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.visual_triage import resolve_visual_verification

    package_dir = PAPER_FIXTURES / "hyperfirin"
    package = canonicalize_paper_package(package_dir)
    flagged_ids = (
        "MAIN-P0000-A0000",
        "MAIN-P0000-A0001",
        "MAIN-P0000-A0002",
        "MAIN-P0000-A0003",
        "MAIN-P0000-A0004",
    )
    assert tuple(visual.visual_id for visual in package.visual_assets[:5]) == flagged_ids
    completed = _routing_payload(package, "task9-batches")
    submission = _flagged_submission(completed, flagged_ids)
    submission_snapshot = copy.deepcopy(submission)
    source_hashes_before = _source_hashes(package_dir, package)
    provider = _ScriptedTriageProvider(
        [
            {
                "decisions": [
                    {"visual_id": visual_id, "decision": "OTHER"} for visual_id in flagged_ids[:4]
                ]
            },
            {
                "decisions": [
                    {"visual_id": flagged_ids[4], "decision": "OTHER"},
                ]
            },
        ]
    )
    interactions: list[tuple[int, object]] = []

    result = resolve_visual_verification(
        package_dir=package_dir,
        package=package,
        routing_submission=submission,
        interaction_create=provider.create,
        on_interaction=lambda batch_index, evidence: interactions.append((batch_index, evidence)),
    )

    assert isinstance(result, ArticleRoutingResult)
    result.validate_against(package)
    assert submission == submission_snapshot
    assert len(provider.requests) == 2
    assert provider.remaining == 0
    assert [batch_index for batch_index, _ in interactions] == [1, 2]
    assert [evidence for _, evidence in interactions] == provider.interactions
    _assert_triage_request(
        provider.requests[0],
        package_dir=package_dir,
        package=package,
        expected_ids=flagged_ids[:4],
    )
    _assert_triage_request(
        provider.requests[1],
        package_dir=package_dir,
        package=package,
        expected_ids=flagged_ids[4:],
    )

    raw_rows = {
        row["visual_id"]: row for row in submission["visual_decisions"] if isinstance(row, dict)
    }
    result_rows = {decision.visual_id: decision for decision in result.visual_decisions}
    for visual_id in flagged_ids:
        decision = result_rows[visual_id]
        assert decision.initial_decision is VisualDecision.NEEDS_VISUAL_VERIFICATION
        assert decision.triage_result is VisualDecision.OTHER
        assert decision.final_decision is VisualDecision.OTHER
    for visual_id in set(result_rows) - set(flagged_ids):
        assert _canonical_json_bytes(result_rows[visual_id].model_dump(mode="json")) == (
            _canonical_json_bytes(raw_rows[visual_id])
        )
    assert _source_hashes(package_dir, package) == source_hashes_before


def test_uncertain_reviews_and_unknown_provider_ids_fail_without_input_mutation() -> None:
    # Break caught: UNCERTAIN is rejected, or an unknown response ID is accepted
    # or mutates caller data.
    from tsdaf.contracts.execution import FailureCode, TypedFailure
    from tsdaf.contracts.routing import ArticleRoutingResult, RoutingDisposition, VisualDecision
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.visual_triage import resolve_visual_verification

    package_dir = PAPER_FIXTURES / "hyperfirin"
    package = canonicalize_paper_package(package_dir)
    visual_id = "MAIN-P0000-A0000"
    completed = _routing_payload(package, "task9-review")
    uncertain_submission = _flagged_submission(completed, (visual_id,))
    uncertain_snapshot = copy.deepcopy(uncertain_submission)
    uncertain_provider = _ScriptedTriageProvider(
        [{"decisions": [{"visual_id": visual_id, "decision": "UNCERTAIN"}]}]
    )

    uncertain = resolve_visual_verification(
        package_dir=package_dir,
        package=package,
        routing_submission=uncertain_submission,
        interaction_create=uncertain_provider.create,
    )

    assert isinstance(uncertain, ArticleRoutingResult)
    uncertain.validate_against(package)
    assert uncertain.disposition is RoutingDisposition.REVIEW_REQUIRED
    decision = next(item for item in uncertain.visual_decisions if item.visual_id == visual_id)
    assert decision.initial_decision is VisualDecision.NEEDS_VISUAL_VERIFICATION
    assert decision.triage_result is VisualDecision.UNCERTAIN
    assert decision.final_decision is VisualDecision.UNCERTAIN
    assert uncertain_submission == uncertain_snapshot

    invalid_submission = _flagged_submission(completed, (visual_id,))
    invalid_snapshot = copy.deepcopy(invalid_submission)
    invalid_provider = _ScriptedTriageProvider(
        [
            {
                "decisions": [
                    {"visual_id": "MAIN-P9999-A9999", "decision": "OTHER"},
                ]
            }
        ]
    )
    invalid_callbacks: list[tuple[int, object]] = []

    invalid = resolve_visual_verification(
        package_dir=package_dir,
        package=package,
        routing_submission=invalid_submission,
        interaction_create=invalid_provider.create,
        on_interaction=lambda batch_index, evidence: invalid_callbacks.append(
            (batch_index, evidence)
        ),
    )

    assert isinstance(invalid, TypedFailure)
    assert invalid.code is FailureCode.VISUAL_VERIFICATION_UNRESOLVED
    assert invalid_submission == invalid_snapshot
    assert len(invalid_provider.requests) == 1
    assert invalid_callbacks == []


def test_main_router_delegates_one_unresolved_visual_and_keeps_default_failure() -> None:
    # Break caught: Main Router gains image context, retries submit, or loses its opt-in boundary.
    from tsdaf.contracts.execution import FailureCode, TypedFailure
    from tsdaf.contracts.routing import ArticleRoutingResult, VisualDecision
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.main_router import MainArticleRouter
    from tsdaf.lanes.document.routing.visual_triage import resolve_visual_verification

    package_dir = PAPER_FIXTURES / "hyperfirin"
    package = canonicalize_paper_package(package_dir)
    unresolved_id = "MAIN-P0001-A0001"
    triage_provider = _ScriptedTriageProvider(
        [
            {
                "decisions": [
                    {"visual_id": unresolved_id, "decision": "FORWARD_ROUTE"},
                ]
            }
        ]
    )
    triage_interactions: list[tuple[int, object]] = []
    resolver = partial(
        resolve_visual_verification,
        package_dir=package_dir,
        package=package,
        interaction_create=triage_provider.create,
        on_interaction=lambda batch_index, evidence: triage_interactions.append(
            (batch_index, evidence)
        ),
    )
    main_provider = _MainUnresolvedProvider(package, unresolved_id)

    result = MainArticleRouter(PROMPT_DIR).route(
        package=package,
        execution_id="task9-main-isolation",
        interaction_create=main_provider.create,
        resolve_visual_verification=resolver,
    )

    assert isinstance(result, ArticleRoutingResult)
    result.validate_against(package)
    resolved = next(item for item in result.visual_decisions if item.visual_id == unresolved_id)
    assert resolved.initial_decision is VisualDecision.NEEDS_VISUAL_VERIFICATION
    assert resolved.triage_result is VisualDecision.FORWARD_ROUTE
    assert resolved.final_decision is VisualDecision.FORWARD_ROUTE
    assert len(main_provider.requests) == 1
    assert len(triage_provider.requests) == 1
    assert [batch_index for batch_index, _ in triage_interactions] == [1]
    _assert_main_request_is_text_only(main_provider.requests[0], package_dir, package)
    _assert_triage_request(
        triage_provider.requests[0],
        package_dir=package_dir,
        package=package,
        expected_ids=(unresolved_id,),
    )

    default_provider = _MainUnresolvedProvider(package, unresolved_id)
    default = MainArticleRouter(PROMPT_DIR).route(
        package=package,
        execution_id="task9-main-isolation",
        interaction_create=default_provider.create,
    )

    assert isinstance(default, TypedFailure)
    assert default.code is FailureCode.VISUAL_VERIFICATION_REQUIRED
    assert len(default_provider.requests) == 1
    assert len(triage_provider.requests) == 1
    _assert_main_request_is_text_only(default_provider.requests[0], package_dir, package)
