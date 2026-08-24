from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


MODEL_NAME = "gemini-3.7-flash-high"


def _chat_url(value: str) -> str:
    base = value.strip().rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


@dataclass(frozen=True)
class HarnessConfig:
    api_base: str = field(repr=False)
    api_key: str = field(repr=False)
    model: str = field(default=MODEL_NAME, init=False)
    temperature: float = field(default=0, init=False)
    top_p: float = field(default=1, init=False)
    max_tokens: int = field(default=32768, init=False)
    timeout_seconds: int = field(default=900, init=False)
    stream: bool = field(default=True, init=False)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "HarnessConfig":
        try:
            base = values["GEMINI_API_BASE"]
            key = values["GEMINI_API_KEY"]
        except KeyError as exc:
            raise ValueError(f"Missing required configuration: {exc.args[0]}") from None
        if not base.strip() or not key.strip():
            raise ValueError("Gemini API base and key must be non-empty")
        return cls(api_base=base.strip().rstrip("/"), api_key=key.strip())

    @classmethod
    def from_file(cls, path: Path | str) -> "HarnessConfig":
        text = Path(path).read_text(encoding="utf-8")
        parsed: dict[str, str] = {}
        for line in text.splitlines():
            match = re.match(r"\s*(GEMINI_API_BASE|GEMINI_API_KEY)\s*=\s*(.*?)\s*$", line)
            if match:
                parsed[match.group(1)] = match.group(2).strip().strip("\"'")
        if len(parsed) == 2:
            return cls.from_mapping(parsed)

        endpoint = re.search(r"https://[^\s\"']+(?:/v1/chat/completions)?", text)
        bearer = re.search(r"Bearer\s+([^\"'\s\\]+)", text)
        if endpoint is None or bearer is None:
            raise ValueError("Could not parse Gemini gateway and bearer credential from the env file")
        return cls(api_base=endpoint.group(0).rstrip("/"), api_key=bearer.group(1))

    @property
    def chat_completions_url(self) -> str:
        return _chat_url(self.api_base)

    def public_metadata(self) -> dict[str, object]:
        return {
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "stream": self.stream,
            "transport": "openai-compatible-chat-completions",
        }
