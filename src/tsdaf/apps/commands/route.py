"""Fixed ``tsdaf route`` commands."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from tsdaf.apps.composition import (
    _configuration_failure,
    _execution_id_failure,
    _preflight_route,
    execute_route_workflow,
    resume_route_workflow,
)
from tsdaf.contracts.schema import canonical_json_bytes

__all__ = ("run_route", "run_route_resume")

_FIXED_MODEL = "gemini-3.7-flash-high"


def _emit(payload: dict[str, object], *, error: bool) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def _offline_provider(**request: object) -> object:
    del request
    raise AssertionError("offline route attempted a provider interaction")


def _safe_base_url(value: str | None) -> str | None:
    if value is None or not value or value != value.strip():
        return None
    parsed = urlsplit(value)
    if (
        not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})
        )
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1"
    ):
        return None
    return value


def _provider(live: bool) -> tuple[Callable[..., object] | None, dict[str, object] | None]:
    if not live:
        return _offline_provider, None
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = _safe_base_url(os.environ.get("OPENAI_BASE_URL"))
    if api_key is None or not api_key.strip():
        return None, _configuration_failure(
            "Explicit live routing requires a nonblank OPENAI_API_KEY."
        )
    if base_url is None:
        return None, _configuration_failure(
            "Explicit live routing requires a safe OPENAI_BASE_URL."
        )
    try:
        from tsdaf.adapters.gemini.interactions import GeminiInteractions

        adapter = GeminiInteractions.from_api_key(api_key=api_key.strip(), base_url=base_url)
    except Exception:
        return None, _configuration_failure(
            "The fixed OpenAI-compatible live adapter could not be initialized."
        )
    return adapter.create, None


def _failure_exit(payload: dict[str, object]) -> int:
    return 2 if payload.get("code") == "CONFIGURATION_INVALID" else 1


def _emit_workflow(code: int, payload: dict[str, object]) -> int:
    _emit(payload, error=code not in {0, 3})
    return code


def run_route(package_dir: Path, *, live: bool) -> int:
    """Run the fixed package-to-handoff workflow."""

    preflight = _preflight_route(package_dir)
    if preflight is not None:
        code = _failure_exit(preflight)
        _emit(preflight, error=True)
        return code
    interaction_create, configuration_failure = _provider(live)
    if configuration_failure is not None:
        _emit(configuration_failure, error=True)
        return 2
    assert interaction_create is not None
    execution_id = f"exec_{uuid.uuid4().hex}"
    code, payload = execute_route_workflow(
        package_dir=package_dir,
        workspace=Path.cwd() / ".tsdaf-workspace",
        execution_id=execution_id,
        interaction_create=interaction_create,
        provider_is_live=True,
        live=live,
    )
    return _emit_workflow(code, payload)


def run_route_resume(execution_id: str, *, live: bool) -> int:
    """Resume one fixed local route execution."""

    input_failure = _execution_id_failure(execution_id)
    if input_failure is not None:
        _emit(input_failure, error=True)
        return 1
    interaction_create, configuration_failure = _provider(live)
    if configuration_failure is not None:
        _emit(configuration_failure, error=True)
        return 2
    assert interaction_create is not None
    code, payload = resume_route_workflow(
        workspace=Path.cwd() / ".tsdaf-workspace",
        execution_id=execution_id,
        interaction_create=interaction_create,
        provider_is_live=True,
        live=live,
    )
    return _emit_workflow(code, payload)
