"""Narrow dependency-inversion ports for concrete adapters."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from tsdaf.lanes.image.detector import RawMoleculeDetection
from tsdaf.lanes.image.models import DetectorProvenance
from tsdaf.lanes.image.ocsr_service import (
    AUTHORIZED_OCSR_SERVICE_CONTRACT,
    OCSRCropRequest,
    OCSRJobOutcome,
    OCSRServiceProvenance,
    RawOCSRTranscription,
)


class MoleculeDetector(Protocol):
    """Provider-neutral molecule detector used by the image runtime."""

    @property
    def provenance(self) -> DetectorProvenance: ...

    def detect(self, image_path: Path) -> tuple[RawMoleculeDetection, ...]: ...


class OCSRService(Protocol):
    """Provider-neutral coordinate-OCSR service used by the image runtime.

    Batch-shaped on purpose: one job may carry many crops, so per-crop and per-image
    call granularity are both expressible without changing this port.
    """

    @property
    def service_contract(self) -> str: ...

    def transcribe(
        self,
        crops: Sequence[OCSRCropRequest],
        *,
        expand_substituents: bool,
    ) -> OCSRJobOutcome: ...


__all__ = (
    "AUTHORIZED_OCSR_SERVICE_CONTRACT",
    "DetectorProvenance",
    "MoleculeDetector",
    "OCSRCropRequest",
    "OCSRJobOutcome",
    "OCSRService",
    "OCSRServiceProvenance",
    "RawMoleculeDetection",
    "RawOCSRTranscription",
)
