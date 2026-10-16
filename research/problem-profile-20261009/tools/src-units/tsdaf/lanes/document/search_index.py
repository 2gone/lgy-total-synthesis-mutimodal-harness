"""Deterministic in-memory search over canonical clean text blocks."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from tsdaf.contracts.package import CanonicalPaperPackage, CanonicalTextBlock
from tsdaf.lanes.document.sentences import split_sentence_spans

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_QUOTED_RE = re.compile(r'"([^"]+)"')
SEARCH_SEMANTICS_VERSION = "2.4.0"
# Additional navigation tokens come only from an explicit typesetting boundary.
# Keep the original joined token too: this is not a rewrite of chemical identity.
_WORD_SCRIPT_RE = re.compile(
    r"(?<![\w])(?P<word>[A-Za-z][a-z]{2,})"
    r"(?P<script>[⁰¹²³⁴⁵⁶⁷⁸⁹]+|<sup>[0-9]+</sup>|(?:\$\s*)?\^(?:\{[0-9]+\}|[0-9]+)(?:\s*\$)?)"
    r"(?![\w⁺⁻+\-])"
)
# Matching sentences named per hit: enough to see what matched and where, not the block.
SENTENCE_MATCHES_PER_HIT = 3


class SearchMode(StrEnum):
    TERMS = "TERMS"
    LITERAL = "LITERAL"


class UnsupportedSearchQuery(ValueError):
    """A query this mode cannot evaluate; never evidence of source absence."""


@dataclass(frozen=True, slots=True)
class SentenceMatch:
    """A matching raw sentence, or a contiguous range ending at an exclusive index."""

    sentence: int
    text: str
    end_sentence: int | None = None


@dataclass(frozen=True, slots=True)
class SearchHit:
    """An exact-source block returned by document search, with where in it the query hit."""

    block_id: str
    page_index: int
    section_path: tuple[str, ...]
    raw_text: str
    sentence_count: int = 0
    matches: tuple[SentenceMatch, ...] = ()
    # Host-only relevance, independent of document-local order. Larger ranks first.
    relevance: tuple[int, int, int, int] = (0, 0, 0, 0)
    matched_terms: tuple[str, ...] = ()

    @property
    def match_kind(self) -> str:
        return "FULL" if self.relevance[1] else "PARTIAL"


def match_tier(relevance: Sequence[int]) -> tuple[int, int]:
    """One eligibility tier, shared by the index and cross-document merge."""
    return relevance[1], relevance[2]


class SearchIndex(Protocol):
    """Block-search interface shared by document-lane adapters."""

    def search_blocks(
        self,
        query: str,
        page_range: tuple[int, int] | None = None,
        section_filter: tuple[str, ...] | None = None,
        limit: int = 20,
        mode: SearchMode = SearchMode.TERMS,
    ) -> tuple[SearchHit, ...]: ...


def _render_search_text(value: str) -> str:
    # Import on use: document's public API also initializes the search module while
    # the shared author-word reader imports document page-furniture mechanics.
    from tsdaf.lanes.literature.author_words import author_rendering

    # Preserve script tags for the common rendering; other HTML is layout (e.g.
    # table cells), and must separate words. Matching returns the untouched source.
    normalized = html.unescape(value).replace("\x00", " ")
    normalized = _HTML_TAG_RE.sub(
        lambda m: m.group() if re.fullmatch(r"</?su[bp]>", m.group()) else " ", normalized
    )
    normalized = author_rendering(normalized)
    normalized = "".join(
        "-" if unicodedata.category(character) == "Pd" else character for character in normalized
    )
    return " ".join(normalized.split())


def _normalize_search_text(value: str) -> str:
    return _render_search_text(value).casefold()


def _token_spans(value: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index, character in enumerate(value):
        if character.isalnum():
            if start is None:
                start = index
        elif start is not None:
            spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, len(value)))
    return tuple(spans)


def _alphanumeric_tokens(value: str) -> tuple[str, ...]:
    return tuple(value[start:end] for start, end in _token_spans(value))


def _search_tokens(value: str) -> tuple[str, tuple[tuple[str, int, int], ...]]:
    """Strict rendered tokens plus lexical bases at real superscript boundaries.

    Positions belong to the normalized display, never to the evidence. Baseline
    digits, isotope prefixes, short units/formulas and signed powers gain no alias.
    A script's scientific meaning is not inferred; its full spelling remains intact.
    """
    raw = html.unescape(value)
    if not _WORD_SCRIPT_RE.search(raw):
        normalized = _normalize_search_text(raw)
        return normalized, tuple((normalized[a:b], a, b) for a, b in _token_spans(normalized))
    marker = next(chr(i) for i in range(0xE000, 0xF900) if chr(i) not in raw)
    marked = _render_search_text(
        _WORD_SCRIPT_RE.sub(lambda m: m["word"] + marker + m["script"], raw)
    ).casefold()
    plain: list[str] = []
    aliases = []
    for character in marked:
        if character != marker:
            plain.append(character)
            continue
        end = len(plain)
        start = end
        while start and plain[start - 1].isalpha():
            start -= 1
        aliases.append(("".join(plain[start:end]), start, end))
    normalized = "".join(plain)
    return normalized, (
        *((normalized[a:b], a, b) for a, b in _token_spans(normalized)),
        *aliases,
    )


def search_preview(text: str, query: str, mode: SearchMode, *, max_chars: int) -> str:
    """A bounded, case-preserving navigation rendering around the actual match.

    The index and preview share normalization and token boundaries. This is not a
    citable raw-source slice: its sentence number still opens the untouched original.
    """
    rendered = _render_search_text(text)
    if len(rendered) <= max_chars:
        return rendered
    normalized = rendered.casefold()
    # Case folding can expand characters (e.g. ß); offsets here belong to the
    # displayed rendering, never to the original evidence.
    offsets = [i for i, ch in enumerate(rendered) for _ in ch.casefold()]
    normalized_query = _normalize_search_text(query)
    phrases = (
        (normalized_query,)
        if mode is SearchMode.LITERAL
        else tuple(_normalize_search_text(p) for p in _QUOTED_RE.findall(query))
    )
    focus = next((at for phrase in phrases if (at := normalized.find(phrase)) >= 0), None)
    width = max_chars - 2  # leave room for both omission marks
    if focus is None:
        tokens = set(_alphanumeric_tokens(normalized_query))
        _, searchable = _search_tokens(text)
        matches = [(token, a, b) for token, a, b in searchable if token in tokens]
        # When query words are separated, show the window carrying most of them.
        # This selects a preview only; it does not change block or sentence ranking.
        starts = [max(0, offsets[a] - width // 4) for _, a, _ in matches]
        start = max(
            starts,
            key=lambda left: (
                len({token for token, a, _ in matches if left <= offsets[a] < left + width}),
                -left,
            ),
            default=0,
        )
    else:
        start = max(0, offsets[focus] - width // 4)
    start = min(start, len(rendered) - width)
    end = start + width
    return ("…" if start else "") + rendered[start:end] + ("…" if end < len(rendered) else "")


class InMemorySearchIndex:
    """Small deterministic block-only index for one canonical paper package."""

    def __init__(
        self,
        package: CanonicalPaperPackage,
        *,
        included_block_ids: tuple[str, ...] | None = None,
    ) -> None:
        # The searchable part of the document: the whole clean view unless the caller
        # was handed a narrower part of it (the Router after the experimental split).
        included_ids = set(
            package.clean_view.included_block_ids
            if included_block_ids is None
            else included_block_ids
        )
        entries: list[tuple[int, CanonicalTextBlock, str, tuple[str, ...]]] = []
        for canonical_order, block in enumerate(package.blocks):
            if block.block_id not in included_ids or not block.raw_text.strip():
                continue
            normalized_text, searchable = _search_tokens(block.raw_text)
            entries.append(
                (
                    canonical_order,
                    block,
                    normalized_text,
                    tuple(token for token, _, _ in searchable),
                )
            )
        self._entries = tuple(entries)

    def search_blocks(
        self,
        query: str,
        page_range: tuple[int, int] | None = None,
        section_filter: tuple[str, ...] | None = None,
        limit: int = 20,
        mode: SearchMode = SearchMode.TERMS,
    ) -> tuple[SearchHit, ...]:
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if page_range is not None:
            first_page, last_page = page_range
            if first_page < 0 or last_page < first_page:
                raise ValueError("page_range must be non-negative and ascending")

        normalized_query = _normalize_search_text(query)
        if not normalized_query:
            raise UnsupportedSearchQuery("EMPTY_QUERY")
        if not isinstance(mode, SearchMode):
            raise ValueError("unknown search mode")
        query_tokens = _alphanumeric_tokens(normalized_query)
        # In TERMS mode a quoted phrase is taken literally: the block must carry it as a
        # continuous string, not merely each of its words somewhere.
        phrases = (
            tuple(_normalize_search_text(quoted) for quoted in _QUOTED_RE.findall(query))
            if mode is SearchMode.TERMS
            else ()
        )
        if mode is SearchMode.TERMS:
            if any(marker in normalized_query for marker in ("*", "?", "\\")):
                raise UnsupportedSearchQuery("PATTERN_SYNTAX_UNSUPPORTED_USE_LITERAL_OR_TERMS")
            if not query_tokens:
                raise UnsupportedSearchQuery("SYMBOL_QUERY_REQUIRES_LITERAL_MODE")
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
            if mode is SearchMode.LITERAL:
                if normalized_query in normalized_text:
                    ranked.append((1, 1, 1, 1, canonical_order, block.block_id, block))
                continue
            block_token_set = set(block_tokens)
            covered_tokens = query_token_set & block_token_set
            if not covered_tokens:
                continue
            if any(phrase not in normalized_text for phrase in phrases):
                continue
            if (
                not query_token_set <= block_token_set
                and len(query_token_set) > 1
                and not any(len(token) > 1 for token in covered_tokens)
            ):
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

        tier = max((match_tier(row[:4]) for row in ranked), default=None)
        ranked = [row for row in ranked if match_tier(row[:4]) == tier]
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
        hits = []
        for exact, complete, coverage, occurrences, _, _, block in ranked[:limit]:
            sentence_count, matches = _sentence_matches(
                block.raw_text, normalized_query, query_token_set, mode, phrases
            )
            hits.append(
                SearchHit(
                    block_id=block.block_id,
                    page_index=block.page_index,
                    section_path=block.section_path,
                    raw_text=block.raw_text,
                    sentence_count=sentence_count,
                    matches=matches,
                    relevance=(exact, complete, coverage, occurrences),
                    matched_terms=tuple(
                        token
                        for token in distinct_query_tokens
                        if token in {word for word, _, _ in _search_tokens(block.raw_text)[1]}
                    ),
                )
            )
        return tuple(hits)


def _sentence_matches(
    raw_text: str,
    normalized_query: str,
    query_tokens: set[str],
    mode: SearchMode,
    phrases: tuple[str, ...] = (),
) -> tuple[int, tuple[SentenceMatch, ...]]:
    """The sentences that carry the match, under the same normalization the block matched by.

    Numbered by the same segmentation a text read selects with, so a hit's sentence is
    the one a ``text_range`` around it returns. The best few by query coverage, in order;
    a sentence carrying a quoted phrase outranks one that merely shares words.
    """
    spans = split_sentence_spans(raw_text)
    searchable_sentences = tuple(_search_tokens(span.text) for span in spans)
    normalized_sentences = tuple(normalized for normalized, _ in searchable_sentences)
    crossing: dict[tuple[int, int], SentenceMatch] = {}
    for phrase in (normalized_query,) if mode is SearchMode.LITERAL else phrases:
        if any(phrase in text for text in normalized_sentences):
            continue
        # Rare fallback: the block matched a literal phrase across sentence boundaries.
        # Locate its first ending sentence, then trim the leading sentences using the
        # same matcher on exact raw slices. No rendered-to-source offset map is needed.
        end = next(
            (
                i + 1
                for i in range(1, len(spans))
                if phrase in _normalize_search_text(raw_text[: spans[i].char_end])
            ),
            None,
        )
        if end is None:
            continue  # The ordinary block entrance remains available.
        for start in range(end - 2, -1, -1):
            raw = raw_text[spans[start].char_start : spans[end - 1].char_end]
            if phrase in _normalize_search_text(raw):
                crossing.setdefault((start, end), SentenceMatch(start, raw, end))
                break
        if len(crossing) == SENTENCE_MATCHES_PER_HIT:
            break
    scored: list[tuple[int, int]] = []
    for index, normalized in enumerate(normalized_sentences):
        if any(start <= index < end for start, end in crossing):
            continue
        if mode is SearchMode.LITERAL:
            score = int(normalized_query in normalized)
        else:
            score = len(query_tokens & {word for word, _, _ in searchable_sentences[index][1]})
            score += sum(len(query_tokens) for phrase in phrases if phrase in normalized)
        if score:
            scored.append((-score, index))
    chosen = sorted(
        index for _, index in sorted(scored)[: SENTENCE_MATCHES_PER_HIT - len(crossing)]
    )
    return len(spans), (
        *crossing.values(),
        *(SentenceMatch(index, spans[index].text) for index in chosen),
    )
