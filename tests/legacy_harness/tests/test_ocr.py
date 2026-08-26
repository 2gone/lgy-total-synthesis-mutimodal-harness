from __future__ import annotations

import importlib
import json

import pytest


def _ocr_module():
    try:
        return importlib.import_module("harness.ocr")
    except ModuleNotFoundError:
        pytest.fail("External OCR orchestration is not implemented yet")


def _make_ocr_repo(root) -> None:
    pages = [f"hires_pages/paper_one/page-{number}.png" for number in range(1, 4)]
    manifest = {
        "papers": [
            {
                "paper_id": "paper_one",
                "pdf_path": "papers/paper_one.pdf",
                "page_count": 3,
                "hires_page_paths": pages,
            }
        ]
    }
    manifest_path = root / "experiments/ocr_ablation/manifests/source_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    for index, relative in enumerate(pages, start=1):
        page = root / "experiments/ocr_ablation" / relative
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_bytes(f"page-{index}".encode())
    schema_source = (
        root / "experiments/ocr_ablation/templates/ocr_output.schema.json"
    )
    schema_source.parent.mkdir(parents=True)
    schema_source.write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["paper_id", "engine", "pages"],
                "properties": {
                    "paper_id": {"type": "string"},
                    "engine": {"type": "object"},
                    "pages": {"type": "array"},
                },
            }
        ),
        encoding="utf-8",
    )
    tools = root / "tools/ocr"
    tools.mkdir(parents=True)
    for name in ("run_ocr.mjs", "package.json", "package-lock.json", "eng.traineddata"):
        (tools / name).write_bytes(name.encode())


def test_ocr_runner_uses_all_pages_in_manifest_order_and_seals_output(tmp_path) -> None:
    """Catches page omission/reordering or editable OCR output used by Condition C."""
    ocr = _ocr_module()
    _make_ocr_repo(tmp_path)
    commands = []

    def executor(command, *, cwd, check):
        commands.append((command, cwd, check))
        output_path = command[3]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "paper_id": "paper_one",
                    "engine": {"name": "Tesseract.js", "version": "7.0.0"},
                    "pages": [],
                }
            ),
            encoding="utf-8",
        )

    seal = ocr.run_ocr(tmp_path, "paper_one", executor=executor)

    command, cwd, check = commands[0]
    assert command[:3] == ["node", tmp_path / "tools/ocr/run_ocr.mjs", "paper_one"]
    assert [path.name for path in command[4:]] == ["page-1.png", "page-2.png", "page-3.png"]
    assert cwd == tmp_path / "tools/ocr"
    assert check is True
    output = tmp_path / "results/ocr_ablation/ocr/paper_one/ocr_output.json"
    assert json.loads(output.read_text(encoding="utf-8"))["paper_id"] == "paper_one"
    assert seal["schema_valid"] is True
    assert (output.parent / "seal.json").is_file()

    second = ocr.run_ocr(tmp_path, "paper_one", executor=lambda *args, **kwargs: pytest.fail())
    assert second == seal
    assert len(commands) == 1
