"""Source reads and incremental findings for the two enrichment lanes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
from importlib.resources import files
from typing import Any, Literal

from pydantic import Field

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.reading_units import UnitKind
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_declaration import declaration_schema, provider_declaration_schema
from tsdaf.contracts.tool_read import DEFAULT_SELECTORS, ImageSelection, WholeSelection, read_models
from tsdaf.contracts.tool_write import WRITE_CONTENT_GUIDANCE, write_request
from tsdaf.lanes.document.page_furniture import (
    PackageContinuity,
    package_continuity,
    page_furniture,
    remove_page_furniture,
)
from tsdaf.lanes.literature.citation_policy import SOURCE_CITATION_POLICY, FINAL_CITATION_POLICY
from tsdaf.lanes.literature.container_enrichment import (
    content_hash,
)
from tsdaf.lanes.literature.enrichment_models import (
    SourceRead,
    TaskScope,
    investigation_write_collections,
)
from tsdaf.lanes.literature.evidence import (
    visual_caption,
    HostVerificationContext,
    ObservationContext,
    ObservationOriginator,
    SourceImagePixelBBox,
    TextFragmentSelection,
    VisualAssetSelection,
    materialize_text_observation,
    materialize_visual_observation,
)
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.investigation_kinds import investigation_kind
from tsdaf.lanes.literature.item_identity import identity_sentence
from tsdaf.lanes.literature.local_visual_read import (
    materialize_local_visual_read,
    read_cached_local_visual_read,
)
from tsdaf.lanes.literature.model_view import passage_view
from tsdaf.lanes.literature.provider_port import (
    AUTHORIZED_MODEL_PROFILE,
)
from tsdaf.lanes.literature.quote_binding import TextQuoteIndex
from tsdaf.lanes.literature.selection_resolution import (
    block_sentence_spans,
    resolve_text_selection,
    resolve_visual_selection,
)
from tsdaf.lanes.literature.source_manifest import LiteratureSources
from tsdaf.lanes.literature.source_reads import SourceReader
from tsdaf.lanes.literature.source_scope import VisualSourceSlice

# Paragraph location and whole-paper reading seal raw answers rather than container
# edits. The major version prevents an old editing stage from satisfying either task.
# 5.1 gives recorded route values their concrete authorized reading locations. A
# completed task from the category-only sheet cannot satisfy this new input view.
# 5.2 changes the round's source handoff and verified failure-delivery account, so
# an older task sheet or recovery scope cannot satisfy the new view.
# 5.3 shows the current handoff only in progress; older cached tasks repeat a stale
# static copy in their first sheet and cannot satisfy this model view.
# 5.4 gives paragraph locators a route and progress view of paragraph assignments,
# without scientific field-filling actions or empty whole-route step checkboxes.
# 5.5 reads group results through their scientific view, gives the figure reader only
# its author-facing card, and resolves historical named reads through one identity.
INVESTIGATION_VERSION = "5.93.0"
READING_PROMPT_SECTIONS = ("reading_yield", "reading_measurements", "reading_rulings", "reading_limits")
INVESTIGATION_TOOL_KINDS = {"read": "read", "write": "write", "finish_investigation": "finish"}

# One figure, its original bytes, and the region the task pointed at, answered in the
# words that task will cite. What dispatches V3 is injected rather than built here, so
# this module still knows nothing about how a sub-execution is run or sealed.
FigureReader = Callable[[VisualSourceSlice, bytes, tuple[int, ...] | None, str | None], str]


# Why a task goes back to a source it was already given. The Host now decides what a
# task starts with -- its own steps' paragraphs, and the passages whose title carries one
# of its numbers -- and that decision will sometimes be wrong: a passage cut at a page
# break, a title belonging to the step before, two reactions written as one paragraph.
# The task could always read any authorized block; what it could not do was say why, and
# without that the only way back was paging the directory until something looked right
# (E100: 24 turns of 223 reached the prose). Naming the reason is what lets the Host
# answer it -- and what makes "this reason never changed a write" countable.
# Every one of them changes what the Host sends back. A reason that changed nothing --
# and whose use the Host could derive anyway, from the task reading a candidate it had
# been handed -- would be a field the model has to fill and nobody can act on.
READ_REASONS: tuple[str, ...] = (
    # The two boundary reasons come back the same way, and are counted apart on
    # purpose: one of them says the packaging broke the passage, the other says the
    # chemistry is hard to bound. Those are different bugs with different owners.
    "PASSAGE_INCOMPLETE",
    "BOUNDARY_UNCERTAIN",
    "ATTRIBUTION_CONFLICT",
)


class FigureQuestion(ContractModel):
    question: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The scientific question you want the separate figure reader to answer; "
            "sent unchanged with the original image. State it when checking a specific "
            "issue; omit for a general reading within your task. Only for image locations "
            "answered in words, not text, records, or direct pixel reads."
        ),
    )


class ScopedWhole(WholeSelection):
    scope: Literal["unit"] | None = Field(
        default=None,
        description=(
            "Omit to read the source block and its continuous passage. 'unit' explicitly "
            "reads every authorized member of its existing text unit, including a technical "
            "attachment. Only for original text source locations with a complete authorized "
            "unit; not saved selections, records, images or figure questions."
        ),
    )


class QuestionedWhole(ScopedWhole, FigureQuestion):
    pass


class QuestionedImage(ImageSelection, FigureQuestion):
    pass


@lru_cache(maxsize=None)
def investigation_read_models(
    kind: str,
) -> tuple[type[ContractModel], type[ContractModel], type[ContractModel]]:
    """Generate this kind's declaration and Host validators from its image mode."""
    image_mode = investigation_kind(kind).image_mode
    return read_models(
        tuple(
            (QuestionedWhole if image_mode == "FIGURE_READER" else ScopedWhole)
            if selector is WholeSelection
            else (QuestionedImage if image_mode == "FIGURE_READER" else ImageSelection)
            if selector is ImageSelection
            else selector
            for selector in DEFAULT_SELECTORS
        ),
        reasons=READ_REASONS,
    )


# Existing callers of the detail read helper keep its generated models.
INVESTIGATION_READ_ITEM, INVESTIGATION_READ_ENVELOPE, _INVESTIGATION_READ = (
    investigation_read_models("EXPERIMENTAL_DETAIL")
)


def investigation_tool_models(
    kind: str,
    *,
    task: TaskScope = "WHOLE",
    expression: bool = True,
    open_expression: bool = False,
    unified_expression: bool = False, current_updates: bool = False,
) -> dict[str, type[ContractModel]]:
    """The three tools an investigation task has; the write is generated from its table."""
    spec = investigation_kind(kind)
    collections = investigation_write_collections(
        kind, task=task, expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    )
    from tsdaf.lanes.literature.local_edits import edit_model, supports_edit

    return {
        "read": investigation_read_models(kind)[2],
        "write": write_request(
            name="InvestigationWrite",
            collections=[
                (key, model)
                for key, (model, _) in collections.items()
            ],
            identity=identity_sentence,
            alternatives={
                key: edit_model(model) for key, (model, _) in collections.items()
                if (open_expression or spec.records_science) and supports_edit(model)
            },
        ),
        "finish_investigation": spec.finish_model(),
    }


def _collection_write_entry(
    kind: str, task: TaskScope, collections: tuple[str, ...], *,
    expression: bool = True, open_expression: bool = False,
    unified_expression: bool = False, current_updates: bool = False,
) -> str:
    """A field index of the actual declaration, not another write contract."""
    registered = investigation_write_collections(
        kind, task=task, expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    )
    schema = declaration_schema(investigation_tool_models(
        kind, task=task, expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    )["write"])
    lines = [
        "Write entry (generated from the write declaration; "
        + WRITE_CONTENT_GUIDANCE + "):"
    ]
    for collection in collections:
        prop = schema["properties"][collection]
        choices = prop["items"].get("anyOf", [prop["items"]])
        # Full items come from their registered model, independently of how the
        # envelope combines full/edit alternatives or the model branches by aspect.
        item_schema = declaration_schema(registered[collection][0])
        item = item_schema
        for choice in choices:
            wrapper = schema["$defs"][choice["$ref"].rsplit("/", 1)[-1]]
            if "edit" not in wrapper.get("properties", {}):
                continue
            edit = schema["$defs"][wrapper["properties"]["edit"]["$ref"].rsplit("/", 1)[-1]]
            changes = schema["$defs"][edit["properties"]["changes"]["$ref"].rsplit("/", 1)[-1]]
            lines.append(
                f"write.{collection}[] also accepts edit with " + ", ".join(edit["required"])
                + "; changes fields: " + ", ".join(changes["properties"]) + ". "
                + wrapper.get("description", "")
            )
        required = item.get("required", [])
        if "oneOf" in item:
            for branch in item["oneOf"]:
                aspect = branch["properties"]["aspect"]["const"]
                lines.append(
                    f"write.{collection}[] full item ({aspect}) required fields: "
                    + ", ".join(branch["required"]) + "."
                )
        else:
            lines.append(f"write.{collection}[] required fields: " + ", ".join(required) + ".")
        if kind == "EXPERIMENTAL_READING" and collection not in {
            "narratives",
            "conflicts",
            "names",
        }:
            continue
        if collection == "narratives":
            lines.append(item.get("description", ""))
        for name, field in item["properties"].items():
            optional = " (optional in shape)" if name not in required else ""
            values = " Values: " + ", ".join(field["enum"]) + "." if "enum" in field else ""
            lines.append(f"- {name}{optional}: {field.get('description', '')}{values}")
            for option in field.get("anyOf", ()):
                if "$ref" not in option:
                    continue
                target = item_schema["$defs"][option["$ref"].rsplit("/", 1)[-1]]
                properties = target.get("properties", {})
                keys = target.get("required", ())
                if keys and all("const" in properties[key] for key in keys):
                    literal = {key: properties[key]["const"] for key in keys}
                    lines.append(f"  Or {json.dumps(literal)}: {target.get('description', '')}")
    return "\n".join(lines)


def _figure_read_entry() -> str:
    """A short usable entry from the same selector exposed by the read declaration."""
    selector = QuestionedWhole(kind="whole", question="Your scientific question about this image")
    description = str(declaration_schema(QuestionedWhole)["properties"]["question"]["description"])
    return (
        "Figure read entry (generated from the read declaration): read an image location "
        "with select "
        + canonical_json_bytes(selector.model_dump(mode="json", exclude_defaults=True)).decode()
        + ". question: "
        + description
    )


def _source_selection_write_entry(
    expression: bool, open_expression: bool, unified_expression: bool = False, current_updates: bool = False
) -> str:
    from tsdaf.lanes.literature.middle_layer import READING_KINDS, finding_write_model, reading_collections
    from tsdaf.lanes.literature.source_parts import source_selection_entries

    writer = finding_write_model(
        expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    )
    entries = [f"- {aspect}.{path}: {entry}"
               for aspect in READING_KINDS
               for path, entry in source_selection_entries(writer, aspect).items()]
    if unified_expression:
        entries.extend(f"- skeleton_repairs.{path}: {entry}"
                       for path, entry in source_selection_entries(
                           reading_collections(unified_expression=True)["skeleton_repairs"][0], root_path="").items())
    return (
        "Source selection write entries (generated from this profile's declaration):\n"
        + "\n".join(entries)
    )


def investigation_prompt(
    kind: str, *, task: TaskScope = "WHOLE", expression: bool = True,
    open_expression: bool = False, unified_expression: bool = False, current_updates: bool = False,
) -> str:
    """Load exactly the instructions named by the kind's one policy."""
    names = investigation_kind(kind).prompt_names(task)
    root = files("tsdaf.lanes.literature").joinpath("prompts")
    from inspect import cleandoc

    from tsdaf.lanes.literature.characterization_methods import CHARACTERIZATION_WRITING_MEANING
    from tsdaf.lanes.literature.condition_categories import CONDITION_KEYS
    from tsdaf.lanes.literature.gate_models import FactChange, ReadingFactChange, UnifiedFactChange
    from tsdaf.lanes.literature.skeleton_meanings import fusion_fact_meanings, skeleton_guidance

    def prompt_source(name: str) -> str:
        text = root.joinpath(
            "experimental_final_mapping_v1.md"
            if name == "experimental_reading_v1.md" and current_updates else name
        ).read_text()
        if name == "experimental_reading_v1.md":
            from tsdaf.lanes.literature.middle_layer import CandidateFinding, INDEPENDENT_EXPERIMENT_ENTRY, unified_measurement_entry
            for section in READING_PROMPT_SECTIONS:
                suffix = "unified" if unified_expression else "legacy"
                text = text.replace("{{" + section + "}}", root.joinpath(f"{section}_{suffix}.md").read_text())
            text = text.replace("{{measurement_entry}}", unified_measurement_entry())
            text = text.replace("{{yield_publication}}", CandidateFinding.publication_note("yield"))
            text = text.replace("{{independent_experiment_entry}}", INDEPENDENT_EXPERIMENT_ENTRY)
        return text

    return "\n".join(
        [
            *(
                prompt_source(name)
                .replace("{{supporting_reading_guidance}}", (
                    "Use supporting passages/original figures to resolve a concrete uncertainty "
                    "or support a new scientific judgment. Converting the verified records does "
                    "not require reading every original source again."
                    if current_updates else
                    "Read supporting passages/original figures when their content has not "
                    "already been delivered or a concrete uncertainty needs more evidence."
                ))
                .replace("{{condition_keys}}", ", ".join(CONDITION_KEYS))
                .replace("{{source_citation_policy}}", FINAL_CITATION_POLICY if current_updates else SOURCE_CITATION_POLICY)
                .replace("{{characterization_meaning}}", CHARACTERIZATION_WRITING_MEANING)
                .replace(
                    "{{condition_sequence_guidance}}",
                    (
                        ("Keep existing primary values unless you explicitly select UPDATE_CURRENT. "
                         if current_updates else "Keep existing skeleton values unless your relation and ruling revise them. ")
                        +
                        "For an explicit succession, sequence retains any number of "
                        "ordered members; "
                        "keep paired values together within their member. Members may be intervals "
                        "or unparsed phrases. shared_unit_text may carry a unit stated once in the "
                        "complete quote. Source positions identify evidence, "
                        "not experimental order."
                        if expression
                        else "The host preserves an existing skeleton temperature "
                        "curve; when absent, "
                        "it orders supported values by their source positions."
                    ),
                )
                .replace(
                    "{{paragraph_write_entry}}",
                    _collection_write_entry(
                        kind, task,
                        tuple(investigation_write_collections(
                            kind, task=task, expression=expression,
                            open_expression=open_expression,
                            unified_expression=unified_expression, current_updates=current_updates,
                        )),
                        expression=expression, open_expression=open_expression,
                        unified_expression=unified_expression, current_updates=current_updates,
                    )
                    if kind == "EXPERIMENTAL_DETAIL"
                    else "",
                )
                .replace(
                    "{{search_status}}",
                    investigation_kind(kind).answer_model(unified_expression=unified_expression, current_updates=current_updates).model_fields["status"].description
                    or "",
                )
                for name in names
            ),
            *([_figure_read_entry()] if investigation_kind(kind).image_mode != "DIRECT" else []),
            *(
                [
                    _collection_write_entry(
                        kind,
                        task,
                        tuple(
                            investigation_write_collections(
                                kind,
                                task=task,
                                expression=expression,
                                open_expression=open_expression,
                                unified_expression=unified_expression, current_updates=current_updates,
                            )
                        ),
                        expression=expression,
                        open_expression=open_expression,
                        unified_expression=unified_expression, current_updates=current_updates,
                    )
                ]
                if kind in {"EXPERIMENTAL_READING", "EXPERIMENTAL_RECORDING"}
                else []
            ),
            *(
                [_source_selection_write_entry(
                    expression, open_expression, unified_expression, current_updates
                )]
                if kind == "EXPERIMENTAL_READING" and open_expression
                else []
            ),
            skeleton_guidance(kind),
            *(
                [
                    "Skeleton repair entries use repair.changes. "
                    + cleandoc(FactChange.__doc__ or "")
                    + ("\n" + cleandoc(UnifiedFactChange.__doc__ or "") if unified_expression else ""),
                    fusion_fact_meanings(unified_expression=unified_expression),
                    *(["repair.changes[].preparation_use: " +
                       (ReadingFactChange.model_fields["preparation_use"].description or "")]
                      if unified_expression else []),
                ]
                if kind == "EXPERIMENTAL_READING"
                else []
            ),
        ]
    )


def investigation_resources(
    kind: str,
    *,
    task: TaskScope = "WHOLE",
    expression: bool = True,
    open_expression: bool = False,
    unified_expression: bool = False, current_updates: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    if kind == 'EXPERIMENTAL_MAPPING':
        from tsdaf.lanes.literature.final_mapping import mapping_resources
        return mapping_resources(task=task)
    models = investigation_tool_models(
        kind, task=task, expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    )
    tools = [
        {
            "type": "function",
            "name": name,
            "description": model.__doc__,
            "parameters": provider_declaration_schema(model),
        }
        for name, model in models.items()
    ]
    return investigation_prompt(
        kind, task=task, expression=expression, open_expression=open_expression,
        unified_expression=unified_expression, current_updates=current_updates,
    ), tools


def source_observation_id(execution_id: str, identity: Mapping[str, Any]) -> str:
    """The same source extent and question in one reader execution have one identity."""
    return "OBS-ENR-" + content_hash([execution_id, identity])


class InvestigationSourceReader:
    """One authorized source catalog with immutable selection and display bindings."""

    def __init__(
        self,
        *,
        kind: str,
        sources: LiteratureSources,
        execution_id: str,
        input_ref: ArtifactRef,
        prompt_ref: ArtifactRef,
        schema_ref: ArtifactRef,
        seal: Callable[[object, str], ArtifactRef],
        store: PrivateArtifactStore,
        allowed_main_visual_ids: tuple[str, ...],
        read_figure: FigureReader | None = None,
    ):
        self.kind_spec = investigation_kind(kind)
        self.kind, self.sources, self.execution_id = kind, sources, execution_id
        self.input_ref, self.prompt_ref, self.schema_ref = input_ref, prompt_ref, schema_ref
        self.seal, self.store = seal, store
        self.allowed_main_visual_ids = allowed_main_visual_ids
        self.read_figure = read_figure
        self.selections: dict[str, dict[str, Any]] = {}
        self.source_observations: list[dict[str, Any]] = []
        self.readers: dict[str, SourceReader] = {}
        # The page's own printing, per package, found once: what is taken out of every
        # sentence before it is shown or stored (E140).
        self.furniture: dict[str, str | None] = {}
        # What the layout did to the paragraphs, per package, found once (E132): each
        # cut paragraph's other half, and the paragraphs whose other half is not in
        # the package at all.
        self._continuity: dict[str, PackageContinuity] = {}
        self._quote_indexes: dict[str, TextQuoteIndex] = {}
        self._quote_verified: set[str] = set()

    @contextmanager
    def staged(self) -> Iterator[Callable[[], None]]:
        """Read into a holding area, and hand back the one call that commits it.

        A read binds a selection and mints the observation that selection cites; the
        binding and the observation are one act and are committed as one, here, so
        nothing above can take the binding and drop the observation the receipt then
        names. Every read made inside the bracket belongs to that one commit -- the
        block asked for, the neighbours it is widened by, the rest of its reading unit
        -- which is also what lets the second read of one passage find the first one's
        binding instead of minting a second observation under the same id for it.

        The reads happen against the live state and are taken back out of it when the
        bracket closes, so a caller that never commits leaves nothing behind, and a
        read that raises leaves nothing behind either.
        """
        before_selections = dict(self.selections)
        before_observations = list(self.source_observations)
        staged_selections: dict[str, dict[str, Any]] = {}
        staged_observations: list[dict[str, Any]] = []

        def commit() -> None:
            self.selections.update({key:value for key,value in staged_selections.items()
                                    if key not in before_selections or before_selections[key] != value})
            for observation in staged_observations:
                if observation not in before_observations and observation not in self.source_observations:
                    self.source_observations.append(observation)

        try:
            yield commit
        finally:
            staged_selections.update(self.selections)
            staged_observations[:] = self.source_observations
            self.selections.clear()
            self.selections.update(before_selections)
            self.source_observations[:] = before_observations

    def _furniture(self, package: CanonicalPaperPackage) -> str | None:
        key = package.canonical_content_sha256
        if key not in self.furniture:
            self.furniture[key] = page_furniture([(b.block_id, b.raw_text) for b in package.blocks])
        return self.furniture[key]

    def quote_index(self, package: CanonicalPaperPackage) -> TextQuoteIndex:
        key = package.canonical_content_sha256
        if key not in self._quote_indexes:
            self._quote_indexes[key] = TextQuoteIndex(package)
        return self._quote_indexes[key]

    def rebind_delivered_text(
        self, binding: dict[str, Any], observation: Mapping[str, Any] | None
    ) -> None:
        """Re-derive geometry for an already delivered, sealed source; grant no read."""
        for private in ("quote_binding", "match_text", "match_furniture"):
            binding.pop(private, None)
        if not isinstance(binding.get("exact_text"), str) or observation is None:
            return
        identity = binding["source_identity"]
        document = self.sources.document(identity["document_revision_id"])
        package, root = document.package, document.package_root
        if package is None or root is None or identity["source_id"] not in document.readable_ids:
            return
        if identity["package_sha256"] != package.canonical_content_sha256:
            raise ValueError("delivered quote is bound to a different package")
        if (
            observation.get("document_revision_id") != identity["document_revision_id"]
            or observation.get("source_phase") != document.registration.source_phase
            or observation.get("verification", {}).get("verified_package_sha256")
            != identity["package_sha256"]
            or observation.get("anchor", {}).get("document_revision_id")
            != identity["document_revision_id"]
            or observation.get("observation_id") != binding["evidence_ref"]["observation_id"]
        ):
            raise ValueError("delivered quote observation identity differs from its source")
        reference = binding["evidence_ref"]
        if content_hash(observation) != reference["source_observation_sha256"]:
            raise ValueError("delivered quote observation differs from its sealed reference")
        index = self.quote_index(package)
        fresh = index.bind(
            identity["source_id"], identity["start_sentence"], identity.get("end_sentence")
        )
        raw = fresh["match_text"]
        fragments = observation.get("anchor", {}).get("fragments", ())
        spans = block_sentence_spans(index.blocks[identity["source_id"]].raw_text)
        start = spans[identity["start_sentence"]].char_start
        if len(fragments) != 1 or (
            fragments[0]["block_id"] != identity["source_id"]
            or fragments[0]["char_start"] != start
            or fragments[0]["char_end"] != start + len(raw)
            or fragments[0]["fragment_sha256"] != hashlib.sha256(raw.encode()).hexdigest()
        ):
            raise ValueError("delivered quote extent differs from its source observation")
        if binding["exact_text"] not in (raw, remove_page_furniture(raw, index.furniture).text):
            raise ValueError("delivered quote text differs from its bound source")
        reader = self.readers.get(identity["document_revision_id"])
        if reader is None:
            reader = self.readers[identity["document_revision_id"]] = SourceReader(package, root)
        if identity["document_revision_id"] not in self._quote_verified:
            reader.verify_text()
            self._quote_verified.add(identity["document_revision_id"])
        if (
            identity.get("source_phase", binding.get("source_phase"))
            != document.registration.source_phase
        ):
            raise ValueError("delivered quote phase differs from its registered source")
        binding.update(fresh)

    def verify_sealed_selection(self, binding: dict[str, Any], observation: Mapping[str, Any]) -> None:
        """Validate a prior reader's source binding; neither reads nor grants a delivery."""
        identity = binding["source_identity"]
        doc = self.sources.document(identity["document_revision_id"])
        package = doc.package
        if package is None:
            raise ValueError("sealed selection source package is unavailable")
        if (identity["package_sha256"] != package.canonical_content_sha256
                or identity.get("source_phase") != doc.registration.source_phase
                or binding.get("source_phase") != doc.registration.source_phase
                or observation.get("document_revision_id") != identity["document_revision_id"]
                or observation.get("source_phase") != doc.registration.source_phase
                or observation.get("anchor", {}).get("document_revision_id") != identity["document_revision_id"]
                or observation.get("verification", {}).get("verified_package_sha256") != identity["package_sha256"]):
            raise ValueError("sealed selection and observation name different source revisions")
        evidence = binding["evidence_ref"]
        if (observation["observation_id"] != evidence["observation_id"]
                or content_hash(observation) != evidence["source_observation_sha256"]):
            raise ValueError("sealed selection observation differs from its evidence")
        # The source reader's identity includes a figure's question. An answer for
        # the same asset under another question is not interchangeable evidence.
        expected_id = source_observation_id(observation["originator"]["execution_id"], identity)
        if observation["observation_id"] != expected_id:
            raise ValueError("sealed selection differs from its original reader identity or question")
        if binding.get("exact_text") is not None:
            if identity["source_id"] not in doc.readable_ids or observation.get("kind") != "TEXT":
                raise ValueError("sealed text source is not in the authorized readable scope")
            self.rebind_delivered_text(binding, observation)
            return
        visual = next((v for v in package.visual_assets if v.visual_id == identity["source_id"]), None)
        if visual is None or observation.get("kind") != "VISUAL":
            raise ValueError("sealed visual source is absent from its registered package")
        if (not binding.get("figure_reading")
                or binding["figure_reading"] != observation.get("producer_note")):
            raise ValueError("sealed figure answer differs from its original observation")
        anchor = observation["anchor"]
        region = identity.get("region") or (0, 0, 1000, 1000)
        box = resolve_visual_selection(x0=region[0], y0=region[1], x1=region[2], y1=region[3],
                                       dimensions=(visual.width, visual.height))
        expected_box = dict(x_min_px=box.x_min_px, y_min_px=box.y_min_px,
                            x_max_px=box.x_max_px, y_max_px=box.y_max_px)
        if (anchor["source_asset"]["sha256"] != visual.asset.sha256
                or anchor.get("bbox_px") != expected_box
                or binding.get("display", {}).get("bbox_px") != expected_box):
            raise ValueError("sealed figure region differs from its source observation")

    def numbered_source(self, identity: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Visible source sentences in the immutable block's numbering.

        Delivery and its later coverage view share this rendering. Removed page
        furniture does not fill gaps in the original sentence numbers.
        """
        doc = self.sources.document(identity["document_revision_id"])
        package = doc.package
        assert package is not None
        block = next(b for b in package.blocks if b.block_id == identity["source_id"])
        furniture = self._furniture(package)
        return [
            {"sentence": i, "text": shown}
            for i, span in enumerate(block_sentence_spans(block.raw_text))
            if identity["start_sentence"] <= i < identity["end_sentence"]
            and (shown := remove_page_furniture(span.text, furniture).text)
        ]

    def continuity(self, revision: str) -> PackageContinuity:
        """The paragraph pairs the layout cut, and the paragraphs it cut with a loss,
        for one registered document. The table is `package_continuity`'s, computed
        once per package here; what this reader adds is the reading's own knowledge
        of which cut blocks are data, not prose."""
        doc = self.sources.document(revision)
        package = doc.package
        assert package is not None
        key = package.canonical_content_sha256
        if key not in self._continuity:
            found = package_continuity(package)
            # A data block the reading marked -- a crystal structure's table line ending
            # without a full stop -- is not a paragraph cut with a loss.
            data = {
                block_id
                for unit in (doc.units.units if doc.units is not None else ())
                if unit.kind
                in {
                    UnitKind.CHARACTERIZATION,
                    UnitKind.CRYSTALLOGRAPHY,
                    UnitKind.BIBLIOGRAPHY,
                    UnitKind.OTHER,
                }
                for block_id in unit.block_ids
            }
            self._continuity[key] = PackageContinuity(
                partners=found.partners, truncated=found.truncated - data
            )
        return self._continuity[key]

    def read(
        self,
        selection: SourceRead,
        created: tuple[ArtifactRef, ...],
        verified_text: set[str],
        *,
        question: str | None = None,
    ) -> list[dict[str, Any]]:
        sources = self.sources
        selections, source_observations, readers = (
            self.selections,
            self.source_observations,
            self.readers,
        )
        allowed_main_visual_ids = self.allowed_main_visual_ids
        kind, execution_id, input_ref = self.kind, self.execution_id, self.input_ref
        prompt_ref, schema_ref, seal = self.prompt_ref, self.schema_ref, self.seal
        doc = sources.document(selection.document_revision_id)
        package, package_root = doc.package, doc.package_root
        assert package is not None and package_root is not None
        reader = readers.get(selection.document_revision_id)
        if reader is None:
            reader = readers[selection.document_revision_id] = SourceReader(package, package_root)
        blocks = {b.block_id: b for b in package.blocks}
        visuals = {v.visual_id: v for v in package.visual_assets}
        if selection.source_id in blocks:
            if question is not None:
                raise ValueError("A figure question requires an image answered by a figure reader.")
            if selection.region is not None:
                raise ValueError("text source does not accept a visual region")
            if selection.source_id not in doc.readable_ids:
                raise ValueError("text source lies outside the authorized cleaned document")
            if selection.document_revision_id not in verified_text:
                reader.verify_text()
                verified_text.add(selection.document_revision_id)
            block = blocks[selection.source_id]
            count = len(block_sentence_spans(block.raw_text))
            end = count if selection.end_sentence is None else selection.end_sentence
            resolved = resolve_text_selection(
                raw_text=block.raw_text, start_sentence=selection.start_sentence, end_sentence=end
            )
            identity: dict[str, Any] = {
                "document_revision_id": selection.document_revision_id,
                "source_phase": doc.registration.source_phase,
                "package_sha256": package.canonical_content_sha256,
                "source_id": selection.source_id,
                "start_sentence": selection.start_sentence,
                "end_sentence": end,
            }
            # Sentences are counted and located on the block as it arrived -- the
            # identity above and the observation below are in those offsets. What the
            # task is shown, and the exact_text it will be held to, is the same span
            # without the page's own printing (E133, E140): on hyperfirin's supplement
            # eight of seventy-three footers sit inside a block's last sentence, and
            # a model that quotes that sentence must not be quoting the footer.
            furniture = self._furniture(package)
            text: str | None = remove_page_furniture(resolved.text, furniture).text
        elif selection.source_id in visuals:
            if (
                selection.start_sentence != 0
                or selection.end_sentence is not None
                or selection.adjacent_blocks
            ):
                raise ValueError("image reads do not accept sentence ranges or text continuation")
            if (
                doc.registration.source_phase == "MAIN"
                and selection.source_id not in allowed_main_visual_ids
            ):
                raise ValueError("original MAIN image is not authorized for this investigation")
            visual = visuals[selection.source_id]
            identity = {
                "document_revision_id": selection.document_revision_id,
                "source_phase": doc.registration.source_phase,
                "package_sha256": package.canonical_content_sha256,
                "source_id": selection.source_id,
            }
            source = reader.visual_source(selection.source_id)
            x0, y0, x1, y1 = selection.region or (0, 0, 1000, 1000)
            box = resolve_visual_selection(
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                dimensions=source.dimensions,
            )
            identity["region"] = list(selection.region) if selection.region is not None else None
            if question is not None:
                if self.kind_spec.image_mode == "DIRECT":
                    raise ValueError(
                        "This image is delivered as pixels, not a figure-reader answer."
                    )
                identity["question"] = question
            text = None
        else:
            raise ValueError("source ID is absent from the registered revision")
        if (
            selection.selection_id in selections
            and selections[selection.selection_id]["source_identity"] != identity
        ):
            raise ValueError("a selection ID cannot be rebound to another source")
        existing = next((s for s in selections.values() if s["source_identity"] == identity), None)
        content: list[dict[str, Any]] = []
        figure_reading: str | None = None
        if text is None:
            data = reader.read_image(selection.source_id)
            if existing is not None:
                view = read_cached_local_visual_read(
                    source=source,
                    original_bytes=data,
                    mapping_ref=ArtifactRef.model_validate(existing["display"]["mapping_ref"]),
                    store=self.store,
                )
            else:
                view = materialize_local_visual_read(
                    source=source,
                    original_bytes=data,
                    sampled_box=box,
                    store=self.store,
                    overview=selection.region is None,
                )
            # Bind both derivatives into the same execution closure as the source observation.
            self.seal(self.store.read_bytes(view.image_ref), view.image_ref.media_type)
            self.seal(self.store.read_bytes(view.mapping_ref), view.mapping_ref.media_type)
            display = {
                "mapping_ref": view.mapping_ref.model_dump(mode="json"),
                "image_ref": view.image_ref.model_dump(mode="json"),
                "dimensions": list(view.dimensions),
                "source_frame": view.source_frame(
                    "source:"
                    + content_hash([selection.document_revision_id, selection.source_id])[:24],
                    source.dimensions,
                ),
                "bbox_px": {
                    "x_min_px": box.x_min_px,
                    "y_min_px": box.y_min_px,
                    "x_max_px": box.x_max_px,
                    "y_max_px": box.y_max_px,
                },
            }
            # The task asked about a figure and is answered in words; it is never handed
            # the image. On the eight-task decumbenine_B run at d77b930 a task that was
            # handed one still had to decode it against a window already holding a paper's
            # worth of prose. A separate reader (V3) is given the whole original and this
            # task's question, may ask the Host to cut a closer look from the original
            # pixels, and returns a reading. That reading is what this task cites, and it
            # is the same text as the observation's producer_note below: one figure, one
            # account of it, written in one place.
            #
            # The supplement lane is not this lane: its subject IS the figures, it reads
            # them by region, and the ruling that moved the detail lane behind a reader
            # left it where it was (E108). It still receives the picture below.
            if self.kind_spec.image_mode == "DIRECT":
                content.append(
                    {
                        "type": "image",
                        "mime_type": "image/png",
                        "artifact_ref": view.image_ref.model_dump(mode="json"),
                        "resolution": "high",
                    }
                )
            elif existing is not None and existing.get("figure_reading") is not None:
                figure_reading = str(existing["figure_reading"])
            elif self.read_figure is None:
                raise ValueError(
                    "no figure reader is configured for this task, and an image is read "
                    "for a task rather than handed to it; read the prose sources instead"
                )
            else:
                figure_reading = self.read_figure(source, data, selection.region, question)
        if existing is None:
            digest = content_hash(
                {key: value for key, value in identity.items() if key != "question"}
            )
            originator = ObservationOriginator(
                originator_kind="MODEL",
                stage_name="literature-" + kind.lower(),
                stage_version=INVESTIGATION_VERSION,
                execution_id=execution_id,
                model_profile=AUTHORIZED_MODEL_PROFILE,
                prompt_sha256=prompt_ref.sha256,
                provider_schema_sha256=schema_ref.sha256,
                requested_by_reviewer_id=None,
                base_artifact_sha256=package.canonical_content_sha256,
            )
            ctx = ObservationContext(
                observation_id=source_observation_id(execution_id, identity),
                anchor_id="ANCH-ENR-" + digest,
                document_revision_id=selection.document_revision_id,
                source_phase=doc.registration.source_phase,
                payload={"payload_kind": "NAVIGATION_ONLY", "candidate_field_refs": []},
                producer_note=(
                    figure_reading if figure_reading is not None else "Verified source for " + kind
                ),
                created_from_artifact_refs=(input_ref, prompt_ref, schema_ref, *created),
                paper_id=sources.paper_id,
            )
            verifier = HostVerificationContext(
                "literature-source-investigation", INVESTIGATION_VERSION, execution_id
            )
            if text is not None:
                observation = materialize_text_observation(
                    package,
                    (
                        TextFragmentSelection(
                            0, block.block_id, resolved.char_start, resolved.char_end
                        ),
                    ),
                    "OTHER_TEXT",
                    originator,
                    context=ctx,
                    verifier=verifier,
                )
            else:
                if visual.width is None or visual.height is None:
                    raise ValueError("original image dimensions are unavailable")
                observation = materialize_visual_observation(
                    package,
                    VisualAssetSelection(visual.visual_id),
                    SourceImagePixelBBox(box.x_min_px, box.y_min_px, box.x_max_px, box.y_max_px),
                    "OTHER_VISUAL",
                    originator,
                    context=ctx,
                    verifier=verifier,
                )
            observation_ref = seal(observation, "application/vnd.tsdaf.source-observation.v1+json")
            source_observations.append(observation)
            bound = {
                "source_identity": identity,
                "source_phase": doc.registration.source_phase,
                "evidence_ref": {
                    "observation_id": observation["observation_id"],
                    "source_observation_sha256": observation_ref.sha256,
                },
                "exact_text": text,
                **({"display": display} if text is None else {}),
                **({"figure_reading": figure_reading} if figure_reading is not None else {}),
            }
        else:
            bound = existing
        if text is not None:
            bound.update(
                self.quote_index(package).bind(selection.source_id, selection.start_sentence, end)
            )
        selections[selection.selection_id] = bound
        content.insert(
            0,
            {
                "type": "text",
                # What the model is shown of the passage it read, through the one
                # projection: the binding above stays the host's (`model_view`).
                "text": canonical_json_bytes(
                    passage_view(
                        {
                            "selection_id": selection.selection_id,
                            "source_phase": doc.registration.source_phase,
                            "numbered_sentences": self.numbered_source(identity)
                            if text is not None
                            else [],
                            "caption": visual_caption(package, visual.visual_id)
                            if text is None
                            else None,
                            **({"display": display} if text is None else {}),
                            **(
                                {"figure_reading": figure_reading}
                                if figure_reading is not None
                                else {}
                            ),
                        }
                    )
                ).decode(),
            },
        )
        if text is not None and end == count:
            # A layout pairing offers adjacent context, not proof of one sentence.
            # Navigation only; quote_binding independently records text geometry.
            partners = self.continuity(selection.document_revision_id).partners
            order = [b.block_id for b in package.blocks]
            later = [
                partner
                for partner in partners.get(selection.source_id, ())
                if order.index(partner) > order.index(selection.source_id)
            ]
            if later:
                bound["continues_in"] = later[0]
        if text is not None and selection.adjacent_blocks:
            # Neighbours are the document's, then kept to what may be read: a readable
            # set with gaps in it -- a step's own paragraphs pages apart (E142) -- must
            # not make the next readable block "the neighbouring passage".
            included = set(package.clean_view.included_block_ids)
            ordered = [b for b in package.blocks if b.block_id in included]
            position = next(i for i, b in enumerate(ordered) if b.block_id == selection.source_id)
            readable = set(doc.readable_ids)
            for neighbor in ordered[
                max(0, position - selection.adjacent_blocks) : position
                + selection.adjacent_blocks
                + 1
            ]:
                if (
                    neighbor.block_id == selection.source_id
                    or neighbor.block_id not in readable
                    or not block_sentence_spans(neighbor.raw_text)
                ):
                    continue
                neighbor_id = selection.selection_id + "-context-" + neighbor.block_id
                content.extend(
                    self.read(
                        SourceRead(
                            selection_id=neighbor_id,
                            document_revision_id=selection.document_revision_id,
                            source_id=neighbor.block_id,
                        ),
                        created,
                        verified_text,
                    )
                )
        return content
