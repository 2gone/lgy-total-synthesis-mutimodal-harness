r"""Turn an author's own words for a quantity into a decimal, deterministically.

Every reported number in the bundle -- a yield, a temperature, a duration -- was
published as the author's text with every numeric view null, because reading "56%" as
56 is arithmetic and the model is not asked to do arithmetic. Nothing did it, so a
consumer comparing two yields had to parse strings, and the contract's numeric fields
were dead weight.

This is that parse, and it is the Host's because it has one right answer. It runs on
text, not on judgement: no unit is guessed from context, no missing unit is assumed, and
nothing about the surrounding chemistry is consulted.

It reads a phrase as it prints (`author_rendering`): the TeX a package carries -- `$73\%$`,
`$1.34~\mathrm{mL}$`, `30.4 $\mu$ mol` -- is read for what it prints, once, on the whole
phrase before it is cut into pieces, by the one definition of the author's words.

What it refuses is as much of the contract as what it accepts. `raw_text` is always the
author's, unchanged. A quantity this cannot read exactly keeps every numeric view null
rather than being half-filled -- a bound ("<5%", "ca. 20 h"), a ratio ("3:1 dr"), a word
("overnight", "room temperature"), a unit not in the table below. A published 95 where
the paper wrote ">95%" is a false fact, and a false fact is worse than an absent one.

`normalized_*` restates an accepted value in one canonical unit per dimension, so two
records can be compared without either consumer knowing the other's units: kelvin for
temperature, seconds for time, pascals for pressure, mol/L for concentration, grams for
mass, moles for amount, and percent unchanged. Conversion is exact decimal arithmetic;
nothing is rounded.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from tsdaf.lanes.literature.author_words import author_rendering, author_rendering_offsets

__all__ = (
    "parse_material_quantity",
    "parse_reported_outcome",
    "parse_reported_scalar",
    "parse_step_count",
    "parse_written_material_quantity",
    "parse_written_material_parts",
)

# One canonical unit per dimension, and the exact factor from each accepted spelling.
# A unit absent here is a unit this parser does not claim to understand, which leaves the
# whole quantity unparsed rather than parsed into a guess.
_SCALE_UNITS: dict[str, tuple[str, Decimal]] = {
    "%": ("%", Decimal(1)),
    "s": ("s", Decimal(1)),
    "sec": ("s", Decimal(1)),
    "secs": ("s", Decimal(1)),
    "second": ("s", Decimal(1)),
    "seconds": ("s", Decimal(1)),
    "min": ("s", Decimal(60)),
    "mins": ("s", Decimal(60)),
    "minute": ("s", Decimal(60)),
    "minutes": ("s", Decimal(60)),
    "h": ("s", Decimal(3600)),
    "hr": ("s", Decimal(3600)),
    "hrs": ("s", Decimal(3600)),
    "hour": ("s", Decimal(3600)),
    "hours": ("s", Decimal(3600)),
    "d": ("s", Decimal(86400)),
    "day": ("s", Decimal(86400)),
    "days": ("s", Decimal(86400)),
    "week": ("s", Decimal(604800)),
    "weeks": ("s", Decimal(604800)),
    "atm": ("Pa", Decimal("101325")),
    "bar": ("Pa", Decimal("100000")),
    "mbar": ("Pa", Decimal("100")),
    "pa": ("Pa", Decimal(1)),
    "kpa": ("Pa", Decimal(1000)),
    "mpa": ("Pa", Decimal(1000000)),
    "torr": ("Pa", Decimal("101325") / Decimal(760)),
    "mmhg": ("Pa", Decimal("101325") / Decimal(760)),
    "psi": ("Pa", Decimal("6894.757293168361")),
    "m": ("mol/L", Decimal(1)),
    "mm": ("mol/L", Decimal("0.001")),
    "um": ("mol/L", Decimal("0.000001")),
    "nm": ("mol/L", Decimal("0.000000001")),
    "mol/l": ("mol/L", Decimal(1)),
    "g": ("g", Decimal(1)),
    "mg": ("g", Decimal("0.001")),
    "kg": ("g", Decimal(1000)),
    "ug": ("g", Decimal("0.000001")),
    "mol": ("mol", Decimal(1)),
    "mmol": ("mol", Decimal("0.001")),
    "umol": ("mol", Decimal("0.000001")),
    "l": ("L", Decimal(1)),
    "ml": ("L", Decimal("0.001")),
    "ul": ("L", Decimal("0.000001")),
    # Stoichiometry is dimensionless: equivalents as written, and a catalyst loading in
    # mol % is the same number over a hundred (5 mol % of the limiting reagent is 0.05
    # equivalents of it).
    "equiv": ("equiv", Decimal(1)),
    "equivs": ("equiv", Decimal(1)),
    "equivalent": ("equiv", Decimal(1)),
    "equivalents": ("equiv", Decimal(1)),
    "eq": ("equiv", Decimal(1)),
    "eqs": ("equiv", Decimal(1)),
    "mol%": ("equiv", Decimal("0.01")),
}
# Temperature is affine, not a scale factor, so it converts by its own rule.
_TEMPERATURE_UNITS = {"c", "°c", "degc", "celsius", "f", "°f", "degf", "fahrenheit", "k"}
_CANONICAL_TEMPERATURE = "K"

# A comparator or hedge means the paper did not report this value, it bounded it. The
# number is real but it is not the quantity, so the quantity stays unparsed. Only what
# stands *before* the number hedges it: "over 95%" is a bound, while the same word after
# the number opens a basis clause -- "56% yield over 2 steps" reports exactly 56%.
_HEDGES = re.compile(
    r"(?:[<>≤≥~≈±]|\b(?:ca|approx|approximately|about|up\s+to|over|at\s+least)\b\.?)"
)
_NUMBER = r"-?\d+(?:\.\d+)?"
_RANGE = re.compile(
    rf"^(?P<low>{_NUMBER})\s*(?:-|to)\s*(?P<high>{_NUMBER})\s*(?P<unit>.*)$",
)
_SINGLE = re.compile(rf"^(?P<value>{_NUMBER})\s*(?P<unit>.*)$")
# "0 degrees C" and "0 deg C" are one unit written as two words.
_DEGREE_WORDS = re.compile(r"\bdeg(?:ree)?s?\b\s*")

# Words that follow a quantity to say what kind of quantity it is. They name the field
# the value belongs to -- which `outcome.kind` already records -- and do not change the
# value, so a unit followed only by these still reads. Anything else after the unit is
# text this module does not understand, and it refuses rather than truncating.
_DESCRIPTORS = frozenset(
    {
        "yield",
        "yields",
        "ee",
        "e.e.",
        "er",
        "dr",
        "d.r.",
        "conversion",
        "purity",
        "pure",
        "recovery",
        "recovered",
        "isolated",
        "combined",
        "overall",
        "brsm",
        "each",
        "total",
        "product",
        "material",
        "of",
        "the",
        # "20 g scale", "10 g scale" name the kind of quantity, as "yield" does.
        "scale",
    }
)
# How an author writes a count -- of steps, of cycles -- in digits or in words: "22",
# "seven", "twenty-two". One spelling rule, `_COUNT` to find a count and `_count` to read
# it, for the basis, the basis clause and the step count alike. The basis once read only
# digits while the step count read words, so "68% over two steps" had no basis.
_NUMBER_WORDS = {
    word: index
    for index, word in enumerate(
        (
            "zero one two three four five six seven eight nine ten eleven twelve thirteen "
            "fourteen fifteen sixteen seventeen eighteen nineteen twenty"
        ).split()
    )
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50}
_ONES = tuple(word for word, number in _NUMBER_WORDS.items() if 0 < number < 10)
# Longest first, so "fourteen" is not read as "four"; the case of a spelled count carries
# no meaning, whatever the pattern around it holds to.
_COUNT = (
    rf"(?i:\d+|(?:{'|'.join(_TENS)})-(?:{'|'.join(_ONES)})"
    rf"|{'|'.join(sorted({*_NUMBER_WORDS, *_TENS}, key=len, reverse=True))})"
)


def _count(token: str) -> int | None:
    """The number a count found by `_COUNT` spells."""
    token = token.lower()
    if token.isdigit():
        return int(token)
    if token in _NUMBER_WORDS or token in _TENS:
        return _NUMBER_WORDS.get(token, _TENS.get(token))
    tens, _, ones = token.partition("-")
    return _TENS[tens] + _NUMBER_WORDS[ones] if tens in _TENS and ones in _ONES else None


# A clause that says what the value is measured over. It carries its own numbers -- "over
# 2 steps" -- which must not be mistaken for a second quantity, and the lane reports it
# separately as the outcome's basis, so it is skipped rather than read.
_BASIS_CLAUSE = re.compile(
    rf"^(?:(?:over|from|on|after|for|across|per|based\s+on)\b|\(\s*{_COUNT}\s+steps?\s*\)"
    r"|\bbrsm\b)"
)
# The basis clause itself, in the words of the value it qualifies: what that value is
# measured over. Reported separately as the outcome's `basis`, in the author's words.
_BASIS = re.compile(
    rf"(?:\b(?:over|for|across|after)\s+{_COUNT}\s+(?:steps?|cycles?|stages?|operations?)\b"
    r"|\bbrsm\b"
    r"|\bbased\s+on\s+recovered\b[^,;()]*"
    rf"|\(\s*{_COUNT}\s+steps?\s*\))",
    re.IGNORECASE,
)
# The pieces of a phrase that reports one quantity several ways -- "26 mg (0.25 mmol, 1
# equiv)" -- are what lies between its commas, semicolons and parentheses.
_PIECE = re.compile(r"[^,;()]+")
_STEP_COUNT = re.compile(rf"\b({_COUNT})\s*-?\s*steps?\b", re.IGNORECASE)


def _empty(raw_text: str) -> dict[str, object]:
    return {
        "raw_text": raw_text,
        "numeric_value_decimal": None,
        "range_min_decimal": None,
        "range_max_decimal": None,
        "unit": None,
        "normalized_value_decimal": None,
        "normalized_range_min_decimal": None,
        "normalized_range_max_decimal": None,
        "normalized_unit": None,
    }


def _decimal_text(value: Decimal) -> str | None:
    """Render a decimal in the exact form the public contract accepts.

    The contract's pattern admits neither exponent notation nor a trailing point, and
    reads `-0` as a distinct token from `0`, so a value that cannot be written plainly is
    not written at all.
    """

    if not value.is_finite():
        return None
    plain = format(value.normalize(), "f")
    if plain in {"-0", "-0.0"}:
        plain = "0"
    if "." in plain:
        plain = plain.rstrip("0").rstrip(".")
    if plain in {"", "-"}:
        plain = "0"
    if not re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", plain):
        return None
    return plain


def _normalized_unit_token(unit: str) -> str:
    """Fold a unit's spelling to the key the tables use, keeping case where it matters."""

    folded = _DEGREE_WORDS.sub("", unit).strip()
    # Whitespace separates words, not arbitrary fragments of a unit: "M in"
    # must not become "min". Only these existing multi-token unit spellings join.
    if " " in folded:
        if not re.fullmatch(r"(?:mol\s+%|°\s*[CF]|mol\s*/\s*[Ll]|[µμ]\s*(?:mol|[gLM]))", folded):
            return ""
        folded = re.sub(r"\s+", "", folded)
    if folded in {"%"}:
        return folded
    lowered = folded.casefold()
    if lowered in _TEMPERATURE_UNITS:
        return lowered
    # Molar concentration is the one place case carries meaning: `M` is molar and `m` is
    # not a unit this table knows, so an unqualified lowercase `m` is refused rather than
    # read as molar.
    if folded in {"M", "mM", "nM", "mol/L", "mol/l"}:
        return lowered
    if folded in {"µM", "μM", "uM"}:
        return "um"
    if folded in {"µg", "μg", "ug"}:
        return "ug"
    if folded in {"µmol", "μmol", "umol"}:
        return "umol"
    if folded in {"µL", "μL", "uL", "µl", "μl"}:
        return "ul"
    if folded in {"m", "mm", "nm"}:
        # Metres and their prefixes are lengths; nothing published here is a length, and
        # reading `nm` as nanomolar would invent a concentration.
        return ""
    return lowered


# A unit the model states on its own, as a separate field. Symbols are matched exactly as
# written, because case is the meaning: M is molar and m is not, MPa is not mPa, mg is not
# Mg, nM is a concentration and nm a length. Unit words are ordinary English and may be
# capitalized. Anything else is an unknown unit: its number and spelling are kept and no
# normalized value is derived. The table keys are the ones `_converted` already reads.
_SYMBOL_SPELLINGS = {
    "%": "%", "s": "s", "min": "min", "h": "h", "d": "d",
    "atm": "atm", "bar": "bar", "mbar": "mbar", "Pa": "pa", "kPa": "kpa", "MPa": "mpa",
    "Torr": "torr", "torr": "torr", "mmHg": "mmhg", "psi": "psi",
    "M": "m", "mM": "mm", "µM": "um", "μM": "um", "uM": "um", "nM": "nm",
    "mol/L": "mol/l", "mol/l": "mol/l",
    "g": "g", "mg": "mg", "kg": "kg", "µg": "ug", "μg": "ug", "ug": "ug",
    "mol": "mol", "mmol": "mmol", "µmol": "umol", "μmol": "umol", "umol": "umol",
    "L": "l", "l": "l", "mL": "ml", "ml": "ml",
    "µL": "ul", "μL": "ul", "uL": "ul", "µl": "ul", "μl": "ul", "ul": "ul",
    "mol%": "mol%",
    "°C": "°c", "℃": "°c", "C": "c", "degC": "degc", "°F": "°f", "degF": "degf", "K": "k",
}
_WORD_SPELLINGS = frozenset({
    "sec", "secs", "second", "seconds", "mins", "minute", "minutes", "hr", "hrs", "hour",
    "hours", "day", "days", "week", "weeks", "equiv", "equivs", "equivalent", "equivalents",
    "eq", "eqs", "celsius", "fahrenheit",
})
# Spellings that are one unit written with a space; no other inner space is removed.
_SPACED_UNIT = re.compile(r"mol\s+%|°\s+[CF]|mol\s*/\s*[Ll]|deg\s+[CF]")
assert set(_SYMBOL_SPELLINGS.values()) | _WORD_SPELLINGS <= set(_SCALE_UNITS) | _TEMPERATURE_UNITS


def stated_unit_token(unit: str) -> str | None:
    """The conversion key of an explicitly stated unit, or None when it is not known."""
    spelling = unit.strip()
    if _SPACED_UNIT.fullmatch(spelling):
        spelling = re.sub(r"\s+", "", spelling)
    if spelling in _SYMBOL_SPELLINGS:
        return _SYMBOL_SPELLINGS[spelling]
    word = spelling.casefold()
    return word if word in _WORD_SPELLINGS else None


def _converted(value: Decimal, token: str) -> tuple[str, Decimal] | None:
    if token in _TEMPERATURE_UNITS:
        if token == "k":
            return _CANONICAL_TEMPERATURE, value
        if token in {"f", "°f", "degf", "fahrenheit"}:
            return _CANONICAL_TEMPERATURE, (value - Decimal(32)) * Decimal(5) / Decimal(
                9
            ) + Decimal("273.15")
        return _CANONICAL_TEMPERATURE, value + Decimal("273.15")
    scale = _SCALE_UNITS.get(token)
    if scale is None:
        return None
    canonical, factor = scale
    return canonical, value * factor


def parse_reported_scalar(raw_text: str) -> dict[str, object]:
    """Read one reported quantity into the public scalar, or leave it as words.

    `raw_text` is returned exactly as given in every case; the numeric views are filled
    only when the text, as it prints, states one value or one range in a unit this
    module knows.
    """

    if not isinstance(raw_text, str):
        raise TypeError("raw_text must be a string")
    return _scalar(author_rendering(raw_text), raw_text)


def parse_printed_scalar(printed: str, raw_text: str) -> dict[str, object]:
    """`parse_reported_scalar` on a piece of a phrase already read as it prints, keeping
    `raw_text` -- the author's words for that piece -- as given."""

    if not isinstance(printed, str) or not isinstance(raw_text, str):
        raise TypeError("printed and raw_text must be strings")
    return _scalar(printed, raw_text)


def _scalar(printed: str, raw_text: str) -> dict[str, object]:
    """`parse_reported_scalar` on a phrase already read as it prints."""

    result = _empty(raw_text)
    # The rendering has folded Unicode forms and the two minus dashes; an em dash
    # between two numbers is a range here too.
    text = " ".join(printed.replace("—", "-").split())
    if not text:
        return result
    first_digit = re.search(r"\d", text)
    if first_digit is None or _HEDGES.search(text[: first_digit.start()]):
        return result

    match = _RANGE.match(text) or _SINGLE.match(text)
    if match is None:
        return result
    resolved = _resolve_unit(match.group("unit"))
    if resolved is None:
        return result
    token, written = resolved

    # Ordered as the contract names them: the single value, then the range ends. A
    # quantity is one or the other, so the unused pair stays None throughout.
    groups = match.groupdict()
    reported: tuple[Decimal | None, Decimal | None, Decimal | None]
    try:
        if "low" in groups:
            reported = (None, Decimal(groups["low"]), Decimal(groups["high"]))
        else:
            reported = (Decimal(groups["value"]), None, None)
    except InvalidOperation:
        return result

    canonical: str | None = None
    rendered: list[str | None] = []
    normalized: list[str | None] = []
    for item in reported:
        if item is None:
            rendered.append(None)
            normalized.append(None)
            continue
        conversion = _converted(item, token)
        if conversion is None:
            return result
        canonical, converted_value = conversion
        as_reported = _decimal_text(item)
        as_normalized = _decimal_text(converted_value)
        if as_reported is None or as_normalized is None:
            return result
        rendered.append(as_reported)
        normalized.append(as_normalized)
    if canonical is None:
        return result

    result["unit"] = written
    result["numeric_value_decimal"] = rendered[0]
    result["range_min_decimal"] = rendered[1]
    result["range_max_decimal"] = rendered[2]
    result["normalized_value_decimal"] = normalized[0]
    result["normalized_range_min_decimal"] = normalized[1]
    result["normalized_range_max_decimal"] = normalized[2]
    result["normalized_unit"] = canonical
    return result


def _resolve_unit(text: str) -> tuple[str, str] | None:
    """Split what follows a number into its unit and the words that merely describe it.

    "98% yield" is 98 percent; the trailing word names the kind, which the outcome record
    already carries. "56% over 2 steps" is also 56 percent, and the 2 belongs to the
    basis the lane reported separately. "2 h then 30 min" is two quantities, and this
    returns nothing for it rather than publishing the first and dropping the second.
    """

    words = text.split()
    for cut in range(len(words), 0, -1):
        token = _normalized_unit_token(" ".join(words[:cut]))
        if not token or (token not in _TEMPERATURE_UNITS and token not in _SCALE_UNITS):
            continue
        rest = words[cut:]
        if _tail_is_harmless(rest):
            return token, " ".join(words[:cut])
        return None
    return None


def _tail_is_harmless(words: list[str]) -> bool:
    """True when what follows the unit cannot be another quantity.

    The two harmless tails compose, and the common one is both at once: "56% yield over
    2 steps" names the kind and then the basis. Descriptors are consumed first, and what
    remains must open a basis clause or be nothing.
    """

    index = 0
    while index < len(words) and words[index].casefold().strip(",;") in _DESCRIPTORS:
        index += 1
    rest = words[index:]
    return not rest or bool(_BASIS_CLAUSE.match(" ".join(rest)))


def _components(printed: str) -> list[tuple[dict[str, object], int, int]]:
    """Each quantity a phrase states, in the order written, as the strict parser reads
    it, with where in `printed` its piece begins and ends. `printed` is the whole phrase
    as it prints: cut before it was read, a formula with a comma in it would come apart
    into halves no reading makes sense of."""

    components = []
    for piece in _PIECE.finditer(printed):
        text = piece.group().strip()
        if not text:
            continue
        parsed = _scalar(text, text)
        if parsed["numeric_value_decimal"] is not None or parsed["range_min_decimal"] is not None:
            components.append((parsed, piece.start(), piece.end()))
    return components


def _authors_piece(raw_text: str, offsets: tuple[int, ...], start: int, end: int) -> str:
    """The author's own words for the rendered piece `[start, end)`: the raw text from
    the piece's first printed character up to the character printed after it."""
    stop = offsets[end] if end < len(offsets) else len(raw_text)
    return raw_text[offsets[start] : stop].strip()


def _preferred_material_amount(components: Sequence[dict[str, object]]) -> dict[str, object] | None:
    """One amount priority for a continuous phrase and explicitly related fragments."""
    for dimension in ("mol", "g", "L"):
        chosen = next((c for c in components if c["normalized_unit"] == dimension), None)
        if chosen is not None:
            return chosen
    return None


class MaterialQuantityProjection(NamedTuple):
    amount: dict[str, object] | None
    equivalents: dict[str, object] | None
    quantities: tuple[dict[str, object], ...]


def project_material_quantity(raw_text: str) -> MaterialQuantityProjection:
    """Parse once for the primary slots and every individually reported quantity.

    Component units remain their own dimensions: concentration and purity never
    become a total amount. No summing or material attribution happens here.
    """
    printed, offsets = author_rendering_offsets(raw_text)
    pieces = _components(printed)
    components = []
    for component, start, end in pieces:
        first = start + len(printed[start:end]) - len(printed[start:end].lstrip())
        components.append({**component, "raw_text": _authors_piece(raw_text, offsets, first, end)})
    chosen = _preferred_material_amount(components)
    amount = {**chosen, "raw_text": raw_text} if chosen is not None else None
    equivalents = next((c for c in components if c["normalized_unit"] == "equiv"), None)
    return MaterialQuantityProjection(amount, equivalents, tuple(components))


def parse_material_quantity(raw_text: str) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Compatibility view of the one material-quantity projection."""
    projection = project_material_quantity(raw_text)
    return projection.amount, projection.equivalents


def parse_written_material_quantity(
    raw_text: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """A written material's amount and equivalents: `parse_material_quantity`'s reading,
    for a phrase with no other home.

    A fusion MATERIAL's amount is a CONDITION, and one the host cannot read stays on the
    operation as a note naming the material. A material written with its words has
    nowhere else to keep them, so a phrase stating no amount and no equivalents -- "ten
    drops" -- stays the amount, every numeric view null, instead of vanishing. Anything
    the parser does read is read exactly as the fusion's phrase is.
    """

    amount, equivalents = parse_material_quantity(raw_text)
    if amount is None and equivalents is None:
        return _empty(raw_text), None
    return amount, equivalents


class MaterialPartsProjection(NamedTuple):
    amount: dict[str, object] | None
    equivalents: dict[str, object] | None
    amount_index: int | None
    equivalents_index: int | None
    quantities: tuple[tuple[int, dict[str, object]], ...] = ()


def project_material_parts(parts: Sequence[str]) -> MaterialPartsProjection:
    """Select values and their source indices together, without joining or summing.

    The two consumers (public values and field evidence) share this exact decision;
    evidence is never guessed later by matching equal numbers or repeated words.
    All original parts remain in the material's source reading.
    """
    if not parts or any(not isinstance(text, str) or not text.strip() for text in parts):
        raise ValueError("material amount parts require nonempty source text")
    projections = [project_material_quantity(text) for text in parts]
    parsed = [(p.amount, p.equivalents) for p in projections]
    amount = _preferred_material_amount([amount for amount, _ in parsed if amount is not None])
    amount_index = (
        next((i for i, (item, _) in enumerate(parsed) if item is amount), None)
        if amount is not None
        else None
    )
    equivalents_index = next((i for i, (_, item) in enumerate(parsed) if item is not None), None)
    equivalents = parsed[equivalents_index][1] if equivalents_index is not None else None
    if amount is None and equivalents is None:
        amount, amount_index = _empty(parts[0]), 0
    return MaterialPartsProjection(amount, equivalents, amount_index, equivalents_index,
        tuple((index, quantity) for index, projection in enumerate(projections)
              for quantity in (projection.quantities or (_empty(parts[index]),))))


def parse_written_material_parts(
    parts: Sequence[str],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    projection = project_material_parts(parts)
    return projection.amount, projection.equivalents


def sourced_material_quantities(parts: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Expose all quantities with the source fragment already selected for this use.

    No numeric or offset fields are supplied by the model. Each part remains
    separate, even when several parts happen to contain equal quantities.
    """
    if not parts:
        return []
    projection = project_material_parts([str(part["text"]) for part in parts])
    return [{"quantity": quantity, "source_parts": [parts[index]]}
            for index, quantity in projection.quantities]


def parse_reported_outcome(raw_text: str) -> tuple[dict[str, object], str | None]:
    """An outcome's value and its basis from the author's phrase.

    "1.58 g (57% over 2 steps)" reports a yield; the percentage is the value (the mass
    stays in `raw_text`), and "over 2 steps" is the basis the yield is measured on. A
    phrase with no percentage gives its first quantity; one with no quantity keeps every
    numeric view null, as the strict parser does.

    The basis is the one the chosen value's own words state: from its piece to the next
    quantity's, and for the first quantity from the start of the phrase. Searched in the
    whole phrase, "71% (84% brsm)" gave 71 measured on recovered starting material.
    """

    printed = author_rendering(raw_text)
    components = _components(printed)
    words = printed
    percentages = [i for i, (c, _, _) in enumerate(components) if c["unit"] == "%"]
    # A bounded/unreadable percentage is still a percentage, never permission to
    # publish the accompanying mass as the outcome (E196).
    if components and (percentages or "%" not in printed):
        index = percentages[0] if percentages else 0
        chosen, start, _ = components[index]
        end = components[index + 1][1] if index + 1 < len(components) else len(printed)
        scalar = {**chosen, "raw_text": raw_text}
        words = printed[start if index else 0 : end]
    else:
        scalar = _empty(raw_text)
    match = _BASIS.search(words)
    basis = None
    if match is not None:
        basis = " ".join(match.group(0).strip("() ").split())
    return scalar, basis


def parse_step_count(raw_text: str) -> int | None:
    """The author's step count: "22 steps", "seven steps", "13-step", or the bare number,
    read as the phrase prints."""

    text = " ".join(author_rendering(raw_text).split())
    if text.isdigit():
        return int(text)
    match = _STEP_COUNT.search(text)
    return None if match is None else _count(match.group(1))
