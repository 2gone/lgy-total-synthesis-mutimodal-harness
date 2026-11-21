from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from tsdaf.lanes.document.package_loader import LoadedPaperPackage

_RESEARCH_RELATIVE = Path("research/problem-profile-20261009")
_RUNNER_RELATIVE = _RESEARCH_RELATIVE / "runner/run_pipeline.py"
_REQUIRED_MODULES = ("openai", "fitz", "pdfplumber", "PIL", "jsonschema")
ResearchStatus = Literal[
    "LIVE_REQUIRED", "CONFIG_REQUIRED", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED"
]


@dataclass(frozen=True, slots=True)
class ResearchChemistrySettings:
    runner: Path
    model: str
    provider_configured: bool
    missing_modules: tuple[str, ...]

    @classmethod
    def from_checkout(cls, checkout: Path) -> ResearchChemistrySettings | None:
        runner = checkout / _RUNNER_RELATIVE
        if not runner.is_file():
            return None
        missing = tuple(
            name for name in _REQUIRED_MODULES if importlib.util.find_spec(name) is None
        )
        return cls(
            runner=runner,
            model=os.environ.get("TSDAF_CHEMISTRY_MODEL", "Gemini-3.8-flash").strip(),
            provider_configured=bool(
                os.environ.get("OPENAI_BASE_URL", "").strip()
                and os.environ.get("OPENAI_API_KEY", "").strip()
            ),
            missing_modules=missing,
        )


@dataclass(slots=True)
class ResearchChemistryJob:
    job_id: str
    paper_id: str
    status: ResearchStatus
    progress: int
    stage: str
    message: str
    created_at: float
    result_path: Path | None = None
    result_dir: Path | None = None
    error_code: str | None = None

    def public(self, result: dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "paper_id": self.paper_id,
            "status": self.status,
            "progress": self.progress,
            "stage": self.stage,
            "message": self.message,
            "mode": "research-pipeline",
            "error_code": self.error_code,
            "pipeline": ["import", "front", "chain", "stats"],
            "real_model_called_now": self.status not in {"LIVE_REQUIRED", "CONFIG_REQUIRED"},
        }
        if result is not None:
            payload.update(
                summary=research_chemistry_summary(result, self.paper_id),
                result=result,
                downloads={
                    "json": f"/api/chemistry/jobs/{self.job_id}/result.json",
                    "zip": f"/api/chemistry/jobs/{self.job_id}/result.zip",
                },
            )
        return payload


class ResearchChemistryManager:
    """Adapt one standard package to the isolated, resumable research pipeline."""

    def __init__(
        self,
        checkout: Path,
        settings: ResearchChemistrySettings | None,
    ) -> None:
        self._checkout = checkout
        self._settings = settings
        self._jobs: dict[str, ResearchChemistryJob] = {}
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._settings is not None

    @property
    def provider_configured(self) -> bool:
        return bool(self._settings and self._settings.provider_configured)

    @property
    def missing_modules(self) -> tuple[str, ...]:
        return self._settings.missing_modules if self._settings else ()

    @property
    def model(self) -> str | None:
        return self._settings.model if self._settings else None

    def start(self, loaded: LoadedPaperPackage, *, live: bool) -> dict[str, Any]:
        paper_id = loaded.manifest.paper_id
        if self._settings is None:
            return _preflight_result(
                paper_id,
                "CONFIG_REQUIRED",
                "RESEARCH_PIPELINE_MISSING",
                "当前检出中没有 research 化学流水线。",
            )
        if self._settings.missing_modules:
            missing = "、".join(self._settings.missing_modules)
            return _preflight_result(
                paper_id,
                "CONFIG_REQUIRED",
                "RESEARCH_DEPENDENCIES_MISSING",
                f"research 流水线已接入，但缺少运行依赖：{missing}。",
            )
        if not live:
            return _preflight_result(
                paper_id,
                "LIVE_REQUIRED",
                "LIVE_REQUIRED",
                "research 化学流水线已接入并通过预检；打开“显式 Live”后才会调用模型。",
            )
        if not self._settings.provider_configured:
            return _preflight_result(
                paper_id,
                "CONFIG_REQUIRED",
                "PROVIDER_CONFIG_REQUIRED",
                "research 化学流水线已接入；服务端尚未配置 OPENAI_BASE_URL 和 OPENAI_API_KEY。",
            )

        job_id = f"chem_{uuid.uuid4().hex}"
        job = ResearchChemistryJob(
            job_id=job_id,
            paper_id=paper_id,
            status="QUEUED",
            progress=5,
            stage="queued",
            message="已创建 research 化学解析任务",
            created_at=time.time(),
        )
        with self._lock:
            self._jobs[job_id] = job
        threading.Thread(target=self._run, args=(job_id, loaded), daemon=True).start()
        return job.public()

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            result_path = job.result_path
            public = job.public()
        if result_path is not None and result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            public = job.public(result)
        return public

    def result_path(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else job.result_path

    def write_result_archive(self, job_id: str, target: Path) -> str | None:
        with self._lock:
            job = self._jobs.get(job_id)
            result_dir = None if job is None else job.result_dir
            paper_id = None if job is None else job.paper_id
        if result_dir is None or paper_id is None or not result_dir.is_dir():
            return None
        with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(result_dir.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(result_dir).as_posix())
        return paper_id

    def _update(self, job_id: str, **changes: object) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for name, value in changes.items():
                setattr(job, name, value)

    def _run(self, job_id: str, loaded: LoadedPaperPackage) -> None:
        assert self._settings is not None
        runtime_root = self._checkout / ".tsdaf-web" / "research-runtime"
        code_root = runtime_root / "checkout"
        job_root = self._checkout / ".tsdaf-web" / "research-jobs" / job_id
        run_name = job_id.removeprefix("chem_")[:12]
        paper_id = _safe_paper_id(loaded.manifest.paper_id)
        log_path = job_root / "pipeline.log"
        try:
            self._update(
                job_id,
                status="RUNNING",
                progress=10,
                stage="prepare",
                message="正在准备隔离的 research 运行目录",
            )
            _prepare_runtime_checkout(self._checkout, code_root)
            collection = prepare_single_package_collection(loaded, job_root / "input", paper_id)
            command = [
                sys.executable,
                str(code_root / _RUNNER_RELATIVE),
                "--packages",
                str(collection),
                "--name",
                run_name,
                "--model",
                self._settings.model,
                "--papers-at-once",
                "1",
                "--budget",
                "8",
                "--hard-cap",
                "8",
                "--max-parallel",
                "8",
            ]
            job_root.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    command,
                    cwd=code_root,
                    env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    stage = _pipeline_stage(line)
                    if stage is not None:
                        self._update(job_id, **stage)
                return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(f"research pipeline exited with code {return_code}")
            result_dir = (
                code_root / _RESEARCH_RELATIVE / "whole-paper" / f"chain-r{run_name}-{paper_id}"
            )
            result_path = result_dir / "result.json"
            if not result_path.is_file():
                raise RuntimeError("research pipeline finished without result.json")
            self._update(
                job_id,
                status="SUCCEEDED",
                progress=100,
                stage="complete",
                message="research 化学解析完成",
                result_path=result_path,
                result_dir=result_dir,
            )
        except Exception as error:
            self._update(
                job_id,
                status="FAILED",
                progress=100,
                stage="failed",
                error_code="RESEARCH_PIPELINE_FAILED",
                message=f"research 化学解析失败：{type(error).__name__}。请查看本地任务日志。",
            )


def prepare_single_package_collection(
    loaded: LoadedPaperPackage,
    collection: Path,
    paper_id: str | None = None,
) -> Path:
    """Create the INDEX/article/main layout expected by ``import_ready200.py``."""
    paper_id = _safe_paper_id(paper_id or loaded.manifest.paper_id)
    paper_root = collection / "packages" / paper_id
    main = paper_root / "main"
    if collection.exists():
        shutil.rmtree(collection)
    shutil.copytree(loaded.package_dir, main)
    document = json.loads(
        (loaded.package_dir / loaded.manifest.normalized_document.relative_path).read_text(
            encoding="utf-8"
        )
    )
    pages = document.get("pages") if isinstance(document, dict) else None
    page_count = len(pages) if isinstance(pages, list) else 0
    article = {
        "main_input_blocks_sha256": loaded.manifest.normalized_blocks.sha256,
        "main_block_type_mappings": [],
        "si": [],
    }
    (paper_root / "article.json").write_text(
        json.dumps(article, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (collection / "INDEX.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "position",
                "legacy_rank",
                "paper_id",
                "doi",
                "title",
                "year",
                "main_sha256",
                "main_pages",
                "package_path",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "position": 1,
                "legacy_rank": 1,
                "paper_id": paper_id,
                "doi": "",
                "title": loaded.manifest.display_name,
                "year": "",
                "main_sha256": loaded.manifest.source_pdf.sha256,
                "main_pages": page_count,
                "package_path": f"packages/{paper_id}",
            }
        )
    return collection


def research_chemistry_summary(result: dict[str, Any], paper_id: str) -> dict[str, Any]:
    paper_record = (
        result.get("paper_record") if isinstance(result.get("paper_record"), dict) else {}
    )
    compounds = paper_record.get("compounds") if isinstance(paper_record, dict) else []
    routes = paper_record.get("route") if isinstance(paper_record, dict) else []
    blocks = result.get("reaction_blocks")
    characterization = result.get("characterization_records")
    disagreements = result.get("author_disagreements")
    unresolved = result.get("unresolved")
    compounds = compounds if isinstance(compounds, list) else []
    routes = routes if isinstance(routes, list) else []
    blocks = blocks if isinstance(blocks, list) else []
    return {
        "paper_id": paper_id,
        "machine_result_id": result.get("label"),
        "schema_version": "tsdaf.research-corpus-result.2026-10",
        "coverage": "MAIN_OR_JOINED_EXPERIMENTAL",
        "completeness": "COMPLETE",
        "source": "research/problem-profile-20261009",
        "real_model_called_now": True,
        "counts": {
            "compounds": len(compounds),
            "reaction_blocks": len(blocks),
            "routes": len(routes),
            "characterization_records": len(characterization or []),
            "field_conflicts": len(disagreements or []),
            "unresolved_items": len(unresolved or []),
        },
        "routes": [
            {
                "route_id": route.get("route_id") or route.get("id") or f"route-{index + 1}",
                "overview": route.get("summary") or route.get("name") or str(route)[:600],
                "reaction_block_count": len(blocks),
                "compound_count": len(compounds),
                "target_count": 1,
            }
            for index, route in enumerate(routes)
            if isinstance(route, dict)
        ],
        "reaction_blocks": [
            {
                "reaction_block_id": _research_block_id(block, index),
                "source_order": index + 1,
                "summary": _research_block_summary(block),
                "input_compound_ids": [],
                "output_compound_ids": [],
                "operation_count": len(_research_block_record(block).get("steps") or []),
                "outcome_count": 1 if _research_block_record(block).get("outcome") else 0,
            }
            for index, block in enumerate(blocks)
            if isinstance(block, dict)
        ],
        "compounds": [
            {
                "compound_id": f"compound-{index + 1}",
                "labels": [str(compound["label"])] if compound.get("label") else [],
                "names": [str(compound["name"])] if compound.get("name") else [],
                "has_structure": False,
            }
            for index, compound in enumerate(compounds)
            if isinstance(compound, dict)
        ],
    }


def _preflight_result(
    paper_id: str,
    status: Literal["LIVE_REQUIRED", "CONFIG_REQUIRED"],
    error_code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "paper_id": paper_id,
        "status": status,
        "progress": 0,
        "stage": "preflight",
        "message": message,
        "mode": "research-pipeline",
        "error_code": error_code,
        "pipeline": ["import", "front", "chain", "stats"],
        "real_model_called_now": False,
    }


def _prepare_runtime_checkout(checkout: Path, code_root: Path) -> None:
    source = checkout / "research"
    target = code_root / "research"
    marker = code_root / ".source-revision"
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if (
        marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == revision
        and target.is_dir()
    ):
        return
    staging = code_root.with_name(f"{code_root.name}.staging-{uuid.uuid4().hex}")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(source, staging / "research")
    (staging / "tests" / "fixtures").mkdir(parents=True)
    (staging / ".source-revision").write_text(revision + "\n", encoding="utf-8")
    shutil.rmtree(code_root, ignore_errors=True)
    staging.replace(code_root)


def _pipeline_stage(line: str) -> dict[str, object] | None:
    stages = {
        "== 1 import": (20, "import", "正在导入标准解析包"),
        "== 2 front-end": (35, "front", "正在进行读图、页面归属与文本核定"),
        "== 3 chain": (60, "chain", "正在提取实验记录、论文级记录并绑定结构"),
        "== 4 stats": (92, "stats", "正在汇总调用、token 与耗时"),
    }
    for prefix, (progress, stage, message) in stages.items():
        if line.startswith(prefix):
            return {"progress": progress, "stage": stage, "message": message}
    return None


def _safe_paper_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe[:120] or f"paper_{uuid.uuid4().hex[:12]}"


def _research_block_summary(block: dict[str, Any]) -> str:
    record = block.get("record")
    if isinstance(record, dict):
        for key in ("summary", "transformation", "product", "title"):
            if record.get(key):
                return str(record[key])[:600]
    return f"实验条目 {block.get('entry', '—')}，块 {block.get('block_in_entry', 1)}"


def _research_block_id(block: dict[str, Any], index: int) -> str:
    entry = block.get("entry", index + 1)
    block_in_entry = block.get("block_in_entry", 1)
    return f"entry-{entry}-{block_in_entry}"


def _research_block_record(block: dict[str, Any]) -> dict[str, Any]:
    record = block.get("record")
    return record if isinstance(record, dict) else {}
