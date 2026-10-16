"""Independent T/V stages on the existing runner; immutable originals, current bindings."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from tsdaf.contracts.execution import (
    ExecutionStatus,
    FailureCode,
    ProviderInteractionError,
    StageResult,
    TypedFailure,
    failure_is_retryable,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.artifacts import seal_exact
from tsdaf.lanes.literature.lane_dispatch import (
    PairPreparationReceipt,
    RawLaneExchange,
    ReaderExecutor,
)
from tsdaf.lanes.literature.lane_proposals import load_reader_exchange, validate_reader_output
from tsdaf.lanes.literature.provider_port import AUTHORIZED_MODEL_PROFILE, ProviderBinding
from tsdaf.lanes.literature.reader_identity import (
    READER_CACHE_OUTPUT_MEDIA,
    READER_CACHE_STAGE_VERSION,
    READER_IDENTITY_MEDIA_TYPE,
    READER_REUSE_MEDIA,
    ReaderCacheOutput,
    ReaderIdentity,
    ReaderReuseBinding,
    ReaderReuseVerifier,
    build_reader_identity,
    reader_cache_stage,
)
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput
from tsdaf.lanes.provider_attempts import seal_provider_attempts
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner, stage_content_key

_RESULT_MEDIA = "application/vnd.tsdaf.stage-result+json"


def reader_content_key(stage: Literal["T1", "V1"], identity_ref: ArtifactRef) -> str:
    return stage_content_key(
        stage_name=reader_cache_stage(stage),
        stage_version=READER_CACHE_STAGE_VERSION,
        input_sha256s=(identity_ref.sha256,),
        settings={},
        model_profile=AUTHORIZED_MODEL_PROFILE,
    )


def reader_reuse_verifier(
    *,
    runner: LocalRunner,
    store: LocalObjectStore,
    provider_binding: ProviderBinding,
    work_unit: VerifiedWorkUnitInput,
) -> ReaderReuseVerifier:
    def verify(binding: ReaderReuseBinding) -> None:
        identity = ReaderIdentity.model_validate_json(store.read_bytes(binding.identity_ref))
        namespace = provider_binding.namespace_for(identity.stage)
        if namespace is None or identity != build_reader_identity(
            stage=identity.stage,
            work_unit=work_unit,
            request_ref=identity.request_ref,
            resource_manifest_ref=identity.resource_manifest_ref,
            backend_namespace=namespace,
        ):
            raise ValueError("reader reuse changes current authority or backend namespace")
        producer = StageResult.model_validate_json(store.read_bytes(binding.producer_result_ref))
        if (
            producer.status != ExecutionStatus.SUCCEEDED
            or producer.stage_name != reader_cache_stage(identity.stage)
            or producer.stage_version != READER_CACHE_STAGE_VERSION
            or producer.input_content_key
            != reader_content_key(identity.stage, binding.identity_ref)
            or producer.output_artifact != binding.output_ref
        ):
            raise ValueError("reader reuse producer does not match the current reader key/output")
        closure = runner.verified_execution_artifact_closure(
            producer.execution_id, binding.producer_terminal_ref
        )
        if binding.producer_result_ref not in closure or binding.output_ref not in closure:
            raise ValueError("reader producer result is outside its verified execution")
        # A reachable arbitrary StageResult is not a registered completed stage.
        manifest = json.loads(store.read_bytes(binding.producer_terminal_ref))
        if manifest["status"] != ExecutionStatus.SUCCEEDED.value:
            raise ValueError("reader producer execution was not successful")
        if not any(
            row["result_ref"] == binding.producer_result_ref.model_dump(mode="json")
            for row in manifest["stage_records"]
        ):
            raise ValueError("reader producer result is not its execution's completed stage")

    return verify


def cached_reader_executor(
    *,
    runner: LocalRunner,
    store: LocalObjectStore,
    work_unit: VerifiedWorkUnitInput,
    provider_binding: ProviderBinding,
    provider_is_live: bool,
    live: bool,
    bind: Callable[..., None],
    emit_event: Callable[[str, dict[str, Any]], None],
) -> ReaderExecutor:
    verifier = reader_reuse_verifier(
        runner=runner, store=store, provider_binding=provider_binding, work_unit=work_unit
    )

    def execute(
        stage: Literal["T1", "V1"],
        preparation: PairPreparationReceipt,
        preparation_ref: ArtifactRef,
        invoke_provider: Callable[[], RawLaneExchange],
    ) -> RawLaneExchange:
        # Old direct-injection callers retain their existing cold path.
        if provider_binding.namespace_for(stage) is None:
            return invoke_provider()
        identity_ref = (
            preparation.text_reader_identity_ref
            if stage == "T1"
            else preparation.visual_reader_identity_ref
        )
        if identity_ref is None or identity_ref.media_type != READER_IDENTITY_MEDIA_TYPE:
            raise ValueError("reader stage requires its current sealed identity")
        identity = ReaderIdentity.model_validate_json(store.read_bytes(identity_ref))
        expected = build_reader_identity(
            stage=stage,
            work_unit=work_unit,
            request_ref=identity.request_ref,
            resource_manifest_ref=identity.resource_manifest_ref,
            backend_namespace=provider_binding.namespace_for(stage),
        )
        if identity != expected:
            raise ValueError("reader stage identity changes current authority/backend")
        child_id = f"reader-{stage.lower()}-{uuid4().hex}"
        name = reader_cache_stage(stage)
        fresh: RawLaneExchange | None = None
        dispatched = False

        def invoke(attempt_id: str, key: str) -> StageResult:
            nonlocal fresh, dispatched
            started = datetime.now(UTC)
            output = None
            refused = None
            status = ExecutionStatus.SUCCEEDED

            def child_bind(*refs: ArtifactRef) -> None:
                runner.bind_attempt_artifacts(
                    child_id, attempt_id, refs, stage_name=name, content_key=key
                )

            child_bind(preparation_ref)
            try:
                dispatched = True
                fresh = invoke_provider()
                child_bind(fresh.record_ref)
                # One parser for cold cache eligibility and normal proposal consumption.
                validate_reader_output(
                    exchange=fresh,
                    preparation=preparation,
                    preparation_ref=preparation_ref,
                    store=store,
                )
                value = ReaderCacheOutput(
                    identity_ref=identity_ref,
                    original_preparation_ref=preparation_ref,
                    raw_record_ref=fresh.record_ref,
                )
                output = seal_exact(
                    store,
                    canonical_json_bytes(value.model_dump(mode="json")),
                    media_type=READER_CACHE_OUTPUT_MEDIA,
                )
            except (ProviderInteractionError, ValueError) as error:
                if isinstance(error, ProviderInteractionError):
                    audit = seal_provider_attempts(
                        store, error, request_bytes=store.read_bytes(identity.request_ref)
                    )
                    if audit is not None:
                        child_bind(audit)
                    refused = error.failure
                else:
                    refused = TypedFailure(
                        schema_version="tsdaf.typed-failure.v1",
                        code=FailureCode.PROVIDER_REJECTED,
                        message="Reader output failed its existing typed/closure validation.",
                        request_id=None,
                        details=(),
                    )
                status = (
                    ExecutionStatus.FAILED_RETRYABLE
                    if failure_is_retryable(refused.code)
                    else ExecutionStatus.FAILED_TERMINAL
                )
            return StageResult(
                schema_version="tsdaf.stage-result.v1",
                execution_id=child_id,
                attempt_id=attempt_id,
                stage_name=name,
                stage_version=READER_CACHE_STAGE_VERSION,
                input_content_key=key,
                status=status,
                output_artifact=output,
                failure=refused,
                cached_from_result=None,
                started_at=started,
                finished_at=datetime.now(UTC),
            )

        result = runner.run_stage(
            execution_id=child_id,
            stage_name=name,
            stage_version=READER_CACHE_STAGE_VERSION,
            input_artifacts=(identity_ref,),
            settings={},
            model_profile=AUTHORIZED_MODEL_PROFILE,
            invoke=invoke,
            requires_live=provider_is_live,
            live=live,
        )
        if isinstance(result, TypedFailure):
            raise ProviderInteractionError(result)
        terminal = runner.finish_execution(
            child_id,
            ExecutionStatus.SUCCEEDED
            if result.failure is None
            else ExecutionStatus.FAILED_TERMINAL,
        )
        result_ref = seal_exact(
            store, canonical_json_bytes(result.model_dump(mode="json")), media_type=_RESULT_MEDIA
        )
        # Bind independently inside each worker, including failures and successful peers.
        bind(result_ref, terminal)
        # Logical provider invocations and final-response reported usage only.
        # Transport-attempt history remains immutable in the original raw/audit refs.
        emit_event(
            "literature_reader_consumed",
            {
                "stage": stage,
                "status": result.status.value,
                "reader_result_ref": result_ref.model_dump(mode="json"),
                "provider_calls": int(dispatched),
                "total_tokens": fresh.record.total_tokens
                if fresh is not None
                else (None if dispatched else 0),
                "elapsed_ms": fresh.record.elapsed_ms
                if fresh is not None
                else (None if dispatched else 0),
            },
        )
        if result.failure is not None:
            raise ProviderInteractionError(result.failure)
        if fresh is not None:
            return fresh
        if result.output_artifact is None or result.cached_from_result is None:
            raise ValueError("reader cached result has no producer output")
        producer = StageResult.model_validate_json(store.read_bytes(result.cached_from_result))
        producer_terminal = runner.finish_execution(producer.execution_id)
        binding = ReaderReuseBinding(
            current_preparation_ref=preparation_ref,
            identity_ref=identity_ref,
            output_ref=result.output_artifact,
            producer_result_ref=result.cached_from_result,
            producer_terminal_ref=producer_terminal,
        )
        verifier(binding)
        output = ReaderCacheOutput.model_validate_json(store.read_bytes(binding.output_ref))
        binding_ref = seal_exact(
            store,
            canonical_json_bytes(binding.model_dump(mode="json")),
            media_type=READER_REUSE_MEDIA,
        )
        exchange = load_reader_exchange(
            store=store,
            stage=stage,
            record_ref=output.raw_record_ref,
            reuse_binding_ref=binding_ref,
        )
        validate_reader_output(
            exchange=exchange,
            preparation=preparation,
            preparation_ref=preparation_ref,
            store=store,
            reuse_verifier=verifier,
        )
        bind(binding_ref)
        return exchange

    return execute
