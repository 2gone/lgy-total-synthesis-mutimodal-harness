from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


PAPERS = (
    "ineleganolide",
    "alchivemycin_A",
    "hyperfirin",
    "kalmanol",
    "principinol_B",
)


def _evaluation_module():
    try:
        return importlib.import_module("harness.evaluation")
    except ModuleNotFoundError:
        pytest.fail("Evaluation barriers are not implemented yet")


def _make_ocr_repo(root: Path) -> None:
    manifest = {
        "papers": [
            {
                "paper_id": paper,
                "pdf_path": f"papers/{paper}.pdf",
                "hires_page_paths": [],
            }
            for paper in PAPERS
        ]
    }
    manifest_path = root / "experiments/ocr_ablation/manifests/source_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    private_path = root / "experiments/ocr_ablation/controller_only/private_eval/reference.json"
    private_path.parent.mkdir(parents=True)
    private_path.write_text("{}", encoding="utf-8")


def test_private_evaluation_is_blocked_until_all_fifteen_cells_are_sealed(tmp_path) -> None:
    """Catches early gold-reference access that could bias later conditions."""
    evaluation = _evaluation_module()
    _make_ocr_repo(tmp_path)

    with pytest.raises(evaluation.EvaluationBarrierError) as blocked:
        evaluation.private_evaluation_files(tmp_path, "ocr_ablation")

    assert len(blocked.value.missing_cells) == 15
    assert ("A_pdf_only", "ineleganolide") in blocked.value.missing_cells

    for condition, paper_id in evaluation.expected_cells(tmp_path, "ocr_ablation"):
        seal = tmp_path / "results" / "ocr_ablation" / condition / paper_id / "seal.json"
        seal.parent.mkdir(parents=True)
        seal.write_text('{"schema_valid":true,"files":{}}', encoding="utf-8")

    private_files = evaluation.private_evaluation_files(tmp_path, "ocr_ablation")

    assert [path.name for path in private_files] == ["reference.json"]


def test_unsealed_output_does_not_satisfy_evaluation_barrier(tmp_path) -> None:
    """Catches treating an editable parsed file as an immutable completed observation."""
    evaluation = _evaluation_module()
    _make_ocr_repo(tmp_path)
    editable = tmp_path / "results/ocr_ablation/A_pdf_only/ineleganolide/parsed_output.json"
    editable.parent.mkdir(parents=True)
    editable.write_text("{}", encoding="utf-8")

    with pytest.raises(evaluation.EvaluationBarrierError) as blocked:
        evaluation.assert_evaluation_ready(tmp_path, "ocr_ablation")

    assert ("A_pdf_only", "ineleganolide") in blocked.value.missing_cells
