from __future__ import annotations

import importlib
import shutil
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

_FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "paper_packages"


def _document_api() -> ModuleType:
    return importlib.import_module("tsdaf.lanes.document")


@cache
def _package_and_index(paper_id: str):  # type: ignore[no-untyped-def]
    api = _document_api()
    package = api.canonicalize_paper_package(_FIXTURE_ROOT / paper_id)
    return package, api.InMemorySearchIndex(package)


def test_exact_phrase_ranks_first_and_returns_unmodified_source_text() -> None:
    _, index = _package_and_index("hyperfirin")
    query = "Scheme 2. Construction of the Bicyclo[3.3.1]nonane Core"

    hits = index.search_blocks(query)

    assert hits == index.search_blocks(query)
    assert hits[0].block_id == "MAIN-P0001-B0005"
    assert hits[0].page_index == 1
    assert hits[0].section_path == (
        "The Total Synthesis of Hyperfirin via a Cyclooctadiene Strategy "
        "Julien A. König, Bernd Morgenstern, and Johann Jauch\\*",
    )
    assert hits[0].raw_text == query


def test_page_and_exact_normalized_section_filters_are_inclusive() -> None:
    _, index = _package_and_index("alchivemycin_A")
    query = "optimized conditions reduced catalyst loadings"
    matching_section = ("LATE–STAGE ENZYMATIC SYNTHESIS",)

    hits = index.search_blocks(
        query,
        page_range=(6, 6),
        section_filter=matching_section,
    )

    assert hits[0].block_id == "MAIN-P0006-B0002"
    assert hits[0].page_index == 6
    assert hits[0].section_path[-1] == "Late-stage enzymatic synthesis"
    earlier_hits = index.search_blocks(query, page_range=(0, 5))
    assert all(0 <= hit.page_index <= 5 for hit in earlier_hits)
    assert all(hit.block_id != "MAIN-P0006-B0002" for hit in earlier_hits)
    methods_hits = index.search_blocks(query, section_filter=("Methods",))
    assert all(
        any(component.casefold() == "methods" for component in hit.section_path)
        for hit in methods_hits
    )
    assert all(hit.block_id != "MAIN-P0006-B0002" for hit in methods_hits)


def test_search_normalizes_html_nuls_dash_variants_and_nfkc_without_mutating_hits() -> None:
    _, hyperfirin_index = _package_and_index("hyperfirin")
    table_hits = hyperfirin_index.search_blocks("entry base equiv")
    assert table_hits[0].block_id == "MAIN-P0002-B0005"
    assert table_hits[0].raw_text.startswith("<table><tr><td>entry</td>")

    _, kalmanol_index = _package_and_index("kalmanol")
    nul_hits = kalmanol_index.search_blocks("C8 C9 bond")
    assert nul_hits[0].block_id == "MAIN-P0000-B0004"
    assert "C8\x00C9 bond" in nul_hits[0].raw_text

    _, principinol_index = _package_and_index("principinol_B")
    dash_hits = principinol_index.search_blocks("carbonyl-olefin metathesis")
    assert any("carbonyl–olefin metathesis" in hit.raw_text for hit in dash_hits)
    ascii_hits = principinol_index.search_blocks("Total Synthesis")
    nfkc_hits = principinol_index.search_blocks("Ｔｏｔａｌ Ｓｙｎｔｈｅｓｉｓ")
    assert nfkc_hits == ascii_hits
    title_hit = next(hit for hit in nfkc_hits if hit.block_id == "MAIN-P0000-B0001")
    assert title_hit.raw_text == "Total Synthesis of Principinol B"


def test_limit_and_page_range_boundaries_are_enforced() -> None:
    _, index = _package_and_index("principinol_B")

    all_hits = index.search_blocks("synthesis")
    one_hit = index.search_blocks("synthesis", limit=1)

    assert one_hit == all_hits[:1]
    assert 1 <= len(all_hits) <= 20
    with pytest.raises(ValueError):
        index.search_blocks("synthesis", limit=0)
    with pytest.raises(ValueError):
        index.search_blocks("synthesis", limit=21)
    with pytest.raises(ValueError):
        index.search_blocks("synthesis", page_range=(-1, 1))
    with pytest.raises(ValueError):
        index.search_blocks("synthesis", page_range=(2, 1))


def test_empty_and_suppressed_reference_or_running_text_queries_return_no_hits() -> None:
    _, alchivemycin_index = _package_and_index("alchivemycin_A")
    assert alchivemycin_index.search_blocks("") == ()
    assert alchivemycin_index.search_blocks("---") == ()
    assert alchivemycin_index.search_blocks("Naghavi") == ()

    _, hyperfirin_index = _package_and_index("hyperfirin")
    assert hyperfirin_index.search_blocks("orlef7") == ()


def test_search_results_are_identical_for_a_copied_package(tmp_path: Path) -> None:
    api = _document_api()
    original_package, original_index = _package_and_index("alchivemycin_A")
    copied_dir = tmp_path / "paper-copy"
    shutil.copytree(_FIXTURE_ROOT / "alchivemycin_A", copied_dir)
    copied_package = api.canonicalize_paper_package(copied_dir)
    copied_index = api.InMemorySearchIndex(copied_package)
    query = "late-stage enzymatic synthesis"

    assert copied_package == original_package
    assert copied_index.search_blocks(query) == original_index.search_blocks(query)
