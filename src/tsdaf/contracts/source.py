"""Immutable source identity and original-provenance contracts."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from ._base import ContractModel, _sha256, _stable_string


class ArtifactRef(ContractModel):
    sha256: str
    media_type: str
    byte_size: int = Field(ge=0)
    relative_path: str

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _sha256(value, label="artifact sha256")

    @field_validator("media_type")
    @classmethod
    def _validate_media_type(cls, value: str) -> str:
        return _stable_string(value, label="media type")

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        _stable_string(value, label="relative path")
        if value.startswith("/") or "\\" in value or re.match(r"^[A-Za-z]:", value):
            raise ValueError("relative path must be a canonical POSIX relative path")
        if any(part in {"", ".", ".."} for part in value.split("/")):
            raise ValueError("relative path contains a noncanonical segment")
        return value


class BoundingBox(ContractModel):
    x0: int = Field(ge=0, le=1000)
    y0: int = Field(ge=0, le=1000)
    x1: int = Field(ge=0, le=1000)
    y1: int = Field(ge=0, le=1000)
    coordinate_space: Literal["normalized_1000"]

    @model_validator(mode="after")
    def _validate_extents(self) -> Self:
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("bounding box must have positive area")
        return self


class SourceLocator(ContractModel):
    document_id: str
    page_index: int = Field(ge=0, le=9999)
    bbox: BoundingBox | None
    native_block_index: int | None = Field(ge=0)
    source_asset_ref: ArtifactRef | None

    @field_validator("document_id")
    @classmethod
    def _validate_document_id(cls, value: str) -> str:
        return _stable_string(value, label="document id")
