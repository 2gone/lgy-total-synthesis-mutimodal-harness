"""Deterministic Main Article Router over one canonical package."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from tsdaf.contracts.tool_declaration import inline_local_schema
from tsdaf.contracts.execution import (
    PROVIDER_ATTEMPT_AUDIT_VERSION,
    FailureCode,
    FailureDetail,
    TypedFailure,
)
from tsdaf.contracts.package import CanonicalPaperPackage, CanonicalTextBlock
from tsdaf.contracts.reading_units import ReadingUnits, unit_reading_group
from tsdaf.contracts.routing import (
    ArticleRoutingResult,
    ArticleType,
    CoarseTextRole,
    RouteAttribute,
    RouterProvenance,
    RoutingDisposition,
    RoutingRuleError,
    SynthesisType,
    TargetClass,
    VisualDecision,
)
from tsdaf.contracts.schema import canonical_json_bytes, contract_schema_sha256
from tsdaf.contracts.tool_read import SEARCH_MATCHING_DESCRIPTION
from tsdaf.contracts.transport import MAX_REQUEST_BYTES
from tsdaf.lanes.document.page_furniture import package_continuity
from tsdaf.lanes.document.search_index import (
    SEARCH_SEMANTICS_VERSION,
    InMemorySearchIndex,
    SearchMode,
    UnsupportedSearchQuery,
)

__all__ = ("MainArticleRouter",)

_ROUTER_ID = "main-article-router"
_ROUTER_VERSION = "1.5.0"
_PROVIDER_ID = "lpzproxy-openai-compatible"
_MODEL_ID = "gemini-3.8-flash-high"
_PROVIDER_PROFILE_ID = "gemini-3.8-flash-high-chat-completions-v1"
_PROVIDER_PROFILE_VERSION = "3.1.0"
_PROMPT_ID = "article-router"
_PROMPT_VERSION = "1.5.0"
_CONTRACT_SCHEMA_BUNDLE_VERSION = "tsdaf.contract-schema-bundle.v1"
_TOOL_SCHEMA_VERSION = "tsdaf.article-router-tools.v3"
# v3: a block may carry `unit`, the reading unit it belongs to (E156); the tools'
# own parameters did not change, so the tool schema version stands.
_CONTEXT_SCHEMA_VERSION = "tsdaf.article-router-context.v5"
# The gateway's measured body ceiling, which is not the Router's to own: the detail
# lane's own figure route has to respect the same wall, and two copies of a measured
# number are two numbers. What was measured, and why this value, is at its definition.
# The papers this once refused are now brought under it by the thumbnail ladder below.
_MAX_CONTEXT_BYTES = MAX_REQUEST_BYTES
# The Router answers one question per image -- what work does this image carry -- and a
# thumbnail is enough to answer it. The literature lanes that read a figure (V1, and V3
# for the detail lane) still receive the full asset.
#
# The ladder the image budget walks down (L5-05). 512 is what a Router turn has always
# sent and what 197 of the 200 papers of the c3 batch fit inside; the three that did not
# are brought under the guard at 320. Image count does not predict the bill -- measured
# on that batch at 512 px, a 149-figure paper cost 2.99 MB and a 71-figure paper 3.80 MB
# -- so the budget is spent in bytes, not in figures. Below 256 a figure stops being a
# picture of anything, so a paper that does not fit there fails rather than being sent
# something unreadable.
_THUMBNAIL_EDGE_LADDER = (512, 384, 320, 256)
_THUMBNAIL_LONGEST_EDGE = _THUMBNAIL_EDGE_LADDER[0]
_THUMBNAIL_MEDIA_TYPE = "image/jpeg"
_SUPPORTED_IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png"})
_PROMPT_NAMES = ("manifest.toml", "system.md", "task.md", "taxonomy.md")
_TAXONOMY_ENUMS = (
    ArticleType,
    SynthesisType,
    TargetClass,
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


def _failure(
    code: FailureCode, message: str, details: tuple[FailureDetail, ...] = ()
) -> TypedFailure:
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message=message,
        request_id=None,
        details=details,
    )


def _schema_failure(details: tuple[FailureDetail, ...] = ()) -> TypedFailure:
    return _failure(
        FailureCode.ROUTER_SCHEMA_INVALID,
        "The article router received an invalid tool call or routing schema.",
        details,
    )


def _reference_failure(details: tuple[FailureDetail, ...] = ()) -> TypedFailure:
    return _failure(
        FailureCode.ROUTER_REFERENCE_INVALID,
        "The submitted routing result does not match the canonical package references.",
        details,
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


# The final contract owns scientific fields. A private submission omits only values
# the host already owns or can derive from the model's explicit scientific selections.
_HOST_FIELDS = (
    "schema_version",
    "paper_id",
    "execution_id",
    "canonical_package_sha256",
    "provenance",
)
_CORE_FIELD = "core_text_block_ids"


def _submission_schema() -> dict[str, Any]:
    schema = ArticleRoutingResult.model_json_schema(mode="validation")
    for node, omitted in ((schema, _HOST_FIELDS), (schema["$defs"]["RouteGroup"], (_CORE_FIELD,))):
        for key in omitted:
            node["properties"].pop(key)
        node["required"] = [key for key in node["required"] if key not in omitted]
    selection = schema["properties"]["coarse_text_selections"]
    selection["description"] = selection["description"].replace(
        "The ROUTE_CORE subsequence must equal that group's core_text_block_ids.",
        "The host derives each group's core text list from its ROUTE_CORE selections.",
    )
    # Remove unreachable host-only definitions from what the model actually sees.
    definitions = schema.pop("$defs")
    needed: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in needed:
                    needed[name] = definitions[name]
                    visit(definitions[name])
            for part in value.values():
                visit(part)
        elif isinstance(value, list):
            for part in value:
                visit(part)

    visit(schema)
    schema["$defs"] = needed
    return inline_local_schema(schema)


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
        "description": "Inclusive page indexes [first, last], in ascending order.",
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
            "description": (
                "Search this canonical paper's clean blocks. "
                + SEARCH_MATCHING_DESCRIPTION
                + "Results distinguish MATCHES, "
                "NO_MATCHES, and UNSUPPORTED_QUERY; no matches is only within the stated mode "
                "and page/section filters, not proof that a scientific concept is absent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": [mode.value for mode in SearchMode],
                        "default": SearchMode.TERMS.value,
                    },
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
            "description": (
                "Submit the scientific routing decisions and candidates for host verification."
            ),
            "strict": True,
            "parameters": _submission_schema(),
        },
    ]


# Declaration and execution use the same schema, including defaults and strict JSON
# types. Only contextual/cross-field checks remain outside JSON Schema.
_ARGUMENT_VALIDATORS = {
    tool["name"]: Draft202012Validator(tool["parameters"]) for tool in _tool_declarations()
}


def _argument_defaults(name: str, arguments: Mapping[str, object]) -> dict[str, Any] | None:
    validator = _ARGUMENT_VALIDATORS[name]
    if not validator.is_valid(arguments):
        return None
    defaults = {
        key: spec["default"]
        for key, spec in validator.schema["properties"].items()
        if "default" in spec
    }
    return {**defaults, **arguments}


def _bind_submission(
    arguments: Mapping[str, object],
    *,
    package: CanonicalPaperPackage,
    execution_id: str,
    provenance: RouterProvenance,
    readable_block_ids: tuple[str, ...] | None = None,
    delivered_caption_blocks: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, Any] | TypedFailure:
    validator = _ARGUMENT_VALIDATORS["submit_article_routing"]
    errors = list(validator.iter_errors(arguments))
    if errors:
        details: list[FailureDetail] = []
        for error in errors:
            path = ".".join(map(str, error.absolute_path))
            if error.validator == "required":
                details.extend(
                    FailureDetail(key=".".join(filter(None, (path, key))), value="missing")
                    for key in error.validator_value
                    if key not in error.instance
                )
            else:
                details.append(FailureDetail(key=path or "submission", value=str(error.validator)))
        return _schema_failure(tuple(details[:8]))
    scientific = cast(dict[str, Any], dict(arguments))
    # Body rows and visual-caption metadata are separate delivered capabilities.
    # A caption outside the body cut may anchor its selected visual's route group,
    # but a post-cut body row (or another visual's caption) was never delivered as
    # text for this route group. Historical calls without a cut retain full scope.
    body_ids = set(
        package.clean_view.included_block_ids if readable_block_ids is None else readable_block_ids
    )
    caption_blocks = delivered_caption_blocks or {}
    selected_visuals = {
        group["route_group_id"]: group["ordered_visual_ids"] for group in scientific["route_groups"]
    }
    for index, selection in enumerate(scientific["coarse_text_selections"]):
        block_id = selection["block_id"]
        if block_id in body_ids:
            continue
        if any(
            block_id in caption_blocks.get(visual_id, ())
            for visual_id in selected_visuals.get(selection["route_group_id"], ())
        ):
            continue
        return _reference_failure(
            (
                FailureDetail(
                    key=f"coarse_text_selections.{index}.block_id",
                    value=(
                        "block was not delivered as readable body or a caption of a selected visual"
                    ),
                ),
            )
        )
    groups = []
    for group in scientific["route_groups"]:
        # Keep submitted order so the final contract can reject an out-of-order
        # selection instead of silently sorting an invalid scientific submission.
        chosen = [
            row["block_id"]
            for row in scientific["coarse_text_selections"]
            if row["route_group_id"] == group["route_group_id"] and row["role"] == "ROUTE_CORE"
        ]
        groups.append({**group, _CORE_FIELD: chosen})
    owned = dict(
        zip(
            _HOST_FIELDS,
            (
                ArticleRoutingResult.model_json_schema()["properties"]["schema_version"]["const"],
                package.manifest.paper_id,
                execution_id,
                package.canonical_content_sha256,
                provenance.model_dump(mode="json"),
            ),
            strict=True,
        )
    )
    return {**scientific, **owned, "route_groups": groups}


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


def _unit_label(units: ReadingUnits | None, block_id: str) -> dict[str, object] | None:
    """The unit this block belongs to, as a reply names it: kind, the first block that
    names the unit, and whose compound it is. Only for a unit of more than one block --
    a block that is its own unit tells the model nothing it cannot see -- and only
    when the document was read into units (E156)."""
    unit = units.unit_containing(block_id) if units is not None else None
    if unit is None or len(unit.block_ids) < 2:
        return None
    return {
        "kind": unit.kind.value,
        "first_block_id": unit.first_block_id,
        "compound_label": unit.compound_label,
    }


def _block_payload(
    block: CanonicalTextBlock, unit: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "block_id": block.block_id,
        "page_index": block.page_index,
        "reading_order": block.reading_order,
        "block_type": block.block_type.value,
        "section_path": list(block.section_path),
        "raw_text": block.raw_text,
        **({"unit": unit} if unit is not None else {}),
    }


def _context_payload(
    package: CanonicalPaperPackage,
    execution_id: str,
    provenance: RouterProvenance,
    readable_block_ids: tuple[str, ...],
    units: ReadingUnits | None = None,
) -> dict[str, object]:
    blocks_by_id = {block.block_id: block for block in package.blocks}
    groups_by_id = {group.visual_group_id: group for group in package.visual_groups}
    decorative_ids = {record.visual_id for record in package.visual_suppressions}
    blocks = [
        _block_payload(blocks_by_id[block_id], _unit_label(units, block_id))
        for block_id in readable_block_ids
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
        "blocks": blocks,
        "visual_assets": visual_assets,
        "constraints": {
            "invent_omitted_or_suppressed_blocks": False,
            "invent_omitted_or_suppressed_visuals": False,
        },
    }


def _thumbnail(payload: bytes, longest_edge: int = _THUMBNAIL_LONGEST_EDGE) -> bytes:
    """One image, small enough to be looked at and too small to be parsed from."""

    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            flattened = image.convert("RGB")
    except (OSError, UnidentifiedImageError, ValueError):
        raise ValueError("visual payload is not a valid supported image") from None
    flattened.thumbnail((longest_edge, longest_edge), Image.Resampling.LANCZOS)
    buffer = BytesIO()
    flattened.save(buffer, format="JPEG", quality=80, optimize=True)
    return buffer.getvalue()


def _visual_content_parts(
    package: CanonicalPaperPackage,
    visual_payloads: Mapping[str, bytes],
    longest_edge: int = _THUMBNAIL_LONGEST_EDGE,
) -> list[dict[str, object]]:
    """Show the Router each visual it is asked to classify, named and downscaled.

    It used to classify from captions, page numbers and byte sizes. On the live
    Ineleganolide run that reached the right answer for four of the five images that
    carry content and called the fifth -- a crossed-out epimerization, uncaptioned --
    `OTHER`. `NEEDS_VISUAL_VERIFICATION` exists for an image the metadata cannot settle
    and was returned zero times, which is what a confident wrong answer looks like: the
    escalation cannot fire when the model does not feel uncertain. The escalation stays;
    this removes the guess that precedes it.

    Each image is preceded by a text part naming its `visual_id`, the shape the visual
    verification batches already use, so the model can tie a picture to a row of the
    inventory it is answering about. Only bytes the canonical package vouches for are
    ever encoded: a payload whose length or digest disagrees with the asset it claims to
    be is refused rather than sent.
    """

    expected = tuple(visual.visual_id for visual in package.visual_assets)
    if tuple(visual_payloads) != expected:
        raise ValueError("visual payload inventory must exactly match the canonical package")
    parts: list[dict[str, object]] = []
    for visual in package.visual_assets:
        payload = visual_payloads[visual.visual_id]
        if type(payload) is not bytes:
            raise TypeError("visual payloads must contain bytes")
        if visual.asset.media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
            raise ValueError("visual payload media type is unsupported")
        if (
            len(payload) != visual.asset.byte_size
            or hashlib.sha256(payload).hexdigest() != visual.asset.sha256
        ):
            raise ValueError("visual payload does not match its canonical asset identity")
        parts.extend(
            (
                {
                    "type": "text",
                    "text": canonical_json_bytes({"visual_id": visual.visual_id}).decode("utf-8"),
                },
                {
                    "type": "image",
                    "mime_type": _THUMBNAIL_MEDIA_TYPE,
                    "data": base64.b64encode(_thumbnail(payload, longest_edge)).decode("ascii"),
                    "resolution": "medium",
                },
            )
        )
    return parts


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


def _search_arguments(
    arguments: Mapping[str, object],
) -> tuple[str, tuple[int, int] | None, tuple[str, ...] | None, int, SearchMode] | None:
    parsed = _argument_defaults("search_blocks", arguments)
    if parsed is None:
        return None
    # JSON Schema integer includes integral JSON numbers (1.0); represent these
    # validated values as Python ints for slicing/range, without accepting strings.
    pages = parsed["page_range"]
    if pages is not None and pages[0] > pages[1]:
        return None
    sections = parsed["section_filter"]
    return (
        parsed["query"],
        (int(pages[0]), int(pages[1])) if pages is not None else None,
        tuple(sections) if sections is not None else None,
        int(parsed["limit"]),
        SearchMode(parsed["mode"]),
    )


def _read_arguments(arguments: Mapping[str, object]) -> tuple[tuple[str, ...], int] | None:
    parsed = _argument_defaults("read_blocks", arguments)
    return None if parsed is None else (tuple(parsed["block_ids"]), int(parsed["neighbor_count"]))


def _search_result(
    index: InMemorySearchIndex,
    arguments: Mapping[str, object],
    units: ReadingUnits | None = None,
) -> dict[str, object] | None:
    checked = _search_arguments(arguments)
    if checked is None:
        return None
    query, page_range, section_filter, limit, mode = checked
    try:
        hits = index.search_blocks(
            query,
            page_range=page_range,
            section_filter=section_filter,
            limit=limit,
            mode=mode,
        )
    except UnsupportedSearchQuery as error:
        return {"status": "UNSUPPORTED_QUERY", "mode": mode.value, "reason": str(error), "hits": []}
    except ValueError:
        return None
    return {
        "status": "MATCHES" if hits else "NO_MATCHES",
        "mode": mode.value,
        "hits": [
            {
                "block_id": hit.block_id,
                "page_index": hit.page_index,
                "section_path": list(hit.section_path),
                "raw_text": hit.raw_text,
                "match_kind": hit.match_kind,
                "matched_terms": list(hit.matched_terms),
                **(
                    {"unit": unit} if (unit := _unit_label(units, hit.block_id)) is not None else {}
                ),
            }
            for hit in hits
        ],
    }


def _read_result(
    package: CanonicalPaperPackage,
    arguments: Mapping[str, object],
    readable_block_ids: tuple[str, ...],
    units: ReadingUnits | None = None,
    partners: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, object] | None:
    """The blocks asked for, each with the passage it belongs to (E156): the rest of
    its reading unit and the other half of a paragraph the layout cut, from the same
    `unit_reading_group` the detail lane delivers by. `neighbor_count` stays what it
    was -- the narrative around a block, which is a different question from the block
    being part of a passage."""
    checked = _read_arguments(arguments)
    if checked is None:
        return None
    requested_ids, neighbor_count = checked
    included_ids = readable_block_ids
    positions = {block_id: index for index, block_id in enumerate(included_ids)}
    if any(block_id not in positions for block_id in requested_ids):
        return None
    selected_positions: set[int] = set()
    for block_id in requested_ids:
        position = positions[block_id]
        lower = max(0, position - neighbor_count)
        upper = min(len(included_ids), position + neighbor_count + 1)
        selected_positions.update(range(lower, upper))
        selected_positions.update(
            positions[member]
            for member in unit_reading_group(
                units, block_id, partners=partners or {}, readable=positions
            )
        )
    blocks_by_id = {block.block_id: block for block in package.blocks}
    return {
        "blocks": [
            _block_payload(
                blocks_by_id[included_ids[position]],
                _unit_label(units, included_ids[position]),
            )
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


def _validation_details(error: ValidationError) -> tuple[FailureDetail, ...]:
    """Expose validator locations/reasons, never submitted values or exception reprs."""
    schema = ArticleRoutingResult.model_json_schema(mode="validation")
    allowed_fields = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        allowed_fields.update(definition.get("properties", {}))
    details: dict[str, str] = {}
    for item in error.errors(include_input=False, include_context=True, include_url=False):
        rule_error = item.get("ctx", {}).get("error")
        if isinstance(rule_error, RoutingRuleError):
            details[rule_error.field_path] = rule_error.reason
            continue
        path = (
            ".".join(
                str(part) if isinstance(part, int) or part in allowed_fields else "<unknown_field>"
                for part in item["loc"]
            )
            or "result"
        )
        details[path] = item["type"]
    return tuple(FailureDetail(key=path, value=reason) for path, reason in sorted(details.items()))


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
    except ValidationError as error:
        details = _validation_details(error)
        if _raw_references_diverge(arguments, package):
            return _reference_failure(details)
        return _schema_failure(details)
    for field, expected in (
        ("paper_id", package.manifest.paper_id),
        ("execution_id", execution_id),
        ("canonical_package_sha256", package.canonical_content_sha256),
        ("provenance", provenance),
    ):
        if getattr(result, field) != expected:
            return _reference_failure(
                (FailureDetail(key=field, value="does not match the supplied identity/provenance"),)
            )
    try:
        result.validate_against(package)
    except RoutingRuleError as error:
        return _reference_failure((FailureDetail(key=error.field_path, value=error.reason),))
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
    """Compile the article context and run tools until a valid submission or failure."""

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
            "context_schema_version": _CONTEXT_SCHEMA_VERSION,
            "search_semantics_version": SEARCH_SEMANTICS_VERSION,
            "provider_attempt_audit_version": PROVIDER_ATTEMPT_AUDIT_VERSION,
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
        on_attempt_audit: Callable[[object, bytes], None] | None = None,
        resolve_visual_verification: (
            Callable[[Mapping[str, object]], ArticleRoutingResult | TypedFailure] | None
        ) = None,
        visual_payloads: Mapping[str, bytes] | None = None,
        readable_block_ids: tuple[str, ...] | None = None,
        units: ReadingUnits | None = None,
    ) -> ArticleRoutingResult | TypedFailure:
        """Route one package.

        `readable_block_ids` is the part of the cleaned document the Router is shown,
        searches and reads: the narrative part when the experimental split has run,
        the whole cleaned document when it has not. It is a prefix of the clean view
        in reading order, derived once by `reading_units.partition_clean_view`.
        `units` is that reading itself, when there is one: each shown block, each
        search hit and each block read is then labelled with the unit it belongs to,
        and a read of one block answers with its passage whole (E156).
        """

        if not isinstance(package, CanonicalPaperPackage):
            raise TypeError("package must be a CanonicalPaperPackage")
        if readable_block_ids is None:
            readable_block_ids = package.clean_view.included_block_ids
        elif type(readable_block_ids) is not tuple or not set(readable_block_ids) <= set(
            package.clean_view.included_block_ids
        ):
            raise ValueError("readable block ids must be cleaned blocks of the package")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise ValueError("execution_id must be a nonempty string")
        if not callable(interaction_create):
            raise TypeError("interaction_create must be callable")
        if on_interaction is not None and not callable(on_interaction):
            raise TypeError("on_interaction must be callable")
        if on_attempt_audit is not None and not callable(on_attempt_audit):
            raise TypeError("on_attempt_audit must be callable")
        if resolve_visual_verification is not None and not callable(resolve_visual_verification):
            raise TypeError("resolve_visual_verification must be callable")
        if visual_payloads is not None and not isinstance(visual_payloads, Mapping):
            raise TypeError("visual_payloads must be a mapping")
        if units is not None and not isinstance(units, ReadingUnits):
            raise TypeError("units must be a ReadingUnits or None")

        context = _context_payload(
            package, execution_id, self._provenance, readable_block_ids, units
        )
        delivered_caption_blocks = {
            row["visual_id"]: tuple(row["group"]["caption_block_ids"])
            for row in cast(list[dict[str, Any]], context["visual_assets"])
        }
        # The inventory stays text: a base64 blob inside the context JSON would be a
        # payload the model reads as data rather than as a picture. The images ride
        # beside it as their own content parts.
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": canonical_json_bytes(context).decode("utf-8"),
            }
        ]
        fixed_size = len(self._prompt.system_instruction.encode("utf-8")) + len(
            canonical_json_bytes(self._tools)
        )

        def sized(parts: Sequence[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
            history: list[dict[str, object]] = [
                {"type": "user_input", "content": [*content, *parts]}
            ]
            return history, len(canonical_json_bytes(history)) + fixed_size

        # The figures get what the text and the tools leave, and they are made to fit by
        # lowering their longest edge (L5-05). Nothing changes for a paper that already
        # fits at 512: the first rung is what the Router has always sent.
        history, initial_size = sized(())
        if visual_payloads is not None:
            for longest_edge in _THUMBNAIL_EDGE_LADDER:
                history, initial_size = sized(
                    _visual_content_parts(package, visual_payloads, longest_edge)
                )
                if initial_size <= _MAX_CONTEXT_BYTES:
                    break
        if initial_size > _MAX_CONTEXT_BYTES:
            return _failure(
                FailureCode.CONTEXT_TOO_LARGE,
                "The complete article-routing context exceeds the one-request guard.",
            )

        search_index = InMemorySearchIndex(package, included_block_ids=readable_block_ids)
        # The pairing table of the paragraphs the layout cut, computed in the one place
        # that computes it (E156); a read of one half answers with the other.
        partners = package_continuity(package).partners
        seen_call_ids: set[str] = set()
        interaction_index = 0

        while True:
            request = {
                "input": list(history),
                "system_instruction": self._prompt.system_instruction,
                "tools": self._tools,
            }
            try:
                interaction = interaction_create(**request)
            except Exception as error:
                if on_attempt_audit is not None:
                    on_attempt_audit(error, _expected_adapter_request_bytes(request))
                carried_failure = getattr(error, "failure", None)
                if isinstance(carried_failure, TypedFailure):
                    return carried_failure
                return _provider_failure()
            if on_attempt_audit is not None:
                on_attempt_audit(interaction, _expected_adapter_request_bytes(request))
            validated = _validate_interaction(interaction, request)
            if validated is None:
                return _provider_failure()
            if on_interaction is not None:
                on_interaction(interaction_index + 1, validated.evidence)
            interaction_index += 1

            raw_steps = validated.response["steps"]
            assert isinstance(raw_steps, list)
            steps = [dict(cast(Mapping[str, object], step)) for step in raw_steps]
            history.extend(steps)
            calls = [step for step in steps if step.get("type") == "function_call"]
            if not calls:
                return _failure(
                    FailureCode.ROUTER_NO_SUBMIT,
                    "The article router returned no usable function call.",
                )

            checked_calls: list[tuple[str, str, dict[str, object] | None]] = []
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
                    or not name.strip()
                ):
                    return _schema_failure()
                seen_call_ids.add(call_id)
                checked_calls.append((call_id, name, arguments))

            for call_id, name, arguments in checked_calls:
                result: dict[str, object] | None = None
                failure: TypedFailure | None = None
                if arguments is None:
                    failure = _schema_failure()
                elif name == "submit_article_routing":
                    bound = _bind_submission(
                        arguments,
                        package=package,
                        execution_id=execution_id,
                        provenance=self._provenance,
                        readable_block_ids=readable_block_ids,
                        delivered_caption_blocks=delivered_caption_blocks,
                    )
                    submitted = (
                        bound
                        if isinstance(bound, TypedFailure)
                        else _resolve_submission(
                            bound,
                            package=package,
                            execution_id=execution_id,
                            provenance=self._provenance,
                            resolve_visual_verification=resolve_visual_verification,
                        )
                    )
                    if isinstance(submitted, ArticleRoutingResult):
                        return submitted
                    if submitted.code not in {
                        FailureCode.ROUTER_SCHEMA_INVALID,
                        FailureCode.ROUTER_REFERENCE_INVALID,
                    }:
                        return submitted
                    failure = submitted
                elif name == "search_blocks":
                    result = _search_result(search_index, arguments, units)
                elif name == "read_blocks":
                    result = _read_result(package, arguments, readable_block_ids, units, partners)
                if result is None:
                    if (
                        failure is None
                        and name in {"search_blocks", "read_blocks"}
                        and arguments is not None
                    ):
                        validator = _ARGUMENT_VALIDATORS[name]
                        details = tuple(
                            FailureDetail(
                                key=".".join(map(str, error.absolute_path)) or name,
                                value=str(error.validator),
                            )
                            for error in list(validator.iter_errors(arguments))[:8]
                        )
                        if (
                            not details
                            and name == "search_blocks"
                            and _search_arguments(arguments) is None
                        ):
                            details = (
                                FailureDetail(
                                    key="page_range",
                                    value=validator.schema["properties"]["page_range"][
                                        "description"
                                    ],
                                ),
                            )
                        failure = _schema_failure(details)
                    failure = failure or _schema_failure()
                    result = {
                        "code": failure.code.value,
                        "details": [detail.model_dump(mode="json") for detail in failure.details],
                    }
                history.append(_function_result(name, call_id, result))
