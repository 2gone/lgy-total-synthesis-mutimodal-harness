"""Provider-neutral injection point for the private literature lanes.

The lane must not import a concrete adapter: `AUTHORIZED_MODEL_PROFILE` pins the
profile its prompts and schemas were frozen against, and `ProviderBinding` carries
the exact adapter types the composition root authorizes. The lane therefore keeps
refusing duck-typed doubles without depending on `tsdaf.adapters`.

`tests/unit/literature/test_provider_port.py` asserts that the pinned profile still
equals the adapter constant, so the two declarations stay one rule.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = (
    "AUTHORIZED_MODEL_PROFILE",
    "MEDIUM_READER_MODEL_PROFILE",
    "LaneCompletedInteraction",
    "LaneInteractionClient",
    "LanePreparedInteraction",
    "ProviderBinding",
    "require_lane_model_profile",
)

AUTHORIZED_MODEL_PROFILE = "gemini-3.8-flash-high-chat-completions-v1"
# No stage accepts this since the reaction windows' readers were retired (2026-09-23). It
# stays because published results are numbered under it (`LITERATURE_MODEL_PROFILE_VERSIONS`).
MEDIUM_READER_MODEL_PROFILE = "gemini-3.8-flash-high-medium-effort-chat-completions-v1"


def require_lane_model_profile(stage: str, profile: str) -> None:
    if profile == AUTHORIZED_MODEL_PROFILE:
        return
    raise ValueError("lane model profile is not authorized for its stage")


class LanePreparedInteraction(Protocol):
    """Exact request bytes bound to the adapter that prepared them."""

    @property
    def request_bytes(self) -> bytes: ...

    @property
    def model_profile(self) -> str: ...

    @property
    def context_capacity(self) -> Mapping[str, object] | None: ...


class LaneCompletedInteraction(Protocol):
    """Immutable provider-neutral evidence from one completed interaction."""

    @property
    def request_bytes(self) -> bytes: ...

    @property
    def response_bytes(self) -> bytes: ...

    @property
    def interaction_id(self) -> str | None: ...

    @property
    def output_text(self) -> str | None: ...

    @property
    def total_input_tokens(self) -> int | None: ...

    @property
    def total_output_tokens(self) -> int | None: ...

    @property
    def total_tokens(self) -> int | None: ...

    @property
    def total_cached_tokens(self) -> int | None: ...

    @property
    def total_thought_tokens(self) -> int | None: ...

    @property
    def total_tool_use_tokens(self) -> int | None: ...

    @property
    def request_id(self) -> str | None: ...

    @property
    def elapsed_ms(self) -> float: ...

    @property
    def transport_attempts(self) -> int: ...


class LaneInteractionClient(Protocol):
    """The only adapter surface the private literature lanes may call.

    `prepare` returns an opaque, adapter-bound token that must be handed straight
    back to the same client, so it is untyped here on the way in: the lane reads only
    `request_bytes` and `model_profile` off it and never constructs one itself.
    """

    @property
    def interaction_stream_owner(self) -> object: ...

    @property
    def context_policy(self) -> Mapping[str, object] | None: ...

    def prepare(
        self,
        *,
        input: object,
        system_instruction: object | None = ...,
        tools: object | None = ...,
        response_format: object | None = ...,
        reasoning_effort: object = ...,
    ) -> LanePreparedInteraction: ...

    def execute_prepared(self, prepared: Any) -> LaneCompletedInteraction: ...


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """Exact adapter types the composition root authorizes for one lane run."""

    client_types: tuple[type, ...]
    interaction_type: type
    # Host-only identity, supplied by composition; absent means no reader reuse.
    backend_namespaces: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.client_types) is not tuple or not self.client_types:
            raise TypeError("provider binding requires a nonempty client type tuple")
        if any(type(item) is not type for item in self.client_types):
            raise TypeError("provider binding client types must be exact classes")
        if len(set(self.client_types)) != len(self.client_types):
            raise ValueError("provider binding client types must be unique")
        if type(self.interaction_type) is not type:
            raise TypeError("provider binding interaction type must be an exact class")

        if type(self.backend_namespaces) is not tuple:
            raise TypeError("backend namespaces must be an immutable tuple")
        seen: set[str] = set()
        for item in self.backend_namespaces:
            if type(item) is not tuple or len(item) != 2:
                raise TypeError("backend namespace entries must be stage/identity pairs")
            stage, namespace = item
            if stage not in ("T1", "V1", "GATE1") or stage in seen:
                raise ValueError("backend namespace stages must be unique route lanes")
            if (
                not isinstance(namespace, str)
                or re.fullmatch(r"(?:live|recorded)-v1:[0-9a-f]{64}", namespace) is None
            ):
                raise ValueError("backend namespace must be a nonsecret versioned digest")
            seen.add(stage)

    def namespace_for(self, stage: str) -> str | None:
        return dict(self.backend_namespaces).get(stage)

    def require_client(self, value: object, *, label: str) -> LaneInteractionClient:
        """Return `value` only when it is an exact authorized adapter instance."""

        if type(value) not in self.client_types:
            raise TypeError(f"{label} requires an exact authorized interaction adapter")
        return value  # type: ignore[return-value]

    def require_interaction(self, value: object, *, label: str) -> LaneCompletedInteraction:
        """Return `value` only when it is an exact authorized interaction record."""

        if type(value) is not self.interaction_type:
            raise TypeError(f"{label} requires an exact authorized interaction record")
        return value  # type: ignore[return-value]
