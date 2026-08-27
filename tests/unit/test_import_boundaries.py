from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPOSITORY_ROOT / "scripts" / "check_import_boundaries.py"


@dataclass(frozen=True)
class CheckerResult:
    returncode: int
    stdout: str
    stderr: str


def run_checker(source_root: Path, *extra_args: str) -> CheckerResult:
    completed = subprocess.run(
        [sys.executable, str(CHECKER_PATH), str(source_root), *extra_args],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return CheckerResult(completed.returncode, completed.stdout, completed.stderr)


def write_tree(root: Path, files: Mapping[str, str | bytes]) -> Path:
    package = root / "src" / "tsdaf"
    for relative, content in files.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return package


def rule_ids(result: CheckerResult) -> list[str]:
    return [line.split(": ", 1)[1].split(" ", 1)[0] for line in result.stdout.splitlines()]


def test_real_tsdaf_package_is_clean() -> None:
    result = run_checker(REPOSITORY_ROOT / "src" / "tsdaf")

    assert result == CheckerResult(0, "", "")


@pytest.mark.parametrize("statement", ["import harness", "import tests.legacy_harness"])
def test_legacy_import_prefixes_are_rejected(tmp_path: Path, statement: str) -> None:
    source_root = write_tree(
        tmp_path,
        {"__init__.py": "", "workflow/example.py": f"{statement}\n"},
    )

    result = run_checker(source_root)

    assert result.returncode == 1
    assert rule_ids(result) == ["IMP001_LEGACY_IMPORT"]


def test_legacy_violation_uses_repository_relative_path_and_message(tmp_path: Path) -> None:
    source_root = write_tree(
        tmp_path,
        {"__init__.py": "", "workflow/example.py": "import harness\n"},
    )

    result = run_checker(source_root)

    assert result == CheckerResult(
        1,
        "src/tsdaf/workflow/example.py:1: IMP001_LEGACY_IMPORT legacy imports are forbidden\n",
        "",
    )


@pytest.mark.parametrize(
    ("source", "target", "allowed"),
    [
        ("contracts", "contracts.value", True),
        ("contracts", "lanes.value", False),
        ("lanes", "contracts.value", True),
        ("lanes", "workflow.value", False),
        ("workflow", "lanes.value", True),
        ("workflow", "adapters.value", False),
        ("adapters", "contracts.value", True),
        ("adapters", "workflow.engine", False),
        ("apps", "adapters.value", True),
        ("apps", "contracts.value", True),
        ("adapters", "workflow.ports", True),
        ("__main__", "apps.cli", True),
    ],
)
def test_layer_rules_and_frozen_exceptions(
    tmp_path: Path, source: str, target: str, allowed: bool
) -> None:
    source_file = "__main__.py" if source == "__main__" else f"{source}/source.py"
    source_root = write_tree(
        tmp_path,
        {
            "__init__.py": "",
            source_file: f"import tsdaf.{target}\n",
            "contracts/value.py": "",
            "lanes/value.py": "",
            "workflow/value.py": "",
            "workflow/engine.py": "",
            "workflow/ports.py": "",
            "adapters/value.py": "",
            "apps/cli.py": "",
        },
    )

    result = run_checker(source_root)

    assert ("IMP002_LAYER_IMPORT" in rule_ids(result)) is not allowed


@pytest.mark.parametrize(
    ("statement", "expected_rule"),
    [
        ("import openai", "IMP003_CONTRACTS_DEPENDENCY"),
        ("import sqlite3", "IMP003_CONTRACTS_DEPENDENCY"),
        ("import typer", "IMP003_CONTRACTS_DEPENDENCY"),
        ("from pydantic_settings import BaseSettings", "IMP003_CONTRACTS_DEPENDENCY"),
        ("import tsdaf.apps", "IMP003_CONTRACTS_DEPENDENCY"),
        ("import tsdaf.adapters", "IMP003_CONTRACTS_DEPENDENCY"),
        ("import pydantic", None),
        ("import tsdaf.contracts", None),
    ],
)
def test_contracts_dependencies_are_limited(
    tmp_path: Path, statement: str, expected_rule: str | None
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "__init__.py": "",
            "contracts/model.py": f"{statement}\n",
            "apps/__init__.py": "",
            "adapters/__init__.py": "",
        },
    )

    result = run_checker(source_root)

    assert rule_ids(result) == ([] if expected_rule is None else [expected_rule])


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.getenv('TOKEN')\n",
        "import os as system\nsystem.getenv('TOKEN')\n",
        "import os\nos.environ['TOKEN']\n",
        "import os as system\nsystem.environ['TOKEN']\n",
        "from os import getenv\ngetenv('TOKEN')\n",
        "from os import getenv as read_env\nread_env('TOKEN')\n",
        "from os import environ\nenviron['TOKEN']\n",
        "from os import environ as process_env\nprocess_env['TOKEN']\n",
        "from pydantic_settings import BaseSettings\nBaseSettings()\n",
        "from pydantic_settings import BaseSettings as Settings\nSettings()\n",
    ],
)
def test_environment_access_outside_apps_is_rejected(tmp_path: Path, source: str) -> None:
    source_root = write_tree(tmp_path, {"__init__.py": "", "workflow/example.py": source})

    result = run_checker(source_root)

    assert rule_ids(result) == ["IMP004_ENVIRONMENT_ACCESS"]


def test_environment_access_in_apps_is_clean(tmp_path: Path) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "__init__.py": "",
            "apps/config.py": (
                "import os as system\n"
                "from os import environ as process_env\n"
                "from os import getenv as read_env\n"
                "from pydantic_settings import BaseSettings as Settings\n"
                "TOKEN = system.getenv('TOKEN')\n"
                "ENV = process_env['TOKEN']\n"
                "OTHER = read_env('OTHER')\n"
                "settings = Settings()\n"
            ),
        },
    )

    result = run_checker(source_root)

    assert result == CheckerResult(0, "", "")


def test_non_environment_os_member_is_clean(tmp_path: Path) -> None:
    source_root = write_tree(
        tmp_path,
        {"__init__.py": "", "workflow/example.py": "import os\nos.replace('a', 'b')\n"},
    )

    result = run_checker(source_root)

    assert result == CheckerResult(0, "", "")


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {
                "__init__.py": "",
                "workflow/a.py": "import tsdaf.workflow.b\n",
                "workflow/b.py": "import tsdaf.workflow.a\n",
            },
            "import cycle: tsdaf.workflow.a -> tsdaf.workflow.b -> tsdaf.workflow.a",
        ),
        (
            {"__init__.py": "", "workflow/self.py": "import tsdaf.workflow.self\n"},
            "import cycle: tsdaf.workflow.self -> tsdaf.workflow.self",
        ),
    ],
)
def test_import_cycles_are_reported_deterministically(
    tmp_path: Path, files: Mapping[str, str], message: str
) -> None:
    source_root = write_tree(tmp_path, files)

    result = run_checker(source_root)

    assert result.returncode == 1
    assert result.stdout.rstrip().endswith(message)
    assert rule_ids(result) == ["IMP005_IMPORT_CYCLE"]


def test_acyclic_import_graph_is_clean(tmp_path: Path) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "__init__.py": "",
            "workflow/a.py": "import tsdaf.workflow.b\n",
            "workflow/b.py": "",
        },
    )

    result = run_checker(source_root)

    assert result == CheckerResult(0, "", "")


def test_comments_and_strings_are_not_imports_or_environment_access(tmp_path: Path) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "__init__.py": "",
            "workflow/example.py": (
                "# import harness and os.getenv('TOKEN')\n"
                "message = 'from pydantic_settings import BaseSettings'\n"
            ),
        },
    )

    result = run_checker(source_root)

    assert result == CheckerResult(0, "", "")


@pytest.mark.parametrize(
    "source_root",
    [
        pytest.param(None, id="wrong-argument-count"),
        pytest.param("missing", id="missing-path"),
        pytest.param("not-package", id="non-package-root"),
        pytest.param("invalid-utf8", id="invalid-utf8"),
        pytest.param("syntax-error", id="syntax-error"),
    ],
)
def test_invalid_inputs_report_boundary_errors(tmp_path: Path, source_root: str | None) -> None:
    if source_root is None:
        package = write_tree(tmp_path, {"__init__.py": ""})
        result = run_checker(package, "extra")
    elif source_root == "missing":
        result = run_checker(tmp_path / "missing")
    elif source_root == "not-package":
        package = tmp_path / "not-package"
        package.mkdir()
        result = run_checker(package)
    elif source_root == "invalid-utf8":
        package = write_tree(tmp_path, {"__init__.py": "", "broken.py": b"\xff"})
        result = run_checker(package)
    else:
        package = write_tree(tmp_path, {"__init__.py": "", "broken.py": "def nope(:\n"})
        result = run_checker(package)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("BOUNDARY_ERROR:")
