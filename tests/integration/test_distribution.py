from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

from packaging.specifiers import SpecifierSet

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_PACKAGE_MEMBERS = {
    "tsdaf/__init__.py",
    "tsdaf/__main__.py",
    "tsdaf/py.typed",
    "tsdaf/contracts/__init__.py",
    "tsdaf/workflow/__init__.py",
    "tsdaf/lanes/__init__.py",
    "tsdaf/adapters/__init__.py",
    "tsdaf/apps/__init__.py",
    "tsdaf/apps/cli.py",
    "tsdaf/apps/commands/__init__.py",
    "tsdaf/apps/commands/docs.py",
}
FORBIDDEN_PAYLOAD_PARTS = (
    "tests/",
    "legacy_harness/",
    "scripts/",
    "research/",
    ".superpowers/",
    "harness/",
)


def build_wheel(output_directory: Path) -> tuple[Path, str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output_directory),
            ".",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = tuple(output_directory.glob("tsdaf-*.whl"))
    assert len(wheels) == 1
    digest = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    return wheels[0], digest


def test_wheel_contains_typed_package_and_no_repository_payload(tmp_path: Path) -> None:
    wheel, digest = build_wheel(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())

    assert EXPECTED_PACKAGE_MEMBERS <= members
    assert not any(part in member for part in FORBIDDEN_PAYLOAD_PARTS for member in members)
    assert len(digest) == 64


def test_wheel_metadata_declares_runtime_contract_and_console_entrypoint(tmp_path: Path) -> None:
    wheel, _ = build_wheel(tmp_path)

    with zipfile.ZipFile(wheel) as archive:
        metadata_member = next(name for name in archive.namelist() if name.endswith("/METADATA"))
        entrypoint_member = next(
            name for name in archive.namelist() if name.endswith("/entry_points.txt")
        )
        metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_member))
        entrypoints = archive.read(entrypoint_member).decode("utf-8")

    assert SpecifierSet(metadata["Requires-Python"]) == SpecifierSet(">=3.11,<3.14")
    assert "pydantic<3,>=2.10" in metadata.get_all("Requires-Dist")
    assert "tsdaf = tsdaf.apps.cli:main" in entrypoints
