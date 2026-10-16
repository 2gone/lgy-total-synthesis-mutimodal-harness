"""The Host services the private literature lanes depend on, declared once.

These protocols were previously redeclared in five modules under two different names
(`_ArtifactStore` and `_VerifiedArtifactStore`), with the execution-closure verifier
redeclared in four. Structural typing kept the copies compatible by accident: nothing
checked them against one another, so tightening any one of them would silently leave
the others behind.

They are layered by capability, so a module names the narrowest service it actually
uses. Each is `runtime_checkable` because several lanes reject an unsuitable store
with `isinstance` before touching it -- that check tests method presence only, which
is exactly the pre-call guard the lanes want.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tsdaf.contracts.source import ArtifactRef

__all__ = (
    "ArtifactReader",
    "ExecutionClosureVerifier",
    "PrivateArtifactStore",
    "VerifiedArtifactStore",
)


@runtime_checkable
class ArtifactReader(Protocol):
    """Read exact bytes back for a content-addressed reference."""

    def read_bytes(self, ref: ArtifactRef) -> bytes: ...


@runtime_checkable
class VerifiedArtifactStore(ArtifactReader, Protocol):
    """Read bytes, and prove a reference still resolves to its recorded digest."""

    def verify(self, ref: ArtifactRef) -> None: ...


@runtime_checkable
class PrivateArtifactStore(VerifiedArtifactStore, Protocol):
    """The full store surface: seal new private artifacts as well as read them."""

    def put_bytes(self, payload: bytes, *, media_type: str) -> ArtifactRef: ...


@runtime_checkable
class ExecutionClosureVerifier(Protocol):
    """Return the exact artifact closure of one sealed terminal execution."""

    def verified_execution_artifact_closure(
        self,
        execution_id: str,
        manifest_ref: ArtifactRef,
    ) -> tuple[ArtifactRef, ...]: ...
