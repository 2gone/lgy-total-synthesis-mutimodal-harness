from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class AlreadySealedError(RuntimeError):
    pass


class UnparseableResponseError(ValueError):
    pass


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _next_attempt(run_dir: Path) -> Path:
    attempts_root = run_dir / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    numbers = [int(path.name) for path in attempts_root.iterdir() if path.is_dir() and path.name.isdigit()]
    return attempts_root / f"{(max(numbers, default=0) + 1):03d}"


def record_attempt(
    *,
    run_dir: Path | str,
    raw_response: bytes,
    metadata: dict[str, Any],
    wire_format: str,
) -> Path:
    destination = Path(run_dir)
    extension = "sse" if wire_format == "sse" else "json"
    attempt = _next_attempt(destination)
    _atomic_write(attempt / f"raw_response.{extension}", raw_response)
    _atomic_write(attempt / "attempt_metadata.json", _json_bytes(metadata))
    return attempt


def seal_observation(
    *,
    run_dir: Path | str,
    raw_response: bytes,
    response_text: str,
    schema: dict[str, Any],
    metadata: dict[str, Any],
    wire_format: str,
) -> dict[str, Any]:
    destination = Path(run_dir)
    if (destination / "seal.json").exists():
        raise AlreadySealedError(f"Observation is already sealed: {destination}")

    extension = "sse" if wire_format == "sse" else "json"
    attempt = record_attempt(
        run_dir=destination,
        raw_response=raw_response,
        metadata=metadata,
        wire_format=wire_format,
    )

    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError as exc:
        _atomic_write(
            attempt / "parse_error.json",
            _json_bytes({"error": str(exc), "response_text": response_text}),
        )
        raise UnparseableResponseError(str(exc)) from exc

    errors = [
        {"path": list(error.absolute_path), "message": error.message}
        for error in sorted(Draft202012Validator(schema).iter_errors(parsed), key=str)
    ]
    report = {"schema_valid": not errors, "errors": errors}
    files = {
        f"raw_response.{extension}": raw_response,
        "parsed_output.json": _json_bytes(parsed),
        "validation_report.json": _json_bytes(report),
        "run_metadata.json": _json_bytes(metadata),
    }
    for name, content in files.items():
        _atomic_write(destination / name, content)

    seal = {
        "schema_valid": report["schema_valid"],
        "wire_format": wire_format,
        "files": {
            name: {"sha256": _sha256(destination / name), "size_bytes": (destination / name).stat().st_size}
            for name in files
        },
    }
    _atomic_write(destination / "seal.json", _json_bytes(seal))
    return seal
