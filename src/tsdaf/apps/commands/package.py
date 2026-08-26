"""Fixed ``tsdaf package`` commands."""

from __future__ import annotations

import sys
from pathlib import Path

from tsdaf.apps.composition import _canonical_package
from tsdaf.contracts.schema import canonical_json_bytes

__all__ = ("run_package_validate",)


def _emit(payload: dict[str, object], *, error: bool) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def run_package_validate(package_dir: Path) -> int:
    """Validate and summarize one canonical paper package."""

    package, failure = _canonical_package(package_dir)
    if failure is not None:
        _emit(failure, error=True)
        return 1
    assert package is not None
    payload: dict[str, object] = {
        "source_schema_version": package.manifest.schema_version,
        "paper_id": package.manifest.paper_id,
        "block_count": len(package.blocks),
        "visual_count": len(package.visual_assets),
        "total_source_asset_bytes": sum(artifact.byte_size for artifact in package.manifest.assets),
        "canonical_package_sha256": package.canonical_content_sha256,
        "status": "VALID",
    }
    _emit(payload, error=False)
    return 0
