"""The Host's one table for what a MATERIAL is: the role it plays and how its amount is
written.

The model chooses a material's role from this table, each role with its meaning, the
way it chooses a condition's key (ledger E61). The meanings used to live in one
parenthesis of the fusion prompt. The middle layer never saw it and coined REACTANT,
DRYING_AGENT, EXTRACT_WORKUP, EXTRACTING_AGENT and EXTRACTIVE_SOLVENT (10 refused writes
on hyperfirin b4-a3, 12 on a4); a drying agent it did name in the vocabulary was an
ADDITIVE in 2 of 12 cases on a3 and 11 of 13 on a4. Both write declarations render the
table from here, and the prompts name the roles without saying what they mean. The keys
are the contract's `Material.role` vocabulary in its order: the public format is
versioned, so the table follows it, and a test holds the two equal.

What a material's amount is, is said once too. It is the author's whole phrase for how
much, which the host reads once (`parse_written_material_quantity`): the fusion writes
it as the SCALE condition on a material, the middle layer as the material's `amount`,
and both declarations render `MATERIAL_AMOUNT_MEANING`. The strength of a reagent's
solution is part of that phrase (the owner's ruling, option A), and the two condition
keys that name a solution's strength say so in one sentence, `SOLUTION_STRENGTH`.

Only the standard library is imported here: the condition table, the fusion's models
and the middle layer's words all read this module.
"""

from __future__ import annotations

__all__ = (
    "MATERIAL_AMOUNT_MEANING",
    "MATERIAL_NAME_MEANING",
    "MATERIAL_ROLES",
    "MATERIAL_ROLE_GUIDANCE",
    "SOLUTION_STRENGTH",
    "REAGENT_PREPARATION",
    "material_role_guidance",
)

MATERIAL_NAME_MEANING = (
    "One material name selected from the cited wording. Keep chemically meaningful "
    "isotopes, charges and subscripts; a trailing reference marker need not be copied. "
    "The host locates the chosen words in the unchanged source; matching is not a "
    "chemical identity judgment."
)

MATERIAL_ROLES: dict[str, str] = {
    "ROUTE_SUBSTRATE": (
        "the route compound the operation starts from and transforms, however the author"
        " names or numbers it"
    ),
    "REAGENT": (
        "a substance added to bring the transformation about and consumed by it (a base, an"
        " oxidant, a reductant, an organometallic, an acid)"
    ),
    "CATALYST": (
        "a substance that brings the transformation about without being consumed by it (a"
        " metal complex, an organocatalyst, an acid or base used catalytically)"
    ),
    "ADDITIVE": (
        "a substance added to the reaction that is neither its reagent, its catalyst nor its"
        " solvent (a ligand, a salt, molecular sieves, an initiator, a stabilizer)"
    ),
    "SOLVENT": (
        "the medium the reaction itself runs in; a solvent used only to quench, extract,"
        " wash or purify is QUENCH_WORKUP"
    ),
    "QUENCH_WORKUP": (
        "anything used after the reaction to stop it or to isolate or purify its product: a"
        " quench, a wash, an extraction solvent, a drying agent, a chromatography eluent"
        " (its ratio kept in its name), silica or celite"
    ),
}

# What a material's amount is. The middle layer's `MaterialWords.amount` and the SCALE
# key on a MATERIAL (the fusion's form of the same phrase) both render this sentence.
MATERIAL_AMOUNT_MEANING = (
    "how much of it the author used: his whole phrase for the amount, copied as written"
    " with every quantity and qualifier in it (26 mg (0.25 mmol, 1 equiv), 1.1 mL (0.55"
    " mmol, 0.5 M in THF), 10 mol %, ten drops); the host reads the amount and the"
    " equivalents from it"
)

# Where the strength of a reagent's solution belongs. CONCENTRATION once sent it to
# STOCK_SOLUTION, "on that MATERIAL", while the amount phrase's own example carried it:
# one fact with two homes, published two ways (a condition parsed as a condition, or
# words kept in the amount). Both keys now render this sentence.
SOLUTION_STRENGTH = (
    "the strength of a reagent's solution (2.5 M in hexanes, 1 M in THF) is part of that"
    " material's amount phrase, not a condition of its own"
)


def material_role_guidance(*, open_roles: bool = False) -> str:
    """The role table as the sentence both write declarations show the model."""
    roles = "; ".join(f"{role}: {meaning}" for role, meaning in MATERIAL_ROLES.items())
    return (
        "Standard role meanings: " + roles
        + ". Use your own role wording when none fits; these examples are not exhaustive."
        if open_roles else "Choose one of these roles by its meaning: " + roles + "."
    )


MATERIAL_ROLE_GUIDANCE = material_role_guidance()


REAGENT_PREPARATION = (
    "A separately prepared reagent is one material of the main operation. Its preparation "
    "ingredients and preparation conditions belong to another experiment: retain that "
    "procedure with its source and explanation, without adding its ingredients or conditions "
    "to the main operation. In an experimental reading, ANOTHER_EXPERIMENT keeps the "
    "reading separate. Preserve the complete account through the declared narrative "
    "entry; if the active declaration offers EXPERIMENT and its operations through "
    "skeleton_repairs, use those for an independent experimental graph. A narrative "
    "alone does not create that graph."
)
