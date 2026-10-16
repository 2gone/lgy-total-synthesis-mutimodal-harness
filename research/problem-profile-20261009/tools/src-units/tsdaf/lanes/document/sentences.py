"""Conservative deterministic sentence spans over one canonical text block."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ("SentenceSpan", "split_sentence_spans")

_ABBREVIATIONS = frozenset(
    {
        "approx",
        "aq",
        "ca",
        "cf",
        "cat",
        "conc",
        "e.g",
        "eq",
        "equiv",
        "et al",
        "etc",
        "ex",
        "fig",
        "figs",
        "i.e",
        "min",
        "mol",
        "no",
        "p",
        "pp",
        "ref",
        "refs",
        "rt",
        "sat",
        "scheme",
        "sec",
        "soln",
        "tab",
        "table",
        "temp",
        "vol",
        "vs",
        "wt",
    }
)
_TERMINATOR = re.compile(r"[.!?](?=[\s\"')\]]|$)")
_TRAILING_TOKEN = re.compile(r"([A-Za-z.]+)\.?$")
# A single letter right after a number (with at most a degree sign and
# whitespace between) is a unit (degrees C, M, N, ...), not an abbreviation:
# the sentence ending here is the normal case, not the exception.
_UNIT_AFTER_NUMBER = re.compile(r"[0-9]\s*°?\s*$")
_CLOSERS = "\"')]"
_MIN_SENTENCE = 12


@dataclass(frozen=True, slots=True)
class SentenceSpan:
    """One immutable exact slice using offsets local to its canonical block."""

    char_start: int
    char_end: int
    text: str

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("sentence span must have positive block-local extent")
        if self.char_end - self.char_start != len(self.text):
            raise ValueError("sentence span length does not match text")


def _ends_with_abbreviation(text: str) -> bool:
    match = _TRAILING_TOKEN.search(text)
    if match is None:
        return False
    raw = match.group(1).rstrip(".")
    if raw.casefold() in _ABBREVIATIONS:
        return True
    if not raw.isalpha():
        return False
    if len(raw) == 1:
        return not _UNIT_AFTER_NUMBER.search(text[: match.start(1)])
    return len(raw) <= 4 and raw[0].isupper() and not raw.isupper()


def _is_decimal_point(text: str, index: int) -> bool:
    before = text[index - 1] if index else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    return before.isdigit() and after.isdigit()


def _trimmed(text: str, start: int, end: int) -> SentenceSpan:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return SentenceSpan(char_start=start, char_end=end, text=text[start:end])


def split_sentence_spans(text: str) -> tuple[SentenceSpan, ...]:
    """Return ordered immutable sentence slices without rewriting source text."""

    if not isinstance(text, str):
        raise TypeError("text must be a str")
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _TERMINATOR.finditer(text):
        index = match.start()
        if text[index] == "." and _is_decimal_point(text, index):
            continue
        if _ends_with_abbreviation(text[start:index]):
            continue
        end = index + 1
        while end < len(text) and text[end] in _CLOSERS:
            end += 1
        if len(text[start:end].strip()) < _MIN_SENTENCE:
            continue
        spans.append((start, end))
        start = end
    tail = text[start:]
    if tail.strip():
        tail_end = len(text.rstrip())
        if spans and len(tail.strip()) < _MIN_SENTENCE:
            previous_start, _ = spans[-1]
            spans[-1] = (previous_start, tail_end)
        else:
            spans.append((start, tail_end))
    return tuple(_trimmed(text, start, end) for start, end in spans)
