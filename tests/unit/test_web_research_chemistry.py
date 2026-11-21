from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import httpx
import pytest

from tsdaf.apps.web import app as web_app
from tsdaf.apps.web.chemistry import RecordedChemistryStore
from tsdaf.apps.web.research_chemistry import (
    ResearchChemistryManager,
    ResearchChemistrySettings,
    prepare_single_package_collection,
    research_chemistry_summary,
)
from tsdaf.lanes.document.package_loader import load_paper_package


def test_single_package_adapter_builds_research_collection(tmp_path: Path) -> None:
    package = Path("tests/fixtures/paper_packages/hyperfirin").resolve()
    loaded = load_paper_package(package)

    collection = prepare_single_package_collection(loaded, tmp_path / "collection")

    rows = list(csv.DictReader((collection / "INDEX.csv").open(encoding="utf-8-sig")))
    article = json.loads(
        (collection / "packages/hyperfirin/article.json").read_text(encoding="utf-8")
    )
    assert rows[0]["paper_id"] == "hyperfirin"
    assert rows[0]["package_path"] == "packages/hyperfirin"
    assert int(rows[0]["main_pages"]) > 0
    assert article == {
        "main_input_blocks_sha256": loaded.manifest.normalized_blocks.sha256,
        "main_block_type_mappings": [],
        "si": [],
    }
    assert (collection / "packages/hyperfirin/main/manifest.json").is_file()


def test_research_manager_defaults_to_safe_preflight(tmp_path: Path) -> None:
    runner = tmp_path / "research/problem-profile-20261009/runner/run_pipeline.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# fixture\n", encoding="utf-8")
    settings = ResearchChemistrySettings(
        runner=runner,
        model="Gemini-3.8-flash",
        provider_configured=False,
        missing_modules=(),
    )
    loaded = load_paper_package(Path("tests/fixtures/paper_packages/hyperfirin").resolve())

    result = ResearchChemistryManager(tmp_path, settings).start(loaded, live=False)

    assert result["status"] == "LIVE_REQUIRED"
    assert result["real_model_called_now"] is False
    assert result["pipeline"] == ["import", "front", "chain", "stats"]


def test_research_summary_covers_experiment_paper_and_structure_result() -> None:
    result = {
        "label": "chain-rdemo-paper",
        "paper_record": {
            "compounds": [{"label": "1", "name": "target"}],
            "route": [{"name": "total synthesis"}],
        },
        "reaction_blocks": [
            {
                "entry": 1,
                "block_in_entry": 1,
                "record": {"summary": "Cyclization", "steps": [{"kind": "REACTION_STAGE"}]},
            }
        ],
        "characterization_records": [{}, {}],
        "author_disagreements": [{}],
        "unresolved": [],
        "structures": {"file": "structure-binding.json", "summary": {}},
    }

    summary = research_chemistry_summary(result, "new-paper")

    assert summary["counts"] == {
        "compounds": 1,
        "reaction_blocks": 1,
        "routes": 1,
        "characterization_records": 2,
        "field_conflicts": 1,
        "unresolved_items": 0,
    }
    assert summary["reaction_blocks"][0]["summary"] == "Cyclization"


def test_unknown_recorded_paper_is_handed_to_research_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = ResearchChemistrySettings(
        runner=tmp_path / "run_pipeline.py",
        model="Gemini-3.8-flash",
        provider_configured=False,
        missing_modules=(),
    )
    monkeypatch.setattr(web_app, "_find_checkout", lambda: Path.cwd())
    monkeypatch.setattr(
        web_app.ResearchChemistrySettings,
        "from_checkout",
        lambda _: settings,
    )
    monkeypatch.setattr(RecordedChemistryStore, "get_for_package", lambda *_: None)
    app = web_app.create_app()
    package = Path("tests/fixtures/paper_packages/hyperfirin").resolve()

    async def exercise() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/chemistry/run",
                json={"package_path": str(package), "live": False},
            )

    response = asyncio.run(exercise())

    assert response.status_code == 200
    assert response.json()["status"] == "LIVE_REQUIRED"
    assert response.json()["mode"] == "research-pipeline"
