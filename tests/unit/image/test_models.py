from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.models import GeminiOCSRResult, OCSRStatus, PixelBox


def _crop_ref() -> ArtifactRef:
    payload = b"crop"
    return ArtifactRef(
        sha256=hashlib.sha256(payload).hexdigest(),
        media_type="image/png",
        byte_size=len(payload),
        relative_path="objects/crop.png",
    )


def test_pixel_box_requires_positive_half_open_area() -> None:
    assert PixelBox(x0=-2, y0=3, x1=5, y1=8).as_xyxy() == (-2, 3, 5, 8)

    with pytest.raises(ValidationError):
        PixelBox(x0=4, y0=3, x1=4, y1=8)


def test_ocsr_result_enforces_smiles_status_matrix() -> None:
    transcribed = GeminiOCSRResult(
        schema_version="tsdaf.gemini-ocsr-result.v1",
        job_id="image-job-001",
        route_image_item_id="RIMG001",
        molecule_id="RIMG001:M001",
        crop_artifact=_crop_ref(),
        status=OCSRStatus.TRANSCRIBED,
        smiles="CCO",
        is_abstract=False,
    )
    assert transcribed.smiles == "CCO"

    with pytest.raises(ValidationError):
        GeminiOCSRResult(
            schema_version="tsdaf.gemini-ocsr-result.v1",
            job_id="image-job-001",
            route_image_item_id="RIMG001",
            molecule_id="RIMG001:M001",
            crop_artifact=_crop_ref(),
            status=OCSRStatus.NO_SMILES,
            smiles="CCO",
            is_abstract=False,
        )


def test_no_smiles_is_a_valid_terminal_ocsr_result() -> None:
    result = GeminiOCSRResult(
        schema_version="tsdaf.gemini-ocsr-result.v1",
        job_id="image-job-001",
        route_image_item_id="RIMG001",
        molecule_id="RIMG001:M001",
        crop_artifact=_crop_ref(),
        status=OCSRStatus.NO_SMILES,
        smiles="",
        is_abstract=False,
    )

    assert result.status is OCSRStatus.NO_SMILES
