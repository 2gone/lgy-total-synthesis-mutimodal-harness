"""Human decisions that key a condition by the author's exact words (ledger E61).

The null-keyed conditions of published results are counted across the corpus
(`scripts/count_open_conditions.py`), clustered offline, and a person decides per
cluster whether it becomes a key and what it is called. That decision lands in two
places: the key in `condition_categories.CONDITION_KEYS`, so the next papers' model can
select it, and here -- the author's words, whitespace folded, mapped to the key -- so
the conditions already sealed take the key when their result is projected again, with
no model call. It keys the author's words, not the model's: the key is a function of
the paper. Entries are added, never removed.
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ("CONDITION_DECISIONS", "check_decisions")

# The author's words (whitespace folded) -> the key a person decided for that cluster.
CONDITION_DECISIONS: dict[str, str] = {}


def check_decisions(decisions: Mapping[str, str], keys: Mapping[str, object]) -> None:
    """A decision names a known key against words already folded; anything else is a
    table error, raised where the tables are loaded rather than found in a result.
    The keys are passed in: this module knows no table, so the table may import it."""
    for words, key in decisions.items():
        if key not in keys:
            raise ValueError(f"condition decision {words!r} names an unknown key {key!r}")
        if words != " ".join(words.split()):
            raise ValueError(f"condition decision {words!r} is not whitespace folded")
