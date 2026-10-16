"""Private validation primitives shared by persistent contracts."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer


class ContractModel(BaseModel):
    """Strict, immutable base for every persistent TS-DAF contract."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _stable_string(value: str, *, label: str) -> str:
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be a nonempty canonical string")
    return value


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256(value: str, *, label: str) -> str:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _formatted_id(value: str, pattern: re.Pattern[str], *, label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")
    return value


_T = TypeVar("_T")


def _unique(values: tuple[_T, ...], *, label: str) -> tuple[_T, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} contains duplicates")
    return values


def _strictly_increasing(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    _unique(values, label=label)
    if tuple(sorted(values)) != values:
        raise ValueError(f"{label} is not in canonical order")
    return values


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _serialize_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


UtcDateTime = Annotated[
    datetime,
    AfterValidator(_normalize_datetime),
    PlainSerializer(_serialize_datetime, return_type=str, when_used="json"),
]
