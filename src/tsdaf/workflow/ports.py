"""Narrow dependency-inversion ports for concrete adapters."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from tsdaf.lanes.image.detector import RawMoleculeDetection
from tsdaf.lanes.image.models import DetectorProvenance


class MoleculeDetector(Protocol):
    """Provider-neutral molecule detector used by the image runtime."""

    @property
    def provenance(self) -> DetectorProvenance: ...

    def detect(self, image_path: Path) -> tuple[RawMoleculeDetection, ...]: ...


__all__ = ("DetectorProvenance", "MoleculeDetector", "RawMoleculeDetection")
