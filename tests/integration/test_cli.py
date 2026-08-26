from __future__ import annotations

import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
OUTSIDE_ERROR = (
    "tsdaf docs check: repository checkout not found "
    "(requires pyproject.toml and scripts/validate_docs.py)\n"
)


def console_command() -> list[str]:
    script = Path(sysconfig.get_path("scripts")) / (
        "tsdaf.exe" if sys.platform == "win32" else "tsdaf"
    )
    assert script.is_file(), f"console entrypoint is missing: {script}"
    return [str(script)]


def entrypoints() -> tuple[list[str], ...]:
    return (console_command(), [sys.executable, "-m", "tsdaf"])


def run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("entrypoint_index", (0, 1))
def test_help_lists_exact_command_groups(entrypoint_index: int) -> None:
    result = run([*entrypoints()[entrypoint_index], "--help"], REPOSITORY_ROOT)

    assert result.returncode == 0
    assert "{package,route,run,docs}" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("entrypoint_index", (0, 1))
@pytest.mark.parametrize(
    "arguments",
    ((), ("unknown",), ("package",), ("route",), ("run",), ("docs", "check", "--live")),
)
def test_invalid_command_surfaces_exit_two(
    entrypoint_index: int, arguments: tuple[str, ...]
) -> None:
    result = run([*entrypoints()[entrypoint_index], *arguments], REPOSITORY_ROOT)

    assert result.returncode == 2


@pytest.mark.parametrize("entrypoint_index", (0, 1))
def test_docs_check_succeeds_against_real_checkout(entrypoint_index: int) -> None:
    result = run([*entrypoints()[entrypoint_index], "docs", "check"], REPOSITORY_ROOT)

    assert result.returncode == 0
    assert result.stdout == "DOCS_OK\n"
    assert result.stderr == ""


@pytest.mark.parametrize("entrypoint_index", (0, 1))
def test_docs_check_discovers_real_checkout_from_nested_directory(entrypoint_index: int) -> None:
    nested = REPOSITORY_ROOT / "tests" / "unit"

    result = run([*entrypoints()[entrypoint_index], "docs", "check"], nested)

    assert result.returncode == 0
    assert result.stdout == "DOCS_OK\n"
    assert result.stderr == ""


@pytest.mark.parametrize("entrypoint_index", (0, 1))
def test_docs_check_propagates_validator_failure_from_temporary_checkout(
    tmp_path: Path, entrypoint_index: int
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for filename in ("validate_docs.py", "docs_support.py"):
        shutil.copyfile(REPOSITORY_ROOT / "scripts" / filename, scripts / filename)
    nested = tmp_path / "nested" / "directory"
    nested.mkdir(parents=True)

    result = run([*entrypoints()[entrypoint_index], "docs", "check"], nested)

    assert result.returncode == 1
    assert "DOC001_REQUIRED_PATH" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("entrypoint_index", (0, 1))
def test_docs_check_outside_checkout_has_exact_error(tmp_path: Path, entrypoint_index: int) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    result = run([*entrypoints()[entrypoint_index], "docs", "check"], outside)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == OUTSIDE_ERROR
