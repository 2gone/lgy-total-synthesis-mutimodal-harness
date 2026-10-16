"""The detail lane's middle layer: the steps' paragraphs, what is read in them, the compile.

A block task used to settle three things in one write: the value, which field and action
it is, and which stretch of the operation it belongs to (ledger E174 and the 2026-09-23
plan, `docs/guides/DETAIL_LANE_MIDDLE_LAYER_PLAN_2026_09_23_ZH.md`). Here they come apart,
in the order the owner set on 2026-09-18: find the paragraphs, settle whose each thing
is, structure last.

- A block task finds its step's paragraphs and delivers them (`ParagraphSet`), taking
  nothing out of them: a value lifted out of its paragraph has already lost the stretch
  it belongs to (plan section 7). Its own account of the search -- how it finished, what
  it read, what it could not find -- compiles to its step's answer (`paragraph_answer`).
- The middle layer, once per paper, reads the route and the steps' paragraphs and writes
  what it read in the author's words, each with where it belongs: which stretch of an
  operation, which measurement it is the same as, which side a contradiction stands on
  (`ReadingAnswer`). Reading and ownership are one paper's, so the answer is compiled
  once, against the machine it will be applied to, with the middle layer's own receipt
  holding every paragraph it was given (`compile_reading`).

What it compiles to are the collections a block task used to write, and the application
and publication that follow are the ones that run today, unchanged: every structuring
rule stays where it is (`normalize_condition`, `FIXED_CONDITION_FIELDS`,
`place_whole_operation_value`, `project_written_value`), and nothing here restates one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache, reduce
from operator import or_
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, NamedTuple, cast, get_args

from pydantic import (
    Field,
    PrivateAttr,
    TypeAdapter,
    create_model,
    field_serializer,
    field_validator,
    model_validator,
)

from tsdaf.contracts._base import ContractModel
from tsdaf.lanes.literature.citation_policy import SOURCE_SELECTION_MEANING
from tsdaf.contracts.literature.expressions import (
    ConditionAdoption,
    ConditionSequenceMember,
    ConditionSequenceValue,
    SourcePrimary,
    SourceRelation,
    ValueRelation,
    material_source_reading,
    merge_interpretations,
    model_interpretation,
    present,
    resolved_condition_note,
    source_statement,
)
from tsdaf.contracts.literature.procedure_targets import (
    AmbiguousMethodUnit as AmbiguousMethodUnit,
    resolve_method_target,
    procedure_target_kind,
    procedure_target_schema,
)
from tsdaf.contracts.literature.schema import MachineProfile, machine_profile
from tsdaf.contracts.tool_write import DIAGNOSTIC_CHARS, SCALAR_TEXT, scalar_text
from tsdaf.lanes.literature.author_labels import AUTHOR_LABELS_MEANING
from tsdaf.lanes.literature.author_words import find_quote, quoted_source_candidates, text_contains
from tsdaf.lanes.literature.characterization_methods import (
    CHARACTERIZATION_WRITING_MEANING,
    characterization_fields,
    validate_characterization_statement,
)
from tsdaf.lanes.literature.condition_categories import FIXED_CONDITION_FIELDS, normalize_condition
from tsdaf.lanes.literature.condition_profiles import condition_source_value
from tsdaf.lanes.literature.container_enrichment import (
    _containers,
    _field_schema,
    _refs,
    content_hash,
    matching_card,
    register_compound_addition,
    same_compound_identity,
)
from tsdaf.lanes.literature.enrichment_values import (
    CompiledCharacterization,
    CompiledCompound,
    CompiledCompoundExpression,
    CompiledCondition,
    CompiledElision,
    CompiledReadingAnswer,
    CompiledReadingPatch,
    CompoundAddition,
    CompoundCharacterization,
    ConditionAddition,
    ContainerPatch,
    HoldsSelections,
    InvestigationAnswer,
    InvestigationCoverage,
    InvestigationLimitation,
    OperationStageAddition,
    ProcedureElision,
    ReceiptEvidence,
    SupplementalNarrative,
    UnifiedNarrative,
    _handle_field,
    same_measurement_field,
)
from tsdaf.lanes.literature.experiment_intents import MeasurementIntent, NewExperiment
from tsdaf.lanes.literature.gate_models import InlineFactChangeGroup, ReadingInlineFactChangeGroup
from tsdaf.lanes.literature.item_identity import keyed
from tsdaf.lanes.literature.material_bindings import bound_rows, unchanged_bindings
from tsdaf.lanes.literature.material_meanings import (
    MATERIAL_AMOUNT_MEANING,
    MATERIAL_NAME_MEANING,
    MATERIAL_ROLE_GUIDANCE,
    material_role_guidance,
    MATERIAL_ROLES,
    REAGENT_PREPARATION,
)
from tsdaf.lanes.literature.material_sources import MATERIAL_SOURCE_FIELDS
from tsdaf.lanes.literature.model_names import UnknownReference
from tsdaf.lanes.literature.outcome_meanings import (
    OUTCOME_KIND_GUIDANCE,
    OUTCOME_KINDS,
    OUTCOME_VALUE_PHRASE,
)
from tsdaf.lanes.literature.reported_scalars import (
    parse_reported_outcome, project_material_parts, sourced_material_quantities,
)
from tsdaf.lanes.literature.source_parts import (
    SourceSelection,
    selected_source_part,
    source_selection_entries,
)
from tsdaf.lanes.literature.written_values import (
    STRUCTURE_FORMATS,
    project_structure,
    project_written_value,
)

__all__ = (
    "CONDITION_ASPECT",
    "NAME_ASPECT",
    "PARAGRAPH_ROLES",
    "PASS_THROUGH_ASPECTS",
    "ConflictRuling",
    "Finding",
    "NameRuling",
    "ParagraphSet",
    "ReadingAnswer",
    "SameMeasurement",
    "Segment",
    "StepParagraph",
    "TwoTransformations",
    "compile_reading",
    "paragraph_collections",
    "read_reading_answer",
    "reading_collections",
    "skeleton_materials",
    "validate_paragraphs",
    "validate_reading",
    "reading_candidates",
    "paragraph_answer",
)

# A condition of an operation, named by the model's own category word; the host keys it
# (`normalize_condition`) and routes it to a field only through `FIXED_CONDITION_FIELDS`.
CONDITION_ASPECT = "condition"
# What a finding may be that is no field of a container: carried over as it was read.
PASS_THROUGH_ASPECTS = (
    "characterization",
    "new_compound",
    "new_measurement",
    "elision",
    "nothing_to_record",
)
# A systematic name read past the cut for a route compound: the middle layer rules on
# it (`NameRuling`); v1.4 preserves the source and explicitly adopted aliases.
NAME_ASPECT = "name"

# What a paragraph is to the step it is delivered for.
ParagraphRole = Literal[
    "PROCEDURE",
    "GENERAL_PROCEDURE",
    "REFERENCED_PROCEDURE",
    "CHARACTERIZATION",
    "COMPOUND_TITLE",
    "FIGURE_CAPTION",
    "NARRATIVE",
    "OTHER",
]
PARAGRAPH_ROLES: tuple[str, ...] = get_args(ParagraphRole)


# One paragraph a block task delivers for its step: a reading unit or a passage it read,
# by its selections, with what it is to the step and why it belongs there.
class StepParagraph(ContractModel):
    """Deliver a complete paragraph with its role and object ownership,
    without extracting isolated scientific values.
    """

    item_id: str | None = _handle_field()
    selection_ids: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Delivered selection ids for the complete paragraph or passage; preserve its "
            "procedural context."
        ),
    )
    role: ParagraphRole = Field(
        description=(
            "How this paragraph serves the step: procedure, characterization, compound "
            "title, caption, narrative, or another declared role."
        )
    )
    about_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Existing operation, reaction-block or compound ids this paragraph concerns; "
            "include those ids when ownership is established, including the assigned step. "
            "Empty means no specific object is established; explain that uncertainty or "
            "the genuinely paper-wide scope, rather than leaving an established "
            "attribution implicit."
        ),
    )
    explanation: str = Field(
        min_length=1,
        description="Why this whole passage belongs to those objects and has the selected role.",
    )


# What one block task delivers: its step's paragraphs, how it finished, its reading
# report, and what it looked for and could not find -- its own account of its search,
# not something read in a paragraph.
class ParagraphSet(ContractModel):
    "The paragraph-location task's raw delivery and scientific search account."

    task: str = Field(
        default="", description="Host task identifier for this paragraph-location answer."
    )
    status: Literal["COMPLETE", "PARTIAL", "NO_ADDITIONAL_INFORMATION"] = Field(
        description=(
            "Scientific search status for the assigned scope: COMPLETE when paragraph ownership "
            "and boundaries are settled and relevant continuations or referenced methods "
            "have been handled. Completeness concerns relevant scientific content and its "
            "attribution, not collecting every form of material. A separate title, caption "
            "or repeated account is needed only when it resolves a concrete gap in that "
            "content, attribution or passage boundary. In a step task, other steps' "
            "unread material does not "
            "prevent COMPLETE; document coverage may honestly remain PARTIAL. "
            "Unanswered candidates or unresolved relevant passages require PARTIAL. "
            "NO_ADDITIONAL_INFORMATION means the assigned search is complete with no "
            "additional paragraphs. An empty candidate list alone does not establish "
            "ownership or completion. Delivery disposition is separate."
        )
    )
    explanation: str = Field(
        min_length=1,
        description="What the paragraph search covered and what remains missing or unresolved.",
    )
    paragraphs: tuple[StepParagraph, ...] = Field(
        default=(),
        description=(
            "Complete located passages with their delivered selection ids, roles and "
            "object ownership. This delivery records source attribution, not a claim of new "
            "scientific findings."
        ),
    )
    coverage: tuple[InvestigationCoverage, ...] = Field(
        default=(),
        description=(
            "Which supplied source revisions were inspected and the result of that inspection."
        ),
    )
    limitations: tuple[InvestigationLimitation, ...] = Field(
        default=(),
        description=(
            "Unresolved source or coverage limitations, with the affected scope and reason."
        ),
    )


# Where a condition belongs within its operation: the whole of it, one declared stretch,
# or a stretch that cannot be told apart.
KnownScope = Literal["WHOLE", "SEGMENT", "PART", "ANOTHER_EXPERIMENT"]
Scope = Literal[KnownScope, "UNRESOLVED"]
# The scopes that are a stretch of an operation, declared or not: only a finding about
# an operation can lie in one (`About.scopes`).
STRETCHES = frozenset({"SEGMENT", "PART"})


class MaterialWords(ContractModel):
    "One material in author order: its name, its role and the author's one phrase for how much."

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(('amount', 'role', 'verbatim_name'))
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(
        (
            'compound_id',
            'identity_relation',
        )
    )

    role: Literal[tuple(MATERIAL_ROLES)] = Field(  # type: ignore[valid-type]
        description="The role this material serves in its operation. " + MATERIAL_ROLE_GUIDANCE
    )
    verbatim_name: str = Field(min_length=1, description=MATERIAL_NAME_MEANING)
    # One phrase, read once by the host. A second slot for equivalents let the model cut
    # the phrase (b4-a4 split 42 of 128 materials, and 38 of the moles it cut off were
    # published as equivalents); answers sealed with it are read by `read_reading_answer`.
    amount: str | Annotated[tuple[SourceSelection, ...], Field(min_length=1)] | None = Field(
        default=None,
        description=(
            "For this material, "
            + MATERIAL_AMOUNT_MEANING
            + "; or select separately located amount fragments. All selected fragments belong "
            "to this material amount, not to other materials. Preserve separate reported "
            "quantities with these selections instead of joining or dropping them to make "
            "one continuous quote. Null when the author gives none."
        ),
    )
    identity_relation: Literal["SAME", "DIFFERENT"] = Field(
        default="SAME",
        description=(
            "Chemical identity only, independent of the finding relation. "
            "SAME is the same chemical as the named skeleton position, even when quantity "
            "differs. DIFFERENT means the old chemical identity is wrong; use CONTRADICTS. "
            "Then compound_id identifies the replacement if established; "
            "otherwise it stays unbound."
        ),
    )
    compound_id: str | None = Field(
        default=None,
        description=(
            "Existing compound id for this material when established; null rather than "
            "inventing an id."
        ),
    )


class QuotedMaterialWords(MaterialWords):
    """The unchanged continuous amount input for profiles through v1.4."""

    amount: str | None = Field(
        default=None,
        description=(
            "For this material, " + MATERIAL_AMOUNT_MEANING + "; null when the author gives none."
        ),
    )


class YieldValueWords(ContractModel):
    "One reported value of an experiment (a yield, a recovery, a ratio), its kind and product."

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(('kind', 'product_label', 'text'))
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(
        (
            'product_ids',
            'same_measurement_as',
        )
    )

    # One phrase, read by the host as the route's OUTCOME text is. A statement with the
    # model's cuts of it (a percentage phrase, an amount phrase) had each cut refused
    # when it was not verbatim; answers sealed so are read by `read_reading_answer`.
    text: str = Field(min_length=1, description=OUTCOME_VALUE_PHRASE)
    kind: Literal[tuple(OUTCOME_KINDS)] = Field(  # type: ignore[valid-type]
        description="What kind of value the author reports. " + OUTCOME_KIND_GUIDANCE
    )
    product_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Existing compound ids explicitly identifying this measurement's products. "
            "Supply them once here; compound-valued about_ids also supplies products "
            "when this field is omitted (legacy input). Other related objects never "
            "infer products or measurement identity."
        ),
    )
    product_label: str | None = Field(
        default=None,
        description=(
            "The author's product label copied from the paragraph when needed; null if absent."
        ),
    )


class YieldWords(YieldValueWords):
    "One reported value of an experiment (a yield, a recovery, a ratio), its kind and product."

    same_measurement_as: str | None = Field(
        default=None,
        description=(
            "Existing measurement name when this is the same product and experimental run. "
            "Choose it here in this finding; explanation states why, without a separate pairing. "
            "No target means identity is undetermined, not inferred from equal values. "
            "On revising this finding, omission retains its inline target; null clears it."
        ),
    )


class UnifiedYieldWords(YieldValueWords):
    """One complete value expression with separate identity and experiment choices."""

    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(
        ('product_ids', 'measurement', 'experiment')
    )
    kind: str = Field(min_length=1, description="The reported kind; use a standard short name when it fits, otherwise retain the author's category.")
    measurement: MeasurementIntent | None = Field(
        default=None,
        description="Choose same_as a displayed measurement, new=true for a distinct measurement, or unknown=true. Omit on an edit to retain the prior choice; first omission is unknown.",
    )
    experiment: str | None = Field(
        default=None, min_length=1,
        description="Displayed experiment or reaction name; null explicitly leaves ownership unknown. Omission inherits an existing same measurement's experiment; for WHOLE it may also inherit a named operation's container. ANOTHER_EXPERIMENT never infers ownership or actual operations from about_ids (comparison context). Products do not determine ownership.",
    )

    @property
    def same_measurement_as(self) -> str | None:
        """Host-only compatibility accessor; never a second writable representation."""
        return self.measurement.same_as if self.measurement is not None else None


def unified_measurement_entry() -> str:
    """Short navigation generated from the current measurement writer fields."""
    return " ".join(
        f"words.{name}: {UnifiedYieldWords.model_fields[name].description}"
        for name in ("measurement", "experiment")
    )


class CharacterizationWords(ContractModel):
    """One characterization statement with its checkable result; the host keys the method
    from these words, so the item has no field for it."""

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(('sample_context', 'statement'))
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(('same_measurement_as',))

    # The host keys it with `normalize_method`. "One technique" was the unit here and in
    # the prompt, and a3 and a4 wrote a technique field ten times (CHROMATOGRAPHY, TLC)
    # that no declaration had.
    statement: str = Field(
        min_length=1,
        description=CHARACTERIZATION_WRITING_MEANING + " Copy source wording as written.",
    )
    same_measurement_as: str | None = same_measurement_field()
    sample_context: str | None = Field(
        default=None,
        description=(
            "Author wording identifying this measurement's sample or conditions when "
            "needed; null if absent."
        ),
    )


class CompoundWords(ContractModel):
    "Source labels and names for a compound newly reported in the delivered paragraphs."

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(('author_labels', 'names'))
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(())

    author_labels: tuple[str, ...] = Field(default=(), description=AUTHOR_LABELS_MEANING)
    names: tuple[str, ...] = Field(
        default=(), description="Names copied from the source for this newly reported compound."
    )


def compound_addition(finding: Finding) -> CompiledCompound:
    """The declared new object, shared by task projection and final compilation."""
    return CompiledCompound(
        compound_key=finding.finding_id,
        author_labels=finding.words.author_labels,
        names=finding.words.names,
        selection_ids=finding.selection_ids,
        explanation=finding.explanation,
    )


def reading_compound_view(
    answer: ReadingAnswer, receipt: HoldsSelections, machine: Mapping[str, Any]
) -> dict[str, Any]:
    """Project this answer's explicit objects without changing the route or baseline.

    Derived on demand from the accepted answer; checkpoint recovery needs no new
    registry. The publisher uses the exact same compound identity/merge operation.
    """
    domain = {
        **machine["domain_payload"],
        "compounds": list(machine["domain_payload"]["compounds"]),
    }
    for finding in answer.findings:
        if finding.aspect == "new_compound" and (has_public_scope(finding, machine)):
            added = compound_addition(finding)
            register_compound_addition(
                domain,
                str(machine["paper_id"]),
                added,
                _refs(receipt, added.selection_ids),
                profile=machine_profile(machine),
            )
    return {**machine, "domain_payload": domain}


def validate_compound_revision(
    before: ReadingAnswer,
    after: ReadingAnswer,
    receipt: HoldsSelections,
    machine: Mapping[str, Any],
) -> None:
    """An edit cannot silently change/remove the identity used by saved findings."""
    old = reading_compound_view(before, receipt, machine)
    new = reading_compound_view(after, receipt, machine)
    old_ids = {c["compound_id"]: c for c in old["domain_payload"]["compounds"]}
    new_ids = {c["compound_id"]: c for c in new["domain_payload"]["compounds"]}
    changed = {
        cid
        for cid, labels in old_ids.items()
        if cid not in new_ids or not same_compound_identity(labels, new_ids[cid])
    }
    for finding in after.findings:
        if not has_public_scope(finding, machine):
            continue
        refs = set(finding.about_ids)
        if isinstance(finding.words, MaterialWords) and finding.words.compound_id:
            refs.add(finding.words.compound_id)
        if isinstance(finding.words, (YieldWords, ProductWords)):
            refs.update(finding.words.product_ids)
        if refs & changed:
            raise ValueError(
                "compound is used by saved findings; update or withdraw those references "
                "before changing its identity or withdrawing it"
            )


class ElisionWords(ContractModel):
    """The author's explicit reference to an omitted procedure,
    kept distinct from reconstructed content.
    """

    allow_method_location: ClassVar[bool] = False

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(('external_citation',))
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(
        (
            'referenced_operation_id',
            'referenced_unit_block_id',
        )
    )

    referenced_operation_id: str | None = Field(
        default=None, min_length=1,
        description=(
            "Existing operation id whose procedure the author explicitly references, if identified."
        ),
    )
    referenced_unit_block_id: str | None = Field(
        default=None, min_length=1,
        description=(
            "Supplied reading-unit block id of the explicitly referenced procedure, if identified."
        ),
    )
    external_citation: bool = Field(
        default=False,
        description=(
            "True when the omitted procedure is referred to an external publication "
            "rather than supplied source content."
        ),
    )

    @model_validator(mode="after")
    def one_target(self) -> ElisionWords:
        procedure_target_kind(
            self.model_dump(), allow_unresolved=True, allow_location=self.allow_method_location
        )
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core)
        schema.update(procedure_target_schema(
            allow_unresolved=True, allow_location=cls.allow_method_location
        ))
        return schema


class IntermediateWords(ContractModel):
    "An intermediate linking existing operations; the host supplies its temporary identity."

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(
        (
            'label',
            'structure',
            'structure_format',
        )
    )
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(('consumed_by', 'produced_by'))

    # Only the sealed-history reader sets this; neither write input nor model output.
    _legacy_identity: str | None = PrivateAttr(default=None)

    label: str | None = Field(
        default=None,
        description=(
            "The author's label for this intermediate, copied from the paragraph; null if absent."
        ),
    )
    produced_by: str = Field(
        min_length=1, description="Existing operation id that produces this intermediate."
    )
    consumed_by: str = Field(
        min_length=1, description="Existing operation id that consumes this intermediate."
    )
    structure: str | None = Field(
        default=None,
        description="Author wording for this intermediate's structure when stated; null if absent.",
    )

    structure_format: Literal[STRUCTURE_FORMATS] | None = Field(  # type: ignore[valid-type]
        default=None,
        description=(
            "Format only when the author supplied an explicit structure representation in "
            "structure. Leave null for ordinary names or descriptions; never generate a "
            "representation from a name. The host keeps the quoted wording and evidence."
        ),
    )

    @model_validator(mode="after")
    def _format_has_representation(self) -> IntermediateWords:
        if self.structure_format is not None and not (self.structure and self.structure.strip()):
            raise ValueError(
                "structure_format requires a nonempty author representation in structure"
            )
        return self

    model_config = {
        "json_schema_extra": {
            "if": {
                "required": ["structure_format"],
                "properties": {"structure_format": {"type": "string"}},
            },
            "then": {
                "required": ["structure"],
                "properties": {"structure": {"type": "string", "pattern": r"\S"}},
            },
        }
    }


class ProductWords(ContractModel):
    "The existing compounds identified as products of a measurement."

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(())
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(('product_ids',))

    product_ids: tuple[str, ...] = Field(
        description="Existing compound ids identified as products of this measurement."
    )


# The kinds of object the route holds, each with ids of its own.
RouteObject = Literal["reaction block", "operation", "measurement", "compound"]
ROUTE_OBJECTS: tuple[RouteObject, ...] = get_args(RouteObject)


class About(NamedTuple):
    """What a reading kind's about_ids name, and how many.

    Said once, here. The declaration states it per aspect, the write-time check refuses
    in the same words (`validate_reading`), and the compile unpacks exactly one id where
    the kind takes exactly one. It was prose in the declaration, three hand-written
    checks and the compile's unpacks, and they disagreed: a characterization naming
    nothing passed the check and failed in the compile.
    """

    names: tuple[RouteObject, ...]
    least: Literal[0, 1] = 0
    most: Literal[1] | None = None

    def text(self) -> str:
        """The rule in the words the declaration states and a refusal repeats."""
        count = {
            (1, 1): "exactly one",
            (1, None): "at least one",
            (0, 1): "at most one",
            (0, None): "any number of",
        }[(self.least, self.most)]
        *others, last = self.names
        nouns = f"{', '.join(others)} or {last}" if others else last
        return f"{count} existing {nouns} id" + ("s" if count == "any number of" else "")


# One aspect of a finding: the shape of its words, which of them quote the author, what
# it compiles to, and what its about_ids name.
YIELD_PUBLICATION_PREREQUISITE = (
    "An existing single measurement requires words.same_measurement_as in the yield finding. "
    "Legacy same_measurements links are also read. SAME alone does not establish identity. "
    "Other experiments and combined spans stay in the saved reading "
    "until they have a supported public home."
)


class ConditionWords(ContractModel):
    """Separate source fragments of one model-owned condition; array order is not time."""

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(('source_parts',))
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(())

    source_parts: tuple[SourceSelection, ...] = Field(
        min_length=1,
        description="Separate author fragments belonging to this condition. Their order "
        "does not declare a procedure sequence; use sequence for that relation.",
    )


class ReadingConditionSequenceMember(ContractModel):
    """Private input references, resolved before creating the public sequence."""

    text: str | None = Field(
        default=None,
        min_length=1,
        description=ConditionSequenceMember.model_fields["text"].description,
    )
    source_parts: tuple[SourceSelection, ...] | None = Field(
        default=None,
        min_length=1,
        description="Delivered fragments belonging to this ordered member; an alternative to text.",
    )
    values: tuple[ConditionSequenceValue, ...] = Field(
        default=(), description=ConditionSequenceMember.model_fields["values"].description
    )

    @model_validator(mode="after")
    def one_expression(self) -> ReadingConditionSequenceMember:
        if (self.text is None) == (self.source_parts is None):
            raise ValueError("sequence member needs exactly one of text or source_parts")
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core)
        schema["oneOf"] = [
            {
                "required": ["text"],
                "properties": {"text": {"type": "string"}, "source_parts": {"type": "null"}},
            },
            {
                "required": ["source_parts"],
                "properties": {"source_parts": {"type": "array"}, "text": {"type": "null"}},
            },
        ]
        return schema


INDEPENDENT_EXPERIMENT_ENTRY = (
    "When the account identifies a distinct experiment and its internal operations, "
    "use skeleton_repairs in the supplied independent experiments scope to express "
    "an EXPERIMENT, its OPERATION facts and their supported relationships. "
    "An unknown link to the main route does not prevent that internal structure. "
    "ANOTHER_EXPERIMENT on a material or condition preserves an unassigned account; "
    "it does not create that experimental structure. Do not invent an owner to avoid uncertainty."
)


class ReadingKind(NamedTuple):
    words: Any
    quoted: tuple[str, ...]
    compiles_to: str
    about: About
    scopes: tuple[str, ...] = ("WHOLE", "ANOTHER_EXPERIMENT")
    public_scopes: tuple[str, ...] = ("WHOLE",)

    @property
    def accepts_stage(self) -> bool:
        return "SEGMENT" in self.scopes

    def publication_note(self, *, preservation: bool = False, unified_expression: bool = False) -> str:
        if self.compiles_to in {"name", "assertion"}:
            if unified_expression:
                return (
                    "Preserve the full source expression for each explicitly named compound. "
                    "NameRuling separately decides alias adoption for its selected compound: "
                    "SUPPLEMENTS adds an alias; CONTRADICTS preserves the comparison without "
                    "changing identity. No ruling is needed to preserve the source expression."
                )
            if preservation:
                return (
                    "With an explicit compound owner, preserve the full source expression. "
                    "A name needs NameRuling: SUPPLEMENTS adds an alias; CONTRADICTS "
                    "keeps the source and explanation without changing compound identity. "
                    "Unowned content remains in the saved reading."
                )
            return (
                "In v1.4 explicit compound expressions can be published; "
                "older profiles retain them only in the saved reading."
            )
        raw = [scope for scope in self.scopes if scope not in self.public_scopes]
        if unified_expression and self.compiles_to in {"condition", "materials"}:
            return (
                "Public fields support scopes: " + ", ".join(self.public_scopes) + ". "
                "ANOTHER_EXPERIMENT preserves the full public account and its source parts, "
                "without applying it to the comparison operation. " + INDEPENDENT_EXPERIMENT_ENTRY
            )
        return (
            "Public compilation supports scopes: "
            + (", ".join(self.public_scopes) or "none")
            + ". Other accepted scopes ("
            + ", ".join(raw)
            + ") remain in the saved reading only; they are not applied to this experiment."
            + (" " + (unified_measurement_entry() if unified_expression else YIELD_PUBLICATION_PREREQUISITE) if self.compiles_to == "yield" else "")
        )


ONE_OPERATION = About(("operation",), 1, 1)
ONE_MEASUREMENT = About(("measurement",), 1, 1)

# The sole closed kind/word-shape/quotation/compile/object table. Types and declarations
# are derived from this table, never inferred from a public field written by a model.
READING_KINDS = {
    "condition": ReadingKind(
        str | ConditionWords,
        (),
        "condition",
        ONE_OPERATION,
        get_args(KnownScope),
        ("WHOLE", "SEGMENT", "PART"),
    ),
    "material": ReadingKind(
        MaterialWords,
        ("verbatim_name", "amount"),
        "materials",
        ONE_OPERATION,
        get_args(KnownScope),
        ("WHOLE", "SEGMENT", "PART"),
    ),
    "yield": ReadingKind(YieldWords, ("text", "product_label"), "yield", About(ROUTE_OBJECTS)),
    "characterization": ReadingKind(
        CharacterizationWords,
        ("statement", "sample_context"),
        "characterization",
        # A card belongs to a compound (`CompoundCharacterization.compound_ids`).
        About(("compound",), 1),
    ),
    "name": ReadingKind(str, (), "name", About(("compound",)), public_scopes=()),
    "assertion": ReadingKind(str, (), "assertion", About(("compound",)), public_scopes=()),
    "new_compound": ReadingKind(
        CompoundWords, ("author_labels", "names"), "new_compound", About(ROUTE_OBJECTS)
    ),
    "elision": ReadingKind(
        ElisionWords,
        (),
        "elision",
        ONE_OPERATION,
        get_args(KnownScope),
        ("WHOLE", "SEGMENT", "PART"),
    ),
    # An activity report, not a scientific limitation or a public fact.
    "nothing_to_record": ReadingKind(str, (), "nothing_to_record", About(ROUTE_OBJECTS, 1, 1)),
    "intermediate": ReadingKind(
        IntermediateWords,
        ("label", "structure"),
        "operation_intermediate",
        ONE_OPERATION,
        get_args(KnownScope),
    ),
    "measurement_products": ReadingKind(ProductWords, (), "product_compound_ids", ONE_MEASUREMENT),
}


def has_public_scope(finding: Finding, machine: Mapping[str, Any]) -> bool:
    if machine_profile(machine).preservation and finding.aspect in {"name", "assertion"}:
        return bool(finding.about_ids)
    return not machine_profile(machine).expression or (
        finding.scope or "WHOLE"
    ) in finding_write_model(
        expression=machine_profile(machine).expression,
        open_expression=machine_profile(machine).open_expression,
        unified_expression=machine_profile(machine).unified_expression,
    ).scope_options(finding.aspect, public=True)


def _declared_about(kind: ReadingKind) -> dict[str, Any]:
    """An aspect's about_ids and scope as its declaration states them. A scope every
    aspect may take is left to the shared field."""
    about = kind.about
    text = about.text()
    ids: dict[str, Any] = {"description": text[:1].upper() + text[1:] + "."}
    if about.least:
        ids["minItems"] = about.least
    if about.most is not None:
        ids["maxItems"] = about.most
    declared: dict[str, Any] = {"about_ids": ids}
    if kind.scopes != get_args(KnownScope):
        declared["scope"] = {"enum": list(kind.scopes)}
    return declared


if TYPE_CHECKING:
    ReadingWords = Any
else:
    ReadingWords = reduce(or_, dict.fromkeys(kind.words for kind in READING_KINDS.values()))


class Finding(ContractModel):
    """One author's value, its object and its place; never a public field or action."""

    allow_unresolved_procedure: ClassVar[bool] = True

    local_edit_fields: ClassVar[frozenset[str]] = frozenset(
        (
            'explanation',
            'review_reason',
            'selection_ids',
            'value_selection_ids',
            'words',
        )
    )
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset(
        (
            'about_ids',
            'aspect',
            'block_id',
            'category_word',
            'clear_summary',
            'condition_about',
            'detail_of',
            'existing_stage_id',
            'finding_id',
            'item_id',
            'relation',
            'reporting_level',
            'scope',
            'segment_key',
            'sequence',
            'shared_unit_text',
            'skeleton_material_id',
            'value_relation',
        )
    )

    open_sources: ClassVar[bool] = True
    unified_measurements: ClassVar[bool] = False
    new_condition_scope: ClassVar[Scope] = "WHOLE"

    @classmethod
    def scope_options(cls, aspect: str, *, public: bool = False) -> tuple[str, ...]:
        kind = READING_KINDS[aspect]
        options = kind.public_scopes if public else kind.scopes
        if cls.unified_measurements and aspect == "yield":
            return kind.scopes
        return (*options, "UNRESOLVED") if cls.open_sources and aspect == "condition" else options

    @classmethod
    def publication_note(cls, aspect: str, *, preservation: bool = False) -> str:
        return (
            READING_KINDS[aspect]
            ._replace(
                scopes=cls.scope_options(aspect),
                public_scopes=cls.scope_options(aspect, public=True),
            )
            .publication_note(preservation=preservation, unified_expression=cls.unified_measurements)
        )

    item_id: str | None = _handle_field()
    finding_id: str = Field(
        min_length=1, description="Your name for this value; rulings name it in finding."
    )
    about_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Existing ids of the objects this value describes; each aspect's declaration "
            "says which."
        ),
    )
    aspect: Literal[tuple(READING_KINDS)] = Field(  # type: ignore[valid-type]
        description=(
            "Select the declared kind of this one finding; its words must use that "
            "kind's declared shape. Do not write public field names or patch actions."
        )
    )
    words: ReadingWords | None = Field(
        default=None,
        description=(
            "One author value, copied from cited evidence, using the text or structured "
            "word fields declared for the selected aspect."
        ),
    )
    category_word: str | None = Field(
        default=None, description=ConditionAddition.model_fields["category_verbatim"].description
    )
    condition_about: str | None = Field(
        default=None,
        description=ConditionAddition.model_fields["about"].description,
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Source selections supporting this finding and its ownership. " + SOURCE_SELECTION_MEANING,
    )
    value_selection_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Optional narrower source selections containing the exact value words; "
            "otherwise selection_ids provide the quotation evidence. " + SOURCE_SELECTION_MEANING
        ),
    )
    relation: SourceRelation = Field(
        default="ADDS",
        description=(
            "Relation of scientific VALUES to the skeleton, not chemical identity. "
            "For a named existing position, SAME retains primary values without applying "
            "supplied additions to primary fields; "
            "complete source wording remains in the saved reading. "
            "ADDS (default) applies additional information, including a newly supplied "
            "amount for an existing material. CONTRADICTS records a conflicting reading "
            "for an explicit ruling. Material identity is expressed separately in "
            "words.identity_relation; the same chemical can have relation=ADDS."
        ),
    )
    value_relation: ValueRelation = Field(
        default="VALUE",
        description=(
            "For a condition, VALUE is one value or interval. ORDERED_POINTS means the "
            "quoted endpoints are successive values in the author's order, including "
            "a shared unit. Decide from the whole procedure; the host will not infer "
            "this relation from words such as to or then."
        ),
    )
    sequence: tuple[ReadingConditionSequenceMember, ...] = Field(
        default=(),
        description=(
            "Optional explicitly ordered condition phrases, any number of members. "
            "Keep complete author wording in words; put paired values in one member. "
            "Do not infer sequence from numeric magnitude."
        ),
    )
    shared_unit_text: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Optional author-stated unit shared by sequence members; "
            "must occur in the parent words."
        ),
    )
    reporting_level: Literal["TOTAL", "USE"] | None = Field(
        default=None,
        description="Material only: author-reported total or one use; omit if unspecified.",
    )
    detail_of: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Material only: this use is a detail of the named material finding or existing "
            "material. The summary may be written later in the same batch."
        ),
    )
    clear_summary: bool = Field(
        default=False,
        description=(
            "Material only: explicitly detach the selected existing use from its summary; "
            "retain the material and amount. Mutually exclusive with detail_of."
        ),
    )
    review_reason: str | None = Field(
        default=None,
        min_length=1,
        description="Optional scientific uncertainty to review; it does not block supported facts.",
    )
    scope: Scope | None = Field(
        default=None,
        description=(
            "For materials, omitted scope retains the existing scope; "
            "explicit WHOLE clears a stage. "
            "WHOLE applies to the entire operation; SEGMENT names a declared stretch; "
            "PART is a stretch that cannot be distinguished; ANOTHER_EXPERIMENT "
            "describes another run or scale and has no segment_key. See the aspect's "
            "publication entry for supported public scopes and explicit experiment ownership. "
            + REAGENT_PREPARATION
        ),
    )
    segment_key: str | None = Field(
        default=None,
        description=(
            "Declared segment_key for scope SEGMENT, belonging to an operation in "
            "about_ids; otherwise null."
        ),
    )
    existing_stage_id: str | None = Field(
        default=None,
        description=(
            "For a supported SEGMENT finding, select an existing stage shown on this "
            "operation instead of declaring it again. Mutually exclusive with segment_key."
        ),
    )
    skeleton_material_id: str | None = Field(
        default=None,
        description=(
            "Exact host-issued material position id this material corresponds to, when "
            "any. The host retains skeleton materials not named by a finding."
        ),
    )
    block_id: str | None = Field(
        default=None,
        description=(
            "Existing reaction-block id identifying the route step for this yield when "
            "needed; null if not established."
        ),
    )
    explanation: str = Field(
        min_length=1,
        description=(
            "Explain the value's object, procedural scope and relation using its cited "
            "paragraph context."
        ),
    )

    @model_validator(mode="after")
    def default_nonmaterial_scope(self) -> Finding:
        if self.aspect != "material" and self.scope is None:
            if "scope" in self.model_fields_set:
                raise ValueError("non-material scope must be an offered scope, not null")
            object.__setattr__(
                self, "scope", self.new_condition_scope if self.aspect == "condition" else "WHOLE"
            )
        if self.scope == "UNRESOLVED" and self.scope not in self.scope_options(self.aspect):
            raise ValueError("UNRESOLVED scope is only available for forward-profile conditions")
        return self

    @field_validator("words", mode="before")
    @classmethod
    def typed_words(cls, value: Any, info: Any) -> Any:
        kind = info.data.get("aspect")
        if value is None and cls.open_sources and kind == "condition":
            return value
        if kind in READING_KINDS:
            adapter: TypeAdapter[Any] = TypeAdapter(cls.word_model(READING_KINDS[kind]))
            return (
                adapter.validate_json(json.dumps(value), strict=True)
                if info.mode == "json"
                else adapter.validate_python(value, strict=True)
            )
        return value

    @classmethod
    def word_model(cls, kind: ReadingKind) -> Any:
        if not cls.open_sources:
            if kind.words is MaterialWords:
                return QuotedMaterialWords
            if kind.compiles_to == "condition":
                return str
        return kind.words

    @model_validator(mode="after")
    def has_words(self) -> Finding:
        if self.aspect == "elision":
            procedure_target_kind(
                self.words.model_dump(), allow_unresolved=self.allow_unresolved_procedure,
                allow_location=getattr(self.words, "allow_method_location", False)
            )
        if self.words is None and (self.aspect != "condition" or not self.sequence):
            raise ValueError(
                "words is required unless a condition supplies sourced sequence members"
            )
        return self

    @model_validator(mode="after")
    def canonical_material_words(self) -> Finding:
        # The wire DTO restricted the input already. Keep the domain representation
        # identical to a later sealed-answer restore, preserving field presence.
        if type(self.words) is QuotedMaterialWords:
            object.__setattr__(
                self,
                "words",
                MaterialWords.model_construct(
                    _fields_set=set(self.words.model_fields_set),
                    **{name: getattr(self.words, name) for name in MaterialWords.model_fields},
                ),
            )
        return self

    @model_validator(mode="after")
    def canonical_sequence_members(self) -> Finding:
        object.__setattr__(
            self,
            "sequence",
            tuple(
                ReadingConditionSequenceMember.model_construct(
                    _fields_set=set(member.model_fields_set),
                    text=member.text,
                    source_parts=None,
                    values=member.values,
                )
                if type(member) is ConditionSequenceMember
                else member
                for member in self.sequence
            ),
        )
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core: Any, handler: Any) -> dict[str, Any]:
        schema: dict[str, Any] = handler(core)
        # Resolve refs through the same handler so nested write declarations retain
        # their definitions. The words union already registers every member.
        variants = [v for v in schema["properties"]["words"]["anyOf"] if v.get("type") != "null"]
        models = list(
            dict.fromkeys(
                leaf
                for kind in READING_KINDS.values()
                for leaf in (get_args(cls.word_model(kind)) or (cls.word_model(kind),))
            )
        )
        shapes = dict(zip(models, variants, strict=True))
        # Each alternative is a complete finding. The model should not have to
        # intersect a broad words union with a second list of partial aspect rules.
        # Keep the common fields derived from this model, never a second hand schema.
        meanings = {
            name: field.pop("description")
            for name, field in schema["properties"].items()
            if "description" in field and name != "about_ids"
        }
        description = schema.pop("description", "")
        branches = []
        for aspect, kind in READING_KINDS.items():
            branch = deepcopy(schema)
            properties = branch["properties"]
            properties["aspect"] = {"type": "string", "const": aspect}
            if cls.__name__ != "LegacyFinding":
                branch["description"] = cls.publication_note(aspect)
            leaves = get_args(cls.word_model(kind)) or (cls.word_model(kind),)
            properties["words"] = (
                shapes[leaves[0]]
                if len(leaves) == 1
                else {"anyOf": [shapes[leaf] for leaf in leaves]}
            )
            if aspect == "elision":
                properties["words"] = {
                    **properties["words"],
                    **procedure_target_schema(allow_unresolved=cls.allow_unresolved_procedure),
                }
            if "words" not in branch["required"]:
                if cls.open_sources and aspect == "condition":
                    branch.setdefault("allOf", []).append(
                        {
                            "anyOf": [
                                {"required": ["words"]},
                                {
                                    "required": ["sequence"],
                                    "properties": {"sequence": {"minItems": 1}},
                                },
                            ]
                        }
                    )
                else:
                    branch["required"].append("words")
            if aspect != "condition":
                properties["sequence"] = {"type": "array", "maxItems": 0, "default": []}
                properties["shared_unit_text"] = {"type": "null", "default": None}
            if not kind.accepts_stage:
                properties["existing_stage_id"] = {"type": "null", "default": None}
            else:
                branch.setdefault("allOf", []).append(
                    {
                        "if": {
                            "required": ["existing_stage_id"],
                            "properties": {"existing_stage_id": {"type": "string"}},
                        },
                        "then": {
                            **(
                                {"required": ["scope"]}
                                if cls.new_condition_scope == "WHOLE" or aspect != "condition"
                                else {}
                            ),
                            "properties": {
                                "scope": {"const": "SEGMENT"},
                                "segment_key": {"type": "null"},
                            },
                        },
                    }
                )
            if cls.new_condition_scope == "UNRESOLVED" and aspect == "condition":
                branch.setdefault("allOf", []).append(
                    {
                        "if": {
                            "required": ["segment_key"],
                            "properties": {"segment_key": {"type": "string"}},
                        },
                        "then": {"properties": {"scope": {"const": "SEGMENT"}}},
                    }
                )
            if aspect != "material" or cls.__name__ == "LegacyFinding":
                properties["reporting_level"] = {"type": "null", "default": None}
                properties["detail_of"] = {"type": "null", "default": None}
                properties["clear_summary"] = {"const": False, "default": False}
                properties["scope"] = {
                    "type": "string",
                    "enum": list(kind.scopes),
                    "default": "WHOLE",
                }
            for name, constraints in _declared_about(kind).items():
                properties[name].update(constraints)
            if kind.about.least:
                branch["required"] = [*branch["required"], "about_ids"]
            if aspect == "material" and cls.__name__ != "LegacyFinding":
                for option in properties["scope"]["anyOf"]:
                    if option.get("type") == "string":
                        option["enum"] = list(cls.scope_options(aspect))
            if aspect != "material":
                properties["scope"]["enum"] = list(cls.scope_options(aspect))
                properties["scope"]["default"] = (
                    cls.new_condition_scope if aspect == "condition" else "WHOLE"
                )
            branches.append(branch)
        return {
            "description": description
            + "\nCommon fields:\n"
            + "\n".join(f"{name}: {meaning}" for name, meaning in meanings.items()),
            "oneOf": branches,
        }


# Mechanical field names shared by compilation and its model-facing baseline view.
# Neither this table nor the view decides a scientific endpoint or evidence scope.
INTERMEDIATE_FIELDS = {
    "produced_by": "produced_by_operation_id",
    "consumed_by": "consumed_by_operation_id",
    "label": "author_label",
}


def reading_write_context(
    route: list[dict[str, Any]],
    *,
    expression: bool = True,
    preservation: bool = False,
    open_expression: bool = False,
    unified_expression: bool = False,
    current_updates: bool = False,
) -> dict[str, Any]:
    """A compact entry at the read/write seam, generated from the actual declaration.

    This is navigation into the one write contract, not a second validator or a
    ready-made finding. Baseline values never become freshly quoted evidence here.
    """
    schema = finding_write_model(
        expression=expression, open_expression=open_expression, unified_expression=unified_expression, current_updates=current_updates
    ).model_json_schema()
    branches = {branch["properties"]["aspect"]["const"]: branch for branch in schema["oneOf"]}

    def resolved(shape: dict[str, Any]) -> dict[str, Any]:
        return schema["$defs"][shape["$ref"].rsplit("/", 1)[-1]] if "$ref" in shape else shape

    def shape_name(shape: dict[str, Any]) -> str:
        shape = resolved(shape)
        if "anyOf" in shape:
            return " | ".join(shape_name(part) for part in shape["anyOf"])
        return str(shape["type"])

    def nested_shape(shape: dict[str, Any]) -> Any:
        shape = resolved(shape)
        if "anyOf" in shape:
            return [nested_shape(part) for part in shape["anyOf"]]
        if shape.get("type") == "array":
            return [nested_shape(shape["items"])] if "items" in shape else []
        if "properties" in shape:
            required = shape.get("required", [])
            return {
                key + ("" if key in required else "?"): nested_shape(value)
                for key, value in shape["properties"].items()
            }
        return shape.get("enum", shape.get("const", shape.get("type", "value")))

    def available(shape: dict[str, Any]) -> bool:
        shape = resolved(shape)
        if "anyOf" in shape:
            return any(available(part) for part in shape["anyOf"])
        return shape.get("type") != "null" and shape.get("maxItems") != 0

    entries = {}
    for aspect, kind in READING_KINDS.items():
        branch = branches[aspect]
        words = resolved(branch["properties"]["words"])
        source_entries = source_selection_entries(
            finding_write_model(expression=expression, open_expression=open_expression, unified_expression=unified_expression, current_updates=current_updates), aspect
        )
        entries[aspect] = {
            **({"source_selection_entries": source_entries} if source_entries else {}),
            "required_fields": {
                name: shape_name(branch["properties"][name]) if name == "words"
                else nested_shape(branch["properties"][name])
                for name in branch["required"]
            },
            **({"required_alternatives": deepcopy([
                group for group in branch.get("allOf", ()) if "anyOf" in group
            ])} if any("anyOf" in group for group in branch.get("allOf", ())) else {}),
            "optional_fields": {
                name: nested_shape(shape)
                for name, shape in branch["properties"].items()
                if name != "words" and name not in branch["required"] and available(shape)
            },
            "about_ids": kind.about.text(),
            **(
                {
                    "publication": finding_write_model(
                        expression=expression, open_expression=open_expression, unified_expression=unified_expression, current_updates=current_updates
                    ).publication_note(aspect, preservation=preservation)
                }
                if expression
                else {}
            ),
            "words_shape": (
                {
                    name: nested_shape(field)
                    if unified_expression or (open_expression and "array" in shape_name(field).split(" | "))
                    else shape_name(field)
                    for name, field in words["properties"].items()
                }
                if "properties" in words
                else shape_name(words)
            ),
            **(
                {"required_words_fields": words.get("required", [])}
                if "properties" in words
                else {}
            ),
        }
    shown_fields = {
        field
        for step in route
        for item in (step, *step.get("operations", ()), *step.get("measurements", ()))
        for field in (*item.get("known", {}), *item.get("missing", ()))
    }
    fields = {
        kind.compiles_to: {"aspect": aspect}
        for aspect, kind in READING_KINDS.items()
        if kind.compiles_to in shown_fields and kind.compiles_to not in {"condition", "yield"}
    }
    fields.update(
        {
            field: {"aspect": "condition", "category_word": category}
            for category, field in FIXED_CONDITION_FIELDS.items()
            if field in shown_fields
        }
    )
    intermediate_inputs = []
    for step in route:
        for operation in step.get("operations", ()):
            value = operation.get("known", {}).get("operation_intermediate")
            if isinstance(value, dict):
                intermediate_inputs.append(
                    {
                        "about_ids": [operation["target_id"]],
                        "aspect": "intermediate",
                        "words": {
                            word: value[field]
                            for word, field in INTERMEDIATE_FIELDS.items()
                            if field in value
                        },
                        "not_converted": {
                            key: value[key]
                            for key in value
                            if key not in INTERMEDIATE_FIELDS.values()
                        },
                    }
                )
    return {
        "entry": "write.findings[]",
        "aspects": entries,
        "recorded_fields": fields,
        "intermediate_inputs": intermediate_inputs,
        "notice": (
            "Recorded route fields are baseline values, not write collections. Select an aspect, "
            "put its value in words, and supply that aspect's required finding fields. "
            "For required_alternatives, satisfy every group and at least one anyOf option "
            "in each group. optional_fields are not unconditionally required but remain "
            "subject to these alternatives. "
            "Intermediate inputs map only recorded endpoints and label; not_converted keeps "
            "public identity/structure read-only, not author words or a local identity. "
            "Cite delivered paragraphs and judge ownership, relation and measurement association. "
            "Full constraints and optional fields remain in the write declaration."
        ),
    }


# One stretch of an operation: the author's words where it begins, and the paragraphs
# they are copied from.
class Segment(ContractModel):
    "Declare a distinct stretch of an operation with its opening author words and cited paragraphs."

    item_id: str | None = _handle_field()
    operation_id: str = Field(
        min_length=1, description="Existing operation id whose procedure contains this stretch."
    )
    segment_key: str = Field(
        min_length=1,
        description=(
            "Your name for this stretch, unique within its operation; findings name it in "
            "segment_key."
        ),
    )
    begins_with: str = Field(
        min_length=1,
        description=(
            "Author words at the beginning of this stretch, copied from one cited paragraph."
        ),
    )
    selection_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Delivered paragraph selection ids containing the start of this stretch.",
    )
    explanation: str = Field(
        min_length=1,
        description=(
            "Why this is a distinct stretch of this operation, preserving the procedure's sequence."
        ),
    )


# Which side stands when a finding contradicts the route: the route's reading, or the
# finding's.
class FindingExplanationRef(ContractModel):
    """Explicitly adopt the linked finding's current explanation as this ruling's reason."""

    from_finding: Literal["explanation"] = Field(
        description="Use the linked finding's current explanation as this ruling's reason."
    )


class SoleFindingOwnerRef(ContractModel):
    """Select the linked finding's sole explicit compound owner; ambiguous owners reject."""

    from_finding: Literal["sole_about_id"] = Field(
        description="Use the linked finding's sole explicit compound owner; "
        "ambiguous owners reject."
    )


class ConflictRuling(ContractModel):
    "Choose which reading is primary when a finding contradicts the skeleton."

    item_id: str | None = _handle_field()
    finding: str = Field(
        min_length=1, description="finding_id of the conflicting reading being adjudicated."
    )
    primary: SourcePrimary = Field(
        description=(
            "SKELETON retains the route reading as primary; FINDING chooses the cited "
            "finding as primary, with evidence explained."
        )
    )
    explanation: SCALAR_TEXT | FindingExplanationRef = Field(
        description=(
            "Why this experimental reading takes priority or the skeleton is retained. "
            "Apply the shared route conventions to the evidence and scope at this position."
        ),
    )


# A measurement read in the experimental part or the supplement that is the same
# measurement as one the route already holds; across steps, the steps it spans.
class SameMeasurement(ContractModel):
    """Legacy measurement relation and overall-yield members.

    New single measurements use words.same_measurement_as.
    """

    item_id: str | None = _handle_field()
    finding: str = Field(
        min_length=1,
        description="finding_id of the reported yield paired with the route measurement.",
    )
    outcome_id: str = Field(
        min_length=1,
        description=(
            "Existing route outcome id measuring the same product and experiment, or the "
            "overall yield this finding contributes to."
        ),
    )
    span_block_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Existing reaction-block ids spanned by the overall measurement when pairing "
            "a multi-step combination; empty for a single measurement."
        ),
    )
    explanation: str = Field(
        min_length=1,
        description=(
            "Why this is the same experiment and product, or why this finding belongs to "
            "the stated overall-yield combination."
        ),
    )


# A systematic name read past the cut: another name for the compound, or one that
# contradicts what the route says it is.
class NameRuling(ContractModel):
    "Decide whether a source name supplements or contradicts a route compound."

    item_id: str | None = _handle_field()
    finding: str = Field(
        min_length=1, description="finding_id of the systematic-name finding being attributed."
    )
    compound_id: str = Field(
        min_length=1, description="Existing route compound id whose identity this name concerns."
    )
    relation: Literal["SUPPLEMENTS", "CONTRADICTS"] = Field(
        description=(
            "SUPPLEMENTS when the source gives another name for this compound; "
            "CONTRADICTS when it describes a different identity."
        )
    )
    explanation: SCALAR_TEXT | FindingExplanationRef = Field(
        description="Why the cited name supplements or contradicts the route compound's identity.",
    )


class OpenNameRuling(NameRuling):
    compound_id: SCALAR_TEXT | SoleFindingOwnerRef = Field(
        description=NameRuling.model_fields["compound_id"].description,
    )


@lru_cache(maxsize=2)
def legacy_ruling_writer(model: type[ContractModel]) -> type[ContractModel]:
    """Keep legacy write declarations strict while canonical history accepts both forms."""
    writer = create_model(
        model.__name__,
        __base__=model,
        __doc__=model.__doc__,
        explanation=(SCALAR_TEXT, Field(description=model.model_fields["explanation"].description)),
    )
    return writer


def ruling_explanation(ruling: ConflictRuling | NameRuling, finding: Finding) -> str:
    """Resolve only the explicit reference; independent reasons never follow a finding."""
    if ruling.finding != finding.finding_id:
        raise ValueError("ruling explanation requires its linked finding")
    return (
        finding.explanation
        if isinstance(ruling.explanation, FindingExplanationRef)
        else ruling.explanation
    )


def bind_ruling_owner(item: OpenNameRuling, answer: ReadingAnswer) -> OpenNameRuling:
    """Resolve an explicit sole-owner selection against this complete write candidate."""
    if not isinstance(item.compound_id, SoleFindingOwnerRef):
        return item
    finding = next((f for f in answer.findings if f.finding_id == item.finding), None)
    if finding is None:
        raise ValueError("sole_about_id requires the linked finding in the candidate")
    owners = tuple(dict.fromkeys(finding.about_ids))
    if len(owners) != 1:
        raise ValueError(
            "sole_about_id requires exactly one explicit owner; choose compound_id from "
            + ", ".join(owners)
        )
    return item.model_copy(update={"compound_id": owners[0]})


def current_ruling_view(
    ruling: ConflictRuling | NameRuling, answer: ReadingAnswer
) -> dict[str, Any]:
    finding = next(f for f in answer.findings if f.finding_id == ruling.finding)
    return {
        **ruling.model_dump(mode="json"),
        "resolved_explanation": ruling_explanation(ruling, finding),
    }


# An operation that carries two transformations; recorded, the public operation is not
# split.
class TwoTransformations(ContractModel):
    "Record two transformations within an existing operation without splitting it."

    item_id: str | None = _handle_field()
    operation_id: str = Field(
        min_length=1,
        description="Existing operation id that combines two transformations in the source.",
    )
    explanation: str = Field(
        min_length=1,
        description=(
            "Explain the two transformations and their source support; this record does "
            "not split the public operation."
        ),
    )


# What the middle layer writes for one paper after reading the steps' paragraphs: every
# finding with where it belongs, the stretches, and its rulings, which name findings by
# their ids.
class SkeletonRepair(ContractModel):
    """A source-grounded supplement or correction via a Fusion atomic change group."""

    item_id: str | None = _handle_field()
    route_group_id: str = Field(description="The route group supplied under skeleton groups.")
    repair: InlineFactChangeGroup = Field(
        description="Existing Fusion changes that must succeed together."
    )

    @field_serializer("repair")
    def _sparse(self, value: InlineFactChangeGroup) -> dict[str, Any]:
        # Omitted fields remain omitted across checkpoint/publication round trips.
        return value.model_dump(mode="json", exclude_unset=True)


class UnifiedSkeletonRepair(SkeletonRepair):
    route_group_id: str = Field(
        description="The supplied editing scope: an existing route group, or independent experiments. "
        "An independent experiment does not require a route assignment."
    )
    repair: ReadingInlineFactChangeGroup = Field(
        description=SkeletonRepair.model_fields["repair"].description
    )


READING_COVERAGE_NOTE = (
    "Saved-item and citation coverage describe this reading's output, not whether a "
    "passage was considered or whether the route already has a result. Do not create "
    "findings or per-paragraph acknowledgements to clear these lists."
)


class ReadingAnswer(ContractModel):
    "The raw paper-wide reading: findings, procedural stretches and ownership rulings."

    status: Literal["COMPLETE", "PARTIAL"] = Field(
        description=(
            "Scientific reading status: COMPLETE when the inherited skeleton and saved "
            "result together faithfully preserve the complete author-reported execution "
            "of the assigned experiments, including their procedural details, relationships "
            "and applicable qualifications, with relevant rulings resolved. Unreported "
            "details remain unknown. An already adequate account may need no new finding. "
            "PARTIAL when relevant scientific meaning remains unorganized, unsaved, "
            "unsupported or unresolved in its attribution. Delivery disposition is "
            "separate. " + READING_COVERAGE_NOTE
        )
    )
    explanation: str = Field(
        min_length=1,
        description=(
            "Summarize the completed reading and any missing evidence, unresolved "
            "ownership or outstanding rulings."
        ),
    )
    skeleton_repairs: tuple[SkeletonRepair, ...] = Field(
        default=(),
        description="Local corrections to the existing skeleton, applied in saved order.",
    )
    segments: tuple[Segment, ...] = Field(
        default=(),
        description=(
            "Author-defined stretches of existing operations; declare before findings "
            "that refer to them."
        ),
    )
    findings: tuple[Finding, ...] = Field(
        default=(),
        description=(
            "One item per author value with its object, scope, quotation and relation to the route."
        ),
    )
    narratives: tuple[SupplementalNarrative, ...] = Field(
        default=(),
        description=(
            "Connected source-grounded accounts that cannot be faithfully reduced to one "
            "value: preserve the complete meaning once, with optional explicit subjects. "
            "Author selections are copied by the host; explanations remain your interpretation. "
            "About an operation does not mean applicable throughout that operation."
        ),
    )
    conflicts: tuple[ConflictRuling, ...] = Field(
        default=(),
        description="Rulings selecting the primary reading for findings that contradict the route.",
    )
    same_measurements: tuple[SameMeasurement, ...] = Field(
        default=(),
        description=(
            "Legacy pairings and members of overall-yield combinations. For a single "
            "measurement, use words.same_measurement_as on its finding instead."
        ),
    )
    names: tuple[NameRuling, ...] = Field(
        default=(),
        description=(
            "Attribution rulings for systematic-name findings and existing route compounds."
        ),
    )
    two_transformations: tuple[TwoTransformations, ...] = Field(
        default=(),
        description=(
            "Records of operations containing two transformations without splitting the "
            "published operation."
        ),
    )


# A yield's cuts of its statement, retired for the one value phrase. `percent_text` is
# the cut the host published the value from.
_RETIRED_YIELD_WORDS = ("percent_text", "amount")


def read_reading_answer(sealed: bytes) -> ReadingAnswer:
    """A sealed reading answer in today's shape: the one seam every reader of one shares
    (`KindSpec.read_answer`), as `read_fusion_result` is for the Gate's (E152).

    An answer is sealed once and read for as long as it is kept -- publication,
    reprojection, every audit -- and its bytes are never rewritten. When a change
    retires a shape the model once wrote, a sealed answer in that shape is read as
    today's here, keyed by the retired fields themselves, which no write under today's
    declaration can carry.

    Retired so far:

    - an intermediate's temporary `identity` (W2). Its original value is retained
      privately for historical compilation, never exposed as a new write field.
      New readings derive that mechanical key from the existing saved item identity.
    - a material's `equivalents` (batch 1). The declaration offered the author's one
      phrase for how much as two slots, and a model split it or not as it pleased (a4
      42 of 128 materials, a5-kalmanol 67 of 106; every a3 material carries the key as
      null). The halves are that one phrase, so they are read as it: joined in the
      author's order into `amount`, which the host reads once
      (`written_values._materials`). Read apart, a4 published 38 moles as equivalents.
    - a yield's `percent_text` and `amount` (batch 1). Its words were a whole statement
      plus the model's cuts of it, and the host published the value from the cut
      percentage (every a2-a5 yield carries both keys). Read back, that percentage is
      the value phrase (`OUTCOME_VALUE_PHRASE`), as the host read it then; without one
      the statement stays the phrase. Read as the statement instead, a4's "3 : 1 dr"
      would publish the 97% written beside it as the ratio.
    """
    data = json.loads(sealed)
    legacy_identities: dict[int, str] = {}
    for index, finding in enumerate(data.get("findings") or ()):
        if not isinstance(finding, dict):
            continue
        words = finding.get("words")
        if not isinstance(words, dict):
            continue
        if finding.get("aspect") == "intermediate" and "identity" in words:
            legacy_identities[index] = scalar_text(words.pop("identity"))
        if finding.get("aspect") == "material" and "equivalents" in words:
            halves = (words.get("amount"), words.pop("equivalents"))
            words["amount"] = "; ".join(half for half in halves if half) or None
        if finding.get("aspect") == "yield" and words.keys() & _RETIRED_YIELD_WORDS:
            if words.get("percent_text") is not None:
                words["text"] = words["percent_text"]
            for key in _RETIRED_YIELD_WORDS:
                words.pop(key, None)
    marker = data.get("reading_answer_profile")
    if "reading_answer_profile" in data and marker != EXPLICIT_CURRENT_UPDATE:
        raise ValueError("unknown sealed reading answer profile")
    answer_model = (UnifiedReadingAnswerV2 if marker == EXPLICIT_CURRENT_UPDATE
                    else UnifiedReadingAnswer if "new_experiments" in data else ReadingAnswer)
    answer = answer_model.model_validate_json(json.dumps(data))
    for index, identity in legacy_identities.items():
        answer.findings[index].words._legacy_identity = identity
    return answer


def paragraph_answer(paragraphs: ParagraphSet) -> InvestigationAnswer:
    """A block task's step as its receipt answers it: how it finished, what it read and
    what it could not find. The paragraphs themselves are the middle layer's to read, so
    nothing else is written here."""
    return InvestigationAnswer(
        status=paragraphs.status,
        explanation=paragraphs.explanation,
        coverage=paragraphs.coverage,
        limitations=paragraphs.limitations,
    )


# A value the host could not read; equal to nothing, so its action is never SAME.
_UNREADABLE = object()

# The field a condition of each fixed kind is filed in, read the other way: a finding
# whose aspect is that field is a condition of that kind. One table, both directions.
_KIND_OF_FIELD = {field: kind for kind, field in FIXED_CONDITION_FIELDS.items()}


def _derive_action(
    finding: Finding,
    old: Mapping[str, Any],
    value: Any,
    ruling: ConflictRuling | None,
) -> Literal["ADD", "REFINE", "SAME", "CONFLICT", "CORRECT_MAIN"]:
    """Compile a local experimental decision; existing unresolved alternatives still
    require an explicit ruling. Resolved details use the experimental reading, while
    SAME and SKELETON have already inherited their existing value before this call.
    """
    from tsdaf.lanes.literature.finding_updates import resolved_intent
    intent = resolved_intent(finding)
    if intent is not None:
        if finding.aspect == "material":
            return "REFINE" if old.get("state") == "PRESENT" else "ADD"
        if old.get("state") != "PRESENT":
            return "ADD"
        if old.get("value") == value:
            return "SAME"
        return "CORRECT_MAIN" if intent == "UPDATE_CURRENT" else "SAME"
    ruled_for_finding = ruling is not None and ruling.primary == "FINDING"
    if old.get("reason_code") == "CONFLICTING_CANDIDATES":
        return "CORRECT_MAIN" if ruled_for_finding else "ADD"
    if old.get("state") != "PRESENT":
        return "ADD"
    if old.get("value") == value:
        return "SAME"
    if finding.relation != "CONTRADICTS":
        return "REFINE"
    return "CORRECT_MAIN"


def measurement_words(words: YieldWords) -> dict[str, str]:
    """What a yield finding writes on the measurement it is the same as: the author's
    value phrase, which the host reads as it reads the route's OUTCOME text
    (`written_values`), the basis that phrase states when it states one, and the kind
    the finding chose (owner decision D11). A basis the phrase does not state is left
    as the route holds it."""
    _, basis = parse_reported_outcome(words.text)
    written = {"reported_value": words.text, "kind": words.kind}
    if basis is not None:
        written["basis"] = basis
    return written


# A report that differs from the current value selects nothing by itself: without a
# ruling the current value stays primary and the report is kept beside it with its
# own evidence. CONTRADICTS names the disagreement; it is not the ruling.
_UNRULED_REPORTS = ("ADDS", "CONTRADICTS")


def _keeps_added_value(finding, ruling, old, value, profile) -> bool:
    """The same unruled-report decision drives the primary and its source role."""
    from tsdaf.lanes.literature.finding_updates import resolved_intent
    return bool(profile.unified_expression and isinstance(old, Mapping)
        and old.get("state") == "PRESENT" and resolved_intent(finding) != "UPDATE_CURRENT"
        and finding.relation in _UNRULED_REPORTS and ruling is None and value != old["value"])


def whole_condition_decisions(
    answer: ReadingAnswer,
    machine: Mapping[str, Any],
    references: Callable[[tuple[str, ...]], list[dict[str, str]]] = lambda ids: [],
    *,
    selections: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[tuple[str, str], list[tuple[Finding, Any, bool]]]:
    """Interpret each WHOLE fixed condition against the same immutable task baseline.

    Compilation and progress use these decisions; equal values concern a single
    field assignment, never the identity of experiments or source statements.
    """
    from tsdaf.lanes.literature.finding_updates import resolved_intent
    containers = _containers(machine)
    rulings = {r.finding: r for r in answer.conflicts}
    grouped: dict[tuple[str, str], list[tuple[Finding, Any, bool]]] = {}
    for finding in sorted(answer.findings, key=lambda f: f.finding_id):
        if finding.aspect != "condition" or finding.scope != "WHOLE":
            continue
        category = normalize_condition(
            finding.category_word or "",
            finding.words if isinstance(finding.words, str) else "",
            about=finding.condition_about,
        )
        if category not in FIXED_CONDITION_FIELDS or (
            category == "TEMPERATURE" and not machine_profile(machine).expression
        ):
            continue
        operation_id = finding.about_ids[0]
        field = FIXED_CONDITION_FIELDS[category]
        old = containers[operation_id][1][field]
        ruling = rulings.get(finding.finding_id)
        from tsdaf.lanes.literature.finding_updates import resolved_intent
        intent = resolved_intent(finding)
        keep = (intent == "RECORD" and old.get("state") == "PRESENT") if intent is not None else (
            finding.relation == "SAME" or (ruling is not None and ruling.primary == "SKELETON"))
        if keep and old.get("state") != "PRESENT":
            raise ValueError(
                f"{finding.finding_id}: SAME or SKELETON requires an existing primary {field} value"
            )
        note = condition_source_note(finding, "ADOPTED", [], selections=selections)
        fragments = _condition_fragments(note)
        if keep:
            value = deepcopy(old["value"])
        elif machine_profile(machine).expression and category == "TEMPERATURE":
            value = condition_source_value(note)
            if value is None:
                continue
        else:
            # A fragment collection is not a single scalar or an implicit total.
            if len(fragments) != 1 or finding.sequence:
                continue
            value = project_written_value(
                _field_schema("Operation", field, machine_profile(machine)),
                fragments[0],
                references(finding.value_selection_ids or finding.selection_ids),
                field_key=field,
            )
        grouped.setdefault((operation_id, field), []).append((finding, value, bool(keep)))
    # An explicit current update selects the primary; alternate records still retain
    # their own source notes. Multiple different explicit updates remain unresolved.
    for key, options in grouped.items():
        selected = [value for finding, value, _ in options
                    if resolved_intent(finding) == "UPDATE_CURRENT"]
        if selected and len({json.dumps(v, sort_keys=True) for v in selected}) == 1:
            grouped[key] = [(f, selected[0] if keep else value, keep) for f, value, keep in options]
    return grouped


def finding_interpretations(
    finding: Finding, refs: list[dict[str, str]], ruling: ConflictRuling | None = None,
    *, profile: MachineProfile = MachineProfile.V1, name_ruling: NameRuling | None = None,
) -> list[dict[str, Any]]:
    """Collect scientific explanations before converting to the public profile."""
    from tsdaf.lanes.literature.finding_updates import resolved_intent
    intent = resolved_intent(finding)
    primary = ("FINDING" if intent == "UPDATE_CURRENT" else "SKELETON") if (
        intent is not None and finding._measurement_binding is not None
    ) else ruling.primary if ruling else None
    entries = merge_interpretations(
        [
            model_interpretation(
                finding.explanation,
                refs,
                relation=finding.relation,
                primary=primary,
            )
        ],
        [
            model_interpretation(
                ruling_explanation(ruling, finding),
                refs,
                relation=finding.relation,
                primary=primary,
            )
        ]
        if ruling
        else (),
    )

    if name_ruling is not None:
        entries = merge_interpretations(entries, [model_interpretation(
            ruling_explanation(name_ruling, finding), refs,
            relation="ADDS" if name_ruling.relation == "SUPPLEMENTS" else "CONTRADICTS",
        )])
    return [present(entry, refs) for entry in entries] if profile.unified_expression else entries


def condition_extent(scope: Scope | None) -> Literal["WHOLE_OWNERS", "WITHIN_OWNER", "UNRESOLVED"]:
    if scope == "UNRESOLVED":
        return "UNRESOLVED"
    return "WHOLE_OWNERS" if scope == "WHOLE" else "WITHIN_OWNER"


def finding_source_selections(finding: Finding) -> tuple[tuple[str, SourceSelection], ...]:
    fields = []
    if finding.aspect == "material" and isinstance(finding.words.amount, tuple):
        fields.append(("words.amount", finding.words.amount))
    if finding.aspect == "condition":
        if isinstance(finding.words, ConditionWords):
            fields.append(("words.source_parts", finding.words.source_parts))
        fields.extend(
            (f"sequence[{i}].source_parts", member.source_parts)
            for i, member in enumerate(finding.sequence)
            if getattr(member, "source_parts", None)
        )
    return tuple(
        (f"{path}[{i}]", choice) for path, choices in fields for i, choice in enumerate(choices)
    )


def condition_source_content(
    finding: Finding,
    selections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve private choices without combining source wording or guessing sequence."""

    def resolve(choices: tuple[SourceSelection, ...], path: str) -> list[dict[str, Any]]:
        return [
            selected_source_part(choice, selections, field_path=f"{path}[{i}]")
            for i, choice in enumerate(choices)
        ]

    content: dict[str, Any] = {}
    if isinstance(finding.words, str):
        content["text"] = finding.words
    elif isinstance(finding.words, ConditionWords):
        content["source_parts"] = resolve(finding.words.source_parts, "words.source_parts")
    members = []
    for i, member in enumerate(finding.sequence):
        choices = getattr(member, "source_parts", None)
        body = (
            {"source_parts": resolve(choices, f"sequence[{i}].source_parts")}
            if choices
            else {"text": member.text}
        )
        if member.values:
            body["values"] = [v.model_dump(mode="json") for v in member.values]
        members.append(body)
    if members:
        content["sequence"] = members
    if finding.words is None:
        parts = []
        for i, member in enumerate(members):
            if "source_parts" in member:
                parts.extend(member["source_parts"])
            else:
                ids = finding.value_selection_ids or finding.selection_ids
                found = find_quote(member["text"], ids, selections)
                if found is None:
                    raise ValueError(f"sequence[{i}].text has no delivered source")
                parts.append(
                    {
                        "text": member["text"],
                        "evidence_refs": [
                            dict(selections[sid]["evidence_ref"]) for sid in found.selection_ids
                        ],
                    }
                )
        content["source_parts"] = parts
    return content


def _condition_fragments(content: Mapping[str, Any]) -> list[str]:
    return (
        [content["text"]]
        if isinstance(content.get("text"), str)
        else [part["text"] for part in content.get("source_parts", ())]
    )


def validate_condition_sequence(
    finding: Finding,
    receipt: HoldsSelections,
    machine: Mapping[str, Any],
    item_label: str,
) -> None:
    if finding.aspect != "condition":
        raise ValueError("sequence belongs to a condition finding")
    if not machine_profile(machine).expression:
        raise ValueError("explicit condition sequences require the v1.3 public expression profile")
    content = condition_source_content(finding, receipt.selections)
    members = content.get("sequence", ())
    legacy = isinstance(finding.words, str) and all("text" in m for m in members)
    sources = _condition_fragments(content)
    if not legacy:
        sources += [text for m in members for text in _condition_fragments(m)]
    if finding.shared_unit_text and (
        not members or not any(text_contains(text, finding.shared_unit_text) for text in sources)
    ):
        raise ValueError(
            f"{item_label} / shared_unit_text: shared unit requires a sequence "
            "and the unit in its verified parent or member sources"
        )
    for i, member in enumerate(members):
        fragments = _condition_fragments(member)
        if legacy and not text_contains(finding.words, member["text"]):
            raise ValueError(
                f"{item_label} / sequence[{i}].text: sequence member text must "
                "occur in the complete condition words (field words)"
            )
        for j, value in enumerate(member.get("values", ())):
            if not any(text_contains(fragment, value["text"]) for fragment in fragments):
                raise ValueError(
                    f"{item_label} / sequence[{i}].values[{j}].text: sequence value "
                    "text must occur continuously in its own member source "
                    f"(field sequence[{i}].{'text' if 'text' in member else 'source_parts'})"
                )


def condition_source_note(
    finding: Finding,
    adoption: ConditionAdoption,
    refs: list[dict[str, str]],
    ruling: ConflictRuling | None = None,
    *,
    selections: Mapping[str, Mapping[str, Any]] | None = None,
    profile: MachineProfile = MachineProfile.V1,
) -> dict[str, Any]:
    note = {
        "category_verbatim": finding.category_word or "OTHER",
        "category": normalize_condition(
            finding.category_word or "",
            finding.words if isinstance(finding.words, str) else "",
            about=finding.condition_about,
        ),
        **condition_source_content(finding, selections or {}),
        "about": finding.condition_about,
        "extent": condition_extent(finding.scope),
        "evidence_refs": refs,
        "adoption": adoption,
        "value_relation": finding.value_relation,
        "model_interpretations": merge_interpretations(
            [model_interpretation(finding.explanation, refs)],
            [model_interpretation(ruling_explanation(ruling, finding), refs)] if ruling else (),
        ),
        **({"shared_unit_text": finding.shared_unit_text} if finding.shared_unit_text else {}),
    }

    return resolved_condition_note(note) if profile.unified_expression else note


def condition_adoptions(
    answer: ReadingAnswer,
    machine: Mapping[str, Any],
    decisions: Any = None,
    *,
    selections: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, ConditionAdoption]:
    rulings = {r.finding: r.primary for r in answer.conflicts}
    roles: dict[str, ConditionAdoption] = {
        f.finding_id: "SUPPORTING"
        if f.relation == "SAME"
        else "ALTERNATIVE"
        if rulings.get(f.finding_id) == "SKELETON"
        else "ADOPTED"
        for f in answer.findings
        if f.aspect == "condition"
    }
    from tsdaf.lanes.literature.finding_updates import resolved_intent
    for finding in answer.findings:
        if (isinstance(finding, CandidateFindingV2) and finding.aspect == "condition"
                and finding._measurement_binding is not None):
            roles[finding.finding_id] = ("ADOPTED" if resolved_intent(finding) == "UPDATE_CURRENT"
                                         else "SUPPORTING" if finding.relation == "SAME" else "ALTERNATIVE")
    containers = _containers(machine)
    profile = machine_profile(machine)
    for (operation_id, field), options in (
        whole_condition_decisions(answer, machine, selections=selections)
        if decisions is None
        else decisions
    ).items():
        for finding, value, keep in options:
            if resolved_intent(finding) is not None:
                continue
            if keep and finding.relation != "SAME":
                roles[finding.finding_id] = "ALTERNATIVE"
            elif _keeps_added_value(finding, rulings.get(finding.finding_id),
                                   containers[operation_id][1][field], value, profile):
                roles[finding.finding_id] = "ALTERNATIVE"
        if len({json.dumps(value, sort_keys=True) for _, value, _ in options}) > 1:
            for finding, _, keep in options:
                if not keep:
                    roles[finding.finding_id] = "UNRESOLVED"
    return roles


def material_source_words(
    finding: Finding,
    selections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve fragments in their declared material field, never from a shared list."""
    words = finding.words.model_dump()
    if isinstance(finding.words.amount, tuple):
        words["amount"] = [
            selected_source_part(choice, selections, field_path=f"words.amount[{i}]")
            for i, choice in enumerate(finding.words.amount)
        ]
    return words


def material_field_refs(
    finding: Finding, key: str, receipt: HoldsSelections
) -> list[dict[str, str]]:
    phrase = finding.words.verbatim_name if key == "verbatim_name" else finding.words.amount
    if isinstance(phrase, tuple):
        parts = material_source_words(finding, receipt.selections)["amount"]
        projection = project_material_parts([part["text"] for part in parts])
        index = projection.amount_index if key == "amount" else projection.equivalents_index
        return _refs(receipt, (phrase[index].selection_id,)) if index is not None else []
    if not phrase:
        return []
    quoted = find_quote(
        phrase,
        finding.value_selection_ids or finding.selection_ids,
        receipt.selections,
        material_name=_is_material_name(finding, key),
    )
    return _refs(receipt, quoted.selection_ids) if quoted is not None else []


def material_value_decision(
    finding: Finding,
    previous: Mapping[str, Any],
    profile: MachineProfile,
    ruling: ConflictRuling | None,
    *,
    selections: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool, bool]:
    """Project one material intent for both compilation and its visible saved result.

    This applies the expressed relation; it never infers a different intent from a
    missing or unequal value. Alternative readings remain in the raw finding.
    """
    source_words = material_source_words(finding, selections or {})
    if profile.unified_expression:
        source_words["role_verbatim"] = source_words["role"]
        source_words["role"] = source_words["role"] if source_words["role"] in MATERIAL_ROLES else None
    incoming = project_written_value(
        _field_schema("Operation", "materials", profile),
        [
            {
                key: value
                for key, value in source_words.items()
                if value is not None and key != "identity_relation"
            }
        ],
        [],
        field_key="materials",
    )[0]
    if (
        previous.get("compound_id")
        and incoming.get("compound_id")
        and previous["compound_id"] != incoming["compound_id"]
        and finding.relation != "CONTRADICTS"
    ):
        raise ValueError("a changed compound identity requires CONTRADICTS")
    identity_changed = finding.words.identity_relation == "DIFFERENT" or bool(
        previous.get("compound_id")
        and incoming.get("compound_id")
        and previous["compound_id"] != incoming["compound_id"]
    )
    if identity_changed and finding.relation != "CONTRADICTS":
        raise ValueError("a changed material identity requires CONTRADICTS")
    from tsdaf.lanes.literature.finding_updates import resolved_intent
    intent = resolved_intent(finding)
    keep = (bool(previous) and intent == "RECORD") if intent is not None else (
        finding.relation == "SAME" or (ruling and ruling.primary == "SKELETON"))
    # New rows already have the writer's canonical shape, including absent
    # fields. Keep it for both the table and its field-source snapshot; a
    # later projection must not add nulls that make their exact values differ.
    material = deepcopy(dict(previous) if previous else incoming)
    if not previous or not keep or intent == "RECORD":
        material.update(
            {
                key: value
                for key, value in incoming.items()
                if value is not None and key != "sequence_index"
                and not (
                    (intent == "RECORD" or (intent is None and profile.unified_expression
                       and finding.relation in _UNRULED_REPORTS and ruling is None
                       and not identity_changed))
                    and previous.get(key) is not None
                )
            }
        )
    if identity_changed and not keep:
        material = deepcopy(incoming)
        if "sequence_index" in previous:
            material["sequence_index"] = previous["sequence_index"]
        if profile.expression:
            for key in (
                "reporting_level",
                "summary_material_index",
                "stage_id",
                "extent",
                "relation_evidence_refs",
            ):
                if key in previous:
                    material[key] = deepcopy(previous[key])
    return incoming, material, bool(identity_changed), bool(keep)


def measurement_bindings(
    answer: ReadingAnswer, machine: Mapping[str, Any] | None = None,
    *, measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, SameMeasurement]:
    """One explicit identity interpretation for validation, compilation and progress."""
    findings = {f.finding_id: f for f in answer.findings}
    result: dict[str, SameMeasurement] = {}
    for link in answer.same_measurements:
        finding = findings.get(link.finding)
        if finding is None or finding.aspect != "yield":
            raise ValueError("measurement link must name a saved yield finding")
        prior = result.get(link.finding)
        if prior and (prior.outcome_id, prior.span_block_ids) != (
            link.outcome_id,
            link.span_block_ids,
        ):
            raise ValueError(f"conflicting explicit measurement targets for {link.finding}")
        result[link.finding] = link
    for finding in answer.findings:
        if finding.aspect != "yield" or finding.words.same_measurement_as is None:
            continue
        target = finding.words.same_measurement_as
        prior = result.get(finding.finding_id)
        if prior and (prior.span_block_ids or prior.outcome_id != target):
            raise ValueError(
                f"conflicting measurement relation for {finding.finding_id}: "
                "inline single measurement and legacy target/span disagree"
            )
        if prior is None:
            result[finding.finding_id] = SameMeasurement(
                finding=finding.finding_id, outcome_id=target, explanation=finding.explanation
            )
    if machine is not None:
        from tsdaf.contracts.literature.objects import experiment_containers, reported_outcomes
        blocks = experiment_containers(machine)
        outcomes = {o["outcome_id"] for o in reported_outcomes(machine)}
        block_ids = {b.get("reaction_block_id", b.get("account_id")) for b in blocks}
        for link in result.values():
            finding = findings[link.finding]
            comparison = (measurement_machines or {}).get(finding.item_id)
            available = ({o["outcome_id"] for o in reported_outcomes(comparison)}
                         if comparison is not None else outcomes)
            if link.outcome_id not in available or set(link.span_block_ids) - block_ids:
                raise ValueError("measurement relation names an unknown measurement or block")
    return result


def reconcile_measurement_updates(
    before: ReadingAnswer,
    after: ReadingAnswer,
    changes: tuple[tuple[str, ContractModel], ...],
) -> ReadingAnswer:
    """Explicit later edits replace old counterpart syntax, never scientific inference.

    Same-call contradictory declarations are refused before old counterparts are removed.
    Historical resources are not altered; the Writer registers the resulting snapshot.
    """
    if isinstance(after, UnifiedReadingAnswer):
        from tsdaf.lanes.literature.finding_updates import inherit_measurement_context
        previous = {f.finding_id: f for f in before.findings}
        return after.model_copy(update={"findings": tuple(
            inherit_measurement_context(finding, previous.get(finding.finding_id))
            for finding in after.findings
        )})
    declared: dict[str, tuple[str | None, tuple[str, ...]]] = {}
    inline_declared: dict[str, str | None] = {}
    for collection, item in changes:
        if collection == "findings" and item.aspect == "yield":
            if "same_measurement_as" not in item.words.model_fields_set:
                continue
            key, relation = item.finding_id, (item.words.same_measurement_as, ())
            inline_declared[key] = item.words.same_measurement_as
        elif collection == "same_measurements":
            key, relation = item.finding, (item.outcome_id, item.span_block_ids)
        else:
            continue
        if key in declared and declared[key] != relation:
            raise ValueError(f"conflicting measurement declarations in this write for {key}")
        declared[key] = relation
    written = {i.finding_id: i for c, i in changes if c == "findings" and i.aspect == "yield"}
    links = {i.finding: i for c, i in changes if c == "same_measurements"}
    previous = {f.finding_id: f for f in before.findings}
    findings = list(after.findings)
    pairs = list(after.same_measurements)
    for index, finding in enumerate(findings):
        if finding.aspect != "yield":
            continue
        key = finding.finding_id
        item = written.get(key)
        explicit = key in inline_declared
        target = inline_declared[key] if explicit else finding.words.same_measurement_as
        if explicit:
            finding = finding.model_copy(
                update={"words": finding.words.model_copy(update={"same_measurement_as": target})}
            )
        old = previous.get(key)
        if item is not None and not explicit and old is not None and old.aspect == "yield":
            target = old.words.same_measurement_as
            finding = finding.model_copy(
                update={"words": finding.words.model_copy(update={"same_measurement_as": target})}
            )
        link = links.get(key)
        if explicit and link is not None:
            if target is None or link.span_block_ids or link.outcome_id != target:
                raise ValueError(f"conflicting measurement declarations in this write for {key}")
        elif explicit:
            old_pairs = [p for p in pairs if p.finding == key]
            if any(p.span_block_ids for p in old_pairs):
                raise ValueError(
                    "a spanning relation cannot be replaced by an inline single measurement"
                )
            pairs = [p for p in pairs if p.finding != key or (target and p.outcome_id == target)]
        elif link is not None and target is not None:
            if link.span_block_ids:
                raise ValueError(
                    "a spanning relation cannot silently replace an inline single measurement"
                )
            if link.outcome_id != target:
                finding = finding.model_copy(
                    update={"words": finding.words.model_copy(update={"same_measurement_as": None})}
                )
        findings[index] = finding
    removed = set(previous) - {f.finding_id for f in findings}
    pairs = [p for p in pairs if p.finding not in removed]
    return after.model_copy(update={"findings": tuple(findings), "same_measurements": tuple(pairs)})


def other_experiment_narratives(answer, receipt):
    """Publicly carry the model's explicit account without assigning a new owner."""
    from tsdaf.lanes.literature.enrichment_values import CompiledNarrative

    def words_only(value):
        if isinstance(value, Mapping):
            return {k: words_only(v) for k, v in value.items()
                    if k not in {"evidence_refs", "selection_id"} and v is not None}
        if isinstance(value, (tuple, list)):
            return [words_only(v) for v in value]
        return value

    result = []
    for finding in getattr(answer, "findings", ()):
        if finding.scope != "ANOTHER_EXPERIMENT" or finding.aspect not in {"material", "condition"}:
            continue
        report = {"aspect": finding.aspect, "scope": finding.scope,
                  "relation": finding.relation}
        if finding.category_word:
            report["category"] = finding.category_word
        if finding.condition_about:
            report["about"] = finding.condition_about
        if finding.about_ids:
            report["comparison_context_not_experiment_assignment"] = list(finding.about_ids)
        if finding.aspect == "material":
            report["material"] = words_only(material_source_words(finding, receipt.selections))
            if finding.reporting_level:
                report["reporting_level"] = finding.reporting_level
        else:
            report.update(words_only(condition_source_content(finding, receipt.selections)))
            report["value_relation"] = finding.value_relation
            if finding.shared_unit_text:
                report["shared_unit_text"] = finding.shared_unit_text
        if finding.review_reason:
            report["review_reason"] = finding.review_reason
        source_ids = tuple(dict.fromkeys((*finding.selection_ids,
            *(choice.selection_id for _, choice in finding_source_selections(finding)))))
        result.append(CompiledNarrative(
            source_finding=finding.finding_id, item_id=finding.item_id,
            source_parts=tuple(selected_source_part(choice, receipt.selections, field_path=path)
                               for path, choice in finding_source_selections(finding)),
            title="Other experiment: " + finding.finding_id,
            kind="EXPERIMENTAL_ACCOUNT", subjects=(),
            explanation=finding.explanation + "\n\nModel's reported account:\n"
                        + json.dumps(report, ensure_ascii=False, indent=2),
            selection_ids=source_ids,
            author_selection_ids=tuple(sid for sid in source_ids
                                       if receipt.selections[sid].get("exact_text") is not None),
        ))
    return tuple(result)


def compile_reading(
    answer: ReadingAnswer,
    receipt: ReceiptEvidence,
    machine: Mapping[str, Any],
    *,
    task_baseline: Mapping[str, Any] | None = None,
    material_bindings: Mapping[str, str | None] | None = None,
    creation_keys: Mapping[str, str] | None = None,
    method_targets: Mapping[str, Mapping[str, str]] | None = None,
    measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
) -> InvestigationAnswer:
    """The collections the middle layer's reading compiles to, against ``machine``.

    ``machine`` is the cumulative wrapping target; ``task_baseline`` is the immutable
    input used to interpret fixed-condition decisions (the same machine if omitted).
    ``receipt`` is the middle
    layer's own, holding every paragraph it was given, so every citation resolves there.
    The answer is what a block task would have written had it settled field, action and
    stretch itself; the executor then treats it as it treats any answer. Compound
    expressions have explicit host-only contributions in v1.4. Two-transformations
    declarations remain in the saved reading without splitting public operations.
    """
    _require_active_reading_capabilities(answer)
    from tsdaf.lanes.literature.finding_updates import resolve_answer, resolved_intent
    answer = resolve_answer(answer, measurement_machines)
    answer = _bound_to_their_words(answer, receipt)
    rulings = {r.finding: r for r in answer.conflicts}
    same = measurement_bindings(answer, machine, measurement_machines=measurement_machines)
    profile = machine_profile(machine)
    containers = _containers(machine)
    condition_baseline = machine if task_baseline is None else task_baseline
    condition_containers = _containers(condition_baseline)
    if material_bindings is None and task_baseline is not None:
        material_bindings = unchanged_bindings(task_baseline, machine)
    def compiled_interpretations(finding, refs, ruling=None, *, name_ruling=None):
        return finding_interpretations(
            finding, refs, ruling, profile=profile, name_ruling=name_ruling
        )

    patches: list[ContainerPatch] = []
    conditions: list[ConditionAddition] = []
    characterizations: list[CompoundCharacterization] = []
    compounds: list[CompoundAddition] = []
    elisions: list[ProcedureElision] = []
    limitations: list[InvestigationLimitation] = []
    compound_expressions: list[CompiledCompoundExpression] = []
    measurements = []
    local_intermediates = []
    contributions: dict[str, list[tuple[str, int]]] = {}
    target_limits: dict[str, list[dict[str, str]]] = {}
    uncompiled: dict[str, str] = {}

    def contributed(collection: str, index: int, items: tuple[Finding, ...]) -> None:
        for item in items:
            contributions.setdefault(item.finding_id, []).append((collection, index))

    def markers(finding: Finding, material_index: int | None = None) -> list[dict[str, Any]]:
        if not finding.review_reason:
            return []
        marker: dict[str, Any] = {
            "reason": finding.review_reason,
            "evidence_refs": _refs(receipt, finding.value_selection_ids or finding.selection_ids),
        }
        if material_index is not None:
            marker["material_index"] = material_index
        return [marker]

    def patch(
        finding: Finding,
        target_id: str,
        field_key: str,
        words: Any,
        *,
        compound_aliases: dict[str, tuple[str, ...]] | None = None,
        review_markers: list[dict[str, Any]] | None = None,
        alternative_words: Any = None,
        material_source_updates: tuple[dict[str, Any], ...] = (),
        material_relation_updates: tuple[dict[str, Any], ...] = (),
        support_selection_ids: tuple[str, ...] | None = None,
        alternative_selection_ids: tuple[str, ...] = (),
        prior_value: Mapping[str, Any] | None = None,
        condition_sources: tuple[dict[str, Any], ...] = (),
        contributing_findings: tuple[Finding, ...] = (),
        outcome_group: str | None = None,
        outcome_source: dict[str, Any] | None = None,
    ) -> None:
        # A target or field the machine does not hold, or words the host cannot read,
        # still compile: the executor refuses them with its own sentence, as it would
        # have refused the task's own write.
        entry = containers.get(target_id)
        old = (
            prior_value
            if prior_value is not None
            else (entry[1].get(field_key) if entry is not None else None)
        )
        ruling = rulings.get(finding.finding_id)
        if ruling and ruling.primary == "SKELETON" and support_selection_ids is None:
            alternative_words = words
            support_selection_ids = ()
            alternative_selection_ids = finding.selection_ids
        if (
            isinstance(old, Mapping)
            and old.get("state") == "PRESENT"
            and resolved_intent(finding) != "UPDATE_CURRENT"
            and (finding.relation == "SAME" or (ruling and ruling.primary == "SKELETON"))
        ):
            words = deepcopy(old["value"])
            if outcome_source is not None:
                # Its original phrase remains separately sourced, not retroactively
                # treated as support for the retained (possibly different) scalar.
                support_selection_ids = ()
        value: Any = _UNREADABLE
        if entry is not None and isinstance(old, Mapping):
            primary = finding.value_selection_ids or finding.selection_ids
            try:
                value = project_written_value(
                    _field_schema(entry[0], field_key, profile),
                    words,
                    _refs(receipt, primary),
                    field_key=field_key,
                )
            except (ValueError, KeyError):
                value = _UNREADABLE
        if field_key != "materials" and _keeps_added_value(finding, ruling, old, value, profile):
            alternative_words = words
            alternative_selection_ids = finding.value_selection_ids or finding.selection_ids
            words = deepcopy(old["value"])
            value = deepcopy(old["value"])
            support_selection_ids = ()
        contributed("patches", len(patches), contributing_findings or (finding,))
        patches.append(
            CompiledReadingPatch(
                condition_sources=condition_sources,
                outcome_group=outcome_group,
                outcome_source=outcome_source,
                target_id=target_id,
                field_key=field_key,
                action=_derive_action(
                    finding,
                    old if isinstance(old, Mapping) else {},
                    value,
                    rulings.get(finding.finding_id),
                ),
                value=words,
                alternative_value=alternative_words,
                material_source_updates=material_source_updates,
                material_relation_updates=material_relation_updates,
                support_selection_ids=support_selection_ids,
                alternative_selection_ids=alternative_selection_ids,
                compound_aliases=compound_aliases or {},
                review_markers=tuple(
                    markers(finding) if review_markers is None else review_markers
                ),
                selection_ids=finding.selection_ids,
                primary_selection_ids=finding.value_selection_ids,
                explanation=finding.explanation,
            )
        )

    words: Any
    grouped_materials: dict[str, list[Finding]] = {}
    # Preserve the complete reading, but compile only this experiment's values into
    # its operation. Apply the boundary before grouping so another run cannot replace
    # a named material, contribute evidence, or turn a sibling supplement into conflict.
    findings = tuple(
        finding
        for finding in answer.findings
        if (
            has_public_scope(finding, machine)
            if profile.expression
            else not (
                finding.scope == "ANOTHER_EXPERIMENT"
                and finding.aspect in {"condition", "material", "yield"}
            )
        )
    )
    for finding in findings:
        if finding.aspect == "material":
            grouped_materials.setdefault(finding.about_ids[0], []).append(finding)

    for operation_id, items in grouped_materials.items():
        original_words = containers[operation_id][1]["materials"].get("value") or []
        words = deepcopy(original_words)
        for position, material in enumerate(words, 1):
            material["sequence_index"] = position
        positions = {
            key: row["index"]
            for key, row in skeleton_materials(machine, material_bindings).items()
            if row["operation_id"] == operation_id
        }
        aliases: dict[str, tuple[str, ...]] = {}
        alternatives: dict[int, Any] = {}
        material_markers: list[dict[str, Any]] = []
        source_updates: list[dict[str, Any]] = []
        relation_updates: list[dict[str, Any]] = []
        finding_positions: dict[str, int] = {}
        retained_materials: set[str] = set()
        for finding in items:
            targets = target_limits.setdefault(finding.finding_id, [])
            targets.append({"object_type": "OPERATION", "object_id": operation_id,
                            "field_key": "materials"})
            index = positions.get(finding.skeleton_material_id or "")
            if finding.skeleton_material_id is not None and index is None:
                raise UnknownReference(
                    "skeleton_material_id",
                    finding.skeleton_material_id,
                    sorted(positions),
                    "selected material was removed or has no unique current binding",
                )
            material_markers.extend(
                markers(finding, (index if index is not None else len(words)) + 1)
            )
            previous = words[index] if index is not None else {}
            # Evidence replay starts from the exact persisted row. Host-added order
            # belongs to the result, not to the identity of the previous source.
            prior_updates = [u for u in source_updates if u["index"] == index]
            source_before = (
                (prior_updates[-1]["after"] if prior_updates else original_words[index])
                if index is not None
                else {}
            )
            ruling = rulings.get(finding.finding_id)
            incoming, material, identity_changed, keep = material_value_decision(
                finding, previous, profile, ruling, selections=receipt.selections
            )
            if index is not None and ruling and ruling.primary == "SKELETON":
                alternate = deepcopy(previous)
                alternate.update(
                    {
                        key: value
                        for key, value in incoming.items()
                        if value is not None and key != "sequence_index"
                    }
                )
                alternate.pop("names", None)
                if identity_changed:
                    alternate = deepcopy(incoming)
                    alternate["sequence_index"] = previous["sequence_index"]
                alternatives[index] = alternate
            # Correspondence is the middle layer's position/identity decision, never
            # a similarity match. Quantity corrections preserve the same identity.
            if not identity_changed:
                names = list(
                    dict.fromkeys(
                        name
                        for name in (
                            *previous.get("names", []),
                            previous.get("verbatim_name"),
                            finding.words.verbatim_name,
                        )
                        if name
                    )
                )
                compound_id = material.get("compound_id")
                if compound_id:
                    targets.append({"object_type": "COMPOUND", "object_id": compound_id,
                                    "field_key": "names"})
                    aliases[compound_id] = tuple(
                        dict.fromkeys((*aliases.get(compound_id, ()), *names))
                    )
                    material.pop("names", None)
                elif profile.wrapping:
                    material["names"] = names
            elif not keep:
                material.pop("names", None)
                if profile.wrapping and not material.get("compound_id"):
                    material["names"] = [material["verbatim_name"]]
            source_index = index if index is not None else len(words)
            finding_positions[finding.finding_id] = source_index
            if profile.expression:
                refs = _refs(receipt, finding.selection_ids)
                material["model_interpretations"] = merge_interpretations(
                    material.get("model_interpretations", []),
                    compiled_interpretations(finding, refs, ruling),
                )
                if keep:
                    retained_materials.add(finding.finding_id)
            if profile.preservation:
                submitted_words = material_source_words(finding, receipt.selections)
                if profile.unified_expression:
                    submitted_words["role_verbatim"] = submitted_words["role"]
                    submitted_words["role"] = submitted_words["role"] if submitted_words["role"] in MATERIAL_ROLES else None
                reading = material_source_reading(
                    **submitted_words,
                    refs=_refs(receipt, finding.value_selection_ids or finding.selection_ids),
                    interpretations=compiled_interpretations(
                        finding, _refs(receipt, finding.selection_ids), ruling
                    ),
                )
                if profile.unified_expression and reading.get("amount"):
                    amount_source = reading["amount"]
                    parts = amount_source if isinstance(amount_source, list) else [
                        {"text": amount_source, "evidence_refs": material_field_refs(finding, "amount", receipt)}
                    ]
                    reading["reported_quantities"] = sourced_material_quantities(parts)
                material["source_readings"] = merge_interpretations(
                    material.get("source_readings", []), [reading]
                )
            if profile.expression and not keep:
                if finding.clear_summary:
                    material.pop("summary_material_index", None)
                if finding.reporting_level is not None:
                    material["reporting_level"] = finding.reporting_level
                if (
                    finding.reporting_level is not None
                    or finding.clear_summary
                    or finding.detail_of is not None
                    or finding.scope is not None
                ):
                    material["relation_evidence_refs"] = list(
                        {
                            json.dumps(ref, sort_keys=True): ref
                            for ref in [
                                *material.get("relation_evidence_refs", []),
                                *_refs(receipt, finding.selection_ids),
                            ]
                        }.values()
                    )
                if finding.scope is not None:
                    relation_updates.append(
                        {
                            "index": source_index,
                            "scope": finding.scope,
                            "segment_key": finding.segment_key,
                            "existing_stage_id": finding.existing_stage_id,
                            "evidence_refs": _refs(receipt, finding.selection_ids),
                        }
                    )
            if index is None:
                material["sequence_index"] = (
                    max((row.get("sequence_index") or 0 for row in words), default=0) + 1
                )
                words.append(material)
            else:
                words[index] = material
            # Only supplied fields actually adopted support this row. A kept skeleton
            # alternative does not become primary evidence; missing quantities inherit.
            source_updates.append(
                {
                    "index": source_index,
                    "before": deepcopy(source_before),
                    "after": deepcopy(material),
                    "reset_identity": bool(identity_changed and not keep),
                    "fields": {
                        key: field_refs
                        for key in MATERIAL_SOURCE_FIELDS
                        if incoming.get(key) is not None
                        and incoming[key] == material.get(key)
                        and not (
                            ruling and ruling.primary == "SKELETON" and finding.relation != "SAME"
                        )
                        and (field_refs := material_field_refs(finding, key, receipt))
                    },
                }
            )
        if profile.expression:
            for finding in items:
                if finding.detail_of is None or finding.finding_id in retained_materials:
                    continue
                index = finding_positions[finding.finding_id]
                target = finding_positions.get(finding.detail_of, positions.get(finding.detail_of))
                if target is None or target == index:
                    raise ValueError("detail_of must name another material in this operation")
                if words[index].get("reporting_level") not in (None, "USE") or words[target].get(
                    "reporting_level"
                ) not in (None, "TOTAL"):
                    raise ValueError("detail_of links a use to a total, not the reverse")
                if words[target].get("summary_material_index") is not None:
                    raise ValueError("a material summary cannot itself be a detail")
                words[index]["reporting_level"] = "USE"
                words[index]["summary_material_index"] = target + 1
                words[target]["reporting_level"] = "TOTAL"
                for row in (words[index], words[target]):
                    row["relation_evidence_refs"] = list(
                        {
                            json.dumps(ref, sort_keys=True): ref
                            for ref in [
                                *row.get("relation_evidence_refs", []),
                                *_refs(receipt, finding.selection_ids),
                            ]
                        }.values()
                    )
            # The relation may change a referenced summary without a separate value write.
            for index, row in enumerate(words):
                if not any(u["index"] == index for u in source_updates):
                    original = containers[operation_id][1]["materials"].get("value") or []
                    if index < len(original) and original[index] != row:
                        source_updates.append(
                            {
                                "index": index,
                                "before": deepcopy(original[index]),
                                "after": deepcopy(row),
                                "reset_identity": False,
                                "fields": {},
                            }
                        )
            # These are the same accepted row decisions, with their resolved public relation.
            for index, row in enumerate(words):
                updates = [u for u in source_updates if u["index"] == index]
                if updates:
                    updates[-1]["after"] = deepcopy(row)
                    for relation_key in ("reporting_level", "summary_material_index"):
                        if row.get(relation_key) != updates[-1]["before"].get(relation_key):
                            updates[-1]["fields"][relation_key] = deepcopy(
                                row.get("relation_evidence_refs", [])
                            )
        # Each position has already been adjudicated. One contradiction cannot
        # convert the integrated table (and its normal siblings) into FieldConflict.
        merged = _combined(items).model_copy(update={"relation": "ADDS"})
        material_rulings = dict(rulings)
        rulings.pop(merged.finding_id, None)
        alternative_words = deepcopy(words) if alternatives else None
        if alternative_words is not None:
            for index, alternate in alternatives.items():
                alternative_words[index] = alternate
        patch(
            merged,
            operation_id,
            "materials",
            words,
            contributing_findings=tuple(items),
            compound_aliases=aliases,
            review_markers=material_markers,
            material_source_updates=tuple(source_updates),
            material_relation_updates=tuple(relation_updates),
            alternative_words=alternative_words,
            support_selection_ids=tuple(
                dict.fromkeys(
                    sid
                    for f in items
                    if not (
                        material_rulings.get(f.finding_id)
                        and material_rulings[f.finding_id].primary == "SKELETON"
                    )
                    for sid in f.selection_ids
                )
            ),
            alternative_selection_ids=tuple(
                dict.fromkeys(
                    sid
                    for f in items
                    if material_rulings.get(f.finding_id)
                    and material_rulings[f.finding_id].primary == "SKELETON"
                    for sid in f.selection_ids
                )
            ),
        )
    # One baseline and one priority rule for every fixed-field group. Equal resulting
    # values permit one field assignment, but do not establish repeated-experiment
    # identity: the original findings and their source-bearing notes remain separate.
    retained_conditions: set[str] = set()
    condition_decisions = whole_condition_decisions(
        answer, condition_baseline, lambda ids: _refs(receipt, ids), selections=receipt.selections
    )
    adoptions = (
        condition_adoptions(answer, condition_baseline, condition_decisions)
        if profile.expression
        else {}
    )
    for (operation_id, field), options in condition_decisions.items():
        ordered = [finding for finding, _, _ in options]
        old = condition_containers[operation_id][1][field]
        sources = (
            tuple(
                condition_source_note(
                    f,
                    adoptions[f.finding_id],
                    _refs(receipt, f.selection_ids),
                    rulings.get(f.finding_id),
                    selections=receipt.selections,
                    profile=profile,
                )
                for f in ordered
            )
            if profile.expression
            else ()
        )
        values = {json.dumps(value, sort_keys=True) for _, value, _ in options}
        if len(values) == 1:
            # Prefer an explicit correcting relation as the representative action.
            # This choice affects no value; every row has the same effective primary.
            preferred = sorted(
                options,
                key=lambda row: (
                    row[2],
                    bool(
                        rulings.get(row[0].finding_id)
                        and rulings[row[0].finding_id].primary == "SKELETON"
                    ),
                    row[0].relation != "CONTRADICTS",
                    row[0].finding_id,
                ),
            )[0]
            finding, value, keep = preferred
            combined = _combined([finding, *[f for f in ordered if f is not finding]])
            support = tuple(
                dict.fromkeys(
                    sid
                    for f, _, retained in options
                    if not (
                        retained
                        and (resolved_intent(f) == "RECORD" or (
                            rulings.get(f.finding_id) and rulings[f.finding_id].primary == "SKELETON"))
                    )
                    for sid in (f.value_selection_ids or f.selection_ids)
                )
            )
            patch(
                combined,
                operation_id,
                field,
                finding.words if keep and resolved_intent(finding) is None else value,
                # Multiple rejected readings keep their own notes, not a representative
                # alternative bearing other values' evidence. Empty tuple means no support.
                support_selection_ids=(() if keep else support) if resolved_intent(finding) is not None
                else None if len(ordered) == 1 else support,
                prior_value=old,
                contributing_findings=tuple(ordered),
                condition_sources=sources,
                review_markers=[m for f in ordered for m in markers(f)],
            )
        else:
            combined = _combined(ordered)
            contributed("patches", len(patches), tuple(ordered))
            patches.append(
                CompiledReadingPatch(
                    condition_sources=sources,
                    target_id=operation_id,
                    field_key=field,
                    action="SAME" if old.get("state") == "PRESENT" else "ADD",
                    value=deepcopy(old.get("value")),
                    selection_ids=combined.selection_ids,
                    explanation="Multiple whole-operation readings have no unique primary value.",
                    competing_values=tuple(
                        {
                            "value": value,
                            "selection_ids": ()
                            if keep
                            else (f.value_selection_ids or f.selection_ids),
                            "baseline": keep,
                        }
                        for f, value, keep in options
                    ),
                )
            )
        if len(ordered) > 1:
            retained_conditions.update(f.finding_id for f in ordered)
    handled_conditions = {
        f.finding_id for options in condition_decisions.values() for f, _, _ in options
    }
    for finding in findings:
        aspect = READING_KINDS[finding.aspect].compiles_to
        if finding.aspect == "material":
            continue
        if aspect == CONDITION_ASPECT or aspect in _KIND_OF_FIELD:
            (operation_id,) = finding.about_ids
            text = finding.words if isinstance(finding.words, str) else ""
            key = _KIND_OF_FIELD.get(aspect) or normalize_condition(
                finding.category_word or "", text, about=finding.condition_about
            )
            scope = finding.scope or "WHOLE"
            if finding.finding_id in handled_conditions and (
                profile.expression or finding.finding_id not in retained_conditions
            ):
                continue
            contributed("new_conditions", len(conditions), (finding,))
            conditions.append(
                CompiledCondition(
                    source_note=condition_source_note(
                        finding,
                        adoptions[finding.finding_id],
                        _refs(receipt, finding.selection_ids),
                        rulings.get(finding.finding_id),
                        selections=receipt.selections,
                    profile=profile,
                    )
                    if profile.expression
                    else None,
                    retain_as_note=finding.finding_id in retained_conditions
                    or (profile.expression and finding.finding_id not in handled_conditions),
                    update_current=(getattr(finding, "_measurement_binding", None) or {}).get("resolved_intent") == "UPDATE_CURRENT",
                    relation=finding.relation,
                    primary=rulings[finding.finding_id].primary
                    if finding.finding_id in rulings
                    else "FINDING",
                    value_relation=finding.value_relation,
                    review_reason=finding.review_reason,
                    value_selection_ids=finding.value_selection_ids,
                    condition_id=finding.finding_id,
                    operation_id=operation_id,
                    category_verbatim=finding.category_word or key or aspect,
                    text=text or None,
                    extent=condition_extent(scope),
                    about=finding.condition_about,
                    stage_key=finding.segment_key if scope == "SEGMENT" else None,
                    existing_stage_id=finding.existing_stage_id,
                    selection_ids=finding.selection_ids,
                    explanation=finding.explanation,
                )
            )
        elif aspect == "characterization":
            words = finding.words.model_dump() if isinstance(finding.words, ContractModel) else {}
            contributed("characterizations", len(characterizations), (finding,))
            characterizations.append(
                CompiledCharacterization(
                    model_interpretations=tuple(
                        compiled_interpretations(
                            finding,
                            _refs(receipt, finding.selection_ids),
                            rulings.get(finding.finding_id),
                        )
                    )
                    if profile.expression
                    else (),
                    review_markers=tuple(markers(finding)),
                    explicit_identity=isinstance(finding, CandidateFindingV2),
                    replace_sample_context="sample_context" in finding.words.model_fields_set,
                    update_current=(getattr(finding, "_measurement_binding", None) or {}).get("resolved_intent") == "UPDATE_CURRENT",
                    characterization_id=finding.finding_id,
                    compound_ids=finding.about_ids,
                    method_verbatim=str(words.get("statement", "")),
                    sample_context=words.get("sample_context"),
                    same_measurement_as=words.get("same_measurement_as"),
                    selection_ids=finding.selection_ids,
                )
            )
        elif aspect == "new_compound":
            contributed("new_compounds", len(compounds), (finding,))
            compounds.append(
                compound_addition(finding).model_copy(
                    update={"review_markers": tuple(markers(finding))}
                )
            )
        elif aspect == "yield":
            if profile.unified_expression:
                from tsdaf.lanes.literature.unified_reading import compile_measurement
                contributed("measurements", len(measurements), (finding,))
                measurements.append(compile_measurement(
                    finding, receipt, machine, creation_keys, rulings.get(finding.finding_id),
                    comparison_machine=(measurement_machines or {}).get(finding.item_id),
                ))
                continue
            ruled = same.get(finding.finding_id)
            yield_words = finding.words.model_copy(
                update={"product_ids": yield_product_ids(finding, machine)}
            )
            # Batch 4 only refines an existing single measurement. Span members,
            # unpaired measurements and other experiments remain in the raw answer;
            # publishing their composition belongs to the later yield lane.
            if ruled is not None and not ruled.span_block_ids:
                source = None
                if profile.preservation:
                    meaning = compiled_interpretations(
                        finding,
                        _refs(receipt, finding.selection_ids),
                        rulings.get(finding.finding_id),
                    )
                    if any(link.finding == finding.finding_id for link in answer.same_measurements):
                        meaning = merge_interpretations(
                            meaning,
                            [
                                model_interpretation(
                                    ruled.explanation, _refs(receipt, finding.selection_ids)
                                )
                            ],
                        )
                    source = source_statement(
                        finding.words.text,
                        _refs(receipt, finding.value_selection_ids or finding.selection_ids),
                        meaning,
                        kind=finding.words.kind,
                        product_ids=list(yield_words.product_ids),
                        product_label=finding.words.product_label,
                    )
                for field_key, words in measurement_words(yield_words).items():
                    patch(
                        finding,
                        ruled.outcome_id,
                        field_key,
                        words,
                        outcome_group=finding.finding_id if source else None,
                        outcome_source=source,
                    )
        elif aspect == "elision":
            words = finding.words.model_dump() if isinstance(finding.words, ContractModel) else {}
            procedure_target_kind(
                words, allow_unresolved=profile.unified_expression,
                allow_location=profile.unified_expression,
            )
            target = resolve_method_target(
                words, method_targets=method_targets or {},
                cited_revisions={str(receipt.selections[sid]["source_identity"]["document_revision_id"])
                                 for sid in finding.selection_ids},
            ) if profile.unified_expression else None
            (operation_id,) = finding.about_ids
            contributed("procedure_elisions", len(elisions), (finding,))
            elisions.append(
                CompiledElision(
                    review_markers=tuple(markers(finding)),
                    operation_id=operation_id,
                    referenced_operation_id=words.get("referenced_operation_id"),
                    referenced_unit_block_id=(
                        target["unit_block_id"] if target else words.get("referenced_unit_block_id")
                    ),
                    target_source_document_id=target["document_id"] if target else None,
                    target_source_revision_id=target["document_revision_id"] if target else None,
                    source_parts=tuple(selected_source_part(
                                       SourceSelection(selection_id=sid), receipt.selections,
                                       field_path="elision.source_parts")
                                       for sid in (finding.value_selection_ids
                                                   or finding.selection_ids)
                                       if receipt.selections[sid].get("exact_text"))
                                 if profile.unified_expression else (),
                    external_citation=bool(words.get("external_citation")),
                    selection_ids=finding.selection_ids,
                    explanation=finding.explanation,
                    ruling_explanation=(
                        ruling_explanation(rulings[finding.finding_id], finding)
                        if finding.finding_id in rulings
                        else None
                    ),
                    extent="WHOLE_OWNERS" if finding.scope == "WHOLE" else "WITHIN_OWNER",
                    stage_key=finding.segment_key if finding.scope == "SEGMENT" else None,
                    existing_stage_id=finding.existing_stage_id,
                    relation=finding.relation,
                    primary=rulings[finding.finding_id].primary
                    if finding.finding_id in rulings
                    else None,
                )
            )
        elif aspect in (NAME_ASPECT, "assertion"):
            if not profile.preservation:
                continue
            name = next((r for r in answer.names if r.finding == finding.finding_id), None)
            if aspect == NAME_ASPECT and name is None and not profile.unified_expression:
                continue
            refs = _refs(receipt, finding.value_selection_ids or finding.selection_ids)
            targets = (
                finding.about_ids if profile.unified_expression
                else (name.compound_id,) if name is not None else finding.about_ids
            )
            for compound_id in targets:
                target_ruling = name if name is not None and name.compound_id == compound_id else None
                interpretations = compiled_interpretations(
                    finding, _refs(receipt, finding.selection_ids), rulings.get(finding.finding_id),
                    name_ruling=target_ruling,
                )
                contributed("compound_expressions", len(compound_expressions), (finding,))
                compound_expressions.append(
                    CompiledCompoundExpression(
                        compound_id=compound_id,
                        source_statement=source_statement(
                            str(finding.words), refs, interpretations
                        ),
                        adopted_names=(str(finding.words),)
                        if target_ruling is not None and target_ruling.relation == "SUPPLEMENTS"
                        else (),
                        selection_ids=finding.selection_ids,
                    )
                )
        elif aspect == "nothing_to_record":
            continue
        else:
            # A field the object offers, other than a condition's.
            (target_id,) = finding.about_ids
            words = finding.words
            if aspect == "operation_intermediate":
                if profile.unified_expression:
                    from tsdaf.lanes.literature.unified_reading import compile_intermediate
                    contributed("local_intermediates", len(local_intermediates), (finding,))
                    local_intermediates.append(compile_intermediate(
                        finding, receipt, machine, creation_keys
                    ))
                    continue
                words = {
                    "local_intermediate_id": words._legacy_identity
                    or "intermediate:"
                    + content_hash([receipt.execution_id, finding.item_id or finding.finding_id]),
                    **{field: getattr(words, name) for name, field in INTERMEDIATE_FIELDS.items()},
                    "structure": project_structure(
                        words.structure,
                        _refs(receipt, finding.value_selection_ids or finding.selection_ids),
                        words.structure_format,
                    ),
                }
            elif aspect == "product_compound_ids":
                words = list(words.product_ids)
            patch(finding, target_id, aspect, words)

    narratives = (*answer.narratives, *(other_experiment_narratives(answer, receipt)
                                       if profile.unified_expression else ()))
    for index, narrative in enumerate(narratives):
        source_finding = getattr(narrative, "source_finding", None)
        if source_finding is not None:
            contributions.setdefault(source_finding, []).append(("narratives", index))
    for finding in answer.findings:
        if finding.finding_id in contributions or finding.aspect == "nothing_to_record":
            continue
        if finding.aspect == "yield":
            reason = (
                "No public measurement contribution was compiled. "
                + finding_write_model(expression=profile.expression, open_expression=profile.open_expression, unified_expression=True).publication_note("yield", preservation=profile.preservation)
                if profile.unified_expression else
                "No public measurement contribution was compiled. " + YIELD_PUBLICATION_PREREQUISITE
            )
        elif finding.aspect == "name":
            reason = (
                "No public name contribution was compiled: name an existing compound "
                "in about_ids to preserve the full source expression."
                if profile.unified_expression else
                "No public name contribution was compiled. v1.4 requires an explicit "
                "compound owner and NameRuling; older profiles retain names only in the reading."
            )
        else:
            reason = (
                "No public contribution was compiled for this finding in this profile and scope. "
                "Its complete submitted reading is retained."
            )
        uncompiled[finding.finding_id] = reason
    stages = [
        OperationStageAddition(
            operation_id=segment.operation_id,
            stage_key=segment.segment_key,
            stage_verbatim=segment.begins_with,
            selection_ids=segment.selection_ids,
            explanation=segment.explanation,
        )
        for segment in answer.segments
    ]
    return CompiledReadingAnswer(
        measurements=tuple(measurements),
        local_intermediates=tuple(local_intermediates),
        narratives=tuple(narratives),
        compound_expressions=tuple(compound_expressions),
        finding_contributions={key: tuple(value) for key, value in contributions.items()},
        finding_target_limits={key: tuple(value) for key, value in target_limits.items()},
        uncompiled_findings=uncompiled,
        status=answer.status,
        explanation=answer.explanation,
        patches=tuple(patches),
        new_stages=tuple(stages),
        procedure_elisions=tuple(elisions),
        new_conditions=tuple(conditions),
        new_compounds=tuple(compounds),
        characterizations=tuple(characterizations),
        limitations=tuple(limitations),
    )


def _combined(items: list[Finding]) -> Finding:
    return items[0].model_copy(
        update={
            "selection_ids": tuple(dict.fromkeys(s for f in items for s in f.selection_ids)),
            "value_selection_ids": tuple(
                dict.fromkeys(s for f in items for s in (f.value_selection_ids or f.selection_ids))
            ),
            "explanation": " ".join(dict.fromkeys(f.explanation for f in items)),
        }
    )


def skeleton_materials(
    machine: Mapping[str, Any], bindings: Mapping[str, str | None] | None = None
) -> dict[str, dict[str, Any]]:
    """Task selectors resolve through accepted identity changes, never current row order."""
    return bound_rows(machine, bindings)


def paragraph_collections() -> dict[str, tuple[type[ContractModel], tuple[str, ...]]]:
    return keyed(
        ("paragraphs", StepParagraph),
        ("coverage", InvestigationCoverage),
        ("limitations", InvestigationLimitation),
    )


if TYPE_CHECKING:
    QuotedReadingWords = Any
else:
    QuotedReadingWords = reduce(
        or_,
        dict.fromkeys(
            QuotedMaterialWords
            if kind.words is MaterialWords
            else str
            if kind.compiles_to == "condition"
            else kind.words
            for kind in READING_KINDS.values()
        ),
    )


class QuotedFinding(Finding):
    allow_unresolved_procedure: ClassVar[bool] = False
    """Finding declaration for profiles without field-bound source selections."""

    open_sources: ClassVar[bool] = False
    scope: KnownScope | None = Field(
        default=None, description=Finding.model_fields["scope"].description
    )
    words: QuotedReadingWords = Field(description=Finding.model_fields["words"].description)
    sequence: tuple[ConditionSequenceMember, ...] = Field(
        default=(), description=Finding.model_fields["sequence"].description
    )


class OpenFinding(Finding):
    allow_unresolved_procedure: ClassVar[bool] = False
    """Forward input: amount source selections also provide their mechanical citations."""

    new_condition_scope: ClassVar[Scope] = "UNRESOLVED"
    scope: Scope | None = Field(
        default=None,
        description=Finding.model_fields["scope"].description
        + " For a new condition, omitted scope means UNRESOLVED, never WHOLE. "
        "When updating the same saved condition, omission retains its scope and stage. "
        "State a scope explicitly to change it; null is not a scope-clearing operation.",
    )

    sequence: tuple[ReadingConditionSequenceMember, ...] = Field(
        default=(),
        description="Explicitly ordered condition members, any number. Each uses "
        "text or source_parts. When every member supplies its source, parent words may be "
        "omitted: the host retains their separate source fragments, without joining them.",
    )
    shared_unit_text: str | None = Field(
        default=None,
        min_length=1,
        description="Optional author-stated unit shared by sequence members; it must occur "
        "in the verified parent or member sources.",
    )
    selection_ids: tuple[str, ...] = Field(
        default=(),
        description="Additional sources supporting ownership or continuous text; "
        "source selectors supply their own citations automatically. " + SOURCE_SELECTION_MEANING,
    )

    @model_validator(mode="after")
    def requires_citations(self) -> OpenFinding:
        if self.words is None and "words" in self.model_fields_set:
            raise ValueError(
                "omit words for a sourced condition sequence; explicit null is invalid"
            )
        object.__setattr__(
            self,
            "selection_ids",
            tuple(
                dict.fromkeys(
                    (
                        *self.selection_ids,
                        *(choice.selection_id for _, choice in finding_source_selections(self)),
                    )
                )
            ),
        )
        if not self.selection_ids:
            raise ValueError("a finding needs source citations or source selections")
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core: Any, handler: Any) -> dict[str, Any]:
        schema = super().__get_pydantic_json_schema__(core, handler)
        for branch in schema["oneOf"]:
            if branch["properties"]["aspect"]["const"] == "material":
                branch.setdefault("allOf", []).append(
                    {
                        "anyOf": [
                            {
                                "required": ["selection_ids"],
                                "properties": {"selection_ids": {"minItems": 1}},
                            },
                            {
                                "properties": {
                                    "words": {
                                        "required": ["amount"],
                                        "properties": {"amount": {"type": "array", "minItems": 1}},
                                    }
                                }
                            },
                        ]
                    }
                )
            elif branch["properties"]["aspect"]["const"] == "condition":
                branch.setdefault("allOf", []).append(
                    {
                        "anyOf": [
                            {
                                "required": ["selection_ids"],
                                "properties": {"selection_ids": {"minItems": 1}},
                            },
                            {"required": ["words"], "properties": {"words": {"type": "object"}}},
                            {
                                "required": ["sequence"],
                                "properties": {
                                    "sequence": {
                                        "contains": {
                                            "required": ["source_parts"],
                                            "properties": {
                                                "source_parts": {"type": "array", "minItems": 1}
                                            },
                                        }
                                    }
                                },
                            },
                        ]
                    }
                )
            else:
                branch["required"] = [*branch["required"], "selection_ids"]
                branch["properties"]["selection_ids"]["minItems"] = 1
        return schema


class UnifiedElisionWords(ElisionWords):
    """The candidate profile can choose a complete existing source location."""

    allow_method_location: ClassVar[bool] = True
    local_edit_fixed_fields: ClassVar[frozenset[str]] = (
        ElisionWords.local_edit_fixed_fields | {"referenced_method_location"}
    )
    referenced_method_location: str | None = Field(
        default=None, min_length=1,
        description=(
            "Existing source location of the intended GENERAL_METHODS unit, from the source directory."
        ),
    )


class UnifiedMaterialWords(MaterialWords):
    """An unfamiliar role still belongs to the same material use."""

    role: str = Field(min_length=1, description=material_role_guidance(open_roles=True))


class KnownOperationRelations(ContractModel):
    """Known links are independent of whether the list is scientifically complete."""

    known_operation_ids: tuple[str, ...] = ()
    completeness: Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"] = "UNKNOWN"


class UnifiedIntermediateWords(IntermediateWords):
    produced_by: KnownOperationRelations | None = Field(
        default=None,
        description="Known producing operations. Omit to use the operation explicitly named in about_ids.",
    )
    consumed_by: KnownOperationRelations = Field(
        default_factory=KnownOperationRelations,
        description="Known consuming operations, possibly none; operation order does not infer a consumer.",
    )


_UNIFIED_WORD_MODELS = {
    YieldWords: UnifiedYieldWords,
    MaterialWords: UnifiedMaterialWords,
    ElisionWords: UnifiedElisionWords,
    IntermediateWords: UnifiedIntermediateWords,
}


if TYPE_CHECKING:
    UnifiedReadingWords = Any
else:
    UnifiedReadingWords = reduce(or_, dict.fromkeys(
        _UNIFIED_WORD_MODELS.get(kind.words, kind.words)
        for kind in READING_KINDS.values()
    ))


class CandidateFinding(OpenFinding):
    """Unified input, unavailable to production until the entire profile is enabled."""
    # Host-only reference to the scientific view selected at a real request.
    # Never a provider field or an additional scientific contribution.
    _measurement_binding: dict[str, Any] | None = PrivateAttr(default=None)
    allow_unresolved_procedure: ClassVar[bool] = True
    unified_measurements: ClassVar[bool] = True
    words: UnifiedReadingWords | None = Field(default=None, description=Finding.model_fields["words"].description)

    @classmethod
    def word_model(cls, kind: ReadingKind) -> Any:
        return _UNIFIED_WORD_MODELS.get(kind.words, super().word_model(kind))


EXPLICIT_CURRENT_UPDATE = "EXPLICIT_CURRENT_UPDATE_V1"


def explicit_current_updates(task_input: Mapping[str, Any]) -> bool:
    """Only the sealed task capability selects the answer and write semantics."""
    mode = task_input.get("finding_update_mode", "LEGACY_V1")
    if mode not in ("LEGACY_V1", EXPLICIT_CURRENT_UPDATE):
        raise ValueError("unknown sealed finding update mode")
    return mode == EXPLICIT_CURRENT_UPDATE


class CurrentMaterialWords(UnifiedMaterialWords):
    role: str | None = Field(default=None, min_length=1,
        description="Omit for a selected existing material; otherwise state its role. "
                    + material_role_guidance(open_roles=True))
    verbatim_name: str | None = Field(default=None, min_length=1,
        description="Name reported by this source; omit when only adding an amount to a selected material.")


if TYPE_CHECKING:
    CurrentReadingWords = Any
else:
    CurrentReadingWords = reduce(or_, dict.fromkeys(
        CurrentMaterialWords if kind.words is MaterialWords
        else _UNIFIED_WORD_MODELS.get(kind.words, kind.words)
        for kind in READING_KINDS.values()
    ))


class CandidateFindingV2(CandidateFinding):
    """Record an interpretation or explicitly update one selected current result."""

    local_edit_fields: ClassVar[frozenset[str]] = Finding.local_edit_fields | {"intent"}
    intent: Literal["RECORD", "UPDATE_CURRENT"] | None = Field(default=None,
        description="RECORD keeps existing primary values and adds your report. UPDATE_CURRENT "
                    "changes the explicitly selected current result. Omit for a new record or "
                    "to retain the intent of the same saved item and target.")
    finding_id: str = Field(min_length=1, description="Your stable name for this finding.")
    relation: SourceRelation = Field(default="ADDS",
        description="How this report relates to the current result: SAME, ADDS or CONTRADICTS. "
                    "This relationship alone never selects a replacement primary value; use intent.")
    words: CurrentReadingWords | None = Field(default=None,
        description=Finding.model_fields["words"].description)

    @classmethod
    def word_model(cls, kind: ReadingKind) -> Any:
        return CurrentMaterialWords if kind.words is MaterialWords else super().word_model(kind)

    @model_validator(mode="after")
    def material_identity_or_target(self):
        if self.aspect == "material" and self.skeleton_material_id is None:
            if self.words.role is None or self.words.verbatim_name is None:
                raise ValueError("a new material needs role and verbatim_name; amount-only selects skeleton_material_id")
        if self.intent is not None and self.aspect not in {"condition", "material", "characterization", "yield"}:
            raise ValueError("intent applies to condition, material, characterization or yield")
        return self

    @classmethod
    def __get_pydantic_json_schema__(cls, core, handler):
        schema = super().__get_pydantic_json_schema__(core, handler)
        for branch in schema["oneOf"]:
            props = branch["properties"]
            aspect = props["aspect"]["const"]
            if aspect not in {"condition", "material", "characterization", "yield"}:
                props["intent"] = {"type": "null", "default": None}
            if aspect == "material":
                branch.setdefault("allOf", []).append({
                    "if": {"not": {"required": ["skeleton_material_id"],
                                   "properties": {"skeleton_material_id": {"type": "string"}}}},
                    "then": {"properties": {"words": {"required": ["role", "verbatim_name"],
                        "properties": {"role": {"type": "string"}, "verbatim_name": {"type": "string"}}}}}
                })
            if aspect in {"material", "characterization", "yield"}:
                original = deepcopy(props["about_ids"])
                props["about_ids"].pop("minItems", None)
                branch["required"] = [k for k in branch["required"] if k != "about_ids"]
                target = ({"required": ["skeleton_material_id"],
                           "properties": {"skeleton_material_id": {"type": "string"}}}
                          if aspect == "material" else
                          {"required": ["words"], "properties": {"words": {
                              "required": ["same_measurement_as"],
                              "properties": {"same_measurement_as": {"type": "string"}}}}}
                          if aspect == "characterization" else
                          {"required": ["words"], "properties": {"words": {
                              "required": ["measurement"], "properties": {"measurement": {
                                  "required": ["same_as"], "properties": {"same_as": {"type": "string"}}}}}}})
                branch.setdefault("allOf", []).append({"anyOf": [target,
                    {"properties": {"about_ids": original},
                     **({"required": ["about_ids"]} if original.get("minItems", 0) else {})}]})
        return schema


class UnifiedReadingAnswer(ReadingAnswer):
    """Host-selected unified answer; old sealed answers keep their original adapter."""
    skeleton_repairs: tuple[UnifiedSkeletonRepair, ...] = ()
    findings: tuple[CandidateFinding, ...] = ()
    narratives: tuple[UnifiedNarrative, ...] = ()
    # Read-only compatibility for sealed candidate research answers. The live
    # collection registry no longer offers this withdrawn producer. Keep even
    # the empty field so the existing sealed-answer discriminator remains valid.
    new_experiments: tuple[NewExperiment, ...] = ()
    same_measurements: tuple[SameMeasurement, ...] = Field(default=(), max_length=0)


class UnifiedReadingAnswerV2(UnifiedReadingAnswer):
    """Host-tagged answer; no marker or second primary table in the write tool."""

    reading_answer_profile: Literal["EXPLICIT_CURRENT_UPDATE_V1"] = EXPLICIT_CURRENT_UPDATE
    findings: tuple[CandidateFindingV2, ...] = ()
    conflicts: tuple[ConflictRuling, ...] = Field(default=(), max_length=0)


def verify_reading_update_mode(answer: ReadingAnswer, task_input: Mapping[str, Any]) -> None:
    if isinstance(answer, UnifiedReadingAnswerV2) != explicit_current_updates(task_input):
        raise ValueError("reading answer profile differs from its sealed task capability")


def _require_active_reading_capabilities(answer: ReadingAnswer) -> None:
    """Do not silently recompile a historical answer with a withdrawn producer."""
    if getattr(answer, "new_experiments", ()):
        raise ValueError(
            "Reading new_experiments creation has been withdrawn; this historical "
            "answer remains readable but requires its archived runtime for replay."
        )


class LegacyFinding(QuotedFinding):
    """A legacy public profile cannot publish expression relations."""

    sequence: tuple[ConditionSequenceMember, ...] = Field(default=(), max_length=0)
    shared_unit_text: None = None
    existing_stage_id: None = None
    reporting_level: None = None
    detail_of: None = None
    clear_summary: Literal[False] = False
    scope: KnownScope = "WHOLE"


def finding_write_model(
    *, expression: bool, open_expression: bool = False, unified_expression: bool = False,
    current_updates: bool = False,
) -> type[Finding]:
    if current_updates:
        if not unified_expression:
            raise ValueError("current updates require the unified profile")
        return CandidateFindingV2
    if unified_expression:
        return CandidateFinding
    return OpenFinding if open_expression else QuotedFinding if expression else LegacyFinding


def inherit_finding_scope(item: Finding, previous: Finding, writer: type[Finding]) -> Finding:
    """A saved forward condition keeps omitted scope/anchors, not guessed ownership."""
    if (
        writer.new_condition_scope != "UNRESOLVED"
        or item.aspect != "condition"
        or previous.aspect != "condition"
    ):
        return item
    updates = {}
    scope = item.scope if "scope" in item.model_fields_set else previous.scope
    if "scope" not in item.model_fields_set:
        updates["scope"] = scope
    if scope == previous.scope == "SEGMENT":
        if not ({"segment_key", "existing_stage_id"} & item.model_fields_set):
            updates.update(
                segment_key=previous.segment_key, existing_stage_id=previous.existing_stage_id
            )
    return item.model_copy(update=updates) if updates else item


def reading_collections(
    *, unified_expression: bool = False, current_updates: bool = False
) -> dict[str, tuple[type[ContractModel], tuple[str, ...]]]:
    return keyed(
        ("skeleton_repairs", UnifiedSkeletonRepair if unified_expression else SkeletonRepair),
        ("segments", Segment),
        ("findings", CandidateFindingV2 if current_updates else CandidateFinding if unified_expression else Finding),
        ("narratives", UnifiedNarrative if unified_expression else SupplementalNarrative),
        *(() if current_updates else (("conflicts", ConflictRuling),)),
        *(() if unified_expression else (("same_measurements", SameMeasurement),)),
        ("names", NameRuling),
        ("two_transformations", TwoTransformations),
    )


@dataclass(frozen=True)
class SelectionsHeld:
    selections: Mapping[str, Mapping[str, Any]]


class QuotedField(NamedTuple):
    path: str
    text: str
    material_name: bool = False


def _quotes(finding: Finding) -> tuple[QuotedField, ...]:
    """Quoted fields retain their meaning through validation and evidence binding."""
    kind = READING_KINDS[finding.aspect]
    values = (
        [("words", finding.words, False)]
        if isinstance(finding.words, str)
        else [
            (f"words.{key}", getattr(finding.words, key), _is_material_name(finding, key))
            for key in kind.quoted
        ]
    )
    for index, part in enumerate(finding.sequence):
        values.append((f"sequence[{index}].text", part.text, False))
        values.extend(
            (f"sequence[{index}].values[{j}].text", value.text, False)
            for j, value in enumerate(part.values)
        )
    if finding.shared_unit_text:
        values.append(("shared_unit_text", finding.shared_unit_text, False))
    return tuple(
        QuotedField(f"{path}[{i}]" if isinstance(value, tuple) else path, text, name)
        for path, value, name in values
        for i, text in enumerate(value if isinstance(value, tuple) else (value,))
        if isinstance(text, str)
    )


def _is_material_name(finding: Finding, key: str) -> bool:
    return finding.aspect == "material" and key == "verbatim_name"


def _diagnostic_item_label(label: str) -> str:
    """Display context, never a new identity or a rewritten submitted name."""
    if len(label) <= 180:
        return label
    return label[:100] + "…" + label[-60:] + " (label abbreviated)"


def _quote_source_entry(writer: type[Finding], aspect: str, path: str) -> str:
    entries = source_selection_entries(writer, aspect)
    normalized = re.sub(r"\[\d+\]", "[*]", path)
    alternative = normalized.removesuffix(".text") + ".source_parts"
    return entries.get(path, entries.get(alternative, ""))


def _quoted(
    words: str,
    ids: tuple[str, ...],
    receipt: HoldsSelections,
    *,
    field_path: str,
    item_label: str,
    material_name: bool = False,
    alternative_entry: str = "",
) -> tuple[str, ...]:
    """The selections the author's `words` stand in, on the rule every quote is held
    to (`find_quote`): bounded, and across the two halves of a paragraph the layout cut."""
    missing = set(ids) - receipt.selections.keys()
    if missing:
        raise UnknownReference(
            "selection_ids",
            sorted(missing)[0],
            sorted(receipt.selections),
            f"{item_label} / {field_path} cites an undelivered selection",
        )
    found = find_quote(words, ids, receipt.selections, material_name=material_name)
    if found is None:
        # These are already delivered, authorized sources, not a guessed match.
        # Budget complete arguments before the shared diagnostic renderer truncates.
        # An arbitrary user-given label must not crowd the field or READ out.
        shown_label = _diagnostic_item_label(item_label)
        prefix = (
            f"{shown_label} / {field_path}: words are not verbatim "
            "in the cited delivered paragraphs. "
            "This quoted field must occur continuously under the existing source rule; "
            "this proposed item was not applied. "
            + (f"This field also supports: {alternative_entry} " if alternative_entry else "")
            + "To inspect its cited sources, read "
        )
        candidates = []
        candidate_prefix = (
            f"{shown_label} / {field_path}: words are not verbatim in the current citations. "
            "Exact wording occurs in these already delivered sources. Choose the source "
            "only if its context supports your attribution, then cite its selection_id; "
            "nothing was applied or automatically attributed. Candidates: "
        )
        for sid, binding in receipt.selections.items():
            if sid in ids or not isinstance(binding.get("exact_text"), str):
                continue
            if find_quote(words, (sid,), {sid: binding}, material_name=material_name) is None:
                continue
            for match in quoted_source_candidates(
                binding["exact_text"], words, material_name=material_name
            ):
                candidate = {"selection_id": sid, "location": "selection:" + sid, **match}
                trial = [*candidates, candidate]
                if (
                    len(candidate_prefix + json.dumps(trial, ensure_ascii=False))
                    <= DIAGNOSTIC_CHARS
                ):
                    candidates = trial
                if len(candidates) >= 4:
                    break
            if len(candidates) >= 4:
                break
        if candidates:
            raise ValueError(candidate_prefix + json.dumps(candidates, ensure_ascii=False))
        suffix = ". Other cited sources remain listed on this item."
        entries = []
        for sid in dict.fromkeys(ids):
            candidate = [*entries, {"location": "selection:" + sid}]
            if len(prefix + json.dumps({"items": candidate}, ensure_ascii=False) + suffix) <= (
                DIAGNOSTIC_CHARS
            ):
                entries = candidate
            if len(entries) == 4:
                break
        if not entries:
            raise ValueError(
                prefix + "this item's original cited locations (too long to repeat here)."
            )
        raise ValueError(
            prefix
            + json.dumps({"items": entries}, ensure_ascii=False)
            + (suffix if len(set(ids)) > len(entries) else ".")
        )
    return found.selection_ids


def _bound_to_their_words(answer: ReadingAnswer, receipt: HoldsSelections) -> ReadingAnswer:
    """Each stretch and finding cites what it cited and the other half of a cut
    paragraph its words run into; never fewer. The one place evidence follows a quote
    across a cut: what compiles from the answer -- its selections, its primary
    selections, a stretch's boundary -- reads the widened citation."""

    def spanned(texts: tuple[QuotedField, ...], ids: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            sid
            for quote in texts
            if (
                found := find_quote(
                    quote.text, ids, receipt.selections, material_name=quote.material_name
                )
            )
            is not None
            for sid in found.selection_ids
        )

    def widened(ids: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*ids, *extra)))

    segments = tuple(
        segment.model_copy(
            update={
                "selection_ids": widened(
                    segment.selection_ids,
                    spanned(
                        (QuotedField("begins_with", segment.begins_with),), segment.selection_ids
                    ),
                )
            }
        )
        for segment in answer.segments
    )
    findings = []
    for finding in answer.findings:
        extra = spanned(_quotes(finding), finding.value_selection_ids or finding.selection_ids)
        extra = widened(
            extra, tuple(choice.selection_id for _, choice in finding_source_selections(finding))
        )
        findings.append(
            finding.model_copy(
                update={
                    "selection_ids": widened(finding.selection_ids, extra),
                    "value_selection_ids": widened(finding.value_selection_ids, extra)
                    if finding.value_selection_ids
                    else tuple(dict.fromkeys(extra)),
                }
            )
        )
    return answer.model_copy(update={"segments": segments, "findings": tuple(findings)})


def _known_subjects(machine: Mapping[str, Any]) -> set[str]:
    if not machine:
        return set()
    return set(_containers(machine)) | {
        c["compound_id"] for c in machine["domain_payload"]["compounds"]
    }


def validate_paragraphs(
    answer: ParagraphSet, receipt: HoldsSelections, machine: Mapping[str, Any]
) -> None:
    known = _known_subjects(machine)
    for paragraph in answer.paragraphs:
        if unknown := sorted(set(paragraph.selection_ids) - receipt.selections.keys()):
            raise UnknownReference(
                "selection_ids",
                unknown[0],
                sorted(receipt.selections),
                "paragraph cites an undelivered selection",
            )
        if unknown := sorted(set(paragraph.about_ids) - known):
            raise UnknownReference(
                "about_ids", unknown[0], sorted(known), "paragraph names an unknown object"
            )


def validate_reading(
    answer: ReadingAnswer,
    receipt: HoldsSelections,
    machine: Mapping[str, Any],
    fresh: Callable[[ContractModel], bool] = lambda item: True,
    *,
    material_bindings: Mapping[str, str | None] | None = None,
    general_methods_units: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    method_targets: Mapping[str, Mapping[str, str]] | None = None,
    measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
    material_check: Callable[..., None] | None = None,
) -> None:
    """The one context rule used by both declaration checks and write acceptance.

    An item's own checks -- its words verbatim, its selections delivered, the objects it
    names -- depend on the item, the task's fixed baseline and the text of selections
    that are never rebound, so an item that passed them passes them still; they run for
    the items ``fresh`` names (by default all). The checks that relate items -- a
    stretch a finding cites, a finding a ruling names -- run over the whole answer every
    time, in the same order. Writing each item used to re-check every item saved before
    it, O(N^2) comparisons of each quote against its paragraph (batch 4, 2026-09-24).
    """
    from tsdaf.contracts.literature.objects import experiment_containers, reported_outcomes
    from tsdaf.lanes.literature.finding_updates import resolve_answer, resolved_intent
    answer = resolve_answer(answer, measurement_machines)
    _require_active_reading_capabilities(answer)
    machine = reading_compound_view(answer, receipt, machine)
    operations = {o["operation_id"] for c in experiment_containers(machine) for o in c["operations"]}
    compounds = {c["compound_id"] for c in machine["domain_payload"]["compounds"]}
    blocks = {c.get("reaction_block_id", c.get("account_id")) for c in experiment_containers(machine)}
    outcomes = {o["outcome_id"] for o in reported_outcomes(machine)}
    ids_of: dict[RouteObject, set[str]] = {
        "reaction block": blocks,
        "operation": operations,
        "measurement": outcomes,
        "compound": compounds,
    }
    segments = set()
    for segment in answer.segments:
        if fresh(segment):
            if segment.operation_id not in operations:
                raise UnknownReference(
                    "operation_id",
                    segment.operation_id,
                    sorted(operations),
                    "segment names an unknown operation",
                )
            _quoted(
                segment.begins_with,
                segment.selection_ids,
                receipt,
                field_path="begins_with",
                item_label=f"segment {segment.segment_key!r} of {segment.operation_id}",
            )
        segments.add((segment.operation_id, segment.segment_key))
    materials = skeleton_materials(machine, material_bindings)
    for finding in answer.findings:
        item_label = _diagnostic_item_label(f"finding {finding.finding_id!r}")
        new = fresh(finding)
        if new:
            kind = READING_KINDS[finding.aspect]
            about = kind.about
            strangers = [
                i for i in finding.about_ids if not any(i in ids_of[name] for name in about.names)
            ]
            count = len(finding.about_ids)
            if strangers:
                allowed = sorted(set().union(*(ids_of[name] for name in about.names)))
                raise UnknownReference(
                    "about_ids",
                    strangers[0],
                    allowed,
                    f"about_ids of a {finding.aspect} finding name {about.text()}"
                    + "; not "
                    + ", ".join(strangers),
                )
            if count < about.least or (about.most is not None and count > about.most):
                raise ValueError(f"about_ids of a {finding.aspect} finding name {about.text()}")
            allowed_scopes = finding_write_model(
                expression=machine_profile(machine).expression,
                open_expression=machine_profile(machine).open_expression,
        unified_expression=machine_profile(machine).unified_expression,
            ).scope_options(finding.aspect)
            if finding.scope not in allowed_scopes and not (
                finding.aspect == "material" and finding.scope is None
            ):
                raise ValueError(
                    f"scope of a {finding.aspect} finding is one of {', '.join(allowed_scopes)}; "
                    "only a finding about an operation lies in a stretch of it"
                )
            if unknown := sorted(set(finding.selection_ids) - receipt.selections.keys()):
                raise UnknownReference(
                    "selection_ids",
                    unknown[0],
                    sorted(receipt.selections),
                    "finding cites an undelivered paragraph",
                )
            if not set(finding.value_selection_ids) <= set(finding.selection_ids):
                raise ValueError("value selections must be among the cited selections")
            choices = finding_source_selections(finding)
            if choices and not machine_profile(machine).open_expression:
                raise ValueError("source selections require the v1.5 profile")
            for path, choice in choices:
                if choice.selection_id not in finding.selection_ids:
                    raise ValueError(path + " sources must belong to this finding's citations")
                selected_source_part(choice, receipt.selections, field_path=path)
            for quote in _quotes(finding):
                _quoted(
                    quote.text,
                    finding.value_selection_ids or finding.selection_ids,
                    receipt,
                    field_path=quote.path,
                    item_label=item_label,
                    material_name=quote.material_name,
                    alternative_entry=_quote_source_entry(
                        finding_write_model(
                            expression=machine_profile(machine).expression,
                            open_expression=machine_profile(machine).open_expression,
        unified_expression=machine_profile(machine).unified_expression,
                        ),
                        finding.aspect,
                        quote.path,
                    ),
                )
        if finding.clear_summary and (
            finding.detail_of is not None or finding.skeleton_material_id is None
        ):
            raise ValueError("clear_summary requires a selected existing material and no detail_of")
        if (
            finding.reporting_level is not None
            or finding.detail_of is not None
            or finding.clear_summary
        ):
            if finding.aspect != "material" or not machine_profile(machine).expression:
                raise ValueError(
                    "material reporting relations require an expression-profile material"
                )
        if (
            finding.aspect == "material"
            and finding.scope in STRETCHES
            and not machine_profile(machine).expression
        ):
            raise ValueError("material stage/scope relations require the v1.3 expression profile")
        if finding.detail_of is not None:
            targets = [
                f
                for f in answer.findings
                if f.finding_id == finding.detail_of
                and f.aspect == "material"
                and f.about_ids == finding.about_ids
            ]
            existing = materials.get(finding.detail_of)
            if (
                not targets
                and (existing is None or existing["operation_id"] not in finding.about_ids)
            ) or finding.detail_of == finding.finding_id:
                raise UnknownReference(
                    "detail_of",
                    finding.detail_of,
                    [
                        f.finding_id
                        for f in answer.findings
                        if f.aspect == "material" and f.about_ids == finding.about_ids
                    ]
                    + [
                        key
                        for key, row in materials.items()
                        if row["operation_id"] in finding.about_ids
                    ],
                    "detail_of must name another material in this operation",
                )
            if finding.reporting_level == "TOTAL" or any(
                t.reporting_level == "USE" or t.detail_of is not None for t in targets
            ):
                raise ValueError("detail_of links USE to TOTAL, without nested summaries")
        if finding.sequence or finding.shared_unit_text:
            validate_condition_sequence(finding, receipt, machine, item_label)
        if (
            machine_profile(machine).open_expression
            and finding.aspect == "condition"
            and finding.segment_key is not None
            and finding.scope != "SEGMENT"
        ):
            raise ValueError("segment_key requires SEGMENT scope for a condition finding")
        if finding.existing_stage_id is not None:
            if (
                not READING_KINDS[finding.aspect].accepts_stage
                or finding.scope != "SEGMENT"
                or finding.segment_key is not None
            ):
                raise ValueError(
                    "existing_stage_id requires a supported SEGMENT finding without segment_key"
                )
            allowed_stages = {
                stage["stage_id"]
                for block in experiment_containers(machine)
                for op in block["operations"]
                if op["operation_id"] in finding.about_ids
                for stage in op.get("stages", [])
            }
            if finding.existing_stage_id not in allowed_stages:
                raise UnknownReference(
                    "existing_stage_id",
                    finding.existing_stage_id,
                    sorted(allowed_stages),
                    "existing stage must belong to the named operation",
                )
        elif finding.scope == "SEGMENT" and not any(
            (i, finding.segment_key) in segments for i in finding.about_ids
        ):
            raise ValueError("finding cites an undeclared segment")
        if not new:
            continue
        if finding.skeleton_material_id is not None:
            target = materials.get(finding.skeleton_material_id)
            if target is None or target["operation_id"] not in finding.about_ids:
                raise UnknownReference(
                    "skeleton_material_id",
                    finding.skeleton_material_id,
                    sorted(
                        material_id
                        for material_id, material in materials.items()
                        if material["operation_id"] in finding.about_ids
                    ),
                    "unknown material selector for this operation",
                )
        if (
            finding.aspect == "material"
            and finding.words.compound_id is not None
            and finding.words.compound_id not in compounds
        ):
            raise UnknownReference(
                "compound_id",
                finding.words.compound_id,
                sorted(compounds),
                "material names an unknown compound",
            )
        if finding.block_id is not None and finding.block_id not in blocks:
            raise UnknownReference(
                "block_id",
                finding.block_id,
                sorted(blocks),
                "finding names an unknown block",
            )
        if finding.aspect == "yield" and isinstance(finding.words, UnifiedYieldWords):
            from tsdaf.lanes.literature.unified_reading import resolve_experiment_reference

            resolve_experiment_reference(
                finding.words.experiment,
                {
                    c.get("reaction_block_id", c.get("account_id")): c
                    for c in experiment_containers(machine)
                },
            )
        if finding.aspect in {"yield", "measurement_products"}:
            # Named, so the correction is one edit: a4 wrote a product id with its CMP-
            # prefix dropped and was told only that some compound was unknown. The Host
            # never mends an id itself.
            unknown = [i for i in dict.fromkeys(finding.words.product_ids) if i not in compounds]
            if unknown:
                raise UnknownReference(
                    "product_ids",
                    unknown[0],
                    sorted(compounds),
                    f"product_ids of a {finding.aspect} finding name no existing compound: "
                    + ", ".join(unknown),
                )
        if finding.aspect == "intermediate":
            if isinstance(finding.words, UnifiedIntermediateWords):
                from tsdaf.lanes.literature.unified_reading import resolve_intermediate_links

                resolve_intermediate_links(finding, machine)
            else:
                linked_ops = {finding.words.produced_by, finding.words.consumed_by}
                if linked_ops - operations:
                    raise ValueError("intermediate names an unknown operation")
        if finding.aspect == "elision":
            target_kind = procedure_target_kind(
                finding.words.model_dump(),
                allow_unresolved=machine_profile(machine).unified_expression,
                allow_location=machine_profile(machine).unified_expression,
            )
            if target_kind == "OPERATION":
                target = finding.words.referenced_operation_id
                if target not in operations or target in finding.about_ids:
                    raise UnknownReference(
                        "referenced_operation_id",
                        target,
                        sorted(operations - set(finding.about_ids)),
                        "referenced_operation_id must name another existing operation",
                    )
            elif target_kind == "GENERAL_METHODS_UNIT":
                resolve_method_target(
                    finding.words.model_dump(), method_targets=method_targets or {},
                    cited_revisions={str(receipt.selections[sid]["source_identity"]["document_revision_id"])
                                     for sid in finding.selection_ids},
                    legacy_units=general_methods_units,
                    require_document=machine_profile(machine).unified_expression,
                )
        if finding.aspect == "characterization":
            validate_characterization_statement(
                finding.words.statement, profile=machine_profile(machine)
            )
            if finding.words.same_measurement_as is not None:
                cards = {
                    c["characterization_id"]: c
                    for c in (measurement_machines or {}).get(finding.item_id, machine)["domain_payload"]["characterization_records"]
                }
                fields = characterization_fields(
                    finding.words.statement, profile=machine_profile(machine)
                )
                if len(fields) != 1:
                    raise ValueError(
                        "Link one measured result to one existing characterization card"
                    )
                matching_card(
                    cards,
                    {**fields[0], "compound_ids": finding.about_ids},
                    finding.words.same_measurement_as,
                    explicit_identity=isinstance(finding, CandidateFindingV2),
                )
    findings = {f.finding_id for f in answer.findings}
    linked: tuple[ConflictRuling | SameMeasurement | NameRuling, ...] = (
        *answer.conflicts,
        *answer.same_measurements,
        *answer.names,
    )
    for ruling in linked:
        if ruling.finding not in findings:
            raise ValueError("ruling names an unwritten finding")
    whole_condition_decisions(answer, machine, selections=receipt.selections)
    measurement_bindings(answer, machine, measurement_machines=measurement_machines)
    for name in answer.names:
        if name.compound_id not in compounds:
            raise ValueError("name ruling names an unknown compound")
        finding = next(f for f in answer.findings if f.finding_id == name.finding)
        if machine_profile(machine).preservation and (
            finding.aspect != "name" or name.compound_id not in finding.about_ids
        ):
            raise ValueError("name ruling must bind its name finding's explicit compound owner")
    for item in answer.two_transformations:
        if item.operation_id not in operations:
            raise ValueError("two-transformations record names an unknown operation")

    if machine_profile(machine).expression and any(f.aspect == "material" for f in answer.findings):
        material_findings = tuple(f for f in answer.findings if f.aspect == "material")
        names = {f.finding_id for f in material_findings}
        material_answer = type(answer)(
            status="PARTIAL",
            explanation="Validate the same material compilation before accepting.",
            findings=material_findings,
            conflicts=tuple(r for r in answer.conflicts if r.finding in names),
        )
        (material_check or validate_material_compilation)(
            material_answer,
            receipt,
            machine,
            material_bindings=material_bindings,
        )


def validate_material_compilation(material_answer, receipt, machine, *, material_bindings=None):
    """The same material compilation rule for direct checks and runtime reuse."""
    from tsdaf.contracts.literature.validation import validate_material_relations

    compiled = compile_reading(
        material_answer, cast(ReceiptEvidence, receipt), machine,
        material_bindings=material_bindings,
    )
    for patch in compiled.patches:
        if patch.field_key == "materials":
            operation = _containers(machine)[patch.target_id][1]
            stage_ids = {s["stage_id"] for s in operation.get("stages", [])}
            validate_material_relations(cast(list[dict[str, Any]], patch.value), stage_ids)


def _percentage(words: YieldWords) -> object:
    """The percentage a yield's phrase reports, as the host reads it; a phrase reporting
    none -- a mass, a ratio, a bound -- has none, whatever its kind."""
    value, _ = parse_reported_outcome(words.text)
    return value["numeric_value_decimal"] if value["normalized_unit"] == "%" else None


def yield_product_ids(finding: Finding, machine: Mapping[str, Any]) -> tuple[str, ...]:
    """Explicit product fields, or legacy compound-valued about_ids; never infer
    products from a block, operation, measurement, label or equal numeric value.
    Explicit product_ids is authoritative when about_ids describes wider context.
    """
    if finding.words.product_ids:
        return finding.words.product_ids
    compounds = {c["compound_id"] for c in machine["domain_payload"]["compounds"]}
    return tuple(dict.fromkeys(i for i in finding.about_ids if i in compounds))


def reading_candidates(
    answer: ReadingAnswer, machine: Mapping[str, Any], *,
    applied_outcomes: Mapping[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Arithmetic candidates from each yield's value phrase as the host reads it, never
    from parsed raw paragraphs."""
    yields = [f for f in answer.findings if f.aspect == "yield" and f.scope != "ANOTHER_EXPERIMENT"]
    measured = [
        (b, o) for b in machine["domain_payload"]["reaction_blocks"] for o in b["reported_outcomes"]
    ]
    products = {f.finding_id: yield_product_ids(f, machine) for f in yields}
    same = {}
    for f in yields:
        candidates = [
            o["outcome_id"]
            for _, o in measured
            if set(products[f.finding_id])
            & set((o.get("product_compound_ids") or {}).get("value") or ())
            and o["outcome_id"] not in (applied_outcomes or {}).get(f.finding_id, set())
        ]
        if candidates:
            same[f.finding_id] = candidates

    known_labels = {
        label
        for compound in machine["domain_payload"]["compounds"]
        for label in compound.get("author_labels", ())
    }
    combinations_found = []
    for block, outcome in measured:
        operation_ids = (outcome.get("operation_ids") or {}).get("value") or ()
        raw = (outcome.get("reported_value") or {}).get("value") or {}
        parsed, basis = parse_reported_outcome(raw.get("raw_text", ""))
        if (
            len(operation_ids) < 2
            and not (
                basis and {"step", "steps", "operations", "stages"} & set(basis.casefold().split())
            )
            and "overall" not in raw.get("raw_text", "").casefold().split()
        ):
            continue
        target = parsed.get("numeric_value_decimal") if parsed.get("unit") == "%" else None
        own = set((outcome.get("product_compound_ids") or {}).get("value") or ())
        if target is None or not own:
            continue
        targets = [
            f for f in yields if products[f.finding_id] and set(products[f.finding_id]) <= own
        ]
        external = [
            f
            for f in yields
            if not products[f.finding_id]
            and f.words.product_label
            and f.words.product_label not in known_labels
        ]
        values: dict[str, Any] = {f.finding_id: _percentage(f.words) for f in (*targets, *external)}
        external = [f for f in external if values[f.finding_id] is not None]
        safe_to_prune = all(0 <= float(values[f.finding_id]) <= 100 for f in external)
        target_value = float(str(target))

        pending: list[tuple[list[Finding], int, float, set[str]]] = [
            ([seed], 0, float(values[seed.finding_id]), set())
            for seed in reversed(targets)
            if values[seed.finding_id] is not None
        ]
        while pending:
            chosen, start, product, identities = pending.pop()
            if len(chosen) >= 2 and abs(product - target_value) <= 1:
                ids = {f.finding_id for f in chosen}
                combinations_found.append(
                    {
                        "outcome_id": outcome["outcome_id"],
                        "block_id": block["reaction_block_id"],
                        "findings": [f.finding_id for f in yields if f.finding_id in ids],
                        "product_percent": round(product, 6),
                    }
                )
            if safe_to_prune and product >= 0 and product < target_value - 1:
                continue
            for index in range(len(external) - 1, start - 1, -1):
                candidate = external[index]
                label = candidate.words.product_label
                if label not in identities:
                    pending.append(
                        (
                            chosen + [candidate],
                            index + 1,
                            product * float(values[candidate.finding_id]) / 100,
                            identities | {label},
                        )
                    )
    return {"same_product_measurements": same, "overall_yield_combinations": combinations_found}
