"""Isolated visual verification for unresolved article-routing decisions."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import cast

from tsdaf.contracts.execution import FailureCode, TypedFailure
from tsdaf.contracts.package import CanonicalPaperPackage, CanonicalVisualAsset
from tsdaf.contracts.routing import ArticleRoutingResult, RoutingDisposition, VisualDecision
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.document.package_loader import PackageLoadError, load_paper_package

__all__ = ("resolve_visual_verification",)

_MODEL_ID = "gemini-3.8-flash-high"
_MAX_BATCH_SIZE = 4
_TASK_INSTRUCTION = (
    "Perform high-level visual category verification only. Do not perform reaction parsing, "
    "crop or panel localization, or SMILES extraction."
)
_TRIAGE_DECISIONS = tuple(
    decision.value
    for decision in VisualDecision
    if decision is not VisualDecision.NEEDS_VISUAL_VERIFICATION
)
_TRIAGE_DECISION_SET = frozenset(_TRIAGE_DECISIONS)


def _failure(code: FailureCode, message: str) -> TypedFailure:
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message=message,
        request_id=None,
        details=(),
    )


def _reference_failure() -> TypedFailure:
    return _failure(
        FailureCode.ROUTER_REFERENCE_INVALID,
        "The visual-verification submission does not match the canonical visual inventory.",
    )


def _unresolved_failure() -> TypedFailure:
    return _failure(
        FailureCode.VISUAL_VERIFICATION_UNRESOLVED,
        "The visual-verification result could not be resolved safely.",
    )


def _provider_failure() -> TypedFailure:
    return _failure(
        FailureCode.PROVIDER_REJECTED,
        "The visual-verification provider rejected the interaction.",
    )


def _package_failure(code: FailureCode) -> TypedFailure:
    messages = {
        FailureCode.PACKAGE_INVALID: ("The visual-verification package could not be validated."),
        FailureCode.ASSET_MISSING: ("A required visual-verification package asset is missing."),
        FailureCode.HASH_MISMATCH: (
            "A visual-verification package asset failed integrity verification."
        ),
    }
    return _failure(
        code,
        messages.get(code, "The visual-verification package could not be loaded."),
    )


def _mapping_rows(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list | tuple):
        return None
    rows: list[dict[str, object]] = []
    for value_row in value:
        if not isinstance(value_row, Mapping) or any(not isinstance(key, str) for key in value_row):
            return None
        rows.append(dict(cast(Mapping[str, object], value_row)))
    return rows


def _visual_preflight(
    submission: Mapping[str, object], package: CanonicalPaperPackage
) -> tuple[list[dict[str, object]], tuple[int, ...]] | TypedFailure:
    decisions = _mapping_rows(submission.get("visual_decisions"))
    if decisions is None:
        return _reference_failure()
    pairs: list[tuple[str, str]] = []
    flagged_positions: list[int] = []
    for position, decision in enumerate(decisions):
        visual_id = decision.get("visual_id")
        visual_group_id = decision.get("visual_group_id")
        if not isinstance(visual_id, str) or not isinstance(visual_group_id, str):
            return _reference_failure()
        pairs.append((visual_id, visual_group_id))
        if (
            decision.get("initial_decision") == VisualDecision.NEEDS_VISUAL_VERIFICATION.value
            and decision.get("triage_result") is None
        ):
            flagged_positions.append(position)
    expected_pairs = [
        (visual.visual_id, visual.visual_group_id) for visual in package.visual_assets
    ]
    if len(pairs) != len(set(pairs)) or pairs != expected_pairs:
        return _reference_failure()
    return decisions, tuple(flagged_positions)


def _candidate_submission(
    submission: Mapping[str, object],
    decisions: list[dict[str, object]],
    flagged_positions: tuple[int, ...],
    package: CanonicalPaperPackage,
) -> tuple[dict[str, object], dict[int, str], ArticleRoutingResult] | TypedFailure:
    candidates: dict[int, str] = {}
    candidate_decisions = copy.deepcopy(decisions)
    for position in flagged_positions:
        candidate = candidate_decisions[position].get("final_decision")
        if not isinstance(candidate, str) or candidate not in _TRIAGE_DECISION_SET:
            return _unresolved_failure()
        candidates[position] = candidate
        candidate_decisions[position]["triage_result"] = candidate
    candidate_payload = copy.deepcopy(dict(submission))
    candidate_payload["visual_decisions"] = candidate_decisions
    try:
        candidate_result = ArticleRoutingResult.model_validate(candidate_payload, strict=False)
        candidate_result.validate_against(package)
    except ValueError:
        return _unresolved_failure()
    return candidate_payload, candidates, candidate_result


def _neighbor_rows(
    package: CanonicalPaperPackage, visual: CanonicalVisualAsset
) -> list[dict[str, str]]:
    groups_by_id = {group.visual_group_id: group for group in package.visual_groups}
    blocks_by_id = {block.block_id: block for block in package.blocks}
    group = groups_by_id[visual.visual_group_id]
    if group.caption_block_ids:
        selected = [blocks_by_id[block_id] for block_id in group.caption_block_ids[:2]]
    else:
        included = set(package.clean_view.included_block_ids)
        canonical_order = {block.block_id: index for index, block in enumerate(package.blocks)}
        anchor_block = next(
            (block for block in package.blocks if block.source.source_asset_ref == visual.asset),
            None,
        )
        anchor_reading_order = anchor_block.reading_order if anchor_block is not None else 0
        candidates = [
            block
            for block in package.blocks
            if block.page_index == visual.page_index
            and block.block_id in included
            and block.source.source_asset_ref is None
            and block.raw_text
        ]

        def distance_key(block_id: str) -> tuple[int, int, int]:
            block = blocks_by_id[block_id]
            block_native = block.source.native_block_index
            visual_native = visual.source.native_block_index
            if block_native is not None and visual_native is not None:
                return (
                    abs(block_native - visual_native),
                    block_native,
                    canonical_order[block.block_id],
                )
            return (
                abs(block.reading_order - anchor_reading_order),
                block.reading_order,
                canonical_order[block.block_id],
            )

        nearest = sorted(candidates, key=lambda block: distance_key(block.block_id))[:2]
        selected = sorted(nearest, key=lambda block: canonical_order[block.block_id])
    return [{"block_id": block.block_id, "raw_text": block.raw_text} for block in selected]


def _response_format(batch_size: int) -> dict[str, object]:
    return {
        "type": "text",
        "mime_type": "application/json",
        "schema": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "minItems": batch_size,
                    "maxItems": batch_size,
                    "items": {
                        "type": "object",
                        "properties": {
                            "visual_id": {"type": "string"},
                            "decision": {
                                "type": "string",
                                "enum": list(_TRIAGE_DECISIONS),
                            },
                        },
                        "required": ["visual_id", "decision"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["decisions"],
            "additionalProperties": False,
        },
    }


def _batch_request(
    *,
    package: CanonicalPaperPackage,
    positions: tuple[int, ...],
    candidates: Mapping[int, str],
    payloads: Mapping[str, bytes],
) -> dict[str, object]:
    content: list[dict[str, object]] = [{"type": "text", "text": _TASK_INSTRUCTION}]
    groups_by_id = {group.visual_group_id: group for group in package.visual_groups}
    for position in positions:
        visual = package.visual_assets[position]
        group = groups_by_id[visual.visual_group_id]
        metadata = {
            "visual_id": visual.visual_id,
            "candidate_category": candidates[position],
            "group_caption": group.caption_text,
            "native_caption": visual.native_caption,
            "page_index": visual.page_index,
            "neighboring_blocks": _neighbor_rows(package, visual),
        }
        content.extend(
            (
                {
                    "type": "text",
                    "text": canonical_json_bytes(metadata).decode("utf-8"),
                },
                {
                    "type": "image",
                    "mime_type": visual.asset.media_type,
                    "data": base64.b64encode(payloads[visual.visual_id]).decode("ascii"),
                    "resolution": "medium",
                },
            )
        )
    return {
        "input": [{"type": "user_input", "content": content}],
        "response_format": _response_format(len(positions)),
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
    response_format = request.get("response_format")
    if response_format is not None:
        payload["response_format"] = response_format
    return canonical_json_bytes(payload)


def _optional_token(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _validated_output_text(interaction: object, request: Mapping[str, object]) -> str | None:
    request_bytes = getattr(interaction, "request_bytes", None)
    response_bytes = getattr(interaction, "response_bytes", None)
    if (
        not isinstance(request_bytes, bytes)
        or not isinstance(response_bytes, bytes)
        or request_bytes != _expected_adapter_request_bytes(request)
    ):
        return None
    try:
        response_value: object = json.loads(response_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(response_value, dict):
        return None
    response = cast(dict[str, object], response_value)
    try:
        normalized_response = canonical_json_bytes(response)
    except (TypeError, ValueError, UnicodeError):
        return None
    raw_steps = response.get("steps")
    if (
        normalized_response != response_bytes
        or not isinstance(response.get("id"), str)
        or not response["id"]
        or not isinstance(response.get("status"), str)
        or not response["status"]
        or not isinstance(raw_steps, list)
        or not raw_steps
        or any(
            not isinstance(step, Mapping) or not isinstance(step.get("type"), str)
            for step in raw_steps
        )
    ):
        return None
    output_text = response.get("output_text")
    if not isinstance(output_text, str) or getattr(interaction, "output_text", None) != output_text:
        return None
    usage_value = response.get("usage")
    usage = cast(Mapping[str, object], usage_value) if isinstance(usage_value, Mapping) else {}
    scalar_pairs = (
        (getattr(interaction, "interaction_id", None), response.get("id")),
        (
            getattr(interaction, "total_input_tokens", None),
            _optional_token(usage.get("total_input_tokens")),
        ),
        (
            getattr(interaction, "total_output_tokens", None),
            _optional_token(usage.get("total_output_tokens")),
        ),
        (
            getattr(interaction, "total_tokens", None),
            _optional_token(usage.get("total_tokens")),
        ),
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
    elapsed_ms = getattr(interaction, "elapsed_ms", None)
    attempts = getattr(interaction, "transport_attempts", None)
    if (
        (request_id is not None and not isinstance(request_id, str))
        or isinstance(elapsed_ms, bool)
        or not isinstance(elapsed_ms, int | float)
        or not math.isfinite(elapsed_ms)
        or elapsed_ms < 0
        or isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 1
    ):
        return None
    return output_text


def _parsed_decisions(output_text: str, expected_ids: tuple[str, ...]) -> tuple[str, ...] | None:
    try:
        value: object = json.loads(output_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or set(value) != {"decisions"}:
        return None
    rows = _mapping_rows(value.get("decisions"))
    if rows is None or len(rows) != len(expected_ids):
        return None
    decisions_by_id: dict[str, str] = {}
    expected = set(expected_ids)
    for row in rows:
        if set(row) != {"visual_id", "decision"}:
            return None
        visual_id = row.get("visual_id")
        decision = row.get("decision")
        if (
            not isinstance(visual_id, str)
            or not isinstance(decision, str)
            or decision not in _TRIAGE_DECISION_SET
        ):
            return None
        if visual_id not in expected or visual_id in decisions_by_id:
            return None
        decisions_by_id[visual_id] = decision
    if set(decisions_by_id) != expected:
        return None
    return tuple(decisions_by_id[visual_id] for visual_id in expected_ids)


def _completed_result(
    *,
    submission: dict[str, object],
    decisions: list[dict[str, object]],
    candidate_result: ArticleRoutingResult,
    resolved: Mapping[int, str],
    package: CanonicalPaperPackage,
) -> ArticleRoutingResult | TypedFailure:
    working = copy.deepcopy(submission)
    working_decisions = copy.deepcopy(decisions)
    for position, decision in resolved.items():
        working_decisions[position]["triage_result"] = decision
        working_decisions[position]["final_decision"] = decision
    working["visual_decisions"] = working_decisions

    membership_ids = {
        visual_id
        for group in candidate_result.route_groups
        for visual_id in group.ordered_visual_ids
    }
    for position, decision in resolved.items():
        visual_id = package.visual_assets[position].visual_id
        if decision == VisualDecision.FORWARD_ROUTE.value and visual_id not in membership_ids:
            return _unresolved_failure()

    route_groups = _mapping_rows(working.get("route_groups"))
    selections = _mapping_rows(working.get("coarse_text_selections"))
    if route_groups is None or selections is None:
        return _unresolved_failure()
    resolved_by_id = {
        package.visual_assets[position].visual_id: decision
        for position, decision in resolved.items()
    }
    retained_groups: list[dict[str, object]] = []
    old_to_new: dict[str, str] = {}
    for group in route_groups:
        old_route_id = group.get("route_group_id")
        raw_members = group.get("ordered_visual_ids")
        if not isinstance(old_route_id, str) or not isinstance(raw_members, list | tuple):
            return _unresolved_failure()
        members = [member for member in raw_members if isinstance(member, str)]
        if len(members) != len(raw_members):
            return _unresolved_failure()
        retained_members = [
            member
            for member in members
            if resolved_by_id.get(member, VisualDecision.FORWARD_ROUTE.value)
            == VisualDecision.FORWARD_ROUTE.value
        ]
        if not retained_members:
            continue
        new_route_id = f"RG{len(retained_groups) + 1:03d}"
        old_to_new[old_route_id] = new_route_id
        group["route_group_id"] = new_route_id
        group["ordered_visual_ids"] = retained_members
        retained_groups.append(group)
    retained_selections: list[dict[str, object]] = []
    for selection in selections:
        old_route_id = selection.get("route_group_id")
        if not isinstance(old_route_id, str):
            return _unresolved_failure()
        mapped_route_id = old_to_new.get(old_route_id)
        if mapped_route_id is None:
            continue
        selection["route_group_id"] = mapped_route_id
        retained_selections.append(selection)
    working["route_groups"] = retained_groups
    working["coarse_text_selections"] = retained_selections

    final_values = [decision.get("final_decision") for decision in working_decisions]
    if VisualDecision.UNCERTAIN.value in final_values:
        working["disposition"] = RoutingDisposition.REVIEW_REQUIRED.value
    elif retained_groups:
        working["disposition"] = RoutingDisposition.ROUTED.value
    elif not retained_selections and VisualDecision.FORWARD_ROUTE.value not in final_values:
        working["disposition"] = RoutingDisposition.REJECTED.value
    else:
        return _unresolved_failure()
    try:
        result = ArticleRoutingResult.model_validate(working, strict=False)
        result.validate_against(package)
    except ValueError:
        return _unresolved_failure()
    return result


def resolve_visual_verification(
    *,
    package_dir: Path,
    package: CanonicalPaperPackage,
    routing_submission: Mapping[str, object],
    interaction_create: Callable[..., object],
    on_interaction: Callable[[int, object], None] | None = None,
) -> ArticleRoutingResult | TypedFailure:
    """Resolve only routing rows explicitly marked for visual verification."""

    if not isinstance(package, CanonicalPaperPackage):
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(routing_submission, Mapping):
        raise TypeError("routing_submission must be a mapping")
    if not callable(interaction_create):
        raise TypeError("interaction_create must be callable")
    if on_interaction is not None and not callable(on_interaction):
        raise TypeError("on_interaction must be callable")
    try:
        submission = copy.deepcopy(dict(routing_submission))
    except Exception:
        return _unresolved_failure()
    preflight = _visual_preflight(submission, package)
    if isinstance(preflight, TypedFailure):
        return preflight
    decisions, flagged_positions = preflight
    if not flagged_positions:
        try:
            result = ArticleRoutingResult.model_validate(submission, strict=False)
            result.validate_against(package)
        except ValueError:
            return _unresolved_failure()
        return result

    candidate = _candidate_submission(
        submission,
        decisions,
        flagged_positions,
        package,
    )
    if isinstance(candidate, TypedFailure):
        return candidate
    candidate_submission, candidates, candidate_result = candidate
    try:
        loaded = load_paper_package(package_dir)
    except PackageLoadError as error:
        return _package_failure(error.code)
    except (OSError, TypeError, ValueError):
        return _package_failure(FailureCode.PACKAGE_INVALID)
    if loaded.manifest != package.manifest:
        return _reference_failure()

    payloads: dict[str, bytes] = {}
    for position in flagged_positions:
        visual = package.visual_assets[position]
        asset_path = loaded.package_dir.joinpath(*PurePosixPath(visual.asset.relative_path).parts)
        try:
            payload = asset_path.read_bytes()
        except OSError:
            return _package_failure(FailureCode.ASSET_MISSING)
        if (
            len(payload) != visual.asset.byte_size
            or hashlib.sha256(payload).hexdigest() != visual.asset.sha256
        ):
            return _package_failure(FailureCode.HASH_MISMATCH)
        payloads[visual.visual_id] = payload

    resolved: dict[int, str] = {}
    for batch_index, start in enumerate(range(0, len(flagged_positions), _MAX_BATCH_SIZE), start=1):
        positions = flagged_positions[start : start + _MAX_BATCH_SIZE]
        request = _batch_request(
            package=package,
            positions=positions,
            candidates=candidates,
            payloads=payloads,
        )
        try:
            interaction = interaction_create(**request)
        except Exception as error:
            carried_failure = getattr(error, "failure", None)
            if isinstance(carried_failure, TypedFailure):
                return carried_failure
            return _provider_failure()
        output_text = _validated_output_text(interaction, request)
        if output_text is None:
            return _unresolved_failure()
        expected_ids = tuple(package.visual_assets[position].visual_id for position in positions)
        batch_decisions = _parsed_decisions(output_text, expected_ids)
        if batch_decisions is None:
            return _unresolved_failure()
        if on_interaction is not None:
            try:
                on_interaction(batch_index, interaction)
            except Exception:
                return _unresolved_failure()
        resolved.update(zip(positions, batch_decisions, strict=True))

    return _completed_result(
        submission=candidate_submission,
        decisions=decisions,
        candidate_result=candidate_result,
        resolved=resolved,
        package=package,
    )
