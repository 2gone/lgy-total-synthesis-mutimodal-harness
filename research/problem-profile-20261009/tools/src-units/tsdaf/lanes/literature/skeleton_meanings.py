"""Skeleton conventions shared by observers, fusion and experimental interpretation.

These are scientific instructions, not a classifier. Examples are development cases,
not a production checklist. Existing material, outcome and identity meanings stay in
those modules. Only stdlib is imported when loading a reader's frozen resources.
"""

from __future__ import annotations

from dataclasses import dataclass

LOCAL_INTERMEDIATE = "LOCAL_INTERMEDIATE"
NON_ROUTE_REACTION_CATEGORIES = frozenset(
    {"MECHANISM", "FAILED_ATTEMPT", "OPTIMIZATION", "RETROSYNTHESIS"}
)


@dataclass(frozen=True)
class SkeletonConvention:
    meaning: str
    consumers: tuple[str, ...]
    positive: str
    negative: str


_OBSERVERS = ("T1", "V1")
_INTERPRETERS = ("FUSION", "EXPERIMENTAL_READING", "EXPERIMENTAL_RECORDING")
# Route topology is settled before the accounts are written: the recording task receives
# the route as context and does not need the conventions for building it.
_ROUTE_BUILDERS = ("FUSION", "EXPERIMENTAL_READING")
_ALL = (*_OBSERVERS, *_INTERPRETERS, "EXPERIMENTAL_DETAIL", "SUPPLEMENTAL_INFORMATION")

CONVENTIONS = {
    "boundaries": SkeletonConvention(
        "Follow the author's numbered endpoints and arrows. One reaction may contain several "
        "operations, one-pot additions or a)/b) conditions. Two arrows with a numbered "
        "intermediate are two steps even when the crude intermediate is used directly; an "
        "unnumbered intermediate isolated AND characterized also ends a step. An unnumbered, "
        "unisolated species belongs inside its operation: keep its name, invent no author label. "
        "Protection or purification alone is not a reason to invent another reaction step.",
        _ALL,
        "23 to numbered crude 24 to 25: two steps; a)/b) on one arrow: operations of one step.",
        "Merge 23 to 25 because 24 was crude, or split every reagent addition into a step.",
    ),
    "authority": SkeletonConvention(
        "Main text owns route topology. For genuinely conflicting accounts of the same event "
        "and scope, main-text prose takes precedence over its figure. A prose summary that "
        "omits a figure's supported intermediate is not a contradiction. Experimental details "
        "from MAIN or SI can correct the corresponding local material, amount, condition, "
        "isomer actually used or yield; SI phase alone is not priority. Finer detail is not "
        "a topology repair. Preserve unrelated skeleton values and supporting evidence.",
        _INTERPRETERS,
        "A to C summarizes a drawn A to B to C; SI specifies which isomer was actually used.",
        "Erase B because the summary omits it, or replace topology merely because SI is finer.",
    ),
    "connected_routes": SkeletonConvention(
        "Preserve branches and convergence: a coupling can consume both branches. Each "
        "declared target has its own supported path; shared steps are a single shared history "
        "within the assigned route group. Keep complete alternative generations and structure-revision "
        "routes distinct, with their author labels and scopes. An abandoned partial route "
        "is a failed attempt, not an invented path to the target. The routing assignment is "
        "navigation; report an unsupported assignment or missing route scope rather than "
        "inventing cross-group identities.",
        (*_OBSERVERS, *_ROUTE_BUILDERS),
        "A common prefix leads to two targets; two fragments converge in one coupling.",
        "Duplicate the prefix for each target, or merge different generations by label alone.",
    ),
    "target_scope": SkeletonConvention(
        "A formal synthesis stops at the author's achieved endpoint: retain which target "
        "it is a formal synthesis of, without importing later literature steps. An analogue "
        "or asymmetric variant reaching an explicit target has a target path; a single-step "
        "demonstration is contextual work. Do not reconstruct the preparation of a fragment "
        "only described as prepared according to literature.",
        (*_OBSERVERS, *_ROUTE_BUILDERS, "SUPPLEMENTAL_INFORMATION"),
        "Keep the achieved formal endpoint and the author's formal-target statement.",
        "Append unperformed literature steps to make a full total synthesis.",
    ),
    "experiment_scope": SkeletonConvention(
        "The experiment with the complete procedure supplies the main experimental details. "
        "Keep an optimization table as connected original evidence; only the final procedure's "
        "conditions belong to the main procedure; table rows remain distinct experimental contexts. "
        "Scaled repeats, alternative runs and their outcomes remain other experiments, not "
        "extra conditions or yields of the main operation.",
        (*_OBSERVERS, *_INTERPRETERS, "EXPERIMENTAL_DETAIL"),
        "Keep the full optimization table with its step and use the final complete procedure.",
        "Combine a scaled run's yield with the original run's quantities.",
    ),
    "route_extras": SkeletonConvention(
        "Retain recovery, epimerization or byproduct reuse outside the main route as contextual "
        "chemistry; do not assign their yields to a main-route result. Likewise preserve "
        "off-route starting-material preparations, mechanistic, isotope, control and failed "
        "experiments with their source scope. Classify from the author's actual purpose, "
        "not a reaction-name keyword. Preserve content whose route relationship is uncertain "
        "with that uncertainty, without fabricating a route step.",
        _ALL,
        "Retain the off-route recovery of 14b from 14a with its own 65% brsm statement.",
        "Put that 65% on the main reaction, or mark every epimerization as off-route by name.",
    ),
    "outcome_scope": SkeletonConvention(
        "An aggregate yield over N steps is one result spanning its supported steps, retained "
        "where the author reports it, never a yield copied to each step. Combined product "
        "yields and stereoisomer ratios belong to the reaction that formed those products; "
        "a yield and a ratio are distinct values. Preserve brsm, crude and not-isolated "
        "qualifications. No isolated yield does not mean a failed or missing step.",
        (*_OBSERVERS, *_INTERPRETERS),
        "One 57% over two steps outcome; separate 97% yield and dr 3:1 for the same reaction.",
        "Assign 57% to each step or mark a crude intermediate as a failed synthesis.",
    ),
    "author_identity": SkeletonConvention(
        "Keep the author's species and identity scope: tautomers, rotamers and salts follow "
        "the identity the author assigns, not a guessed structure. An abbreviation and its "
        "established full name are names of one identity. Repeated labels across unrelated "
        "figures or routes alone do not establish identity. A statement that data agree "
        "with literature is original context, not a missing measurement or a value to invent.",
        _ALL,
        "Retain a literature-data agreement statement without inventing a measured value.",
        "Merge unrelated schemes' compound 1 solely because both use that number.",
    ),
}


def skeleton_guidance(consumer: str) -> str:
    """Only the conventions needed by this existing task; no model-facing rule ids."""
    meanings = [rule.meaning for rule in CONVENTIONS.values() if consumer in rule.consumers]
    return "Route conventions:\n" + "\n".join("- " + meaning for meaning in meanings)


TEXT_QUOTE_KINDS = ("CONDITION", "OUTCOME", "TARGET")
TARGET_QUOTE_NOTE_CATEGORIES = ("AUTHOR_STEP_COUNT", "AUTHOR_OVERALL_YIELD")


def text_quote_note_categories() -> tuple[str, ...]:
    # Lazy for the same frozen reader-resource boundary as fact_meanings.
    from tsdaf.contracts.literature.schema import fact_enum

    return ("CHARACTERIZATION", *TARGET_QUOTE_NOTE_CATEGORIES, *fact_enum("NarrativeKindValue"))


def requires_author_words(kind: str, category: str | None) -> bool:
    """The original text field, not parsed scalars, classification keys or rationale."""
    return kind in TEXT_QUOTE_KINDS or (kind == "NOTE" and category in text_quote_note_categories())


def fusion_fact_meanings(*, unified_expression: bool = False) -> str:
    """Native fact meanings for the task prompt, never a second field schema."""
    # These imports are only needed by the fusion task. T/V resource loading
    # must not import Pydantic proposal models or schema generators.
    return (
        "Native fact meanings:\n"
        + "\n".join(f"- {kind}: {meaning}" for kind, meaning in fact_meanings(unified_expression=unified_expression).items())
        + (
            "\nFor "
            + "/".join(TEXT_QUOTE_KINDS)
            + " text and NOTE text in "
            + "/".join(text_quote_note_categories())
            + ", when all cited sources are "
            "original text already read here, use the author's words from those citations "
            "(printed equivalents are accepted). This check does not require a visual value "
            "to occur in text, or require inherited findings to be reread."
        )
    )


def fact_meanings(*, unified_expression: bool = False) -> dict[str, str]:
    from tsdaf.contracts.literature.schema import fact_enum
    from tsdaf.lanes.literature.characterization_methods import CHARACTERIZATION_WRITING_MEANING
    from tsdaf.lanes.literature.material_meanings import MATERIAL_ROLES
    from tsdaf.lanes.literature.outcome_meanings import OUTCOME_KINDS

    meanings = {
        "COMPOUND": (
            "text is a source-supported name or designation; labels are author-assigned. "
            "Put explanations in rationale or a sourced NOTE, not the name. Names do not "
            "establish structure. Omit unsupplied labels. A species formed and consumed "
            f"inside one reaction is CONTEXT, category {LOCAL_INTERMEDIATE}, owned by the "
            "operation forming it; a name alone does not make it an input, product or target."
        ),
        "REACTION": (
            "inputs and outputs name compound facts at the supported block boundary. "
            "A main-route step is ACCEPTED with no category. Off-route relations may be "
            "CONTEXT, retaining their evidence without entering the public route. The "
            "existing contextual categories "
            + "/".join(sorted(NON_ROUTE_REACTION_CATEGORIES))
            + " also do not publish route steps or require operations. Do not invent an "
            "ACCEPTED category to mean off-route."
        ),
        "OPERATION": (
            "text describes an actual action; category TRANSFORMATION/WORKUP/PURIFICATION/"
            "TRANSFER/OTHER; owners names its reaction and after its actual predecessor "
            "operations. Invent no operation when the source gives only a reaction boundary."
        ),
        "MATERIAL": (
            "text names one material; category "
            + "/".join(MATERIAL_ROLES)
            + ", chosen by the declared material-role meanings; "
            "owners names the operation using it. Every stated reagent, solvent and catalyst "
            "is its own material even if already mentioned in the operation's text."
        ),
        "CONDITION": (
            "text is the author's condition wording; category follows the declared condition "
            "meanings, or your own word if none fits (kept with a null public key, never "
            "inferred from text). owners names the operation OR material modified. Multiple "
            "TEMPERATURE conditions on an operation are its profile in written order. "
            "Use WITHIN_OWNER and explain in rationale for a substage, not the whole operation."
        ),
        "OUTCOME": (
            "text follows the declared outcome phrase meaning; category "
            + "/".join(OUTCOME_KINDS)
            + ", chosen by the declared kind meanings. owners "
            "lists exactly the measured operations and outputs the products measured. "
            "WHOLE_OWNERS covers those whole operations; WITHIN_OWNER is a substage "
            "explained in rationale."
        ),
        "TARGET": (
            "owners names the declared target compound; text is the author's target statement. "
            "Author step counts and overall yields are separately sourced NOTE facts with "
            "category "
            + "/".join(TARGET_QUOTE_NOTE_CATEGORIES)
            + " owned by TARGET; text is the author's count or yield phrase, which the host "
            "parses into a value. Assertions "
            "use two sourced NOTE facts owned by TARGET: ASSERTION_SUBJECT ("
            + "/".join(fact_enum("AssertionSubjectValue"))
            + ") and ASSERTION_OUTCOME ("
            + "/".join(fact_enum("AssertionOutcomeValue"))
            + "); their text is the listed classification value, not a quote. "
            "Never compute author claims from graph length or yield multiplication."
        ),
        "NOTE": (
            "A sourced statement with a public home. CHARACTERIZATION: owners are measured "
            "COMPOUND facts. " + CHARACTERIZATION_WRITING_MEANING
            + " Rationale may describe sample context. Paper narrative categories are "
            + "/".join(fact_enum("NarrativeKindValue"))
            + "; text is the author's words and rationale your explanation. owners names "
            "the reaction or operation concerned; MECHANISM/OPTIMIZATION/FAILED_ATTEMPT "
            "attach to that block. The TARGET-owned categories retain their meanings. "
            "Biological activity and assays are outside this task; other prose stays in "
            "raw evidence rather than being parsed into invented facts."
        ),
    }

    if unified_expression:
        from tsdaf.lanes.literature.gate_models import UnifiedFactChange

        meanings["EXPERIMENT"] = (
            "A source-supported experimental account, independently of a main-route step. "
            "Operations may name it as their owner; do not fabricate a route reaction for it."
        )
        meanings["OPERATION"] = (
            "text describes an actual action; category TRANSFORMATION/WORKUP/PURIFICATION/TRANSFER/OTHER. "
            "Follow the declared owners and after relations; invent no action from a boundary alone."
        )
        meanings["OUTCOME"] = (
            "text follows the declared outcome phrase meaning; category "
            + "/".join(OUTCOME_KINDS)
            + ". Follow the declared owners and outputs separately from measurement identity. "
            "Retain unsupported ownership as unresolved rather than inferring it from a product or value."
        )
        meanings["OUTCOME"] += " measurement: " + (UnifiedFactChange.model_fields["measurement"].description or "")
        meanings["NOTE"] = (
            "A source-grounded statement retaining its category, subjects, source text and "
            "interpretation. Use the declared characterization and TARGET categories for "
            "their meanings; unfamiliar narrative categories remain explicit source expressions. "
            "A narrative does not create unstated experimental graph relationships."
        )
    return meanings
