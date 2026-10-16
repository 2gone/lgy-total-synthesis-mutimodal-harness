"""Source checks for card identity, without changing its key or evidence (E120).

A card's words are held to its cited source on the one rule every quote is held to
(`author_words`). No OCR repair or semantic matching is attempted; unavailable evidence
and a failed match remain distinct.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from tsdaf.lanes.literature.author_words import text_contains
from tsdaf.lanes.literature.characterization_methods import normalize_method


def characterization_verification(
    card: Mapping[str, Any],
    texts: Mapping[str, str] | None,
    *,
    furniture: Sequence[str] = (),
) -> str:
    """Verify the method statement and identity discriminator in one cited source.

    Ownership remains governed by the existing compound/route identity rules. The
    statement must support the host's method key, and its ion/context discriminator
    must occur in the same cited observation. Values and facts are never rewritten.
    """
    sources = [
        texts[ref["observation_id"]]
        for ref in card.get("evidence_refs", ())
        if texts is not None and ref["observation_id"] in texts
    ]
    if not sources:
        return "SOURCE_UNAVAILABLE"
    statement = card.get("method_verbatim") or ""
    if not statement or (
        card.get("method") is not None and normalize_method(statement) != card["method"]
    ):
        return "SOURCE_MISMATCH"
    parts = [statement]
    discriminator = card.get("reported_ion") or card.get("sample_context")
    if discriminator:
        parts.append(str(discriminator))
    if any(
        all(text_contains(source, part, furniture=furniture) for part in parts)
        for source in sources
    ):
        return "VERIFIED"
    return "SOURCE_MISMATCH"
