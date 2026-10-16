from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_NOT_FOUND = (
    "tsdaf docs check: repository checkout not found "
    "(requires pyproject.toml and scripts/validate_docs.py)\n"
)


def find_repository_root(start: Path) -> Path | None:
    location = start.resolve()
    if location.is_file():
        location = location.parent
    for candidate in (location, *location.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "scripts" / "validate_docs.py"
        ).is_file():
            return candidate
    return None


def run_docs_check(start: Path | None = None) -> int:
    root = find_repository_root(start or Path.cwd())
    if root is None:
        sys.stderr.write(_NOT_FOUND)
        return 2
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "validate_docs.py")],
        cwd=root,
        shell=False,
        check=False,
    )
    return result.returncode
