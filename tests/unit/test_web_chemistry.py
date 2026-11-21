from __future__ import annotations

import asyncio
import json
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from tsdaf.apps.web import app as web_app
from tsdaf.apps.web.chemistry import (
    RecordedChemistryStore,
    StructureBinding,
    _reaction_crop_box,
    _reaction_crop_quality,
    _reaction_visual_candidates,
    chemistry_summary,
)


def _recorded_result(paper_id: str = "principinol_B") -> dict[str, object]:
    return {
        "result_kind": "MACHINE_RESULT",
        "machine_result_id": "MACHINE_TEST_R1",
        "paper_id": paper_id,
        "schema_version": "tsdaf.machine-paper-result.v1.7",
        "coverage": "MAIN_ONLY",
        "completeness": "COMPLETE",
        "execution_id": "EXEC_RECORDED_TEST",
        "unresolved_items": [],
        "domain_payload": {
            "compounds": [
                {
                    "compound_id": "COMPOUND-1",
                    "author_labels": "1",
                    "names": ["target"],
                    "structure": {"state": "UNSTATED", "value": None},
                }
            ],
            "reaction_blocks": [
                {
                    "reaction_block_id": "REACTION-1",
                    "source_order": 1,
                    "summary": "Starting material gives target.",
                    "input_compound_ids": {"value": ["COMPOUND-0"]},
                    "output_compound_ids": {"value": ["COMPOUND-1"]},
                    "operations": [{}],
                    "reported_outcomes": [{}],
                }
            ],
            "routes": [
                {
                    "route_id": "ROUTE-1",
                    "model_overview": "A one-step recorded route.",
                    "reaction_block_ids": ["REACTION-1"],
                    "compound_ids": ["COMPOUND-0", "COMPOUND-1"],
                    "target_assertions": [{}],
                }
            ],
            "characterization_records": [{}, {}],
            "experimental_accounts": [],
            "biological_findings": [],
            "field_conflicts": [],
            "paper_narratives": [{}],
        },
    }


def _write_result(root: Path, payload: dict[str, object]) -> None:
    paper_id = str(payload["paper_id"])
    directory = root / paper_id
    directory.mkdir(parents=True)
    (directory / "machine-paper-result.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_structure_binding(root: Path, paper_id: str, crop_sha256: str) -> bytes:
    directory = root / paper_id
    crop = b"\x89PNG\r\n\x1a\nrecorded-structure-crop"
    figure_sha256 = "f" * 64
    binding = {
        "schema_version": "tsdaf.structure-binding.v0",
        "authority": "MODEL_READING_NOT_CONFIRMED",
        "paper_id": paper_id,
        "notice": "Independent structure reading for display.",
        "compounds": [
            {
                "compound_id": "COMPOUND-0",
                "structures": [
                    {
                        "binding": "BOUND",
                        "readable": True,
                        "figure_asset_sha256": figure_sha256,
                        "crop_sha256": "b" * 64,
                        "bbox_px": [5, 10, 35, 45],
                        "printed_label": "0",
                    }
                ],
            },
            {
                "compound_id": "COMPOUND-1",
                "structures": [
                    {
                        "binding": "BOUND",
                        "readable": True,
                        "figure_asset_sha256": figure_sha256,
                        "crop_sha256": crop_sha256,
                        "bbox_px": [80, 10, 110, 45],
                        "printed_label": "1",
                        "smiles": "CCO",
                        "formula": "C2H6O",
                        "reader_confidence": 0.97,
                    }
                ],
            }
        ],
        "summary": {"compounds_with_a_structure": 1},
    }
    (directory / "structure-binding.json").write_text(json.dumps(binding), encoding="utf-8")
    crop_directory = directory / "crops"
    crop_directory.mkdir()
    (crop_directory / f"{crop_sha256}.png").write_bytes(crop)
    figure_directory = directory / "figures"
    figure_directory.mkdir()
    Image.new("RGB", (240, 120), "white").save(
        figure_directory / f"{figure_sha256}.png"
    )
    return crop


def test_recorded_chemistry_store_validates_and_summarizes(tmp_path: Path) -> None:
    payload = _recorded_result()
    _write_result(tmp_path, payload)
    result = RecordedChemistryStore(tmp_path, result_dir=tmp_path).get("principinol_B")

    assert result is not None
    summary = chemistry_summary(result)
    assert summary["schema_version"] == "tsdaf.machine-paper-result.v1.7"
    assert summary["real_model_called_now"] is False
    assert summary["counts"] == {
        "compounds": 1,
        "reaction_blocks": 1,
        "routes": 1,
        "characterization_records": 2,
        "experimental_accounts": 0,
        "biological_findings": 0,
        "field_conflicts": 0,
        "paper_narratives": 1,
        "unresolved_items": 0,
    }
    assert summary["compounds"][0]["labels"] == ["1"]


def test_recorded_chemistry_store_reads_bundled_delivery_results(tmp_path: Path) -> None:
    payload = _recorded_result()
    _write_result(tmp_path / "results" / "five-papers-20261010", payload)

    result = RecordedChemistryStore(tmp_path).get("principinol_B")

    assert result is not None
    assert result.source == "configured-recorded-directory"


def test_recorded_chemistry_store_matches_reparsed_package_by_source_pdf(
    tmp_path: Path,
) -> None:
    payload = _recorded_result("alchivemycin_A")
    _write_result(tmp_path, payload)
    store = RecordedChemistryStore(tmp_path, result_dir=tmp_path)

    matched = store.get_for_package(
        "alchivemycin_a_5ad8252e2ef5",
        "5ad8252e2ef59f608c7e4c47720b9ef4899e672b73d374d2e73fcb11d46a236f",
    )

    assert matched is not None
    assert matched.paper_id == "alchivemycin_A"
    assert store.get_for_package("alchivemycin_A_copy", "0" * 64) is None


def test_structure_binding_adds_auditable_visuals_without_changing_machine_facts(
    tmp_path: Path,
) -> None:
    payload = _recorded_result()
    _write_result(tmp_path, payload)
    crop_sha256 = "a" * 64
    expected_crop = _write_structure_binding(tmp_path, "principinol_B", crop_sha256)
    store = RecordedChemistryStore(tmp_path, result_dir=tmp_path)
    result = store.get("principinol_B")
    binding = store.get_structure_binding("principinol_B")

    assert result is not None
    assert binding is not None
    summary = chemistry_summary(result, binding)
    compound = summary["compounds"][0]
    reaction_output = summary["reaction_blocks"][0]["participants"]["outputs"][0]
    assert compound["has_structure"] is False
    assert compound["visual_structure"]["smiles"] == "CCO"
    assert reaction_output["visual_structure"]["image_url"].endswith(f"/{crop_sha256}.png")
    assert summary["structure_binding"]["authority"] == "MODEL_READING_NOT_CONFIRMED"
    assert summary["reaction_blocks"][0]["reaction_visual"]["source"] == (
        "DERIVED_LOCAL_ORIGINAL_FIGURE_CROP"
    )
    assert store.get_structure_crop("principinol_B", crop_sha256) == expected_crop
    assert store.get_structure_crop("principinol_B", "b" * 64) is None
    figure_context = store.get_figure_context("principinol_B", "f" * 64)
    assert figure_context is not None
    with Image.open(BytesIO(figure_context)) as image:
        assert image.format == "PNG"
        assert image.size == (240, 120)
    assert store.get_figure_context("principinol_B", "e" * 64) is None
    reaction_crop = store.get_reaction_crop("principinol_B", "REACTION-1")
    assert reaction_crop is not None
    with Image.open(BytesIO(reaction_crop)) as image:
        assert image.format == "PNG"
        assert image.width > 110


def test_reaction_crop_box_keeps_condition_text_around_horizontal_reaction() -> None:
    crop_box = _reaction_crop_box([100, 100, 300, 180], (1000, 800))

    assert crop_box[0] <= 75
    assert crop_box[1] <= 44
    assert crop_box[2] >= 325
    assert crop_box[3] >= 236


def test_reaction_crop_box_keeps_lateral_conditions_for_vertical_reaction() -> None:
    crop_box = _reaction_crop_box([400, 80, 520, 420], (1000, 800))

    assert crop_box[0] <= 250
    assert crop_box[2] >= 670
    assert crop_box[1] <= 48
    assert crop_box[3] >= 452


def test_reaction_crop_quality_rejects_multi_step_scheme_span() -> None:
    quality = _reaction_crop_quality([20, 20, 900, 500], (1000, 800))

    assert quality == {
        "status": "CONTEXT_ONLY",
        "reason_code": "MULTI_STEP_SCHEME_LAYOUT",
    }


def test_reaction_visual_selects_local_occurrences_instead_of_whole_figure() -> None:
    figure_sha256 = "f" * 64
    binding = StructureBinding(
        paper_id="alchivemycin_A",
        raw=b"{}",
        source="test",
        payload={
            "compounds": [
                {
                    "compound_id": "INPUT",
                    "structures": [
                        {
                            "binding": "BOUND",
                            "figure_asset_sha256": figure_sha256,
                            "bbox_px": [10, 10, 60, 50],
                        },
                        {
                            "binding": "BOUND",
                            "figure_asset_sha256": figure_sha256,
                            "bbox_px": [10, 210, 60, 250],
                        },
                    ],
                },
                {
                    "compound_id": "OUTPUT",
                    "structures": [
                        {
                            "binding": "BOUND",
                            "figure_asset_sha256": figure_sha256,
                            "bbox_px": [160, 10, 220, 50],
                        },
                        {
                            "binding": "BOUND",
                            "figure_asset_sha256": figure_sha256,
                            "bbox_px": [160, 210, 220, 250],
                        },
                    ],
                },
            ]
        },
    )
    block = {
        "input_compound_ids": {"value": ["INPUT"]},
        "output_compound_ids": {"value": ["OUTPUT"]},
    }

    candidates = _reaction_visual_candidates(block, binding)

    assert [candidate["bbox_px"] for candidate in candidates] == [
        [10.0, 10.0, 220.0, 50.0],
        [10.0, 210.0, 220.0, 250.0],
    ]


def test_reaction_visual_uses_the_input_occurrence_aligned_with_the_output() -> None:
    figure_sha256 = "e" * 64
    binding = StructureBinding(
        paper_id="alchivemycin_A",
        raw=b"{}",
        source="test",
        payload={
            "compounds": [
                {
                    "compound_id": "INPUT",
                    "structures": [
                        {
                            "binding": "BOUND",
                            "figure_asset_sha256": figure_sha256,
                            "bbox_px": [10, 10, 60, 50],
                        },
                        {
                            "binding": "BOUND",
                            "figure_asset_sha256": figure_sha256,
                            "bbox_px": [10, 210, 60, 250],
                        },
                    ],
                },
                {
                    "compound_id": "OUTPUT",
                    "structures": [
                        {
                            "binding": "BOUND",
                            "figure_asset_sha256": figure_sha256,
                            "bbox_px": [160, 210, 220, 250],
                        }
                    ],
                },
            ]
        },
    )
    block = {
        "input_compound_ids": {"value": ["INPUT"]},
        "output_compound_ids": {"value": ["OUTPUT"]},
    }

    assert _reaction_visual_candidates(block, binding) == [
        {
            "figure_asset_sha256": figure_sha256,
            "participant_count": 2,
            "bbox_px": [10.0, 210.0, 220.0, 250.0],
        }
    ]


def test_reaction_visual_reports_missing_input_binding_without_guessing(tmp_path: Path) -> None:
    payload = _recorded_result()
    _write_result(tmp_path, payload)
    crop_sha256 = "d" * 64
    _write_structure_binding(tmp_path, "principinol_B", crop_sha256)
    binding_path = tmp_path / "principinol_B" / "structure-binding.json"
    binding_payload = json.loads(binding_path.read_text(encoding="utf-8"))
    binding_payload["compounds"][0]["structures"] = []
    binding_path.write_text(json.dumps(binding_payload), encoding="utf-8")
    store = RecordedChemistryStore(tmp_path, result_dir=tmp_path)
    result = store.get("principinol_B")
    binding = store.get_structure_binding("principinol_B")

    assert result is not None
    assert binding is not None
    visual = chemistry_summary(result, binding)["reaction_blocks"][0]["reaction_visual"]
    figure_sha256 = "f" * 64
    assert visual == {
        "status": "UNAVAILABLE",
        "reason_code": "MISSING_INPUT_VISUAL_BINDING",
        "missing_input_compound_ids": ["COMPOUND-0"],
        "missing_output_compound_ids": [],
        "figure_asset_sha256": figure_sha256,
        "context_image_url": (
            f"/api/chemistry/results/principinol_B/figures/{figure_sha256}.png"
        ),
    }


def test_reaction_visual_uses_full_scheme_context_when_local_crop_is_unsafe(
    tmp_path: Path,
) -> None:
    payload = _recorded_result()
    _write_result(tmp_path, payload)
    crop_sha256 = "c" * 64
    _write_structure_binding(tmp_path, "principinol_B", crop_sha256)
    store = RecordedChemistryStore(tmp_path, result_dir=tmp_path)
    result = store.get("principinol_B")
    binding = store.get_structure_binding("principinol_B")

    assert result is not None
    assert binding is not None
    figure_sha256 = "f" * 64
    visual = chemistry_summary(
        result,
        binding,
        figure_dimensions={figure_sha256: (120, 60)},
    )["reaction_blocks"][0]["reaction_visual"]

    assert visual == {
        "status": "CONTEXT_ONLY",
        "reason_code": "MULTI_STEP_SCHEME_LAYOUT",
        "figure_asset_sha256": figure_sha256,
        "context_image_url": (
            f"/api/chemistry/results/principinol_B/figures/{figure_sha256}.png"
        ),
    }


def test_chemistry_summary_exposes_reaction_details_and_display_translation(
    tmp_path: Path,
) -> None:
    payload = _recorded_result("ineleganolide")
    block = payload["domain_payload"]["reaction_blocks"][0]
    block["summary"] = "Homologation of aldehyde 7 to epoxide 8"
    block["operations"] = [
        {
            "operation_id": "OPERATION-1",
            "sequence_index": 1,
            "description": "Aldehyde 7 undergoes oxirane homologation.",
            "atmosphere": {"state": "PRESENT", "value": "nitrogen"},
            "materials": {
                "state": "PRESENT",
                "value": [
                    {
                        "role": "REAGENT",
                        "names": ["n-BuLi"],
                        "amount": {"raw_text": "10 mL"},
                        "equivalents": {"raw_text": "1.1 equiv."},
                    }
                ],
            },
            "stages": [
                {"display_index": 1, "description": "Cool the mixture to -78 °C."},
                {"display_index": 2, "description": "Warm to room temperature overnight."},
            ],
        }
    ]
    block["reported_outcomes"] = [
        {
            "quantities": [
                {
                    "kind": {"state": "PRESENT", "value": "ISOLATED_YIELD"},
                    "reported_value": {
                        "state": "PRESENT",
                        "value": {"raw_text": "72 %"},
                    },
                }
            ]
        }
    ]
    _write_result(tmp_path, payload)
    result = RecordedChemistryStore(tmp_path, result_dir=tmp_path).get("ineleganolide")

    assert result is not None
    reaction = chemistry_summary(result)["reaction_blocks"][0]
    assert reaction["summary_zh"] == "醛 7 同系化生成环氧化物 8"
    assert reaction["route_context"]["position"] == 1
    assert reaction["route_context"]["total_reaction_blocks"] == 1
    assert reaction["experimental_steps"] == [
        "Cool the mixture to -78 °C.",
        "Warm to room temperature overnight.",
    ]
    assert reaction["conditions"][0]["atmosphere"] == "nitrogen"
    assert reaction["conditions"][0]["materials"] == [
        {
            "role": "REAGENT",
            "name": "n-BuLi",
            "amount": "10 mL",
            "equivalents": "1.1 equiv.",
        }
    ]
    assert reaction["outcomes"] == [
        {"kind": "ISOLATED_YIELD", "value": "72 %", "basis": None}
    ]
    assert reaction["primary_yield"] == "72 %"


def test_chemistry_endpoints_display_and_download_recorded_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _recorded_result()
    _write_result(tmp_path, payload)
    crop_sha256 = "c" * 64
    expected_crop = _write_structure_binding(tmp_path, "principinol_B", crop_sha256)
    monkeypatch.setenv("TSDAF_CHEMISTRY_RESULTS_DIR", str(tmp_path))
    monkeypatch.setattr(web_app, "_find_checkout", lambda: Path.cwd())
    app = web_app.create_app()
    package = Path("tests/fixtures/paper_packages/principinol_B").resolve()

    async def exercise() -> tuple[
        httpx.Response,
        httpx.Response,
        httpx.Response,
        httpx.Response,
        httpx.Response,
    ]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            run = await client.post("/api/chemistry/run", json={"package_path": str(package)})
            json_download = await client.get("/api/chemistry/results/principinol_B.json")
            zip_download = await client.get("/api/chemistry/results/principinol_B.zip")
            visual = await client.get(
                f"/api/chemistry/results/principinol_B/visuals/{crop_sha256}.png"
            )
            reaction = await client.get(
                "/api/chemistry/results/principinol_B/reactions/REACTION-1.png"
            )
            return run, json_download, zip_download, visual, reaction

    run, json_download, zip_download, visual, reaction = asyncio.run(exercise())

    assert run.status_code == 200
    assert run.json()["mode"] == "recorded-interface"
    assert run.json()["summary"]["counts"]["reaction_blocks"] == 1
    assert run.json()["summary"]["compounds"][0]["visual_structure"]["smiles"] == "CCO"
    assert json_download.json() == payload
    assert visual.content == expected_crop
    assert visual.headers["etag"] == f'"{crop_sha256}"'
    assert reaction.status_code == 200
    with Image.open(BytesIO(reaction.content)) as image:
        assert image.format == "PNG"
    with zipfile.ZipFile(BytesIO(zip_download.content)) as archive:
        assert set(archive.namelist()) == {
            "machine-paper-result.json",
            "result-summary.json",
            "structure-binding.json",
            f"structure-crops/{crop_sha256}.png",
        }
        assert json.loads(archive.read("machine-paper-result.json")) == payload
