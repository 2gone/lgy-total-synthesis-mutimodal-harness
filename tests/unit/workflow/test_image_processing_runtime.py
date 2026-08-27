from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from tsdaf.contracts.handoff import ImageProcessingJob, RouteImageItem
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.detector import RawMoleculeDetection
from tsdaf.lanes.image.models import DetectorProvenance
from tsdaf.lanes.image.ocsr import MoleculeOCSR
from tsdaf.lanes.image.source_loader import LoadedSourceImage
from tsdaf.workflow.image_processing_runtime import ImageProcessingRun, run_image_processing
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner


class _Detector:
    def __init__(self) -> None:
        self.calls = 0
        self.provenance = DetectorProvenance(
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
        assert image_path.is_file()
        self.calls += 1
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


class _Interactions:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def create(self, **request: object) -> _Interaction:
        with self._lock:
            self.requests.append(request)
        content = request["input"][0]["content"]  # type: ignore[index]
        task = content[0]["text"]
        smiles = "CCO" if str(task).endswith("M001") else "CCN"
        output = json.dumps(
            {
                "readable": True,
                "smiles": smiles,
                "confidence": 0.9,
                "is_abstract": False,
                "notes": "",
            },
            separators=(",", ":"),
        )
        return _Interaction(
            request_bytes=json.dumps(request, default=str).encode(),
            response_bytes=output.encode(),
            interaction_id=f"interaction-{smiles}",
            output_text=output,
        )


def _source(tmp_path: Path, store: LocalObjectStore) -> LoadedSourceImage:
    buffer = BytesIO()
    Image.new("RGB", (60, 40), "white").save(buffer, format="PNG")
    payload = buffer.getvalue()
    source_path = tmp_path / "scheme.png"
    source_path.write_bytes(payload)
    source_ref = ArtifactRef(
        sha256=hashlib.sha256(payload).hexdigest(),
        media_type="image/png",
        byte_size=len(payload),
        relative_path="images/scheme.png",
    )
    item = RouteImageItem(
        route_image_item_id="RIMG001",
        visual_group_id="MAIN-P0001-VG0001",
        source_asset=source_ref,
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
    return LoadedSourceImage(
        job=job,
        job_artifact=store.put_json(job.model_dump(mode="json")),
        source_artifact=store.put_bytes(payload, media_type="image/png"),
        verified_local_path=source_path.resolve(),
        dimensions=(60, 40),
        detected_media_type="image/png",
    )


def test_runtime_runs_one_request_per_crop_and_reuses_stage_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    detector = _Detector()
    interactions = _Interactions()
    ocsr = MoleculeOCSR(Path(__file__).parents[3] / "prompts" / "molecule_ocsr" / "v1")
    source = _source(tmp_path, store)

    first = run_image_processing(
        store=store,
        runner=runner,
        execution_id="image-exec-001",
        source=source,
        detector=detector,
        ocsr=ocsr,
        interaction_create=interactions.create,
        provider_is_live=False,
        live=False,
        workers=4,
        retries=2,
        timeout=120.0,
    )
    second = run_image_processing(
        store=store,
        runner=runner,
        execution_id="image-exec-002",
        source=source,
        detector=detector,
        ocsr=ocsr,
        interaction_create=interactions.create,
        provider_is_live=False,
        live=False,
        workers=4,
        retries=2,
        timeout=120.0,
    )

    assert isinstance(first, ImageProcessingRun)
    assert isinstance(second, ImageProcessingRun)
    assert detector.calls == 1
    assert len(interactions.requests) == 2
    assert all(
        sum(part["type"] == "image" for part in request["input"][0]["content"]) == 1  # type: ignore[index]
        for request in interactions.requests
    )
    assert [result.smiles for result in first.ocsr_results] == ["CCO", "CCN"]
    assert all(stage.status.value == "CACHED" for stage in second.ocsr_stages)
