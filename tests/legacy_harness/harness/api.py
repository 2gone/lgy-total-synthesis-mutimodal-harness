from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .config import HarnessConfig
from .suites import Asset


@dataclass(frozen=True)
class DecodedSSE:
    content: str
    usage: dict[str, Any] | None


@dataclass(frozen=True)
class ApiResponse:
    raw: bytes
    status: int
    wire_format: str
    metadata: dict[str, Any]


def decode_sse(raw: bytes) -> DecodedSSE:
    fragments: list[str] = []
    usage: dict[str, Any] | None = None
    for line in raw.decode("utf-8").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        event = json.loads(data)
        if event.get("usage") is not None:
            usage = event["usage"]
        choices = event.get("choices") or []
        if choices:
            content = choices[0].get("delta", {}).get("content")
            if isinstance(content, str):
                fragments.append(content)
    return DecodedSSE(content="".join(fragments), usage=usage)


def build_chat_payload(
    settings: HarnessConfig,
    prompt: str,
    assets: Sequence[Asset],
    *,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if schema is not None:
        prompt = (
            prompt
            + "\nThe exact JSON Schema follows. Use these exact field names and no others.\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for asset in assets:
        path = asset.path
        if not path.is_file():
            raise FileNotFoundError(path)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        if asset.media_type == "application/pdf":
            content.append(
                {
                    "type": "file",
                    "file": {
                        "filename": path.name,
                        "file_data": "data:application/pdf;base64," + encoded,
                    },
                }
            )
        elif asset.media_type == "image/png":
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + encoded, "detail": "high"},
                }
            )
        elif asset.media_type == "application/json":
            content.append(
                {
                    "type": "text",
                    "text": (
                        "Persisted external OCR evidence follows. It is evidence, not authority.\n"
                        + path.read_text(encoding="utf-8")
                    ),
                }
            )
        else:
            raise ValueError(f"Unsupported asset type: {asset.media_type}")

    payload: dict[str, Any] = {
        "model": settings.model,
        "messages": [{"role": "user", "content": content}],
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "max_tokens": settings.max_tokens,
        "stream": settings.stream,
        "stream_options": {"include_usage": True},
    }
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "controlled_experiment_output", "strict": True, "schema": schema},
        }
    return payload


def extract_response_text(response: ApiResponse) -> str:
    if response.wire_format == "sse":
        return decode_sse(response.raw).content
    body = json.loads(response.raw.decode("utf-8"))
    content = body["choices"][0]["message"]["content"]
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item.get("text", "") for item in content if isinstance(item, dict))
    raise ValueError("Unsupported response content representation")


def post_chat_completion(settings: HarnessConfig, payload: dict[str, Any]) -> ApiResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        settings.chat_completions_url,
        data=body,
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as http_response:
            raw = http_response.read()
            status = http_response.status
            content_type = http_response.headers.get("Content-Type") or ""
            wire_format = "sse" if "text/event-stream" in content_type else "json"
            error = None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
        wire_format = "json"
        error = f"HTTPError {exc.code}"
    except Exception as exc:
        raw = json.dumps({"transport_error": type(exc).__name__}).encode("utf-8")
        status = 0
        wire_format = "json"
        error = type(exc).__name__
    ended = datetime.now(timezone.utc)
    return ApiResponse(
        raw=raw,
        status=status,
        wire_format=wire_format,
        metadata={
            "start_timestamp": started.isoformat(),
            "end_timestamp": ended.isoformat(),
            "wall_clock_seconds": round(time.perf_counter() - clock, 6),
            "http_status": status,
            "error": error,
            "request_bytes": len(body),
        },
    )
