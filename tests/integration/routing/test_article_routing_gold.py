"""Offline validation for the frozen five-paper human routing gold."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
GOLD_PATH = PROJECT_ROOT / "tests" / "golden" / "article_routing_v1.json"
PAPER_FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "paper_packages"
SEMANTIC_FIXTURE = (
    PROJECT_ROOT / "tests" / "fixtures" / "providers" / "fake" / "article_router_v1.json"
)

PAPER_IDS = (
    "alchivemycin_A",
    "hyperfirin",
    "ineleganolide",
    "kalmanol",
    "principinol_B",
)

EXPECTED_GOLD = {
    "schema_version": "tsdaf.article-routing-human-gold.v1",
    "gold_id": "weak-article-router-v1-five-paper",
    "frozen_date": "2026-08-26",
    "total_visual_decisions": 37,
    "papers": [
        {
            "paper_id": "alchivemycin_A",
            "canonical_package_sha256": (
                "cd8704c9f7c2b7aafaadf7c955011c79fbe5afac5a94fa7ebc6e30659811578e"
            ),
            "expected_article_type": "TOTAL_SYNTHESIS",
            "expected_visual_count": 5,
            "required_route_attributes": ["CHEMOENZYMATIC"],
            "forward_assets": [
                {
                    "human_label": "Fig. 3",
                    "visual_id": "MAIN-P0004-A0000",
                    "source_asset_sha256": (
                        "a3057bc392aca32054932b4677850e965f10627995a3b5e0df54f254cd158f17"
                    ),
                },
                {
                    "human_label": "Fig. 4",
                    "visual_id": "MAIN-P0005-A0000",
                    "source_asset_sha256": (
                        "9335d15e09fc7d4889f316c95ba1db3a7418d230af8ddc6e3c307e58b4f9a45b"
                    ),
                },
                {
                    "human_label": "complete Fig. 5",
                    "visual_id": "MAIN-P0006-A0000",
                    "source_asset_sha256": (
                        "4d64043f869c5417aa84c02b9634b0ec859ce4af4588dce9f431b9682beb58b6"
                    ),
                },
            ],
        },
        {
            "paper_id": "hyperfirin",
            "canonical_package_sha256": (
                "ab9e5955dd9cdef1950138e0a7d0ca64065c5c39a55cdaa18d1ed2ecba7386cf"
            ),
            "expected_article_type": "TOTAL_SYNTHESIS",
            "expected_visual_count": 9,
            "required_route_attributes": [],
            "forward_assets": [
                {
                    "human_label": "Scheme 2 route asset",
                    "visual_id": "MAIN-P0001-A0001",
                    "source_asset_sha256": (
                        "5983f3b72b704d3ad0f4d94c7f03287f2577dd0d356b22aba156293987468be0"
                    ),
                },
                {
                    "human_label": "Scheme 3 route asset",
                    "visual_id": "MAIN-P0002-A0000",
                    "source_asset_sha256": (
                        "d706696f559b0075b106465a58ba6059d9085b0df5fead74b22b42751facb94a"
                    ),
                },
            ],
        },
        {
            "paper_id": "ineleganolide",
            "canonical_package_sha256": (
                "1b0d69f350a2d7766bc0021960b11d719df8a2bf123dd195a8804795854e1078"
            ),
            "expected_article_type": "TOTAL_SYNTHESIS",
            "expected_visual_count": 13,
            "required_route_attributes": [],
            "forward_assets": [
                {
                    "human_label": "Scheme 1",
                    "visual_id": "MAIN-P0001-A0000",
                    "source_asset_sha256": (
                        "3d2eda8baea927ef6c5e8ffdde12a76a6653e3be28c15c27fb1bb15b96b61328"
                    ),
                }
            ],
        },
        {
            "paper_id": "kalmanol",
            "canonical_package_sha256": (
                "7cf7c4e3b2cf67d21c939e9c43bba382250260b5995fb6b779e037a923694500"
            ),
            "expected_article_type": "TOTAL_SYNTHESIS",
            "expected_visual_count": 6,
            "required_route_attributes": [],
            "forward_assets": [
                {
                    "human_label": "Scheme 2 route asset",
                    "visual_id": "MAIN-P0001-A0001",
                    "source_asset_sha256": (
                        "f3b6874b62543babe547b945209cd1b7e78410f5468ddac64d44ba132e43bd6b"
                    ),
                },
                {
                    "human_label": "Scheme 3 route asset",
                    "visual_id": "MAIN-P0002-A0000",
                    "source_asset_sha256": (
                        "0084dd0937d74c0401c6e243103bf47213015f9dbf9472b601f62554001cc007"
                    ),
                },
            ],
        },
        {
            "paper_id": "principinol_B",
            "canonical_package_sha256": (
                "e77c9ce11c376e5b423eafff53120a7a7a4727cf5bd2ab52a4beb6a7eae11e60"
            ),
            "expected_article_type": "TOTAL_SYNTHESIS",
            "expected_visual_count": 4,
            "required_route_attributes": [],
            "forward_assets": [
                {
                    "human_label": "Scheme 2",
                    "visual_id": "MAIN-P0001-A0000",
                    "source_asset_sha256": (
                        "dc98f1c370c2b62e6c5ca9573ce00b65e709bb26cc6672f7e0ec126bb104f87c"
                    ),
                }
            ],
        },
    ],
}


def test_five_paper_human_gold_matches_canonical_sources_and_offline_semantics() -> None:
    # Break caught: routing acceptance can proceed without its frozen human-reviewed seal.
    assert GOLD_PATH.is_file(), f"frozen article-routing gold seal is absent: {GOLD_PATH}"

    gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
    assert gold == EXPECTED_GOLD
    assert tuple(paper["paper_id"] for paper in gold["papers"]) == PAPER_IDS

    from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package

    packages = {}
    total_visuals = 0
    total_forward_assets = 0
    for paper_gold in gold["papers"]:
        paper_id = paper_gold["paper_id"]
        package_dir = PAPER_FIXTURES / paper_id
        package = canonicalize_paper_package(package_dir)
        packages[paper_id] = package

        assert package.canonical_content_sha256 == paper_gold["canonical_package_sha256"]
        assert len(package.visual_assets) == paper_gold["expected_visual_count"]

        raw_manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        original_assets = {asset["relative_path"]: asset for asset in raw_manifest["assets"]}
        visuals_by_id = {visual.visual_id: visual for visual in package.visual_assets}
        assert len(visuals_by_id) == len(package.visual_assets)
        for visual in package.visual_assets:
            artifact = visual.asset.model_dump(mode="json")
            assert artifact == original_assets[visual.asset.relative_path]
            assert visual.source.source_asset_ref is not None
            assert visual.source.source_asset_ref.model_dump(mode="json") == artifact

        expected_forward = paper_gold["forward_assets"]
        for expected in expected_forward:
            visual = visuals_by_id[expected["visual_id"]]
            assert visual.asset.sha256 == expected["source_asset_sha256"]

        total_visuals += len(package.visual_assets)
        total_forward_assets += len(expected_forward)

    semantic = json.loads(SEMANTIC_FIXTURE.read_text(encoding="utf-8"))
    assert semantic["schema_version"] == "tsdaf.fake-article-router.v1"
    semantic_papers = semantic["papers"]
    assert tuple(paper["paper_id"] for paper in semantic_papers) == PAPER_IDS

    visual_escalations = 0
    for paper_gold, semantic_paper in zip(gold["papers"], semantic_papers, strict=True):
        paper_id = paper_gold["paper_id"]
        package = packages[paper_id]
        expected_forward_ids = tuple(asset["visual_id"] for asset in paper_gold["forward_assets"])

        assert semantic_paper["canonical_package_sha256"] == paper_gold["canonical_package_sha256"]
        assert semantic_paper["article_type"] == paper_gold["expected_article_type"]
        assert tuple(semantic_paper["ordered_visual_ids"]) == expected_forward_ids
        assert set(paper_gold["required_route_attributes"]).issubset(semantic_paper["attributes"])

        decisions = semantic_paper["visual_decisions"]
        decision_ids = tuple(decision["visual_id"] for decision in decisions)
        canonical_visual_ids = tuple(visual.visual_id for visual in package.visual_assets)
        assert decision_ids == canonical_visual_ids
        assert len(decision_ids) == len(set(decision_ids))
        assert (
            tuple(
                decision["visual_id"]
                for decision in decisions
                if decision["final_decision"] == "FORWARD_ROUTE"
            )
            == expected_forward_ids
        )
        assert all(decision["triage_result"] is None for decision in decisions)
        assert all(
            decision["initial_decision"] != "NEEDS_VISUAL_VERIFICATION"
            and decision["final_decision"] != "UNCERTAIN"
            for decision in decisions
        )
        visual_escalations += sum(
            decision["initial_decision"] == "NEEDS_VISUAL_VERIFICATION"
            or decision["triage_result"] is not None
            or decision["final_decision"] == "UNCERTAIN"
            for decision in decisions
        )

        canonical_block_ids = {block.block_id for block in package.blocks}
        route_group_id = semantic_paper["route_group_id"]
        route_core_ids = tuple(semantic_paper["core_text_block_ids"])
        assert route_group_id
        assert route_core_ids
        assert set(route_core_ids) <= canonical_block_ids

    assert gold["total_visual_decisions"] == total_visuals == 37
    assert total_forward_assets == 9
    assert visual_escalations == 0
