"""The one vocabulary for what a fusion unit's sealed decision says (ledger E52).

The fusion runtime writes the decision, two receipts validate it and publication reads
it; each had its own literals until 2026-09-12, and one of them (windows had to be
CLOSED, which nothing produced) once listed every unit of a published paper as
unresolved (ledger E46). They now share these names.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

__all__ = (
    "COMPLETE_DECISIONS",
    "UNIT_DECISIONS",
    "UnitDecision",
    "decision_for_delivery",
    "delivered_complete",
    "paper_completeness",
)

UnitDecision = Literal["DEEPEN", "STOPPED", "PARTIAL"]
# DEEPEN: the unit handed work off and stopped -- historical, read on old receipts only;
# nothing hands work off since reaction windows were retired (2026-09-23). STOPPED: the
# model finished with a COMPLETE delivery. PARTIAL: it finished PARTIAL or the host
# closed it out.
UNIT_DECISIONS: frozenset[str] = frozenset({"DEEPEN", "STOPPED", "PARTIAL"})
# CLOSED is the historical spelling of a complete unit, kept so old receipts still read.
COMPLETE_DECISIONS: frozenset[str] = frozenset({"STOPPED", "CLOSED"})


def decision_for_delivery(disposition: str | None) -> UnitDecision:
    """The decision a finish records: COMPLETE delivery stops the unit, anything else
    (a PARTIAL delivery, the host's close-out) leaves it partial."""
    return "STOPPED" if disposition == "COMPLETE" else "PARTIAL"


def delivered_complete(decision: str | None) -> bool:
    return decision in COMPLETE_DECISIONS


def paper_completeness(
    *,
    group_decisions: Iterable[tuple[str | None, str | None]],
    window_decisions: Iterable[tuple[str | None, str | None]],
    excluded_groups: int,
    pending_units: int,
    prior: str | None = None,
    partial_receipts: int = 0,
) -> Literal["COMPLETE", "PARTIAL"]:
    """What a published result's `completeness` says, derived from the terminal state
    of the units that made it (ledger E76 (4), E83) -- written here, beside the decision
    vocabulary it reads, and nowhere else.

    COMPLETE means every route group and every window it ran (windows are historical:
    retired 2026-09-23, `window_decisions` is only ever nonempty for an older receipt)
    delivered COMPLETE, none failed, no group was left out of the publication and no
    handoff is still pending;
    a later revision stays COMPLETE only while it was, and every receipt it adds is a
    COMPLETE delivery with nothing withheld. WAIT_FOR_SI items do not make a result
    PARTIAL (00 §11): a paper can have expressed the boundary of what its main text
    proves and still wait for the supplement.
    """
    if prior == "PARTIAL" or partial_receipts:
        return "PARTIAL"
    if excluded_groups or pending_units:
        return "PARTIAL"
    for decision, failure in (*group_decisions, *window_decisions):
        if failure or not delivered_complete(decision):
            return "PARTIAL"
    return "COMPLETE"
