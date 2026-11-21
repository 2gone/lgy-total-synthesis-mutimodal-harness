from __future__ import annotations

import time
from pathlib import Path

import pytest

from tsdaf.apps.web.upstream import (
    UpstreamParseManager,
    UpstreamSettings,
    _safe_upstream_url,
)


def test_upstream_url_requires_https_or_literal_loopback() -> None:
    assert _safe_upstream_url("https://parser.example.edu/v1/packages") is not None
    assert _safe_upstream_url("http://127.0.0.1:9000/parse") is not None
    assert _safe_upstream_url("http://localhost:9000/parse") is not None
    assert _safe_upstream_url("http://parser.example.edu/parse") is None
    assert _safe_upstream_url("https://user:pass@parser.example.edu/parse") is None
    assert _safe_upstream_url("https://parser.example.edu/parse?token=secret") is None


def test_settings_read_optional_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TSDAF_UPSTREAM_PARSE_URL", "https://parser.example.edu/parse")
    monkeypatch.setenv("TSDAF_UPSTREAM_PARSE_TOKEN", "  secret-token  ")

    settings = UpstreamSettings.from_environment()

    assert settings == UpstreamSettings(
        url="https://parser.example.edu/parse",
        bearer_token="secret-token",
    )


def test_background_job_materializes_relative_package_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-fixture")
    package_dir = tmp_path / "imported" / "paper"
    package_dir.mkdir(parents=True)

    def fake_request(self: object, source: Path, filename: str, target: Path) -> None:
        del self
        assert source == pdf_path
        assert filename == "paper.pdf"
        target.write_bytes(b"PK")

    def fake_extract(checkout: Path, archive: Path) -> Path:
        assert checkout == tmp_path
        assert archive.read_bytes() == b"PK"
        return package_dir

    monkeypatch.setattr(UpstreamParseManager, "_request_package", fake_request)
    manager = UpstreamParseManager(
        checkout=tmp_path,
        settings=UpstreamSettings(url="https://parser.example.edu/parse", bearer_token=None),
        extract_package=fake_extract,
    )

    created = manager.submit(pdf_path, "paper.pdf")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        job = manager.snapshot(str(created["job_id"]))
        assert job is not None
        if job["status"] == "SUCCEEDED":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("parse job did not finish")

    assert job["package_path"] == "imported/paper"
    assert job["progress"] == 100
