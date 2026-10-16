"""Live and recorded adapters for the AtomFlow OCSR 8010 job API.

The service is asynchronous at the envelope even though each job runs online: `POST
/runs` returns a job handle, the caller polls `GET /runs/{job_id}` until the job leaves
`queued`/`running`, and the delivery payload is read from `GET /runs/{job_id}/results`.

Transport is injected so the live adapter is exercisable without a network or a key;
the default transport is `urllib` from the standard library, which keeps this adapter
inside the repository's two-dependency footprint.

The API key is supplied by the composition root, which is the only layer allowed to
read the process environment. It is sent as a header value and never reaches the
recorded request bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from tsdaf.workflow.ports import (
    AUTHORIZED_OCSR_SERVICE_CONTRACT,
    OCSRCropRequest,
    OCSRJobOutcome,
    OCSRServiceProvenance,
    RawOCSRTranscription,
)

__all__ = (
    "OCSRHttpService",
    "OCSRRecordedService",
    "OCSRServiceTransportError",
    "OCSRTransportResponse",
    "SERVICE_CONTRACT",
    "record_key",
)

SERVICE_CONTRACT = AUTHORIZED_OCSR_SERVICE_CONTRACT
"""Pinned alongside the lane constant; `tests/unit/adapters/test_ocsr_service.py`
asserts the two stay one declaration."""

_TERMINAL_STATES = frozenset({"succeeded", "failed"})
_PENDING_STATES = frozenset({"queued", "running"})
_FIXTURE_SCHEMA_VERSION = "tsdaf.recorded-ocsr-jobs.v1"


class OCSRServiceTransportError(RuntimeError):
    """The service could not be reached, refused the call, or failed the job."""


@dataclass(frozen=True, slots=True)
class OCSRTransportResponse:
    status: int
    body: bytes


class OCSRTransport(Protocol):
    def __call__(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> OCSRTransportResponse: ...


def _urllib_transport(timeout_seconds: float) -> OCSRTransport:
    def transport(
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> OCSRTransportResponse:
        request = urllib.request.Request(url, data=body, method=method)
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return OCSRTransportResponse(status=int(response.status), body=response.read())
        except urllib.error.HTTPError as error:
            return OCSRTransportResponse(status=int(error.code), body=error.read() or b"")
        except urllib.error.URLError as error:
            # The reason can embed a host or socket path but never the key, which is
            # only ever sent as a header value.
            raise OCSRServiceTransportError(
                f"OCSR service is unreachable: {error.reason}"
            ) from None

    return transport


def _json_body(response: OCSRTransportResponse, *, what: str) -> dict[str, Any]:
    if response.status != 200:
        # The body may quote the request; it never contains the key.
        raise OCSRServiceTransportError(f"OCSR {what} returned HTTP {response.status}")
    try:
        payload = json.loads(response.body)
    except json.JSONDecodeError:
        raise OCSRServiceTransportError(f"OCSR {what} returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise OCSRServiceTransportError(f"OCSR {what} did not return a JSON object")
    return payload


def _mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _provenance(run_parameters: Mapping[str, Any]) -> OCSRServiceProvenance:
    """Record what the service said it ran.

    Read leniently: these fields are the service's own description of a deployment we
    do not control, so a missing one is recorded as empty rather than failing a job
    whose structures are otherwise sound. This is audit evidence, not stage identity.
    """

    return OCSRServiceProvenance(
        service_contract=SERVICE_CONTRACT,
        ocsr_model=str(run_parameters.get("ocsr_model", "")),
        review_model=str(run_parameters.get("model", "")),
        thinking_level=str(run_parameters.get("thinking_level", "")),
        expand_substituents=bool(run_parameters.get("expand_substituents", False)),
        postpatch_qa=bool(run_parameters.get("postpatch_qa", False)),
    )


def _transcription(item: Mapping[str, Any]) -> RawOCSRTranscription:
    source_name = item.get("source_name")
    if not isinstance(source_name, str) or not source_name:
        raise OCSRServiceTransportError("OCSR result item has no source name")
    internal = _mapping(item.get("internal_json")) or {}
    expanded = _mapping(item.get("expanded_internal_json")) or {}
    external = _mapping(item.get("external_mol")) or {}
    raw_errors = item.get("errors")
    errors = tuple(str(error) for error in raw_errors) if isinstance(raw_errors, list) else ()
    return RawOCSRTranscription(
        source_name=source_name,
        graph_payload=_mapping(internal.get("data")),
        expanded_graph_payload=_mapping(expanded.get("data")),
        external_mol=_text(external.get("content")),
        decision_state=str(item.get("decision_state", "")),
        errors=errors,
    )


def _outcome(
    *,
    job_id: str,
    request_bytes: bytes,
    response_bytes: bytes,
    payload: Mapping[str, Any],
    elapsed_ms: float,
) -> OCSRJobOutcome:
    items = payload.get("items")
    if not isinstance(items, list):
        raise OCSRServiceTransportError("OCSR results payload has no item list")
    run_parameters = _mapping(payload.get("run_parameters")) or {}
    return OCSRJobOutcome(
        job_id=job_id,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        provenance=_provenance(run_parameters),
        transcriptions=tuple(_transcription(item) for item in items if isinstance(item, Mapping)),
        elapsed_ms=elapsed_ms,
    )


def _request_payload(
    crops: Sequence[OCSRCropRequest],
    *,
    expand_substituents: bool,
) -> dict[str, Any]:
    if not crops:
        raise ValueError("OCSR job requires at least one crop")
    return {
        # We keep detection and deterministic cropping, so the service's own detector
        # stays off and each uploaded image is treated as one already-cropped molecule.
        "use_yolo": False,
        "expand_substituents": expand_substituents,
        "include_diagnostics": False,
        "images": [
            {
                "filename": crop.source_name,
                "image_base64": base64.b64encode(crop.crop_bytes).decode("ascii"),
            }
            for crop in crops
        ],
    }


def record_key(request_bytes: bytes) -> str:
    """Fixture key for one job request. Replay is keyed by request, not by call order."""

    return hashlib.sha256(request_bytes).hexdigest()


class OCSRHttpService:
    """One deployed 8010 service. Satisfies `tsdaf.workflow.ports.OCSRService`."""

    __slots__ = ("_api_key", "_base_url", "_poll_interval", "_poll_timeout", "_sleep", "_transport")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: OCSRTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = 2.0,
        poll_timeout_seconds: float = 900.0,
        request_timeout_seconds: float = 60.0,
    ) -> None:
        if not base_url.strip():
            raise ValueError("OCSR service requires a base URL")
        if not api_key.strip():
            # The composition root reads the environment; an empty key here means it
            # was never configured, and a request without one would 401 anyway.
            raise ValueError("OCSR service requires an API key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport or _urllib_transport(request_timeout_seconds)
        self._sleep = sleep
        self._poll_interval = poll_interval_seconds
        self._poll_timeout = poll_timeout_seconds

    @property
    def service_contract(self) -> str:
        return SERVICE_CONTRACT

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "X-API-Key": self._api_key}

    def transcribe(
        self,
        crops: Sequence[OCSRCropRequest],
        *,
        expand_substituents: bool,
    ) -> OCSRJobOutcome:
        request_bytes = json.dumps(
            _request_payload(crops, expand_substituents=expand_substituents),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        started = time.monotonic()

        submitted = _json_body(
            self._transport(
                method="POST",
                url=f"{self._base_url}/runs",
                headers=self._headers(),
                body=request_bytes,
            ),
            what="submit",
        )
        job_id = submitted.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise OCSRServiceTransportError("OCSR submit returned no job ID")

        status = str(submitted.get("status", "queued"))
        deadline = started + self._poll_timeout
        while status in _PENDING_STATES:
            if time.monotonic() >= deadline:
                raise OCSRServiceTransportError(f"OCSR job {job_id} did not finish in time")
            self._sleep(self._poll_interval)
            polled = _json_body(
                self._transport(
                    method="GET",
                    url=f"{self._base_url}/runs/{job_id}",
                    headers=self._headers(),
                    body=None,
                ),
                what="status",
            )
            status = str(polled.get("status", ""))
            if status not in _PENDING_STATES and status not in _TERMINAL_STATES:
                raise OCSRServiceTransportError(f"OCSR job {job_id} reported status {status!r}")
            if status == "failed":
                raise OCSRServiceTransportError(
                    f"OCSR job {job_id} failed: {polled.get('error') or 'no reason given'}"
                )
        if status != "succeeded":
            raise OCSRServiceTransportError(f"OCSR job {job_id} ended as {status!r}")

        response = self._transport(
            method="GET",
            url=f"{self._base_url}/runs/{job_id}/results",
            headers=self._headers(),
            body=None,
        )
        payload = _json_body(response, what="results")
        return _outcome(
            job_id=job_id,
            request_bytes=request_bytes,
            response_bytes=response.body,
            payload=payload,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )


class OCSRRecordedService:
    """Deterministic replay of recorded job results, keyed by request.

    Keyed by request rather than by call order so concurrent callers each receive the
    answer recorded for their own question whatever order they ask in -- the same
    property the recorded Gemini adapter needs, for the same reason.
    """

    __slots__ = ("_records",)

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        if fixture.get("schema_version") != _FIXTURE_SCHEMA_VERSION:
            raise OCSRServiceTransportError("recorded OCSR fixture has an unknown schema version")
        records = fixture.get("records")
        if not isinstance(records, list) or not records:
            raise OCSRServiceTransportError("recorded OCSR fixture has no records")
        by_key: dict[str, dict[str, Any]] = {}
        for record in records:
            if not isinstance(record, dict):
                raise OCSRServiceTransportError("recorded OCSR fixture record is invalid")
            key = record.get("request_sha256")
            if not isinstance(key, str) or key in by_key:
                raise OCSRServiceTransportError("recorded OCSR fixture record key is invalid")
            by_key[key] = record
        self._records = by_key

    @property
    def service_contract(self) -> str:
        return SERVICE_CONTRACT

    def transcribe(
        self,
        crops: Sequence[OCSRCropRequest],
        *,
        expand_substituents: bool,
    ) -> OCSRJobOutcome:
        request_bytes = json.dumps(
            _request_payload(crops, expand_substituents=expand_substituents),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        record = self._records.get(record_key(request_bytes))
        if record is None:
            # Replaying a request nobody recorded would otherwise silently invent an
            # answer, which is the one thing a fixture must never do.
            raise OCSRServiceTransportError("recorded OCSR fixture has no record for this request")
        response_bytes = json.dumps(record["response"], sort_keys=True).encode("utf-8")
        payload = record["response"]
        return _outcome(
            job_id=str(payload.get("job_id", "recorded")),
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            payload=payload,
            elapsed_ms=float(record.get("elapsed_ms", 0.0)),
        )
