from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from .api import ApiResponse, build_chat_payload, extract_response_text, post_chat_completion
from .config import MODEL_NAME, HarnessConfig
from .sealing import UnparseableResponseError, record_attempt, seal_observation
from .suites import InputPlan, build_input_plan


class RunFailedError(RuntimeError):
    pass


class MissingPrerequisiteError(RuntimeError):
    pass


Sender = Callable[[HarnessConfig, dict[str, Any]], ApiResponse]


def execute_observation(
    *,
    run_dir: Path | str,
    settings: HarnessConfig,
    payload: dict[str, Any],
    schema: dict[str, Any],
    sender: Sender = post_chat_completion,
    max_attempts: int = 3,
    observation_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if payload.get("model") != MODEL_NAME or settings.model != MODEL_NAME:
        raise ValueError(f"Only {MODEL_NAME} is permitted")

    destination = Path(run_dir)
    for _ in range(max_attempts):
        response = sender(settings, payload)
        metadata = {
            **settings.public_metadata(),
            **(observation_metadata or {}),
            **response.metadata,
            "http_status": response.status,
            "wire_format": response.wire_format,
        }
        if not 200 <= response.status < 300:
            record_attempt(
                run_dir=destination,
                raw_response=response.raw,
                metadata=metadata,
                wire_format=response.wire_format,
            )
            continue
        try:
            response_text = extract_response_text(response)
        except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError) as exc:
            record_attempt(
                run_dir=destination,
                raw_response=response.raw,
                metadata={**metadata, "decode_error": type(exc).__name__},
                wire_format=response.wire_format,
            )
            continue
        try:
            return seal_observation(
                run_dir=destination,
                raw_response=response.raw,
                response_text=response_text,
                schema=schema,
                metadata=metadata,
                wire_format=response.wire_format,
            )
        except UnparseableResponseError:
            continue
    raise RunFailedError(f"Gateway or response parsing failed after {max_attempts} attempts")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _suite_files(repo_root: Path, suite: str) -> tuple[Path, Path]:
    suite_root = repo_root / "experiments" / suite
    if suite == "ocr_ablation":
        return (
            suite_root / "prompts" / "worker_prompt.md",
            suite_root / "templates" / "gemini_evidence_output.schema.json",
        )
    return (
        suite_root / "prompts" / "worker_prompt.md",
        suite_root / "templates" / "route_image_output.schema.json",
    )


def _inventory(repo_root: Path, plan: InputPlan) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for asset in plan.assets:
        exists = asset.path.is_file()
        inventory.append(
            {
                "path": asset.path.relative_to(repo_root).as_posix(),
                "media_type": asset.media_type,
                "exists": exists,
                "size_bytes": asset.path.stat().st_size if exists else None,
                "sha256": _sha256(asset.path) if exists else None,
            }
        )
    return inventory


def _check_prerequisites(repo_root: Path, plan: InputPlan) -> None:
    missing = [asset.path for asset in plan.assets if not asset.path.is_file()]
    if plan.suite == "ocr_ablation" and plan.condition == "C_pdf_hires_ocr":
        ocr_root = repo_root / "results" / "ocr_ablation" / "ocr" / plan.paper_id
        if missing or not (ocr_root / "seal.json").is_file():
            raise MissingPrerequisiteError(
                f"OCR artifact and seal are required before Condition C for {plan.paper_id}"
            )
    if missing:
        raise MissingPrerequisiteError("Missing input assets: " + ", ".join(str(path) for path in missing))


def dry_run_summary(
    repo_root: Path | str,
    suite: str,
    condition: str,
    paper_id: str,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    plan = build_input_plan(root, suite, condition, paper_id)
    prompt_path, schema_path = _suite_files(root, suite)
    inventory = _inventory(root, plan)
    ready = all(item["exists"] for item in inventory) and prompt_path.is_file() and schema_path.is_file()
    if suite == "ocr_ablation" and condition == "C_pdf_hires_ocr":
        ready = ready and (
            root / "results" / suite / "ocr" / paper_id / "seal.json"
        ).is_file()
    return {
        "suite": suite,
        "condition": condition,
        "paper_id": paper_id,
        "model": MODEL_NAME,
        "will_call_api": False,
        "ready": ready,
        "assets": inventory,
        "worker_prompt": prompt_path.relative_to(root).as_posix(),
        "schema": schema_path.relative_to(root).as_posix(),
        "result_dir": (root / "results" / suite / condition / paper_id).relative_to(root).as_posix(),
        "prompt_prefix": plan.prompt_prefix,
    }


def run_cell(
    *,
    repo_root: Path | str,
    suite: str,
    condition: str,
    paper_id: str,
    settings: HarnessConfig,
    sender: Sender = post_chat_completion,
    max_attempts: int = 3,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    result_dir = root / "results" / suite / condition / paper_id
    seal_path = result_dir / "seal.json"
    if seal_path.is_file():
        return json.loads(seal_path.read_text(encoding="utf-8"))

    plan = build_input_plan(root, suite, condition, paper_id)
    _check_prerequisites(root, plan)
    prompt_path, schema_path = _suite_files(root, suite)
    if not prompt_path.is_file() or not schema_path.is_file():
        raise MissingPrerequisiteError("Worker prompt or output schema is missing")
    worker_prompt = prompt_path.read_text(encoding="utf-8")
    worker_prompt = worker_prompt.replace("${PAPER_ID}", paper_id).replace("${CONDITION}", condition)
    prompt = (plan.prompt_prefix + "\n\n" + worker_prompt).strip()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = build_chat_payload(settings, prompt, plan.assets, schema=schema)
    observation_metadata = {
        "suite": suite,
        "condition": condition,
        "paper_id": paper_id,
        "input_inventory": _inventory(root, plan),
        "worker_prompt_sha256": _sha256(prompt_path),
        "schema_sha256": _sha256(schema_path),
    }
    return execute_observation(
        run_dir=result_dir,
        settings=settings,
        payload=payload,
        schema=schema,
        sender=sender,
        max_attempts=max_attempts,
        observation_metadata=observation_metadata,
    )
