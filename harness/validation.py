from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


MANIFEST_NAME = "MANIFEST.sha256.json"


@dataclass(frozen=True)
class ValidationProblem:
    kind: str
    path: str
    detail: str = ""


@dataclass(frozen=True)
class ValidationReport:
    checked_files: int
    problems: tuple[ValidationProblem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _eligible(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    parts = relative.parts
    if relative.as_posix() == MANIFEST_NAME:
        return False
    if parts and parts[0] in {".git", ".work", ".pytest_cache", ".venv", "venv", "results"}:
        return False
    if any(part == "node_modules" or part == "__pycache__" or part.endswith(".egg-info") for part in parts):
        return False
    if path.suffix in {".pyc", ".pyo"} or path.name.endswith(".tmp"):
        return False
    if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
        return False
    return path.is_file()


def _distributed_files(root: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            (path for path in root.rglob("*") if _eligible(root, path)),
            key=lambda path: path.relative_to(root).as_posix(),
        )
    )


def build_manifest(repo_root: Path | str) -> dict:
    root = Path(repo_root).resolve()
    return {
        "algorithm": "sha256",
        "files": [
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in _distributed_files(root)
        ],
    }


def write_manifest(repo_root: Path | str) -> Path:
    root = Path(repo_root).resolve()
    destination = root / MANIFEST_NAME
    temporary = root / (MANIFEST_NAME + ".tmp")
    temporary.write_text(
        json.dumps(build_manifest(root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def validate_manifest(repo_root: Path | str) -> ValidationReport:
    root = Path(repo_root).resolve()
    manifest_path = root / MANIFEST_NAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[ValidationProblem] = []
    listed: set[str] = set()

    for item in data.get("files", []):
        relative = item.get("path", "")
        candidate_relative = Path(relative)
        if not relative or candidate_relative.is_absolute() or ".." in candidate_relative.parts:
            problems.append(ValidationProblem("unsafe_path", relative))
            continue
        if relative in listed:
            problems.append(ValidationProblem("duplicate", relative))
            continue
        listed.add(relative)
        path = root / candidate_relative
        if not path.is_file():
            problems.append(ValidationProblem("missing", relative))
            continue
        if path.stat().st_size != item.get("size_bytes"):
            problems.append(ValidationProblem("size_mismatch", relative))
        if _sha256(path) != item.get("sha256"):
            problems.append(ValidationProblem("hash_mismatch", relative))

    actual = {path.relative_to(root).as_posix() for path in _distributed_files(root)}
    for relative in sorted(actual - listed):
        problems.append(ValidationProblem("unlisted", relative))
    return ValidationReport(checked_files=len(listed), problems=tuple(problems))
