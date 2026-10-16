"""Fixed ``tsdaf route`` commands."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tsdaf.apps._provider_config import live_gateway_client, safe_base_url
from tsdaf.apps.composition import (
    _configuration_failure,
    _execution_id_failure,
    _preflight_route,
    execute_route_workflow,
    resume_route_workflow,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.provider_port import (
    LaneCompletedInteraction,
    LaneInteractionClient,
    LanePreparedInteraction,
)

__all__ = ("run_route", "run_route_resume")


def _emit(payload: dict[str, object], *, error: bool) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def _offline_provider(**request: object) -> object:
    del request
    raise AssertionError("offline route attempted a provider interaction")


class _OfflineSplitClient:
    """An offline route serves the reading from its cache or stops at LIVE_REQUIRED."""

    context_policy = None

    @property
    def interaction_stream_owner(self) -> object:
        return self

    def prepare(
        self,
        *,
        input: object,
        system_instruction: object | None = None,
        tools: object | None = None,
        response_format: object | None = None,
        reasoning_effort: object = None,
    ) -> LanePreparedInteraction:
        del input, system_instruction, tools, response_format, reasoning_effort
        raise AssertionError("offline route attempted a reading-units interaction")

    def execute_prepared(self, prepared: Any) -> LaneCompletedInteraction:
        del prepared
        raise AssertionError("offline route attempted a reading-units interaction")


def _provider(
    live: bool,
) -> tuple[
    tuple[Callable[..., object], LaneInteractionClient] | None,
    dict[str, object] | None,
]:
    """The Router's interaction call and the reading's client: two adapters, two streams."""

    if not live:
        return (_offline_provider, _OfflineSplitClient()), None
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = safe_base_url(os.environ.get("OPENAI_BASE_URL"))
    if api_key is None or not api_key.strip():
        return None, _configuration_failure(
            "Explicit live routing requires a nonblank OPENAI_API_KEY."
        )
    if base_url is None:
        return None, _configuration_failure(
            "Explicit live routing requires a safe OPENAI_BASE_URL."
        )
    try:
        adapter = live_gateway_client(api_key.strip(), base_url)
        units_adapter = live_gateway_client(api_key.strip(), base_url)
    except Exception:
        return None, _configuration_failure(
            "The fixed OpenAI-compatible live adapter could not be initialized."
        )
    return (adapter.create, units_adapter), None


def _failure_exit(payload: dict[str, object]) -> int:
    return 2 if payload.get("code") == "CONFIGURATION_INVALID" else 1


def _emit_workflow(code: int, payload: dict[str, object]) -> int:
    _emit(payload, error=code not in {0, 3})
    return code


# The human review queue's fixed place (E86): beside the working directory, outside
# every run workspace, so one batch has one file to aggregate.
DEFAULT_REVIEW_QUEUE = Path("review_queue.csv")


def run_route(package_dir: Path, *, live: bool, review_queue: Path | None = None) -> int:
    """Run the fixed package-to-handoff workflow."""

    preflight = _preflight_route(package_dir)
    if preflight is not None:
        code = _failure_exit(preflight)
        _emit(preflight, error=True)
        return code
    clients, configuration_failure = _provider(live)
    if configuration_failure is not None:
        _emit(configuration_failure, error=True)
        return 2
    assert clients is not None
    interaction_create, units_client = clients
    execution_id = f"exec_{uuid.uuid4().hex}"
    code, payload = execute_route_workflow(
        package_dir=package_dir,
        workspace=Path.cwd() / ".tsdaf-workspace",
        execution_id=execution_id,
        interaction_create=interaction_create,
        provider_is_live=True,
        live=live,
        units_client=units_client,
        review_queue=(Path.cwd() / DEFAULT_REVIEW_QUEUE) if review_queue is None else review_queue,
    )
    return _emit_workflow(code, payload)


def run_route_resume(execution_id: str, *, live: bool, review_queue: Path | None = None) -> int:
    """Resume one fixed local route execution."""

    input_failure = _execution_id_failure(execution_id)
    if input_failure is not None:
        _emit(input_failure, error=True)
        return 1
    clients, configuration_failure = _provider(live)
    if configuration_failure is not None:
        _emit(configuration_failure, error=True)
        return 2
    assert clients is not None
    interaction_create, units_client = clients
    code, payload = resume_route_workflow(
        workspace=Path.cwd() / ".tsdaf-workspace",
        execution_id=execution_id,
        interaction_create=interaction_create,
        provider_is_live=True,
        live=live,
        units_client=units_client,
        review_queue=(Path.cwd() / DEFAULT_REVIEW_QUEUE) if review_queue is None else review_queue,
    )
    return _emit_workflow(code, payload)
