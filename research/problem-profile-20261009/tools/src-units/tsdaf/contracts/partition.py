"""Where a document's experimental part begins, decided once per package.

Whether a paper carries its experimental detail in the main text or in a supplement
depends on the journal, so the cut is a model's judgement, not a rule. The Host keeps
only the judgement it can check: the handle the model returned, which must name a
block the document cleaning kept. Everything either side of the cut is derived from
the package and that one handle (see `lanes.document.reading_units`), never
stored twice.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from ._base import ContractModel, _sha256, _stable_string
from .package import CanonicalPaperPackage

EXPERIMENTAL_PARTITION_MEDIA_TYPE = "application/vnd.tsdaf.experimental-partition+json"


class ExperimentalPartitionProvenance(ContractModel):
    stage_profile: str
    model_profile: str
    prompt_sha256: str
    schema_sha256: str

    @field_validator("stage_profile", "model_profile")
    @classmethod
    def _validate_identity(cls, value: str) -> str:
        return _stable_string(value, label="partition provenance identity")

    @field_validator("prompt_sha256", "schema_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="partition provenance hash")


class ExperimentalPartition(ContractModel):
    """One document's cut between its narrative and its experimental part."""

    schema_version: Literal["tsdaf.experimental-partition.v1"]
    paper_id: str
    source_phase: Literal["MAIN", "SI"]
    canonical_package_sha256: str
    # The first cleaned block of the experimental part, or None when the document has
    # no experimental part at all (a communication whose detail lives in its supplement).
    cut_block_id: str | None
    # What the model was shown, so a reviewer can reproduce the decision.
    skeleton_sha256: str
    # The model's one sentence; recorded for review, authoritative for nothing.
    rationale: str = Field(min_length=1)
    provenance: ExperimentalPartitionProvenance

    @field_validator("paper_id")
    @classmethod
    def _validate_paper_id(cls, value: str) -> str:
        return _stable_string(value, label="partition paper id")

    @field_validator("canonical_package_sha256", "skeleton_sha256")
    @classmethod
    def _validate_hash(cls, value: str) -> str:
        return _sha256(value, label="partition hash")

    @field_validator("cut_block_id")
    @classmethod
    def _validate_cut(cls, value: str | None) -> str | None:
        return None if value is None else _stable_string(value, label="partition cut block id")

    @model_validator(mode="after")
    def _validate_rationale(self) -> Self:
        if not self.rationale.strip():
            raise ValueError("partition rationale must not be blank")
        return self

    def validate_against(self, package: CanonicalPaperPackage) -> None:
        """Refuse a partition that names another package or a block cleaning dropped."""

        if type(package) is not CanonicalPaperPackage:
            raise TypeError("package must be a CanonicalPaperPackage")
        if (
            self.paper_id != package.manifest.paper_id
            or self.canonical_package_sha256 != package.canonical_content_sha256
        ):
            raise ValueError("experimental partition does not belong to this package")
        if (
            self.cut_block_id is not None
            and self.cut_block_id not in package.clean_view.included_block_ids
        ):
            raise ValueError("experimental partition cut is not a cleaned block of the package")
