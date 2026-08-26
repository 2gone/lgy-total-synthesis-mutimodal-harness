"""Task 8 contract tests for the offline, text-only Main Article Router."""

from __future__ import annotations

import hashlib
import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PAPER_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "paper_packages"
SEMANTIC_FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "providers" / "fake" / "article_router_v1.json"
)
PROMPT_DIR = PROJECT_ROOT / "prompts" / "article_router" / "v1"

_PROMPT_NAMES = ("manifest.toml", "system.md", "task.md", "taxonomy.md")
_TAXONOMY_DOCUMENT = {
    "ArticleType": [
        "TOTAL_SYNTHESIS",
        "FORMAL_SYNTHESIS",
        "SEMISYNTHESIS",
        "SYNTHETIC_APPROACH",
        "NOT_IN_SCOPE",
    ],
    "RouteAttribute": [
        "ASYMMETRIC",
        "RACEMIC",
        "BIOMIMETIC",
        "CHEMOENZYMATIC",
        "DIVERGENT",
        "COLLECTIVE",
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
    "CoarseTextRole": [
        "ROUTE_CORE",
        "DIRECT_OPTIMIZATION_CONTEXT",
        "DIRECT_MECHANISM_CONTEXT",
        "DIRECT_FAILED_ATTEMPT_CONTEXT",
    ],
    "RoutingDisposition": ["ROUTED", "REVIEW_REQUIRED", "REJECTED"],
}
_EXPECTED_TAXONOMY = "\n".join(
    ["# Article Router Taxonomy", ""]
    + [
        line
        for enum_name, values in _TAXONOMY_DOCUMENT.items()
        for line in (f"## {enum_name}", *(f"- `{value}`" for value in values), "")
    ]
)
_PROVIDER_PROFILE = {
    "schema_version": "tsdaf.provider-profile.v1",
    "provider_id": "lpzproxy-openai-compatible",
    "model_id": "gemini-3.7-flash-high",
    "profile_id": "gemini-3.7-flash-high-chat-completions-v1",
    "profile_version": "3.1.0",
    "api": "chat-completions-v1",
    "reasoning_effort": "high",
    "store": False,
    "stream": False,
    "background": False,
}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _bound_prompt_sha256(prompt_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in _PROMPT_NAMES:
        payload = (prompt_dir / name).read_bytes()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _semantic_rows() -> dict[str, dict[str, Any]]:
    payload = json.loads(SEMANTIC_FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "tsdaf.fake-article-router.v1"
    rows = payload["papers"]
    assert isinstance(rows, list)
    return {row["paper_id"]: row for row in rows}


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
    semantic_ids = [decision["visual_id"] for decision in semantic["visual_decisions"]]
    assert semantic_ids == list(visual_groups)
    decisions = [
        {
            **decision,
            "visual_group_id": visual_groups[decision["visual_id"]],
        }
        for decision in semantic["visual_decisions"]
    ]
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
        "visual_decisions": decisions,
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


def _function_response(
    name: str,
    arguments: object,
    *,
    turn: int,
    call_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": f"interaction-unit-{turn:03d}",
        "status": "completed",
        "steps": [
            {
                "type": "function_call",
                "name": name,
                "call_id": call_id or f"call-unit-{turn:03d}",
                "arguments": arguments,
            }
        ],
        "usage": {
            "total_input_tokens": 100 + turn,
            "total_output_tokens": 20 + turn,
            "total_tokens": 120 + 2 * turn,
            "total_cached_tokens": 0,
            "total_thought_tokens": 3,
            "total_tool_use_tokens": 4,
        },
        "request_id": f"req-unit-{turn:03d}",
    }


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


def _interaction_evidence(
    request: dict[str, object], response: dict[str, object], *, turn: int
) -> _InteractionEvidence:
    usage = response.get("usage", {})
    assert isinstance(usage, dict)
    return _InteractionEvidence(
        request_bytes=_canonical_json_bytes(_adapter_request(request)),
        response_bytes=_canonical_json_bytes(response),
        interaction_id=response.get("id") if isinstance(response.get("id"), str) else None,
        output_text=(
            response.get("output_text") if isinstance(response.get("output_text"), str) else None
        ),
        total_input_tokens=usage.get("total_input_tokens"),
        total_output_tokens=usage.get("total_output_tokens"),
        total_tokens=usage.get("total_tokens"),
        total_cached_tokens=usage.get("total_cached_tokens"),
        total_thought_tokens=usage.get("total_thought_tokens"),
        total_tool_use_tokens=usage.get("total_tool_use_tokens"),
        request_id=(
            response.get("request_id") if isinstance(response.get("request_id"), str) else None
        ),
        elapsed_ms=float(turn) + 0.25,
        transport_attempts=1,
    )


class _ScriptedProvider:
    def __init__(
        self,
        handler: Callable[[int, dict[str, object]], dict[str, object]],
    ) -> None:
        self.handler = handler
        self.requests: list[dict[str, object]] = []
        self.interactions: list[_InteractionEvidence] = []

    def create(self, **request: object) -> _InteractionEvidence:
        request_dict = dict(request)
        self.requests.append(request_dict)
        turn = len(self.requests)
        response = self.handler(turn, request_dict)
        interaction = _interaction_evidence(request_dict, response, turn=turn)
        self.interactions.append(interaction)
        return interaction


def _submitting_provider() -> _ScriptedProvider:
    def submit(turn: int, request: dict[str, object]) -> dict[str, object]:
        context = _initial_context(request)
        return _function_response(
            "submit_article_routing",
            _routing_payload(context),
            turn=turn,
        )

    return _ScriptedProvider(submit)


def _function_result(step: dict[str, object]) -> dict[str, Any]:
    assert step["type"] == "function_result"
    result = step["result"]
    assert isinstance(result, list) and result == [result[0]]
    assert result[0]["type"] == "text"
    value = json.loads(result[0]["text"])
    assert isinstance(value, dict)
    return value


def test_context_prompt_schema_and_profile_are_fully_bound_and_text_only() -> None:
    # Break caught: a prompt, taxonomy, schema, or package byte is omitted from the request binding.
    from tsdaf.contracts.routing import ArticleRoutingResult
    from tsdaf.contracts.schema import contract_schema_sha256
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.main_router import MainArticleRouter

    package = canonicalize_paper_package(PAPER_FIXTURES / "hyperfirin")
    provider = _submitting_provider()
    result = MainArticleRouter(PROMPT_DIR).route(
        package=package,
        execution_id="exec-unit-context",
        interaction_create=provider.create,
    )

    assert isinstance(result, ArticleRoutingResult)
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert set(request) == {"input", "system_instruction", "tools"}
    assert "previous_interaction_id" not in request
    context = _initial_context(request)
    assert context["schema_version"] == "tsdaf.article-router-context.v1"
    assert context["paper_id"] == package.manifest.paper_id
    assert context["canonical_package_sha256"] == package.canonical_content_sha256
    assert context["canonicalizer_version"] == package.canonicalizer_version
    assert context["execution_id"] == "exec-unit-context"
    assert context["constraints"] == {
        "invent_omitted_or_suppressed_blocks": False,
        "invent_omitted_or_suppressed_visuals": False,
    }

    blocks_by_id = {block.block_id: block for block in package.blocks}
    expected_blocks = [
        {
            "block_id": block_id,
            "page_index": blocks_by_id[block_id].page_index,
            "reading_order": blocks_by_id[block_id].reading_order,
            "block_type": blocks_by_id[block_id].block_type.value,
            "section_path": list(blocks_by_id[block_id].section_path),
            "raw_text": blocks_by_id[block_id].raw_text,
        }
        for block_id in package.clean_view.included_block_ids
    ]
    assert context["blocks"] == expected_blocks
    assert [row["block_id"] for row in context["blocks"]] == list(
        package.clean_view.included_block_ids
    )

    groups = {group.visual_group_id: group for group in package.visual_groups}
    decorative_ids = {item.visual_id for item in package.visual_suppressions}
    expected_visuals = []
    for visual in package.visual_assets:
        group = groups[visual.visual_group_id]
        expected_visuals.append(
            {
                "visual_id": visual.visual_id,
                "visual_group_id": visual.visual_group_id,
                "page_index": visual.page_index,
                "bbox": (visual.bbox.model_dump(mode="json") if visual.bbox is not None else None),
                "width": visual.width,
                "height": visual.height,
                "asset": {
                    "sha256": visual.asset.sha256,
                    "media_type": visual.asset.media_type,
                    "byte_size": visual.asset.byte_size,
                },
                "group": {
                    "label_candidates": list(group.label_candidates),
                    "caption_block_ids": list(group.caption_block_ids),
                    "caption_text": group.caption_text,
                    "association_status": group.association_status,
                },
                "decorative": visual.visual_id in decorative_ids,
            }
        )
    assert context["visual_assets"] == expected_visuals
    assert [row["visual_id"] for row in context["visual_assets"]] == [
        visual.visual_id for visual in package.visual_assets
    ]
    forbidden_payload_keys = {"bytes", "base64", "data_url", "pixels", "crop", "relative_path"}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            assert forbidden_payload_keys.isdisjoint(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(context)

    manifest = tomllib.loads((PROMPT_DIR / "manifest.toml").read_text(encoding="utf-8"))
    assert manifest == {
        "prompt_id": "article-router",
        "version": "1.0.0",
        "schema_version": "tsdaf.article-routing-result.v1",
        "model_profile": "gemini-3.7-flash-high-chat-completions-v1",
    }
    assert (PROMPT_DIR / "taxonomy.md").read_text(encoding="utf-8") == _EXPECTED_TAXONOMY
    answer_markers = (
        "MAIN-P0004-A0000",
        "cd8704c9f7c2b7aafaadf7c955011c79fbe5afac5a94fa7ebc6e30659811578e",
    )
    for prompt_name in ("system.md", "task.md"):
        prompt_text = (PROMPT_DIR / prompt_name).read_text(encoding="utf-8")
        assert all(marker not in prompt_text for marker in answer_markers)

    tools = request["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools] == [
        "search_blocks",
        "read_blocks",
        "submit_article_routing",
    ]
    assert "strict" not in tools[0]
    assert "strict" not in tools[1]
    assert tools[2]["strict"] is True
    assert tools[2]["parameters"] == ArticleRoutingResult.model_json_schema(mode="validation")
    provenance = context["expected_provenance"]
    assert provenance == result.provenance.model_dump(mode="json")
    assert provenance["schema_version"] == "tsdaf.router-provenance.v1"
    assert provenance["router_id"] == "main-article-router"
    assert provenance["router_version"] == "1.1.0"
    assert provenance["provider_id"] == "lpzproxy-openai-compatible"
    assert provenance["model_id"] == "gemini-3.7-flash-high"
    assert provenance["provider_profile_id"] == "gemini-3.7-flash-high-chat-completions-v1"
    assert provenance["provider_profile_version"] == "3.1.0"
    assert (
        provenance["provider_profile_sha256"]
        == hashlib.sha256(_canonical_json_bytes(_PROVIDER_PROFILE)).hexdigest()
    )
    assert provenance["prompt_id"] == "article-router"
    assert provenance["prompt_version"] == "1.0.0"
    assert provenance["prompt_sha256"] == _bound_prompt_sha256(PROMPT_DIR)
    assert provenance["contract_schema_bundle_version"] == "tsdaf.contract-schema-bundle.v1"
    assert provenance["contract_schema_sha256"] == contract_schema_sha256()
    assert (
        provenance["taxonomy_sha256"]
        == hashlib.sha256(_canonical_json_bytes(_TAXONOMY_DOCUMENT)).hexdigest()
    )
    assert provenance["tool_schema_version"] == "tsdaf.article-router-tools.v1"
    assert (
        provenance["tool_schema_sha256"] == hashlib.sha256(_canonical_json_bytes(tools)).hexdigest()
    )


def test_search_read_submit_uses_exact_limits_results_and_stateless_full_history() -> None:
    # Break caught: a retrieval result is lossy or continuation switches to
    # stateful interaction IDs.
    from tsdaf.contracts.routing import ArticleRoutingResult
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.main_router import MainArticleRouter

    package = canonicalize_paper_package(PAPER_FIXTURES / "hyperfirin")

    def flow(turn: int, request: dict[str, object]) -> dict[str, object]:
        if turn == 1:
            return _function_response(
                "search_blocks",
                {
                    "query": "bicyclic core",
                    "page_range": [1, 1],
                    "section_filter": None,
                    "limit": 1,
                },
                turn=turn,
            )
        if turn == 2:
            return _function_response(
                "read_blocks",
                {
                    "block_ids": ["MAIN-P0001-B0003", "MAIN-P0001-B0005"],
                    "neighbor_count": 1,
                },
                turn=turn,
            )
        return _function_response(
            "submit_article_routing",
            _routing_payload(_initial_context(request)),
            turn=turn,
        )

    provider = _ScriptedProvider(flow)
    result = MainArticleRouter(PROMPT_DIR).route(
        package=package,
        execution_id="exec-unit-tools",
        interaction_create=provider.create,
    )

    assert isinstance(result, ArticleRoutingResult)
    assert len(provider.requests) == 3
    initial = provider.requests[0]["input"]
    first_response = json.loads(provider.interactions[0].response_bytes)
    first_result_step = provider.requests[1]["input"][-1]
    assert provider.requests[1]["input"] == [
        *initial,
        *first_response["steps"],
        first_result_step,
    ]
    assert first_result_step["name"] == "search_blocks"
    assert first_result_step["call_id"] == "call-unit-001"
    search_result = _function_result(first_result_step)
    assert search_result == {
        "hits": [
            {
                "block_id": "MAIN-P0001-B0003",
                "page_index": 1,
                "section_path": list(
                    next(
                        block.section_path
                        for block in package.blocks
                        if block.block_id == "MAIN-P0001-B0003"
                    )
                ),
                "raw_text": next(
                    block.raw_text
                    for block in package.blocks
                    if block.block_id == "MAIN-P0001-B0003"
                ),
            }
        ]
    }

    second_response = json.loads(provider.interactions[1].response_bytes)
    second_result_step = provider.requests[2]["input"][-1]
    assert provider.requests[2]["input"] == [
        *provider.requests[1]["input"],
        *second_response["steps"],
        second_result_step,
    ]
    assert second_result_step["name"] == "read_blocks"
    assert second_result_step["call_id"] == "call-unit-002"
    read_result = _function_result(second_result_step)
    expected_read_ids = [
        "MAIN-P0001-B0002",
        "MAIN-P0001-B0003",
        "MAIN-P0001-B0004",
        "MAIN-P0001-B0005",
        "MAIN-P0001-B0006",
    ]
    blocks_by_id = {block.block_id: block for block in package.blocks}
    assert read_result == {
        "blocks": [
            {
                "block_id": block_id,
                "page_index": blocks_by_id[block_id].page_index,
                "reading_order": blocks_by_id[block_id].reading_order,
                "block_type": blocks_by_id[block_id].block_type.value,
                "section_path": list(blocks_by_id[block_id].section_path),
                "raw_text": blocks_by_id[block_id].raw_text,
            }
            for block_id in expected_read_ids
        ]
    }
    assert all("previous_interaction_id" not in request for request in provider.requests)

    tools = provider.requests[0]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    search_schema = by_name["search_blocks"]["parameters"]
    assert search_schema["properties"]["limit"] == {
        "default": 20,
        "maximum": 20,
        "minimum": 1,
        "type": "integer",
    }
    read_schema = by_name["read_blocks"]["parameters"]
    assert read_schema["properties"]["block_ids"]["maxItems"] == 10
    assert read_schema["properties"]["block_ids"]["uniqueItems"] is True
    assert read_schema["properties"]["neighbor_count"] == {
        "default": 1,
        "maximum": 2,
        "minimum": 0,
        "type": "integer",
    }
    semantic = _semantic_rows()["hyperfirin"]
    assert result.model_dump(mode="json") == _routing_payload(
        _initial_context(provider.requests[0])
    )
    assert result.route_groups[0].ordered_visual_ids == tuple(semantic["ordered_visual_ids"])


def test_router_returns_only_stable_safe_failures_and_allows_one_semantic_correction() -> None:
    # Break caught: malformed tools, references, or provider errors leak text or
    # bypass fixed budgets.
    from tsdaf.contracts.execution import FailureCode, TypedFailure
    from tsdaf.contracts.routing import ArticleRoutingResult
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.lanes.document.routing.main_router import MainArticleRouter

    package = canonicalize_paper_package(PAPER_FIXTURES / "hyperfirin")
    router = MainArticleRouter(PROMPT_DIR)

    oversized_blocks = list(package.blocks)
    included_id = package.clean_view.included_block_ids[0]
    oversized_index = next(
        index for index, block in enumerate(oversized_blocks) if block.block_id == included_id
    )
    oversized_blocks[oversized_index] = oversized_blocks[oversized_index].model_copy(
        update={"raw_text": "X" * 900_100}
    )
    oversized_package = package.model_copy(update={"blocks": tuple(oversized_blocks)})

    class ExplodingProvider:
        def __init__(self, message: str) -> None:
            self.message = message
            self.calls = 0

        def create(self, **request: object) -> object:
            del request
            self.calls += 1
            raise RuntimeError(self.message)

    should_not_call = ExplodingProvider("oversized provider must remain untouched")
    oversized = router.route(
        package=oversized_package,
        execution_id="exec-unit-oversized",
        interaction_create=should_not_call.create,
    )
    assert isinstance(oversized, TypedFailure)
    assert oversized.code is FailureCode.CONTEXT_TOO_LARGE
    assert should_not_call.calls == 0

    def retrievals(turn: int, request: dict[str, object]) -> dict[str, object]:
        del request
        return {
            "id": f"interaction-budget-{turn}",
            "status": "completed",
            "steps": [
                {
                    "type": "function_call",
                    "name": "search_blocks",
                    "call_id": f"call-budget-{turn}-a",
                    "arguments": {"query": "route", "limit": 1},
                },
                {
                    "type": "function_call",
                    "name": "search_blocks",
                    "call_id": f"call-budget-{turn}-b",
                    "arguments": {"query": "synthesis", "limit": 1},
                },
            ],
        }

    budget_provider = _ScriptedProvider(retrievals)
    exhausted = router.route(
        package=package,
        execution_id="exec-unit-budget",
        interaction_create=budget_provider.create,
    )
    assert isinstance(exhausted, TypedFailure)
    assert exhausted.code is FailureCode.TOOL_BUDGET_EXHAUSTED
    assert len(budget_provider.requests) == 4

    def submit_on_fifth_turn(turn: int, request: dict[str, object]) -> dict[str, object]:
        if turn < 5:
            return _function_response(
                "search_blocks",
                {"query": "route", "limit": 1},
                turn=turn,
            )
        return _function_response(
            "submit_article_routing",
            _routing_payload(_initial_context(request)),
            turn=turn,
        )

    fifth_turn_provider = _ScriptedProvider(submit_on_fifth_turn)
    fifth_turn_submit = router.route(
        package=package,
        execution_id="exec-unit-fifth-turn-submit",
        interaction_create=fifth_turn_provider.create,
    )
    assert isinstance(fifth_turn_submit, ArticleRoutingResult)
    assert len(fifth_turn_provider.requests) == 5

    no_call_provider = _ScriptedProvider(
        lambda turn, request: {
            "id": f"interaction-no-call-{turn}",
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "x"}]}],
        }
    )
    no_submit = router.route(
        package=package,
        execution_id="exec-unit-no-submit",
        interaction_create=no_call_provider.create,
    )
    assert isinstance(no_submit, TypedFailure)
    assert no_submit.code is FailureCode.ROUTER_NO_SUBMIT
    assert len(no_call_provider.requests) == 1

    def unresolved_visual(turn: int, request: dict[str, object]) -> dict[str, object]:
        payload = _routing_payload(_initial_context(request))
        payload["visual_decisions"][0]["initial_decision"] = "NEEDS_VISUAL_VERIFICATION"
        return _function_response("submit_article_routing", payload, turn=turn)

    visual_provider = _ScriptedProvider(unresolved_visual)
    visual_required = router.route(
        package=package,
        execution_id="exec-unit-visual-required",
        interaction_create=visual_provider.create,
    )
    assert isinstance(visual_required, TypedFailure)
    assert visual_required.code is FailureCode.VISUAL_VERIFICATION_REQUIRED
    assert len(visual_provider.requests) == 1

    def invalid_reference(turn: int, request: dict[str, object]) -> dict[str, object]:
        payload = _routing_payload(_initial_context(request))
        payload["paper_id"] = "unknown-paper"
        return _function_response("submit_article_routing", payload, turn=turn)

    reference_provider = _ScriptedProvider(invalid_reference)
    bad_reference = router.route(
        package=package,
        execution_id="exec-unit-reference",
        interaction_create=reference_provider.create,
    )
    assert isinstance(bad_reference, TypedFailure)
    assert bad_reference.code is FailureCode.ROUTER_REFERENCE_INVALID
    assert len(reference_provider.requests) == 2

    unsafe_value = "raw-provider-secret-that-must-not-enter-correction"

    def corrected_submit(turn: int, request: dict[str, object]) -> dict[str, object]:
        payload = _routing_payload(_initial_context(request))
        if turn == 1:
            payload["article_type"] = unsafe_value
        return _function_response("submit_article_routing", payload, turn=turn)

    correction_provider = _ScriptedProvider(corrected_submit)
    corrected = router.route(
        package=package,
        execution_id="exec-unit-correction",
        interaction_create=correction_provider.create,
    )
    assert isinstance(corrected, ArticleRoutingResult)
    assert len(correction_provider.requests) == 2
    correction_step = correction_provider.requests[1]["input"][-1]
    correction = _function_result(correction_step)
    assert set(correction) == {"code", "instruction"}
    assert correction["code"] == "ROUTER_SCHEMA_INVALID"
    assert unsafe_value not in correction_step["result"][0]["text"]

    unsafe_exception = "Authorization: Bearer task-8-secret https://private.invalid"
    rejected_provider = ExplodingProvider(unsafe_exception)
    rejected = router.route(
        package=package,
        execution_id="exec-unit-provider-rejected",
        interaction_create=rejected_provider.create,
    )
    assert isinstance(rejected, TypedFailure)
    assert rejected.code is FailureCode.PROVIDER_REJECTED
    assert rejected_provider.calls == 1
    assert unsafe_exception not in rejected.model_dump_json()
    assert "task-8-secret" not in rejected.message

    preserved_failure = TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=FailureCode.PROVIDER_TRANSIENT,
        message="Safe transient provider failure.",
        request_id="req-safe-008",
        details=(),
    )

    class AdapterError(Exception):
        def __init__(self, failure: TypedFailure) -> None:
            self.failure = failure

    def fail_with_typed_error(**request: object) -> object:
        del request
        raise AdapterError(preserved_failure)

    preserved = router.route(
        package=package,
        execution_id="exec-unit-provider-preserved",
        interaction_create=fail_with_typed_error,
    )
    assert preserved == preserved_failure
