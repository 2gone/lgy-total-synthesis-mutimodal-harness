"""Deterministic text-only Main Article Router over one canonical package."""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tsdaf.contracts.execution import FailureCode, TypedFailure
from tsdaf.contracts.package import CanonicalPaperPackage, CanonicalTextBlock
from tsdaf.contracts.routing import (
    ArticleRoutingResult,
    ArticleType,
    CoarseTextRole,
    RouteAttribute,
    RouterProvenance,
    RoutingDisposition,
    VisualDecision,
)
from tsdaf.contracts.schema import canonical_json_bytes, contract_schema_sha256
from tsdaf.lanes.document.search_index import InMemorySearchIndex

__all__ = ("MainArticleRouter",)

_ROUTER_ID = "main-article-router"
_ROUTER_VERSION = "1.2.0"
_PROVIDER_ID = "lpzproxy-openai-compatible"
_MODEL_ID = "gemini-3.7-flash-high"
_PROVIDER_PROFILE_ID = "gemini-3.7-flash-high-chat-completions-v1"
_PROVIDER_PROFILE_VERSION = "3.1.0"
_PROMPT_ID = "article-router"
_PROMPT_VERSION = "1.0.0"
_CONTRACT_SCHEMA_BUNDLE_VERSION = "tsdaf.contract-schema-bundle.v1"
_TOOL_SCHEMA_VERSION = "tsdaf.article-router-tools.v1"
_CONTEXT_SCHEMA_VERSION = "tsdaf.article-router-context.v1"
_MAX_CONTEXT_BYTES = 900_000
_MAX_PROVIDER_TURNS = 5
_MAX_RETRIEVAL_CALLS = 6
_PROMPT_NAMES = ("manifest.toml", "system.md", "task.md", "taxonomy.md")
_KNOWN_TOOL_NAMES = frozenset({"search_blocks", "read_blocks", "submit_article_routing"})
_TAXONOMY_ENUMS = (
    ArticleType,
    RouteAttribute,
    VisualDecision,
    CoarseTextRole,
    RoutingDisposition,
)


@dataclass(frozen=True, slots=True)
class _PromptBundle:
    system_instruction: str
    prompt_sha256: str
    taxonomy_sha256: str


@dataclass(frozen=True, slots=True)
class _ValidatedInteraction:
    evidence: object
    response: dict[str, object]


def _failure(code: FailureCode, message: str) -> TypedFailure:
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message=message,
        request_id=None,
        details=(),
    )


def _schema_failure() -> TypedFailure:
    return _failure(
        FailureCode.ROUTER_SCHEMA_INVALID,
        "The article router received an invalid tool call or routing schema.",
    )


def _reference_failure() -> TypedFailure:
    return _failure(
        FailureCode.ROUTER_REFERENCE_INVALID,
        "The submitted routing result does not match the canonical package references.",
    )


def _provider_failure() -> TypedFailure:
    return _failure(
        FailureCode.PROVIDER_REJECTED,
        "The article routing provider returned an invalid interaction.",
    )


def _taxonomy_document() -> dict[str, object]:
    return {
        enum_type.__name__: [member.value for member in enum_type] for enum_type in _TAXONOMY_ENUMS
    }


def _taxonomy_markdown() -> str:
    lines = ["# Article Router Taxonomy", ""]
    for enum_type in _TAXONOMY_ENUMS:
        lines.append(f"## {enum_type.__name__}")
        lines.extend(f"- `{member.value}`" for member in enum_type)
        lines.append("")
    return "\n".join(lines)


def _prompt_sha256(prompt_dir: Path) -> str:
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


def _read_prompt_bundle(prompt_dir: Path) -> _PromptBundle:
    if not isinstance(prompt_dir, Path):
        raise TypeError("prompt_dir must be a pathlib.Path")
    try:
        payloads = {name: (prompt_dir / name).read_bytes() for name in _PROMPT_NAMES}
        texts = {name: payload.decode("utf-8") for name, payload in payloads.items()}
        manifest = tomllib.loads(texts["manifest.toml"])
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise ValueError("article router prompt bundle is invalid") from None
    if manifest != {
        "prompt_id": _PROMPT_ID,
        "version": _PROMPT_VERSION,
        "schema_version": "tsdaf.article-routing-result.v1",
        "model_profile": _PROVIDER_PROFILE_ID,
    }:
        raise ValueError("article router prompt manifest is invalid")
    if texts["taxonomy.md"] != _taxonomy_markdown():
        raise ValueError("article router taxonomy asset is invalid")
    if not texts["system.md"].strip() or not texts["task.md"].strip():
        raise ValueError("article router instructions are empty")
    system_instruction = (
        "\n\n".join(
            (
                texts["system.md"].rstrip("\n"),
                texts["task.md"].rstrip("\n"),
                texts["taxonomy.md"].rstrip("\n"),
            )
        )
        + "\n"
    )
    return _PromptBundle(
        system_instruction=system_instruction,
        prompt_sha256=_prompt_sha256(prompt_dir),
        taxonomy_sha256=hashlib.sha256(canonical_json_bytes(_taxonomy_document())).hexdigest(),
    )


def _provider_profile_sha256() -> str:
    profile = {
        "schema_version": "tsdaf.provider-profile.v1",
        "provider_id": _PROVIDER_ID,
        "model_id": _MODEL_ID,
        "profile_id": _PROVIDER_PROFILE_ID,
        "profile_version": _PROVIDER_PROFILE_VERSION,
        "api": "chat-completions-v1",
        "reasoning_effort": "high",
        "store": False,
        "stream": False,
        "background": False,
    }
    return hashlib.sha256(canonical_json_bytes(profile)).hexdigest()


def _tool_declarations() -> list[dict[str, object]]:
    nullable_page_range: dict[str, object] = {
        "anyOf": [
            {
                "type": "array",
                "items": {"type": "integer", "minimum": 0, "maximum": 9999},
                "minItems": 2,
                "maxItems": 2,
            },
            {"type": "null"},
        ],
        "default": None,
    }
    nullable_section_filter: dict[str, object] = {
        "anyOf": [
            {
                "type": "array",
                "items": {"type": "string"},
                "uniqueItems": True,
            },
            {"type": "null"},
        ],
        "default": None,
    }
    return [
        {
            "type": "function",
            "name": "search_blocks",
            "description": "Search exact clean source blocks in this canonical paper.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "page_range": nullable_page_range,
                    "section_filter": nullable_section_filter,
                    "limit": {
                        "default": 20,
                        "maximum": 20,
                        "minimum": 1,
                        "type": "integer",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "read_blocks",
            "description": "Read exact clean blocks and bounded canonical-order neighbors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "block_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                        "uniqueItems": True,
                    },
                    "neighbor_count": {
                        "default": 1,
                        "maximum": 2,
                        "minimum": 0,
                        "type": "integer",
                    },
                },
                "required": ["block_ids"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "submit_article_routing",
            "description": "Submit the complete validated article-routing result.",
            "strict": True,
            "parameters": ArticleRoutingResult.model_json_schema(mode="validation"),
        },
    ]


def _provenance(bundle: _PromptBundle, tools: list[dict[str, object]]) -> RouterProvenance:
    return RouterProvenance(
        schema_version="tsdaf.router-provenance.v1",
        router_id=_ROUTER_ID,
        router_version=_ROUTER_VERSION,
        provider_id=_PROVIDER_ID,
        model_id=_MODEL_ID,
        provider_profile_id=_PROVIDER_PROFILE_ID,
        provider_profile_version=_PROVIDER_PROFILE_VERSION,
        provider_profile_sha256=_provider_profile_sha256(),
        prompt_id=_PROMPT_ID,
        prompt_version=_PROMPT_VERSION,
        prompt_sha256=bundle.prompt_sha256,
        contract_schema_bundle_version=_CONTRACT_SCHEMA_BUNDLE_VERSION,
        contract_schema_sha256=contract_schema_sha256(),
        taxonomy_sha256=bundle.taxonomy_sha256,
        tool_schema_version=_TOOL_SCHEMA_VERSION,
        tool_schema_sha256=hashlib.sha256(canonical_json_bytes(tools)).hexdigest(),
    )


def _block_payload(block: CanonicalTextBlock) -> dict[str, object]:
    return {
        "block_id": block.block_id,
        "page_index": block.page_index,
        "reading_order": block.reading_order,
        "block_type": block.block_type.value,
        "section_path": list(block.section_path),
        "raw_text": block.raw_text,
    }


def _context_payload(
    package: CanonicalPaperPackage,
    execution_id: str,
    provenance: RouterProvenance,
) -> dict[str, object]:
    blocks_by_id = {block.block_id: block for block in package.blocks}
    groups_by_id = {group.visual_group_id: group for group in package.visual_groups}
    decorative_ids = {record.visual_id for record in package.visual_suppressions}
    blocks = [
        _block_payload(blocks_by_id[block_id]) for block_id in package.clean_view.included_block_ids
    ]
    visual_assets: list[dict[str, object]] = []
    for visual in package.visual_assets:
        group = groups_by_id[visual.visual_group_id]
        visual_assets.append(
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
    return {
        "schema_version": _CONTEXT_SCHEMA_VERSION,
        "paper_id": package.manifest.paper_id,
        "canonical_package_sha256": package.canonical_content_sha256,
        "canonicalizer_version": package.canonicalizer_version,
        "execution_id": execution_id,
        "expected_provenance": provenance.model_dump(mode="json"),
        "blocks": blocks,
        "visual_assets": visual_assets,
        "constraints": {
            "invent_omitted_or_suppressed_blocks": False,
            "invent_omitted_or_suppressed_visuals": False,
        },
    }


def _expected_adapter_request_bytes(request: Mapping[str, object]) -> bytes:
    payload: dict[str, object] = {
        "input": request["input"],
        "model": _MODEL_ID,
        "store": False,
        "stream": False,
        "background": False,
        "generation_config": {"thinking_level": "high"},
    }
    for name in ("system_instruction", "tools", "response_format"):
        value = request.get(name)
        if value is not None:
            payload[name] = value
    return canonical_json_bytes(payload)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_token(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _validate_interaction(
    interaction: object,
    request: Mapping[str, object],
) -> _ValidatedInteraction | None:
    request_bytes = getattr(interaction, "request_bytes", None)
    response_bytes = getattr(interaction, "response_bytes", None)
    if (
        not isinstance(request_bytes, bytes)
        or not isinstance(response_bytes, bytes)
        or request_bytes != _expected_adapter_request_bytes(request)
    ):
        return None
    try:
        response = json.loads(response_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(response, dict)
        or canonical_json_bytes(response) != response_bytes
        or not isinstance(response.get("id"), str)
        or not response["id"]
        or not isinstance(response.get("status"), str)
        or not response["status"]
        or not isinstance(response.get("steps"), list)
        or not response["steps"]
        or any(
            not isinstance(step, Mapping) or not isinstance(step.get("type"), str)
            for step in response["steps"]
        )
    ):
        return None
    usage_value = response.get("usage")
    usage = usage_value if isinstance(usage_value, Mapping) else {}
    scalar_pairs = (
        (getattr(interaction, "interaction_id", None), response.get("id")),
        (getattr(interaction, "output_text", None), _optional_string(response.get("output_text"))),
        (
            getattr(interaction, "total_input_tokens", None),
            _optional_token(usage.get("total_input_tokens")),
        ),
        (
            getattr(interaction, "total_output_tokens", None),
            _optional_token(usage.get("total_output_tokens")),
        ),
        (getattr(interaction, "total_tokens", None), _optional_token(usage.get("total_tokens"))),
        (
            getattr(interaction, "total_cached_tokens", None),
            _optional_token(usage.get("total_cached_tokens")),
        ),
        (
            getattr(interaction, "total_thought_tokens", None),
            _optional_token(usage.get("total_thought_tokens")),
        ),
        (
            getattr(interaction, "total_tool_use_tokens", None),
            _optional_token(usage.get("total_tool_use_tokens")),
        ),
    )
    if any(actual != expected for actual, expected in scalar_pairs):
        return None
    request_id = getattr(interaction, "request_id", None)
    if request_id is not None and not isinstance(request_id, str):
        return None
    elapsed_ms = getattr(interaction, "elapsed_ms", None)
    if (
        isinstance(elapsed_ms, bool)
        or not isinstance(elapsed_ms, int | float)
        or not math.isfinite(elapsed_ms)
        or elapsed_ms < 0
    ):
        return None
    attempts = getattr(interaction, "transport_attempts", None)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        return None
    return _ValidatedInteraction(
        evidence=interaction,
        response=cast(dict[str, object], response),
    )


def _function_result(name: str, call_id: str, value: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "function_result",
        "name": name,
        "call_id": call_id,
        "result": [
            {
                "type": "text",
                "text": canonical_json_bytes(dict(value)).decode("utf-8"),
            }
        ],
    }


def _mapping_arguments(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        return None
    return dict(value)


def _structured_output_arguments(steps: list[dict[str, object]]) -> dict[str, object] | None:
    model_outputs = [step for step in steps if step.get("type") == "model_output"]
    if len(model_outputs) != 1:
        return None
    content = model_outputs[0].get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    item = content[0]
    if (
        not isinstance(item, Mapping)
        or item.get("type") != "text"
        or not isinstance(item.get("text"), str)
    ):
        return None
    try:
        parsed = json.loads(item["text"])
    except json.JSONDecodeError:
        return None
    return _mapping_arguments(parsed)


def _search_arguments(
    arguments: Mapping[str, object],
) -> tuple[str, tuple[int, int] | None, tuple[str, ...] | None, int] | None:
    if set(arguments) - {"query", "page_range", "section_filter", "limit"}:
        return None
    query = arguments.get("query")
    if not isinstance(query, str):
        return None
    raw_page_range = arguments.get("page_range")
    page_range: tuple[int, int] | None
    if raw_page_range is None:
        page_range = None
    elif (
        isinstance(raw_page_range, list)
        and len(raw_page_range) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in raw_page_range)
        and 0 <= raw_page_range[0] <= raw_page_range[1] <= 9999
    ):
        page_range = (raw_page_range[0], raw_page_range[1])
    else:
        return None
    raw_section_filter = arguments.get("section_filter")
    section_filter: tuple[str, ...] | None
    if raw_section_filter is None:
        section_filter = None
    elif (
        isinstance(raw_section_filter, list)
        and all(isinstance(item, str) for item in raw_section_filter)
        and len(raw_section_filter) == len(set(raw_section_filter))
    ):
        section_filter = tuple(raw_section_filter)
    else:
        return None
    limit = arguments.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        return None
    return query, page_range, section_filter, limit


def _read_arguments(arguments: Mapping[str, object]) -> tuple[tuple[str, ...], int] | None:
    if set(arguments) - {"block_ids", "neighbor_count"}:
        return None
    raw_ids = arguments.get("block_ids")
    if (
        not isinstance(raw_ids, list)
        or not 1 <= len(raw_ids) <= 10
        or any(not isinstance(item, str) for item in raw_ids)
        or len(raw_ids) != len(set(raw_ids))
    ):
        return None
    neighbor_count = arguments.get("neighbor_count", 1)
    if (
        isinstance(neighbor_count, bool)
        or not isinstance(neighbor_count, int)
        or not 0 <= neighbor_count <= 2
    ):
        return None
    return tuple(raw_ids), neighbor_count


def _search_result(
    index: InMemorySearchIndex,
    arguments: Mapping[str, object],
) -> dict[str, object] | None:
    checked = _search_arguments(arguments)
    if checked is None:
        return None
    query, page_range, section_filter, limit = checked
    try:
        hits = index.search_blocks(
            query,
            page_range=page_range,
            section_filter=section_filter,
            limit=limit,
        )
    except ValueError:
        return None
    return {
        "hits": [
            {
                "block_id": hit.block_id,
                "page_index": hit.page_index,
                "section_path": list(hit.section_path),
                "raw_text": hit.raw_text,
            }
            for hit in hits
        ]
    }


def _read_result(
    package: CanonicalPaperPackage,
    arguments: Mapping[str, object],
) -> dict[str, object] | None:
    checked = _read_arguments(arguments)
    if checked is None:
        return None
    requested_ids, neighbor_count = checked
    included_ids = package.clean_view.included_block_ids
    positions = {block_id: index for index, block_id in enumerate(included_ids)}
    if any(block_id not in positions for block_id in requested_ids):
        return None
    selected_positions: set[int] = set()
    for block_id in requested_ids:
        position = positions[block_id]
        lower = max(0, position - neighbor_count)
        upper = min(len(included_ids), position + neighbor_count + 1)
        selected_positions.update(range(lower, upper))
    blocks_by_id = {block.block_id: block for block in package.blocks}
    return {
        "blocks": [
            _block_payload(blocks_by_id[included_ids[position]])
            for position in sorted(selected_positions)
        ]
    }


def _raw_references_diverge(
    arguments: Mapping[str, object], package: CanonicalPaperPackage
) -> bool:
    raw_decisions = arguments.get("visual_decisions")
    if isinstance(raw_decisions, list) and all(isinstance(item, Mapping) for item in raw_decisions):
        pairs: list[tuple[str, str]] = []
        for item in raw_decisions:
            visual_id = item.get("visual_id")
            group_id = item.get("visual_group_id")
            if not isinstance(visual_id, str) or not isinstance(group_id, str):
                break
            pairs.append((visual_id, group_id))
        else:
            expected_pairs = [
                (visual.visual_id, visual.visual_group_id) for visual in package.visual_assets
            ]
            if pairs != expected_pairs:
                return True

    visual_ids = {visual.visual_id for visual in package.visual_assets}
    block_ids = {block.block_id for block in package.blocks}
    raw_groups = arguments.get("route_groups")
    route_ids: set[str] = set()
    if isinstance(raw_groups, list) and all(isinstance(item, Mapping) for item in raw_groups):
        for group in raw_groups:
            route_id = group.get("route_group_id")
            if isinstance(route_id, str):
                route_ids.add(route_id)
            raw_visual_ids = group.get("ordered_visual_ids")
            if isinstance(raw_visual_ids, list) and all(
                isinstance(item, str) for item in raw_visual_ids
            ):
                if any(item not in visual_ids for item in raw_visual_ids):
                    return True
            raw_block_ids = group.get("core_text_block_ids")
            if isinstance(raw_block_ids, list) and all(
                isinstance(item, str) for item in raw_block_ids
            ):
                if any(item not in block_ids for item in raw_block_ids):
                    return True
    raw_selections = arguments.get("coarse_text_selections")
    if isinstance(raw_selections, list) and all(
        isinstance(item, Mapping) for item in raw_selections
    ):
        for selection in raw_selections:
            route_id = selection.get("route_group_id")
            block_id = selection.get("block_id")
            if isinstance(route_id, str) and route_id not in route_ids:
                return True
            if isinstance(block_id, str) and block_id not in block_ids:
                return True
    return False


def _requests_unresolved_visual_verification(arguments: Mapping[str, object]) -> bool:
    decisions = arguments.get("visual_decisions")
    if not isinstance(decisions, list):
        return False
    return any(
        isinstance(decision, Mapping)
        and decision.get("initial_decision") == "NEEDS_VISUAL_VERIFICATION"
        and decision.get("triage_result") is None
        for decision in decisions
    )


def _validate_submission(
    arguments: Mapping[str, object],
    *,
    package: CanonicalPaperPackage,
    execution_id: str,
    provenance: RouterProvenance,
) -> ArticleRoutingResult | TypedFailure:
    if _requests_unresolved_visual_verification(arguments):
        return _failure(
            FailureCode.VISUAL_VERIFICATION_REQUIRED,
            "The routing result requires the visual-verification stage.",
        )
    try:
        result = ArticleRoutingResult.model_validate(dict(arguments), strict=False)
    except ValueError:
        if _raw_references_diverge(arguments, package):
            return _reference_failure()
        return _schema_failure()
    if (
        result.paper_id != package.manifest.paper_id
        or result.execution_id != execution_id
        or result.canonical_package_sha256 != package.canonical_content_sha256
        or result.provenance != provenance
    ):
        return _reference_failure()
    try:
        result.validate_against(package)
    except ValueError:
        return _reference_failure()
    return result


def _resolve_submission(
    arguments: Mapping[str, object],
    *,
    package: CanonicalPaperPackage,
    execution_id: str,
    provenance: RouterProvenance,
    resolve_visual_verification: (
        Callable[[Mapping[str, object]], ArticleRoutingResult | TypedFailure] | None
    ),
) -> ArticleRoutingResult | TypedFailure:
    if resolve_visual_verification is not None and _requests_unresolved_visual_verification(
        arguments
    ):
        resolver = cast(Callable[..., object], resolve_visual_verification)
        try:
            delegated = resolver(routing_submission=arguments)
        except Exception:
            return _failure(
                FailureCode.VISUAL_VERIFICATION_UNRESOLVED,
                "The visual-verification callback did not resolve the routing result.",
            )
        if not isinstance(delegated, ArticleRoutingResult | TypedFailure):
            return _failure(
                FailureCode.VISUAL_VERIFICATION_UNRESOLVED,
                "The visual-verification callback did not resolve the routing result.",
            )
        return delegated
    return _validate_submission(
        arguments,
        package=package,
        execution_id=execution_id,
        provenance=provenance,
    )


class MainArticleRouter:
    """Compile one complete text context and execute a bounded stateless tool loop."""

    def __init__(self, prompt_dir: Path) -> None:
        self._prompt = _read_prompt_bundle(prompt_dir)
        self._tools = _tool_declarations()
        self._provenance = _provenance(self._prompt, self._tools)

    def _runtime_bindings(self) -> tuple[dict[str, object], str, str, str]:
        settings: dict[str, object] = {
            "router_id": self._provenance.router_id,
            "router_version": self._provenance.router_version,
            "taxonomy_sha256": self._provenance.taxonomy_sha256,
            "tool_schema_version": self._provenance.tool_schema_version,
            "tool_schema_sha256": self._provenance.tool_schema_sha256,
            "provider_profile_sha256": self._provenance.provider_profile_sha256,
        }
        return (
            settings,
            self._provenance.prompt_sha256,
            self._provenance.contract_schema_sha256,
            self._provenance.provider_profile_id,
        )

    def route(
        self,
        *,
        package: CanonicalPaperPackage,
        execution_id: str,
        interaction_create: Callable[..., object],
        on_interaction: Callable[..., None] | None = None,
        resolve_visual_verification: (
            Callable[[Mapping[str, object]], ArticleRoutingResult | TypedFailure] | None
        ) = None,
    ) -> ArticleRoutingResult | TypedFailure:
        if not isinstance(package, CanonicalPaperPackage):
            raise TypeError("package must be a CanonicalPaperPackage")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution_id must be a nonempty string")
        if not callable(interaction_create):
            raise TypeError("interaction_create must be callable")
        if on_interaction is not None and not callable(on_interaction):
            raise TypeError("on_interaction must be callable")
        if resolve_visual_verification is not None and not callable(resolve_visual_verification):
            raise TypeError("resolve_visual_verification must be callable")

        context = _context_payload(package, execution_id, self._provenance)
        history: list[dict[str, object]] = [
            {
                "type": "user_input",
                "content": [
                    {
                        "type": "text",
                        "text": canonical_json_bytes(context).decode("utf-8"),
                    }
                ],
            }
        ]
        initial_size = (
            len(canonical_json_bytes(history))
            + len(self._prompt.system_instruction.encode("utf-8"))
            + len(canonical_json_bytes(self._tools))
        )
        if initial_size > _MAX_CONTEXT_BYTES:
            return _failure(
                FailureCode.CONTEXT_TOO_LARGE,
                "The complete article-routing context exceeds the fixed safe size limit.",
            )

        search_index = InMemorySearchIndex(package)
        seen_call_ids: set[str] = set()
        retrieval_calls = 0
        invalid_submits = 0

        for turn in range(1, _MAX_PROVIDER_TURNS + 1):
            if invalid_submits:
                request = {
                    "input": list(history),
                    "system_instruction": self._prompt.system_instruction,
                    "response_format": {
                        "schema": ArticleRoutingResult.model_json_schema(mode="validation")
                    },
                }
            else:
                request = {
                    "input": list(history),
                    "system_instruction": self._prompt.system_instruction,
                    "tools": self._tools,
                }
            try:
                interaction = interaction_create(**request)
            except Exception as error:
                carried_failure = getattr(error, "failure", None)
                if isinstance(carried_failure, TypedFailure):
                    return carried_failure
                return _provider_failure()
            validated = _validate_interaction(interaction, request)
            if validated is None:
                return _provider_failure()
            if on_interaction is not None:
                on_interaction(turn, validated.evidence)

            raw_steps = validated.response["steps"]
            assert isinstance(raw_steps, list)
            steps = [dict(cast(Mapping[str, object], step)) for step in raw_steps]
            history.extend(steps)
            if invalid_submits:
                arguments = _structured_output_arguments(steps)
                if arguments is None:
                    return _schema_failure()
                return _resolve_submission(
                    arguments,
                    package=package,
                    execution_id=execution_id,
                    provenance=self._provenance,
                    resolve_visual_verification=resolve_visual_verification,
                )
            calls = [step for step in steps if step.get("type") == "function_call"]
            if not calls:
                return _failure(
                    FailureCode.ROUTER_NO_SUBMIT,
                    "The article router returned no usable function call.",
                )

            checked_calls: list[tuple[str, str, dict[str, object]]] = []
            for call in calls:
                call_id = call.get("call_id")
                name = call.get("name")
                arguments = _mapping_arguments(call.get("arguments"))
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or call_id.strip() != call_id
                    or call_id in seen_call_ids
                    or not isinstance(name, str)
                    or name not in _KNOWN_TOOL_NAMES
                    or arguments is None
                ):
                    return _schema_failure()
                seen_call_ids.add(call_id)
                checked_calls.append((call_id, name, arguments))

            submit_calls = [call for call in checked_calls if call[1] == "submit_article_routing"]
            if submit_calls:
                if len(checked_calls) != 1:
                    return _schema_failure()
                call_id, _, arguments = submit_calls[0]
                submitted = _resolve_submission(
                    arguments,
                    package=package,
                    execution_id=execution_id,
                    provenance=self._provenance,
                    resolve_visual_verification=resolve_visual_verification,
                )
                if isinstance(submitted, ArticleRoutingResult):
                    return submitted
                if submitted.code not in {
                    FailureCode.ROUTER_SCHEMA_INVALID,
                    FailureCode.ROUTER_REFERENCE_INVALID,
                }:
                    return submitted
                invalid_submits += 1
                if invalid_submits >= 2:
                    return submitted
                history.append(
                    _function_result(
                        "submit_article_routing",
                        call_id,
                        {
                            "code": submitted.code.value,
                            "instruction": (
                                "Return the complete corrected result as the structured JSON "
                                "response. Do not call a tool."
                            ),
                        },
                    )
                )
                continue

            host_results: list[dict[str, object]] = []
            for call_id, name, arguments in checked_calls:
                retrieval_calls += 1
                if retrieval_calls > _MAX_RETRIEVAL_CALLS:
                    return _failure(
                        FailureCode.TOOL_BUDGET_EXHAUSTED,
                        "The article router exhausted the fixed retrieval-call budget.",
                    )
                if name == "search_blocks":
                    result = _search_result(search_index, arguments)
                else:
                    result = _read_result(package, arguments)
                if result is None:
                    return _schema_failure()
                host_results.append(_function_result(name, call_id, result))
            history.extend(host_results)

        return _failure(
            FailureCode.ROUTER_NO_SUBMIT,
            "The article router did not submit an accepted result within five turns.",
        )
