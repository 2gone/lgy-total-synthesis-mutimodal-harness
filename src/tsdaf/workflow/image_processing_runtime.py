"""Immutable local execution for MolYOLO crops and one-crop Gemini OCSR."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tsdaf.contracts.execution import ExecutionStatus, StageResult, TypedFailure
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.cropper import materialize_crop_set
from tsdaf.lanes.image.detector import prepare_detections
from tsdaf.lanes.image.models import (
    CropPolicy,
    GeminiOCSRResult,
    MoleculeCrop,
    MoleculeCropSet,
)
from tsdaf.lanes.image.ocsr import MoleculeOCSR
from tsdaf.lanes.image.source_loader import LoadedSourceImage
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.ports import MoleculeDetector

_DETECTION_STAGE = "molyolo-detect-crop"
_DETECTION_VERSION = "1.0.0"
_OCSR_STAGE = "gemini-molecule-ocsr"
_OCSR_VERSION = "1.0.0"
_CROP_SET_MEDIA_TYPE = "application/vnd.tsdaf.molecule-crop-set+json"
_OCSR_MEDIA_TYPE = "application/vnd.tsdaf.gemini-ocsr-result+json"
_INTERACTION_MEDIA_TYPE = "application/json"
_DEFAULT_CROP_POLICY = CropPolicy(
    horizontal_padding_fraction=0.15,
    vertical_padding_fraction=0.20,
    minimum_padding_pixels=16,
)


@dataclass(frozen=True, slots=True)
class ImageProcessingRun:
    crop_stage: StageResult
    crop_set: MoleculeCropSet
    ocsr_stages: tuple[StageResult, ...]
    ocsr_results: tuple[GeminiOCSRResult, ...]


def _stage_result(
    *,
    execution_id: str,
    attempt_id: str,
    stage_name: str,
    stage_version: str,
    content_key: str,
    output: ArtifactRef,
    started_at: datetime,
) -> StageResult:
    return StageResult(
        schema_version="tsdaf.stage-result.v1",
        execution_id=execution_id,
        attempt_id=attempt_id,
        stage_name=stage_name,
        stage_version=stage_version,
        input_content_key=content_key,
        status=ExecutionStatus.SUCCEEDED,
        output_artifact=output,
        failure=None,
        cached_from_result=None,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )


def _read_crop_set(store: LocalObjectStore, ref: ArtifactRef) -> MoleculeCropSet:
    if ref.media_type != _CROP_SET_MEDIA_TYPE:
        raise ValueError("crop-set artifact has the wrong media type")
    value = MoleculeCropSet.model_validate_json(store.read_bytes(ref))
    store.verify(value.source_image_artifact)
    store.verify(value.review_image_artifact)
    for molecule in value.molecules:
        store.verify(molecule.crop_artifact)
    return value


def _read_ocsr_result(store: LocalObjectStore, ref: ArtifactRef) -> GeminiOCSRResult:
    if ref.media_type != _OCSR_MEDIA_TYPE:
        raise ValueError("OCSR result artifact has the wrong media type")
    value = GeminiOCSRResult.model_validate_json(store.read_bytes(ref))
    store.verify(value.crop_artifact)
    return value


def run_image_processing(
    *,
    store: LocalObjectStore,
    runner: LocalRunner,
    execution_id: str,
    source: LoadedSourceImage,
    detector: MoleculeDetector,
    ocsr: MoleculeOCSR,
    interaction_create: Callable[..., object],
    provider_is_live: bool,
    live: bool,
    workers: int = 4,
    retries: int = 2,
    timeout: float = 120.0,
) -> ImageProcessingRun | TypedFailure:
    """Run or reuse one image job with one independent OCSR stage per crop."""

    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
        raise ValueError("retries must be a nonnegative integer")
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ValueError("timeout must be a positive number")
    if not callable(interaction_create):
        raise TypeError("interaction_create must be callable")

    detection_settings = {
        "detector": detector.provenance.model_dump(mode="json"),
        "crop_policy": _DEFAULT_CROP_POLICY.model_dump(mode="json"),
    }

    def invoke_detection(attempt_id: str, content_key: str) -> StageResult:
        started_at = datetime.now(UTC)
        raw = detector.detect(source.verified_local_path)
        prepared = prepare_detections(
            route_image_item_id=source.job.route_image_item.route_image_item_id,
            width=source.dimensions[0],
            height=source.dimensions[1],
            raw=raw,
            iou_threshold=float(detector.provenance.iou_threshold),
        )
        crop_set = materialize_crop_set(
            source=source,
            detections=prepared,
            raw_detection_count=len(raw),
            detector=detector.provenance,
            store=store,
            policy=_DEFAULT_CROP_POLICY,
        )
        output = store.put_json(
            crop_set.model_dump(mode="json", by_alias=True, exclude_none=False),
            media_type=_CROP_SET_MEDIA_TYPE,
        )
        return _stage_result(
            execution_id=execution_id,
            attempt_id=attempt_id,
            stage_name=_DETECTION_STAGE,
            stage_version=_DETECTION_VERSION,
            content_key=content_key,
            output=output,
            started_at=started_at,
        )

    crop_stage = runner.run_stage(
        execution_id=execution_id,
        stage_name=_DETECTION_STAGE,
        stage_version=_DETECTION_VERSION,
        input_artifacts=(source.job_artifact, source.source_artifact),
        settings=detection_settings,
        invoke=invoke_detection,
    )
    if isinstance(crop_stage, TypedFailure):
        return crop_stage
    if crop_stage.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
        raise ValueError("detection stage did not complete successfully")
    assert crop_stage.output_artifact is not None
    crop_set = _read_crop_set(store, crop_stage.output_artifact)

    def run_one(molecule: MoleculeCrop) -> tuple[StageResult, GeminiOCSRResult] | TypedFailure:
        crop_bytes = store.read_bytes(molecule.crop_artifact)
        settings = {
            "molecule_id": molecule.molecule_id,
            "retries": retries,
            "timeout_seconds": float(timeout),
        }

        def invoke_ocsr(attempt_id: str, content_key: str) -> StageResult:
            started_at = datetime.now(UTC)
            request = ocsr.build_request(molecule=molecule, crop_bytes=crop_bytes)
            interaction: Any = interaction_create(
                input=request.input,
                system_instruction=request.system_instruction,
                tools=None,
                response_format=request.response_format,
            )
            request_ref = store.put_bytes(
                interaction.request_bytes,
                media_type=_INTERACTION_MEDIA_TYPE,
            )
            response_ref = store.put_bytes(
                interaction.response_bytes,
                media_type=_INTERACTION_MEDIA_TYPE,
            )
            runner.append_event(
                execution_id,
                "molecule_ocsr_interaction",
                {
                    "molecule_id": molecule.molecule_id,
                    "interaction_id": interaction.interaction_id,
                    "request_id": interaction.request_id,
                    "request_artifact": request_ref.model_dump(mode="json"),
                    "response_artifact": response_ref.model_dump(mode="json"),
                    "total_input_tokens": interaction.total_input_tokens,
                    "total_output_tokens": interaction.total_output_tokens,
                    "total_tokens": interaction.total_tokens,
                    "total_cached_tokens": interaction.total_cached_tokens,
                    "total_thought_tokens": interaction.total_thought_tokens,
                    "total_tool_use_tokens": interaction.total_tool_use_tokens,
                    "elapsed_ms": interaction.elapsed_ms,
                    "transport_attempts": interaction.transport_attempts,
                },
                attempt_id=attempt_id,
                stage_name=_OCSR_STAGE,
                content_key=content_key,
            )
            parsed = ocsr.parse_result(
                interaction.output_text,
                job_id=crop_set.job_id,
                route_image_item_id=crop_set.route_image_item_id,
                molecule=molecule,
            )
            output = store.put_json(
                parsed.model_dump(mode="json", by_alias=True, exclude_none=False),
                media_type=_OCSR_MEDIA_TYPE,
            )
            return _stage_result(
                execution_id=execution_id,
                attempt_id=attempt_id,
                stage_name=_OCSR_STAGE,
                stage_version=_OCSR_VERSION,
                content_key=content_key,
                output=output,
                started_at=started_at,
            )

        stage = runner.run_stage(
            execution_id=execution_id,
            stage_name=_OCSR_STAGE,
            stage_version=_OCSR_VERSION,
            input_artifacts=(molecule.crop_artifact,),
            settings=settings,
            invoke=invoke_ocsr,
            prompt_sha256=ocsr.prompt.prompt_sha256,
            schema_sha256=ocsr.prompt.schema_sha256,
            model_profile=ocsr.prompt.model_profile,
            requires_live=provider_is_live,
            live=live,
        )
        if isinstance(stage, TypedFailure):
            return stage
        if stage.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
            raise ValueError("OCSR stage did not complete successfully")
        assert stage.output_artifact is not None
        return stage, _read_ocsr_result(store, stage.output_artifact)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        values = list(pool.map(run_one, crop_set.molecules))
    failures = [value for value in values if isinstance(value, TypedFailure)]
    if failures:
        return failures[0]
    completed = [value for value in values if not isinstance(value, TypedFailure)]
    return ImageProcessingRun(
        crop_stage=crop_stage,
        crop_set=crop_set,
        ocsr_stages=tuple(value[0] for value in completed),
        ocsr_results=tuple(value[1] for value in completed),
    )


__all__ = ("ImageProcessingRun", "run_image_processing")
