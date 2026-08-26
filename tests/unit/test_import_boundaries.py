from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = REPOSITORY_ROOT / "scripts" / "check_import_boundaries.py"


def load_checker() -> ModuleType:
    if not CHECKER_PATH.is_file():
        pytest.fail(f"boundary checker is missing: {CHECKER_PATH}")
    spec = importlib.util.spec_from_file_location("task2_boundary_checker", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker() -> ModuleType:
    return load_checker()


def write_tree(root: Path, files: dict[str, str]) -> Path:
    source_root = root / "src" / "tsdaf"
    source_root.mkdir(parents=True)
    (source_root / "__init__.py").write_text("\n", encoding="utf-8")
    for relative, source in files.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return source_root


def rules(report: object) -> list[str]:
    return [violation.rule_id for violation in report.violations]


@pytest.mark.parametrize(
    "statement",
    (
        "import harness",
        "from harness import cli",
        "import tests.legacy_harness.harness.cli",
        "from tests.legacy_harness.harness import cli",
    ),
)
def test_legacy_import_forms_are_rejected(
    checker: ModuleType, tmp_path: Path, statement: str
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": f"{statement}\n"})

    report = checker.analyze_source_tree(source_root)

    assert rules(report) == ["IMP001_LEGACY_IMPORT"]


@pytest.mark.parametrize(
    "statement",
    (
        "import google.genai",
        "from google import genai",
        "import google.generativeai",
        "from google import generativeai",
        "import prefect",
        "from prefect import flow",
        "import sqlite3",
        "from sqlite3 import connect",
        "import aiosqlite",
        "from aiosqlite import connect",
        "import argparse",
        "from argparse import ArgumentParser",
        "import click",
        "from click import command",
        "import typer",
        "from typer import Typer",
        "import pydantic_settings",
        "from pydantic_settings import BaseSettings",
        "import tsdaf.apps",
        "from tsdaf import apps",
        "import tsdaf.adapters",
        "from tsdaf import adapters",
    ),
)
def test_contracts_specific_dependencies_are_reported_once(
    checker: ModuleType, tmp_path: Path, statement: str
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "contracts/__init__.py": "\n",
            "contracts/model.py": f"{statement}\n",
            "apps/__init__.py": "\n",
            "adapters/__init__.py": "\n",
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert rules(report) == ["IMP003_CONTRACTS_DEPENDENCY"]


@pytest.mark.parametrize(
    ("source", "statement"),
    (
        ("contracts/model.py", "import tsdaf.lanes"),
        ("contracts/model.py", "import tsdaf.workflow"),
        ("lanes/example.py", "import tsdaf.workflow"),
        ("lanes/example.py", "import tsdaf.adapters"),
        ("lanes/example.py", "import tsdaf.apps"),
        ("workflow/engine.py", "import tsdaf.adapters"),
        ("workflow/engine.py", "import tsdaf.apps"),
        ("adapters/example.py", "import tsdaf.lanes"),
        ("adapters/example.py", "import tsdaf.workflow.engine"),
        ("adapters/example.py", "import tsdaf.apps"),
    ),
)
def test_illegal_layer_directions_are_rejected(
    checker: ModuleType, tmp_path: Path, source: str, statement: str
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "contracts/__init__.py": "\n",
            "lanes/__init__.py": "\n",
            "workflow/__init__.py": "\n",
            "workflow/engine.py": "\n",
            "adapters/__init__.py": "\n",
            "apps/__init__.py": "\n",
            source: f"{statement}\n",
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert "IMP002_LAYER_IMPORT" in rules(report)


def test_legal_layer_matrix_and_relative_contracts_imports_are_clean(
    checker: ModuleType, tmp_path: Path
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "contracts/__init__.py": "from . import value\n",
            "contracts/base.py": "import dataclasses\nimport pydantic\n",
            "contracts/value.py": "\n",
            "lanes/__init__.py": "\n",
            "lanes/route.py": "import tsdaf.contracts\nimport tsdaf.lanes\n",
            "workflow/__init__.py": "\n",
            "workflow/ports.py": "\n",
            "workflow/engine.py": (
                "import tsdaf.contracts\nimport tsdaf.lanes\nimport tsdaf.workflow.ports\n"
            ),
            "adapters/__init__.py": "\n",
            "adapters/provider.py": (
                "import tsdaf.contracts\nfrom tsdaf.workflow import ports\nimport tsdaf.adapters\n"
            ),
            "apps/__init__.py": "\n",
            "apps/cli.py": (
                "import tsdaf.contracts\n"
                "import tsdaf.lanes\n"
                "import tsdaf.workflow\n"
                "import tsdaf.adapters\n"
                "import tsdaf.apps\n"
            ),
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert report.violations == ()


@pytest.mark.parametrize(
    "statement",
    (
        "import os\nos.getenv('TOKEN')",
        "from os import getenv\ngetenv('TOKEN')",
        "import os as operating\noperating.environ",
        "import os\nos.environ['TOKEN']",
        "import os\nos.environ.get('TOKEN')",
        "from os import environ as process_env\nprocess_env.get('TOKEN')",
        "from pydantic_settings import BaseSettings\nBaseSettings()",
        "import pydantic_settings as settings\nclass Config(settings.BaseSettings):\n    pass",
    ),
)
def test_environment_access_outside_apps_is_rejected(
    checker: ModuleType, tmp_path: Path, statement: str
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": f"{statement}\n"})

    report = checker.analyze_source_tree(source_root)

    assert rules(report) == ["IMP004_ENVIRONMENT_ACCESS"]


def test_environment_access_in_apps_is_legal(checker: ModuleType, tmp_path: Path) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "apps/__init__.py": "\n",
            "apps/config.py": (
                "import os\n"
                "from pydantic_settings import BaseSettings\n"
                "TOKEN = os.getenv('TOKEN')\n"
                "ENV = os.environ\n"
                "class Config(BaseSettings):\n"
                "    pass\n"
            ),
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert report.violations == ()


def test_environment_lexical_scope_keeps_module_alias_when_sibling_reuses_name(
    checker: ModuleType, tmp_path: Path
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "workflow/example.py": (
                "import os as runtime\n"
                "def read_token():\n"
                "    return runtime.getenv('TOKEN')\n"
                "def encode(value):\n"
                "    import json as runtime\n"
                "    return runtime.dumps(value)\n"
            )
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (3, "IMP004_ENVIRONMENT_ACCESS")
    ]


def test_environment_lexical_scope_parameter_shadows_module_alias(
    checker: ModuleType, tmp_path: Path
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "workflow/example.py": (
                "from os import getenv\ndef normalize(getenv):\n    return getenv('TOKEN')\n"
            )
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert report.violations == ()


def test_environment_lexical_scope_recognizes_local_import(
    checker: ModuleType, tmp_path: Path
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "workflow/example.py": (
                "def read_token():\n    import os as runtime\n    return runtime.getenv('TOKEN')\n"
            )
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (3, "IMP004_ENVIRONMENT_ACCESS")
    ]


def test_environment_lexical_scope_rebind_is_ordered(checker: ModuleType, tmp_path: Path) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "workflow/example.py": (
                "import os as runtime\n"
                "TOKEN = runtime.getenv('TOKEN')\n"
                "import json as runtime\n"
                "PAYLOAD = runtime.dumps({'token': TOKEN})\n"
            )
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (2, "IMP004_ENVIRONMENT_ACCESS")
    ]


def test_environment_lexical_scope_resolves_nested_closure_alias(
    checker: ModuleType, tmp_path: Path
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "workflow/example.py": (
                "import os as runtime\n"
                "def outer():\n"
                "    def read_token():\n"
                "        return runtime.getenv('TOKEN')\n"
                "    return read_token\n"
            )
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (4, "IMP004_ENVIRONMENT_ACCESS")
    ]


@pytest.mark.parametrize(
    ("source", "expected_line"),
    (
        (
            "import os as runtime\n"
            "def read_token():\n"
            "    global runtime\n"
            "    return runtime.getenv('TOKEN')\n",
            4,
        ),
        (
            "def outer():\n"
            "    import os as runtime\n"
            "    def read_token():\n"
            "        nonlocal runtime\n"
            "        return runtime.getenv('TOKEN')\n"
            "    return read_token\n",
            5,
        ),
        (
            "from os import getenv\nread_token = lambda: getenv('TOKEN')\n",
            2,
        ),
        (
            "import os\nVALUES = [value for value in os.environ]\n",
            2,
        ),
    ),
)
def test_environment_lexical_scope_resolves_declared_and_nested_lookups(
    checker: ModuleType, tmp_path: Path, source: str, expected_line: int
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (expected_line, "IMP004_ENVIRONMENT_ACCESS")
    ]


@pytest.mark.parametrize(
    "source",
    (
        "from os import getenv\nread_token = lambda getenv: getenv('TOKEN')\n",
        "import os\nVALUES = [os.getenv for os in providers]\n",
        (
            "import os as runtime\n"
            "def encode():\n"
            "    runtime = object()\n"
            "    return runtime.getenv('TOKEN')\n"
        ),
    ),
)
def test_environment_lexical_scope_real_local_bindings_are_clean(
    checker: ModuleType, tmp_path: Path, source: str
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert report.violations == ()


@pytest.mark.parametrize(
    ("source", "expected_line"),
    (
        (
            "def read_token():\n"
            "    import os as runtime\n"
            "    token = runtime.getenv('TOKEN')\n"
            "    import json as runtime\n"
            "    return runtime.dumps(token)\n",
            3,
        ),
        (
            "import os as runtime\n"
            "class Settings:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    import json as runtime\n"
            "    PAYLOAD = runtime.dumps({'token': TOKEN})\n",
            3,
        ),
    ),
)
def test_environment_lexical_scope_local_and_class_rebinds_are_ordered(
    checker: ModuleType, tmp_path: Path, source: str, expected_line: int
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (expected_line, "IMP004_ENVIRONMENT_ACCESS")
    ]


@pytest.mark.parametrize(
    ("source", "expected_line"),
    (
        (
            "def outer():\n"
            "    runtime = object()\n"
            "    def read_token():\n"
            "        return runtime.getenv('TOKEN')\n"
            "    import os as runtime\n"
            "    return read_token\n",
            4,
        ),
        (
            "def outer():\n"
            "    runtime = object()\n"
            "    read_token = lambda: runtime.getenv('TOKEN')\n"
            "    import os as runtime\n"
            "    return read_token\n",
            3,
        ),
        (
            "runtime = object()\n"
            "class Settings:\n"
            "    def read_token(self):\n"
            "        return runtime.getenv('TOKEN')\n"
            "import os as runtime\n",
            4,
        ),
        (
            "runtime = object()\n"
            "TOKENS = (runtime.getenv('TOKEN') for _ in range(1))\n"
            "import os as runtime\n",
            2,
        ),
    ),
)
def test_environment_round2_deferred_bodies_see_late_bindings(
    checker: ModuleType, tmp_path: Path, source: str, expected_line: int
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (expected_line, "IMP004_ENVIRONMENT_ACCESS")
    ]


@pytest.mark.parametrize(
    ("source", "expected_line"),
    (
        (
            "runtime = object()\n"
            "def configure():\n"
            "    global runtime\n"
            "    import os as runtime\n"
            "configure()\n"
            "def read_token():\n"
            "    return runtime.getenv('TOKEN')\n",
            7,
        ),
        (
            "def outer():\n"
            "    runtime = object()\n"
            "    def configure():\n"
            "        nonlocal runtime\n"
            "        import os as runtime\n"
            "    configure()\n"
            "    def read_token():\n"
            "        return runtime.getenv('TOKEN')\n"
            "    return read_token\n",
            8,
        ),
    ),
)
def test_environment_round2_declared_target_writes_reach_sibling_reads(
    checker: ModuleType, tmp_path: Path, source: str, expected_line: int
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (expected_line, "IMP004_ENVIRONMENT_ACCESS")
    ]


@pytest.mark.parametrize(
    ("source", "expected_line"),
    (
        (
            "import os as runtime\n"
            "for item in [1]:\n"
            "    break\n"
            "else:\n"
            "    import json as runtime\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            6,
        ),
        (
            "runtime = object()\n"
            "try:\n"
            "    import os as runtime\n"
            "    raise RuntimeError\n"
            "    import json as runtime\n"
            "except RuntimeError:\n"
            "    pass\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            8,
        ),
        (
            "import os as runtime\n"
            "match value:\n"
            "    case 0:\n"
            "        import json as runtime\n"
            "    case _:\n"
            "        pass\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            7,
        ),
        (
            "import os as runtime\n"
            "if condition:\n"
            "    import json as runtime\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            4,
        ),
    ),
)
def test_environment_round2_control_flow_joins_all_feasible_exits(
    checker: ModuleType, tmp_path: Path, source: str, expected_line: int
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert [(violation.line, violation.rule_id) for violation in report.violations] == [
        (expected_line, "IMP004_ENVIRONMENT_ACCESS")
    ]


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    (
        (
            "runtime = object()\n"
            "def read_token():\n"
            "    return runtime.getenv('TOKEN')\n"
            "def configure():\n"
            "    global runtime\n"
            "    import os as runtime\n"
            "configure()\n"
            "read_token()\n",
            (3,),
        ),
        (
            "def outer():\n"
            "    runtime = object()\n"
            "    def read_token():\n"
            "        return runtime.getenv('TOKEN')\n"
            "    def configure():\n"
            "        nonlocal runtime\n"
            "        import os as runtime\n"
            "    configure()\n"
            "    return read_token\n",
            (4,),
        ),
        (
            "import os\n"
            "runtime = object()\n"
            "def read_token():\n"
            "    return runtime.getenv('TOKEN')\n"
            "runtime = os\n",
            (4,),
        ),
        (
            "import os\n"
            "runtime = object()\n"
            "def read_token():\n"
            "    return runtime.getenv('TOKEN')\n"
            "def configure():\n"
            "    global runtime\n"
            "    runtime = os\n"
            "configure()\n",
            (4,),
        ),
        (
            "import os\n"
            "def outer():\n"
            "    runtime = object()\n"
            "    def read_token():\n"
            "        return runtime.getenv('TOKEN')\n"
            "    def configure():\n"
            "        nonlocal runtime\n"
            "        runtime = os\n"
            "    configure()\n"
            "    return read_token\n",
            (5,),
        ),
        (
            "runtime = object()\n"
            "class Settings:\n"
            "    def read_token(self):\n"
            "        return runtime.getenv('TOKEN')\n"
            "def configure():\n"
            "    global runtime\n"
            "    import os as runtime\n"
            "configure()\n",
            (4,),
        ),
        (
            "def outer():\n"
            "    runtime = object()\n"
            "    def middle():\n"
            "        runtime = object()\n"
            "        def read_token():\n"
            "            return runtime.getenv('TOKEN')\n"
            "        def configure():\n"
            "            nonlocal runtime\n"
            "            import os as runtime\n"
            "        configure()\n"
            "        return read_token\n"
            "    return middle\n",
            (6,),
        ),
        (
            "runtime = object()\n"
            "EAGER = runtime.getenv('TOKEN')\n"
            "def read_token():\n"
            "    return runtime.getenv('TOKEN')\n"
            "def configure():\n"
            "    global runtime\n"
            "    import os as runtime\n"
            "configure()\n",
            (4,),
        ),
        (
            "import os\n"
            "def read_token():\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    runtime = os\n"
            "    return TOKEN\n",
            (),
        ),
    ),
)
def test_environment_round3_summary_fixed_point_and_lexical_slots(
    checker: ModuleType, tmp_path: Path, source: str, expected_lines: tuple[int, ...]
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert tuple(violation.line for violation in report.violations) == expected_lines


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    (
        (
            "def outer():\n"
            "    runtime = object()\n"
            "    def read_token():\n"
            "        return runtime.getenv('TOKEN')\n"
            "    raise RuntimeError\n"
            "    import os as runtime\n",
            (),
        ),
        (
            "def outer():\n"
            "    runtime = object()\n"
            "    def read_token():\n"
            "        return runtime.getenv('TOKEN')\n"
            "    return read_token\n"
            "    import os as runtime\n",
            (),
        ),
        (
            "runtime = object()\n"
            "def read_token():\n"
            "    return runtime.getenv('TOKEN')\n"
            "def outer():\n"
            "    return None\n"
            "    def configure():\n"
            "        global runtime\n"
            "        import os as runtime\n"
            "outer()\n"
            "read_token()\n",
            (),
        ),
        (
            "runtime = object()\n"
            "try:\n"
            "    import os as runtime\n"
            "    raise RuntimeError\n"
            "    import json as runtime\n"
            "except RuntimeError:\n"
            "    pass\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            (8,),
        ),
    ),
)
def test_environment_round3_reachability_excludes_unreachable_suffixes(
    checker: ModuleType, tmp_path: Path, source: str, expected_lines: tuple[int, ...]
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert tuple(violation.line for violation in report.violations) == expected_lines


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    (
        (
            "import json as runtime\n"
            "for item in [1, 2]:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    import os as runtime\n",
            (3,),
        ),
        (
            "import json as runtime\n"
            "count = 0\n"
            "while count < 2:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    import os as runtime\n"
            "    count += 1\n",
            (4,),
        ),
        (
            "import json as runtime\n"
            "for item in [1, 2]:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    import os as runtime\n"
            "    continue\n",
            (3,),
        ),
        (
            "import os as runtime\n"
            "for item in [1]:\n"
            "    break\n"
            "else:\n"
            "    import json as runtime\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            (6,),
        ),
        (
            "import os as runtime\n"
            "for outer in [1]:\n"
            "    for inner in [1]:\n"
            "        break\n"
            "    TOKEN = runtime.getenv('TOKEN')\n",
            (5,),
        ),
        (
            "import os as runtime\n"
            "for outer in [1]:\n"
            "    for inner in [1]:\n"
            "        continue\n"
            "    TOKEN = runtime.getenv('TOKEN')\n",
            (5,),
        ),
    ),
)
def test_environment_round3_loop_fixed_points_and_exit_ownership(
    checker: ModuleType, tmp_path: Path, source: str, expected_lines: tuple[int, ...]
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert tuple(violation.line for violation in report.violations) == expected_lines


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    (
        (
            "import os as runtime\n"
            "try:\n"
            "    VALUE = 1\n"
            "finally:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n",
            (5,),
        ),
        (
            "import os as runtime\n"
            "def finish():\n"
            "    try:\n"
            "        return None\n"
            "    finally:\n"
            "        TOKEN = runtime.getenv('TOKEN')\n",
            (6,),
        ),
        (
            "import os as runtime\n"
            "try:\n"
            "    raise RuntimeError\n"
            "finally:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n",
            (5,),
        ),
        (
            "import os as runtime\n"
            "for item in [1]:\n"
            "    try:\n"
            "        break\n"
            "    finally:\n"
            "        TOKEN = runtime.getenv('TOKEN')\n",
            (6,),
        ),
        (
            "import os as runtime\n"
            "for item in [1]:\n"
            "    try:\n"
            "        continue\n"
            "    finally:\n"
            "        TOKEN = runtime.getenv('TOKEN')\n",
            (6,),
        ),
        (
            "import os as runtime\n"
            "def finish():\n"
            "    try:\n"
            "        return None\n"
            "    finally:\n"
            "        return None\n"
            "        TOKEN = runtime.getenv('TOKEN')\n",
            (),
        ),
        (
            "runtime = object()\n"
            "try:\n"
            "    import os as runtime\n"
            "    raise RuntimeError\n"
            "    import json as runtime\n"
            "except RuntimeError:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n",
            (7,),
        ),
        (
            "import os as runtime\n"
            "try:\n"
            "    raise ExceptionGroup('errors', [RuntimeError()])\n"
            "except* RuntimeError:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "finally:\n"
            "    FINAL = runtime.getenv('TOKEN')\n",
            (5, 7),
        ),
        (
            "import os as runtime\n"
            "try:\n"
            "    might_raise()\n"
            "    import json as runtime\n"
            "finally:\n"
            "    TOKEN = runtime.getenv('TOKEN')\n",
            (6,),
        ),
    ),
)
def test_environment_round3_try_finally_preserves_all_exit_channels(
    checker: ModuleType, tmp_path: Path, source: str, expected_lines: tuple[int, ...]
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert tuple(violation.line for violation in report.violations) == expected_lines


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    (
        (
            "import os as runtime\n"
            "def inspect(value):\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    match value:\n"
            "        case runtime:\n"
            "            return runtime\n",
            (),
        ),
        (
            "import os as runtime\n"
            "match value:\n"
            "    case _:\n"
            "        import json as runtime\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            (),
        ),
        (
            "import os as runtime\n"
            "match value:\n"
            "    case _ if condition:\n"
            "        import json as runtime\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            (5,),
        ),
        (
            "import os as runtime\n"
            "match value:\n"
            "    case 0:\n"
            "        import json as runtime\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            (5,),
        ),
        (
            "import os as runtime\n"
            "match value:\n"
            "    case 0:\n"
            "        import json as runtime\n"
            "    case 1:\n"
            "        pass\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            (7,),
        ),
        (
            "import os as runtime\n"
            "match value:\n"
            "    case 1:\n"
            "        pass\n"
            "    case 0:\n"
            "        import json as runtime\n"
            "TOKEN = runtime.getenv('TOKEN')\n",
            (7,),
        ),
        (
            "import os as runtime\nmatch value:\n    case runtime.getenv:\n        pass\n",
            (3,),
        ),
        (
            "import os as runtime\n"
            "def inspect(value):\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    match value:\n"
            "        case [*runtime]:\n"
            "            return runtime\n",
            (),
        ),
        (
            "import os as runtime\n"
            "def inspect(value):\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    match value:\n"
            "        case {**runtime}:\n"
            "            return runtime\n",
            (),
        ),
        (
            "import os as runtime\n"
            "def inspect(value):\n"
            "    TOKEN = runtime.getenv('TOKEN')\n"
            "    match value:\n"
            "        case [item] as runtime:\n"
            "            return runtime\n",
            (),
        ),
        (
            "import os as runtime\n"
            "def inspect(value):\n"
            "    match value:\n"
            "        case [runtime]:\n"
            "            pass\n"
            "        case _:\n"
            "            TOKEN = runtime.getenv('TOKEN')\n",
            (),
        ),
    ),
)
def test_environment_round3_match_alternatives_and_pattern_scope(
    checker: ModuleType, tmp_path: Path, source: str, expected_lines: tuple[int, ...]
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert tuple(violation.line for violation in report.violations) == expected_lines


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    (
        (
            "import os\n"
            "runtime = object()\n"
            "VALUES = (runtime.getenv('TOKEN') for _ in [1])\n"
            "runtime = os\n",
            (3,),
        ),
        (
            "import os\n"
            "runtime = object()\n"
            "VALUES = (item for item in [1] if runtime.getenv('TOKEN'))\n"
            "runtime = os\n",
            (3,),
        ),
        (
            "import os as runtime\nVALUES = (runtime.getenv('TOKEN') for runtime in providers)\n",
            (),
        ),
        (
            "runtime = object()\n"
            "VALUES = (item for item in runtime.getenv('TOKEN'))\n"
            "import os as runtime\n",
            (),
        ),
        (
            "runtime = object()\n"
            "VALUES = [runtime.getenv('TOKEN') for item in [1]]\n"
            "import os as runtime\n",
            (),
        ),
        (
            "runtime = object()\n"
            "VALUES = {runtime.getenv('TOKEN') for item in [1]}\n"
            "import os as runtime\n",
            (),
        ),
        (
            "runtime = object()\n"
            "VALUES = {item: runtime.getenv('TOKEN') for item in [1]}\n"
            "import os as runtime\n",
            (),
        ),
    ),
)
def test_environment_round3_eager_and_deferred_comprehension_boundaries(
    checker: ModuleType, tmp_path: Path, source: str, expected_lines: tuple[int, ...]
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert tuple(violation.line for violation in report.violations) == expected_lines


_ENVIRONMENT_ROUND4_TARGET_CASES = (
    (
        "T1",
        "import os as runtime\nruntime: object\nTOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "T2",
        "import os as runtime\n"
        "class Settings:\n"
        "    runtime: object\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (4,),
    ),
    (
        "T3",
        "import os as runtime\n"
        "def read_token():\n"
        "    TOKEN = runtime.getenv('TOKEN')\n"
        "    runtime: object\n"
        "    return TOKEN\n",
        (),
    ),
    (
        "T4",
        "import os as runtime\n"
        "class Settings:\n"
        "    runtime = object()\n"
        "    del runtime\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (5,),
    ),
    (
        "T5",
        "import os as runtime\ndel runtime\nTOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "T6",
        "import os as runtime\ndel runtime.getenv\nTOKEN = runtime.getenv('TOKEN')\n",
        (2, 3),
    ),
    (
        "T7",
        "import os as runtime\ndel holder[runtime.getenv]\nTOKEN = runtime.getenv('TOKEN')\n",
        (2, 3),
    ),
    (
        "T8",
        "import os as os_alias\n"
        "runtime = object()\n"
        "runtime: (runtime := os_alias) = object()\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (4,),
    ),
    (
        "T9",
        "import os as os_alias\n"
        "runtime = object()\n"
        "runtime: (runtime := object()) = os_alias\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
)

_ENVIRONMENT_ROUND4_EXPRESSION_CASES = (
    (
        "E1",
        "import os as runtime\n"
        "condition and (runtime := object())\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "E2",
        "import os as runtime\n"
        "condition or (runtime := object())\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "E3",
        "import os as runtime\n"
        "CHECK = (runtime := object()) if condition else None\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "E4",
        "import os as runtime\n"
        "CHECK = (runtime := object()) if condition else (runtime := object())\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "E5",
        "import os as runtime\n"
        "CHECK = left < middle < (runtime := object())\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "E6",
        "import os as runtime\nCHECK = (runtime := object())\nTOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "E7",
        "import os as runtime\n"
        "assert condition, (runtime := object())\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "E8",
        "import os as runtime\nassert condition, runtime.getenv('TOKEN')\n",
        (2,),
    ),
    (
        "E9",
        "import os\n"
        "runtime = object()\n"
        "try:\n"
        "    VALUE = ((runtime := os), may_raise(), (runtime := object()))\n"
        "except Exception:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (6,),
    ),
    (
        "E10",
        "import os\n"
        "runtime = object()\n"
        "VALUE = {(runtime := os): runtime.getenv('TOKEN'), (runtime := object()): 1}\n",
        (3,),
    ),
    (
        "E11",
        "import os\n"
        "runtime = object()\n"
        "VALUE = {(runtime := os): (runtime := object()), runtime.getenv('TOKEN'): 1}\n",
        (),
    ),
    (
        "E12",
        "import os\n"
        "runtime = object()\n"
        "try:\n"
        "    VALUE = ((runtime := os), missing, (runtime := object()))\n"
        "except Exception:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (6,),
    ),
)

_ENVIRONMENT_ROUND4_COMPREHENSION_CASES = (
    (
        "C1",
        "import os as runtime\n"
        "VALUES = [(runtime := object()) for item in providers]\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "C2",
        "import os as runtime\n"
        "VALUES = {(runtime := object()) for item in providers}\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "C3",
        "import os as runtime\n"
        "VALUES = {item: (runtime := object()) for item in providers}\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "C4",
        "import os\n"
        "runtime = object()\n"
        "VALUES = [(runtime.getenv('TOKEN'), (runtime := os)) for item in providers]\n",
        (3,),
    ),
    (
        "C5",
        "import os as runtime\n"
        "VALUES = [item for item in providers if condition and (runtime := object())]\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "C6",
        "import os as runtime\n"
        "VALUES = [(runtime := object()) for left in providers for right in others]\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (3,),
    ),
    (
        "C7",
        "import os\n"
        "runtime = object()\n"
        "VALUES = [(runtime.getenv('TOKEN'), (runtime := os)) "
        "for left in providers for right in others]\n",
        (3,),
    ),
    (
        "C8",
        "import os as runtime\n"
        "def build():\n"
        "    TOKEN = runtime.getenv('TOKEN')\n"
        "    VALUES = [(runtime := object()) for item in providers]\n"
        "    return VALUES\n",
        (),
    ),
    (
        "C9",
        "import os\n"
        "runtime = object()\n"
        "def build():\n"
        "    VALUES = [(runtime := os) for item in providers]\n"
        "def read_token():\n"
        "    return runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "C10",
        "import os as runtime\n"
        "def build():\n"
        "    TOKEN = runtime.getenv('TOKEN')\n"
        "    VALUES = ((runtime := object()) for item in providers)\n"
        "    return VALUES\n",
        (),
    ),
    (
        "C11",
        "import os\n"
        "runtime = object()\n"
        "VALUES = ((runtime.getenv('TOKEN'), (runtime := os)) for item in providers)\n",
        (3,),
    ),
    (
        "C12",
        "import os as runtime\n"
        "VALUES = [runtime.getenv('TOKEN') for item in providers "
        "if condition and (runtime := object())]\n",
        (),
    ),
    (
        "C13",
        "import os as runtime\n"
        "VALUES = (runtime.getenv('TOKEN') for item in providers "
        "if condition and (runtime := object()))\n",
        (),
    ),
)

_ENVIRONMENT_ROUND4_MATCH_CASES = (
    (
        "M1",
        "import os\n"
        "runtime = object()\n"
        "match value:\n"
        "    case _ if (runtime := os) and False:\n"
        "        runtime = object()\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (6,),
    ),
    (
        "M2",
        "import os as runtime\n"
        "match value:\n"
        "    case _ if (runtime := object()):\n"
        "        pass\n"
        "    case _:\n"
        "        pass\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "M3",
        "import os as runtime\n"
        "match value:\n"
        "    case _ if condition and (runtime := object()):\n"
        "        pass\n"
        "    case _:\n"
        "        pass\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (7,),
    ),
    (
        "M4",
        "import os\n"
        "runtime = object()\n"
        "match value:\n"
        "    case _ if not (runtime := os):\n"
        "        pass\n"
        "    case _:\n"
        "        TOKEN = runtime.getenv('TOKEN')\n",
        (7,),
    ),
    (
        "M5",
        "import os as runtime\n"
        "match value:\n"
        "    case _:\n"
        "        import json as runtime\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "M6",
        "import os as runtime\n"
        "match value:\n"
        "    case _ if condition:\n"
        "        import json as runtime\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (5,),
    ),
)

_ENVIRONMENT_ROUND4_CONDITION_CASES = (
    (
        "X1",
        "import os as runtime\n"
        "if condition and (runtime := object()):\n"
        "    pass\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (4,),
    ),
    (
        "X2",
        "import os as runtime\n"
        "while condition and (runtime := object()):\n"
        "    break\n"
        "TOKEN = runtime.getenv('TOKEN')\n",
        (4,),
    ),
    (
        "X3",
        "import os as runtime\n"
        "try:\n"
        "    assert condition, ((runtime := object()), may_raise())\n"
        "except Exception:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (5,),
    ),
    (
        "X4",
        "import os as runtime\n"
        "try:\n"
        "    might_raise()\n"
        "    runtime = object()\n"
        "finally:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (6,),
    ),
)

_ENVIRONMENT_ROUND4_GROUPS = (
    _ENVIRONMENT_ROUND4_TARGET_CASES,
    _ENVIRONMENT_ROUND4_EXPRESSION_CASES,
    _ENVIRONMENT_ROUND4_COMPREHENSION_CASES,
    _ENVIRONMENT_ROUND4_MATCH_CASES,
    _ENVIRONMENT_ROUND4_CONDITION_CASES,
)
assert tuple(len(group) for group in _ENVIRONMENT_ROUND4_GROUPS) == (9, 12, 13, 6, 4)
_ENVIRONMENT_ROUND4_CASES = sum(_ENVIRONMENT_ROUND4_GROUPS, ())
_ENVIRONMENT_ROUND4_IDS = tuple(case_id for case_id, _, _ in _ENVIRONMENT_ROUND4_CASES)
assert len(_ENVIRONMENT_ROUND4_CASES) == 44
assert len(set(_ENVIRONMENT_ROUND4_IDS)) == 44


@pytest.mark.parametrize(
    ("case_id", "source", "expected_lines"),
    _ENVIRONMENT_ROUND4_CASES,
    ids=_ENVIRONMENT_ROUND4_IDS,
)
def test_environment_round4_expression_and_control_flow_matrix(
    checker: ModuleType,
    tmp_path: Path,
    case_id: str,
    source: str,
    expected_lines: tuple[int, ...],
) -> None:
    assert case_id
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    assert tuple(violation.line for violation in report.violations) == expected_lines


_ENVIRONMENT_ROUND5_EXPANSION_CASES = (
    (
        "R5P01",
        """import os
runtime = None
try:
    VALUE = [*(runtime := os), (runtime := None)]
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (6,),
    ),
    (
        "R5P02",
        """import os
runtime = None
try:
    VALUE = {*(runtime := os), (runtime := None)}
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (6,),
    ),
    (
        "R5P03",
        """import os
runtime = None
try:
    VALUE = {**(runtime := os), 'tail': (runtime := None)}
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (6,),
    ),
    (
        "R5P04",
        """import os
consume = lambda *args, **kwargs: None
runtime = None
try:
    consume(*(runtime := os), (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (7,),
    ),
    (
        "R5P05",
        """import os
consume = lambda *args, **kwargs: None
runtime = None
try:
    consume(**(runtime := os), tail=(runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (7,),
    ),
    (
        "R5P06",
        """import os
runtime = None
try:
    class Settings(*(runtime := os), metaclass=(runtime := None)):
        pass
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (7,),
    ),
    (
        "R5P07",
        """import os
runtime = None
try:
    VALUE = [*(runtime := None)]
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (),
    ),
    (
        "R5P08",
        """import os
runtime = None
try:
    VALUE = (*(runtime := os), (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
runtime = None
try:
    class Settings(**(runtime := os), tail=(runtime := None)):
        pass
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (6, 12),
    ),
)

_ENVIRONMENT_ROUND5_COMPREHENSION_CASES = (
    (
        "R5C01",
        """import os
runtime = None
try:
    VALUE = ((runtime := os), [item for item in runtime], (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (6,),
    ),
    (
        "R5C02",
        """import os
runtime = None
try:
    VALUE = ((runtime := os), (item for item in runtime), (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (6,),
    ),
    (
        "R5C03",
        """import os
runtime = None
providers = [None]
try:
    VALUES = [right for left in providers if (runtime := os) for right in runtime]
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (7,),
    ),
    (
        "R5C04",
        """import os
class Broken:
    def __init__(self): self.done = False
    def __iter__(self): return self
    def __next__(self):
        if self.done: raise TypeError
        self.done = True
        return None
items = Broken()
runtime = None
try:
    VALUE = ([item for item in items if (runtime := os)], (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (14,),
    ),
    (
        "R5C05",
        """import os
runtime = None
providers = [None]
try:
    VALUE = ({((runtime := os), []) for item in providers}, (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (7,),
    ),
    (
        "R5C06",
        """import os
runtime = None
providers = [None]
try:
    VALUE = ({((runtime := os), []): None for item in providers}, (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (7,),
    ),
    (
        "R5C07",
        """import os
runtime = None
providers = [None]
try:
    VALUE = ({((runtime := os), []): (runtime := None) for item in providers}, (runtime := None))
except TypeError:
    TOKEN = runtime.getenv('TOKEN')
""",
        (),
    ),
    (
        "R5C08",
        """import os
runtime = None
providers = [None]
try:
    VALUE = ({((runtime := os), []) for item in providers}, (runtime := None))
finally:
    TOKEN = runtime.getenv('TOKEN')
""",
        (7,),
    ),
)

_ENVIRONMENT_ROUND5_ANNOTATION_CASES = (
    (
        "R5A01",
        """import os
runtime = None
def configure(*args: (runtime := None), kw: (runtime := os)):
    pass
TOKEN = runtime.getenv('TOKEN')
""",
        (5,),
    ),
    (
        "R5A02",
        """import os
runtime = None
def configure(*args: (runtime := os), kw: (runtime := None)):
    pass
TOKEN = runtime.getenv('TOKEN')
""",
        (),
    ),
    (
        "R5A03",
        """import os
runtime = None
def configure(*args: (runtime := os), kw: runtime.getenv('TOKEN'), **kwargs: (runtime := None)):
    pass
""",
        (3,),
    ),
    (
        "R5A04",
        """import os
runtime = None
def configure(value=(runtime := os), *args: (runtime := None)):
    pass
TOKEN = runtime.getenv('TOKEN')
""",
        (),
    ),
    (
        "R5A05",
        """import os
runtime = None
def configure(left: (runtime := None), /, right: (runtime := os)):
    pass
TOKEN = runtime.getenv('TOKEN')
""",
        (),
    ),
    (
        "R5A06",
        """import os
runtime = None
def configure(left: (runtime := os), /, right: (runtime := None)):
    pass
TOKEN = runtime.getenv('TOKEN')
""",
        (5,),
    ),
)

_ENVIRONMENT_ROUND5_LEXICAL_CASES = (
    (
        "R5L01",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    def inner(value=(runtime := None)):
        return value
    return inner
""",
        (),
    ),
    (
        "R5L02",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    inner = lambda value=(runtime := None): value
    return inner
""",
        (),
    ),
    (
        "R5L03",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    @(runtime := (lambda function: function))
    def inner():
        pass
    return inner
""",
        (),
    ),
    (
        "R5L04",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    def inner() -> (runtime := None):
        return None
    return inner
""",
        (),
    ),
    (
        "R5L05",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    class Inner((runtime := object)):
        pass
    return Inner
""",
        (),
    ),
    (
        "R5L06",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    class Inner(metaclass=(runtime := type)):
        pass
    return Inner
""",
        (),
    ),
    (
        "R5L07",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    def inner():
        return (runtime := None)
    return inner
""",
        (3,),
    ),
    (
        "R5L08",
        """import os as runtime
def outer():
    TOKEN = runtime.getenv('TOKEN')
    inner = lambda: (runtime := None)
    class Inner:
        runtime = None
    return inner, Inner
""",
        (3,),
    ),
    (
        "R5L09",
        """import os as runtime
def outer():
    global runtime
    TOKEN = runtime.getenv('TOKEN')
    def inner(value=(runtime := None)):
        return value
    return inner
""",
        (4,),
    ),
    (
        "R5L10",
        """def parent():
    import os as runtime
    def outer():
        nonlocal runtime
        TOKEN = runtime.getenv('TOKEN')
        def inner(value=(runtime := None)):
            return value
        return inner
    return outer
""",
        (5,),
    ),
)

_ENVIRONMENT_ROUND5_HANDLER_CASES = (
    (
        "H5N",
        "import os as runtime\n"
        "class Settings:\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except RuntimeError as runtime:\n"
        "        pass\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (7,),
    ),
    (
        "H5B",
        "import os as runtime\n"
        "class Settings:\n"
        "    runtime = None\n"
        "    for item in [1]:\n"
        "        try:\n"
        "            raise RuntimeError\n"
        "        except RuntimeError as runtime:\n"
        "            break\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (9,),
    ),
    (
        "H5C",
        "import os as runtime\n"
        "class Settings:\n"
        "    runtime = None\n"
        "    for item in [1]:\n"
        "        try:\n"
        "            raise RuntimeError\n"
        "        except RuntimeError as runtime:\n"
        "            continue\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (9,),
    ),
    (
        "H5R",
        "import os as runtime\n"
        "def finish():\n"
        "    try:\n"
        "        raise RuntimeError\n"
        "    except RuntimeError as runtime:\n"
        "        return None\n"
        "    finally:\n"
        "        TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "H5X",
        "import os as runtime\n"
        "class Settings:\n"
        "    try:\n"
        "        try:\n"
        "            raise RuntimeError\n"
        "        except RuntimeError as runtime:\n"
        "            raise ValueError\n"
        "    finally:\n"
        "        TOKEN = runtime.getenv('TOKEN')\n",
        (9,),
    ),
    (
        "H5S",
        "import os as runtime\n"
        "class Settings:\n"
        "    try:\n"
        "        raise ExceptionGroup('errors', [RuntimeError()])\n"
        "    except* RuntimeError as runtime:\n"
        "        pass\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (7,),
    ),
    (
        "H6O",
        "import os\n"
        "runtime = None\n"
        "try:\n"
        "    raise KeyError\n"
        "except (ValueError if (runtime := os) else ValueError):\n"
        "    pass\n"
        "except KeyError:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (8,),
    ),
    (
        "H6P",
        "import os as runtime\n"
        "try:\n"
        "    raise KeyError\n"
        "except (ValueError if (runtime := None) else ValueError):\n"
        "    pass\n"
        "except KeyError:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "H6B",
        "import os\n"
        "runtime = None\n"
        "try:\n"
        "    raise KeyError\n"
        "except (ValueError if (runtime := os) else ValueError):\n"
        "    pass\n"
        "except:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (8,),
    ),
    (
        "H6U",
        "import os as runtime\n"
        "try:\n"
        "    raise KeyError\n"
        "except (ValueError if (runtime := None) else ValueError):\n"
        "    pass\n"
        "finally:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "H6E",
        "import os\n"
        "runtime = None\n"
        "try:\n"
        "    try:\n"
        "        raise KeyError\n"
        "    except ((runtime := os).missing, (runtime := None), ValueError)[-1]:\n"
        "        pass\n"
        "    except KeyError:\n"
        "        runtime = None\n"
        "finally:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (11,),
    ),
    (
        "H6R",
        "import os as os_alias\n"
        "def finish():\n"
        "    runtime = None\n"
        "    try:\n"
        "        raise KeyError\n"
        "    except (ValueError if (runtime := None) else ValueError):\n"
        "        runtime = os_alias\n"
        "        return None\n"
        "    except KeyError:\n"
        "        TOKEN = runtime.getenv('TOKEN')\n",
        (),
    ),
    (
        "H6T",
        "import os\n"
        "runtime = None\n"
        "try:\n"
        "    raise ExceptionGroup('errors', [KeyError()])\n"
        "except* (ValueError if (runtime := os) else ValueError):\n"
        "    pass\n"
        "except* KeyError:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (8,),
    ),
    (
        "H6G",
        "import os\n"
        "runtime = None\n"
        "try:\n"
        "    raise ExceptionGroup('errors', [TypeError(), KeyError()])\n"
        "except* TypeError:\n"
        "    runtime = os\n"
        "except* KeyError:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (8,),
    ),
    (
        "H6Q",
        "import os as runtime\n"
        "try:\n"
        "    raise ExceptionGroup('errors', [TypeError(), KeyError()])\n"
        "except* TypeError:\n"
        "    runtime = None\n"
        "except* KeyError:\n"
        "    TOKEN = runtime.getenv('TOKEN')\n",
        (7,),
    ),
    (
        "H6D",
        "import os\n"
        "runtime = None\n"
        "try:\n"
        "    try:\n"
        "        raise ExceptionGroup('errors', [TypeError(), KeyError()])\n"
        "    except* TypeError:\n"
        "        runtime = os\n"
        "        raise RuntimeError\n"
        "    except* KeyError:\n"
        "        TOKEN = runtime.getenv('TOKEN')\n"
        "finally:\n"
        "    FINAL = runtime.getenv('TOKEN')\n",
        (10, 12),
    ),
)

_ENVIRONMENT_ROUND5_GROUPS = (
    _ENVIRONMENT_ROUND5_EXPANSION_CASES,
    _ENVIRONMENT_ROUND5_COMPREHENSION_CASES,
    _ENVIRONMENT_ROUND5_ANNOTATION_CASES,
    _ENVIRONMENT_ROUND5_LEXICAL_CASES,
    _ENVIRONMENT_ROUND5_HANDLER_CASES,
)
assert tuple(len(group) for group in _ENVIRONMENT_ROUND5_GROUPS) == (8, 8, 6, 10, 16)
_ENVIRONMENT_ROUND5_CASES = sum(_ENVIRONMENT_ROUND5_GROUPS, ())
_ENVIRONMENT_ROUND5_IDS = tuple(case_id for case_id, _, _ in _ENVIRONMENT_ROUND5_CASES)
assert len(_ENVIRONMENT_ROUND5_CASES) == 48
assert len(set(_ENVIRONMENT_ROUND5_IDS)) == 48
assert len({source for _, source, _ in _ENVIRONMENT_ROUND5_CASES}) == 48
assert set(_ENVIRONMENT_ROUND5_IDS).isdisjoint(_ENVIRONMENT_ROUND4_IDS)


@pytest.mark.parametrize(
    ("case_id", "source", "expected_lines"),
    _ENVIRONMENT_ROUND5_CASES,
    ids=_ENVIRONMENT_ROUND5_IDS,
)
def test_environment_round5_transfer_matrix(
    checker: ModuleType,
    tmp_path: Path,
    case_id: str,
    source: str,
    expected_lines: tuple[int, ...],
) -> None:
    assert case_id
    source_root = write_tree(tmp_path, {"workflow/example.py": source})

    report = checker.analyze_source_tree(source_root)

    actual_lines = tuple(
        violation.line
        for violation in report.violations
        if violation.rule_id == "IMP004_ENVIRONMENT_ACCESS"
    )
    assert actual_lines == expected_lines


def test_two_module_cycle_and_self_loop_are_reported_deterministically(
    checker: ModuleType, tmp_path: Path
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "contracts/__init__.py": "\n",
            "contracts/a.py": "import tsdaf.contracts.b\n",
            "contracts/b.py": "import tsdaf.contracts.a\n",
            "contracts/self_loop.py": "import tsdaf.contracts.self_loop\n",
        },
    )

    first = checker.analyze_source_tree(source_root)
    second = checker.analyze_source_tree(source_root)
    cycle_violations = [
        violation for violation in first.violations if violation.rule_id == "IMP005_IMPORT_CYCLE"
    ]

    assert len(cycle_violations) == 2
    assert first == second


def test_comments_and_strings_do_not_create_false_positives(
    checker: ModuleType, tmp_path: Path
) -> None:
    source_root = write_tree(
        tmp_path,
        {
            "contracts/__init__.py": "\n",
            "contracts/clean.py": (
                "# import harness; os.getenv('TOKEN')\n"
                "TEXT = 'google.genai and os.environ and tests.legacy_harness'\n"
            ),
        },
    )

    report = checker.analyze_source_tree(source_root)

    assert report.violations == ()


@pytest.mark.parametrize("case", ("missing", "empty", "non_package", "syntax", "utf8"))
def test_invalid_source_roots_are_errors_and_cli_exit_two(
    checker: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    source_root = tmp_path / "src" / "tsdaf"
    if case != "missing":
        source_root.mkdir(parents=True)
    if case not in {"missing", "empty", "non_package"}:
        (source_root / "__init__.py").write_text("\n", encoding="utf-8")
    if case == "non_package":
        (source_root / "module.py").write_text("\n", encoding="utf-8")
    if case == "syntax":
        (source_root / "broken.py").write_text("if True print('broken')\n", encoding="utf-8")
    if case == "utf8":
        (source_root / "broken.py").write_bytes(b"\xff\xfe")

    with pytest.raises(checker.BoundaryCheckError):
        checker.analyze_source_tree(source_root)
    exit_code = checker.main([str(source_root)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.startswith("BOUNDARY_ERROR: ")
    assert captured.err.count("\n") == 1


def test_violation_cli_output_is_repository_relative_and_exit_one(
    checker: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source_root = write_tree(tmp_path, {"workflow/example.py": "import harness\n"})

    exit_code = checker.main([str(source_root)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == (
        "src/tsdaf/workflow/example.py:1: IMP001_LEGACY_IMPORT legacy imports are forbidden\n"
    )
    assert captured.err == ""


def test_real_package_graph_is_nonempty_clean_and_has_bootstrap_edges(
    checker: ModuleType,
) -> None:
    report = checker.analyze_source_tree(REPOSITORY_ROOT / "src" / "tsdaf")
    edge_pairs = {(edge.source, edge.target) for edge in report.edges}

    assert len(report.modules) > 1
    assert ("tsdaf.__main__", "tsdaf.apps.cli") in edge_pairs
    assert ("tsdaf.apps.cli", "tsdaf.apps.commands.docs") in edge_pairs
    assert {rule_id: rules(report).count(rule_id) for rule_id in checker.RULE_IDS} == {
        "IMP001_LEGACY_IMPORT": 0,
        "IMP002_LAYER_IMPORT": 0,
        "IMP003_CONTRACTS_DEPENDENCY": 0,
        "IMP004_ENVIRONMENT_ACCESS": 0,
        "IMP005_IMPORT_CYCLE": 0,
    }


def test_checker_process_reports_clean_real_tree() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER_PATH), "src/tsdaf"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
