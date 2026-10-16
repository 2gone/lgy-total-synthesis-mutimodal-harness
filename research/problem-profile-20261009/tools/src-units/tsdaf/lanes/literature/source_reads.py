"""Task-scoped package validation and exact reads of the requested source files."""

from __future__ import annotations

import hashlib
import os
import stat
from io import BytesIO
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
from tsdaf.lanes.document.package_loader import PackageLoadError
from tsdaf.lanes.literature.source_inventory_models import LiteratureSourceInventory
from tsdaf.lanes.literature.source_scope import (
    VerifiedWorkUnitInput,
    VisualSourceSlice,
    _visual_slices,
    resolve_fusion_read_authority,
)

_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SUPPORTED_IMAGES = {"image/jpeg": "JPEG"}


class SourceReader:
    """Bind one immutable package revision; recheck actual files on explicit reads.

    The package is reconstructed once when this reader is opened. Text reads verify
    the normalized text artifact; image reads verify their path, bytes and image shape.
    No file bytes are cached between model reads. Callers may reuse a returned payload
    for evidence binding and delivery of that same read.
    """

    def __init__(self, package: CanonicalPaperPackage, package_root: Path):
        if type(package) is not CanonicalPaperPackage:
            raise TypeError("package must be a CanonicalPaperPackage")
        if not isinstance(package_root, Path):
            raise TypeError("package_root must be a pathlib.Path")
        try:
            reloaded = canonicalize_paper_package(package_root)
        except PackageLoadError as error:
            raise ValueError("package root bytes do not match the canonical package") from error
        if reloaded != package:
            raise ValueError("package root does not match the supplied canonical package")
        self.package = reloaded
        self._root = package_root
        self._visuals = {visual.visual_id: visual for visual in reloaded.visual_assets}
        self._text_ref = reloaded.manifest.normalized_blocks

    def visual_source(self, visual_id: str) -> VisualSourceSlice:
        """Source metadata only; callers must authorize and verify bytes before display."""
        if visual_id not in self._visuals:
            raise ValueError("visual id is absent from the registered canonical package")
        return _visual_slices(self.package, (visual_id,))[0]

    def verify_text(self) -> None:
        """Verify the actual text file underlying this package's numbered sentences."""
        _read_contained_artifact_bytes(self._root, self._text_ref)

    def read_image(
        self,
        visual_id: str,
        *,
        expected: VisualSourceSlice | None = None,
        required_mode: str | None = None,
    ) -> bytes:
        """Read one registered image; the caller supplies its task's source permission."""
        if visual_id not in self._visuals:
            raise ValueError("visual id is absent from the registered canonical package")
        source = _visual_slices(self.package, (visual_id,))[0]
        if expected is not None and expected != source:
            raise ValueError("authorized visual slice differs from canonical source metadata")
        visual = self._visuals[visual_id]
        if visual.asset not in self.package.manifest.assets:
            raise ValueError("authorized visual asset is absent from package inventory")
        payload = _read_contained_artifact_bytes(self._root, visual.asset)
        supported = _SUPPORTED_IMAGES.get(source.source_asset.media_type)
        if supported is None:
            raise ValueError("authorized visual media type is unsupported")
        try:
            with Image.open(BytesIO(payload)) as image:
                image_format, image_mode, dimensions = image.format, image.mode, image.size
                image.load()
        except (OSError, UnidentifiedImageError, ValueError):
            raise ValueError("authorized visual bytes are not a valid supported image") from None
        if image_format != supported:
            raise ValueError("authorized visual format does not match its media type")
        if required_mode is not None and image_mode != required_mode:
            raise ValueError("authorized visual color mode is unsupported or mismatched")
        if dimensions != source.dimensions:
            raise ValueError("authorized visual dimensions do not match canonical metadata")
        return payload

    def read_visuals(
        self,
        *,
        work_unit: VerifiedWorkUnitInput,
        visual_ids: tuple[str, ...],
        source_inventory: LiteratureSourceInventory | None = None,
    ) -> dict[str, bytes]:
        if type(work_unit) is not VerifiedWorkUnitInput:
            raise TypeError("work_unit must be a VerifiedWorkUnitInput")
        work_unit = VerifiedWorkUnitInput.model_validate(work_unit.model_dump(mode="python"))
        authority = resolve_fusion_read_authority(
            work_unit=work_unit, package=self.package, source_inventory=source_inventory
        )
        sources = {source.visual_id: source for source in work_unit.visual_sources}
        if any(
            visual_id not in authority.visual_ids
            or (source_inventory is None and visual_id not in sources)
            for visual_id in visual_ids
        ):
            raise ValueError("visual id is not authorized by this work unit")
        return {
            visual_id: self.read_image(
                visual_id,
                expected=sources[visual_id] if source_inventory is None else None,
                required_mode="RGB",
            )
            for visual_id in dict.fromkeys(visual_ids)
        }


def _is_reparse(stat_result: os.stat_result) -> bool:
    return stat.S_ISLNK(stat_result.st_mode) or bool(
        getattr(stat_result, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat.S_IFMT(stat_result.st_mode),
    )


def _inspect_contained_regular_file(
    root: Path,
    artifact: ArtifactRef,
) -> tuple[Path, tuple[tuple[int, int, int], ...], os.stat_result]:
    try:
        root_stat = root.lstat()
    except OSError as error:
        raise ValueError("canonical package root cannot be inspected") from error
    if _is_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("canonical package root is not a real directory")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ValueError("canonical package root cannot be resolved") from error
    current = resolved_root
    path_identities = [_file_identity(root_stat)]
    parts = PurePosixPath(artifact.relative_path).parts
    current_stat = root_stat
    for index, part in enumerate(parts):
        current /= part
        try:
            current_stat = current.lstat()
        except OSError as error:
            raise ValueError("authorized visual asset is missing") from error
        if _is_reparse(current_stat):
            raise ValueError("authorized visual asset contains a symlink or reparse point")
        path_identities.append(_file_identity(current_stat))
        if index < len(parts) - 1:
            if not stat.S_ISDIR(current_stat.st_mode):
                raise ValueError("authorized visual asset parent is not a directory")
        elif not stat.S_ISREG(current_stat.st_mode):
            raise ValueError("authorized visual asset is not a regular file")
    try:
        current.resolve(strict=True).relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise ValueError("authorized visual asset escapes canonical package root") from error
    return current, tuple(path_identities), current_stat


def _validate_open_handle(stat_result: os.stat_result) -> None:
    if _is_reparse(stat_result) or not stat.S_ISREG(stat_result.st_mode):
        raise ValueError("authorized visual handle is not a real regular file")


def _read_contained_artifact_bytes(root: Path, artifact: ArtifactRef) -> bytes:
    """Read from one handle while rejecting static and observable path redirection."""

    asset_path, path_identities, _ = _inspect_contained_regular_file(root, artifact)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(asset_path, flags)
    except OSError as error:
        raise ValueError("authorized visual asset cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        _validate_open_handle(before)
        opened_path, opened_identities, opened_path_stat = _inspect_contained_regular_file(
            root, artifact
        )
        if opened_path != asset_path or opened_identities != path_identities:
            raise ValueError("authorized visual path changed while its handle was opened")
        if _file_identity(opened_path_stat) != _file_identity(before):
            raise ValueError("authorized visual handle differs from its contained path")

        chunks: list[bytes] = []
        remaining = artifact.byte_size + 1
        while remaining:
            try:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
            except OSError as error:
                raise ValueError("authorized visual handle cannot be read") from error
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)

        after = os.fstat(descriptor)
        _validate_open_handle(after)
        closed_path, closed_identities, closed_path_stat = _inspect_contained_regular_file(
            root, artifact
        )
        if closed_path != asset_path or closed_identities != opened_identities:
            raise ValueError("authorized visual path changed while its handle was read")
        if _file_identity(closed_path_stat) != _file_identity(after):
            raise ValueError("authorized visual handle no longer matches its contained path")
        if _file_identity(after) != _file_identity(before) or after.st_size != before.st_size:
            raise ValueError("authorized visual handle identity or size was not stable")
    except OSError as error:
        raise ValueError("authorized visual handle cannot be inspected") from error
    finally:
        os.close(descriptor)

    if len(payload) != artifact.byte_size:
        raise ValueError("authorized visual asset final bytes have the wrong size")
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ValueError("authorized visual asset final bytes have the wrong hash")
    return payload
