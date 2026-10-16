"""Recover selected source wording from existing sealed producers, never current aliases."""

import json

from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.lanes.literature.enrichment_models import InvestigationReceipt
from tsdaf.lanes.literature.gate_models import read_fusion_result
from tsdaf.lanes.literature.source_parts import SourceSelection
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.investigation_origins import INVESTIGATION_INTERACTION_MEDIA, InvestigationOrigins
from tsdaf.workflow.literature_fusion_actions import name_binder
from tsdaf.workflow.literature_paper_facts import resolve_public_ref


def _source_fields(result):
    for fact in result.facts:
        variants = [(fact.source_parts, result.field_origins.get(fact.fact_id, {}).get("source_parts", ()))]
        conflict = result.conflicts.get(fact.fact_id)
        if conflict and "source_parts" in conflict.candidates:
            variants = [(tuple(SourceSelection.model_validate_json(canonical_json_bytes(v))
                               for v in option.value), option.origins)
                        for option in conflict.candidates["source_parts"]]
        for parts, origins in variants:
            if parts:
                yield fact.fact_id, parts, origins


def inherited_fact_selections(*, groups, machine, sources, store, runner, restore_domain):
    """A disposable map for projector consumers of a verified public baseline.

    Follow only that baseline's existing assembly/previous-machine chain. Each
    source field selects its original accepted child action, whose receipt owns
    the immutable selections. No store scan or current-name fallback is allowed.
    """
    needed = []
    for group in groups:
        if "result" not in group:
            continue
        result = read_fusion_result(canonical_json_bytes(group["result"]))
        needed.extend((group["route_group_id"], result, fid, parts, origins)
                      for fid, parts, origins in _source_fields(result))
    if not needed:
        return {}

    receipts = {}
    visited = set()
    queue = [machine]
    while queue:
        current = queue.pop()
        manifest = resolve_public_ref(store, current["execution_manifest_ref"])
        if manifest.sha256 in visited:
            continue
        visited.add(manifest.sha256)
        closure = runner.verified_execution_artifact_closure(current["execution_id"], manifest)
        for ref in closure:
            if ref.media_type != "application/vnd.tsdaf.literature-enrichment-assembly+json":
                continue
            assembly = json.loads(read_artifact(store.read_bytes, ref))
            for entry in assembly["investigation_refs"]:
                receipt_ref = ArtifactRef.model_validate(entry)
                if receipt_ref not in closure:
                    raise ValueError("source receipt is outside its assembly closure")
                receipt = InvestigationReceipt.model_validate_json(read_artifact(store.read_bytes, receipt_ref))
                if receipt.paper_id != machine["paper_id"]:
                    raise ValueError("source receipt belongs to a different paper")
                receipts[receipt_ref] = receipt
            previous_ref = ArtifactRef.model_validate(assembly["previous_machine_ref"])
            if previous_ref not in closure:
                raise ValueError("previous source machine is outside its assembly closure")
            queue.append(json.loads(read_artifact(store.read_bytes, previous_ref)))

    histories = {}
    restored = {}
    for gid, result, fid, parts, origins in needed:
        if not origins:
            raise ValueError("account source field has no accepted origin")
        for origin in origins:
            matches = [(ref, receipt) for ref, receipt in receipts.items()
                       if origin.interaction_ref in receipt.audit_refs]
            if not matches:
                if origin.interaction_ref.media_type == INVESTIGATION_INTERACTION_MEDIA:
                    raise ValueError("original Reading source receipt is not in the publication ancestry")
                _fusion_parts(result, fid, parts, origin,
                              tuple(d.package for d in sources.documents if d.package is not None),
                              store, runner, None, restored.setdefault(gid, {}), histories)
                continue
            if len(matches) != 1:
                raise ValueError("source field origin has ambiguous investigation receipts")
            ref, receipt = matches[0]
            if ref not in histories:
                terminal = runner.verified_terminal_manifest(receipt.execution_id)
                closure = runner.verified_execution_artifact_closure(receipt.execution_id, terminal)
                if ref not in closure:
                    raise ValueError("source receipt lacks its producer seal")
                histories[ref] = InvestigationOrigins(store.read_bytes).verified_repair_history(
                    receipt, restore_domain=restore_domain, accepted_refs=closure,
                    require_command_receipts=True,
                )
            actions = [a for a in histories[ref]["actions"]
                       if a["origin"]["action_id"] == origin.action_id
                       and a["origin"]["interaction_ref"] == origin.interaction_ref.model_dump(mode="json")
                       and a["origin"]["call_index"] == origin.call_index
                       and a["origin"]["item"] == origin.item]
            if len(actions) != 1:
                raise ValueError("source field is not an accepted original action")
            action = actions[0]
            changes = action["command_evidence"]["changes"]
            if origin.change_index >= len(changes):
                raise ValueError("source field child is outside its accepted action")
            change = changes[origin.change_index]
            expected = [p.model_dump(mode="json") for p in parts]
            actual = [SourceSelection.model_validate_json(canonical_json_bytes(p)).model_dump(mode="json")
                      for p in change.get("source_parts", ())]
            if change["fact_id"] != fid or actual != expected:
                raise ValueError("source field differs from its accepted child")
            from tsdaf.workflow.reading_source_names import reading_source_binder
            bind = reading_source_binder(result, receipt.selections)
            submitted = action["value"]["repair"]["changes"][origin.change_index]
            original_parts = [SourceSelection.model_validate_json(canonical_json_bytes({
                **p, "selection_id": bind(p["selection_id"]),
            })).model_dump(mode="json") for p in submitted.get("source_parts", ())]
            if original_parts != expected:
                raise ValueError("source field differs from the model's original selection")
            original = {bind(sid): (sid, selection) for sid, selection in receipt.selections.items()}
            for part in parts:
                if part.selection_id not in original:
                    raise ValueError("accepted source field has no original selection")
                sid, selection = original[part.selection_id]
                if sid not in {*action["delivered_selection_ids"], *action.get("inherited_selection_ids", ())}:
                    raise ValueError("source field cites text not delivered before its action")
                _put(restored.setdefault(gid, {}), result, part.selection_id, selection)
    return restored


def _put(target, result, sid, selection):
    if dict(selection["evidence_ref"]) not in result.sources.get(sid, ()):
        raise ValueError("selected text evidence differs from the fact's accepted source")
    if sid in target and any(target[sid].get(k) != selection.get(k) for k in ("exact_text", "evidence_ref")):
        raise ValueError("one accepted source identity has different text or evidence")
    target[sid] = dict(selection)


def fusion_fact_selections(result, *, packages, store, accepted_refs, runner=None):
    """Resolve complete source fields for MAIN publication and downstream views."""
    restored, histories = {}, {}
    for fid, parts, origins in _source_fields(result):
        if not origins:
            raise ValueError("account source field has no accepted origin")
        for origin in origins:
            _fusion_parts(result, fid, parts, origin, packages, store, runner,
                          set(accepted_refs), restored, histories)
    return restored


def _producer_closure(execution, runner, histories):
    if runner is None:
        raise ValueError("Fusion source recovery requires its producer verifier")
    if execution not in histories:
        terminal = runner.verified_terminal_manifest(execution)
        histories[execution] = set(runner.verified_execution_artifact_closure(execution, terminal))
    return histories[execution]


def _fusion_parts(result, fid, parts, origin, packages, store, runner, accepted_refs, target, histories):
    from tsdaf.lanes.literature.evidence_binding import WorkUnitEvidenceRegistry, source_key, source_location, text_source_quote
    from tsdaf.lanes.literature.gate_models import FUSION_RECEIPT_VERSION
    from tsdaf.workflow.literature_fusion_runtime import FUSION_AUDIT_MEDIA
    from tsdaf.workflow.naming_evidence import FrozenNaming, restore_fusion_domain
    from tsdaf.workflow.tool_execution import is_call_receipt
    from tsdaf.workflow.tool_turns import response_call_steps
    from tsdaf.workflow.write_delivery import write_action_id

    if origin.interaction_ref.media_type != FUSION_AUDIT_MEDIA:
        raise ValueError("source origin is not a known Fusion interaction")
    turn = json.loads(read_artifact(store.read_bytes, origin.interaction_ref))
    execution = turn.get("execution_id")
    if not isinstance(execution, str) or not execution:
        raise ValueError("Fusion source origin has no producer execution")
    closure = _producer_closure(execution, runner, histories)

    def record(ref):
        if ref not in closure or (accepted_refs is not None and ref not in accepted_refs):
            raise ValueError("Fusion source action is outside its producer closure")
        return json.loads(read_artifact(store.read_bytes, ref))

    record(origin.interaction_ref)
    receipts = []
    for ref in closure:
        if ref.media_type != FUSION_AUDIT_MEDIA:
            continue
        value = json.loads(read_artifact(store.read_bytes, ref))
        if value.get("schema_version") == FUSION_RECEIPT_VERSION and value.get("execution_id") == execution:
            if origin.interaction_ref.model_dump(mode="json") in value["attempt_refs"]:
                receipts.append(record(ref))
    if len(receipts) != 1:
        raise ValueError("source origin has no unique Fusion delivery receipt")
    receipt = receipts[0]
    calls = [value for ref in receipt["attempt_refs"]
             if (value := record(ArtifactRef.model_validate(ref)))
             and is_call_receipt(value)
             and value["interaction_ref"] == origin.interaction_ref.model_dump(mode="json")
             and value["call_index"] == origin.call_index]
    if len(calls) != 1 or origin.action_id != write_action_id(
        origin.interaction_ref.sha256, origin.call_index, origin.item
    ):
        raise ValueError("source origin has no unique accepted Fusion call")
    call = calls[0]
    rows = [row for row in call.get("write_results", ())
            if row["item"] == origin.item and row["action_id"] == origin.action_id]
    items = [item for item in call.get("items", ()) if item.get("action_id") == origin.action_id]
    if (len(rows) != 1 or rows[0]["outcome"] not in {"SAVED", "NO_CHANGE"}
            or len(items) != 1 or items[0]["outcome"] != rows[0]["outcome"]):
        raise ValueError("source origin points to a refused Fusion item")
    item = items[0]
    if (f"{item['collection']}:{item['index']}" != origin.item
            or origin.change_index >= len(item.get("fact_ids", ()))
            or item["fact_ids"][origin.change_index] != fid):
        raise ValueError("source origin does not identify the accepted Fusion child")
    expected = [part.model_dump(mode="json") for part in parts]

    def source_parts(change):
        return [SourceSelection.model_validate_json(canonical_json_bytes(part)).model_dump(mode="json")
                for part in change.get("source_parts", ())]

    changes = [change for batch in call.get("batches", ()) for change in batch
               if change.get("fact_id") == fid]
    if len(changes) != 1 or source_parts(changes[0]) != expected:
        raise ValueError("source field differs from the normalized accepted Fusion child")
    saved_ref = ArtifactRef.model_validate(call["result_ref"])
    saved = read_fusion_result(canonical_json_bytes(record(saved_ref)))
    final = read_fusion_result(canonical_json_bytes(record(ArtifactRef.model_validate(receipt["result_ref"]))))
    if saved.work_unit_id != receipt["work_unit_id"] or final.work_unit_id != receipt["work_unit_id"]:
        raise ValueError("Fusion source receipt belongs to a different work unit")
    if not any(saved_fid == fid and saved_parts == parts and origin in origins
               for saved_fid, saved_parts, origins in _source_fields(saved)):
        raise ValueError("source field differs from the accepted Fusion result")

    # Reuse the frozen request's name decoder; never guess today's alias mapping.
    response_ref = ArtifactRef.model_validate(turn["response_ref"])
    response = record(response_ref)
    calls = response_call_steps(canonical_json_bytes(response))
    if origin.call_index >= len(calls):
        raise ValueError("source origin has no original model call")
    _, submitted_call = calls[origin.call_index]
    if submitted_call["name"] != "write":
        raise ValueError("source origin is not a model write")
    naming = FrozenNaming.from_snapshot(record(ArtifactRef.model_validate(turn["naming_ref"])), restore_fusion_domain)
    raw = naming.resolve(submitted_call["arguments"])
    submitted = raw[item["collection"]][item["index"]]
    if item["collection"] == "groups":
        submitted = submitted["changes"][origin.change_index]
    elif item["collection"] != "changes" or origin.change_index != 0:
        raise ValueError("source origin has no original model child")
    bind = name_binder(saved)
    original = {**submitted, "source_parts": [
        {**part, "selection_id": bind(part["selection_id"])} for part in submitted.get("source_parts", ())]}
    if source_parts(original) != expected:
        raise ValueError("source field differs from the model's original Fusion selection")

    packages = {p.canonical_content_sha256: p for p in packages}
    found = set()
    for ref in result.evidence_registry_refs:
        registry = WorkUnitEvidenceRegistry.model_validate_json(read_artifact(store.read_bytes, ref))
        producer = _producer_closure(registry.execution_id, runner, histories)
        if ref not in producer or (accepted_refs is not None and ref not in accepted_refs):
            raise ValueError("source registry lacks its own producer seal")
        for part in parts:
            for entry in registry.entries:
                if entry.modality != "TEXT" or source_key(registry.work_unit_id, entry.modality, entry.selection_id) != part.selection_id:
                    continue
                package = packages.get(registry.package_sha256)
                if package is None:
                    raise ValueError("original source package is not registered")
                selection = source_location(entry, part.selection_id, package)
                quote = text_source_quote(selection, package,
                    document_revision_id=registry.document_revision_id,
                    package_sha256=registry.package_sha256, partners={})
                _put(target, result, part.selection_id, {**quote, "evidence_ref": entry.evidence_ref.model_dump(mode="json")})
                found.add(part.selection_id)
    if found != {part.selection_id for part in parts}:
        raise ValueError("source field cannot be recovered from its original sealed producer")
