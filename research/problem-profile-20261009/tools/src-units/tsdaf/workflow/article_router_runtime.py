"""Concrete local runner for the text-only Main Article Router stage."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from tsdaf.contracts.execution import (
    ExecutionStatus,
    FailureCode,
    StageResult,
    TypedFailure,
    failure_is_retryable,
)
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.partition import ExperimentalPartition
from tsdaf.contracts.reading_units import ReadingUnits
from tsdaf.contracts.routing import ArticleRoutingResult
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.reading_units import partition_clean_view
from tsdaf.lanes.document.routing import MainArticleRouter, resolve_visual_verification
from tsdaf.lanes.provider_attempts import seal_provider_attempts
from tsdaf.workflow.local_runtime import (
    LocalObjectStore,
    LocalRunner,
    LocalRuntimeError,
    stage_content_key,
)

__all__ = ("article_router_stage_identity", "run_article_router_stage")

_STAGE_NAME = "main-article-router"
_STAGE_VERSION = "1.1.0"
_PACKAGE_MEDIA_TYPE = "application/vnd.tsdaf.canonical-paper-package+json"
_ROUTING_MEDIA_TYPE = "application/vnd.tsdaf.article-routing-result+json"
_INTERACTION_MEDIA_TYPE = "application/json"


@dataclass(frozen=True, slots=True)
class _RouterStageIdentity:
    package_bytes: bytes
    settings: Mapping[str, object]
    prompt_sha256: str
    schema_sha256: str
    model_profile: str
    content_key: str
    # The part of the cleaned document the Router is shown; see `partition_clean_view`.
    readable_block_ids: tuple[str, ...]


def article_router_stage_identity(
    *,
    package: CanonicalPaperPackage,
    router: MainArticleRouter,
    visual_triage_enabled: bool,
    partition: ExperimentalPartition | None = None,
    units: ReadingUnits | None = None,
) -> _RouterStageIdentity:
    """Use one stage configuration for execution, offline preflight and resume.

    The experimental partition narrows what the Router reads, so the readable part is
    in the stage identity: a package split differently is routed again, a package the
    split has not seen (no partition) is routed over its whole cleaned document, as
    before the split existed. The reading units label what the Router is shown and
    answered with (E156), so they are in the identity too: a package read into
    different units is routed again, and a route sealed before the units were shown
    is not served as this configuration's answer.
    """
    settings, prompt_sha256, schema_sha256, model_profile = router._runtime_bindings()
    if visual_triage_enabled:
        settings = {
            **settings,
            "visual_triage": {"enabled": True, "version": "1.0.0"},
        }
    readable_block_ids, _ = partition_clean_view(package, partition)
    if partition is not None:
        settings = {
            **settings,
            "experimental_partition": {
                "cut_block_id": partition.cut_block_id,
                "readable_block_ids_sha256": hashlib.sha256(
                    canonical_json_bytes(list(readable_block_ids))
                ).hexdigest(),
            },
        }
    if units is not None:
        settings = {
            **settings,
            "reading_units": {
                "content_hash": hashlib.sha256(
                    canonical_json_bytes(units.model_dump(mode="json", by_alias=True))
                ).hexdigest()
            },
        }
    package_value = package.model_dump(mode="json", by_alias=True, exclude_none=False)
    package_bytes = canonical_json_bytes(package_value)
    content_key = stage_content_key(
        stage_name=_STAGE_NAME,
        stage_version=_STAGE_VERSION,
        input_sha256s=(hashlib.sha256(package_bytes).hexdigest(),),
        settings=settings,
        prompt_sha256=prompt_sha256,
        schema_sha256=schema_sha256,
        model_profile=model_profile,
    )
    return _RouterStageIdentity(
        package_bytes=package_bytes,
        settings=settings,
        prompt_sha256=prompt_sha256,
        schema_sha256=schema_sha256,
        model_profile=model_profile,
        content_key=content_key,
        readable_block_ids=readable_block_ids,
    )


def _failure(code: FailureCode, message: str) -> TypedFailure:
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message=message,
        request_id=None,
        details=(),
    )


def _live_required() -> TypedFailure:
    return _failure(
        FailureCode.LIVE_REQUIRED,
        "This uncached stage requires an explicitly enabled live invocation.",
    )


def _local_failure(error: LocalRuntimeError) -> TypedFailure:
    return _failure(
        error.code,
        "The local article-router runtime could not persist verified evidence.",
    )


def _stage_result(
    *,
    execution_id: str,
    attempt_id: str,
    content_key: str,
    status: ExecutionStatus,
    output_artifact: ArtifactRef | None,
    failure: TypedFailure | None,
    started_at: datetime,
) -> StageResult:
    return StageResult(
        schema_version="tsdaf.stage-result.v1",
        execution_id=execution_id,
        attempt_id=attempt_id,
        stage_name=_STAGE_NAME,
        stage_version=_STAGE_VERSION,
        input_content_key=content_key,
        status=status,
        output_artifact=output_artifact,
        failure=failure,
        cached_from_result=None,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def run_article_router_stage(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    execution_id: str,
    prompt_dir: Path,
    interaction_create: Callable[..., object],
    provider_is_live: bool,
    live: bool = False,
    package_dir: Path | None = None,
    finish_execution: bool = True,
    partition: ExperimentalPartition | None = None,
    units: ReadingUnits | None = None,
) -> StageResult | TypedFailure:
    """Run or reuse the immutable local Main Article Router stage."""

    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a pathlib.Path")
    if not isinstance(package, CanonicalPaperPackage):
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ValueError("execution_id must be a nonempty string")
    if not isinstance(prompt_dir, Path):
        raise TypeError("prompt_dir must be a pathlib.Path")
    if not callable(interaction_create):
        raise TypeError("interaction_create must be callable")
    if not isinstance(provider_is_live, bool) or not isinstance(live, bool):
        raise TypeError("provider_is_live and live must be bool values")
    if package_dir is not None and not isinstance(package_dir, Path):
        raise TypeError("package_dir must be a pathlib.Path or None")
    if not isinstance(finish_execution, bool):
        raise TypeError("finish_execution must be a bool value")
    if partition is not None and not isinstance(partition, ExperimentalPartition):
        raise TypeError("partition must be an ExperimentalPartition or None")
    if units is not None and not isinstance(units, ReadingUnits):
        raise TypeError("units must be a ReadingUnits or None")

    router = MainArticleRouter(prompt_dir)
    identity = article_router_stage_identity(
        package=package,
        router=router,
        visual_triage_enabled=package_dir is not None,
        partition=partition,
        units=units,
    )
    package_bytes = identity.package_bytes
    settings = identity.settings
    prompt_sha256 = identity.prompt_sha256
    schema_sha256 = identity.schema_sha256
    model_profile = identity.model_profile
    content_key = identity.content_key
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    if (
        provider_is_live
        and not live
        and runner.stage_is_uncached(
            execution_id=execution_id, stage_name=_STAGE_NAME, content_key=content_key
        )
    ):
        return _live_required()

    try:
        input_artifact = store.put_bytes(package_bytes, media_type=_PACKAGE_MEDIA_TYPE)
    except LocalRuntimeError as error:
        return _local_failure(error)

    def invoke(attempt_id: str, invoked_content_key: str) -> StageResult:
        started_at = datetime.now(UTC)

        def record_evidence(
            *,
            event_type: str,
            index_name: str,
            index: int,
            interaction: object,
        ) -> None:
            evidence = cast(Any, interaction)
            request_bytes = evidence.request_bytes
            response_bytes = evidence.response_bytes
            request_ref = store.put_bytes(request_bytes, media_type=_INTERACTION_MEDIA_TYPE)
            response_ref = store.put_bytes(response_bytes, media_type=_INTERACTION_MEDIA_TYPE)
            runner.append_event(
                execution_id,
                event_type,
                {
                    index_name: index,
                    "interaction_id": evidence.interaction_id,
                    "request_id": evidence.request_id,
                    "request_artifact": request_ref.model_dump(mode="json"),
                    "response_artifact": response_ref.model_dump(mode="json"),
                    "total_input_tokens": evidence.total_input_tokens,
                    "total_output_tokens": evidence.total_output_tokens,
                    "total_tokens": evidence.total_tokens,
                    "total_cached_tokens": evidence.total_cached_tokens,
                    "total_thought_tokens": evidence.total_thought_tokens,
                    "total_tool_use_tokens": evidence.total_tool_use_tokens,
                    "elapsed_ms": evidence.elapsed_ms,
                    "transport_attempts": evidence.transport_attempts,
                },
                attempt_id=attempt_id,
                stage_name=_STAGE_NAME,
                content_key=invoked_content_key,
            )

        def record_interaction(turn: int, interaction: object) -> None:
            record_evidence(
                event_type="article_router_interaction",
                index_name="turn",
                index=turn,
                interaction=interaction,
            )

        def record_visual_interaction(batch_index: int, interaction: object) -> None:
            record_evidence(
                event_type="visual_triage_interaction",
                index_name="batch_index",
                index=batch_index,
                interaction=interaction,
            )

        def record_attempt_audit(carrier: object, request_bytes: bytes) -> None:
            ref = seal_provider_attempts(store, carrier, request_bytes=request_bytes)
            if ref is not None:
                runner.bind_attempt_artifacts(
                    execution_id,
                    attempt_id,
                    (ref,),
                    stage_name=_STAGE_NAME,
                    content_key=invoked_content_key,
                )
                runner.append_event(
                    execution_id,
                    "article_router_provider_attempts",
                    {"provider_attempt_audit_ref": ref.model_dump(mode="json")},
                    attempt_id=attempt_id,
                    stage_name=_STAGE_NAME,
                    content_key=invoked_content_key,
                )

        try:
            visual_resolver: (
                Callable[[Mapping[str, object]], ArticleRoutingResult | TypedFailure] | None
            )
            visual_payloads: dict[str, bytes] | None = None
            if package_dir is None:
                visual_resolver = None
            else:
                resolved_package_dir = package_dir.resolve(strict=False)
                runner.append_event(
                    execution_id,
                    "route_source",
                    {
                        "package_dir": str(resolved_package_dir),
                        "canonical_package_sha256": package.canonical_content_sha256,
                    },
                    attempt_id=attempt_id,
                    stage_name=_STAGE_NAME,
                    content_key=invoked_content_key,
                )

                # The Router is shown every visual it is asked to classify. It used to
                # be given captions, page numbers and byte sizes and asked to classify
                # from those; the images the package already vouches for are right here.
                visual_payloads = {
                    visual.visual_id: resolved_package_dir.joinpath(
                        *PurePosixPath(visual.asset.relative_path).parts
                    ).read_bytes()
                    for visual in package.visual_assets
                }

                def visual_resolver(
                    routing_submission: Mapping[str, object],
                ) -> ArticleRoutingResult | TypedFailure:
                    return resolve_visual_verification(
                        package_dir=resolved_package_dir,
                        package=package,
                        routing_submission=routing_submission,
                        interaction_create=interaction_create,
                        on_interaction=record_visual_interaction,
                    )

            routed = router.route(
                package=package,
                execution_id=execution_id,
                interaction_create=interaction_create,
                on_interaction=record_interaction,
                on_attempt_audit=record_attempt_audit,
                resolve_visual_verification=visual_resolver,
                visual_payloads=visual_payloads,
                readable_block_ids=identity.readable_block_ids,
                units=units,
            )
            if isinstance(routed, ArticleRoutingResult):
                output_ref = store.put_json(
                    routed.model_dump(mode="json"),
                    media_type=_ROUTING_MEDIA_TYPE,
                )
                return _stage_result(
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    content_key=invoked_content_key,
                    status=ExecutionStatus.SUCCEEDED,
                    output_artifact=output_ref,
                    failure=None,
                    started_at=started_at,
                )
            status = (
                ExecutionStatus.FAILED_RETRYABLE
                if failure_is_retryable(routed.code)
                else ExecutionStatus.FAILED_TERMINAL
            )
            return _stage_result(
                execution_id=execution_id,
                attempt_id=attempt_id,
                content_key=invoked_content_key,
                status=status,
                output_artifact=None,
                failure=routed,
                started_at=started_at,
            )
        except LocalRuntimeError as error:
            failure = _local_failure(error)
            return _stage_result(
                execution_id=execution_id,
                attempt_id=attempt_id,
                content_key=invoked_content_key,
                status=(
                    ExecutionStatus.FAILED_RETRYABLE
                    if failure_is_retryable(failure.code)
                    else ExecutionStatus.FAILED_TERMINAL
                ),
                output_artifact=None,
                failure=failure,
                started_at=started_at,
            )

    result = runner.run_stage(
        execution_id=execution_id,
        stage_name=_STAGE_NAME,
        stage_version=_STAGE_VERSION,
        input_artifacts=(input_artifact,),
        settings=settings,
        invoke=invoke,
        prompt_sha256=prompt_sha256,
        schema_sha256=schema_sha256,
        model_profile=model_profile,
        requires_live=provider_is_live,
        live=live,
    )
    if (
        isinstance(result, StageResult)
        and result.status
        in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.CACHED,
        }
        and finish_execution
    ):
        runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
    return result
