"""Small handoffs consumed by investigators and immutable public assembly."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_delivery import WriteDeliveryControl as WriteDeliveryControl
from tsdaf.lanes.literature.enrichment_values import (
    ANSWER_COLLECTIONS as ANSWER_COLLECTIONS,
)
from tsdaf.lanes.literature.enrichment_values import (
    BLOCK_SCOPED_COLLECTIONS as BLOCK_SCOPED_COLLECTIONS,
)
from tsdaf.lanes.literature.enrichment_values import (
    BLOCK_TASKS_COVER as BLOCK_TASKS_COVER,
)
from tsdaf.lanes.literature.enrichment_values import (
    CONDITION_KEY_GUIDANCE as CONDITION_KEY_GUIDANCE,
)
from tsdaf.lanes.literature.enrichment_values import (
    NARRATIVE_KINDS as NARRATIVE_KINDS,
)
from tsdaf.lanes.literature.enrichment_values import (
    BlockOperation as BlockOperation,
)
from tsdaf.lanes.literature.enrichment_values import (
    CompoundAddition as CompoundAddition,
)
from tsdaf.lanes.literature.enrichment_values import (
    CompoundCharacterization as CompoundCharacterization,
)
from tsdaf.lanes.literature.enrichment_values import (
    ConditionAddition as ConditionAddition,
)
from tsdaf.lanes.literature.enrichment_values import (
    ContainerPatch as ContainerPatch,
)
from tsdaf.lanes.literature.enrichment_values import (
    FinishInvestigation as FinishInvestigation,
)
from tsdaf.lanes.literature.enrichment_values import (
    FinishInvestigationCommand as FinishInvestigationCommand,
)
from tsdaf.lanes.literature.enrichment_values import (
    InvestigationAnswer as InvestigationAnswer,
)
from tsdaf.lanes.literature.enrichment_values import UnifiedInvestigationAnswer
from tsdaf.lanes.literature.enrichment_values import (
    InvestigationCoverage as InvestigationCoverage,
)
from tsdaf.lanes.literature.enrichment_values import (
    InvestigationLimitation as InvestigationLimitation,
)
from tsdaf.lanes.literature.enrichment_values import (
    MainExperimentAssessment as MainExperimentAssessment,
)
from tsdaf.lanes.literature.enrichment_values import (
    NarrativeKind as NarrativeKind,
)
from tsdaf.lanes.literature.enrichment_values import (
    OperationAddition as OperationAddition,
)
from tsdaf.lanes.literature.enrichment_values import (
    OperationStageAddition as OperationStageAddition,
)
from tsdaf.lanes.literature.enrichment_values import (
    OutcomeAddition as OutcomeAddition,
)
from tsdaf.lanes.literature.enrichment_values import (
    ProcedureElision as ProcedureElision,
)
from tsdaf.lanes.literature.enrichment_values import (
    ReactionBlockAddition as ReactionBlockAddition,
)
from tsdaf.lanes.literature.enrichment_values import (
    SavedItem as SavedItem,
)
from tsdaf.lanes.literature.enrichment_values import (
    SourceRead as SourceRead,
)
from tsdaf.lanes.literature.enrichment_values import (
    SupplementalNarrative as SupplementalNarrative,
)
from tsdaf.lanes.literature.enrichment_values import (
    TaskScope as TaskScope,
)
from tsdaf.lanes.literature.enrichment_values import (
    WithdrawFinding as WithdrawFinding,
)
from tsdaf.lanes.literature.enrichment_values import (
    _answer_items as _answer_items,
)
from tsdaf.lanes.literature.enrichment_values import (
    _handle_field as _handle_field,
)
from tsdaf.lanes.literature.enrichment_values import (
    cited_selection_ids as cited_selection_ids,
)
from tsdaf.lanes.literature.enrichment_values import (
    condition_key_guidance as condition_key_guidance,
)
from tsdaf.lanes.literature.enrichment_values import (
    delivered_nothing as delivered_nothing,
)
from tsdaf.lanes.literature.enrichment_values import (
    supplemental_collections as supplemental_collections,
)
from tsdaf.lanes.literature.investigation_kinds import investigation_kind, investigation_kinds
from tsdaf.lanes.literature.item_identity import keyed
from tsdaf.lanes.literature.read_models import VisualReadBounds as VisualReadBounds


def legacy_investigation_collections(
    kind: str, *, task: TaskScope = "WHOLE"
) -> dict[str, tuple[type[ContractModel], tuple[str, ...]]]:
    """Read historical writes; the retired detail collections are never offered live."""

    spec = investigation_kind(kind)
    if spec.publication_channel == "NARRATIVE":
        return supplemental_collections()
    if spec.input_mode != "PARAGRAPH_LOCATION":
        raise ValueError("this investigation kind has no historical container writes")
    every = keyed(
        ("patches", ContainerPatch),
        ("new_outcomes", OutcomeAddition),
        # The three the route never saw come before the conditions: a condition may bind
        # to an operation written in this same answer, and items apply in this order.
        # The stages come before the conditions too: a condition names the stretch it
        # was read in by a stage_key this same answer declared (E157).
        ("new_compounds", CompoundAddition),
        ("new_reaction_blocks", ReactionBlockAddition),
        ("new_operations", OperationAddition),
        ("new_stages", OperationStageAddition),
        ("procedure_elisions", ProcedureElision),
        ("new_conditions", ConditionAddition),
        ("characterizations", CompoundCharacterization),
        ("coverage", InvestigationCoverage),
        ("limitations", InvestigationLimitation),
    )
    if task == "BLOCK":
        return {key: every[key] for key in BLOCK_SCOPED_COLLECTIONS}
    if task == "PAPER":
        return {key: value for key, value in every.items() if key not in BLOCK_TASKS_COVER}
    return every


def investigation_collections(
    kind: str, *, task: TaskScope = "WHOLE"
) -> dict[str, tuple[type[ContractModel], tuple[str, ...]]]:
    """Production findings and identities, from the kind's one collection table."""
    return investigation_kind(kind).collections(task)


def investigation_write_collections(
    kind: str, *, task: TaskScope = "WHOLE", expression: bool = True,
    open_expression: bool = False, unified_expression: bool = False, current_updates: bool = False,
) -> dict[str, tuple[type[ContractModel], tuple[str, ...]]]:
    """One write table, restricted to the selected public profile's capabilities."""
    collections = investigation_kind(kind).write_collections(
        task, unified_expression=unified_expression, current_updates=current_updates
    )
    if kind == "EXPERIMENTAL_READING":
        from tsdaf.lanes.literature.middle_layer import (
            ConflictRuling,
            NameRuling,
            OpenNameRuling,
            finding_write_model,
            legacy_ruling_writer,
        )

        collections = dict(collections)
        _, identity = collections["findings"]
        collections["findings"] = (
            finding_write_model(
                expression=expression,
                open_expression=open_expression,
                unified_expression=unified_expression, current_updates=current_updates,
            ), identity,
        )
        for name, domain in (("conflicts", ConflictRuling), ("names", NameRuling)):
            if name not in collections:
                continue
            writer = (
                (OpenNameRuling if name == "names" else domain)
                if open_expression
                else legacy_ruling_writer(domain)
            )
            collections[name] = (writer, collections[name][1])
    if open_expression and "narratives" in collections:
        from tsdaf.lanes.literature.enrichment_values import OpenNarrative, UnifiedNarrative

        collections = dict(collections)
        _, identity = collections["narratives"]
        collections["narratives"] = (
            UnifiedNarrative if unified_expression else OpenNarrative, identity
        )
    return collections


class InvestigationReceipt(ContractModel):
    schema_version: Literal["tsdaf.literature-investigation.v2"] = (
        "tsdaf.literature-investigation.v2"
    )
    kind: str = Field(json_schema_extra={"enum": [s.name for s in investigation_kinds()]})
    paper_id: str
    execution_id: str
    baseline_machine_ref: ArtifactRef | None = None
    input_ref: ArtifactRef
    answer: InvestigationAnswer | UnifiedInvestigationAnswer | None = None
    # Model output is evidence; the compiled InvestigationAnswer exists only while
    # publishing. A JSON dictionary avoids importing middle_layer back into this
    # module; the kind's actual model validates it lazily, without rewriting it.
    raw_answer: dict[str, JsonValue] | None = None
    failure: str | None = None
    observations: tuple[dict[str, object], ...] = ()
    selections: dict[str, dict[str, object]] = Field(default_factory=dict)
    # Explicit execution capability, not inferred from missing historical feedback.
    record_source_version: Literal[1] | None = None
    read_scopes: tuple[dict[str, object], ...] = ()
    unread_sources: tuple[dict[str, object], ...] = ()
    read_errors: tuple[dict[str, object], ...] = ()
    resources_sha256: str = ""
    # What this task cost at the provider. Kept on the receipt because the only other
    # copy is inside per-turn audit artifacts nothing above this layer opens, and
    # without the rate a run cannot tell a slow gateway from a slower harness: the
    # e108 fusion phase read as 1.69x slower than e106 when the model had in fact done
    # less work, and the whole difference was generation rate (183 -> 121 tok/s).
    thought_tokens: int = 0
    model_seconds: float = 0.0
    request_refs: tuple[ArtifactRef, ...] = ()
    audit_refs: tuple[ArtifactRef, ...] = ()
    source_identities: tuple[dict[str, object], ...] = ()
    # The reason the Host ended this task instead of the model finishing it: the stall
    # rule or the turn budget, in the executor's own words, or None when the model called
    # finish_investigation itself. Two of the eight tasks of the e106 round (B001, B006)
    # were closed out having delivered nothing, and the sealed receipt did not say so --
    # `delivered_nothing` could tell you they brought nothing, never that the Host cut
    # them off, and the reason lived only inside a write-delivery audit artifact.
    closed_out: str | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        investigation_kind(value)
        return value

    @model_validator(mode="after")
    def _one_answer_source(self) -> InvestigationReceipt:
        if self.answer is not None and self.raw_answer is not None:
            raise ValueError("an investigation receipt has exactly one answer source")
        if self.raw_answer is not None:
            self.raw_model()
        return self

    def raw_model(self) -> ContractModel | None:
        """Parse the actual evidence, never compile it or insert defaults into it. It is
        read through the kind's one seam, which reads a shape since retired as today's."""
        if self.raw_answer is None:
            return self.answer
        return investigation_kind(self.kind).read_answer(canonical_json_bytes(self.raw_answer))
