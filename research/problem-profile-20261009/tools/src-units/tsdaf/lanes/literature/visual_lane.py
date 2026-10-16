"""Visual-only request projection for private route discovery."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.profiles import (
    PreparedLaneCall,
    load_lane_resources,
    prepare_lane_call,
)
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput


def _lane_assignment(exact: VerifiedWorkUnitInput) -> dict[str, object]:
    """Publish this lane's half of the Router's assignment, and only its half.

    The two lanes must reach the Gate independently, or their agreement proves
    nothing. Naming the peer lane's sources here would leak what this lane has not
    been shown -- the prose is T1's business -- so the anchors it cannot read are dropped.
    """

    value = exact.route_group_assignment.model_dump(mode="json")
    value.pop("text_anchors", None)
    return value


def build_visual_route_request(
    work_unit: VerifiedWorkUnitInput,
    visual_payloads: Mapping[str, bytes],
) -> PreparedLaneCall:
    exact = work_unit
    resources = load_lane_resources("V1")
    if not isinstance(visual_payloads, Mapping):
        raise TypeError("visual_payloads must be a mapping")
    expected_ids = tuple(item.visual_id for item in exact.visual_sources)
    if tuple(visual_payloads) != expected_ids:
        raise ValueError("visual payload inventory must exactly match route-root authority")
    content: list[dict[str, object]] = []
    for source in exact.visual_sources:
        payload = visual_payloads[source.visual_id]
        if type(payload) is not bytes:
            raise TypeError("visual payloads must contain bytes")
        if (
            len(payload) != source.source_asset.byte_size
            or hashlib.sha256(payload).hexdigest() != source.source_asset.sha256
        ):
            raise ValueError("visual payload does not match its authorized pathless identity")
        metadata = {
            "schema_version": "tsdaf.visual-route-source.v2",
            "host_identity": {
                "paper_id": exact.paper_id,
                "document_revision_id": exact.document_revision_id,
                "source_phase": exact.source_phase,
                "route_group_id": exact.route_group_id,
                "depth": exact.depth,
                "source_order": exact.source_order,
                "ordinal": exact.ordinal,
            },
            "selected_subject_scope": exact.selected_subject_scope.model_dump(mode="json"),
            # What the Router decided this route group is for. It bounds nothing;
            # it is how a lane reading a whole article knows which route it traces.
            "route_group_assignment": _lane_assignment(exact),
            "visual_source": {
                "visual_id": source.visual_id,
                "visual_group_id": source.visual_group_id,
                "page_index": source.page_index,
                "bbox": source.bbox.model_dump(mode="json") if source.bbox is not None else None,
                "dimensions": source.dimensions,
                "source_asset": {"media_type": source.source_asset.media_type},
                "native_caption": source.native_caption,
                "group_caption": source.group_caption,
                "label_candidates": source.label_candidates,
            },
        }
        content.extend(
            (
                {"type": "text", "text": canonical_json_bytes(metadata).decode("utf-8")},
                {
                    "type": "image",
                    "mime_type": source.source_asset.media_type,
                    "data": base64.b64encode(payload).decode("ascii"),
                    "resolution": "high",
                },
            )
        )
    return prepare_lane_call(
        stage="V1",
        resources=resources,
        call_kwargs={
            "input": [{"type": "user_input", "content": content}],
            "system_instruction": resources.prompt_text,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": resources.provider_schema,
            },
        },
    )
