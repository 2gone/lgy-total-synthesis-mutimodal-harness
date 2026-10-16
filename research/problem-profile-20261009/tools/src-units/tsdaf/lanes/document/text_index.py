"""Deterministic block-local sentence index over a canonical paper package."""

from __future__ import annotations

from dataclasses import dataclass

from tsdaf.contracts.package import CanonicalPaperPackage

from .sentences import SentenceSpan, split_sentence_spans

__all__ = ("IndexedTextBlock", "PaperTextIndex", "build_paper_text_index")


@dataclass(frozen=True, slots=True)
class IndexedTextBlock:
    """One canonical block and its exact block-local sentence slices."""

    block_id: str
    page_index: int
    section_path: tuple[str, ...]
    raw_text: str
    sentences: tuple[SentenceSpan, ...]


@dataclass(frozen=True, slots=True)
class PaperTextIndex:
    """Immutable canonical-order text index with no page-text offset domain."""

    paper_id: str
    canonical_package_sha256: str
    blocks: tuple[IndexedTextBlock, ...]

    def block(self, block_id: str) -> IndexedTextBlock | None:
        """Return an indexed block by canonical ID, if present."""

        for candidate in self.blocks:
            if candidate.block_id == block_id:
                return candidate
        return None


def build_paper_text_index(package: CanonicalPaperPackage) -> PaperTextIndex:
    """Index every canonical block in package order without changing its text."""

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    return PaperTextIndex(
        paper_id=package.manifest.paper_id,
        canonical_package_sha256=package.canonical_content_sha256,
        blocks=tuple(
            IndexedTextBlock(
                block_id=block.block_id,
                page_index=block.page_index,
                section_path=block.section_path,
                raw_text=block.raw_text,
                sentences=split_sentence_spans(block.raw_text),
            )
            for block in package.blocks
        ),
    )
