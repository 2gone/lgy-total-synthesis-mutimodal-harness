"""Export the tool calls a real model made in a sealed run, in the shape the replay corpus uses.

`tests/fixtures/model_calls/` holds calls real models made, replayed against the current
contracts so a change cannot quietly make the tool harder to use. This module is the one
way calls get there from a run: it walks the sealed executions of one workspace, reads
back every model turn the Host recorded and the receipt the Host sealed for each call,
and writes them out as a corpus document -- arguments verbatim, plus what the Host
answered at the time, so a later replay can be compared with the Host's own verdict.

Only bound artifacts are read: each execution's verified artifact closure, never loose
objects, so a call in the corpus is one the Host accepted a response for. No record of
the workspace is written or changed (verifying a sealed execution takes its lock).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts.execution import ExecutionStatus
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.investigation_kinds import investigation_kind_for_execution
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.literature_fusion_runtime import FUSION_AUDIT_MEDIA
from tsdaf.workflow.local_runtime import LocalObjectStore, LocalRunner
from tsdaf.workflow.naming_evidence import verify_accepted_turn_naming
from tsdaf.workflow.source_investigation import INVESTIGATION_INTERACTION_MEDIA
from tsdaf.workflow.tool_execution import is_call_receipt

__all__ = (
    "CORPUS_SCHEMA_VERSION",
    "ExportedCalls",
    "corpus_document",
    "export_model_calls",
    "export_router_calls",
    "lane_of_execution",
    "router_corpus_document",
    "sealed_execution_ids",
)

CORPUS_SCHEMA_VERSION = "tsdaf.model-calls-corpus.v1"

# The media types under which a lane seals the record of one model turn. The same type
# carries the receipts of that turn's calls in the fusion lane; the two are told apart
# by shape below.
_INTERACTION_MEDIA = frozenset({FUSION_AUDIT_MEDIA, INVESTIGATION_INTERACTION_MEDIA})

# How the paper driver names the executions it starts under one route group
# (`literature_paper_driver.group_execution_id`) and how the composition root names the
# two investigation lanes. `-W` is historical -- no execution has been named that way
# since reaction windows were retired (2026-09-23) -- kept so a sealed corpus that
# predates the retirement, and the two replay gates that read it, still classify right.
_WINDOW = re.compile(r"-W\d{3}(-R\d+)?$")
_GROUP = re.compile(r"-G\d{3}$")


def lane_of_execution(execution_id: str) -> str:
    """Which lane an execution id belongs to, read off the driver's naming."""
    investigation = investigation_kind_for_execution(execution_id)
    if investigation is not None:
        return investigation.export_lane
    if _WINDOW.search(execution_id):
        return "window"
    if _GROUP.search(execution_id):
        return "group-fusion"
    return "route-group"


@dataclass(frozen=True, slots=True)
class ExportedCalls:
    """Every call of one export, and the runs (executions) they came from."""

    runs: tuple[dict[str, Any], ...]
    calls: tuple[dict[str, Any], ...]
    model_profiles: tuple[str, ...]
    # The day the last of these executions finished: when the model made the calls.
    recorded: str
    naming_snapshots: Mapping[str, dict[str, Any]] = field(default_factory=dict)

    def of_tool(self, tool: str) -> tuple[dict[str, Any], ...]:
        return tuple(call for call in self.calls if call["tool"] == tool)


def sealed_execution_ids(workspace: Path) -> list[str]:
    """The executions of a workspace that have a sealed terminal manifest."""
    root = workspace / "executions"
    if not root.is_dir():
        return []
    return sorted(
        path.name for path in root.iterdir() if (path / "manifest.json.seal.json").is_file()
    )


def _manifest(workspace: Path, execution_id: str) -> dict[str, Any]:
    manifest = json.loads((workspace / "executions" / execution_id / "manifest.json").read_text())
    if not isinstance(manifest, dict):
        raise ValueError(f"execution manifest of {execution_id} is not an object")
    return manifest


def _turn_records(
    store: LocalObjectStore, closure: Sequence[ArtifactRef]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """The model turns of one closure and the call receipts keyed by the turn's sha."""
    turns: list[dict[str, Any]] = []
    receipts: dict[str, list[dict[str, Any]]] = {}
    for ref in closure:
        if ref.media_type not in _INTERACTION_MEDIA:
            continue
        value = json.loads(store.read_bytes(ref))
        if not isinstance(value, Mapping):
            continue
        if is_call_receipt(value):
            receipts.setdefault(str(value["interaction_ref"]["sha256"]), []).append(dict(value))
        elif "request_ref" in value and "response_ref" in value and "iteration" in value:
            turns.append(
                {**value, "_sha256": ref.sha256, "interaction_ref": ref.model_dump(mode="json")}
            )
    return turns, receipts


def _function_calls(response: Any) -> Iterator[tuple[str, Any]]:
    if not isinstance(response, Mapping):
        return
    for step in response.get("steps", ()):
        if not isinstance(step, Mapping) or step.get("type") != "function_call":
            continue
        arguments = step.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = decode_tool_arguments(arguments)
            except ValueError:
                pass
        yield str(step.get("name") or "?"), arguments


def _host_answer(receipt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """What the Host sealed for one call: its outcome and each item's, nothing else."""
    if receipt is None:
        return None
    return {
        "outcome": receipt.get("outcome"),
        "diagnostic": receipt.get("diagnostic"),
        "items": [
            {
                "collection": item.get("collection"),
                "index": item.get("index"),
                "location": item.get("location"),
                "outcome": item.get("outcome"),
                "diagnostic": item.get("diagnostic"),
            }
            for item in (receipt.get("items") or ())
            if isinstance(item, Mapping)
        ],
    }


def _turn_evidence(
    store: LocalObjectStore,
    closure: Sequence[ArtifactRef],
    turns: Sequence[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Historical declarations and exact name tables from this verified closure only."""
    by_sha = {ref.sha256: ref for ref in closure}
    snapshots: dict[str, dict[str, Any]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for turn in turns:
        binding = turn.get("binding") or {}
        declarations: dict[str, str | None] = {"fusion_profile": None, "naming_version": None}
        resources = by_sha.get(str(binding.get("resources_sha256", "")))
        if (
            resources is not None
            and resources.media_type == "application/vnd.tsdaf.fusion-resources+octets"
        ):
            recorded = json.loads(read_artifact(store.read_bytes, resources))
            for output, source in (("fusion_profile", "profile"), ("naming_version", "naming")):
                if isinstance(recorded.get(source), str):
                    declarations[output] = recorded[source]
        request_ref = ArtifactRef.model_validate(turn["request_ref"])
        snapshot = verify_accepted_turn_naming(
            store.read_bytes,
            turn,
            closure,
            expected_naming_version=declarations["naming_version"],
        )
        naming_ref: ArtifactRef | None = None
        if snapshot is not None:
            naming_ref = ArtifactRef.model_validate(turn["naming_ref"])
            declarations["naming_version"] = snapshot["naming_version"]
            snapshots[naming_ref.sha256] = snapshot
        metadata[turn["_sha256"]] = {
            "request_ref": request_ref.model_dump(mode="json"),
            "naming_ref": naming_ref.model_dump(mode="json") if naming_ref else None,
            "naming_status": "RECORDED" if naming_ref else "NOT_RECORDED",
            "declarations": declarations,
        }
    return metadata, snapshots


def _consistent_declarations(calls: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    """A common header cannot pretend mixed or missing evidence is one known version."""
    result: dict[str, str | None] = {}
    for key in ("fusion_profile", "naming_version"):
        values = [call.get("declarations", {}).get(key) for call in calls]
        result[key] = (
            values[0]
            if values and isinstance(values[0], str) and all(value == values[0] for value in values)
            else None
        )
    return result


def export_model_calls(workspace: Path, *, execution_prefix: str) -> ExportedCalls:
    """Every tool call a model made in the sealed executions named by ``execution_prefix``.

    The prefix is the route group's execution id (``lit-<paper>-RG001-<tag>``); its
    group fusions and investigation lanes carry it as their own prefix, and so, in a
    sealed run from before reaction windows were retired (2026-09-23), do its windows.
    Calls are ordered by execution, turn and position; a turn seen in more than one
    closure (a window bound into its parent) is exported once.
    """
    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a pathlib.Path")
    if not execution_prefix:
        raise ValueError("execution_prefix must name the route group's execution")
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    turns: dict[str, dict[str, Any]] = {}
    receipts: dict[str, list[dict[str, Any]]] = {}
    evidence: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    closures: dict[str, tuple[ArtifactRef, ...]] = {}
    turn_closures: dict[str, tuple[ArtifactRef, ...]] = {}
    finished: list[str] = []
    for execution_id in sealed_execution_ids(workspace):
        if execution_id != execution_prefix and not execution_id.startswith(execution_prefix + "-"):
            continue
        manifest = _manifest(workspace, execution_id)
        finished.append(str(manifest.get("finished_at") or ""))
        manifest_ref = runner.finish_execution(execution_id, ExecutionStatus(manifest["status"]))
        closure = runner.verified_execution_artifact_closure(execution_id, manifest_ref)
        closures[execution_id] = closure
        found, sealed = _turn_records(store, closure)
        for turn in found:
            turns.setdefault(turn["_sha256"], turn)
            turn_closures.setdefault(turn["_sha256"], closure)
        for sha, rows in sealed.items():
            known = receipts.setdefault(sha, [])
            for row in rows:
                if row not in known:
                    known.append(row)

    def execution_of(turn: Mapping[str, Any]) -> str:
        binding = turn.get("binding") or {}
        return str(turn.get("execution_id") or binding.get("execution_id") or "")

    turns = {
        sha: turn
        for sha, turn in turns.items()
        if execution_of(turn) == execution_prefix
        or execution_of(turn).startswith(execution_prefix + "-")
    }
    # A parent may carry a child interaction without its request intent. Verify
    # against the sealed owning execution, never an unrelated execution's evidence.
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for turn in turns.values():
        by_owner.setdefault(execution_of(turn), []).append(turn)
    for owner, owned_turns in by_owner.items():
        closure = closures.get(owner, turn_closures[owned_turns[0]["_sha256"]])
        turn_evidence, turn_snapshots = _turn_evidence(store, closure, owned_turns)
        evidence.update(turn_evidence)
        snapshots.update(turn_snapshots)

    ordered = sorted(turns.values(), key=lambda t: (execution_of(t), int(t["iteration"])))
    runs: dict[str, dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    profiles: set[str] = set()
    for turn in ordered:
        execution_id = execution_of(turn)
        lane = lane_of_execution(execution_id)
        runs.setdefault(
            execution_id,
            {"name": execution_id, "lane": lane, "turns": 0, "calls": 0},
        )
        runs[execution_id]["turns"] += 1
        if isinstance(turn.get("model_profile"), str):
            profiles.add(turn["model_profile"])
        response = json.loads(store.read_bytes(ArtifactRef.model_validate(turn["response_ref"])))
        by_index = {int(r["call_index"]): r for r in receipts.get(turn["_sha256"], ())}
        for index, (tool, arguments) in enumerate(_function_calls(response)):
            runs[execution_id]["calls"] += 1
            calls.append(
                {
                    "run": execution_id,
                    "turn": int(turn["iteration"]),
                    "index": index,
                    "tool": tool,
                    "arguments": arguments,
                    "host": _host_answer(by_index.get(index)),
                    **evidence[turn["_sha256"]],
                }
            )
    return ExportedCalls(
        runs=tuple(runs.values()),
        calls=tuple(calls),
        model_profiles=tuple(sorted(profiles)),
        recorded=max(finished, default="")[:10],
        naming_snapshots=snapshots,
    )


def corpus_document(exported: ExportedCalls, *, tool: str, source: str) -> dict[str, Any]:
    """One corpus file: the calls of one tool, with where they came from."""
    calls = exported.of_tool(tool)
    runs = {call["run"] for call in calls}
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "note": (
            f"Calls a real model made against the {tool} tool in the sealed run {source}, "
            "kept verbatim with what the Host answered. Exported by `tsdaf run export-calls`. "
            "Evidence, not a specification: accepting more of them is an improvement; "
            "accepting fewer means a change made the tool harder to use."
        ),
        "source": source,
        "recorded": exported.recorded,
        "model_profiles": list(exported.model_profiles),
        # Declarations are historical evidence. Per-call values remain authoritative
        # when runs or turns differ; absent maps are not reconstructed with today's code.
        "declarations": _consistent_declarations(calls),
        "naming_snapshots": {
            sha: snapshot
            for sha, snapshot in exported.naming_snapshots.items()
            if any(
                call.get("naming_ref", {}).get("sha256") == sha
                for call in calls
                if isinstance(call.get("naming_ref"), Mapping)
            )
        },
        "runs": [run for run in exported.runs if run["name"] in runs],
        "calls": list(calls),
    }


# --- the Router's calls -----------------------------------------------------------
#
# The Router seals each model turn as an event (`article_router_interaction`) with the
# request and response artifacts, and the Host's answer to a call is the function
# result the next turn's request carries back. A submission the Host accepted is the
# one no later request answers: the routing result sealed under the execution.

_ROUTER_EVENT = "article_router_interaction"


def _router_events(workspace: Path, execution_id: str) -> list[dict[str, Any]]:
    path = workspace / "executions" / execution_id / "events.jsonl"
    if not path.is_file():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return sorted(
        (dict(e["data"]) for e in events if e.get("event_type") == _ROUTER_EVENT),
        key=lambda d: int(d["turn"]),
    )


def _router_host_answer(tool: str, result_text: str | None) -> dict[str, Any] | None:
    """The Host's answer to one Router call, from the function result it sent back:
    a validation failure for a submission, the search or read outcome otherwise. None
    when no later turn carries an answer -- for the last submission, acceptance."""
    if result_text is None:
        return (
            {"outcome": "ACCEPTED", "diagnostic": None}
            if tool == "submit_article_routing"
            else None
        )
    try:
        value = json.loads(result_text)
    except ValueError:
        return {"outcome": "RESULT", "diagnostic": result_text[:200]}
    if not isinstance(value, Mapping):
        return {"outcome": "RESULT", "diagnostic": None}
    if "code" in value:
        details = value.get("details") or ()
        keys = [str(d.get("key")) for d in details if isinstance(d, Mapping)]
        return {"outcome": "REJECTED", "diagnostic": str(value["code"]), "fields": keys}
    if "status" in value:
        return {
            "outcome": "RESULT",
            "diagnostic": str(value["status"]),
            "hits": len(value.get("hits") or ()),
        }
    return {"outcome": "RESULT", "diagnostic": None, "hits": len(value.get("blocks") or ())}


def export_router_calls(workspace: Path, *, execution_id: str) -> ExportedCalls:
    """Every tool call the Router made in one sealed execution, with the Host's answer
    to each: the validation verdict on a submission, the outcome of a search or read."""
    if not isinstance(workspace, Path):
        raise TypeError("workspace must be a pathlib.Path")
    store = LocalObjectStore(workspace)
    runner = LocalRunner(workspace, store)
    manifest = _manifest(workspace, execution_id)
    # Verifies the sealed execution (its lock is taken); the turns are its events.
    runner.finish_execution(execution_id, ExecutionStatus(manifest["status"]))
    turns = _router_events(workspace, execution_id)
    # The answers, keyed by call id, from every request after the first.
    answers: dict[str, str | None] = {}
    for turn in turns[1:]:
        request = json.loads(store.read_bytes(ArtifactRef.model_validate(turn["request_artifact"])))
        for item in request.get("input", ()):
            if isinstance(item, Mapping) and item.get("type") == "function_result":
                results = item.get("result") or ()
                text = (
                    results[0].get("text") if results and isinstance(results[0], Mapping) else None
                )
                answers[str(item.get("call_id"))] = text
    calls: list[dict[str, Any]] = []
    prompt: dict[str, Any] = {}
    for turn in turns:
        response_ref = ArtifactRef.model_validate(turn["response_artifact"])
        response = json.loads(store.read_bytes(response_ref))
        index = 0
        for step in response.get("steps", ()):
            if not isinstance(step, Mapping) or step.get("type") != "function_call":
                continue
            arguments = step.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = decode_tool_arguments(arguments)
                except ValueError:
                    pass
            tool = str(step.get("name") or "?")
            call_id = str(step.get("call_id"))
            calls.append(
                {
                    "run": execution_id,
                    "turn": int(turn["turn"]),
                    "index": index,
                    "tool": tool,
                    "arguments": arguments,
                    "host": _router_host_answer(tool, answers.get(call_id)),
                }
            )
            if tool == "submit_article_routing" and isinstance(arguments, Mapping):
                provenance = arguments.get("provenance")
                if isinstance(provenance, Mapping) and not prompt:
                    prompt = {
                        "prompt_version": provenance.get("prompt_version"),
                        "prompt_sha256": provenance.get("prompt_sha256"),
                        "router_version": provenance.get("router_version"),
                    }
            index += 1
    run = {
        "name": execution_id,
        "lane": "router",
        "turns": len(turns),
        "calls": len(calls),
        **prompt,
    }
    return ExportedCalls(
        runs=(run,),
        calls=tuple(calls),
        model_profiles=(),
        recorded=str(manifest.get("finished_at") or "")[:10],
    )


def router_corpus_document(exported: ExportedCalls, *, source: str) -> dict[str, Any]:
    """One corpus file for a Router run: its search, read and submit calls together."""
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "note": (
            f"Calls a real model made as the Router in the sealed run {source}, kept "
            "verbatim with what the Host answered: the validation verdict on each "
            "submission, the outcome of each search or read. Exported by `tsdaf route "
            "export-calls`. Evidence, not a specification: a submission the Host accepted "
            "must still be accepted by today's contract, and one it refused still refused."
        ),
        "source": source,
        "recorded": exported.recorded,
        "runs": list(exported.runs),
        "calls": list(exported.calls),
    }
