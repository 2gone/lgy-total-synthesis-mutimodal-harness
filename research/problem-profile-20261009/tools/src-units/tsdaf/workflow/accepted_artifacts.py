"""Exact immutable bytes and acceptance callbacks shared by content consumers."""

import hashlib
from collections.abc import Callable

from pydantic import BaseModel

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef


def read_artifact(read_bytes: Callable[[ArtifactRef], bytes], ref: ArtifactRef) -> bytes:
    payload = read_bytes(ref)
    if (
        type(payload) is not bytes
        or len(payload) != ref.byte_size
        or hashlib.sha256(payload).hexdigest() != ref.sha256
    ):
        raise ValueError("artifact bytes differ from their sealed identity")
    return payload


def accept_artifact(
    record: BaseModel,
    *,
    media_type: str,
    put_bytes: Callable[[bytes, str], ArtifactRef],
    read_bytes: Callable[[ArtifactRef], bytes],
    bind: Callable[..., None],
) -> ArtifactRef:
    """Writing an object is not acceptance; the authenticated binding is the commit."""
    payload = canonical_json_bytes(record.model_dump(mode="json"))
    ref = put_bytes(payload, media_type)
    if ref.media_type != media_type or read_artifact(read_bytes, ref) != payload:
        raise ValueError("recovery record differs from its intended bytes or type")
    bind(ref)
    return ref
