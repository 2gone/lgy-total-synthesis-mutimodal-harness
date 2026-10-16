"""The page's own printing, where normalization left it inside the running text.

A normalizer types page headers, footers and numbers as their own blocks, and the
package's clean view drops them: 27 of decumbenine_B's 123, 107 of alchivemycin_A's 176.
That works whenever the typing works. On the hyperfirin supplement it does not -- that
document came back with three block types in all, everything else PARAGRAPH, and the
publisher's footer sits inside 73 of the 154 blocks, sometimes at the end of a sentence
and sometimes in the middle of one where a page broke.

Block-granular suppression cannot reach that: the furniture is not a block. And a rule
that knows what a footer looks like is a rule per publisher, which is the same mistake
as a rule per paper. What makes furniture furniture is structural: one string, in many
blocks of one document, always in the same place. That is what this looks for, so it
works on a corpus nobody has seen.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from tsdaf.contracts.package import CanonicalPaperPackage

# Bumped whenever what this module removes or rejoins changes, so a model stage whose
# input is the cleaned text carries it in its cache identity and does not keep serving an
# answer computed over text it would no longer produce.
PAGE_FURNITURE_VERSION = "2"

# How far back to look for a repeated tail. Long enough that a shared sentence ending
# ("...under argon.") cannot reach it, short enough to catch a footer of ordinary length.
_WINDOW = 40
# What fraction of a document's blocks must end the same way before it is the page's
# printing rather than something the author wrote.
_SHARE = 5
_MINIMUM = 3
# A page number runs in front of its footer and changes on every page, so it is never
# part of the repeated string; it comes off with the footer it belongs to.
_PAGE_NUMBER = re.compile(r"(?:^|\s)\d{1,4}\s*$")
_SPACES = re.compile(r"[ \t]{2,}")
# A page prints a line or two, not a paragraph. Without a bound, the backward growth
# keeps going while the blocks happen to agree, and on a document whose paragraphs end
# alike it would take the author's words with it.
_LONGEST = 200
_LEADING_PUNCTUATION = re.compile(r"^[\s.,;:!?)\]]+")


def page_furniture(blocks: Sequence[tuple[str, str]]) -> str | None:
    """The one string this document prints on its pages, or None if it prints none.

    Found by repetition and position, never by content. The window that finds it is
    short; the string returned is the longest suffix every block carrying it shares,
    because a footer is longer than the window and it all has to come off.
    """
    tails: dict[str, int] = {}
    for _, text in blocks:
        body = text.strip()
        if len(body) >= _WINDOW:
            tails[body[-_WINDOW:]] = tails.get(body[-_WINDOW:], 0) + 1
    if not tails:
        return None
    tail, count = max(tails.items(), key=lambda item: item[1])
    if count < max(_MINIMUM, len(blocks) // _SHARE):
        return None
    sharing = [text.strip() for _, text in blocks if text.strip().endswith(tail)]
    shortest = min(len(body) for body in sharing)
    longest = tail
    for size in range(len(tail) + 1, min(shortest, _LONGEST) + 1):
        candidate = sharing[0][-size:]
        if not all(body.endswith(candidate) for body in sharing):
            break
        longest = candidate
    # A footer never begins with the punctuation that ended the sentence before it. When
    # every block on a page ends its last sentence, the backward growth reaches that
    # full stop and would take it along, leaving the author's sentence unterminated.
    return _LEADING_PUNCTUATION.sub("", longest).strip() or None


@dataclass(frozen=True, slots=True)
class FurnitureRemoval:
    """The block as the reader should see it, and every string taken out to get there.

    `removed` carries offsets into the text as it arrived, so a removal can be shown
    beside the original without re-deriving it. It is what makes this removal auditable
    without a model call: the detector decides by repetition, which is a judgement about
    a document rather than a fact about a publisher, and a judgement that leaves no
    record is one nobody can check after it is wrong.
    """

    text: str
    removed: tuple[tuple[int, str], ...]


def remove_page_furniture(text: str, furniture: str | None) -> FurnitureRemoval:
    """This block without the page's printing, and the account of what that cost.

    Every occurrence, not only the last: a block that spans two pages carries the footer
    in the middle as well, which is what a page break inside a paragraph looks like once
    the columns are flattened. The page number in front of each occurrence comes off
    with it, and so does one left at the very end of the block. Nothing else is touched
    -- the only operation here is removal, so every word that remains is a word the
    author wrote, in the order they wrote it.
    """
    if not furniture:
        return FurnitureRemoval(text=text, removed=())
    parts = text.split(furniture)
    if len(parts) == 1:
        return FurnitureRemoval(text=text, removed=())
    kept: list[str] = []
    removed: list[tuple[int, str]] = []
    cursor = 0
    for index, part in enumerate(parts):
        number = _PAGE_NUMBER.search(part)
        if number is None:
            kept.append(part)
        else:
            kept.append(part[: number.start()])
            removed.append((cursor + number.start(), number.group()))
        cursor += len(part)
        if index < len(parts) - 1:
            removed.append((cursor, furniture))
            cursor += len(furniture)
    return FurnitureRemoval(text=_SPACES.sub(" ", "".join(kept)).strip(), removed=tuple(removed))


def cleaned_block_text(text: str, furniture: str | None) -> str:
    """The cleaned text alone, for a caller with no use for the account of it."""
    return remove_page_furniture(text, furniture).text


# --- what the page break did to the paragraph it fell inside ------------------------

# A sentence has ended when its last character is terminal punctuation, allowing for a
# closing quote or bracket after it.
_SENTENCE_ENDS = re.compile(r"[.!?:;]['\")\]]?\s*$")
# What the supplement's text-layer extraction sets down after the end of a sentence: the
# subscripts of `Et2O` and `C17H25O3` arriving late as ` f 2` and ` 17 25 3`, after the
# full stop they belong before. There is no OCR in the chain: pdfplumber reads the PDF's
# text layer and groups characters into lines by their y-coordinate, and a subscript --
# smaller font, lower baseline -- forms a small line of its own, placed after the main
# one. Only the five hand-made supplements of 2026-09-08 were read that way; a supplement
# goes through MinerU like the main text, whose packages mark subscripts (E148). These
# tokens are the author's content, never furniture. Short tokens, up to three characters,
# never a word. Stripped only to ask whether the sentence ended -- the text itself is not
# touched, here or anywhere else.
_LATE_SUBSCRIPTS = re.compile(r"(?:\s+[A-Za-z0-9]{1,3}[.)\]]?){1,6}\s*$")
# A citation marker printed as a superscript and flattened onto the full stop before it:
# `in accordance with the literature.1`, `more material" [1]`, `in this system.[7c]`.
_LATE_CITATION = re.compile(r"""(?<=[.!?"'\u201d])\s*\[?\d{1,3}[a-z]?\]?\s*$""")
# The normalizer's own markup around a superscript or subscript, `3-position.<sup>6</sup>`:
# not text, and never what a sentence ends with.
_MARKUP = re.compile(r"</?su[bp]>")
# How the second half of a cut sentence begins: mid-word, mid-number, mid-clause. A new
# sentence, a heading, a table row or a compound's title begins with a capital.
_CONTINUES = re.compile(r"^(?:[a-z]|\d)")
# Only running text is rejoined. A table row, a caption, a heading typed as its own block
# ends without a full stop for reasons of its own, and the front matter that closes the
# first page of every paper -- affiliations, dates received -- never had one.
_PARAGRAPH = "PARAGRAPH"
# A numeric fragment may finish prose; a standalone equation/assignment is not a
# continuation merely because the previous block lacks punctuation.
_FORMULA_TAIL = re.compile(r"^\s*\${1,2}\s*[+−-]?\s*\d")
# A supplement's page number printed on its own -- `S7`, `12` -- and typed PARAGRAPH
# because the normalizer typed nothing else. On three real supplements it was the last
# "paragraph" of nearly every page, and 144 of 154 joins spliced it to the next page's
# first sentence. It is not a paragraph and is looked past, so the paragraph before it is
# the one whose ending is judged; the same on the next page's first line.
_PAGE_MARKER = re.compile(r"^\s*S?\d{1,4}\s*$")
_HAS_LETTER = re.compile(r"[A-Za-z]")


def is_page_marker(text: str) -> bool:
    """Whether this block is the page's own number, or a line with no words at all --
    a rule of the crystallographic tables, a run of subscripts the extraction set down late.
    Asked by the rejoin below and by the reading-units request, so that what one looks
    past the other does not show."""
    body = text.strip()
    return bool(_PAGE_MARKER.match(body)) or not _HAS_LETTER.search(body)


@dataclass(frozen=True, slots=True)
class PageBreakJoin:
    """Two blocks that were one paragraph until the page ended between them."""

    upper: str
    lower: str


def sentence_finished(text: str) -> bool:
    """Whether this text ends a sentence, once the extraction's late subscripts are set
    aside -- set aside for the judgement only; they are the author's text and stay."""
    body = _MARKUP.sub("", text).strip()
    if _SENTENCE_ENDS.search(body):
        return True
    return bool(_SENTENCE_ENDS.search(_LATE_SUBSCRIPTS.sub("", _LATE_CITATION.sub("", body))))


# A footnote or a list item typed as a paragraph begins with its number and a capital:
# `3 Diastereomers were separable`. A continuation that begins with a digit does not --
# `3-position of the isoquinoline`, `0 °C` -- so this, and only this, is not a
# continuation among the digit-led openings.
_NUMBERED_ITEM = re.compile(r"^\d{1,3}\s+[A-Z]")
# Running text is at least this long. The front matter of a first page -- an affiliation,
# `Supporting Information` -- is typed PARAGRAPH, ends without a full stop, and is not a
# paragraph anyone cut (E132's measurement needed a floor before its numbers meant
# anything; its 120 kept out a real 116-character paragraph cut by a table). Below it,
# a line that does not finish a sentence and begins with a capital -- `Scheme 2` typed
# as a paragraph -- is layout, looked past when the continuation is sought.
PROSE_CHARS = 80
# ...and reads as running text: this many words of letters. An author line, an ORCID,
# a "(PDF)" entry of the supporting-information list are long enough and are not.
PROSE_WORDS = 8
_WORD = re.compile(r"\b[a-z]{2,}\b")
# A line of the paper's apparatus rather than its text: an e-mail address, an ORCID, a
# link, the supporting-information list's "(PDF)". Long enough and wordy enough to
# pass for prose, and never a paragraph anyone cut.
_APPARATUS = re.compile(r"@|orcid\.org|https?://|\((?:PDF|ZIP|XLSX?|CIF|MOL|TXT|CSV)\)\s*$")


def is_prose(text: str) -> bool:
    """Whether this block is running text: long enough, made of words, and not a line
    of the paper's apparatus."""
    body = text.strip()
    return (
        len(body) >= PROSE_CHARS
        and len(_WORD.findall(body)) >= PROSE_WORDS
        and not _APPARATUS.search(body)
    )


def _layout_line(body: str) -> bool:
    """A caption or a heading the normalizer typed as a paragraph: short, and not a
    sentence. Looked past when a continuation is sought, and never an upper half."""
    return len(body) < PROSE_CHARS and not sentence_finished(body)


@dataclass(frozen=True, slots=True)
class Truncation:
    """A paragraph that ends mid-sentence and has no continuation in the package."""

    block_id: str


def paragraph_joins(
    blocks: Sequence[tuple[str, int, str, str]],
) -> tuple[tuple[PageBreakJoin, ...], tuple[Truncation, ...]]:
    """Every paragraph cut in two, wherever the cut fell -- and every one cut with its
    other half lost.

    `page_break_joins` below finds the pairs a page break made, and the reading-units
    request is built on it. E132 found the other kind: on decumbenine_B a paragraph is
    cut by `Scheme 2` on the same page, and its second half -- "3-position of the
    isoquinoline, other mechanisms must be operating" -- reads as a whole, confident
    sentence whose premise ("the trimethylsilyl substituent was **not** incorporated")
    is in the first. Measured with this rule on the seven packages here: the page-break
    pairs, plus four more on the main texts of hyperfirin and decumbenine_B, each a
    sentence cut by a scheme, a table or a caption; and the front-matter lines the
    prose floor keeps out.

    The rule is E132's: both sides speak -- the earlier paragraph does not finish its
    sentence, the next paragraph in reading order begins mid-way -- and what stands
    between them (a caption, a table, a page number, an empty block, a page of figures)
    is looked past, not joined. A paragraph that does not finish and whose next
    paragraph begins a new sentence has lost its other half upstream (E132's second
    example: "3-phenylisoquinoline, and **not**" ends a page and nothing continues it);
    that is reported, not repaired.
    """
    source_positions = {block[0]: index for index, block in enumerate(blocks)}
    paragraphs = [
        (block_id, text.strip(), block_type)
        for block_id, _, block_type, text in blocks
        if block_type in {_PARAGRAPH, "DISPLAY_EQUATION"}
        and text.strip()
        and (block_type == "DISPLAY_EQUATION" or not is_page_marker(text.strip()))
    ]
    joins: list[PageBreakJoin] = []
    cut: list[Truncation] = []
    for index, (block_id, body, kind) in enumerate(paragraphs):
        if kind != _PARAGRAPH or _layout_line(body) or sentence_finished(body):
            continue
        joined = False
        for lower_index, (lower_id, lower, lower_kind) in enumerate(
            paragraphs[index + 1 :], start=index + 1
        ):
            # An immediately following display equation may be the extracted end of
            # this unfinished sentence (LTMP's "...30 minutes at" / "-78 °C.").
            # Do not jump past another paragraph to attach an unrelated equation.
            if lower_kind == "DISPLAY_EQUATION":
                if (
                    lower_index == index + 1
                    and source_positions[lower_id] == source_positions[block_id] + 1
                    and _FORMULA_TAIL.match(lower)
                    and sentence_finished(lower.strip("$ \n\r"))
                    and not re.search(r"[=+*/×÷]", lower.lstrip("$ \n\r+-−"))
                ):
                    joins.append(PageBreakJoin(upper=block_id, lower=lower_id))
                    joined = True
                break
            if _CONTINUES.match(lower) and not _NUMBERED_ITEM.match(lower):
                joins.append(PageBreakJoin(upper=block_id, lower=lower_id))
                joined = True
                break
            if _layout_line(lower):
                continue
            break
        # A join needs no more than both halves speaking -- a line of NMR data cut by
        # the page is joined like a sentence. A loss is reported only of running text:
        # an author line or an ORCID ends without a full stop and lost nothing.
        if not joined and is_prose(body):
            cut.append(Truncation(block_id=block_id))
    return tuple(joins), tuple(cut)


@dataclass(frozen=True, slots=True)
class PackageContinuity:
    """Layout candidates, not scientific evidence of continuity or missing text.

    `truncated` is the historical name for unpaired, unpunctuated prose-like tails;
    complete titles can also meet that heuristic. Never present it as source absence.
    """

    partners: Mapping[str, tuple[str, ...]]
    truncated: frozenset[str]


def package_continuity(package: CanonicalPaperPackage) -> PackageContinuity:
    """Compute layout candidates once for context delivery and exact quote matching.

    Paired blocks retain independent identities; pairing does not establish a sentence
    or experimental relationship. Consumers must interpret the delivered source.
    """
    furniture = page_furniture([(b.block_id, b.raw_text) for b in package.blocks])
    joins, cut = paragraph_joins(
        [
            (
                b.block_id,
                b.page_index,
                b.block_type.value,
                remove_page_furniture(b.raw_text, furniture).text,
            )
            for b in package.blocks
        ]
    )
    partners: dict[str, tuple[str, ...]] = {}
    for join in joins:
        partners[join.upper] = (*partners.get(join.upper, ()), join.lower)
        partners[join.lower] = (*partners.get(join.lower, ()), join.upper)
    return PackageContinuity(partners=partners, truncated=frozenset(t.block_id for t in cut))


def page_break_joins(
    blocks: Sequence[tuple[str, int, str, str]],
) -> tuple[PageBreakJoin, ...]:
    """The paragraphs a page break cut in two, as pairs of block ids to read as one.

    `blocks` are `(block_id, page_index, block_type, text)` in reading order, the text
    already without its furniture. The one place a rejoin has a single candidate is the
    page boundary: the last paragraph on a page that does not finish its sentence, and
    the first paragraph on the next page that begins mid-way. Measured on the seven
    packages here, that finds nine of seventy boundaries, every one the sentence the
    page cut, including the one a real run reported as `UNREAD_SOURCE` -- "truncated
    mid-sentence at the bottom of the page" -- because that is exactly what it was. On
    the five real supplements of the intake corpus, where the normalizer typed no page
    number as such and a bare `S7` closes nearly every page, looking past the markers
    turns 144 false pairs of 154 into 148 pairs of 148 that read as one sentence.

    Pages a figure fills contribute no paragraph and are looked past, so a paragraph cut
    by a page break with a scheme page between its halves is still one pair.
    """
    by_page: dict[int, list[tuple[str, str]]] = {}
    for block_id, page_index, block_type, text in blocks:
        body = text.strip()
        if block_type != _PARAGRAPH or not body or is_page_marker(body):
            continue
        by_page.setdefault(page_index, []).append((block_id, text))
    pages = sorted(by_page)
    joins: list[PageBreakJoin] = []
    for upper_page, lower_page in zip(pages, pages[1:], strict=False):
        upper_id, upper_text = by_page[upper_page][-1]
        lower_id, lower_text = by_page[lower_page][0]
        if not sentence_finished(upper_text) and _CONTINUES.match(lower_text.strip()):
            joins.append(PageBreakJoin(upper=upper_id, lower=lower_id))
    return tuple(joins)
