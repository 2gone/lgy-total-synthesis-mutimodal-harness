from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.models import MoleculeCrop, OCSRStatus, PixelBox
from tsdaf.lanes.image.ocsr import MoleculeOCSR, OCSRResponseError


def _molecule() -> MoleculeCrop:
    payload = b"png-crop"
    return MoleculeCrop(
        molecule_id="RIMG001:M001",
        detection_index=1,
        class_name="mol",
        detector_score=0.9,
        detector_bbox_raw_xyxy=(10.0, 20.0, 30.0, 40.0),
        bbox_xyxy_px=PixelBox(x0=10, y0=20, x1=30, y1=40),
        crop_requested_xyxy_px=PixelBox(x0=6, y0=16, x1=34, y1=44),
        crop_sampled_xyxy_px=PixelBox(x0=6, y0=16, x1=34, y1=44),
        crop_width=28,
        crop_height=28,
        crop_artifact=ArtifactRef(
            sha256=hashlib.sha256(payload).hexdigest(),
            media_type="image/png",
            byte_size=len(payload),
            relative_path="objects/crop.png",
        ),
    )


def _ocsr() -> MoleculeOCSR:
    return MoleculeOCSR(Path(__file__).parents[3] / "prompts" / "molecule_ocsr" / "v1")


def test_ocsr_builds_exactly_one_crop_request() -> None:
    request = _ocsr().build_request(molecule=_molecule(), crop_bytes=b"png-crop")

    assert request.molecule_id == "RIMG001:M001"
    assert request.image_count == 1
    content = request.input[0]["content"]
    assert [part["type"] for part in content] == ["text", "image"]
    assert content[0]["text"].endswith("RIMG001:M001")
    assert content[1]["mime_type"] == "image/png"
    assert content[1]["data"] == "cG5nLWNyb3A="


def test_ocsr_parses_transcribed_and_no_smiles_results() -> None:
    ocsr = _ocsr()
    molecule = _molecule()

    transcribed = ocsr.parse_result(
        '{"readable":true,"smiles":"CCO","confidence":0.95,"is_abstract":false,"notes":""}',
        job_id="job-001",
        route_image_item_id="RIMG001",
        molecule=molecule,
    )
    no_smiles = ocsr.parse_result(
        "```json\n"
        '{"readable":false,"smiles":"","confidence":0,'
        '"is_abstract":false,"notes":"unreadable"}\n```',
        job_id="job-001",
        route_image_item_id="RIMG001",
        molecule=molecule,
    )

    assert transcribed.status is OCSRStatus.TRANSCRIBED
    assert transcribed.smiles == "CCO"
    assert no_smiles.status is OCSRStatus.NO_SMILES


def test_ocsr_rejects_malformed_provider_json() -> None:
    with pytest.raises(OCSRResponseError):
        _ocsr().parse_result(
            '{"readable":true,"smiles":[]}',
            job_id="job-001",
            route_image_item_id="RIMG001",
            molecule=_molecule(),
        )
