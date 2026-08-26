"""Opt-in paid validation of the frozen five-paper article-routing gold."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PAPER_FIXTURES = REPOSITORY_ROOT / "tests" / "fixtures" / "paper_packages"
GOLD_PATH = REPOSITORY_ROOT / "tests" / "golden" / "article_routing_v1.json"
PAPER_IDS = (
    "alchivemycin_A",
    "hyperfirin",
    "ineleganolide",
    "kalmanol",
    "principinol_B",
)
RECEIPT_MEDIA_TYPE = "application/vnd.tsdaf.task-12-live-gold-receipt+json"
POINTER_NAME = "task-12-live-gold-receipt-ref.json"


def _compact_json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _run_compact_json(command: tuple[str, ...], *, workdir: Path) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=workdir,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, (
        f"live CLI exited {completed.returncode}; inspect the persistent workspace with the CLI"
    )
    assert completed.stderr == ""
    assert completed.stdout.endswith("\n")
    wire = completed.stdout[:-1]
    assert "\n" not in wire
    payload = json.loads(wire)
    assert isinstance(payload, dict)
    assert wire == _compact_json_text(payload)
    return payload


def _events(payload: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    events = payload["events"]
    assert isinstance(events, list)
    return [event for event in events if event["event_type"] == event_type]


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def test_live_five_paper_gold(live_gold_workdir: Path) -> None:
    # Break caught: an explicitly paid run can drift from the sealed human routing decisions.
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key is None or not api_key.strip():
        raise AssertionError("Explicit live gold requires a nonblank OPENAI_API_KEY.")
    sealed_runs: list[dict[str, Any]] = []
    for paper_id in PAPER_IDS:
        package_dir = (PAPER_FIXTURES / paper_id).resolve(strict=True)
        route_payload = _run_compact_json(
            (
                sys.executable,
                "-m",
                "tsdaf",
                "route",
                "run",
                str(package_dir),
                "--live",
            ),
            workdir=live_gold_workdir,
        )
        assert route_payload["status"] == "SUCCEEDED"
        assert route_payload["stage_status"] in {"SUCCEEDED", "CACHED"}
        assert route_payload["cache_hit"] is (route_payload["stage_status"] == "CACHED")
        execution_id = route_payload["execution_id"]
        assert isinstance(execution_id, str) and execution_id

        inspected = _run_compact_json(
            (sys.executable, "-m", "tsdaf", "run", "inspect", execution_id),
            workdir=live_gold_workdir,
        )
        assert inspected["execution_id"] == execution_id
        assert inspected["status"] == "SUCCEEDED"
        assert inspected["finished_at"] is not None
        assert inspected["handoffs"] is not None
        finished = _events(inspected, "execution_finished")
        assert len(finished) == 1
        assert finished[0]["data"] == {"status": "SUCCEEDED"}
        sealed_runs.append(
            {
                "paper_id": paper_id,
                "route": route_payload,
                "inspection": inspected,
            }
        )

    gold_bytes = GOLD_PATH.read_bytes()
    gold_sha256 = hashlib.sha256(gold_bytes).hexdigest()
    gold = json.loads(gold_bytes)
    assert isinstance(gold, dict)
    assert gold["schema_version"] == "tsdaf.article-routing-human-gold.v1"
    assert gold["gold_id"] == "weak-article-router-v1-five-paper"
    assert tuple(paper["paper_id"] for paper in gold["papers"]) == PAPER_IDS
    gold_by_paper = {paper["paper_id"]: paper for paper in gold["papers"]}

    from tsdaf.contracts.handoff import CoarseTextHandoff, VisualRoutingHandoff
    from tsdaf.contracts.routing import (
        ArticleRoutingResult,
        ArticleType,
        CoarseTextRole,
        VisualDecision,
    )
    from tsdaf.contracts.source import ArtifactRef
    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
    from tsdaf.workflow.local_runtime import LocalObjectStore

    workspace = live_gold_workdir / ".tsdaf-workspace"
    store = LocalObjectStore(workspace)
    total_decisions = 0
    total_forward_assets = 0
    visual_escalations = 0
    receipt_papers: list[dict[str, Any]] = []

    for sealed in sealed_runs:
        paper_id = sealed["paper_id"]
        route_payload = sealed["route"]
        inspected = sealed["inspection"]
        paper_gold = gold_by_paper[paper_id]

        routing_ref = ArtifactRef.model_validate(route_payload["routing_result_artifact"])
        visual_ref = ArtifactRef.model_validate(route_payload["visual_handoff_artifact"])
        coarse_ref = ArtifactRef.model_validate(route_payload["coarse_text_handoff_artifact"])
        for artifact in (routing_ref, visual_ref, coarse_ref):
            store.verify(artifact)

        command_handoffs = {
            "routing_result_artifact": routing_ref.model_dump(mode="json"),
            "visual_handoff_artifact": visual_ref.model_dump(mode="json"),
            "coarse_text_handoff_artifact": coarse_ref.model_dump(mode="json"),
            "visual_item_count": route_payload["visual_item_count"],
            "coarse_text_item_count": route_payload["coarse_text_item_count"],
        }
        assert inspected["handoffs"] == command_handoffs
        handoff_events = _events(inspected, "handoffs_materialized")
        assert len(handoff_events) == 1
        assert handoff_events[0]["data"] == command_handoffs

        cached_events = _events(inspected, "stage_cached")
        if route_payload["cache_hit"]:
            assert len(cached_events) == 1
            evidence_execution_id = cached_events[0]["data"]["producer_execution_id"]
            assert isinstance(evidence_execution_id, str) and evidence_execution_id
            evidence = _run_compact_json(
                (
                    sys.executable,
                    "-m",
                    "tsdaf",
                    "run",
                    "inspect",
                    evidence_execution_id,
                ),
                workdir=live_gold_workdir,
            )
        else:
            assert cached_events == []
            evidence_execution_id = route_payload["execution_id"]
            evidence = inspected
        assert evidence["execution_id"] == evidence_execution_id
        assert evidence["status"] == "SUCCEEDED"
        assert evidence["finished_at"] is not None

        package = canonicalize_paper_package(PAPER_FIXTURES / paper_id)
        assert package.canonical_content_sha256 == paper_gold["canonical_package_sha256"]
        routing_result = ArticleRoutingResult.model_validate_json(store.read_bytes(routing_ref))
        visual_handoff = VisualRoutingHandoff.model_validate_json(store.read_bytes(visual_ref))
        coarse_handoff = CoarseTextHandoff.model_validate_json(store.read_bytes(coarse_ref))
        routing_result.validate_against(package)
        visual_handoff.validate_against(package, routing_result)
        coarse_handoff.validate_against(package, routing_result)
        assert routing_result.execution_id == evidence_execution_id
        assert routing_result.article_type is ArticleType.TOTAL_SYNTHESIS
        assert routing_result.article_type.value == paper_gold["expected_article_type"]

        canonical_visual_ids = tuple(visual.visual_id for visual in package.visual_assets)
        decision_ids = tuple(decision.visual_id for decision in routing_result.visual_decisions)
        assert decision_ids == canonical_visual_ids
        assert len(decision_ids) == len(set(decision_ids))
        assert len(decision_ids) == paper_gold["expected_visual_count"]
        assert all(decision.triage_result is None for decision in routing_result.visual_decisions)
        assert all(
            decision.initial_decision is not VisualDecision.NEEDS_VISUAL_VERIFICATION
            and decision.final_decision is not VisualDecision.UNCERTAIN
            for decision in routing_result.visual_decisions
        )

        expected_ids = tuple(asset["visual_id"] for asset in paper_gold["forward_assets"])
        expected_hashes = tuple(
            asset["source_asset_sha256"] for asset in paper_gold["forward_assets"]
        )
        selected_ids = tuple(
            visual_id
            for route_group in routing_result.route_groups
            for visual_id in route_group.ordered_visual_ids
        )
        visuals_by_id = {visual.visual_id: visual for visual in package.visual_assets}
        selected_hashes = tuple(visuals_by_id[visual_id].asset.sha256 for visual_id in selected_ids)
        assert selected_ids == expected_ids
        assert selected_hashes == expected_hashes
        assert tuple(item.source_asset for item in visual_handoff.items) == tuple(
            visuals_by_id[visual_id].asset for visual_id in expected_ids
        )

        blocks_by_id = {block.block_id: block for block in package.blocks}
        assert all(
            item.block.source == blocks_by_id[item.selection.block_id].source
            for item in coarse_handoff.items
        )
        for route_group in routing_result.route_groups:
            assert any(
                selection.route_group_id == route_group.route_group_id
                and selection.role is CoarseTextRole.ROUTE_CORE
                for selection in routing_result.coarse_text_selections
            )
        required_attributes = set(paper_gold["required_route_attributes"])
        actual_attributes = {
            attribute.value
            for route_group in routing_result.route_groups
            for attribute in route_group.attributes
        }
        assert required_attributes <= actual_attributes

        observability = evidence["observability"]
        assert observability["model_profile"] is not None
        assert observability["prompt_sha256"] is not None
        assert observability["schema_sha256"] is not None
        assert observability["model_profile"] == routing_result.provenance.provider_profile_id
        assert observability["prompt_sha256"] == routing_result.provenance.prompt_sha256
        assert observability["schema_sha256"] == routing_result.provenance.contract_schema_sha256
        assert _nonnegative_integer(observability["router_turn_count"])
        assert observability["router_turn_count"] > 0
        assert _nonnegative_integer(observability["function_call_count"])
        assert observability["function_call_count"] > 0
        required_token_fields = (
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
        )
        optional_token_fields = (
            "total_cached_tokens",
            "total_thought_tokens",
            "total_tool_use_tokens",
        )
        assert all(_nonnegative_integer(observability[field]) for field in required_token_fields)
        assert all(
            observability[field] is None or _nonnegative_integer(observability[field])
            for field in optional_token_fields
        )
        assert observability["total_input_tokens"] > 0
        assert observability["total_output_tokens"] > 0
        assert observability["total_tokens"] > 0
        assert isinstance(observability["total_latency_ms"], int | float)
        assert not isinstance(observability["total_latency_ms"], bool)
        assert observability["total_latency_ms"] >= 0
        assert observability["visual_triage_batch_count"] == 0
        assert observability["errors"] == []
        assert _events(evidence, "visual_triage_interaction") == []

        interaction_events = _events(evidence, "article_router_interaction")
        assert len(interaction_events) == observability["router_turn_count"]
        evidence_artifacts = {
            ArtifactRef.model_validate(value) for value in evidence["artifact_refs"]
        }
        interaction_request_refs = []
        interaction_response_refs = []
        for event in interaction_events:
            data = event["data"]
            request_ref = ArtifactRef.model_validate(data["request_artifact"])
            response_ref = ArtifactRef.model_validate(data["response_artifact"])
            store.verify(request_ref)
            store.verify(response_ref)
            assert request_ref in evidence_artifacts
            assert response_ref in evidence_artifacts
            interaction_request_refs.append(request_ref.model_dump(mode="json"))
            interaction_response_refs.append(response_ref.model_dump(mode="json"))

        escalations = sum(
            decision.initial_decision is VisualDecision.NEEDS_VISUAL_VERIFICATION
            or decision.triage_result is not None
            or decision.final_decision is VisualDecision.UNCERTAIN
            for decision in routing_result.visual_decisions
        )
        total_decisions += len(routing_result.visual_decisions)
        total_forward_assets += len(selected_ids)
        visual_escalations += escalations
        receipt_papers.append(
            {
                "paper_id": paper_id,
                "execution_id": route_payload["execution_id"],
                "evidence_execution_id": evidence_execution_id,
                "cache_hit": route_payload["cache_hit"],
                "canonical_package_sha256": package.canonical_content_sha256,
                "artifacts": {
                    "routing_result": routing_ref.model_dump(mode="json"),
                    "visual_handoff": visual_ref.model_dump(mode="json"),
                    "coarse_text_handoff": coarse_ref.model_dump(mode="json"),
                    "interaction_requests": interaction_request_refs,
                    "interaction_responses": interaction_response_refs,
                },
                "selected_visual_ids": list(selected_ids),
                "selected_source_asset_sha256s": list(selected_hashes),
                "visual_decision_count": len(routing_result.visual_decisions),
                "visual_escalation_count": escalations,
                "observability": {
                    "model_profile": observability["model_profile"],
                    "prompt_sha256": observability["prompt_sha256"],
                    "schema_sha256": observability["schema_sha256"],
                    "router_turn_count": observability["router_turn_count"],
                    "function_call_count": observability["function_call_count"],
                    **{
                        field: observability[field]
                        for field in required_token_fields + optional_token_fields
                    },
                    "total_latency_ms": observability["total_latency_ms"],
                    "visual_triage_batch_count": observability["visual_triage_batch_count"],
                    "error_count": 0,
                },
                "provenance": routing_result.provenance.model_dump(mode="json"),
            }
        )

    assert total_decisions == gold["total_visual_decisions"] == 37
    assert total_forward_assets == 9
    assert visual_escalations == 0

    receipt = {
        "schema_version": "tsdaf.task-12-live-gold-receipt.v1",
        "created_at": datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "gold_id": gold["gold_id"],
        "gold_sha256": gold_sha256,
        "paper_count": len(receipt_papers),
        "total_visual_decisions": total_decisions,
        "total_forward_assets": total_forward_assets,
        "visual_escalation_count": visual_escalations,
        "papers": receipt_papers,
    }
    receipt_wire = _compact_json_text(receipt)
    assert not any(
        forbidden in receipt_wire.casefold()
        for forbidden in (
            "gemini_api_key",
            "openai_api_key",
            "authorization",
            '"headers"',
            '"request_bytes"',
            '"response_bytes"',
            '"transcript"',
        )
    )
    if api_key in receipt_wire:
        raise AssertionError("Live receipt must not contain the OpenAI API key.")
    receipt_ref = store.put_json(receipt, media_type=RECEIPT_MEDIA_TYPE)
    pointer = {
        "schema_version": "tsdaf.task-12-live-gold-receipt-pointer.v1",
        "receipt_artifact": receipt_ref.model_dump(mode="json"),
    }
    _atomic_write(
        live_gold_workdir / POINTER_NAME,
        _compact_json_text(pointer).encode("utf-8"),
    )
    print(_compact_json_text(receipt_ref.model_dump(mode="json")))
