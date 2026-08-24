from __future__ import annotations

import json
from pathlib import Path

from .suites import suite_conditions, suite_papers


class EvaluationBarrierError(RuntimeError):
    def __init__(self, missing_cells: tuple[tuple[str, str], ...]):
        self.missing_cells = missing_cells
        super().__init__(f"Evaluation is blocked; {len(missing_cells)} experiment cells are unsealed")


def expected_cells(repo_root: Path | str, suite: str) -> tuple[tuple[str, str], ...]:
    root = Path(repo_root)
    return tuple(
        (condition, paper_id)
        for condition in suite_conditions(suite)
        for paper_id in suite_papers(root, suite)
    )


def _is_sealed(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        seal = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    return isinstance(seal.get("schema_valid"), bool) and isinstance(seal.get("files"), dict)


def assert_evaluation_ready(repo_root: Path | str, suite: str) -> None:
    root = Path(repo_root).resolve()
    missing = tuple(
        (condition, paper_id)
        for condition, paper_id in expected_cells(root, suite)
        if not _is_sealed(root / "results" / suite / condition / paper_id / "seal.json")
    )
    if missing:
        raise EvaluationBarrierError(missing)


def private_evaluation_files(repo_root: Path | str, suite: str) -> tuple[Path, ...]:
    root = Path(repo_root).resolve()
    assert_evaluation_ready(root, suite)
    private_root = root / "experiments" / suite / "controller_only" / "private_eval"
    if not private_root.is_dir():
        return ()
    return tuple(sorted((path for path in private_root.rglob("*") if path.is_file()), key=str))
