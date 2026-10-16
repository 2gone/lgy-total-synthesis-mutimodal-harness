"""Host-side deterministic resolution of discrete lane selections into exact extents.

The private lanes ask a provider only for values it can actually see and enumerate:
a half-open sentence range within one canonical block, and a normalized `[0, 1000]`
box over one visual source. Turning those into Unicode character offsets and original
image pixels is arithmetic the Host owns, so a provider can no longer put the run into
an unrecoverable state by mispredicting an absolute index.

Every function here is pure and deterministic: the same block text or the same source
dimensions always produce the same extent, which is what lets the request builder and
the evidence compiler agree without exchanging any state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tsdaf.lanes.document.sentences import SentenceSpan, split_sentence_spans

__all__ = (
    "NORMALIZED_COORDINATE_CEILING",
    "ResolvedPixelBox",
    "ResolvedTextSpan",
    "block_sentence_spans",
    "normalize_pixel_extent",
    "resolve_text_selection",
    "resolve_visual_selection",
)

NORMALIZED_COORDINATE_CEILING = 1000


def normalize_pixel_extent(pixel: int, dimension: int) -> int:
    """Scale one original-image pixel edge back into the normalized `[0, 1000]` space.

    This is the inverse direction of `resolve_visual_selection`, and it deliberately
    is not its exact inverse: that function widens a box outward to the enclosing
    pixel grid, so a resolved box normalizes back one unit wider on each edge it had
    to round. `tests/unit/literature/test_selection_resolution.py` pins that policy.

    Rounding is half-up and integral, so the public value never depends on float
    behaviour.
    """

    if isinstance(pixel, bool) or not isinstance(pixel, int) or pixel < 0:
        raise ValueError("pixel edge must be a nonnegative integer")
    if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension < 1:
        raise ValueError("image dimension must be a positive integer")
    if pixel > dimension:
        raise ValueError("pixel edge lies outside its image dimension")
    return (2 * pixel * NORMALIZED_COORDINATE_CEILING + dimension) // (2 * dimension)


@dataclass(frozen=True, slots=True)
class ResolvedTextSpan:
    """One exact block-local half-open character extent and the text it covers."""

    char_start: int
    char_end: int
    text: str

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("resolved text span must have positive block-local extent")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("resolved text span length does not match its text")


@dataclass(frozen=True, slots=True)
class ResolvedPixelBox:
    """One exact original-image pixel box with positive area."""

    x_min_px: int
    y_min_px: int
    x_max_px: int
    y_max_px: int

    def __post_init__(self) -> None:
        if self.x_min_px < 0 or self.y_min_px < 0:
            raise ValueError("resolved pixel box must start inside the image")
        if self.x_max_px <= self.x_min_px or self.y_max_px <= self.y_min_px:
            raise ValueError("resolved pixel box must have positive area")


def block_sentence_spans(raw_text: str) -> tuple[SentenceSpan, ...]:
    """Return the deterministic sentence slices a provider may reference by index."""

    if type(raw_text) is not str:
        raise TypeError("raw_text must be a str")
    return split_sentence_spans(raw_text)


def resolve_text_selection(
    *,
    raw_text: str,
    start_sentence: int,
    end_sentence: int,
) -> ResolvedTextSpan:
    """Resolve one half-open sentence range into exact block-local character offsets."""

    spans = block_sentence_spans(raw_text)
    if isinstance(start_sentence, bool) or isinstance(end_sentence, bool):
        raise TypeError("sentence indices must be integers")
    if not isinstance(start_sentence, int) or not isinstance(end_sentence, int):
        raise TypeError("sentence indices must be integers")
    if not spans:
        raise ValueError("block carries no referenceable sentence")
    if start_sentence < 0 or end_sentence <= start_sentence or end_sentence > len(spans):
        raise ValueError(
            f"text selection [{start_sentence}, {end_sentence}) is not a sentence range "
            f"inside its block: supplied sentence indices are 0..{len(spans) - 1}; "
            f"require 0 <= start_sentence < end_sentence <= {len(spans)}. "
            "The end is exclusive; equal endpoints select no text. Do not recount sentences."
        )
    char_start = spans[start_sentence].char_start
    char_end = spans[end_sentence - 1].char_end
    return ResolvedTextSpan(
        char_start=char_start,
        char_end=char_end,
        text=raw_text[char_start:char_end],
    )


def resolve_visual_selection(
    *,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    dimensions: tuple[int, int],
) -> ResolvedPixelBox:
    """Scale one normalized `[0, 1000]` box into exact original-image pixels.

    The lower edges floor and the upper edges ceil, so a resolved crop never trims
    content the provider meant to include.
    """

    if not isinstance(dimensions, tuple) or len(dimensions) != 2:
        raise TypeError("dimensions must be a (width, height) tuple")
    width, height = dimensions
    for value in (width, height):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("image dimensions must be positive integers")
    for value in (x0, y0, x1, y1):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("normalized coordinates must be integers")
        if value < 0 or value > NORMALIZED_COORDINATE_CEILING:
            raise ValueError("normalized coordinates must lie within [0, 1000]")
    if x0 >= x1 or y0 >= y1:
        raise ValueError("normalized selection must have positive area")
    return ResolvedPixelBox(
        x_min_px=math.floor(x0 * width / NORMALIZED_COORDINATE_CEILING),
        y_min_px=math.floor(y0 * height / NORMALIZED_COORDINATE_CEILING),
        x_max_px=math.ceil(x1 * width / NORMALIZED_COORDINATE_CEILING),
        y_max_px=math.ceil(y1 * height / NORMALIZED_COORDINATE_CEILING),
    )
