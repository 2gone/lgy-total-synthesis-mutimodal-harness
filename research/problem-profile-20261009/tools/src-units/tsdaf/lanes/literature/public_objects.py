"""Exact object positions in a public snapshot, never matches on scientific wording."""

from typing import Any

from tsdaf.lanes.literature.material_bindings import material_position


def public_object_records(value: Any) -> dict[str, tuple[str, dict[str, Any]]]:
    """Identify definitions, not references such as a material's compound_id."""
    positions: dict[str, tuple[str, dict[str, Any]]] = {}

    def visit(node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            for field, defining in (
                ("compound_id", "author_labels"),
                ("operation_id", "materials"),
                ("outcome_id", "reported_value"),
                ("reaction_block_id", "operations"),
                ("characterization_id", "method_verbatim"),
                ("stage_id", "description"),
                ("route_id", "reaction_block_ids"),
                ("narrative_id", "author_text"),
                ("narrative_id", "explanation"),
                ("account_id", "operations"),
                ("material_use_id", "verbatim_name"),
                ("local_intermediate_id", "produced_by_operation_id"),
                ("local_intermediate_id", "produced_by"),
                ("unresolved_item_id", "reason_code"),
                ("target_path_id", "target_compound_id"),
                ("assertion_id", "author_statement"),
                ("conflict_id", "candidate_values"),
            ):
                if defining in node and isinstance(node.get(field), str):
                    positions.setdefault(node[field], (pointer, node))
            if "operation_id" in node and isinstance(node.get("materials"), dict):
                for index, _row in enumerate(node["materials"].get("value") or (), 1):
                    positions[material_position(node["operation_id"], index)] = (
                        pointer + f"/materials/value/{index - 1}",
                        _row,
                    )
            for key, child in node.items():
                visit(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, pointer + "/" + str(index))

    visit(value, "")
    return positions


def public_object_pointers(value: Any) -> dict[str, str]:
    """Read locations and model naming consume the same definition inventory."""
    return {identity: pointer for identity, (pointer, _) in public_object_records(value).items()}
