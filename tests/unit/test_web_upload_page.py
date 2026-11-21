from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

STATIC = Path("src/tsdaf/apps/web/static")


class _UploadPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.file_inputs: list[dict[str, str | None]] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if identifier := attributes.get("id"):
            self.ids.add(identifier)
        if tag == "input" and attributes.get("type") == "file":
            self.file_inputs.append(attributes)


def test_upload_page_has_one_pdf_and_zip_intake() -> None:
    parser = _UploadPageParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))

    assert len(parser.file_inputs) == 1
    assert parser.file_inputs[0]["id"] == "document-input"
    assert ".pdf" in str(parser.file_inputs[0]["accept"])
    assert ".zip" in str(parser.file_inputs[0]["accept"])
    assert {"upload-landing", "task-workspace", "unified-drop-zone"} <= parser.ids
    assert "pdf-input" not in parser.ids
    assert "zip-input" not in parser.ids


def test_upload_script_dispatches_pdf_and_zip_to_separate_flows() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'name.endsWith(".pdf")' in script
    assert 'name.endsWith(".zip")' in script
    assert "if (isPdf) await uploadPdf(file);" in script
    assert "else await uploadZip(file);" in script
    assert 'showTaskWorkspace(file.name, isPdf ? "PDF · MINERU INTAKE"' in script
