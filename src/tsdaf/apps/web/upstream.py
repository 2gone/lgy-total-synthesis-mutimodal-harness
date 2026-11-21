from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

_MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
_TIMEOUT_SECONDS = 15 * 60


def _safe_upstream_url(value: str | None) -> str | None:
    if value is None or not value or value != value.strip():
        return None
    parsed = urlsplit(value)
    safe_scheme = parsed.scheme == "https" or (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    )
    if (
        not safe_scheme
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    return value


@dataclass(frozen=True, slots=True)
class UpstreamSettings:
    url: str
    bearer_token: str | None

    @classmethod
    def from_environment(cls) -> UpstreamSettings | None:
        url = _safe_upstream_url(os.environ.get("TSDAF_UPSTREAM_PARSE_URL"))
        if url is None:
            return None
        token = os.environ.get("TSDAF_UPSTREAM_PARSE_TOKEN")
        normalized_token = token.strip() if token and token.strip() else None
        return cls(url=url, bearer_token=normalized_token)


JobStatus = Literal["QUEUED", "PARSING", "SUCCEEDED", "FAILED"]


@dataclass(slots=True)
class ParseJob:
    job_id: str
    filename: str
    status: JobStatus = "QUEUED"
    progress: int = 5
    message: str = "等待上游解析"
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
        }


class UpstreamParseManager:
    def __init__(
        self,
        *,
        checkout: Path,
        settings: UpstreamSettings,
        extract_package: Callable[[Path, Path], Path],
    ) -> None:
        self._checkout = checkout
        self._settings = settings
        self._extract_package = extract_package
        self._jobs: dict[str, ParseJob] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tsdaf-upstream")

    def submit(self, pdf_path: Path, filename: str) -> dict[str, object]:
        job_id = f"parse_{uuid.uuid4().hex}"
        job = ParseJob(job_id=job_id, filename=filename)
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

    def _run(self, job_id: str, pdf_path: Path, filename: str) -> None:
        archive_path = pdf_path.with_suffix(".package.zip")
        try:
            self._update(
                job_id,
                status="PARSING",
                progress=30,
                message="上游服务正在解析 PDF",
            )
            self._request_package(pdf_path, filename, archive_path)
            self._update(job_id, progress=82, message="正在验证上游返回的标准解析包")
            package_dir = self._extract_package(self._checkout, archive_path)
            relative = package_dir.relative_to(self._checkout).as_posix()
            self._update(
                job_id,
                status="SUCCEEDED",
                progress=100,
                message="解析完成，标准解析包已就绪",
                package_path=relative,
            )
        except HTTPError as error:
            self._fail(job_id, "UPSTREAM_HTTP_ERROR", f"上游解析服务返回 HTTP {error.code}")
        except URLError:
            self._fail(job_id, "UPSTREAM_UNREACHABLE", "无法连接上游解析服务")
        except Exception as error:
            detail = getattr(error, "detail", None)
            message = detail if isinstance(detail, str) else "上游返回的解析包无效"
            self._fail(job_id, "UPSTREAM_RESULT_INVALID", message)
        finally:
            archive_path.unlink(missing_ok=True)

    def _fail(self, job_id: str, code: str, message: str) -> None:
        self._update(
            job_id,
            status="FAILED",
            progress=100,
            message=message,
            error_code=code,
        )

    def _request_package(self, pdf_path: Path, filename: str, archive_path: Path) -> None:
        headers = {
            "Accept": "application/zip",
            "Content-Type": "application/pdf",
            "X-Filename": quote(filename, safe="._-"),
        }
        if self._settings.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self._settings.bearer_token}"
        request = Request(
            self._settings.url,
            data=pdf_path.read_bytes(),
            headers=headers,
            method="POST",
        )
        total = 0
        response = urlopen(request, timeout=_TIMEOUT_SECONDS)
        with response, archive_path.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_PACKAGE_BYTES:
                    raise ValueError("upstream package is too large")
                output.write(chunk)
        with archive_path.open("rb") as result:
            if result.read(4)[:2] != b"PK":
                raise ValueError("upstream response is not a ZIP archive")
