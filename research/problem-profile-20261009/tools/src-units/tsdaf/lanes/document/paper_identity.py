"""Which packages are one paper: DOI pairing of supporting information (ledger E76 (2)).

A supporting-information package arrives from intake as a package of its own, with its
own intake id. What ties it to its main paper is the DOI both manifests carry in their
`identity`; before intake fills DOIs, the fixtures' convention -- the SI carries the
main paper's `paper_id` -- still pairs. Nothing here guesses: a pair that neither rule
makes is refused with the reason, and a corpus pairing that finds no SI, or two, for a
paper writes that down (`si_registry.csv`) rather than picking one.

The rule is written once, here, and read by the runtime (`load_literature_sources`
verifying the SI packages it was given), by the corpus script (`scripts/
pair_si_packages.py`) and by the intake identity probe (E76 (8)).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from tsdaf.contracts.package import PaperManifest
from tsdaf.lanes.document.host_tables import journal_publishes_si_in_main_text

__all__ = (
    "SI_REGISTRY_COLUMNS",
    "SiRegistryRow",
    "SiStatus",
    "normalize_doi",
    "pair_packages",
    "pairing_failure",
)

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_doi(value: str | None) -> str | None:
    """A DOI as a pairing key: no resolver prefix, no surrounding space, lower case
    (DOIs are case-insensitive by definition). None stays None."""
    if value is None:
        return None
    text = _DOI_PREFIX_RE.sub("", value.strip())
    return text.lower() or None


def pairing_failure(main: PaperManifest, si: PaperManifest) -> str | None:
    """Why `si` is not the supporting information of `main`, or None when it is.

    DOI first: when both manifests carry one, they must agree. Without two DOIs, the
    SI must carry the main paper's `paper_id` (the fixtures' convention). A package
    that declares itself MAIN is not an SI whatever its ids say.
    """
    si_phase = si.identity.source_phase if si.identity is not None else None
    if si_phase == "MAIN":
        return "package declares itself MAIN, not SI"
    main_doi = normalize_doi(main.identity.doi if main.identity is not None else None)
    si_doi = normalize_doi(si.identity.doi if si.identity is not None else None)
    if main_doi is not None and si_doi is not None:
        if main_doi != si_doi:
            return f"SI DOI {si_doi} differs from MAIN DOI {main_doi}"
        return None
    if si.paper_id != main.paper_id:
        return (
            "SI package does not name the main paper: no DOI on both sides to pair by, "
            f"and its paper_id {si.paper_id!r} is not {main.paper_id!r}"
        )
    return None


# PAIRED: exactly one SI carries the paper's DOI and no other main paper does.
# PAIRED_UNVERIFIED: an SI carries the DOI, but so does another main paper -- M1 found
# the corpus's duplicate DOIs to be *different* papers (bryostatin 1 vs 16), so the SI
# is named, not trusted; the identity probe (E76 (8)) promotes it to PAIRED or not,
# and a batch reads only PAIRED rows. NOT_FOUND: no SI, or two (the note names them).
# NOT_APPLICABLE: the journal prints the experimental part in the main text.
SiStatus = Literal["PAIRED", "PAIRED_UNVERIFIED", "NOT_FOUND", "NOT_APPLICABLE"]

# The registry a corpus pairing writes, one row per main paper (E76 (2); the user's
# 2026-09-13 decision: a paper without SI is registered, never silently MAIN_ONLY).
SI_REGISTRY_COLUMNS = ("paper_id", "doi", "si_status", "si_package_sha256", "note")


@dataclass(frozen=True, slots=True)
class SiRegistryRow:
    paper_id: str
    doi: str | None
    si_status: SiStatus
    si_package_sha256: str | None
    note: str

    def as_record(self) -> dict[str, str]:
        return {
            "paper_id": self.paper_id,
            "doi": self.doi or "",
            "si_status": self.si_status,
            "si_package_sha256": self.si_package_sha256 or "",
            "note": self.note,
        }


def pair_packages(
    mains: Mapping[str, PaperManifest],
    sis: Mapping[str, PaperManifest],
    *,
    si_hashes: Mapping[str, str],
) -> tuple[list[SiRegistryRow], list[str]]:
    """Pair every main package with its SI by DOI; the registry rows and the SIs no
    main paper claimed.

    `mains` and `sis` map a package key (its directory name) to its manifest;
    `si_hashes` maps an SI key to its canonical content hash. A DOI that more than one
    main paper carries pairs as PAIRED_UNVERIFIED (the note names the papers that share
    it): the corpus's duplicate DOIs are different papers more often than copies, so
    the identity probe decides. A DOI with two SI candidates is not paired -- the row
    says so -- and a journal the Host's table lists as publishing the experimental
    part in the main text registers NOT_APPLICABLE.
    """
    si_by_doi: dict[str, list[str]] = {}
    for key, manifest in sis.items():
        doi = normalize_doi(manifest.identity.doi if manifest.identity is not None else None)
        if doi is not None:
            si_by_doi.setdefault(doi, []).append(key)
    mains_by_doi: dict[str, list[str]] = {}
    for key, manifest in mains.items():
        doi = normalize_doi(manifest.identity.doi if manifest.identity is not None else None)
        if doi is not None:
            mains_by_doi.setdefault(doi, []).append(key)
    claimed: set[str] = set()
    rows: list[SiRegistryRow] = []
    for key in sorted(mains):
        manifest = mains[key]
        identity = manifest.identity
        doi = normalize_doi(identity.doi if identity is not None else None)
        journal = identity.journal if identity is not None else None
        candidates = sorted(si_by_doi.get(doi, ())) if doi is not None else []
        if len(candidates) == 1:
            (si_key,) = candidates
            claimed.add(si_key)
            sharing = sorted(k for k in mains_by_doi[doi] if k != key) if doi else []
            if sharing:
                rows.append(
                    SiRegistryRow(
                        manifest.paper_id,
                        doi,
                        "PAIRED_UNVERIFIED",
                        si_hashes[si_key],
                        f"{si_key}; DOI shared with " + ", ".join(sharing),
                    )
                )
            else:
                rows.append(
                    SiRegistryRow(manifest.paper_id, doi, "PAIRED", si_hashes[si_key], si_key)
                )
        elif candidates:
            rows.append(
                SiRegistryRow(
                    manifest.paper_id,
                    doi,
                    "NOT_FOUND",
                    None,
                    "AMBIGUOUS: " + ", ".join(candidates),
                )
            )
        elif journal is not None and journal_publishes_si_in_main_text(journal):
            rows.append(
                SiRegistryRow(manifest.paper_id, doi, "NOT_APPLICABLE", None, f"journal: {journal}")
            )
        else:
            rows.append(
                SiRegistryRow(
                    manifest.paper_id,
                    doi,
                    "NOT_FOUND",
                    None,
                    "no DOI to pair by" if doi is None else "no SI package with this DOI",
                )
            )
    orphans = sorted(set(sis) - claimed)
    return rows, orphans


def registry_records(rows: Iterable[SiRegistryRow]) -> list[dict[str, str]]:
    return [row.as_record() for row in rows]
