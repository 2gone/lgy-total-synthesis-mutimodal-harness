"""The Host's one table for characterization methods: the author's words to a key.

A `CharacterizationRecord` carries `method_verbatim`, the author's own words ("mp",
"HRMS (ESI)", "single-crystal X-ray"), and `method`, the key this table assigns to
them -- or None when the table has no entry, which is a fact about the table, not
about the paper (ledger E47). The model is never shown the keys; it writes the words.

The table was seeded on 2026-09-12 by counting method phrases in the experimental
paragraphs of the 200-paper corpus on mfy-win (1,559 paragraphs in 164 papers):
NMR 1,138 mentions in 55 papers, MS 751/29, IR 423/28, HRMS 256/23, elemental analysis
166/13, melting point 126/24, ee/er 84/34, X-ray 64/31, optical rotation 51/20, TLC/Rf
46/18, HPLC 41/19, 2D NMR 23/10, UV 20/14, GC 15/8, Mosher 11/6, CD 4/2, Raman 0. The
two identity checks the frozen enum named (co-injection, authentic sample) are kept.
Publication counts the words it could not place; an entry is added, never removed.
"""

from __future__ import annotations

import re
from typing import Any

from tsdaf.contracts.literature.schema import MachineProfile
from tsdaf.lanes.literature.author_words import (
    author_rendering,
    author_rendering_offsets,
    printed_line_break,
)
from tsdaf.lanes.literature.reported_scalars import parse_printed_scalar

__all__ = (
    "CHARACTERIZATION_CATEGORY",
    "CHARACTERIZATION_LINE_CHARS",
    "CHARACTERIZATION_METHODS",
    "TRANSCRIPTION_REFUSAL",
    "VALUELESS_REFUSAL",
    "characterization_fields",
    "characterization_scalar",
    "normalize_method",
    "refuse_transcription",
    "refuse_a_method_with_no_value",
)

# The NOTE category a fusion uses for one characterization statement.
CHARACTERIZATION_CATEGORY = "CHARACTERIZATION"
# One line: "mp 142-144 °C" or "single-crystal X-ray" fits many times over; a peak list
# does not. A note longer than this is a transcription and is refused at write time.
CHARACTERIZATION_LINE_CHARS = 200
# The one sentence every writer of a characterization is told, at every arrival point:
# the fusion's CHARACTERIZATION note and the detail lane's card say it the same way.
TRANSCRIPTION_REFUSAL = "cite the spectrum, do not transcribe it"


CHARACTERIZATION_WRITING_MEANING = (
    "Preserve the author's complete characterization statement, qualifications, comparisons "
    "and sample context with its evidence. In v1.4 this includes reported acquisition "
    "conditions or comparisons even when no reliable scalar is reported; the host leaves "
    "such a value null. Older profiles require a stated result beyond a technique heading. "
    "Do not transcribe peak lists; their full data remain in the cited evidence. "
    "These examples and named methods do not limit what reported characterization means."
)


# The other half of the same rule. The e108 round wrote three cards that said
# "1H NMR (CDCl3)", "13C NMR (CDCl3)" and "IR (CHCl3)" and nothing else: told never to
# transcribe a peak list, the model wrote the method and left the measurement out. A
# card is a measurement someone can check, so a statement that names a technique and a
# solvent and stops is not a short card, it is an empty one -- what it says belongs in
# this document's coverage, which is where a task records what it looked at.
VALUELESS_REFUSAL = (
    "a characterization card states a measured value someone can check (mp 142-144 °C; "
    "HRMS calcd 339.1931, found 339.1935); naming a method and its solvent with no value "
    "records only that the spectrum exists, which belongs in this document's coverage"
)
# What is technique-naming rather than measurement: the nucleus, the technique, and the
# parenthesised solvent or conditions. Whatever survives all three is the value.
_NAMING = re.compile(
    r"\([^()]*\)"
    r"|\b\d{1,3}\s*[A-Z][a-z]?\b"
    r"|\b(?:NMR|IR|UV(?:[-/ ]?vis)?|HRMS|LRMS|MS|EI|ESI|FAB|APCI|CI|mp|bp|TLC|HPLC|GC)\b"
    r"|\b(?:COSY|HSQC|HMQC|HMBC|NOESY|ROESY|TOCSY|DEPT)\b",
    re.IGNORECASE,
)


def refuse_a_method_with_no_value(text: str) -> None:
    """Refuse a card that names a technique and never says what it measured.

    Narrow on purpose: it fires only when the statement is nothing but naming. A single
    surviving character -- a shift, a wavenumber, a temperature, the word `found` -- is
    a value and the card stands, because a false refusal here would put the Host in an
    argument with a model that is holding a real measurement.
    """
    if not _NAMING.sub(" ", text).strip(" ,;:.()[]-\u2013\u2014"):
        raise ValueError(VALUELESS_REFUSAL)



def validate_characterization_statement(
    text: str, *, profile: MachineProfile, complete_statement: bool | None = None
) -> None:
    """One profile-aware acceptance rule, shared by both writers and publication.

    v1.4 preserves evidence-bound sample/measurement context independently of whether
    a reliable scalar can be derived. DTOs carry structure, not this runtime policy.
    """
    refuse_transcription(
        text,
        complete_statement=profile.expression if complete_statement is None else complete_statement,
    )
    if not profile.preservation:
        refuse_a_method_with_no_value(text)


def refuse_transcription(text: str, *, complete_statement: bool = False) -> None:
    """Refuse a characterization statement that is a transcription rather than a citation.

    One rule, one place: a printed line break or more than `CHARACTERIZATION_LINE_CHARS`
    printed characters is a peak list or a full spectral line, and it never enters a
    fusion state, a card or a model's later view. Printed, as the author's words define
    it: a display formula MinerU broke across lines inside its `$$` is one line, and its
    TeX is not counted as characters.
    """
    if complete_statement:
        return  # Length/layout does not establish that a measurement is a peak list.
    if printed_line_break(text) or len(author_rendering(text)) > CHARACTERIZATION_LINE_CHARS:
        raise ValueError(TRANSCRIPTION_REFUSAL)


# Key -> patterns over the author's words, in the order they are tried. More specific
# families (HRMS before MS, 2D experiments inside NMR) come first so a phrase naming
# both lands on the specific key.
CHARACTERIZATION_METHODS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "HRMS",
        (
            r"\bHR[ -]?MS\b",
            r"\bHR[ -]?(ESI|EI|FAB|CI|APCI)[ -]?MS\b",
            r"\bhigh[- ]resolution mass",
            r"\bexact mass\b",
        ),
    ),
    (
        "NMR",
        (r"\bNMR\b", r"\b(COSY|HSQC|HMQC|HMBC|NOESY|ROESY|TOCSY|DEPT)\b", r"\bnOe\b", r"\bNOE\b"),
    ),
    ("IR", (r"\bFT[- ]?IR\b", r"\bIR\b", r"\binfrared\b", r"\bATR\b")),
    ("MELTING_POINT", (r"\bmp\b", r"\bm\.\s?p\.", r"\bmelting point\b")),
    ("OPTICAL_ROTATION", (r"\[\s*[aα]\s*\]", r"\boptical rotation\b", r"\bspecific rotation\b")),
    ("X_RAY", (r"\bX-?ray\b", r"\bsingle[- ]crystal\b", r"\bcrystallograph")),
    (
        "ELEMENTAL_ANALYSIS",
        (r"\bAnal\.?\s*Calcd\b", r"\belemental analysis\b", r"\bcombustion analysis\b"),
    ),
    ("CO_INJECTION_CO_ELUTION", (r"\bco-?injection\b", r"\bco-?elution\b", r"\bco-?eluted\b")),
    (
        "AUTHENTIC_SAMPLE_COMPARISON",
        (r"\bauthentic (sample|material|specimen)\b", r"\bnatural sample\b"),
    ),
    ("MOSHER", (r"\bMosher\b", r"\bMTPA\b")),
    (
        "EE",
        (
            r"\benantiomeric (excess|ratio)\b",
            r"\bee\b",
            r"\ber\b",
            r"\bdr\b",
            r"\bdiastereomeric ratio\b",
        ),
    ),
    ("HPLC", (r"\bHPLC\b", r"\bSFC\b", r"\bUPLC\b")),
    ("GC", (r"\bGC[- ]?MS\b", r"\bGC\b")),
    ("LC_MS_MS", (r"\bLC[- ]?MS/MS\b", r"\bMS/MS\b")),
    ("LC_MS", (r"\bLC[- ]?MS\b", r"\bUPLC[- ]?MS\b")),
    (
        "MS",
        (
            r"\bLRMS\b",
            r"\b(ESI|EI|FAB|CI|APCI|MALDI)[- ]?(MS|TOF)\b",
            r"\bMS\b",
            r"\bm/z\b",
            r"\bmass spectr",
        ),
    ),
    ("UV", (r"\bUV[-/ ]?vis\b", r"\bUV\b")),
    ("TLC", (r"\bR[fF]\b", r"\bTLC\b")),
    ("CD", (r"\bECD\b", r"\bcircular dichroism\b", r"\bCD spectr", r"\bVCD\b")),
    ("RAMAN", (r"\bRaman\b",)),
)
_COMPILED = tuple(
    (key, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for key, patterns in CHARACTERIZATION_METHODS
)


def normalize_method(verbatim: str) -> str | None:
    """The key for the author's words, or None when the table has no entry for them."""

    if not isinstance(verbatim, str):
        raise TypeError("verbatim must be a string")
    for key, patterns in _COMPILED:
        if any(pattern.search(verbatim) for pattern in patterns):
            return key
    return None


# A nucleus written before "NMR" ("1H NMR", "13C NMR") belongs to that method's words.
_NUCLEUS = re.compile(r"(?:\b\d{1,3}[A-Z][a-z]?\s*)$")
# The joiners between methods listed in one breath; never part of a method's words.
_JOINERS = re.compile(r"^(?:[,;]\s*)?(?:and|or)?\s*", re.IGNORECASE)
_TRAILING = re.compile(r"(?:[\s,;]*\b(?:and|or)\b)?[\s,;]*$", re.IGNORECASE)
_SEPARATOR = re.compile(r"[,;]|\b(?:and|or)\b", re.IGNORECASE)
# "m/z" places a statement in the MS family but is where an HRMS line's numbers begin,
# not where a second method begins; it never starts a segment of its own.
_NOT_A_START = {r"\bm/z\b"}
_PARENTHETICAL = re.compile(r"\([^)]*\)?")
# An HRMS statement: the formula, an ion written before or after it, the calculated
# value and the found value. "calcd" carries the author's comparison; "found" the
# measurement.
_FORMULA = r"[A-Z][a-z]?\d*(?:[A-Z][a-z]?\d*)+"
_ION = r"\[\s*M[^\]]*\]\s*[+\-−]?"
_HRMS = re.compile(
    rf"calcd\.?\s*(?:for\s*)?(?:(?P<ion_before>{_ION})\s*)?"
    rf"(?:for\s*)?(?P<formula>{_FORMULA})\s*(?:(?P<ion_after>{_ION})\s*)?"
    r"(?P<calcd>\d+\.\d+)\s*[,;:]?\s*found[:,]?\s*(?P<found>\d+\.\d+)",
    re.IGNORECASE,
)
_ION_FIRST = re.compile(rf"(?P<ion>{_ION})\s*calcd", re.IGNORECASE)
# What a value was compared with: a literature value, or agreement with a natural,
# authentic or reported sample.
_LITERATURE = re.compile(r"\(?\s*(lit\.?\s*[^)]*)\)?", re.IGNORECASE)
_AGREEMENT = re.compile(
    r"\b((?:identical|consistent|in\s+(?:good\s+|full\s+|excellent\s+)?agreement|matched|"
    r"match(?:es|ing)?|agreed?|agrees|comparable|superimposable)\b[^.;]*?"
    r"\b(?:natural|authentic|reported|literature|previously)\b[^.;]*)",
    re.IGNORECASE,
)


def _segments(words: str) -> list[str]:
    """The stretch of a statement that belongs to each method it names, in order."""

    starts: list[int] = []
    for _, patterns in _COMPILED:
        for pattern in patterns:
            if pattern.pattern in _NOT_A_START:
                continue
            for match in pattern.finditer(words):
                starts.append(match.start())
    starts = sorted(set(starts))
    if len(starts) < 2:
        return [words]
    # A method's words begin at its own match, pulled back over a nucleus; the next
    # method's start ends them. Only starts that survive as their own segment count:
    # a pattern matching inside another method's stretch (the "MS" in "HRMS", the
    # "found" numbers) is not a second method.
    cuts: list[int] = [0]
    for start in starts:
        nucleus = _NUCLEUS.search(words[:start])
        cut = nucleus.start() if nucleus else start
        # Methods listed in one breath are separated by punctuation or a conjunction;
        # two patterns hit inside one method's words ("single-crystal X-ray") are not.
        if cut > cuts[-1] and _SEPARATOR.search(words[cuts[-1] : cut]):
            cuts.append(cut)
    segments = []
    for index, cut in enumerate(cuts):
        end = cuts[index + 1] if index + 1 < len(cuts) else len(words)
        piece = _TRAILING.sub("", _JOINERS.sub("", words[cut:end], count=1)).strip()
        if piece and normalize_method(piece) is not None:
            segments.append(piece)
    return segments if len(segments) >= 2 else [words]


def characterization_fields(
    words: str, *, profile: MachineProfile = MachineProfile.V1
) -> list[dict[str, Any]]:
    """The cards one characterization statement names, each with what the Host reads.

    One statement, one method: one card, its words the author's. Several methods in
    one breath ("1H NMR, 13C NMR, HRMS, and [a]D"): one card per method, the words
    cut at each method. On each card: `method` from the table, `reported_value` when
    the words carry a number (the found value for HRMS, in m/z), `reported_formula`
    and `reported_ion` from an HRMS line, `comparator` from a calculated value, a
    literature value or a stated agreement with a natural, authentic or reported sample.
    """

    cards = []
    for segment in _segments(" ".join(words.split())):
        printed = author_rendering(segment) if profile.preservation else segment
        method = normalize_method(printed)
        card: dict[str, Any] = {
            "method_verbatim": segment,
            "method": method,
            "reported_value": characterization_scalar(segment),
            "reported_formula": None,
            "reported_ion": None,
            "comparator": None,
        }
        if profile.preservation and method == "TLC":
            # A whole, single dimensionless statement only. A numeric prefix is not
            # a scalar: ranges, precision, multiple eluents and qualifiers stay in
            # the complete source expression instead of being silently truncated.
            match = re.fullmatch(
                r"\s*Rf\s*(?:=|:)?\s*(\d+(?:\.\d+)?|\.\d+)\s*", printed, re.IGNORECASE
            )
            card["reported_value"] = None
            if match is not None:
                card["reported_value"] = {
                    **parse_printed_scalar(match.group(1), segment),
                    "numeric_value_decimal": match.group(1),
                    "unit": None,
                }
        hrms = _HRMS.search(printed)
        if hrms is not None:
            ion = hrms.group("ion_before") or hrms.group("ion_after")
            if ion is None:
                first = _ION_FIRST.search(printed)
                ion = first.group("ion") if first else None
            card["reported_formula"] = hrms.group("formula")
            card["reported_ion"] = " ".join(ion.split()) if ion else None
            card["reported_value"] = {
                "raw_text": "found " + hrms.group("found"),
                "numeric_value_decimal": hrms.group("found"),
                "range_min_decimal": None,
                "range_max_decimal": None,
                "unit": "m/z",
                "normalized_value_decimal": None,
                "normalized_range_min_decimal": None,
                "normalized_range_max_decimal": None,
                "normalized_unit": None,
            }
            card["comparator"] = "calcd " + hrms.group("calcd")
        elif not profile.unified_expression and (literature := _LITERATURE.search(segment)) and literature.group(1):
            card["comparator"] = " ".join(literature.group(1).split()).rstrip(")")
        elif not profile.unified_expression and (agreement := _AGREEMENT.search(segment)) is not None:
            card["comparator"] = " ".join(agreement.group(1).split())
        cards.append(card)
    return cards


def characterization_scalar(words: str) -> dict[str, Any] | None:
    """The number a characterization statement carries, when it carries one.

    "mp 142-144 °C" is a range in degrees; "single-crystal X-ray" has no number and the
    value stays None (00 §9.11: the card is light, the full text is in the observation).
    The method words are dropped from the front until what is left parses as a quantity.
    """
    # A parenthetical after the number is a comparison ("(lit. 222-224 °C)"), read
    # separately as the comparator; the number is what stands before it. The statement
    # is read as it prints, whole, before it is cut: cut first, a word inside a formula
    # lost the formula's opening $ and its digits read apart.
    printed, offsets = author_rendering_offsets(words)
    removed = {i for m in _PARENTHETICAL.finditer(printed) for i in range(m.start(), m.end())}
    kept = [i for i in range(len(printed)) if i not in removed]
    text = "".join(printed[i] for i in kept)
    tokens = [m.start() for m in re.finditer(r"\S+", text)]
    for start in tokens[:4]:
        first = kept[start]
        # The author's words for the value: from its first printed character up to the
        # comparison that follows it, or to the end.
        stop = next((offsets[i] for i in sorted(removed) if i > first), len(words))
        scalar = parse_printed_scalar(text[start:], words[offsets[first] : stop].strip())
        if any(
            scalar.get(key) is not None for key in ("numeric_value_decimal", "range_min_decimal")
        ):
            return scalar
    return None
