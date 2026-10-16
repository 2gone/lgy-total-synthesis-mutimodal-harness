"""Application composition for the fixed image-processing v1 workflow."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from tsdaf.adapters.gemini.interactions import GeminiInteractions
from tsdaf.adapters.moldet.molyolo_v10 import MolyoloBundleError, MolYoloV10Adapter
from tsdaf.apps._provider_config import safe_base_url
from tsdaf.contracts.execution import ExecutionStatus, FailureCode, TypedFailure
from tsdaf.contracts.handoff import ImageProcessingJob
from tsdaf.lanes.image.ocsr import MoleculeOCSR
from tsdaf.lanes.image.source_loader import SourceImageLoadError, load_source_image
from tsdaf.workflow.image_processing_runtime import ImageProcessingRun, run_image_processing
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner, LocalRuntimeError
from tsdaf.workflow.ports import MoleculeDetector

__all__ = ("compose_image_workflow", "execute_image_workflow")

_PROMPT_FILES = ("manifest.toml", "system.md", "task.md")


def _failure_payload(
    code: FailureCode | str,
    message: str,
    *,
    execution_id: str,
    status: str = "ERROR",
) -> dict[str, object]:
    return {
        "execution_id": execution_id,
        "status": status,
        "code": code.value if isinstance(code, FailureCode) else code,
        "message": message,
    }


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
            prompt_dir = candidate / "prompts" / "molecule_ocsr" / "v1"
            if (candidate / "pyproject.toml").is_file() and all(
                (prompt_dir / name).is_file() for name in _PROMPT_FILES
            ):
                return prompt_dir
    return None


def _offline_provider(**request: object) -> object:
    del request
    raise AssertionError("offline image processing attempted a provider interaction")


def compose_image_workflow(
    *,
    job_path: Path,
    source_package_dir: Path,
    bundle_root: Path,
    workspace: Path,
    device: str = "auto",
    workers: int = 4,
    retries: int = 2,
    timeout: float = 120.0,
    execution_id: str,
    live: bool,
    environment: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    """Assemble external adapters around the private image lane."""

    prompt_dir = _find_prompt_directory()
    if prompt_dir is None:
        return 2, _failure_payload(
            "CONFIGURATION_INVALID",
            "The molecule-OCSR prompt checkout could not be located.",
            execution_id=execution_id,
        )

    interaction_create: Callable[..., object] = _offline_provider
    if live:
        values = os.environ if environment is None else environment
        api_key = values.get("OPENAI_API_KEY")
        base_url = safe_base_url(values.get("OPENAI_BASE_URL"))
        if api_key is None or not api_key.strip():
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                "Explicit live OCSR requires a nonblank OPENAI_API_KEY.",
                execution_id=execution_id,
            )
        if base_url is None:
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                "Explicit live OCSR requires a safe OPENAI_BASE_URL.",
                execution_id=execution_id,
            )
        try:
            gemini = GeminiInteractions.for_molecule_ocsr(
                api_key=api_key.strip(),
                base_url=base_url,
                timeout=timeout,
                retries=retries,
            )
        except Exception:
            return 2, _failure_payload(
                "CONFIGURATION_INVALID",
                "The fixed OpenAI-compatible OCSR adapter could not be initialized.",
                execution_id=execution_id,
            )
        interaction_create = gemini.create

    try:
        detector = MolYoloV10Adapter(bundle_root, device=device)
    except (MolyoloBundleError, OSError, TypeError, ValueError):
        return 2, _failure_payload(
            "CONFIGURATION_INVALID",
            "The external MolYOLO bundle could not be initialized.",
            execution_id=execution_id,
        )

    return execute_image_workflow(
        job_path=job_path,
        source_package_dir=source_package_dir,
        workspace=workspace,
        execution_id=execution_id,
        detector=detector,
        interaction_create=interaction_create,
        provider_is_live=True,
        live=live,
        prompt_dir=prompt_dir,
        workers=workers,
        retries=retries,
        timeout=timeout,
    )


def execute_image_workflow(
    *,
    job_path: Path,
    source_package_dir: Path,
    workspace: Path,
    execution_id: str,
    detector: MoleculeDetector,
    interaction_create: Callable[..., object],
    provider_is_live: bool,
    live: bool,
    prompt_dir: Path,
    workers: int = 4,
    retries: int = 2,
    timeout: float = 120.0,
) -> tuple[int, dict[str, object]]:
    """Load one authorized job, run image processing, and seal its execution."""

    if not isinstance(job_path, Path) or not isinstance(source_package_dir, Path):
        raise TypeError("job and source package paths must be pathlib.Path values")
    if not isinstance(workspace, Path) or not isinstance(prompt_dir, Path):
        raise TypeError("workspace and prompt_dir must be pathlib.Path values")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ValueError("execution_id must be a nonempty string")

    try:
        job = ImageProcessingJob.model_validate_json(job_path.read_bytes())
        store = LocalObjectStore(workspace)
        runner = LocalRunner(workspace, store)
        source = load_source_image(job=job, package_dir=source_package_dir, store=store)
        ocsr = MoleculeOCSR(prompt_dir)
        outcome = run_image_processing(
            store=store,
            runner=runner,
            execution_id=execution_id,
            source=source,
            detector=detector,
            ocsr=ocsr,
            interaction_create=interaction_create,
            provider_is_live=provider_is_live,
            live=live,
            workers=workers,
            retries=retries,
            timeout=timeout,
        )
        if isinstance(outcome, TypedFailure):
            return (
                3 if outcome.code is FailureCode.LIVE_REQUIRED else 1,
                _failure_payload(
                    outcome.code,
                    outcome.message,
                    execution_id=execution_id,
                    status="NOT_STARTED",
                ),
            )
        assert isinstance(outcome, ImageProcessingRun)
        runner.finish_execution(execution_id, ExecutionStatus.SUCCEEDED)
        assert outcome.crop_stage.output_artifact is not None
        ocsr_artifacts = [
            stage.output_artifact.model_dump(mode="json")
            for stage in outcome.ocsr_stages
            if stage.output_artifact is not None
        ]
        return 0, {
            "execution_id": execution_id,
            "status": ExecutionStatus.SUCCEEDED.value,
            "job_id": job.job_id,
            "route_image_item_id": job.route_image_item.route_image_item_id,
            "molecule_count": len(outcome.crop_set.molecules),
            "ocsr_result_count": len(outcome.ocsr_results),
            "crop_set_artifact": outcome.crop_stage.output_artifact.model_dump(mode="json"),
            "ocsr_result_artifacts": ocsr_artifacts,
        }
    except SourceImageLoadError as error:
        return 1, _failure_payload(
            error.code,
            str(error),
            execution_id=execution_id,
        )
    except LocalRuntimeError as error:
        return 1, _failure_payload(
            error.code,
            "The local image execution evidence could not be verified.",
            execution_id=execution_id,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 1, _failure_payload(
            FailureCode.PACKAGE_INVALID,
            "The image processing job or runtime configuration is invalid.",
            execution_id=execution_id,
        )
