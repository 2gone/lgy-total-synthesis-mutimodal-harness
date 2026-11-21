from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from tsdaf.apps.web import app as web_app
from tsdaf.apps.web.app import _extract_package_archive, _write_package_archive
from tsdaf.lanes.document.package_loader import load_paper_package


def test_download_archive_preserves_valid_package_root(tmp_path: Path) -> None:
    package = Path("tests/fixtures/paper_packages/principinol_B")
    loaded = load_paper_package(package)
    archive_path = tmp_path / "package.zip"

    _write_package_archive(package, archive_path)

    expected = {
        "manifest.json",
        loaded.manifest.source_pdf.relative_path,
        loaded.manifest.normalized_document.relative_path,
        loaded.manifest.normalized_blocks.relative_path,
        *(asset.relative_path for asset in loaded.manifest.assets),
    }
    with zipfile.ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == expected
        assert "manifest.json" in archive.namelist()


@pytest.mark.parametrize("kind", ["bad-zip", "missing-manifest"])
def test_failed_archive_import_removes_partial_directory(tmp_path: Path, kind: str) -> None:
    archive_path = tmp_path / "upload.zip"
    if kind == "bad-zip":
        archive_path.write_bytes(b"not a zip")
    else:
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("partial/file.txt", "incomplete")

    with pytest.raises(HTTPException):
        _extract_package_archive(tmp_path, archive_path)

    import_root = tmp_path / ".tsdaf-web" / "imports"
    assert not import_root.exists() or list(import_root.iterdir()) == []


def test_import_endpoint_removes_uploaded_archive_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_app, "_find_checkout", lambda: tmp_path)
    app = web_app.create_app()

    async def post_invalid_archive() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/packages/import",
                content=b"not a zip",
                headers={"content-type": "application/zip"},
            )

    response = asyncio.run(post_invalid_archive())

    assert response.status_code == 400
    upload_root = tmp_path / ".tsdaf-web" / "uploads"
    assert upload_root.is_dir()
    assert list(upload_root.iterdir()) == []
