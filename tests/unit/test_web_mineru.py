from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from tsdaf.apps.web.mineru import (
    MinerUConversionError,
    MinerUSettings,
    convert_mineru_zip,
)
from tsdaf.lanes.document.package_loader import load_paper_package


def _write_mineru_zip(path: Path) -> None:
    middle = {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {
            "file_suffix": "pdf",
            "producer": {"name": "mineru", "version": "4.1.0"},
        },
        "extensions": {
            "mineru": {"tier": "standard", "parse_mode": "txt"},
            "docvortex_layout": {
                "version": 1,
                "pages": [{"page_idx": 0, "width_pt": 600.0, "height_pt": 800.0}],
            },
        },
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "paragraph_title",
                        "index": 0,
                        "level": 1,
                        "bbox": [0.1, 0.1, 0.8, 0.15],
                        "content": [{"type": "text", "content": "Results"}],
                    },
                    {
                        "type": "text",
                        "index": 1,
                        "bbox": [0.1, 0.2, 0.9, 0.3],
                        "content": [{"type": "text", "content": "Reaction narrative."}],
                    },
                    {
                        "type": "image",
                        "index": 2,
                        "bbox": [0.1, 0.35, 0.9, 0.8],
                        "content": [
                            {
                                "type": "image_body",
                                "index": 0,
                                "content": "",
                                "image_path": "images/scheme.png",
                            },
                            {
                                "type": "image_caption",
                                "content": [{"type": "text", "content": "Scheme 1"}],
                            },
                        ],
                    },
                ],
            }
        ],
        "is_full_document": True,
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("middle_json.json", json.dumps(middle))
        archive.writestr("structured_content.json", "{}")
        archive.writestr("markdown.md", "# Results")
        archive.writestr("images/scheme.png", b"\x89PNG\r\n\x1a\nfixture")


def test_settings_use_local_mineru_command_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TSDAF_MINERU_COMMAND", raising=False)
    monkeypatch.delenv("TSDAF_MINERU_TIER", raising=False)
    monkeypatch.setattr("tsdaf.apps.web.mineru.shutil.which", lambda _: "C:/tools/mineru-kit.exe")

    settings = MinerUSettings.from_environment()

    assert settings == MinerUSettings(command="C:/tools/mineru-kit.exe", tier="standard")


def test_settings_auto_discovers_checkout_wsl_mineru_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mineru = tmp_path / ".mineru" / "bin" / "mineru-kit"
    mineru.parent.mkdir(parents=True)
    mineru.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.delenv("TSDAF_MINERU_COMMAND", raising=False)
    monkeypatch.setattr("tsdaf.apps.web.mineru.os.name", "nt")
    monkeypatch.setattr(
        "tsdaf.apps.web.mineru.shutil.which",
        lambda name: "C:/Windows/System32/wsl.exe" if name == "wsl.exe" else None,
    )

    settings = MinerUSettings.from_environment(tmp_path)

    assert settings is not None
    assert settings.command.endswith("/.mineru/bin/mineru-kit")
    assert settings.command_prefix == ("C:/Windows/System32/wsl.exe", "--")
    assert settings.invocation("--version")[-2:] == [settings.command, "--version"]
    assert settings.path_argument(tmp_path / "paper.pdf").startswith("/mnt/")


def test_settings_reject_invalid_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TSDAF_MINERU_COMMAND", "mineru-kit")
    monkeypatch.setenv("TSDAF_MINERU_TIER", "ultra")

    with pytest.raises(ValueError, match="TSDAF_MINERU_TIER"):
        MinerUSettings.from_environment()


def test_mineru_zip_converts_to_valid_tsdaf_package(tmp_path: Path) -> None:
    pdf_path = tmp_path / "A paper.pdf"
    pdf_path.write_bytes(b"%PDF-fixture")
    mineru_zip = tmp_path / "mineru.zip"
    _write_mineru_zip(mineru_zip)
    package_dir = tmp_path / "package"

    result = convert_mineru_zip(
        pdf_path=pdf_path,
        mineru_zip=mineru_zip,
        package_dir=package_dir,
        display_name="A paper.pdf",
    )
    loaded = load_paper_package(package_dir)

    assert result["paper_id"].startswith("a_paper_")
    assert loaded.manifest.paper_id == result["paper_id"]
    assert [row["raw_text"] for row in loaded.blocks] == [
        "Results",
        "Reaction narrative.",
        "Scheme 1",
    ]
    assert loaded.blocks[1]["section_path"] == ["Results"]
    assert loaded.blocks[2]["asset_ref"].startswith("images/")
    assert loaded.blocks[2]["captions"] == ["Scheme 1"]


def test_mineru_conversion_refuses_missing_geometry(tmp_path: Path) -> None:
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-fixture")
    mineru_zip = tmp_path / "mineru.zip"
    middle = {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "metadata": {"producer": {"name": "mineru", "version": "4.1.0"}},
        "extensions": {
            "docvortex_layout": {"pages": [{"page_idx": 0, "width_pt": 600, "height_pt": 800}]}
        },
        "pages": [{"page_idx": 0, "blocks": [{"type": "text", "content": []}]}],
    }
    with zipfile.ZipFile(mineru_zip, "w") as archive:
        archive.writestr("middle_json.json", json.dumps(middle))

    with pytest.raises(MinerUConversionError, match="bbox"):
        convert_mineru_zip(
            pdf_path=pdf_path,
            mineru_zip=mineru_zip,
            package_dir=tmp_path / "package",
            display_name="paper.pdf",
        )
