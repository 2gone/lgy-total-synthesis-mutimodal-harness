"""One-crop Gemini OCSR request construction and strict result parsing."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.image.models import GeminiOCSRResult, MoleculeCrop, OCSRStatus

_PROMPT_FILES = ("manifest.toml", "system.md", "task.md")
_MODEL_PROFILE = "gemini-3.8-flash-high-molecule-ocsr-chat-completions-v1"
_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "readable": {"type": "boolean"},
        "smiles": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "is_abstract": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["readable", "smiles", "confidence", "is_abstract", "notes"],
    "additionalProperties": False,
}


class OCSRPromptError(ValueError):
    pass


class OCSRResponseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MoleculeOCSRPrompt:
    prompt_id: str
    version: str
    model_profile: str
    system_instruction: str
    task_template: str
    prompt_sha256: str
    schema_sha256: str


@dataclass(frozen=True, slots=True)
class OCSRRequest:
    molecule_id: str
    input: list[dict[str, object]]
    system_instruction: str
    response_format: dict[str, object]

    @property
    def image_count(self) -> int:
        count = 0
        for step in self.input:
            content = step.get("content")
            if not isinstance(content, list):
                continue
            count += sum(part.get("type") == "image" for part in content if isinstance(part, dict))
        return count


def _prompt_hash(texts: dict[str, str]) -> str:
    manifest = [
        {
            "name": name,
            "sha256": hashlib.sha256(texts[name].encode("utf-8")).hexdigest(),
        }
        for name in _PROMPT_FILES
    ]
    return hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()


def _load_prompt(prompt_dir: Path) -> MoleculeOCSRPrompt:
    if not isinstance(prompt_dir, Path):
        raise TypeError("prompt_dir must be a pathlib.Path")
    try:
        texts = {name: (prompt_dir / name).read_text(encoding="utf-8") for name in _PROMPT_FILES}
        manifest = tomllib.loads(texts["manifest.toml"])
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise OCSRPromptError("Molecule OCSR prompt bundle is invalid") from None
    expected_manifest = {
        "prompt_id": "molecule-ocsr",
        "version": "1.0.0",
        "schema_version": "tsdaf.gemini-ocsr-result.v1",
        "model_profile": _MODEL_PROFILE,
    }
    if manifest != expected_manifest:
        raise OCSRPromptError("Molecule OCSR prompt manifest is invalid")
    if not texts["system.md"].strip() or "{molecule_id}" not in texts["task.md"]:
        raise OCSRPromptError("Molecule OCSR prompt text is invalid")
    return MoleculeOCSRPrompt(
        prompt_id="molecule-ocsr",
        version="1.0.0",
        model_profile=_MODEL_PROFILE,
        system_instruction=texts["system.md"].strip(),
        task_template=texts["task.md"].strip(),
        prompt_sha256=_prompt_hash(texts),
        schema_sha256=hashlib.sha256(canonical_json_bytes(_RESPONSE_SCHEMA)).hexdigest(),
    )


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise OCSRResponseError("Gemini OCSR response does not contain JSON") from None
        try:
            payload = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            raise OCSRResponseError("Gemini OCSR response JSON is invalid") from None
    if not isinstance(payload, dict):
        raise OCSRResponseError("Gemini OCSR response must be a JSON object")
    return payload


class MoleculeOCSR:
    """Fixed prompt and response boundary; no provider transport lives here."""

    __slots__ = ("_prompt",)

    def __init__(self, prompt_dir: Path) -> None:
        self._prompt = _load_prompt(prompt_dir)

    @property
    def prompt(self) -> MoleculeOCSRPrompt:
        return self._prompt

    def build_request(self, *, molecule: MoleculeCrop, crop_bytes: bytes) -> OCSRRequest:
        if not isinstance(molecule, MoleculeCrop):
            raise TypeError("molecule must be a MoleculeCrop")
        if not isinstance(crop_bytes, bytes):
            raise TypeError("crop_bytes must be bytes")
        if (
            len(crop_bytes) != molecule.crop_artifact.byte_size
            or hashlib.sha256(crop_bytes).hexdigest() != molecule.crop_artifact.sha256
        ):
            raise ValueError("crop bytes do not match the molecule artifact")
        task = self._prompt.task_template.format(molecule_id=molecule.molecule_id)
        return OCSRRequest(
            molecule_id=molecule.molecule_id,
            input=[
                {
                    "type": "user_input",
                    "content": [
                        {"type": "text", "text": task},
                        {
                            "type": "image",
                            "mime_type": "image/png",
                            "data": base64.b64encode(crop_bytes).decode("ascii"),
                            "resolution": "high",
                        },
                    ],
                }
            ],
            system_instruction=self._prompt.system_instruction,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": _RESPONSE_SCHEMA,
            },
        )

    def parse_result(
        self,
        text: str,
        *,
        job_id: str,
        route_image_item_id: str,
        molecule: MoleculeCrop,
    ) -> GeminiOCSRResult:
        if not isinstance(text, str):
            raise OCSRResponseError("Gemini OCSR response text is missing")
        payload = _extract_json(text)
        if set(payload) != {"readable", "smiles", "confidence", "is_abstract", "notes"}:
            raise OCSRResponseError("Gemini OCSR response fields are invalid")
        readable = payload["readable"]
        smiles = payload["smiles"]
        confidence = payload["confidence"]
        is_abstract = payload["is_abstract"]
        notes = payload["notes"]
        if (
            not isinstance(readable, bool)
            or not isinstance(smiles, str)
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not math.isfinite(float(confidence))
            or not 0.0 <= float(confidence) <= 1.0
            or not isinstance(is_abstract, bool)
            or not isinstance(notes, str)
        ):
            raise OCSRResponseError("Gemini OCSR response field types are invalid")
        normalized_smiles = smiles.strip()
        if readable != bool(normalized_smiles):
            raise OCSRResponseError("Gemini OCSR readable flag contradicts SMILES")
        return GeminiOCSRResult(
            schema_version="tsdaf.gemini-ocsr-result.v1",
            job_id=job_id,
            route_image_item_id=route_image_item_id,
            molecule_id=molecule.molecule_id,
            crop_artifact=molecule.crop_artifact,
            status=(OCSRStatus.TRANSCRIBED if normalized_smiles else OCSRStatus.NO_SMILES),
            smiles=normalized_smiles,
            is_abstract=is_abstract,
        )


__all__ = (
    "MoleculeOCSR",
    "MoleculeOCSRPrompt",
    "OCSRPromptError",
    "OCSRRequest",
    "OCSRResponseError",
)
