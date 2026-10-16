"""Package-derived text boundaries shared by source reads and Fusion evidence.

This is private geometry, not a claim that adjacent blocks form one sentence or
experiment. Callers still establish authorization and actual delivery separately.
"""

from __future__ import annotations

from typing import Any

from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.lanes.document.page_furniture import page_furniture
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans

QUOTE_BINDING_VERSION = 1
_FURNITURE = frozenset({"PAGE_HEADER", "PAGE_FOOTER", "PAGE_NUMBER", "RUNNING_TEXT"})


class TextQuoteIndex:
    """One verified package's ordered text stream; never an index of readable IDs."""

    def __init__(self, package: CanonicalPaperPackage) -> None:
        self.package_sha256 = package.canonical_content_sha256
        self.blocks = {block.block_id: block for block in package.blocks}
        skipped = {
            block.block_id for block in package.clean_view.suppressed if block.reason in _FURNITURE
        }
        self.order = {
            block.block_id: i
            for i, block in enumerate(b for b in package.blocks if b.block_id not in skipped)
        }
        self.furniture = page_furniture([(b.block_id, b.raw_text) for b in package.blocks])

    def bind(self, block_id: str, start: int, end: int | None) -> dict[str, Any]:
        block = self.blocks[block_id]
        spans = block_sentence_spans(block.raw_text)
        stop = len(spans) if end is None else end
        if not (0 <= start < stop <= len(spans)):
            raise ValueError("quote binding requires an actual contiguous source range")
        return {
            "match_text": block.raw_text[spans[start].char_start : spans[stop - 1].char_end],
            "match_furniture": self.furniture,
            "quote_binding": {
                "version": QUOTE_BINDING_VERSION,
                "order": self.order.get(block_id),
                "starts_at_head": start == 0,
                "ends_at_tail": stop == len(spans),
            },
        }
