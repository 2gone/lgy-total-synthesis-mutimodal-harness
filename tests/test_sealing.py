from __future__ import annotations

import importlib
import json

import pytest

from harness import api


def _sealing_module():
    try:
        return importlib.import_module("harness.sealing")
    except ModuleNotFoundError:
        pytest.fail("Observation sealing is not implemented yet")


def _integer_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }


def test_sse_decoder_assembles_content_without_changing_wire_bytes() -> None:
    """Catches dropped stream chunks or mutation of the raw evidence representation."""
    raw = (
        b'data: {"choices":[{"delta":{"content":"{\\\"value\\\":"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":" 7}"}}],"usage":{"total_tokens":19}}\n\n'
        b"data: [DONE]\n\n"
    )

    decoded = api.decode_sse(raw)

    assert decoded.content == '{"value": 7}'
    assert decoded.usage == {"total_tokens": 19}
    assert raw.endswith(b"data: [DONE]\n\n")


def test_schema_invalid_output_is_preserved_and_sealed_as_observed(tmp_path) -> None:
    """Catches silent repair or deletion of a parseable but schema-invalid model response."""
    sealing = _sealing_module()
    run_dir = tmp_path / "results" / "condition" / "paper"
    raw = b'data: {"choices":[{"delta":{"content":"observed"}}]}\n\n'
    response_text = '{"value":"observed"}'

    seal = sealing.seal_observation(
        run_dir=run_dir,
        raw_response=raw,
        response_text=response_text,
        schema=_integer_schema(),
        metadata={"model": "gemini-3.7-flash-high"},
        wire_format="sse",
    )

    assert (run_dir / "raw_response.sse").read_bytes() == raw
    assert json.loads((run_dir / "parsed_output.json").read_text(encoding="utf-8")) == {
        "value": "observed"
    }
    report = json.loads((run_dir / "validation_report.json").read_text(encoding="utf-8"))
    assert report["schema_valid"] is False
    assert report["errors"]
    assert seal["schema_valid"] is False
    assert json.loads((run_dir / "seal.json").read_text(encoding="utf-8")) == seal


def test_existing_seal_prevents_overwrite_or_second_attempt(tmp_path) -> None:
    """Catches resume behavior that replaces a previously sealed observation."""
    sealing = _sealing_module()
    run_dir = tmp_path / "run"
    first = sealing.seal_observation(
        run_dir=run_dir,
        raw_response=b"first-wire-observation",
        response_text='{"value":7}',
        schema=_integer_schema(),
        metadata={"model": "gemini-3.7-flash-high"},
        wire_format="sse",
    )

    with pytest.raises(sealing.AlreadySealedError):
        sealing.seal_observation(
            run_dir=run_dir,
            raw_response=b"second-wire-observation",
            response_text='{"value":8}',
            schema=_integer_schema(),
            metadata={"model": "gemini-3.7-flash-high"},
            wire_format="sse",
        )

    assert (run_dir / "raw_response.sse").read_bytes() == b"first-wire-observation"
    assert json.loads((run_dir / "seal.json").read_text(encoding="utf-8")) == first
    assert len(list((run_dir / "attempts").iterdir())) == 1


def test_unparseable_response_keeps_attempt_evidence_without_creating_a_seal(tmp_path) -> None:
    """Catches loss of transport evidence when a retryable response cannot be parsed."""
    sealing = _sealing_module()
    run_dir = tmp_path / "run"

    with pytest.raises(sealing.UnparseableResponseError):
        sealing.seal_observation(
            run_dir=run_dir,
            raw_response=b"gateway bytes",
            response_text="not json",
            schema=_integer_schema(),
            metadata={"http_status": 200},
            wire_format="sse",
        )

    assert not (run_dir / "seal.json").exists()
    assert (run_dir / "attempts" / "001" / "raw_response.sse").read_bytes() == b"gateway bytes"
