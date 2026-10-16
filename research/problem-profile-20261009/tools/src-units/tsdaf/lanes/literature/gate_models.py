"""One scientific result and a small, sparse proposal, shared by both fusion consumers.

No draft editor or historical execution model lives here. Historical sealed JSON remains
readable through the artifact store; only this version may enter new execution.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal, Self, get_args

from pydantic import Field, GetJsonSchemaHandler, JsonValue, model_validator
from pydantic.json_schema import JsonSchemaValue, SkipJsonSchema
from pydantic_core import CoreSchema

from tsdaf.contracts._base import ContractModel, _unique
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_declaration import provider_declaration_schema
from tsdaf.contracts.tool_delivery import WriteControl, WriteDeliveryControl
from tsdaf.contracts.tool_read import ReadRequest
from tsdaf.contracts.tool_write import write_request
from tsdaf.lanes.literature.author_labels import AUTHOR_LABELS_MEANING
from tsdaf.lanes.literature.enrichment_values import (
    CONDITION_KEY_GUIDANCE,
    MainExperimentAssessment,
)
from tsdaf.lanes.literature.item_identity import identity_sentence
from tsdaf.lanes.literature.material_meanings import MATERIAL_NAME_MEANING, MATERIAL_ROLE_GUIDANCE, material_role_guidance
from tsdaf.lanes.literature.measurement_intent import MeasurementIntent
from tsdaf.lanes.literature.outcome_meanings import OUTCOME_KIND_GUIDANCE, OUTCOME_VALUE_PHRASE
from tsdaf.lanes.literature.read_models import VisualReadBounds
from tsdaf.lanes.literature.source_scope import ReadingPlan
from tsdaf.lanes.literature.source_parts import SourceSelection

FactKind = Literal[
    "COMPOUND", "REACTION", "OPERATION", "MATERIAL", "CONDITION", "OUTCOME", "TARGET", "NOTE"
]
UnifiedFactKind = Literal[(*get_args(FactKind), "EXPERIMENT")]
FactStatus = Literal["ACCEPTED", "CONTEXT", "REJECTED", "UNRESOLVED"]
FactClearField = Literal[
    "text", "category", "labels", "owners", "inputs", "outputs", "after", "same_as", "rationale"
]
UnifiedFactClearField = Literal[(*get_args(FactClearField), "source_parts")]
# A question's status says where its answer stands; it hands no work to anyone. The two
# statuses that once did -- READ and INTEGRATE, a window scheduled after this task -- are
# retired (2026-09-23 ruling): the main text is this task's to write in full, and the
# experimental detail is the detail lane's, sent to every step of the route. This is the
# one source; the fusion prompt restates its conclusion and is pinned on its own side.
ACTION_MEANINGS = {
    "RESOLVED": "Stop this question with the actual result or evidenced conclusion supplied.",
    "UNRESOLVED": "Stop this question for now with a stated limitation.",
    "WAIT_FOR_SI": (
        "Stop MAIN investigation with an explicit SI dependency; does not assert SI exists."
    ),
    "CAPABILITY_LIMIT": (
        "Stop because the required interpretation cannot be expressed or performed."
    ),
}
ACTION_GUIDANCE = "Scientific question status. " + " ".join(
    f"{action}: {meaning}" for action, meaning in ACTION_MEANINGS.items()
)
FUSION_RECEIPT_VERSION = "tsdaf.fusion-execution-receipt.v4"
FUSION_RESULT_MEDIA = "application/vnd.tsdaf.fusion-result+json"


class PreparationTarget(ContractModel):
    """One explicit use of a preparation by an already displayed operation."""
    operation: str = Field(min_length=1, description="The displayed operation using this preparation.")
    material: str | None = Field(default=None, min_length=1,
        description="Optional displayed material-use name belonging to that operation; omission selects none.")


class ScientificFact(ContractModel):
    fact_id: str = Field(min_length=1)
    kind: UnifiedFactKind
    status: FactStatus = "ACCEPTED"
    text: str = ""
    category: str = ""
    extent: Literal["WHOLE_OWNERS", "WITHIN_OWNER", "UNRESOLVED"] = "WHOLE_OWNERS"
    labels: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    same_as: str | None = None
    rationale: str = ""
    measurement: MeasurementIntent | None = Field(default=None, exclude_if=lambda value: value is None)
    source_parts: tuple[SourceSelection, ...] = Field(default=(), exclude_if=lambda value: not value)
    preparation_use: PreparationTarget | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _distinct_refs(self) -> Self:
        for name in ("owners", "inputs", "outputs", "after", "source_ids", "labels"):
            values = getattr(self, name)
            _unique(values, label=name)
            if any(not value.strip() for value in values):
                raise ValueError("fact references and labels must be nonempty")
        return self


# A CONDITION's category is chosen from the Host's key table, never coined (ledger E61):
# the table is rendered once, in enrichment_models, and shown here and in the detail
# investigation's declaration; the prompts name the same keys.
CONDITION_CATEGORY_GUIDANCE = "For a CONDITION: " + CONDITION_KEY_GUIDANCE
# A MATERIAL's role and an OUTCOME's kind are chosen by meaning from the Host's tables
# (`material_meanings`, `outcome_meanings`), which the middle layer's words render too.
# They follow the condition sentence: the condition table is found by its first
# "written as such".
MATERIAL_CATEGORY_GUIDANCE = "For a MATERIAL: " + MATERIAL_ROLE_GUIDANCE
OUTCOME_CATEGORY_GUIDANCE = "For an OUTCOME: " + OUTCOME_KIND_GUIDANCE


class FactChange(ContractModel):
    """One fact's sparse change. Name the fact in fact_id and write only the properties
    being changed. A read returns a complete fact, not a write template: omit unchanged
    properties, including empty ones. clear lists optional properties to remove;
    withdraw: true with rationale withdraws the whole fact, keeping history. Repair
    incoming relations in the same group. Use add_source_ids to add evidence while
    retaining earlier evidence. A material is independently addressable.
    """

    fact_id: str = Field(
        min_length=1,
        description=(
            "The fact this change is about: an existing fact by its id as the progress "
            "lists it (a read location here instead binds the version you read); a fact "
            "created in this write by a name of your choosing, given here and nowhere else. "
            "The host binds identity and compares with the version a write naming no view "
            "is compared with (view says which)."
        ),
    )
    kind: FactKind | None = Field(
        default=None,
        description=(
            "The fact's kind; required for a new fact, omitted for an existing one. "
            "Use the native fact meanings in the task instructions."
        ),
    )
    status: FactStatus | None = Field(
        default=None,
        description="The fact's evidential status; assign it only to change it.",
    )
    text: str | None = Field(
        default=None,
        description=(
            "Scientific content. For COMPOUND, only its source-supported name or designation; "
            "put author labels in labels and explanations in rationale or a sourced NOTE. "
            "Names do not establish structure or cross-figure identity. "
            # An OUTCOME's text is the value phrase the middle layer's yield words are too.
            "For a MATERIAL: " + MATERIAL_NAME_MEANING + " "
            "For an OUTCOME: " + OUTCOME_VALUE_PHRASE + " "
            "Omission preserves. To remove an optional statement, name it in clear."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "The kind's role: for an OPERATION or NOTE use the task's native fact meanings. "
            + CONDITION_CATEGORY_GUIDANCE
            + " "
            + MATERIAL_CATEGORY_GUIDANCE
            + " "
            + OUTCOME_CATEGORY_GUIDANCE
        ),
    )
    extent: Literal["WHOLE_OWNERS", "WITHIN_OWNER"] | None = Field(
        default=None,
        description=(
            "Condition or measurement scope: whole owners or a substage explained in rationale."
        ),
    )
    labels: tuple[str, ...] | None = Field(
        default=None,
        description=AUTHOR_LABELS_MEANING,
    )
    owners: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Exact subjects: operation belongs to reaction; material to operation; "
            "condition to operation OR material; outcome to operations."
        ),
    )
    inputs: tuple[str, ...] | None = Field(
        default=None,
        description="For a REACTION, the compound facts it consumes: its block boundary.",
    )
    outputs: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "For a REACTION, the compound facts it forms; for an OUTCOME, the products "
            "it identifies."
        ),
    )
    after: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Operation predecessors within its reaction. "
            "Preserve or explicitly rebuild order when replacing operations."
        ),
    )
    source_ids: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Keys in the supplied immutable source index supporting the change, "
            "never invented citations."
        ),
    )
    same_as: str | None = Field(
        default=None,
        description=(
            "Explicit source-supported identity with another fact. "
            "Labels alone do not establish identity. Use clear to remove the relation."
        ),
    )
    rationale: str | None = Field(
        default=None,
        description=(
            "Explain the source meaning behind a correction, context role or identity; "
            "do not fill missing evidence by inference."
        ),
    )
    clear: tuple[FactClearField, ...] = Field(
        default=(),
        description=(
            "Explicitly clear optional properties of an existing fact. On a new fact an "
            "empty value is simply its default; on an existing fact omission preserves the "
            "property and clearing it is said here, never by sending an empty value. "
            "Required scientific content and relations cannot be emptied."
        ),
    )
    withdraw: bool = Field(
        default=False,
        description=(
            "Withdraw this fact with a rationale, keeping history. Fix necessary "
            "incoming relations in the same group; no cascade deletion."
        ),
    )
    add_source_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Add supporting evidence without replacing earlier evidence or resubmitting "
            "the content."
        ),
    )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        value = handler(schema)
        # Optional properties are declared by their content type, not as nullable. An
        # empty value is not a shape error: on a new fact it is the default, and on an
        # existing fact the executor refuses it as an implicit clear, which only the
        # executor can tell apart because only it knows whether the fact exists.
        for name, prop in value["properties"].items():
            if name in {"clear", "withdraw", "add_source_ids"}:
                continue
            alternatives = prop.pop("anyOf", None)
            if alternatives:
                prop.update(next(item for item in alternatives if item.get("type") != "null"))
                prop.pop("default", None)
        return value

    @model_validator(mode="after")
    def _explicit_changes(self) -> Self:
        _unique(self.clear, label="cleared properties")
        _unique(self.add_source_ids, label="added evidence")
        if any(name in self.model_fields_set for name in self.clear):
            raise ValueError("a property cannot be assigned and cleared together")
        if self.withdraw and (not self.rationale or not self.rationale.strip()):
            raise ValueError("withdrawal requires its scientific rationale")
        if self.withdraw and self.status not in (None, "REJECTED"):
            raise ValueError("withdrawal cannot assign an active status")
        return self


class UnifiedFactChange(FactChange):
    """Sparse fact changes with independent experimental accounts and measurement intent."""

    category: str | None = Field(
        default=None,
        description=FactChange.model_fields["category"].description.replace(
            MATERIAL_ROLE_GUIDANCE, material_role_guidance(open_roles=True)
        ),
    )

    source_parts: tuple[SourceSelection, ...] | None = Field(
        default=None,
        description="For an EXPERIMENT: its complete source wording, selected in separate "
        "fragments when discontinuous. The host copies exact text and evidence. Omission "
        "preserves; assigning replaces this field; clear removes the field, not the account.",
    )
    clear: tuple[UnifiedFactClearField, ...] = Field(
        default=(), description=FactChange.model_fields["clear"].description,
    )

    owners: tuple[str, ...] | None = Field(
        default=None,
        description="Exact subjects: an operation belongs to one REACTION or EXPERIMENT; "
        "a material belongs to its operation; a condition to its operation or material. "
        "For an OUTCOME, name its measured operations or its REACTION/EXPERIMENT directly. "
        "Multiple experimental owners retain unresolved ownership, not a guessed container.",
    )
    inputs: tuple[str, ...] | None = Field(
        default=None,
        description="For a REACTION or EXPERIMENT, the explicitly reported input compounds.",
    )
    outputs: tuple[str, ...] | None = Field(
        default=None,
        description="For a REACTION or EXPERIMENT, the explicitly reported output compounds; "
        "for an OUTCOME, the products identified by that measurement.",
    )
    after: tuple[str, ...] | None = Field(
        default=None,
        description="Actual operation predecessors within the same REACTION or EXPERIMENT. "
        "Preserve or explicitly rebuild scientific order when replacing operations.",
    )

    kind: UnifiedFactKind | None = Field(default=None, description=FactChange.model_fields["kind"].description)
    extent: Literal["WHOLE_OWNERS", "WITHIN_OWNER", "UNRESOLVED"] | None = Field(
        default=None, description=FactChange.model_fields["extent"].description)
    measurement: MeasurementIntent | None = Field(
        default=None,
        description="For an OUTCOME: same_as a named measurement, new=true, or unknown=true. Omission preserves on edit; first omission is unknown. Experiment ownership is expressed by owners independently.",
    )


class ReadingFactChange(UnifiedFactChange):
    """The same fact edit with references to Reading's displayed public inventory."""
    preparation_use: PreparationTarget | SkipJsonSchema[None] = Field(default=None,
        description="For a NOTE owned by exactly one EXPERIMENT: one use of that preparation. "
        "Choose a displayed operation and optional material use. Keep full meaning in text/rationale "
        "and evidence in source_ids. Each relation has its own fact name; omit to preserve, "
        "edit to replace, withdraw to remove this relation.")

    @model_validator(mode="after")
    def _preparation_present(self) -> Self:
        if "preparation_use" in self.model_fields_set and self.preparation_use is None:
            raise ValueError("preparation_use cannot be cleared; withdraw this relation instead")
        return self


class InlineFactChangeGroup(ContractModel):
    """Changes written inline that stand or fall together. Unrelated items stay independent."""

    changes: tuple[FactChange, ...] = Field(
        default=(),
        description="The changes that stand or fall together, one per fact, written inline.",
    )
    reason: str = Field(
        default="", description="Why these changes belong together; may be left empty."
    )

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        targets = (*(c.fact_id for c in self.changes), *getattr(self, "fact_ids", ()))
        if not targets:
            raise ValueError("an atomic group requires at least one change")
        _unique(targets, label="group targets")
        return self


class UnifiedInlineFactChangeGroup(InlineFactChangeGroup):
    """Inline form of the same candidate Fusion changes."""

    changes: tuple[UnifiedFactChange, ...] = Field(
        default=(), description=InlineFactChangeGroup.model_fields["changes"].description
    )


class ReadingInlineFactChangeGroup(UnifiedInlineFactChangeGroup):
    changes: tuple[ReadingFactChange, ...] = Field(
        default=(), description=InlineFactChangeGroup.model_fields["changes"].description)


class FactChangeGroup(InlineFactChangeGroup):
    """Changes that stand or fall together: changes written inline, or fact_ids naming items
    sent under changes in this same write, with a reason. Unrelated items stay independent."""

    fact_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Instead of inline changes: the fact_id of items sent under changes in this "
            "same write that belong to this group. The host moves them into the group."
        ),
    )


# A question as a Gate result holds it. Today's declaration takes `FusionQuestion`, which
# narrows this; a result sealed before 2026-09-23 may also hold READ or INTEGRATE -- a
# handoff to a window, retired that day -- and the reading plan a READ carried. Sealed
# bytes are never rewritten, and a handoff is not an unresolved question, so history
# keeps its own words here rather than being translated on read (E152 only filled in a
# field that had not existed; this would change what a record says).
class SealedQuestion(ContractModel):
    question_id: str = Field(min_length=1, description="Your name for this question.")
    subject_ids: tuple[str, ...] = Field(
        min_length=1, description="The facts or navigation segments this question is about."
    )
    question: str = Field(
        min_length=1,
        description=(
            "A neutral, self-contained question stating the investigation range, the "
            "remaining issue and the expected output."
        ),
    )
    status: Literal[
        "READ", "INTEGRATE", "RESOLVED", "UNRESOLVED", "WAIT_FOR_SI", "CAPABILITY_LIMIT"
    ] = Field(description=ACTION_GUIDANCE)
    explanation: str = Field(
        min_length=1,
        description="Why this status, grounded in the evidence read or the resolution reached.",
    )
    reading_plan: ReadingPlan | None = None
    text_block_ids: tuple[str, ...] = Field(
        default=(), description="Authorized MAIN text blocks from the catalog this concerns."
    )
    visual_ids: tuple[str, ...] = Field(
        default=(), description="Authorized MAIN images from the catalog this concerns."
    )

    @model_validator(mode="after")
    def _reading(self) -> Self:
        if (self.status == "READ") != (self.reading_plan is not None):
            raise ValueError("only a requested reading carries a reading plan")
        return self


def as_recorded(question: SealedQuestion) -> SealedQuestion:
    """The question as a result holds it: always a `SealedQuestion`, never the write shape.

    Pydantic compares models by class as well as fields, so a question written this turn
    (a `FusionQuestion`) and the same question read back from a sealed result (a
    `SealedQuestion`) would compare unequal, and the check that a question has not changed
    since it was read would refuse the model's own next edit of it. A result keeps one
    class, and everything that puts a question into one goes through here.
    """

    if type(question) is SealedQuestion:
        return question
    return SealedQuestion.model_validate(question.model_dump(mode="python"))


class FusionQuestion(SealedQuestion):
    """One scoped question and its status; only new or changed questions are written."""

    status: Literal["RESOLVED", "UNRESOLVED", "WAIT_FOR_SI", "CAPABILITY_LIMIT"] = Field(
        description=ACTION_GUIDANCE
    )
    reading_plan: SkipJsonSchema[None] = None


class RouteSegment(ContractModel):
    """Navigation scope linking observations to work; never a second executable graph."""

    segment_id: str = Field(min_length=1, description="Your name for this segment.")
    scope: str = Field(
        min_length=1,
        description=(
            "The part of the route this segment covers, in words; a step range or a "
            "rationale goes here, and a segment has no other prose field."
        ),
    )
    source_ids: tuple[str, ...] = Field(
        min_length=1,
        description="Keys in the supplied source index locating this scope, never invented.",
    )
    fact_ids: tuple[str, ...] = Field(
        default=(), description="Current results covering this scope."
    )


class UnifiedFactChangeGroup(FactChangeGroup):
    changes: tuple[UnifiedFactChange, ...] = Field(default=(), description=FactChangeGroup.model_fields["changes"].description)


class ReadingFactChangeGroup(UnifiedFactChangeGroup):
    changes: tuple[ReadingFactChange, ...] = Field(default=(), description=FactChangeGroup.model_fields["changes"].description)


class FusionProposal(ContractModel):
    """The items of one write, each already accepted on its own, for the state layer.

    Not the tool declaration: that is generated from ``fusion_write_collections``.
    Omitted parts preserve content; clear, withdraw and added evidence are explicit.
    """

    overview: str | None = None
    segments: tuple[RouteSegment, ...] = ()
    changes: tuple[FactChange | UnifiedFactChange | ReadingFactChange, ...] = ()
    groups: tuple[FactChangeGroup | UnifiedFactChangeGroup | ReadingFactChangeGroup, ...] = ()
    experiment_assessments: tuple[MainExperimentAssessment, ...] = ()
    questions: tuple[FusionQuestion, ...] = ()
    note: str | None = None

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        _unique(tuple(s.segment_id for s in self.segments), label="route segments")
        _unique(tuple(c.fact_id for c in self.changes), label="changed facts")
        _unique(tuple(q.question_id for q in self.questions), label="questions")
        return self


class ReadFusion(VisualReadBounds):
    """Read an authorized catalog location or revisit a saved read location.

    Returns source, historical model work or current findings distinctly. Actual reads
    provide immutable editable/citable locations; directory entries alone grant no write.
    """

    location: str = Field(min_length=1)
    start_sentence: int | None = Field(default=None, ge=0)
    end_sentence: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _range(self) -> Self:
        if (self.start_sentence is None) != (self.end_sentence is None):
            raise ValueError("provide both sentence bounds or neither")
        if self.start_sentence is not None and self.end_sentence is not None:
            if self.start_sentence >= self.end_sentence:
                raise ValueError("sentence range must be nonempty")
        if self.region is not None:
            if self.start_sentence is not None:
                raise ValueError("choose sentence bounds or a visual region")
        return self


class FinishFusion(WriteDeliveryControl):
    """Stop with saved results and limitations. PARTIAL may stop with an incomplete result;
    scientific publication requirements are checked separately.
    """

    note: str = Field(
        min_length=1, description="Why this investigation stops, including its limits."
    )


# The one name of this task's result as it stands; the catalog lists it as CURRENT_RESULT.
# A read resolves it to the version it pins, a write's view to the version the host last
# showed. It was one name to the catalog, the read rule and the declaration and none to
# the write check, which refused the 16 items a4's fusion wrote with view='current'.
CURRENT_LOCATION = "current"

# Which version a write is compared with, said once: the declaration shows it and the
# executor refuses a view with this same text (`compared_version`).
VIEW_RULE = (
    "The version of this result the write was revised from, when it is older than the "
    "one the host would otherwise compare with: the view_location a read of "
    f"'{CURRENT_LOCATION}' returned. Without view, or with '{CURRENT_LOCATION}', that "
    "version is the later of the latest progress and what your own last write left."
)


class ViewControl(ContractModel):
    """Which saved version of the result a write is compared with (`VIEW_RULE`)."""

    view: str | None = Field(default=None, description="Optional. " + VIEW_RULE)


def fusion_write_collections(*, unified_expression: bool = False, public_targets: bool = False) -> dict[str, type[ContractModel]]:
    """Everything a fusion write may change, in receipt order: the one table the
    declaration, the executor's registry and any probe derive from. How an item names
    its subject is the executor's, because facts are named through read locations."""
    return {
        "changes": (ReadingFactChange if public_targets else UnifiedFactChange) if unified_expression else FactChange,
        "groups": (ReadingFactChangeGroup if public_targets else UnifiedFactChangeGroup) if unified_expression else FactChangeGroup,
        "segments": RouteSegment,
        "questions": FusionQuestion,
        "experiment_assessments": MainExperimentAssessment,
    }


# A group may name this write's changes by fact_id instead of repeating them inline; the
# shape rule checks the names at both arrival points, the executor moves the items.
FUSION_WRITE_REFERENCES = (("groups", "fact_ids", "changes", "fact_id"),)

FUSION_WRITE_SCALARS: tuple[tuple[str, str], ...] = (
    ("overview", "The whole route in a few sentences. Set it or leave it."),
    ("note", "Scientific integration: source meaning, decisions and remaining limitations."),
)
# How a change names its fact is said by FactChange.fact_id and the changes collection's
# identity; this said it a third time.
FUSION_WRITE_DOC = (
    "Declare necessary linked changes in groups. Clear, withdraw and added evidence are explicit."
)

FUSION_TOOL_MODELS: dict[str, type[ContractModel]] = {
    "read": ReadRequest,
    "write": write_request(
        name="FusionWrite",
        collections=list(fusion_write_collections().items()),
        scalars=FUSION_WRITE_SCALARS,
        controls=(WriteControl, ViewControl),
        doc=FUSION_WRITE_DOC,
        references=FUSION_WRITE_REFERENCES,
        identity=identity_sentence,
    ),
    "finish_fusion": FinishFusion,
}


def fusion_tool_models(*, unified_expression: bool = False) -> dict[str, type[ContractModel]]:
    if not unified_expression:
        return FUSION_TOOL_MODELS
    return {
        **FUSION_TOOL_MODELS,
        "write": write_request(
            name="FusionWriteUnified",
            collections=list(fusion_write_collections(unified_expression=True).items()),
            scalars=FUSION_WRITE_SCALARS, controls=(WriteControl, ViewControl),
            doc=FUSION_WRITE_DOC, references=FUSION_WRITE_REFERENCES, identity=identity_sentence,
        ),
    }


def fusion_tools(*, unified_expression: bool = False) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "name": name,
            "description": model.__doc__,
            "parameters": provider_declaration_schema(model),
        }
        for name, model in fusion_tool_models(unified_expression=unified_expression).items()
    ]


class FactOrigin(ContractModel):
    """Host-proven accepted action, never a model-writable scientific field."""

    interaction_ref: ArtifactRef
    call_index: int = Field(ge=0)
    action_id: str = Field(min_length=1)
    item: str = Field(min_length=1)
    change_index: int = Field(default=0, ge=0)


class FactCandidate(ContractModel):
    """One actually recorded field value and its verified source references."""

    value: JsonValue
    source_ids: tuple[str, ...] = ()
    origins: tuple[FactOrigin, ...] = ()


class FactConflict(ContractModel):
    """Conflicting field readings; fact status remains the model's own judgment."""

    candidates: dict[str, tuple[FactCandidate, ...]] = Field(min_length=1)


class FusionResult(ContractModel):
    schema_version: Literal["tsdaf.fusion-result.v5"] = "tsdaf.fusion-result.v5"
    work_unit_id: str
    package_sha256: str
    overview: str = ""
    segments: tuple[RouteSegment, ...] = ()
    facts: tuple[ScientificFact, ...]
    conflicts: dict[str, FactConflict] = Field(default_factory=dict)
    field_origins: dict[str, dict[str, tuple[FactOrigin, ...]]] = Field(default_factory=dict)
    # First accepted creation, distinct from mutable per-field contribution origins.
    fact_creation_origins: dict[str, FactOrigin] = Field(default_factory=dict)
    questions: tuple[SealedQuestion, ...] = ()
    # Verified reading selection -> public evidence. Ownership exists only in facts.
    sources: dict[str, tuple[dict[str, str], ...]]
    evidence_registry_refs: tuple[ArtifactRef, ...]
    parent_result_ref: ArtifactRef | None = None
    question_id: str | None = None
    root_question_id: str | None = None
    source_errors: tuple[dict[str, object], ...] = ()
    experiment_assessments: tuple[MainExperimentAssessment, ...] = ()
    note: str

    def fields_known(self, fact: ScientificFact, *fields: str) -> bool:
        conflict = self.conflicts.get(fact.fact_id)
        return conflict is None or not set(fields) & conflict.candidates.keys()


# E152: `FusionQuestion.explanation` did not exist before FUSION_PROFILE v28. A sealed
# Gate result is read back whether it was just produced or is a frozen export years old
# (offline reprojection replays one byte-for-byte); the sealed bytes themselves are
# never rewritten, so this is the one seam every reader shares -- the one place a
# question sealed before the field existed is given the explanation it was never asked
# for, instead of every one of this project's eleven read call sites growing its own
# missing-field guess. A question written under today's declaration already carries
# the field, so this is a no-op on every fresh result.
_NO_RECORDED_EXPLANATION = (
    "Sealed before FusionQuestion required an explanation; none was recorded."
)


def read_fusion_result(payload: bytes) -> FusionResult:
    """Parse a sealed Gate result, tolerating one it predates `explanation` on."""
    data = json.loads(payload)
    for question in data.get("questions") or ():
        if isinstance(question, dict):
            question.setdefault("explanation", _NO_RECORDED_EXPLANATION)
    return FusionResult.model_validate_json(json.dumps(data))


def related_scope(result: FusionResult, subjects: Iterable[str]) -> set[str]:
    """Descendants are writable; owning events and boundaries are read dependencies."""
    scope = set(subjects)
    scope.update(fid for s in result.segments if s.segment_id in scope for fid in s.fact_ids)
    while True:
        before = set(scope)
        scope.update(f.fact_id for f in result.facts if set(f.owners) & scope)
        scope.update(f.fact_id for f in result.facts if f.same_as in scope)
        if scope == before:
            return scope
