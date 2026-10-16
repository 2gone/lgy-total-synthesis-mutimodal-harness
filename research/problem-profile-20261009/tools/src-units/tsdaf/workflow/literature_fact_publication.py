"""Publish only fact JSON whose artifact references close to sealed local evidence."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tsdaf.contracts.literature.validation import validate_fact_bundle
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.canonicalizer import canonicalize_paper_package
from tsdaf.lanes.document.paper_identity import pairing_failure
from tsdaf.lanes.literature.source_manifest import LiteratureSources
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner

_PUBLIC_ARTIFACT_KEYS = frozenset({"sha256", "media_type", "byte_size", "logical_name"})
_CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "secret",
        "secret_key",
    }
)
_CREDENTIAL_FIELD_SUFFIXES = (
    "_access_token",
    "_api_key",
    "_auth_token",
    "_client_secret",
    "_password",
    "_private_key",
    "_secret_key",
)
_PRIVATE_CONTROL_FIELD_NAMES = frozenset(
    {
        "exception",
        "exceptions",
        "log",
        "logs",
        "model_log",
        "model_logs",
        "model_raw_output",
        "model_response",
        "provider_response",
        "raw_model_output",
        "raw_model_response",
        "raw_request",
        "raw_response",
        "request_body",
        "request_headers",
        "response_body",
        "response_headers",
        "scratch",
        "scratchpad",
        "stack_trace",
        "traceback",
    }
)
_MACHINE_CONTROL_STRING_FIELDS = (
    "schema_version",
    "result_kind",
    "machine_result_id",
    "paper_id",
    "main_package_revision_id",
    "si_package_revision_ids",
    "coverage",
    "completeness",
    "execution_id",
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z0-9]+_)*(?:access_token|api_key|apikey|auth_token|"
    r"client_secret|password|private_key|secret_key)\s*[:=]\s*[^\s,;]+"
)
_DISTINCTIVE_CREDENTIAL = re.compile(
    r"(?i)(?<![a-z0-9_-])(?:"
    r"sk-proj-[a-z0-9_-]{8,}|sk-[a-z0-9]{20,}|"
    r"gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|"
    r"xox[baprs]-[a-z0-9-]{10,}|akia[0-9a-z]{16}"
    r")(?![a-z0-9_-])"
)
_AUTHORIZATION_CREDENTIAL = re.compile(
    r"(?i)(?<![a-z0-9])authorization\s*:\s*(?:"
    r"bearer\s+[a-z0-9._~+/-]{8,}={0,2}|basic\s+[a-z0-9+/]{8,}={0,2}"
    r")(?![a-z0-9._~+/=-])"
)
_WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![a-z0-9])(?:[a-z]:[\\/][^\s]+|\\\\[^\\/\s]+[\\/][^\s]+)"
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![a-z0-9:/])/(?!/)(?:[^/\s]+/)+[^/\s]+", re.I)
_FILE_URI_PATH = re.compile(r"(?i)\bfile:///(?:[^/\s]+/)+[^/\s]+")


def _public_ref(ref: ArtifactRef, logical_name: str | None) -> dict[str, object]:
    return {
        "sha256": ref.sha256,
        "media_type": ref.media_type,
        "byte_size": ref.byte_size,
        "logical_name": logical_name,
    }


def _artifact_identity(value: Mapping[str, object]) -> tuple[str, str, int]:
    sha256 = value["sha256"]
    media_type = value["media_type"]
    byte_size = value["byte_size"]
    if (
        not isinstance(sha256, str)
        or not isinstance(media_type, str)
        or not isinstance(byte_size, int)
    ):
        raise ValueError("public artifact ref has invalid identity fields")
    return sha256, media_type, byte_size


def _walk(value: object) -> Iterable[Mapping[str, object]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list | tuple):
        for child in value:
            yield from _walk(child)


def _public_artifacts(value: object) -> tuple[Mapping[str, object], ...]:
    return tuple(item for item in _walk(value) if frozenset(item) == _PUBLIC_ARTIFACT_KEYS)


def _source_assets(value: object) -> tuple[Mapping[str, object], ...]:
    refs: list[Mapping[str, object]] = []
    for item in _walk(value):
        for key in ("source_asset", "source_asset_ref"):
            candidate = item.get(key)
            if isinstance(candidate, Mapping) and frozenset(candidate) == _PUBLIC_ARTIFACT_KEYS:
                refs.append(candidate)
        candidates = item.get("source_asset_refs")
        if isinstance(candidates, list):
            refs.extend(
                candidate
                for candidate in candidates
                if isinstance(candidate, Mapping) and frozenset(candidate) == _PUBLIC_ARTIFACT_KEYS
            )
    return tuple(refs)


def _reject_forbidden_structure(value: object) -> None:
    for item in _walk(value):
        if "relative_path" in item:
            raise ValueError("public facts must not contain relative_path")
        normalized_keys = {key.lower().replace("-", "_") for key in item if isinstance(key, str)}
        if normalized_keys & _CREDENTIAL_FIELD_NAMES or any(
            key.endswith(_CREDENTIAL_FIELD_SUFFIXES) for key in normalized_keys
        ):
            raise ValueError("public facts must not contain credential fields")
        if normalized_keys & _PRIVATE_CONTROL_FIELD_NAMES:
            raise ValueError("public facts must not contain private control fields")


def _direct_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list | tuple):
        for item in value:
            if isinstance(item, str):
                yield item


def _nonhash_control_strings(value: object) -> Iterable[str]:
    if not isinstance(value, Mapping):
        return
    for key, item in value.items():
        if isinstance(key, str) and not key.endswith("_sha256"):
            yield from _direct_strings(item)


def _publication_control_strings(
    machine: Mapping[str, object], observations: Iterable[Mapping[str, object]]
) -> Iterable[str]:
    """Route only provenance/control text to leak detection; never scan domain/source text."""

    checked_observations = tuple(observations)
    for value in (machine, *checked_observations):
        for artifact in _public_artifacts(value):
            yield from _direct_strings(artifact.get("logical_name"))
    for field_name in _MACHINE_CONTROL_STRING_FIELDS:
        yield from _direct_strings(machine.get(field_name))
    profiles = machine.get("model_profile_versions")
    if isinstance(profiles, list | tuple):
        for profile in profiles:
            yield from _nonhash_control_strings(profile)
    for observation in checked_observations:
        yield from _direct_strings(observation.get("producer_note"))
        yield from _nonhash_control_strings(observation.get("originator"))
        yield from _nonhash_control_strings(observation.get("verification"))


def _reject_control_text(public_text: str) -> None:
    if (
        _CREDENTIAL_ASSIGNMENT.search(public_text)
        or _DISTINCTIVE_CREDENTIAL.search(public_text)
        or _AUTHORIZATION_CREDENTIAL.search(public_text)
    ):
        raise ValueError("public facts must not contain credentials")
    if (
        _WINDOWS_ABSOLUTE_PATH.search(public_text)
        or _POSIX_ABSOLUTE_PATH.search(public_text)
        or _FILE_URI_PATH.search(public_text)
    ):
        raise ValueError("public facts must not contain an absolute host path")


def _package_refs(package: CanonicalPaperPackage) -> tuple[ArtifactRef, ...]:
    manifest = package.manifest
    return (
        manifest.source_pdf,
        manifest.normalized_document,
        manifest.normalized_blocks,
        *manifest.assets,
    )


@dataclass(frozen=True, init=False)
class FactPublicationAuthority:
    """One immutable authorization closure derived from package and sealed execution evidence."""

    execution_id: str
    # Historical: a layered run (reaction windows, retired 2026-09-23) sealed one
    # terminal execution per layer. Each supporting execution is verified exactly like
    # the primary one, and an observation may bind to any of them -- but the machine
    # result still names only the primary.
    supporting_execution_ids: tuple[str, ...]
    paper_id: str
    canonical_content_sha256: str
    terminal_status: str
    terminal_manifest_ref: ArtifactRef
    artifact_closure: tuple[ArtifactRef, ...]
    review_readable_artifacts: tuple[ArtifactRef, ...]
    document_identities: tuple[tuple[str, str, str], ...]
    _store: LocalObjectStore = field(repr=False, compare=False)

    @property
    def verifying_execution_ids(self) -> frozenset[str]:
        return frozenset({self.execution_id, *self.supporting_execution_ids})

    @classmethod
    def _from_verified(
        cls,
        *,
        execution_id: str,
        supporting_execution_ids: tuple[str, ...],
        paper_id: str,
        canonical_content_sha256: str,
        terminal_status: str,
        terminal_manifest_ref: ArtifactRef,
        artifact_closure: tuple[ArtifactRef, ...],
        review_readable_artifacts: tuple[ArtifactRef, ...],
        document_identities: tuple[tuple[str, str, str], ...],
        store: LocalObjectStore,
    ) -> FactPublicationAuthority:
        authority = object.__new__(cls)
        object.__setattr__(authority, "execution_id", execution_id)
        object.__setattr__(authority, "supporting_execution_ids", supporting_execution_ids)
        object.__setattr__(authority, "paper_id", paper_id)
        object.__setattr__(authority, "canonical_content_sha256", canonical_content_sha256)
        object.__setattr__(authority, "terminal_status", terminal_status)
        object.__setattr__(authority, "terminal_manifest_ref", terminal_manifest_ref)
        object.__setattr__(authority, "artifact_closure", artifact_closure)
        object.__setattr__(authority, "review_readable_artifacts", review_readable_artifacts)
        object.__setattr__(authority, "document_identities", document_identities)
        object.__setattr__(authority, "_store", store)
        return authority


@dataclass(frozen=True)
class PublishedFactSet:
    machine: Mapping[str, object]
    observations: tuple[Mapping[str, object], ...]
    machine_artifact_ref: Mapping[str, object]
    observation_artifact_refs: tuple[Mapping[str, object], ...]
    review_readable_artifacts: tuple[Mapping[str, object], ...]


def build_fact_publication_authority(
    package: CanonicalPaperPackage,
    package_dir: Path,
    terminal_manifest: ArtifactRef,
    store: LocalObjectStore,
    runner: LocalRunner,
    supporting_terminal_manifests: Sequence[ArtifactRef] = (),
    sources: LiteratureSources | None = None,
) -> FactPublicationAuthority:
    """Verify and freeze the only artifact closure permitted for one publication."""

    if not isinstance(package, CanonicalPaperPackage):
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(package_dir, Path):
        raise TypeError("package_dir must be a pathlib.Path")
    if not isinstance(terminal_manifest, ArtifactRef):
        raise TypeError("terminal_manifest must be an ArtifactRef")
    if not isinstance(store, LocalObjectStore) or not isinstance(runner, LocalRunner):
        raise TypeError("store and runner must be local runtime values")
    if runner._object_store is not store:
        raise ValueError("runner and store must be the same local runtime binding")
    try:
        manifest = json.loads(store.read_bytes(terminal_manifest))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("terminal manifest is not readable canonical JSON") from error
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("execution_id"), str):
        raise ValueError("terminal manifest has no execution identity")
    execution_id = manifest["execution_id"]
    status = manifest.get("status")
    if status not in {"SUCCEEDED", "REVIEW_REQUIRED"}:
        raise ValueError(
            "fact publication requires a successful or review-required terminal execution"
        )
    execution_closure = list(
        runner.verified_execution_artifact_closure(execution_id, terminal_manifest)
    )
    supporting_execution_ids: list[str] = []
    for supporting in supporting_terminal_manifests:
        if not isinstance(supporting, ArtifactRef):
            raise TypeError("supporting terminal manifests must be ArtifactRef values")
        try:
            supporting_manifest = json.loads(store.read_bytes(supporting))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("supporting manifest is not readable canonical JSON") from error
        if not isinstance(supporting_manifest, Mapping) or not isinstance(
            supporting_manifest.get("execution_id"), str
        ):
            raise ValueError("supporting manifest has no execution identity")
        # Supporting closures preserve provenance, including accepted observations
        # and diagnostics from a failed investigation. They do not authorize the
        # publication: the assembly above must succeed and FactPublisher still
        # validates every fact and evidence binding. The runtime verifies the real
        # terminal status and its seal; never relabel a failed producer as successful.
        supporting_id = supporting_manifest["execution_id"]
        if supporting_id == execution_id or supporting_id in supporting_execution_ids:
            raise ValueError("supporting executions must be distinct from one another")
        supporting_execution_ids.append(supporting_id)
        execution_closure.extend(
            runner.verified_execution_artifact_closure(supporting_id, supporting)
        )
    materialized_package_refs: list[ArtifactRef] = []
    document_identities: list[tuple[str, str, str]] = []
    registered_packages = [(package, package_dir)]
    if sources is not None:
        if sources.paper_id != package.manifest.paper_id or sources.main.package != package:
            raise ValueError("publication source manifest differs from MAIN authority")
        registered_packages = []
        for document in sources.documents:
            if document.package is None or document.package_root is None:
                continue
            verified = canonicalize_paper_package(document.package_root)
            registration = document.registration
            if (
                verified != document.package
                or verified.canonical_content_sha256 != registration.package_sha256
                or (
                    verified.manifest.paper_id != sources.paper_id
                    if registration.source_phase == "MAIN"
                    else pairing_failure(package.manifest, verified.manifest) is not None
                )
            ):
                raise ValueError("publication document revision changed after registration")
            # A document answers to the revision it was registered under and to its
            # stable revision (E83): a public observation names the latter.
            for revision in {
                registration.document_revision_id,
                verified.stable_revision_id(registration.source_phase),
            }:
                document_identities.append(
                    (revision, registration.source_phase, verified.canonical_content_sha256)
                )
            registered_packages.append((verified, document.package_root))
    original_readable: list[ArtifactRef] = []
    for registered, root in registered_packages:
        original_readable.extend((registered.manifest.source_pdf, *registered.manifest.assets))
        for package_ref in _package_refs(registered):
            path = root / package_ref.relative_path
            if not path.is_file() or path.is_symlink():
                raise ValueError("canonical package artifact cannot materialize from package_dir")
            materialized = store.put_bytes(path.read_bytes(), media_type=package_ref.media_type)
            if (
                materialized.sha256 != package_ref.sha256
                or materialized.byte_size != package_ref.byte_size
            ):
                raise ValueError("canonical package artifact identity does not match package bytes")
            materialized_package_refs.append(materialized)
    all_refs = (*execution_closure, *materialized_package_refs)
    by_identity: dict[tuple[str, str, int], ArtifactRef] = {}
    for ref in all_refs:
        key = (ref.sha256, ref.media_type, ref.byte_size)
        existing = by_identity.get(key)
        if existing is not None and existing != ref:
            raise ValueError("artifact identity resolves to divergent storage references")
        by_identity[key] = ref
    readable: list[ArtifactRef] = []
    for original in original_readable:
        key = (original.sha256, original.media_type, original.byte_size)
        readable.append(by_identity[key])
    return FactPublicationAuthority._from_verified(
        execution_id=execution_id,
        supporting_execution_ids=tuple(supporting_execution_ids),
        paper_id=package.manifest.paper_id,
        canonical_content_sha256=package.canonical_content_sha256,
        terminal_status=status,
        terminal_manifest_ref=terminal_manifest,
        artifact_closure=tuple(by_identity[key] for key in sorted(by_identity)),
        review_readable_artifacts=tuple(readable),
        document_identities=tuple(document_identities),
        store=store,
    )


class FactPublisher:
    """Materialize a public Machine/Observation bundle only after closure verification."""

    def publish(
        self,
        machine_draft: Mapping[str, object],
        observations: Iterable[Mapping[str, object]],
        authority: FactPublicationAuthority,
        store: LocalObjectStore,
        *,
        provenance: Mapping[str, Sequence[str]] | None = None,
    ) -> PublishedFactSet:
        """`provenance` names, per keyed observation id, the sha256 of every sealed
        observation it was derived from (E83): a keyed observation carries no run
        coordinate itself, so its authorization is that those originals lie in the
        authority's closure."""
        if not isinstance(machine_draft, Mapping):
            raise TypeError("machine_draft must be a mapping")
        if not isinstance(authority, FactPublicationAuthority):
            raise TypeError("authority must be a FactPublicationAuthority")
        if not isinstance(store, LocalObjectStore):
            raise TypeError("store must be a LocalObjectStore")
        if store is not authority._store:
            raise ValueError("publication must use the ObjectStore selected by its authority")
        machine = copy.deepcopy(dict(machine_draft))
        checked_observations = tuple(copy.deepcopy(dict(value)) for value in observations)
        _reject_forbidden_structure(machine)
        for observation in checked_observations:
            _reject_forbidden_structure(observation)
        for public_text in _publication_control_strings(machine, checked_observations):
            _reject_control_text(public_text)
        if machine.get("execution_id") != authority.execution_id:
            raise ValueError("machine execution_id is not authorized by the sealed execution")
        domain = machine.get("domain_payload")
        if (
            machine.get("paper_id") != authority.paper_id
            or not isinstance(domain, Mapping)
            or domain.get("paper_id") != authority.paper_id
            or any(
                observation.get("paper_id") != authority.paper_id
                for observation in checked_observations
            )
        ):
            raise ValueError("published facts must bind the canonical package paper_id")
        closure_shas = {ref.sha256 for ref in authority.artifact_closure}
        for observation in checked_observations:
            verification = observation.get("verification")
            revision = observation.get("document_revision_id")
            phase = observation.get("source_phase")
            documents = {r: (p, h) for r, p, h in authority.document_identities}
            if documents:
                expected = documents.get(revision) if isinstance(revision, str) else None
            else:
                expected = (
                    ("MAIN", authority.canonical_content_sha256)
                    if revision == machine.get("main_package_revision_id")
                    else None
                )
            if not isinstance(verification, Mapping) or expected != (
                phase,
                verification.get("verified_package_sha256"),
            ):
                raise ValueError("observation verification does not bind the publication authority")
            if "anchor_key" in observation:
                originals = (provenance or {}).get(str(observation.get("observation_id")), ())
                if not originals or any(sha not in closure_shas for sha in originals):
                    raise ValueError(
                        "keyed observation has no sealed original in the publication authority"
                    )
            elif verification.get("execution_id") not in authority.verifying_execution_ids:
                raise ValueError("observation verification does not bind the publication authority")
        manifest_ref = machine.get("execution_manifest_ref")
        if not isinstance(manifest_ref, Mapping):
            raise ValueError("machine has no public execution manifest ref")
        if _artifact_identity(manifest_ref) != (
            authority.terminal_manifest_ref.sha256,
            authority.terminal_manifest_ref.media_type,
            authority.terminal_manifest_ref.byte_size,
        ):
            raise ValueError("machine execution manifest ref is not in the authorized closure")
        by_identity = {_artifact_identity(ref.model_dump()): ref for ref in authority.artifact_closure}
        def read_public(ref):
            artifact = by_identity.get(_artifact_identity(ref))
            if artifact is None:
                raise ValueError('public scientific artifact is outside publication authority')
            return store.read_bytes(artifact)
        validate_fact_bundle(machine, checked_observations, read_public=read_public)
        from tsdaf.contracts.literature.schema import machine_profile
        if machine_profile(machine).faithful_mapping:
            from tsdaf.contracts.literature.public_science import load_public_science
            bundle = load_public_science(machine, read_public)
            for record in bundle['records'].values():
                if record['producer_receipt_sha256'] not in closure_shas:
                    raise ValueError('public record lacks its authorized producer receipt')
        for ref in authority.artifact_closure:
            store.verify(ref)
        allowed = {
            (ref.sha256, ref.media_type, ref.byte_size) for ref in authority.artifact_closure
        }
        review_readable = {
            (ref.sha256, ref.media_type, ref.byte_size)
            for ref in authority.review_readable_artifacts
        }
        public_artifacts = list(_public_artifacts(machine))
        for observation in checked_observations:
            public_artifacts.extend(_public_artifacts(observation))
        for public_ref in public_artifacts:
            if _artifact_identity(public_ref) not in allowed:
                raise ValueError("public artifact ref cannot materialize in the authorized closure")
        source_assets = list(_source_assets(machine))
        for observation in checked_observations:
            source_assets.extend(_source_assets(observation))
        for source_asset in source_assets:
            if _artifact_identity(source_asset) not in review_readable:
                raise ValueError(
                    "source assets must be canonical-package review-readable artifacts"
                )
        observation_refs = tuple(
            store.put_bytes(
                canonical_json_bytes(observation),
                media_type="application/vnd.tsdaf.source-observation.v1+json",
            )
            for observation in checked_observations
        )
        # The media type is the result's own contract, read off the result (E158): a v1.1
        # payload labelled v1 would be a second place where the version is decided, and
        # the two would part company the first time a profile changed.
        machine_ref = store.put_bytes(
            canonical_json_bytes(machine),
            media_type=f"application/vnd.{machine['schema_version']}+json",
        )
        return PublishedFactSet(
            machine=machine,
            observations=checked_observations,
            machine_artifact_ref=_public_ref(machine_ref, "MachinePaperResult"),
            observation_artifact_refs=tuple(
                _public_ref(ref, f"SourceObservation:{observation['observation_id']}")
                for ref, observation in zip(observation_refs, checked_observations, strict=True)
            ),
            review_readable_artifacts=tuple(
                _public_ref(ref, None) for ref in authority.review_readable_artifacts
            ),
        )
