"""Closed-world tables the document layer keeps about the world (ledger E76 (1) 3).

One table here so far: the journals that publish a paper's experimental part in the
main text and no supporting information. A paper of such a journal whose main text
has an experimental part registers its SI as NOT_APPLICABLE; every other paper without
a paired SI is NONE -- the table is the only way out of NONE, and a journal not in it
is treated as publishing SI. The table only grows; every row names where the claim
comes from (the journal's policy, or at least three papers of the measured corpus).

Spellings differ between sources ("Chemistry – A European Journal" with an en dash,
"Chemistry - A European Journal" with a hyphen, a leading "The"); `fold_journal`
brings them to one key.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ("SI_IN_MAIN_TEXT_JOURNALS", "fold_journal", "journal_publishes_si_in_main_text")

_DASHES_RE = re.compile(r"[‐-―−]")


def fold_journal(name: str) -> str:
    """A journal name as the table keys it: NFKC, lower case, one kind of dash, one
    space between words, no leading article."""
    text = unicodedata.normalize("NFKC", name).lower()
    text = _DASHES_RE.sub("-", text)
    text = " ".join(text.split())
    return text.removeprefix("the ").strip()


# key -> where the claim comes from. First version from the 200-paper measurement
# (M1: the share of papers whose main text has an experimental-section heading).
SI_IN_MAIN_TEXT_JOURNALS: dict[str, str] = {
    "journal of organic chemistry": (
        "ACS JOC prints the Experimental Section in the article; M1 2026-09-14: 5 of 6 "
        "measured papers have the heading in the main text"
    ),
    "chemistry - a european journal": (
        "Wiley Chem. Eur. J. full papers print the Experimental Section; M1 2026-09-14: "
        "6 of 7 measured papers have the heading in the main text"
    ),
}


def journal_publishes_si_in_main_text(journal: str) -> bool:
    return fold_journal(journal) in SI_IN_MAIN_TEXT_JOURNALS
