from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from tsdaf.contracts.package import PaperManifest
from tsdaf.contracts.source import ArtifactRef

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "paper_packages"
_EXPECTED_PACKAGES: dict[str, dict[str, Any]] = {
    "alchivemycin_A": {
        "display_name": "Alchivemycin A",
        "pdf_sha256": "5ad8252e2ef59f608c7e4c47720b9ef4899e672b73d374d2e73fcb11d46a236f",
        "pdf_bytes": 1_275_980,
        "document_sha256": "f9949513fc8d286cd64aecf34291554567f4dba3b120db61b023a352a5594777",
        "document_bytes": 10_869,
        "blocks_sha256": "65d86d6c648cee0abaf97eeeef9966ab7183aadcfc37bf81f8a0d105a9e2c2c5",
        "blocks_bytes": 178_644,
        "blocks": 176,
        "assets": 5,
        "asset_bytes": 804_509,
    },
    "hyperfirin": {
        "display_name": "Hyperfirin",
        "pdf_sha256": "98173d29f2fa21e540559562285ba382d25eb63f374985db47c88b0ebf433759",
        "pdf_bytes": 1_622_880,
        "document_sha256": "e175644e8956d11d063388f047b48645c0d321851530d414da3df45ab31619aa",
        "document_bytes": 8_169,
        "blocks_sha256": "e7ad44946605914e2f6ec4ede840828269630fbcde1c0aeb6ce80deff9cd397b",
        "blocks_bytes": 134_786,
        "blocks": 135,
        "assets": 9,
        "asset_bytes": 291_878,
    },
    "ineleganolide": {
        "display_name": "Ineleganolide",
        "pdf_sha256": "3ddb93a0267dbce84dd083eb6a1c3ff063ef85f61d01681ff50d5b3de766d7b1",
        "pdf_bytes": 1_733_430,
        "document_sha256": "fffe545a29eebf0f7eb4977629cc1ba0f284f634970c12747baf202258aebc9a",
        "document_bytes": 7_204,
        "blocks_sha256": "4a29cbd33ed5532810fa21325dd6325672eac97c41acd6cbfbe1c5daddb64a65",
        "blocks_bytes": 116_669,
        "blocks": 116,
        "assets": 13,
        "asset_bytes": 337_673,
    },
    "kalmanol": {
        "display_name": "Kalmanol",
        "pdf_sha256": "7297073433fd7008dd1a9611d566705ee322cf794cbf54d774077959263c8680",
        "pdf_bytes": 5_865_711,
        "document_sha256": "bce374722d51dd7805dedbc9cc0f75ccb2d5725d081ae69b7be8efffd14f8d3c",
        "document_bytes": 6_433,
        "blocks_sha256": "bd2ad7a2de7209fa4412a4d4f35b20f0694d184fd7b4cfcb7beff63841b2d8fb",
        "blocks_bytes": 99_625,
        "blocks": 101,
        "assets": 6,
        "asset_bytes": 493_047,
    },
    "principinol_B": {
        "display_name": "Principinol B",
        "pdf_sha256": "38b78065c24f052132ef2f0039f16e3a27633506e669f32640d668fc4f779fc1",
        "pdf_bytes": 3_629_408,
        "document_sha256": "fc42d151ea40fa95e597c927d3f37b99137b28ec1c3eb11656959a7f34b2a713",
        "document_bytes": 5_723,
        "blocks_sha256": "9c8b329d5b0c0d5c62c3bd7f1d1ef3a277b6056e1fad6b609526ca1a801bd54c",
        "blocks_bytes": 86_089,
        "blocks": 87,
        "assets": 4,
        "asset_bytes": 427_551,
    },
}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _all_artifacts(manifest: PaperManifest) -> tuple[ArtifactRef, ...]:
    return (
        manifest.source_pdf,
        manifest.normalized_document,
        manifest.normalized_blocks,
        *manifest.assets,
    )


def _artifact_diagnostics(
    package_root: Path,
    paper_id: str,
    artifact: ArtifactRef,
) -> list[str]:
    diagnostics: list[str] = []
    relative_path = artifact.relative_path
    posix_path = PurePosixPath(relative_path)
    if posix_path.as_posix() != relative_path or posix_path.is_absolute():
        return [f"{paper_id}:{relative_path}:NONCANONICAL_PATH"]

    artifact_path = package_root.joinpath(*posix_path.parts)
    if artifact_path.is_symlink():
        return [f"{paper_id}:{relative_path}:SYMLINK"]
    if not artifact_path.is_file():
        return [f"{paper_id}:{relative_path}:NOT_REGULAR_FILE"]
    try:
        artifact_path.resolve(strict=True).relative_to(package_root.resolve(strict=True))
    except (OSError, ValueError):
        return [f"{paper_id}:{relative_path}:PATH_ESCAPE"]

    payload = artifact_path.read_bytes()
    if len(payload) != artifact.byte_size:
        diagnostics.append(f"{paper_id}:{relative_path}:SIZE_MISMATCH")
    if _sha256_bytes(payload) != artifact.sha256:
        diagnostics.append(f"{paper_id}:{relative_path}:HASH_MISMATCH")
    return diagnostics


def _text_diagnostics(path: Path, paper_id: str) -> list[str]:
    payload = path.read_bytes()
    relative_path = (
        path.name if path.name == "manifest.json" else path.parent.name + "/" + path.name
    )
    diagnostics: list[str] = []
    if payload.startswith(b"\xef\xbb\xbf"):
        diagnostics.append(f"{paper_id}:{relative_path}:UTF8_BOM")
    if b"\r" in payload:
        diagnostics.append(f"{paper_id}:{relative_path}:CR_BYTE")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        diagnostics.append(f"{paper_id}:{relative_path}:INVALID_UTF8")
    return diagnostics


def _package_diagnostics(
    package_root: Path,
    paper_id: str,
    expected: dict[str, Any],
) -> tuple[list[str], Counter[str]]:
    diagnostics: list[str] = []
    stats: Counter[str] = Counter()
    if package_root.is_symlink():
        return [f"{paper_id}:PACKAGE_SYMLINK"], stats
    if not package_root.is_dir():
        return [f"{paper_id}:MISSING_PACKAGE"], stats

    manifest_path = package_root / "manifest.json"
    if manifest_path.is_symlink():
        return [f"{paper_id}:MANIFEST_SYMLINK"], stats
    if not manifest_path.is_file():
        return [f"{paper_id}:MISSING_MANIFEST"], stats
    diagnostics.extend(_text_diagnostics(manifest_path, paper_id))
    try:
        manifest = PaperManifest.model_validate_json(manifest_path.read_bytes())
    except ValueError as error:
        label = "PATH_ESCAPE" if "noncanonical segment" in str(error) else "MANIFEST_INVALID"
        return [*diagnostics, f"{paper_id}:{label}"], stats

    if manifest.paper_id != paper_id:
        diagnostics.append(f"{paper_id}:PAPER_ID_MISMATCH")
    if manifest.display_name != expected["display_name"]:
        diagnostics.append(f"{paper_id}:DISPLAY_NAME_MISMATCH")

    frozen_artifacts = (
        (
            manifest.source_pdf,
            "application/pdf",
            "source/paper.pdf",
            expected["pdf_sha256"],
            expected["pdf_bytes"],
        ),
        (
            manifest.normalized_document,
            "application/json",
            "normalized/document.json",
            expected["document_sha256"],
            expected["document_bytes"],
        ),
        (
            manifest.normalized_blocks,
            "application/x-ndjson",
            "normalized/blocks.jsonl",
            expected["blocks_sha256"],
            expected["blocks_bytes"],
        ),
    )
    for artifact, media_type, relative_path, sha256, byte_size in frozen_artifacts:
        if (
            artifact.media_type,
            artifact.relative_path,
            artifact.sha256,
            artifact.byte_size,
        ) != (media_type, relative_path, sha256, byte_size):
            diagnostics.append(f"{paper_id}:{relative_path}:FROZEN_REF_MISMATCH")

    for artifact in manifest.assets:
        if artifact.media_type != "image/jpeg":
            diagnostics.append(f"{paper_id}:{artifact.relative_path}:MEDIA_TYPE_MISMATCH")
        if re.fullmatch(r"images/[0-9a-f]{64}\.jpg", artifact.relative_path) is None:
            diagnostics.append(f"{paper_id}:{artifact.relative_path}:ASSET_PATH_MISMATCH")
        if PurePosixPath(artifact.relative_path).stem == artifact.sha256:
            diagnostics.append(f"{paper_id}:{artifact.relative_path}:FILENAME_IS_CONTENT_HASH")

    artifacts = _all_artifacts(manifest)
    expected_files = {"manifest.json", *(artifact.relative_path for artifact in artifacts)}
    actual_files = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual_files != expected_files:
        diagnostics.append(f"{paper_id}:FILE_INVENTORY_MISMATCH")

    for artifact in artifacts:
        diagnostics.extend(_artifact_diagnostics(package_root, paper_id, artifact))
        stats["artifact_bytes"] += artifact.byte_size
        stats["artifact_files"] += 1

    document_path = package_root / manifest.normalized_document.relative_path
    blocks_path = package_root / manifest.normalized_blocks.relative_path
    if not document_path.is_file() or not blocks_path.is_file():
        return diagnostics, stats
    diagnostics.extend(_text_diagnostics(document_path, paper_id))
    diagnostics.extend(_text_diagnostics(blocks_path, paper_id))
    try:
        document = json.loads(document_path.read_bytes())
        block_lines = blocks_path.read_bytes().splitlines()
        blocks = [json.loads(line) for line in block_lines]
    except (UnicodeDecodeError, json.JSONDecodeError):
        diagnostics.append(f"{paper_id}:NORMALIZED_JSON_INVALID")
        return diagnostics, stats

    expected_pdf_sha256 = expected["pdf_sha256"]
    if document.get("source_sha256") != expected_pdf_sha256:
        diagnostics.append(f"{paper_id}:DOCUMENT_SOURCE_MISMATCH")
    if any(block.get("source_sha256") != expected_pdf_sha256 for block in blocks):
        diagnostics.append(f"{paper_id}:BLOCK_SOURCE_MISMATCH")
    if any(
        block.get("source_locator", {}).get("pdf_sha256") != expected_pdf_sha256 for block in blocks
    ):
        diagnostics.append(f"{paper_id}:LOCATOR_SOURCE_MISMATCH")

    page_block_ids = tuple(
        block_id for page in document.get("pages", ()) for block_id in page.get("block_ids", ())
    )
    evidence_ids = tuple(block.get("evidence_id") for block in blocks)
    declared_blocks = document.get("counts", {}).get("blocks")
    if page_block_ids != evidence_ids:
        diagnostics.append(f"{paper_id}:BLOCK_ORDER_MISMATCH")
    if len(blocks) != expected["blocks"] or declared_blocks != expected["blocks"]:
        diagnostics.append(f"{paper_id}:BLOCK_COUNT_MISMATCH")

    block_asset_refs = [
        block["asset_ref"] for block in blocks if block.get("asset_ref") is not None
    ]
    first_occurrence_refs = tuple(dict.fromkeys(block_asset_refs))
    manifest_asset_refs = tuple(artifact.relative_path for artifact in manifest.assets)
    if len(first_occurrence_refs) != len(block_asset_refs):
        diagnostics.append(f"{paper_id}:DUPLICATE_BLOCK_ASSET_REF")
    if first_occurrence_refs != manifest_asset_refs:
        diagnostics.append(f"{paper_id}:ASSET_ORDER_MISMATCH")
    if len(manifest.assets) != expected["assets"]:
        diagnostics.append(f"{paper_id}:ASSET_COUNT_MISMATCH")

    asset_bytes = sum(artifact.byte_size for artifact in manifest.assets)
    if asset_bytes != expected["asset_bytes"]:
        diagnostics.append(f"{paper_id}:ASSET_BYTES_MISMATCH")
    stats["blocks"] += len(blocks)
    stats["assets"] += len(manifest.assets)
    stats["declared_images"] += int(document.get("counts", {}).get("images", 0))
    for block in blocks:
        if block.get("asset_ref") is not None:
            stats[f"asset_type:{block.get('type')}"] += 1
    return diagnostics, stats


def _tree_diagnostics(fixture_root: Path) -> list[str]:
    diagnostics: list[str] = []
    expected_ids = set(_EXPECTED_PACKAGES)
    actual_ids = {path.name for path in fixture_root.iterdir()} if fixture_root.is_dir() else set()
    if actual_ids != expected_ids:
        diagnostics.append("FIXTURE_SET_MISMATCH")

    totals: Counter[str] = Counter()
    for paper_id, expected in _EXPECTED_PACKAGES.items():
        package_diagnostics, package_stats = _package_diagnostics(
            fixture_root / paper_id,
            paper_id,
            expected,
        )
        diagnostics.extend(package_diagnostics)
        totals.update(package_stats)

    expected_totals = Counter(
        {
            "artifact_bytes": 17_136_278,
            "artifact_files": 52,
            "blocks": 615,
            "assets": 37,
            "declared_images": 34,
            "asset_type:image": 34,
            "asset_type:table": 3,
        }
    )
    if totals != expected_totals:
        diagnostics.append("AGGREGATE_INVENTORY_MISMATCH")
    return diagnostics


def _assert_source_fixtures_are_valid() -> None:
    assert _tree_diagnostics(_FIXTURE_ROOT) == []


def test_paper_packages_close_the_frozen_inventory() -> None:
    _assert_source_fixtures_are_valid()


def test_complete_paper_packages_remain_valid_after_copy(tmp_path: Path) -> None:
    _assert_source_fixtures_are_valid()
    copied_root = tmp_path / "paper_packages"
    shutil.copytree(_FIXTURE_ROOT, copied_root)

    assert _tree_diagnostics(copied_root) == []


def test_mutated_artifact_reports_hash_mismatch_without_touching_fixture(
    tmp_path: Path,
) -> None:
    _assert_source_fixtures_are_valid()
    original_path = _FIXTURE_ROOT / "alchivemycin_A" / "source" / "paper.pdf"
    original_sha256 = _sha256_bytes(original_path.read_bytes())
    copied_root = tmp_path / "paper_packages"
    shutil.copytree(_FIXTURE_ROOT, copied_root)
    copied_path = copied_root / "alchivemycin_A" / "source" / "paper.pdf"
    mutated_payload = bytearray(copied_path.read_bytes())
    mutated_payload[0] ^= 1
    copied_path.write_bytes(mutated_payload)

    diagnostics = _tree_diagnostics(copied_root)

    assert any("HASH_MISMATCH" in diagnostic for diagnostic in diagnostics)
    assert _sha256_bytes(original_path.read_bytes()) == original_sha256


def test_manifest_path_escape_is_rejected_by_contract_boundary(tmp_path: Path) -> None:
    _assert_source_fixtures_are_valid()
    copied_root = tmp_path / "paper_packages"
    shutil.copytree(_FIXTURE_ROOT, copied_root)
    manifest_path = copied_root / "alchivemycin_A" / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_bytes())
    manifest_payload["source_pdf"]["relative_path"] = "../outside.pdf"
    manifest_path.write_bytes(
        (json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )

    assert "alchivemycin_A:PATH_ESCAPE" in _tree_diagnostics(copied_root)
