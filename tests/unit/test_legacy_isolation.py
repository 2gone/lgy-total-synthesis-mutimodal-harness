from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_LEGACY_ENTRYPOINTS = (
    Path("harness/__main__.py"),
    Path("harness/cli.py"),
)


def load_boundary_checker() -> ModuleType:
    checker_path = REPOSITORY_ROOT / "scripts" / "check_import_boundaries.py"
    if not checker_path.is_file():
        pytest.fail(f"boundary checker is missing: {checker_path}")
    spec = importlib.util.spec_from_file_location("legacy_boundary_checker", checker_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.parametrize(
    "forbidden_statement",
    (
        "import tests.legacy_harness.harness.cli",
        "from tests.legacy_harness.harness import cli",
    ),
)
def test_import_boundary_checker_rejects_legacy_import_syntaxes(
    tmp_path: Path, forbidden_statement: str
) -> None:
    source_root = tmp_path / "src" / "tsdaf"
    source_root.mkdir(parents=True)
    (source_root / "__init__.py").write_text("\n", encoding="utf-8")
    (source_root / "synthetic.py").write_text(f"{forbidden_statement}\n", encoding="utf-8")

    checker = load_boundary_checker()
    report = checker.analyze_source_tree(source_root)

    assert [violation.rule_id for violation in report.violations] == ["IMP001_LEGACY_IMPORT"]


def test_production_source_has_no_legacy_imports() -> None:
    checker = load_boundary_checker()
    report = checker.analyze_source_tree(REPOSITORY_ROOT / "src" / "tsdaf")

    assert all(violation.rule_id != "IMP001_LEGACY_IMPORT" for violation in report.violations)
