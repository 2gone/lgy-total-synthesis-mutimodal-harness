from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _cli_module():
    try:
        return importlib.import_module("harness.cli")
    except ModuleNotFoundError:
        pytest.fail("Harness CLI is not implemented yet")


def test_dry_run_prints_public_inventory_without_requiring_env_file(capsys) -> None:
    """Catches dry-run behavior that reads secrets or hides which assets would be sent."""
    cli = _cli_module()

    exit_code = cli.main(
        [
            "--repo-root",
            str(REPO_ROOT),
            "dry-run",
            "--suite",
            "route_image_understanding",
            "--condition",
            "B_raw_route_images",
            "--paper",
            "principinol_B",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["model"] == "gemini-3.7-flash-high"
    assert output["will_call_api"] is False
    assert output["ready"] is True
    assert [asset["path"] for asset in output["assets"]] == [
        "experiments/route_image_understanding/route_images/raw/principinol_B/principinol_scheme2.png"
    ]
