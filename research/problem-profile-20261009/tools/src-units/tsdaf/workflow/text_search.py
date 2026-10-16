"""Find authorized text by what it says, for a task that does not yet know where to look.

Neutral mechanics over the existing deterministic index. What a task may search is what
it may read: the caller supplies the authorized block ids, and nothing outside them can
be matched, so a query can never widen a task's reach.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.tool_read import QuerySelection
from tsdaf.lanes.document.search_index import (
    InMemorySearchIndex,
    SearchMode,
    UnsupportedSearchQuery,
    match_tier,
    search_preview,
)

SEARCH_LOCATION = "search:text"
# A matching sentence is quoted up to this many characters; the read carries the rest.
MATCH_TEXT_CHARS = 400


def text_search_value(
    package: CanonicalPaperPackage,
    selection: QuerySelection,
    *,
    authorized_block_ids: Iterable[str],
    location_of: Callable[[str], str],
    figure_of: Callable[[str], str | Sequence[str] | None] = lambda _: None,
    for_merge: bool = False,
) -> dict[str, Any]:
    """Run one query and answer with where it matched, inside the authorized scope only.

    A hit is navigation: the location, and the sentences that matched, numbered as a text
    read counts them, so the next read can select a range around one or take the block.
    It does not carry the block, which a read of it delivers with a citable selection.
    A hit on a figure's caption also names where that figure reads (``figure_of``).
    ``NO_MATCHES`` is an answer, not a failure: nothing matched this query, in this mode,
    within what this task may read. It says nothing about the paper as a whole.
    """
    allowed = set(authorized_block_ids)
    try:
        hits = InMemorySearchIndex(package, included_block_ids=tuple(allowed)).search_blocks(
            selection.terms,
            page_range=None,
            section_filter=None,
            limit=selection.limit,
            mode=SearchMode(selection.mode),
        )
    except UnsupportedSearchQuery as error:
        return {
            "status": "UNSUPPORTED_QUERY",
            "mode": selection.mode,
            "reason": str(error),
            "hits": [],
        }
    return _answer(
        selection,
        searched=len(allowed),
        hits=[
            {
                "location": location_of(hit.block_id),
                "select": None,
                "match_kind": hit.match_kind,
                "matched_terms": list(hit.matched_terms),
                **({"_relevance": hit.relevance} if for_merge else {}),
                **_figure_links(figure_of(hit.block_id)),
                "section_path": list(hit.section_path),
                "sentence_count": hit.sentence_count,
                "matches": [
                    {
                        "sentence": match.sentence,
                        **(
                            {"end_sentence": match.end_sentence}
                            if match.end_sentence is not None
                            else {}
                        ),
                        **_quoted(match.text, selection),
                    }
                    for match in hit.matches
                ],
            }
            for hit in hits
        ],
    )


def _figure_links(locations: str | Sequence[str] | None) -> dict[str, Any]:
    if not locations:
        return {}
    unique = list(dict.fromkeys([locations] if isinstance(locations, str) else locations))
    return ({"figure_location": unique[0]} if len(unique) == 1
            else {"figure_locations": unique})


def merged_search(found: Sequence[dict[str, Any]], selection: QuerySelection) -> dict[str, Any]:
    """Merge host-ranked answers; excluded text neither ranks nor consumes the limit.

    Reuse the index's relevance, before snippets are shortened. A stable sort keeps
    source and block order only for ties. Host ranking never reaches the tool reply.
    """
    hits = sorted(
        (hit for value in found for hit in value["hits"]),
        key=lambda hit: hit["_relevance"],
        reverse=True,
    )
    tier = max((match_tier(hit["_relevance"]) for hit in hits), default=None)
    hits = [hit for hit in hits if match_tier(hit["_relevance"]) == tier]
    return _answer(
        selection,
        searched=sum(value["searched"] for value in found),
        hits=[
            {key: value for key, value in hit.items() if key != "_relevance"}
            for hit in hits[: selection.limit]
        ],
    )


def _answer(
    selection: QuerySelection, *, searched: int, hits: list[dict[str, Any]]
) -> dict[str, Any]:
    # A scoped miss is not evidence of absence. Do not search the excluded bibliography
    # just to tell the model that the same number also exists somewhere it cannot read.
    return {
        "status": "MATCHES" if hits else "NO_MATCHES",
        "mode": selection.mode,
        "terms": selection.terms,
        "searched": searched,
        **(
            {
                "reason": "No matches within this task's readable scope; not evidence of absence "
                "from the whole document."
            }
            if not hits
            else {}
        ),
        "hits": hits,
    }


def _quoted(text: str, selection: QuerySelection) -> dict[str, str]:
    if len(text) <= MATCH_TEXT_CHARS:
        return {"text": text}
    return {
        "text": search_preview(
            text, selection.terms, SearchMode(selection.mode), max_chars=MATCH_TEXT_CHARS
        ),
        "rendering": "search_preview",
    }
