"""Stored paper execution receipts for evidence inspection and publication.

Automatic paper scheduling has been removed. These data models keep historical
sealed results readable without reopening or reclassifying their executions.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string, _unique
from tsdaf.contracts.execution import TypedFailure
from tsdaf.contracts.source import ArtifactRef
from tsdaf.workflow.unit_decisions import UNIT_DECISIONS

__all__ = (
    "LiteraturePaperReceipt",
    "LiteratureWindowReceipt",
    "ReactionWindowOutcome",
    "RouteGroupOutcome",
    "WINDOW_RECEIPT_MEDIA",
)

# The one place a window receipt's media type is written; the readers reference it.
WINDOW_RECEIPT_MEDIA = "application/vnd.tsdaf.literature-reaction-window-receipt+json"


class RouteGroupOutcome(ContractModel):
    """What one route group produced, whether or not it ran to a decision."""

    route_group_id: str
    # None together with a failure code: the group never reached a Gate decision.
    gate_decision: str | None
    receipt_ref: ArtifactRef | None
    failure_code: str | None
    failure: TypedFailure | None = None
    child_work_unit_count: int = Field(ge=0)

    @model_serializer(mode="wrap")
    def _historical_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        if self.failure is None:
            value.pop("failure", None)
        return value

    @field_validator("route_group_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        return _stable_string(value, label="paper receipt route group id")

    @model_validator(mode="after")
    def _decided_or_failed(self) -> Self:
        decided = self.gate_decision is not None
        if decided == (self.failure_code is not None):
            raise ValueError("a route group outcome is either decided or failed, never both")
        if decided != (self.receipt_ref is not None):
            raise ValueError("a decided route group must carry its receipt and a failed one none")
        if self.failure is not None and str(self.failure.code) != self.failure_code:
            raise ValueError("route group failure details must match its failure code")
        if not decided and self.child_work_unit_count:
            raise ValueError("a failed route group cannot report children")
        if self.receipt_ref is not None and self.receipt_ref.media_type != (
            "application/vnd.tsdaf.literature-route-group-receipt+json"
        ):
            raise ValueError("route group outcome has a wrong receipt media type")
        return self


class ReactionWindowOutcome(ContractModel):
    """What one reaction window produced, whether or not it reached a Gate decision."""

    child_handoff_sha256: str
    # Each window owns one execution, named here. A window that reached a decision sealed
    # that execution successfully, which is what makes its observations publishable
    # independently of what its siblings did.
    window_execution_id: str
    depth: int = Field(ge=2)
    route_group_id: str | None
    input_label: str | None
    output_label: str | None
    gate_decision: str | None
    receipt_ref: ArtifactRef | None
    failure_code: str | None
    child_work_unit_count: int = Field(ge=0)

    @field_validator("child_handoff_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="reaction window outcome handoff hash")

    @field_validator("window_execution_id")
    @classmethod
    def _execution(cls, value: str) -> str:
        return _stable_string(value, label="reaction window execution id")

    @model_validator(mode="after")
    def _decided_or_failed(self) -> Self:
        decided = self.gate_decision is not None
        if decided == (self.failure_code is not None):
            raise ValueError("a window outcome is either decided or failed, never both")
        if decided != (self.receipt_ref is not None):
            raise ValueError("a decided window carries its receipt and a failed one none")
        if not decided and self.child_work_unit_count:
            raise ValueError("a failed window cannot report steps, conflicts or children")
        if self.receipt_ref is not None and self.receipt_ref.media_type != WINDOW_RECEIPT_MEDIA:
            raise ValueError("window outcome has a wrong receipt media type")
        return self


class LiteratureWindowReceipt(ContractModel):
    """Content-addressed record of every private artifact one window run sealed.

    Kept only so historical sealed results stay readable (`_read_window_receipt`,
    `_merge_windows` in literature_paper_facts.py); nothing runs a window anymore.
    """

    schema_version: Literal["tsdaf.literature-reaction-window-receipt.v5"]
    parent_execution_id: str
    window_execution_id: str
    document_revision_id: str
    route_group_id: str
    parent_work_unit_id: str
    work_unit_id: str
    work_unit_sha256: str
    source_view_sha256: str
    depth: int = Field(ge=2)
    # The two ends this window was opened between, copied from its sealed assignment so
    # a receipt reads as the transformation it covers rather than as an opaque hash.
    input_label: str
    output_label: str
    child_handoff_ref: ArtifactRef
    text_proposal_ref: ArtifactRef | None
    text_proposal_provenance_ref: ArtifactRef | None
    visual_proposal_ref: ArtifactRef | None
    visual_proposal_provenance_ref: ArtifactRef | None
    evidence_registry_ref: ArtifactRef | None
    evidence_entry_count: int = Field(ge=0)
    gate_proposal_ref: ArtifactRef
    gate_provenance_ref: ArtifactRef
    gate_decision: str
    child_work_unit_refs: tuple[ArtifactRef, ...]
    work_unit_ref: ArtifactRef | None = None
    source_inventory_ref: ArtifactRef | None = None

    @field_validator(
        "parent_execution_id",
        "window_execution_id",
        "document_revision_id",
        "route_group_id",
        "parent_work_unit_id",
        "work_unit_id",
        "gate_decision",
    )
    @classmethod
    def _identities(cls, value: str) -> str:
        return _stable_string(value, label="literature window receipt identity")

    @field_validator("work_unit_sha256", "source_view_sha256")
    @classmethod
    def _hashes(cls, value: str) -> str:
        return _sha256(value, label="literature window receipt hash")

    @model_validator(mode="after")
    def _distinct_children(self) -> Self:
        if self.child_work_unit_refs and self.gate_decision not in UNIT_DECISIONS:
            raise ValueError("literature window receipt decision and child inventory disagree")
        if self.gate_decision == "DEEPEN" and not self.child_work_unit_refs:
            raise ValueError("literature window receipt decision and child inventory disagree")
        digests = tuple(ref.sha256 for ref in self.child_work_unit_refs)
        if len(set(digests)) != len(digests):
            raise ValueError("literature window receipt repeats a sub-step window")
        return self


class LiteraturePaperReceipt(ContractModel):
    """One paper's worth of route-group runs, in the Router's canonical order."""

    schema_version: Literal["tsdaf.literature-paper-receipt.v2"]
    route_execution_id: str
    literature_execution_id: str
    paper_id: str
    document_revision_id: str
    package_sha256: str
    route_group_outcomes: tuple[RouteGroupOutcome, ...] = Field(min_length=1)
    # Stored execution history; an empty list does not establish scientific completion.
    reaction_window_outcomes: tuple[ReactionWindowOutcome, ...] = ()
    # Historical authorized handoffs that were not executed in this receipt.
    pending_work_unit_refs: tuple[ArtifactRef, ...] = ()

    @field_validator(
        "route_execution_id",
        "literature_execution_id",
        "paper_id",
        "document_revision_id",
    )
    @classmethod
    def _identities(cls, value: str) -> str:
        return _stable_string(value, label="literature paper receipt identity")

    @field_validator("package_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="literature paper receipt hash")

    @model_validator(mode="after")
    def _one_outcome_per_group(self) -> Self:
        _unique(
            tuple(item.route_group_id for item in self.route_group_outcomes),
            label="literature paper receipt route groups",
        )
        _unique(
            tuple(item.window_execution_id for item in self.reaction_window_outcomes),
            label="literature paper receipt window executions",
        )
        return self
