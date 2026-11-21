from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from tsdaf.apps.web.chemistry import RecordedChemistryStore, chemistry_summary
from tsdaf.apps.web.mineru import MinerULocalParseManager, MinerUSettings
from tsdaf.apps.web.research_chemistry import (
    ResearchChemistryManager,
    ResearchChemistrySettings,
)
from tsdaf.apps.web.upstream import UpstreamParseManager, UpstreamSettings
from tsdaf.lanes.document.package_loader import PackageLoadError, load_paper_package

_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 20_000
_ROUTE_MODEL_CANDIDATES = ("Gemini-4-argon", "Gemini-3.8-flash")


class PackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_path: str = Field(min_length=1, max_length=4096)


class RouteRequest(PackageRequest):
    live: bool = False


class ChemistryRequest(PackageRequest):
    live: bool = False


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    live: bool = False


def _find_checkout() -> Path:
    candidates = (Path.cwd(), Path(__file__).resolve())
    for candidate in candidates:
        for parent in (candidate, *candidate.parents):
            if (parent / "pyproject.toml").is_file() and (
                parent / "prompts" / "article_router" / "v1"
            ).is_dir():
                return parent
    raise RuntimeError("TS-DAF source checkout could not be located")


def _json_from_process(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    output = result.stdout.strip() or result.stderr.strip()
    try:
        payload: object = json.loads(output)
    except json.JSONDecodeError:
        payload = {
            "code": "WEB_COMMAND_OUTPUT_INVALID",
            "message": "The workflow command did not return its documented JSON result.",
        }
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "payload": payload,
    }


def _run_cli(checkout: Path, *arguments: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "tsdaf", *arguments],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    return _json_from_process(result)


def _resolve_package(checkout: Path, raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = checkout / candidate
    resolved = candidate.resolve(strict=False)
    if not resolved.is_dir():
        raise HTTPException(status_code=404, detail="找不到这个标准解析包目录。")
    return resolved


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(status_code=400, detail="压缩包包含不安全的路径。")
    return path


def _extract_package_archive(checkout: Path, archive_path: Path) -> Path:
    import_root = checkout / ".tsdaf-web" / "imports" / f"pkg_{uuid.uuid4().hex}"
    import_root.mkdir(parents=True, exist_ok=False)
    keep_import = False
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise HTTPException(status_code=413, detail="压缩包文件数量过多。")
            expanded = sum(member.file_size for member in members)
            if expanded > _MAX_EXPANDED_BYTES:
                raise HTTPException(status_code=413, detail="压缩包展开后过大。")
            for member in members:
                relative = _safe_member_path(member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise HTTPException(status_code=400, detail="压缩包不能包含符号链接。")
                target = import_root.joinpath(*relative.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("xb") as destination:
                    while chunk := source.read(1024 * 1024):
                        destination.write(chunk)
        manifests = list(import_root.rglob("manifest.json"))
        if len(manifests) != 1:
            raise HTTPException(
                status_code=400,
                detail="标准解析包必须且只能包含一个 manifest.json。",
            )
        package_dir = manifests[0].parent
        keep_import = True
        return package_dir
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=400, detail="上传的文件不是有效 ZIP。") from error
    finally:
        if not keep_import:
            shutil.rmtree(import_root, ignore_errors=True)


def _write_package_archive(package_dir: Path, archive_path: Path) -> str:
    loaded = load_paper_package(package_dir)
    references = (
        loaded.manifest.source_pdf,
        loaded.manifest.normalized_document,
        loaded.manifest.normalized_blocks,
        *loaded.manifest.assets,
    )
    with zipfile.ZipFile(
        archive_path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        allowZip64=True,
    ) as archive:
        archive.write(loaded.package_dir / "manifest.json", "manifest.json")
        for artifact in references:
            archive.write(
                loaded.package_dir.joinpath(*PurePosixPath(artifact.relative_path).parts),
                artifact.relative_path,
            )
    return loaded.manifest.paper_id


def _remove_download(path: Path) -> None:
    path.unlink(missing_ok=True)


def _route_mode_from_environment() -> str:
    value = os.environ.get("TSDAF_WEB_ROUTE_MODE", "contract-mock").strip().casefold()
    if value not in {"contract-mock", "cli"}:
        raise ValueError("TSDAF_WEB_ROUTE_MODE must be contract-mock or cli")
    return value


def _mock_route_response(paper_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "exit_code": 0,
        "payload": {
            "schema_version": "tsdaf.web-route-interface.v1",
            "paper_id": paper_id,
            "status": "INTERFACE_READY",
            "code": "MOCK_ONLY",
            "mode": "contract-mock",
            "provider_protocol": "openai-compatible-chat-completions",
            "model_policy": "backend-selected",
            "model_candidates": list(_ROUTE_MODEL_CANDIDATES),
            "real_model_called": False,
            "credentials_required": False,
            "message": "接口字段与状态展示已对齐；本次未调用真实模型。",
        },
    }


def create_app() -> FastAPI:
    checkout = _find_checkout()
    static_root = Path(__file__).with_name("static")
    mineru_settings = MinerUSettings.from_environment(checkout)
    upstream_settings = UpstreamSettings.from_environment()
    route_mode = _route_mode_from_environment()
    chemistry_store = RecordedChemistryStore(checkout)
    research_chemistry = ResearchChemistryManager(
        checkout,
        ResearchChemistrySettings.from_checkout(checkout),
    )
    remote_upstream = (
        None
        if upstream_settings is None
        else UpstreamParseManager(
            checkout=checkout,
            settings=upstream_settings,
            extract_package=_extract_package_archive,
        )
    )
    local_mineru = (
        None
        if mineru_settings is None
        else MinerULocalParseManager(checkout=checkout, settings=mineru_settings)
    )
    parser = local_mineru or remote_upstream
    parser_backend = (
        "mineru-wsl"
        if local_mineru is not None and mineru_settings and mineru_settings.command_prefix
        else "mineru-local"
        if local_mineru is not None
        else "configured-upstream"
        if parser
        else None
    )
    app = FastAPI(title="TS-DAF 工作台", version="0.1.0")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "mode": "local",
            "version": "0.1.0",
            "upstream_configured": parser is not None,
            "parse_backend": parser_backend,
            "mineru_tier": mineru_settings.tier if mineru_settings is not None else None,
            "route_mode": route_mode,
            "route_model_candidates": list(_ROUTE_MODEL_CANDIDATES),
            "chemistry_mode": "recorded-and-research-pipeline",
            "chemistry_recorded_papers": chemistry_store.available_papers(),
            "chemistry_research_available": research_chemistry.available,
            "chemistry_provider_configured": research_chemistry.provider_configured,
            "chemistry_missing_modules": list(research_chemistry.missing_modules),
            "chemistry_model": research_chemistry.model,
        }

    @app.get("/api/fixtures")
    def fixtures() -> dict[str, object]:
        fixture_root = checkout / "tests" / "fixtures" / "paper_packages"
        items = []
        if fixture_root.is_dir():
            for package in sorted(path for path in fixture_root.iterdir() if path.is_dir()):
                items.append(
                    {
                        "name": package.name,
                        "path": package.relative_to(checkout).as_posix(),
                    }
                )
        return {"items": items}

    @app.post("/api/packages/import")
    async def import_package(request: Request) -> dict[str, object]:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _MAX_ARCHIVE_BYTES:
            raise HTTPException(status_code=413, detail="ZIP 不能超过 256 MB。")
        body = await request.body()
        if len(body) > _MAX_ARCHIVE_BYTES:
            raise HTTPException(status_code=413, detail="ZIP 不能超过 256 MB。")
        upload_root = checkout / ".tsdaf-web" / "uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        archive_path = upload_root / f"upload_{uuid.uuid4().hex}.zip"
        archive_path.write_bytes(body)
        try:
            package_dir = _extract_package_archive(checkout, archive_path)
        finally:
            archive_path.unlink(missing_ok=True)
        return {
            "package_path": package_dir.relative_to(checkout).as_posix(),
            "display_name": package_dir.name,
        }

    @app.post("/api/parsing/jobs")
    async def create_parse_job(request: Request) -> dict[str, object]:
        if parser is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "当前环境找不到 mineru-kit，也没有配置 TSDAF_UPSTREAM_PARSE_URL。"
                    "请先安装 MinerU。"
                ),
            )
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _MAX_ARCHIVE_BYTES:
            raise HTTPException(status_code=413, detail="PDF 不能超过 256 MB。")
        filename = Path(unquote(request.headers.get("x-filename", "paper.pdf"))).name
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="只能上传 PDF 文件。")
        upload_root = checkout / ".tsdaf-web" / "pdf-uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        pdf_path = upload_root / f"pdf_{uuid.uuid4().hex}.pdf"
        total = 0
        prefix = b""
        try:
            with pdf_path.open("xb") as output:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > _MAX_ARCHIVE_BYTES:
                        raise HTTPException(status_code=413, detail="PDF 不能超过 256 MB。")
                    if len(prefix) < 5:
                        prefix += chunk[: 5 - len(prefix)]
                    output.write(chunk)
            if total == 0 or prefix != b"%PDF-":
                raise HTTPException(status_code=400, detail="上传内容不是有效 PDF。")
        except Exception:
            pdf_path.unlink(missing_ok=True)
            raise
        return parser.submit(pdf_path, filename)

    @app.get("/api/parsing/jobs/{job_id}")
    def inspect_parse_job(job_id: str) -> dict[str, object]:
        if parser is None:
            raise HTTPException(status_code=503, detail="MinerU intake 尚未可用。")
        job = parser.snapshot(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到这个解析任务。")
        return job

    @app.post("/api/packages/validate")
    def validate_package(payload: PackageRequest) -> dict[str, Any]:
        package = _resolve_package(checkout, payload.package_path)
        return _run_cli(checkout, "package", "validate", str(package))

    @app.post("/api/packages/download")
    def download_package(payload: PackageRequest) -> FileResponse:
        package = _resolve_package(checkout, payload.package_path)
        download_root = checkout / ".tsdaf-web" / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(suffix=".zip", dir=download_root)
        os.close(descriptor)
        archive_path = Path(temporary_name)
        archive_path.unlink(missing_ok=True)
        try:
            paper_id = _write_package_archive(package, archive_path)
        except PackageLoadError as error:
            archive_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"标准解析包下载前校验失败：{error.code.value}。",
            ) from error
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{paper_id}_tsdaf_package.zip",
            background=BackgroundTask(_remove_download, archive_path),
        )

    @app.post("/api/routes/run")
    def run_route(payload: RouteRequest) -> dict[str, Any]:
        package = _resolve_package(checkout, payload.package_path)
        if route_mode == "contract-mock":
            try:
                loaded = load_paper_package(package)
            except PackageLoadError as error:
                raise HTTPException(
                    status_code=400,
                    detail=f"标准解析包路由前校验失败：{error.code.value}。",
                ) from error
            return _mock_route_response(loaded.manifest.paper_id)
        arguments = ["route", "run", str(package)]
        if payload.live:
            arguments.append("--live")
        return _run_cli(checkout, *arguments)

    @app.post("/api/routes/{execution_id}/resume")
    def resume_route(execution_id: str, payload: ResumeRequest) -> dict[str, Any]:
        if route_mode == "contract-mock":
            raise HTTPException(status_code=409, detail="接口模拟模式没有可恢复的真实执行。")
        arguments = ["route", "resume", execution_id]
        if payload.live:
            arguments.append("--live")
        return _run_cli(checkout, *arguments)

    @app.post("/api/chemistry/run")
    def run_chemistry(payload: ChemistryRequest) -> dict[str, Any]:
        package = _resolve_package(checkout, payload.package_path)
        try:
            loaded = load_paper_package(package)
        except PackageLoadError as error:
            raise HTTPException(
                status_code=400,
                detail=f"标准解析包化学解析前校验失败：{error.code.value}。",
            ) from error
        result = chemistry_store.get_for_package(
            loaded.manifest.paper_id,
            loaded.manifest.source_pdf.sha256,
        )
        if result is not None:
            structure_binding = chemistry_store.get_structure_binding(result.paper_id)
            return {
                "status": "SUCCEEDED",
                "mode": "recorded-interface",
                "message": (
                    "已根据原始 PDF 指纹匹配仓库内真实历史运行结果；本次没有调用模型。"
                    if result.paper_id != loaded.manifest.paper_id
                    else "已读取仓库内真实历史运行结果；本次没有调用模型。"
                ),
                "summary": chemistry_summary(
                    result,
                    structure_binding,
                    chemistry_store.get_figure_dimensions(result.paper_id),
                ),
                "result": result.payload,
            }
        return research_chemistry.start(loaded, live=payload.live)

    @app.get("/api/chemistry/jobs/{job_id}")
    def inspect_chemistry_job(job_id: str) -> dict[str, Any]:
        job = research_chemistry.snapshot(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到这个化学解析任务。")
        return job

    @app.get("/api/chemistry/jobs/{job_id}/result.json")
    def download_research_chemistry_json(job_id: str) -> FileResponse:
        result_path = research_chemistry.result_path(job_id)
        if result_path is None or not result_path.is_file():
            raise HTTPException(status_code=404, detail="这个化学解析任务还没有结果 JSON。")
        return FileResponse(
            result_path,
            media_type="application/json",
            filename=f"{job_id}_research_result.json",
        )

    @app.get("/api/chemistry/jobs/{job_id}/result.zip")
    def download_research_chemistry_zip(job_id: str) -> FileResponse:
        download_root = checkout / ".tsdaf-web" / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(suffix=".zip", dir=download_root)
        os.close(descriptor)
        archive_path = Path(temporary_name)
        archive_path.unlink(missing_ok=True)
        paper_id = research_chemistry.write_result_archive(job_id, archive_path)
        if paper_id is None:
            archive_path.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="这个化学解析任务还没有完整结果包。")
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{paper_id}_research_chemistry_result.zip",
            background=BackgroundTask(_remove_download, archive_path),
        )

    @app.get("/api/chemistry/results/{paper_id}.json")
    def download_chemistry_json(paper_id: str) -> Response:
        result = chemistry_store.get(paper_id)
        if result is None:
            raise HTTPException(status_code=404, detail="找不到这篇论文的录制化学解析结果。")
        return Response(
            content=result.raw,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{paper_id}_machine-paper-result.json"'
                )
            },
        )

    @app.get("/api/chemistry/results/{paper_id}.zip")
    def download_chemistry_zip(paper_id: str) -> FileResponse:
        result = chemistry_store.get(paper_id)
        if result is None:
            raise HTTPException(status_code=404, detail="找不到这篇论文的录制化学解析结果。")
        download_root = checkout / ".tsdaf-web" / "downloads"
        download_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(suffix=".zip", dir=download_root)
        os.close(descriptor)
        archive_path = Path(temporary_name)
        structure_binding = chemistry_store.get_structure_binding(paper_id)
        try:
            with zipfile.ZipFile(
                archive_path, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                archive.writestr("machine-paper-result.json", result.raw)
                if structure_binding is not None:
                    archive.writestr("structure-binding.json", structure_binding.raw)
                    crop_hashes = {
                        structure.get("crop_sha256")
                        for compound in structure_binding.payload.get("compounds") or []
                        if isinstance(compound, dict)
                        for structure in compound.get("structures") or []
                        if isinstance(structure, dict)
                        and isinstance(structure.get("crop_sha256"), str)
                    }
                    for crop_sha256 in sorted(crop_hashes):
                        crop = chemistry_store.get_structure_crop(paper_id, crop_sha256)
                        if crop is not None:
                            archive.writestr(f"structure-crops/{crop_sha256}.png", crop)
                archive.writestr(
                    "result-summary.json",
                    json.dumps(
                        chemistry_summary(
                            result,
                            structure_binding,
                            chemistry_store.get_figure_dimensions(result.paper_id),
                        ),
                        ensure_ascii=False,
                        indent=2,
                    ).encode("utf-8"),
                )
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{paper_id}_chemistry_result.zip",
            background=BackgroundTask(_remove_download, archive_path),
        )

    @app.get("/api/chemistry/results/{paper_id}/visuals/{crop_sha256}.png")
    def chemistry_structure_crop(paper_id: str, crop_sha256: str) -> Response:
        crop = chemistry_store.get_structure_crop(paper_id, crop_sha256)
        if crop is None:
            raise HTTPException(status_code=404, detail="找不到这个已绑定的结构裁剪图。")
        return Response(
            content=crop,
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{crop_sha256}"',
            },
        )

    @app.get("/api/chemistry/results/{paper_id}/figures/{asset_sha256}.png")
    def chemistry_figure_context(paper_id: str, asset_sha256: str) -> Response:
        figure = chemistry_store.get_figure_context(paper_id, asset_sha256)
        if figure is None:
            raise HTTPException(status_code=404, detail="找不到这个反应对应的原始 Figure。")
        return Response(
            content=figure,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/chemistry/results/{paper_id}/reactions/{reaction_block_id}.png")
    def chemistry_reaction_crop(paper_id: str, reaction_block_id: str) -> Response:
        crop = chemistry_store.get_reaction_crop(paper_id, reaction_block_id)
        if crop is None:
            raise HTTPException(status_code=404, detail="找不到这个反应对应的原文图区域。")
        return Response(
            content=crop,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get(
        "/api/chemistry/results/{paper_id}/reactions/"
        "{reaction_block_id}/{visual_index}.png"
    )
    def chemistry_reaction_alternative_crop(
        paper_id: str,
        reaction_block_id: str,
        visual_index: int,
    ) -> Response:
        crop = chemistry_store.get_reaction_crop(
            paper_id,
            reaction_block_id,
            visual_index,
        )
        if crop is None:
            raise HTTPException(status_code=404, detail="找不到这个反应对应的原文图区域。")
        return Response(
            content=crop,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    @app.get("/api/runs")
    def list_runs() -> dict[str, object]:
        execution_root = checkout / ".tsdaf-workspace" / "executions"
        items = []
        if execution_root.is_dir():
            for path in sorted(
                execution_root.iterdir(),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            ):
                if path.is_dir() and path.name.startswith("exec_"):
                    items.append(
                        {
                            "execution_id": path.name,
                            "updated_at": path.stat().st_mtime,
                        }
                    )
        return {"items": items}

    @app.get("/api/runs/{execution_id}")
    def inspect_run(execution_id: str) -> dict[str, Any]:
        return _run_cli(checkout, "run", "inspect", execution_id)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(static_root / "favicon.svg", media_type="image/svg+xml")

    app.mount("/", StaticFiles(directory=static_root, html=True), name="static")
    return app


app = create_app()
