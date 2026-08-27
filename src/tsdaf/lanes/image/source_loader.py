"""Verified source-image materialization for authorized image jobs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image, UnidentifiedImageError

from tsdaf.contracts.execution import FailureCode
from tsdaf.contracts.handoff import ImageProcessingJob
from tsdaf.contracts.source import ArtifactRef

_JOB_MEDIA_TYPE = "application/vnd.tsdaf.image-processing-job+json"
_FORMAT_MEDIA_TYPES = {
    "BMP": "image/bmp",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}


class SourceImageLoadError(RuntimeError):
    """Safe source-boundary failure carrying an existing domain code."""

    code: FailureCode
    relative_path: str

    def __init__(self, code: FailureCode, message: str, relative_path: str) -> None:
        super().__init__(message)
        self.code = code
        self.relative_path = relative_path


@dataclass(frozen=True, slots=True)
class LoadedSourceImage:
    job: ImageProcessingJob
    job_artifact: ArtifactRef
    source_artifact: ArtifactRef
    verified_local_path: Path
    dimensions: tuple[int, int]
    detected_media_type: str


@runtime_checkable
class ImageObjectStore(Protocol):
    """Small object-store surface required by the image lane."""

    def put_bytes(self, payload: bytes, *, media_type: str) -> ArtifactRef: ...

    def put_json(self, value: object, *, media_type: str = "application/json") -> ArtifactRef: ...

    def read_bytes(self, ref: ArtifactRef) -> bytes: ...


def _fail(code: FailureCode, message: str, relative_path: str) -> SourceImageLoadError:
    return SourceImageLoadError(code, message, relative_path)


def _image_metadata(payload: bytes, relative_path: str) -> tuple[str, tuple[int, int]]:
    try:
        with Image.open(BytesIO(payload)) as image:
            image_format = image.format
            dimensions = image.size
            image.verify()
    except (OSError, UnidentifiedImageError, ValueError):
        raise _fail(
            FailureCode.PACKAGE_INVALID,
            "The image job source is not a supported image.",
            relative_path,
        ) from None
    media_type = _FORMAT_MEDIA_TYPES.get(str(image_format).upper())
    if media_type is None:
        raise _fail(
            FailureCode.PACKAGE_INVALID,
            "The image job source format is not supported.",
            relative_path,
        )
    return media_type, (int(dimensions[0]), int(dimensions[1]))


def load_source_image(
    *,
    job: ImageProcessingJob,
    package_dir: Path,
    store: ImageObjectStore,
) -> LoadedSourceImage:
    """Resolve, verify, and store the exact source bytes referenced by one job."""

    if not isinstance(job, ImageProcessingJob):
        raise TypeError("job must be an ImageProcessingJob")
    if not isinstance(package_dir, Path):
        raise TypeError("package_dir must be a pathlib.Path")
    if not isinstance(store, ImageObjectStore):
        raise TypeError("store must implement ImageObjectStore")

    source = job.route_image_item.source_asset
    relative_path = source.relative_path
    parts = relative_path.split("/")
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise _fail(
            FailureCode.PACKAGE_INVALID,
            "The image job source path is not a canonical relative path.",
            relative_path,
        )

    root = package_dir.resolve(strict=False)
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise _fail(
            FailureCode.PACKAGE_INVALID,
            "The image job source path escapes the package root.",
            relative_path,
        )
    if candidate.is_symlink():
        raise _fail(
            FailureCode.PACKAGE_INVALID,
            "The image job source cannot be a symbolic link.",
            relative_path,
        )
    if not candidate.is_file():
        raise _fail(
            FailureCode.ASSET_MISSING,
            "The image job source file is missing.",
            relative_path,
        )
    try:
        payload = candidate.read_bytes()
    except OSError:
        raise _fail(
            FailureCode.ASSET_MISSING,
            "The image job source file cannot be read.",
            relative_path,
        ) from None

    if len(payload) != source.byte_size or hashlib.sha256(payload).hexdigest() != source.sha256:
        raise _fail(
            FailureCode.HASH_MISMATCH,
            "The image job source bytes do not match the handoff identity.",
            relative_path,
        )
    detected_media_type, dimensions = _image_metadata(payload, relative_path)
    if detected_media_type != source.media_type:
        raise _fail(
            FailureCode.PACKAGE_INVALID,
            "The image job source media type does not match its bytes.",
            relative_path,
        )
    declared_dimensions = job.route_image_item.dimensions
    if declared_dimensions is not None and declared_dimensions != dimensions:
        raise _fail(
            FailureCode.PACKAGE_INVALID,
            "The image job source dimensions do not match the handoff.",
            relative_path,
        )

    source_artifact = store.put_bytes(payload, media_type=source.media_type)
    job_artifact = store.put_json(
        job.model_dump(mode="json", by_alias=True, exclude_none=False),
        media_type=_JOB_MEDIA_TYPE,
    )
    return LoadedSourceImage(
        job=job,
        job_artifact=job_artifact,
        source_artifact=source_artifact,
        verified_local_path=resolved,
        dimensions=dimensions,
        detected_media_type=detected_media_type,
    )


__all__ = (
    "ImageObjectStore",
    "LoadedSourceImage",
    "SourceImageLoadError",
    "load_source_image",
)
