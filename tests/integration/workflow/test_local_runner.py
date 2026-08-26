from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_STARTED_AT = datetime(2026, 8, 26, 4, 0, 0, 1, tzinfo=UTC)
_FINISHED_AT = _STARTED_AT + timedelta(seconds=1)
_INDEX_TABLES = (
    "executions",
    "execution_events",
    "stage_attempts",
    "stage_outputs",
    "artifacts",
)


def _runtime_api() -> ModuleType:
    return importlib.import_module("tsdaf.workflow.local_runtime")


def _contracts() -> tuple[ModuleType, ModuleType]:
    source = importlib.import_module("tsdaf.contracts.source")
    execution = importlib.import_module("tsdaf.contracts.execution")
    return source, execution


def _stage_result(
    *,
    execution_id: str,
    attempt_id: str,
    content_key: str,
    output_artifact: Any | None,
    status_name: str = "SUCCEEDED",
) -> Any:
    _, execution = _contracts()
    failure = None
    if status_name == "FAILED_RETRYABLE":
        failure = execution.TypedFailure(
            schema_version="tsdaf.typed-failure.v1",
            code=execution.FailureCode.PROVIDER_TRANSIENT,
            message="Recorded provider was temporarily unavailable.",
            request_id=None,
            details=(),
        )
    return execution.StageResult(
        schema_version="tsdaf.stage-result.v1",
        execution_id=execution_id,
        attempt_id=attempt_id,
        stage_name="main-router",
        stage_version="1.0.0",
        input_content_key=content_key,
        status=execution.ExecutionStatus[status_name],
        output_artifact=output_artifact,
        failure=failure,
        cached_from_result=None,
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _artifact_refs(value: object) -> tuple[Any, ...]:
    source, _ = _contracts()
    keys = ("sha256", "media_type", "byte_size", "relative_path")
    refs: list[Any] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if all(key in node for key in keys):
                refs.append(source.ArtifactRef.model_validate({key: node[key] for key in keys}))
                return
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return tuple(refs)


def _stage_results_from_record(path: Path, store: Any) -> tuple[Any, ...]:
    _, execution = _contracts()
    results: list[Any] = []
    for ref in _artifact_refs(_read_json(path)):
        store.verify(ref)
        try:
            results.append(execution.StageResult.model_validate_json(store.read_bytes(ref)))
        except ValueError:
            continue
    return tuple(results)


def _events(path: Path) -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())


def _workspace_snapshot(root: Path) -> tuple[tuple[str, str, bytes], ...]:
    if not root.exists():
        return ()
    entries: list[tuple[str, str, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root).as_posix()
        if path.is_dir():
            entries.append(("directory", relative_path, b""))
        elif path.is_file():
            entries.append(("file", relative_path, path.read_bytes()))
        else:
            entries.append(("other", relative_path, b""))
    return tuple(entries)


def _sqlite_rows(
    database_path: Path,
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]]:
    rows: dict[str, tuple[tuple[str, ...], tuple[tuple[Any, ...], ...]]] = {}
    with closing(sqlite3.connect(database_path)) as connection:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert set(_INDEX_TABLES) <= existing
        for table in _INDEX_TABLES:
            columns = tuple(
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            )
            values = tuple(
                sorted(connection.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
            )
            rows[table] = (columns, values)
    return rows


def test_concurrent_executions_invoke_once_and_share_verified_producer(
    tmp_path: Path,
) -> None:
    runtime = _runtime_api()
    _, execution = _contracts()
    workspace = tmp_path / "workspace"
    store = runtime.LocalObjectStore(workspace)
    input_ref = store.put_bytes(b"canonical-package", media_type="application/json")
    output_ref = store.put_bytes(b"router-output", media_type="application/json")
    ready = threading.Barrier(3)
    invoke_entered = threading.Event()
    release_invoke = threading.Event()
    state_lock = threading.Lock()
    calls: list[tuple[str, str, str]] = []
    results: dict[str, Any] = {}
    errors: list[BaseException] = []

    def worker(execution_id: str) -> None:
        try:
            runner = runtime.LocalRunner(workspace, runtime.LocalObjectStore(workspace))
            ready.wait(timeout=5.0)

            def invoke(attempt_id: str, content_key: str) -> Any:
                with state_lock:
                    calls.append((execution_id, attempt_id, content_key))
                invoke_entered.set()
                if not release_invoke.wait(timeout=5.0):
                    raise TimeoutError("test did not release the producer callback")
                return _stage_result(
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    content_key=content_key,
                    output_artifact=output_ref,
                )

            result = runner.run_stage(
                execution_id=execution_id,
                stage_name="main-router",
                stage_version="1.0.0",
                input_artifacts=(input_ref,),
                settings={"mode": "recorded"},
                invoke=invoke,
            )
            with state_lock:
                results[execution_id] = result
        except BaseException as error:
            with state_lock:
                errors.append(error)

    threads = tuple(
        threading.Thread(target=worker, args=(execution_id,))
        for execution_id in ("execution-concurrent-a", "execution-concurrent-b")
    )
    for thread in threads:
        thread.start()
    ready.wait(timeout=5.0)
    entered = invoke_entered.wait(timeout=5.0)
    release_invoke.set()
    assert entered
    for thread in threads:
        thread.join(timeout=5.0)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(calls) == 1
    assert len({call[2] for call in calls}) == 1
    assert sorted(result.status.value for result in results.values()) == ["CACHED", "SUCCEEDED"]

    succeeded = next(
        result
        for result in results.values()
        if result.status is execution.ExecutionStatus.SUCCEEDED
    )
    cached = next(
        result for result in results.values() if result.status is execution.ExecutionStatus.CACHED
    )
    assert succeeded.output_artifact == cached.output_artifact == output_ref
    assert succeeded.attempt_id is not None
    assert cached.attempt_id is None
    assert cached.cached_from_result is not None
    store.verify(output_ref)
    store.verify(cached.cached_from_result)
    stored_producer = execution.StageResult.model_validate_json(
        store.read_bytes(cached.cached_from_result)
    )
    assert stored_producer == succeeded
    attempt_directories = tuple(workspace.glob("executions/*/attempts/attempt_*"))
    assert len(attempt_directories) == 1


def test_retry_preserves_failed_attempt_then_publishes_success_and_resumes(
    tmp_path: Path,
) -> None:
    runtime = _runtime_api()
    _, execution = _contracts()
    workspace = tmp_path / "workspace"
    store = runtime.LocalObjectStore(workspace)
    runner = runtime.LocalRunner(workspace, store)
    input_ref = store.put_bytes(b"canonical-package", media_type="application/json")
    output_ref = store.put_bytes(b"router-output", media_type="application/json")
    calls: list[tuple[str, str]] = []

    def invoke(attempt_id: str, content_key: str) -> Any:
        calls.append((attempt_id, content_key))
        if len(calls) == 1:
            return _stage_result(
                execution_id="execution-retry",
                attempt_id=attempt_id,
                content_key=content_key,
                output_artifact=None,
                status_name="FAILED_RETRYABLE",
            )
        return _stage_result(
            execution_id="execution-retry",
            attempt_id=attempt_id,
            content_key=content_key,
            output_artifact=output_ref,
        )

    arguments = {
        "execution_id": "execution-retry",
        "stage_name": "main-router",
        "stage_version": "1.0.0",
        "input_artifacts": (input_ref,),
        "settings": {"mode": "recorded"},
    }
    failed = runner.run_stage(**arguments, invoke=invoke)
    assert failed.status is execution.ExecutionStatus.FAILED_RETRYABLE
    assert failed.attempt_id is not None
    cache_path = (
        workspace / "stage-results" / "main-router" / failed.input_content_key / "result.json"
    )
    assert not cache_path.exists()
    failed_record = (
        workspace
        / "executions"
        / "execution-retry"
        / "attempts"
        / failed.attempt_id
        / "result-ref.json"
    )
    failed_record_bytes = failed_record.read_bytes()
    assert _stage_results_from_record(failed_record, store) == (failed,)

    succeeded = runner.run_stage(**arguments, invoke=invoke)
    assert succeeded.status is execution.ExecutionStatus.SUCCEEDED
    assert succeeded.attempt_id is not None
    assert succeeded.attempt_id != failed.attempt_id
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1] == succeeded.input_content_key
    assert failed_record.read_bytes() == failed_record_bytes
    succeeded_record = (
        workspace
        / "executions"
        / "execution-retry"
        / "attempts"
        / succeeded.attempt_id
        / "result-ref.json"
    )
    assert _stage_results_from_record(succeeded_record, store) == (succeeded,)
    assert _stage_results_from_record(cache_path, store) == (succeeded,)
    assert (
        workspace
        / "executions"
        / "execution-retry"
        / "stages"
        / succeeded.input_content_key
        / "result-ref.json"
    ).is_file()
    assert len(tuple((workspace / "executions/execution-retry/attempts").glob("attempt_*"))) == 2
    store.verify(output_ref)

    events_path = workspace / "executions" / "execution-retry" / "events.jsonl"
    events_before_resume = events_path.read_bytes()

    def unexpected_invoke(attempt_id: str, content_key: str) -> Any:
        raise AssertionError(f"resume unexpectedly invoked {attempt_id} for {content_key}")

    resumed = runner.run_stage(**arguments, invoke=unexpected_invoke)
    assert resumed == succeeded
    assert events_path.read_bytes() == events_before_resume


def test_aborted_retry_and_offline_live_boundary_preserve_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime_api()
    _, execution = _contracts()

    exception_workspace = tmp_path / "exception-workspace"
    exception_store = runtime.LocalObjectStore(exception_workspace)
    exception_runner = runtime.LocalRunner(exception_workspace, exception_store)
    exception_input = exception_store.put_bytes(b"exception-input", media_type="application/json")
    exception_output = exception_store.put_bytes(b"exception-output", media_type="application/json")
    aborted_attempts: list[str] = []
    secret_sentinel = "Authorization: Bearer task6-secret-sentinel-7f319d"

    def abort(attempt_id: str, content_key: str) -> Any:
        aborted_attempts.append(attempt_id)
        raise RuntimeError(f"forced callback abort for {content_key}; {secret_sentinel}")

    exception_arguments = {
        "execution_id": "execution-aborted",
        "stage_name": "main-router",
        "stage_version": "1.0.0",
        "input_artifacts": (exception_input,),
        "settings": {"mode": "recorded"},
    }
    with pytest.raises(RuntimeError, match="forced callback abort") as propagated:
        exception_runner.run_stage(**exception_arguments, invoke=abort)
    assert secret_sentinel in str(propagated.value)

    assert len(aborted_attempts) == 1
    aborted_id = aborted_attempts[0]
    execution_root = exception_workspace / "executions" / "execution-aborted"
    aborted_root = execution_root / "attempts" / aborted_id
    assert (aborted_root / "started.json").is_file()
    assert not (aborted_root / "result-ref.json").exists()
    events_path = execution_root / "events.jsonl"
    assert secret_sentinel not in events_path.read_text(encoding="utf-8")
    aborted_events = _events(events_path)
    assert tuple(event["event_type"] for event in aborted_events) == (
        "attempt_started",
        "attempt_aborted",
    )
    assert tuple(event["sequence"] for event in aborted_events) == (1, 2)
    aborted_data = aborted_events[-1]["data"]
    assert aborted_data == {"error_type": "RuntimeError"}
    assert secret_sentinel not in json.dumps(aborted_data, sort_keys=True)
    aborted_key = runtime.stage_content_key(
        stage_name="main-router",
        stage_version="1.0.0",
        input_sha256s=(exception_input.sha256,),
        settings={"mode": "recorded"},
    )
    assert not (execution_root / "stages" / aborted_key / "result-ref.json").exists()
    assert not (
        exception_workspace / "stage-results" / "main-router" / aborted_key / "result.json"
    ).exists()

    def recover(attempt_id: str, content_key: str) -> Any:
        return _stage_result(
            execution_id="execution-aborted",
            attempt_id=attempt_id,
            content_key=content_key,
            output_artifact=exception_output,
        )

    recovered = exception_runner.run_stage(**exception_arguments, invoke=recover)
    assert recovered.status is execution.ExecutionStatus.SUCCEEDED
    assert recovered.attempt_id not in {None, aborted_id}

    offline_workspace = tmp_path / "offline-workspace"
    offline_store = runtime.LocalObjectStore(offline_workspace)
    offline_input = offline_store.put_bytes(b"offline-input", media_type="application/json")
    offline_runner = runtime.LocalRunner(offline_workspace, offline_store)
    offline_arguments = {
        "stage_name": "main-router",
        "stage_version": "1.0.0",
        "input_artifacts": (offline_input,),
        "settings": {"mode": "recorded"},
        "requires_live": True,
        "live": False,
    }
    callback_called = False

    def forbidden_callback(attempt_id: str, content_key: str) -> Any:
        nonlocal callback_called
        callback_called = True
        raise AssertionError(f"offline miss invoked {attempt_id} for {content_key}")

    before = _workspace_snapshot(offline_workspace)
    live_required = offline_runner.run_stage(
        execution_id="execution-offline-miss",
        invoke=forbidden_callback,
        **offline_arguments,
    )
    after = _workspace_snapshot(offline_workspace)
    assert isinstance(live_required, execution.TypedFailure)
    assert live_required.code is execution.FailureCode.LIVE_REQUIRED
    assert callback_called is False
    assert after == before

    offline_output = offline_store.put_bytes(b"offline-output", media_type="application/json")

    def produce_cache(attempt_id: str, content_key: str) -> Any:
        return _stage_result(
            execution_id="execution-live-producer",
            attempt_id=attempt_id,
            content_key=content_key,
            output_artifact=offline_output,
        )

    producer_arguments = dict(offline_arguments)
    producer_arguments["live"] = True
    producer = offline_runner.run_stage(
        execution_id="execution-live-producer",
        invoke=produce_cache,
        **producer_arguments,
    )
    assert producer.status is execution.ExecutionStatus.SUCCEEDED

    cached = offline_runner.run_stage(
        execution_id="execution-offline-cache",
        invoke=forbidden_callback,
        **offline_arguments,
    )
    assert cached.status is execution.ExecutionStatus.CACHED
    assert cached.attempt_id is None
    assert cached.output_artifact == producer.output_artifact == offline_output
    assert cached.cached_from_result is not None
    offline_store.verify(cached.cached_from_result)
    assert not (offline_workspace / "executions" / "execution-offline-cache" / "attempts").exists()


def test_finish_seals_events_and_deleted_sqlite_index_rebuilds_exactly(
    tmp_path: Path,
) -> None:
    runtime = _runtime_api()
    _, execution = _contracts()
    workspace = tmp_path / "workspace"
    store = runtime.LocalObjectStore(workspace)
    runner = runtime.LocalRunner(workspace, store)
    input_ref = store.put_bytes(b"canonical-package", media_type="application/json")
    output_ref = store.put_bytes(b"router-output", media_type="application/json")

    def invoke(attempt_id: str, content_key: str) -> Any:
        return _stage_result(
            execution_id="execution-finished",
            attempt_id=attempt_id,
            content_key=content_key,
            output_artifact=output_ref,
        )

    result = runner.run_stage(
        execution_id="execution-finished",
        stage_name="main-router",
        stage_version="1.0.0",
        input_artifacts=(input_ref,),
        settings={"mode": "recorded"},
        invoke=invoke,
    )
    assert result.status is execution.ExecutionStatus.SUCCEEDED
    runner.append_event(
        "execution-finished",
        "router_output_validated",
        {"artifact_sha256": output_ref.sha256},
        attempt_id=result.attempt_id,
        stage_name=result.stage_name,
        content_key=result.input_content_key,
    )

    manifest_ref = runner.finish_execution(
        "execution-finished", execution.ExecutionStatus.SUCCEEDED
    )
    execution_root = workspace / "executions" / "execution-finished"
    events_path = execution_root / "events.jsonl"
    manifest_path = execution_root / "manifest.json"
    events_bytes = events_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    events = _events(events_path)
    manifest = _read_json(manifest_path)
    assert events_bytes.endswith(b"\n")
    assert tuple(event["sequence"] for event in events) == tuple(range(1, len(events) + 1))
    assert "SUCCEEDED" in json.dumps(manifest, sort_keys=True)
    assert store.read_bytes(manifest_ref) == manifest_bytes

    manifest_refs = _artifact_refs(manifest)
    assert manifest_refs
    for ref in manifest_refs:
        store.verify(ref)
    event_digest = hashlib.sha256(events_bytes).hexdigest()
    event_refs = tuple(
        ref
        for ref in manifest_refs
        if ref.sha256 == event_digest and ref.byte_size == len(events_bytes)
    )
    assert len(event_refs) == 1
    assert store.read_bytes(event_refs[0]) == events_bytes
    store.verify(result.output_artifact)
    stage_record = execution_root / "stages" / result.input_content_key / "result-ref.json"
    assert _stage_results_from_record(stage_record, store) == (result,)
    assert result.attempt_id is not None
    attempt_record = execution_root / "attempts" / result.attempt_id / "result-ref.json"
    assert _stage_results_from_record(attempt_record, store) == (result,)

    index_path = workspace / "index.sqlite"
    rows_before = _sqlite_rows(index_path)
    assert all(rows_before[table][1] for table in _INDEX_TABLES)
    attempts_before = _workspace_snapshot(execution_root / "attempts")
    stages_before = _workspace_snapshot(execution_root / "stages")
    index_bytes = index_path.read_bytes()

    post_finish_invoked = False

    def forbidden_post_finish_invoke(attempt_id: str, content_key: str) -> Any:
        nonlocal post_finish_invoked
        post_finish_invoked = True
        raise AssertionError(f"terminal execution invoked {attempt_id=} for {content_key=}")

    with pytest.raises(runtime.LocalRuntimeError):
        runner.run_stage(
            execution_id="execution-finished",
            stage_name="main-router",
            stage_version="1.0.0",
            input_artifacts=(input_ref,),
            settings={"mode": "distinct-post-finish"},
            invoke=forbidden_post_finish_invoke,
        )
    assert not post_finish_invoked
    assert _workspace_snapshot(execution_root / "attempts") == attempts_before
    assert _workspace_snapshot(execution_root / "stages") == stages_before
    assert events_path.read_bytes() == events_bytes
    assert manifest_path.read_bytes() == manifest_bytes
    assert index_path.read_bytes() == index_bytes
    assert _sqlite_rows(index_path) == rows_before

    repeated_ref = runner.finish_execution(
        "execution-finished", execution.ExecutionStatus.SUCCEEDED
    )
    assert repeated_ref == manifest_ref
    assert _workspace_snapshot(execution_root / "attempts") == attempts_before
    assert _workspace_snapshot(execution_root / "stages") == stages_before
    assert events_path.read_bytes() == events_bytes
    assert manifest_path.read_bytes() == manifest_bytes
    assert index_path.read_bytes() == index_bytes
    assert _sqlite_rows(index_path) == rows_before

    with pytest.raises(runtime.LocalRuntimeError):
        runner.append_event("execution-finished", "too_late", {})
    assert events_path.read_bytes() == events_bytes
    with pytest.raises(runtime.LocalRuntimeError):
        runner.finish_execution("execution-finished", execution.ExecutionStatus.FAILED_TERMINAL)
    assert manifest_path.read_bytes() == manifest_bytes

    for suffix in ("", "-wal", "-shm"):
        Path(f"{index_path}{suffix}").unlink(missing_ok=True)
    rebuilt_path = runner.rebuild_index()
    assert rebuilt_path.resolve() == index_path.resolve()
    assert _sqlite_rows(rebuilt_path) == rows_before
    store.verify(manifest_ref)
    store.verify(result.output_artifact)
