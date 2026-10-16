"""Private, versioned values emitted by the image-processing lane."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, FiniteFloat, field_validator, model_validator

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string, _unique
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.image.coordinate_graph import FlattenBlocker, Resolvability

_MOLECULE_ID_RE = re.compile(r"(RIMG[0-9]{3}):M([0-9]{3})")


class PixelBox(ContractModel):
    """Half-open XYXY box in source-image pixels; requested crops may be negative."""

    x0: int
    y0: int
    x1: int
    y1: int

    @model_validator(mode="after")
    def _validate_area(self) -> Self:
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("pixel box must have positive half-open area")
        return self

    def as_xyxy(self) -> tuple[int, int, int, int]:
        return (self.x0, self.y0, self.x1, self.y1)


class DetectorProvenance(ContractModel):
    detector_id: str
    adapter_version: str
    weights_sha256: str
    runtime_bundle_sha256: str
    ultralytics_version: str
    image_size: int = Field(gt=0)
    confidence_threshold: FiniteFloat = Field(ge=0.0, le=1.0)
    iou_threshold: FiniteFloat = Field(ge=0.0, le=1.0)
    device: str

    @field_validator("detector_id", "adapter_version", "ultralytics_version", "device")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="detector provenance value")

    @field_validator("weights_sha256", "runtime_bundle_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="detector provenance hash")


class CropPolicy(ContractModel):
    policy_version: Literal["1.0.0"] = "1.0.0"
    horizontal_padding_fraction: FiniteFloat = Field(ge=0.0)
    vertical_padding_fraction: FiniteFloat = Field(ge=0.0)
    minimum_padding_pixels: int = Field(ge=0)
    background: Literal["white"] = "white"
    output_format: Literal["PNG"] = "PNG"
    color_mode: Literal["RGB"] = "RGB"


class MoleculeCrop(ContractModel):
    molecule_id: str
    detection_index: int = Field(ge=1)
    class_name: str
    detector_score: FiniteFloat = Field(ge=0.0, le=1.0)
    detector_bbox_raw_xyxy: tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]
    bbox_xyxy_px: PixelBox
    crop_requested_xyxy_px: PixelBox
    crop_sampled_xyxy_px: PixelBox
    crop_width: int = Field(gt=0)
    crop_height: int = Field(gt=0)
    crop_artifact: ArtifactRef

    @field_validator("molecule_id")
    @classmethod
    def _validate_molecule_id(cls, value: str) -> str:
        match = _MOLECULE_ID_RE.fullmatch(value)
        if match is None or int(match.group(2)) == 0:
            raise ValueError("invalid molecule crop ID")
        return value

    @field_validator("class_name")
    @classmethod
    def _validate_class_name(cls, value: str) -> str:
        return _stable_string(value, label="detector class name")

    @model_validator(mode="after")
    def _validate_crop(self) -> Self:
        if self.crop_artifact.media_type != "image/png":
            raise ValueError("molecule crop artifact must be image/png")
        requested = self.crop_requested_xyxy_px
        if self.crop_width != requested.x1 - requested.x0:
            raise ValueError("crop width does not match requested crop box")
        if self.crop_height != requested.y1 - requested.y0:
            raise ValueError("crop height does not match requested crop box")
        return self


class ImageQualityFlag(StrEnum):
    ZERO_DETECTIONS = "ZERO_DETECTIONS"


class MoleculeCropSet(ContractModel):
    schema_version: Literal["tsdaf.molecule-crop-set.v1"]
    job_id: str
    route_image_item_id: str
    source_image_artifact: ArtifactRef
    source_width: int = Field(gt=0)
    source_height: int = Field(gt=0)
    detector: DetectorProvenance
    crop_policy: CropPolicy
    raw_detection_count: int = Field(ge=0)
    molecules: tuple[MoleculeCrop, ...]
    review_image_artifact: ArtifactRef
    quality_flags: tuple[ImageQualityFlag, ...] = ()

    @field_validator("job_id")
    @classmethod
    def _validate_job_id(cls, value: str) -> str:
        return _stable_string(value, label="image job ID")

    @field_validator("route_image_item_id")
    @classmethod
    def _validate_route_image_item_id(cls, value: str) -> str:
        if re.fullmatch(r"RIMG[0-9]{3}", value) is None:
            raise ValueError("invalid route image item ID")
        return value

    @model_validator(mode="after")
    def _validate_set(self) -> Self:
        molecule_ids = tuple(molecule.molecule_id for molecule in self.molecules)
        _unique(molecule_ids, label="molecule IDs")
        expected = tuple(
            f"{self.route_image_item_id}:M{index:03d}"
            for index in range(1, len(self.molecules) + 1)
        )
        if molecule_ids != expected:
            raise ValueError("molecules are not in canonical M-ID order")
        if any(
            molecule.detection_index != index for index, molecule in enumerate(self.molecules, 1)
        ):
            raise ValueError("molecule detection indices are not canonical")
        has_zero_flag = ImageQualityFlag.ZERO_DETECTIONS in self.quality_flags
        if has_zero_flag != (len(self.molecules) == 0):
            raise ValueError("ZERO_DETECTIONS flag contradicts molecule count")
        if self.review_image_artifact.media_type != "image/png":
            raise ValueError("review image artifact must be image/png")
        return self


class OCSRStatus(StrEnum):
    TRANSCRIBED = "TRANSCRIBED"
    NO_SMILES = "NO_SMILES"


class GeminiOCSRResult(ContractModel):
    schema_version: Literal["tsdaf.gemini-ocsr-result.v1"]
    job_id: str
    route_image_item_id: str
    molecule_id: str
    crop_artifact: ArtifactRef
    status: OCSRStatus
    smiles: str
    is_abstract: bool

    @field_validator("job_id", "route_image_item_id", "molecule_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="OCSR identity")

    @model_validator(mode="after")
    def _validate_result(self) -> Self:
        if self.crop_artifact.media_type != "image/png":
            raise ValueError("OCSR crop artifact must be image/png")
        if not self.molecule_id.startswith(f"{self.route_image_item_id}:"):
            raise ValueError("OCSR molecule ID does not belong to route image item")
        if self.status is OCSRStatus.TRANSCRIBED:
            if not self.smiles or self.smiles != self.smiles.strip():
                raise ValueError("transcribed OCSR result requires canonical nonempty SMILES text")
        elif self.smiles != "":
            raise ValueError("NO_SMILES result must have an empty SMILES string")
        return self


class CoordinateOCSRStatus(StrEnum):
    TRANSCRIBED = "TRANSCRIBED"
    NO_STRUCTURE = "NO_STRUCTURE"


class OCSRServiceRun(ContractModel):
    """What the remote service reported about the run behind one result.

    Recorded for audit, not as stage identity: these values arrive in the response,
    so they cannot key a cache that must be consulted before the request is sent.
    `service_contract` is the pinned request-side constant and is the value that
    identifies what this result was produced against.
    """

    service_contract: str
    ocsr_model: str
    review_model: str
    expand_substituents: bool

    @field_validator("service_contract")
    @classmethod
    def _validate_contract(cls, value: str) -> str:
        return _stable_string(value, label="OCSR service contract")


class CoordinateOCSRResult(ContractModel):
    """One molecule transcribed by the coordinate-OCSR service.

    Replaces the flat SMILES record for the service branch. The structure is a
    coordinate graph rather than a string, so it is sealed as its own content-addressed
    artifact and referenced here; the record itself stays small and strictly typed.

    `resolvability` and `flatten_blockers` are carried on the record rather than
    recomputed by readers: whether this structure may become a single concrete molecule
    is a judgement about evidence, and it must be sealed with the evidence it judged
    rather than re-derived later against a possibly different rule set.
    """

    schema_version: Literal["tsdaf.coordinate-ocsr-result.v1"]
    job_id: str
    route_image_item_id: str
    molecule_id: str
    crop_artifact: ArtifactRef
    service_run: OCSRServiceRun
    status: CoordinateOCSRStatus
    graph_artifact: ArtifactRef | None
    external_mol_artifact: ArtifactRef | None
    resolvability: Resolvability | None
    flatten_blockers: tuple[FlattenBlocker, ...]
    decision_state: str
    errors: tuple[str, ...] = ()

    @field_validator("job_id", "route_image_item_id", "molecule_id")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="coordinate OCSR identity")

    @model_validator(mode="after")
    def _validate_result(self) -> Self:
        if self.crop_artifact.media_type != "image/png":
            raise ValueError("coordinate OCSR crop artifact must be image/png")
        if not self.molecule_id.startswith(f"{self.route_image_item_id}:"):
            raise ValueError("coordinate OCSR molecule ID does not belong to route image item")
        if self.status is CoordinateOCSRStatus.TRANSCRIBED:
            if self.graph_artifact is None or self.resolvability is None:
                raise ValueError("transcribed coordinate OCSR result requires a graph and verdict")
        else:
            # A missing structure is an explicit outcome, not a partially filled record:
            # nothing downstream may read a verdict that judged no evidence.
            if self.graph_artifact is not None or self.external_mol_artifact is not None:
                raise ValueError("NO_STRUCTURE result must carry no structure artifacts")
            if self.resolvability is not None or self.flatten_blockers:
                raise ValueError("NO_STRUCTURE result must carry no resolvability verdict")
        if self.graph_artifact is not None and not self.graph_artifact.media_type.endswith("+json"):
            raise ValueError("coordinate graph artifact must be a typed JSON media type")
        if (
            self.external_mol_artifact is not None
            and self.external_mol_artifact.media_type != "chemical/x-mdl-molfile"
        ):
            raise ValueError("external MOL artifact must be chemical/x-mdl-molfile")
        if self.resolvability is Resolvability.RESOLVED and self.flatten_blockers:
            raise ValueError("a RESOLVED verdict cannot carry flatten blockers")
        if self.resolvability is not None and self.resolvability is not Resolvability.RESOLVED:
            if not self.flatten_blockers:
                raise ValueError("a non-RESOLVED verdict must name at least one blocker")
        return self


__all__ = (
    "CoordinateOCSRResult",
    "CoordinateOCSRStatus",
    "CropPolicy",
    "DetectorProvenance",
    "GeminiOCSRResult",
    "ImageQualityFlag",
    "MoleculeCrop",
    "MoleculeCropSet",
    "OCSRServiceRun",
    "OCSRStatus",
    "PixelBox",
)
