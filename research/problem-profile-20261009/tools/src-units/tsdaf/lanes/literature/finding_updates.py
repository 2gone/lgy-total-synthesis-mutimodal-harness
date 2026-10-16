"""Resolve explicit current-result selections without changing submitted words.

The writer and verified action replay share these mechanical rules. Snapshots are
comparison context, never contributions or cumulative application baselines.
"""

from collections.abc import Mapping
from copy import deepcopy

from tsdaf.contracts.literature.objects import experiment_containers, reported_outcomes
from tsdaf.lanes.literature.condition_categories import FIXED_CONDITION_FIELDS, normalize_condition
from tsdaf.lanes.literature.material_bindings import bound_rows


def inherit_measurement_context(item, previous):
    """Effective same-item omissions, shared by acceptance and verified replay."""
    if item.aspect == "yield" and previous is not None and previous.aspect == "yield":
        inherited = {key: getattr(previous.words, key) for key in ("measurement", "experiment")
                     if key not in item.words.model_fields_set and hasattr(previous.words, key)}
        if inherited:
            item = item.model_copy(update={"words": item.words.model_copy(update=inherited)})
    return item


def comparison_value(item, previous):
    return inherit_measurement_context(item, previous).model_dump(mode="json")


def current_target(value, machine, previous_target=None):
    aspect = value.get("aspect")
    if aspect in {"condition", "material"} and value.get("scope") == "ANOTHER_EXPERIMENT":
        return None
    words = value.get("words")
    words = words if isinstance(words, Mapping) else {}
    target = None
    selected_id = (
        words.get("same_measurement_as") if aspect == "characterization"
        else (words.get("measurement") or {}).get("same_as") if aspect == "yield"
        else value.get("skeleton_material_id") if aspect == "material" else None
    )
    if (previous_target is not None and selected_id is not None
            and previous_target["kind"] == aspect and previous_target["id"] == selected_id):
        if (aspect != "yield" and value.get("about_ids")
                and set(value["about_ids"]) != set(previous_target["about_ids"])):
            raise ValueError("selected current result and about_ids name different objects")
        return deepcopy(previous_target)
    if aspect == "characterization" and words.get("same_measurement_as"):
        key = words["same_measurement_as"]
        card = next((c for c in machine["domain_payload"]["characterization_records"]
                     if c["characterization_id"] == key), None)
        if card is None:
            raise ValueError("same_measurement_as must select a current characterization card")
        target = {"kind": aspect, "id": key, "about_ids": card["compound_ids"]}
    elif aspect == "yield" and (words.get("measurement") or {}).get("same_as"):
        key = words["measurement"]["same_as"]
        if not any(o["outcome_id"] == key for o in reported_outcomes(machine)):
            raise ValueError("measurement.same_as must select a current measurement")
        target = {"kind": aspect, "id": key, "about_ids": [key]}
    elif aspect == "material" and value.get("skeleton_material_id"):
        key = value["skeleton_material_id"]
        row = bound_rows(machine, machine.get("_material_bindings")).get(key)
        if row is None:
            raise ValueError("skeleton_material_id must select a current material position")
        target = {"kind": aspect, "id": key, "about_ids": [row["operation_id"]]}
    elif aspect == "condition" and value.get("scope") in {"WHOLE", "SEGMENT"}:
        owners = value.get("about_ids", ())
        if len(owners) != 1:
            return None
        operation = next((o for c in experiment_containers(machine) for o in c["operations"]
                          if o["operation_id"] == owners[0]), None)
        if operation is None:
            raise ValueError("condition must select a current operation")
        category = normalize_condition(value.get("category_word") or "",
            value.get("words") if isinstance(value.get("words"), str) else "",
            about=value.get("condition_about"))
        stage = value.get("existing_stage_id")
        if value["scope"] == "SEGMENT":
            if stage is None:
                return None
            if not any(s["stage_id"] == stage for s in operation.get("stages", ())):
                raise ValueError("condition must select a current stage")
        elif category not in FIXED_CONDITION_FIELDS:
            return None
        target = {"kind": aspect, "id": [owners[0], category, stage], "about_ids": list(owners)}
    if target is not None and value.get("about_ids"):
        # An explicit wider yield context is already checked by the existing yield
        # resolver. A card/material owner cannot be silently changed by its selector.
        if aspect != "yield" and set(value["about_ids"]) != set(target["about_ids"]):
            raise ValueError("selected current result and about_ids name different objects")
    return target


def update_binding(value, previous, submitted, request_binding, machine):
    changes = submitted.get("edit", {}).get("changes", {}) if "edit" in submitted else submitted
    prior_target = previous.get("target") if previous else None
    target = current_target(value, machine, prior_target)
    explicit = changes.get("intent") is not None
    if previous is not None and target != prior_target and not explicit:
        raise ValueError("changing the selected target requires explicit intent RECORD or UPDATE_CURRENT")
    intent = changes.get("intent") if explicit else (
        previous.get("resolved_intent", "RECORD") if previous else "RECORD")
    if intent == "UPDATE_CURRENT" and target is None:
        raise ValueError("UPDATE_CURRENT requires one explicit current result selection")
    if target is None:
        return None
    if request_binding is None:
        raise ValueError("current-result selection needs its original request comparison")
    words = changes.get("words", {})
    selector = (isinstance(words, Mapping) and (
        "measurement" in words if value.get("aspect") == "yield"
        else "same_measurement_as" in words if value.get("aspect") == "characterization"
        else False
    )) or "skeleton_material_id" in changes or "existing_stage_id" in changes
    same_snapshot = (previous is not None
        and previous.get("snapshot_ref") is not None
        and previous["snapshot_ref"] == request_binding.get("snapshot_ref"))
    if previous is not None and target == prior_target and (
        not (explicit or selector) or same_snapshot
    ):
        # Keep the selected comparison, even after its own contribution has changed
        # current. New source data do not become their own baseline.
        return {**deepcopy(previous), "resolved_intent": intent}
    return {**deepcopy(request_binding), "target": target, "resolved_intent": intent}


def resolved_intent(finding):
    from tsdaf.lanes.literature.middle_layer import CandidateFindingV2

    if not isinstance(finding, CandidateFindingV2) or finding.aspect not in {"condition", "material", "characterization", "yield"}:
        return None
    binding = finding._measurement_binding
    if binding is not None:
        return binding["resolved_intent"]
    if finding.intent == "UPDATE_CURRENT":
        raise ValueError("explicit update has no verified current-result binding")
    return "RECORD"


def resolve_answer(answer, comparisons=None):
    """Private working view; do not persist inherited fields as the model's words."""
    from tsdaf.lanes.literature.middle_layer import CandidateFindingV2

    items = []
    for finding in answer.findings:
        if not isinstance(finding, CandidateFindingV2):
            items.append(finding)
            continue
        binding = (comparisons or {}).get(finding.item_id, {}).get("_finding_binding",
                                                                          finding._measurement_binding)
        item = finding.model_copy()
        item._measurement_binding = binding
        resolved_intent(item)
        if binding is not None and not item.about_ids:
            item = item.model_copy(update={"about_ids": tuple(binding["target"]["about_ids"])})
        items.append(item)
    return answer.model_copy(update={"findings": tuple(items)})
