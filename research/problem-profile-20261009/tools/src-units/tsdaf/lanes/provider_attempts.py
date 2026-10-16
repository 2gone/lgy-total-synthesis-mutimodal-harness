"""Seal optional provider attempts at the existing Router, reader and Gate boundaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from tsdaf.contracts.execution import PROVIDER_ATTEMPT_AUDIT_VERSION
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef

# Model tool arguments are evidence, never Host artifact declarations. Keep this
# canonical JSON byte record opaque to the execution's reference-graph walker.
PROVIDER_ATTEMPT_MEDIA_TYPE = "application/vnd.tsdaf.provider-attempt-audit+octets"


class _AuditStore(Protocol):
    def put_bytes(self, payload: bytes, *, media_type: str) -> ArtifactRef: ...
    def verify(self, ref: ArtifactRef) -> None: ...
    def read_bytes(self, ref: ArtifactRef) -> bytes: ...


def seal_provider_attempts(
    store: _AuditStore, carrier: object, *, request_bytes: bytes
) -> ArtifactRef | None:
    """Old adapters have no audit; exact prepared requests authorize new audit bytes."""
    raw = getattr(carrier, "attempt_audit_bytes", None)
    if raw is None:
        return None
    if type(raw) is not bytes:
        raise ValueError("provider attempt audit must be exact bytes")
    _validate_attempts(raw, request_bytes=request_bytes)
    ref = store.put_bytes(raw, media_type=PROVIDER_ATTEMPT_MEDIA_TYPE)
    if (
        type(ref) is not ArtifactRef
        or ref.sha256 != hashlib.sha256(raw).hexdigest()
        or ref.byte_size != len(raw)
        or ref.media_type != PROVIDER_ATTEMPT_MEDIA_TYPE
    ):
        raise ValueError("provider attempt store returned an incorrect artifact identity")
    store.verify(ref)
    if store.read_bytes(ref) != raw:
        raise ValueError("provider attempt audit failed exact readback")
    return ref


def read_provider_attempts(
    store: _AuditStore, ref: ArtifactRef, *, request_bytes: bytes
) -> dict[str, Any]:
    """Use the same exact-request validation when deriving spending or retry policy."""
    if ref.media_type != PROVIDER_ATTEMPT_MEDIA_TYPE:
        raise ValueError("not a provider attempt audit")
    store.verify(ref)
    return _validate_attempts(store.read_bytes(ref), request_bytes=request_bytes)


def _validate_attempts(raw: bytes, *, request_bytes: bytes) -> dict[str, Any]:
    audit = json.loads(raw)
    if (
        not isinstance(audit, dict)
        or canonical_json_bytes(audit) != raw
        or set(audit) != {"schema_version", "request_sha256", "request", "attempts"}
        or audit["schema_version"] != PROVIDER_ATTEMPT_AUDIT_VERSION
        or audit["request_sha256"] != hashlib.sha256(request_bytes).hexdigest()
        or canonical_json_bytes(audit["request"]) != request_bytes
        or not isinstance(audit["attempts"], list)
        or not audit["attempts"]
    ):
        raise ValueError("provider attempt audit does not close its exact request")
    for index, attempt in enumerate(audit["attempts"], 1):
        if not isinstance(attempt, dict) or attempt.get("attempt_index") != index:
            raise ValueError("provider attempt audit is not contiguous")
    return audit
