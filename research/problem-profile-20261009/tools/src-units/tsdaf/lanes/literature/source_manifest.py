"""Revision-scoped, verified sources for detail and narrative investigation.

Package-local identifiers are opaque: an SI package may also contain MAIN-P... IDs.
The registered revision, role and exact package hash establish source identity.

The Host derives a registration from a package directory (ledger E76 (2)): the
revision id from the package's content, the phase from where the package was given,
the pairing with the main paper from the DOI both manifests carry. A hand-written
source manifest is still read, for the shapes it alone can express (an attachment
registered as unavailable, an inactive revision).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.reading_units import ReadingUnits
from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
from tsdaf.lanes.document.package_loader import PackageLoadError
from tsdaf.lanes.document.paper_identity import pairing_failure
from tsdaf.lanes.literature.enrichment_values import SourceRead
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans

SourcePhase = Literal["MAIN", "SI"]


def stable_revision_id(phase: SourcePhase, package: CanonicalPaperPackage) -> str:
    """A document revision id that is a function of the document (ledger E66 (4), E79):
    the form lives on the contract (`CanonicalPaperPackage.stable_revision_id`), so the
    validators read the same rule the runtime mints by."""
    return package.stable_revision_id(phase)


class DocumentRegistration(ContractModel):
    document_revision_id: str = Field(min_length=1)
    source_phase: SourcePhase
    attachment_id: str = Field(min_length=1)
    active: bool = True
    # The Host computes the hash of a located package when a manifest leaves it out,
    # and holds the package to it when a manifest gives one (E76 (2)).
    package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    package_dir: str | None = None
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def _available(self) -> Self:
        if self.package_dir is None and not self.unavailable_reason:
            raise ValueError("an unlocated source requires an unavailable reason")
        return self


def derive_registration(
    package: CanonicalPaperPackage,
    root: Path,
    *,
    phase: SourcePhase,
    attachment_id: str,
    revision: str | None = None,
) -> DocumentRegistration:
    """The registration a package directory implies: located, active, hashed, with the
    stable revision id unless the caller names one (MAIN's CLI label, until E66 (4)
    lands)."""
    return DocumentRegistration(
        document_revision_id=revision or stable_revision_id(phase, package),
        source_phase=phase,
        attachment_id=attachment_id,
        package_sha256=package.canonical_content_sha256,
        package_dir=str(root),
    )


class LiteratureSourceManifest(ContractModel):
    schema_version: Literal["tsdaf.literature-sources.v1"] = "tsdaf.literature-sources.v1"
    paper_id: str = Field(min_length=1)
    documents: tuple[DocumentRegistration, ...]

    @model_validator(mode="after")
    def _identities(self) -> Self:
        revisions = [d.document_revision_id for d in self.documents]
        if len(set(revisions)) != len(revisions):
            raise ValueError("document revision IDs must be unique")
        main = [d for d in self.documents if d.source_phase == "MAIN"]
        if len(main) != 1 or not main[0].active:
            raise ValueError("exactly one active MAIN registration is required")
        active = [(d.source_phase, d.attachment_id) for d in self.documents if d.active]
        if len(set(active)) != len(active):
            raise ValueError("an attachment may have only one active revision")
        return self


@dataclass(frozen=True, slots=True)
class PaperSourceStatus:
    """What the Host knows about a paper's sources before any model runs, and tells
    the route group in its own words (ledger E76 (5), E83): whether a supporting
    information is registered for the paper, and whether the main text has an
    experimental part -- from the sealed partition when available; None until a
    runtime that holds the package decides. The defaults describe every run before
    E83, so nothing that does not pass a status changes behavior."""

    si_registered: bool = True
    has_experimental_part: bool | None = None


# The status every caller that does not pass one gets: the pre-E83 assumptions.
UNKNOWN_SOURCE_STATUS = PaperSourceStatus()


@dataclass(frozen=True, slots=True)
class VerifiedSourceDocument:
    registration: DocumentRegistration
    package: CanonicalPaperPackage | None
    package_root: Path | None
    failure: str | None = None
    # The part of the cleaned document an investigation may list and read; None means
    # all of it. The detail lane's part is derived from upstream experimental units;
    # it is not the contiguous text after the first experimental block.
    readable_block_ids: tuple[str, ...] | None = None
    # The document read once into units (E136): where each compound's procedure, data
    # and crystal structure are, by the label the author printed. None on a run whose
    # Router execution predates the stage, or an SI that was not read.
    units: ReadingUnits | None = None
    # Authorized text beyond that part: upstream narrative units across routes and
    # exact sources of inherited facts (E142). Kept apart
    # from `readable_block_ids` so what is said about the experimental part -- its size,
    # whether there is one -- stays true of the experimental part.
    step_block_ids: tuple[str, ...] = ()

    def identity(self) -> dict[str, object]:
        return {
            **self.registration.model_dump(mode="json", exclude={"package_dir"}),
            "failure": self.failure,
        }

    @property
    def part_ids(self) -> tuple[str, ...]:
        """The document's readable part alone: MAIN's experimental part for the detail
        lane, an SI's part from its cut, or all of it."""
        if self.package is None:
            return ()
        if self.readable_block_ids is None:
            return self.package.clean_view.included_block_ids
        return self.readable_block_ids

    @property
    def readable_ids(self) -> tuple[str, ...]:
        """Everything the investigation may list and read: the part, and the step
        blocks beyond it, in the document's order."""
        part = self.part_ids
        if not self.step_block_ids or self.package is None:
            return part
        wanted = set(part) | set(self.step_block_ids)
        return tuple(b.block_id for b in self.package.blocks if b.block_id in wanted)


def general_method_targets(
    documents: Iterable[VerifiedSourceDocument],
) -> dict[str, dict[str, str]]:
    """Exact readable method-unit targets in the supplied verified document scope."""
    from tsdaf.contracts.literature.procedure_targets import source_catalog_location
    from tsdaf.lanes.literature.stage_ledger import general_methods_first_blocks

    records = tuple(documents)
    first_blocks = general_methods_first_blocks(
        (document.registration.document_revision_id, document.units) for document in records
    )
    targets = {}
    for document in records:
        if document.package is None or not document.registration.active:
            continue
        revision = document.registration.document_revision_id
        readable = set(document.readable_ids)
        for block in document.package.blocks:
            if block.block_id not in readable or (revision, block.block_id) not in first_blocks:
                continue
            targets[source_catalog_location(revision, block.block_id)] = {
                "document_id": block.source.document_id,
                "document_revision_id": revision,
                "unit_block_id": block.block_id,
            }
    return targets


@dataclass(frozen=True, slots=True)
class LiteratureSources:
    paper_id: str
    documents: tuple[VerifiedSourceDocument, ...]

    @property
    def main(self) -> VerifiedSourceDocument:
        return next(d for d in self.documents if d.registration.source_phase == "MAIN")

    @property
    def si_registered(self) -> bool:
        """Whether at least one supporting information is registered and available."""
        return any(document.package is not None for document in self.active_si)

    @property
    def active_si(self) -> tuple[VerifiedSourceDocument, ...]:
        return tuple(
            d
            for d in self.documents
            if d.registration.source_phase == "SI" and d.registration.active
        )

    def active(self) -> tuple[VerifiedSourceDocument, ...]:
        """MAIN and every active SI. A lane reads what it was given: which documents is
        its kind's (`of_phases`), and which semantic units of each are decided where
        its sources are assembled."""
        return (self.main, *self.active_si)

    def of_phases(self, phases: tuple[SourcePhase, ...]) -> LiteratureSources:
        """The same sources with only the documents of these phases: what a task of one
        kind reads (`investigation_kinds.KindSpec.phases`)."""
        return replace(
            self,
            documents=tuple(d for d in self.documents if d.registration.source_phase in phases),
        )

    def with_readable(self, readable: Mapping[str, tuple[str, ...]]) -> LiteratureSources:
        """The same sources with the documents named by revision narrowed to the given
        cleaned blocks (ledger E81: MAIN's experimental part for the detail lane, an SI's
        part before or after its own cut for either lane). A revision not named keeps
        what it had; an unavailable one cannot be narrowed."""
        by_revision = {d.registration.document_revision_id: d for d in self.documents}
        unknown = set(readable) - set(by_revision)
        if unknown:
            raise ValueError(f"readable block ids name unregistered revisions: {sorted(unknown)}")
        narrowed: list[VerifiedSourceDocument] = []
        for document in self.documents:
            block_ids = readable.get(document.registration.document_revision_id)
            if block_ids is None:
                narrowed.append(document)
                continue
            if document.package is None:
                raise ValueError(
                    f"{document.registration.document_revision_id} package is not available"
                )
            if not set(block_ids) <= set(document.package.clean_view.included_block_ids):
                raise ValueError(
                    "readable block ids must be cleaned blocks of "
                    + document.registration.document_revision_id
                )
            narrowed.append(replace(document, readable_block_ids=tuple(block_ids)))
        return LiteratureSources(self.paper_id, tuple(narrowed))

    def with_units(self, units: Mapping[str, ReadingUnits | None]) -> LiteratureSources:
        """The same sources with each named revision carrying its reading units. A
        revision not named keeps what it had; units for an unregistered revision are a
        caller's error."""
        by_revision = {d.registration.document_revision_id: d for d in self.documents}
        unknown = set(units) - set(by_revision)
        if unknown:
            raise ValueError(f"reading units name unregistered revisions: {sorted(unknown)}")
        return replace(
            self,
            documents=tuple(
                replace(document, units=units[document.registration.document_revision_id])
                if document.registration.document_revision_id in units
                else document
                for document in self.documents
            ),
        )

    def with_step_blocks(self, step_blocks: Mapping[str, tuple[str, ...]]) -> LiteratureSources:
        """The same sources with the named revisions readable at these blocks beyond
        their part (E142). A revision not named keeps what it had; the blocks must be
        cleaned blocks of a loaded package."""
        by_revision = {d.registration.document_revision_id: d for d in self.documents}
        unknown = set(step_blocks) - set(by_revision)
        if unknown:
            raise ValueError(f"step blocks name unregistered revisions: {sorted(unknown)}")
        widened: list[VerifiedSourceDocument] = []
        for document in self.documents:
            block_ids = step_blocks.get(document.registration.document_revision_id)
            if block_ids is None:
                widened.append(document)
                continue
            if document.package is None:
                raise ValueError(
                    f"{document.registration.document_revision_id} package is not available"
                )
            if not set(block_ids) <= set(document.package.clean_view.included_block_ids):
                raise ValueError(
                    "step blocks must be cleaned blocks of "
                    + document.registration.document_revision_id
                )
            widened.append(replace(document, step_block_ids=tuple(block_ids)))
        return LiteratureSources(self.paper_id, tuple(widened))

    def with_main_readable(self, block_ids: tuple[str, ...]) -> LiteratureSources:
        """The same sources with MAIN narrowed to `block_ids`, a subset of its clean view."""
        main = self.main
        if main.package is None:
            raise ValueError("MAIN package is not available")
        return self.with_readable({main.registration.document_revision_id: block_ids})

    def document(self, revision: str) -> VerifiedSourceDocument:
        for document in self.active():
            if document.registration.document_revision_id == revision:
                if document.package is None:
                    raise ValueError(
                        "registered source is unavailable: " + (document.failure or "missing")
                    )
                return document
        raise ValueError("source revision is not authorized for this investigation")

    def catalog(self, *, allowed_main_visual_ids: tuple[str, ...] = ()) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for doc in self.active():
            package = doc.package
            item = doc.identity()
            if package is not None:
                included = set(doc.readable_ids)
                item["text"] = [
                    {
                        "source_id": block.block_id,
                        "page": block.page_index,
                        "section_path": list(block.section_path),
                        "sentence_count": len(block_sentence_spans(block.raw_text)),
                        "preview": block.raw_text[:240],
                        "read_selection": SourceRead(
                            selection_id=f"{doc.registration.document_revision_id}:{block.block_id}",
                            document_revision_id=doc.registration.document_revision_id,
                            source_id=block.block_id,
                        ).model_dump(mode="json", exclude_unset=True),
                    }
                    for block in package.blocks
                    if block.block_id in included
                ]
                item["visuals"] = [
                    {
                        "source_id": v.visual_id,
                        "page": v.page_index,
                        "caption": v.native_caption,
                        "sha256": v.asset.sha256,
                        "read_selection": SourceRead(
                            selection_id=f"{doc.registration.document_revision_id}:{v.visual_id}",
                            document_revision_id=doc.registration.document_revision_id,
                            source_id=v.visual_id,
                        ).model_dump(mode="json", exclude_unset=True),
                    }
                    for v in package.visual_assets
                    if doc.registration.source_phase != "MAIN"
                    or v.visual_id in allowed_main_visual_ids
                ]
            result.append(item)
        return result


def _derived_si_registrations(
    main_package: CanonicalPaperPackage, si_roots: tuple[Path, ...]
) -> tuple[DocumentRegistration, ...]:
    """One registration per SI directory given, in the order given: `SI-1`, `SI-2`...
    A directory that does not load is registered as unavailable with the loader's
    reason -- the obligation stays visible -- and never as absence."""
    registrations: list[DocumentRegistration] = []
    for index, root in enumerate(si_roots, start=1):
        attachment_id = f"SI-{index}"
        try:
            package = canonicalize_paper_package(root)
        except (OSError, PackageLoadError) as error:
            reason = str(error) if isinstance(error, PackageLoadError) else type(error).__name__
            registrations.append(
                DocumentRegistration(
                    document_revision_id=f"SI-UNAVAILABLE-{index}",
                    source_phase="SI",
                    attachment_id=attachment_id,
                    unavailable_reason=reason,
                )
            )
            continue
        registrations.append(
            derive_registration(package, root, phase="SI", attachment_id=attachment_id)
        )
    return tuple(registrations)


def load_literature_sources(
    *,
    main_package: CanonicalPaperPackage,
    main_root: Path,
    main_revision: str,
    manifest_path: Path | None = None,
    si_roots: tuple[Path, ...] = (),
) -> LiteratureSources:
    """The verified sources of one paper: MAIN as the Router bound it, and its SI.

    The SI comes one of two ways, never both: `si_roots`, package directories the Host
    derives registrations from (E76 (2)); or `manifest_path`, a hand-written manifest
    whose MAIN must match the Router-bound input. Either way every SI package is held
    to the pairing rule (`paper_identity.pairing_failure`) and, when a manifest names
    a hash, to that hash.
    """
    if manifest_path is not None and si_roots:
        raise ValueError("SI packages come from a source manifest or from --si-package, not both")
    main_registration = derive_registration(
        main_package, main_root, phase="MAIN", attachment_id="MAIN", revision=main_revision
    )
    if manifest_path is None:
        manifest = LiteratureSourceManifest(
            paper_id=main_package.manifest.paper_id,
            documents=(main_registration, *_derived_si_registrations(main_package, si_roots)),
        )
        origin = Path.cwd()
    else:
        manifest = LiteratureSourceManifest.model_validate_json(manifest_path.read_bytes())
        origin = manifest_path.resolve().parent
    if manifest.paper_id != main_package.manifest.paper_id:
        raise ValueError("source manifest belongs to another paper")
    documents: list[VerifiedSourceDocument] = []
    for registration in manifest.documents:
        location = Path(registration.package_dir) if registration.package_dir is not None else None
        if location is not None and not location.is_absolute():
            location = origin / location
        if registration.source_phase == "MAIN":
            if (
                registration.document_revision_id != main_revision
                or registration.package_sha256 not in (None, main_package.canonical_content_sha256)
                or location is None
                or location.resolve() != main_root.resolve()
            ):
                raise ValueError("source manifest MAIN differs from the Router-bound input")
            loaded = canonicalize_paper_package(location)
            if loaded != main_package:
                raise ValueError("MAIN package changed after Router binding")
            if registration.package_sha256 is None:
                registration = registration.model_copy(
                    update={"package_sha256": main_package.canonical_content_sha256}
                )
            documents.append(VerifiedSourceDocument(registration, loaded, location))
            continue
        if location is None:
            documents.append(
                VerifiedSourceDocument(
                    registration,
                    None,
                    location,
                    registration.unavailable_reason or "INACTIVE_REVISION",
                )
            )
            continue
        try:
            package = canonicalize_paper_package(location)
            failure = pairing_failure(main_package.manifest, package.manifest)
            if failure is not None:
                raise ValueError(failure)
            if registration.package_sha256 is None:
                registration = registration.model_copy(
                    update={"package_sha256": package.canonical_content_sha256}
                )
            elif package.canonical_content_sha256 != registration.package_sha256:
                raise ValueError("SI package hash differs from registered revision")
        except (OSError, PackageLoadError, ValueError) as error:
            # A registered but failed SI remains a visible obligation, never absence.
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            documents.append(VerifiedSourceDocument(registration, None, location, reason))
        else:
            documents.append(VerifiedSourceDocument(registration, package, location))
    return LiteratureSources(manifest.paper_id, tuple(documents))
