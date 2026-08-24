from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


TOKEN_IDENTITY_PREFACE = (
    "Each box labelled Mxx represents one molecular object. The value in parentheses is the visible "
    "paper compound label. Molecular structures are intentionally hidden. Treat the Mxx identifiers "
    "as the only molecular identities available."
)

_CONDITIONS = {
    "ocr_ablation": ("A_pdf_only", "B_pdf_hires", "C_pdf_hires_ocr"),
    "route_image_understanding": (
        "A_full_pdf",
        "B_raw_route_images",
        "C_tokenized_route_images",
    ),
}


@dataclass(frozen=True)
class Asset:
    path: Path

    @property
    def media_type(self) -> str:
        return {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".json": "application/json",
        }[self.path.suffix.lower()]


@dataclass(frozen=True)
class InputPlan:
    suite: str
    condition: str
    paper_id: str
    assets: tuple[Asset, ...]
    prompt_prefix: str = ""

    @property
    def result_dir_parts(self) -> tuple[str, str, str]:
        return self.suite, self.condition, self.paper_id


def suite_conditions(suite: str) -> tuple[str, ...]:
    try:
        return _CONDITIONS[suite]
    except KeyError:
        raise ValueError(f"Unknown suite: {suite}") from None


def _manifest_path(repo_root: Path, suite: str) -> Path:
    suite_conditions(suite)
    return repo_root / "experiments" / suite / "manifests" / "source_manifest.json"


def _manifest(repo_root: Path, suite: str) -> dict:
    return json.loads(_manifest_path(repo_root, suite).read_text(encoding="utf-8"))


def suite_papers(repo_root: Path | str, suite: str) -> tuple[str, ...]:
    data = _manifest(Path(repo_root), suite)
    papers = data["papers"]
    if isinstance(papers, list):
        return tuple(paper["paper_id"] for paper in papers)
    return tuple(papers.keys())


def _validate_cell(repo_root: Path, suite: str, condition: str, paper_id: str) -> dict:
    if condition not in suite_conditions(suite):
        raise ValueError(f"Unknown condition for {suite}: {condition}")
    data = _manifest(repo_root, suite)
    if paper_id not in suite_papers(repo_root, suite):
        raise ValueError(f"Unknown paper for {suite}: {paper_id}")
    return data


def build_input_plan(
    repo_root: Path | str,
    suite: str,
    condition: str,
    paper_id: str,
) -> InputPlan:
    root = Path(repo_root).resolve()
    data = _validate_cell(root, suite, condition, paper_id)
    assets: list[Asset] = []
    prefix = ""

    if suite == "ocr_ablation":
        paper = next(item for item in data["papers"] if item["paper_id"] == paper_id)
        assets.append(Asset(root / "corpus" / "papers" / Path(paper["pdf_path"]).name))
        if condition in {"B_pdf_hires", "C_pdf_hires_ocr"}:
            assets.extend(
                Asset(root / "experiments" / suite / relative)
                for relative in paper["hires_page_paths"]
            )
        if condition == "C_pdf_hires_ocr":
            assets.append(
                Asset(root / "results" / suite / "ocr" / paper_id / "ocr_output.json")
            )
    else:
        paper = data["papers"][paper_id]
        if condition == "A_full_pdf":
            assets.append(Asset(root / "corpus" / "papers" / Path(paper["pdf"]).name))
        else:
            field = "raw_image" if condition == "B_raw_route_images" else "tokenized_image"
            assets.extend(
                Asset(root / "experiments" / suite / route[field])
                for route in data["routes"]
                if route["paper_id"] == paper_id
            )
            if condition == "C_tokenized_route_images":
                prefix = TOKEN_IDENTITY_PREFACE

    return InputPlan(
        suite=suite,
        condition=condition,
        paper_id=paper_id,
        assets=tuple(assets),
        prompt_prefix=prefix,
    )
