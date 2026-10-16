"""Fixed ``tsdaf run`` commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tsdaf.apps.composition import _execution_id_failure, inspect_execution
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.workflow.model_call_export import (
    corpus_document,
    export_model_calls,
    export_router_calls,
    router_corpus_document,
)

__all__ = ("run_export_calls", "run_export_router_calls", "run_inspect")


def run_inspect(execution_id: str) -> int:
    """Verify and inspect one local execution."""

    input_failure = _execution_id_failure(execution_id)
    if input_failure is not None:
        sys.stderr.write(canonical_json_bytes(input_failure).decode("utf-8") + "\n")
        return 1
    code, payload = inspect_execution(
        workspace=Path.cwd() / ".tsdaf-workspace",
        execution_id=execution_id,
    )
    stream = sys.stdout if code == 0 else sys.stderr
    stream.write(canonical_json_bytes(payload).decode("utf-8") + "\n")
    return code


def run_export_calls(workspace: Path, execution_id: str, out_dir: Path) -> int:
    """Export the calls a model made in one sealed run to the replay corpus shape.

    Writes ``<out_dir>/<execution_id>-read.json`` and ``-write.json``; the read-only
    walk of the workspace is `tsdaf.workflow.model_call_export`. The summary on stdout
    says how many turns and calls each execution contributed.
    """
    input_failure = _execution_id_failure(execution_id)
    if input_failure is not None:
        sys.stderr.write(canonical_json_bytes(input_failure).decode("utf-8") + "\n")
        return 1
    if not (workspace / "executions").is_dir():
        sys.stderr.write(f"not a run workspace (no executions/): {workspace}\n")
        return 1
    exported = export_model_calls(workspace, execution_prefix=execution_id)
    if not exported.calls:
        sys.stderr.write(f"no sealed model calls under {execution_id} in {workspace}\n")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, int] = {}
    for tool in ("read", "write"):
        document = corpus_document(exported, tool=tool, source=execution_id)
        if not document["calls"]:
            continue
        path = out_dir / f"{execution_id}-{tool}.json"
        path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n")
        written[str(path)] = len(document["calls"])
    summary = {
        "execution_id": execution_id,
        "runs": list(exported.runs),
        "model_profiles": list(exported.model_profiles),
        "written": written,
    }
    sys.stdout.write(canonical_json_bytes(summary).decode("utf-8") + "\n")
    return 0


def run_export_router_calls(
    workspace: Path, execution_id: str, out_dir: Path, *, source: str | None = None
) -> int:
    """Export the Router's calls of one sealed route execution to the replay corpus.

    Writes ``<out_dir>/<source>-router.json`` (``source`` defaults to the execution id):
    every search, read and submission the model made, with the Host's answer to each.
    """
    input_failure = _execution_id_failure(execution_id)
    if input_failure is not None:
        sys.stderr.write(canonical_json_bytes(input_failure).decode("utf-8") + "\n")
        return 1
    if not (workspace / "executions" / execution_id).is_dir():
        sys.stderr.write(f"no execution {execution_id} in {workspace}\n")
        return 1
    exported = export_router_calls(workspace, execution_id=execution_id)
    if not exported.calls:
        sys.stderr.write(f"no sealed Router calls under {execution_id} in {workspace}\n")
        return 1
    name = source or execution_id
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}-router.json"
    document = router_corpus_document(exported, source=name)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n")
    summary = {
        "execution_id": execution_id,
        "runs": list(exported.runs),
        "written": {str(path): len(document["calls"])},
    }
    sys.stdout.write(canonical_json_bytes(summary).decode("utf-8") + "\n")
    return 0
