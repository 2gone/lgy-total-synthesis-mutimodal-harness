"""Deterministic in-memory search over canonical clean text blocks."""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from tsdaf.contracts.package import CanonicalPaperPackage, CanonicalTextBlock

_HTML_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True, slots=True)
class SearchHit:
    """An exact-source block returned by document search."""

    block_id: str
    page_index: int
    section_path: tuple[str, ...]
    raw_text: str


class SearchIndex(Protocol):
    """Block-search interface shared by document-lane adapters."""

    def search_blocks(
        self,
        query: str,
        page_range: tuple[int, int] | None = None,
        section_filter: tuple[str, ...] | None = None,
        limit: int = 20,
    ) -> tuple[SearchHit, ...]: ...


def _normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = html.unescape(normalized)
    normalized = _HTML_TAG_RE.sub(" ", normalized)
    normalized = normalized.replace("\x00", " ").casefold()
    normalized = "".join(
        "-" if unicodedata.category(character) == "Pd" else character for character in normalized
    )
    return " ".join(normalized.split())


def _alphanumeric_tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    current: list[str] = []
    for character in value:
        if character.isalnum():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current.clear()
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


class InMemorySearchIndex:
    """Small deterministic block-only index for one canonical paper package."""

    def __init__(self, package: CanonicalPaperPackage) -> None:
        included_ids = set(package.clean_view.included_block_ids)
        entries: list[tuple[int, CanonicalTextBlock, str, tuple[str, ...]]] = []
        for canonical_order, block in enumerate(package.blocks):
            if block.block_id not in included_ids or not block.raw_text.strip():
                continue
            normalized_text = _normalize_search_text(block.raw_text)
            entries.append(
                (
                    canonical_order,
                    block,
                    normalized_text,
                    _alphanumeric_tokens(normalized_text),
                )
            )
        self._entries = tuple(entries)

    def search_blocks(
        self,
        query: str,
        page_range: tuple[int, int] | None = None,
        section_filter: tuple[str, ...] | None = None,
        limit: int = 20,
    ) -> tuple[SearchHit, ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if page_range is not None:
            first_page, last_page = page_range
            if first_page < 0 or last_page < first_page:
                raise ValueError("page_range must be non-negative and ascending")

        normalized_query = _normalize_search_text(query)
        query_tokens = _alphanumeric_tokens(normalized_query)
        if not query_tokens:
            return ()
        distinct_query_tokens = tuple(dict.fromkeys(query_tokens))
        query_token_set = set(distinct_query_tokens)
        normalized_sections = (
            {_normalize_search_text(component) for component in section_filter}
            if section_filter is not None
            else None
        )

        ranked: list[tuple[int, int, int, int, int, str, CanonicalTextBlock]] = []
        for canonical_order, block, normalized_text, block_tokens in self._entries:
            if page_range is not None and not (page_range[0] <= block.page_index <= page_range[1]):
                continue
            if normalized_sections is not None and not any(
                _normalize_search_text(component) in normalized_sections
                for component in block.section_path
            ):
                continue
            block_token_set = set(block_tokens)
            covered_tokens = query_token_set & block_token_set
            if not covered_tokens:
                continue
            total_occurrences = sum(block_tokens.count(token) for token in distinct_query_tokens)
            ranked.append(
                (
                    int(normalized_query in normalized_text),
                    int(query_token_set <= block_token_set),
                    len(covered_tokens),
                    total_occurrences,
                    canonical_order,
                    block.block_id,
                    block,
                )
            )

        ranked.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                -item[2],
                -item[3],
                item[4],
                item[5],
            )
        )
        return tuple(
            SearchHit(
                block_id=block.block_id,
                page_index=block.page_index,
                section_path=block.section_path,
                raw_text=block.raw_text,
            )
            for *_, block in ranked[:limit]
        )
