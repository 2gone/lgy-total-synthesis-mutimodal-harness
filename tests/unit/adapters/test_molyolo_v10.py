from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tsdaf.adapters.moldet.molyolo_v10 import MolyoloBundleError, MolYoloV10Adapter


class _Tensor:
    def __init__(self, value: object) -> None:
        self._value = value

    def detach(self) -> _Tensor:
        return self

    def cpu(self) -> _Tensor:
        return self

    def int(self) -> _Tensor:
        return self

    def tolist(self) -> object:
        return self._value


class _Boxes:
    xyxy = _Tensor([[1.25, 2.5, 9.75, 12.25]])
    conf = _Tensor([0.875])
    cls = _Tensor([0])


class _Result:
    boxes = _Boxes()
    names = {0: "mol"}


class _Model:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def predict(self, image_path: str, **kwargs: object) -> list[_Result]:
        self.calls.append({"image_path": image_path, **kwargs})
        return [_Result()]


def _bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    weights = root / "weights" / "MolYOLO.pt"
    runtime = root / ".vendor" / "molyolo_runtime" / "ultralytics"
    weights.parent.mkdir(parents=True)
    runtime.mkdir(parents=True)
    weights.write_bytes(b"weights")
    (runtime / "__init__.py").write_text('__version__ = "8.1.34"\n', encoding="utf-8")
    (runtime / "engine.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def test_molyolo_adapter_loads_once_and_converts_provider_output(tmp_path: Path) -> None:
    model = _Model()
    loads: list[tuple[Path, Path, str]] = []

    def factory(weights: Path, runtime: Path, device: str) -> _Model:
        loads.append((weights, runtime, device))
        return model

    adapter = MolYoloV10Adapter(
        _bundle(tmp_path),
        device="cpu",
        model_factory=factory,
    )
    image = tmp_path / "scheme.png"
    image.write_bytes(b"image")

    first = adapter.detect(image)
    second = adapter.detect(image)

    assert len(loads) == 1
    assert first == second
    assert first[0].bbox_xyxy == (1.25, 2.5, 9.75, 12.25)
    assert first[0].confidence == 0.875
    assert model.calls[0] == {
        "image_path": str(image.resolve()),
        "imgsz": 1024,
        "conf": 0.25,
        "iou": 0.5,
        "device": "cpu",
        "save": False,
        "save_txt": False,
        "verbose": False,
    }
    assert adapter.provenance.ultralytics_version == "8.1.34"


def test_molyolo_adapter_rejects_incomplete_bundle(tmp_path: Path) -> None:
    with pytest.raises(MolyoloBundleError):
        MolYoloV10Adapter(tmp_path / "missing", device="cpu")
