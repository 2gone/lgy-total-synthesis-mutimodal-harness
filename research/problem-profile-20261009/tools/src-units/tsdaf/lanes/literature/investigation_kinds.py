"""The investigation kinds' one policy, shared by tools, execution and publication.

Answer factories resolve the passive values and compiler through this table.
Receipt policy depends on the table; values and evidence interfaces do not.
Nothing here imports a workflow or seals a derived answer.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

from pydantic import create_model

from tsdaf.contracts._base import ContractModel
from tsdaf.lanes.literature.item_identity import keyed

if TYPE_CHECKING:
    from tsdaf.lanes.literature.enrichment_values import (
        FinishInvestigationCommand,
        InvestigationAnswer,
        ReceiptEvidence,
        TaskScope,
    )
    from tsdaf.lanes.literature.progress_views import ProgressState
    from tsdaf.lanes.literature.source_manifest import SourcePhase

CollectionTable = dict[str, tuple[type[ContractModel], tuple[str, ...]]]


def _paragraph_model() -> type[ContractModel]:
    from tsdaf.lanes.literature.middle_layer import ParagraphSet

    return ParagraphSet


def _reading_model() -> type[ContractModel]:
    from tsdaf.lanes.literature.middle_layer import ReadingAnswer

    return ReadingAnswer


def _record_model() -> type[ContractModel]:
    from tsdaf.lanes.literature.experimental_records import ExperimentalRecordAnswer
    return ExperimentalRecordAnswer


def _record_collections() -> CollectionTable:
    from tsdaf.lanes.literature.experimental_records import ExperimentalRecord
    return keyed(("records", ExperimentalRecord))


def _record_publication(receipt, *args, **kwargs):
    from tsdaf.lanes.literature.experimental_records import record_publication_answer
    return record_publication_answer(receipt)


def _record_progress(answer, state):
    from tsdaf.lanes.literature.experimental_records import record_progress
    return record_progress(answer, state)


def _mapping_model():
    from tsdaf.lanes.literature.final_mapping import FinalMappingAnswer
    return FinalMappingAnswer


def _mapping_publication(*args, **kwargs):
    raise ValueError('final documents publish their compiled snapshot, not legacy findings')


def _mapping_progress(answer, state):
    return {'model_delivery':answer.model_delivery, 'saved':answer.machine_ref is not None}


def _narrative_model() -> type[ContractModel]:
    from tsdaf.lanes.literature.enrichment_values import InvestigationAnswer

    return InvestigationAnswer


def _paragraph_collections() -> CollectionTable:
    from tsdaf.lanes.literature.middle_layer import paragraph_collections

    return paragraph_collections()


def _reading_collections() -> CollectionTable:
    from tsdaf.lanes.literature.middle_layer import reading_collections

    return reading_collections()


def _read_reading(sealed: bytes) -> ContractModel:
    from tsdaf.lanes.literature.middle_layer import read_reading_answer

    return read_reading_answer(sealed)


def _narrative_collections() -> CollectionTable:
    from tsdaf.lanes.literature.enrichment_values import supplemental_collections

    return supplemental_collections()


def _paragraph_answer(
    receipt: ReceiptEvidence,
    machine: Mapping[str, Any],
    task_baseline: Mapping[str, Any] | None,
    material_bindings: Mapping[str, str | None] | None = None,
    creation_keys: Mapping[str, str] | None = None,
    method_targets: Mapping[str, Mapping[str, str]] | None = None,
    measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
) -> InvestigationAnswer | None:
    from tsdaf.lanes.literature.middle_layer import ParagraphSet, paragraph_answer

    answer = receipt.raw_model()
    assert isinstance(answer, ParagraphSet)
    return paragraph_answer(answer)


def _reading_answer(
    receipt: ReceiptEvidence,
    machine: Mapping[str, Any],
    task_baseline: Mapping[str, Any] | None,
    material_bindings: Mapping[str, str | None] | None = None,
    creation_keys: Mapping[str, str] | None = None,
    method_targets: Mapping[str, Mapping[str, str]] | None = None,
    measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
) -> InvestigationAnswer | None:
    from tsdaf.lanes.literature.middle_layer import ReadingAnswer, compile_reading

    answer = receipt.raw_model()
    assert isinstance(answer, ReadingAnswer)
    return compile_reading(
        answer, receipt, machine, task_baseline=task_baseline, material_bindings=material_bindings,
        **({"creation_keys": creation_keys} if creation_keys is not None else {}),
        **({"method_targets": method_targets} if method_targets is not None else {}),
        measurement_machines=measurement_machines,
    )


def _narrative_answer(
    receipt: ReceiptEvidence,
    machine: Mapping[str, Any],
    task_baseline: Mapping[str, Any] | None,
    material_bindings: Mapping[str, str | None] | None = None,
    creation_keys: Mapping[str, str] | None = None,
    method_targets: Mapping[str, Mapping[str, str]] | None = None,
    measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
) -> InvestigationAnswer | None:
    from tsdaf.lanes.literature.enrichment_values import InvestigationAnswer

    answer = receipt.raw_model()
    assert isinstance(answer, InvestigationAnswer)
    return answer


def _paragraph_progress(answer: ContractModel, state: ProgressState) -> dict[str, Any]:
    from tsdaf.lanes.literature.middle_layer import ParagraphSet
    from tsdaf.lanes.literature.progress_views import paragraph_view

    assert isinstance(answer, ParagraphSet)
    return paragraph_view(answer, state)


def _reading_progress(answer: ContractModel, state: ProgressState) -> dict[str, Any]:
    from tsdaf.lanes.literature.middle_layer import ReadingAnswer
    from tsdaf.lanes.literature.progress_views import reading_view

    assert isinstance(answer, ReadingAnswer)
    return reading_view(answer, state)


def _narrative_progress(answer: ContractModel, state: ProgressState) -> dict[str, Any]:
    from tsdaf.lanes.literature.enrichment_values import InvestigationAnswer
    from tsdaf.lanes.literature.progress_views import narrative_view

    assert isinstance(answer, InvestigationAnswer)
    return narrative_view(answer, state)


@dataclass(frozen=True)
class KindSpec:
    name: str
    _answer_model: Callable[[], type[ContractModel]]
    _collections: Callable[[], CollectionTable]
    _publication_answer: Callable[
        [
            ReceiptEvidence,
            Mapping[str, Any],
            Mapping[str, Any] | None,
            Mapping[str, str | None] | None,
            Mapping[str, str] | None,
            Mapping[str, Mapping[str, str]] | None,
            Mapping[str, Mapping[str, Any]] | None,
        ],
        InvestigationAnswer | None,
    ]
    # What the task is shown every turn of what it has saved and what is still open
    # (`progress_views`). Every kind gives one; there is no storage-shaped fallback.
    _progress_view: Callable[[ContractModel, ProgressState], dict[str, Any]]
    _prompt_names: tuple[str, ...]
    _scoped_prompts: bool
    image_mode: Literal["DIRECT", "FIGURE_READER"]
    input_mode: Literal["FIRST_LAYER", "PARAGRAPH_LOCATION", "PAPER_READING", "FINAL_MAPPING"]
    publication_channel: Literal["CONTAINER", "NARRATIVE"]
    reconcile_detail: bool
    execution_suffix_pattern: str
    # The documents a task of this kind reads, by phase. Its catalog, its searches, its
    # reads and its images all come from these (`LiteratureSources.of_phases`), and so
    # do the documents it assesses. The supplement is the main text's own narrative and
    # reads MAIN alone (P4, the owner's option 1, 2026-09-24): listed a supporting
    # information it was never meant to read, a4's supplement wrote that it had not read
    # it, and that one limitation made the paper's whole second revision PARTIAL.
    phases: tuple[SourcePhase, ...]
    # How a sealed answer of this kind is read back (`read_answer`): only a kind that has
    # retired a shape its answers were sealed in has a seam of its own.
    _read_answer: Callable[[bytes], ContractModel] | None = None
    records_science: bool = False

    @property
    def accepts_paragraphs(self) -> bool:
        return self.input_mode == "PAPER_READING"

    @property
    def typed_reading(self) -> bool:
        return self.accepts_paragraphs and not self.records_science

    @property
    def export_lane(self) -> str:
        return self.name

    def answer_model(self, *, unified_expression: bool = False, current_updates: bool = False) -> type[ContractModel]:
        if self.name == "EXPERIMENTAL_READING" and unified_expression:
            from tsdaf.lanes.literature.middle_layer import UnifiedReadingAnswer, UnifiedReadingAnswerV2

            return UnifiedReadingAnswerV2 if current_updates else UnifiedReadingAnswer
        if self.name == "SUPPLEMENTAL_INFORMATION" and unified_expression:
            from tsdaf.lanes.literature.enrichment_values import UnifiedInvestigationAnswer

            return UnifiedInvestigationAnswer
        return self._answer_model()

    def read_answer(self, sealed: bytes) -> ContractModel:
        """A sealed answer in today's shape, read the one way every reader reads it. A
        kind whose answers were never sealed in another shape reads them as its model."""
        if self._read_answer is not None:
            return self._read_answer(sealed)
        if self.name == "SUPPLEMENTAL_INFORMATION":
            from pydantic import ValidationError

            try:
                return self.answer_model().model_validate_json(sealed)
            except ValidationError:
                return self.answer_model(unified_expression=True).model_validate_json(sealed)
        return self.answer_model().model_validate_json(sealed)

    def empty_answer(self, execution_id: str, *, unified_expression: bool = False, current_updates: bool = False) -> ContractModel:
        model = self.answer_model(unified_expression=unified_expression, current_updates=current_updates)
        if self.input_mode == 'FINAL_MAPPING':
            return model()
        data = {"status": "PARTIAL", "explanation": "Investigation in progress."}
        if "task" in model.model_fields:
            data["task"] = execution_id
        return model.model_validate(data)

    def finish_model(self) -> type[FinishInvestigationCommand]:
        """The existing delivery command with the actual answer's scientific status."""
        if self.input_mode == 'FINAL_MAPPING':
            from tsdaf.lanes.literature.final_mapping import FinishFinalMapping
            return FinishFinalMapping
        return _finish_model(self.name)

    def collections(
        self, task: TaskScope = "WHOLE", *, unified_expression: bool = False, current_updates: bool = False
    ) -> CollectionTable:
        if self.name == "EXPERIMENTAL_READING" and unified_expression:
            from tsdaf.lanes.literature.middle_layer import reading_collections

            return reading_collections(unified_expression=True, current_updates=current_updates)
        if self.name == "SUPPLEMENTAL_INFORMATION" and unified_expression:
            from tsdaf.lanes.literature.enrichment_values import UnifiedNarrative

            return {**self._collections(), **keyed(("narratives", UnifiedNarrative))}
        return self._collections()

    def write_collections(
        self, task: TaskScope = "WHOLE", *, unified_expression: bool = False, current_updates: bool = False
    ) -> CollectionTable:
        from tsdaf.lanes.literature.enrichment_values import WithdrawFinding

        collections = self.collections(task, unified_expression=unified_expression, current_updates=current_updates)
        if task == "BLOCK":
            return collections
        return {**collections, **keyed(("withdrawals", WithdrawFinding))}

    def names_skeleton_materials(self) -> bool:
        """Whether this kind's answers can name a route material by its host position.

        Derived from what the kind writes, not declared beside it: only a kind that can
        name one is shown the route's materials under their positions. A locator writes
        paragraphs and was handed the whole position table anyway, 14 KB on every turn
        of every block task of batch 4's second run, none of it usable (2026-09-24).
        """
        return any(
            "skeleton_material_id" in model.model_fields for model, _ in self.collections().values()
        )

    def prompt_names(self, task: TaskScope = "WHOLE") -> tuple[str, ...]:
        if not self._scoped_prompts:
            return self._prompt_names
        scope = "block" if task == "BLOCK" else "paper"
        return (*self._prompt_names, f"experimental_detail_{scope}_v1.md")

    def publication_answer(
        self,
        receipt: ReceiptEvidence,
        current_machine: Mapping[str, Any],
        *,
        task_baseline: Mapping[str, Any] | None = None,
        material_bindings: Mapping[str, str | None] | None = None,
        creation_keys: Mapping[str, str] | None = None,
        method_targets: Mapping[str, Mapping[str, str]] | None = None,
        measurement_machines: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> InvestigationAnswer | None:
        if receipt.raw_answer is None:
            return receipt.answer
        return self._publication_answer(
            receipt, current_machine, task_baseline, material_bindings, creation_keys, method_targets,
            measurement_machines,
        )

    def progress_view(self, answer: ContractModel, state: ProgressState) -> dict[str, Any]:
        return self._progress_view(answer, state)

    def counts_against_paper(self, receipt: ReceiptEvidence) -> bool:
        from tsdaf.lanes.literature.enrichment_values import delivered_nothing

        if receipt.failure:
            return True
        if self.input_mode == 'FINAL_MAPPING':
            answer = receipt.raw_model()
            return answer is None or answer.model_delivery != 'COMPLETE'
        if self.input_mode == "PAPER_READING":
            # The whole-paper middle layer owns its scientific completion claim.
            # COMPLETE can legitimately add nothing; read/citation counts cannot
            # override it. Failure above remains an independent execution fact.
            answer = receipt.raw_model()
            return answer is None or getattr(answer, "status", None) != "COMPLETE"
        if self.reconcile_detail:
            return False
        if delivered_nothing(receipt):
            return True
        answer = receipt.raw_model()
        assert answer is not None
        return bool(
            getattr(answer, "status", None) != "COMPLETE" or getattr(answer, "limitations", ())
        )

    def matches_execution(self, execution_id: str) -> bool:
        return re.search(self.execution_suffix_pattern, execution_id) is not None


_KINDS = (
    KindSpec(
        name='EXPERIMENTAL_MAPPING', _answer_model=_mapping_model, _collections=lambda: {},
        _publication_answer=_mapping_publication, _progress_view=_mapping_progress,
        _prompt_names=('final_science_document_v1.md',), _scoped_prompts=False,
        image_mode='FIGURE_READER', input_mode='FINAL_MAPPING', publication_channel='CONTAINER',
        reconcile_detail=False, execution_suffix_pattern=r'-mapping(-B\d{3})?(-R\d+)?$', phases=('MAIN','SI'),
    ),
    KindSpec(
        name="EXPERIMENTAL_DETAIL",
        _answer_model=_paragraph_model,
        _collections=_paragraph_collections,
        _publication_answer=_paragraph_answer,
        _progress_view=_paragraph_progress,
        _prompt_names=("experimental_knowledge_v1.md", "experimental_detail_v1.md"),
        _scoped_prompts=True,
        image_mode="FIGURE_READER",
        input_mode="PARAGRAPH_LOCATION",
        publication_channel="CONTAINER",
        reconcile_detail=True,
        execution_suffix_pattern=r"-detail(-B\d{3})?(-R\d+)?$",
        phases=("MAIN", "SI"),
    ),
    KindSpec(
        name="EXPERIMENTAL_RECORDING",
        _answer_model=_record_model,
        _collections=_record_collections,
        _publication_answer=_record_publication,
        _progress_view=_record_progress,
        _prompt_names=("experimental_recording_v1.md",),
        _scoped_prompts=False,
        image_mode="FIGURE_READER",
        input_mode="PAPER_READING",
        publication_channel="NARRATIVE",
        reconcile_detail=False,
        execution_suffix_pattern=r"-recording(-R\d+)?$",
        phases=("MAIN", "SI"),
        records_science=True,
    ),
    KindSpec(
        name="EXPERIMENTAL_READING",
        _answer_model=_reading_model,
        _collections=_reading_collections,
        _publication_answer=_reading_answer,
        _progress_view=_reading_progress,
        _prompt_names=("experimental_knowledge_v1.md", "experimental_reading_v1.md"),
        _scoped_prompts=False,
        image_mode="FIGURE_READER",
        input_mode="PAPER_READING",
        publication_channel="CONTAINER",
        reconcile_detail=True,
        execution_suffix_pattern=r"-reading(-R\d+)?$",
        phases=("MAIN", "SI"),
        _read_answer=_read_reading,
    ),
    KindSpec(
        name="SUPPLEMENTAL_INFORMATION",
        _answer_model=_narrative_model,
        _collections=_narrative_collections,
        _publication_answer=_narrative_answer,
        _progress_view=_narrative_progress,
        _prompt_names=("experimental_knowledge_v1.md", "supplemental_information_v1.md"),
        _scoped_prompts=False,
        image_mode="DIRECT",
        input_mode="FIRST_LAYER",
        publication_channel="NARRATIVE",
        reconcile_detail=False,
        execution_suffix_pattern=r"-supplement(-R\d+)?$",
        phases=("MAIN",),
    ),
)
_BY_NAME = {spec.name: spec for spec in _KINDS}


def investigation_kinds() -> tuple[KindSpec, ...]:
    return _KINDS


def investigation_kind(kind: str) -> KindSpec:
    try:
        return _BY_NAME[kind]
    except KeyError:
        raise ValueError(f"unknown investigation kind: {kind}") from None


def investigation_kind_for_execution(execution_id: str) -> KindSpec | None:
    return next((spec for spec in _KINDS if spec.matches_execution(execution_id)), None)


def new_investigation_kind(kind: str, profile=None) -> KindSpec:
    """Choose a new task, leaving historical receipt decoding unchanged."""
    spec=investigation_kind(kind)
    if profile is not None and profile.faithful_mapping and spec.typed_reading:
        raise ValueError('The faithful workflow uses scientific recording followed by EXPERIMENTAL_MAPPING; '
                         'the legacy typed-reading update protocol is only for older profiles.')
    return spec


@cache
def _finish_model(kind: str) -> type[FinishInvestigationCommand]:
    from tsdaf.lanes.literature.enrichment_values import FinishInvestigationCommand

    spec = investigation_kind(kind)
    status = spec.answer_model().model_fields["status"]
    description = FinishInvestigationCommand.__doc__
    if spec.records_science:
        from tsdaf.lanes.literature.experimental_records import RECORD_FINISH_DESCRIPTION
        description = RECORD_FINISH_DESCRIPTION
    fields: dict[str, Any] = {"status": (status.annotation, copy.deepcopy(status))}
    if spec.records_science:
        explanation = spec.answer_model().model_fields["explanation"]
        fields["explanation"] = (explanation.annotation, copy.deepcopy(explanation))
    return create_model(
        kind.title().replace("_", "") + "Finish",
        __base__=FinishInvestigationCommand,
        __doc__=description,
        **fields,
    )
