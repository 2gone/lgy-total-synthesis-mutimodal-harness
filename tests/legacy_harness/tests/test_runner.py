from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from harness import api
from harness.config import HarnessConfig
from harness.suites import Asset


def _runner_module():
    try:
        return importlib.import_module("harness.runner")
    except ModuleNotFoundError:
        pytest.fail("Controlled run execution is not implemented yet")


def _settings() -> HarnessConfig:
    return HarnessConfig.from_mapping(
        {
            "GEMINI_API_BASE": "https://gateway.example.invalid/v1",
            "GEMINI_API_KEY": "private-test-secret",
        }
    )


def _schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }


def _sse(content: str) -> bytes:
    event = {"choices": [{"delta": {"content": content}}]}
    return ("data: " + json.dumps(event, separators=(",", ":")) + "\n\ndata: [DONE]\n\n").encode(
        "utf-8"
    )


def test_payload_encodes_only_explicit_assets_and_attaches_exact_schema(tmp_path) -> None:
    """Catches missing multimodal data, undeclared content, or loss of structured-output constraints."""
    pdf = tmp_path / "paper.pdf"
    image = tmp_path / "route.png"
    ocr = tmp_path / "ocr_output.json"
    pdf.write_bytes(b"%PDF-test")
    image.write_bytes(b"\x89PNG-test")
    ocr.write_text('{"pages":[]}', encoding="utf-8")
    schema = _schema()

    payload = api.build_chat_payload(
        _settings(),
        "Extract evidence.",
        [Asset(pdf), Asset(image), Asset(ocr)],
        schema=schema,
    )

    content = payload["messages"][0]["content"]
    assert [item["type"] for item in content] == ["text", "file", "image_url", "text"]
    assert content[1]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "Persisted external OCR evidence" in content[3]["text"]
    assert payload["response_format"]["json_schema"]["schema"] == schema
    assert "source_manifest" not in json.dumps(payload)


def test_runner_retries_transport_and_unparseable_failures_but_seals_schema_invalid_output(
    tmp_path,
) -> None:
    """Catches retries that either stop too early or overwrite an observed schema failure."""
    runner = _runner_module()
    responses = [
        api.ApiResponse(raw=b'{"error":"gateway"}', status=503, wire_format="json", metadata={}),
        api.ApiResponse(raw=_sse("not json"), status=200, wire_format="sse", metadata={}),
        api.ApiResponse(
            raw=_sse('{"value":"observed"}'), status=200, wire_format="sse", metadata={}
        ),
    ]
    calls = 0

    def sender(settings, payload):
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    run_dir = tmp_path / "run"
    seal = runner.execute_observation(
        run_dir=run_dir,
        settings=_settings(),
        payload={"model": "gemini-3.7-flash-high"},
        schema=_schema(),
        sender=sender,
        max_attempts=3,
    )

    assert calls == 3
    assert seal["schema_valid"] is False
    assert sorted(path.name for path in (run_dir / "attempts").iterdir()) == ["001", "002", "003"]
    assert (run_dir / "attempts/001/raw_response.json").read_bytes() == b'{"error":"gateway"}'
    assert (run_dir / "attempts/002/raw_response.sse").read_bytes() == _sse("not json")


def test_runner_stops_and_reports_after_repeated_gateway_failures(tmp_path) -> None:
    """Catches unbounded retries when the configured gateway remains unavailable."""
    runner = _runner_module()
    calls = 0

    def sender(settings, payload):
        nonlocal calls
        calls += 1
        return api.ApiResponse(
            raw=b'{"error":"bad gateway"}',
            status=502,
            wire_format="json",
            metadata={"error": "Bad Gateway"},
        )

    with pytest.raises(runner.RunFailedError, match="3 attempts"):
        runner.execute_observation(
            run_dir=tmp_path / "run",
            settings=_settings(),
            payload={"model": "gemini-3.7-flash-high"},
            schema=_schema(),
            sender=sender,
            max_attempts=3,
        )

    assert calls == 3
    assert not (tmp_path / "run/seal.json").exists()


def test_run_cell_records_input_inventory_without_credentials(tmp_path) -> None:
    """Catches result metadata that cannot reproduce inputs or that leaks local credentials."""
    runner = _runner_module()
    manifest_path = tmp_path / "experiments/route_image_understanding/manifests/source_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "papers": {"paper_one": {"pdf": "papers/paper_one.pdf", "route_image_ids": ["r1"]}},
                "routes": [
                    {
                        "paper_id": "paper_one",
                        "route_image_id": "r1",
                        "raw_image": "route_images/raw/paper_one/r1.png",
                        "tokenized_image": "route_images/tokenized/paper_one/r1.png",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    image = tmp_path / "experiments/route_image_understanding/route_images/raw/paper_one/r1.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"route-image")
    worker = tmp_path / "experiments/route_image_understanding/prompts/worker_prompt.md"
    worker.parent.mkdir(parents=True)
    worker.write_text("paper=${PAPER_ID}; condition=${CONDITION}", encoding="utf-8")
    schema_path = (
        tmp_path
        / "experiments/route_image_understanding/templates/route_image_output.schema.json"
    )
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(json.dumps(_schema()), encoding="utf-8")

    def sender(settings, payload):
        return api.ApiResponse(
            raw=_sse('{"value":7}'), status=200, wire_format="sse", metadata={}
        )

    runner.run_cell(
        repo_root=tmp_path,
        suite="route_image_understanding",
        condition="B_raw_route_images",
        paper_id="paper_one",
        settings=_settings(),
        sender=sender,
    )

    metadata_path = (
        tmp_path
        / "results/route_image_understanding/B_raw_route_images/paper_one/run_metadata.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    published = json.dumps(metadata)
    assert metadata["suite"] == "route_image_understanding"
    assert metadata["condition"] == "B_raw_route_images"
    assert metadata["paper_id"] == "paper_one"
    assert metadata["input_inventory"][0]["path"].endswith("route_images/raw/paper_one/r1.png")
    assert "private-test-secret" not in published
    assert "gateway.example.invalid" not in published


def test_ocr_condition_c_requires_a_sealed_ocr_artifact_before_sender_is_used() -> None:
    """Catches Condition C calls that omit or use an unsealed OCR prerequisite."""
    runner = _runner_module()
    repo_root = Path(__file__).resolve().parents[1]
    calls = 0

    def sender(settings, payload):
        nonlocal calls
        calls += 1
        raise AssertionError("sender must not be called")

    with pytest.raises(runner.MissingPrerequisiteError, match="OCR artifact"):
        runner.run_cell(
            repo_root=repo_root,
            suite="ocr_ablation",
            condition="C_pdf_hires_ocr",
            paper_id="ineleganolide",
            settings=_settings(),
            sender=sender,
        )

    assert calls == 0
