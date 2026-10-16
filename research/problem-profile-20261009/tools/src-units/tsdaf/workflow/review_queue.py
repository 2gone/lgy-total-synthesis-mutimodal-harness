"""The human review queue: which papers the Router could not route, in one file
(ledger E76 (7), E86).

A paper the Router rejected -- out of scope, or nothing to route -- and a paper it
routed with a visual it could not decide (REVIEW_REQUIRED) are written as one row each
to `review_queue.csv`, so nothing leaves the chain silently and a person has one
place to look. The columns are fixed (E76 (3c)); step 8's review fills only the last
three. The queue is a Host record: the reason is derived from the sealed routing
result, never asked of the model, and the file lives outside any run workspace so a
batch has one queue to aggregate.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tsdaf.contracts.package import PaperManifest
from tsdaf.contracts.routing import (
    ArticleRoutingResult,
    ArticleType,
    RoutingDisposition,
    TargetClass,
    VisualDecision,
)

__all__ = (
    "CORPUS_TARGET_CLASSES",
    "REVIEW_QUEUE_COLUMNS",
    "ReviewQueueRow",
    "append_review_queue",
    "corpus_exclusion",
    "needs_human_review",
    "review_queue_row",
    "review_reason",
)

# The corpus rule (Router v1.4): the Router labels every route's target and never
# drops a paper for its target; which classes enter the literature chain is decided
# here, in one place, and a paper whose routes all fall outside is queued, not run.
CORPUS_TARGET_CLASSES = frozenset(
    {TargetClass.NATURAL_PRODUCT, TargetClass.ANALOGUE, TargetClass.DRUG}
)

# Fixed on 2026-09-15 (E76 (3c)): the first eight are written when a paper is queued,
# the last three by the review (step 8), which does not change the format.
REVIEW_QUEUE_COLUMNS = (
    "paper_id",
    "doi",
    "title",
    "article_type",
    "disposition",
    "reason",
    "router_execution_id",
    "queued_at",
    "reviewed_by",
    "review_disposition",
    "reviewed_at",
)


@dataclass(frozen=True, slots=True)
class ReviewQueueRow:
    paper_id: str
    doi: str
    title: str
    article_type: str
    disposition: str
    reason: str
    router_execution_id: str
    queued_at: str

    def as_record(self) -> dict[str, str]:
        return {
            "paper_id": self.paper_id,
            "doi": self.doi,
            "title": self.title,
            "article_type": self.article_type,
            "disposition": self.disposition,
            "reason": self.reason,
            "router_execution_id": self.router_execution_id,
            "queued_at": self.queued_at,
            "reviewed_by": "",
            "review_disposition": "",
            "reviewed_at": "",
        }


def corpus_exclusion(routing: ArticleRoutingResult) -> str | None:
    """Why a routed paper stays out of the chain under the corpus rule, or None when
    at least one of its routes targets a class the corpus collects."""
    if routing.disposition is RoutingDisposition.REJECTED or not routing.route_groups:
        return None
    classes = sorted({group.target_class.value for group in routing.route_groups})
    if any(group.target_class in CORPUS_TARGET_CLASSES for group in routing.route_groups):
        return None
    return (
        f"OUT_OF_CORPUS: {len(routing.route_groups)} route group(s), every target "
        f"{' / '.join(classes)}; the corpus collects "
        + ", ".join(sorted(c.value for c in CORPUS_TARGET_CLASSES))
    )


def needs_human_review(routing: ArticleRoutingResult) -> bool:
    """REJECTED (nothing enters the chain), REVIEW_REQUIRED (the chain runs, but a
    visual the Router left UNCERTAIN wants a person) and a routed paper the corpus
    rule keeps out are queued; a routed paper in the corpus is not."""
    return (
        routing.disposition is not RoutingDisposition.ROUTED
        or corpus_exclusion(routing) is not None
    )


def review_reason(routing: ArticleRoutingResult) -> str:
    """Why the paper is queued, said from the sealed routing facts: the document kind,
    the disposition and the signal that produced it. The routing contract carries no
    free text from the model (E86 audit: not worth a contract change for the queue)."""
    uncertain = [
        d.visual_id
        for d in routing.visual_decisions
        if d.final_decision is VisualDecision.UNCERTAIN
    ]
    if routing.disposition is RoutingDisposition.REVIEW_REQUIRED:
        return (
            f"REVIEW_REQUIRED: {len(uncertain)} visual(s) the Router left UNCERTAIN "
            f"({', '.join(uncertain)}); {len(routing.route_groups)} route group(s) routed"
        )
    excluded = corpus_exclusion(routing)
    if excluded is not None:
        return excluded
    kind = routing.article_type
    if kind is ArticleType.REVIEW:
        return "REVIEW: routes reported from the literature; labelled and queued, not routed"
    if kind is ArticleType.COMPANION:
        return (
            "COMPANION: not the article itself (supporting information, a correction, a "
            "commentary or another file under this title); for the intake registry"
        )
    if kind is ArticleType.NOT_CHEMISTRY:
        return "NOT_CHEMISTRY: not about chemical synthesis"
    return (
        f"REJECTED as {kind.value}: the Router found no route the authors executed "
        f"({len(routing.visual_decisions)} visual(s), none forwarded)"
    )


def review_queue_row(
    routing: ArticleRoutingResult,
    manifest: PaperManifest,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ReviewQueueRow | None:
    """The queue row for a routing result, or None when the paper was routed."""
    if not needs_human_review(routing):
        return None
    identity = manifest.identity
    return ReviewQueueRow(
        paper_id=routing.paper_id,
        doi=(identity.doi if identity is not None and identity.doi else ""),
        title=(identity.title if identity is not None and identity.title else ""),
        article_type=routing.article_type.value,
        disposition=routing.disposition.value,
        reason=review_reason(routing),
        router_execution_id=routing.execution_id,
        queued_at=now().isoformat(timespec="seconds"),
    )


def append_review_queue(path: Path, row: ReviewQueueRow) -> bool:
    """Append `row` to the queue at `path`, creating the file with its header; False
    when the same paper under the same Router execution is already queued (a rerun
    or a resume writes nothing twice). A file whose header is not the fixed one is
    refused rather than extended."""
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != REVIEW_QUEUE_COLUMNS:
                raise ValueError(f"{path} is not a review queue: its columns differ")
            for existing in reader:
                if (
                    existing["paper_id"] == row.paper_id
                    and existing["router_execution_id"] == row.router_execution_id
                ):
                    return False
        with path.open("a", encoding="utf-8", newline="") as handle:
            csv.DictWriter(handle, fieldnames=REVIEW_QUEUE_COLUMNS).writerow(row.as_record())
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_QUEUE_COLUMNS)
        writer.writeheader()
        writer.writerow(row.as_record())
    return True
