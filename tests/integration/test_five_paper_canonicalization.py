from __future__ import annotations

import importlib
import json
from collections import Counter
from pathlib import Path
from types import ModuleType

_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "paper_packages"
_EXPECTED_PACKAGES = {
    "alchivemycin_A": {
        "blocks": 176,
        "assets": 5,
        "groups": 5,
        "suppressed": 107,
        "included": 69,
        "visual_suppressions": 0,
        "normalized_sha256": ("880aae2f9692017f292ddb58eea6aeb725bfe8bff560c88b497bab4ccb506b9c"),
    },
    "hyperfirin": {
        "blocks": 135,
        "assets": 9,
        "groups": 9,
        "suppressed": 71,
        "included": 64,
        "visual_suppressions": 3,
        "normalized_sha256": ("d5ed0a5d3e3f0019b7da09b654b0dd69435b39a30b6c9b3dc112151f43776b94"),
    },
    "ineleganolide": {
        "blocks": 116,
        "assets": 13,
        "groups": 10,
        "suppressed": 62,
        "included": 54,
        "visual_suppressions": 4,
        "normalized_sha256": ("ad5ad8873f346f18a5d3cf5579053752e9140f44316c3ad15f215902ec87c3d1"),
    },
    "kalmanol": {
        "blocks": 101,
        "assets": 6,
        "groups": 4,
        "suppressed": 60,
        "included": 41,
        "visual_suppressions": 0,
        "normalized_sha256": ("6e6d5e132512442a474a59710ecb0db008c094d115b7f1da29eacb8d5d58d6bf"),
    },
    "principinol_B": {
        "blocks": 87,
        "assets": 4,
        "groups": 4,
        "suppressed": 53,
        "included": 34,
        "visual_suppressions": 0,
        "normalized_sha256": ("f65b2ab848c59b60e0b1d7a1b0511fe1c817873b92a7606d2d90619e461f3bf0"),
    },
}
_EXPECTED_TYPE_COUNTS = Counter(
    {
        "DOCUMENT_TITLE": 5,
        "SECTION_HEADING": 40,
        "PARAGRAPH": 141,
        "FIGURE_CAPTION": 4,
        "TABLE_CAPTION": 1,
        "FIGURE_NOTE": 4,
        "TABLE_TEXT": 3,
        "FOOTNOTE": 13,
        "PAGE_HEADER": 83,
        "PAGE_FOOTER": 43,
        "PAGE_NUMBER": 21,
        "REFERENCE_ENTRY": 183,
        "OTHER": 74,
    }
)
_EXPECTED_SUPPRESSION_COUNTS = Counter(
    {
        "PAGE_HEADER": 83,
        "PAGE_FOOTER": 43,
        "PAGE_NUMBER": 21,
        "RUNNING_TEXT": 18,
        "REFERENCES_SECTION": 188,
    }
)
_EXPECTED_DIMENSIONS = {
    "alchivemycin_A": (
        (1414, 1598),
        (1420, 717),
        (1395, 937),
        (1418, 1359),
        (1417, 1676),
    ),
    "hyperfirin": (
        (60, 62),
        (56, 62),
        (668, 559),
        (670, 182),
        (120, 165),
        (678, 603),
        (676, 1337),
        (678, 928),
        (676, 543),
    ),
    "ineleganolide": (
        (57, 59),
        (59, 62),
        (34, 35),
        (198, 151),
        (193, 176),
        (256, 162),
        (656, 467),
        (117, 168),
        (1398, 1148),
        (650, 510),
        (481, 326),
        (535, 198),
        (209, 170),
    ),
    "kalmanol": (
        (678, 364),
        (668, 340),
        (1400, 571),
        (1359, 237),
        (1400, 862),
        (1384, 239),
    ),
    "principinol_B": ((678, 1009), (1400, 1239), (681, 1093), (678, 590)),
}
_EXPECTED_DECORATIVE_IDS = {
    "hyperfirin": {
        "MAIN-P0000-A0000",
        "MAIN-P0000-A0001",
        "MAIN-P0000-A0004",
    },
    "ineleganolide": {
        "MAIN-P0000-A0000",
        "MAIN-P0000-A0001",
        "MAIN-P0000-A0002",
        "MAIN-P0000-A0007",
    },
}


def _document_api() -> ModuleType:
    return importlib.import_module("tsdaf.lanes.document")


def _source_rows(paper_id: str) -> tuple[dict[str, object], ...]:
    path = _FIXTURE_ROOT / paper_id / "normalized" / "blocks.jsonl"
    return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())


def test_five_papers_close_the_frozen_canonical_inventory() -> None:
    api = _document_api()
    type_counts: Counter[str] = Counter()
    suppression_counts: Counter[str] = Counter()
    total_blocks = 0
    total_assets = 0
    total_groups = 0
    total_included = 0
    total_visual_suppressions = 0

    for paper_id, expected in _EXPECTED_PACKAGES.items():
        package = api.canonicalize_paper_package(_FIXTURE_ROOT / paper_id)
        assert len(package.blocks) == expected["blocks"]
        assert len(package.visual_assets) == expected["assets"]
        assert len(package.visual_groups) == expected["groups"]
        assert len(package.clean_view.suppressed) == expected["suppressed"]
        assert len(package.clean_view.included_block_ids) == expected["included"]
        assert len(package.visual_suppressions) == expected["visual_suppressions"]
        assert package.normalized_package_sha256 == expected["normalized_sha256"]
        assert (
            tuple((visual.width, visual.height) for visual in package.visual_assets)
            == _EXPECTED_DIMENSIONS[paper_id]
        )
        assert {record.visual_id for record in package.visual_suppressions} == (
            _EXPECTED_DECORATIVE_IDS.get(paper_id, set())
        )

        type_counts.update(block.block_type.value for block in package.blocks)
        suppression_counts.update(record.reason for record in package.clean_view.suppressed)
        total_blocks += len(package.blocks)
        total_assets += len(package.visual_assets)
        total_groups += len(package.visual_groups)
        total_included += len(package.clean_view.included_block_ids)
        total_visual_suppressions += len(package.visual_suppressions)

    assert (total_blocks, total_assets, total_groups) == (615, 37, 32)
    assert (sum(suppression_counts.values()), total_included) == (353, 262)
    assert total_visual_suppressions == 7
    assert type_counts == _EXPECTED_TYPE_COUNTS
    assert suppression_counts == _EXPECTED_SUPPRESSION_COUNTS


def test_every_normalized_row_and_asset_remains_reversible() -> None:
    api = _document_api()
    raw_text_nul_count = 0
    native_caption_nul_count = 0

    for paper_id in _EXPECTED_PACKAGES:
        package = api.canonicalize_paper_package(_FIXTURE_ROOT / paper_id)
        rows = _source_rows(paper_id)
        assets_by_path = {asset.relative_path: asset for asset in package.manifest.assets}
        assert len(package.blocks) == len(rows)

        for block, row in zip(package.blocks, rows, strict=True):
            page_index = int(row["page_idx"])
            reading_order = int(row["reading_order"])
            assert block.block_id == (f"MAIN-P{page_index:04d}-B{reading_order:04d}")
            assert block.raw_text == row["raw_text"]
            assert block.section_path == tuple(row["section_path"])
            assert block.source.document_id == paper_id
            assert block.source.page_index == page_index
            assert block.source.native_block_index == row["mineru_native_ref"]["native_index"]
            assert (
                block.source.bbox.x0,
                block.source.bbox.y0,
                block.source.bbox.x1,
                block.source.bbox.y1,
            ) == tuple(row["bbox_norm_1000"])
            expected_asset = assets_by_path.get(row["asset_ref"])
            assert block.source.source_asset_ref == expected_asset
            raw_text_nul_count += block.raw_text.count("\x00")

        asset_rows = tuple(row for row in rows if row["asset_ref"] is not None)
        assert tuple(visual.asset for visual in package.visual_assets) == package.manifest.assets
        assert tuple(visual.source.native_block_index for visual in package.visual_assets) == tuple(
            row["mineru_native_ref"]["native_index"] for row in asset_rows
        )
        native_caption_nul_count += sum(
            (visual.native_caption or "").count("\x00") for visual in package.visual_assets
        )

    assert raw_text_nul_count == 7
    assert native_caption_nul_count == 1
