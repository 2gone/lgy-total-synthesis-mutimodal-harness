"""Exact byte sealing for private literature artifacts."""

from __future__ import annotations

import hashlib

from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.host_ports import PrivateArtifactStore


def seal_exact(
    store: PrivateArtifactStore,
    payload: bytes,
    *,
    media_type: str,
) -> ArtifactRef:
    if type(payload) is not bytes:
        raise TypeError("private artifact payload must be bytes")
    ref = store.put_bytes(payload, media_type=media_type)
    if type(ref) is not ArtifactRef:
        raise TypeError("private artifact store must return an ArtifactRef")
    expected_hash = hashlib.sha256(payload).hexdigest()
    if ref.sha256 != expected_hash or ref.byte_size != len(payload) or ref.media_type != media_type:
        raise ValueError("private artifact ref does not describe the intended artifact")
    store.verify(ref)
    persisted = store.read_bytes(ref)
    if type(persisted) is not bytes or persisted != payload:
        raise ValueError("private artifact readback differs from the intended artifact")
    return ref
