"""The middle layer's scientific account, independent of the final fact schema."""

from typing import ClassVar, Literal

from pydantic import Field

from tsdaf.contracts._base import ContractModel
from tsdaf.lanes.literature.enrichment_values import SavedItem


RECORD_FINISH_DESCRIPTION = (
    'Deliver the saved scientific account. Completing its organization does not resolve the scientific questions it explicitly retains. status states that scientific state; disposition separately states technical write delivery.'
)


class ExperimentalRecord(SavedItem):
    """Maintain one coherent scientific account with its supporting sources.

    Explain objects, order, conditions, relationships and uncertainty in the way that
    preserves their meaning. Saving an account does not adopt a database field value.
    """

    local_edit_fields: ClassVar[frozenset[str]] = frozenset({"title", "explanation", "selection_ids"})
    local_edit_fixed_fields: ClassVar[frozenset[str]] = frozenset({"item_id"})
    item_id: str | None = Field(default=None, description=(
        "Select the supplied handle to replace a saved account. Omit to create a new account."
    ))
    title: str = Field(min_length=1, description="A readable title; titles need not be unique.")
    explanation: str = Field(min_length=1, description=(
        'Your complete source-grounded scientific account. Organize the supplied meaning faithfully '
        'rather than improving or completing the procedure. Keep qualifications attached to their '
        'assertions; preserve the source wording of a limiting clause when paraphrasing could '
        'strengthen or change its claim. Distinguish any model interpretation from what is '
        'reported. Keep the precision of the evidence. Reorganization must not add unstated timing, '
        'identity, scope or certainty; if a paraphrase would add a qualifier, retain the original '
        'wording instead. Any further model interpretation must be identified as interpretation, '
        'not presented as a reported fact.'
    ))
    selection_ids: tuple[str, ...] = Field(default=(), description=(
        "Optional supplied source selections. Omit to preserve a saved account's sources; "
        "send [] to clear them. An unbound source is reported separately and does not discard the account."
    ))


class ExperimentalRecordAnswer(ContractModel):
    status: Literal["COMPLETE", "PARTIAL"] = Field(description=(
        'Scientific state of the account, not whether writing is finished. Use PARTIAL when the account explicitly retains an unresolved scientific question or unresolved alternatives, even if all supplied meaning has been organized. Use COMPLETE when no such unresolved scientific question remains within this task. Missing database fields or facts outside the task do not determine this status. Both statuses can be delivered successfully; disposition separately states technical write delivery.'
    ))
    explanation: str = Field(min_length=1, description=(
        'Brief reason for the chosen scientific status, pointing to unresolved matters if any. The '
        'saved records carry the complete scientific account; do not restate their experimental '
        'procedures or add new scientific conclusions here.'
    ))
    records: tuple[ExperimentalRecord, ...] = ()


def record_progress(answer, state):
    # No public compilation and no implied missing-field checklist.
    return {"records": [item.model_dump(mode="json") for item in answer.records]}


def record_publication_answer(receipt):
    """Copy submitted accounts as model prose; no scientific category or owner inference."""
    from tsdaf.lanes.literature.enrichment_values import UnifiedInvestigationAnswer, UnifiedNarrative
    answer = receipt.raw_model()
    if not isinstance(answer, ExperimentalRecordAnswer):
        raise ValueError("expected scientific records")
    return UnifiedInvestigationAnswer(status=answer.status, explanation=answer.explanation,
        narratives=tuple(UnifiedNarrative(
            item_id=item.item_id, title=item.title, kind="EXPERIMENTAL_ACCOUNT",
            explanation=item.explanation, selection_ids=item.selection_ids,
        ) for item in answer.records))
