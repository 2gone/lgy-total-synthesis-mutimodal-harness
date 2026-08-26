from __future__ import annotations

import importlib
import hashlib
import json
from pathlib import Path
import subprocess

import pytest


ALPHA_SHA256 = "8ed3f6ad685b959ead7022518e1af76cd816f8e8ec7ccdda1ed4018e8f2223f8"
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREE_ROOT = REPO_ROOT.parents[1]


def _validation_module():
    try:
        return importlib.import_module("harness.validation")
    except ModuleNotFoundError:
        pytest.fail("Repository manifest validation is not implemented yet")


def _write_literal_manifest(root) -> None:
    manifest = {
        "algorithm": "sha256",
        "files": [{"path": "a.txt", "size_bytes": 5, "sha256": ALPHA_SHA256}],
    }
    (root / "MANIFEST.sha256.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_manifest_validator_reports_modified_missing_and_unlisted_files(tmp_path) -> None:
    """Catches asset substitution, deletion, and untracked delivery files."""
    validation = _validation_module()
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    _write_literal_manifest(tmp_path)
    assert validation.validate_manifest(tmp_path).ok is True

    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    modified = validation.validate_manifest(tmp_path)
    assert modified.ok is False
    assert any(problem.kind == "hash_mismatch" for problem in modified.problems)

    (tmp_path / "a.txt").unlink()
    missing = validation.validate_manifest(tmp_path)
    assert any(problem.kind == "missing" for problem in missing.problems)

    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("extra", encoding="utf-8")
    unlisted = validation.validate_manifest(tmp_path)
    assert any(problem.kind == "unlisted" and problem.path == "extra.txt" for problem in unlisted.problems)


def test_manifest_builder_is_deterministic_and_excludes_git_metadata(tmp_path) -> None:
    """Catches nondeterministic ordering or accidental hashing of local Git internals."""
    validation = _validation_module()
    (tmp_path / "z.txt").write_text("alpha", encoding="utf-8")
    git_file = tmp_path / ".git/objects/local"
    git_file.parent.mkdir(parents=True)
    git_file.write_text("never distribute", encoding="utf-8")

    manifest = validation.build_manifest(tmp_path)

    assert manifest == {
        "algorithm": "sha256",
        "files": [{"path": "z.txt", "size_bytes": 5, "sha256": ALPHA_SHA256}],
    }


def test_git_index_bytes_match_the_distributed_manifest() -> None:
    """Catches checkout-time line-ending conversion that invalidates distributed hashes."""
    if not (WORKTREE_ROOT / ".git").exists():
        pytest.skip("Git index is unavailable in a source archive")
    manifest = json.loads((REPO_ROOT / "MANIFEST.sha256.json").read_text(encoding="utf-8"))
    mismatches = []
    for item in manifest["files"]:
        completed = subprocess.run(
            ["git", "show", f":tests/legacy_harness/{item['path']}"],
            cwd=WORKTREE_ROOT,
            check=True,
            stdout=subprocess.PIPE,
        )
        if len(completed.stdout) != item["size_bytes"] or hashlib.sha256(
            completed.stdout
        ).hexdigest() != item["sha256"]:
            mismatches.append(item["path"])

    assert mismatches == []
