"""Concrete local artifact storage and execution evidence runtime."""

from __future__ import annotations

import hashlib as _hashlib
import importlib as _importlib
import json as _json
import math as _math
import os as _os
import re as _re
import sqlite3 as _sqlite3
import stat as _stat
import threading as _threading
import time as _time
import uuid as _uuid
from collections.abc import Callable as _Callable
from collections.abc import Iterator as _Iterator
from collections.abc import Mapping as _Mapping
from collections.abc import Sequence as _Sequence
from contextlib import contextmanager as _contextmanager
from datetime import UTC as _UTC
from datetime import datetime as _datetime
from pathlib import Path as _Path
from typing import Any as _Any

from tsdaf.contracts.execution import ExecutionStatus as _ExecutionStatus
from tsdaf.contracts.execution import FailureCode as _FailureCode
from tsdaf.contracts.execution import StageResult as _StageResult
from tsdaf.contracts.execution import TypedFailure as _TypedFailure
from tsdaf.contracts.schema import canonical_json_bytes as _canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef as _ArtifactRef

__all__ = [
    "LocalObjectStore",
    "LocalRunner",
    "LocalRuntimeError",
    "stage_content_key",
]

_SHA256_RE = _re.compile(r"^[0-9a-f]{64}$")
_SAFE_COMPONENT_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTENT_KEY_SCHEMA_VERSION = "tsdaf.stage-content-key.v1"
_RESULT_MEDIA_TYPE = "application/vnd.tsdaf.stage-result+json"
_EVENT_MEDIA_TYPE = "application/x-ndjson"
_MANIFEST_MEDIA_TYPE = "application/json"
_RECORD_SEAL_SCHEMA_VERSION = "tsdaf.local-record-seal.v1"
_INDEX_USER_VERSION = 1
_INDEX_TABLES = (
    "executions",
    "execution_events",
    "stage_attempts",
    "stage_outputs",
    "artifacts",
)

_PROCESS_LOCKS: dict[str, _threading.Lock] = {}
_PROCESS_LOCKS_GUARD = _threading.Lock()


class LocalRuntimeError(RuntimeError):
    """A narrow local-runtime failure carrying an existing domain failure code."""

    code: _FailureCode
    relative_path: str | None

    def __init__(
        self,
        code: _FailureCode,
        message: str,
        relative_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.relative_path = relative_path


def _runtime_error(
    code: _FailureCode,
    message: str,
    *,
    workspace: _Path | None = None,
    path: _Path | None = None,
    relative_path: str | None = None,
) -> LocalRuntimeError:
    if relative_path is None and workspace is not None and path is not None:
        try:
            relative_path = path.relative_to(workspace).as_posix()
        except ValueError:
            relative_path = None
    return LocalRuntimeError(code, message, relative_path)


def _path_exists(path: _Path) -> bool:
    return _os.path.lexists(path)


def _resolved_path(path: _Path) -> _Path:
    resolved = path.resolve(strict=False)
    if _os.name != "nt":
        return resolved
    value = str(resolved)
    if value.startswith("\\\\?\\UNC\\"):
        return _Path(f"\\\\{value[8:]}")
    if value.startswith("\\\\?\\"):
        return _Path(value[4:])
    return resolved


def _ensure_contained(workspace: _Path, path: _Path) -> None:
    resolved = _resolved_path(path)
    if not resolved.is_relative_to(_resolved_path(workspace)):
        raise _runtime_error(
            _FailureCode.ASSET_MISSING,
            "runtime path escapes the workspace",
            workspace=workspace,
            path=path,
        )


def _validate_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase 64-character SHA-256")
    return value


def _validate_safe_component(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_COMPONENT_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a safe nonempty identifier")
    return value


def _validate_stable_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a nonempty stable string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")
    return value


def _normalize_json(value: object, *, label: str, active: set[int] | None = None) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not _math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if active is None:
        active = set()
    if isinstance(value, _Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} contains a circular mapping")
        active.add(identity)
        try:
            normalized: dict[str, object] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"{label} mapping keys must be strings")
                normalized[key] = _normalize_json(child, label=label, active=active)
            return normalized
        finally:
            active.remove(identity)
    if isinstance(value, list | tuple):
        identity = id(value)
        if identity in active:
            raise ValueError(f"{label} contains a circular sequence")
        active.add(identity)
        try:
            return [_normalize_json(child, label=label, active=active) for child in value]
        finally:
            active.remove(identity)
    raise TypeError(f"{label} contains a non-JSON value of type {type(value).__name__}")


def _canonical_mapping(value: _Mapping[str, object], *, label: str) -> dict[str, object]:
    normalized = _normalize_json(value, label=label)
    if not isinstance(normalized, dict):
        raise TypeError(f"{label} must be a mapping")
    return normalized


def stage_content_key(
    *,
    stage_name: str,
    stage_version: str,
    input_sha256s: _Sequence[str],
    settings: _Mapping[str, object],
    prompt_sha256: str | None = None,
    schema_sha256: str | None = None,
    model_profile: str | None = None,
) -> str:
    """Return the canonical content identity for one deterministic stage invocation."""

    checked_stage_name = _validate_safe_component(stage_name, label="stage name")
    checked_stage_version = _validate_safe_component(stage_version, label="stage version")
    if isinstance(input_sha256s, str | bytes) or not isinstance(input_sha256s, _Sequence):
        raise TypeError("input_sha256s must be an ordered sequence")
    checked_inputs = [
        _validate_sha256(value, label="input artifact sha256") for value in input_sha256s
    ]
    if not isinstance(settings, _Mapping):
        raise TypeError("settings must be a mapping")
    checked_settings = _canonical_mapping(settings, label="settings")
    checked_prompt = (
        None if prompt_sha256 is None else _validate_sha256(prompt_sha256, label="prompt sha256")
    )
    checked_schema = (
        None if schema_sha256 is None else _validate_sha256(schema_sha256, label="schema sha256")
    )
    checked_model = (
        None
        if model_profile is None
        else _validate_stable_string(model_profile, label="model profile")
    )
    preimage = {
        "schema_version": _CONTENT_KEY_SCHEMA_VERSION,
        "stage_name": checked_stage_name,
        "stage_version": checked_stage_version,
        "input_sha256s": checked_inputs,
        "settings": checked_settings,
        "prompt_sha256": checked_prompt,
        "schema_sha256": checked_schema,
        "model_profile": checked_model,
    }
    return _hashlib.sha256(_canonical_json_bytes(preimage)).hexdigest()


def _process_lock(path: _Path) -> _threading.Lock:
    resolved = _resolved_path(path)
    key = str(resolved).casefold() if _os.name == "nt" else str(resolved)
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = _threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _acquire_descriptor_lock(descriptor: int) -> None:
    if _os.name == "nt":
        _msvcrt = _importlib.import_module("msvcrt")

        while True:
            try:
                _os.lseek(descriptor, 0, _os.SEEK_SET)
                _msvcrt.locking(descriptor, _msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                _time.sleep(0.01)
    else:
        _fcntl = _importlib.import_module("fcntl")

        _fcntl.flock(descriptor, _fcntl.LOCK_EX)


def _release_descriptor_lock(descriptor: int) -> None:
    if _os.name == "nt":
        _msvcrt = _importlib.import_module("msvcrt")

        _os.lseek(descriptor, 0, _os.SEEK_SET)
        _msvcrt.locking(descriptor, _msvcrt.LK_UNLCK, 1)
    else:
        _fcntl = _importlib.import_module("fcntl")

        _fcntl.flock(descriptor, _fcntl.LOCK_UN)


@_contextmanager
def _native_lock(workspace: _Path, path: _Path) -> _Iterator[None]:
    _ensure_contained(workspace, path)
    local_lock = _process_lock(path)
    local_lock.acquire()
    descriptor: int | None = None
    descriptor_locked = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_contained(workspace, path)
        descriptor = _os.open(path, _os.O_RDWR | _os.O_CREAT, 0o600)
        size = _os.fstat(descriptor).st_size
        if size == 0:
            _os.lseek(descriptor, 0, _os.SEEK_SET)
            _os.write(descriptor, b"\0")
            _os.fsync(descriptor)
        elif size != 1:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "native lock file must contain exactly one byte",
                workspace=workspace,
                path=path,
            )
        _acquire_descriptor_lock(descriptor)
        descriptor_locked = True
        yield
    except LocalRuntimeError:
        raise
    except OSError as error:
        raise _runtime_error(
            _FailureCode.ARTIFACT_WRITE_FAILED,
            f"native lock operation failed: {error}",
            workspace=workspace,
            path=path,
        ) from error
    finally:
        if descriptor is not None:
            try:
                if descriptor_locked:
                    _release_descriptor_lock(descriptor)
            finally:
                _os.close(descriptor)
        local_lock.release()


def _fsync_parent(path: _Path) -> None:
    if _os.name == "nt":
        return
    descriptor = _os.open(path, _os.O_RDONLY)
    try:
        _os.fsync(descriptor)
    finally:
        _os.close(descriptor)


def _temporary_sibling(path: _Path) -> _Path:
    return path.with_name(f".tmp-{_uuid.uuid4().hex[:12]}")


def _write_exclusive_file(path: _Path, payload: bytes) -> None:
    descriptor = _os.open(path, _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL, 0o600)
    try:
        with _os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            _os.fsync(handle.fileno())
    finally:
        _os.close(descriptor)


def _unlink_if_present(path: _Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _atomic_create_exact(workspace: _Path, path: _Path, payload: bytes) -> bool:
    _ensure_contained(workspace, path)
    temporary = _temporary_sibling(path)
    created = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ensure_contained(workspace, path)
        _write_exclusive_file(temporary, payload)
        if _path_exists(path):
            if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "immutable record already exists with different bytes",
                    workspace=workspace,
                    path=path,
                )
            return False
        _os.replace(temporary, path)
        created = True
        _fsync_parent(path.parent)
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "published immutable record failed verification",
                workspace=workspace,
                path=path,
            )
        return True
    except LocalRuntimeError:
        raise
    except OSError as error:
        raise _runtime_error(
            _FailureCode.ARTIFACT_WRITE_FAILED,
            f"atomic record publication failed: {error}",
            workspace=workspace,
            path=path,
        ) from error
    finally:
        if not created:
            _unlink_if_present(temporary)


def _seal_path(path: _Path) -> _Path:
    return path.with_name(f"{path.name}.seal.json")


def _seal_document(payload: bytes, *, media_type: str) -> dict[str, object]:
    return {
        "schema_version": _RECORD_SEAL_SCHEMA_VERSION,
        "record_kind": "immutable_record_seal",
        "sha256": _hashlib.sha256(payload).hexdigest(),
        "byte_size": len(payload),
        "media_type": media_type,
    }


def _read_sealed_bytes(workspace: _Path, path: _Path, *, media_type: str) -> bytes:
    seal_path = _seal_path(path)
    if not _path_exists(path):
        raise _runtime_error(
            _FailureCode.ASSET_MISSING,
            "immutable record is missing",
            workspace=workspace,
            path=path,
        )
    if not _path_exists(seal_path):
        raise _runtime_error(
            _FailureCode.HASH_MISMATCH,
            "immutable record seal is missing",
            workspace=workspace,
            path=seal_path,
        )
    if path.is_symlink() or not path.is_file() or seal_path.is_symlink() or not seal_path.is_file():
        raise _runtime_error(
            _FailureCode.ASSET_MISSING,
            "immutable record or seal is not a regular file",
            workspace=workspace,
            path=path,
        )
    try:
        payload = path.read_bytes()
        seal_payload = seal_path.read_bytes()
        seal = _json.loads(seal_payload)
    except (OSError, UnicodeDecodeError, _json.JSONDecodeError) as error:
        raise _runtime_error(
            _FailureCode.HASH_MISMATCH,
            f"immutable record seal cannot be read: {error}",
            workspace=workspace,
            path=path,
        ) from error
    expected = _seal_document(payload, media_type=media_type)
    if seal != expected or seal_payload != _canonical_json_bytes(expected):
        raise _runtime_error(
            _FailureCode.HASH_MISMATCH,
            "immutable record seal does not match its bytes",
            workspace=workspace,
            path=path,
        )
    return payload


def _write_sealed_bytes(
    workspace: _Path,
    path: _Path,
    payload: bytes,
    *,
    media_type: str,
) -> None:
    if _path_exists(path) or _path_exists(_seal_path(path)):
        existing = _read_sealed_bytes(workspace, path, media_type=media_type)
        if existing != payload:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "immutable record conflicts with existing bytes",
                workspace=workspace,
                path=path,
            )
        return
    created = _atomic_create_exact(workspace, path, payload)
    try:
        seal_payload = _canonical_json_bytes(_seal_document(payload, media_type=media_type))
        _atomic_create_exact(workspace, _seal_path(path), seal_payload)
    except Exception:
        if created:
            _unlink_if_present(path)
        raise


def _write_sealed_json(workspace: _Path, path: _Path, value: object) -> None:
    normalized = _normalize_json(value, label="runtime record")
    _write_sealed_bytes(
        workspace,
        path,
        _canonical_json_bytes(normalized),
        media_type="application/json",
    )


def _read_sealed_json(workspace: _Path, path: _Path) -> dict[str, _Any]:
    payload = _read_sealed_bytes(workspace, path, media_type="application/json")
    try:
        value = _json.loads(payload)
    except (UnicodeDecodeError, _json.JSONDecodeError) as error:
        raise _runtime_error(
            _FailureCode.HASH_MISMATCH,
            f"immutable JSON record is invalid: {error}",
            workspace=workspace,
            path=path,
        ) from error
    if not isinstance(value, dict) or _canonical_json_bytes(value) != payload:
        raise _runtime_error(
            _FailureCode.HASH_MISMATCH,
            "immutable JSON record is not canonical",
            workspace=workspace,
            path=path,
        )
    return value


def _optional_sealed_json(workspace: _Path, path: _Path) -> dict[str, _Any] | None:
    if not _path_exists(path) and not _path_exists(_seal_path(path)):
        return None
    return _read_sealed_json(workspace, path)


def _seal_existing_bytes(
    workspace: _Path,
    path: _Path,
    payload: bytes,
    *,
    media_type: str,
) -> None:
    if not _path_exists(path) or path.is_symlink() or not path.is_file():
        raise _runtime_error(
            _FailureCode.ASSET_MISSING,
            "cannot seal a missing or non-regular file",
            workspace=workspace,
            path=path,
        )
    try:
        actual = path.read_bytes()
    except OSError as error:
        raise _runtime_error(
            _FailureCode.ASSET_MISSING,
            f"file cannot be read before sealing: {error}",
            workspace=workspace,
            path=path,
        ) from error
    if actual != payload:
        raise _runtime_error(
            _FailureCode.HASH_MISMATCH,
            "file changed before its terminal seal was written",
            workspace=workspace,
            path=path,
        )
    seal_payload = _canonical_json_bytes(_seal_document(payload, media_type=media_type))
    _atomic_create_exact(workspace, _seal_path(path), seal_payload)
    _read_sealed_bytes(workspace, path, media_type=media_type)


class LocalObjectStore:
    """Content-addressed immutable bytes rooted beneath one local workspace."""

    def __init__(self, workspace: _Path) -> None:
        if not isinstance(workspace, _Path):
            raise TypeError("workspace must be a pathlib.Path")
        self._workspace = workspace.resolve(strict=False)

    def _relative_path(self, sha256: str) -> str:
        return f"objects/sha256/{sha256[:2]}/{sha256}"

    def _content_lock_path(self, key: str) -> _Path:
        return self._workspace / "locks" / "content" / key[:2] / f"{key}.lock"

    def put_bytes(self, payload: bytes, *, media_type: str) -> _ArtifactRef:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        digest = _hashlib.sha256(payload).hexdigest()
        relative_path = self._relative_path(digest)
        ref = _ArtifactRef(
            sha256=digest,
            media_type=media_type,
            byte_size=len(payload),
            relative_path=relative_path,
        )
        final_path = self._workspace / relative_path
        temporary = _temporary_sibling(final_path)
        published = False
        try:
            _ensure_contained(self._workspace, final_path)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            _ensure_contained(self._workspace, final_path)
            _write_exclusive_file(temporary, payload)
            temporary_payload = temporary.read_bytes()
            if (
                len(temporary_payload) != ref.byte_size
                or _hashlib.sha256(temporary_payload).hexdigest() != ref.sha256
            ):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "temporary object bytes do not match their requested identity",
                    workspace=self._workspace,
                    path=temporary,
                )
            with _native_lock(self._workspace, self._content_lock_path(digest)):
                if _path_exists(final_path):
                    self.verify(ref)
                    return ref
                try:
                    _os.replace(temporary, final_path)
                    published = True
                    _fsync_parent(final_path.parent)
                except OSError as error:
                    raise _runtime_error(
                        _FailureCode.ARTIFACT_WRITE_FAILED,
                        f"object publication failed: {error}",
                        workspace=self._workspace,
                        path=final_path,
                    ) from error
                self.verify(ref)
                return ref
        except LocalRuntimeError:
            raise
        except OSError as error:
            raise _runtime_error(
                _FailureCode.ARTIFACT_WRITE_FAILED,
                f"object write failed: {error}",
                workspace=self._workspace,
                path=final_path,
            ) from error
        finally:
            if not published:
                _unlink_if_present(temporary)

    def put_json(
        self,
        value: object,
        *,
        media_type: str = "application/json",
    ) -> _ArtifactRef:
        normalized = _normalize_json(value, label="JSON artifact")
        return self.put_bytes(_canonical_json_bytes(normalized), media_type=media_type)

    def verified_path(self, ref: _ArtifactRef) -> _Path:
        if not isinstance(ref, _ArtifactRef):
            raise TypeError("ref must be an ArtifactRef")
        expected_relative_path = self._relative_path(ref.sha256)
        if ref.relative_path != expected_relative_path:
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                "artifact path is not the canonical object path for its hash",
                relative_path=ref.relative_path,
            )
        path = self._workspace / ref.relative_path
        _ensure_contained(self._workspace, path)
        if not _path_exists(path) or path.is_symlink():
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                "artifact is missing or is not a regular contained file",
                workspace=self._workspace,
                path=path,
            )
        try:
            mode = path.stat().st_mode
        except OSError as error:
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                f"artifact metadata cannot be read: {error}",
                workspace=self._workspace,
                path=path,
            ) from error
        if not _stat.S_ISREG(mode):
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                "artifact is not a regular file",
                workspace=self._workspace,
                path=path,
            )
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                f"artifact bytes cannot be read: {error}",
                workspace=self._workspace,
                path=path,
            ) from error
        if len(payload) != ref.byte_size or _hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "artifact size or SHA-256 does not match its reference",
                workspace=self._workspace,
                path=path,
            )
        return path

    def read_bytes(self, ref: _ArtifactRef) -> bytes:
        return self.verified_path(ref).read_bytes()

    def verify(self, ref: _ArtifactRef) -> None:
        self.verified_path(ref)


def _utc_now() -> _datetime:
    return _datetime.now(_UTC)


def _utc_json(value: _datetime) -> str:
    return value.astimezone(_UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ref_json(ref: _ArtifactRef) -> dict[str, _Any]:
    return ref.model_dump(mode="json")


def _ref_from_json(value: object, *, label: str) -> _ArtifactRef:
    try:
        return _ArtifactRef.model_validate(value)
    except ValueError as error:
        raise LocalRuntimeError(
            _FailureCode.HASH_MISMATCH,
            f"{label} is not a valid ArtifactRef: {error}",
        ) from error


def _record_header(
    value: _Mapping[str, object],
    *,
    schema_version: str,
    record_kind: str,
    workspace: _Path,
    path: _Path,
) -> None:
    if value.get("schema_version") != schema_version or value.get("record_kind") != record_kind:
        raise _runtime_error(
            _FailureCode.HASH_MISMATCH,
            f"unexpected immutable {record_kind} record header",
            workspace=workspace,
            path=path,
        )


class LocalRunner:
    """Small filesystem-authoritative runner for deterministic local stages."""

    def __init__(
        self,
        workspace: _Path,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        if not isinstance(workspace, _Path):
            raise TypeError("workspace must be a pathlib.Path")
        self._workspace = workspace.resolve(strict=False)
        self._object_store = object_store or LocalObjectStore(workspace)
        if self._object_store._workspace != self._workspace:
            raise ValueError("object_store must use the runner workspace")

    def _execution_root(self, execution_id: str) -> _Path:
        return self._workspace / "executions" / execution_id

    def _execution_lock_path(self, execution_id: str) -> _Path:
        return self._workspace / "locks" / "execution" / f"{execution_id}.lock"

    def _content_lock_path(self, content_key: str) -> _Path:
        return self._workspace / "locks" / "content" / content_key[:2] / f"{content_key}.lock"

    def _index_lock_path(self) -> _Path:
        return self._workspace / "locks" / "index" / "index.lock"

    def _stage_record_path(self, execution_id: str, content_key: str) -> _Path:
        return self._execution_root(execution_id) / "stages" / content_key / "result-ref.json"

    def _attempt_root(self, execution_id: str, attempt_id: str) -> _Path:
        return self._execution_root(execution_id) / "attempts" / attempt_id

    def _cache_path(self, stage_name: str, content_key: str) -> _Path:
        return self._workspace / "stage-results" / stage_name / content_key / "result.json"

    def _validate_request(
        self,
        *,
        execution_id: str,
        stage_name: str,
        stage_version: str,
        input_artifacts: _Sequence[_ArtifactRef],
        settings: _Mapping[str, object],
        prompt_sha256: str | None,
        schema_sha256: str | None,
        model_profile: str | None,
        requires_live: bool,
        live: bool,
    ) -> tuple[str, str, str, tuple[_ArtifactRef, ...], dict[str, object], str]:
        checked_execution = _validate_safe_component(execution_id, label="execution id")
        checked_stage = _validate_safe_component(stage_name, label="stage name")
        checked_version = _validate_safe_component(stage_version, label="stage version")
        if not isinstance(input_artifacts, _Sequence):
            raise TypeError("input_artifacts must be an ordered sequence")
        checked_artifacts: list[_ArtifactRef] = []
        for artifact in input_artifacts:
            if not isinstance(artifact, _ArtifactRef):
                raise TypeError("input_artifacts must contain ArtifactRef values")
            self._object_store.verify(artifact)
            checked_artifacts.append(artifact)
        if not isinstance(settings, _Mapping):
            raise TypeError("settings must be a mapping")
        checked_settings = _canonical_mapping(settings, label="settings")
        if not isinstance(requires_live, bool) or not isinstance(live, bool):
            raise TypeError("requires_live and live must be bool values")
        content_key = stage_content_key(
            stage_name=checked_stage,
            stage_version=checked_version,
            input_sha256s=tuple(artifact.sha256 for artifact in checked_artifacts),
            settings=checked_settings,
            prompt_sha256=prompt_sha256,
            schema_sha256=schema_sha256,
            model_profile=model_profile,
        )
        return (
            checked_execution,
            checked_stage,
            checked_version,
            tuple(checked_artifacts),
            checked_settings,
            content_key,
        )

    def _load_result_artifact(
        self,
        ref: _ArtifactRef,
        *,
        evidence_path: _Path,
    ) -> _StageResult:
        payload = self._object_store.read_bytes(ref)
        try:
            result = _StageResult.model_validate_json(payload)
        except ValueError as error:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                f"stage-result artifact is invalid: {error}",
                workspace=self._workspace,
                path=evidence_path,
            ) from error
        if _canonical_json_bytes(result.model_dump(mode="json")) != payload:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "stage-result artifact is not canonical JSON",
                workspace=self._workspace,
                path=evidence_path,
            )
        if result.output_artifact is not None:
            self._object_store.verify(result.output_artifact)
        if result.cached_from_result is not None:
            producer_payload = self._object_store.read_bytes(result.cached_from_result)
            try:
                producer = _StageResult.model_validate_json(producer_payload)
            except ValueError as error:
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    f"cached producer result is invalid: {error}",
                    workspace=self._workspace,
                    path=evidence_path,
                ) from error
            if (
                producer.status is not _ExecutionStatus.SUCCEEDED
                or producer.output_artifact != result.output_artifact
            ):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "cached result does not bind one successful producer",
                    workspace=self._workspace,
                    path=evidence_path,
                )
        return result

    def _load_result_record(
        self,
        path: _Path,
        *,
        schema_version: str,
        record_kind: str,
    ) -> tuple[_StageResult, _ArtifactRef, dict[str, _Any]]:
        record = _read_sealed_json(self._workspace, path)
        _record_header(
            record,
            schema_version=schema_version,
            record_kind=record_kind,
            workspace=self._workspace,
            path=path,
        )
        result_ref = _ref_from_json(record.get("result_ref"), label="result_ref")
        result = self._load_result_artifact(result_ref, evidence_path=path)
        return result, result_ref, record

    def _load_local_stage(
        self,
        execution_id: str,
        stage_name: str,
        stage_version: str,
        content_key: str,
    ) -> _StageResult | None:
        path = self._stage_record_path(execution_id, content_key)
        if not _path_exists(path) and not _path_exists(_seal_path(path)):
            return None
        result, _, record = self._load_result_record(
            path,
            schema_version="tsdaf.local-stage-result-ref.v1",
            record_kind="completed_stage_result_ref",
        )
        if (
            record.get("execution_id") != execution_id
            or record.get("stage_name") != stage_name
            or record.get("stage_version") != stage_version
            or record.get("content_key") != content_key
            or result.execution_id != execution_id
            or result.stage_name != stage_name
            or result.stage_version != stage_version
            or result.input_content_key != content_key
            or result.status is _ExecutionStatus.FAILED_RETRYABLE
        ):
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "execution-local completed stage has divergent identity",
                workspace=self._workspace,
                path=path,
            )
        return result

    def _load_cache(
        self,
        stage_name: str,
        stage_version: str,
        content_key: str,
    ) -> tuple[_StageResult, _ArtifactRef] | None:
        path = self._cache_path(stage_name, content_key)
        if not _path_exists(path) and not _path_exists(_seal_path(path)):
            return None
        result, result_ref, record = self._load_result_record(
            path,
            schema_version="tsdaf.local-stage-cache.v1",
            record_kind="successful_stage_cache",
        )
        output_ref = _ref_from_json(record.get("output_artifact"), label="cache output_artifact")
        if (
            record.get("stage_name") != stage_name
            or record.get("stage_version") != stage_version
            or record.get("content_key") != content_key
            or result.stage_name != stage_name
            or result.stage_version != stage_version
            or result.input_content_key != content_key
            or result.status is not _ExecutionStatus.SUCCEEDED
            or result.output_artifact != output_ref
        ):
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "global stage cache is not one verified successful producer",
                workspace=self._workspace,
                path=path,
            )
        self._object_store.verify(output_ref)
        return result, result_ref

    def _ensure_execution_unlocked(self, execution_id: str) -> dict[str, _Any]:
        path = self._execution_root(execution_id) / "execution.json"
        existing = _optional_sealed_json(self._workspace, path)
        if existing is not None:
            _record_header(
                existing,
                schema_version="tsdaf.local-execution.v1",
                record_kind="execution",
                workspace=self._workspace,
                path=path,
            )
            if existing.get("execution_id") != execution_id:
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "execution record identity is divergent",
                    workspace=self._workspace,
                    path=path,
                )
            return existing
        record = {
            "schema_version": "tsdaf.local-execution.v1",
            "record_kind": "execution",
            "execution_id": execution_id,
            "created_at": _utc_json(_utc_now()),
        }
        _write_sealed_json(self._workspace, path, record)
        return record

    def _ensure_execution_mutable_unlocked(self, execution_id: str) -> None:
        execution_root = self._execution_root(execution_id)
        manifest_path = execution_root / "manifest.json"
        if _path_exists(manifest_path) or _path_exists(_seal_path(manifest_path)):
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "cannot mutate a finished execution",
                workspace=self._workspace,
                path=manifest_path,
            )
        events_path = execution_root / "events.jsonl"
        if _path_exists(_seal_path(events_path)):
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "cannot mutate a sealed terminal execution",
                workspace=self._workspace,
                path=events_path,
            )

    def _load_events(self, execution_id: str) -> tuple[dict[str, _Any], ...]:
        path = self._execution_root(execution_id) / "events.jsonl"
        if not _path_exists(path):
            if _path_exists(_seal_path(path)):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "event seal exists without its event log",
                    workspace=self._workspace,
                    path=path,
                )
            return ()
        if path.is_symlink() or not path.is_file():
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                "event log is not a regular contained file",
                workspace=self._workspace,
                path=path,
            )
        _ensure_contained(self._workspace, path)
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                f"event log cannot be read: {error}",
                workspace=self._workspace,
                path=path,
            ) from error
        if _path_exists(_seal_path(path)):
            sealed = _read_sealed_bytes(self._workspace, path, media_type=_EVENT_MEDIA_TYPE)
            if sealed != payload:
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "sealed event log changed after terminal publication",
                    workspace=self._workspace,
                    path=path,
                )
        if not payload:
            return ()
        lines = payload.splitlines(keepends=True)
        events: list[dict[str, _Any]] = []
        for expected_sequence, line in enumerate(lines, start=1):
            if not line.endswith(b"\n"):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "event log has a partial final line",
                    workspace=self._workspace,
                    path=path,
                )
            try:
                event = _json.loads(line)
            except (UnicodeDecodeError, _json.JSONDecodeError) as error:
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    f"event log contains invalid JSON: {error}",
                    workspace=self._workspace,
                    path=path,
                ) from error
            if (
                not isinstance(event, dict)
                or line != _canonical_json_bytes(event) + b"\n"
                or event.get("schema_version") != "tsdaf.local-event.v1"
                or event.get("record_kind") != "execution_event"
                or event.get("execution_id") != execution_id
                or event.get("sequence") != expected_sequence
            ):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "event log identity, sequence, or canonical bytes are invalid",
                    workspace=self._workspace,
                    path=path,
                )
            events.append(event)
        return tuple(events)

    def _append_event_unlocked(
        self,
        execution_id: str,
        event_type: str,
        data: dict[str, object],
        *,
        attempt_id: str | None,
        stage_name: str | None,
        content_key: str | None,
    ) -> dict[str, _Any]:
        execution_root = self._execution_root(execution_id)
        events_path = execution_root / "events.jsonl"
        self._ensure_execution_mutable_unlocked(execution_id)
        self._ensure_execution_unlocked(execution_id)
        existing = self._load_events(execution_id)
        event: dict[str, _Any] = {
            "schema_version": "tsdaf.local-event.v1",
            "record_kind": "execution_event",
            "sequence": len(existing) + 1,
            "execution_id": execution_id,
            "event_type": event_type,
            "data": data,
            "attempt_id": attempt_id,
            "stage_name": stage_name,
            "content_key": content_key,
            "occurred_at": _utc_json(_utc_now()),
        }
        line = _canonical_json_bytes(event) + b"\n"
        try:
            events_path.parent.mkdir(parents=True, exist_ok=True)
            _ensure_contained(self._workspace, events_path)
            with events_path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                _os.fsync(handle.fileno())
        except OSError as error:
            raise _runtime_error(
                _FailureCode.ARTIFACT_WRITE_FAILED,
                f"event append failed: {error}",
                workspace=self._workspace,
                path=events_path,
            ) from error
        return event

    def _start_attempt(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        stage_name: str,
        stage_version: str,
        content_key: str,
        input_artifacts: tuple[_ArtifactRef, ...],
        settings: dict[str, object],
        prompt_sha256: str | None,
        schema_sha256: str | None,
        model_profile: str | None,
    ) -> None:
        with _native_lock(self._workspace, self._execution_lock_path(execution_id)):
            self._ensure_execution_mutable_unlocked(execution_id)
            self._ensure_execution_unlocked(execution_id)
            path = self._attempt_root(execution_id, attempt_id) / "started.json"
            record = {
                "schema_version": "tsdaf.local-attempt-start.v1",
                "record_kind": "attempt_start",
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "stage_name": stage_name,
                "stage_version": stage_version,
                "content_key": content_key,
                "input_artifacts": [_ref_json(ref) for ref in input_artifacts],
                "settings": settings,
                "prompt_sha256": prompt_sha256,
                "schema_sha256": schema_sha256,
                "model_profile": model_profile,
                "started_at": _utc_json(_utc_now()),
            }
            _write_sealed_json(self._workspace, path, record)
            self._append_event_unlocked(
                execution_id,
                "attempt_started",
                {},
                attempt_id=attempt_id,
                stage_name=stage_name,
                content_key=content_key,
            )

    def _store_result(self, result: _StageResult) -> _ArtifactRef:
        return self._object_store.put_json(
            result.model_dump(mode="json"),
            media_type=_RESULT_MEDIA_TYPE,
        )

    def _record_attempt_result(
        self,
        *,
        result: _StageResult,
        result_ref: _ArtifactRef,
    ) -> None:
        if result.attempt_id is None:
            raise ValueError("attempt result requires an attempt_id")
        with _native_lock(self._workspace, self._execution_lock_path(result.execution_id)):
            self._ensure_execution_mutable_unlocked(result.execution_id)
            path = self._attempt_root(result.execution_id, result.attempt_id) / "result-ref.json"
            record = {
                "schema_version": "tsdaf.local-attempt-result-ref.v1",
                "record_kind": "attempt_result_ref",
                "execution_id": result.execution_id,
                "attempt_id": result.attempt_id,
                "stage_name": result.stage_name,
                "stage_version": result.stage_version,
                "content_key": result.input_content_key,
                "status": result.status.value,
                "result_ref": _ref_json(result_ref),
            }
            _write_sealed_json(self._workspace, path, record)
            self._append_event_unlocked(
                result.execution_id,
                "attempt_completed",
                {"status": result.status.value, "result_sha256": result_ref.sha256},
                attempt_id=result.attempt_id,
                stage_name=result.stage_name,
                content_key=result.input_content_key,
            )

    def _record_completed_stage(
        self,
        *,
        result: _StageResult,
        result_ref: _ArtifactRef,
    ) -> None:
        with _native_lock(self._workspace, self._execution_lock_path(result.execution_id)):
            self._ensure_execution_mutable_unlocked(result.execution_id)
            path = self._stage_record_path(result.execution_id, result.input_content_key)
            record = {
                "schema_version": "tsdaf.local-stage-result-ref.v1",
                "record_kind": "completed_stage_result_ref",
                "execution_id": result.execution_id,
                "stage_name": result.stage_name,
                "stage_version": result.stage_version,
                "content_key": result.input_content_key,
                "status": result.status.value,
                "result_ref": _ref_json(result_ref),
            }
            _write_sealed_json(self._workspace, path, record)
            self._append_event_unlocked(
                result.execution_id,
                "stage_completed",
                {"status": result.status.value, "result_sha256": result_ref.sha256},
                attempt_id=result.attempt_id,
                stage_name=result.stage_name,
                content_key=result.input_content_key,
            )

    def _publish_cache(self, result: _StageResult, result_ref: _ArtifactRef) -> None:
        if result.status is not _ExecutionStatus.SUCCEEDED or result.output_artifact is None:
            raise ValueError("only a successful stage result can be cached")
        path = self._cache_path(result.stage_name, result.input_content_key)
        record = {
            "schema_version": "tsdaf.local-stage-cache.v1",
            "record_kind": "successful_stage_cache",
            "stage_name": result.stage_name,
            "stage_version": result.stage_version,
            "content_key": result.input_content_key,
            "producer_execution_id": result.execution_id,
            "result_ref": _ref_json(result_ref),
            "output_artifact": _ref_json(result.output_artifact),
        }
        _write_sealed_json(self._workspace, path, record)
        loaded = self._load_cache(result.stage_name, result.stage_version, result.input_content_key)
        if loaded is None or loaded != (result, result_ref):
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "published stage cache did not verify",
                workspace=self._workspace,
                path=path,
            )

    def _reuse_cache(
        self,
        *,
        execution_id: str,
        stage_name: str,
        stage_version: str,
        content_key: str,
        producer: _StageResult,
        producer_ref: _ArtifactRef,
    ) -> _StageResult:
        with _native_lock(self._workspace, self._execution_lock_path(execution_id)):
            existing = self._load_local_stage(
                execution_id,
                stage_name,
                stage_version,
                content_key,
            )
            if existing is not None:
                return existing
            self._ensure_execution_mutable_unlocked(execution_id)
            self._ensure_execution_unlocked(execution_id)
            started_at = _utc_now()
            cached = _StageResult(
                schema_version="tsdaf.stage-result.v1",
                execution_id=execution_id,
                attempt_id=None,
                stage_name=stage_name,
                stage_version=stage_version,
                input_content_key=content_key,
                status=_ExecutionStatus.CACHED,
                output_artifact=producer.output_artifact,
                failure=None,
                cached_from_result=producer_ref,
                started_at=started_at,
                finished_at=_utc_now(),
            )
            cached_ref = self._store_result(cached)
            path = self._stage_record_path(execution_id, content_key)
            record = {
                "schema_version": "tsdaf.local-stage-result-ref.v1",
                "record_kind": "completed_stage_result_ref",
                "execution_id": execution_id,
                "stage_name": stage_name,
                "stage_version": stage_version,
                "content_key": content_key,
                "status": cached.status.value,
                "result_ref": _ref_json(cached_ref),
            }
            _write_sealed_json(self._workspace, path, record)
            self._append_event_unlocked(
                execution_id,
                "stage_cached",
                {
                    "producer_execution_id": producer.execution_id,
                    "producer_result_sha256": producer_ref.sha256,
                },
                attempt_id=None,
                stage_name=stage_name,
                content_key=content_key,
            )
        self.rebuild_index()
        return cached

    def _validate_invoked_result(
        self,
        result: object,
        *,
        execution_id: str,
        attempt_id: str,
        stage_name: str,
        stage_version: str,
        content_key: str,
    ) -> _StageResult:
        if not isinstance(result, _StageResult):
            raise TypeError("invoke must return a StageResult")
        if (
            result.execution_id != execution_id
            or result.attempt_id != attempt_id
            or result.stage_name != stage_name
            or result.stage_version != stage_version
            or result.input_content_key != content_key
            or result.status
            not in {
                _ExecutionStatus.SUCCEEDED,
                _ExecutionStatus.REVIEW_REQUIRED,
                _ExecutionStatus.FAILED_RETRYABLE,
                _ExecutionStatus.FAILED_TERMINAL,
            }
        ):
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "invoke returned a StageResult with divergent invocation identity or status",
            )
        if result.output_artifact is not None:
            self._object_store.verify(result.output_artifact)
        return result

    def _record_abort(
        self,
        *,
        execution_id: str,
        attempt_id: str,
        stage_name: str,
        content_key: str,
        error: Exception,
    ) -> None:
        with _native_lock(self._workspace, self._execution_lock_path(execution_id)):
            self._ensure_execution_mutable_unlocked(execution_id)
            result_path = self._attempt_root(execution_id, attempt_id) / "result-ref.json"
            if _path_exists(result_path) or _path_exists(_seal_path(result_path)):
                return
            self._append_event_unlocked(
                execution_id,
                "attempt_aborted",
                {"error_type": type(error).__name__},
                attempt_id=attempt_id,
                stage_name=stage_name,
                content_key=content_key,
            )

    def run_stage(
        self,
        *,
        execution_id: str,
        stage_name: str,
        stage_version: str,
        input_artifacts: _Sequence[_ArtifactRef],
        settings: _Mapping[str, object],
        invoke: _Callable[[str, str], _StageResult],
        prompt_sha256: str | None = None,
        schema_sha256: str | None = None,
        model_profile: str | None = None,
        requires_live: bool = False,
        live: bool = False,
    ) -> _StageResult | _TypedFailure:
        if not callable(invoke):
            raise TypeError("invoke must be callable")
        (
            checked_execution,
            checked_stage,
            checked_version,
            checked_inputs,
            checked_settings,
            content_key,
        ) = self._validate_request(
            execution_id=execution_id,
            stage_name=stage_name,
            stage_version=stage_version,
            input_artifacts=input_artifacts,
            settings=settings,
            prompt_sha256=prompt_sha256,
            schema_sha256=schema_sha256,
            model_profile=model_profile,
            requires_live=requires_live,
            live=live,
        )
        local = self._load_local_stage(
            checked_execution,
            checked_stage,
            checked_version,
            content_key,
        )
        if local is not None:
            return local
        cache = self._load_cache(checked_stage, checked_version, content_key)
        if cache is not None:
            return self._reuse_cache(
                execution_id=checked_execution,
                stage_name=checked_stage,
                stage_version=checked_version,
                content_key=content_key,
                producer=cache[0],
                producer_ref=cache[1],
            )
        if requires_live and not live:
            return _TypedFailure(
                schema_version="tsdaf.typed-failure.v1",
                code=_FailureCode.LIVE_REQUIRED,
                message="This uncached stage requires an explicitly enabled live invocation.",
                request_id=None,
                details=(),
            )

        with _native_lock(self._workspace, self._content_lock_path(content_key)):
            local = self._load_local_stage(
                checked_execution,
                checked_stage,
                checked_version,
                content_key,
            )
            if local is not None:
                return local
            cache = self._load_cache(checked_stage, checked_version, content_key)
            if cache is not None:
                return self._reuse_cache(
                    execution_id=checked_execution,
                    stage_name=checked_stage,
                    stage_version=checked_version,
                    content_key=content_key,
                    producer=cache[0],
                    producer_ref=cache[1],
                )
            if requires_live and not live:
                return _TypedFailure(
                    schema_version="tsdaf.typed-failure.v1",
                    code=_FailureCode.LIVE_REQUIRED,
                    message="This uncached stage requires an explicitly enabled live invocation.",
                    request_id=None,
                    details=(),
                )

            attempt_id = f"attempt_{_uuid.uuid4().hex}"
            self._start_attempt(
                execution_id=checked_execution,
                attempt_id=attempt_id,
                stage_name=checked_stage,
                stage_version=checked_version,
                content_key=content_key,
                input_artifacts=checked_inputs,
                settings=checked_settings,
                prompt_sha256=prompt_sha256,
                schema_sha256=schema_sha256,
                model_profile=model_profile,
            )
            try:
                invoked = invoke(attempt_id, content_key)
                result = self._validate_invoked_result(
                    invoked,
                    execution_id=checked_execution,
                    attempt_id=attempt_id,
                    stage_name=checked_stage,
                    stage_version=checked_version,
                    content_key=content_key,
                )
                result_ref = self._store_result(result)
                self._record_attempt_result(result=result, result_ref=result_ref)
            except Exception as error:
                self._record_abort(
                    execution_id=checked_execution,
                    attempt_id=attempt_id,
                    stage_name=checked_stage,
                    content_key=content_key,
                    error=error,
                )
                self.rebuild_index()
                raise

            if result.status is _ExecutionStatus.FAILED_RETRYABLE:
                self.rebuild_index()
                return result

            self._record_completed_stage(result=result, result_ref=result_ref)
            if result.status is _ExecutionStatus.SUCCEEDED:
                self._publish_cache(result, result_ref)
            self.rebuild_index()
            if result.status in {
                _ExecutionStatus.REVIEW_REQUIRED,
                _ExecutionStatus.FAILED_TERMINAL,
            }:
                self.finish_execution(checked_execution, result.status)
            return result

    def append_event(
        self,
        execution_id: str,
        event_type: str,
        data: _Mapping[str, object],
        *,
        attempt_id: str | None = None,
        stage_name: str | None = None,
        content_key: str | None = None,
    ) -> None:
        checked_execution = _validate_safe_component(execution_id, label="execution id")
        checked_event = _validate_safe_component(event_type, label="event type")
        if not isinstance(data, _Mapping):
            raise TypeError("event data must be a mapping")
        checked_data = _canonical_mapping(data, label="event data")
        checked_attempt = (
            None if attempt_id is None else _validate_safe_component(attempt_id, label="attempt id")
        )
        checked_stage = (
            None if stage_name is None else _validate_safe_component(stage_name, label="stage name")
        )
        checked_key = (
            None if content_key is None else _validate_sha256(content_key, label="content key")
        )
        with _native_lock(self._workspace, self._execution_lock_path(checked_execution)):
            execution_path = self._execution_root(checked_execution) / "execution.json"
            if not _path_exists(execution_path) and not _path_exists(_seal_path(execution_path)):
                raise _runtime_error(
                    _FailureCode.ASSET_MISSING,
                    "cannot append an event for an unknown execution",
                    workspace=self._workspace,
                    path=execution_path,
                )
            self._ensure_execution_unlocked(checked_execution)
            self._append_event_unlocked(
                checked_execution,
                checked_event,
                checked_data,
                attempt_id=checked_attempt,
                stage_name=checked_stage,
                content_key=checked_key,
            )
        self.rebuild_index()

    def _record_descriptor(self, path: _Path) -> dict[str, object]:
        payload = _read_sealed_bytes(self._workspace, path, media_type="application/json")
        return {
            "record_relative_path": path.relative_to(self._workspace).as_posix(),
            "record_sha256": _hashlib.sha256(payload).hexdigest(),
            "record_byte_size": len(payload),
            "record_media_type": "application/json",
        }

    def _manifest_bindings(
        self,
        execution_id: str,
        events: tuple[dict[str, _Any], ...],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        attempt_ids: list[str] = []
        stage_keys: list[str] = []
        for event in events:
            attempt_id = event.get("attempt_id")
            content_key = event.get("content_key")
            if (
                event.get("event_type") == "attempt_started"
                and isinstance(attempt_id, str)
                and attempt_id not in attempt_ids
            ):
                attempt_ids.append(attempt_id)
            if (
                event.get("event_type") in {"stage_completed", "stage_cached"}
                and isinstance(content_key, str)
                and content_key not in stage_keys
            ):
                stage_keys.append(content_key)

        attempts_root = self._execution_root(execution_id) / "attempts"
        if attempts_root.exists():
            for path in sorted(attempts_root.iterdir(), key=lambda item: item.name):
                if path.is_dir() and path.name not in attempt_ids:
                    attempt_ids.append(path.name)
        stages_root = self._execution_root(execution_id) / "stages"
        if stages_root.exists():
            for path in sorted(stages_root.iterdir(), key=lambda item: item.name):
                if path.is_dir() and path.name not in stage_keys:
                    stage_keys.append(path.name)

        attempt_records: list[dict[str, object]] = []
        for attempt_id in attempt_ids:
            started_path = self._attempt_root(execution_id, attempt_id) / "started.json"
            started = _read_sealed_json(self._workspace, started_path)
            _record_header(
                started,
                schema_version="tsdaf.local-attempt-start.v1",
                record_kind="attempt_start",
                workspace=self._workspace,
                path=started_path,
            )
            if (
                started.get("execution_id") != execution_id
                or started.get("attempt_id") != attempt_id
            ):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "attempt start record identity is divergent",
                    workspace=self._workspace,
                    path=started_path,
                )
            result_path = self._attempt_root(execution_id, attempt_id) / "result-ref.json"
            binding: dict[str, object] = {
                "attempt_id": attempt_id,
                "started_record": self._record_descriptor(started_path),
                "result_record": None,
                "result_ref": None,
            }
            if _path_exists(result_path) or _path_exists(_seal_path(result_path)):
                result, result_ref, record = self._load_result_record(
                    result_path,
                    schema_version="tsdaf.local-attempt-result-ref.v1",
                    record_kind="attempt_result_ref",
                )
                if (
                    record.get("execution_id") != execution_id
                    or record.get("attempt_id") != attempt_id
                    or result.execution_id != execution_id
                    or result.attempt_id != attempt_id
                ):
                    raise _runtime_error(
                        _FailureCode.HASH_MISMATCH,
                        "attempt result record identity is divergent",
                        workspace=self._workspace,
                        path=result_path,
                    )
                binding["result_record"] = self._record_descriptor(result_path)
                binding["result_ref"] = _ref_json(result_ref)
            attempt_records.append(binding)

        stage_records: list[dict[str, object]] = []
        for content_key in stage_keys:
            path = self._stage_record_path(execution_id, content_key)
            result, result_ref, record = self._load_result_record(
                path,
                schema_version="tsdaf.local-stage-result-ref.v1",
                record_kind="completed_stage_result_ref",
            )
            if (
                record.get("execution_id") != execution_id
                or record.get("content_key") != content_key
                or result.execution_id != execution_id
                or result.input_content_key != content_key
            ):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "completed stage record identity is divergent",
                    workspace=self._workspace,
                    path=path,
                )
            stage_records.append(
                {
                    "content_key": content_key,
                    "stage_name": result.stage_name,
                    "stage_version": result.stage_version,
                    "status": result.status.value,
                    "record": self._record_descriptor(path),
                    "result_ref": _ref_json(result_ref),
                }
            )
        return attempt_records, stage_records

    def _manifest_artifact_ref(self, manifest_path: _Path) -> _ArtifactRef:
        payload = _read_sealed_bytes(
            self._workspace,
            manifest_path,
            media_type="application/json",
        )
        digest = _hashlib.sha256(payload).hexdigest()
        ref = _ArtifactRef(
            sha256=digest,
            media_type=_MANIFEST_MEDIA_TYPE,
            byte_size=len(payload),
            relative_path=f"objects/sha256/{digest[:2]}/{digest}",
        )
        self._object_store.verify(ref)
        return ref

    def _verify_manifest(
        self,
        execution_id: str,
        manifest_path: _Path,
    ) -> tuple[dict[str, _Any], _ArtifactRef]:
        manifest = _read_sealed_json(self._workspace, manifest_path)
        _record_header(
            manifest,
            schema_version="tsdaf.local-execution-manifest.v1",
            record_kind="terminal_execution_manifest",
            workspace=self._workspace,
            path=manifest_path,
        )
        if manifest.get("execution_id") != execution_id:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "terminal manifest execution identity is divergent",
                workspace=self._workspace,
                path=manifest_path,
            )
        status_value = manifest.get("status")
        if status_value not in {
            _ExecutionStatus.SUCCEEDED.value,
            _ExecutionStatus.REVIEW_REQUIRED.value,
            _ExecutionStatus.FAILED_TERMINAL.value,
        }:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "terminal manifest has an invalid status",
                workspace=self._workspace,
                path=manifest_path,
            )
        event_ref = _ref_from_json(manifest.get("event_log"), label="manifest event_log")
        events_path = self._execution_root(execution_id) / "events.jsonl"
        events_payload = _read_sealed_bytes(
            self._workspace,
            events_path,
            media_type=_EVENT_MEDIA_TYPE,
        )
        if self._object_store.read_bytes(event_ref) != events_payload:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "terminal manifest does not bind the exact final event log",
                workspace=self._workspace,
                path=manifest_path,
            )
        events = self._load_events(execution_id)
        if (
            not events
            or events[-1].get("event_type") != "execution_finished"
            or events[-1].get("data") != {"status": status_value}
        ):
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "terminal event does not match the manifest status",
                workspace=self._workspace,
                path=events_path,
            )
        attempts, stages = self._manifest_bindings(execution_id, events)
        if manifest.get("attempt_records") != attempts or manifest.get("stage_records") != stages:
            raise _runtime_error(
                _FailureCode.HASH_MISMATCH,
                "terminal manifest record bindings are divergent",
                workspace=self._workspace,
                path=manifest_path,
            )
        return manifest, self._manifest_artifact_ref(manifest_path)

    def finish_execution(
        self,
        execution_id: str,
        status: _ExecutionStatus = _ExecutionStatus.SUCCEEDED,
    ) -> _ArtifactRef:
        checked_execution = _validate_safe_component(execution_id, label="execution id")
        if not isinstance(status, _ExecutionStatus) or status not in {
            _ExecutionStatus.SUCCEEDED,
            _ExecutionStatus.REVIEW_REQUIRED,
            _ExecutionStatus.FAILED_TERMINAL,
        }:
            raise ValueError("finish status must be SUCCEEDED, REVIEW_REQUIRED, or FAILED_TERMINAL")
        manifest_path = self._execution_root(checked_execution) / "manifest.json"
        with _native_lock(self._workspace, self._execution_lock_path(checked_execution)):
            if _path_exists(manifest_path) or _path_exists(_seal_path(manifest_path)):
                manifest, manifest_ref = self._verify_manifest(checked_execution, manifest_path)
                if manifest.get("status") != status.value:
                    raise _runtime_error(
                        _FailureCode.HASH_MISMATCH,
                        "execution was already finished with a different status",
                        workspace=self._workspace,
                        path=manifest_path,
                    )
                return manifest_ref

            execution_path = self._execution_root(checked_execution) / "execution.json"
            if not _path_exists(execution_path) and not _path_exists(_seal_path(execution_path)):
                raise _runtime_error(
                    _FailureCode.ASSET_MISSING,
                    "cannot finish an unknown execution",
                    workspace=self._workspace,
                    path=execution_path,
                )
            execution_record = self._ensure_execution_unlocked(checked_execution)
            events_path = self._execution_root(checked_execution) / "events.jsonl"
            if _path_exists(_seal_path(events_path)):
                events = self._load_events(checked_execution)
                if (
                    not events
                    or events[-1].get("event_type") != "execution_finished"
                    or events[-1].get("data") != {"status": status.value}
                ):
                    raise _runtime_error(
                        _FailureCode.HASH_MISMATCH,
                        "sealed terminal event contradicts the requested finish status",
                        workspace=self._workspace,
                        path=events_path,
                    )
            else:
                self._append_event_unlocked(
                    checked_execution,
                    "execution_finished",
                    {"status": status.value},
                    attempt_id=None,
                    stage_name=None,
                    content_key=None,
                )
                events = self._load_events(checked_execution)
            events_payload = events_path.read_bytes()
            event_ref = self._object_store.put_bytes(
                events_payload,
                media_type=_EVENT_MEDIA_TYPE,
            )
            _seal_existing_bytes(
                self._workspace,
                events_path,
                events_payload,
                media_type=_EVENT_MEDIA_TYPE,
            )
            attempt_records, stage_records = self._manifest_bindings(
                checked_execution,
                events,
            )
            finished_at = events[-1].get("occurred_at")
            if not isinstance(finished_at, str):
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "terminal event is missing its timestamp",
                    workspace=self._workspace,
                    path=events_path,
                )
            manifest = {
                "schema_version": "tsdaf.local-execution-manifest.v1",
                "record_kind": "terminal_execution_manifest",
                "execution_id": checked_execution,
                "status": status.value,
                "started_at": execution_record["created_at"],
                "finished_at": finished_at,
                "event_log": _ref_json(event_ref),
                "attempt_records": attempt_records,
                "stage_records": stage_records,
            }
            _write_sealed_json(self._workspace, manifest_path, manifest)
            manifest_ref = self._object_store.put_json(
                manifest,
                media_type=_MANIFEST_MEDIA_TYPE,
            )
            verified_manifest, verified_ref = self._verify_manifest(
                checked_execution,
                manifest_path,
            )
            if verified_manifest != manifest or verified_ref != manifest_ref:
                raise _runtime_error(
                    _FailureCode.HASH_MISMATCH,
                    "terminal manifest failed post-publication verification",
                    workspace=self._workspace,
                    path=manifest_path,
                )
        self.rebuild_index()
        return manifest_ref

    def _scan_projection(
        self,
    ) -> tuple[
        list[tuple[_Any, ...]],
        list[tuple[_Any, ...]],
        list[tuple[_Any, ...]],
        list[tuple[_Any, ...]],
        list[tuple[_Any, ...]],
    ]:
        execution_rows: list[tuple[_Any, ...]] = []
        event_rows: list[tuple[_Any, ...]] = []
        attempt_rows: list[tuple[_Any, ...]] = []
        output_rows: list[tuple[_Any, ...]] = []
        artifacts: dict[tuple[str, str], tuple[_Any, ...]] = {}

        def add_artifact(ref: _ArtifactRef) -> None:
            self._object_store.verify(ref)
            key = (ref.sha256, ref.media_type)
            row = (ref.sha256, ref.media_type, ref.byte_size, ref.relative_path)
            existing = artifacts.get(key)
            if existing is not None and existing != row:
                raise LocalRuntimeError(
                    _FailureCode.HASH_MISMATCH,
                    "one artifact identity has divergent projection metadata",
                    ref.relative_path,
                )
            artifacts[key] = row

        executions_root = self._workspace / "executions"
        if not executions_root.exists():
            return execution_rows, event_rows, attempt_rows, output_rows, []
        if executions_root.is_symlink() or not executions_root.is_dir():
            raise _runtime_error(
                _FailureCode.ASSET_MISSING,
                "executions root is not a regular directory",
                workspace=self._workspace,
                path=executions_root,
            )

        for execution_root in sorted(executions_root.iterdir(), key=lambda item: item.name):
            if not execution_root.is_dir() or execution_root.is_symlink():
                continue
            execution_id = _validate_safe_component(execution_root.name, label="execution id")
            with _native_lock(self._workspace, self._execution_lock_path(execution_id)):
                execution_path = execution_root / "execution.json"
                execution = _read_sealed_json(self._workspace, execution_path)
                _record_header(
                    execution,
                    schema_version="tsdaf.local-execution.v1",
                    record_kind="execution",
                    workspace=self._workspace,
                    path=execution_path,
                )
                if execution.get("execution_id") != execution_id:
                    raise _runtime_error(
                        _FailureCode.HASH_MISMATCH,
                        "execution directory and record identities differ",
                        workspace=self._workspace,
                        path=execution_path,
                    )
                created_at = execution.get("created_at")
                if not isinstance(created_at, str):
                    raise _runtime_error(
                        _FailureCode.HASH_MISMATCH,
                        "execution record has no canonical creation timestamp",
                        workspace=self._workspace,
                        path=execution_path,
                    )
                events = self._load_events(execution_id)
                manifest_path = execution_root / "manifest.json"
                manifest: dict[str, _Any] | None = None
                manifest_ref: _ArtifactRef | None = None
                if _path_exists(manifest_path) or _path_exists(_seal_path(manifest_path)):
                    manifest, manifest_ref = self._verify_manifest(execution_id, manifest_path)
                    add_artifact(manifest_ref)
                    event_ref = _ref_from_json(
                        manifest.get("event_log"),
                        label="manifest event_log",
                    )
                    add_artifact(event_ref)
                execution_rows.append(
                    (
                        execution_id,
                        manifest["status"] if manifest is not None else "RUNNING",
                        created_at,
                        manifest["finished_at"] if manifest is not None else None,
                        manifest_ref.sha256 if manifest_ref is not None else None,
                    )
                )

                aborted_events: dict[str, dict[str, _Any]] = {}
                for event in events:
                    data = event.get("data")
                    if not isinstance(data, dict):
                        raise _runtime_error(
                            _FailureCode.HASH_MISMATCH,
                            "event data is not a JSON object",
                            workspace=self._workspace,
                            path=execution_root / "events.jsonl",
                        )
                    event_rows.append(
                        (
                            execution_id,
                            event["sequence"],
                            event["event_type"],
                            _canonical_json_bytes(data).decode("utf-8"),
                            event.get("attempt_id"),
                            event.get("stage_name"),
                            event.get("content_key"),
                            event["occurred_at"],
                        )
                    )
                    if event.get("event_type") == "attempt_aborted" and isinstance(
                        event.get("attempt_id"), str
                    ):
                        aborted_events[event["attempt_id"]] = event

                attempts_root = execution_root / "attempts"
                if attempts_root.exists():
                    for attempt_root in sorted(attempts_root.iterdir(), key=lambda item: item.name):
                        if not attempt_root.is_dir() or attempt_root.is_symlink():
                            continue
                        attempt_id = _validate_safe_component(
                            attempt_root.name,
                            label="attempt id",
                        )
                        started_path = attempt_root / "started.json"
                        started = _read_sealed_json(self._workspace, started_path)
                        _record_header(
                            started,
                            schema_version="tsdaf.local-attempt-start.v1",
                            record_kind="attempt_start",
                            workspace=self._workspace,
                            path=started_path,
                        )
                        if (
                            started.get("execution_id") != execution_id
                            or started.get("attempt_id") != attempt_id
                        ):
                            raise _runtime_error(
                                _FailureCode.HASH_MISMATCH,
                                "attempt start projection identity is divergent",
                                workspace=self._workspace,
                                path=started_path,
                            )
                        input_values = started.get("input_artifacts")
                        if not isinstance(input_values, list):
                            raise _runtime_error(
                                _FailureCode.HASH_MISMATCH,
                                "attempt start inputs are invalid",
                                workspace=self._workspace,
                                path=started_path,
                            )
                        for value in input_values:
                            add_artifact(_ref_from_json(value, label="attempt input artifact"))

                        result_path = attempt_root / "result-ref.json"
                        result: _StageResult | None = None
                        result_ref: _ArtifactRef | None = None
                        if _path_exists(result_path) or _path_exists(_seal_path(result_path)):
                            result, result_ref, record = self._load_result_record(
                                result_path,
                                schema_version="tsdaf.local-attempt-result-ref.v1",
                                record_kind="attempt_result_ref",
                            )
                            if (
                                record.get("execution_id") != execution_id
                                or record.get("attempt_id") != attempt_id
                                or result.execution_id != execution_id
                                or result.attempt_id != attempt_id
                            ):
                                raise _runtime_error(
                                    _FailureCode.HASH_MISMATCH,
                                    "attempt result projection identity is divergent",
                                    workspace=self._workspace,
                                    path=result_path,
                                )
                            add_artifact(result_ref)
                            if result.output_artifact is not None:
                                add_artifact(result.output_artifact)
                            if result.cached_from_result is not None:
                                add_artifact(result.cached_from_result)

                        aborted = aborted_events.get(attempt_id)
                        attempt_rows.append(
                            (
                                execution_id,
                                attempt_id,
                                started["stage_name"],
                                started["stage_version"],
                                started["content_key"],
                                (
                                    result.status.value
                                    if result is not None
                                    else "ABORTED"
                                    if aborted is not None
                                    else "RUNNING"
                                ),
                                result_ref.sha256 if result_ref is not None else None,
                                started["started_at"],
                                (
                                    _utc_json(result.finished_at)
                                    if result is not None
                                    else aborted.get("occurred_at")
                                    if aborted is not None
                                    else None
                                ),
                            )
                        )

                stages_root = execution_root / "stages"
                if stages_root.exists():
                    for stage_root in sorted(stages_root.iterdir(), key=lambda item: item.name):
                        if not stage_root.is_dir() or stage_root.is_symlink():
                            continue
                        content_key = _validate_sha256(
                            stage_root.name,
                            label="stage content key",
                        )
                        result_path = stage_root / "result-ref.json"
                        result, result_ref, record = self._load_result_record(
                            result_path,
                            schema_version="tsdaf.local-stage-result-ref.v1",
                            record_kind="completed_stage_result_ref",
                        )
                        if (
                            record.get("execution_id") != execution_id
                            or record.get("content_key") != content_key
                            or result.execution_id != execution_id
                            or result.input_content_key != content_key
                        ):
                            raise _runtime_error(
                                _FailureCode.HASH_MISMATCH,
                                "stage output projection identity is divergent",
                                workspace=self._workspace,
                                path=result_path,
                            )
                        add_artifact(result_ref)
                        if result.output_artifact is not None:
                            add_artifact(result.output_artifact)
                        if result.cached_from_result is not None:
                            add_artifact(result.cached_from_result)
                        output_rows.append(
                            (
                                execution_id,
                                content_key,
                                result.stage_name,
                                result.stage_version,
                                result.status.value,
                                result_ref.sha256,
                                (
                                    result.output_artifact.sha256
                                    if result.output_artifact is not None
                                    else None
                                ),
                                (
                                    result.cached_from_result.sha256
                                    if result.cached_from_result is not None
                                    else None
                                ),
                            )
                        )

        return (
            execution_rows,
            event_rows,
            attempt_rows,
            output_rows,
            list(artifacts.values()),
        )

    def rebuild_index(self) -> _Path:
        database_path = self._workspace / "index.sqlite"
        with _native_lock(self._workspace, self._index_lock_path()):
            (
                execution_rows,
                event_rows,
                attempt_rows,
                output_rows,
                artifact_rows,
            ) = self._scan_projection()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            _ensure_contained(self._workspace, database_path)
            connection: _sqlite3.Connection | None = None
            try:
                connection = _sqlite3.connect(database_path)
                connection.isolation_level = None
                connection.execute("PRAGMA foreign_keys = ON")
                journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                    raise _sqlite3.OperationalError("SQLite did not enable WAL journal mode")
                current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current_version not in {0, _INDEX_USER_VERSION}:
                    raise _sqlite3.OperationalError(
                        f"unsupported runtime index user_version {current_version}"
                    )
                connection.execute("BEGIN IMMEDIATE")
                for table in (
                    "execution_events",
                    "stage_attempts",
                    "stage_outputs",
                    "artifacts",
                    "executions",
                ):
                    connection.execute(f'DROP TABLE IF EXISTS "{table}"')
                connection.execute(
                    """
                    CREATE TABLE executions (
                        execution_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        finished_at TEXT,
                        manifest_sha256 TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE execution_events (
                        execution_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        event_type TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        attempt_id TEXT,
                        stage_name TEXT,
                        content_key TEXT,
                        occurred_at TEXT NOT NULL,
                        PRIMARY KEY (execution_id, sequence),
                        FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE stage_attempts (
                        execution_id TEXT NOT NULL,
                        attempt_id TEXT NOT NULL,
                        stage_name TEXT NOT NULL,
                        stage_version TEXT NOT NULL,
                        content_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_sha256 TEXT,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        PRIMARY KEY (execution_id, attempt_id),
                        FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE stage_outputs (
                        execution_id TEXT NOT NULL,
                        content_key TEXT NOT NULL,
                        stage_name TEXT NOT NULL,
                        stage_version TEXT NOT NULL,
                        status TEXT NOT NULL,
                        result_sha256 TEXT NOT NULL,
                        output_sha256 TEXT,
                        cached_from_result_sha256 TEXT,
                        PRIMARY KEY (execution_id, content_key),
                        FOREIGN KEY (execution_id) REFERENCES executions(execution_id)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE artifacts (
                        sha256 TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        byte_size INTEGER NOT NULL,
                        relative_path TEXT NOT NULL,
                        PRIMARY KEY (sha256, media_type)
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO executions VALUES (?, ?, ?, ?, ?)",
                    execution_rows,
                )
                connection.executemany(
                    "INSERT INTO execution_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    event_rows,
                )
                connection.executemany(
                    "INSERT INTO stage_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    attempt_rows,
                )
                connection.executemany(
                    "INSERT INTO stage_outputs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    output_rows,
                )
                connection.executemany(
                    "INSERT INTO artifacts VALUES (?, ?, ?, ?)",
                    artifact_rows,
                )
                connection.execute(f"PRAGMA user_version = {_INDEX_USER_VERSION}")
                connection.execute("COMMIT")
            except _sqlite3.Error as error:
                if connection is not None and connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise _runtime_error(
                    _FailureCode.ARTIFACT_WRITE_FAILED,
                    f"runtime index rebuild failed: {error}",
                    workspace=self._workspace,
                    path=database_path,
                ) from error
            finally:
                if connection is not None:
                    connection.close()
        return database_path
