"""Passive investigation values shared by declarations, compilation and source evidence."""

from __future__ import annotations

from collections.abc import Container, Mapping
from typing import Annotated, Any, ClassVar, Literal, Protocol, get_args

from pydantic import Field, JsonValue, model_validator

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.literature.expressions import SourcePrimary, SourceRelation, ValueRelation
from tsdaf.contracts.literature.procedure_targets import (
    procedure_target_kind,
    procedure_target_schema,
)
from tsdaf.contracts.literature.schema import MachineProfile, fact_definition, fact_enum
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_delivery import WriteDeliveryControl
from tsdaf.lanes.literature.characterization_methods import CHARACTERIZATION_WRITING_MEANING
from tsdaf.lanes.literature.condition_categories import CONDITION_KEYS
from tsdaf.lanes.literature.item_identity import keyed
from tsdaf.lanes.literature.read_models import VisualReadBounds
from tsdaf.lanes.literature.source_parts import fact_source_ids


class MainExperimentAssessment(ContractModel):
    """Whether the MAIN experimental investigation of these subjects is sufficient."""

    subject_ids: tuple[str, ...] = Field(
        min_length=1, description="The exact facts or navigation segments assessed."
    )
    status: Literal["SUFFICIENT", "NEEDS_READ", "UNRESOLVED"] = Field(
        description=(
            "SUFFICIENT only when the MAIN experimental investigation actually is; "
            "NEEDS_READ with the remaining direction; UNRESOLVED with its limitation."
        )
    )
    source_ids: tuple[str, ...] = Field(
        min_length=1, description="Keys in the supplied source index supporting the assessment."
    )
    explanation: str = Field(
        min_length=1,
        description=(
            "The reasoning, and the assessment's only prose field: a rationale or a "
            "limitation goes here."
        ),
    )


class SourceRead(VisualReadBounds):
    selection_id: str = Field(
        min_length=1, description="Unique name for this exact source selection."
    )
    document_revision_id: str = Field(
        min_length=1, description="Exact revision from source_catalog."
    )
    source_id: str = Field(
        min_length=1, description="Exact text or original-image ID in that revision."
    )
    start_sentence: int = Field(default=0, ge=0, description="Inclusive zero-based sentence index.")
    end_sentence: int | None = Field(
        default=None,
        ge=1,
        description="Exclusive sentence index; omit for the whole remaining block.",
    )
    adjacent_blocks: int = Field(default=0, ge=0, le=2)


# What a handle is, for every collection alike. Which fields identify an item is its
# collection's to say (`item_identity`); each handle used to name them again, by hand,
# and the narrative's said "title or kind" where its collection keyed both.
HANDLE = (
    "Host-issued handle returned when this item was first saved; quote it only to change "
    "the fields that identify the item (its collection says which), and omit it for a new "
    "item."
)


def _handle_field() -> Any:
    """The stable handle every saved item carries; the host issues it, the model quotes it."""
    return Field(default=None, description=HANDLE)


class SavedItem(ContractModel):
    """An item the host keeps under a handle of its own, apart from what the item says.

    Every subclass declares ``item_id`` through ``_handle_field``, so every handle is
    declared alike; what identifies the item is its collection's (`item_identity`).
    """


class ContainerPatch(SavedItem):
    """Save one source-grounded increment to an offered existing container field."""

    item_id: str | None = _handle_field()
    target_id: str = Field(min_length=1, description="Exact existing public container ID.")
    field_key: str = Field(
        min_length=1,
        description=(
            "A field this target_id offers on the task sheet: one it lists as missing, for "
            "ADD, or one it already knows, for the other actions."
        ),
    )
    action: Literal["ADD", "REFINE", "SAME", "CONFLICT", "CORRECT_MAIN"] = Field(
        description="The action to apply to this existing field, using the exact enum value."
    )
    value: JsonValue = Field(description="Native JSON value matching the offered value_schema.")
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read that evidence this patch."
    )
    primary_selection_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Selections supporting the proposed primary value; "
            "other selections supply refinement context. "
            "Required when MAIN and SI are mixed. These selections must share one source phase."
        ),
    )
    explanation: str = Field(
        min_length=1,
        description="How the cited evidence corresponds to this field's experiment and scope.",
    )


class CompiledReadingPatch(ContainerPatch):
    """Ephemeral host result; never part of provider or historical write declarations."""

    outcome_group: str | None = None
    outcome_source: dict[str, Any] | None = None
    condition_sources: tuple[dict[str, Any], ...] = ()
    material_source_updates: tuple[dict[str, Any], ...] = ()
    material_relation_updates: tuple[dict[str, Any], ...] = ()
    competing_values: tuple[dict[str, Any], ...] = ()
    alternative_value: JsonValue = None
    support_selection_ids: tuple[str, ...] | None = None
    alternative_selection_ids: tuple[str, ...] = ()
    compound_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    review_markers: tuple[dict[str, Any], ...] = ()


class OutcomeAddition(SavedItem):
    """Save one new measurement in an offered reaction."""

    item_id: str | None = _handle_field()
    addition_id: str = Field(min_length=1, description="Your name for this new measurement.")
    reaction_block_id: str = Field(
        min_length=1,
        description=(
            "The existing reaction block, from the task sheet, that this measurement belongs to."
        ),
    )
    value: dict[str, JsonValue] = Field(
        description=(
            "Object with kind, reported_value, operation_ids, product_compound_ids, "
            "and optional basis. "
            "Use existing operation and compound IDs within this reaction."
        ),
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read that evidence this measurement."
    )
    explanation: str = Field(
        min_length=1,
        description="How the cited evidence corresponds to this reaction's experiment and scope.",
    )


def condition_key_guidance() -> str:
    """The Host's key table as the sentence both write declarations show the model.

    A condition's key is chosen from this table, and the model's own word stands where
    none fits, with a null key (ledger E61). What each key means is written here and
    nowhere else. The detail prompt and the fusion contract's CONDITION line list the
    keys' names, in this order, and point here for the meanings: that list is the
    checklist of what counts as a condition, a different job from this one. Removing it
    (66716c0) on the grounds that this table already carried the keys took the detail
    tasks' substantive conditions from 129 to 29 -- the model reads this when it has
    decided to write a condition, and the list is what made it decide.
    `test_the_prompts_name_the_same_condition_keys_as_the_table` holds both halves on both
    sides, and `test_the_declaration_names_exactly_the_tables_condition_keys_in_its_order`
    holds the order the projection's eight fixed fields depend on.
    """
    keys = "; ".join(f"{key}: {entry.meaning}" for key, entry in CONDITION_KEYS.items())
    return (
        "Prefer one of these keys, written as such: "
        + keys
        + ". If none fits, write your own word here and the host keeps the condition "
        "with a null key; text is always the author's own words."
    )


CONDITION_KEY_GUIDANCE = condition_key_guidance()


class ConditionAddition(SavedItem):
    """Save one condition of an existing public operation that no fixed field holds.

    The model writes the condition's key, or its own word where no key fits, and the
    author's words for it; the host keys the word with its one table, builds the evidence
    from the cited selections, and appends the note to the operation's other_conditions
    (ledger E57). A whole-operation condition whose key has a field of its own is routed
    to that field by the host.
    """

    item_id: str | None = _handle_field()
    condition_id: str = Field(min_length=1, description="Your name for this condition.")
    operation_id: str = Field(
        min_length=1,
        description=(
            "The operation the condition applies to: a public operation id from "
            "the task sheet, or the operation_key of an operation "
            "written in this same answer."
        ),
    )
    category_verbatim: str = Field(
        min_length=1, description="What kind of condition it is. " + CONDITION_KEY_GUIDANCE
    )
    text: str = Field(
        min_length=1,
        description="The author's own words for the condition: value and unit, or instruction.",
    )
    extent: Literal["WHOLE_OWNERS", "WITHIN_OWNER"] = Field(
        description=(
            "WHOLE_OWNERS when the condition applies to the whole operation; WITHIN_OWNER "
            "for a substage, explained in explanation."
        )
    )
    about: str | None = Field(
        default=None,
        # Not a solution's strength, which is part of that material's amount phrase
        # (`material_meanings.SOLUTION_STRENGTH`).
        description="The material the condition is about, when it is about one; omitted otherwise.",
    )
    stage_key: str | None = Field(
        default=None,
        description=(
            "The stretch of this procedure the condition was read in: the stage_key of a "
            "stage this operation declared under new_stages ('cold', 'after warming'), "
            "reused for every condition of that stretch. A name no declared stage "
            "carries is refused. Two stretches that each ran forty minutes are two "
            "durations, and the declared stage is what keeps them apart. Omit it when "
            "the condition covers the whole operation, or when it covers part of it and "
            "you cannot tell which."
        ),
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read where the condition appears."
    )
    explanation: str = Field(
        min_length=1, description="How the cited evidence corresponds to this operation."
    )


class CompiledCondition(ConditionAddition):
    """Middle-layer semantics carried to application, not a second model write."""

    text: str | None = Field(default=None, min_length=1)
    extent: Literal["WHOLE_OWNERS", "WITHIN_OWNER", "UNRESOLVED"]
    source_note: dict[str, Any] | None = None
    existing_stage_id: str | None = None
    retain_as_note: bool = False
    update_current: bool = False
    relation: SourceRelation = "ADDS"
    primary: SourcePrimary = "FINDING"
    value_relation: ValueRelation = "VALUE"
    review_reason: str | None = None
    value_selection_ids: tuple[str, ...] = ()


class OperationStageAddition(SavedItem):
    """Declare one stretch of an existing public operation's procedure: the author's own
    words where it begins, checked against the cited selection the way a
    characterization card's method statement is checked (E120). Declare every stretch
    of an operation before hanging a condition on it with stage_key; a condition naming
    a stage_key this operation never declared is refused.
    """

    item_id: str | None = _handle_field()
    operation_id: str = Field(
        min_length=1,
        description=(
            "The operation the stretch belongs to: a public operation id from the task "
            "sheet, or the operation_key of an operation written in this same answer."
        ),
    )
    stage_key: str = Field(
        min_length=1,
        description=(
            "Your own short name for this stretch, reused by every condition of it "
            "('cold', 'after warming')."
        ),
    )
    stage_verbatim: str = Field(
        min_length=1,
        description=(
            "The author's own words where this stretch begins: the sentence or clause a "
            "reader would look at to see the boundary. The host checks it is actually in "
            "one of the cited selections."
        ),
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Selection IDs returned by read where stage_verbatim appears.",
    )
    explanation: str = Field(
        min_length=1,
        description="How the cited words mark off this stretch from the rest of the operation.",
    )


class ProcedureElision(SavedItem):
    """Say that an existing operation's own procedure is the same as another operation's,
    or the same as a general-methods unit's, instead of restating it. Bind by identity
    only -- an id, never the author's words for it (ledger E117, principle one).
    """

    allow_unresolved_target: ClassVar[bool] = False

    item_id: str | None = _handle_field()
    operation_id: str = Field(
        min_length=1, description="The operation whose procedure this elides."
    )
    referenced_operation_id: str | None = Field(
        default=None, min_length=1,
        description=(
            "The public operation whose procedure this one follows: a public operation "
            "id from the task sheet, or the operation_key of an operation written in this "
            "same answer. Not this operation itself."
        ),
    )
    referenced_unit_block_id: str | None = Field(
        default=None, min_length=1,
        description="The first block id of the GENERAL_METHODS reading unit this elides to.",
    )
    external_citation: bool = Field(
        default=False,
        description=(
            "True when the elided procedure is not in this document at all, only cited "
            "(a literature reference). The host records this and derives nothing from it."
        ),
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Selection IDs returned by read where the author says the procedure is elided.",
    )
    explanation: str = Field(
        min_length=1, description="How the cited words say whose procedure this operation follows."
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> ProcedureElision:
        procedure_target_kind(self.model_dump(), allow_unresolved=self.allow_unresolved_target)
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core)
        schema.update(procedure_target_schema(allow_unresolved=cls.allow_unresolved_target))
        return schema


class CompoundAddition(SavedItem):
    """Save one compound the route never saw, prepared or named only in the experimental
    part.

    The host mints its public id and keeps its structure pending (ledger E76 ⑥). A
    block or an operation written in this same answer may name it by compound_key.
    """

    item_id: str | None = _handle_field()
    compound_key: str = Field(
        min_length=1,
        description=(
            "Your name for this compound; a block of this same answer names the compound by it."
        ),
    )
    author_labels: tuple[str, ...] = Field(
        default=(), description="Author-assigned labels only, e.g. 13 or S7; not names."
    )
    names: tuple[str, ...] = Field(
        default=(), description="Source-supported names or designations of the compound."
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read where the compound appears."
    )
    explanation: str = Field(
        min_length=1, description="Why this compound is not already in the public result."
    )

    @model_validator(mode="after")
    def _named(self) -> CompoundAddition:
        if not self.author_labels and not self.names:
            raise ValueError("a new compound names itself in author_labels or in names")
        return self


class BlockOperation(ContractModel):
    """One operation of a reaction block written in this same answer."""

    operation_key: str = Field(
        min_length=1,
        description=(
            "Your name for this operation; a condition of this same answer binds to the "
            "operation by it."
        ),
    )
    description: str = Field(
        min_length=1,
        description="The author's words for what the operation does, one or two sentences.",
    )


class ReactionBlockAddition(SavedItem):
    """Save one reaction step the route never saw, with its operations.

    Its endpoints name public compound ids or the compound_key of compounds written in
    this same answer. A block carries at least one operation, written inside it, so the
    step is whole when it is saved; the host mints the public ids and appends the block
    after the route's blocks.
    """

    item_id: str | None = _handle_field()
    block_key: str = Field(
        min_length=1,
        description=(
            "Your name for this block; an operation of this same answer binds to the block by it."
        ),
    )
    input_compound_ids: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "The compounds consumed: public compound ids, or the compound_key of compounds "
            "written in this same answer."
        ),
    )
    output_compound_ids: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "The compounds formed: public compound ids, or the compound_key of compounds "
            "written in this same answer."
        ),
    )
    summary: str = Field(
        min_length=1, description="One line saying what the step does, in the author's terms."
    )
    operations: tuple[BlockOperation, ...] = Field(
        min_length=1,
        description=(
            "The step's operations in order, at least one; each under an operation_key a "
            "condition of this same answer may bind to."
        ),
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read where the step appears."
    )
    explanation: str = Field(
        min_length=1, description="Why this step is not already a block of the public result."
    )


class OperationAddition(SavedItem):
    """Save one operation the route never saw in a public reaction block.

    It is appended after that block's operations; its fields stay pending until patched.
    An operation of a block written in this same answer goes inside that block instead.
    A condition of this same answer may bind to it by operation_key.
    """

    item_id: str | None = _handle_field()
    operation_key: str = Field(
        min_length=1,
        description=(
            "Your name for this operation; a condition of this same answer binds to the "
            "operation by it."
        ),
    )
    reaction_block_id: str = Field(
        min_length=1,
        description=(
            "The public reaction block the operation belongs to, from "
            "the task sheet; an operation of a block written in this "
            "same answer is written inside that block."
        ),
    )
    description: str = Field(
        min_length=1,
        description="The author's words for what the operation does, one or two sentences.",
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read where the operation appears."
    )
    explanation: str = Field(
        min_length=1, description="Why this operation is not already in its block."
    )


def same_measurement_field() -> Any:
    return Field(
        default=None,
        description=(
            "Existing characterization card shown on the task sheet that reports this same "
            "measurement on the same sample. Link only with source support; equal method or "
            "value alone is not identity. Omit for an independent or unpaired measurement. "
            "A link fills missing details and joins evidence; it does not "
            "replace conflicting filled values."
        ),
    )


class CompoundCharacterization(SavedItem):
    """Save one characterization card: the author's one-line statement of a measurement on
    existing public compounds, cited where it appears and never transcribed.

    The host keys the method from the words, parses the value, and seals the card with
    the cited passage as one package per compound (ledger E47); a peak list or a full
    spectral line is refused with the same sentence the fusion says of a
    CHARACTERIZATION note.
    """

    item_id: str | None = _handle_field()
    characterization_id: str = Field(min_length=1, description="Your name for this card.")
    compound_ids: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Exact existing public compound IDs the statement characterizes; one card may "
            "cover several compounds measured together."
        ),
    )
    method_verbatim: str = Field(
        min_length=1,
        description=CHARACTERIZATION_WRITING_MEANING,
    )
    same_measurement_as: str | None = same_measurement_field()
    sample_context: str | None = Field(
        default=None,
        description=(
            "The sample state the author gives when it qualifies the measurement, e.g. "
            "'3:1 mixture of diastereomers' or 'as the TFA salt'."
        ),
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read where the statement appears."
    )


# The public narrative vocabulary is the contract pack's, read here rather than retyped
# (kalmanol r7 lost a narrative to a hand-copied list that had drifted). BIOLOGICAL_FINDING
# is the one kind that lives only in the supplement channel's receipt (ledger E47).
NARRATIVE_KINDS: tuple[str, ...] = (*fact_enum("NarrativeKindValue"), "BIOLOGICAL_FINDING")
NarrativeKind = Literal[NARRATIVE_KINDS]  # type: ignore[valid-type]


NarrativeSubjectType = Literal[
    tuple(
        fact_definition("NarrativeSubject", profile=MachineProfile.V1_4)["properties"][
            "object_type"
        ]["enum"]
    )
]


class NarrativeSubject(ContractModel):
    """An explicitly named subject, without claiming whole-process applicability."""

    object_type: NarrativeSubjectType = Field(
        description="Type of the existing public object this narrative explicitly discusses."
    )
    object_id: str = Field(
        min_length=1, description="Supplied name of that existing public object."
    )


class SupplementalNarrative(SavedItem):
    """Save one connected source-grounded account without reducing its meaning to one value.

    Explain the full logic and qualifications once; the host copies selected author
    passages separately. Optional subjects mean about, not applies throughout. This
    account is public prose with evidence, not an adopted typed value or experiment relation.
    """

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(
        (
            'author_selection_ids',
            'explanation',
            'selection_ids',
            'title',
        )
    )
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(('item_id', 'kind', 'subjects'))

    subjects: tuple[NarrativeSubject, ...] = Field(
        default=(),
        description=(
            "In v1.4, optional existing public objects explicitly discussed by this narrative. "
            "This means about, not applies throughout. Preserve exceptions and experiment "
            "limits in the words and explanation. Empty means no object attribution supplied."
        ),
    )

    item_id: str | None = _handle_field()
    title: str = Field(min_length=1, description="The topic's title, in your words.")
    kind: NarrativeKind = Field(
        description=(
            "The narrative's kind. BIOLOGICAL_FINDING is recorded in this channel's own "
            "receipt and never enters the main result."
        )
    )
    explanation: str = Field(
        min_length=1, description="Your source-grounded synthesis of the topic."
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1, description="Selection IDs returned by read that the synthesis draws on."
    )
    author_selection_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Exact TEXT selections to retain as author quotations, copied byte for byte; "
            "empty for pure images."
        ),
    )


class OpenNarrative(SupplementalNarrative):
    """A connected account naming its subjects without repeating their storage type."""

    subjects: tuple[Annotated[str, Field(min_length=1)] | NarrativeSubject, ...] = Field(
        default=(),
        description=(
            "Existing public objects this account discusses: select their supplied names, "
            "one name per array member. The host binds each name to its object and type. "
            "The previous object_type/object_id form is also accepted. This means about, "
            "not applies throughout; retain all conditions and exceptions in explanation. "
            "Empty means no object attribution supplied."
        ),
    )


class UnifiedNarrative(OpenNarrative):
    """The candidate contract retains an unrecognized kind and its authored words."""

    kind: str = Field(min_length=1, description=(
        "The kind of account. Use a supplied standard name when it fits; otherwise "
        "retain the meaningful category you need. Its words and explanation are preserved."
    ))


def bind_narrative_subjects(
    item: OpenNarrative, machine: Mapping[str, Any] | None,
    registered_subjects: Container[str] = (),
) -> OpenNarrative:
    """Resolve the declared short form against the same rule that validates publication.

    Names has already resolved supplied aliases. Never infer type from an ID prefix,
    title, source text or scientific explanation; only existing public membership counts.
    The sealed domain and public contract retain their original typed references.
    """
    from tsdaf.contracts.literature.validation import validate_narrative_subjects

    bound = []
    for index, subject in enumerate(item.subjects):
        if isinstance(subject, NarrativeSubject):
            bound.append(subject)
            continue
        if machine is None:
            raise ValueError(f"subjects[{index}] requires an existing public object context")
        if subject not in registered_subjects:
            raise ValueError(
                f"subjects[{index}] {subject!r} is not a supplied public object name; "
                "select its offered read/write name and retain the complete explanation."
            )
        candidates = []
        for kind in get_args(NarrativeSubjectType):
            candidate = {"object_type": kind, "object_id": subject}
            try:
                validate_narrative_subjects([{"subjects": [candidate]}], machine)
            except ValueError:
                continue
            candidates.append(NarrativeSubject.model_validate(candidate))
        if len(candidates) != 1:
            reason = "ambiguous across public object types" if candidates else (
                "not an existing supported public subject"
            )
            raise ValueError(
                f"subjects[{index}] {subject!r} is {reason}; choose the supplied name "
                "of the intended public object. Keep its explanation and source evidence."
            )
        bound.append(candidates[0])
    return item.model_copy(update={"subjects": tuple(bound)})


class InvestigationLimitation(SavedItem):
    """Save one limitation."""

    item_id: str | None = _handle_field()
    # Compared literally with a step's reaction_block_id (`completion_guard`): a3 B004
    # sent scope as an object naming the block, and lost the limitation.
    scope: str = Field(
        min_length=1,
        description=(
            "The exact id this limitation covers, copied as the task sheet or progress "
            "gives it: a reaction_block_id, compound_id, document_revision_id or selection id."
        ),
    )
    reason: str = Field(
        min_length=1, description="Why this scope could not be completed from the sources."
    )
    selection_ids: tuple[str, ...] = Field(
        default=(), description="Selections evidencing the limitation, when any."
    )


class InvestigationCoverage(SavedItem):
    """Save the evidenced scope assessment of one document listed in documents."""

    item_id: str | None = _handle_field()
    # What a task assesses is the documents it is listed (`documents`), the one set the
    # host derives from what it can read; the detail lanes see no catalog since E189.
    document_revision_id: str = Field(
        min_length=1, description="A document revision as documents lists it."
    )
    status: Literal["EXAMINED", "NO_EXPERIMENTAL_DETAILS", "PARTIAL", "UNAVAILABLE"] = Field(
        description=(
            "Your assessment of the assigned document scope. Reading and citation counts "
            "are activity records and do not determine this scientific statement."
        )
    )
    selection_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Optional source selections from this document for your assessment."
        ),
    )
    explanation: str = Field(
        min_length=1,
        description=(
            "What was inspected for the assigned task and what remains unread or unresolved. "
            "Do not claim the rest of a document is irrelevant merely because this "
            "task's assigned scope is settled."
        ),
    )


class FinishInvestigation(ContractModel):
    """Stop with the saved findings and an evidenced completion disposition; no new findings."""

    status: Literal["COMPLETE", "PARTIAL", "NO_ADDITIONAL_INFORMATION"] = Field(
        description=(
            "The scientific state: COMPLETE, PARTIAL while obligations remain, or "
            "NO_ADDITIONAL_INFORMATION when inspected coverage found nothing to add."
        )
    )
    explanation: str = Field(
        min_length=1,
        description="Your reason for this scientific status.",
    )


class FinishInvestigationCommand(FinishInvestigation, WriteDeliveryControl):
    """Stop with the saved findings. status is the scientific disposition; disposition is
    delivery as declared by WriteDeliveryControl.
    """


class InvestigationAnswer(FinishInvestigation):
    coverage: tuple[InvestigationCoverage, ...] = ()
    patches: tuple[ContainerPatch, ...] = ()
    new_outcomes: tuple[OutcomeAddition, ...] = ()
    new_stages: tuple[OperationStageAddition, ...] = ()
    procedure_elisions: tuple[ProcedureElision, ...] = ()
    new_conditions: tuple[ConditionAddition, ...] = ()
    new_compounds: tuple[CompoundAddition, ...] = ()
    new_reaction_blocks: tuple[ReactionBlockAddition, ...] = ()
    new_operations: tuple[OperationAddition, ...] = ()
    characterizations: tuple[CompoundCharacterization, ...] = ()
    narratives: tuple[SupplementalNarrative, ...] = ()
    limitations: tuple[InvestigationLimitation, ...] = ()


class UnifiedInvestigationAnswer(InvestigationAnswer):
    """Candidate-profile receipt; legacy receipts retain their original vocabulary."""

    narratives: tuple[UnifiedNarrative, ...] = ()


class WithdrawFinding(ContractModel):
    """Withdraw one saved item, named by its handle or its current returned location.

    Every other item is retained. The handle survives edits; the location names one
    exact version and goes stale when the item is updated.
    """

    item_id: str | None = Field(
        default=None, description="The saved item's host-issued handle, as returned on save."
    )
    location: str | None = Field(
        default=None, description="The item's current returned location; stale after an update."
    )
    reason: str = Field(min_length=1, description="Why the item is withdrawn.")

    @model_validator(mode="after")
    def _names_one_item(self) -> WithdrawFinding:
        if not self.item_id and not self.location:
            raise ValueError("a withdrawal names the saved item by item_id or by location")
        return self


# What a task given a few reaction blocks may write. The six it leaves out are the
# paper's own work -- a compound, block or operation the route never saw, a measurement
# on a reaction, each document's coverage, the withdrawal of an earlier finding -- and
# they belong to the paper-scoped task of the same round, which is given every block.
# Sending them anyway would be 12 KB of declaration per turn asking for work this task
# cannot do, and a way for it to write outside its own step. A stage of one of its own
# operations and an elision of one of their procedures are its step's work (E157).
BLOCK_SCOPED_COLLECTIONS = (
    "patches",
    "new_stages",
    "new_conditions",
    "procedure_elisions",
    "characterizations",
    "limitations",
)

# What the paper's own task gives up when the round also has one task per step: the two
# collections its neighbours already cover. Measured over three sealed rounds of
# decumbenine_B -- every characterization card the paper's task wrote was the same
# compound and the same method a block task had already written (9 of 9 in e108, 9 of 9
# again in e109), and it wrote no patch at all (0, 0, 0). Two tasks writing one
# measurement is not merely wasted turns: it is how one melting point reached
# publication as two records under one public id (E111). Conditions stay with it for
# now, because in e108 six operations had a condition no block task wrote.
BLOCK_TASKS_COVER = ("patches", "characterizations")

# Which of the round's tasks this is. A block task is given a few steps; the paper's
# task is given every step and writes what no single step owns; and a round that is one
# task for the whole paper -- what a recorded fixture replays, and what the lane was
# before E106 -- is neither, and keeps everything.
TaskScope = Literal["BLOCK", "PAPER", "WHOLE"]


def supplemental_collections() -> dict[str, tuple[type[ContractModel], tuple[str, ...]]]:
    """The unchanged narrative channel's findings, keyed by `item_identity`."""
    return keyed(
        ("narratives", SupplementalNarrative),
        ("coverage", InvestigationCoverage),
        ("limitations", InvestigationLimitation),
    )


class HoldsSelections(Protocol):
    """What the write-time checks read of a receipt: its bound selections. A receipt is
    one; so is `SelectionsHeld`, which a running task passes instead of building a whole
    receipt -- and revalidating the whole answer inside it -- on every accepted item."""

    @property
    def selections(self) -> Mapping[str, Mapping[str, Any]]: ...


class ReceiptEvidence(Protocol):
    """Read-only evidence already held by a receipt, independent of its kind policy."""

    @property
    def answer(self) -> InvestigationAnswer | None: ...

    @property
    def raw_answer(self) -> dict[str, JsonValue] | None: ...

    @property
    def failure(self) -> str | None: ...

    @property
    def baseline_machine_ref(self) -> ArtifactRef | None: ...

    @property
    def execution_id(self) -> str: ...

    @property
    def paper_id(self) -> str: ...

    @property
    def observations(self) -> tuple[dict[str, object], ...]: ...

    @property
    def selections(self) -> dict[str, dict[str, object]]: ...

    def raw_model(self) -> ContractModel | None: ...


# The answer's collections, read off the model rather than retyped: a conclusion carries
# a status and an explanation, and everything the answer adds on top of those two is a
# collection of findings. A tenth collection joins this tuple by being declared.
ANSWER_COLLECTIONS: tuple[str, ...] = tuple(
    name
    for name in InvestigationAnswer.model_fields
    if name not in FinishInvestigation.model_fields
)


def cited_selection_ids(answer: ContractModel | None, selections: Container[str] = ()) -> set[str]:
    """Every selection this answer's findings cite, whichever collection carries them.

    One rule in one place: the finish guard refuses an answer that cites what was never
    delivered and refuses a COMPLETE that cites nothing at all, and the round's own
    reconciliation asks which Host delivery nothing cited. All three mean the same thing
    by "cited", so all three read it here, and a collection joins by being declared --
    the same way ``ANSWER_COLLECTIONS`` is read off the model rather than retyped.
    """
    if answer is None:
        return set()
    direct = {
        str(selection_id)
        for name in type(answer).model_fields
        for item in _answer_items(answer, name)
        for selection_id in getattr(item, "selection_ids", ())
    }
    # Fusion sources and reading selections have different registries. Count only
    # actual selections here; the Fusion executor validates inherited/new sources.
    repaired = {
        sid
        for item in getattr(answer, "skeleton_repairs", ())
        for change in item.repair.changes
        for sid in fact_source_ids(change)
        if sid in selections
    }
    return direct | repaired


def _answer_items(answer: ContractModel, name: str) -> tuple[ContractModel, ...]:
    """A model's declared item tuples, excluding its status, task and explanation."""
    value = getattr(answer, name)
    if not isinstance(value, tuple):
        return ()
    return tuple(item for item in value if isinstance(item, ContractModel))


def delivered_nothing(receipt: ReceiptEvidence) -> bool:
    """Whether this receipt put nothing at all into the round.

    A host fact about the receipt rather than a claim the model made about itself: the
    task failed, or it never concluded, or it concluded with every collection empty.

    The third leg is the one that matters, and the one the ledger recorded as done while
    the code never did it (E107 ④). `close_out` always constructs an answer, so
    `answer is None` is reachable only by a receipt that was never concluded at all: a
    task the turn limit ended after writing nothing came back looking exactly like a task
    that had finished its work. Two of the eight tasks of the e106 round were that task,
    and nothing anywhere counted them.
    """
    if receipt.failure:
        return True
    answer = receipt.raw_model()
    if answer is None:
        return True
    return not any(_answer_items(answer, name) for name in type(answer).model_fields)


class CompiledCharacterization(CompoundCharacterization):
    """Host-only review metadata for an accepted characterization."""

    explicit_identity: bool = False
    update_current: bool = False
    replace_sample_context: bool = False

    review_markers: tuple[dict[str, Any], ...] = ()
    model_interpretations: tuple[dict[str, Any], ...] = ()


class CompiledCompound(CompoundAddition):
    """Host-only review metadata for an accepted compound."""

    review_markers: tuple[dict[str, Any], ...] = ()


class CompiledElision(ProcedureElision):
    """Host-only review metadata for an accepted procedure reference."""

    allow_unresolved_target: ClassVar[bool] = True

    target_source_document_id: str | None = None
    target_source_revision_id: str | None = None
    source_parts: tuple[dict[str, Any], ...] = ()
    review_markers: tuple[dict[str, Any], ...] = ()
    extent: Literal["WHOLE_OWNERS", "WITHIN_OWNER"] = "WHOLE_OWNERS"
    stage_key: str | None = None
    existing_stage_id: str | None = None
    ruling_explanation: str | None = None
    relation: SourceRelation = "ADDS"
    primary: SourcePrimary | None = None


class CompiledCompoundExpression(ContractModel):
    """Host-only contribution to an existing compound, with no identity replacement."""

    compound_id: str
    source_statement: dict[str, Any]
    adopted_names: tuple[str, ...] = ()
    selection_ids: tuple[str, ...]


class CompiledMeasurement(ContractModel):
    """Host projection, indexed by its accepted item rather than its scientific value."""

    contribution_key: str
    outcome: dict[str, Any]
    experiment: dict[str, str] | None
    experiment_reading: dict[str, Any] | None = None
    identity_known: bool
    explanation: str
    refs: tuple[dict[str, str], ...]
    relation: Literal["SAME", "ADDS", "CONTRADICTS"]
    primary: Literal["SKELETON", "FINDING"] | None = None
    skeleton: dict[str, Any] | None = None
    selection_ids: tuple[str, ...]


class CompiledLocalIntermediate(ContractModel):
    container_id: str
    value: dict[str, Any]
    selection_ids: tuple[str, ...]


class CompiledNarrative(UnifiedNarrative):
    """A public account mechanically projected from an accepted finding."""

    source_finding: str
    # Host-resolved quotations use the existing public SourcePart carrier. They
    # are never exposed as another writer field or serialized into explanation.
    source_parts: tuple[dict[str, Any], ...] = ()


class CompiledReadingAnswer(InvestigationAnswer):
    """Explicit compiled contributions not exposed as another model write contract."""

    narratives: tuple[CompiledNarrative | SupplementalNarrative | UnifiedNarrative, ...] = ()
    patches: tuple[CompiledReadingPatch, ...] = ()
    new_conditions: tuple[CompiledCondition, ...] = ()
    new_compounds: tuple[CompiledCompound, ...] = ()
    characterizations: tuple[CompiledCharacterization, ...] = ()
    measurements: tuple[CompiledMeasurement, ...] = ()
    local_intermediates: tuple[CompiledLocalIntermediate, ...] = ()
    procedure_elisions: tuple[CompiledElision, ...] = ()
    compound_expressions: tuple[CompiledCompoundExpression, ...] = ()
    finding_contributions: dict[str, tuple[tuple[str, int], ...]] = {}
    # Only needed for aggregated items: a contributor's targets before aggregation.
    # These are joined with successful application locations, never success claims.
    finding_target_limits: dict[str, tuple[dict[str, str], ...]] = {}
    uncompiled_findings: dict[str, str] = {}
