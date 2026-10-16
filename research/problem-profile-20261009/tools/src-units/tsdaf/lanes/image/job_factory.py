"""Pure release gate for immutable image-processing jobs."""

from __future__ import annotations

import hashlib

from tsdaf.contracts.execution import FailureCode, TypedFailure
from tsdaf.contracts.handoff import ImageProcessingJob, ReleaseReceipt, VisualRoutingHandoff
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef

__all__ = ("create_image_processing_job",)


def _invalid_handoff() -> TypedFailure:
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=FailureCode.HANDOFF_INVALID,
        message="The image processing handoff authorization is invalid.",
        request_id=None,
        details=(),
    )


def create_image_processing_job(
    *,
    job_id: str,
    handoff: VisualRoutingHandoff,
    route_image_item_id: str,
    release_receipt: ReleaseReceipt | None,
    release_receipt_artifact: ArtifactRef | None,
) -> ImageProcessingJob | TypedFailure:
    """Create one job only when receipt evidence binds the current handoff."""

    if type(job_id) is not str:
        raise TypeError("job_id must be a str")
    if type(handoff) is not VisualRoutingHandoff:
        raise TypeError("handoff must be a VisualRoutingHandoff")
    if type(route_image_item_id) is not str:
        raise TypeError("route_image_item_id must be a str")
    if release_receipt is not None and type(release_receipt) is not ReleaseReceipt:
        raise TypeError("release_receipt must be a ReleaseReceipt or None")
    if release_receipt_artifact is not None and type(release_receipt_artifact) is not ArtifactRef:
        raise TypeError("release_receipt_artifact must be an ArtifactRef or None")

    if release_receipt is None or release_receipt_artifact is None:
        return _invalid_handoff()

    handoff_bytes = canonical_json_bytes(
        handoff.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    if release_receipt.handoff_sha256 != hashlib.sha256(handoff_bytes).hexdigest():
        return _invalid_handoff()

    receipt_bytes = canonical_json_bytes(
        release_receipt.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    if (
        release_receipt_artifact.sha256 != hashlib.sha256(receipt_bytes).hexdigest()
        or release_receipt_artifact.byte_size != len(receipt_bytes)
        or release_receipt_artifact.media_type != "application/json"
    ):
        return _invalid_handoff()

    selected_items = tuple(
        item for item in handoff.items if item.route_image_item_id == route_image_item_id
    )
    if len(selected_items) != 1:
        return _invalid_handoff()

    return ImageProcessingJob(
        job_id=job_id,
        route_image_item=selected_items[0],
        release_receipt=release_receipt_artifact,
        requested_pipeline="YOLO_MOLDET_OCSR_V1",
    )
