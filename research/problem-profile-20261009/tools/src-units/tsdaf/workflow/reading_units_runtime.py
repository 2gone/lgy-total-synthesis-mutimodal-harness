"""Local runner for the reading-units stage: one sealed division per package.

Runs after cleaning and before the Router, in the Router's execution, and once per
supplement in the literature run. The stage is keyed on the package, the cleaning
version, the prompt, the schema and the model profile, so a package the stage has
seen is served from the cache; an uncached package needs an explicitly live run, like
every other model stage here. The model's answer is checked against the package
(`resolve_reading_units_answer`) and, when it names a handle the rows do not carry or
two units that overlap, shown that sentence once for a corrective turn; a second bad
answer fails the stage as a Router reference failure rather than inventing a division.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from tsdaf.contracts.execution import (
    ExecutionStatus,
    FailureCode,
    ProviderInteractionError,
    StageResult,
    TypedFailure,
    failure_is_retryable,
)
from tsdaf.contracts.package import CanonicalPaperPackage
from tsdaf.contracts.reading_units import (
    READING_UNITS_MEDIA_TYPE,
    ReadingUnits,
    UnitKind,
    answer_shape_sha256,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.document.page_furniture import PAGE_FURNITURE_VERSION
from tsdaf.lanes.document.reading_units import (
    READING_UNITS_STAGE_PROFILE,
    ReadingUnitsAnswer,
    UnitAnswer,
    accept_reading_units_part,
    build_reading_units_part,
    build_reading_units_request,
    load_reading_units_resources,
    reading_units_form,
    resolve_reading_units_answer,
)
from tsdaf.lanes.literature.provider_port import AUTHORIZED_MODEL_PROFILE, LaneInteractionClient
from tsdaf.lanes.provider_attempts import seal_provider_attempts
from tsdaf.workflow.local_runtime import (
    LocalObjectStore,
    LocalRunner,
    LocalRuntimeError,
    stage_content_key,
)

__all__ = (
    "READING_UNITS_STAGE_NAME",
    "read_reading_units",
    "reading_units_stage_identity",
    "run_reading_units_stage",
)

READING_UNITS_STAGE_NAME = "reading-units"
_STAGE_VERSION = "1.0.0"
_PACKAGE_MEDIA_TYPE = "application/vnd.tsdaf.canonical-paper-package+json"
_INTERACTION_MEDIA_TYPE = "application/json"
# One answer, one correction: a model that cannot copy handles from the rows in two
# turns is not going to on the third, and the failure names the handle it gave.
_MAX_TURNS = 2
# A part cut at the output ceiling is halved down to this many rows, no further.
_SMALLEST_PART_ROWS = 8
def _whole_document(
    package: CanonicalPaperPackage,
    source_phase: Literal["MAIN", "SI"],
    answer: ReadingUnitsAnswer,
    prepared: Any,
    identity: Any,
) -> tuple[list[UnitAnswer], int] | str:
    resolved = resolve_reading_units_answer(
        package,
        source_phase=source_phase,
        answer=answer,
        request_sha256=prepared.request_sha256,
        model_profile=identity.model_profile,
        resources=prepared.resources,
        not_shown_block_ids=prepared.not_shown_block_ids,
    )
    return resolved if isinstance(resolved, str) else (list(answer.units), 0)


_EXPERIMENTAL_KINDS = frozenset(
    {UnitKind.GENERAL_METHODS, UnitKind.PROCEDURE, UnitKind.CHARACTERIZATION, UnitKind.CRYSTALLOGRAPHY}
)


@dataclass(frozen=True, slots=True)
class _ReadingUnitsStageIdentity:
    package_bytes: bytes
    settings: dict[str, object]
    prompt_sha256: str
    schema_sha256: str
    model_profile: str
    content_key: str


def reading_units_stage_identity(
    *, package: CanonicalPaperPackage, source_phase: Literal["MAIN", "SI"]
) -> _ReadingUnitsStageIdentity:
    """One stage configuration for execution, offline preflight and resume.

    The cleaning version is part of it (E136, decision 9): the text the model reads is
    what cleaning makes of the package, so a change to cleaning is a change of input,
    and a cached answer over the old text must not be served for the new.
    """

    resources = load_reading_units_resources()
    package_bytes = canonical_json_bytes(
        package.model_dump(mode="json", by_alias=True, exclude_none=False)
    )
    settings: dict[str, object] = {
        "stage_profile": READING_UNITS_STAGE_PROFILE,
        "source_phase": source_phase,
        "cleaning": {"version": PAGE_FURNITURE_VERSION},
        # The shape of the answer is part of the stage: a sealed answer is verified by
        # re-serialising it, so an answer sealed under an older ReadingUnits cannot be
        # served for today's (E161). The hash comes from the contract itself, so a new
        # field invalidates the cache without anyone remembering to say so.
        "answer": {"shape": answer_shape_sha256()},
    }
    content_key = stage_content_key(
        stage_name=READING_UNITS_STAGE_NAME,
        stage_version=_STAGE_VERSION,
        input_sha256s=(hashlib.sha256(package_bytes).hexdigest(),),
        settings=settings,
        prompt_sha256=resources.prompt_sha256,
        schema_sha256=resources.schema_sha256,
        model_profile=AUTHORIZED_MODEL_PROFILE,
    )
    return _ReadingUnitsStageIdentity(
        package_bytes=package_bytes,
        settings=settings,
        prompt_sha256=resources.prompt_sha256,
        schema_sha256=resources.schema_sha256,
        model_profile=AUTHORIZED_MODEL_PROFILE,
        content_key=content_key,
    )


def _failure(code: FailureCode, message: str) -> TypedFailure:
    return TypedFailure(
        schema_version="tsdaf.typed-failure.v1",
        code=code,
        message=message,
        request_id=None,
        details=(),
    )


def read_reading_units(store: LocalObjectStore, ref: ArtifactRef) -> ReadingUnits:
    if ref.media_type != READING_UNITS_MEDIA_TYPE:
        raise ValueError("artifact is not a reading-units record")
    return ReadingUnits.model_validate_json(store.read_bytes(ref))


def run_reading_units_stage(
    *,
    workspace: Path,
    package: CanonicalPaperPackage,
    execution_id: str,
    client: LaneInteractionClient,
    source_phase: Literal["MAIN", "SI"],
    provider_is_live: bool,
    live: bool = False,
) -> StageResult | TypedFailure:
    """Run or reuse the immutable local reading-units stage for one package."""

    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a pathlib.Path")
    if not isinstance(package, CanonicalPaperPackage):
        raise TypeError("package must be a CanonicalPaperPackage")
    if not isinstance(execution_id, str) or not execution_id.strip():
        raise ValueError("execution_id must be a nonempty string")
    if not isinstance(provider_is_live, bool) or not isinstance(live, bool):
        raise TypeError("provider_is_live and live must be bool values")

    identity = reading_units_stage_identity(package=package, source_phase=source_phase)
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    if (
        provider_is_live
        and not live
        and runner.stage_is_uncached(
            execution_id=execution_id,
            stage_name=READING_UNITS_STAGE_NAME,
            content_key=identity.content_key,
        )
    ):
        return _failure(
            FailureCode.LIVE_REQUIRED,
            "This uncached stage requires an explicitly enabled live invocation.",
        )

    try:
        input_artifact = store.put_bytes(identity.package_bytes, media_type=_PACKAGE_MEDIA_TYPE)
    except LocalRuntimeError as error:
        return _failure(error.code, "The local reading-units runtime could not persist its input.")

    def stage_result(
        *,
        attempt_id: str,
        content_key: str,
        started_at: datetime,
        status: ExecutionStatus,
        output_artifact: ArtifactRef | None,
        failure: TypedFailure | None,
    ) -> StageResult:
        return StageResult(
            schema_version="tsdaf.stage-result.v1",
            execution_id=execution_id,
            attempt_id=attempt_id,
            stage_name=READING_UNITS_STAGE_NAME,
            stage_version=_STAGE_VERSION,
            input_content_key=content_key,
            status=status,
            output_artifact=output_artifact,
            failure=failure,
            cached_from_result=None,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )

    def failed(
        *, attempt_id: str, content_key: str, started_at: datetime, failure: TypedFailure
    ) -> StageResult:
        return stage_result(
            attempt_id=attempt_id,
            content_key=content_key,
            started_at=started_at,
            status=(
                ExecutionStatus.FAILED_RETRYABLE
                if failure_is_retryable(failure.code)
                else ExecutionStatus.FAILED_TERMINAL
            ),
            output_artifact=None,
            failure=failure,
        )

    def invoke(attempt_id: str, content_key: str) -> StageResult:
        started_at = datetime.now(UTC)
        prepared = build_reading_units_request(package, source_phase=source_phase)
        if isinstance(prepared, TypedFailure):
            return failed(
                attempt_id=attempt_id,
                content_key=content_key,
                started_at=started_at,
                failure=prepared,
            )
        units: list[UnitAnswer] = []
        rationale: str | None = None
        # In the windows form no document is asked for in one answer, each part is told the
        # units already saved, and a part cut at the output ceiling is shown again at half
        # its rows rather than sent again unchanged.
        windows = reading_units_form() == "WINDOWS"
        start, scale, whole = 0, 1, not windows
        rows_limit: int | None = None
        try:
            while True:
                # First the whole document in one answer, exactly as before. If the provider
                # cannot deliver that answer (a long supplement's reply is cut at the output
                # ceiling, identically on every retry), the document is divided part by part;
                # the next part begins where the last whole unit ended.
                kwargs, shown, final = build_reading_units_part(
                    prepared,
                    start,
                    scale=scale,
                    whole=whole,
                    rows_limit=rows_limit,
                    before=units if windows else None,
                )
                halved = False
                history = list(cast(list[dict[str, object]], kwargs["input"]))
                accepted: tuple[list[UnitAnswer], int] | None = None
                by_parts = False
                for turn in range(1, _MAX_TURNS + 1):
                    adapter_prepared = client.prepare(**{**kwargs, "input": history})
                    if adapter_prepared.model_profile != identity.model_profile:
                        raise ValueError(
                            "reading-units client model profile is not the authorized profile"
                        )
                    try:
                        interaction = client.execute_prepared(adapter_prepared)
                    except ProviderInteractionError as error:
                        audit_ref = seal_provider_attempts(
                            store, error, request_bytes=adapter_prepared.request_bytes
                        )
                        if audit_ref is not None:
                            runner.bind_attempt_artifacts(
                                execution_id,
                                attempt_id,
                                (audit_ref,),
                                stage_name=READING_UNITS_STAGE_NAME,
                                content_key=content_key,
                            )
                        if whole and not build_reading_units_part(prepared, 0)[2]:
                            by_parts = True
                            break
                        if (
                            windows
                            and error.failure.code is FailureCode.PROVIDER_OUTPUT_TRUNCATED
                            and len(shown) > _SMALLEST_PART_ROWS
                        ):
                            rows_limit = max(_SMALLEST_PART_ROWS, len(shown) // 2)
                            scale, halved = 1, True
                            break
                        return failed(
                            attempt_id=attempt_id,
                            content_key=content_key,
                            started_at=started_at,
                            failure=error.failure,
                        )
                    request_ref = store.put_bytes(
                        interaction.request_bytes, media_type=_INTERACTION_MEDIA_TYPE
                    )
                    response_ref = store.put_bytes(
                        interaction.response_bytes, media_type=_INTERACTION_MEDIA_TYPE
                    )
                    runner.append_event(
                        execution_id,
                        "reading_units_interaction",
                        {
                            "turn": turn,
                            "part_first_row": start,
                            "interaction_id": interaction.interaction_id,
                            "request_id": interaction.request_id,
                            "request_artifact": request_ref.model_dump(mode="json"),
                            "response_artifact": response_ref.model_dump(mode="json"),
                            "request_chars": prepared.request_chars,
                            "total_input_tokens": interaction.total_input_tokens,
                            "total_output_tokens": interaction.total_output_tokens,
                            "total_tokens": interaction.total_tokens,
                            "total_cached_tokens": interaction.total_cached_tokens,
                            "total_thought_tokens": interaction.total_thought_tokens,
                            "total_tool_use_tokens": interaction.total_tool_use_tokens,
                            "elapsed_ms": interaction.elapsed_ms,
                            "transport_attempts": interaction.transport_attempts,
                        },
                        attempt_id=attempt_id,
                        stage_name=READING_UNITS_STAGE_NAME,
                        content_key=content_key,
                    )
                    audit_ref = seal_provider_attempts(
                        store, interaction, request_bytes=interaction.request_bytes
                    )
                    if audit_ref is not None:
                        runner.bind_attempt_artifacts(
                            execution_id,
                            attempt_id,
                            (audit_ref,),
                            stage_name=READING_UNITS_STAGE_NAME,
                            content_key=content_key,
                        )
                    try:
                        answer = ReadingUnitsAnswer.model_validate_json(
                            interaction.output_text or ""
                        )
                    except ValidationError as error:
                        refusal: str | None = (
                            "the answer did not match the requested JSON shape "
                            f"({error.error_count()} problem(s)); answer again with units "
                            "(each a first_block_id and last_block_id copied from the rows, a "
                            "kind and a compound_label) and a one-sentence rationale"
                        )
                    else:
                        # The one-answer call is judged exactly as it always was, against the
                        # package; only a part is judged against the rows it showed.
                        outcome = (
                            _whole_document(package, source_phase, answer, prepared, identity)
                            if whole
                            else accept_reading_units_part(shown, answer, final=final)
                        )
                        if isinstance(outcome, str):
                            refusal = outcome
                        else:
                            accepted, refusal = outcome, None
                            if outcome is not None and (
                                rationale is None
                                or not any(unit.kind in _EXPERIMENTAL_KINDS for unit in units)
                            ):
                                rationale = answer.rationale
                            break
                    if turn == _MAX_TURNS:
                        return failed(
                            attempt_id=attempt_id,
                            content_key=content_key,
                            started_at=started_at,
                            failure=_failure(
                                FailureCode.ROUTER_REFERENCE_INVALID,
                                f"The reading-units answer was refused twice: {refusal}.",
                            ),
                        )
                    response: Any = json.loads(interaction.response_bytes)
                    steps = response.get("steps") if isinstance(response, dict) else None
                    if isinstance(steps, list):
                        history.extend(dict(step) for step in steps if isinstance(step, dict))
                    history.append(
                        {"type": "user_input", "content": [{"type": "text", "text": refusal}]}
                    )
                if by_parts:
                    whole = False
                    continue
                if halved:
                    continue
                if accepted is None:
                    # The part held less than one whole unit: show more rows from the same start.
                    scale *= 2
                    continue
                units.extend(accepted[0])
                if final:
                    break
                start, scale, rows_limit = start + accepted[1], 1, None
            resolved = resolve_reading_units_answer(
                package,
                source_phase=source_phase,
                answer=ReadingUnitsAnswer(units=units, rationale=rationale or ""),
                request_sha256=prepared.request_sha256,
                model_profile=identity.model_profile,
                resources=prepared.resources,
                not_shown_block_ids=prepared.not_shown_block_ids,
            )
            if not isinstance(resolved, ReadingUnits):
                return failed(
                    attempt_id=attempt_id,
                    content_key=content_key,
                    started_at=started_at,
                    failure=_failure(
                        FailureCode.ROUTER_REFERENCE_INVALID,
                        f"The reading-units parts do not make one division: {resolved}.",
                    ),
                )
            output_ref = store.put_bytes(
                canonical_json_bytes(resolved.model_dump(mode="json")),
                media_type=READING_UNITS_MEDIA_TYPE,
            )
            return stage_result(
                attempt_id=attempt_id,
                content_key=content_key,
                started_at=started_at,
                status=ExecutionStatus.SUCCEEDED,
                output_artifact=output_ref,
                failure=None,
            )
        except LocalRuntimeError as error:
            return failed(
                attempt_id=attempt_id,
                content_key=content_key,
                started_at=started_at,
                failure=_failure(
                    error.code,
                    "The local reading-units runtime could not persist verified evidence.",
                ),
            )
        raise AssertionError("the reading-units turn loop always returns")

    return runner.run_stage(
        execution_id=execution_id,
        stage_name=READING_UNITS_STAGE_NAME,
        stage_version=_STAGE_VERSION,
        input_artifacts=(input_artifact,),
        settings=identity.settings,
        invoke=invoke,
        prompt_sha256=identity.prompt_sha256,
        schema_sha256=identity.schema_sha256,
        model_profile=identity.model_profile,
        requires_live=provider_is_live,
        live=live,
    )
