"""Immutable local execution for MolYOLO crops and remote coordinate OCSR.

This is the service branch. It keeps the existing detector and deterministic cropping
verbatim -- those are the part of the image path the service is fed with -- and replaces
only the transcription step: instead of asking a general model for a SMILES string, it
sends each crop to the coordinate-OCSR service and seals the graph that comes back.

The older one-crop SMILES path stays untouched in `image_processing_runtime` and is not
called from here. It remains available as the fallback while the service branch is
unproven.

One crop per job. The port is batch-shaped, so sending a whole crop set in one job is a
later change of this file alone; keeping one crop per stage for now means the cache and
the sealed record stay per molecule, exactly as the existing path does, and a rerun of
one molecule stays a rerun of one molecule.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tsdaf.contracts.execution import ExecutionStatus, StageResult, TypedFailure
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.coordinate_graph import (
    classify_resolvability,
    parse_coordinate_graph,
)
from tsdaf.lanes.image.models import (
    CoordinateOCSRResult,
    CoordinateOCSRStatus,
    MoleculeCrop,
    MoleculeCropSet,
    OCSRServiceRun,
)
from tsdaf.lanes.image.ocsr_service import (
    OCSRCropRequest,
    RawOCSRTranscription,
    bind_transcriptions,
    require_ocsr_service_contract,
)
from tsdaf.lanes.image.source_loader import LoadedSourceImage
from tsdaf.workflow.image_processing_runtime import (
    _CROP_SET_MEDIA_TYPE,
    _DETECTION_STAGE,
    _DETECTION_VERSION,
    _detection_invoker,
    _detection_settings,
    _stage_result,
)
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.ports import MoleculeDetector, OCSRService

_OCSR_STAGE = "coordinate-molecule-ocsr"
_OCSR_VERSION = "1.0.0"
_RESULT_MEDIA_TYPE = "application/vnd.tsdaf.coordinate-ocsr-result+json"
_GRAPH_MEDIA_TYPE = "application/vnd.tsdaf.coordinate-graph.v1+json"
_MOL_MEDIA_TYPE = "chemical/x-mdl-molfile"
_EXCHANGE_MEDIA_TYPE = "application/json"

__all__ = ("CoordinateOCSRRun", "execute_coordinate_ocsr")


@dataclass(frozen=True, slots=True)
class CoordinateOCSRRun:
    crop_stage: StageResult
    crop_set: MoleculeCropSet
    ocsr_stages: tuple[StageResult, ...]
    ocsr_results: tuple[CoordinateOCSRResult, ...]


def _read_crop_set(store: LocalObjectStore, ref: ArtifactRef) -> MoleculeCropSet:
    if ref.media_type != _CROP_SET_MEDIA_TYPE:
        raise ValueError("crop set artifact has an unexpected media type")
    return MoleculeCropSet.model_validate_json(store.read_bytes(ref))


def _read_result(store: LocalObjectStore, ref: ArtifactRef) -> CoordinateOCSRResult:
    if ref.media_type != _RESULT_MEDIA_TYPE:
        raise ValueError("coordinate OCSR artifact has an unexpected media type")
    return CoordinateOCSRResult.model_validate_json(store.read_bytes(ref))


def _seal_structure(
    store: LocalObjectStore,
    transcription: RawOCSRTranscription,
) -> tuple[ArtifactRef | None, ArtifactRef | None]:
    graph_ref = (
        store.put_json(dict(transcription.graph_payload), media_type=_GRAPH_MEDIA_TYPE)
        if transcription.graph_payload is not None
        else None
    )
    mol_ref = (
        store.put_bytes(transcription.external_mol.encode("utf-8"), media_type=_MOL_MEDIA_TYPE)
        if transcription.external_mol
        else None
    )
    return graph_ref, mol_ref


def _build_result(
    *,
    store: LocalObjectStore,
    crop_set: MoleculeCropSet,
    molecule: MoleculeCrop,
    transcription: RawOCSRTranscription,
    service_run: OCSRServiceRun,
) -> CoordinateOCSRResult:
    """Turn one service answer into a sealed record.

    An absent graph is an ordinary outcome and becomes `NO_STRUCTURE`. A graph that is
    present but malformed is a provider fault and raises, because accepting it would
    mean sealing evidence we could not read -- the one case the record must never
    represent as a chemistry result.
    """

    if transcription.graph_payload is None:
        return CoordinateOCSRResult(
            schema_version="tsdaf.coordinate-ocsr-result.v1",
            job_id=crop_set.job_id,
            route_image_item_id=crop_set.route_image_item_id,
            molecule_id=molecule.molecule_id,
            crop_artifact=molecule.crop_artifact,
            service_run=service_run,
            status=CoordinateOCSRStatus.NO_STRUCTURE,
            graph_artifact=None,
            external_mol_artifact=None,
            resolvability=None,
            flatten_blockers=(),
            decision_state=transcription.decision_state,
            errors=transcription.errors,
        )

    resolution = classify_resolvability(parse_coordinate_graph(dict(transcription.graph_payload)))
    graph_ref, mol_ref = _seal_structure(store, transcription)
    return CoordinateOCSRResult(
        schema_version="tsdaf.coordinate-ocsr-result.v1",
        job_id=crop_set.job_id,
        route_image_item_id=crop_set.route_image_item_id,
        molecule_id=molecule.molecule_id,
        crop_artifact=molecule.crop_artifact,
        service_run=service_run,
        status=CoordinateOCSRStatus.TRANSCRIBED,
        graph_artifact=graph_ref,
        external_mol_artifact=mol_ref,
        resolvability=resolution.resolvability,
        flatten_blockers=resolution.blockers,
        decision_state=transcription.decision_state,
        errors=transcription.errors,
    )


def execute_coordinate_ocsr(
    *,
    store: LocalObjectStore,
    runner: LocalRunner,
    execution_id: str,
    source: LoadedSourceImage,
    detector: MoleculeDetector,
    service: OCSRService,
    provider_is_live: bool,
    live: bool,
    expand_substituents: bool = True,
    workers: int = 4,
) -> CoordinateOCSRRun | TypedFailure:
    """Run or reuse one image job with one coordinate-OCSR stage per crop."""

    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError("workers must be a positive integer")
    require_ocsr_service_contract(service.service_contract)

    crop_stage = runner.run_stage(
        execution_id=execution_id,
        stage_name=_DETECTION_STAGE,
        stage_version=_DETECTION_VERSION,
        input_artifacts=(source.job_artifact, source.source_artifact),
        settings=_detection_settings(detector),
        invoke=_detection_invoker(
            store=store,
            execution_id=execution_id,
            source=source,
            detector=detector,
        ),
    )
    if isinstance(crop_stage, TypedFailure):
        return crop_stage
    if crop_stage.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
        raise ValueError("detection stage did not complete successfully")
    assert crop_stage.output_artifact is not None
    crop_set = _read_crop_set(store, crop_stage.output_artifact)

    def run_one(molecule: MoleculeCrop) -> tuple[StageResult, CoordinateOCSRResult] | TypedFailure:
        crop_bytes = store.read_bytes(molecule.crop_artifact)
        # The service contract is the only request-side identity a remote service offers:
        # what it reports about itself arrives in the response, too late to key a cache.
        settings = {
            "molecule_id": molecule.molecule_id,
            "service_contract": service.service_contract,
            "expand_substituents": expand_substituents,
        }

        def invoke_ocsr(attempt_id: str, content_key: str) -> StageResult:
            started_at = datetime.now(UTC)
            request = OCSRCropRequest(
                source_name=molecule.molecule_id,
                crop_bytes=crop_bytes,
            )
            outcome = service.transcribe((request,), expand_substituents=expand_substituents)
            bound = bind_transcriptions((request,), outcome)
            request_ref = store.put_bytes(
                outcome.request_bytes,
                media_type=_EXCHANGE_MEDIA_TYPE,
            )
            response_ref = store.put_bytes(
                outcome.response_bytes,
                media_type=_EXCHANGE_MEDIA_TYPE,
            )
            runner.append_event(
                execution_id,
                "coordinate_ocsr_interaction",
                {
                    "molecule_id": molecule.molecule_id,
                    "job_id": outcome.job_id,
                    "service_contract": service.service_contract,
                    "ocsr_model": outcome.provenance.ocsr_model,
                    "review_model": outcome.provenance.review_model,
                    "request_artifact": request_ref.model_dump(mode="json"),
                    "response_artifact": response_ref.model_dump(mode="json"),
                    "elapsed_ms": outcome.elapsed_ms,
                },
                attempt_id=attempt_id,
                stage_name=_OCSR_STAGE,
                content_key=content_key,
            )
            parsed = _build_result(
                store=store,
                crop_set=crop_set,
                molecule=molecule,
                transcription=bound[0][1],
                service_run=OCSRServiceRun(
                    service_contract=service.service_contract,
                    ocsr_model=outcome.provenance.ocsr_model,
                    review_model=outcome.provenance.review_model,
                    expand_substituents=outcome.provenance.expand_substituents,
                ),
            )
            output = store.put_json(
                parsed.model_dump(mode="json", by_alias=True, exclude_none=False),
                media_type=_RESULT_MEDIA_TYPE,
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
            model_profile=service.service_contract,
            requires_live=provider_is_live,
            live=live,
        )
        if isinstance(stage, TypedFailure):
            return stage
        if stage.status not in {ExecutionStatus.SUCCEEDED, ExecutionStatus.CACHED}:
            raise ValueError("coordinate OCSR stage did not complete successfully")
        assert stage.output_artifact is not None
        return stage, _read_result(store, stage.output_artifact)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        values: list[Any] = list(pool.map(run_one, crop_set.molecules))
    failures = [value for value in values if isinstance(value, TypedFailure)]
    if failures:
        return failures[0]
    completed = [value for value in values if not isinstance(value, TypedFailure)]
    return CoordinateOCSRRun(
        crop_stage=crop_stage,
        crop_set=crop_set,
        ocsr_stages=tuple(value[0] for value in completed),
        ocsr_results=tuple(value[1] for value in completed),
    )
