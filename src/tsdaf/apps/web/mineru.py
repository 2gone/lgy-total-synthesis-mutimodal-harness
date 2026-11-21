"""Local MinerU intake adapter and explicit MinerU-to-TS-DAF conversion boundary."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from tsdaf.lanes.document.package_loader import load_paper_package

_MAX_ARCHIVE_MEMBERS = 20_000
_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_MINERU_TIMEOUT_SECONDS = 2 * 60 * 60
_TIERS = frozenset({"flash", "basic", "standard", "advanced"})
_DIRECT_TYPES = frozenset(
    {"aside_text", "footer", "header", "page_footnote", "page_number", "ref_text", "text"}
)
_HEADING_TYPES = frozenset({"doc_title", "document_title", "paragraph_title", "section_heading"})
_ASSET_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class MinerUConversionError(ValueError):
    """MinerU output cannot be represented by the governed TS-DAF package contract."""


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _artifact(path: Path, relative_path: str, media_type: str) -> dict[str, object]:
    sha256, size = _sha256(path)
    return {
        "sha256": sha256,
        "media_type": media_type,
        "byte_size": size,
        "relative_path": relative_path,
    }


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise MinerUConversionError("MinerU ZIP contains an unsafe path")
    return path


def _extract_mineru_archive(archive_path: Path, output_dir: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise MinerUConversionError("MinerU ZIP contains too many files")
            if sum(member.file_size for member in members) > _MAX_EXPANDED_BYTES:
                raise MinerUConversionError("MinerU ZIP is too large after extraction")
            for member in members:
                relative = _safe_relative_path(member.filename)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise MinerUConversionError("MinerU ZIP cannot contain symbolic links")
                target = output_dir.joinpath(*relative.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
    except zipfile.BadZipFile as error:
        raise MinerUConversionError("MinerU result is not a valid ZIP") from error


def _text_content(value: object) -> str:
    parts: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            return
        if isinstance(item, Mapping):
            visit(item.get("content"))
            return
        if isinstance(item, Sequence) and not isinstance(item, bytes | bytearray):
            for child in item:
                visit(child)

    visit(value)
    return " ".join(parts)


def _caption_texts(block: Mapping[str, object]) -> list[str]:
    captions: list[str] = []
    content = block.get("content")
    if not isinstance(content, list):
        return captions
    for item in content:
        if not isinstance(item, Mapping):
            continue
        item_type = str(item.get("type", ""))
        if "caption" not in item_type:
            continue
        text = _text_content(item.get("content"))
        if text and text not in captions:
            captions.append(text)
    return captions


def _image_path(block: Mapping[str, object]) -> PurePosixPath | None:
    candidates: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, Mapping):
            value = item.get("image_path")
            if isinstance(value, str) and value:
                candidates.append(value)
            visit(item.get("content"))
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(block)
    if not candidates:
        return None
    return _safe_relative_path(candidates[0])


def _page_layout(middle: Mapping[str, object]) -> dict[int, tuple[float, float]]:
    extensions = middle.get("extensions")
    layout = extensions.get("docvortex_layout") if isinstance(extensions, Mapping) else None
    pages = layout.get("pages") if isinstance(layout, Mapping) else None
    if not isinstance(pages, list):
        raise MinerUConversionError("MinerU middle_json has no PDF page layout")
    result: dict[int, tuple[float, float]] = {}
    for page in pages:
        if not isinstance(page, Mapping):
            continue
        page_idx = page.get("page_idx")
        width = page.get("width_pt")
        height = page.get("height_pt")
        if (
            isinstance(page_idx, int)
            and not isinstance(page_idx, bool)
            and isinstance(width, int | float)
            and not isinstance(width, bool)
            and isinstance(height, int | float)
            and not isinstance(height, bool)
            and width > 0
            and height > 0
        ):
            result[page_idx] = (float(width), float(height))
    return result


def _bbox(
    block: Mapping[str, object], page_size: tuple[float, float]
) -> tuple[list[float], list[int]]:
    value = block.get("bbox")
    if not (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int | float) and not isinstance(item, bool) for item in value)
    ):
        raise MinerUConversionError("MinerU block has no valid bbox")
    x0, y0, x1, y1 = (float(item) for item in value)
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise MinerUConversionError("MinerU block bbox is outside the normalized page")
    width, height = page_size
    raw = [x0 * width, y0 * height, x1 * width, y1 * height]
    normalized = [
        max(0, min(999, math.floor(x0 * 1000))),
        max(0, min(999, math.floor(y0 * 1000))),
        max(1, min(1000, math.ceil(x1 * 1000))),
        max(1, min(1000, math.ceil(y1 * 1000))),
    ]
    if normalized[0] >= normalized[2] or normalized[1] >= normalized[3]:
        raise MinerUConversionError("MinerU block bbox collapses after normalization")
    return raw, normalized


def _paper_id(display_name: str, source_sha256: str) -> str:
    stem = Path(display_name).stem.casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", stem).strip("_") or "paper"
    return f"{slug[:48]}_{source_sha256[:12]}"


def _section_path(current: list[str], title: str, level: object) -> list[str]:
    depth = level if isinstance(level, int) and not isinstance(level, bool) and level > 0 else 1
    prefix = [item for item in current[: max(0, depth - 1)] if item != title]
    return [*prefix, title]


def _normalized_type(native_type: str, has_asset: bool) -> str:
    if native_type in _DIRECT_TYPES:
        return native_type
    if native_type == "image" and has_asset:
        return "image"
    if native_type in {"table", "chart"} and has_asset:
        return "table"
    return "text"


def convert_mineru_zip(
    *, pdf_path: Path, mineru_zip: Path, package_dir: Path, display_name: str
) -> dict[str, object]:
    """Convert MinerU 4.x ZIP output into one validated TS-DAF paper package."""

    package_dir = package_dir.resolve(strict=False)
    if package_dir.exists():
        raise MinerUConversionError("target package directory already exists")
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    source_sha256, _ = _sha256(pdf_path)
    paper_id = _paper_id(display_name, source_sha256)
    with tempfile.TemporaryDirectory(prefix="mineru-intake-", dir=package_dir.parent) as temp_name:
        temp_root = Path(temp_name)
        extracted = temp_root / "mineru"
        extracted.mkdir()
        _extract_mineru_archive(mineru_zip, extracted)
        middle_path = extracted / "middle_json.json"
        try:
            middle: object = json.loads(middle_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MinerUConversionError("MinerU ZIP has no valid middle_json.json") from error
        if not isinstance(middle, Mapping) or middle.get("schema") != "docvortex.middle":
            raise MinerUConversionError("unsupported MinerU middle_json schema")
        pages = middle.get("pages")
        if not isinstance(pages, list) or not pages:
            raise MinerUConversionError("MinerU middle_json has no pages")
        layouts = _page_layout(middle)

        staging = temp_root / "package"
        (staging / "source").mkdir(parents=True)
        (staging / "normalized").mkdir()
        (staging / "images").mkdir()
        shutil.copyfile(pdf_path, staging / "source" / "paper.pdf")

        rows: list[dict[str, object]] = []
        page_entries: list[dict[str, object]] = []
        assets: list[tuple[Path, str, str]] = []
        sections: list[str] = []
        document_order = 0
        image_count = 0
        producer = middle.get("metadata")
        producer = producer.get("producer") if isinstance(producer, Mapping) else None
        producer_version = producer.get("version") if isinstance(producer, Mapping) else "unknown"

        for expected_page_idx, page in enumerate(pages):
            if not isinstance(page, Mapping) or page.get("page_idx") != expected_page_idx:
                raise MinerUConversionError("MinerU page indices are not ordered and contiguous")
            page_size = layouts.get(expected_page_idx)
            if page_size is None:
                raise MinerUConversionError("MinerU page layout is incomplete")
            native_blocks = page.get("blocks")
            if not isinstance(native_blocks, list):
                raise MinerUConversionError("MinerU page blocks are invalid")
            block_ids: list[str] = []
            for reading_order, block in enumerate(native_blocks):
                if not isinstance(block, Mapping):
                    raise MinerUConversionError("MinerU block is invalid")
                native_type = str(block.get("type", "text"))
                captions = _caption_texts(block)
                asset_source = _image_path(block)
                normalized_type = _normalized_type(native_type, asset_source is not None)
                text = (
                    " ".join(captions)
                    if normalized_type in {"image", "table"}
                    else _text_content(block)
                )
                if native_type in _HEADING_TYPES and text:
                    sections = _section_path(sections, text, block.get("level"))
                raw_bbox, normalized_bbox = _bbox(block, page_size)
                asset_ref: str | None = None
                if normalized_type in {"image", "table"}:
                    assert asset_source is not None
                    source_asset = extracted.joinpath(*asset_source.parts)
                    if not source_asset.is_file():
                        raise MinerUConversionError(
                            f"MinerU image is missing: {asset_source.as_posix()}"
                        )
                    suffix = source_asset.suffix.lower()
                    media_type = _ASSET_MEDIA_TYPES.get(suffix)
                    if media_type is None:
                        raise MinerUConversionError(f"unsupported MinerU image type: {suffix}")
                    asset_sha256, _ = _sha256(source_asset)
                    asset_name = f"{asset_sha256}{suffix}"
                    if any(relative == f"images/{asset_name}" for _, relative, _ in assets):
                        asset_name = f"{asset_sha256}-{document_order:04d}{suffix}"
                    asset_ref = f"images/{asset_name}"
                    destination = staging / asset_ref
                    shutil.copyfile(source_asset, destination)
                    assets.append((destination, asset_ref, media_type))
                    if normalized_type == "image":
                        image_count += 1

                evidence_id = (
                    f"mineru-{source_sha256[:20]}:p{expected_page_idx:04d}:b{reading_order:04d}"
                )
                block_ids.append(evidence_id)
                rows.append(
                    {
                        "evidence_id": evidence_id,
                        "paper_id": paper_id,
                        "source_sha256": source_sha256,
                        "page_idx": expected_page_idx,
                        "reading_order": reading_order,
                        "document_order": document_order,
                        "type": normalized_type,
                        "sub_type": None,
                        "raw_text": text,
                        "section_path": list(sections),
                        "bbox_raw": raw_bbox,
                        "bbox_norm_1000": normalized_bbox,
                        "asset_ref": asset_ref,
                        "captions": captions,
                        "mineru_native_ref": {
                            "source_file": "middle_json.json",
                            "native_index": document_order,
                        },
                        "source_locator": {
                            "pdf_sha256": source_sha256,
                            "page_idx": expected_page_idx,
                            "bbox_norm_1000": normalized_bbox,
                        },
                    }
                )
                document_order += 1
            page_entries.append(
                {
                    "page_idx": expected_page_idx,
                    "page_size": [page_size[0], page_size[1]],
                    "block_ids": block_ids,
                }
            )

        document = {
            "schema_version": "paper_document.v1",
            "paper_id": paper_id,
            "source_sha256": source_sha256,
            "parse_run_id": f"mineru-{source_sha256[:20]}",
            "reported_backend": "mineru",
            "reported_version": str(producer_version),
            "artifacts": {"middle_json": "middle_json.json"},
            "pages": page_entries,
            "counts": {"blocks": len(rows), "images": image_count},
            "warnings": [],
        }
        (staging / "normalized" / "document.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / "normalized" / "blocks.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        manifest: dict[str, object] = {
            "schema_version": "tsdaf.paper-package.v1",
            "paper_id": paper_id,
            "display_name": Path(display_name).stem or paper_id,
            "source_pdf": _artifact(
                staging / "source" / "paper.pdf", "source/paper.pdf", "application/pdf"
            ),
            "normalized_document": _artifact(
                staging / "normalized" / "document.json",
                "normalized/document.json",
                "application/json",
            ),
            "normalized_blocks": _artifact(
                staging / "normalized" / "blocks.jsonl",
                "normalized/blocks.jsonl",
                "application/x-ndjson",
            ),
            "assets": [
                _artifact(path, relative, media_type) for path, relative, media_type in assets
            ],
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        load_paper_package(staging)
        os.replace(staging, package_dir)
        return manifest


@dataclass(frozen=True, slots=True)
class MinerUSettings:
    command: str
    tier: str = "standard"
    command_prefix: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls, checkout: Path | None = None) -> MinerUSettings | None:
        configured = os.environ.get("TSDAF_MINERU_COMMAND")
        command = (
            configured.strip() if configured and configured.strip() else shutil.which("mineru-kit")
        )
        command_prefix: tuple[str, ...] = ()
        if command is None and os.name == "nt" and checkout is not None:
            wsl = shutil.which("wsl.exe") or shutil.which("wsl")
            wsl_mineru = checkout / ".mineru" / "bin" / "mineru-kit"
            if wsl is not None and wsl_mineru.is_file():
                command = _wsl_path(wsl_mineru)
                command_prefix = (wsl, "--")
        if command is None:
            return None
        tier = os.environ.get("TSDAF_MINERU_TIER", "standard").strip().casefold()
        if tier not in _TIERS:
            raise ValueError("TSDAF_MINERU_TIER must be flash, basic, standard, or advanced")
        return cls(command=command, tier=tier, command_prefix=command_prefix)

    def invocation(self, *arguments: str) -> list[str]:
        return [*self.command_prefix, self.command, *arguments]

    def path_argument(self, path: Path) -> str:
        return _wsl_path(path) if self.command_prefix else str(path)


def _wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").casefold()
    if len(drive) != 1 or not drive.isalpha():
        raise ValueError("WSL MinerU auto-discovery requires a drive-letter path")
    relative = resolved.as_posix().split(":", 1)[1].lstrip("/")
    return f"/mnt/{drive}/{relative}"


JobStatus = Literal["QUEUED", "PARSING", "NORMALIZING", "SUCCEEDED", "FAILED"]


@dataclass(slots=True)
class MinerUJob:
    job_id: str
    filename: str
    status: JobStatus = "QUEUED"
    progress: int = 5
    message: str = "等待 MinerU 解析"
    package_path: str | None = None
    error_code: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "filename": self.filename,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "package_path": self.package_path,
            "error_code": self.error_code,
            "engine": "mineru-local",
        }


class MinerULocalParseManager:
    """Run official ``mineru-kit`` outside the governed TS-DAF route workflow."""

    def __init__(self, *, checkout: Path, settings: MinerUSettings) -> None:
        self._checkout = checkout
        self._settings = settings
        self._jobs: dict[str, MinerUJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tsdaf-mineru")

    def submit(self, pdf_path: Path, filename: str) -> dict[str, object]:
        job_id = f"parse_{uuid.uuid4().hex}"
        job = MinerUJob(job_id=job_id, filename=filename)
        with self._lock:
            self._jobs[job_id] = job
        self._executor.submit(self._run, job_id, pdf_path, filename)
        return job.public()

    def snapshot(self, job_id: str) -> dict[str, object] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else job.public()

    def _update(self, job_id: str, **changes: object) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for name, value in changes.items():
                setattr(job, name, value)

    def _fail(self, job_id: str, code: str, message: str) -> None:
        self._update(job_id, status="FAILED", progress=100, error_code=code, message=message)

    def _run(self, job_id: str, pdf_path: Path, filename: str) -> None:
        output_root = self._checkout / ".tsdaf-web" / "mineru-results"
        output_root.mkdir(parents=True, exist_ok=True)
        mineru_zip = output_root / f"{job_id}.zip"
        package_dir = self._checkout / ".tsdaf-web" / "imports" / f"pkg_{uuid.uuid4().hex}"
        try:
            self._update(
                job_id,
                status="PARSING",
                progress=20,
                message="MinerU 正在解析 PDF（阶段进度，完成后自动更新）",
            )
            subprocess.run(
                self._settings.invocation(
                    "parse",
                    self._settings.path_argument(pdf_path),
                    "--output",
                    self._settings.path_argument(mineru_zip),
                    "--format",
                    "zip",
                    "--tier",
                    self._settings.tier,
                ),
                cwd=self._checkout,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=_MINERU_TIMEOUT_SECONDS,
            )
            self._update(
                job_id,
                status="NORMALIZING",
                progress=78,
                message="正在转换为 TS-DAF 标准解析包",
            )
            convert_mineru_zip(
                pdf_path=pdf_path,
                mineru_zip=mineru_zip,
                package_dir=package_dir,
                display_name=filename,
            )
            relative = package_dir.relative_to(self._checkout).as_posix()
            self._update(
                job_id,
                status="SUCCEEDED",
                progress=100,
                message="MinerU 解析与标准化完成",
                package_path=relative,
            )
        except FileNotFoundError:
            self._fail(job_id, "MINERU_NOT_INSTALLED", "找不到 mineru-kit，请先安装 MinerU")
        except subprocess.TimeoutExpired:
            self._fail(job_id, "MINERU_TIMEOUT", "MinerU 解析超过两小时")
        except subprocess.CalledProcessError:
            self._fail(job_id, "MINERU_PARSE_FAILED", "MinerU 解析失败，请查看服务端日志")
        except MinerUConversionError as error:
            self._fail(job_id, "MINERU_RESULT_INVALID", str(error))
        except Exception:
            self._fail(job_id, "MINERU_INTERNAL_ERROR", "MinerU intake 发生未预期错误")
        finally:
            mineru_zip.unlink(missing_ok=True)
            pdf_path.unlink(missing_ok=True)
