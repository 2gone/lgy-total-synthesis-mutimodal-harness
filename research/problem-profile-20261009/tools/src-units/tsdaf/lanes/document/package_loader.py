"""Validated loading boundary for normalized TS-DAF paper packages."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import ValidationError

from tsdaf.contracts.execution import FailureCode
from tsdaf.contracts.package import PaperManifest
from tsdaf.contracts.source import ArtifactRef

# The block types the intake line (MinerU) writes. The first nine were the corpus of the
# fixtures; the 200-paper measurement (ledger E68) added three MinerU emits for chemistry
# papers: `chart` (a picture block by another name -- it carries an asset and the intake
# counts it among the images), `equation` (LaTeX text) and `code` (a scheme MinerU typeset
# as an algorithm block, sub_type "algorithm"). The 200-selection's supplements add an
# `image` with sub_type "seal" (a stamp or logo MinerU told apart from a figure); the
# sub_type is MinerU's word and is carried, not interpreted.
_NORMALIZED_TYPES = frozenset(
    {
        "aside_text",
        "chart",
        "code",
        "equation",
        "footer",
        "header",
        "image",
        "page_footnote",
        "page_number",
        "ref_text",
        "table",
        "text",
    }
)
# Types whose block is a picture: an asset is required, and the intake's image count
# covers them. `table` is a text block that may carry a picture behind it: MinerU
# sometimes gets neither a picture nor HTML text for a whole-page table and leaves
# the reference empty, which the loader reads as "no asset".
PICTURE_TYPES = frozenset({"image", "chart"})
_VISUAL_TYPES = PICTURE_TYPES | {"table"}
_SUBTYPED_TYPES = frozenset({"code", "image"})
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class PackageLoadError(Exception):
    """A typed, path-scoped package loading failure."""

    __slots__ = ("code", "message", "relative_path")

    def __init__(self, code: FailureCode, relative_path: str, message: str) -> None:
        self.code = code
        self.relative_path = relative_path
        self.message = message
        super().__init__(f"{relative_path}: {message}")


@dataclass(frozen=True, slots=True)
class LoadedPaperPackage:
    """Validated manifest and normalized inputs rooted at one package directory."""

    package_dir: Path
    manifest: PaperManifest
    document: dict[str, Any]
    blocks: tuple[dict[str, Any], ...]


def _invalid(relative_path: str, message: str) -> PackageLoadError:
    return PackageLoadError(FailureCode.PACKAGE_INVALID, relative_path, message)


def _lstat(path: Path, relative_path: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as error:
        raise _invalid(relative_path, f"cannot inspect path: {error}") from error


def _is_reparse(stat_result: os.stat_result) -> bool:
    return stat.S_ISLNK(stat_result.st_mode) or bool(
        getattr(stat_result, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _resolve_package_root(package_dir: Path) -> Path:
    path = Path(package_dir)
    try:
        root_stat = path.lstat()
    except FileNotFoundError as error:
        raise _invalid(".", "package directory does not exist") from error
    except OSError as error:
        raise _invalid(".", f"cannot inspect package directory: {error}") from error
    if _is_reparse(root_stat):
        raise _invalid(".", "package directory is a symlink or reparse point")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise _invalid(".", "package path is not a directory")
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise _invalid(".", f"cannot resolve package directory: {error}") from error


def _load_manifest(root: Path) -> PaperManifest:
    manifest_path = root / "manifest.json"
    try:
        manifest_stat = manifest_path.lstat()
    except FileNotFoundError as error:
        raise _invalid("manifest.json", "manifest is missing") from error
    except OSError as error:
        raise _invalid("manifest.json", f"cannot inspect manifest: {error}") from error
    if _is_reparse(manifest_stat) or not stat.S_ISREG(manifest_stat.st_mode):
        raise _invalid("manifest.json", "manifest is not a contained regular file")
    try:
        text = manifest_path.read_bytes().decode("utf-8")
        return PaperManifest.model_validate_json(text)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise _invalid("manifest.json", f"invalid manifest: {error}") from error


def _artifact_path(root: Path, artifact: ArtifactRef) -> Path:
    relative_path = artifact.relative_path
    current = root
    parts = PurePosixPath(relative_path).parts
    for index, part in enumerate(parts):
        current /= part
        try:
            current_stat = current.lstat()
        except FileNotFoundError as error:
            raise PackageLoadError(
                FailureCode.ASSET_MISSING,
                relative_path,
                "manifest-listed file is missing",
            ) from error
        except OSError as error:
            message = f"cannot inspect manifest-listed path: {error}"
            raise _invalid(relative_path, message) from error
        if _is_reparse(current_stat):
            raise _invalid(relative_path, f"path component {part!r} is a symlink or reparse point")
        if index < len(parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
            raise _invalid(relative_path, f"path component {part!r} is not a directory")
        if index == len(parts) - 1 and not stat.S_ISREG(current_stat.st_mode):
            raise _invalid(relative_path, "manifest-listed path is not a regular file")
    try:
        current.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as error:
        raise _invalid(relative_path, "manifest-listed path escapes package root") from error
    return current


def _inventory(root: Path) -> set[str]:
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as error:
            relative = directory.relative_to(root).as_posix() or "."
            raise _invalid(relative, f"cannot enumerate package: {error}") from error
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            entry_stat = _lstat(path, relative)
            if _is_reparse(entry_stat):
                raise _invalid(relative, "package contains a symlink or reparse point")
            if stat.S_ISDIR(entry_stat.st_mode):
                pending.append(path)
            elif stat.S_ISREG(entry_stat.st_mode):
                files.add(relative)
            else:
                raise _invalid(relative, "package contains a non-regular filesystem entry")
    return files


def _verify_artifact_bytes(path: Path, artifact: ArtifactRef) -> None:
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                byte_size += len(chunk)
    except OSError as error:
        message = f"cannot read manifest-listed file: {error}"
        raise _invalid(artifact.relative_path, message) from error
    if byte_size != artifact.byte_size or digest.hexdigest() != artifact.sha256:
        raise PackageLoadError(
            FailureCode.HASH_MISMATCH,
            artifact.relative_path,
            "final bytes do not match declared SHA-256 and size",
        )


def _decode_json(path: Path, relative_path: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _invalid(relative_path, f"invalid UTF-8 JSON: {error}") from error
    if not isinstance(payload, dict):
        raise _invalid(relative_path, "JSON root must be an object")
    return payload


def _decode_jsonl(path: Path, relative_path: str) -> tuple[dict[str, Any], ...]:
    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise _invalid(relative_path, f"invalid UTF-8 JSONL: {error}") from error
    if not lines or any(not line.strip() for line in lines):
        raise _invalid(relative_path, "JSONL must contain only non-empty rows")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise _invalid(relative_path, f"invalid JSONL row {line_number}: {error}") from error
        if not isinstance(row, dict):
            raise _invalid(relative_path, f"JSONL row {line_number} must be an object")
        rows.append(row)
    return tuple(rows)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_bbox(value: object, *, normalized: bool) -> bool:
    if not isinstance(value, list) or len(value) != 4:
        return False
    if normalized:
        if not all(_is_int(item) and 0 <= item <= 1000 for item in value):
            return False
    elif not all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
        return False
    x0, y0, x1, y1 = value
    return bool(x0 < x1 and y0 < y1)


def _validate_document(
    manifest: PaperManifest,
    document: dict[str, Any],
    rows: tuple[dict[str, Any], ...],
) -> None:
    document_path = manifest.normalized_document.relative_path
    blocks_path = manifest.normalized_blocks.relative_path
    if document.get("schema_version") != "paper_document.v1":
        raise _invalid(document_path, "unsupported normalized document schema")
    if document.get("paper_id") != manifest.paper_id:
        raise _invalid(document_path, "paper identity does not match manifest")
    if document.get("source_sha256") != manifest.source_pdf.sha256:
        raise _invalid(document_path, "source identity does not match manifest")

    artifacts = document.get("artifacts")
    if not isinstance(artifacts, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in artifacts.items()
    ):
        raise _invalid(document_path, "document artifact labels must be strings")
    pages = document.get("pages")
    counts = document.get("counts")
    if not isinstance(pages, list) or not isinstance(counts, dict):
        raise _invalid(document_path, "document pages/counts have invalid shape")
    if counts.get("blocks") != len(rows):
        raise _invalid(document_path, "declared block count does not match JSONL")

    ordered_evidence_ids: list[str] = []
    page_lengths: dict[int, int] = {}
    for expected_page_index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("page_idx") != expected_page_index:
            raise _invalid(document_path, "page indices must be ordered and contiguous")
        page_size = page.get("page_size")
        if not (
            isinstance(page_size, list)
            and len(page_size) == 2
            and all(
                isinstance(item, int | float) and not isinstance(item, bool) and item > 0
                for item in page_size
            )
        ):
            raise _invalid(document_path, "page size is invalid")
        block_ids = page.get("block_ids")
        if not isinstance(block_ids, list) or not all(isinstance(item, str) for item in block_ids):
            raise _invalid(document_path, "page block IDs have invalid shape")
        ordered_evidence_ids.extend(block_ids)
        page_lengths[expected_page_index] = len(block_ids)
    if len(ordered_evidence_ids) != len(set(ordered_evidence_ids)):
        raise _invalid(document_path, "document block IDs are not unique")
    if tuple(ordered_evidence_ids) != tuple(row.get("evidence_id") for row in rows):
        raise _invalid(blocks_path, "document block order does not match JSONL")

    native_indices: set[int] = set()
    asset_refs: list[str] = []
    page_orders: dict[int, list[int]] = {}
    image_count = 0
    for document_order, row in enumerate(rows):
        if row.get("paper_id") != manifest.paper_id:
            raise _invalid(blocks_path, "block paper identity does not match manifest")
        if row.get("source_sha256") != manifest.source_pdf.sha256:
            raise _invalid(blocks_path, "block source identity does not match manifest")
        if row.get("document_order") != document_order:
            raise _invalid(blocks_path, "document order must be contiguous")

        page_index_value = row.get("page_idx")
        reading_order_value = row.get("reading_order")
        if not _is_int(page_index_value):
            raise _invalid(blocks_path, "block page index is invalid")
        page_index = cast(int, page_index_value)
        if page_index not in page_lengths:
            raise _invalid(blocks_path, "block page index is invalid")
        if not _is_int(reading_order_value):
            raise _invalid(blocks_path, "block reading order is invalid")
        reading_order = cast(int, reading_order_value)
        if reading_order < 0:
            raise _invalid(blocks_path, "block reading order is invalid")
        page_orders.setdefault(page_index, []).append(reading_order)

        normalized_type = row.get("type")
        sub_type = row.get("sub_type")
        if normalized_type not in _NORMALIZED_TYPES or not (
            sub_type is None
            or (normalized_type in _SUBTYPED_TYPES and isinstance(sub_type, str) and sub_type)
        ):
            raise _invalid(blocks_path, "unknown normalized block type/subtype")
        if normalized_type in PICTURE_TYPES:
            image_count += 1
        if not isinstance(row.get("raw_text"), str):
            raise _invalid(blocks_path, "block raw text must be a string")
        section_path = row.get("section_path")
        if not (
            isinstance(section_path, list)
            and all(isinstance(item, str) and item for item in section_path)
            and len(section_path) == len(set(section_path))
        ):
            raise _invalid(blocks_path, "block section path is invalid")
        if not _valid_bbox(row.get("bbox_raw"), normalized=False) or not _valid_bbox(
            row.get("bbox_norm_1000"), normalized=True
        ):
            raise _invalid(blocks_path, "block bounding box is invalid")

        native_ref = row.get("mineru_native_ref")
        if not isinstance(native_ref, dict):
            raise _invalid(blocks_path, "native block reference is invalid")
        native_index_value = native_ref.get("native_index")
        if not _is_int(native_index_value):
            raise _invalid(blocks_path, "native block index/reference is invalid")
        native_index = cast(int, native_index_value)
        if (
            native_index < 0
            or native_index in native_indices
            or not isinstance(native_ref.get("source_file"), str)
            or not native_ref["source_file"]
        ):
            raise _invalid(blocks_path, "native block index/reference is invalid")
        native_indices.add(native_index)

        locator = row.get("source_locator")
        if not isinstance(locator, dict) or (
            locator.get("pdf_sha256") != manifest.source_pdf.sha256
            or locator.get("page_idx") != page_index
            or locator.get("bbox_norm_1000") != row.get("bbox_norm_1000")
        ):
            raise _invalid(blocks_path, "source locator does not match block provenance")

        captions = row.get("captions")
        if not isinstance(captions, list) or not all(isinstance(item, str) for item in captions):
            raise _invalid(blocks_path, "block captions have invalid shape")
        asset_ref = row.get("asset_ref")
        if normalized_type == "table" and asset_ref == "":
            # The row every consumer reads is this one: a table with no picture says
            # so as None here, once, rather than each consumer knowing the empty string.
            row["asset_ref"] = asset_ref = None
        if asset_ref is not None and (not isinstance(asset_ref, str) or not asset_ref):
            raise _invalid(blocks_path, "block asset reference is invalid")
        if normalized_type in PICTURE_TYPES:
            if asset_ref is None:
                raise _invalid(blocks_path, "visual block and asset reference disagree")
        elif normalized_type not in _VISUAL_TYPES and asset_ref is not None:
            raise _invalid(blocks_path, "visual block and asset reference disagree")
        if asset_ref is not None:
            asset_refs.append(asset_ref)

    if any(orders != list(range(page_lengths[page])) for page, orders in page_orders.items()):
        raise _invalid(blocks_path, "page-local reading order must be contiguous")
    if set(page_orders) != set(page_lengths):
        raise _invalid(blocks_path, "JSONL does not cover every declared page")
    if counts.get("images") != image_count:
        raise _invalid(document_path, "declared image count does not match JSONL")

    first_occurrences = tuple(dict.fromkeys(asset_refs))
    manifest_asset_paths = tuple(asset.relative_path for asset in manifest.assets)
    if len(first_occurrences) != len(asset_refs) or first_occurrences != manifest_asset_paths:
        raise _invalid(blocks_path, "asset references do not close over manifest assets in order")


def load_paper_package(package_dir: Path) -> LoadedPaperPackage:
    """Validate and load one normalized package without parsing PDF/native artifacts."""

    root = _resolve_package_root(package_dir)
    manifest = _load_manifest(root)
    references = (
        manifest.source_pdf,
        manifest.normalized_document,
        manifest.normalized_blocks,
        *manifest.assets,
    )
    artifact_paths = tuple(_artifact_path(root, artifact) for artifact in references)
    expected_files = {"manifest.json", *(artifact.relative_path for artifact in references)}
    actual_files = _inventory(root)
    if actual_files != expected_files:
        unexpected = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        detail = unexpected[0] if unexpected else missing[0]
        raise _invalid(detail, "package file inventory does not match manifest")
    for path, artifact in zip(artifact_paths, references, strict=True):
        _verify_artifact_bytes(path, artifact)

    document_path = root.joinpath(*PurePosixPath(manifest.normalized_document.relative_path).parts)
    blocks_path = root.joinpath(*PurePosixPath(manifest.normalized_blocks.relative_path).parts)
    document = _decode_json(document_path, manifest.normalized_document.relative_path)
    blocks = _decode_jsonl(blocks_path, manifest.normalized_blocks.relative_path)
    _validate_document(manifest, document, blocks)
    return LoadedPaperPackage(
        package_dir=root,
        manifest=manifest,
        document=document,
        blocks=blocks,
    )
