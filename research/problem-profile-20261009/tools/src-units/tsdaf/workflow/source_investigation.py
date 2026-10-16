"""Execute source investigations over the shared runtime context projection."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any, cast

from tsdaf.contracts.tool_read import READ_DELIVERY_VERSION
from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.execution import ProviderInteractionError
from tsdaf.contracts.literature.objects import experiment_operations
from tsdaf.contracts.literature.procedure_targets import source_catalog_location
from tsdaf.contracts.literature.schema import MachineProfile, machine_profile
from tsdaf.contracts.literature.stage_semantics import compile_operation_stages
from tsdaf.contracts.package import (
    BLOCK_ID_PATTERN,
    GROUP_ID_PATTERN,
    PAGE_ID_PATTERN,
    VISUAL_ID_PATTERN,
    format_page_id,
)
from tsdaf.contracts.reading_units import ReadingUnits, UnitKind, unit_reading_group
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.workflow.record_source_feedback import (
    project_record_binding_feedback, record_binding_view, source_binding_view,
)
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_read import (
    QuerySelection,
    Selection,
    TextSelection,
    WholeSelection,
    item_name,
)
from tsdaf.contracts.tool_write import write_shape
from tsdaf.lanes.literature.application_contributions import finding_applications
from tsdaf.lanes.literature.citation_policy import SOURCE_CITATION_POLICY, FINAL_CITATION_POLICY
from tsdaf.lanes.literature.container_enrichment import (
    _scientific_view,
    anchored_asset,
    anchored_block_id,
    apply_container_enrichment_items,
    candidate_step_sources,
    candidate_step_units,
    characterization_view,
    container_view,
    content_hash,
    figure_sources,
    general_methods_blocks,
    unclaimed_step_units,
    observations_these_blocks_cite,
    project_detail_group_context,
    public_narratives,
    route_account,
    scoped_blocks,
    step_author_labels,
    step_sources,
)
from tsdaf.lanes.literature.enrichment_models import (
    ANSWER_COLLECTIONS,
    FinishInvestigationCommand,
    InvestigationAnswer,
    InvestigationReceipt,
    SourceRead,
    TaskScope,
    WithdrawFinding,
    cited_selection_ids,
    investigation_write_collections,
)
from tsdaf.lanes.literature.enrichment_values import OpenNarrative, bind_narrative_subjects
from tsdaf.lanes.literature.evidence import furniture_by_package, visual_caption, visual_label
from tsdaf.lanes.literature.gate_models import FusionResult, read_fusion_result
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.investigation_kinds import investigation_kind
from tsdaf.lanes.literature.item_identity import identity_of
from tsdaf.lanes.literature.local_edits import restore_semantic_state, semantic_state
from tsdaf.lanes.literature.material_sources import material_sources_for
from tsdaf.lanes.literature.material_write_dependencies import (
    ReadingWritePlan,
    reading_write_groups,
)
from tsdaf.lanes.literature.middle_layer import (
    AmbiguousMethodUnit,
    Finding,
    FindingExplanationRef,
    OpenNameRuling,
    ParagraphSet,
    ReadingAnswer,
    SelectionsHeld,
    bind_ruling_owner,
    current_ruling_view,
    inherit_finding_scope,
    reading_write_context,
    reconcile_measurement_updates,
    validate_compound_revision,
    validate_material_compilation,
    validate_paragraphs,
    validate_reading,
)
from tsdaf.lanes.literature.model_names import (
    NAMING_VERSION,
    Namer,
    Names,
    UnknownReference,
    minted,
    route_names,
    task_names,
)
from tsdaf.lanes.literature.model_view import (
    compound_view,
    delivered_paragraphs,
    detail_context_view,
    document_view,
    group_result_view,
    observation_views,
    passage_steps,
    round_left_view,
    route_result_view,
    routing_view,
)
from tsdaf.lanes.literature.progress_views import ProgressState
from tsdaf.lanes.literature.provider_port import LaneInteractionClient, ProviderBinding
from tsdaf.lanes.literature.public_objects import public_object_pointers
from tsdaf.lanes.literature.round_handoff import paragraph_scopes, source_handoff
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans
from tsdaf.lanes.literature.source_investigation import (
    INVESTIGATION_TOOL_KINDS,
    INVESTIGATION_VERSION,
    FigureReader,
    InvestigationSourceReader,
    QuestionedWhole,
    investigation_read_models,
    investigation_resources,
)
from tsdaf.lanes.literature.source_manifest import LiteratureSources, general_method_targets
from tsdaf.lanes.literature.source_scope import VisualSourceSlice
from tsdaf.lanes.literature.stage_ledger import EnrichmentLedgers, general_methods_first_blocks
from tsdaf.lanes.literature.visual_detail_lane import (
    FIGURE_READING_MEDIA,
    FIGURE_REQUEST_MEDIA,
    read_figure_in_words,
)
from tsdaf.workflow.accepted_artifacts import accept_artifact, read_artifact
from tsdaf.workflow.action_progress import bounded_action_view
from tsdaf.workflow.call_budget import BudgetPaused, CallBudget
from tsdaf.workflow.content_window import (
    DELIVERED_KEY,
    IN_FIRST_MESSAGE,
    SourceDelivery,
    read_entries,
)
from tsdaf.workflow.fusion_names import (
    POINTER,
    GroupNames,
    at_pointer,
    first_shown,
    group_names,
    pointed,
)
from tsdaf.workflow.handle_names import handle_names
from tsdaf.workflow.investigation_origins import (
    INVESTIGATION_INTERACTION_MEDIA,
    REPAIR_COMMAND_VERSION,
    delivered_selections_in as _delivered_in,
    InvestigationOrigins,
    expression_creation_keys,
    narrative_creation_keys,
)
from tsdaf.workflow.investigation_progress import (
    INVESTIGATION_PROGRESS_MEDIA,
    InvestigationCheckpoint,
    restore_investigation,
)
from tsdaf.workflow.literature_fusion_actions import binder_from
from tsdaf.workflow.repair_target_names import RepairTargetNames
from tsdaf.workflow.naming_evidence import (
    DomainNaming,
    names_component,
    names_from_component,
)
from tsdaf.workflow.read_dispatch import (
    RecordTarget,
    SourceTarget,
    fragment_of,
    project_record_value,
    resolve_read_location,
)
from tsdaf.workflow.reading_skeleton import RepairedReading, RepairProjectionMemo
from tsdaf.workflow.reading_repair_mode import SPARSE, reading_repair_mode
from tsdaf.workflow.reading_repair_plan import (
    prepare_sparse_repair_preview, verify_sparse_repair_plan, sparse_reading_write_groups,
)
from tsdaf.workflow.resource_reads import (
    READ_BYTES,
    READ_ITEM_KEY,
    ReadFailure,
    ReadPlan,
    ReadValue,
    pointer_value,
    project_source_directory,
    resource_binding,
    resource_location,
    resource_receipt,
)
from tsdaf.workflow.source_navigation import authorized_visual_location
from tsdaf.workflow.state_resources import (
    STATE_LOCATION_HISTORY,
)
from tsdaf.workflow.text_search import SEARCH_LOCATION, merged_search, text_search_value
from tsdaf.workflow.tool_execution import (
    CallContext,
    FinishIntent,
    PreparedWrite,
    ToolConsumer,
    ToolLoopStalled,
    WriteOutcome,
    call_receipt,
    diagnostic_text,
    execute_tool_turns,
    lane_interaction_event,
    seal_artifact,
)
from tsdaf.workflow.tool_turns import (
    TOOL_REQUEST_INTENT_MEDIA,
    ToolTurnState,
    UncertainToolResponse,
)
from tsdaf.workflow.write_delivery import WriteDelivery, named_target, write_result
from tsdaf.workflow.write_registry import WriteRegistry, WriteTarget, item_key

INVESTIGATION_REJECTED_ERRORS = (ValueError, KeyError, TypeError, OSError)


class RelatedWriteFailure(ValueError):
    """Keep the original typed error and the member that produced it."""

    def __init__(self, member: str, error: Exception) -> None:
        self.member, self.error = member, error
        super().__init__(f"Related write {member} failed: {error}")


class PrerequisiteFailed(ValueError):
    def __init__(self, members: list[str]) -> None:
        self.members = members
        super().__init__(
            "PREREQUISITE_FAILED: this call's required write failed: " + ", ".join(members)
        )
# A supplement task reads most of a paper; with 24K characters the blocks it read first
# had left the window by the time it wrote, and it read them again (hyperfirin: seven
# blocks re-read twice over in thirty turns). At 48K the experimental-detail lane on a
# JOC paper (r5, E54) held a working set of some fifteen items -- the catalog, seven
# baseline blocks, five source blocks, the compounds, about 65K -- and cycled through
# them for thirty-three turns, each read pushing the next one out, before it wrote.
# The window holds the working set of the largest task seen; a turn spent re-reading
# costs the whole request again, which no window this size ever will.
INVESTIGATION_WINDOW_CHARS = 120_000
# Characters of step source the Host delivers with the task. This text sits in the first
# message, which the projection shows whole on every request and never ages, so it is
# paid for on every turn: half the window leaves the model's own reads the other half.
# The measured demand is far below it -- decumbenine_B's whole-paper task reaches five
# readable blocks and 7,018 characters, and each of its block tasks one block of 1.0 to
# 1.7 KB -- so the cap is a bound on a long route, not a routine trim.
INVESTIGATION_DELIVERY_CHARS = INVESTIGATION_WINDOW_CHARS // 2
# The same budget, per lane, now that an image is charged to the window
# (`content_window.ReadEntry.size`). The detail lane reads prose and no pixels -- its
# visual locations answer whole and the dispatch refuses a region there -- so it shows no
# picture again and needs no room for one. The supplement lane's subject IS the figures
# (it read the same four five times over in twenty-six turns, which is where
# DEFAULT_WINDOW_IMAGES came from), and four of decumbenine_B's overview PNGs are about
# 501,000 base64 characters; below that the first picture alone overflows the budget, and
# an entry that does not fit latches it shut for every older read behind it.
DETAIL_WINDOW_CHARS = INVESTIGATION_WINDOW_CHARS
# What the experimental sources' table of contents lists, and how much of each unit's
# first block it shows.
_DIRECTORY_KINDS = frozenset(
    {UnitKind.GENERAL_METHODS, UnitKind.PROCEDURE, UnitKind.CHARACTERIZATION, UnitKind.CRYSTALLOGRAPHY}
)
_DIRECTORY_HEAD_CHARS = 90
_FIGURE_MENTIONS = 10
# Said with a search answer that names a location instead of finding text.
NAMED_LOCATION_NOTE = (
    "These words are the NAME of something this task can read (a figure, a page, a block). "
    "A name is read, not searched: make the read call(s) given in read_next exactly as written. "
    "Searching for the same name again gives this same answer. "
    "To search the words inside the text instead, begin the terms with LITERAL."
)
# Said when the name is a figure's: the figure itself and the text that mentions it are
# two different things a reader may be after, and the answer carries both.
NAMED_FIGURE_NOTE = (
    "These words are the name of a figure this task can read. named_sources is the figure "
    "itself: read it with the call(s) in read_next unless this task already has. hits are "
    "the passages of text that mention this name."
)
DETAIL_WINDOW_IMAGES = 0
# Pictures no longer spend the character budget (`content_window.ReadEntry.text_size`).
SUPPLEMENT_WINDOW_CHARS = INVESTIGATION_WINDOW_CHARS
SUPPLEMENT_WINDOW_IMAGES = 4

# A read hands a selection back twice: as `selection_id` ("read:...") and as `location`,
# the same id under this prefix, which is the address a later read uses. The prefix is
# one rule for every side that meets it -- minting a location, reading one back, and a
# write citing one. The read side always resolved it; the write side did not, and on
# f08 three supplement tasks cited the location they had just been handed and had the
# items refused as citing content they were never given.
SELECTION_LOCATION_PREFIX = "selection:"


def _cited_as_selections(value: Any) -> Any:
    """A write's citations as selection ids, whichever of a read's two names it used."""
    if isinstance(value, dict):
        return {
            key: (
                [
                    ref.removeprefix(SELECTION_LOCATION_PREFIX) if isinstance(ref, str) else ref
                    for ref in item
                ]
                if key.endswith("selection_ids") and isinstance(item, list)
                else item.removeprefix(SELECTION_LOCATION_PREFIX)
                if key == "selection_id" and isinstance(item, str)
                else _cited_as_selections(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_cited_as_selections(item) for item in value]
    return value


# The collections that write the public containers, read off the answer model rather
# than retyped: everything but the narrative channel and the two bookkeeping
# collections. A collection joins by being declared (E157 added two, and the four
# lists that had enumerated seven by hand were one rule in four places).
CONTAINER_COLLECTIONS: tuple[str, ...] = tuple(
    name for name in ANSWER_COLLECTIONS if name not in ("coverage", "limitations", "narratives")
)


def writes_containers(answer: InvestigationAnswer) -> bool:
    """Whether an answer puts anything into the public containers."""
    return any(getattr(answer, name) for name in CONTAINER_COLLECTIONS)


# How far the Host widens a read that says why it is going back. The scope follows the
# reason and not the ask, which is the whole difference between this and letting a task
# roam: a passage cut mid-sentence needs the passage beside it and nothing else, and a
# candidate the task has not settled needs no more text at all -- it needs a decision.
# One reason cannot be traded for the paper.
_WIDENS_BY_ONE_BLOCK = frozenset({"PASSAGE_INCOMPLETE", "BOUNDARY_UNCERTAIN"})


def _read_reason(ctx: CallContext) -> str | None:
    """The reason this read gave for going back, read off the call the model made.

    Per item, because one read may go back for one passage and forward for another.
    """
    return getattr(ctx.read_item, "reason", None)


def adjacent_blocks_for(reason: str | None, *, asked_for: int) -> int:
    """The neighbouring blocks a reasoned read is answered with, never fewer than asked."""
    if reason in _WIDENS_BY_ONE_BLOCK:
        return max(asked_for, 1)
    return asked_for


def siblings_by_label(
    candidates: Sequence[Mapping[str, Any]], location: str
) -> list[tuple[str, str]]:
    """The other passages carrying the same author number as the one being read.

    What an attribution conflict needs is the alternatives: the task is saying that what
    this passage describes is not what its sheet says this step makes, and the question
    that answers is which other passage carries the number. The Host already found them
    -- `candidate_step_sources` returns every block a number titles -- so it sends them
    rather than leaving the task to page the directory for them.
    """
    here = next((c for c in candidates if c.get("location") == location), None)
    if here is None:
        return []
    return [
        (str(c["document_revision_id"]), str(c["source_id"]))
        for c in candidates
        if c.get("matched_label") == here.get("matched_label") and c.get("location") != location
    ]


class TaskContextTooLarge(ValueError):
    """Host capacity admission failed, before any provider request intent.

    Only counts and the measurement method enter the diagnostic, never input text.
    This remains an INPUT fault: resending the same request cannot repair it.
    """

    def __init__(self, key: str, size: int, limit: int) -> None:
        self.key, self.size, self.limit = key, size, limit
        self.receipt: InvestigationReceipt | None = None
        super().__init__(
            f"task request '{key}' estimates {size:,} input tokens against {limit:,} "
            "available after output reserve; conservative estimate, not exact token count"
        )


def check_context_capacity(prepared: Any) -> None:
    """Use the adapter's whole-request estimate without reimplementing its protocol."""
    capacity = prepared.context_capacity
    if capacity is None or capacity.get("estimated_input_tokens") is None:
        return  # No numerical claim for providers/modalities without a measurement.
    if capacity["request_sha256"] != hashlib.sha256(prepared.request_bytes).hexdigest():
        raise ValueError("capacity measurement does not bind this prepared request")
    if capacity["model_profile"] != prepared.model_profile:
        raise ValueError("capacity measurement does not bind this model profile")
    size = int(capacity["estimated_input_tokens"])
    limit = int(capacity["context_tokens"]) - int(capacity["output_reserve_tokens"])
    if size > limit:
        raise TaskContextTooLarge(str(capacity["method"]), size, limit)


def _natural_target(model: type[ContractModel], value: Any) -> str | None:
    """Encode the declared identity, including content, independently of a saved handle."""
    identity = identity_of(model)
    if identity.handle_only:
        return named_target(value, ("item_id",)) if value.get("item_id") else None
    return named_target(
        value,
        identity.keys,
        content_keys=frozenset(name for name, source in identity.fields if source == "CONTENT"),
    )


def _identity(model: type[ContractModel]) -> Callable[[Any], str | None]:
    """Name what an item targets: its handle when it quotes one, else its identifying fields.

    A failed write and its corrected resend must name the same target for the loop to
    pair them. A first write has no handle yet and is named by what it says it is; an
    update quoting a handle is named by the handle, however its fields changed.
    """

    def identity(value: Any) -> str | None:
        if isinstance(value, dict) and isinstance(value.get("item_id"), str) and value["item_id"]:
            return named_target(value, ("item_id",))
        return _natural_target(model, value)

    return identity


@dataclass(frozen=True)
class InFlightWrite:
    """One call's ephemeral prefix; no member is a sealed acceptance receipt."""

    baseline: ContractModel
    context: CallContext
    accepted: tuple[tuple[dict[str, Any], ...], ...] = ()
    pending: tuple[dict[str, Any], ...] = ()
    # Present only during validation of the complete, reconciled candidate.
    # Earlier construction callbacks must use the accepted prefix, not pending.
    candidate: ContractModel | None = None


class InvestigationWriter:
    """The accepted findings book, and the three write callbacks that change it.

    One accepted answer, one seal point, one place every save, withdrawal and
    finish reads and updates it. The scientific checks stay at the site that
    supplied them, injected as ``validate``.
    """

    def __init__(
        self,
        *,
        collections: dict[str, tuple[type[ContractModel], tuple[str, ...]]],
        write_collections: dict[str, tuple[type[ContractModel], tuple[str, ...]]],
        execution_id: str,
        seal: Callable[..., ArtifactRef],
        validate: Callable[..., None],
        register: Callable[..., Any],
        resource_locations: dict[str, Any],
        empty_answer: Callable[[], ContractModel] | None = None,
        read_answer: Callable[[bytes], ContractModel] | None = None,
        finish_model: type[FinishInvestigationCommand] = FinishInvestigationCommand,
        dependency_groups: Callable[..., list[list[str]] | ReadingWritePlan] | None = None,
        public_machine: Callable[[ContractModel], Mapping[str, Any] | None] | None = None,
        subject_names: Callable[[], Names] | None = None,
        bind_item: Callable[..., ContractModel] | None = None,
        item_feedback: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.collections = collections
        self.write_collections = write_collections
        self.registry = WriteRegistry(
            [
                WriteTarget(key, "per_item", _identity(model))
                for key, (model, _) in write_collections.items()
            ]
        )
        self.execution_id = execution_id
        self._seal = seal
        self._validate = validate
        self._register = register
        self._resource_locations = resource_locations
        self._empty_answer = empty_answer or (
            lambda: InvestigationAnswer(status="PARTIAL", explanation="Investigation in progress.")
        )
        self._read_answer = read_answer or type(self._empty_answer()).model_validate_json
        self._finish_model = finish_model
        self._dependency_groups = dependency_groups
        self._public_machine = public_machine
        self._subject_names = subject_names
        self._bind_item = bind_item
        self._item_feedback = item_feedback
        self._in_flight: InFlightWrite | None = None
        self.answer: ContractModel | None = None
        self.answer_ref: ArtifactRef | None = None
        # The answer `answer_ref` seals. The accepted answer is sealed when something
        # reads it -- a checkpoint, the finish -- and once per version: sealing every
        # accepted item wrote the whole answer again each time (a1: 178 copies, 7.8 MB,
        # for 9 checkpoints; batch 4 redundancy, 2026-09-24).
        self._sealed: ContractModel | None = None
        # Items whose own checks have passed, by object. Answers are frozen and a write
        # replaces one item, so an unchanged item is the same object in every later
        # candidate and its own checks are not run again; the checks that relate items
        # still run over the whole answer (`validate_reading`). In memory only: a
        # restored writer checks everything once.
        self._verified: dict[int, ContractModel] = {}
        # Each collection's saved items by identity, kept as items change; rebuilt when
        # the answer changed some other way.
        self._identities: dict[str, dict[str | None, ContractModel]] = {}
        self._identities_of: ContractModel | None = None

    @property
    def in_flight_write(self) -> InFlightWrite | None:
        """The existing validation/read callbacks may inspect this call's prefix."""
        return self._in_flight

    def _record_pending(
        self, key: str | None, raw: Any, item: ContractModel | None,
        *, detail: Mapping[str, Any], changed: bool,
    ) -> None:
        if self._in_flight is not None and key is not None:
            self._in_flight = replace(self._in_flight, pending=(
                *self._in_flight.pending,
                {"item": key, "submitted": copy.deepcopy(raw),
                 "value": item.model_dump(mode="json") if item is not None else None,
                 "location": detail.get("location"), "candidate_changed": changed,
                 "measurement_binding": copy.deepcopy(getattr(item, "_measurement_binding", None)),
                 **({"source_bindings": copy.deepcopy(detail["source_bindings"])}
                    if "source_bindings" in detail else {})},
            ))

    def bind_pending_repair(
        self, item: str, expected_value: Mapping[str, Any], command: Mapping[str, Any]
    ) -> None:
        """Attach validator-produced evidence to this exact pending candidate.

        This carries no authority by itself. Only a successfully returned outer
        batch can copy it into the existing private call receipt.
        """
        if self._in_flight is None or not item.startswith("skeleton_repairs:"):
            raise ValueError("repair binding requires its pending write candidate")
        found = [entry for entry in self._in_flight.pending if entry["item"] == item]
        if len(found) != 1 or found[0]["value"] != expected_value:
            raise ValueError("repair binding does not match the pending candidate")
        previous = found[0].get("repair_command")
        if previous is not None and previous != command:
            raise ValueError("one pending repair produced inconsistent command bindings")
        self._in_flight = replace(self._in_flight, pending=tuple(
            {**entry, "repair_command": copy.deepcopy(dict(command))} if entry["item"] == item else entry
            for entry in self._in_flight.pending
        ))

    def current(self) -> ContractModel:
        return self.answer or self._empty_answer()

    def restore_answer(self, payload: bytes) -> None:
        """Restore exactly the answer type whose original progress this task saves.

        A version whose location the restored checkpoints do not carry is registered
        here, once; accepting used to rescan every item on every save for this.
        """
        self.answer = self._read_answer(payload)
        self._sealed = self.answer
        self._verified = {}
        for collection in self.collections:
            for item in getattr(self.answer, collection, ()):
                self._register_version(collection, item)

    def sealed_answer_ref(self) -> ArtifactRef | None:
        """The accepted answer, sealed once per version, for whatever reads it."""
        if self.answer is not None and self.answer is not self._sealed:
            self.answer_ref = self._seal(
                self.answer.model_dump(mode="json"),
                "application/vnd.tsdaf.investigation-progress+json",
            )
            self._sealed = self.answer
        return self.answer_ref

    def _fresh(self, item: ContractModel) -> bool:
        return self._verified.get(id(item)) is not item

    def _register_version(
        self, collection: str, item: ContractModel, answer: ContractModel | None = None
    ) -> dict[str, Any]:
        """Register one saved version where a read or a withdrawal can name it."""
        value = self.finding(collection, item, answer)
        if value["location"] not in self._resource_locations:
            pinned = self._register(value, origin="SAVED_FINDING")
            self._resource_locations[value["location"]] = self._resource_locations[pinned]
        return value

    def accept(
        self,
        candidate: ContractModel,
        *,
        final: bool = False,
        changed: tuple[str, ContractModel] | None = None,
        changes: tuple[tuple[str, ContractModel], ...] = (),
    ) -> str:
        """Accept a candidate answer: validated, then its new version registered, then kept.

        The candidate is built from items already validated on their own; only what
        changed around them -- the top-level fields and the rules across items -- is
        validated again. Registration comes after validation, so a rejected version is
        never registered, and before the answer changes, so a version the host could
        not register is not saved.
        """
        registrations = (*changes, *((changed,) if changed is not None else ()))
        if isinstance(candidate, ReadingAnswer):
            candidate = reconcile_measurement_updates(self.current(), candidate, registrations)
        model = type(self._empty_answer())
        candidate = model.model_validate(
            {name: getattr(candidate, name) for name in model.model_fields}
        )
        unchanged = candidate == self.answer
        if changed is not None and self._in_flight is not None:
            # A single-item outcome compares the reconciled answer. Atomic
            # groups deliberately retain their per-construction item outcomes.
            self._in_flight = replace(self._in_flight, pending=tuple(
                {**entry, "candidate_changed": not unchanged}
                for entry in self._in_flight.pending
            ))
        if self._in_flight is not None:
            self._in_flight = replace(self._in_flight, candidate=candidate)
        try:
            self._validate(candidate, final=final, fresh=self._fresh)
        finally:
            if self._in_flight is not None:
                self._in_flight = replace(self._in_flight, candidate=None)
        if unchanged:
            return "NO_CHANGE"
        # Derive actual final changes: a compatibility update can also change the
        # counterpart's collection. Never register intermediate or removed versions.
        registrations = tuple(
            (collection, item)
            for collection in self.collections
            for item in getattr(candidate, collection, ())
            if item not in getattr(self.answer, collection, ())
            or self.finding(collection, item, candidate) != self.finding(collection, item)
        )
        locations_before = dict(self._resource_locations)
        try:
            for collection, item in registrations:
                self._register_version(collection, item, candidate)
        except Exception:
            self._resource_locations.clear()
            self._resource_locations.update(locations_before)
            raise
        self.answer = candidate
        self._identities = {}
        self._identities_of = None
        self._verified = {
            id(item): item
            for collection in self.collections
            for item in getattr(candidate, collection, ())
        }
        return "SAVED"

    def _identity_index(self, collection: str) -> dict[str | None, ContractModel]:
        if self._identities_of is not self.answer:
            self._identities = {}
            self._identities_of = self.answer
        if collection not in self._identities:
            model, _ = self.write_collections[collection]
            self._identities[collection] = {
                _natural_target(model, entry.model_dump(mode="json")): entry
                for entry in getattr(self.current(), collection)
            }
        return self._identities[collection]

    def finding(
        self, collection: str, item: ContractModel, answer: ContractModel | None = None
    ) -> dict[str, Any]:
        """One saved item as the model sees it: its handle, this version's location, its value.

        The handle is the item's identity across edits; the location names this exact
        version of it and changes with every accepted edit. Both are the host's.
        """
        value = item.model_dump(mode="json")
        binding = {}
        if isinstance(getattr(item, "explanation", None), FindingExplanationRef):
            state = answer if answer is not None else self.current()
            linked = next((f for f in state.findings if f.finding_id == item.finding), None)
            if linked is not None:
                binding = {
                    "explanation_binding": {
                        "finding": linked.finding_id,
                        "finding_version": content_hash(linked.model_dump(mode="json")),
                        "resolved_explanation": current_ruling_view(item, state)[
                            "resolved_explanation"
                        ],
                    }
                }
        identity = [self.execution_id, collection, value, *([binding] if binding else [])]
        return {
            "item_id": value.get("item_id"),
            "location": "finding:" + content_hash(identity),
            "collection": collection,
            "value": value,
            **binding,
        }

    def _handle(self, collection: str, item: ContractModel, existing: list[Any], entry_key=None) -> str:
        """The handle a new item is issued: from what it is at first save, then kept.

        A handle names one saved item. An item renamed away from its first identity keeps
        its handle, so a later new item under that identity must not be issued the same
        one: the mint carries a sequence past every handle the book holds. An item that
        was withdrawn and later said again is issued its old handle, as the same item.
        """
        model, keys = self.collections[collection]
        if identity_of(model).handle_only:
            if self._in_flight is None or entry_key is None:
                raise ValueError("a new account needs its host-bound write action")
            ctx = self._in_flight.context
            return "item:" + content_hash([
                self.execution_id, collection, ctx.interaction_sha256, ctx.call_index, entry_key
            ])[:24]
        identity = {key: item.model_dump(mode="json")[key] for key in keys}
        taken = {getattr(entry, "item_id", None) for entry in existing}
        for sequence in itertools.count():
            seed = [self.execution_id, collection, identity, *([sequence] if sequence else [])]
            handle = "item:" + content_hash(seed)[:24]
            if handle not in taken:
                return handle
        raise AssertionError("unreachable")

    def findings(self) -> list[dict[str, Any]]:
        return [
            self.finding(collection, item)
            for collection in self.collections
            for item in getattr(self.answer, collection, ())
        ]

    def targets(self, name: str, raw: Any) -> dict[str, str | None]:
        """What each item of a write targets, so a failure and its correction pair up.

        An item that names a saved item by its fields targets that item's handle, exactly
        as one quoting the handle would: one saved item, one target, however it is named.
        Only an item that matches nothing saved is targeted by what it says it is -- which
        includes an item created by an earlier item of the same write, since targets are
        named before the write is applied; a failure there pairs with a resend by fields.
        """
        if not isinstance(raw, dict):
            return {}
        found = self.registry.item_targets(raw)
        for collection, (model, _) in self.write_collections.items():
            values = raw.get(collection, [])
            if collection == "withdrawals" or not isinstance(values, list):
                continue
            saved = {
                identity: getattr(entry, "item_id", None)
                for identity, entry in self._identity_index(collection).items()
            }
            for index, value in enumerate(values):
                if not isinstance(value, dict) or value.get("item_id"):
                    continue
                handle = saved.get(_natural_target(model, value))
                if handle:
                    found[item_key(collection, index)] = named_target(
                        {"item_id": handle}, ("item_id",)
                    )
        return found

    def apply_write(
        self,
        name: str,
        raw: dict[str, Any],
        ctx: CallContext,
        write_targets: dict[str, str | None],
        repairs: dict[str, list[str]],
    ) -> WriteOutcome:
        """Each independent item/group is checked with this call's candidate metadata."""
        if self._in_flight is not None:
            raise ValueError("a write cannot begin inside an unfinished write")
        prepared = getattr(ctx, "prepared_write", None)
        self._edit_states = {
            id(raw[c][int(i)]): state
            for key, state in (prepared.candidate_semantics.items() if prepared else ())
            for c, i in [key.split(":")]
        }
        self._in_flight = InFlightWrite(self.current(), ctx)
        try:
            return self._apply_write(name, raw, ctx, write_targets, repairs)
        finally:
            self._edit_states = {}
            self._in_flight = None

    def _apply_write(self, name, raw, ctx, write_targets, repairs) -> WriteOutcome:
        self._check_shape(raw)
        prepared = getattr(ctx, "prepared_write", None)
        initial_order = {
            c: [getattr(item, "item_id", None) for item in getattr(self.current(), c, ())]
            for c in self.collections
        }
        rows: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        grouped: dict[str, list[str]] = {}
        dependencies: dict[str, tuple[str, ...]] = {}
        plan = self._dependency_groups(raw, self.findings()) if self._dependency_groups else None
        groups = plan.atomic_groups if isinstance(plan, ReadingWritePlan) else plan or ()
        if isinstance(plan, ReadingWritePlan):
            dependencies = plan.prerequisites
        for group in groups:
            for key in group:
                grouped[key] = list(group)
        outcomes: dict[str, dict[str, Any] | Exception] = {}
        visiting: set[str] = set()
        accepted_item_batches: list[list[str]] = []

        def apply_required(key: str) -> None:
            if key in outcomes:
                return
            group = grouped.get(key, [key])
            if any(member in visiting for member in group):
                error = ValueError("cyclic write prerequisites: " + ", ".join(group))
                outcomes.update({member: error for member in group})
                return
            visiting.update(group)
            required = list(dict.fromkeys(parent for member in group
                for parent in dependencies.get(member, ()) if parent not in group))
            for parent in required:
                apply_required(parent)
            failed = [parent for parent in required if isinstance(outcomes.get(parent), Exception)]
            try:
                if failed:
                    raise PrerequisiteFailed(failed)
                for member in group:
                    if prepared and member in prepared.errors:
                        raise RelatedWriteFailure(member, ValueError(prepared.errors[member]))
                if len(group) > 1:
                    accepted = self._apply_group(group, raw)
                    outcomes.update(accepted)
                else:
                    collection, index = key.split(":")
                    accepted = {key: self._apply_one(collection, raw[collection][int(index)], _key=key)}
                    outcomes.update(accepted)
                accepted_item_batches.append(list(accepted))
                if self._in_flight is not None:
                    self._in_flight = replace(self._in_flight, accepted=(
                        *self._in_flight.accepted, tuple(
                            {**entry, "saved": copy.deepcopy(accepted[entry["item"]])}
                            for entry in self._in_flight.pending
                        )), pending=())
            except INVESTIGATION_REJECTED_ERRORS as error:
                outcomes.update({member: error for member in group})
            finally:
                if self._in_flight is not None:
                    self._in_flight = replace(self._in_flight, pending=())
                visiting.difference_update(group)

        for collection in self.write_collections:
            for index, _value in enumerate(raw.get(collection, [])):
                key = item_key(collection, index)
                diagnostic: Any = None
                apply_required(key)
                entry = outcomes[key]
                if isinstance(entry, Exception):
                    detail = {"outcome": "REJECTED"}
                    error = entry.error if isinstance(entry, RelatedWriteFailure) else entry
                    origin = (
                        entry.member.split(":")[0]
                        if isinstance(entry, RelatedWriteFailure) else collection
                    )
                    diagnostic = diagnostic_text(
                        error, model=self.write_collections[origin][0], collection=origin
                    )
                    if isinstance(entry, RelatedWriteFailure):
                        detail["related_item"] = entry.member
                    if isinstance(entry, PrerequisiteFailed):
                        detail["blocked_by"] = entry.members
                    if isinstance(error, UnknownReference) and ctx.naming is not None:
                        detail.update(ctx.naming.diagnose_reference(error))
                else:
                    detail = entry
                items.append({"item": key, "collection": collection, **detail})
                if diagnostic is not None:
                    items[-1]["diagnostic"] = diagnostic
                rows.append(
                    write_result(
                        interaction_sha256=ctx.interaction_sha256,
                        call_index=ctx.call_index,
                        item=key,
                        tool=name,
                        target=write_targets.get(key),
                        outcome=detail["outcome"],
                        repairs=repairs.get(key, []),
                        diagnostic=diagnostic,
                    )
                )
        if plan is not None:
            candidate = self.current()
            for collection, prior_handles in initial_order.items():
                ordered_handles = list(
                    dict.fromkeys(
                        [
                            *prior_handles,
                            *(
                                item["item_id"]
                                for item in items
                                if item["collection"] == collection
                                and item.get("item_id") is not None
                            ),
                        ]
                    )
                )
                current_items = getattr(candidate, collection, ())
                ranks = {handle: i for i, handle in enumerate(ordered_handles)}
                ordered_items = tuple(
                    sorted(
                        current_items,
                        key=lambda item: ranks.get(getattr(item, "item_id", None), len(ranks)),
                    )
                )
                candidate = candidate.model_copy(update={collection: ordered_items})
            self.accept(candidate)
        changed = any(row["outcome"] in ("SAVED", "WITHDRAWN") for row in rows)
        rejected = any(row["outcome"] == "REJECTED" for row in rows)
        outcome = (
            "PARTIAL"
            if rejected and changed
            else "REJECTED"
            if rejected
            else "SAVED"
            if changed
            else "NO_CHANGE"
        )
        semantics = {}
        from tsdaf.lanes.literature.local_edits import supports_edit
        for row in items:
            if row["outcome"] in {"SAVED", "NO_CHANGE"} and supports_edit(
                self.collections[row["collection"]][0]
            ):
                saved = next(i for i in getattr(self.current(), row["collection"])
                             if i.item_id == row["item_id"])
                semantics[row["item"]] = semantic_state(saved)
        measurement_bindings = {
            entry["item"]: entry["measurement_binding"]
            for batch in (self._in_flight.accepted if self._in_flight is not None else ())
            for entry in batch if entry["item"].startswith("findings:")
            and entry["measurement_binding"] is not None
        }
        repair_commands = {
            entry["item"]: entry["repair_command"]
            for batch in (self._in_flight.accepted if self._in_flight is not None else ())
            for entry in batch if "repair_command" in entry
        }
        return WriteOutcome({"outcome": outcome, "items": items}, rows,
                            audit={"accepted_item_batches": accepted_item_batches,
                                   **({"measurement_bindings": measurement_bindings} if measurement_bindings else {}),
                                   **({"repair_command_version": REPAIR_COMMAND_VERSION, "repair_commands": repair_commands}
                                      if repair_commands else {}),
                                   **({"item_semantics": semantics} if semantics else {})})

    def _check_shape(self, raw: dict[str, Any]) -> None:
        """A malformed envelope is one rejection, not a silent partial write."""
        write_shape(raw, collections=tuple(self.write_collections))

    def _candidate_one(
        self,
        candidate: ContractModel,
        collection: str,
        value: Any,
        identity_index: dict[str | None, ContractModel] | None = None,
        entry_key: str | None = None,
    ) -> tuple[ContractModel, ContractModel | None, dict[str, Any]]:
        model, keys = self.write_collections[collection]
        item = model.model_validate_json(canonical_json_bytes(_cited_as_selections(value)))
        if collection == "withdrawals":
            return self._withdraw_candidate(candidate, cast(WithdrawFinding, item))
        if isinstance(item, OpenNarrative):
            item = bind_narrative_subjects(
                item,
                self._public_machine(candidate) if self._public_machine else None,
                self._subject_names().names if self._subject_names else (),
            )
        if isinstance(item, OpenNameRuling):
            item = bind_ruling_owner(item, cast(ReadingAnswer, candidate))
        domain_model = self.collections[collection][0]
        if type(item) is not domain_model:
            if not isinstance(item, domain_model):
                raise TypeError("write profile must refine its accepted domain model")
            # The profile DTO has already enforced the provider's restrictions.
            # Store the same domain class used by restore, without serializing away
            # nested explicit/omitted fields or historical private compilation state.
            normalized = domain_model.model_construct(
                _fields_set=set(item.model_fields_set),
                **{name: getattr(item, name) for name in domain_model.model_fields},
            )
            if item.__pydantic_private__:
                object.__setattr__(
                    normalized, "__pydantic_private__", dict(item.__pydantic_private__)
                )
            item = normalized
        existing = list(getattr(candidate, collection))
        index = identity_index if identity_index is not None else self._identity_index(collection)

        def natural(entry: ContractModel) -> str | None:
            return _natural_target(model, entry.model_dump(mode="json"))

        identity = natural(item)
        quoted = getattr(item, "item_id", None)
        if quoted is None:
            # A new item, or the same item said again: what it says it is decides.
            match = index.get(identity)
            handle = getattr(match, "item_id", None) or self._handle(collection, item, existing, entry_key)
        else:
            # An update by handle: the handle decides, whatever the fields now say.
            match = next((e for e in existing if getattr(e, "item_id", None) == quoted), None)
            if match is None:
                if identity_of(model).handle_only:
                    raise ValueError("unknown saved account: select an item_id from current, "
                                     "or omit item_id to create a new account")
                # In this collection's own identity: it named the ids of six retired
                # collections to tasks whose declarations had none of them.
                raise ValueError(
                    f"unknown item_id {quoted!r} under {collection}: the host issues item_id when "
                    "an item is first saved. To change a saved item, send it again without "
                    f"item_id under the same {' and '.join(keys)}; omit item_id for a new item."
                )
            handle = quoted
            other = index.get(identity)
            if other is not None and other is not match:
                raise ValueError(
                    "another saved item already has this identity; update that one by its "
                    "item_id, or withdraw it first"
                )
        if isinstance(item, Finding) and isinstance(match, Finding):
            item = inherit_finding_scope(item, match, cast(type[Finding], model))
        if collection == "records" and match is not None and "selection_ids" not in value:
            # Omission is an identity-preserving edit, not an instruction to drop sources.
            item = item.model_copy(update={"selection_ids": match.selection_ids})
        state = getattr(self, "_edit_states", {}).get(id(value))
        if state is not None:
            restore_semantic_state(item, state, exact=False)
        item = item.model_copy(update={"item_id": handle})
        if self._bind_item is not None and self._in_flight is not None:
            item = self._bind_item(collection, item, match, self._in_flight, entry_key)
        updated = [item if e is match else e for e in existing]
        if match is None:
            updated.append(item)
        candidate = candidate.model_copy(update={collection: tuple(updated)})
        feedback = self._item_feedback(collection, item, match, self._in_flight, entry_key) \
            if self._item_feedback is not None else {}
        return candidate, item, {**self.finding(collection, item, candidate), **feedback}

    def _apply_one(self, collection: str, value: Any, *, _key: str | None = None) -> dict[str, Any]:
        index = self._identity_index(collection) if collection != "withdrawals" else None
        before = self.current()
        candidate, item, detail = self._candidate_one(before, collection, value, index, entry_key=_key)
        self._record_pending(_key, value, item, detail=detail, changed=candidate != before)
        outcome = self.accept(candidate, changed=(collection, item) if item is not None else None)
        if item is not None:
            saved = next(i for i in getattr(self.answer, collection) if i.item_id == item.item_id)
            detail = {**detail, **self.finding(collection, saved)}
        return {"outcome": "WITHDRAWN" if item is None else outcome, **detail}

    def _update_identity(
        self, index: dict[str | None, ContractModel], collection: str, item: ContractModel
    ) -> None:
        for key, previous in list(index.items()):
            if getattr(previous, "item_id", None) == getattr(item, "item_id", None):
                index.pop(key)
        model = self.write_collections[collection][0]
        index[_natural_target(model, item.model_dump(mode="json"))] = item

    def _apply_group(self, group: list[str], raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
        # Build in original submission order; only the final connected candidate is visible.
        candidate = self.current()
        changes = []
        replies = {}
        local_indexes = {c: dict(self._identity_index(c)) for c in self.collections}
        ordered = sorted(
            group,
            key=lambda key: (
                list(self.write_collections).index(key.split(":")[0]),
                int(key.split(":")[1]),
            ),
        )
        for key in ordered:
            collection, index = key.split(":")
            before = candidate
            try:
                candidate, item, detail = self._candidate_one(
                    candidate,
                    collection,
                    raw[collection][int(index)],
                    local_indexes.get(collection), entry_key=key,
                )
            except INVESTIGATION_REJECTED_ERRORS as error:
                raise RelatedWriteFailure(key, error) from error
            self._record_pending(
                key, raw[collection][int(index)], item,
                detail=detail, changed=candidate != before,
            )
            outcome = (
                "NO_CHANGE" if candidate == before else "WITHDRAWN" if item is None else "SAVED"
            )
            replies[key] = {"outcome": outcome, **detail}
            if item is not None:
                changes.append((collection, item))
                self._update_identity(local_indexes[collection], collection, item)
            elif item is None:
                # Explicit withdrawals can affect any collection in this component.
                for name, identities in local_indexes.items():
                    remaining = {getattr(v, "item_id", None) for v in getattr(candidate, name, ())}
                    for identity, previous in list(identities.items()):
                        if getattr(previous, "item_id", None) not in remaining:
                            identities.pop(identity)
        self.accept(candidate, changes=tuple(changes))
        for key, reply in replies.items():
            if reply.get("item_id") and reply["outcome"] != "WITHDRAWN":
                collection = key.split(":")[0]
                saved = next(
                    i for i in getattr(self.answer, collection) if i.item_id == reply["item_id"]
                )
                replies[key] = {**reply, **self.finding(collection, saved)}
        return replies

    def _withdraw_candidate(
        self, candidate: ContractModel, withdrawal: WithdrawFinding
    ) -> tuple[ContractModel, None, dict[str, Any]]:
        def named(finding: dict[str, Any]) -> bool:
            by_handle = withdrawal.item_id is None or finding["item_id"] == withdrawal.item_id
            by_location = withdrawal.location is None or finding["location"] == withdrawal.location
            return by_handle and by_location

        withdrawn = next(
            (
                self.finding(c, f)
                for c in self.collections
                for f in getattr(candidate, c, ())
                if named(self.finding(c, f))
            ),
            None,
        )
        if withdrawn is None:
            raise ValueError(
                "no saved finding has this item_id and location: the location may be stale, "
                "or the two name different items. A handle is returned in the reply that "
                "saved the item; a read of 'current' shows every saved item with its handle "
                "and current location"
            )
        remaining = tuple(
            existing
            for existing in getattr(candidate, withdrawn["collection"])
            if existing.model_dump(mode="json") != withdrawn["value"]
        )
        return (
            candidate.model_copy(update={withdrawn["collection"]: remaining}),
            None,
            {"location": withdrawal.location},
        )

    def _withdraw(self, withdrawal: WithdrawFinding) -> dict[str, Any]:
        candidate, _, detail = self._withdraw_candidate(self.current(), withdrawal)
        self.accept(candidate)
        return {"outcome": "WITHDRAWN", **detail}

    def finish(self, name: str, raw: dict[str, Any], ctx: CallContext) -> FinishIntent:
        return self._conclude(self._finish_model.model_validate_json(canonical_json_bytes(raw)))

    def close_out(self, reason: str) -> FinishIntent:
        """Close execution without impersonating a scientific finish."""
        return FinishIntent(disposition="PARTIAL", note=reason)

    def _conclude(self, conclusion: FinishInvestigationCommand) -> FinishIntent:
        def accept_final() -> None:
            self.accept(
                self.current().model_copy(update=conclusion.model_dump(exclude={"disposition"})),
                final=True,
            )

        return FinishIntent(
            disposition=conclusion.disposition,
            note=conclusion.explanation,
            accept=accept_final,
        )


def source_situation_note(sources: LiteratureSources) -> str:
    """What the Host knows about where this paper's experimental detail can be, said
    to the detail investigation in one sentence (ledger E76 (5), E83: the same three
    situations the route group is told about, from the sources the lane was given
    rather than guessed): the main text's experimental part and a registered
    supplement, the part alone, or the supplement alone."""
    # The part, not everything readable: a step task also reads the paragraphs its
    # steps were built from (E142), and those are the route's narrative, not an
    # experimental part the main text does not have.
    main_blocks = len(sources.main.part_ids)
    supplements = len(sources.active_si)
    part = f"the main text's experimental part ({main_blocks} blocks)"
    beyond = len(sources.main.step_block_ids)
    steps = (
        f" Another {beyond} main-text block{'s' if beyond > 1 else ''} from upstream "
        "narrative units or cited sources are readable on demand."
        if beyond
        else ""
    )
    units = sources.main.units
    known = units.has_experimental_part if units is not None else None
    if known is None:
        scope = "MAIN experimental scope has not been established by complete upstream segmentation"
    elif known:
        scope = part
    else:
        scope = "upstream classified MAIN without an experimental section"
    return (
        f"Sources: {scope}; {supplements} supporting-information documents are registered. "
        "Registration and delivery describe available inputs, not scientific conclusions."
        f"{steps}"
    )


def located_source_parts(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Name each returned passage by the saved selection it can be read back at.

    The reader binds the selection; this is the one place that turns that binding into
    the ``location``/``origin`` a read reply carries, so a passage the model asked for
    and a passage the Host delivered with the task are spelled the same way and the
    content window recognizes one as the other.
    """
    for part in parts:
        if part["type"] == "text":
            value = json.loads(part["text"])
            if "selection_id" in value:
                value.update(
                    location=SELECTION_LOCATION_PREFIX + value["selection_id"], origin="SOURCE"
                )
                part["text"] = canonical_json_bytes(value).decode()
    return parts


def delivered_source_evidence(
    row: dict[str, Any],
    parts: Sequence[dict[str, Any]],
    *,
    selections: Mapping[str, Mapping[str, Any]],
    location_for: Callable[[str, str], str | None],
    sentences_for: Callable[[Mapping[str, Any]], list[dict[str, Any]]],
) -> tuple[SourceDelivery, ...]:
    """Project real source deliveries through their existing verified bindings.

    This is shared by initial passages, tool reads (including continuations), and
    inherited located text. Navigation and model work cannot claim source coverage.
    The transcript remains the delivery record; bindings alone never imply a read.
    """
    if row.get("outcome") != "READ" or row.get("origin") != "SOURCE":
        return ()
    found = []
    for part in parts:
        text = part.get("text")
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        structured = isinstance(payload, dict) and payload.get("origin") == "SOURCE"
        sid = payload.get("selection_id") if structured else row.get("selection_id")
        if not isinstance(sid, str):
            continue
        binding = selections.get(sid)
        if binding is None:
            continue
        identity = binding["source_identity"]
        location = location_for(identity["document_revision_id"], identity["source_id"])
        if location is None:
            continue
        first, last = identity.get("start_sentence"), identity.get("end_sentence")
        expected_sentences = (
            sentences_for(identity) if type(first) is int and type(last) is int else []
        )
        expected_by_number = {sentence["sentence"]: sentence for sentence in expected_sentences}
        complete = False
        if structured:
            sentences = payload.get("numbered_sentences")
            if not isinstance(sentences, list):
                continue
            numbers = frozenset(
                sentence["sentence"]
                for sentence in sentences
                if isinstance(sentence, dict)
                and type(sentence.get("sentence")) is int
                and isinstance(sentence.get("text"), str)
                and sentence["text"].strip()
            )
            if len(numbers) != len(sentences):
                continue
            if any(
                expected_by_number.get(sentence["sentence"]) != sentence for sentence in sentences
            ):
                continue
            complete = bool(sentences) and sentences == expected_sentences
            if not numbers and not (
                payload.get("figure_reading") is not None or payload.get("display") is not None
            ):
                continue
            if not numbers:
                answer = binding.get("figure_reading")
                image = binding.get("display", {}).get("image_ref", {}).get("sha256")
                complete = (
                    isinstance(answer, str) and payload.get("figure_reading") == answer
                ) or (
                    isinstance(image, str)
                    and any(p.get("artifact_ref", {}).get("sha256") == image for p in parts)
                )
        else:
            # The middle layer receives the exact located text under its selection
            # header. A paragraph's interpretation/attribution alone is not evidence.
            if not text.startswith(f"[{sid}]"):
                continue
            body = text.partition("\n")[2]
            expected = binding.get("exact_text") or binding.get("figure_reading")
            if not isinstance(expected, str) or not expected or body != expected:
                continue
            numbers = frozenset(expected_by_number)
            complete = True
        found.append(
            SourceDelivery(location, SELECTION_LOCATION_PREFIX + sid, sid, numbers, complete)
        )
    return tuple(found)


def _tag_candidate_parts(parts: list[dict[str, Any]], candidate: Mapping[str, Any]) -> None:
    """Stamp a delivered passage with the candidate that already claims it.

    `candidates_left_unanswered` names an unmet candidate by its matched_label in a
    sentence of its own (``source_note``); the passage that label is about sat beside
    that sentence in the same message, but under no name the two could be joined by.
    Two live runs (f06 and f07 detail-B009, the same candidate, `MAIN-P0060-B0000`
    labelled ``23``) each spent a turn on `candidates_left_unanswered` before finding
    the selection_id to cite. Putting the label on the passage itself removes the
    lookup rather than improving the message that follows the miss.
    """
    for part in parts:
        if part["type"] != "text":
            continue
        value = json.loads(part["text"])
        if "selection_id" not in value:
            continue
        value["matched_label"] = candidate["matched_label"]
        if "unit_kind" in candidate:
            value["unit_kind"] = candidate["unit_kind"]
        part["text"] = canonical_json_bytes(value).decode()


# A record the host pins where a read can name it again (`resource_location`), and the
# digests a read of one reports (`resource_receipt`).
_RECORD = re.compile(r"result:[0-9a-f]{32}")
RECORD, VERSION = "record", "record_version"


@dataclass(frozen=True)
class RecordNames:
    """The names of the records a task was handed, and of their versions."""

    locations: Names
    versions: Names

    def present(self, value: Any) -> Any:
        return self.versions.present(self.locations.present(value))

    def resolve(self, value: Any) -> Any:
        """Every record named where a read names a location, at its pointer too; a
        version is never written back."""
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        return {
            key: at_pointer(self.locations, self.locations.resolve({key: item})[key])
            if key == "location" and isinstance(item, str)
            else self.resolve(item)
            for key, item in value.items()
        }


def record_names(
    locations: Mapping[str, Mapping[str, Any]],
    history: Sequence[dict[str, Any]],
    *,
    seed: RecordNames | None = None,
) -> RecordNames:
    """What the records a task read are called (batch 2A, 2d).

    A read of a record answers at the location the host pins it at, ``result:`` and a
    hash of its binding, with the digests of the whole it came from (``version``) and of
    what came back (``content_version``). The model copies the location back to read the
    record again and never sends a digest back. Each is named from the binding the host
    keeps (``locations``), in the order the task was first shown it (``history``), so a
    name does not move between turns or when a task resumes:

    - a record handed over at a location of its own is named by that location as the
      task is shown it: a route group's result by "main:0", a saved version by its
      handle's name, "item 2 version 1" (`handle_names`);
    - what "current" held when it was read, which moves, by the order it was read, as a
      saved version is: "current version 2";
    - a part of one, read at a pointer or a selection, by what it was read from, as the
      fusion names its own: "main:0 as read";
    - a digest by the record it is the content of, a version of "main:0" as "main:0".

    Two records that would share a name take their order in brackets.
    """
    handles = handle_names((), history)
    wholes: dict[str, str] = {}
    for location, offered in sorted(locations.items()):
        shown = handles.present(location)
        if not offered["derivation"]:
            label = offered.get("source_navigation")
            if isinstance(label, str):
                wholes.setdefault(str(offered["root_ref"]["sha256"]), label)
            elif offered.get("public_object_snapshot"):
                wholes.setdefault(str(offered["root_ref"]["sha256"]), "public result")
            elif not minted(shown):
                wholes.setdefault(str(offered["root_ref"]["sha256"]), shown)
    names = Namer(seed.locations if seed is not None else None)
    versions = Namer(seed.versions if seed is not None else None)
    current = 0
    for token in first_shown(history, _RECORD):
        binding = locations.get(token)
        if binding is None:
            continue
        root = str(binding["root_ref"]["sha256"])
        if root not in wholes:
            if binding["origin"] != "CURRENT_FINDINGS":
                continue
            current += 1
            wholes[root] = f"current version {current}"
        name = names.give(
            token, wholes[root] + (" as read" if binding["derivation"] else ""), RECORD
        )
        versions.give(root, wholes[root], VERSION)
        versions.give(str(binding["artifact_ref"]["sha256"]), name, VERSION)
        if (
            binding.get("public_object_snapshot")
            and isinstance(binding.get("science_version"), str)
        ):
            versions.give(binding["science_version"], name + " content", VERSION)
    # A directory is a navigation reply, not a sealed content fragment. Its
    # object version is recorded in the read receipt rather than a new binding.
    for entry in reversed(read_entries(history)):
        row = entry.row
        root = str(row.get("location", "")).partition("#")[0]
        binding = locations.get(root, {})
        if (
            row.get("outcome") == "DIRECTORY"
            and binding.get("public_object_snapshot")
            and isinstance(row.get("version"), str)
        ):
            versions.give(
                row["version"], names.names.get(root, "public result") + " content", VERSION
            )
    pointed(names, RECORD)
    return RecordNames(
        Names.of(
            names.names,
            names.families,
            field_families={"location": frozenset({RECORD})},
            reserved=tuple(names.reserved),
        ),
        Names.of(versions.names, versions.families, reserved=tuple(versions.reserved)),
    )


def investigation_domain(
    task: Names,
    records: RecordNames,
    groups: GroupNames,
    owner_by_location: Mapping[str, str | None],
    *,
    repair_targets: RepairTargetNames | None = None,
) -> DomainNaming:
    """The actual composite view used by one investigation request and its calls."""

    def owner(location: str) -> str | None:
        return owner_by_location.get(location.partition(POINTER)[0])

    def present(visible: Any) -> Any:
        if repair_targets is not None:
            visible = repair_targets.present(visible)
        return task.present(records.present(groups.present(visible)))

    def resolve(raw: Any) -> Any:
        value = task.resolve(groups.resolve(records.resolve(raw), owner))
        return repair_targets.resolve(value) if repair_targets is not None else value

    return DomainNaming(
        present,
        resolve,
        {
            "kind": "investigation",
            "task": names_component(task),
            "records_locations": names_component(records.locations),
            "records_versions": names_component(records.versions),
            "groups_units": names_component(groups.units),
            "groups_tag_pattern": groups.tags.pattern if groups.tags is not None else None,
            "groups_binders": {
                unit: {"prefix": prefix, "bound": list(bound)}
                for unit, (prefix, bound) in groups.binder_specs.items()
            },
            "owner_by_location": dict(owner_by_location),
            **({"repair_targets": repair_targets.component()} if repair_targets is not None else {}),
        },
    )


def restore_investigation_domain(value: dict[str, Any]) -> DomainNaming:
    if value.get("kind") != "investigation":
        raise ValueError("not an investigation naming view")
    records = RecordNames(
        names_from_component(value["records_locations"]),
        names_from_component(value["records_versions"]),
    )
    specs = {
        unit: (entry["prefix"], tuple(entry["bound"]))
        for unit, entry in value["groups_binders"].items()
    }
    groups = GroupNames(
        names_from_component(value["groups_units"]),
        re.compile(value["groups_tag_pattern"]) if value["groups_tag_pattern"] else None,
        {unit: binder_from(prefix, set(bound)) for unit, (prefix, bound) in specs.items()},
        specs,
    )
    return investigation_domain(
        names_from_component(value["task"]),
        records,
        groups,
        value["owner_by_location"],
        repair_targets=RepairTargetNames.from_component(value["repair_targets"])
        if "repair_targets" in value else None,
    )


def investigate_sources(
    *,
    kind: str,
    sources: LiteratureSources,
    execution_id: str,
    input_ref: ArtifactRef,
    context: dict[str, Any],
    store: PrivateArtifactStore,
    client: LaneInteractionClient,
    # The figure reader's own stream. It is a second client and not this task's, because
    # a V3 turn happens between two of this task's turns: sharing one transport would
    # interleave the two conversations, and on a recorded fixture it would consume the
    # task's next recorded answer. Without one, this task reads prose and nothing else.
    figure_client: LaneInteractionClient | None = None,
    call_budget: CallBudget | None = None,
    provider_binding: ProviderBinding,
    bind: Callable[..., None],
    baseline_machine_ref: ArtifactRef | None = None,
    machine: dict[str, Any] | None = None,
    emit_event: Callable[[str, dict[str, Any]], None] | None = None,
    observations: tuple[dict[str, Any], ...] = (),
    allowed_main_visual_ids: tuple[str, ...] = (),
    recovery_artifacts: tuple[ArtifactRef, ...] = (),
    block_ids: tuple[str, ...] | None = None,
    # Whether one task per reaction block is running beside this one. It cannot be read
    # off `block_ids`: a paper-scoped task has none either way, and what it may write
    # depends on whether its neighbours exist (E112).
    shares_the_round: bool = False,
    inherited_selections: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    first_layer_source_selections: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    first_layer_profiles: Mapping[str, MachineProfile] | None = None,
) -> InvestigationReceipt:
    from tsdaf.lanes.literature.investigation_kinds import new_investigation_kind
    spec = new_investigation_kind(kind,machine_profile(machine) if machine is not None else None)
    if machine is not None:
        machine_profile(machine).require_production()
    if kind == 'EXPERIMENTAL_MAPPING':
        from tsdaf.workflow.final_science_mapping import map_final_science
        return map_final_science(sources=sources, execution_id=execution_id, input_ref=input_ref,
            context=context, store=store, client=client, provider_binding=provider_binding, bind=bind,
            machine=machine, baseline_machine_ref=baseline_machine_ref, call_budget=call_budget,
            emit_event=emit_event, recovery_artifacts=recovery_artifacts)
    read_item_model, read_envelope_model, _ = investigation_read_models(kind)
    supplement = spec.input_mode == "FIRST_LAYER"
    reading = spec.typed_reading
    paragraph_input = spec.accepts_paragraphs
    recording = spec.records_science
    sealed_task_input = json.loads(read_artifact(store.read_bytes, input_ref))
    repair_mode = reading_repair_mode(sealed_task_input)
    from tsdaf.lanes.literature.middle_layer import explicit_current_updates, verify_reading_update_mode
    current_updates = explicit_current_updates(sealed_task_input)
    sparse_reading = reading and repair_mode == SPARSE
    images_readable = spec.image_mode == "DIRECT" or figure_client is not None
    image_unavailable = (
        "No figure reader is configured for this task; its images are not currently readable."
    )
    # A task scoped to reaction blocks is given those blocks and nothing else: its
    # sheet, its value shapes and the collections its write declares are all cut to
    # what those steps can carry. `None` is the paper's own task, which keeps all of it.
    scope = None if supplement else block_ids
    task: TaskScope = "BLOCK" if scope is not None else "PAPER" if shares_the_round else "WHOLE"
    expression = machine is not None and machine_profile(machine or {}).expression
    open_expression = machine is not None and machine_profile(machine).open_expression
    unified_expression = machine is not None and machine_profile(machine).unified_expression
    write_collections = investigation_write_collections(
        kind, task=task, expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    )
    # A figure the route was read from stays readable downstream, whatever the Router
    # decided of it (owner ruling, batch 2A, 2026-09-25).
    allowed_main_visual_ids = tuple(
        dict.fromkeys(
            (*allowed_main_visual_ids, *(v for _, v in figure_sources(observations, sources)))
        )
    )
    catalog = sources.catalog(allowed_main_visual_ids=allowed_main_visual_ids)
    prompt, declarations = investigation_resources(
        kind, task=task, expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    )

    def seal(value: object, media: str, *, accept: bool = True) -> ArtifactRef:
        return seal_artifact(store, bind, value, media, accept=accept)

    accepted_for_edits = set((*recovery_artifacts, input_ref))
    original_bind = bind

    def bind(*refs: ArtifactRef) -> None:
        original_bind(*refs)
        accepted_for_edits.update(refs)

    prompt_ref = seal(prompt.encode(), "text/vnd.tsdaf.investigation-prompt+markdown")
    schema_ref = seal(declarations, "application/vnd.tsdaf.investigation-tools+json")
    # The complete handoff remains sealed as input_ref. These keys of it are the Host's
    # inputs and never the model's; every record the model is shown from the rest goes
    # through one projection (`model_view`), which is where "no producer audits, no
    # repeated field values in a model-facing view" is enforced rather than said.
    #
    # `block_provenance` is the fourth of these: the Host's own map from fusion fact to
    # published id, which is what this function filters the group's conclusions *with*.
    # It is bookkeeping in the Host's private ids and there is nothing a model can do
    # with it -- and it is not small (roughly 6,000 bytes on hyperfirin, more on a
    # longer route), so shipping it would have spent a task's own byte budget on the
    # very thing that exists to keep that budget spendable.
    context_purpose = "FINAL_MAPPING" if current_updates else "RECORDING" if recording else "READING"
    model_context = {
        k: v
        for k, v in context.items()
        if k
        not in {
            "original_main_baseline_ref",
            "existing_public_containers",
            "existing_observations",
            "block_provenance",
            "observation_provenance",
            "material_sources",
            "paragraphs",
            "paragraph_selections",
            "paragraph_observations",
            "paragraph_target_bindings",
            "record_selections",
            "record_observations",
            # The digest the supplement's first layer is sealed under.
            "first_layer_sha256",
        }
    }
    # The first layer as a later task is shown it (`model_view`): handed over raw, one
    # real route result (95,022 bytes on hyperfirin) put the supplement's first message
    # over its budget once task context stopped being demoted by size (E188).
    if "routing" in model_context:
        model_context["routing"] = routing_view(model_context["routing"])
    if "route_results" in model_context:
        model_context["route_results"] = [
            route_result_view(
                read_fusion_result(canonical_json_bytes(result)),
                profile=(first_layer_profiles or {}).get(result["work_unit_id"],
                    machine_profile(machine) if machine is not None else MachineProfile.V1),
                source_selections=(first_layer_source_selections or {}).get(result["work_unit_id"]),
            )
            for result in model_context["route_results"]
        ]
    if not supplement:
        model_context["source_situation"] = source_situation_note(sources)
    main_context: dict[str, Any] = {}
    resource_locations: dict[str, dict[str, Any]] = {}

    def register_resource(
        value: Any,
        *,
        origin: str,
        ref: ArtifactRef | None = None,
        source_navigation: str | None = None,
        public_object_snapshot: bool = False,
    ) -> str:
        artifact = ref or seal(value, "application/vnd.tsdaf.readable-record+json")
        owner = (
            execution_id
            if origin in {"CURRENT_FINDINGS", "SAVED_FINDING"}
            else value.get("work_unit_id")
            if isinstance(value, dict)
            else None
        )
        binding = resource_binding(
            artifact.model_dump(mode="json"),
            origin=origin,
            provided_to=execution_id,
            owner_task=owner,
        )
        if source_navigation is not None:
            # Host-owned navigation provenance, part of the existing binding identity
            # and checkpoint. It is never inferred from the JSON being read.
            binding["source_navigation"] = source_navigation
        if public_object_snapshot:
            binding["public_object_snapshot"] = True
        location = resource_location(binding)
        resource_locations[location] = binding
        return location

    if not supplement and machine is not None and "groups" in context:
        # Which published step each fusion fact became. Sealed with the publication the
        # round was dispatched against; absent from an older one, and then nothing is
        # placed and every subject goes to the paper's task, which is where an
        # unplaceable one goes anyway.
        identity_map = cast(Mapping[str, Mapping[str, str]], context.get("block_provenance") or {})
        model_context["groups"] = project_detail_group_context(
            context["groups"], identity_map, machine, scope,
            purpose=context_purpose,
        )
        if scope is None:
            # The fusion lane's whole result is offered to the paper's own task and to no
            # block task. It is registered unconditionally and never passed the 8,000-byte
            # rule, so withdrawing the containers left it standing as the one large
            # fetchable thing a block task could still walk -- and the e108 round shows
            # what that means: the seven block tasks spent 109 turns on it, 42 of them
            # paging the directory of its `facts`. The menu the plan named was closed and
            # the turns moved next door, which says the turns were never about the
            # containers: they were about there being something large to fetch at all.
            for index, group in enumerate(context["groups"]):
                if "result" not in group:
                    continue
                location = f"main:{index}"
                main_context[location] = group["result"]
                pinned = register_resource(
                    group["result"],
                    origin="MODEL_WORK",
                    ref=seal(
                        group_result_view(group["result"], purpose=context_purpose),
                        "application/vnd.tsdaf.readable-record+json",
                    ),
                )
                resource_locations[location] = resource_locations[pinned]
                if not current_updates:
                    model_context["groups"][index]["result_location"] = location
    if not supplement and not paragraph_input:
        if observations:
            # A block task is told which observations its own steps rest on and nothing
            # else; the paper's task, which answers for the whole set, is told all of them
            # -- each where it reads it, once `searchable` below says where that is.
            model_context["existing_observations"] = (
                observations_these_blocks_cite(observations, machine, scope)
                if scope is not None
                else [
                    {
                        **observation,
                        "block_id": anchored_block_id(observation),
                        "asset_sha256": anchored_asset(observation),
                    }
                    for observation in observations
                ]
            )
        # No `existing_public_containers`, for the paper's task either: a locator writes
        # paragraphs, and the route on its sheet already names every object it can say
        # a paragraph concerns, with the values recorded. Over 8,000 bytes the containers
        # arrived as a menu anyway (E108; a2 2026-09-24), which the model never needed.
    source_locations: dict[str, dict[str, Any]] = {}
    # Searching answers with locations, so each document keeps the map from its block
    # ids to the offered names; a hit outside this map is outside what may be read.
    # A kept block with no sentence in it -- an empty block the normalizer typed OTHER,
    # 48 of them inside the procedures of hyperfirin's MinerU supplement (E156) -- has
    # nothing a search can hit or the Host can hand over, so it is offered in the
    # directory as it always was but is not in this map: a unit delivered whole is the
    # unit's text, and a block of no text contributes none.
    searchable: dict[str, dict[str, str]] = {}
    text_locations: dict[str, dict[str, str]] = {}
    figure_locations: dict[str, dict[str, str]] = {}
    for document in catalog:
        for medium in ("text", "visuals"):
            for entry in cast(list[dict[str, Any]], document.get(medium, [])):
                offered_selection = entry.pop("read_selection")
                offered_location = source_catalog_location(
                    str(document["document_revision_id"]), str(entry["source_id"])
                )
                entry["location"] = offered_location
                revision = str(document["document_revision_id"])
                if medium == "text":
                    text_locations.setdefault(revision, {})[str(entry["source_id"])] = (
                        offered_location
                    )
                if medium == "text" and entry["sentence_count"]:
                    searchable.setdefault(revision, {})[str(entry["source_id"])] = offered_location
                if medium == "visuals":
                    figure_locations.setdefault(revision, {})[str(entry["source_id"])] = (
                        offered_location
                    )
                # Both image modes accept a region. FIGURE_READER uses it as V3 focus;
                # DIRECT delivers the selected pixels to the task.
                entry["selectors"] = (
                    ["text_range", "directory", "whole"]
                    if medium == "text"
                    else ["image_region", "whole"]
                )
                if medium == "visuals":
                    entry["answer_mode"] = (
                        "figure_reader_words" if spec.image_mode == "FIGURE_READER" else "pixels"
                    )
                if medium == "visuals" and not images_readable:
                    entry["selectors"] = []
                    entry["unavailable"] = image_unavailable
                source_locations[offered_location] = {**offered_selection, "medium": medium}
    # Exact normalized-row identity links a block to its figure, including a figure
    # without a caption. No nearest-neighbour or label guessing; only a unique pair
    # in the same document and an already-authorized visual can supply an entry.
    figure_at: dict[str, dict[str, str]] = {}
    for revision, blocks in text_locations.items():
        offered_figures = figure_locations.get(revision, {})
        package = sources.document(revision).package
        if not offered_figures or package is None:
            continue
        for block in package.blocks:
            native = block.source.native_block_index
            if native is None or block.block_id not in blocks:
                continue
            if sum(b.source.native_block_index == native for b in package.blocks) != 1:
                continue
            visuals = [v for v in package.visual_assets if v.source.native_block_index == native]
            if len(visuals) == 1 and visuals[0].visual_id in offered_figures:
                figure_at.setdefault(revision, {})[block.block_id] = offered_figures[
                    visuals[0].visual_id
                ]
    # An empty normalized row has no sentence selector. Its catalog and later
    # refusal both point to the same authorized row-to-image binding.
    for document in catalog:
        revision = str(document["document_revision_id"])
        for entry in cast(list[dict[str, Any]], document.get("text", [])):
            if entry["sentence_count"]:
                continue
            figure = figure_at.get(revision, {}).get(entry["source_id"])
            entry["selectors"] = ["whole"] if figure and images_readable else []
            if figure:
                entry["figure_location"] = figure
                if not images_readable:
                    entry["unavailable"] = image_unavailable
            else:
                entry["unavailable"] = (
                    "This block has no readable text or unique authorized figure."
                )
    source_entries = {
        entry["location"]: {**entry, "document_revision_id": document["document_revision_id"]}
        for document in catalog
        for medium in ("text", "visuals")
        for entry in cast(list[dict[str, Any]], document.get(medium, []))
    }
    readable_source_locations = {
        location
        for location, entry in source_entries.items()
        if entry.get("selectors") and not entry.get("unavailable")
    }

    method_targets = {
        location: target for location, target in general_method_targets(sources.documents).items()
        if location in readable_source_locations
    }

    def navigation_entry(location: str) -> dict[str, Any]:
        """The catalog owns the target's meaning and actual read capability."""
        entry = source_entries[location]
        return {
            key: entry[key]
            for key in (
                "location",
                "source_id",
                "document_revision_id",
                "page",
                "preview",
                "sentence_count",
                "figure_location",
                "unavailable",
            )
            if key in entry
        } | ({"select": None} if location in readable_source_locations else {})

    # A registration may name MAIN by the CLI's own label ("MAIN-decumbenine_Be106" on the
    # 2026-09-11 real chain) while every published observation names the same document by
    # the stable revision its content decides ("MAIN-b7296d45ff4f4c06", E66 (4)). Joining
    # the two on the string alone reaches nothing at all; the package is what they agree
    # on, so the stable id maps back to the id this task's own catalog uses.
    registered_revision = {
        doc.package.stable_revision_id(doc.registration.source_phase): (
            doc.registration.document_revision_id
        )
        for doc in sources.active()
        if doc.package is not None
    }
    # The documents this task assesses: those whose part it was given text of, one set
    # for the documents it is shown and the coverage it may save. A document whose part
    # is empty is the Host's own cut, which the task's sources note already states
    # (`source_situation_note`): a3's and a4's paper tasks were shown the main text
    # among their documents anyway, refused for assessing it without evidence they could
    # not have, and a4 spent thirteen turns hunting main text before finishing PARTIAL.
    # The paragraphs a task's steps were built from are readable beside the part
    # (`step_block_ids`), not a part of their own: they make nothing to assess.
    parts = {doc.registration.document_revision_id: set(doc.part_ids) for doc in sources.active()}
    assessed = [
        document
        for document in catalog
        if any(
            row["source_id"] in parts.get(str(document["document_revision_id"]), ())
            for row in cast(list[dict[str, Any]], document.get("text", []))
        )
    ]
    # Every row the Host hands over that names a block -- an observation's anchor, a
    # step task's claimed source, unread prose, an unclaimed procedure -- is shown by
    # the location this task reads that block at and under the revision `documents`
    # names, not by the package's block id beside a second id for the same document:
    # a4's paper task was handed MAIN-P0000-B0018 so, read it bare and with `source:`,
    # and was refused both times. A row whose block this task cannot read gets no
    # location and stays the Host's (`model_view`). Patched before the projection below,
    # and on copies: the round's own record is this task's input and is hashed as such.

    # A figure is found by the asset its observation cites: an image anchor names no block.
    figures_by_asset = {
        doc.registration.document_revision_id: {
            visual.asset.sha256: visual.visual_id for visual in doc.package.visual_assets
        }
        for doc in sources.active()
        if doc.package is not None
    }

    def locate(rows: list[dict[str, Any]], block_key: str) -> None:
        for row in rows:
            observed = str(row.get("document_revision_id"))
            revision = registered_revision.get(observed, observed)
            row["document_revision_id"] = revision
            location = searchable.get(revision, {}).get(str(row.get(block_key)))
            if location is None and row.get("asset_sha256"):
                visual_id = figures_by_asset.get(revision, {}).get(str(row["asset_sha256"]))
                location = figure_locations.get(revision, {}).get(str(visual_id))
            if location is not None:
                row["location"] = location

    # Each value's supporting sources use the very same authorized catalog as every
    # other read. Preserve all text fragments; a whole-block entry offers context, not
    # an invented sentence-precise anchor. Detail figures are read whole by the figure
    # reader, so no region selector is offered here.
    evidence_locations: dict[str, tuple[Mapping[str, Any], ...]] = {}
    def register_evidence_locations(current_observations):
        for observation in current_observations:
            anchor = observation.get("anchor") or {}
            rows = [
                {
                    "document_revision_id": observation["document_revision_id"],
                    "block_id": fragment["block_id"],
                }
                for fragment in anchor.get("fragments", ())
            ]
            if asset := anchored_asset(observation):
                rows.append(
                    {
                        "document_revision_id": observation["document_revision_id"],
                        "asset_sha256": asset,
                    }
                )
            locate(rows, "block_id")
            evidence_locations[str(observation["observation_id"])] = tuple(
                {"location": row["location"], "select": {"kind": "whole"}}
                if "location" in row and ("asset_sha256" not in row or figure_client is not None)
                else {
                    "unavailable": image_unavailable
                    if "location" in row
                    else "The supporting source is outside this task's catalog."
                }
                for row in rows
            )
    if not supplement:
        register_evidence_locations(observations)
    account = (
        route_account(
            cast(Sequence[Mapping[str, Any]], context.get("groups") or ()),
            cast(Mapping[str, Mapping[str, str]], context.get("block_provenance") or {}),
            observations,
            sources.main.part_ids,
            read_at=evidence_locations,
            material_sources=material_sources_for(machine, context.get("material_sources") or {})
            if machine is not None
            else {},
        )
        if not supplement
        else None
    )

    existing_observations = model_context.get("existing_observations")
    if isinstance(existing_observations, list):
        locate(existing_observations, "block_id")
        shown = observation_views(existing_observations)
        if shown:
            model_context["existing_observations"] = shown
        else:
            del model_context["existing_observations"]
    round_left = model_context.pop("round_left", None)
    located_left: dict[str, Any] | None = None
    if isinstance(round_left, dict):
        located_left = copy.deepcopy(round_left)
        for key, block_key in (
            ("remaining_sources", "source_id"),
            ("handled_sources", "source_id"),
        ):
            locate(located_left.get(key) or [], block_key)
        handoff = located_left.get("source_handoff")
        if isinstance(handoff, dict):
            locate(handoff.get("catalog") or [], "source_id")
        # Keep the full host input for progress and on-demand handled reads. The
        # model sees the current handoff once, in saved.still_left on every turn.
    # Task context is not demoted by size. Any key over 8,000 bytes used to become a
    # directory the model had to go and fetch -- for a block task a hard error instead
    # -- and on a2 (2026-09-24) that is where the paper task's own remaining list, its
    # observations and the supplement's route results went: host derivations handed
    # over as menus. What the model is shown is what its kind can act on, projected
    # (`model_view`); whole-request admission runs after final naming and preparation.
    # The detail lane is told that pictures exist and where their index is; it is not
    # handed the index. Whatever the Host says at length becomes a menu anyway -- task
    # context over 8,000 bytes is demoted to a readable location a few lines above -- so
    # an index of 19 figures (5,828 bytes on decumbenine_B) either costs every turn of a
    # lane that reads prose, or is fetched on demand. Withheld is not deleted: without
    # the sentence below a task could not name an image, and on demand needs a name.
    withheld_document_keys = {"text"} if supplement else {"text", "visuals"}
    # No list to page through (E189): a figure is found as prose is, by a search that
    # lands on its caption. On a2 and a3 no block or paper task read a single figure,
    # and their catalog pages were most of what every request re-sent.
    images_note = (
        f"{sum(len(cast(list[Any], d.get('visuals', ()))) for d in catalog)} images of this "
        "paper are authorized for this task. Figures in delivered passages have linked "
        "read locations; a search that lands on a caption also names its figure location."
        " A query containing a registered figure name also returns named_sources: "
        "direct image read locations, not image contents or evidence already read."
        + (
            " A figure reader can read that location when needed."
            if images_readable
            else " " + image_unavailable
        )
    )

    # Where this task's own steps were written, read by the Host rather than searched for
    # by the model. E100 measured the run this replaces: of 223 model turns on one real
    # eight-task run of decumbenine_B, 116 landed on host-sealed `result:` locations that
    # by construction cannot mint a selection_id, and only 24 reached the paper's prose.
    # Deleting the prompt sentence that pointed there moved nothing -- the model went 74
    # times after the deletion -- so the constraint lives on the side that hands the text
    # over, not in the prompt.
    def attachment_reference(revision: str, block_id: str) -> dict[str, Any] | None:
        document = sources.document(revision)
        unit = document.units.unit_containing(block_id) if document.units is not None else None
        if unit is None or not unit.is_attachment:
            return None
        location = next(
            (searchable[revision][b] for b in unit.block_ids if b in searchable.get(revision, {})),
            None,
        )
        if location is None:
            return None
        return {
            "location": location,
            "unit_kind": unit.kind.value,
            "upstream_label": unit.compound_label,
            "source_text": (
                "Not delivered. Read location for this original block and its continuous "
                "passage; complete_unit_read is optional when the whole attachment is needed."
            ),
            **(
                {
                    "complete_unit_read": {
                        "location": location,
                        "select": QuestionedWhole(kind="whole", scope="unit").model_dump(
                            mode="json", exclude_defaults=True
                        ),
                    }
                }
                if all(b in text_locations.get(revision, {}) for b in unit.block_ids)
                else {}
            ),
        }

    def default_source_cost(revision: str, block_id: str, text: str) -> int:
        reference = attachment_reference(revision, block_id)
        return len(canonical_json_bytes(reference)) if reference else len(text)

    delivered_sources: list[tuple[str, str, str]] = []
    spent = 0
    for observed_revision, block_id in step_sources(
        machine if not supplement and not paragraph_input else None, observations, scope
    ):
        revision = registered_revision.get(observed_revision, observed_revision)
        # Not `location`: that name is already a `str` in this function (the `main:`
        # result locations above), and rebinding it here would be a second meaning.
        step_location = searchable.get(revision, {}).get(block_id)
        if step_location is None:
            # The step was also read where this task may not read: the route's own
            # narrative, on the far side of the experimental cut. Three of the eight
            # blocks decumbenine_B's steps name are there, and they stay unread here.
            continue
        package = sources.document(revision).package
        assert package is not None  # `searchable` names only blocks of a loaded package
        spent += default_source_cost(
            revision, block_id, next(b.raw_text for b in package.blocks if b.block_id == block_id)
        )
        if spent > INVESTIGATION_DELIVERY_CHARS:
            break
        delivered_sources.append((revision, block_id, step_location))
    built_from = len(delivered_sources)

    # The supplement's task is the main text's own narrative, so the main text travels
    # with it, in the document's order, under the same budget as a step's paragraphs.
    # It used to be found through the catalog: 19 of the 29 passages a3's supplement read
    # (E189). The six papers of the offline corpus run to 13,000-49,000 characters of
    # main text; what does not fit is found by search.
    main_text = 0
    if supplement:
        for revision, blocks in searchable.items():
            source_document = sources.document(revision)
            package = source_document.package
            if package is None or source_document.registration.source_phase != "MAIN":
                continue
            for block in package.blocks:
                main_location = blocks.get(block.block_id)
                if main_location is None:
                    continue
                spent += default_source_cost(revision, block.block_id, block.raw_text)
                if spent > INVESTIGATION_DELIVERY_CHARS:
                    break
                delivered_sources.append((revision, block.block_id, main_location))
                main_text += 1

    # A step reaches its own paragraphs through the observations the route was built
    # from, and when the experimental part is in a supplement there are none: every
    # observation of the hyperfirin baseline is MAIN, so thirteen block tasks would
    # start with no experimental prose at all and a 150-entry directory to walk. The
    # author's own numbering is the other handle the Host has, and since E136 the
    # document's own reading says where each number's procedure, data and crystal
    # structure are: those units are handed to the step whose product carries the
    # label. For a label no unit claims, the title scan of E127 is the fallback -- an
    # experimental entry is titled by the compound it makes (E96).
    #
    # A candidate and nothing more, either way. The Host has not decided that this
    # passage is this step -- a reader with the whole document in view said whose it
    # is, or a number was read off a title -- and both the row and the note say which.
    # The units claim all thirteen of hyperfirin's steps; the scan reached nine.
    # Two ledgers this task's receipt carries, so the round can ask questions no single
    # turn can answer: which of the Host's guesses were put in front of it, and every
    # time it went back to a source with a reason.
    reasoned_reads: list[dict[str, Any]] = []
    candidates: list[dict[str, str]] = []
    general_methods = 0
    if not supplement and machine is not None and scope is not None:
        pool: list[tuple[str, str, str]] = []
        documents: list[tuple[str, ReadingUnits | None, frozenset[str]]] = []
        for revision, blocks in searchable.items():
            source_document = sources.document(revision)
            package = source_document.package
            if package is None:
                continue
            documents.append((revision, source_document.units, frozenset(blocks)))
            attachment_bodies = {
                block_id
                for unit in (source_document.units.units if source_document.units else ())
                if unit.is_attachment
                for block_id in unit.block_ids
                if block_id not in unit.reader_block_ids
            }
            pool.extend(
                (revision, block.block_id, block.raw_text)
                for block in package.blocks
                if block.block_id in blocks and block.block_id not in attachment_bodies
            )
        text_of = {(revision, block_id): text for revision, block_id, text in pool}
        label_owner = {
            label: str(block["reaction_block_id"])
            for block in scoped_blocks(machine, scope)
            for label in step_author_labels(machine, (str(block["reaction_block_id"]),))
        }
        already = {(revision, block_id) for revision, block_id, _ in delivered_sources}
        labels = step_author_labels(machine, scope)
        rows = candidate_step_units(labels, documents)
        claimed = {row["matched_label"] for row in rows}
        rows.extend(
            candidate_step_sources([label for label in labels if label not in claimed], pool)
        )
        for row in rows:
            site = (row["document_revision_id"], row["source_id"])
            candidate_location = searchable.get(site[0], {}).get(site[1])
            if site in already or candidate_location is None:
                continue
            spent += default_source_cost(*site, text_of[site])
            if spent > INVESTIGATION_DELIVERY_CHARS:
                break
            delivered_sources.append((*site, candidate_location))
            already.add(site)
            # Which step's number found it: a limitation naming that step is one of the
            # two ways this candidate can be answered.
            candidates.append(
                {
                    **row,
                    "location": candidate_location,
                    "reaction_block_id": label_owner.get(row["matched_label"], ""),
                }
            )
        # The document's general methods, after the step's own passages and under the
        # same budget. Delivery does not imply applicability to every procedure;
        # the original method states its scientific scope.
        for site in general_methods_blocks(documents):
            general_location = searchable.get(site[0], {}).get(site[1])
            if site in already or general_location is None:
                continue
            spent += len(text_of[site])
            if spent > INVESTIGATION_DELIVERY_CHARS:
                break
            delivered_sources.append((*site, general_location))
            already.add(site)
            general_methods += 1

    # The whole-route task is handed the procedure and data units that no step claims.
    # The document's own reading classified them and no step task owns them, so without
    # this nobody is shown them. Same budget as a step's passages; delivery says nothing
    # about whether the experiment belongs to the route.
    unclaimed_units = 0
    if not supplement and machine is not None and scope is None:
        shown = {(revision, block_id) for revision, block_id, _ in delivered_sources}
        whole_documents: list[tuple[str, ReadingUnits | None, frozenset[str]]] = []
        raw_text: dict[tuple[str, str], str] = {}
        for revision, blocks in searchable.items():
            source_document = sources.document(revision)
            if source_document.package is None:
                continue
            whole_documents.append((revision, source_document.units, frozenset(blocks)))
            raw_text.update(
                ((revision, block.block_id), block.raw_text)
                for block in source_document.package.blocks
                if block.block_id in blocks
            )
        unclaimed_firsts: set[tuple[str, str]] = set()
        reached_by_steps = {
            (registered_revision.get(observed, observed), block_id)
            for observed, block_id in step_sources(machine, observations, None)
        }
        for row in unclaimed_step_units(machine, whole_documents, reached_by_steps):
            site = (row["document_revision_id"], row["source_id"])
            unclaimed_location = searchable.get(site[0], {}).get(site[1])
            if site in shown or unclaimed_location is None or site not in raw_text:
                continue
            spent += default_source_cost(*site, raw_text[site])
            if spent > INVESTIGATION_DELIVERY_CHARS:
                break
            delivered_sources.append((*site, unclaimed_location))
            shown.add(site)
            unclaimed_firsts.add((site[0], row["unit_first_block_id"]))
        unclaimed_units = len(unclaimed_firsts)

    # The experimental sources' own table of contents, for every task that locates in them.
    # The document's reading already divided each source into units and labelled them, and
    # the tasks were not shown that: across three papers (2026-10-10) 477 of 716 locating
    # reads were text searches -- for a compound's procedure, for a page, for the general
    # methods -- and half of those found nothing; what the tasks did find that way was
    # mostly a procedure whose printed label differs from the step's (ineleganolide: 24,
    # 25, 26, numbered only in the supplement). A unit is listed with where it reads from
    # and how it begins; listing it says nothing about which step it belongs to.
    experimental_units: list[dict[str, object]] = []
    if not supplement and machine is not None:
        for revision, blocks in searchable.items():
            source_document = sources.document(revision)
            if source_document.package is None or source_document.units is None:
                continue
            words = {block.block_id: block.raw_text for block in source_document.package.blocks}
            for unit in source_document.units.units:
                if unit.kind not in _DIRECTORY_KINDS:
                    continue
                first = next((block_id for block_id in unit.block_ids if block_id in blocks), None)
                if first is None:
                    continue
                experimental_units.append(
                    {
                        "location": blocks[first],
                        "kind": unit.kind.value,
                        "label": unit.compound_label,
                        "begins": " ".join(words.get(first, "").split())[:_DIRECTORY_HEAD_CHARS],
                        "blocks": len(unit.block_ids),
                    }
                )

    # Where the text speaks of each figure this task can read. After the unit list, the
    # searches left were for the main text: of 107 searches with hits in two papers
    # (2026-10-10), 74 hit the main text only, and most were a scheme's name -- looking
    # for its caption and for the paragraphs that discuss it. Both are in the source
    # already; this is a text index, with no judgment about what belongs to the task.
    figure_mentions: list[dict[str, object]] = []
    if kind == "EXPERIMENTAL_DETAIL" and not supplement and machine is not None:
        for revision, blocks in searchable.items():
            source_document = sources.document(revision)
            package = source_document.package
            if package is None:
                continue
            for visual_id, location in figure_locations.get(revision, {}).items():
                label = visual_label(package, visual_id)
                if not label:
                    continue
                named = re.compile(
                    r"(?<!\w)" + r"\s+".join(re.escape(word) for word in label.split()) + r"(?!\w)",
                    flags=re.IGNORECASE,
                )
                mentioned = [
                    blocks[block.block_id]
                    for block in package.blocks
                    if block.block_id in blocks and named.search(block.raw_text or "")
                ]
                figure_mentions.append(
                    {
                        "figure": label,
                        "location": location,
                        "caption": visual_caption(package, visual_id),
                        "mentioned_in": mentioned[:_FIGURE_MENTIONS],
                        **(
                            {"more_mentions": len(mentioned) - _FIGURE_MENTIONS}
                            if len(mentioned) > _FIGURE_MENTIONS
                            else {}
                        ),
                    }
                )

    # Both the locator's packet and the reading-specific packet need the same
    # usable entrance. Replacing the generic packet must not hide this capability.
    search_note = (
        f"To find a source, read location '{SEARCH_LOCATION}' with select "
        '{"kind": "query", "terms": "..."}. Every hit answers with its own location, '
        "which reads like any other source."
    )

    def source_note() -> str:
        """What this task has been given, and what it still has to go and find.

        Before the pre-read this said the task had been given no source text at all and
        named search as the way to get some; under that instruction 24 turns of 223
        reached the prose (E100). The steps now arrive with their own paragraphs, so
        search is for what those paragraphs do not say rather than the opening move.
        """
        # The directory's own count: what the task may read, including a block of no
        # text that nothing can hit or hand over (those are kept out of `searchable`).
        total = sum(len(cast(list[Any], document.get("text", []))) for document in catalog)
        head = f"{total} authorized text sources back this paper."
        if not supplement:
            head += (
                " Text reads and search share the upstream-segmented scope, across routes,"
                " together with exact sources cited by upstream findings. Search does not"
                " fall back to the full document."
            )
            for document in sources.active():
                if document.package is None:
                    continue
                revision = document.registration.document_revision_id
                units = document.units
                if units is None:
                    head += (
                        f" {revision}: upstream segmentation is unavailable; only explicitly"
                        " granted sources can be read, not an assertion of absent experiments."
                    )
                elif units.uncovered_block_ids:
                    head += (
                        f" {revision}: upstream segmentation left {len(units.unclaimed_block_ids)}"
                        f" shown blocks unclassified and {len(units.not_shown_block_ids)} blocks"
                        " unshown; these are segmentation gaps, not evidence of absent content."
                    )
        if built_from:
            head += (
                f" The {built_from} your steps were built from are in this same "
                "message: original passages with selection_ids and technical attachments with "
                "read locations. Reading a delivered passage again is "
                "answered IN_CONTEXT while that content is shown."
            )
        elif main_text:
            head += (
                f" {main_text} passages of the main text are in this same message, in the "
                "document's order, each with the selection_id to cite it by; reading one "
                "again is answered IN_CONTEXT while that content is shown."
            )
        elif not candidates:
            head += " You have not been given their text."
        if candidates:
            by_units = [row for row in candidates if row["match"] == "UNIT_LABEL"]
            by_title = [row for row in candidates if row["match"] != "UNIT_LABEL"]
            if by_units:
                numbers = ", ".join(sorted({row["matched_label"] for row in by_units}))
                head += (
                    f" {len(by_units)} further passages or technical-attachment entries are here"
                    " because the"
                    " document was read once beforehand and each procedure, each set"
                    " of data and each crystal structure was marked with the label of"
                    f" the compound it is about; these carry a label your step makes"
                    f" ({numbers}). That reading had the whole document in view, and"
                    " it is still a claim about the passage, not about your step: read"
                    " the original source when needed and say whether it is about your step,"
                    " or about none"
                    " of them."
                )
            if by_title:
                numbers = ", ".join(sorted({row["matched_label"] for row in by_title}))
                head += (
                    f" {len(by_title)} passages are here because their title carries a"
                    f" number your step makes ({numbers}) and no unit claimed that"
                    " number: the Host found them by the number alone. Read them and"
                    " say whether each is about your step, or about none."
                )
        if unclaimed_units:
            head += (
                f" {unclaimed_units} procedure or data unit"
                f"{'s' if unclaimed_units > 1 else ''} that no step of the route claims"
                " are in this message: the document was read once beforehand and marked"
                " them as experimental, and no step makes their compound. No step task"
                " reads them; they are this task's. Say what each reports and whom it is"
                " about; being here does not make it part of the route."
            )
        if general_methods:
            head += (
                f" The document's general methods ({general_methods} passage"
                f"{'s' if general_methods > 1 else ''}) are in this message as well:"
                " available background methods. Their applicability follows the original text;"
                " delivery alone does not make each one relevant to your step or require a read."
            )
        # Search is the one way to find what was not handed over. The sentence that
        # named a list of every source (kalmanol, 2026-09-11, when tasks were given no
        # text) sent a3's tasks paging through it: its pages were 85% of everything
        # re-sent, and block tasks found 107 of their 112 sources by search (E189).
        return f"{head} {search_note}"

    loop = ToolTurnState()
    loop.history = [
        {
            "type": "user_input",
            "content": [
                {
                    "type": "text",
                    "text": canonical_json_bytes(
                        {
                            "kind": kind,
                            # The first thing this task reads is decided by the task, not
                            # by an index of every block in the paper: the full catalog
                            # stays a readable state resource rather than travelling in
                            # every turn's input, and the paragraphs this task's own steps
                            # were written in travel with it instead of being searched for.
                            "sources": source_note(),
                            # Which documents this task may use: the scope, without the
                            # block list that used to travel with it, and for the detail
                            # lane without the image index either -- `images` just below
                            # says how a figure is found.
                            "documents": [
                                document_view(
                                    document,
                                    media=[
                                        m
                                        for m in ("text", "visuals")
                                        if m not in withheld_document_keys
                                    ],
                                )
                                for document in assessed
                            ],
                            **({} if supplement else {"images": images_note}),
                            # Said before the first write, not as a refusal after it: across
                            # three papers (2026-10-10) 18 of 51 locating tasks wrote a
                            # coverage entry for the main text they had read passages of,
                            # had it refused, and spent a second write -- the costliest kind
                            # of reply -- taking it out.
                            **(
                                {
                                    "coverage_documents": (
                                        "A coverage entry is recorded only for the documents listed in "
                                        "documents: "
                                        + ", ".join(
                                            str(document_view(d, media=[])["document_revision_id"])
                                            for d in assessed
                                        )
                                        + ". Passages read from any other document are used as "
                                        "evidence without a coverage entry of their own."
                                    )
                                }
                                if kind == "EXPERIMENTAL_DETAIL" and assessed
                                else {}
                            ),
                            **(
                                {
                                    "experimental_units": {
                                        "meaning": (
                                            "Every procedure, data and general-methods unit of the "
                                            "experimental sources, in reading order, with the location "
                                            "its first block reads from, the compound label printed on "
                                            "it, and how it begins. Read a location directly; there is "
                                            "no need to search for a unit listed here. A unit being "
                                            "listed does not make it part of this task."
                                        ),
                                        # When the work is done. Across three papers (2026-10-10)
                                        # 93% of what 51 locating tasks delivered was already in
                                        # their first message or in this list, yet 50 of them
                                        # searched (192 searches) and for 40 the searching added
                                        # nothing: an open-ended task gives no way to know that
                                        # nothing was missed.
                                        "complete_when": (
                                            "This list is complete: nothing experimental in these "
                                            "sources lies outside it, so there is nothing further to "
                                            "discover by searching them. The work on the experimental "
                                            "sources is done when every listed unit that belongs to this "
                                            "task has been read and recorded. Decide which units belong "
                                            "by what they say -- how each begins, and its text once read "
                                            "-- not by the label alone: a label can be missing, loose or "
                                            "reused. Search only for something this list cannot hold: a "
                                            "passage of the main text about this task, or a detail no "
                                            "listed unit turned out to contain."
                                        ),
                                        "units": experimental_units,
                                    }
                                }
                                if experimental_units
                                else {}
                            ),
                            **(
                                {
                                    "figure_mentions": {
                                        "meaning": (
                                            "Every labelled figure this task can read: the location "
                                            "it reads from, its caption as the source holds it, and "
                                            "the text blocks that mention its name. Read those "
                                            "locations directly; there is no need to search for a "
                                            "figure's name or its caption. A figure being listed "
                                            "does not make it part of this task."
                                        ),
                                        "figures": figure_mentions,
                                    }
                                }
                                if figure_mentions
                                else {}
                            ),
                            "context": model_context,
                            # The route step by step, with the shapes once (E96),
                            # not one record per offered field with its own schema.
                            "route": container_view(
                                machine if not supplement else None,
                                scope,
                                account,
                                paragraph_locator=not (supplement or paragraph_input),
                                recording=recording,
                            )["containers"],
                        }
                    ).decode(),
                }
            ],
        }
    ]

    if paragraph_input:
        assert machine is not None
        # The route with each material under the position a finding names it by, when
        # this kind can name one; the compounds as the one projection shows them. The
        # route's own account rides along: this task reads the whole paper, so it is
        # handed the whole route's wording and where each recorded value was read --
        # the same two things a block task sees for its own steps (batch 2A, 2d, E203).
        reading_route = container_view(
            machine, material_ids=spec.names_skeleton_materials(), route=account,
            final_mapping=current_updates, recording=recording,
        )["containers"]
        narrative_entries = []
        for narrative in container_view(machine, route=account).get("paper_narratives", []):
            location = register_resource({"narrative": narrative}, origin="MODEL_WORK")
            text = narrative.get("author_text") or narrative.get("explanation") or ""
            narrative_entries.append(
                {
                    "narrative_id": narrative["narrative_id"],
                    "kind": narrative["kind"],
                    "title": narrative.get("title"),
                    "subjects": narrative.get("subjects", []),
                    "summary": (text[:240] + ("…" if len(text) > 240 else ""))
                    if isinstance(text, str) else _scientific_view(text, account),
                    "location": location,
                }
            )
        initial = {
            "kind": kind,
            **({"scientific_records": context["scientific_records"],
                "record_status": context["record_status"],
                "record_explanation": context["record_explanation"]}
               if "scientific_records" in context else {}),
            "machine_profile": machine_profile(machine).value,
            "route": reading_route,
            "characterization_cards": characterization_view(machine, account),
            "paper_narratives": narrative_entries,
            **({"finding_entries": reading_write_context(
                reading_route,
                expression=expression,
                preservation=machine_profile(machine).preservation,
                open_expression=open_expression,
                unified_expression=unified_expression, current_updates=current_updates,
            )} if reading else {}),
            "skeleton_groups": model_context.get("groups", []),
            "compounds": [compound_view(c) for c in machine["domain_payload"]["compounds"]],
            "source_access": (
                "Procedural and characterization passages follow in full. Technical attachments "
                "carry the locator's attribution and a read location; their original text has not "
                "been delivered here. Read it before citing new original words. Supporting source "
                "locations accompany recorded route values. Read a supplied compound, operation, "
                "measurement or material name to inspect its current public result; this is model "
                "work, not a new reading of its original source. "
                + search_note
                + (" " + image_unavailable if not images_readable else "")
            ),
        }
        loop.history[0]["content"] = [
            {"type": "text", "text": canonical_json_bytes(initial).decode()}
        ]

    def figure_question() -> str:
        """Default scope for an ordinary image read without a specific model question."""

        if supplement:
            return (
                "A narrative of this paper's synthesis is being written from its "
                "supporting information. Report what this figure states about that "
                "synthesis: the transformations it shows, and the labels, conditions "
                "and quantities attached to them."
            )
        # A model prompt of its own, outside the task's loop: each step by the name the
        # task knows it by, never its id (batch 2A, 2d).
        route = route_names(machine)
        steps = "; ".join(
            route.present(str(block["reaction_block_id"]))
            for block in scoped_blocks(machine, scope)
        )
        return (
            "The experimental detail of "
            + (f"these steps of the route: {steps}" if steps else "this paper's route")
            + " is being recorded from the author's own words. Report what this figure "
            "states about it: the transformations it shows, and the labels, conditions, "
            "quantities and yields attached to them. Say plainly what it does not show."
        )

    figure_reservations = ExitStack()
    figure_reader: FigureReader | None = None
    if figure_client is not None:
        figure_task = figure_question()

        def read_figure(
            source: VisualSourceSlice,
            original_bytes: bytes,
            region: tuple[int, ...] | None,
            question: str | None,
        ) -> str:
            """Read one figure in its own sub-execution and return the words for it.

            The sub-execution seals its requests, its responses and its reading into this
            task's closure, so a replay replays the figure read along with the task. A
            second read of the same source spends nothing further: the saved selection
            already holds these words (`InvestigationSourceReader.read`).
            """

            assert figure_client is not None
            figure_binding = {
                **turn_binding,
                "figure_action": {
                    "interaction": loop.pending["interaction_ref"] if loop.pending else None,
                    "call_index": loop.next_call,
                    "visual_id": source.visual_id,
                    "source_sha256": source.source_asset.sha256,
                    "region": list(region) if region is not None else None,
                    **({"question": question} if question is not None else {}),
                },
            }
            reservation = None
            figure_attempt_limit = (
                call_budget.require_client(figure_client) if call_budget is not None else 1
            )
            if call_budget is not None:
                for ref in recovery_artifacts:
                    if ref.media_type == TOOL_REQUEST_INTENT_MEDIA:
                        if json.loads(store.read_bytes(ref)).get("binding") == figure_binding:
                            raise UncertainToolResponse(
                                "An interrupted figure action has a prior request intent; "
                                "automatic replay is disabled. Accepted task state is preserved."
                            )

            active_intent: ArtifactRef | None = None

            def before_request(request_bytes: bytes, turn: int) -> tuple[ArtifactRef, ArtifactRef]:
                nonlocal active_intent, reservation
                # An adapter without an attempt audit cannot refund its admitted
                # upper bound, but its completed request must not retain a live lease.
                figure_reservations.close()
                if call_budget is not None:
                    reservation = figure_reservations.enter_context(
                        call_budget.reserve(figure_attempt_limit)
                    )
                request_ref = seal(request_bytes, FIGURE_REQUEST_MEDIA)
                # Always journal the send, even when the caller sets no cost limit;
                # adding a limit on resume cannot make earlier figure calls free.
                intent = seal(
                    {
                        "binding": figure_binding,
                        "iteration": turn,
                        "request_ref": request_ref.model_dump(mode="json"),
                        "transport_attempt_limit": getattr(
                            figure_client, "max_transport_attempts", None
                        ),
                    },
                    TOOL_REQUEST_INTENT_MEDIA,
                )
                active_intent = intent
                if reservation is not None:
                    reservation.started(intent, attempts=figure_attempt_limit)
                return request_ref, intent

            def after_request(audit: ArtifactRef) -> None:
                audits.append(audit)
                if reservation is not None:
                    assert active_intent is not None
                    reservation.settled(active_intent, audit)
                    # A closer look is a separate provider request. Release this
                    # lease before it seeks admission, including failed audited sends.
                    figure_reservations.close()

            found = read_figure_in_words(
                source=source,
                original_bytes=original_bytes,
                question=(question or figure_task)
                if region is None or question is not None
                else figure_task
                + " The task pointed at the normalized-1000 region "
                + f"{list(region)} of this image; begin there, and read whatever else in "
                "the figure the question needs.",
                focus_region=region,
                host_identity={
                    "task_id": execution_id,
                    "kind": kind,
                    "block_ids": list(scope) if scope is not None else None,
                },
                client=figure_client,
                provider_binding=provider_binding,
                store=store,
                seal=seal,
                before_request=before_request,
                after_request=after_request,
            )
            seal(canonical_json_bytes(found.record()), FIGURE_READING_MEDIA)
            # Error paths without an accepted audit close in the outer finally;
            # their uncertain send remains charged at its admitted upper bound.
            figure_reservations.close()
            return (
                ("Question: " + question + "\n") if question is not None else ""
            ) + found.producer_note()

        figure_reader = read_figure

    source_reader = InvestigationSourceReader(
        kind=kind,
        sources=sources,
        execution_id=execution_id,
        input_ref=input_ref,
        prompt_ref=prompt_ref,
        schema_ref=schema_ref,
        seal=seal,
        store=store,
        allowed_main_visual_ids=allowed_main_visual_ids,
        read_figure=figure_reader,
    )

    def passage_figures(revision: str, block_id: str) -> list[dict[str, Any]]:
        """Navigation only: offering a figure never reads it or mints a selection."""
        revision = registered_revision.get(revision, revision)
        if revision not in figure_at:
            return []
        document = sources.document(revision)
        members = unit_reading_group(
            document.units,
            block_id,
            partners=source_reader.continuity(revision).partners,
            readable=document.readable_ids,
        )
        return [
            {
                "source_block": member,
                "location": figure,
                "select": {"kind": "whole"},
                **({} if images_readable else {"unavailable": image_unavailable}),
            }
            for member in members
            if (figure := figure_at.get(revision, {}).get(member)) is not None
        ]

    # A passage is delivered whole (E156): every block delivered above -- a step's own
    # paragraph, the title scan's block, a unit's row -- brings the rest of the unit
    # the reading placed it in, and the other half of a paragraph the layout cut
    # (E132, the pairing table). Which blocks go together is `unit_reading_group`'s
    # one answer; the units handed over whole by `candidate_step_units` come back as
    # the same rows and are not added twice. Each group is handed over in the
    # package's own order, the anchor in its natural place among the blocks it
    # brought (E159): delivered anchor-first, a model read the end of a passage before
    # its start. The blocks brought along are not charged to the delivery budget, as
    # E147's halves never were.
    with_halves: list[tuple[str, str, str]] = []
    emitted: set[tuple[str, str]] = set()
    for revision, block_id, _ in delivered_sources:
        if (revision, block_id) in emitted:
            # already handed over inside an earlier anchor's group, in its place there
            continue
        source_document = sources.document(revision)
        package = source_document.package
        assert package is not None  # `searchable` names only blocks of a loaded package
        position = {b.block_id: index for index, b in enumerate(package.blocks)}
        group = unit_reading_group(
            source_document.units,
            block_id,
            partners=source_reader.continuity(revision).partners,
            readable=source_document.readable_ids,
        )
        delivered_group: list[tuple[str, str, str]] = []
        for member in group:
            site = (revision, member)
            # a member with no location is a block of no text: granted, not delivered
            member_location = searchable.get(revision, {}).get(member)
            if site not in emitted and member_location is not None:
                emitted.add(site)
                delivered_group.append((revision, member, member_location))
        delivered_group.sort(key=lambda row: position[row[1]])
        with_halves.extend(delivered_group)
    delivered_sources[:] = with_halves
    selections = source_reader.selections
    delivered_selections: set[str] = set()
    # Only the verified final handoff grants inherited citation authority. It does
    # not count as a read in this task, including after capacity-driven deferral.
    inherited_record_sources = set(context.get("record_selections", {})) if current_updates else set()
    deferred_selections: set[str] = set()
    citation_policy = FINAL_CITATION_POLICY if current_updates else SOURCE_CITATION_POLICY

    def handed_over() -> set[str]:
        """Which of those the Host read for the task rather than the model asking for it.

        A pre-read enters `delivered_selections` like any other read, so without this the
        round cannot tell evidence a task went and got from evidence it was handed and
        never used -- and the completion gate would be satisfied by the Host's own work.
        It is read off the first message's own delivery ledger, written when the task
        starts and carried by its checkpoint: it used to be a set filled in while the
        Host pre-read, which a resumed task never does, so a resumed task called every
        passage it had been handed its own (E189).
        """
        ledger = loop.history[0].get(DELIVERED_KEY) if loop.history else None
        reply = ledger.get("reply") if isinstance(ledger, dict) else None
        rows = reply.get("items", ()) if isinstance(reply, dict) else ()
        return {
            str(row["selection_id"])
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("selection_id"), str)
        }

    # The route step each paragraph the reading task was handed is shown under; it goes
    # onto the receipt's read scope, which is what publication groups an uncited
    # delivery by. Derived from the task's own inputs, so a resumed task has it too.
    shown_under: dict[str, str] = (
        {
            sid: str(reading_route[index]["reaction_block_id"])
            for sid, index in passage_steps(context.get("paragraphs", ()), reading_route).items()
            if index is not None
        }
        if paragraph_input
        else {}
    )
    source_observations = source_reader.source_observations
    read_errors: list[dict[str, object]] = []
    requests: list[ArtifactRef] = []
    audits: list[ArtifactRef] = []
    failure: str | None = None
    closed_out: str | None = None
    thought_tokens = 0
    model_seconds = 0.0
    loop.finished = False

    def delivered_scopes(*, include_next_request: bool = False) -> tuple[dict[str, Any], ...]:
        """One entry per distinct source range in the ledger, saying who put it there
        and, for a paragraph the reading task was handed, the route step it was shown
        under.

        The identity is copied rather than handed out: the flag belongs to the ledger
        line, not to the saved selection the reader bound. Progress for the request
        being assembled includes the immediately preceding tool READ reply; the
        write validator still waits for the next provider response before citing it.
        """
        scopes: dict[bytes, dict[str, Any]] = {}
        handed = handed_over()
        for sid, value in selections.items():
            if sid not in delivered_selections and (not include_next_request or sid in deferred_selections):
                continue
            identity = value["source_identity"]
            entry = scopes.setdefault(
                canonical_json_bytes(identity), {**identity, "delivered_by_host": False}
            )
            entry["delivered_by_host"] = entry["delivered_by_host"] or sid in handed
            if sid in shown_under:
                entry.setdefault("step", shown_under[sid])
        return tuple(scopes.values())

    def delivered_scopes_by_revision() -> dict[str, list[dict[str, Any]]]:
        """The sentence ranges actually delivered, by document revision, for the guard."""
        by_revision: dict[str, list[dict[str, Any]]] = {}
        for sid, value in selections.items():
            if sid in delivered_selections:
                identity = value["source_identity"]
                by_revision.setdefault(identity["document_revision_id"], []).append(
                    {
                        "source_id": identity.get("source_id"),
                        "start_sentence": identity.get("start_sentence", 0),
                        "end_sentence": identity.get("end_sentence", 0),
                    }
                )
        return by_revision

    def unread() -> tuple[dict[str, object], ...]:
        scopes = delivered_scopes()
        remaining: list[dict[str, object]] = []
        for doc in sources.active():
            revision = doc.registration.document_revision_id
            if doc.package is None:
                remaining.append({"document_revision_id": revision, "reason": doc.failure})
                continue
            for block in doc.package.blocks:
                if block.block_id not in doc.readable_ids:
                    continue
                count = len(block_sentence_spans(block.raw_text))
                covered = {
                    i
                    for s in scopes
                    if s["document_revision_id"] == revision and s["source_id"] == block.block_id
                    for i in range(s.get("start_sentence", 0), s.get("end_sentence", 0))
                }
                if count and covered != set(range(count)):
                    remaining.append(
                        {
                            "document_revision_id": revision,
                            "source_id": block.block_id,
                            "reason": "NOT_FULLY_READ",
                        }
                    )
        return tuple(remaining)

    def receipt(proposed: ContractModel | None) -> InvestigationReceipt:
        return InvestigationReceipt(
            kind=kind,
            paper_id=sources.paper_id,
            execution_id=execution_id,
            baseline_machine_ref=baseline_machine_ref,
            input_ref=input_ref,
            answer=cast(InvestigationAnswer, proposed) if supplement else None,
            raw_answer=proposed.model_dump(mode="json")
            if proposed is not None and not supplement
            else None,
            observations=tuple(source_observations),
            selections=selections,
            record_source_version=sealed_task_input.get("record_source_version") if recording else None,
        )

    origin_index = InvestigationOrigins(store.read_bytes)
    origin_cache: tuple[object, dict[tuple[str, str], dict[str, str]]] | None = None

    from tsdaf.workflow.reading_measurement_context import (
        MeasurementContexts, MEASUREMENT_SNAPSHOT_MEDIA, selected_binding,
    )
    measurement_context = MeasurementContexts(store.read_bytes)

    def measurement_machines(answer):
        return measurement_context.machines(
            {f.item_id: f._measurement_binding for f in getattr(answer, "findings", ())
             if getattr(f, "_measurement_binding", None) is not None}, accepted_for_edits)

    def bind_measurement_item(collection, item, previous, flight, entry_key):
        if collection != "findings" or entry_key is None or (not current_updates and item.aspect != "yield"):
            return item
        raw = flight.context.call["arguments"]
        if isinstance(raw, str):
            raw = decode_tool_arguments(raw)
        submitted = raw[collection][int(entry_key.split(":")[1])]
        request_binding = measurement_context.request(flight.context.record, accepted_for_edits)
        if request_binding is None:
            return item
        from tsdaf.lanes.literature.finding_updates import comparison_value
        value = comparison_value(item, previous)
        prior_binding = getattr(previous, "_measurement_binding", None)
        if current_updates and previous is not None and prior_binding is None:
            prior_binding = {"target": None, "resolved_intent": "RECORD"}
        bound = selected_binding(value, prior_binding, submitted, request_binding,
            current_updates=current_updates,
            machine=measurement_context.record(request_binding["snapshot_ref"]) if current_updates else None)
        if current_updates:
            from tsdaf.lanes.literature.finding_updates import current_target
            if bound is not None:
                current = current_public_machine()
                unavailable = procedure_source_preview().get("unavailable")
                if unavailable:
                    if prior_binding is None or prior_binding.get("target") != bound["target"]:
                        raise ValueError("Current result could not verify this selection: " + str(unavailable))
                else:
                    current = {**current, "_material_bindings": repaired(writer.current()).material_bindings}
                    if current_target(value, current) != bound["target"]:
                        raise ValueError("selected result is no longer present in the accepted result")
            item = item.model_copy()
            item._measurement_binding = bound
            return item
        words = value.get("words")
        choice = words.get("measurement") if isinstance(words, Mapping) else None
        target = choice.get("same_as") if isinstance(choice, Mapping) else None
        previous_target = (
            previous.words.same_measurement_as
            if previous is not None and previous.aspect == "yield" else None
        )
        if target is not None and target != previous_target:
            from tsdaf.contracts.literature.objects import reported_outcomes

            current = current_public_machine()
            unavailable = procedure_source_preview().get("unavailable")
            if unavailable:
                raise ValueError("Current result could not verify this new measurement selection: "
                                 + str(unavailable))
            available = {o["outcome_id"] for o in reported_outcomes(current)}
            if target not in available:
                raise UnknownReference(
                    "words.measurement.same_as", target, sorted(available),
                    "selected measurement is no longer present in the accepted result",
                )
        item = item.model_copy()
        item._measurement_binding = bound
        return item

    def current_origins() -> dict[tuple[str, str], dict[str, str]]:
        nonlocal origin_cache
        if sparse_reading and writer.in_flight_write is not None:
            # Registered batches in this call precede its sealed call receipt.
            # Use the existing verified fold, never a partial sealed-history fallback.
            return dict(sparse_accepted_preview().creation_origins)
        key = (
            tuple(audits),
            tuple(requests),
            content_hash(writer.current().model_dump(mode="json")),
        )
        if origin_cache is not None and origin_cache[0] == key:
            return origin_cache[1]
        origins = origin_index.derive(
            receipt(writer.current()).model_copy(
                update={"audit_refs": tuple(audits), "request_refs": tuple(requests)}
            ),
            allow_call_prefix=True,
        )
        origin_cache = key, origins
        return origins

    record_binding_cache = None

    def current_record_bindings():
        nonlocal record_binding_cache
        from tsdaf.workflow.record_source_feedback import verified_record_bindings
        flight = writer.in_flight_write
        answer = flight.baseline if flight is not None else writer.current()
        key = (tuple(audits), tuple(requests), content_hash(answer.model_dump(mode="json")))
        if record_binding_cache is None or record_binding_cache[0] != key:
            proven = verified_record_bindings(
                receipt(answer).model_copy(update={"audit_refs": tuple(audits), "request_refs": tuple(requests)}),
                read_bytes=store.read_bytes, accepted_refs=accepted_for_edits,
                restore_domain=restore_investigation_domain, allow_call_prefix=True,
            )
            record_binding_cache = key, proven
        return copy.deepcopy(record_binding_cache[1])

    def record_item_feedback(collection, item, previous, flight, key):
        if collection != "records" or sealed_task_input.get("record_source_version") != 1:
            return {}
        from tsdaf.workflow.record_source_feedback import bind_record_sources, sources_submitted
        explicit = sources_submitted(flight.context.call["arguments"], key)
        if previous is not None and not explicit:
            bindings = current_record_bindings().get(item.item_id)
            # Earlier accepted batches and the current atomic candidate are provisional
            # until this call succeeds; they never mutate the sealed history index.
            prior = [entry for batch in flight.accepted for entry in batch] + list(flight.pending)
            for entry in prior:
                if (entry.get("value") or {}).get("item_id") == item.item_id and "source_bindings" in entry:
                    bindings = entry["source_bindings"]
            if bindings is None:
                raise ValueError("saved record has no accepted source-binding history")
        else:
            bindings = bind_record_sources(item.selection_ids, selections=selections,
                                           authorized=delivered_selections)
        return {"source_binding_version": 1, "source_bindings": copy.deepcopy(bindings)}

    def current_findings_digest() -> str:
        """Accepted content and proven creation generations, both host-maintained."""
        current_origins()
        content = {
            "answer": writer.current().model_dump(mode="json"),
            "measurement_bindings": {f.item_id: f._measurement_binding
                for f in getattr(writer.current(), "findings", ()) if getattr(f, "_measurement_binding", None)},
        }
        if recording and sealed_task_input.get("record_source_version") == 1:
            # A source-only rebind changes the current read view, not the record's
            # scientific edit identity. Private-only hash changes do not change it.
            content["source_binding_view"] = {
                handle: source_binding_view(bindings)
                for handle, bindings in current_record_bindings().items()
            }
        flight = writer.in_flight_write
        if sparse_reading and flight is not None:
            # The exact proven execution prefix also preserves child birth changes;
            # no scientific-field whitelist or provisional branch enters this key.
            return content_hash({"content": content, "accepted_prefix": [
                flight.context.interaction_sha256, flight.context.call_index, flight.accepted,
            ]})
        return origin_index.generation(content)

    def handled_snapshot() -> list[dict[str, Any]]:
        """The same current handoff the progress view derives, for versioned reading."""
        prior = located_left.get("source_handoff") if located_left is not None else None
        if not isinstance(prior, Mapping):
            return []
        current = writer.current()
        answer = current if isinstance(current, ParagraphSet) else None
        handled = source_handoff(
            prior.get("catalog", ()),
            [*prior.get("delivered", ()), *delivered_scopes()],
            [*prior.get("handed_off", ()), *paragraph_scopes(answer, selections)],
        )["handled_sources"]
        # The public read location already names this collection. Do not wrap it in
        # another record field that a directory reader must open before seeing rows.
        return cast(
            list[dict[str, Any]],
            round_left_view({"remaining_sources": handled})["remaining_sources"],
        )

    repair_cache: tuple[Any, RepairedReading] | None = None
    projection_memo = RepairProjectionMemo()
    sparse_preview_cache = None
    narrative_cache: tuple[str, dict[str, Any]] | None = None
    narrative_applications: list[dict[str, Any]] = []

    def sparse_prepared_preview(flight):
        nonlocal sparse_preview_cache
        flight_key = (execution_id, flight.context.interaction_sha256,
                      flight.context.call_index, tuple(audits), tuple(requests))
        if (sparse_preview_cache is None or sparse_preview_cache[0] != flight_key
                or sparse_preview_cache[3] is not flight.baseline
                or not sparse_preview_cache[1] <= accepted_for_edits):
            authority = frozenset(accepted_for_edits)
            original = receipt(flight.baseline).model_copy(
                update={"audit_refs": tuple(audits), "request_refs": tuple(requests)}
            )
            prepared = prepare_sparse_repair_preview(
                original, machine=machine or {}, store=store,
                accepted_refs=tuple(authority), restore_domain=restore_investigation_domain,
                inherited_selections=inherited_selections,
                projection_memo=projection_memo,
            )
            sparse_preview_cache = (flight_key, authority, prepared, flight.baseline)
        return sparse_preview_cache[2]

    def sparse_accepted_preview():
        flight = writer.in_flight_write
        # Ignore pending even during group construction or provisional validation.
        return sparse_prepared_preview(flight)(replace(flight, candidate=None))

    def repaired(proposed: ReadingAnswer) -> RepairedReading:
        nonlocal repair_cache, sparse_preview_cache
        if sparse_reading:
            flight = writer.in_flight_write
            if flight is not None:
                preview = sparse_prepared_preview(flight)(flight)
                for entry in flight.pending:
                    if entry["item"] in preview.commands:
                        writer.bind_pending_repair(entry["item"], entry["value"],
                                                  preview.commands[entry["item"]])
                return preview.projection
            baseline = writer.current()
            key = (tuple(audits), tuple(requests), content_hash(baseline.model_dump(mode="json")),
                   frozenset(accepted_for_edits))
            if repair_cache is None or repair_cache[0] != key:
                original = receipt(baseline).model_copy(
                    update={"audit_refs": tuple(audits), "request_refs": tuple(requests)}
                )
                plan = verify_sparse_repair_plan(
                    original, machine=machine or {}, store=store,
                    accepted_refs=tuple(accepted_for_edits),
                    restore_domain=restore_investigation_domain,
                    allow_call_prefix=True,
                    inherited_selections=inherited_selections,
                    projection_memo=projection_memo,
                )
                repair_cache = key, plan.replay(
                    proposed, machine=machine or {}, context=context,
                    selections=selections, store=store,
                    inherited_selections=inherited_selections,
                    projection_memo=projection_memo,
                )
            return repair_cache[1]
        current_origins()
        repair_origins = origin_index.repair_birth_origins
        # Validation may ask about an unaccepted proposal. Its own content must
        # participate, while births remain those proven by accepted history.
        key = origin_index.generation(proposed.model_dump(mode="json"))
        if repair_cache is None or repair_cache[0] != key:
            repair_cache = (
                key,
                projection_memo.replay(
                    proposed,
                    machine=machine or {},
                    context=context,
                    selections=selections,
                    store=store,
                    delivered=delivered_selections,
                    repair_origins=repair_origins,
                    inherited_selections=inherited_selections,
                ),
            )
        return repair_cache[1]

    def current_machine() -> Mapping[str, Any] | None:
        if reading:
            return repaired(cast(ReadingAnswer, writer.current())).machine
        return machine

    def current_narratives() -> dict[str, Any]:
        """This task's own public narrative projection, under its actual input scope."""
        nonlocal narrative_cache
        key = current_findings_digest()
        if narrative_cache is None or narrative_cache[0] != key:
            if reading:
                # Use the actual application candidate, including refused dependent
                # increments, rather than naming an optimistic repair-only view.
                procedure_source_preview()
                if narrative_cache is None or narrative_cache[0] != key:
                    return {"paper_narratives": []}
                return narrative_cache[1]
            narrative_applications.clear()
            narratives = public_narratives(
                receipt(writer.current()),
                current_machine(),
                creation_keys=narrative_creation_keys(sources.paper_id, current_origins()),
                applications=narrative_applications,
            )
            narrative_cache = key, {"paper_narratives": narratives}
        return narrative_cache[1]

    def narrative_publication() -> dict[str, Any]:
        current_narratives()
        return {
            row["item_id"]: {
                "state": row["state"],
                **(
                    {"public_location": row["public_targets"][0]["object_id"]}
                    if row["public_targets"]
                    else {"reasons": row["reasons"]}
                ),
            }
            for row in narrative_applications if row["collection"] == "narratives"
        }

    material_success = None

    def check_materials(answer, held, candidate_machine, *, material_bindings=None):
        nonlocal material_success
        # Compare complete compiler inputs, including source contents, not just
        # selection names or which item the writer calls fresh. Failures are not cached.
        inputs = (answer, candidate_machine, material_bindings, held.selections)
        if material_success is not None and inputs == material_success:
            return
        validate_material_compilation(
            answer, held, candidate_machine, material_bindings=material_bindings)
        material_success = copy.deepcopy(inputs)

    def validate_answer(
        proposed: ContractModel,
        *,
        final: bool,
        fresh: Callable[[ContractModel], bool] = lambda item: True,
    ) -> None:
        # The write-time checks read the bound selections and nothing else of a receipt,
        # so they are handed those rather than a whole receipt built -- and the whole
        # answer revalidated inside it -- for every accepted item (batch 4, 2026-09-24).
        if recording and sealed_task_input.get("record_source_version") == 1:
            return  # Source proposals are bound separately in accepted item feedback.
        held = SelectionsHeld(selections)
        cited = cited_selection_ids(proposed, selections)
        authorized = delivered_selections | inherited_record_sources
        if not cited <= authorized:
            unknown = sorted(cited - authorized - selections.keys())
            if unknown:
                raise UnknownReference(
                    "selection_ids",
                    unknown[0],
                    sorted(authorized),
                    citation_policy,
                )
            raise ValueError(citation_policy)
        bind_candidate_selections(cited)
        if recording:
            return
        if reading:
            verify_reading_update_mode(proposed, sealed_task_input)
            validate_compound_revision(
                cast(ReadingAnswer, writer.current()),
                cast(ReadingAnswer, proposed),
                held,
                machine or {},
            )
            before = cast(ReadingAnswer, writer.current())
            answer = cast(ReadingAnswer, proposed)
            view = repaired(answer)
            try:
                validate_reading(
                    answer,
                    held,
                    view.machine,
                    (lambda item: True)
                    if before.skeleton_repairs != answer.skeleton_repairs
                    else fresh,
                    material_bindings=view.material_bindings,
                    material_check=check_materials,
                    measurement_machines=measurement_machines(answer),
                    method_targets=method_targets,
                    general_methods_units=general_methods_first_blocks(
                        (d.registration.document_revision_id, d.units) for d in sources.documents),
                )
            except AmbiguousMethodUnit as error:
                offered_reads = [navigation_entry(location)
                                 for location, binding in source_locations.items()
                                 if (binding["document_revision_id"], binding["source_id"])
                                 in error.targets and location in readable_source_locations]
                raise ValueError(str(error) + " Read the intended method for context: "
                                 + json.dumps(offered_reads, ensure_ascii=False)) from error
            if before.narratives != answer.narratives or (
                answer.narratives and before.skeleton_repairs != answer.skeleton_repairs
            ):
                public_narratives(receipt(answer), view.machine)
            return
        proposed = cast(ParagraphSet | InvestigationAnswer, proposed)
        if not supplement:
            validate_paragraphs(cast(ParagraphSet, proposed), held, machine or {})
        else:
            public_narratives(receipt(proposed), machine)
        coverage_items = getattr(proposed, "coverage", ())
        coverage = {c.document_revision_id: c for c in coverage_items}
        if len(coverage) != len(coverage_items):
            raise ValueError("duplicate coverage document identity")
        allowed = {str(d["document_revision_id"]) for d in assessed}
        for revision, assessment in coverage.items():
            if revision not in allowed:
                raise UnknownReference(
                    "document_revision_id",
                    revision,
                    sorted(allowed),
                    "coverage names a document this task does not assess; "
                    "assess the documents listed in documents",
                )
            for sid in assessment.selection_ids:
                if (
                    sid not in selections
                    or selections[sid]["source_identity"]["document_revision_id"] != revision
                ):
                    matching = sorted(
                        sid
                        for sid, entry in selections.items()
                        if entry["source_identity"]["document_revision_id"] == revision
                    )
                    raise UnknownReference(
                        "selection_ids",
                        sid,
                        matching,
                        "coverage assessment must cite inspected evidence from its own revision",
                    )
        if proposed.status == "NO_ADDITIONAL_INFORMATION":
            collections = spec.collections(task=task, unified_expression=unified_expression, current_updates=current_updates)
            substantive = [key for key in collections if key not in {"coverage", "limitations"}]
            if any(getattr(proposed, key) for key in substantive):
                raise ValueError(
                    "no-increment disposition cannot include new increments"
                )

    writer = InvestigationWriter(
        collections=spec.collections(task=task, unified_expression=unified_expression, current_updates=current_updates),
        write_collections=write_collections,
        execution_id=execution_id,
        seal=seal,
        validate=validate_answer,
        register=register_resource,
        bind_item=bind_measurement_item,
        item_feedback=record_item_feedback if recording else None,
        resource_locations=resource_locations,
        empty_answer=lambda: spec.empty_answer(
            execution_id, unified_expression=unified_expression, current_updates=current_updates
        ),
        read_answer=spec.read_answer,
        finish_model=spec.finish_model(),
        dependency_groups=(
            (lambda raw, saved: sparse_reading_write_groups(
                raw, saved, repaired(cast(ReadingAnswer, writer.current()))
            )) if sparse_reading else reading_write_groups
        ) if kind == "EXPERIMENTAL_READING" else None,
        public_machine=lambda candidate: (
            repaired(cast(ReadingAnswer, candidate)).machine if reading else machine
        ),
        subject_names=lambda: active_task_names or names_now(),
    )

    client = provider_binding.require_client(client, label=kind)
    scope_sha256 = content_hash(
        [
            prompt,
            declarations,
            NAMING_VERSION,
            INVESTIGATION_VERSION, READ_DELIVERY_VERSION,
            client.context_policy,
            kind,
            context,
            allowed_main_visual_ids,
            baseline_machine_ref.model_dump(mode="json") if baseline_machine_ref else None,
            machine,
            observations,
            [d.identity() for d in sources.active()],
            # Two block tasks of one paper share every other input; without their own
            # scope here the second is served the first one's sealed answer.
            block_ids,
        ] + ([repair_mode] if sparse_reading else [])
    )
    restored = restore_investigation(
        recovery_artifacts,
        execution_id=execution_id,
        input_ref=input_ref,
        resources_sha256=scope_sha256,
        store=store,
    )
    checkpoint_ref, writer.answer_ref = restored.ref, restored.answer_ref
    if restored.ref is not None:
        loop = restored.loop
        selections.update(restored.selections)
        resource_locations.update(restored.resource_locations)
        source_observations.extend(restored.observations)
        restored_observations = {o["observation_id"]: o for o in source_observations}
        for binding in selections.values():
            source_reader.rebind_delivered_text(
                binding, restored_observations.get(binding["evidence_ref"]["observation_id"])
            )
        read_errors.extend(restored.errors)
        requests, audits = restored.requests, restored.audits
        delivered_selections = restored.delivered
        if current_updates:
            deferred_selections.update(set(selections) - delivered_selections - handed_over())
        if writer.answer_ref is not None:
            writer.restore_answer(read_artifact(store.read_bytes, writer.answer_ref))
    delivery = WriteDelivery(loop.history)
    from tsdaf.workflow.investigation_edits import InvestigationEdits

    edit_history = InvestigationEdits(
        read_bytes=store.read_bytes, audits=lambda: audits,
        accepted=lambda: accepted_for_edits, binding=lambda: turn_binding,
        restore_domain=restore_investigation_domain,
    )

    def seal_edit(value, media):
        ref = seal(value, media)
        audits.append(ref)
        return ref

    def prepare_write(name, raw, ctx):
        if not open_expression or not isinstance(raw, dict):
            return None
        if not any(
            isinstance(v, dict) and "edit" in v
            for rows in raw.values() if isinstance(rows, list) for v in rows
        ):
            return PreparedWrite(raw)
        origins = current_origins()
        return edit_history.prepare(
            raw, ctx, writer=writer, delivery=delivery, origins=origins,
            action_origins=origin_index.action_origins, seal=seal_edit, register=register_resource,
        )
    sizes = {
        "history": len(loop.history) if restored.ref else 0,
        "outbox": len(loop.outbox),
        "observations": len(source_observations),
        "errors": len(read_errors),
        "requests": len(requests),
        "audits": len(audits),
    }
    saved_selections = set(selections)
    saved_delivered = set(delivered_selections)
    saved_resources = set(restored.resource_locations)

    def save_progress() -> None:
        nonlocal checkpoint_ref
        sizes["outbox"] = min(sizes["outbox"], len(loop.outbox))
        checkpoint = InvestigationCheckpoint(
            execution_id=execution_id,
            input_ref=input_ref,
            resources_sha256=scope_sha256,
            previous_ref=checkpoint_ref,
            answer_ref=writer.sealed_answer_ref(),
            history_added=tuple(loop.history[sizes["history"] :]),
            selections_added={k: v for k, v in selections.items() if k not in saved_selections},
            resource_locations_added={
                k: v for k, v in resource_locations.items() if k not in saved_resources
            },
            observations_added=tuple(source_observations[sizes["observations"] :]),
            read_errors_added=tuple(read_errors[sizes["errors"] :]),
            request_refs_added=tuple(requests[sizes["requests"] :]),
            audit_refs_added=tuple(audits[sizes["audits"] :]),
            outbox_added=tuple(loop.outbox[sizes["outbox"] :]),
            pending_ref=ArtifactRef.model_validate(loop.pending["interaction_ref"])
            if loop.pending
            else None,
            next_call=loop.next_call,
            iteration=loop.iteration,
            finished=loop.finished,
            delivered_added=tuple(sorted(delivered_selections - saved_delivered)),
        )
        checkpoint_ref = accept_artifact(
            checkpoint,
            media_type=INVESTIGATION_PROGRESS_MEDIA,
            put_bytes=lambda payload, media: store.put_bytes(payload, media_type=media),
            read_bytes=store.read_bytes,
            bind=bind,
        )
        sizes.update(
            history=len(loop.history),
            outbox=len(loop.outbox),
            observations=len(source_observations),
            errors=len(read_errors),
            requests=len(requests),
            audits=len(audits),
        )
        saved_selections.update(selections)
        saved_delivered.update(delivered_selections)
        saved_resources.update(resource_locations)
        # Only release unused V3 capacity after its accepted source/tool cursor is
        # durable. An admitted action is never interrupted between its figure turns.
        figure_reservations.close()

    def deliver(
        parts: list[dict[str, Any]],
        items: Sequence[dict[str, Any]] = (),
        rows: Sequence[dict[str, Any]] = (),
    ) -> None:
        """Put delivered text into the first message and register it, in one place.

        Every passage the host hands over with the task comes through here -- a block
        task's step sources and the reading task's located paragraphs alike -- so each
        is registered where the content window recognizes a later read of it as
        already in context, and each counts against the one first-message budget.
        The registration says the text travels with the first message, which the
        projection resends whole on every request: the window treats it as always in
        context and never re-announces it.
        """
        if len(items) == 1:
            parts = [{**part, READ_ITEM_KEY: item_name(items[0])} for part in parts]
        loop.history[0]["content"].extend(parts)
        if items:
            delivered = loop.history[0].setdefault(
                DELIVERED_KEY,
                {"items": [], "reply": {"outcome": "READ", "items": []}, IN_FIRST_MESSAGE: True},
            )
            delivered["items"].extend(items)
            delivered["reply"]["items"].extend(rows)

    def deliver_step_sources() -> None:
        """Read this task's own step sources for it, into the message it starts from.

        A step names the observations it was built from, each observation's anchor names
        the blocks it was read in, and ``step_sources`` walks that; validated offline
        across two papers and 23 steps with no step left unreached. The passages go into
        the first message, which the projection shows whole on every request, and the
        same reads are entered in the delivered ledger the content window keeps, so a
        model that reads one of them again is answered IN_CONTEXT instead of spending a
        turn on it -- the shape a reaction window's first-turn delivery used (retired
        with the windows, 2026-09-23).

        Only on a fresh task. A resumed one restores both the message and the bound
        selections from its checkpoint, and reading again would seal a second copy of
        every observation. A source this cannot read is this task's own failure, like any
        other unreadable authorized source: the round publishes with what the rest found.
        """
        verified_text: set[str] = set()
        offered_attachments: set[str] = set()
        for revision, block_id, location in delivered_sources:
            reference = attachment_reference(revision, block_id)
            if reference is not None:
                if reference["location"] not in offered_attachments:
                    offered_attachments.add(reference["location"])
                    deliver(
                        [
                            {
                                "type": "text",
                                "text": canonical_json_bytes(
                                    {"technical_attachment": reference}
                                ).decode(),
                            }
                        ]
                    )
                continue
            # The same name a model read of this whole block would be given, computed the
            # one way `plan` computes it. A pre-read under a name of its own would be a
            # second identity for one passage: the model would read it again, be told
            # IN_CONTEXT, and have no id in hand to cite what it had been handed.
            binding = {
                key: value
                for key, value in source_locations[location].items()
                if key not in ("medium", "selection_id")
            }
            selection_id = "read:" + content_hash(binding)[:24]
            parts = located_source_parts(
                source_reader.read(
                    SourceRead(
                        selection_id=selection_id,
                        document_revision_id=revision,
                        source_id=block_id,
                    ),
                    (),
                    verified_text,
                )
            )
            matched_candidate = next(
                (
                    c
                    for c in candidates
                    if (c["document_revision_id"], c["source_id"]) == (revision, block_id)
                ),
                None,
            )
            if matched_candidate is not None:
                _tag_candidate_parts(parts, matched_candidate)
            deliver(
                parts,
                [{"location": location, "select": {"kind": "whole"}}],
                [
                    {
                        "outcome": "READ",
                        "location": location,
                        "selection_id": selection_id,
                        "origin": "SOURCE",
                        "coverage": "SELECTED_SOURCE",
                    }
                ],
            )

    if restored.ref is None:
        if paragraph_input:
            inherited = context.get("paragraph_selections", {})
            references = {
                sid: {**reference, "locator_source": SELECTION_LOCATION_PREFIX + sid}
                for sid, binding in inherited.items()
                if (
                    reference := attachment_reference(
                        binding["source_identity"]["document_revision_id"],
                        binding["source_identity"]["source_id"],
                    )
                )
                is not None
            }
            for sid, text in delivered_paragraphs(
                context.get("paragraphs", ()),
                inherited,
                reading_route,
                references=references,
            ):
                if sid is not None:
                    selections[sid] = copy.deepcopy(inherited[sid])
                    source_reader.rebind_delivered_text(
                        selections[sid],
                        next(
                            (
                                o
                                for o in context.get("paragraph_observations", ())
                                if o["observation_id"]
                                == selections[sid]["evidence_ref"]["observation_id"]
                            ),
                            None,
                        ),
                    )
                where = None if sid is None else SELECTION_LOCATION_PREFIX + sid
                deliver(
                    [{"type": "text", "text": text}],
                    [] if where is None else [{"location": where, "select": {"kind": "whole"}}],
                    []
                    if where is None
                    else [
                        {
                            "outcome": "READ",
                            "location": where,
                            "selection_id": sid,
                            "origin": "SOURCE",
                            "coverage": "SELECTED_SOURCE",
                        }
                    ],
                )
            observation_ids = {
                binding["evidence_ref"]["observation_id"] for binding in selections.values()
            }
            source_observations.extend(
                copy.deepcopy(observation)
                for observation in context.get("paragraph_observations", ())
                if observation["observation_id"] in observation_ids
            )
        # These are original sources the recording task actually read, including
        # its later image answers. Deliver once without re-running that reader.
        for sid, original in context.get("record_selections", {}).items():
            if sid in handed_over():
                continue
            binding = copy.deepcopy(original)
            observation = next(o for o in context["record_observations"]
                if o["observation_id"] == binding["evidence_ref"]["observation_id"])
            source_reader.rebind_delivered_text(binding, observation)
            selections[sid] = binding
            if not any(o["observation_id"] == observation["observation_id"] for o in source_observations):
                source_observations.append(copy.deepcopy(observation))
            location = SELECTION_LOCATION_PREFIX + sid
            text = binding.get("exact_text") or binding.get("figure_reading")
            if not text:
                raise ValueError("record source has no delivered text or figure interpretation")
            question = binding["source_identity"].get("question")
            header = f"[{sid}]" + (" · question: " + question if question else "")
            deliver([{"type": "text", "text": header + "\n" + text}], [{"location": location, "select": {"kind": "whole"}}],
                [{"outcome": "READ", "location": location, "selection_id": sid,
                  "origin": "SOURCE", "coverage": "SELECTED_SOURCE"}])
        deliver_step_sources()
        # Only the figures belonging to the actually delivered passages, once. Do not
        # turn the whole authorized image inventory into another first-message menu.
        linked: dict[str, dict[str, Any]] = {}
        for value in selections.values():
            identity = value["source_identity"]
            revision, block = identity["document_revision_id"], identity["source_id"]
            for entry in passage_figures(revision, block):
                linked.setdefault(entry["location"], entry)
        if linked:
            deliver(
                [
                    {
                        "type": "text",
                        "text": canonical_json_bytes(
                            {"figures_in_delivered_passages": list(linked.values())}
                        ).decode(),
                    }
                ]
            )
        save_progress()

    # Candidate settlement is derived from the first message's evidence ledger on
    # both fresh and resumed tasks. Restoring the saved answer alone is insufficient:
    # newly derived candidates otherwise have no selection_id and every accepted
    # citation is reported unanswered. Reuse the actual held selection; do not reread
    # a source or turn a predicted identity into evidence of delivery.
    def bind_candidate_selections(cited: Collection[str] = ()) -> None:
        # Offered attachments are not pre-read. Bind them after a real read, using
        # the same delivered evidence on fresh, resumed and subsequent turns.
        available = handed_over() | delivered_selections
        candidate_sites = {
            (
                entry["source_identity"]["document_revision_id"],
                entry["source_identity"]["source_id"],
            ): sid
            for sid in sorted(available, key=lambda sid: (sid in cited, sid))
            if (entry := selections.get(sid)) is not None
        }
        for candidate in candidates:
            site = (candidate["document_revision_id"], candidate["source_id"])
            if site in candidate_sites:
                candidate["selection_id"] = candidate_sites[site]

    bind_candidate_selections()

    def sentence_spans(text: str) -> list[tuple[int, int]]:
        return [(s.char_start, s.char_end) for s in block_sentence_spans(text)]

    def readable_document(revision: str) -> Any:
        doc = sources.document(revision)
        if doc.package is None:
            raise ReadFailure("UNAVAILABLE", "No accepted readable source is available.")
        return doc

    def seal_readable(value: Any) -> dict[str, Any]:
        return store.put_bytes(
            canonical_json_bytes(value), media_type="application/vnd.tsdaf.readable-record+json"
        ).model_dump(mode="json")

    def source_target(
        root: str,
        source_binding: dict[str, Any],
        *,
        saved: bool,
        created: tuple[ArtifactRef, ArtifactRef],
        derived: str | None = None,
    ) -> SourceTarget:
        medium = source_binding.pop("medium")
        inherited_question = source_binding.pop("question", None)
        revision, source_id = source_binding["document_revision_id"], source_binding["source_id"]

        def dimensions() -> tuple[int, int]:
            doc = readable_document(revision)
            visual = next(v for v in doc.package.visual_assets if v.visual_id == source_id)
            if visual.width is None or visual.height is None:
                raise ReadFailure("UNAVAILABLE", "Source image dimensions are unavailable.")
            return visual.width, visual.height

        def raw_text() -> str:
            doc = readable_document(revision)
            text: str = next(b.raw_text for b in doc.package.blocks if b.block_id == source_id)
            return text

        def plan(
            selection: Selection | None, region: tuple[int, int, int, int] | None, pixels: int
        ) -> Callable[[], ReadValue]:
            readable_document(revision)
            question = getattr(selection, "question", None) or inherited_question
            if question is not None and (medium != "visuals" or supplement):
                raise ReadFailure(
                    "UNSUPPORTED_SELECTION",
                    "A figure question requires an image answered in words.",
                )
            if medium == "visuals":
                if region is not None:
                    source_binding["region"] = region
            elif isinstance(selection, TextSelection):
                source_binding.update(selection.model_dump(exclude={"kind"}))
            elif isinstance(selection, WholeSelection):
                source_binding.update(start_sentence=0, end_sentence=None, adjacent_blocks=0)
            request = SourceRead.model_validate_json(
                canonical_json_bytes({"selection_id": "read:pending", **source_binding})
            )
            if not saved or selection is not None:
                # Derive a selection from the declared source range, not from whether
                # the caller spelled its default fields. A local read followed by a
                # unit read keeps the anchor's existing name instead of inventing (2).
                request = request.model_copy(
                    update={
                        "selection_id": "read:"
                        + content_hash(
                            {
                                **request.model_dump(
                                    mode="json", exclude={"selection_id"}, exclude_defaults=True
                                ),
                                **({"question": question} if question is not None else {}),
                            }
                        )[:24]
                    }
                )

            def perform_source() -> ReadValue:
                # The read happens, and is held back until the batch says it fits: what
                # it bound and what it minted are staged together by the reader, which
                # is the one place that knows a selection and its observation are one
                # act (`InvestigationSourceReader.staged`).
                with source_reader.staged() as accept_source:
                    parts = located_source_parts(
                        source_reader.read(request, created, set(), question=question)
                    )
                    if medium == "text" and (links := passage_figures(revision, source_id)):
                        parts.append(
                            {
                                "type": "text",
                                "text": canonical_json_bytes(
                                    {"figures_in_this_passage": links}
                                ).decode(),
                            }
                        )
                    bound = selections[request.selection_id]
                    pixels = (
                        bound["display"]["dimensions"][0] * bound["display"]["dimensions"][1]
                        if medium == "visuals"
                        else 0
                    )
                def accept_delivered_source():
                    accept_source()
                    deferred_selections.discard(request.selection_id)
                return ReadValue(
                    {
                        "outcome": "READ",
                        "location": SELECTION_LOCATION_PREFIX + request.selection_id,
                        "selection_id": request.selection_id,
                        "origin": "SOURCE",
                        "coverage": "SELECTED_SOURCE",
                        **({"derived": derived} if derived is not None else {}),
                    },
                    parts,
                    pixels,
                    accept_delivered_source,
                )

            return perform_source

        offered_region = source_binding.get("region")
        return SourceTarget(
            medium=medium,
            plan=plan,
            saved=saved,
            preview_location=root,
            preview_extra={"document_revision_id": revision},
            allow_adjacent=True,
            allow_region=medium == "visuals" and spec.image_mode in {"DIRECT", "FIGURE_READER"},
            region=tuple(offered_region) if offered_region is not None else None,
            dimensions=dimensions,
            raw_text=raw_text,
        )

    record_json_cache: tuple[ArtifactRef, Any] | None = None

    def record_json(artifact: ArtifactRef) -> Any:
        nonlocal record_json_cache
        # History checks typically visit many objects in one immutable snapshot.
        # Keep only that last document, not a growing cache of every prior version.
        if record_json_cache is None or record_json_cache[0] != artifact:
            record_json_cache = artifact, json.loads(read_artifact(store.read_bytes, artifact))
        return record_json_cache[1]

    def record_version(binding: dict[str, Any], pointer: str, value: Any) -> str:
        # The named object's content, not its parent snapshot or array position,
        # determines whether an earlier read still describes the current object.
        # Root references and locations independently preserve snapshot provenance.
        if pointer and binding.get("public_object_snapshot"):
            return "public-object:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()
        return str(resource_receipt(binding)["version"])

    def record_target(binding: dict[str, Any]) -> RecordTarget:
        artifact = ArtifactRef.model_validate(binding["artifact_ref"])

        def perform_record(pointer: str, selection: Selection | None, max_bytes=READ_BYTES) -> ReadValue:
            value = record_json(artifact)
            if pointer:
                value = pointer_value(value, pointer)
            version = record_version(binding, pointer, value)
            actual = resource_location(binding)
            projected = project_record_value(
                value,
                selection,
                location=actual + ("#" + pointer if pointer else ""),
                # A record's source location is a reference, not an item identity.
                # Only MODEL_WORK declares a semantic item key of its own.
                item_key="fact_id" if binding["origin"] == "MODEL_WORK" else None,
                sentence_spans=sentence_spans, max_bytes=max_bytes,
            )
            if binding.get("source_navigation"):
                project_source_directory(
                    projected,
                    value,
                    readable_locations=readable_source_locations,
                )
            pending_locations = {actual: binding}
            fragment = fragment_of(
                binding, projected, seal_value=seal_readable, pointer=pointer, selection=selection
            )
            if fragment is not None:
                if pointer and binding.get("public_object_snapshot"):
                    fragment["science_version"] = version
                selected_ref = ArtifactRef.model_validate(fragment["artifact_ref"])
                selected_binding = fragment
                actual = resource_location(selected_binding)
                pending_locations[actual] = selected_binding
                projected["location"] = actual
            else:
                selected_ref, selected_binding = artifact, binding
                if pointer and binding.get("public_object_snapshot"):
                    selected_binding = {**binding, "science_version": version}
            projected.update(resource_receipt(selected_binding))

            def accept_record_read() -> None:
                bind(artifact, selected_ref)
                resource_locations.update(pending_locations)

            return ReadValue(projected, accept=accept_record_read)

        return RecordTarget(perform_record, with_budget=perform_record)

    def resolve_read(location: str, selection: Selection | None, ctx: CallContext) -> ReadPlan:
        created = (ctx.request_ref, ctx.response_ref)
        if getattr(selection, "question", None) is not None and location.startswith("state:"):
            raise ReadFailure(
                "UNSUPPORTED_SELECTION", "A figure question requires an image answered in words."
            )
        reason = _read_reason(ctx)
        offered = source_locations.get(location) or {}
        whole_unit = getattr(selection, "scope", None) == "unit"
        if whole_unit and (
            offered.get("medium") != "text" or getattr(selection, "question", None) is not None
        ):
            raise ReadFailure(
                "UNSUPPORTED_SELECTION",
                "Unit scope requires an original authorized text source, not a saved selection, "
                "record or image. Read this location without unit scope for its own content.",
            )
        # A read of one block of a text source answers with the passage whole (E156):
        # the rest of the unit the reading placed it in, and the other half of a
        # paragraph the layout cut -- the same group delivery hands over, asked of the
        # same function. No reason is needed to get it; a document never read into
        # units (`units` is None) still gets its other half from the pairing table.
        unit_siblings: list[tuple[str, str]] = []
        # The pairing table's own members of that group: consulted once here, for the
        # group and for the widening below (E159), not twice.
        other_halves: list[tuple[str, str]] = []
        if offered.get("medium") == "text":
            revision, block_id = str(offered["document_revision_id"]), str(offered["source_id"])
            read_document = sources.document(revision)
            if whole_unit:
                unit = (
                    read_document.units.unit_containing(block_id)
                    if read_document.units is not None
                    else None
                )
                if unit is None or not all(
                    member in text_locations.get(revision, {}) for member in unit.block_ids
                ):
                    raise ReadFailure(
                        "UNSUPPORTED_SELECTION",
                        "No complete authorized text unit is available here. Read this location "
                        "without unit scope for its authorized passage.",
                    )
            partners = source_reader.continuity(revision).partners
            unit_siblings = [
                (revision, member)
                for member in unit_reading_group(
                    read_document.units,
                    block_id,
                    partners=partners,
                    readable=read_document.readable_ids,
                    explicit=whole_unit,
                )
                if member != block_id and member in searchable.get(revision, {})
            ]
            paired = partners.get(block_id, ())
            other_halves = [site for site in unit_siblings if site[1] in paired]
        halves: list[tuple[str, str]] = []
        if reason is not None:
            reasoned_reads.append({"location": location, "reason": reason})
            asked = selection.adjacent_blocks if isinstance(selection, TextSelection) else 0
            if reason in _WIDENS_BY_ONE_BLOCK:
                # The scope follows the reason -- and when the Host knows which
                # paragraph this one was cut from, that is the answer (E132), not the
                # block beside it: on decumbenine_B the other half stands three blocks
                # away, past a scheme.
                halves = other_halves
            widened = asked if halves else adjacent_blocks_for(reason, asked_for=asked)
            if halves:
                reasoned_reads[-1]["other_halves"] = [half for _, half in halves]
            if widened != asked and not whole_unit:
                # The scope follows the reason. A task that says a passage breaks off
                # mid-sentence is answered with the passage beside it, which is the
                # difference between going back to the source and paging the directory
                # until something looks right (E100).
                selection = (
                    selection.model_copy(update={"adjacent_blocks": widened})
                    if isinstance(selection, TextSelection)
                    else TextSelection(kind="text_range", adjacent_blocks=widened)
                )
                reasoned_reads[-1]["adjacent_blocks"] = widened

        def resolve_root(root: str) -> SourceTarget | RecordTarget | None:
            visual_aliases: dict[str, list[dict[str, Any]]] = {}
            for offered_location, offered_binding in source_locations.items():
                if (offered_binding["medium"] == "visuals"
                        and offered_location in readable_source_locations):
                    visual_aliases.setdefault(offered_binding["source_id"], []).append(
                        navigation_entry(offered_location)
                    )
            root = authorized_visual_location(root, visual_aliases)
            if root == SEARCH_LOCATION:

                def perform_search(pointer: str, selected: Selection | None, max_bytes=READ_BYTES) -> ReadValue:
                    if not isinstance(selected, QuerySelection):
                        raise ReadFailure(
                            "INVALID_READ",
                            "This location is searched: select kind 'query' with terms.",
                        )
                    # Exact authorized object names are navigation, not bag-of-words
                    # content queries. Reuse the same task names and canonical page
                    # identity as read/canonicalization; never infer an unknown address.
                    terms = selected.terms.strip().strip("\"'").rstrip(".").strip()
                    names = ctx.naming or names_now()
                    entries: list[dict[str, Any]] = []
                    if selected.mode == "TERMS":
                        for location, binding in source_locations.items():
                            aliases = (location, names.present(location), binding["source_id"])
                            if terms.casefold() not in {a.casefold() for a in aliases if a}:
                                continue
                            entries.append(navigation_entry(location))
                        # Names owns the selection-name/root rules; a selection's
                        # internal key is its sid, not its read location.
                        held = {**context.get("paragraph_selections", {}), **selections}
                        resolved = names.resolve({"location": terms})["location"]
                        sid = resolved.removeprefix(SELECTION_LOCATION_PREFIX)
                        if sid in held:
                            entries.append(
                                {
                                    "location": SELECTION_LOCATION_PREFIX + sid,
                                    "select": None,
                                }
                            )
                        # A shared author label can name more than one authorized image.
                        # Offer all of them, without choosing an attachment for the model.
                        for revision, visuals in figure_locations.items():
                            for visual_id, location in visuals.items():
                                label = figure_names.get((revision, visual_id))
                                if label and " ".join(terms.casefold().split()) == " ".join(
                                    label.casefold().split()
                                ):
                                    entries.append(
                                        {
                                            **navigation_entry(location),
                                            "kind": "figure",
                                            "label": label,
                                        }
                                    )
                        if not entries:
                            for catalog_document in catalog:
                                phase = sources.document(
                                    str(catalog_document["document_revision_id"])
                                ).registration.source_phase
                                for medium in ("text", "visuals"):
                                    for entry in cast(
                                        list[dict[str, Any]], catalog_document.get(medium, [])
                                    ):
                                        if (
                                            terms.casefold()
                                            == format_page_id(phase, entry["page"]).casefold()
                                        ):
                                            entries.append(navigation_entry(entry["location"]))
                        # A figure's name is asked for by a reader after the figure and by
                        # one after the text that discusses it. The second got an empty
                        # hits list and learned LITERAL by trial: across three papers
                        # (2026-10-10) 122 searches named a figure, most by tasks that had
                        # already read it. So a figure's name goes on to the text search
                        # below, which names the figure beside its hits.
                        figures = {e["location"] for e in entries if e.get("kind") == "figure"}
                        if entries and all(entry["location"] in figures for entry in entries):
                            entries = []
                        if entries:
                            # Keep one entry per object, preferring its labelled form.
                            entries = list({entry["location"]: entry for entry in entries}.values())
                            value: dict[str, Any] = {
                                "status": "MATCHES",
                                "mode": selected.mode,
                                "terms": selected.terms,
                                "hits": [],
                                "named_sources": entries[: selected.limit],
                                # What to do next, as the call to make. The note used to say
                                # "known authorized locations ... read the offered locations",
                                # under an empty hits list; across three papers (2026-10-10) a
                                # figure's name was searched 124 times and that answer was read
                                # as "nothing found" -- one task asked for "Scheme 1" five times
                                # and never read it.
                                "read_next": [
                                    {"location": entry["location"]}
                                    for entry in entries[: selected.limit]
                                ],
                                "navigation": NAMED_LOCATION_NOTE,
                            }
                            if len(entries) > selected.limit:
                                directory = register_resource(
                                    entries,
                                    origin="HOST_CONTEXT",
                                    source_navigation=f"Read entries for {terms}",
                                )
                                value["named_sources_total"] = len(entries)
                                value["directory"] = {"location": directory}
                            return ReadValue(
                                {"outcome": "READ", "location": SEARCH_LOCATION, "value": value}
                            )
                        if any(
                            pattern.fullmatch(terms.upper())
                            for pattern in (
                                PAGE_ID_PATTERN,
                                BLOCK_ID_PATTERN,
                                VISUAL_ID_PATTERN,
                                GROUP_ID_PATTERN,
                            )
                        ):
                            # Syntax identifies navigation intent, never authorization.
                            return ReadValue(
                                {
                                    "outcome": "READ",
                                    "location": SEARCH_LOCATION,
                                    "value": {
                                        "status": "NO_MATCHES",
                                        "mode": selected.mode,
                                        "terms": selected.terms,
                                        "hits": [],
                                        "named_sources": [],
                                        "navigation": "No such location in this task's "
                                        "authorized catalog. This is not a text search "
                                        "or evidence of absence from the paper. "
                                        "Use LITERAL to search these words in source text.",
                                    },
                                }
                            )
                    # One query over every document this task reads; each hit names the
                    # offered location it can be read at, never a bare block id. A
                    # document it may read none of contributes no hits; a scoped miss
                    # never asserts absence from the whole document.
                    found: list[dict[str, Any]] = []
                    for document in sources.active():
                        if document.package is None:
                            continue
                        revision = document.registration.document_revision_id
                        by_block = searchable.get(revision, {})
                        value = text_search_value(
                            document.package,
                            selected,
                            authorized_block_ids=by_block,
                            location_of=by_block.__getitem__,
                            figure_of=figure_at.get(revision, {}).get,
                            for_merge=True,
                        )
                        if not images_readable:
                            for hit in value["hits"]:
                                if "figure_location" in hit:
                                    hit["figure_unavailable"] = image_unavailable
                        if value["status"] == "UNSUPPORTED_QUERY":
                            return ReadValue(
                                {"outcome": "READ", "location": SEARCH_LOCATION, "value": value}
                            )
                        found.append(value)
                    value = merged_search(found, selected)
                    # The same authorized objects already offered by name can be
                    # discovered here without their caption being a text-search hit.
                    # Match the complete registered label, never its number alone.
                    named_sources = []
                    for revision, visuals in figure_locations.items():
                        for visual_id, location in visuals.items():
                            label = figure_names.get((revision, visual_id))
                            if not label or not re.search(
                                r"(?<!\w)"
                                + r"\s+".join(re.escape(word) for word in label.split())
                                + r"(?!\w)",
                                selected.terms,
                                flags=re.IGNORECASE,
                            ):
                                continue
                            named_sources.append(
                                {
                                    **navigation_entry(location),
                                    "kind": "figure",
                                    "label": label,
                                }
                            )
                    if named_sources:
                        value["status"] = "MATCHES"
                        value.pop("reason", None)
                        value["named_sources"] = named_sources[: selected.limit]
                        value["read_next"] = [
                            {"location": entry["location"]}
                            for entry in named_sources[: selected.limit]
                        ]
                        value["navigation"] = NAMED_FIGURE_NOTE
                        if len(named_sources) > selected.limit:
                            value["named_sources_total"] = len(named_sources)
                    return ReadValue(
                        {"outcome": "READ", "location": SEARCH_LOCATION, "value": value}
                    )

                return RecordTarget(perform_search, with_budget=perform_search)
            source_binding = copy.deepcopy(source_locations.get(root))
            saved_id = (
                root.removeprefix(SELECTION_LOCATION_PREFIX)
                if root.startswith(SELECTION_LOCATION_PREFIX)
                else None
            )
            inherited_binding = (
                context.get("paragraph_selections", {}).get(saved_id) if paragraph_input else None
            )
            saved_binding = (
                selections.get(saved_id) if saved_id is not None else None
            ) or inherited_binding
            if saved_id is not None and saved_binding is not None:
                identity = saved_binding["source_identity"]
                source_binding = {
                    "document_revision_id": identity["document_revision_id"],
                    "source_id": identity["source_id"],
                    "selection_id": saved_id,
                    "medium": "visuals" if "region" in identity else "text",
                    **{
                        key: identity[key]
                        for key in ("start_sentence", "end_sentence", "region", "question")
                        if key in identity
                    },
                }
            derived: str | None = None
            if source_binding is None and root.startswith("source:"):
                bare_root = root.removeprefix("source:")
                if bare_root in {doc.registration.document_revision_id for doc in sources.active()}:
                    raise ReadFailure(
                        "UNAVAILABLE",
                        f"'{bare_root}' is this document's own revision id, not a position "
                        "inside it; search within it with location='search:text', or read one "
                        "of the source: entries the documents directory already offers.",
                    )
                # A block id is unique to its own document in `searchable`; naming it
                # without the document's `source:` root it was offered under is a
                # syntax mistake, not a fact the model got wrong, when only one active
                # document could possibly mean it (two documents sharing a block id,
                # MAIN and SI before P2, are left unresolved rather than guessed at).
                block_hits = [
                    location
                    for blocks in text_locations.values()
                    for block_id, location in blocks.items()
                    if block_id == bare_root
                ]
                if len(block_hits) == 1:
                    source_binding = copy.deepcopy(source_locations.get(block_hits[0]))
                    derived = (
                        f"source:{bare_root} named a block by its package id; "
                        "read at its offered location"
                    )
            if source_binding is not None:
                if source_binding["medium"] == "text":
                    revision, block = (
                        source_binding["document_revision_id"],
                        source_binding["source_id"],
                    )
                    figure = figure_at.get(revision, {}).get(block)
                    if block not in searchable.get(revision, {}):
                        if figure is None:
                            raise ReadFailure(
                                "UNAVAILABLE",
                                "This block has no readable text or unique authorized figure.",
                            )
                        if selection is not None and not isinstance(selection, WholeSelection):
                            raise ReadFailure(
                                "UNSUPPORTED_SELECTION",
                                "This block has zero text sentences and identifies an image. "
                                "The requested text/record selection cannot read it; "
                                "use the offered image location.",
                                available_reads=[
                                    {
                                        "location": figure,
                                        "select": {"kind": "whole"},
                                        **(
                                            {"unavailable": image_unavailable}
                                            if not images_readable
                                            else {}
                                        ),
                                    }
                                ],
                            )
                        source_binding = copy.deepcopy(source_locations[figure])
                        saved_id = None
                        derived = (
                            "This empty text block identifies a figure; "
                            "reading its authorized image."
                        )
                if getattr(selection, "question", None) is not None and (
                    source_binding["medium"] != "visuals" or supplement
                ):
                    raise ReadFailure(
                        "UNSUPPORTED_SELECTION",
                        "A figure question requires an image answered in words.",
                    )
                if source_binding["medium"] == "visuals" and not images_readable:
                    raise ReadFailure("UNAVAILABLE", image_unavailable)
                return source_target(
                    root,
                    source_binding,
                    saved=saved_id is not None,
                    created=created,
                    derived=derived,
                )
            if getattr(selection, "question", None) is not None:
                raise ReadFailure(
                    "UNSUPPORTED_SELECTION",
                    "A figure question requires an image answered in words.",
                )
            binding = copy.deepcopy(resource_locations.get(root))
            if root == "current":
                # This value is pinned now, before any item in this read batch is executed.
                snapshot = writer.findings()
                if recording and sealed_task_input.get("record_source_version") == 1:
                    bindings = current_record_bindings()
                    snapshot = [{**row,
                                 "source_bindings": source_binding_view(bindings[row["item_id"]])}
                                for row in snapshot]
                if isinstance(writer.answer, ReadingAnswer):
                    by_handle = {
                        r.item_id: r for r in (*writer.answer.conflicts, *writer.answer.names)
                    }
                    snapshot = [
                        {
                            **row,
                            "current_ruling": current_ruling_view(
                                by_handle[row["item_id"]], writer.answer
                            ),
                        }
                        if row["item_id"] in by_handle
                        else row
                        for row in snapshot
                    ]
                binding = resource_binding(
                    seal_readable(snapshot),
                    origin="CURRENT_FINDINGS",
                    provided_to=execution_id,
                    owner_task=execution_id,
                )
                # Receipt and staleness checks must share the lifecycle-aware version.
                binding["science_version"] = current_findings_digest()
            if root == "handled" and located_left is not None:
                binding = resource_binding(
                    seal_readable(handled_snapshot()),
                    origin="CURRENT_FINDINGS",
                    provided_to=execution_id,
                    owner_task=execution_id,
                )
            return None if binding is None else record_target(binding)

        if paragraph_input or supplement:
            names = (
                names_from_component(ctx.naming.domain.components["task"])
                if ctx.naming is not None
                else names_now()
            )
            identity = names.identities.get(location, location)
            if names.families.get(identity) == "route":
                locations = public_read_locations()
                if identity not in locations:
                    raise ReadFailure(
                        "UNAVAILABLE",
                        "This named object is absent from the current public result; "
                        "its saved reading may still be raw-only.",
                    )
                location = locations[identity]
        try:
            plan = resolve_read_location(
                location,
                selection,
                task_id=execution_id,
                store=store,
                state_resources=ctx.state_resources,
                persisted_locations=resource_locations,
                resolve_root=resolve_root,
                sentence_spans=sentence_spans,
            )
        except ReadFailure as error:
            if error.code == "UNAVAILABLE":
                source = names_now().source_for_unregistered_selection(location)
                if source is not None:
                    # Validate with the same authorized planner, but do not perform or
                    # accept a read. The next real delivery owns its selection identity.
                    resolve_read_location(
                        source,
                        None,
                        task_id=execution_id,
                        store=store,
                        state_resources=ctx.state_resources,
                        persisted_locations=resource_locations,
                        resolve_root=resolve_root,
                        sentence_spans=sentence_spans,
                    )
                    raise ReadFailure(
                        "UNAVAILABLE",
                        "This selection has not been delivered in this task. "
                        "Read the corresponding authorized source at available_reads.",
                        available_reads=[{"location": source}],
                    ) from error
            raise
        by_label = (
            siblings_by_label(candidates, location) if reason == "ATTRIBUTION_CONFLICT" else []
        )
        siblings: list[tuple[str, str]] = []
        for site in (*unit_siblings, *halves, *by_label):
            if site not in siblings:
                siblings.append(site)
        if siblings:
            # The unit and the halves, then the alternatives, each with the read that
            # mints its selection id, so the task can cite whichever one turns out to
            # be its step rather than going to find it. The alternatives are bounded by
            # what the matcher already found, which is at most three.
            if by_label:
                reasoned_reads[-1]["siblings"] = [block_id for _, block_id in by_label]
            perform = plan.perform

            def with_siblings() -> ReadValue:
                # One read item, one commit. The siblings are read inside the same
                # staging bracket as the read they came with, so accepting the item
                # takes the passage whole -- every binding together with the
                # observation it cites -- and refusing it takes none of it. Read
                # outside that bracket they were bound and then had their observations
                # dropped by the read's own commit, and the receipt went on to name
                # evidence it did not carry.
                with source_reader.staged() as accept_passage:
                    value = perform()
                    value.accept()
                    for revision, block_id in siblings:
                        binding = {"document_revision_id": revision, "source_id": block_id}
                        value.content.extend(
                            located_source_parts(
                                source_reader.read(
                                    SourceRead(
                                        selection_id="read:" + content_hash(binding)[:24],
                                        document_revision_id=revision,
                                        source_id=block_id,
                                    ),
                                    created,
                                    set(),
                                )
                            )
                        )
                accepted = value.accept

                def accept_with_siblings() -> None:
                    accepted()
                    accept_passage()

                value.accept = accept_with_siblings
                return value

            plan.perform = with_siblings
        return plan

    turn_binding = {
        "execution_id": execution_id,
        "input_ref": input_ref.model_dump(mode="json"),
        "scope_sha256": scope_sha256,
    }
    if restored.ref is not None and open_expression:
        edit_history.restore_delivery(delivery)
        edit_history.restore_current(writer)
        if recording and sealed_task_input.get("record_source_version") == 1:
            current_record_bindings()
        if reading and unified_expression:
            restored_receipt = receipt(writer.current()).model_copy(
                update={"audit_refs": tuple(audits), "request_refs": tuple(requests)}
            )
            origin_index.derive(restored_receipt, allow_call_prefix=True)
            proven_bindings = measurement_context.bindings(
                restored_receipt, origins=origin_index, closure=accepted_for_edits,
                restore_domain=restore_investigation_domain,
            )
            for finding in writer.current().findings:
                if finding._measurement_binding != proven_bindings.get(finding.item_id):
                    raise ValueError("restored measurement comparison differs from its accepted actions")

    active_task_names: Names | None = None

    def versions(location: str) -> str | None:
        # Both dynamic records are pinned before a read, with a content version.
        root = location.partition("#")[0]
        if paragraph_input or supplement:
            # The frozen task table retains previously offered identities after
            # rename or withdrawal. A fresh table cannot recognize a removed one.
            names = active_task_names or names_now()
            identity = names.identities.get(location, location)
            if names.families.get(identity) == "route":
                public_location = public_read_locations().get(identity)
                if public_location is None:
                    # Invalidate a previously read object removed from the current
                    # result; the planner will report its absence on a new read.
                    return current_findings_digest()
                root, _, pointer = public_location.partition("#")
                binding = resource_locations[root]
                # The immutable READ receipt and current-object version must use
                # one rule, including when a pointer produced a sealed fragment.
                value = record_json(ArtifactRef.model_validate(binding["artifact_ref"]))
                return record_version(binding, pointer, pointer_value(value, pointer))
        if root == "current":
            return current_findings_digest()
        if root == "handled" and located_left is not None:
            return hashlib.sha256(canonical_json_bytes(handled_snapshot())).hexdigest()
        return None

    # What the kind's progress view reads besides the running answer: this task's own
    # inputs, fixed at dispatch. `candidates` is the live list the delivery above fills
    # in with the selection id each guessed passage was delivered under.
    progress_state = ProgressState(
        route=reading_route
        if paragraph_input
        else container_view(
            machine if not supplement else None,
            scope,
            account,
            paragraph_locator=not supplement,
            recording=recording,
        )["containers"],
        machine=machine,
        delivered={sid: selections[sid] for sid in handed_over() if sid in selections},
        bindings=selections,
        round_left=located_left,
        ledger=context.get("paragraphs", ()) if paragraph_input else (),
        candidates=candidates,
        documents=[str(d["document_revision_id"]) for d in assessed],
        task_scope=task,
    )

    source_preview_cache: tuple[str, dict[str, Any]] | None = None
    public_objects_cache: tuple[str, dict[str, str]] | None = None
    public_snapshot_cache: tuple[str, Mapping[str, Any]] | None = None

    def register_public_objects(value: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
        # A current saved value can cite evidence read during this execution. Resolve
        # it through the same authorized catalog as baseline evidence, before sealing
        # its view; never require a fresh task to make an existing source readable.
        live_account = account
        if account is not None:
            register_evidence_locations(source_observations)
            observed = route_account([], {}, source_observations, sources.main.part_ids)
            live_account = replace(
                account,
                read_at=evidence_locations,
                read_in={**account.read_in, **observed.read_in},
            )
        view = detail_context_view(_scientific_view(value, live_account), context_purpose)
        location = register_resource(view, origin="MODEL_WORK", public_object_snapshot=True)
        return location, {
            key: location + "#" + pointer for key, pointer in public_object_pointers(view).items()
        }

    def public_read_locations() -> dict[str, str]:
        nonlocal public_objects_cache
        key = current_findings_digest()
        if public_objects_cache is not None and public_objects_cache[0] == key:
            return public_objects_cache[1]
        if supplement:
            _, locations = register_public_objects(current_narratives())
            public_objects_cache = key, locations
            return locations
        if recording:
            _, locations = register_public_objects(machine)
            public_objects_cache = key, locations
            return locations
        answer = cast(ReadingAnswer, writer.current())
        if not (
            answer.findings or answer.segments or answer.skeleton_repairs or answer.narratives
        ):
            _, locations = register_public_objects(machine)
            public_objects_cache = key, locations
        else:
            preview = procedure_source_preview()
            if public_objects_cache is None or public_objects_cache[0] != key:
                raise ReadFailure(
                    "UNAVAILABLE",
                    "Current public result unavailable: "
                    + str(preview.get("unavailable", "not compiled")),
                )
        return public_objects_cache[1]

    def procedure_source_preview() -> dict[str, Any]:
        """One actual application per changed snapshot, never a second binding rule.

        Rebuild from the task baseline; previews cannot become subsequent inputs or
        sealed facts. Reads leave the accepted snapshot and its bound evidence intact.
        """
        nonlocal source_preview_cache, public_objects_cache, narrative_cache, public_snapshot_cache
        if supplement:
            return {"narrative_publication": narrative_publication()}
        if not reading:
            return {}
        answer = cast(ReadingAnswer, writer.current())
        owners = {
            owner
            for finding in answer.findings
            if finding.aspect == "elision" and finding.scope != "ANOTHER_EXPERIMENT"
            for owner in finding.about_ids
        }
        if not (
            answer.findings or answer.segments or answer.skeleton_repairs or answer.narratives
        ):
            return {}
        key = current_findings_digest()
        if source_preview_cache is not None and source_preview_cache[0] == key:
            return source_preview_cache[1]
        try:
            base = repaired(answer)
            record = receipt(answer)
            compiled = spec.publication_answer(
                record,
                base.machine,
                task_baseline=base.machine,
                material_bindings=base.material_bindings,
                creation_keys=expression_creation_keys(sources.paper_id, current_origins())
                if unified_expression else None,
                method_targets=method_targets,
                measurement_machines=measurement_machines(answer),
            )
            ledgers = EnrichmentLedgers(
                material_sources=material_sources_for(
                    base.machine, base.material_sources or context.get("material_sources") or {}
                ),
                furniture=furniture_by_package(
                    tuple(d.package for d in sources.documents if d.package is not None)
                ),
                general_methods_first_blocks=general_methods_first_blocks(
                    (d.registration.document_revision_id, d.units) for d in sources.documents
                ),
            )
            applied_locations: dict[tuple[str, int], list[dict[str, str]]] = {}
            candidate, failures, _ = apply_container_enrichment_items(
                base.machine,
                observations,
                record.model_copy(update={"answer": compiled, "raw_answer": None}),
                bound_to=machine,
                ledgers=ledgers,
                applied_locations=applied_locations,
            )
            profile = machine_profile(candidate)
            candidate = compile_operation_stages(
                candidate, None if profile.expression else ledgers, {},
                rekey_relations=profile.expression,
            )
            narrative_applications.clear()
            narrative_failures: list[dict[str, Any]] = []
            current = public_narratives(
                record,
                candidate,
                creation_keys=narrative_creation_keys(sources.paper_id, current_origins()),
                applications=narrative_applications,
                failures=narrative_failures,
            )
            narrative_cache = key, {"paper_narratives": current}
            narratives = {
                n["narrative_id"]: n for n in candidate["domain_payload"]["paper_narratives"]
            }
            narratives.update({n["narrative_id"]: n for n in current})
            candidate["domain_payload"]["paper_narratives"] = list(narratives.values())
            _, object_locations = register_public_objects(candidate)
            public_objects_cache = key, object_locations
            public_snapshot_cache = key, candidate
            publication = {}
            for application in finding_applications(answer, compiled, applied_locations, failures,
                                                    narrative_applications=narrative_applications):
                if application["state"] == "SAVED_ONLY":
                    publication[application["finding_id"]] = {
                        "state": "SAVED_ONLY",
                        "reason": " ".join(application["reasons"]),
                    }
                    continue
                targets = {target["object_id"] for target in application["public_targets"]}
                publication[application["finding_id"]] = {
                    "state": application["state"],
                    "public_targets": application["public_targets"],
                    "note": (
                        "Actual application status, not scientific completeness. Selected primary "
                        "values and retained source expressions remain distinct. Public locations "
                        "show the successful contributions; reasons describe unsuccessful ones."
                    ),
                    "public_locations": [
                        object_locations[target]
                        for target in sorted(targets)
                        if target in object_locations
                    ],
                    **({"reasons": application["reasons"]} if application.get("reasons") else {}),
                }
            preview = {
                "finding_publication": publication,
                "narrative_publication": narrative_publication(),
                "note": (
                    "Actual public compilation after applying the saved snapshot to "
                    "this task's skeleton. SAVED is not the same as SUPPORTING or ADOPTED. "
                    "This preview neither publishes nor judges scientific completeness."
                ),
                "operations": [
                    _scientific_view(
                        {
                            "operation_id": operation["operation_id"],
                            "procedure_sources": operation.get("procedure_sources", []),
                            "stages": [
                                {"stage_id": stage["stage_id"], "description": stage["description"]}
                                for stage in operation.get("stages", [])
                            ],
                        },
                        None,
                    )
                    for operation in experiment_operations(candidate)
                    if operation["operation_id"] in owners
                ],
                "application_failures": [
                    _scientific_view(failure, None) for failure in (*failures, *narrative_failures)
                ],
            }
        except ValueError as error:
            preview = {"unavailable": str(error)}
        source_preview_cache = key, preview
        return preview

    def current_public_machine() -> Mapping[str, Any] | None:
        baseline = current_machine()
        if reading:
            procedure_source_preview()
            if public_snapshot_cache is not None and public_snapshot_cache[0] == current_findings_digest():
                return public_snapshot_cache[1]
        return baseline

    def request_intent_extras(request_ref):
        if not unified_expression or not reading:
            return {}
        current = current_public_machine()
        if current is None:
            raise ValueError("current measurement view is unavailable before request dispatch")
        if current_updates:
            current = {**current, "_material_bindings": repaired(writer.current()).material_bindings}
        ref = seal(current, MEASUREMENT_SNAPSHOT_MEDIA)
        return {"science_snapshot_ref": ref.model_dump(mode="json")}

    def progress(current_states: Mapping[str, Any]) -> dict[str, Any]:
        # No menu of state locations, and no list of every source read so far: the
        # recent actions here already name each read and its location, bounded, and the
        # content window shows or points to what was read. The locations still resolve
        # for a read that names one. What the task has saved, and what is still open,
        # is the kind's own account by the route, every turn (`progress_views`) -- not
        # the saved items in storage shape, which above 8,000 bytes became a directory
        # of twenty heads that named no operation and no aspect (batch 4, 2026-09-24).
        if not recording:
            bind_candidate_selections(cited_selection_ids(writer.current(), selections))
        skeleton = []
        if reading and cast(ReadingAnswer, writer.current()).skeleton_repairs:
            for group in repaired(cast(ReadingAnswer, writer.current())).groups:
                location = register_resource(
                    group["result"],
                    origin="MODEL_WORK",
                    ref=seal(
                        group_result_view(group["result"], purpose=context_purpose),
                        "application/vnd.tsdaf.readable-record+json",
                    ),
                )
                skeleton.append(
                    {"route_group_id": group["route_group_id"], "result_location": location}
                )
        return {
            **({"record_source_bindings": current_record_bindings()}
               if recording and sealed_task_input.get("record_source_version") == 1 else {}),
            **({"skeleton_groups": skeleton} if skeleton else {}),
            "unresolved_writes": delivery.pending(),
            "saved": spec.progress_view(
                writer.current(),
                replace(
                    progress_state,
                    machine=current_public_machine(),
                    material_bindings=repaired(
                        cast(ReadingAnswer, writer.current())
                    ).material_bindings
                    if reading
                    else None,
                    route=container_view(
                        current_public_machine(),
                        material_ids=True,
                        final_mapping=current_updates,
                        material_bindings=repaired(
                            cast(ReadingAnswer, writer.current())
                        ).material_bindings,
                    )["containers"]
                    if reading
                    else progress_state.route,
                    read_scopes=delivered_scopes(include_next_request=True),
                    procedure_source_preview=procedure_source_preview(),
                ),
            ),
            "action_progress": bounded_action_view(current_states[STATE_LOCATION_HISTORY].value),
        }

    def interaction_extras(
        prepared: object, completed: object, response: dict[str, Any]
    ) -> dict[str, Any]:
        # What this interaction's reply may cite: everything bound before it. The set
        # only grows, so the record says what it added and a reader takes the union --
        # it used to write the whole set in every record (a2: 90% of their bytes).
        nonlocal delivered_selections
        added = set(selections) - deferred_selections - delivered_selections
        delivered_selections |= added
        return {
            "delivered_added": sorted(added),
            "context_capacity": getattr(prepared, "context_capacity", None),
        }

    submitted_selections: set[str] = set()

    def on_request_sealed(ref: ArtifactRef) -> None:
        requests.append(ref)
        submitted_selections.clear()
        submitted_selections.update(set(selections) - deferred_selections)

    def on_provider_error(error: ProviderInteractionError, audit: ArtifactRef | None) -> None:
        nonlocal failure, delivered_selections
        if audit is not None:
            audits.append(audit)
        # The failed provider request already carried the prior tool's READ reply.
        # There is no completed interaction to call interaction_extras, so keep this
        # request's actually bound selections in the sealed failure receipt too.
        delivered_selections |= submitted_selections
        failure = error.failure.code.value

    def on_turn_error(error: BaseException) -> None:
        nonlocal failure
        failure = str(error) if isinstance(error, ValueError) else type(error).__name__
        audits.append(
            seal({"diagnostic": failure}, "application/vnd.tsdaf.investigation-diagnostic+json")
        )

    def on_stalled(error: ToolLoopStalled) -> None:
        # Not a failure: the Host has closed the task out with what it saved. The reason
        # is kept beside the delivery so the run stays traceable to the turn that ended it.
        audits.append(
            seal({"diagnostic": str(error)}, "application/vnd.tsdaf.investigation-diagnostic+json")
        )

    def on_close_out(completion: dict[str, Any]) -> None:
        # Which of the two close-out reasons ended this task is a Host fact about the
        # receipt, like `delivered_nothing`, and until now the only copy of it was inside
        # this sealed audit artifact, which nothing above this function opens. Keep it on
        # the receipt as well, so a round can tell a task that chose to stop from one the
        # Host stopped (E107 ④ is the same shape of blindness one layer up).
        nonlocal closed_out
        closed_out = str(completion["closed_out"])
        audits.append(seal(completion, "application/vnd.tsdaf.write-delivery+json"))

    def on_turn_recorded(interaction_ref: ArtifactRef, audit: ArtifactRef | None) -> None:
        audits.extend([*((audit,) if audit is not None else ()), interaction_ref])

    def on_interaction(record: dict[str, Any]) -> None:
        nonlocal thought_tokens, model_seconds
        thought_tokens += int(record.get("total_thought_tokens") or 0)
        model_seconds += float(record.get("elapsed_ms") or 0) / 1000
        if emit_event is not None:
            emit_event("literature_lane_interaction", lane_interaction_event(kind, record))

    def on_recovered(record: dict[str, Any]) -> None:
        nonlocal delivered_selections
        recovered_request = ArtifactRef.model_validate(record["request_ref"])
        if recovered_request not in requests:
            requests.append(recovered_request)
        for key in ("response_ref", "provider_attempt_ref", "interaction_ref"):
            if record.get(key) is not None:
                recovered_ref = ArtifactRef.model_validate(record[key])
                if recovered_ref not in audits:
                    audits.append(recovered_ref)
        delivered_selections |= _delivered_in(record)

    def on_call(ctx: CallContext) -> None:
        nonlocal delivered_selections
        delivered_selections |= _delivered_in(ctx.record)

    def audit(ctx: CallContext, event: dict[str, Any]) -> None:
        # One receipt per call in the executor's shape (`call_receipt`): what the Host
        # answered, on record, so a replay can be held to it (ledger E70).
        audits.append(seal(call_receipt(ctx, event), INVESTIGATION_INTERACTION_MEDIA))
        # A read carries its rows whether some of them came back or none of them did; what
        # this lane owes the model is the same list either way.
        rejected = [row for row in event.get("items", ()) if row["outcome"] == "REJECTED"]
        if rejected:
            read_errors.extend(rejected)
            audits.append(seal(rejected, "application/vnd.tsdaf.investigation-read-errors+json"))
        elif event["kind"] == "FINISH":
            audits.append(seal(event["completion"], "application/vnd.tsdaf.write-delivery+json"))
        elif event["kind"] == "REJECTED":
            audits.append(
                seal(
                    {"diagnostic": event["diagnostic"]},
                    "application/vnd.tsdaf.investigation-diagnostic+json",
                )
            )

    # What the task calls what it is shown -- the route's objects, the documents, the
    # blocks it may read and the passages it holds: shown by name, written back by name,
    # and held by the host as identities (batch 2A). Rebuilt from the facts at each
    # request and each call, so a passage handed over this turn is named from its binding.
    named_documents = [
        (d.registration.document_revision_id, d.registration.source_phase) for d in sources.active()
    ]

    # A figure is read by the author's label for it ("Scheme 2") where its group has one.
    figure_names = {
        (doc.registration.document_revision_id, visual.visual_id): label
        for doc in sources.active()
        if doc.package is not None
        for visual in doc.package.visual_assets
        if (label := visual_label(doc.package, visual.visual_id)) is not None
    }

    def names_now(seed: Names | None = None) -> Names:
        names = task_names(
            current_public_machine(),
            public_objects=current_narratives() if reading or supplement else None,
            documents=named_documents,
            sources=source_locations,
            selections={**context.get("paragraph_selections", {}), **selections}
            if paragraph_input
            else selections,
            figure_names=figure_names,
            seed=seed,
            material_bindings=repaired(cast(ReadingAnswer, writer.current())).material_bindings
            if reading
            else None,
        )
        if reading:
            names = replace(
                names,
                field_families={
                    **names.field_families,
                    **{
                        key: frozenset({"route"})
                        for key in (
                            "fact_id",
                            "fact_ids",
                            "same_as",
                            "owners",
                            "inputs",
                            "outputs",
                            "after",
                        )
                    },
                    **{key: frozenset({"selection"}) for key in ("source_ids", "add_source_ids")},
                },
            )
        return names

    # The route groups' conclusions and results, as a later task is shown them (batch 2A,
    # 2d): fixed at dispatch, like the groups.
    dispatch_groups = cast(Sequence[Mapping[str, Any]], context.get("groups") or ())
    # A source the fusion read itself, by the unit that read it: shown here by what was
    # read, the name that fusion gave it, so one key has one name in both tasks and a
    # later task reads it back. A run whose dispatch carried none names them as handed.
    group_reads: dict[str, Mapping[str, Mapping[str, Any]]] = {
        str(group["result"]["work_unit_id"]): cast(Mapping[str, Mapping[str, Any]], group["reads"])
        for group in dispatch_groups
        if isinstance(group.get("result"), Mapping) and isinstance(group.get("reads"), Mapping)
    }

    def reads_of(result: FusionResult) -> Mapping[str, Mapping[str, Any]]:
        return group_reads.get(result.work_unit_id, {})

    groups_named = group_names(dispatch_groups, reads_of)

    def records_now(seed: RecordNames | None = None) -> RecordNames:
        return record_names(resource_locations, loop.history, seed=seed)

    def record_owner(location: str) -> str | None:
        # The unit whose record a location holds: a group's result, or a part read of it.
        binding = resource_locations.get(location.partition(POINTER)[0]) or {}
        return cast("str | None", binding.get("owner_task"))

    def make_domain_names(previous: dict[str, Any] | None) -> DomainNaming:
        nonlocal active_task_names, groups_named
        task_seed = names_from_component(previous["task"]) if previous is not None else None
        record_seed = (
            RecordNames(
                names_from_component(previous["records_locations"]),
                names_from_component(previous["records_versions"]),
            )
            if previous is not None
            else None
        )
        active_task_names = names_now(task_seed)
        if sparse_reading:
            current_groups = repaired(cast(ReadingAnswer, writer.current())).groups
            current_reads = {g["result"]["work_unit_id"]: g.get("reads", {}) for g in current_groups}
            groups_named = group_names(current_groups,
                lambda result: current_reads.get(result.work_unit_id, {}),
                seed=names_from_component(previous["groups_units"]) if previous is not None else None)
        return investigation_domain(
            active_task_names,
            records_now(record_seed),
            groups_named,
            {
                location: cast("str | None", binding.get("owner_task"))
                for location, binding in resource_locations.items()
            },
            repair_targets=(RepairTargetNames.of(repaired(
                cast(ReadingAnswer, writer.current())).identity) if sparse_reading else None),
        )

    def restore_domain_names(components: dict[str, Any]) -> DomainNaming:
        nonlocal active_task_names
        active_task_names = names_from_component(components["task"])
        return restore_investigation_domain(components)

    def current_receipt() -> InvestigationReceipt:
        return receipt(writer.answer).model_copy(
            update={
                "failure": failure,
                "closed_out": closed_out,
                "read_scopes": delivered_scopes(),
                "unread_sources": unread(),
                "read_errors": tuple(read_errors),
                "resources_sha256": content_hash(
                    [
                        prompt,
                        declarations,
                        NAMING_VERSION,
                        INVESTIGATION_VERSION, READ_DELIVERY_VERSION,
                        client.context_policy,
                    ]
                ),
                "thought_tokens": thought_tokens,
                "model_seconds": round(model_seconds, 1),
                "request_refs": tuple(requests),
                "audit_refs": tuple(audits),
                "source_identities": tuple(d.identity() for d in sources.active()),
            }
        )

    def execute():
        execute_tool_turns(
            loop,
            ToolConsumer(
                label=kind,
                name="investigation",
                task_id=execution_id,
                turn_binding=turn_binding,
                request_media="application/vnd.tsdaf.investigation-request+octets",
                response_media="application/vnd.tsdaf.investigation-response+octets",
                interaction_media=INVESTIGATION_INTERACTION_MEDIA,
                delivery=delivery,
                catalog_data=[document_view(d, media=("text", "visuals")) for d in catalog],
                locations=resource_locations,
                wrap_state_content=False,
                window_chars=SUPPLEMENT_WINDOW_CHARS if supplement else DETAIL_WINDOW_CHARS,
                window_images=SUPPLEMENT_WINDOW_IMAGES if supplement else DETAIL_WINDOW_IMAGES,
                versions=versions,
                evidence_view=lambda row, parts: delivered_source_evidence(
                    row,
                    parts,
                    selections=selections,
                    location_for=lambda revision, source: (
                        text_locations.get(revision, {}).get(source)
                        or figure_locations.get(revision, {}).get(source)
                    ),
                    sentences_for=source_reader.numbered_source,
                ),
                result_ref=writer.sealed_answer_ref,
                system_instruction=prompt,
                tools=declarations,
                read_item_model=read_item_model,
                read_envelope_model=read_envelope_model,
                tool_kinds=INVESTIGATION_TOOL_KINDS,
                progress=progress,
                present=lambda visible: names_now().present(
                    records_now().present(groups_named.present(visible))
                ),
                resolve=lambda raw: names_now().resolve(
                    groups_named.resolve(records_now().resolve(raw), record_owner)
                ),
                make_domain_names=make_domain_names,
                feedback_view=(
                    record_binding_view
                    if recording and sealed_task_input.get("record_source_version") == 1
                    else lambda value: value
                ),
                after_projection=(
                    project_record_binding_feedback
                    if recording and sealed_task_input.get("record_source_version") == 1
                    else lambda visible: None
                ),
                restore_domain_names=restore_domain_names,
                interaction_extras=interaction_extras,
                check_prepared=check_context_capacity,
                request_intent_extras=request_intent_extras,
                on_request_sealed=on_request_sealed,
                on_response_sealed=audits.append,
                on_provider_error=on_provider_error,
                turn_errors=(ValueError, KeyError, TypeError, OSError),
                on_turn_error=on_turn_error,
                on_stalled=on_stalled,
                close_out=writer.close_out,
                on_close_out=on_close_out,
                on_turn_recorded=on_turn_recorded,
                on_interaction=on_interaction,
                on_recovered=on_recovered,
                on_call=on_call,
                targets=writer.targets,
                prepare_write=prepare_write,
                resolve_read=resolve_read,
                apply_write=writer.apply_write,
                finish=writer.finish,
                audit=audit,
                diagnose=diagnostic_text,
                rejected_errors=INVESTIGATION_REJECTED_ERRORS,
                finished_message="investigation was explicitly finished earlier in this response",
                unknown_tool_message="unknown investigation tool",
                reject_message="Rejected; saved findings remain.",
            ),
            store=store,
            bind=bind,
            client=client,
            provider_binding=provider_binding,
            recovery_artifacts=recovery_artifacts,
            checkpoint=save_progress,
            call_budget=call_budget,
        )

    def defer_final_working_set() -> None:
        initial = json.loads(loop.history[0]["content"][0]["text"])
        initial["scientific_records"] = [
            {"title": record["title"], "selection_ids": record["selection_ids"],
             "location": register_resource(record, origin="MODEL_WORK")}
            for record in context["scientific_records"]
        ]
        initial["record_source_access"] = (
            "The complete packet exceeds this model's measured capacity. Each record location "
            "opens one complete scientific account. Read these accounts to map their meaning; "
            "their inherited source selections remain citable without claiming their originals "
            "were read here. Read an offered source only for the clarification you need."
        )
        initial["source_access"] = initial["record_source_access"]
        initial["record_sources"] = [
            {"selection_id": sid, "location": SELECTION_LOCATION_PREFIX + sid,
             "question": value["source_identity"].get("question")}
            for sid, value in context["record_selections"].items()
        ]
        # Remove the unsubmitted source contents and their actual-delivery ledger
        # together. Bindings remain authorized navigation, never forged read events.
        loop.history[0]["content"] = [{"type": "text", "text": canonical_json_bytes(initial).decode()}]
        loop.history[0].pop(DELIVERED_KEY, None)
        deferred_selections.update(selections)
        delivered_selections.clear()

    try:
        try:
            execute()
        except TaskContextTooLarge:
            if requests or not current_updates or restored.ref is not None:
                raise
            defer_final_working_set()
            execute()
    except BudgetPaused:
        save_progress()
        raise
    except ProviderInteractionError as error:
        # The figure request's audit was accepted before dispatch propagated its
        # failure. Preserve accepted science and a typed failed-task receipt.
        on_provider_error(error, None)
        save_progress()
    except TaskContextTooLarge as error:
        # A refused initial packet was held by the host, never delivered to a model.
        if not requests:
            delivered_selections.clear()
        failure = f"CONTEXT_TOO_LARGE:{error}"
        audits.append(
            seal({"diagnostic": failure}, "application/vnd.tsdaf.investigation-diagnostic+json")
        )
        save_progress()
        error.receipt = current_receipt()
        raise
    finally:
        figure_reservations.close()
    return current_receipt()
