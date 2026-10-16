"""Read one figure for a detail task and hand back words, never the image itself.

A detail task never receives an image. The reason measured on the eight-task
decumbenine_B run at d77b930 is not the token cost: base64 images convert at 115
bytes/token against prose at 2.03-2.18, so the nine image turns of that run were 44.6%
of its request BYTES and 1.1-1.8% of its TOKENS. The reason is attention. The task that
asked already carries a paper's worth of prose in its window, and a figure it has to
decode itself competes with the writing it opened the figure for. So the figure is read
here, by a reader whose whole job is this one question, and its answer travels as text.

This is a short loop rather than one call because a reader that cannot make out a label
has to be able to ask for that label larger. The closer looks it asks for are cut from
the original pixels by the Host (`materialize_local_visual_read`); nothing is
super-resolved and nothing outside the original is invented. One turn measured 1,406
input + 3,096 thinking + 157 output tokens in 24.5 s live, and the detail task that
asked is blocked for every one of them, so the loop is bounded to `MAX_FIGURE_TURNS`
and ends sooner on either of the other two conditions `_wants_a_closer_look` names.

V3 answers with `VisualDetailReading` (`schemas/visual_detail_v1.json`): an overview,
observations bound to regions, and the regions themselves, defined by the same region
model V1 uses. What it does not carry is the name of the image. The Host dispatched the
image and records which one it was (`FigureReading.record`); a reader asked to repeat it
was held to V1's rule that only a MAIN image has a name, and a supplement figure has
another. A reading with no observation and a stated limitation is how this reader says it
could not make something out, which is what lets a request for a closer look be expressed
without a second shape. It is not V1: V1 is dispatched from a route group's own sealed
work unit, which a detail turn does not have.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from tsdaf.contracts.execution import ProviderInteractionError
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore
from tsdaf.lanes.literature.lane_models import VisualDetailReading
from tsdaf.lanes.literature.local_visual_read import materialize_local_visual_read
from tsdaf.lanes.literature.model_view import figure_source_view
from tsdaf.lanes.literature.profiles import (
    PreparedLaneCall,
    load_lane_resources,
    prepare_lane_call,
)
from tsdaf.lanes.literature.provider_port import (
    AUTHORIZED_MODEL_PROFILE,
    LaneInteractionClient,
    ProviderBinding,
)
from tsdaf.lanes.literature.selection_resolution import resolve_visual_selection
from tsdaf.lanes.literature.source_scope import VisualSourceSlice
from tsdaf.lanes.provider_attempts import seal_provider_attempts

__all__ = (
    "FIGURE_READING_MEDIA",
    "FIGURE_REQUEST_MEDIA",
    "MAX_FIGURE_TURNS",
    "CloserLook",
    "DetailTurn",
    "FigureReading",
    "build_visual_detail_request",
    "read_figure_in_words",
)

FIGURE_READING_MEDIA = "application/vnd.tsdaf.literature-v3-figure-reading+json"
FIGURE_REQUEST_MEDIA = "application/vnd.tsdaf.literature-v3-interaction-request+json"
_RESPONSE_MEDIA = "application/vnd.tsdaf.literature-v3-interaction-response+octets"
# Three turns: the first look, one closer look, and one more for a figure whose panels
# have to be taken separately. A turn costs 4,659 tokens and 24.5 s measured live, and
# the detail task that asked for the figure waits through every one of them.
MAX_FIGURE_TURNS = 3


@dataclass(frozen=True, slots=True)
class CloserLook:
    """One region the reader asked to see again, and the original pixels it was given."""

    selection_id: str
    payload: bytes
    dimensions: tuple[int, int]
    box: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class DetailTurn:
    """One completed exchange: what the reader answered, and what it was shown next."""

    output_text: str
    closer_looks: tuple[CloserLook, ...]


@dataclass(frozen=True, slots=True)
class FigureReading:
    """One figure's reading, the image it is a reading of, and the sealed calls behind it."""

    reading: VisualDetailReading
    turns: int
    exhausted: bool
    request_refs: tuple[ArtifactRef, ...]
    response_refs: tuple[ArtifactRef, ...]
    visual_id: str
    source_sha256: str
    host_identity: Mapping[str, object]

    def record(self) -> dict[str, object]:
        """The reading as sealed: what the reader said, and which image the Host showed it.

        The image is named here and nowhere in the reading. The reader was shown one image
        and marks regions of it; the Host chose that image, so the Host says which it was,
        down to the bytes (a supplement's image names repeat across supplements).
        """

        return {
            "schema_version": "tsdaf.visual-detail-reading.v2",
            "host_identity": dict(self.host_identity),
            "visual_id": self.visual_id,
            "source_sha256": self.source_sha256,
            "reading": self.reading.model_dump(mode="json"),
        }

    def producer_note(self) -> str:
        """The whole reading, in the order it was written, as one citable note.

        Nothing is dropped. This note is the only record of what the figure said --
        the image itself never reaches the task that cites it -- so a shortened note
        would leave the citation unverifiable by the reader who comes after.
        """

        parts = [self.reading.overview]
        parts.extend(f"{item.scope}: {item.text}" for item in self.reading.observations)
        if self.reading.limitations:
            parts.append("Limitations: " + self.reading.limitations)
        return "\n".join(parts)


def build_visual_detail_request(
    source: VisualSourceSlice,
    payload: bytes,
    *,
    question: str,
    focus_region: tuple[int, ...] | None = None,
    history: tuple[DetailTurn, ...] = (),
) -> PreparedLaneCall:
    """Show the whole original image and the question, then every closer look since.

    The payload is checked against the source's pathless identity here as well as in the
    reader that supplied it, because a request that acquired the wrong bytes would seal a
    reading of an image this figure never was.
    """

    resources = load_lane_resources("V3")
    if type(payload) is not bytes:
        raise TypeError("visual payload must be bytes")
    if (
        len(payload) != source.source_asset.byte_size
        or hashlib.sha256(payload).hexdigest() != source.source_asset.sha256
    ):
        raise ValueError("visual payload does not match its authorized pathless identity")
    if not question.strip():
        raise ValueError("a figure read requires the question it is answering")
    metadata = {
        "schema_version": "tsdaf.visual-detail-source.v2",
        "question": question,
        **({"requested_region_normalized_1000": list(focus_region)} if focus_region else {}),
        # The whole original, at its own resolution. A closer look is cut from these same
        # pixels when this reader asks; it is never handed a downsampled overview first.
        "visual_source": figure_source_view(source.model_dump(mode="json")),
    }
    steps: list[dict[str, object]] = [
        {
            "type": "user_input",
            "content": [
                {"type": "text", "text": canonical_json_bytes(metadata).decode("utf-8")},
                {
                    "type": "image",
                    "mime_type": source.source_asset.media_type,
                    "data": base64.b64encode(payload).decode("ascii"),
                    "resolution": "high",
                },
            ],
        }
    ]
    for turn in history:
        steps.append(
            {"type": "model_output", "content": [{"type": "text", "text": turn.output_text}]}
        )
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": canonical_json_bytes(
                    {
                        "schema_version": "tsdaf.visual-detail-closer-look.v1",
                        "visual_id": source.visual_id,
                        "closer_looks": [
                            {
                                "selection_id": look.selection_id,
                                "source_pixel_box": list(look.box),
                                "display_dimensions": list(look.dimensions),
                            }
                            for look in turn.closer_looks
                        ],
                        "instruction": (
                            "These are the original pixels of the regions you named, in "
                            "the order you named them, enlarged no further than the "
                            "source allows. Answer the question now, or say in "
                            "limitations what is still not legible."
                        ),
                    }
                ).decode("utf-8"),
            }
        ]
        content.extend(
            {
                "type": "image",
                "mime_type": "image/png",
                "data": base64.b64encode(look.payload).decode("ascii"),
                "resolution": "high",
            }
            for look in turn.closer_looks
        )
        steps.append({"type": "user_input", "content": content})
    return prepare_lane_call(
        stage="V3",
        resources=resources,
        call_kwargs={
            "input": steps,
            "system_instruction": resources.prompt_text,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": resources.provider_schema,
            },
        },
    )


def _wants_a_closer_look(reading: VisualDetailReading, served: frozenset[str]) -> tuple[str, ...]:
    """The selections this answer still needs larger, or nothing when it is an answer.

    Three conditions end the loop, so it always ends. An answer carrying an observation
    is an answer, whatever else it says. A reading with no observation carries a stated
    limitation -- `VisualDetailReading` refuses one with neither -- and asks for more only when
    it also names regions; a reading that names only regions an earlier turn already
    served has stopped making progress, which is the rule the tool runtime already
    applies to a search returning a hit set it has returned before (E37).
    """

    if reading.observations or not reading.source_selections:
        return ()
    asked = tuple(item.selection_id for item in reading.source_selections)
    return () if set(asked) <= served else asked


def _cut(
    source: VisualSourceSlice,
    original_bytes: bytes,
    selection_id: str,
    box: tuple[int, int, int, int],
    *,
    store: PrivateArtifactStore,
) -> CloserLook:
    """Cut one named region out of the original pixels, with no resampling of any kind."""

    x0, y0, x1, y1 = box
    resolved = resolve_visual_selection(x0=x0, y0=y0, x1=x1, y1=y1, dimensions=source.dimensions)
    view = materialize_local_visual_read(
        source=source, original_bytes=original_bytes, sampled_box=resolved, store=store
    )
    return CloserLook(
        selection_id=selection_id,
        payload=view.payload,
        dimensions=view.dimensions,
        box=(resolved.x_min_px, resolved.y_min_px, resolved.x_max_px, resolved.y_max_px),
    )


def read_figure_in_words(
    *,
    source: VisualSourceSlice,
    original_bytes: bytes,
    question: str,
    focus_region: tuple[int, ...] | None = None,
    host_identity: Mapping[str, object],
    client: LaneInteractionClient,
    provider_binding: ProviderBinding,
    store: PrivateArtifactStore,
    seal: Callable[[bytes, str], ArtifactRef],
    before_request: Callable[[bytes, int], tuple[ArtifactRef, ArtifactRef]] | None = None,
    after_request: Callable[[ArtifactRef], None] | None = None,
) -> FigureReading:
    """Answer one question about one figure in text, asking for crops while it needs them.

    This establishes no authority of its own. The caller has already decided that its task
    may read this visual and has read the original bytes through the source reader; what
    happens here is that pixels become words. Every request and every response is sealed
    as it is made, so the sub-execution is replayable from the same closure as the task
    that asked for it, whether or not the loop ends with an answer.
    """

    client = provider_binding.require_client(client, label="V3")
    history: tuple[DetailTurn, ...] = ()
    served: frozenset[str] = frozenset()
    request_refs: list[ArtifactRef] = []
    response_refs: list[ArtifactRef] = []
    reading: VisualDetailReading | None = None
    for turn in range(MAX_FIGURE_TURNS):
        prepared = build_visual_detail_request(
            source,
            original_bytes,
            question=question,
            focus_region=focus_region,
            history=history,
        )
        adapter_call = client.prepare(**prepared.call_kwargs)
        if adapter_call.model_profile != AUTHORIZED_MODEL_PROFILE:
            raise ValueError("figure reader request profile is not the authorized one")
        # Host admission precedes sealing a send: a denied request is not an
        # uncertain historical send lacking its intent.
        if before_request is not None:
            request_ref, intent = before_request(adapter_call.request_bytes, turn)
        else:
            request_ref, intent = seal(adapter_call.request_bytes, FIGURE_REQUEST_MEDIA), None
        request_refs.append(request_ref)
        request_record = {
            "host_identity": dict(host_identity),
            "turn": turn,
            "request_ref": request_refs[-1].model_dump(mode="json"),
            **({"intent_ref": intent.model_dump(mode="json")} if intent else {}),
        }
        try:
            interaction = provider_binding.require_interaction(
                client.execute_prepared(adapter_call), label="V3"
            )
        except ProviderInteractionError as error:
            audit = seal_provider_attempts(store, error, request_bytes=adapter_call.request_bytes)
            if audit is not None:
                seal(store.read_bytes(audit), audit.media_type)
                seal(
                    canonical_json_bytes(
                        {
                            **request_record,
                            "provider_attempt_ref": audit.model_dump(mode="json"),
                            "failure_code": error.failure.code.value,
                        }
                    ),
                    "application/vnd.tsdaf.literature-v3-interaction+json",
                )
                if after_request is not None:
                    after_request(audit)
            raise
        audit = seal_provider_attempts(store, interaction, request_bytes=adapter_call.request_bytes)
        if audit is not None:
            seal(store.read_bytes(audit), audit.media_type)
        response_refs.append(seal(interaction.response_bytes, _RESPONSE_MEDIA))
        seal(
            canonical_json_bytes(
                {
                    **request_record,
                    "response_ref": response_refs[-1].model_dump(mode="json"),
                    "transport_attempts": interaction.transport_attempts,
                    "provider_attempt_ref": audit.model_dump(mode="json") if audit else None,
                }
            ),
            "application/vnd.tsdaf.literature-v3-interaction+json",
        )
        if audit is not None and after_request is not None:
            after_request(audit)
        if interaction.output_text is None:
            raise ValueError("figure reader returned no answer")
        reading = VisualDetailReading.model_validate_json(interaction.output_text)
        wanted = _wants_a_closer_look(reading, served)
        if not wanted:
            return FigureReading(
                reading=reading,
                turns=turn + 1,
                exhausted=False,
                request_refs=tuple(request_refs),
                response_refs=tuple(response_refs),
                visual_id=source.visual_id,
                source_sha256=source.source_asset.sha256,
                host_identity=host_identity,
            )
        served |= frozenset(wanted)
        history += (
            DetailTurn(
                output_text=interaction.output_text,
                closer_looks=tuple(
                    _cut(
                        source,
                        original_bytes,
                        item.selection_id,
                        (item.x0, item.y0, item.x1, item.y1),
                        store=store,
                    )
                    for item in reading.source_selections
                    if item.selection_id in wanted
                ),
            ),
        )
    assert reading is not None
    return FigureReading(
        reading=reading,
        turns=MAX_FIGURE_TURNS,
        exhausted=True,
        request_refs=tuple(request_refs),
        response_refs=tuple(response_refs),
        visual_id=source.visual_id,
        source_sha256=source.source_asset.sha256,
        host_identity=host_identity,
    )
