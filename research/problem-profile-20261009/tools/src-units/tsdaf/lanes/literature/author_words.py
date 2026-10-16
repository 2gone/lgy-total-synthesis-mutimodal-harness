"""The author's words: what a text prints, the one definition every quote is held to.

A task reads MinerU's text -- TeX inside and outside `$...$`, a space MinerU sets on
either side of an inline formula, a sentence the layout cut into two blocks -- and
writes what it read, sometimes rendered and sometimes copied as it stood. Three rules
used to answer "are these the author's words": the middle layer's unbounded substring,
the bounded `text_position` a card's statement (E120) and a stretch's boundary (E157)
were held to, and `stable_keys`' whitespace-only fold. One quote got three answers, and
all three compared it with an "original" the model was never shown: the middle layer
refused 20 (a3) and 26 (a4) quotes copied exactly. This module is the one answer:

* one table of what TeX markup prints: `TEX_TYPESETTING` prints nothing of its own,
  `TEX_SPACING` prints a space, `TEX_CHARACTERS` prints one character. A command not in
  it is kept as written, so it can never vanish and let a quote through; the table
  grows by counting the corpus, never by guessing;
* one fold (`_fold`) that reads a text into the characters it prints, each with its
  offset in the text as it arrived (E159), the space printed before it, and whether a
  number boundary stands before it;
* one bounded match (`text_position`): compared on `comparison_text`, bounded by what
  was printed;
* one join (`find_quote`): the two halves of one paragraph the layout cut, and only
  those the package's continuity table paired (E156), recorded on the upper half's
  binding at delivery as `continues_in`.

A parser reads `author_rendering`: the same fold, with the author's case and printed
spaces kept.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

from tsdaf.lanes.document.page_furniture import remove_page_furniture
from tsdaf.lanes.literature.author_labels import PRIME

__all__ = (
    "ESCAPED_WHITESPACE",
    "TEX_CHARACTERS",
    "TEX_SPACING",
    "TEX_TYPESETTING",
    "Quoted",
    "author_rendering",
    "author_rendering_offsets",
    "comparison_text",
    "find_quote",
    "printed_line_break",
    "text_contains",
    "text_position",
)

# Commands that set their argument in a style and print nothing of their own.
TEX_TYPESETTING = frozenset(
    """mathrm mathbf mathit mathsf mathtt mathcal mathbb mathnormal text textrm textbf
    textit textsf texttt textup textnormal emph operatorname boldsymbol bm mbox hbox rm bf
    it sf tt left right big Big bigl bigr Bigl Bigr displaystyle textstyle scriptstyle
    limits nolimits""".split()
)
# Commands that print a space and nothing else: `\,` `\;` `\:` `\!` `\ ` and the quads.
TEX_SPACING = frozenset({",", ";", ":", "!", " ", "quad", "qquad", "enspace", "thinspace"})
# Commands that print exactly one character.
TEX_CHARACTERS: Mapping[str, str] = {
    "%": "%",
    "&": "&",
    "#": "#",
    "$": "$",
    "{": "{",
    "}": "}",
    "_": "_",
    "circ": "°",
    "degree": "°",
    "textdegree": "°",
    "rightarrow": "→",
    "to": "→",
    "longrightarrow": "→",
    "leftarrow": "←",
    "leftrightarrow": "↔",
    "rightleftharpoons": "⇌",
    "times": "×",
    "pm": "±",
    "mp": "∓",
    "cdot": "·",
    "bullet": "•",
    "sim": "~",
    "approx": "≈",
    "leq": "≤",
    "le": "≤",
    "geq": "≥",
    "ge": "≥",
    "neq": "≠",
    "ne": "≠",
    "prime": PRIME,  # the label rules' one prime character
    "AA": "Å",
    "infty": "∞",
    "equiv": "≡",
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "Delta": "Δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "eta": "η",
    "theta": "θ",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "phi": "φ",
    "varphi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Omega": "Ω",
}
# A line break, tab or return a quote carries as an escape: a JSON string escaped twice
# arrives as a backslash and a letter, and is the whitespace it names -- also when a word
# follows it ("\\nThe residue"): a control word that starts with one of these letters and
# is no command of the tables above is the escape and the author's word.
ESCAPED_WHITESPACE: Mapping[str, str] = {"n": "\n", "r": "\n", "t": " "}
_COMMANDS = TEX_TYPESETTING | TEX_SPACING | frozenset(TEX_CHARACTERS)

# The HTML script tags a package may carry; any other tag is the author's text.
_HTML_SCRIPT = re.compile(r"<(/?)(sub|sup)>")
# The two dashes a minus sign is typeset as; an em dash is the author's.
_DASHES = {"–": "-", "−": "-"}
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85  ")
_WORD = re.compile(r"\w")
# A control word is ASCII letters, in the fold and here alike: `\\nδ` is an escaped line
# break and a delta, not a command named nδ.
_CONTROL_WORD = re.compile(r"\\+([A-Za-z]+)")


@dataclass(frozen=True, slots=True)
class _Char:
    """One printed character: NFKC, dash-folded, the author's case."""

    ch: str
    # Its offset in the text as it arrived (E159): a position found in the fold is a
    # position in the source, whatever the fold removed before it.
    offset: int
    # The whitespace printed before it: "" none, " " a space, "\n" a line break.
    gap: str
    # A number boundary stands before it: two digits with a printed space or a change of
    # script between them ("12 3" is not 123, "10^{3}" is not 103).
    separates: bool
    # The script it prints in: "^" raised, "_" lowered, "" on the line.
    raised: str = ""
    # Exclusive end of this character's actual source token, including its closing markup.
    end: int = 0


def _script(ch: str) -> str | None:
    """Which script a character prints in by itself: ³ is a superscript 3."""
    decomposition = unicodedata.decomposition(ch)
    if decomposition.startswith("<super>"):
        return "^"
    if decomposition.startswith("<sub>"):
        return "_"
    return None


def _escaped_whitespace(name: str) -> bool:
    """Whether the control word `name` is an escaped line break, tab or return, with the
    author's word after it when it runs on."""
    return name[0] in ESCAPED_WHITESPACE and (len(name) == 1 or name not in _COMMANDS)


def _carries_tex(text: str) -> bool:
    """A control word other than an escaped line break: TeX that arrived without its $."""
    return any(not _escaped_whitespace(m.group(1)) for m in _CONTROL_WORD.finditer(text))


@lru_cache(maxsize=4096)
def _fold(
    text: str, furniture: tuple[str, ...] = (), bare_tex_is_math: bool = True
) -> tuple[_Char, ...]:
    """The characters `text` prints, in order.

    The page's own printing is set aside first (E133, E140; `remove_page_furniture`'s
    one answer, read for what it removed so the offsets survive). Then, left to right:
    `<sub>`/`<sup>` open and close a script; `$` or `$$` opens or closes math (a text
    with no `$` that carries a control word is read as math throughout: none of 3,133
    corpus paragraphs is one, a quote copied from inside a formula is); a run of
    backslashes is one backslash; a control word is read on the tables above, and the
    whitespace after it ends it rather than printing; braces group; `^` and `_` put
    what follows in a script; `~` is a printed space in math or before a command, and
    the author's tilde anywhere else; whitespace inside math does not print. A base
    character and its combining marks are normalized (NFKC) as a unit, and the
    characters one source character unfolds into all keep its offset. A TeX spacing
    command between two digits groups them (10\\,000 is 10000).

    `bare_tex_is_math=False` reads a text with no `$` as prose whatever it carries: a
    line break the author set counts there even beside a bare command (C5).
    """
    chars: list[tuple[str, int]] = list(zip(text, range(len(text)), strict=True))
    for printing in furniture:
        current = "".join(ch for ch, _ in chars)
        cut = 0
        kept: list[tuple[str, int]] = []
        for offset, removed in remove_page_furniture(current, printing).removed:
            kept.extend(chars[cut:offset])
            cut = offset + len(removed)
        kept.extend(chars[cut:])
        chars = kept
    body = "".join(ch for ch, _ in chars)
    out: list[_Char] = []
    math = bare_tex_is_math and "$" not in body and _carries_tex(body)
    pending = ""  # whitespace printed since the last character
    spaced = False  # a TeX space since the last character
    braces: list[str | None] = []  # each open group: the script it sets, if any
    tags: list[str] = []  # open HTML script tags
    script: str | None = None  # a `^` or `_` waiting for its argument
    last: tuple[str, ...] = ()  # the script the last character printed in

    def emit(printed: str, offset: int, own: str | None = None, *, end: int | None = None) -> None:
        nonlocal pending, spaced, script, last
        level = (
            *(b for b in braces if b is not None),
            *tags,
            *((script,) if script else ()),
            *((own,) if own else ()),
        )
        for ch in printed:
            before = out[-1] if out else None
            digits = before is not None and before.ch.isdigit() and ch.isdigit()
            gap = pending or (" " if spaced and not digits else "")
            raised = level[-1] if level else ""
            out.append(
                _Char(
                    ch,
                    offset,
                    gap,
                    digits and (bool(gap) or level != last),
                    raised,
                    end if end is not None else offset + 1,
                )
            )
            pending, spaced, last = "", False, level
        script = None

    index = 0
    while index < len(chars):
        ch, offset = chars[index]
        tag = _HTML_SCRIPT.match(body, index) if ch == "<" else None
        if tag is not None:
            if tag.group(1):
                if tags:
                    tags.pop()
                    if out and out[-1].end == offset:
                        out[-1] = replace(out[-1], end=chars[tag.end() - 1][1] + 1)
            else:
                tags.append("^" if tag.group(2) == "sup" else "_")
            index = tag.end()
        elif ch == "$":
            width = 2 if body.startswith("$$", index) else 1
            if math and out and out[-1].end == offset:
                out[-1] = replace(out[-1], end=chars[index + width - 1][1] + 1)
            math = not math
            braces, script = [], None
            index += width
        elif ch == "\\":
            start = index
            while index < len(chars) and chars[index][0] == "\\":
                index += 1
            if index == len(chars):
                emit("\\", offset)
                continue
            name = chars[index][0]
            index += 1
            token_end = chars[index - 1][1] + 1
            if name.isascii() and name.isalpha():
                while (
                    index < len(chars) and chars[index][0].isascii() and chars[index][0].isalpha()
                ):
                    name += chars[index][0]
                    index += 1
                if _escaped_whitespace(name):
                    # The escape is its first letter; the rest is the author's word.
                    index -= len(name) - 1
                    if not math:
                        escaped = ESCAPED_WHITESPACE[name[0]]
                        pending = "\n" if escaped == "\n" else pending or " "
                    continue
                token_end = chars[index - 1][1] + 1
                # TeX ends a control word at the first non-letter and eats the
                # whitespace after it: its terminator, not a printed space.
                while index < len(chars) and chars[index][0].isspace():
                    index += 1
            if name in TEX_TYPESETTING:
                continue
            if name in TEX_SPACING:
                spaced = True
            elif name in TEX_CHARACTERS:
                emit(TEX_CHARACTERS[name], chars[start][1], end=token_end)
            else:
                emit("\\" + name, chars[start][1], end=token_end)
        elif ch == "{":
            braces.append(script)
            script = None
            index += 1
        elif ch == "}":
            if braces:
                braces.pop()
                if out and out[-1].end == offset:
                    out[-1] = replace(out[-1], end=offset + 1)
            index += 1
        elif ch in "^_":
            script = ch
            index += 1
        elif ch == "~" and (math or body.startswith("\\", index + 1)):
            spaced = True
            index += 1
        elif ch.isspace():
            if not math:
                pending = "\n" if ch in _LINE_BREAKS else pending or " "
            index += 1
        else:
            end = index + 1
            while end < len(chars) and unicodedata.combining(chars[end][0]):
                end += 1
            unit = unicodedata.normalize("NFKC", "".join(c for c, _ in chars[index:end]))
            emit(
                "".join(_DASHES.get(c, c) for c in unit),
                offset,
                _script(ch),
                end=chars[end - 1][1] + 1,
            )
            index = end
    return tuple(out)


def _key(chars: Sequence[_Char]) -> str:
    return "".join((" " if c.separates else "") + c.ch.casefold() for c in chars)


def comparison_text(text: str, *, furniture: Sequence[str] = ()) -> str:
    """What two quotes are compared on: the printed characters, case folded, with a
    space only where it is a number boundary. Other whitespace is not compared; where a
    word ends is the bounded match's to say, from what was printed."""
    return _key(_fold(text, tuple(furniture)))


def author_rendering_offsets(
    text: str, *, furniture: Sequence[str] = ()
) -> tuple[str, tuple[int, ...]]:
    """`author_rendering`, with each printed character's offset in `text` as it arrived:
    a parser cuts the rendering and slices the author's own words by these offsets. A
    space or script marker printed before a character carries that character's offset."""
    printed: list[str] = []
    offsets: list[int] = []
    before: _Char | None = None
    for c in _fold(text, tuple(furniture)):
        if (
            before is not None
            and before.ch.isdigit()
            and c.raised
            and c.raised != before.raised
            and not c.gap
            and (c.ch.isdigit() or c.ch in "+-")
        ):
            # A number or a sign set in a script right after a number prints its
            # marker: 10^-3 is a power, neither 10-3 nor 103. A degree sign or a
            # footnote letter is no number and prints as it is (-100°C, 71a).
            lead = c.raised
        else:
            lead = c.gap or (" " if c.separates else "")
        for ch in lead + c.ch:
            printed.append(ch)
            offsets.append(c.offset)
        before = c
    rendered = "".join(printed)
    start = len(rendered) - len(rendered.lstrip())
    end = len(rendered.rstrip())
    return rendered[start:end], tuple(offsets[start:end])


def author_rendering(text: str, *, furniture: Sequence[str] = ()) -> str:
    """The text as it prints, in the author's case and with the spaces printed: what a
    parser reads. A number boundary a change of script made prints as one -- a space, or
    the script's marker when the script opens right after the number -- so no number is
    read across it."""
    return author_rendering_offsets(text, furniture=furniture)[0]


def printed_line_break(text: str) -> bool:
    """Whether the text, as it prints, runs over two lines: a display formula MinerU
    broke across lines inside its `$$` is one line; a peak list set line by line is not,
    bare TeX in it or not. A break before the first printed character is not between
    two lines."""
    return any(c.gap == "\n" for c in _fold(text, (), False)[1:])


def _matches(
    hay: Sequence[_Char], needle: Sequence[_Char], *, material_name: bool = False
) -> Iterator[tuple[int, int]]:
    """Each place `needle` stands in `hay`, as (first, last) indices into `hay`.

    Compared on `comparison_text`; bounded by what was printed. A quote starts after a
    printed space or a character that is no word's -- and a quote that starts with a
    digit not after a point or a minus sign, so neither -78 nor 44-45 gives up 78 or
    45. It ends before a printed space or a character that is no word's -- and a quote
    that ends with a digit not before a point or comma and a digit, nor a hyphen and a
    number, so 44 is found neither in 44.5 nor in 44-45.
    """
    target = _key(needle)
    if not target:
        return
    owner: list[int] = []
    starts: set[int] = set()
    ends: set[int] = set()
    for at, c in enumerate(hay):
        if c.separates:
            owner.append(at)
        starts.add(len(owner))
        owner.extend(at for _ in c.ch.casefold())
        ends.add(len(owner) - 1)
    key = _key(hay)
    opens_with_digit, closes_with_digit = needle[0].ch.isdigit(), needle[-1].ch.isdigit()
    found = key.find(target)
    while found >= 0:
        stop = found + len(target) - 1
        if found in starts and stop in ends:
            first, last = owner[found], owner[stop]
            if _bounded(
                hay, first, last, opens_with_digit, closes_with_digit, material_name=material_name
            ):
                yield first, last
        found = key.find(target, found + 1)


def _name_suffix_boundary(hay: Sequence[_Char], last: int) -> bool:
    """A name excerpt can end before a trailing raised run, without classifying it.

    This locates the model's chosen name; it does not assert that the suffix is a
    footnote, or that the excerpt names the same chemical species as the full text.
    No leading or internal script is removed and ordinary numeric quotes stay strict.
    """
    if hay[last].raised or last + 1 >= len(hay) or hay[last + 1].raised != "^":
        return False
    end = last + 1
    while end < len(hay) and hay[end].raised == "^" and not hay[end].gap:
        end += 1
    return (
        end == len(hay)
        or bool(hay[end].gap)
        or (not _WORD.match(hay[end].ch) and hay[end].ch != "-")
    )


def _bounded(
    hay: Sequence[_Char],
    first: int,
    last: int,
    opens: bool,
    closes: bool,
    *,
    material_name: bool = False,
) -> bool:
    if first > 0 and not hay[first].gap:
        before = hay[first - 1].ch
        if _WORD.match(before) or (opens and before in ".-"):
            return False
    if last + 1 < len(hay) and not hay[last + 1].gap:
        if material_name and _name_suffix_boundary(hay, last):
            return True
        after = hay[last + 1].ch
        beyond = hay[last + 2] if last + 2 < len(hay) else None
        if _WORD.match(after):
            return False
        if closes and beyond is not None and beyond.ch.isdigit():
            if after in ".," and not beyond.gap:
                return False
            if after == "-":
                return False
    return True


def text_position(
    source: str, part: str, *, furniture: Sequence[str] = (), material_name: bool = False
) -> int | None:
    """Where `part` is found in `source`, as an offset into `source` as it arrived, or
    None when it is not there.

    The one rule for every quote the Host checks against a cited source -- a middle-layer
    finding and stretch, a card's method statement (E120), a declared stretch's boundary
    (E157) -- so that what verifies for one verifies for all. The offset is raw, not an
    offset into the fold (E159): folding is not length-preserving, so a folded offset
    added to a block coordinate would name a different character depending on how much
    of the block the selection carried.
    """
    furniture = tuple(furniture)
    hay = _fold(source, furniture)
    for first, _ in _matches(hay, _fold(part, furniture), material_name=material_name):
        return hay[first].offset
    return None


def _source_quote_spans(
    source: str, quote: str, *, material_name: bool = False
) -> tuple[tuple[int, int], ...]:
    hay = _fold(source, ())
    return tuple(
        (hay[first].offset, hay[last].end)
        for first, last in _matches(hay, _fold(quote, ()), material_name=material_name)
    )


def quoted_source_fragments(source: str, quote: str) -> tuple[str, ...]:
    """Copy every bounded match without choosing among identical occurrences."""
    return tuple(source[start:end].strip() for start, end in _source_quote_spans(source, quote))


def quoted_source_candidates(
    source: str, quote: str, *, limit: int = 4, material_name: bool = False
) -> list[dict[str, str]]:
    """Bounded source context and quoted fragment/locator pairs, never ownership.

    Offsets stay private. Only the locator expands to nearby word boundaries.
    The saved quote remains the original matched fragment. Both locator and quote
    must resolve uniquely at their respective levels through the ordinary matcher.
    """
    words = list(re.finditer(r"\S+", source))
    candidates = []
    for start, end in _source_quote_spans(source, quote, material_name=material_name)[:limit]:
        left = [word.start() for word in words if word.end() <= start][-3:]
        right = [word.end() for word in words if word.start() >= end][:3]
        expansions = sorted(
            {source[a:b].strip() for a in [*left, start] for b in [end, *right]},
            key=lambda text: (len(text), text),
        )
        candidate = {"context": source[max(0, start - 70) : min(len(source), end + 70)]}
        for text in expansions:
            fragment = source[start:end].strip()
            if (
                len(text) <= 240
                and len(_source_quote_spans(source, text)) == 1
                and len(_source_quote_spans(text, fragment)) == 1
            ):
                candidate.update(quote=fragment, within=text)
                break
        # A huge matched span must not turn a refusal into a copy of the document.
        if len(candidate["context"]) > 300:
            candidate["context"] = candidate["context"][:140] + " … " + candidate["context"][-140:]
        candidates.append(candidate)
    return candidates


def text_contains(source: str, part: str, *, furniture: Sequence[str] = ()) -> bool:
    """Whether `part` is found in `source` on `text_position`'s terms."""
    return text_position(source, part, furniture=furniture) is not None


@dataclass(frozen=True, slots=True)
class Quoted:
    """Matching selections and their separate raw, selection-local extents."""

    selection_ids: tuple[str, ...]
    offset: int
    spans: tuple[tuple[int, int], ...] = ()


def matching_text(binding: Mapping[str, Any]) -> str | None:
    """Raw text when verified geometry was supplied; legacy display otherwise."""
    text = binding.get("match_text", binding.get("exact_text"))
    return text if isinstance(text, str) else None


def _printing(binding: Mapping[str, Any], furniture: Mapping[str, str | None]) -> tuple[str, ...]:
    printing = furniture.get(
        str(binding["source_identity"]["package_sha256"]), binding.get("match_furniture")
    )
    return (printing,) if printing else ()


def _halves(
    cited: Sequence[str], selections: Mapping[str, Mapping[str, Any]]
) -> Iterator[tuple[str, str]]:
    """Adjacent canonical text ranges, both delivered, at least one cited.

    Layout hints and the order of a citation set never establish source geometry.
    """
    from tsdaf.lanes.literature.quote_binding import QUOTE_BINDING_VERSION

    for upper, top in selections.items():
        a = top.get("quote_binding", {})
        if a.get("version") != QUOTE_BINDING_VERSION or not a.get("ends_at_tail"):
            continue
        if type(a.get("order")) is not int or matching_text(top) is None:
            continue
        for lower, bottom in selections.items():
            b = bottom.get("quote_binding", {})
            if (
                b.get("version") == QUOTE_BINDING_VERSION
                and b.get("starts_at_head")
                and b.get("order") == a["order"] + 1
                and matching_text(bottom) is not None
                and (upper in cited or lower in cited)
                and all(
                    top["source_identity"].get(key) is not None
                    and top["source_identity"].get(key) == bottom["source_identity"].get(key)
                    for key in ("document_revision_id", "package_sha256", "source_phase")
                )
            ):
                yield upper, lower


def find_quote(
    words: str,
    cited: Sequence[str],
    selections: Mapping[str, Mapping[str, Any]],
    *,
    furniture: Mapping[str, str | None] | None = None,
    material_name: bool = False,
) -> Quoted | None:
    """Exact printed wording within cited text or across verified text boundaries.

    Returned offsets refer to the matcher's input. Consumers requiring canonical
    coordinates must additionally bind that raw input to their observation anchor.
    """
    printing_of = furniture or {}
    cited = tuple(dict.fromkeys(cited))
    for sid in cited:
        text = matching_text(selections[sid])
        if text is not None:
            printing = _printing(selections[sid], printing_of)
            hay = _fold(text, printing)
            for first, last in _matches(hay, _fold(words, printing), material_name=material_name):
                start = hay[first].offset
                return Quoted((sid,), start, ((start, hay[last].end),))
    for upper, lower in _halves(cited, selections):
        printing = _printing(selections[upper], printing_of)
        top_text, bottom_text = matching_text(selections[upper]), matching_text(selections[lower])
        assert top_text is not None and bottom_text is not None
        top, bottom = _fold(top_text, printing), _fold(bottom_text, printing)
        if not top or not bottom:
            continue
        seam = replace(
            bottom[0], gap=" ", separates=top[-1].ch.isdigit() and bottom[0].ch.isdigit()
        )
        joined = (*top, seam, *bottom[1:])
        for first, last in _matches(joined, _fold(words, printing), material_name=material_name):
            if first < len(top) <= last:
                start = top[first].offset
                return Quoted(
                    (upper, lower),
                    start,
                    ((start, top[-1].end), (bottom[0].offset, bottom[last - len(top)].end)),
                )
    return None
