"""Provider-neutral value types for a coordinate-OCSR service, and its pinned contract.

This mirrors `detector.py`: the lane owns the shapes an adapter must produce, and
`tsdaf.workflow.ports` turns them into the Protocol a concrete adapter satisfies. The
lane never imports an adapter.

Two things differ from the in-process detector and drive the design here.

The service answers a *batch*: one job carries many crops. `transcribe` is therefore
batch-shaped even though the current runtime calls OCSR once per molecule, so choosing
per-crop or per-image jobs stays a caller decision and does not reshape this port.

The service is remote and self-configuring, so nothing it reports about itself is
available before the call. `run_parameters` names the OCSR model, review model and
thinking level, but it arrives *in the response* -- it cannot key a cache that has to
be consulted before the request is sent, and it cannot pin what a sealed stage was
executed against. `AUTHORIZED_OCSR_SERVICE_CONTRACT` is the request-side substitute:
a constant bumped by hand whenever we re-verify our parsing against the deployed
service. `OCSRServiceProvenance` records what the service said, for audit, and is
deliberately not the identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = (
    "AUTHORIZED_OCSR_SERVICE_CONTRACT",
    "OCSRCropRequest",
    "OCSRJobOutcome",
    "OCSRServiceError",
    "OCSRServiceProvenance",
    "RawOCSRTranscription",
    "bind_transcriptions",
    "require_ocsr_service_contract",
)

AUTHORIZED_OCSR_SERVICE_CONTRACT = "atomflow-ocsr-8010-v1"
"""The service contract this lane's parsing and guards were frozen against.

Bump this only after re-verifying against the deployed service, the way a prompt hash
is bumped: it is what a sealed stage records, so a silent change here would make two
different executions look identical.
"""


class OCSRServiceError(RuntimeError):
    """The service answered, but not with what was asked for."""


def require_ocsr_service_contract(contract: str) -> None:
    """Refuse an adapter frozen against a different service contract."""

    if contract != AUTHORIZED_OCSR_SERVICE_CONTRACT:
        raise OCSRServiceError("OCSR service contract is not authorized for this lane")


@dataclass(frozen=True, slots=True)
class OCSRCropRequest:
    """One already-cropped molecule to transcribe.

    `source_name` is ours, not the service's: we send it and the service echoes it back
    on the matching item, which is the only way to bind a batch answer to the crop that
    produced it. The lane keeps detection and cropping, so requests always go out with
    the service's own detector disabled.
    """

    source_name: str
    crop_bytes: bytes

    def __post_init__(self) -> None:
        if not self.source_name or self.source_name != self.source_name.strip():
            raise ValueError("OCSR crop request needs a canonical source name")
        if not self.crop_bytes:
            raise ValueError("OCSR crop request needs nonempty crop bytes")


@dataclass(frozen=True, slots=True)
class OCSRServiceProvenance:
    """What the service reported about its own configuration. Audit only, not identity."""

    service_contract: str
    ocsr_model: str
    review_model: str
    thinking_level: str
    expand_substituents: bool
    postpatch_qa: bool


@dataclass(frozen=True, slots=True)
class RawOCSRTranscription:
    """One item of a completed job, exactly as the service returned it.

    The graph payloads stay raw mappings: parsing and flattening safety belong to
    `coordinate_graph`, and this type must not quietly normalize evidence on the way in.
    """

    source_name: str
    graph_payload: Mapping[str, Any] | None
    expanded_graph_payload: Mapping[str, Any] | None
    external_mol: str | None
    decision_state: str
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OCSRJobOutcome:
    """One completed job: the exact bytes exchanged, plus its parsed items."""

    job_id: str
    request_bytes: bytes
    response_bytes: bytes
    provenance: OCSRServiceProvenance
    transcriptions: tuple[RawOCSRTranscription, ...]
    elapsed_ms: float


def bind_transcriptions(
    requests: Sequence[OCSRCropRequest],
    outcome: OCSRJobOutcome,
) -> tuple[tuple[OCSRCropRequest, RawOCSRTranscription], ...]:
    """Pair each request with its answer, or refuse the whole job.

    A batch endpoint can answer partially, reorder, or return an item for something
    that was never sent, and none of those raise on the wire. Silently zipping by
    position would then attach one molecule's structure to another molecule's crop --
    a wrong fact with correct-looking provenance, which is worse than no fact. The job
    is therefore only usable when its items are exactly the requested names.
    """

    requested = [request.source_name for request in requests]
    if len(set(requested)) != len(requested):
        raise OCSRServiceError("OCSR crop requests contain duplicate source names")

    answered: dict[str, RawOCSRTranscription] = {}
    for transcription in outcome.transcriptions:
        if transcription.source_name in answered:
            raise OCSRServiceError("OCSR job answered one crop more than once")
        answered[transcription.source_name] = transcription

    missing = [name for name in requested if name not in answered]
    if missing:
        raise OCSRServiceError(f"OCSR job did not answer {len(missing)} requested crop(s)")
    unexpected = sorted(set(answered) - set(requested))
    if unexpected:
        raise OCSRServiceError("OCSR job answered a crop that was never requested")

    return tuple((request, answered[request.source_name]) for request in requests)
