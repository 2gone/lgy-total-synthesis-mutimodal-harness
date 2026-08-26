from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator


Executor = Callable[..., Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def run_ocr(
    repo_root: Path | str,
    paper_id: str,
    *,
    executor: Executor = subprocess.run,
) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    result_dir = root / "results" / "ocr_ablation" / "ocr" / paper_id
    seal_path = result_dir / "seal.json"
    if seal_path.is_file():
        return json.loads(seal_path.read_text(encoding="utf-8"))

    manifest = json.loads(
        (root / "experiments/ocr_ablation/manifests/source_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    try:
        paper = next(item for item in manifest["papers"] if item["paper_id"] == paper_id)
    except StopIteration:
        raise ValueError(f"Unknown OCR paper: {paper_id}") from None
    pages = tuple(
        root / "experiments" / "ocr_ablation" / relative
        for relative in paper["hires_page_paths"]
    )
    missing = [path for path in pages if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(str(path) for path in missing))

    tools_root = root / "tools" / "ocr"
    script = tools_root / "run_ocr.mjs"
    attempt_output = result_dir / "attempts" / "001" / "ocr_output.json"
    command = ["node", script, paper_id, attempt_output, *pages]
    executor(command, cwd=tools_root, check=True)
    if not attempt_output.is_file():
        raise RuntimeError("OCR process completed without writing its declared output")

    parsed = json.loads(attempt_output.read_text(encoding="utf-8"))
    schema_path = root / "experiments/ocr_ablation/templates/ocr_output.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(parsed))
    if errors:
        raise ValueError("OCR output failed schema validation: " + errors[0].message)

    output_path = result_dir / "ocr_output.json"
    _atomic_write(output_path, _json_bytes(parsed))
    metadata = {
        "paper_id": paper_id,
        "schema_valid": True,
        "input_pages": [
            {
                "path": path.relative_to(root).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in pages
        ],
        "tool_files": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
            }
            for path in (
                script,
                tools_root / "package.json",
                tools_root / "package-lock.json",
                tools_root / "eng.traineddata",
            )
        ],
    }
    metadata_path = result_dir / "ocr_metadata.json"
    _atomic_write(metadata_path, _json_bytes(metadata))
    seal = {
        "schema_valid": True,
        "files": {
            "ocr_output.json": {
                "size_bytes": output_path.stat().st_size,
                "sha256": _sha256(output_path),
            },
            "ocr_metadata.json": {
                "size_bytes": metadata_path.stat().st_size,
                "sha256": _sha256(metadata_path),
            },
        },
    }
    _atomic_write(seal_path, _json_bytes(seal))
    return seal
