"""The Host's one table for a condition's kind: keys the model selects from (ledger E61).

A CONDITION fact carries the author's words for the condition (`text`) and the model's
word for what kind of condition it is (`category`). The write declaration offers the
model this table's keys, each with a one-line meaning, to choose from; when none fits,
the model writes its own word and the key stays null -- visibly, in the open slot
`Operation.other_conditions`, with the model's word kept as `category_verbatim`.

The Host never reads a kind off the prose. A key comes from exactly three places:

1. the model's selection -- its word, spelled canonically, is a key of this table;
2. a recorded human decision -- the author's exact words are in `condition_decisions`;
3. the structure a condition hangs on -- a concentration on a material is the strength
   of that material's solution, which belongs to its amount phrase (`SOLUTION_STRENGTH`).

Ledger E57 matched patterns over the model's word and the author's words instead; the
deep audit (L8-c) showed the key then depended on the model's wording and misread
"0.5 nM" as light, "MW 342.4" as microwave heating, "O2 bubbled" as degassing. A
null key is a visible gap; a wrong key is an invisible one. The table grows from the
corpus: the null-keyed entries are counted, clustered offline, and a person names a
cluster's key -- then it is added here (never removed) and recorded as a decision for
the author's words already seen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from tsdaf.contracts.literature.expressions import expressed_value
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature import condition_decisions
from tsdaf.lanes.literature.material_meanings import (
    MATERIAL_AMOUNT_MEANING,
    REAGENT_PREPARATION,
    SOLUTION_STRENGTH,
)

__all__ = (
    "CONDITION_KEYS",
    "FIXED_CONDITION_FIELDS",
    "ConditionKey",
    "canonical_key",
    "fold_text",
    "merge_condition",
    "normalize_condition",
    "same_condition",
    "stage_covers",
)


@dataclass(frozen=True)
class ConditionKey:
    """One kind of condition: what it means, in one line the declaration shows the
    model, and the public Operation field it fills when it has one."""

    meaning: str
    field: str | None = None


# Every key the model may select, with its meaning. The vocabulary the write
# declarations and prompts name is exactly these keys; they generate it from here. A
# meaning also draws the line where the audit saw conditions cross it: a solvent is a
# MATERIAL, a purification is an OPERATION, the rotavap's vacuum is not the reaction's
# pressure. Keys are added from corpus clusters a person has looked at, never removed.
CONDITION_KEYS: dict[str, ConditionKey] = {
    "TEMPERATURE": ConditionKey(
        "a temperature the reaction is held at or brought to (reflux, -78 °C, rt); several"
        " on one operation are its profile in the order written",
        "temperature_profile",
    ),
    "DURATION": ConditionKey(
        "how long the operation or a stage of it runs (2 h, overnight)", "duration"
    ),
    "ATMOSPHERE": ConditionKey(
        "the gas the reaction is run under (argon, N2, air, H2 balloon)", "atmosphere"
    ),
    "PRESSURE": ConditionKey(
        "the pressure the reaction is run at (4 atm H2, 50 bar); the vacuum of a"
        " concentration or distillation is REDUCED_PRESSURE, not this",
        "pressure",
    ),
    # A reagent's solution and a material's amount are the material table's meanings,
    # rendered here: the fusion writes a material's amount as its SCALE condition, and
    # the middle layer writes the same phrase as the material's amount.
    "CONCENTRATION": ConditionKey(
        "the concentration of the reaction mixture (0.1 M, c = 0.05 M); " + SOLUTION_STRENGTH,
        "concentration",
    ),
    "SCALE": ConditionKey(
        "the amount the operation is run on; on a MATERIAL, " + MATERIAL_AMOUNT_MEANING,
        "scale",
    ),
    "ADDITION": ConditionKey(
        "how or in what order a material is added (dropwise, portionwise, over 30 min, added last)",
        "key_addition",
    ),
    "WORKUP": ConditionKey(
        "how the reaction is quenched, washed or extracted; purification does not by itself"
        " create a separate operation. Its media are QUENCH_WORKUP materials",
        "key_workup",
    ),
    "MICROWAVE": ConditionKey("heated by microwave irradiation (MW, μW)"),
    "PHOTOCHEMICAL": ConditionKey(
        "driven by light: a lamp, LED or hv at a wavelength; keeping light out is LIGHT_EXCLUSION"
    ),
    "SONICATION": ConditionKey("run under ultrasound (sonication, ultrasonic bath)"),
    "FLOW": ConditionKey("run in continuous flow or a flow reactor rather than a flask"),
    "SEALED_VESSEL": ConditionKey(
        "run in a sealed tube, pressure tube, autoclave or bomb; an ordinary flask is"
        " not a condition"
    ),
    "DEGASSED": ConditionKey(
        "the mixture or solvent was degassed (freeze-pump-thaw, sparged with an inert"
        " gas); bubbling a reactive gas through is that gas's ATMOSPHERE"
    ),
    "ONE_POT": ConditionKey(
        "carried out in the same pot as the preceding operation without isolation"
        " (one-pot, telescoped, tandem)"
    ),
    "STOCK_SOLUTION": ConditionKey(
        "how a reagent solution was supplied or handled for use (titrated before use); "
        + SOLUTION_STRENGTH
        + ". "
        + REAGENT_PREPARATION
    ),
    "MONITORING": ConditionKey(
        "how completion was judged (monitored by TLC, until consumption of the starting material)"
    ),
    "STIRRING": ConditionKey("a stated stirring or agitation regime (vigorous stirring, rpm)"),
    "REDUCED_PRESSURE": ConditionKey(
        "under vacuum for a concentration, distillation or removal of a volatile"
        " (in vacuo, rotary evaporation, reduced pressure)"
    ),
    "LIGHT_EXCLUSION": ConditionKey("kept from light (amber flask, wrapped in foil, in the dark)"),
    "SOLVENT_RATIO": ConditionKey(
        "the proportion of mixed reaction solvents (THF:H2O 4:1, 8:1 THF/1 M NaOH); "
        "the solvents themselves are MATERIALs. Chromatography eluents and their ratios "
        "belong to QUENCH_WORKUP materials"
    ),
}

# Key -> the public Operation field it fills, derived from the table: the eight fixed
# kinds are the entries that name a field.
FIXED_CONDITION_FIELDS: dict[str, str] = {
    key: entry.field for key, entry in CONDITION_KEYS.items() if entry.field is not None
}


def canonical_key(word: str) -> str:
    """The model's word as a key spelling: upper case, runs of anything but letters and
    digits as one underscore. "one-pot", "One pot" and "ONE_POT" are one spelling."""
    parts = "".join(ch if ch.isalnum() else " " for ch in word).split()
    return "_".join(parts).upper()


def fold_text(text: str) -> str:
    """The author's words with whitespace folded: the form the decision table keys on."""
    return " ".join(text.split())


def normalize_condition(
    category_verbatim: str, text: str, *, about: str | None = None
) -> str | None:
    """The key for a condition, or None when neither the model nor a recorded decision
    named one.

    The model's selection comes first: its word, spelled canonically, is a key. Then a
    human decision recorded against the author's exact words. A condition on a material
    (`about`) that is a concentration is not the reaction mixture's: it is the strength of
    that material's solution, STOCK_SOLUTION, a key whose meaning puts it in the
    material's amount phrase and which fills no field of the operation. The model was
    asked to write it there; written apart, it stays a note naming the material rather
    than the operation's concentration. Nothing here reads the prose for a kind.
    """
    if not isinstance(category_verbatim, str) or not isinstance(text, str):
        raise TypeError("category_verbatim and text must be strings")
    key: str | None = None
    selected = canonical_key(category_verbatim)
    if selected in CONDITION_KEYS:
        key = selected
    else:
        key = condition_decisions.CONDITION_DECISIONS.get(fold_text(text))
    if about is not None and key == "CONCENTRATION":
        return "STOCK_SOLUTION"
    return key


# A malformed decision fails here, where the tables load, not inside a projection.
condition_decisions.check_decisions(condition_decisions.CONDITION_DECISIONS, CONDITION_KEYS)


# A word that carries an amount, a duration or a temperature setting. Two readings of one
# condition differ in prose ("in a 2 dram vial" against "2 dram vial"); two conditions
# differ in what they state. The words below are what a second condition states.
_SPELLED_NUMBERS = frozenset(
    """one two three four five six seven eight nine ten eleven twelve thirteen fourteen
    fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy
    eighty ninety hundred thousand half once twice several few couple""".split()
)
_UNITS = frozenset(
    """h hr hrs hour hours min mins minute minutes s sec secs second seconds d day days
    week weeks °c °f k m mm nm µm um mmol mol equiv equivalents ml l µl ul g mg kg atm
    psi torr bar mbar % ppm""".split()
)
# A temperature written as a word rather than a number is still a temperature.
_SETPOINTS = frozenset("rt room temperature reflux overnight ambient ice bath".split())
# These state no amount at all; they state that this reading is not the stretch the
# other one describes. `40 minutes` and `an additional 40 minutes` sit on one operation
# of the hyperfirin supplement, and under the quantity test alone they merged -- the
# added word is not a digit, a spelled number, a unit or a setpoint -- so one of the two
# durations was gone before anything could be attributed to a part of the procedure.
# Ordinary English, and the same class of word in any paper: not a rule about one paper.
_PLACES_IT_LATER = frozenset(
    "additional further another again then subsequently afterwards".split()
)
_STATES_A_SECOND_CONDITION = _SPELLED_NUMBERS | _UNITS | _SETPOINTS | _PLACES_IT_LATER


def _words(text: str) -> tuple[str, ...]:
    return tuple(fold_text(text).casefold().split())


def _states_a_second_condition(word: str) -> bool:
    """Whether a word the longer reading adds makes it a different condition.

    Two ways it can: by stating how much (a number, a unit, a temperature written as a
    word), or by placing itself after what the other reading describes.
    """
    bare = "".join(ch for ch in word if ch.isalnum() or ch in "°%\u00b5")
    return any(ch.isdigit() for ch in word) or bare in _STATES_A_SECOND_CONDITION


def _run_start(whole: Sequence[str], part: Sequence[str]) -> int | None:
    """Where `part` sits inside `whole` as a run of consecutive words, if it does.

    Words, never characters. "0 °C" is a substring of "-40 °C", "4 h" of "24 h" and "2 h"
    of "12 h" -- every one of those values is in this corpus, and a character test merges
    each pair into a single wrong condition.
    """
    for start in range(len(whole) - len(part) + 1):
        if tuple(whole[start : start + len(part)]) == tuple(part):
            return start
    return None


def same_condition(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> bool:
    """Whether two notes on one operation are one condition read twice (ledger E114).

    Two tasks read one procedure -- a block task and the paper task, or two block tasks
    sharing a telescoped paragraph -- and write the same condition in their own prose. The
    Host kept both whenever a single word differed, so a published operation carried
    `2 dram vial` beside `in a 2 dram vial`, `TLC` beside `monitored by TLC to establish
    completion`, `1:1` beside `1:1 hexanes/EtOAc`.

    One condition, on these terms only:

    1. The same words are one condition however the table keyed them -- the rule that
       was already here. Past that, both readings must name a kind the table knows, and
       the same one: a word the table cannot key is a visible gap (E61) and never
       absorbs a reading that says something else.
    2. What the condition is about agrees: the same material, or one reading naming it
       and the other not. Two materials is two conditions -- the corpus has `dropwise`
       for I2 beside `dropwise over a five minute period` for n-BuLi on one operation.
    3. Either the words match once a named material is folded in, or one reading's words
       sit inside the other's as a run of consecutive words.
    4. What the longer reading adds is prose, and neither a quantity nor a placement:
       no digit, no spelled number, no unit, no temperature setting, and no word that
       puts it after the other reading. `stirred` and `The mixture was stirred for 5
       min` are a whole operation and one stage of it, and stay two; so do `40 minutes`
       and `an additional 40 minutes`, which are two stretches of one procedure.
    """
    if _wrapped_condition(existing) or _wrapped_condition(incoming):
        return _condition_contribution(existing) == _condition_contribution(incoming)
    if existing.get("_stage_key") != incoming.get("_stage_key"):
        # Two stretches of one procedure are two conditions even word for word: a
        # procedure that ran forty minutes cold and forty minutes warmed states two
        # durations. Unknown is not a wildcard either -- an unplaced reading may be
        # this stretch or another, and merging it would decide that by accident.
        return False
    here, there = existing.get("about"), incoming.get("about")
    if here is not None and there is not None and fold_text(here) != fold_text(there):
        return False
    mine, yours = _words(existing["text"]), _words(incoming["text"])
    if mine == yours:
        # The same words on one operation were already one condition with both citations,
        # whatever the table made of them. That much holds for a word the table cannot
        # key too; everything below needs a key to reason with.
        return True
    key = existing.get("category")
    if key is None or key != incoming.get("category"):
        return False
    if mine + _words(here or "") == yours + _words(there or ""):
        return True
    whole, part = (mine, yours) if len(mine) >= len(yours) else (yours, mine)
    start = _run_start(whole, part)
    if start is None:
        return False
    added = (*whole[:start], *whole[start + len(part) :])
    return not any(_states_a_second_condition(word) for word in added)


def _wrapped_condition(note: Mapping[str, Any]) -> bool:
    return any(
        isinstance(note.get(key), Mapping) for key in ("text", "category_verbatim", "extent")
    )


def _condition_contribution(note: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in note.items() if key != "evidence_refs"}


def merge_condition(existing: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """The one record two readings of one condition leave behind.

    Content decides every field, so the two readings may arrive in either order: the
    fuller wording survives -- and by `same_condition` it holds every word of the one it
    absorbs, which is the guarantee the characterization merge could not make (E111: a
    LaTeX rendering and a clean one share no containment at all, so there length asked
    the wrong question) -- the named material is kept by whichever reading named it, the
    narrowest extent any reading states is the condition's (the rule the projection
    already applies to a fact's witnesses), and both readings' evidence is cited.
    """
    if _wrapped_condition(existing) or _wrapped_condition(incoming):
        if not same_condition(existing, incoming):
            raise ValueError("distinct condition expressions require independent contributions")
        merged = dict(existing)
        refs = {
            canonical_json_bytes(ref): ref
            for ref in (*existing.get("evidence_refs", ()), *incoming.get("evidence_refs", ()))
        }
        merged["evidence_refs"] = [refs[key] for key in sorted(refs)]
        return merged
    fuller, lesser = existing, incoming
    if (len(_words(incoming["text"])), fold_text(incoming["text"])) > (
        len(_words(existing["text"])),
        fold_text(existing["text"]),
    ):
        fuller, lesser = incoming, existing
    merged = dict(fuller)
    named = fuller.get("about")
    merged["about"] = named if named is not None else lesser.get("about")
    merged["extent"] = (
        "WITHIN_OWNER"
        if "WITHIN_OWNER" in (existing.get("extent"), incoming.get("extent"))
        else fuller["extent"]
    )
    stage = fuller.get("_stage_key") or lesser.get("_stage_key")
    if stage is not None:
        merged["_stage_key"] = stage
        merged["extent"] = "WITHIN_OWNER"
    seen = {
        canonical_json_bytes(ref): ref
        for ref in (*existing.get("evidence_refs", ()), *incoming.get("evidence_refs", ()))
    }
    merged["evidence_refs"] = [seen[key] for key in sorted(seen)]
    return merged


# The Host's working name for the stretch of a procedure a condition was read in. It
# decides what is one condition and what the whole operation may claim, and it is not
# part of the published shape: a stretch consumers can query is a public contract
# change, with its own version, fixtures and pins.
STAGE_KEY = "_stage_key"


def stage_of(condition: Any) -> str | None:
    """Which stretch of the procedure a write says it read this condition in.

    A write may name one and say `WHOLE_OWNERS` beside it. Those cannot both be true of
    the same words, and only one of them was checked against anything: the named stretch
    is what the model actually located, so it decides.
    """
    key = getattr(condition, "stage_key", None)
    return key.strip() if isinstance(key, str) and key.strip() else None


def public_note(note: Mapping[str, Any]) -> dict[str, Any]:
    """One condition note as the published record holds it: without the Host's own keys."""
    return {key: value for key, value in note.items() if not key.startswith("_")}


def stage_covers(operation: Mapping[str, Any], category: str) -> bool:
    """Whether a stretch of this operation's procedure already owns conditions of this
    kind: a note of the kind recorded WITHIN_OWNER, with or without the Host's working
    stage key on it.

    The one predicate that decides where a whole-operation value of a fixed kind goes
    (ledger E157). It routes and derives, and never refuses: on the
    write path a value it covers is kept as unassigned instead of filed, and at
    finalize a filed value it covers is withdrawn, so the order the two arrived in
    stops mattering. It reads no ledger and compares no words -- the two text guards
    it replaced (E118, E129) both missed f07's five field takeovers, one blind to the
    working key it never set, the other undone by the very words that mark a second
    stretch. A note that names no stage counts too: the fusion projection writes a
    stage's condition as a pending field beside a WITHIN_OWNER note without any key,
    and `decumbenine_B-a1`'s nitrogen (E118) is that note.
    """
    return any(
        expressed_value(note.get("extent")) == "WITHIN_OWNER" and note.get("category") == category
        and (
            "category_verbatim" not in note
            or expressed_value(note["category_verbatim"]) is not None
        )
        for note in operation.get("other_conditions", ())
    )
