"""The Host's one table for what an OUTCOME's kind means.

The contract's outcome kinds were names without meanings. The middle layer's declaration
said "Choose the declared outcome kind", and the fusion's gave one example; on hyperfirin
b4-a4 the model wrote STEREOISOMER_RATIO and UNKNOWN for a dr, and the two findings and
the two rulings naming them were refused. The fusion writes the kind as an OUTCOME's
category and the middle layer as a yield's `kind`, and once the middle layer's kind
compiles (owner decision D11) a kind chosen without a meaning changes what is published.
So the kinds get their meanings here, in the style of the material roles
(`material_meanings`) and the condition keys (E61): both write declarations render the
table, and the prompts name the kinds without saying what they mean. The keys are the
contract's `OutcomeKindValue` in its order, and a test holds the two equal.

What an outcome's words are is said once too, `OUTCOME_VALUE_PHRASE`: the author's phrase
for one reported value, which the host reads the number and the basis from
(`parse_reported_outcome`). The fusion writes it as an OUTCOME's text and the middle
layer as a yield's `text`, and both declarations render it. The middle layer once
defined it as a whole statement plus the model's cuts of it (a percentage phrase, an
amount phrase): each cut was quoted on its own and refused when it was not verbatim (4
writes on hyperfirin b4-a3), and a statement paired with the route's value never read as
the same value (every pairing on a3 and a4 refined it).
"""

from __future__ import annotations

__all__ = (
    "OUTCOME_KINDS",
    "OUTCOME_KIND_GUIDANCE",
    "OUTCOME_KIND_WORDS",
    "OUTCOME_VALUE_PHRASE",
    "outcome_kind_guidance",
    "outcome_kind_word",
)

OUTCOME_KINDS: dict[str, str] = {
    "ISOLATED_YIELD": (
        "the yield of a product the author isolated, with or without its mass (2.65 g"
        " (97%), 42 mg (52%), 70% isolated yield)"
    ),
    "REPORTED_YIELD": (
        "a yield the author states without saying how it was determined, as a scheme or"
        " the narrative often does (81% over 3 steps, quantitative)"
    ),
    "ASSAY_YIELD": (
        "a yield measured on the unpurified mixture by GC, HPLC or another calibrated assay"
    ),
    "NMR_YIELD": "a yield measured on the unpurified mixture by NMR against an internal standard",
    "BRSM_YIELD": "a yield based on recovered starting material (71% brsm)",
    "EE": "the product's enantiomeric excess (98% ee)",
    "DR": (
        "the product's diastereomeric ratio, a single diastereomer included (3:1 dr, >20:1"
        " d.r., single diastereomer)"
    ),
    "RATIO": (
        "any other ratio between products or isomers (E/Z 5:1, a regioisomer ratio, an"
        " enantiomeric ratio)"
    ),
    "RECOVERED_STARTING_MATERIAL": (
        "how much starting material was recovered unreacted (15% rsm, 323 mg (37%) of the"
        " starting diastereomer)"
    ),
    # A run's input is the SCALE condition (Operation.scale); this kind is only what
    # the run made (review of batch 1: both meanings once claimed "20 g scale").
    "SCALE": (
        "how much product a run made where the author states an amount but no yield (1.52 g"
        " of 14b); the amount a run is carried out on is the SCALE condition, not this"
    ),
}


def outcome_kind_guidance() -> str:
    """The kind table as the sentence both write declarations show the model."""
    kinds = "; ".join(f"{kind}: {meaning}" for kind, meaning in OUTCOME_KINDS.items())
    return "Choose one of these kinds by its meaning: " + kinds + "."


OUTCOME_KIND_GUIDANCE = outcome_kind_guidance()

# What an outcome's words are, for the fusion's OUTCOME text and the middle layer's yield
# `text` alike. One value per phrase: a value is read, with its basis, from its own
# words, so two values written together are two phrases.
OUTCOME_VALUE_PHRASE = (
    "The author's words for one reported value, copied as written with the mass and basis"
    ' written with it ("1.58 g (57% over 2 steps)", "84% brsm", "dr = 3:1"). Two values'
    ' written together are two phrases: "71% (84% brsm)" is "71%" and "84% brsm". The'
    " host reads the number and the basis from these words; do not calculate or restate"
    " them."
)


# What an outcome's kind is called where a model reads it by name: the type word in a
# measurement's name, so "yield 1" and "scale 1" say what they are without saying what
# they hold (owner ruling of 2026-09-26: a name that held the content would move the
# moment the middle layer corrects the value, and a model that copied the old one would
# be refused). One word per kind, beside the meaning each kind already has here.
OUTCOME_KIND_WORDS: dict[str, str] = {
    "ISOLATED_YIELD": "yield",
    "REPORTED_YIELD": "yield",
    "ASSAY_YIELD": "assay yield",
    "NMR_YIELD": "NMR yield",
    "BRSM_YIELD": "yield",
    "EE": "ee",
    "DR": "dr",
    "RATIO": "ratio",
    "RECOVERED_STARTING_MATERIAL": "recovered starting material",
    "SCALE": "scale",
}


def outcome_kind_word(kind: str | None) -> str:
    """What this kind is called in a name. An unresolved kind, or one the table does not
    hold, is called a measurement: the host does not guess what the model meant."""
    return OUTCOME_KIND_WORDS.get(kind or "", "measurement")
