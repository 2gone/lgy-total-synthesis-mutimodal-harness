from __future__ import annotations

import hashlib
import importlib
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _runtime_api() -> ModuleType:
    return importlib.import_module("tsdaf.workflow.local_runtime")


def _contracts() -> tuple[ModuleType, ModuleType, ModuleType]:
    source = importlib.import_module("tsdaf.contracts.source")
    execution = importlib.import_module("tsdaf.contracts.execution")
    schema = importlib.import_module("tsdaf.contracts.schema")
    return source, execution, schema


def test_objects_are_canonical_verified_and_corruption_is_never_overwritten(
    tmp_path: Path,
) -> None:
    runtime = _runtime_api()
    _, execution, schema = _contracts()
    workspace = tmp_path / "workspace"
    store = runtime.LocalObjectStore(workspace)
    payload = b"local-artifact-payload"

    first = store.put_bytes(payload, media_type="application/octet-stream")
    second = store.put_bytes(payload, media_type="application/octet-stream")

    expected_sha256 = hashlib.sha256(payload).hexdigest()
    assert first == second
    assert first.sha256 == expected_sha256
    assert first.byte_size == len(payload)
    assert first.relative_path == (f"objects/sha256/{expected_sha256[:2]}/{expected_sha256}")
    assert store.read_bytes(first) == payload
    assert store.verify(first) is None

    json_value = {"z": [3, 2, 1], "a": {"enabled": True, "value": None}}
    canonical_payload = schema.canonical_json_bytes(json_value)
    json_ref = store.put_json(json_value)
    assert json_ref == store.put_bytes(canonical_payload, media_type="application/json")
    assert store.read_bytes(json_ref) == canonical_payload

    object_path = store.verified_path(first)
    tampered = bytearray(payload)
    tampered[-1] ^= 1
    object_path.write_bytes(tampered)

    with pytest.raises(runtime.LocalRuntimeError) as verify_error:
        store.verify(first)
    assert verify_error.value.code is execution.FailureCode.HASH_MISMATCH
    assert verify_error.value.relative_path == first.relative_path

    with pytest.raises(runtime.LocalRuntimeError) as put_error:
        store.put_bytes(payload, media_type="application/octet-stream")
    assert put_error.value.code is execution.FailureCode.HASH_MISMATCH
    assert object_path.read_bytes() == bytes(tampered)


def test_failed_publication_leaves_no_final_object_or_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime_api()
    _, execution, _ = _contracts()
    workspace = tmp_path / "workspace"
    store = runtime.LocalObjectStore(workspace)
    payload = b"must-not-be-published"
    digest = hashlib.sha256(payload).hexdigest()
    final_path = workspace / "objects" / "sha256" / digest[:2] / digest

    def fail_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        raise OSError("forced pre-publication replacement failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(runtime.LocalRuntimeError) as error:
        store.put_bytes(payload, media_type="application/octet-stream")

    assert error.value.code is execution.FailureCode.ARTIFACT_WRITE_FAILED
    assert not final_path.exists()
    object_root = workspace / "objects"
    assert not object_root.exists() or not any(path.is_file() for path in object_root.rglob("*"))


def test_content_key_is_canonical_order_sensitive_and_validated() -> None:
    runtime = _runtime_api()
    _, _, schema = _contracts()
    hash_a = "a" * 64
    hash_b = "b" * 64
    hash_c = "c" * 64
    hash_d = "d" * 64
    settings_a = {
        "temperature": 0,
        "nested": {"beta": 2, "alpha": 1},
    }
    settings_b = {
        "nested": {"alpha": 1, "beta": 2},
        "temperature": 0,
    }

    def content_key(**overrides: Any) -> str:
        arguments: dict[str, Any] = {
            "stage_name": "main-router",
            "stage_version": "1.0.0",
            "input_sha256s": (hash_a, hash_b),
            "settings": settings_a,
            "prompt_sha256": None,
            "schema_sha256": None,
            "model_profile": None,
        }
        arguments.update(overrides)
        return runtime.stage_content_key(**arguments)

    base = content_key()
    reordered_settings = content_key(settings=settings_b)
    expected_preimage = {
        "schema_version": "tsdaf.stage-content-key.v1",
        "stage_name": "main-router",
        "stage_version": "1.0.0",
        "input_sha256s": [hash_a, hash_b],
        "settings": settings_a,
        "prompt_sha256": None,
        "schema_sha256": None,
        "model_profile": None,
    }
    expected = hashlib.sha256(schema.canonical_json_bytes(expected_preimage)).hexdigest()

    assert base == expected
    assert reordered_settings == base
    changed = {
        content_key(input_sha256s=(hash_b, hash_a)),
        content_key(stage_name="main-router-v2"),
        content_key(stage_version="2.0.0"),
        content_key(prompt_sha256=hash_c),
        content_key(schema_sha256=hash_d),
        content_key(model_profile="recorded-fake-v1"),
    }
    assert base not in changed
    assert len(changed) == 6

    invalid_calls: tuple[Callable[[], str], ...] = (
        lambda: content_key(input_sha256s=("not-a-sha256",)),
        lambda: content_key(prompt_sha256="A" * 64),
        lambda: content_key(settings={"temperature": float("nan")}),
        lambda: content_key(settings={"unsupported": object()}),
        lambda: content_key(settings={1: "non-string-key"}),
        lambda: content_key(stage_name="../unsafe"),
    )
    for invalid_call in invalid_calls:
        with pytest.raises((TypeError, ValueError)):
            invalid_call()
