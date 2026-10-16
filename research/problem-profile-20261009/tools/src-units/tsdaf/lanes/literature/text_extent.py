"""Decide how much of a paper's prose one route group is allowed to read.

This replaces `seed_coherence`, whose rule was written before anyone measured a real
paper. That rule restored only the blocks bracketed between two of the Router's own
picks, on the grounds that deciding how much surrounding context a route needs is a
judgement belonging to the lane. The reasoning was sound; the premise was not. On the
five packaged papers the Router seeds 2-5 blocks, and the closure left T1 reading 8%
of the narrative on two of them -- a synthesis cannot be recovered from 16 sentences
out of 209.

The premise that failed was capacity. A whole cleaned paper here is 84-224 sentences
and 13-32K characters, which is one comfortable call for the fixed fast model, so
narrowing the *authority* was never buying head-room. It was only buying blindness.
The rule is now: the Host grants authority over everything the document cleaning kept,
and the lane still decides what within it is chemically relevant. Authority is
mechanical; relevance stays a judgement.

Which blocks are prose at all is not decided here either. `lanes.document.cleaning`
already suppresses references, running heads, page numbers and footers, and seals the
result into the canonical package as `clean_view` with a reason recorded per block.
That is the single owner of "this is not body text", and reproducing any part of it
here would create a second one.
"""

from __future__ import annotations

from dataclasses import dataclass

from tsdaf.contracts.package import CanonicalPaperPackage, CanonicalTextBlock
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans

__all__ = ("TextReadExtent", "resolve_text_read_extent")


@dataclass(frozen=True, slots=True)
class TextReadExtent:
    """The blocks one route group may read, and the Router's own picks within them."""

    ordered_block_ids: tuple[str, ...]
    anchor_block_ids: tuple[str, ...]
    # Cleaned blocks left out because nothing in them could be cited as evidence.
    unreferenceable_block_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not set(self.anchor_block_ids) <= set(self.ordered_block_ids):
            raise ValueError("a text extent must contain every Router anchor")
        if set(self.unreferenceable_block_ids) & set(self.ordered_block_ids):
            raise ValueError("an unreferenceable block cannot also be readable")


def _is_referenceable(block: CanonicalTextBlock) -> bool:
    """Only a block a lane could actually cite is worth carrying into a request."""

    return bool(block_sentence_spans(block.raw_text))


def resolve_text_read_extent(
    *,
    package: CanonicalPaperPackage,
    anchor_block_ids: tuple[str, ...],
    readable_block_ids: tuple[str, ...] | None = None,
) -> TextReadExtent:
    """Resolve the readable prose for one route group of a canonical package.

    Every route group of a paper may read all of the cleaned document's narrative
    part. Separating one target's prose from another's is not done by withholding
    text -- it is done by telling each group what it is for, which travels with the
    work unit as the Router's `RouteGroupAssignment`. Narrowing authority to keep
    targets apart would trade a real loss of coverage for a separation the assignment
    already provides.

    `readable_block_ids` is that narrative part: the cleaned blocks before the
    experimental split's cut, derived once by `reading_units.partition_clean_view`
    and handed in here so the extent does not decide the cut a second time. Without it
    (a Router run sealed before the split existed) the whole cleaned document is read,
    as before. The experimental part is not lost: the detail lane reads it on purpose.
    """

    if type(package) is not CanonicalPaperPackage:
        raise TypeError("package must be a CanonicalPaperPackage")
    if type(anchor_block_ids) is not tuple:
        raise TypeError("anchor_block_ids must be a tuple")
    if len(set(anchor_block_ids)) != len(anchor_block_ids):
        raise ValueError("Router anchor block ids must be unique")
    if readable_block_ids is None:
        readable_block_ids = package.clean_view.included_block_ids
    elif type(readable_block_ids) is not tuple or not set(readable_block_ids) <= set(
        package.clean_view.included_block_ids
    ):
        raise ValueError("readable block ids must be cleaned blocks of the package")

    blocks = {block.block_id: block for block in package.blocks}
    if any(item not in blocks for item in anchor_block_ids):
        raise ValueError("a Router anchor is not part of the canonical package")

    anchors = set(anchor_block_ids)
    candidates = set(readable_block_ids) | anchors

    readable = {item for item in candidates if _is_referenceable(blocks[item])}
    # An anchor the Router chose is carried even when cleaning or sentence
    # segmentation would otherwise drop it: refusing to read what the Router pointed
    # at would silently narrow its authority rather than widen ours.
    readable |= {item for item in anchors if _is_referenceable(blocks[item])}
    unreferenceable = candidates - readable

    ordered = tuple(block.block_id for block in package.blocks if block.block_id in readable)
    return TextReadExtent(
        ordered_block_ids=ordered,
        anchor_block_ids=tuple(item for item in ordered if item in anchors),
        unreferenceable_block_ids=tuple(
            block.block_id for block in package.blocks if block.block_id in unreferenceable
        ),
    )
