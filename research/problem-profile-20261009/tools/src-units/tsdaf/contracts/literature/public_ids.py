"""The rule every public id of a literature fact follows (ledger E66).

It lives in the contract layer because two layers mint ids by it: the lane's
`stable_keys` pass, which keys the records a projection built, and this package's
`stage_semantics`, which keys the stretches a procedure was declared in. One rule,
one place -- a second spelling of `prefix + sha256(key)[:16]` anywhere would make two
ids of one content the day either spelling changed.
"""

from __future__ import annotations

import hashlib

__all__ = ("public_id",)


def public_id(prefix: str, key: str) -> str:
    """The id a stable key derives: a prefix and the first sixteen hex digits of the
    key's sha256. The key itself travels beside it as `stable_key`."""
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:16]}"
