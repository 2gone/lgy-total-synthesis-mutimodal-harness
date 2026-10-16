"""One Host-only identity for the two route readers, independent of their execution.

Descriptors and reuse bindings are Host-only. A missing backend namespace keeps
the legacy cold path; existing caller/authority checks still apply.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Literal

from pydantic import field_validator

from tsdaf.contracts._base import ContractModel, _sha256
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput

READER_IDENTITY_MEDIA_TYPE = "application/vnd.tsdaf.literature-reader-identity+json"
# Bump when Host sentence/coordinate/authority validation changes reader meaning.
READER_VALIDATION_VERSION = "route-reader-source-validation.v1"


class ReaderIdentity(ContractModel):
    schema_version: Literal["tsdaf.literature-reader-identity.v1"]
    stage: Literal["T1", "V1"]
    validation_version: str
    request_ref: ArtifactRef
    resource_manifest_ref: ArtifactRef
    paper_id: str
    document_revision_id: str
    source_phase: Literal["MAIN"]
    package_sha256: str
    lane_authority_sha256: str
    backend_namespace: str | None

    @field_validator("package_sha256", "lane_authority_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="reader identity hash")

    @property
    def cache_eligible(self) -> bool:
        return self.backend_namespace is not None


def build_reader_identity(
    *,
    stage: Literal["T1", "V1"],
    work_unit: VerifiedWorkUnitInput,
    request_ref: ArtifactRef,
    resource_manifest_ref: ArtifactRef,
    backend_namespace: str | None,
) -> ReaderIdentity:
    """Describe exact prepared bytes plus all delivered same-lane source authority.

    Request refs must come from the authorized adapter's prepare token, not from
    builder kwargs. Hashing the full lane source projection includes unselected
    sources and Host-only source identities without storing another copy of text.
    """
    if stage not in ("T1", "V1"):
        raise ValueError("reader identity requires T1 or V1")
    source_ids = (
        work_unit.read_authority.text_block_ids
        if stage == "T1"
        else work_unit.read_authority.visual_ids
    )
    sources = work_unit.text_sources if stage == "T1" else work_unit.visual_sources
    authority = canonical_json_bytes(
        {
            "source_ids": source_ids,
            "sources": [source.model_dump(mode="json") for source in sources],
        }
    )
    return ReaderIdentity(
        schema_version="tsdaf.literature-reader-identity.v1",
        stage=stage,
        validation_version=READER_VALIDATION_VERSION,
        request_ref=request_ref,
        resource_manifest_ref=resource_manifest_ref,
        paper_id=work_unit.paper_id,
        document_revision_id=work_unit.document_revision_id,
        source_phase=work_unit.source_phase,
        package_sha256=work_unit.package_sha256,
        lane_authority_sha256=hashlib.sha256(authority).hexdigest(),
        backend_namespace=backend_namespace,
    )


def verify_reader_identity(
    *,
    store: PrivateArtifactStore,
    ref: ArtifactRef | None,
    stage: Literal["T1", "V1"],
    request_ref: ArtifactRef,
    resource_manifest_ref: ArtifactRef,
    work_unit: VerifiedWorkUnitInput | None = None,
) -> None:
    """Close new descriptors; pre-C1 sealed pairs have no descriptor to consume."""
    if ref is None:
        return
    if ref.media_type != READER_IDENTITY_MEDIA_TYPE:
        raise ValueError("reader identity has the wrong media type")
    store.verify(ref)
    payload = store.read_bytes(ref)
    identity = ReaderIdentity.model_validate_json(payload)
    if payload != canonical_json_bytes(identity.model_dump(mode="json")):
        raise ValueError("reader identity is not Host-canonical")
    if (
        identity.stage != stage
        or identity.request_ref != request_ref
        or identity.resource_manifest_ref != resource_manifest_ref
        or identity.validation_version != READER_VALIDATION_VERSION
    ):
        raise ValueError("reader identity does not close the prepared lane request")
    if work_unit is not None and identity != build_reader_identity(
        stage=stage,
        work_unit=work_unit,
        request_ref=request_ref,
        resource_manifest_ref=resource_manifest_ref,
        backend_namespace=identity.backend_namespace,
    ):
        raise ValueError("reader identity does not close the current source authority")


READER_CACHE_STAGE_VERSION = "1.0.0"
READER_CACHE_OUTPUT_MEDIA = "application/vnd.tsdaf.literature-reader-cache-output+json"
READER_REUSE_MEDIA = "application/vnd.tsdaf.literature-reader-reuse-binding+json"


def reader_cache_stage(stage: Literal["T1", "V1"]) -> str:
    return f"literature-{stage.lower()}-reader"


class ReaderCacheOutput(ContractModel):
    schema_version: Literal["tsdaf.reader-cache-output.v1"] = "tsdaf.reader-cache-output.v1"
    identity_ref: ArtifactRef
    original_preparation_ref: ArtifactRef
    raw_record_ref: ArtifactRef


class ReaderReuseBinding(ContractModel):
    schema_version: Literal["tsdaf.reader-reuse-binding.v1"] = "tsdaf.reader-reuse-binding.v1"
    current_preparation_ref: ArtifactRef
    identity_ref: ArtifactRef
    output_ref: ArtifactRef
    producer_result_ref: ArtifactRef
    producer_terminal_ref: ArtifactRef


# Workflow proves the existing runner's producer/key/terminal and current backend;
# lane validation proves the immutable original and current request/source closures.
ReaderReuseVerifier = Callable[[ReaderReuseBinding], None]
