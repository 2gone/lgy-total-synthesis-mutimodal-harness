"""Thin ``tsdaf image`` command."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from tsdaf.apps.image_composition import compose_image_workflow
from tsdaf.contracts.schema import canonical_json_bytes

__all__ = ("run_image",)


def _emit(payload: dict[str, object], *, error: bool) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def run_image(
    job_path: Path,
    source_package_dir: Path,
    *,
    bundle_root: Path,
    workspace: Path,
    device: str,
    workers: int,
    retries: int,
    timeout: float,
    execution_id: str | None,
    live: bool,
) -> int:
    """Run the fixed source-image to molecule-OCSR workflow."""

    resolved_execution_id = execution_id or f"exec_image_{uuid.uuid4().hex}"
    code, payload = compose_image_workflow(
        job_path=job_path,
        source_package_dir=source_package_dir,
        bundle_root=bundle_root,
        workspace=workspace,
        device=device,
        workers=workers,
        retries=retries,
        timeout=timeout,
        execution_id=resolved_execution_id,
        live=live,
    )
    _emit(payload, error=code not in {0, 3})
    return code
