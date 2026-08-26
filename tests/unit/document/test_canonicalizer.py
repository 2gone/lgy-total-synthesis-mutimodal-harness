from __future__ import annotations

import importlib
import shutil
from functools import cache
from pathlib import Path
from types import ModuleType

from tsdaf.contracts.package import TextBlockType

_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "paper_packages"


def _document_api() -> ModuleType:
    return importlib.import_module("tsdaf.lanes.document")


@cache
def _canonical(paper_id: str):  # type: ignore[no-untyped-def]
    return _document_api().canonicalize_paper_package(_FIXTURE_ROOT / paper_id)


def _blocks_by_id(package):  # type: ignore[no-untyped-def]
    return {block.block_id: block for block in package.blocks}


def _groups_by_id(package):  # type: ignore[no-untyped-def]
    return {group.visual_group_id: group for group in package.visual_groups}


def test_canonicalization_is_repeatable_and_copy_independent(tmp_path: Path) -> None:
    api = _document_api()
    package_dir = _FIXTURE_ROOT / "hyperfirin"
    copied_dir = tmp_path / "renamed-package-directory"
    shutil.copytree(package_dir, copied_dir)

    first = api.canonicalize_paper_package(package_dir)
    repeated = api.canonicalize_paper_package(package_dir)
    copied = api.canonicalize_paper_package(copied_dir)

    assert first == repeated == copied
    assert first.normalized_package_sha256 == (
        "d5ed0a5d3e3f0019b7da09b654b0dd69435b39a30b6c9b3dc112151f43776b94"
    )
    assert len(first.canonical_content_sha256) == 64
    assert first.blocks[0].block_id == "MAIN-P0000-B0000"
    assert first.blocks[-1].block_id == "MAIN-P0004-B0021"


def test_asset_order_dimensions_and_source_locator_retain_manifest_identity() -> None:
    package = _canonical("hyperfirin")
    blocks = _blocks_by_id(package)

    assert tuple(visual.asset for visual in package.visual_assets) == package.manifest.assets
    visual = package.visual_assets[5]
    assert visual.visual_id == "MAIN-P0001-A0000"
    assert (visual.width, visual.height) == (678, 603)
    assert (visual.bbox.x0, visual.bbox.y0, visual.bbox.x1, visual.bbox.y1) == (
        80,
        95,
        482,
        366,
    )
    assert visual.source.document_id == "hyperfirin"
    assert visual.source.native_block_index == 31
    assert visual.source.source_asset_ref == package.manifest.assets[5]

    source_block = blocks["MAIN-P0001-B0000"]
    assert source_block.block_type is TextBlockType.OTHER
    assert source_block.raw_text == ""
    assert source_block.source == visual.source
    assert source_block.section_path == (
        "The Total Synthesis of Hyperfirin via a Cyclooctadiene Strategy "
        "Julien A. König, Bernd Morgenstern, and Johann Jauch\\*",
    )


def test_adjacent_and_continued_captions_keep_exact_evidence() -> None:
    hyperfirin = _canonical("hyperfirin")
    hyperfirin_groups = _groups_by_id(hyperfirin)
    scheme_2 = hyperfirin_groups["MAIN-P0001-VG0001"]
    assert scheme_2.member_visual_ids == ("MAIN-P0001-A0001",)
    assert scheme_2.label_candidates == ("Scheme 2",)
    assert scheme_2.caption_block_ids == ("MAIN-P0001-B0005",)
    assert scheme_2.caption_text == ("Scheme 2. Construction of the Bicyclo[3.3.1]nonane Core")
    assert scheme_2.association_status == "CONFIRMED"

    alchivemycin = _canonical("alchivemycin_A")
    alchivemycin_blocks = _blocks_by_id(alchivemycin)
    figure_5 = _groups_by_id(alchivemycin)["MAIN-P0006-VG0000"]
    assert figure_5.member_visual_ids == ("MAIN-P0006-A0000",)
    assert figure_5.label_candidates == ("Figure 5",)
    assert figure_5.caption_block_ids == (
        "MAIN-P0006-B0001",
        "MAIN-P0006-B0002",
    )
    assert figure_5.caption_text == "\n".join(
        (
            alchivemycin_blocks["MAIN-P0006-B0001"].raw_text,
            alchivemycin_blocks["MAIN-P0006-B0002"].raw_text,
        )
    )
    assert figure_5.association_status == "CONFIRMED"


def test_explicit_panel_evidence_forms_only_the_two_real_multi_asset_patterns() -> None:
    ineleganolide = _canonical("ineleganolide")
    figure_1 = _groups_by_id(ineleganolide)["MAIN-P0000-VG0003"]
    assert figure_1.member_visual_ids == (
        "MAIN-P0000-A0003",
        "MAIN-P0000-A0004",
        "MAIN-P0000-A0005",
        "MAIN-P0000-A0006",
    )
    assert figure_1.label_candidates == ("Figure 1", "Figure 1A", "Figure 1B")
    assert figure_1.caption_block_ids == (
        "MAIN-P0000-B0013",
        "MAIN-P0000-B0017",
    )
    assert figure_1.association_status == "CONFIRMED"

    kalmanol_groups = _groups_by_id(_canonical("kalmanol"))
    scheme_2 = kalmanol_groups["MAIN-P0001-VG0001"]
    assert scheme_2.member_visual_ids == (
        "MAIN-P0001-A0001",
        "MAIN-P0001-A0002",
    )
    assert scheme_2.label_candidates == (
        "Scheme 2",
        "Scheme 2a",
        "Scheme 2b",
        "Scheme 2c",
    )
    assert scheme_2.association_status == "CONFIRMED"

    scheme_3 = kalmanol_groups["MAIN-P0002-VG0000"]
    assert scheme_3.member_visual_ids == (
        "MAIN-P0002-A0000",
        "MAIN-P0002-A0001",
    )
    assert scheme_3.label_candidates == (
        "Scheme 3",
        "Scheme 3a",
        "Scheme 3b",
        "Scheme 3c",
    )
    assert scheme_3.caption_block_ids == ("MAIN-P0002-B0004",)
    assert scheme_3.association_status == "CONFIRMED"


def test_conflicting_displaced_and_narrative_only_labels_stay_conservative() -> None:
    hyperfirin_groups = _groups_by_id(_canonical("hyperfirin"))
    displaced_scheme = hyperfirin_groups["MAIN-P0002-VG0000"]
    conflicting_table = hyperfirin_groups["MAIN-P0002-VG0001"]
    assert displaced_scheme.member_visual_ids == ("MAIN-P0002-A0000",)
    assert displaced_scheme.label_candidates == ("Scheme 3",)
    assert displaced_scheme.association_status == "AMBIGUOUS"
    assert conflicting_table.member_visual_ids == ("MAIN-P0002-A0001",)
    assert conflicting_table.label_candidates == ("Table 1", "Scheme 3")
    assert conflicting_table.caption_block_ids == ("MAIN-P0002-B0004",)
    assert conflicting_table.association_status == "AMBIGUOUS"

    principinol_groups = _groups_by_id(_canonical("principinol_B"))
    narrative_only = principinol_groups["MAIN-P0001-VG0000"]
    assert narrative_only.member_visual_ids == ("MAIN-P0001-A0000",)
    assert narrative_only.label_candidates == ()
    assert narrative_only.caption_block_ids == ()
    assert narrative_only.caption_text is None
    assert narrative_only.association_status == "NO_CAPTION"


def test_clean_view_is_reversible_and_keeps_nonreference_footnotes_and_affiliations() -> None:
    kalmanol = _canonical("kalmanol")
    kalmanol_blocks = _blocks_by_id(kalmanol)
    suppressed = {record.block_id: record.reason for record in kalmanol.clean_view.suppressed}
    included = set(kalmanol.clean_view.included_block_ids)
    assert set(kalmanol_blocks) == included | set(suppressed)
    assert included.isdisjoint(suppressed)
    assert suppressed["MAIN-P0001-B0007"] == "RUNNING_TEXT"
    assert suppressed["MAIN-P0003-B0013"] == "REFERENCES_SECTION"
    assert "MAIN-P0004-B0027" in included
    assert kalmanol_blocks["MAIN-P0004-B0027"].block_type is TextBlockType.FOOTNOTE

    ineleganolide = _canonical("ineleganolide")
    ineleganolide_blocks = _blocks_by_id(ineleganolide)
    ineleganolide_included = set(ineleganolide.clean_view.included_block_ids)
    assert "MAIN-P0003-B0012" in ineleganolide_included
    assert "MAIN-P0003-B0013" in ineleganolide_included
    assert ineleganolide_blocks["MAIN-P0003-B0012"].block_type is TextBlockType.PARAGRAPH
    assert ineleganolide_blocks["MAIN-P0003-B0022"].block_type is TextBlockType.REFERENCE_ENTRY
