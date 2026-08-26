from __future__ import annotations

import importlib
import importlib.resources
import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_PACKAGES = ("tsdaf", "contracts", "workflow", "lanes", "adapters", "apps")
FORBIDDEN_IMPORT_PREFIXES = (
    "google.genai",
    "google.generativeai",
    "prefect",
    "sqlite3",
    "aiosqlite",
    "harness",
    "tests.legacy_harness",
    "tsdaf.apps.cli",
    "tsdaf.apps.commands.docs",
    "tsdaf.document",
    "tsdaf.routing",
    "tsdaf.image",
    "tsdaf.text",
    "tsdaf.fusion",
    "tsdaf.storage",
    "tsdaf.gemini",
)


def test_boundary_packages_are_importable_and_have_phase_owned_exports() -> None:
    for package in BOUNDARY_PACKAGES:
        module_name = "tsdaf" if package == "tsdaf" else f"tsdaf.{package}"
        module = importlib.import_module(module_name)
        if package == "contracts":
            assert module.__all__
            assert len(module.__all__) == len(set(module.__all__))
            assert all(hasattr(module, name) for name in module.__all__)
        else:
            assert module.__all__ == ()


def test_importing_root_package_has_no_eager_or_forbidden_imports() -> None:
    script = (
        "import json, sys; import tsdaf; "
        f"prefixes = {FORBIDDEN_IMPORT_PREFIXES!r}; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if any(name == prefix or name.startswith(prefix + '.') for prefix in prefixes))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_py_typed_is_visible_through_importlib_resources() -> None:
    marker = importlib.resources.files("tsdaf").joinpath("py.typed")

    assert marker.is_file()
    assert marker.read_bytes() == b""
