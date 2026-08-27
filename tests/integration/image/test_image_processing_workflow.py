from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from tsdaf.apps.image_composition import execute_image_workflow
from tsdaf.contracts.handoff import ImageProcessingJob, RouteImageItem
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.detector import RawMoleculeDetection
from tsdaf.lanes.image.models import DetectorProvenance


class _Detector:
    provenance = DetectorProvenance(
        detector_id="molyolo:yolov10",
        adapter_version="1.0.0",
        weights_sha256="1" * 64,
        runtime_bundle_sha256="2" * 64,
        ultralytics_version="8.1.34",
        image_size=1024,
        confidence_threshold=0.25,
        iou_threshold=0.5,
        device="cpu",
    )

    def detect(self, image_path: Path) -> tuple[RawMoleculeDetection, ...]:
        return (
            RawMoleculeDetection((2.0, 2.0, 15.0, 15.0), 0.9, 0, "mol"),
            RawMoleculeDetection((30.0, 2.0, 45.0, 15.0), 0.8, 0, "mol"),
        )


@dataclass(frozen=True)
class _Interaction:
    request_bytes: bytes
    response_bytes: bytes
    interaction_id: str
    output_text: str
    total_input_tokens: int = 10
    total_output_tokens: int = 5
    total_tokens: int = 15
    total_cached_tokens: int = 0
    total_thought_tokens: int = 0
    total_tool_use_tokens: int = 0
    request_id: str = "request-001"
    elapsed_ms: float = 12.5
    transport_attempts: int = 1


def _job_and_package(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / "package"
    image_path = package / "images" / "scheme.png"
    image_path.parent.mkdir(parents=True)
    buffer = BytesIO()
    Image.new("RGB", (60, 40), "white").save(buffer, format="PNG")
    payload = buffer.getvalue()
    image_path.write_bytes(payload)
    item = RouteImageItem(
        route_image_item_id="RIMG001",
        visual_group_id="MAIN-P0001-VG0001",
        source_asset=ArtifactRef(
            sha256=hashlib.sha256(payload).hexdigest(),
            media_type="image/png",
            byte_size=len(payload),
            relative_path="images/scheme.png",
        ),
        page_index=0,
        bbox=None,
        dimensions=(60, 40),
        figure_or_scheme_label="Scheme 1",
        caption=None,
        related_block_ids=("MAIN-P0001-B0001",),
        related_original_paragraphs=("Scheme 1.",),
        route_group_id="RG001",
        sequence_index=0,
        router_execution_id="exec_router",
    )
    receipt = b"receipt"
    job = ImageProcessingJob(
        job_id="job-001",
        route_image_item=item,
        release_receipt=ArtifactRef(
            sha256=hashlib.sha256(receipt).hexdigest(),
            media_type="application/json",
            byte_size=len(receipt),
            relative_path="receipts/release.json",
        ),
        requested_pipeline="YOLO_MOLDET_OCSR_V1",
    )
    job_path = tmp_path / "job.json"
    job_path.write_text(job.model_dump_json(), encoding="utf-8")
    return job_path, package


def test_application_composition_runs_job_to_sealed_ocsr_results(tmp_path: Path) -> None:
    job_path, package = _job_and_package(tmp_path)
    requests: list[dict[str, object]] = []

    def interact(**request: object) -> _Interaction:
        requests.append(request)
        output = '{"readable":true,"smiles":"CCO","confidence":0.9,"is_abstract":false,"notes":""}'
        return _Interaction(
            request_bytes=json.dumps(request, default=str).encode(),
            response_bytes=output.encode(),
            interaction_id=f"interaction-{len(requests)}",
            output_text=output,
        )

    code, payload = execute_image_workflow(
        job_path=job_path,
        source_package_dir=package,
        workspace=tmp_path / "workspace",
        execution_id="image-exec-001",
        detector=_Detector(),
        interaction_create=interact,
        provider_is_live=False,
        live=False,
        prompt_dir=Path(__file__).parents[3] / "prompts" / "molecule_ocsr" / "v1",
        workers=4,
        retries=2,
        timeout=120.0,
    )

    assert code == 0
    assert payload["status"] == "SUCCEEDED"
    assert payload["molecule_count"] == 2
    assert payload["ocsr_result_count"] == 2
    assert len(payload["ocsr_result_artifacts"]) == 2
    assert len(requests) == 2
