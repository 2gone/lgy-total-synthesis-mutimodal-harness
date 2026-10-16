"""Lazy adapter for the extracted MolYOLO / YOLOv10 handoff bundle."""

from __future__ import annotations

import hashlib
import importlib
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tsdaf.workflow.ports import DetectorProvenance, RawMoleculeDetection

_ModelFactory = Callable[[Path, Path, str], object]
_VERSION_RE = re.compile(r"__version__\s*=\s*['\"]([^'\"]+)['\"]")


class MolyoloBundleError(RuntimeError):
    """The configured external MolYOLO bundle is incomplete or invalid."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_hash(runtime_root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in (runtime_root / "ultralytics").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    if not files:
        raise MolyoloBundleError("MolYOLO runtime contains no auditable files")
    for path in files:
        relative = path.relative_to(runtime_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path)))
    return digest.hexdigest()


def _runtime_version(init_path: Path) -> str:
    try:
        text = init_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise MolyoloBundleError("MolYOLO runtime entry point cannot be read") from None
    match = _VERSION_RE.search(text)
    if match is None:
        raise MolyoloBundleError("MolYOLO runtime version cannot be identified")
    return match.group(1)


def _default_model_factory(weights: Path, runtime_root: Path, device: str) -> object:
    del device
    runtime_text = str(runtime_root)
    if runtime_text not in sys.path:
        sys.path.insert(0, runtime_text)
    module = importlib.import_module("ultralytics")
    model_type = getattr(module, "YOLOv10", None)
    if model_type is None:
        raise MolyoloBundleError("MolYOLO runtime does not expose YOLOv10")
    return model_type(str(weights))


class MolYoloV10Adapter:
    """Concrete detector with lazy, one-time model loading."""

    __slots__ = (
        "_device",
        "_model",
        "_model_factory",
        "_runtime_root",
        "_weights_path",
        "_provenance",
    )

    def __init__(
        self,
        bundle_root: Path,
        *,
        device: str = "auto",
        model_factory: _ModelFactory | None = None,
    ) -> None:
        if not isinstance(bundle_root, Path):
            raise TypeError("bundle_root must be a pathlib.Path")
        if not isinstance(device, str) or not device.strip():
            raise ValueError("device must be a nonempty string")
        root = bundle_root.resolve(strict=False)
        weights = root / "weights" / "MolYOLO.pt"
        runtime_root = root / ".vendor" / "molyolo_runtime"
        runtime_init = runtime_root / "ultralytics" / "__init__.py"
        if not weights.is_file() or not runtime_init.is_file():
            raise MolyoloBundleError(
                "MolYOLO bundle requires weights/MolYOLO.pt and the vendored YOLOv10 runtime"
            )
        resolved_device = self._resolve_device(device)
        self._weights_path = weights.resolve()
        self._runtime_root = runtime_root.resolve()
        self._device = resolved_device
        self._model_factory = model_factory or _default_model_factory
        self._model: object | None = None
        self._provenance = DetectorProvenance(
            detector_id="molyolo:yolov10",
            adapter_version="1.0.0",
            weights_sha256=_sha256_file(self._weights_path),
            runtime_bundle_sha256=_runtime_hash(self._runtime_root),
            ultralytics_version=_runtime_version(runtime_init),
            image_size=1024,
            confidence_threshold=0.25,
            iou_threshold=0.5,
            device=self._device,
        )

    @staticmethod
    def _resolve_device(requested: str) -> str:
        if requested != "auto":
            return requested
        try:
            torch = importlib.import_module("torch")
            cuda = torch.cuda
            return "0" if bool(cuda.is_available()) else "cpu"
        except (ImportError, AttributeError):
            return "cpu"

    @property
    def provenance(self) -> DetectorProvenance:
        return self._provenance

    def _loaded_model(self) -> object:
        if self._model is None:
            self._model = self._model_factory(
                self._weights_path,
                self._runtime_root,
                self._device,
            )
        return self._model

    def detect(self, image_path: Path) -> tuple[RawMoleculeDetection, ...]:
        if not isinstance(image_path, Path):
            raise TypeError("image_path must be a pathlib.Path")
        resolved = image_path.resolve(strict=False)
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        model: Any = self._loaded_model()
        result = model.predict(
            str(resolved),
            imgsz=self._provenance.image_size,
            conf=float(self._provenance.confidence_threshold),
            iou=float(self._provenance.iou_threshold),
            device=self._device,
            save=False,
            save_txt=False,
            verbose=False,
        )[0]
        boxes = result.boxes.xyxy.detach().cpu().tolist()
        scores = result.boxes.conf.detach().cpu().tolist()
        classes = result.boxes.cls.detach().cpu().int().tolist()
        if not (len(boxes) == len(scores) == len(classes)):
            raise ValueError("MolYOLO returned divergent box arrays")
        if any(len(box) != 4 for box in boxes):
            raise ValueError("MolYOLO returned an invalid bounding box")
        return tuple(
            RawMoleculeDetection(
                bbox_xyxy=(
                    float(box[0]),
                    float(box[1]),
                    float(box[2]),
                    float(box[3]),
                ),
                confidence=float(score),
                class_id=int(class_id),
                class_name=str(result.names[int(class_id)]),
            )
            for box, score, class_id in zip(boxes, scores, classes, strict=True)
        )


__all__ = ("MolyoloBundleError", "MolYoloV10Adapter")
