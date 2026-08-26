from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from tsdaf.contracts.execution import FailureCode

_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "paper_packages"


def _document_api() -> ModuleType:
    return importlib.import_module("tsdaf.lanes.document")


def _copy_package(tmp_path: Path, paper_id: str = "hyperfirin") -> Path:
    copied = tmp_path / paper_id
    shutil.copytree(_FIXTURE_ROOT / paper_id, copied)
    return copied


def _rewrite_manifest_digest(package_dir: Path, artifact_key: str, payload: bytes) -> None:
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    artifact = manifest[artifact_key]
    artifact["sha256"] = hashlib.sha256(payload).hexdigest()
    artifact["byte_size"] = len(payload)
    manifest_path.write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


def test_valid_copied_package_loads_normalized_content_and_manifest_inventory(
    tmp_path: Path,
) -> None:
    api = _document_api()
    package_dir = _copy_package(tmp_path)

    loaded = api.load_paper_package(package_dir)

    assert loaded.manifest.paper_id == "hyperfirin"
    assert loaded.document["paper_id"] == "hyperfirin"
    assert loaded.document["artifacts"]["markdown"] == "full.md"
    assert len(loaded.blocks) == 135
    assert tuple(
        row["asset_ref"] for row in loaded.blocks if row["asset_ref"] is not None
    ) == tuple(asset.relative_path for asset in loaded.manifest.assets)


def test_missing_manifest_asset_is_reported_as_asset_missing(tmp_path: Path) -> None:
    api = _document_api()
    package_dir = _copy_package(tmp_path)
    relative_path = "images/34a5693860a2f1a7bf1361aacaa3475dafbb4a19f50eb9f39b866c4732d94a8b.jpg"
    (package_dir / relative_path).unlink()

    with pytest.raises(api.PackageLoadError) as raised:
        api.load_paper_package(package_dir)

    assert raised.value.code is FailureCode.ASSET_MISSING
    assert relative_path in str(raised.value)


def test_one_byte_artifact_mutation_is_reported_before_parsing(tmp_path: Path) -> None:
    api = _document_api()
    package_dir = _copy_package(tmp_path)
    pdf_path = package_dir / "source" / "paper.pdf"
    payload = bytearray(pdf_path.read_bytes())
    payload[0] ^= 1
    pdf_path.write_bytes(payload)

    with pytest.raises(api.PackageLoadError) as raised:
        api.load_paper_package(package_dir)

    assert raised.value.code is FailureCode.HASH_MISMATCH
    assert "source/paper.pdf" in str(raised.value)


def test_unlisted_file_fails_closed_as_package_invalid(tmp_path: Path) -> None:
    api = _document_api()
    package_dir = _copy_package(tmp_path)
    (package_dir / "unexpected.txt").write_text("not in the manifest", encoding="utf-8")

    with pytest.raises(api.PackageLoadError) as raised:
        api.load_paper_package(package_dir)

    assert raised.value.code is FailureCode.PACKAGE_INVALID
    assert "unexpected.txt" in str(raised.value)


def test_unknown_normalized_block_type_is_package_invalid(tmp_path: Path) -> None:
    api = _document_api()
    package_dir = _copy_package(tmp_path)
    blocks_path = package_dir / "normalized" / "blocks.jsonl"
    lines = blocks_path.read_text(encoding="utf-8").splitlines()
    first_row = json.loads(lines[0])
    first_row["type"] = "unknown_visual_text"
    lines[0] = json.dumps(first_row, ensure_ascii=False, separators=(",", ":"))
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    blocks_path.write_bytes(payload)
    _rewrite_manifest_digest(package_dir, "normalized_blocks", payload)

    with pytest.raises(api.PackageLoadError) as raised:
        api.load_paper_package(package_dir)

    assert raised.value.code is FailureCode.PACKAGE_INVALID
    assert "normalized/blocks.jsonl" in str(raised.value)


@pytest.mark.parametrize(
    "invalid_path",
    ["../outside.pdf", "/outside.pdf"],
    ids=["escaping", "absolute"],
)
def test_nonrelative_manifest_paths_are_translated_to_package_invalid(
    tmp_path: Path,
    invalid_path: str,
) -> None:
    package_dir = _copy_package(tmp_path)
    manifest_path = package_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["source_pdf"]["relative_path"] = invalid_path
    manifest_path.write_bytes(
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    api = _document_api()

    with pytest.raises(api.PackageLoadError) as raised:
        api.load_paper_package(package_dir)

    assert raised.value.code is FailureCode.PACKAGE_INVALID
    assert invalid_path in str(raised.value)


def test_manifest_artifact_reparse_component_escaping_package_is_package_invalid(
    tmp_path: Path,
) -> None:
    package_dir = _copy_package(tmp_path)
    source_dir = package_dir / "source"
    outside_source_dir = tmp_path / "outside" / "source"
    outside_source_dir.mkdir(parents=True)
    shutil.copy2(source_dir / "paper.pdf", outside_source_dir / "paper.pdf")
    (source_dir / "paper.pdf").unlink()
    source_dir.rmdir()
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(source_dir), str(outside_source_dir)],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        source_dir.symlink_to(outside_source_dir, target_is_directory=True)
    api = _document_api()

    with pytest.raises(api.PackageLoadError) as raised:
        api.load_paper_package(package_dir)

    assert raised.value.code is FailureCode.PACKAGE_INVALID
    assert "source/paper.pdf" in str(raised.value)
