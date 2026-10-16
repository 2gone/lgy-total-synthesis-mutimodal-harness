"""Text-only request projection for private route discovery."""

from __future__ import annotations

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.profiles import (
    PreparedLaneCall,
    load_lane_resources,
    prepare_lane_call,
)
from tsdaf.lanes.literature.source_scope import VerifiedWorkUnitInput, numbered_text_source


def _lane_assignment(exact: VerifiedWorkUnitInput) -> dict[str, object]:
    """Publish this lane's half of the Router's assignment, and only its half.

    The two lanes must reach the Gate independently, or their agreement proves
    nothing. Naming the peer lane's sources here would leak what this lane has not
    been shown -- the figures are V1's business -- so the anchors it cannot read are dropped.
    """

    value = exact.route_group_assignment.model_dump(mode="json")
    value.pop("visual_anchors", None)
    return value


def build_text_route_request(work_unit: VerifiedWorkUnitInput) -> PreparedLaneCall:
    exact = work_unit
    resources = load_lane_resources("T1")
    payload = {
        "schema_version": "tsdaf.text-route-request.v2",
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
        "text_sources": [numbered_text_source(item) for item in exact.text_sources],
    }
    return prepare_lane_call(
        stage="T1",
        resources=resources,
        call_kwargs={
            "input": [
                {
                    "type": "user_input",
                    "content": [
                        {
                            "type": "text",
                            "text": canonical_json_bytes(payload).decode("utf-8"),
                        }
                    ],
                }
            ],
            "system_instruction": resources.prompt_text,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": resources.provider_schema,
            },
        },
    )
