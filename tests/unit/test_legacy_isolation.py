from __future__ import annotations

import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_LEGACY_ENTRYPOINTS = (Path("harness/__main__.py"), Path("harness/cli.py"))


def test_legacy_entrypoints_are_isolated_below_legacy_harness() -> None:
    legacy_root = REPOSITORY_ROOT / "tests" / "legacy_harness"

    assert not (REPOSITORY_ROOT / "harness").exists()
    assert all((legacy_root / entrypoint).is_file() for entrypoint in REQUIRED_LEGACY_ENTRYPOINTS)


def test_default_pytest_collection_roots_exclude_legacy_harness() -> None:
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as config_file:
        config = tomllib.load(config_file)

    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == [
        "tests/unit",
        "tests/integration",
    ]
