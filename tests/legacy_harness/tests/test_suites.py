from __future__ import annotations

from pathlib import Path
import importlib

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _suites_module():
    try:
        return importlib.import_module("harness.suites")
    except ModuleNotFoundError:
        pytest.fail("Suite inventory and input isolation are not implemented yet")


def _relative_assets(plan) -> list[str]:
    return [asset.path.relative_to(REPO_ROOT).as_posix() for asset in plan.assets]


def test_suite_inventories_keep_three_conditions_and_five_papers_separate() -> None:
    """Catches renamed or dropped cells that would make the comparison incomplete."""
    suites = _suites_module()
    assert suites.suite_conditions("ocr_ablation") == (
        "A_pdf_only",
        "B_pdf_hires",
        "C_pdf_hires_ocr",
    )
    assert suites.suite_conditions("route_image_understanding") == (
        "A_full_pdf",
        "B_raw_route_images",
        "C_tokenized_route_images",
    )
    assert suites.suite_papers(REPO_ROOT, "ocr_ablation") == (
        "ineleganolide",
        "alchivemycin_A",
        "hyperfirin",
        "kalmanol",
        "principinol_B",
    )
    assert suites.suite_papers(REPO_ROOT, "route_image_understanding") == suites.suite_papers(
        REPO_ROOT, "ocr_ablation"
    )


def test_ocr_conditions_add_only_the_declared_evidence_layers() -> None:
    """Catches cross-condition leakage in the OCR ablation."""
    suites = _suites_module()
    pdf_only = _relative_assets(
        suites.build_input_plan(REPO_ROOT, "ocr_ablation", "A_pdf_only", "ineleganolide")
    )
    hires = _relative_assets(
        suites.build_input_plan(REPO_ROOT, "ocr_ablation", "B_pdf_hires", "ineleganolide")
    )
    with_ocr = _relative_assets(
        suites.build_input_plan(REPO_ROOT, "ocr_ablation", "C_pdf_hires_ocr", "ineleganolide")
    )

    assert pdf_only == ["corpus/papers/ineleganolide.pdf"]
    assert hires[0] == pdf_only[0]
    assert hires[1:] == [
        f"experiments/ocr_ablation/hires_pages/ineleganolide/page-{number}.png"
        for number in range(1, 6)
    ]
    assert with_ocr[:-1] == hires
    assert with_ocr[-1] == "results/ocr_ablation/ocr/ineleganolide/ocr_output.json"


def test_raw_route_condition_receives_images_only() -> None:
    """Catches accidental PDF, token image, or annotated-manifest leakage into Route B."""
    suites = _suites_module()
    plan = suites.build_input_plan(
        REPO_ROOT,
        "route_image_understanding",
        "B_raw_route_images",
        "alchivemycin_A",
    )
    assets = _relative_assets(plan)

    assert assets == [
        "experiments/route_image_understanding/route_images/raw/alchivemycin_A/alchivemycin_fig3a.png",
        "experiments/route_image_understanding/route_images/raw/alchivemycin_A/alchivemycin_fig4.png",
        "experiments/route_image_understanding/route_images/raw/alchivemycin_A/alchivemycin_fig5c.png",
    ]
    assert all(path.endswith(".png") and "/raw/" in path for path in assets)
    assert plan.prompt_prefix == ""


def test_tokenized_route_condition_has_exact_preface_and_no_other_input_type() -> None:
    """Catches identity-semantics drift or raw/PDF leakage into Route C."""
    suites = _suites_module()
    plan = suites.build_input_plan(
        REPO_ROOT,
        "route_image_understanding",
        "C_tokenized_route_images",
        "kalmanol",
    )
    assets = _relative_assets(plan)

    assert assets == [
        "experiments/route_image_understanding/route_images/tokenized/kalmanol/kalmanol_scheme2.png",
        "experiments/route_image_understanding/route_images/tokenized/kalmanol/kalmanol_scheme3.png",
    ]
    assert plan.prompt_prefix == suites.TOKEN_IDENTITY_PREFACE
    assert all("/tokenized/" in path for path in assets)


def test_unknown_suite_cell_is_rejected_before_any_paid_call() -> None:
    """Catches typos that could silently route a run through a default condition."""
    suites = _suites_module()
    with pytest.raises(ValueError, match="Unknown condition"):
        suites.build_input_plan(
            REPO_ROOT,
            "route_image_understanding",
            "B_raw_route_image",
            "kalmanol",
        )
