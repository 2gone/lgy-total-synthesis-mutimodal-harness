"""Run-local admission, rebuilt from authenticated request intents, not SQLite.

Only investigation calls (including their figure reader) are covered. A reservation
covers one provider request and its bounded retries, not future figure turns.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Condition, Lock

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.provider_attempts import PROVIDER_ATTEMPT_MEDIA_TYPE, read_provider_attempts
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.tool_turns import TOOL_REQUEST_INTENT_MEDIA


class BudgetPaused(RuntimeError):
    """Operational pause before a request; no scientific completion or failure."""


class CallBudget:
    def __init__(self, limit: int, *, workspace: Path, execution_ids: tuple[str, ...]) -> None:
        if type(limit) is not int or limit < 0:
            raise ValueError("call limit must be a nonnegative integer")
        if not execution_ids or len(set(execution_ids)) != len(execution_ids):
            raise ValueError("budget requires unique, explicit execution IDs")
        self.limit = limit
        self.execution_ids = frozenset(execution_ids)
        self._lock = Lock()
        self._available = Condition(self._lock)
        self._active = 0
        self._reserved = 0
        self._paused = False
        self._charged: dict[str, int] = {}
        store = LocalObjectStore(workspace)
        self._store = store
        runner = LocalRunner(workspace, store)
        # Complete the fold before any worker can reserve. Unanswered intents count:
        # sending state is uncertain and must never be treated as free capacity.
        for execution_id in execution_ids:
            refs = runner.verified_attempt_artifacts(execution_id, allow_not_started=True)
            intended_requests: set[str] = set()
            intent_counts: Counter[str] = Counter()
            completed_attempts: dict[str, dict[str, int | None]] = {}
            audited_attempts: dict[str, dict[str, int]] = {}
            paired_attempts: dict[str, dict[str, int]] = {}
            for ref in refs:
                if ref.media_type == TOOL_REQUEST_INTENT_MEDIA:
                    record = json.loads(store.read_bytes(ref))
                    intent_counts[record["request_ref"]["sha256"]] += 1
                elif ref.media_type in {
                    "application/vnd.tsdaf.investigation-interaction+json",
                    "application/vnd.tsdaf.literature-v3-interaction+json",
                }:
                    record = json.loads(store.read_bytes(ref))
                    # Tool-action audits share the interaction media type, but are
                    # not provider request/response pairs.
                    if record.get("intent_ref") and record.get("provider_attempt_ref"):
                        intent = ArtifactRef.model_validate(record["intent_ref"])
                        audit_ref = ArtifactRef.model_validate(record["provider_attempt_ref"])
                        if intent not in refs or audit_ref not in refs:
                            raise ValueError("attempt settlement is outside the accepted closure")
                        intended = json.loads(store.read_bytes(intent))
                        if (
                            intent.media_type != TOOL_REQUEST_INTENT_MEDIA
                            or intended["binding"]["execution_id"] != execution_id
                            or record["request_ref"] != intended["request_ref"]
                        ):
                            raise ValueError("attempt settlement is not bound to its intent")
                        request_ref = ArtifactRef.model_validate(record["request_ref"])
                        store.verify(request_ref)
                        audit = read_provider_attempts(
                            store, audit_ref, request_bytes=store.read_bytes(request_ref)
                        )
                        count = len(audit["attempts"])
                        if record.get("transport_attempts", count) != count:
                            raise ValueError("provider usage differs from its attempt audit")
                        paired_attempts.setdefault(intent.sha256, {})[ref.sha256] = count
                    if "request_ref" not in record or "response_ref" not in record:
                        continue
                    attempts = record.get("transport_attempts")
                    completed_attempts.setdefault(record["request_ref"]["sha256"], {})[
                        ref.sha256
                    ] = attempts if type(attempts) is int and attempts > 0 else None
                elif ref.media_type == PROVIDER_ATTEMPT_MEDIA_TYPE:
                    record = json.loads(store.read_bytes(ref))
                    audit = read_provider_attempts(
                        store, ref, request_bytes=canonical_json_bytes(record["request"])
                    )
                    audited_attempts.setdefault(audit["request_sha256"], {})[ref.sha256] = len(
                        audit["attempts"]
                    )
            seen: set[str] = set()
            for ref in refs:
                if ref.media_type == TOOL_REQUEST_INTENT_MEDIA:
                    record = json.loads(store.read_bytes(ref))
                    if record["binding"]["execution_id"] != execution_id:
                        raise ValueError("call intent is bound to another execution")
                    store.verify(ArtifactRef.model_validate(record["request_ref"]))
                    if ref.sha256 in seen:
                        raise ValueError("Repeated historical call intent has ambiguous spending")
                    seen.add(ref.sha256)
                    request_sha = record["request_ref"]["sha256"]
                    actual = completed_attempts.get(request_sha, {})
                    # Wire equality is not call identity. Only one intent and one
                    # accepted pair permit exact attribution. Otherwise each intent
                    # retains its own sealed upper bound, including unanswered calls.
                    attempts = record.get("transport_attempt_limit")
                    audits = audited_attempts.get(request_sha, {})
                    paired = paired_attempts.get(ref.sha256, {})
                    if paired:
                        if len(paired) == 1:
                            audited = next(iter(paired.values()))
                            if type(attempts) is not int or audited > attempts:
                                raise ValueError("provider exceeded its admitted attempt bound")
                            attempts = audited
                    elif intent_counts[request_sha] == 1:
                        if len(audits) == 1 and len(actual) <= 1:
                            audited = next(iter(audits.values()))
                            # A conflicting accepted usage record cannot refund an intent.
                            if not actual or next(iter(actual.values())) == audited:
                                if type(attempts) is int and audited > attempts:
                                    raise ValueError("provider exceeded its admitted attempt bound")
                                attempts = audited
                        elif not audits and len(actual) == 1:
                            attempts = next(iter(actual.values())) or attempts
                    if type(attempts) is not int or attempts < 1:
                        raise ValueError("Historical call has no verified transport-attempt bound")
                    self._charged[ref.sha256] = attempts
                    intended_requests.add(request_sha)
            if any(
                ref.media_type == "application/vnd.tsdaf.literature-v3-interaction-request+json"
                and ref.sha256 not in intended_requests
                for ref in refs
            ):
                raise ValueError(
                    "Historical figure requests lack bound admission intents; "
                    "their spending cannot be reconstructed safely for budgeted resume."
                )

    def require_execution(self, execution_id: str) -> None:
        if execution_id not in self.execution_ids:
            raise ValueError("investigation is outside the preloaded call budget")

    def require_client(self, client: object) -> int:
        attempts = getattr(client, "max_transport_attempts", None)
        if type(attempts) is not int or attempts < 1:
            raise ValueError("budgeted clients must declare a positive transport attempt bound")
        return attempts

    @property
    def used(self) -> int:
        with self._lock:
            return sum(self._charged.values())

    def check_running(self) -> None:
        with self._lock:
            if self._paused:
                raise BudgetPaused("Investigation call budget paused; accepted work is preserved.")

    @contextmanager
    def reserve(self, count: int) -> Iterator[CallReservation]:
        if type(count) is not int or count < 1:
            raise ValueError("reservation must be a positive integer")
        with self._available:
            while self._paused or sum(self._charged.values()) + self._reserved + count > self.limit:
                # Active leases can still settle their in-flight upper bounds or
                # release unused attempt capacity. Contention is not exhaustion.
                if self._paused or count > self.limit or not self._active:
                    self._paused = True
                    self._available.notify_all()
                    raise BudgetPaused(
                        "Investigation call budget paused; accepted work is preserved."
                    )
                self._available.wait()
            self._reserved += count
            self._active += 1
        reservation = CallReservation(self, count)
        try:
            yield reservation
        finally:
            with self._available:
                self._reserved -= reservation.remaining
                reservation.remaining = 0
                self._active -= 1
                self._available.notify_all()


class CallReservation:
    def __init__(self, budget: CallBudget, count: int) -> None:
        self.budget = budget
        self.remaining = count

    def started(self, intent: ArtifactRef, *, attempts: int = 1) -> None:
        """Charge the durably bound intent before transport, even if no reply returns."""
        with self.budget._lock:
            if type(attempts) is not int or attempts < 1 or self.remaining < attempts:
                raise RuntimeError("call exceeded its admitted reservation")
            if intent.sha256 in self.budget._charged:
                raise RuntimeError("an accepted call intent cannot be sent again")
            self.remaining -= attempts
            self.budget._reserved -= attempts
            self.budget._charged[intent.sha256] = attempts

    def settled(self, intent: ArtifactRef, audit_ref: ArtifactRef) -> None:
        """Settle only after the caller durably accepts the exact request's audit.

        Without that evidence the initial upper bound stays charged, including an
        interrupted or ambiguous request. Recovery folds the very same audit.
        """
        store = self.budget._store
        record = json.loads(store.read_bytes(intent))
        request_ref = ArtifactRef.model_validate(record["request_ref"])
        store.verify(request_ref)
        audit = read_provider_attempts(
            store, audit_ref, request_bytes=store.read_bytes(request_ref)
        )
        actual = len(audit["attempts"])
        with self.budget._available:
            upper = self.budget._charged[intent.sha256]
            if actual > upper:
                raise ValueError("provider exceeded its admitted attempt bound")
            self.budget._charged[intent.sha256] = actual
            self.budget._available.notify_all()
