"""Find the reference markers in a block of extracted body text.

A lane published compounds numbered 13, 14 and 19 that the paper never drew. They came
from prose like "Gaich,<sup>10</sup> Romo,<sup>11</sup> Moeller,<sup>12</sup> and our
group.<sup>13</sup>" -- reference markers the extractor left as raw markup and the Host
handed to the model without saying what they were. Three of them were read as compound
labels, and one of those was published as a synthesis target.

That was the Host under-preparing its input, not the model failing at chemistry. Whether
a number names a compound is a chemical judgement and stays the model's. What `<sup>13</sup>`
*is* in this markup is a typographic fact with one right answer, and this is the Host
answering it.

The text is never rewritten. An observation quotes `raw_text` by character offset, and a
published quote has to be the paper's own bytes, so the spans travel beside the text
rather than replacing anything in it.

The rule is about the markup, not the chemistry, which is what lets it stay honest about
its own counter-example: a superscript of digits immediately followed by a letter is an
isotope, not a reference. This corpus contains both -- 27 markers and the two of
`<sup>1</sup>H` and `<sup>13</sup>C` -- and the same rule separates them.
"""

from __future__ import annotations

import re

__all__ = ("citation_marker_spans",)

# A reference marker's content is a number or a list of them: "13", "4,5", "19,20",
# "10-12". Anything else -- a letter, a symbol -- is not one, and is left alone.
_MARKER = re.compile(r"<sup>\s*[0-9]+(?:\s*[,–—-]\s*[0-9]+)*\s*</sup>")


def citation_marker_spans(raw_text: str) -> tuple[tuple[int, int], ...]:
    """Return the half-open spans of `raw_text` that are reference markers."""

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    spans: list[tuple[int, int]] = []
    for match in _MARKER.finditer(raw_text):
        end = match.end()
        if end < len(raw_text) and raw_text[end].isalpha():
            # "<sup>13</sup>C" is carbon-13, and "<sup>1</sup>H" is a proton. A marker
            # that binds to the word after it is part of that word.
            continue
        spans.append((match.start(), end))
    return tuple(spans)
