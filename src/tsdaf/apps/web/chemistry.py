from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from io import BytesIO
from math import ceil, floor
from pathlib import Path
from typing import Any

from PIL import Image

from tsdaf.apps.web.translations import reaction_summary_zh

_RECORDED_COMMIT = "5654ccaaa74da3cd30e4b68f05adf85ba9673c83"
_RECORDED_ASSET_COMMIT = "05a846a472ca57baeb19af74f6cc2a19be732a56"
_BUNDLED_RESULTS_DIRECTORY = Path("results/five-papers-20261010")
_RECORDED_ASSET_DIRECTORY = Path("data/overlay-harness-v1/files")
_RECORDED_PAPERS = (
    "alchivemycin_A",
    "hyperfirin",
    "ineleganolide",
    "kalmanol",
    "principinol_B",
)
_RECORDED_SOURCE_PAPERS = {
    "5ad8252e2ef59f608c7e4c47720b9ef4899e672b73d374d2e73fcb11d46a236f": "alchivemycin_A",
    "98173d29f2fa21e540559562285ba382d25eb63f374985db47c88b0ebf433759": "hyperfirin",
    "3ddb93a0267dbce84dd083eb6a1c3ff063ef85f61d01681ff50d5b3de766d7b1": "ineleganolide",
    "7297073433fd7008dd1a9611d566705ee322cf794cbf54d774077959263c8680": "kalmanol",
    "38b78065c24f052132ef2f0039f16e3a27633506e669f32640d668fc4f779fc1": "principinol_B",
}


class ChemistryResultError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChemistryResult:
    paper_id: str
    raw: bytes
    payload: dict[str, Any]
    source: str


@dataclass(frozen=True, slots=True)
class StructureBinding:
    paper_id: str
    raw: bytes
    payload: dict[str, Any]
    source: str


class RecordedChemistryStore:
    """Read immutable recorded results without pretending to execute a model."""

    def __init__(self, checkout: Path, result_dir: Path | None = None) -> None:
        configured = os.environ.get("TSDAF_CHEMISTRY_RESULTS_DIR")
        self._checkout = checkout
        bundled = checkout / _BUNDLED_RESULTS_DIRECTORY
        self._result_dir = result_dir or (
            Path(configured).expanduser() if configured else bundled if bundled.is_dir() else None
        )
        self._cache: dict[str, ChemistryResult] = {}
        self._binding_cache: dict[str, StructureBinding | None] = {}
        self._figure_size_cache: dict[tuple[str, str], tuple[int, int] | None] = {}

    def available_papers(self) -> list[str]:
        return [paper_id for paper_id in _RECORDED_PAPERS if self._can_read(paper_id)]

    def get(self, paper_id: str) -> ChemistryResult | None:
        if paper_id not in _RECORDED_PAPERS:
            return None
        if paper_id in self._cache:
            return self._cache[paper_id]
        loaded = self._read_directory(paper_id) or self._read_git(paper_id)
        if loaded is None:
            return None
        result = self._validate(paper_id, *loaded)
        self._cache[paper_id] = result
        return result

    def get_for_package(self, paper_id: str, source_sha256: str) -> ChemistryResult | None:
        """Resolve a recorded result by contract ID or immutable source-PDF identity."""
        exact = self.get(paper_id)
        if exact is not None:
            return exact
        recorded_paper_id = _RECORDED_SOURCE_PAPERS.get(source_sha256.lower())
        return self.get(recorded_paper_id) if recorded_paper_id is not None else None

    def get_structure_binding(self, paper_id: str) -> StructureBinding | None:
        """Read the immutable OCSR sidecar without merging it into machine facts."""
        if paper_id not in _RECORDED_PAPERS:
            return None
        if paper_id in self._binding_cache:
            return self._binding_cache[paper_id]
        loaded = self._read_binding_directory(paper_id) or self._read_binding_git(paper_id)
        if loaded is None:
            self._binding_cache[paper_id] = None
            return None
        raw, source = loaded
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChemistryResultError("recorded structure binding is not valid JSON") from error
        if not isinstance(payload, dict) or payload.get("paper_id") != paper_id:
            raise ChemistryResultError("recorded structure binding identity does not match")
        if not isinstance(payload.get("compounds"), list):
            raise ChemistryResultError("recorded structure binding compounds are invalid")
        binding = StructureBinding(paper_id=paper_id, raw=raw, payload=payload, source=source)
        self._binding_cache[paper_id] = binding
        return binding

    def get_structure_crop(self, paper_id: str, crop_sha256: str) -> bytes | None:
        """Return only a crop explicitly authorized by this paper's binding sidecar."""
        if len(crop_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in crop_sha256
        ):
            return None
        binding = self.get_structure_binding(paper_id)
        if binding is None or crop_sha256 not in _binding_crop_hashes(binding.payload):
            return None
        directory_crop = self._read_crop_directory(paper_id, crop_sha256)
        if directory_crop is not None:
            return directory_crop
        object_name = (
            f"{_RECORDED_ASSET_COMMIT}:"
            f"{(_RECORDED_ASSET_DIRECTORY / f'{crop_sha256}.png').as_posix()}"
        )
        result = subprocess.run(
            ["git", "show", object_name],
            cwd=self._checkout,
            capture_output=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    def get_reaction_crop(
        self,
        paper_id: str,
        reaction_block_id: str,
        visual_index: int = 0,
    ) -> bytes | None:
        """Crop one reaction directly from its immutable source figure."""
        if visual_index < 0:
            return None
        result = self.get(paper_id)
        binding = self.get_structure_binding(paper_id)
        if result is None or binding is None:
            return None
        blocks = result.payload["domain_payload"].get("reaction_blocks") or []
        block = next(
            (
                item
                for item in blocks
                if isinstance(item, dict)
                and item.get("reaction_block_id") == reaction_block_id
            ),
            None,
        )
        if block is None:
            return None
        candidates = _reaction_visual_candidates(block, binding)
        if visual_index >= len(candidates):
            return None
        candidate = candidates[visual_index]
        figure = self._read_figure_asset(paper_id, candidate["figure_asset_sha256"])
        if figure is None:
            return None
        try:
            with Image.open(BytesIO(figure)) as source:
                image = source.convert("RGB")
                if _reaction_crop_quality(candidate["bbox_px"], image.size)["status"] != (
                    "AVAILABLE"
                ):
                    return None
                crop_box = _reaction_crop_box(candidate["bbox_px"], image.size)
                cropped = image.crop(crop_box)
                output = BytesIO()
                cropped.save(output, format="PNG", optimize=True)
                return output.getvalue()
        except (OSError, ValueError):
            return None

    def get_figure_dimensions(self, paper_id: str) -> dict[str, tuple[int, int]]:
        binding = self.get_structure_binding(paper_id)
        if binding is None:
            return {}
        dimensions: dict[str, tuple[int, int]] = {}
        for asset_sha256 in sorted(_binding_figure_hashes(binding.payload)):
            cache_key = (paper_id, asset_sha256)
            if cache_key not in self._figure_size_cache:
                figure = self._read_figure_asset(paper_id, asset_sha256)
                if figure is None:
                    self._figure_size_cache[cache_key] = None
                else:
                    try:
                        with Image.open(BytesIO(figure)) as image:
                            self._figure_size_cache[cache_key] = image.size
                    except OSError:
                        self._figure_size_cache[cache_key] = None
            size = self._figure_size_cache[cache_key]
            if size is not None:
                dimensions[asset_sha256] = size
        return dimensions

    def get_figure_context(self, paper_id: str, asset_sha256: str) -> bytes | None:
        """Return a paper-authorized full Figure as PNG for display-only context."""
        figure = self._read_figure_asset(paper_id, asset_sha256)
        if figure is None:
            return None
        try:
            with Image.open(BytesIO(figure)) as source:
                output = BytesIO()
                source.convert("RGB").save(output, format="PNG", optimize=True)
                return output.getvalue()
        except OSError:
            return None

    def _can_read(self, paper_id: str) -> bool:
        if (
            self._result_dir is not None
            and (self._result_dir / paper_id / "machine-paper-result.json").is_file()
        ):
            return True
        object_name = f"{_RECORDED_COMMIT}:{self._git_path(paper_id)}"
        check = subprocess.run(
            ["git", "cat-file", "-e", object_name],
            cwd=self._checkout,
            capture_output=True,
            check=False,
        )
        return check.returncode == 0

    def _read_directory(self, paper_id: str) -> tuple[bytes, str] | None:
        if self._result_dir is None:
            return None
        path = self._result_dir / paper_id / "machine-paper-result.json"
        if not path.is_file():
            return None
        return path.read_bytes(), "configured-recorded-directory"

    def _read_git(self, paper_id: str) -> tuple[bytes, str] | None:
        object_name = f"{_RECORDED_COMMIT}:{self._git_path(paper_id)}"
        result = subprocess.run(
            ["git", "show", object_name],
            cwd=self._checkout,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout, "recorded-five-paper-run-20261010"

    def _read_binding_directory(self, paper_id: str) -> tuple[bytes, str] | None:
        if self._result_dir is None:
            return None
        path = self._result_dir / paper_id / "structure-binding.json"
        if not path.is_file():
            return None
        return path.read_bytes(), "configured-recorded-directory"

    def _read_binding_git(self, paper_id: str) -> tuple[bytes, str] | None:
        object_name = (
            f"{_RECORDED_COMMIT}:results/five-papers-20261010/"
            f"{paper_id}/structure-binding.json"
        )
        result = subprocess.run(
            ["git", "show", object_name],
            cwd=self._checkout,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout, "recorded-five-paper-run-20261010"

    def _read_crop_directory(self, paper_id: str, crop_sha256: str) -> bytes | None:
        candidates: list[Path] = []
        if self._result_dir is not None:
            paper_root = self._result_dir / paper_id
            candidates.extend(
                (paper_root / "crops" / f"{crop_sha256}.png", paper_root / f"{crop_sha256}.png")
            )
        candidates.append(self._checkout / _RECORDED_ASSET_DIRECTORY / f"{crop_sha256}.png")
        for path in candidates:
            if path.is_file():
                return path.read_bytes()
        return None

    def _read_figure_asset(self, paper_id: str, asset_sha256: str) -> bytes | None:
        binding = self.get_structure_binding(paper_id)
        if binding is None or asset_sha256 not in _binding_figure_hashes(binding.payload):
            return None
        candidates: list[Path] = []
        if self._result_dir is not None:
            paper_root = self._result_dir / paper_id
            for suffix in (".jpg", ".jpeg", ".png"):
                candidates.extend(
                    (
                        paper_root / "figures" / f"{asset_sha256}{suffix}",
                        paper_root / f"{asset_sha256}{suffix}",
                    )
                )
        for suffix in (".jpg", ".jpeg", ".png"):
            candidates.append(
                self._checkout / _RECORDED_ASSET_DIRECTORY / f"{asset_sha256}{suffix}"
            )
        for path in candidates:
            if path.is_file():
                return path.read_bytes()
        for suffix in (".jpg", ".jpeg", ".png"):
            object_name = (
                f"{_RECORDED_ASSET_COMMIT}:"
                f"{(_RECORDED_ASSET_DIRECTORY / f'{asset_sha256}{suffix}').as_posix()}"
            )
            result = subprocess.run(
                ["git", "show", object_name],
                cwd=self._checkout,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout
        return None

    @staticmethod
    def _git_path(paper_id: str) -> str:
        return f"results/five-papers-20261010/{paper_id}/machine-paper-result.json"

    @staticmethod
    def _validate(paper_id: str, raw: bytes, source: str) -> ChemistryResult:
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ChemistryResultError("recorded chemistry result is not valid JSON") from error
        if not isinstance(payload, dict):
            raise ChemistryResultError("recorded chemistry result root must be an object")
        if payload.get("result_kind") != "MACHINE_RESULT" or payload.get("paper_id") != paper_id:
            raise ChemistryResultError("recorded chemistry result identity does not match")
        schema = payload.get("schema_version")
        domain = payload.get("domain_payload")
        if not isinstance(schema, str) or not schema.startswith("tsdaf.machine-paper-result."):
            raise ChemistryResultError("recorded chemistry result schema is unsupported")
        if not isinstance(domain, dict):
            raise ChemistryResultError("recorded chemistry result has no domain payload")
        for field in ("compounds", "reaction_blocks", "routes"):
            if not isinstance(domain.get(field), list):
                raise ChemistryResultError(f"recorded chemistry result field {field} is invalid")
        return ChemistryResult(paper_id=paper_id, raw=raw, payload=payload, source=source)


def chemistry_summary(
    result: ChemistryResult,
    structure_binding: StructureBinding | None = None,
    figure_dimensions: dict[str, tuple[int, int]] | None = None,
) -> dict[str, Any]:
    payload = result.payload
    domain = payload["domain_payload"]
    routes = domain.get("routes") or []
    blocks = domain.get("reaction_blocks") or []
    compounds = domain.get("compounds") or []
    route_previews = [
        {
            "route_id": route.get("route_id"),
            "overview": route.get("model_overview") or route.get("model_integration_note"),
            "reaction_block_count": len(route.get("reaction_block_ids") or []),
            "compound_count": len(route.get("compound_ids") or []),
            "target_count": len(route.get("target_assertions") or []),
        }
        for route in routes
    ]
    visual_bindings = _compound_visual_bindings(result.paper_id, structure_binding)
    compounds_by_id = {
        compound.get("compound_id"): compound
        for compound in compounds
        if isinstance(compound.get("compound_id"), str)
    }
    route_contexts = _reaction_route_contexts(routes)
    block_previews = [
        {
            "reaction_block_id": block.get("reaction_block_id"),
            "source_order": block.get("source_order"),
            "summary": block.get("summary") or "未提供步骤摘要",
            "summary_zh": _display_text(block.get("summary_zh"))
            or reaction_summary_zh(result.paper_id, block.get("source_order")),
            "input_compound_ids": _relation_value(block.get("input_compound_ids")),
            "output_compound_ids": _relation_value(block.get("output_compound_ids")),
            "operation_count": len(block.get("operations") or []),
            "outcome_count": len(block.get("reported_outcomes") or []),
            "participants": {
                "inputs": _reaction_participants(
                    _relation_value(block.get("input_compound_ids")),
                    compounds_by_id,
                    visual_bindings,
                ),
                "outputs": _reaction_participants(
                    _relation_value(block.get("output_compound_ids")),
                    compounds_by_id,
                    visual_bindings,
                ),
            },
            "reaction_visual": _reaction_visual_summary(
                result.paper_id,
                block,
                structure_binding,
                figure_dimensions,
            ),
            "experimental_steps": _reaction_experimental_steps(block),
            "conditions": _reaction_conditions(block),
            "outcomes": _reaction_outcomes(block),
            "primary_yield": _reaction_primary_yield(block),
            "route_context": route_contexts.get(
                block.get("reaction_block_id"),
                {"included": False},
            ),
        }
        for block in blocks
    ]
    compound_previews = [
        {
            "compound_id": compound.get("compound_id"),
            "labels": _text_values(compound.get("author_labels")),
            "names": _text_values(compound.get("names")),
            "has_structure": _has_relation_value(compound.get("structure")),
            "visual_structure": visual_bindings.get(compound.get("compound_id")),
        }
        for compound in compounds
    ]
    return {
        "paper_id": result.paper_id,
        "machine_result_id": payload.get("machine_result_id"),
        "schema_version": payload.get("schema_version"),
        "coverage": payload.get("coverage"),
        "completeness": payload.get("completeness"),
        "execution_id": payload.get("execution_id"),
        "source": result.source,
        "real_model_called_now": False,
        "structure_binding": _structure_binding_summary(structure_binding),
        "counts": {
            "compounds": len(compounds),
            "reaction_blocks": len(blocks),
            "routes": len(routes),
            "characterization_records": len(domain.get("characterization_records") or []),
            "experimental_accounts": len(domain.get("experimental_accounts") or []),
            "biological_findings": len(domain.get("biological_findings") or []),
            "field_conflicts": len(domain.get("field_conflicts") or []),
            "paper_narratives": len(domain.get("paper_narratives") or []),
            "unresolved_items": len(payload.get("unresolved_items") or []),
        },
        "routes": route_previews,
        "reaction_blocks": block_previews,
        "compounds": compound_previews,
    }


def _relation_value(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("value")
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _text_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        if isinstance(item, str):
            output.append(item)
        elif isinstance(item, dict):
            text = item.get("value") or item.get("text") or item.get("name") or item.get("label")
            if isinstance(text, str):
                output.append(text)
    return output


def _relation_data(value: object) -> object:
    if isinstance(value, dict) and "state" in value and "value" in value:
        return value.get("value") if value.get("state") == "PRESENT" else None
    return value


def _display_text(value: object) -> str | None:
    value = _relation_data(value)
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("raw_text", "text", "name", "label"):
            text = value.get(key)
            if isinstance(text, str) and text.strip():
                return text.strip()
    if isinstance(value, list):
        items = [text for item in value if (text := _display_text(item))]
        return "；".join(items) or None
    return None


def _reaction_route_contexts(routes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for route in routes:
        if not isinstance(route, dict):
            continue
        reaction_ids = _relation_value(route.get("reaction_block_ids"))
        for position, reaction_id in enumerate(reaction_ids, start=1):
            contexts.setdefault(
                reaction_id,
                {
                    "included": True,
                    "route_id": route.get("route_id"),
                    "position": position,
                    "total_reaction_blocks": len(reaction_ids),
                    "overview": route.get("model_overview")
                    or route.get("model_integration_note"),
                },
            )
    return contexts


def _reaction_experimental_steps(block: dict[str, Any]) -> list[str]:
    steps: list[tuple[int, str]] = []
    fallback_index = 0
    for operation in block.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        stages = [stage for stage in operation.get("stages") or [] if isinstance(stage, dict)]
        if stages:
            for stage in stages:
                description = _display_text(stage.get("description"))
                if description:
                    fallback_index += 1
                    display_index = stage.get("display_index")
                    steps.append(
                        (
                            display_index if isinstance(display_index, int) else fallback_index,
                            description,
                        )
                    )
            continue
        description = _display_text(operation.get("description"))
        if description:
            fallback_index += 1
            sequence_index = operation.get("sequence_index")
            steps.append(
                (
                    sequence_index if isinstance(sequence_index, int) else fallback_index,
                    description,
                )
            )
    return [description for _, description in sorted(steps, key=lambda item: item[0])]


def _reaction_conditions(block: dict[str, Any]) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    for index, operation in enumerate(block.get("operations") or [], start=1):
        if not isinstance(operation, dict):
            continue
        materials_value = _relation_data(operation.get("materials"))
        materials: list[dict[str, str | None]] = []
        if isinstance(materials_value, list):
            for material in materials_value:
                if not isinstance(material, dict):
                    continue
                names_value = _relation_data(material.get("names"))
                names = _text_values(names_value)
                name = names[0] if names else _display_text(material.get("verbatim_name"))
                if name:
                    materials.append(
                        {
                            "role": _display_text(material.get("role")) or "OTHER",
                            "name": name,
                            "amount": _display_text(material.get("amount")),
                            "equivalents": _display_text(material.get("equivalents")),
                        }
                    )
        other_conditions = [
            text
            for item in operation.get("other_conditions") or []
            if isinstance(item, dict) and (text := _display_text(item.get("about")))
        ]
        conditions.append(
            {
                "operation_index": operation.get("sequence_index") or index,
                "reaction_class": _display_text(operation.get("reaction_class")),
                "atmosphere": _display_text(operation.get("atmosphere")),
                "temperature": _display_text(operation.get("temperature_profile")),
                "duration": _display_text(operation.get("duration")),
                "pressure": _display_text(operation.get("pressure")),
                "concentration": _display_text(operation.get("concentration")),
                "key_addition": _display_text(operation.get("key_addition")),
                "key_workup": _display_text(operation.get("key_workup")),
                "materials": materials,
                "other_conditions": other_conditions,
            }
        )
    return conditions


def _reaction_outcomes(block: dict[str, Any]) -> list[dict[str, str | None]]:
    outcomes: list[dict[str, str | None]] = []
    seen: dict[tuple[str | None, str | None], int] = {}
    for outcome in block.get("reported_outcomes") or []:
        if not isinstance(outcome, dict):
            continue
        for quantity in outcome.get("quantities") or []:
            if not isinstance(quantity, dict):
                continue
            item = {
                "kind": _display_text(quantity.get("kind")),
                "value": _display_text(quantity.get("reported_value")),
                "basis": _display_text(quantity.get("basis")),
            }
            identity = (item["kind"], item["value"])
            if not item["value"]:
                continue
            previous_index = seen.get(identity)
            if previous_index is None:
                seen[identity] = len(outcomes)
                outcomes.append(item)
            elif outcomes[previous_index]["basis"] is None and item["basis"]:
                outcomes[previous_index]["basis"] = item["basis"]
    return outcomes


def _reaction_primary_yield(block: dict[str, Any]) -> str | None:
    return next(
        (
            outcome["value"]
            for outcome in _reaction_outcomes(block)
            if outcome["kind"] and "YIELD" in outcome["kind"] and outcome["value"]
        ),
        None,
    )


def _has_relation_value(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("state") == "PRESENT" and value.get("value") is not None
    return value is not None


def _binding_crop_hashes(payload: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for compound in payload.get("compounds") or []:
        if not isinstance(compound, dict):
            continue
        for structure in compound.get("structures") or []:
            if isinstance(structure, dict) and isinstance(structure.get("crop_sha256"), str):
                hashes.add(structure["crop_sha256"])
    return hashes


def _binding_figure_hashes(payload: dict[str, Any]) -> set[str]:
    hashes: set[str] = set()
    for compound in payload.get("compounds") or []:
        if not isinstance(compound, dict):
            continue
        for structure in compound.get("structures") or []:
            if isinstance(structure, dict) and isinstance(
                structure.get("figure_asset_sha256"), str
            ):
                hashes.add(structure["figure_asset_sha256"])
    return hashes


def _compound_visual_bindings(
    paper_id: str,
    binding: StructureBinding | None,
) -> dict[str, dict[str, Any]]:
    if binding is None:
        return {}
    output: dict[str, dict[str, Any]] = {}
    authority = binding.payload.get("authority")
    for compound in binding.payload.get("compounds") or []:
        if not isinstance(compound, dict) or not isinstance(compound.get("compound_id"), str):
            continue
        structures = [item for item in compound.get("structures") or [] if isinstance(item, dict)]
        readable = next(
            (
                item
                for item in structures
                if item.get("binding") == "BOUND"
                and item.get("readable") is True
                and isinstance(item.get("crop_sha256"), str)
            ),
            None,
        )
        if readable is None:
            continue
        crop_sha256 = readable["crop_sha256"]
        output[compound["compound_id"]] = {
            "image_url": f"/api/chemistry/results/{paper_id}/visuals/{crop_sha256}.png",
            "crop_sha256": crop_sha256,
            "printed_label": readable.get("printed_label"),
            "smiles": readable.get("smiles"),
            "formula": readable.get("formula"),
            "reader_confidence": readable.get("reader_confidence"),
            "authority": authority,
            "binding": readable.get("binding"),
        }
    return output


def _reaction_participants(
    compound_ids: list[str],
    compounds_by_id: dict[object, dict[str, Any]],
    visual_bindings: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    participants: list[dict[str, Any]] = []
    for compound_id in compound_ids:
        compound = compounds_by_id.get(compound_id) or {}
        participants.append(
            {
                "compound_id": compound_id,
                "labels": _text_values(compound.get("author_labels")),
                "names": _text_values(compound.get("names")),
                "visual_structure": visual_bindings.get(compound_id),
            }
        )
    return participants


def _structure_binding_summary(binding: StructureBinding | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    return {
        "schema_version": binding.payload.get("schema_version"),
        "authority": binding.payload.get("authority"),
        "notice": binding.payload.get("notice"),
        "source": binding.source,
        "summary": binding.payload.get("summary"),
    }


def _reaction_visual_summary(
    paper_id: str,
    block: dict[str, Any],
    binding: StructureBinding | None,
    figure_dimensions: dict[str, tuple[int, int]] | None = None,
) -> dict[str, Any] | None:
    if binding is None:
        return None
    reaction_block_id = block.get("reaction_block_id")
    if not isinstance(reaction_block_id, str):
        return None
    candidates = _reaction_visual_candidates(block, binding)
    if not candidates:
        unavailable = _reaction_visual_unavailable(block, binding)
        context_sha256 = _reaction_context_figure(block, binding)
        if context_sha256 is not None:
            unavailable.update(_figure_context(paper_id, context_sha256))
        return unavailable
    safe_candidates = []
    context_only_candidates = []
    for candidate in candidates:
        dimensions = (figure_dimensions or {}).get(candidate["figure_asset_sha256"])
        if dimensions is None:
            safe_candidates.append(candidate)
            continue
        quality = _reaction_crop_quality(candidate["bbox_px"], dimensions)
        if quality["status"] == "AVAILABLE":
            safe_candidates.append(candidate)
        else:
            context_only_candidates.append(candidate)
    if not safe_candidates:
        context = context_only_candidates[0]
        return {
            **_reaction_crop_quality(
                context["bbox_px"],
                (figure_dimensions or {})[context["figure_asset_sha256"]],
            ),
            **_figure_context(paper_id, context["figure_asset_sha256"]),
        }
    visuals = []
    for index, candidate in enumerate(safe_candidates):
        suffix = ".png" if index == 0 else f"/{index}.png"
        visuals.append(
            {
                "image_url": (
                    f"/api/chemistry/results/{paper_id}/reactions/"
                    f"{reaction_block_id}{suffix}"
                ),
                "source": "DERIVED_LOCAL_ORIGINAL_FIGURE_CROP",
                "figure_asset_sha256": candidate["figure_asset_sha256"],
                "participant_count": candidate["participant_count"],
                "visual_index": index,
                "context_image_url": _figure_context_url(
                    paper_id, candidate["figure_asset_sha256"]
                ),
            }
        )
    primary = {"status": "AVAILABLE", **visuals[0]}
    primary["alternatives"] = visuals[1:]
    return primary


def _figure_context(paper_id: str, figure_asset_sha256: str) -> dict[str, str]:
    return {
        "figure_asset_sha256": figure_asset_sha256,
        "context_image_url": _figure_context_url(paper_id, figure_asset_sha256),
    }


def _figure_context_url(paper_id: str, figure_asset_sha256: str) -> str:
    return f"/api/chemistry/results/{paper_id}/figures/{figure_asset_sha256}.png"


def _reaction_context_figure(
    block: dict[str, Any],
    binding: StructureBinding,
) -> str | None:
    participant_ids = set(_relation_value(block.get("input_compound_ids"))) | set(
        _relation_value(block.get("output_compound_ids"))
    )
    candidates: list[str] = []
    for compound in binding.payload.get("compounds") or []:
        if not isinstance(compound, dict) or compound.get("compound_id") not in participant_ids:
            continue
        for structure in compound.get("structures") or []:
            if not isinstance(structure, dict):
                continue
            figure_sha256 = structure.get("figure_asset_sha256")
            if (
                structure.get("binding") in {"BOUND", "BOUND_SHARED"}
                and isinstance(figure_sha256, str)
                and figure_sha256 not in candidates
            ):
                candidates.append(figure_sha256)
    return candidates[0] if candidates else None


def _reaction_visual_candidate(
    block: dict[str, Any],
    binding: StructureBinding,
) -> dict[str, Any] | None:
    candidates = _reaction_visual_candidates(block, binding)
    return candidates[0] if candidates else None


def _reaction_visual_candidates(
    block: dict[str, Any],
    binding: StructureBinding,
) -> list[dict[str, Any]]:
    """Select local input/output occurrences without joining unrelated figure panels.

    This is explicitly a display-layer heuristic. Machine reaction facts and the immutable
    structure-binding sidecar remain unchanged.
    """
    input_ids = set(_relation_value(block.get("input_compound_ids")))
    output_ids = set(_relation_value(block.get("output_compound_ids")))
    structures_by_figure: dict[str, dict[str, list[list[float]]]] = {}
    for compound in binding.payload.get("compounds") or []:
        if not isinstance(compound, dict):
            continue
        compound_id = compound.get("compound_id")
        if compound_id not in input_ids | output_ids:
            continue
        for structure in compound.get("structures") or []:
            if not isinstance(structure, dict):
                continue
            figure_sha = structure.get("figure_asset_sha256")
            bbox = structure.get("bbox_px")
            if (
                structure.get("binding") not in {"BOUND", "BOUND_SHARED"}
                or not isinstance(figure_sha, str)
                or not isinstance(bbox, list)
                or len(bbox) != 4
                or not all(isinstance(value, (int, float)) for value in bbox)
            ):
                continue
            structures_by_figure.setdefault(figure_sha, {}).setdefault(
                str(compound_id), []
            ).append([float(value) for value in bbox])
    candidates: list[dict[str, Any]] = []
    for figure_sha, structures in structures_by_figure.items():
        figure_input_ids = sorted(input_ids & structures.keys())
        figure_output_ids = sorted(output_ids & structures.keys())
        if not figure_input_ids or not figure_output_ids:
            continue
        figure_participant_ids = sorted((input_ids | output_ids) & structures.keys())
        for input_id in figure_input_ids:
            for input_box in structures[input_id]:
                for output_id in figure_output_ids:
                    for output_box in structures[output_id]:
                        selected = {input_id: input_box, output_id: output_box}
                        for compound_id in figure_participant_ids:
                            if compound_id in selected:
                                continue
                            selected[compound_id] = min(
                                structures[compound_id],
                                key=lambda box: _expanded_bbox_area(
                                    [*selected.values(), box]
                                ),
                            )
                        boxes = list(selected.values())
                        bbox = _union_bbox(boxes)
                        overlap, vertical_gap = _horizontal_alignment(
                            input_box, output_box
                        )
                        candidates.append(
                            {
                                "figure_asset_sha256": figure_sha,
                                "participant_count": len(selected),
                                "bbox_px": bbox,
                                "_area": _bbox_area(bbox),
                                "_horizontally_aligned": overlap > 0,
                                "_vertical_gap": vertical_gap,
                            }
                        )
    if not candidates:
        return []

    maximum_participants = max(item["participant_count"] for item in candidates)
    candidates = [
        item for item in candidates if item["participant_count"] == maximum_participants
    ]
    if any(item["_horizontally_aligned"] for item in candidates):
        candidates = [item for item in candidates if item["_horizontally_aligned"]]
    candidates.sort(
        key=lambda item: (
            item["_vertical_gap"],
            item["_area"],
            item["bbox_px"][1],
            item["bbox_px"][0],
            item["figure_asset_sha256"],
        )
    )

    selected_candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[float, ...]]] = set()
    for candidate in candidates:
        identity = (
            candidate["figure_asset_sha256"],
            tuple(candidate["bbox_px"]),
        )
        if identity in seen:
            continue
        seen.add(identity)
        if any(
            candidate["figure_asset_sha256"] == selected["figure_asset_sha256"]
            and _bbox_iou(candidate["bbox_px"], selected["bbox_px"]) > 0.15
            for selected in selected_candidates
        ):
            continue
        selected_candidates.append(candidate)
        if len(selected_candidates) == 4:
            break
    for candidate in selected_candidates:
        candidate.pop("_area", None)
        candidate.pop("_horizontally_aligned", None)
        candidate.pop("_vertical_gap", None)
    return selected_candidates


def _reaction_visual_unavailable(
    block: dict[str, Any],
    binding: StructureBinding,
) -> dict[str, Any]:
    bound_ids = {
        str(compound.get("compound_id"))
        for compound in binding.payload.get("compounds") or []
        if isinstance(compound, dict)
        and any(
            isinstance(structure, dict)
            and structure.get("binding") in {"BOUND", "BOUND_SHARED"}
            and isinstance(structure.get("figure_asset_sha256"), str)
            and isinstance(structure.get("bbox_px"), list)
            for structure in compound.get("structures") or []
        )
    }
    missing_inputs = sorted(set(_relation_value(block.get("input_compound_ids"))) - bound_ids)
    missing_outputs = sorted(set(_relation_value(block.get("output_compound_ids"))) - bound_ids)
    if missing_inputs and missing_outputs:
        reason_code = "MISSING_INPUT_AND_OUTPUT_VISUAL_BINDING"
    elif missing_inputs:
        reason_code = "MISSING_INPUT_VISUAL_BINDING"
    elif missing_outputs:
        reason_code = "MISSING_OUTPUT_VISUAL_BINDING"
    else:
        reason_code = "NO_SHARED_FIGURE_VISUAL_BINDING"
    return {
        "status": "UNAVAILABLE",
        "reason_code": reason_code,
        "missing_input_compound_ids": missing_inputs,
        "missing_output_compound_ids": missing_outputs,
    }


def _union_bbox(boxes: list[list[float]]) -> list[float]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _reaction_crop_quality(
    bbox: list[float],
    image_size: tuple[int, int],
) -> dict[str, str]:
    """Reject rectangles that necessarily span several rows of a full Scheme.

    A full-Scheme context link is safer than presenting a large multi-step rectangle as if it
    were a single reaction. This rule affects display only and never changes machine facts.
    """
    image_width, image_height = image_size
    left, top, right, bottom = bbox
    width_ratio = max(0.0, right - left) / image_width
    height_ratio = max(0.0, bottom - top) / image_height
    area_ratio = width_ratio * height_ratio
    if area_ratio > 0.30 or (
        area_ratio > 0.22 and height_ratio > 0.38
    ) or (width_ratio > 0.82 and height_ratio > 0.30):
        return {
            "status": "CONTEXT_ONLY",
            "reason_code": "MULTI_STEP_SCHEME_LAYOUT",
        }
    return {"status": "AVAILABLE", "reason_code": "LOCAL_REACTION_CROP"}


def _reaction_crop_box(
    bbox: list[float],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Add layout-aware space for arrows, conditions, yields and labels.

    Horizontal reactions need substantially more vertical context than molecule-only boxes;
    vertical or wrapped reactions need lateral context. Figure-relative minimums avoid the old
    failure mode where small structures caused their reaction conditions to be clipped away.
    """
    image_width, image_height = image_size
    left, top, right, bottom = bbox
    width = max(1.0, right - left)
    height = max(1.0, bottom - top)
    vertical_layout = height > width * 0.75
    if vertical_layout:
        horizontal_padding = max(image_width * 0.25, width * 0.20)
        vertical_padding = max(image_height * 0.04, height * 0.12)
    else:
        horizontal_padding = max(image_width * 0.025, width * 0.04)
        vertical_padding = max(image_height * 0.07, height * 0.35)
    return (
        max(0, floor(left - horizontal_padding)),
        max(0, floor(top - vertical_padding)),
        min(image_width, ceil(right + horizontal_padding)),
        min(image_height, ceil(bottom + vertical_padding)),
    )


def _bbox_area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _expanded_bbox_area(boxes: list[list[float]]) -> float:
    return _bbox_area(_union_bbox(boxes))


def _horizontal_alignment(first: list[float], second: list[float]) -> tuple[float, float]:
    overlap = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    minimum_height = max(1.0, min(first[3] - first[1], second[3] - second[1]))
    first_center = (first[1] + first[3]) / 2
    second_center = (second[1] + second[3]) / 2
    return overlap / minimum_height, abs(first_center - second_center) / minimum_height


def _bbox_iou(first: list[float], second: list[float]) -> float:
    intersection = [
        max(first[0], second[0]),
        max(first[1], second[1]),
        min(first[2], second[2]),
        min(first[3], second[3]),
    ]
    intersection_area = _bbox_area(intersection)
    union_area = _bbox_area(first) + _bbox_area(second) - intersection_area
    return intersection_area / union_area if union_area > 0 else 0.0
