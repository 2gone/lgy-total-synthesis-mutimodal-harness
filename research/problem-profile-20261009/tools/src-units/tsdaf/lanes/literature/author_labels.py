"""One normalization of an author's compound label (ledger E66).

A compound's label is the author's numbering as written -- "14a", "14 a", "14′",
"(±)-14", "S3". Three things about it are typography or description rather than
identity, and only those three are folded here:

1. whitespace inside the label ("14 a" is "14a");
2. the prime character (every prime is ′, a double prime is ″) -- the prime itself is
   part of the label: 14′ and 14 are two compounds;
3. a leading optical or racemic sign, "(±)-", "(+)-", "(−)-", "(-)-" -- the number is
   the same compound, the sign describes a sample of it (the sign is kept in `names`,
   and two records whose signs disagree are not merged: the projection records a
   conflict instead).

Everything else is kept verbatim. A word prefix the author chose (ent-, epi-, iso-)
names a different compound; the SI's "S3" is its own namespace; case is the author's.
Anything beyond this list would be the Host guessing what the author meant, so the
list grows only from corpus counts, as the condition key table does.

The three places that compare labels -- key generation, same-label merging, label
search -- all call `canonical_author_label`; a test holds the rules to this module.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence

__all__ = (
    "AUTHOR_LABELS_MEANING",
    "PRIME",
    "author_label_key",
    "author_label_signs_disagree",
    "canonical_author_label",
    "canonical_author_labels",
    "optical_sign",
)

# The one prime character: every way a prime is written reads as it, and TeX's
# \prime prints it (`author_words`).
PRIME = "′"
_PRIMES = {"'": PRIME, "’": PRIME, "′": PRIME, "ʹ": PRIME, "´": PRIME}
_DOUBLE_PRIME = "″"
_SIGNS = (("(±)-", "±"), ("(+)-", "+"), ("(−)-", "-"), ("(-)-", "-"))


def _folded(written: str) -> str:
    if not isinstance(written, str):
        raise TypeError("a label must be a string")
    text = unicodedata.normalize("NFKC", written)
    text = "".join(text.split())
    for raw, prime in _PRIMES.items():
        text = text.replace(raw, prime)
    text = text.replace("′′", _DOUBLE_PRIME)
    return text


def _split_sign(folded: str) -> tuple[str | None, str]:
    for prefix, sign in _SIGNS:
        if folded.startswith(prefix):
            return sign, folded[len(prefix) :]
    return None, folded


def canonical_author_label(written: str) -> str:
    """The label as a key component: spacing folded, primes unified, sign dropped."""
    _, label = _split_sign(_folded(written))
    if not label:
        raise ValueError(f"not an author label: {written!r}")
    return label


def optical_sign(written: str) -> str | None:
    """The optical or racemic sign the author wrote before the label, if any:
    "+", "-" or "±"; None when the label carries none."""
    sign, _ = _split_sign(_folded(written))
    return sign


AUTHOR_LABELS_MEANING = (
    "Author-assigned labels only, not names or explanations. For one mixture, give all "
    "component labels (9a and 9b); its identity uses the complete label set, not its first "
    "member. A single component and the mixture are different identities."
)


def canonical_author_labels(labels: Sequence[str]) -> tuple[str, ...]:
    """The full numbered identity; order, duplicate spelling and typography add no member."""
    return tuple(sorted({canonical_author_label(label) for label in labels}))


def author_label_key(labels: Sequence[str]) -> str | None:
    """Keep single-label keys stable; encode every member of a multi-label identity."""
    members = canonical_author_labels(labels)
    if not members:
        return None
    return (
        members[0]
        if len(members) == 1
        else json.dumps(members, ensure_ascii=False, separators=(",", ":"))
    )


def author_label_signs_disagree(left: Sequence[str], right: Sequence[str]) -> bool:
    """Opposite explicitly signed samples are not merged by a typography fold."""
    return any(
        canonical_author_label(a) == canonical_author_label(b)
        and optical_sign(a) is not None
        and optical_sign(b) is not None
        and optical_sign(a) != optical_sign(b)
        for a in left
        for b in right
    )
