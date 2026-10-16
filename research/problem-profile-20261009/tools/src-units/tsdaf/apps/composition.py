"""Direct application composition for the fixed article-routing workflow."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tsdaf.contracts.execution import ExecutionStatus, FailureCode, StageResult, TypedFailure
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.partition import ExperimentalPartition
from tsdaf.contracts.reading_units import ReadingUnits
from tsdaf.contracts.routing import ArticleRoutingResult
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
from tsdaf.lanes.document.package_loader import PackageLoadError
from tsdaf.lanes.document.reading_units import partition_clean_view
from tsdaf.lanes.document.routing import MainArticleRouter, build_proposed_handoffs
from tsdaf.lanes.literature.provider_port import LaneInteractionClient
from tsdaf.workflow.article_router_runtime import (
    article_router_stage_identity,
    run_article_router_stage,
)
from tsdaf.workflow.local_runtime import (
    LocalObjectStore,
    LocalRunner,
    LocalRuntimeError,
)
from tsdaf.workflow.reading_units_runtime import (
    read_reading_units,
    run_reading_units_stage,
)
from tsdaf.workflow.review_queue import append_review_queue, review_queue_row

__all__ = ("execute_route_workflow", "inspect_execution", "resume_route_workflow")

_STAGE_NAME = "main-article-router"
_ROUTING_MEDIA_TYPE = "application/vnd.tsdaf.article-routing-result+json"
_VISUAL_HANDOFF_MEDIA_TYPE = "application/vnd.tsdaf.visual-routing-handoff+json"
_COARSE_HANDOFF_MEDIA_TYPE = "application/vnd.tsdaf.coarse-text-handoff+json"
_STAGE_RESULT_MEDIA_TYPE = "application/vnd.tsdaf.stage-result+json"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROMPT_FILES = ("manifest.toml", "system.md", "task.md", "taxonomy.md")
_TERMINAL_STATUSES = {
    ExecutionStatus.SUCCEEDED.value,
    ExecutionStatus.REVIEW_REQUIRED.value,
    ExecutionStatus.FAILED_TERMINAL.value,
}


def _validate_inputs(
    *,
    workspace: Path,
    execution_id: str,
    interaction_create: Callable[..., object] | None = None,
    provider_is_live: bool | None = None,
    live: bool | None = None,
) -> None:
    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a pathlib.Path")
    if _execution_id_failure(execution_id) is not None:
        raise ValueError("execution_id must be a safe nonempty identifier")
    if interaction_create is not None and not callable(interaction_create):
        raise TypeError("interaction_create must be callable")
    if provider_is_live is not None and not isinstance(provider_is_live, bool):
        raise TypeError("provider_is_live must be a bool value")
    if live is not None and not isinstance(live, bool):
        raise TypeError("live must be a bool value")


def _configuration_failure(message: str) -> dict[str, object]:
    return {
        "status": "ERROR",
        "code": "CONFIGURATION_INVALID",
        "message": message,
    }


def _failure_payload(
    code: FailureCode,
    message: str,
    *,
    execution_id: str | None = None,
    status: str = "ERROR",
    locator: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "code": code.value,
        "message": message,
    }
    if execution_id is not None:
        payload["execution_id"] = execution_id
    if locator is not None:
        payload["locator"] = locator
    return payload


def _execution_id_failure(execution_id: object) -> dict[str, object] | None:
    if isinstance(execution_id, str) and _SAFE_ID_RE.fullmatch(execution_id) is not None:
        return None
    return _failure_payload(
        FailureCode.PACKAGE_INVALID,
        "The execution identifier is invalid.",
    )


def _package_failure(error: PackageLoadError, execution_id: str | None = None) -> dict[str, object]:
    messages = {
        FailureCode.PACKAGE_INVALID: "The paper package is invalid.",
        FailureCode.ASSET_MISSING: "A manifest-listed paper-package asset is missing.",
        FailureCode.HASH_MISMATCH: "A paper-package asset failed integrity verification.",
    }
    return _failure_payload(
        error.code,
        messages.get(error.code, "The paper package could not be loaded."),
        execution_id=execution_id,
        locator=error.relative_path,
    )


def _runtime_failure(error: LocalRuntimeError, execution_id: str) -> dict[str, object]:
    return _failure_payload(
        error.code,
        "The local execution evidence could not be verified.",
        execution_id=execution_id,
    )


def _candidate_roots(start: Path) -> tuple[Path, ...]:
    location = start.resolve(strict=False)
    if location.is_file():
        location = location.parent
    return (location, *location.parents)


def _find_prompt_directory() -> Path | None:
    seen: set[Path] = set()
    for start in (Path.cwd(), Path(__file__).resolve()):
        for candidate in _candidate_roots(start):
            if candidate in seen:
                continue
            seen.add(candidate)
            prompt_dir = candidate / "prompts" / "article_router" / "v1"
            if (candidate / "pyproject.toml").is_file() and all(
                (prompt_dir / name).is_file() for name in _PROMPT_FILES
            ):
                return prompt_dir
    return None


def _canonical_package(
    package_dir: Path,
    *,
    execution_id: str | None = None,
) -> tuple[CanonicalPaperPackage | None, dict[str, object] | None]:
    if not isinstance(package_dir, Path):
        raise TypeError("package_dir must be a pathlib.Path")
    try:
        return canonicalize_paper_package(package_dir), None
    except PackageLoadError as error:
        return None, _package_failure(error, execution_id)
    except (OSError, TypeError, ValueError):
        return None, _failure_payload(
            FailureCode.PACKAGE_INVALID,
            "The paper package is invalid.",
            execution_id=execution_id,
            locator=".",
        )


def _preflight_route(package_dir: Path) -> dict[str, object] | None:
    package, failure = _canonical_package(package_dir)
    del package
    if failure is not None:
        return failure
    if _find_prompt_directory() is None:
        return _configuration_failure("The article-router prompt checkout could not be located.")
    return None


def _resolver_content_key(
    package: CanonicalPaperPackage,
    prompt_dir: Path,
    partition: ExperimentalPartition | None,
    units: ReadingUnits | None,
) -> str:
    return article_router_stage_identity(
        package=package,
        router=MainArticleRouter(prompt_dir),
        visual_triage_enabled=True,
        partition=partition,
        units=units,
    ).content_key


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise ValueError("verified local JSON record is not canonical")
    return value


def _read_events(workspace: Path, execution_id: str) -> list[dict[str, Any]]:
    path = workspace / "executions" / execution_id / "events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise ValueError("verified event log has a partial line")
        event = json.loads(line)
        if not isinstance(event, dict) or canonical_json_bytes(event) + b"\n" != line:
            raise ValueError("verified event log is not canonical")
        events.append(event)
    return events


def _artifact(value: object) -> ArtifactRef:
    return ArtifactRef.model_validate(value)


def _routing_result(
    *,
    store: LocalObjectStore,
    ref: ArtifactRef,
    package: CanonicalPaperPackage,
) -> ArticleRoutingResult:
    if ref.media_type != _ROUTING_MEDIA_TYPE:
        raise ValueError("routing-result artifact has the wrong media type")
    payload = store.read_bytes(ref)
    result = ArticleRoutingResult.model_validate_json(payload)
    expected = canonical_json_bytes(
        result.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    if payload != expected:
        raise ValueError("routing-result artifact is not canonical")
    result.validate_against(package)
    return result


def _handoff_data(
    *,
    routing_ref: ArtifactRef,
    visual_ref: ArtifactRef,
    coarse_ref: ArtifactRef,
    visual_count: int,
    coarse_count: int,
    units_ref: ArtifactRef | None,
) -> dict[str, object]:
    return {
        "routing_result_artifact": routing_ref.model_dump(mode="json"),
        "visual_handoff_artifact": visual_ref.model_dump(mode="json"),
        "coarse_text_handoff_artifact": coarse_ref.model_dump(mode="json"),
        "visual_item_count": visual_count,
        "coarse_text_item_count": coarse_count,
        # The sealed reading units the Router was narrowed by (their derived cut); None
        # when the stage did not run, in which case the Router read the whole cleaned
        # document. Executions sealed by the retired split stage carry
        # `experimental_partition_artifact` here instead, and are still read.
        "reading_units_artifact": (
            None if units_ref is None else units_ref.model_dump(mode="json")
        ),
    }


class _WorkflowStop(Exception):
    """The workflow cannot continue; carries the exit code and payload to emit."""

    def __init__(self, code: int, payload: dict[str, object]) -> None:
        super().__init__(payload.get("message", "workflow stopped"))
        self.code = code
        self.payload = payload


def _read_units(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    execution_id: str,
    units_client: LaneInteractionClient | None,
    provider_is_live: bool,
    live: bool,
) -> tuple[StageResult | None, ReadingUnits | None]:
    """Run the reading-units stage ahead of the Router; raise `_WorkflowStop` when it cannot."""

    if units_client is None:
        return None, None
    try:
        stage = run_reading_units_stage(
            workspace=workspace,
            package=package,
            execution_id=execution_id,
            client=units_client,
            source_phase="MAIN",
            provider_is_live=provider_is_live,
            live=live,
        )
    except LocalRuntimeError as error:
        raise _WorkflowStop(1, _runtime_failure(error, execution_id)) from error
    if isinstance(stage, TypedFailure):
        payload = _failure_payload(
            stage.code, stage.message, execution_id=execution_id, status="NOT_STARTED"
        )
        raise _WorkflowStop(3 if stage.code is FailureCode.LIVE_REQUIRED else 1, payload)
    if stage.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
        raise _WorkflowStop(1, _stage_failure_payload(stage))
    if stage.output_artifact is None:
        raise _WorkflowStop(
            1,
            _failure_payload(
                FailureCode.HASH_MISMATCH,
                "The successful reading-units stage result has no units artifact.",
                execution_id=execution_id,
            ),
        )
    try:
        units = read_reading_units(LocalObjectStore(workspace), stage.output_artifact)
        units.validate_against(package)
    except (LocalRuntimeError, ValueError) as error:
        raise _WorkflowStop(
            1,
            _failure_payload(
                FailureCode.HASH_MISMATCH,
                f"The sealed reading units could not be verified: {error}",
                execution_id=execution_id,
            ),
        ) from error
    return stage, units


def _stage_failure_payload(result: StageResult) -> dict[str, object]:
    failure = result.failure
    if failure is None:
        return _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The stage result did not contain its required failure evidence.",
            execution_id=result.execution_id,
            status=result.status.value,
        )
    payload = _failure_payload(
        failure.code,
        failure.message,
        execution_id=result.execution_id,
        status=result.status.value,
    )
    payload.update(
        {
            "stage_status": result.status.value,
            "stage_name": result.stage_name,
            "stage_version": result.stage_version,
            "content_key": result.input_content_key,
            "cache_hit": False,
        }
    )
    return payload


def execute_route_workflow(
    *,
    package_dir: Path,
    workspace: Path,
    execution_id: str,
    interaction_create: Callable[..., object],
    provider_is_live: bool,
    live: bool,
    units_client: LaneInteractionClient | None = None,
    review_queue: Path | None = None,
) -> tuple[int, dict[str, object]]:
    """Validate, read into units, route, materialize both handoffs, and seal one execution.

    `units_client` runs the reading-units stage ahead of the Router (ledger E136, the
    successor of the split of E47); without one the Router reads the whole cleaned
    document and the handoff records no units.
    `review_queue` is the human review queue (E86): a paper the Router rejected or
    routed with an undecided visual is appended there once, outside the workspace.
    """

    _validate_inputs(
        workspace=workspace,
        execution_id=execution_id,
        interaction_create=interaction_create,
        provider_is_live=provider_is_live,
        live=live,
    )
    package, package_failure = _canonical_package(package_dir, execution_id=execution_id)
    if package_failure is not None:
        return 1, package_failure
    assert package is not None
    prompt_dir = _find_prompt_directory()
    if prompt_dir is None:
        failure = _configuration_failure("The article-router prompt checkout could not be located.")
        failure["execution_id"] = execution_id
        return 2, failure

    try:
        units_stage, units = _read_units(
            workspace=workspace,
            package=package,
            execution_id=execution_id,
            units_client=units_client,
            provider_is_live=provider_is_live,
            live=live,
        )
    except _WorkflowStop as stop:
        return stop.code, stop.payload
    partition = None if units is None else units.partition()
    try:
        stage = run_article_router_stage(
            workspace=workspace,
            package=package,
            execution_id=execution_id,
            prompt_dir=prompt_dir,
            interaction_create=interaction_create,
            provider_is_live=provider_is_live,
            live=live,
            package_dir=package_dir,
            finish_execution=False,
            partition=partition,
            units=units,
        )
    except LocalRuntimeError as error:
        return 1, _runtime_failure(error, execution_id)

    if isinstance(stage, TypedFailure):
        payload = _failure_payload(
            stage.code,
            stage.message,
            execution_id=execution_id,
            status="NOT_STARTED",
        )
        return (3 if stage.code is FailureCode.LIVE_REQUIRED else 1), payload
    if stage.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
        return 1, _stage_failure_payload(stage)
    if stage.output_artifact is None:
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The successful stage result has no routing artifact.",
            execution_id=execution_id,
        )

    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    try:
        routing_result = _routing_result(
            store=store,
            ref=stage.output_artifact,
            package=package,
        )
        visual_handoff, coarse_handoff = build_proposed_handoffs(
            package=package,
            routing_result=routing_result,
        )
        visual_ref = store.put_json(
            visual_handoff.model_dump(mode="json", by_alias=True, exclude_none=False),
            media_type=_VISUAL_HANDOFF_MEDIA_TYPE,
        )
        coarse_ref = store.put_json(
            coarse_handoff.model_dump(mode="json", by_alias=True, exclude_none=False),
            media_type=_COARSE_HANDOFF_MEDIA_TYPE,
        )
        event_data = _handoff_data(
            routing_ref=stage.output_artifact,
            visual_ref=visual_ref,
            coarse_ref=coarse_ref,
            visual_count=len(visual_handoff.items),
            coarse_count=len(coarse_handoff.items),
            units_ref=None if units_stage is None else units_stage.output_artifact,
        )
        existing = [
            event
            for event in _read_events(workspace, execution_id)
            if event.get("event_type") == "handoffs_materialized"
        ]
        existing_matches = bool(existing) and (
            existing[0].get("data") == event_data
            and existing[0].get("attempt_id") == stage.attempt_id
            and existing[0].get("stage_name") == stage.stage_name
            and existing[0].get("content_key") == stage.input_content_key
        )
        if len(existing) > 1 or (existing and not existing_matches):
            return 1, _failure_payload(
                FailureCode.HASH_MISMATCH,
                "Existing handoff evidence does not match the verified routing result.",
                execution_id=execution_id,
            )
        if not existing:
            runner.append_event(
                execution_id,
                "handoffs_materialized",
                event_data,
                attempt_id=stage.attempt_id,
                stage_name=stage.stage_name,
                content_key=stage.input_content_key,
            )
        runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
    except LocalRuntimeError as error:
        return 1, _runtime_failure(error, execution_id)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The routing or handoff evidence could not be verified.",
            execution_id=execution_id,
        )

    narrative_ids, experimental_ids = partition_clean_view(package, partition)
    # The human review queue (E86): rejected and review-required papers are one row
    # each; the run still succeeds -- being queued is the outcome, not a failure.
    queued: dict[str, object] | None = None
    row = review_queue_row(routing_result, package.manifest)
    if row is not None:
        queued = {**row.as_record(), "queue": None, "appended": False}
        if review_queue is not None:
            try:
                queued["appended"] = append_review_queue(review_queue, row)
            except (OSError, ValueError) as error:
                return 1, _failure_payload(
                    FailureCode.PACKAGE_INVALID,
                    f"The review queue could not be written: {error}",
                    execution_id=execution_id,
                )
            queued["queue"] = str(review_queue)
    return 0, {
        "execution_id": execution_id,
        "status": ExecutionStatus.SUCCEEDED.value,
        "disposition": routing_result.disposition.value,
        "article_type": routing_result.article_type.value,
        "review_queue": queued,
        "stage_status": stage.status.value,
        "cache_hit": stage.status is ExecutionStatus.CACHED,
        "stage_name": stage.stage_name,
        "stage_version": stage.stage_version,
        "content_key": stage.input_content_key,
        "routing_result_artifact": stage.output_artifact.model_dump(mode="json"),
        "visual_handoff_artifact": visual_ref.model_dump(mode="json"),
        "coarse_text_handoff_artifact": coarse_ref.model_dump(mode="json"),
        "visual_item_count": len(visual_handoff.items),
        "coarse_text_item_count": len(coarse_handoff.items),
        "reading_units": None
        if units_stage is None or units is None
        else {
            "stage_status": units_stage.status.value,
            "units_artifact": event_data["reading_units_artifact"],
            "experimental_begins_at": units.experimental_begins_at,
            "unit_count": len(units.units),
            "uncovered_block_count": len(units.uncovered_block_ids),
            "not_shown_block_count": len(units.not_shown_block_ids),
            "unclaimed_block_count": len(units.unclaimed_block_ids),
            "rationale": units.rationale,
            "narrative_block_count": len(narrative_ids),
            "experimental_block_count": len(experimental_ids),
        },
    }


def _execution_row(workspace: Path, execution_id: str) -> tuple[str, str, str | None] | None:
    database = workspace / "index.sqlite"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status, created_at, finished_at FROM executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1]), None if row[2] is None else str(row[2])


def resume_route_workflow(
    *,
    workspace: Path,
    execution_id: str,
    interaction_create: Callable[..., object],
    provider_is_live: bool,
    live: bool,
    units_client: LaneInteractionClient | None = None,
    review_queue: Path | None = None,
) -> tuple[int, dict[str, object]]:
    """Resume only the verified package and immutable route identity already recorded."""

    _validate_inputs(
        workspace=workspace,
        execution_id=execution_id,
        interaction_create=interaction_create,
        provider_is_live=provider_is_live,
        live=live,
    )
    runner = LocalRunner(workspace, LocalObjectStore(workspace))
    try:
        runner.refresh_index(execution_id)
        execution = _execution_row(workspace, execution_id)
        if execution is None:
            return 1, _failure_payload(
                FailureCode.ASSET_MISSING,
                "The requested execution does not exist.",
                execution_id=execution_id,
            )
        status, _, _ = execution
        if status in _TERMINAL_STATUSES:
            return 0, {
                "execution_id": execution_id,
                "status": status,
                "resume": "ALREADY_TERMINAL",
            }
        events = _read_events(workspace, execution_id)
    except LocalRuntimeError as error:
        return 1, _runtime_failure(error, execution_id)
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The requested execution evidence could not be verified.",
            execution_id=execution_id,
        )

    sources = [event for event in events if event.get("event_type") == "route_source"]
    if not sources:
        return 1, _failure_payload(
            FailureCode.ASSET_MISSING,
            "The execution has no verified route source.",
            execution_id=execution_id,
        )
    source = sources[-1]
    data = source.get("data")
    if (
        not isinstance(data, dict)
        or set(data) != {"package_dir", "canonical_package_sha256"}
        or not isinstance(data.get("package_dir"), str)
        or not isinstance(data.get("canonical_package_sha256"), str)
        or _SHA256_RE.fullmatch(data["canonical_package_sha256"]) is None
        or source.get("stage_name") != _STAGE_NAME
        or not isinstance(source.get("content_key"), str)
    ):
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The execution route source is invalid.",
            execution_id=execution_id,
        )

    package_dir = Path(data["package_dir"]).resolve(strict=False)
    package, package_failure = _canonical_package(package_dir, execution_id=execution_id)
    if package_failure is not None:
        return 1, package_failure
    assert package is not None
    if package.canonical_content_sha256 != data["canonical_package_sha256"]:
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The resume source no longer matches its canonical package hash.",
            execution_id=execution_id,
        )
    prompt_dir = _find_prompt_directory()
    if prompt_dir is None:
        failure = _configuration_failure("The article-router prompt checkout could not be located.")
        failure["execution_id"] = execution_id
        return 2, failure
    # The Router's identity includes the part of the document it read, so the sealed
    # (or, for a live resume, freshly run) reading comes first, exactly as in a run.
    try:
        _, units = _read_units(
            workspace=workspace,
            package=package,
            execution_id=execution_id,
            units_client=units_client,
            provider_is_live=provider_is_live,
            live=live,
        )
    except _WorkflowStop as stop:
        return stop.code, stop.payload
    partition = None if units is None else units.partition()
    try:
        expected_key = _resolver_content_key(package, prompt_dir, partition, units)
    except (OSError, TypeError, ValueError):
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The stored route identity cannot be reproduced.",
            execution_id=execution_id,
        )
    if source["content_key"] != expected_key:
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The stored route identity does not match the current fixed configuration.",
            execution_id=execution_id,
        )
    return execute_route_workflow(
        package_dir=package_dir,
        workspace=workspace,
        execution_id=execution_id,
        interaction_create=interaction_create,
        provider_is_live=provider_is_live,
        live=live,
        units_client=units_client,
        review_queue=review_queue,
    )


def _event_data(event_type: object, value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("event data is not a mapping")
    data = dict(value)
    allowed: dict[str, tuple[str, ...]] = {
        "route_source": ("canonical_package_sha256",),
        "article_router_interaction": (
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
        ),
        "reading_units_interaction": (
            "turn",
            "interaction_id",
            "request_id",
            "request_artifact",
            "response_artifact",
            "request_chars",
            "total_input_tokens",
            "total_output_tokens",
            "total_tokens",
            "total_cached_tokens",
            "total_thought_tokens",
            "total_tool_use_tokens",
            "elapsed_ms",
            "transport_attempts",
        ),
        "literature_lane_interaction": (
            "lane",
            "turn",
            "interaction_artifact",
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
            "request_id",
            "model_profile",
        ),
        "visual_triage_interaction": (
            "batch_index",
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
        ),
        "attempt_completed": ("status", "result_sha256"),
        "attempt_aborted": ("error_type",),
        "stage_completed": ("status", "result_sha256"),
        "stage_cached": ("producer_execution_id", "producer_result_sha256"),
        "handoffs_materialized": (
            "routing_result_artifact",
            "visual_handoff_artifact",
            "coarse_text_handoff_artifact",
            "visual_item_count",
            "coarse_text_item_count",
            "reading_units_artifact",
            "experimental_partition_artifact",
        ),
        "execution_finished": ("status",),
    }
    keys = allowed.get(event_type, ()) if isinstance(event_type, str) else ()
    return {key: data[key] for key in keys if key in data}


def _response_function_calls(store: LocalObjectStore, ref: ArtifactRef) -> int:
    payload = store.read_bytes(ref)
    value = json.loads(payload)
    if not isinstance(value, dict) or canonical_json_bytes(value) != payload:
        raise ValueError("interaction response artifact is not canonical JSON")
    steps = value.get("steps")
    if not isinstance(steps, list):
        raise ValueError("interaction response artifact has no step list")
    return sum(isinstance(step, Mapping) and step.get("type") == "function_call" for step in steps)


def _metric_total(events: list[dict[str, Any]], field: str) -> int | float | None:
    if not events:
        return None
    values = [event["data"].get(field) for event in events]
    if any(value is None for value in values):
        return None
    if field == "elapsed_ms":
        if any(
            isinstance(value, bool) or not isinstance(value, int | float) or value < 0
            for value in values
        ):
            raise ValueError("interaction latency is invalid")
        return sum(float(value) for value in values)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("interaction token total is invalid")
    return sum(int(value) for value in values)


def inspect_execution(
    *,
    workspace: Path,
    execution_id: str,
) -> tuple[int, dict[str, object]]:
    """Verify local evidence and return deterministic JSON-ready inspection facts."""

    _validate_inputs(workspace=workspace, execution_id=execution_id)
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    try:
        runner.refresh_index(execution_id)
        execution = _execution_row(workspace, execution_id)
        if execution is None:
            return 1, _failure_payload(
                FailureCode.ASSET_MISSING,
                "The requested execution does not exist.",
                execution_id=execution_id,
            )
        status, created_at, finished_at = execution
        with sqlite3.connect(workspace / "index.sqlite") as connection:
            attempt_rows = connection.execute(
                """
                SELECT attempt_id, stage_name, stage_version, content_key, status,
                       result_sha256, started_at, finished_at
                FROM stage_attempts WHERE execution_id = ? ORDER BY started_at, attempt_id
                """,
                (execution_id,),
            ).fetchall()
            output_rows = connection.execute(
                """
                SELECT content_key, stage_name, stage_version, status, result_sha256,
                       output_sha256, cached_from_result_sha256
                FROM stage_outputs WHERE execution_id = ? ORDER BY content_key
                """,
                (execution_id,),
            ).fetchall()
        artifacts: dict[tuple[str, str], ArtifactRef] = {}

        def add_artifact(ref: ArtifactRef) -> None:
            store.verify(ref)
            key = (ref.sha256, ref.media_type)
            existing = artifacts.get(key)
            if existing is not None and existing != ref:
                raise ValueError("artifact identity has divergent metadata")
            artifacts[key] = ref

        attempts: list[dict[str, object]] = []
        attempt_results: list[StageResult] = []
        for row in attempt_rows:
            attempt_id = str(row[0])
            started = _read_json_object(
                workspace / "executions" / execution_id / "attempts" / attempt_id / "started.json"
            )
            attempt: dict[str, object] = {
                "attempt_id": attempt_id,
                "stage_name": str(row[1]),
                "stage_version": str(row[2]),
                "content_key": str(row[3]),
                "status": str(row[4]),
                "result_sha256": None if row[5] is None else str(row[5]),
                "started_at": str(row[6]),
                "finished_at": None if row[7] is None else str(row[7]),
                "input_artifacts": started.get("input_artifacts"),
                "settings": started.get("settings"),
                "prompt_sha256": started.get("prompt_sha256"),
                "schema_sha256": started.get("schema_sha256"),
                "model_profile": started.get("model_profile"),
            }
            input_artifacts = started.get("input_artifacts")
            if not isinstance(input_artifacts, list):
                raise ValueError("attempt input artifacts are invalid")
            for value in input_artifacts:
                add_artifact(_artifact(value))
            result_path = (
                workspace
                / "executions"
                / execution_id
                / "attempts"
                / attempt_id
                / "result-ref.json"
            )
            if result_path.is_file():
                record = _read_json_object(result_path)
                result_ref = _artifact(record.get("result_ref"))
                add_artifact(result_ref)
                result = StageResult.model_validate_json(store.read_bytes(result_ref))
                attempt_results.append(result)
                if result.output_artifact is not None:
                    add_artifact(result.output_artifact)
                if result.cached_from_result is not None:
                    add_artifact(result.cached_from_result)
                attempt["result_artifact"] = result_ref.model_dump(mode="json")
                attempt["failure"] = (
                    None
                    if result.failure is None
                    else {
                        "type": "TypedFailure",
                        "code": result.failure.code.value,
                    }
                )
            else:
                attempt["result_artifact"] = None
                attempt["failure"] = None
            attempts.append(attempt)

        stage_outputs: list[dict[str, object]] = []
        for row in output_rows:
            content_key = str(row[0])
            record = _read_json_object(
                workspace / "executions" / execution_id / "stages" / content_key / "result-ref.json"
            )
            result_ref = _artifact(record.get("result_ref"))
            add_artifact(result_ref)
            result = StageResult.model_validate_json(store.read_bytes(result_ref))
            if result.output_artifact is not None:
                add_artifact(result.output_artifact)
            if result.cached_from_result is not None:
                add_artifact(result.cached_from_result)
            stage_outputs.append(
                {
                    "content_key": content_key,
                    "stage_name": str(row[1]),
                    "stage_version": str(row[2]),
                    "status": str(row[3]),
                    "result_artifact": result_ref.model_dump(mode="json"),
                    "output_artifact": (
                        None
                        if result.output_artifact is None
                        else result.output_artifact.model_dump(mode="json")
                    ),
                    "cached_from_result": (
                        None
                        if result.cached_from_result is None
                        else result.cached_from_result.model_dump(mode="json")
                    ),
                }
            )

        raw_events = _read_events(workspace, execution_id)
        events = [
            {
                "sequence": event.get("sequence"),
                "event_type": event.get("event_type"),
                "data": _event_data(event.get("event_type"), event.get("data")),
                "attempt_id": event.get("attempt_id"),
                "stage_name": event.get("stage_name"),
                "content_key": event.get("content_key"),
                "occurred_at": event.get("occurred_at"),
            }
            for event in raw_events
        ]
        interaction_events = [
            event
            for event in events
            if event["event_type"]
            in {
                "reading_units_interaction",
                "article_router_interaction",
                "visual_triage_interaction",
                "literature_lane_interaction",
            }
        ]
        function_call_count = 0
        for event in interaction_events:
            data = event["data"]
            assert isinstance(data, dict)
            request_ref = _artifact(data.get("request_artifact"))
            response_ref = _artifact(data.get("response_artifact"))
            add_artifact(request_ref)
            add_artifact(response_ref)
            function_call_count += _response_function_calls(store, response_ref)

        handoff_events = [
            event for event in events if event["event_type"] == "handoffs_materialized"
        ]
        if len(handoff_events) > 1:
            raise ValueError("execution has duplicate handoff materialization events")
        handoffs: dict[str, object] | None = None
        if handoff_events:
            data = handoff_events[0]["data"]
            # The units key is absent on executions sealed before either stage existed,
            # null when the stage did not run, and named `experimental_partition_artifact`
            # on executions sealed by the retired split stage; all three are valid records.
            if not isinstance(data, dict) or set(data) - {
                "reading_units_artifact",
                "experimental_partition_artifact",
            } != {
                "routing_result_artifact",
                "visual_handoff_artifact",
                "coarse_text_handoff_artifact",
                "visual_item_count",
                "coarse_text_item_count",
            }:
                raise ValueError("handoff materialization event is invalid")
            for key in (
                "routing_result_artifact",
                "visual_handoff_artifact",
                "coarse_text_handoff_artifact",
            ):
                add_artifact(_artifact(data[key]))
            for optional in ("reading_units_artifact", "experimental_partition_artifact"):
                if data.get(optional) is not None:
                    add_artifact(_artifact(data[optional]))
            handoffs = data

        # Each stage of the execution (the reading, the Router) is bound to its own
        # prompt and schema; within one stage every attempt must share them.
        for stage_name in {attempt["stage_name"] for attempt in attempts}:
            stage_attempts = [a for a in attempts if a["stage_name"] == stage_name]
            for field in ("model_profile", "prompt_sha256", "schema_sha256"):
                values = [attempt[field] for attempt in stage_attempts]
                if values and any(value != values[0] for value in values):
                    raise ValueError("attempt runtime bindings are divergent")
        # The bindings reported are the Router's; the reading's are on its own attempt.
        router_attempts = [a for a in attempts if a["stage_name"] == _STAGE_NAME] or attempts
        bindings = router_attempts[0] if router_attempts else {}

        errors: list[dict[str, object]] = [
            {"type": "TypedFailure", "code": result.failure.code.value}
            for result in attempt_results
            if result.failure is not None
        ]
        errors.extend(
            {"type": event["data"].get("error_type"), "code": None}
            for event in events
            if event["event_type"] == "attempt_aborted" and isinstance(event["data"], dict)
        )
        observability = {
            "cache_hit": any(
                output["status"] == ExecutionStatus.CACHED.value for output in stage_outputs
            ),
            "model_profile": bindings.get("model_profile"),
            "prompt_sha256": bindings.get("prompt_sha256"),
            "schema_sha256": bindings.get("schema_sha256"),
            "reading_units_turn_count": sum(
                event["event_type"] == "reading_units_interaction" for event in events
            ),
            "router_turn_count": sum(
                event["event_type"] == "article_router_interaction" for event in events
            ),
            "visual_triage_batch_count": sum(
                event["event_type"] == "visual_triage_interaction" for event in events
            ),
            "function_call_count": function_call_count,
            "total_input_tokens": _metric_total(interaction_events, "total_input_tokens"),
            "total_output_tokens": _metric_total(interaction_events, "total_output_tokens"),
            "total_tokens": _metric_total(interaction_events, "total_tokens"),
            "total_cached_tokens": _metric_total(interaction_events, "total_cached_tokens"),
            "total_thought_tokens": _metric_total(interaction_events, "total_thought_tokens"),
            "total_tool_use_tokens": _metric_total(interaction_events, "total_tool_use_tokens"),
            "total_latency_ms": _metric_total(interaction_events, "elapsed_ms"),
            "visual_tokens": None,
            "cost_estimate": {
                "amount_usd": None,
                "currency": "USD",
                "pricing_snapshot_version": "unconfigured",
            },
            "errors": errors,
        }
        artifact_payloads = [
            ref.model_dump(mode="json")
            for ref in sorted(
                artifacts.values(),
                key=lambda item: (item.sha256, item.media_type, item.relative_path),
            )
        ]
    except LocalRuntimeError as error:
        return 1, _runtime_failure(error, execution_id)
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return 1, _failure_payload(
            FailureCode.HASH_MISMATCH,
            "The requested execution evidence could not be verified.",
            execution_id=execution_id,
        )

    return 0, {
        "execution_id": execution_id,
        "status": status,
        "created_at": created_at,
        "finished_at": finished_at,
        "stage_outputs": stage_outputs,
        "attempts": attempts,
        "events": events,
        "artifact_refs": artifact_payloads,
        "handoffs": handoffs,
        "observability": observability,
    }
