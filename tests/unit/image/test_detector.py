from __future__ import annotations

from tsdaf.lanes.image.detector import RawMoleculeDetection, prepare_detections


def _raw(
    bbox: tuple[float, float, float, float],
    confidence: float,
) -> RawMoleculeDetection:
    return RawMoleculeDetection(
        bbox_xyxy=bbox,
        confidence=confidence,
        class_id=0,
        class_name="mol",
    )


def test_prepare_detections_uses_half_open_pixels_and_stable_m_ids() -> None:
    prepared = prepare_detections(
        route_image_item_id="RIMG001",
        width=100,
        height=80,
        raw=(_raw((10.2, 20.1, 30.1, 40.2), 0.9),),
    )

    assert prepared[0].molecule_id == "RIMG001:M001"
    assert prepared[0].bbox_xyxy_px.as_xyxy() == (10, 20, 31, 41)


def test_prepare_detections_suppresses_overlap_then_sorts_in_reading_order() -> None:
    prepared = prepare_detections(
        route_image_item_id="RIMG002",
        width=200,
        height=120,
        raw=(
            _raw((110.0, 10.0, 150.0, 40.0), 0.80),
            _raw((11.0, 11.0, 49.0, 39.0), 0.70),
            _raw((10.0, 10.0, 50.0, 40.0), 0.95),
            _raw((20.0, 70.0, 60.0, 100.0), 0.85),
        ),
    )

    assert [item.molecule_id for item in prepared] == [
        "RIMG002:M001",
        "RIMG002:M002",
        "RIMG002:M003",
    ]
    assert [item.bbox_xyxy_px.as_xyxy() for item in prepared] == [
        (10, 10, 50, 40),
        (110, 10, 150, 40),
        (20, 70, 60, 100),
    ]


def test_prepare_detections_clamps_boxes_and_drops_zero_area_outputs() -> None:
    prepared = prepare_detections(
        route_image_item_id="RIMG003",
        width=40,
        height=30,
        raw=(
            _raw((-4.0, -3.0, 8.2, 7.1), 0.9),
            _raw((50.0, 50.0, 60.0, 60.0), 0.8),
        ),
    )

    assert len(prepared) == 1
    assert prepared[0].bbox_xyxy_px.as_xyxy() == (0, 0, 9, 8)
