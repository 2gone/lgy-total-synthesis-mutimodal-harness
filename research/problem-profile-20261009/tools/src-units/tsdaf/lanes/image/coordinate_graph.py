"""Parsing and flattening safety for the `v:mg1` coordinate graph.

`v:mg1` is the OCSR service's structure contract: an ordered op list carrying atom
and label nodes with in-crop coordinates, bonds, and explicitly ambiguous
constructs. It is deliberately richer than SMILES, so a graph is only safe to
flatten once nothing in it has been silently dropped.

The provider's own vocabulary is not self-consistent: its compact-graph parser
accepts `abbr` but not `abbrev`, and knows no `position_variation_bond`, while its
MOL exporter handles both `abbrev` and `position_variation_bond` and knows no
`ring`/`repeat`/`comment`. Its `normalize_bond_type` also silently rewrites any
unrecognized bond to `single`. This module therefore never rejects a graph for
unknown vocabulary and never rewrites it: unknown ops, node kinds and bond types
are preserved verbatim and reported, and `classify_resolvability` treats them as
blocking. An unknown construct is a construct whose information we cannot prove we
kept, which is exactly the case where flattening must not happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = (
    "CoordinateGraph",
    "CoordinateGraphError",
    "FlattenBlocker",
    "GraphBond",
    "GraphNode",
    "GraphResolution",
    "Resolvability",
    "classify_resolvability",
    "parse_coordinate_graph",
)

SCHEMA_TAG = "mg1"
"""Value of the graph's `v` field. The service versions the graph here, not in a URL."""

COORDINATE_MAX = 1000
"""Coordinates are `crop_xy_1000`: origin at the crop's top-left, 0-1000 on both axes."""

# Semantic-patch v1 kinds plus the legacy compact-graph aliases the provider still
# emits from older runs. `abbrev` and `placeholder` are absent from the provider's
# own _VALID_KINDS but are handled by its exporter and documented in its 8010 API,
# so they are known here.
_ATOM_KINDS = frozenset({"atom"})
_LABEL_KINDS = frozenset(
    {"substituent", "abbrev", "abbr", "attachment_point", "dum", "placeholder"}
)
_GENERIC_KINDS = frozenset({"r_group", "variable", "r", "var"})
_STRUCTURAL_KINDS = frozenset({"ring"})
_KNOWN_NODE_KINDS = _ATOM_KINDS | _LABEL_KINDS | _GENERIC_KINDS | _STRUCTURAL_KINDS

_KNOWN_BOND_TYPES = frozenset(
    {"single", "double", "triple", "aromatic", "wavy", "single_wedge", "single_hashed", "dative"}
)

_NODE_OP = "n"
_BOND_OP = "e"
# Ops that carry no structure we would lose by flattening.
_INERT_OPS = frozenset({"group", "comment"})
# Ops that carry structure SMILES cannot express. `or` and `position_variation_bond`
# both encode "this connection is one of several"; the provider's MOL exporter has no
# branch for `or` at all, so its candidate bonds vanish and the fragment they would
# have attached is emitted disconnected.
_AMBIGUOUS_OPS = frozenset({"or"})
_POLYMER_OPS = frozenset({"polymer_bracket", "repeat", "ring"})
_POSITION_VARIATION_OPS = frozenset({"position_variation_bond"})
_KNOWN_OPS = (
    {_NODE_OP, _BOND_OP} | _INERT_OPS | _AMBIGUOUS_OPS | _POLYMER_OPS | _POSITION_VARIATION_OPS
)


class CoordinateGraphError(ValueError):
    """The payload is not a well-formed `v:mg1` graph."""


class FlattenBlocker(StrEnum):
    """Why a graph cannot become a single concrete structure."""

    AMBIGUOUS_POSITION = "AMBIGUOUS_POSITION"
    POLYMER_UNIT = "POLYMER_UNIT"
    UNEXPANDED_LABEL = "UNEXPANDED_LABEL"
    GENERIC_SITE = "GENERIC_SITE"
    UNKNOWN_VOCABULARY = "UNKNOWN_VOCABULARY"
    NO_ATOMS = "NO_ATOMS"


class Resolvability(StrEnum):
    """How far a graph may be projected.

    `GENERIC` is a real answer, not a failure: an R-group graph is a valid
    substructure query even though it is not one molecule.
    """

    RESOLVED = "RESOLVED"
    GENERIC = "GENERIC"
    UNRESOLVABLE = "UNRESOLVABLE"


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    kind: str
    label: str
    x: float
    y: float
    metadata: dict[str, Any]

    @property
    def semantic_kind(self) -> str:
        """The node's real type. The op's kind slot is only a fallback.

        The provider writes the semantic type into node metadata and its own MOL
        exporter resolves it as `meta["type"] or meta["semantic_type"] or kind`.
        Observed on a real crop from a total-synthesis scheme: a node carrying the
        abbreviation label "OTES" was emitted with kind slot "atom" and metadata type
        "substituent". Reading the slot alone classifies an unexpanded abbreviation as
        a concrete atom, which is exactly the graph that must not be flattened.
        """

        meta_type = self.metadata.get("type") or self.metadata.get("semantic_type")
        return str(meta_type) if meta_type else self.kind

    @property
    def is_known_kind(self) -> bool:
        return self.semantic_kind in _KNOWN_NODE_KINDS


@dataclass(frozen=True, slots=True)
class GraphBond:
    source_id: str
    target_id: str
    bond_type: str

    @property
    def is_known_type(self) -> bool:
        return self.bond_type in _KNOWN_BOND_TYPES


@dataclass(frozen=True, slots=True)
class CoordinateGraph:
    """One parsed `v:mg1` graph. Op order is preserved; nothing is normalized."""

    nodes: tuple[GraphNode, ...]
    bonds: tuple[GraphBond, ...]
    other_ops: tuple[str, ...]
    unknown_ops: tuple[str, ...]

    def nodes_of_kind(self, kinds: frozenset[str]) -> tuple[GraphNode, ...]:
        return tuple(node for node in self.nodes if node.semantic_kind in kinds)


def _require_list_op(op: object, index: int) -> list[Any]:
    if not isinstance(op, list) or not op:
        raise CoordinateGraphError(f"v:mg1 op {index} is not a nonempty list")
    name = op[0]
    if not isinstance(name, str) or not name:
        raise CoordinateGraphError(f"v:mg1 op {index} has no op name")
    return op


def _parse_node(op: list[Any], index: int) -> GraphNode:
    if len(op) < 5:
        raise CoordinateGraphError(f"v:mg1 node op {index} is too short")
    node_id, kind, label, xy = op[1], op[2], op[3], op[4]
    if not isinstance(node_id, str) or not node_id:
        raise CoordinateGraphError(f"v:mg1 node op {index} has no node ID")
    if not isinstance(kind, str) or not kind:
        raise CoordinateGraphError(f"v:mg1 node op {index} has no kind")
    if not isinstance(xy, list) or len(xy) < 2:
        raise CoordinateGraphError(f"v:mg1 node op {index} has no coordinate pair")
    coords: list[float] = []
    for value in xy[:2]:
        # bool is an int subclass; a boolean coordinate is a malformed payload.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise CoordinateGraphError(f"v:mg1 node op {index} coordinate is not a number")
        if not 0 <= float(value) <= COORDINATE_MAX:
            raise CoordinateGraphError(f"v:mg1 node op {index} coordinate is out of frame")
        coords.append(float(value))
    metadata = op[5] if len(op) > 5 and isinstance(op[5], dict) else {}
    return GraphNode(
        node_id=node_id,
        kind=kind,
        label=str(label),
        x=coords[0],
        y=coords[1],
        metadata=dict(metadata),
    )


def _parse_bond(op: list[Any], index: int) -> GraphBond:
    if len(op) < 4:
        raise CoordinateGraphError(f"v:mg1 bond op {index} is too short")
    source_id, target_id, bond_type = op[1], op[2], op[3]
    if not isinstance(source_id, str) or not isinstance(target_id, str):
        raise CoordinateGraphError(f"v:mg1 bond op {index} has a non-string endpoint")
    if not isinstance(bond_type, str) or not bond_type:
        raise CoordinateGraphError(f"v:mg1 bond op {index} has no bond type")
    return GraphBond(source_id=source_id, target_id=target_id, bond_type=bond_type)


def parse_coordinate_graph(payload: object) -> CoordinateGraph:
    """Parse one `v:mg1` payload, rejecting only what is structurally malformed.

    Unknown op names, node kinds and bond types are preserved and reported rather
    than rejected, because the provider's parser and exporter disagree about the
    vocabulary and either side may gain entries. Structural faults -- a missing node
    ID, a dangling bond endpoint, an out-of-frame coordinate -- are rejected, because
    they make the graph unusable rather than merely unfamiliar.
    """

    if not isinstance(payload, dict):
        raise CoordinateGraphError("v:mg1 payload must be a JSON object")
    if payload.get("v") != SCHEMA_TAG:
        raise CoordinateGraphError("v:mg1 payload is not tagged mg1")
    ops = payload.get("g")
    if not isinstance(ops, list):
        raise CoordinateGraphError("v:mg1 payload has no op list")

    nodes: list[GraphNode] = []
    bonds: list[GraphBond] = []
    other_ops: list[str] = []
    unknown_ops: list[str] = []
    for index, raw_op in enumerate(ops):
        op = _require_list_op(raw_op, index)
        name = op[0]
        if name == _NODE_OP:
            nodes.append(_parse_node(op, index))
        elif name == _BOND_OP:
            bonds.append(_parse_bond(op, index))
        else:
            other_ops.append(name)
            if name not in _KNOWN_OPS:
                unknown_ops.append(name)

    node_ids = {node.node_id for node in nodes}
    if len(node_ids) != len(nodes):
        raise CoordinateGraphError("v:mg1 node IDs are not unique")
    for bond in bonds:
        if bond.source_id not in node_ids or bond.target_id not in node_ids:
            raise CoordinateGraphError("v:mg1 bond references an undeclared node")

    return CoordinateGraph(
        nodes=tuple(nodes),
        bonds=tuple(bonds),
        other_ops=tuple(other_ops),
        unknown_ops=tuple(dict.fromkeys(unknown_ops)),
    )


@dataclass(frozen=True, slots=True)
class GraphResolution:
    """How far this graph may be projected, and every reason it stops there."""

    resolvability: Resolvability
    blockers: tuple[FlattenBlocker, ...]

    @property
    def may_flatten(self) -> bool:
        """True only for one concrete structure. `GENERIC` is queryable, not flattenable."""

        return self.resolvability is Resolvability.RESOLVED


def classify_resolvability(graph: CoordinateGraph) -> GraphResolution:
    """Decide whether `graph` may be projected to a single concrete structure.

    Every blocker is reported, not just the first, so a rejection says what a human
    or a targeted rerun would have to resolve. Callers must not produce a structure
    fact when `may_flatten` is false: the provider's exporter drops `or` candidate
    bonds without an error, and the resulting disconnected fragment parses cleanly,
    so a silent flatten yields a wrong molecule rather than a visible failure.
    """

    blockers: set[FlattenBlocker] = set()

    if graph.unknown_ops:
        blockers.add(FlattenBlocker.UNKNOWN_VOCABULARY)
    if any(not node.is_known_kind for node in graph.nodes):
        blockers.add(FlattenBlocker.UNKNOWN_VOCABULARY)
    if any(not bond.is_known_type for bond in graph.bonds):
        blockers.add(FlattenBlocker.UNKNOWN_VOCABULARY)

    ops = set(graph.other_ops)
    if ops & (_AMBIGUOUS_OPS | _POSITION_VARIATION_OPS):
        blockers.add(FlattenBlocker.AMBIGUOUS_POSITION)
    if ops & _POLYMER_OPS:
        blockers.add(FlattenBlocker.POLYMER_UNIT)

    if graph.nodes_of_kind(_LABEL_KINDS):
        blockers.add(FlattenBlocker.UNEXPANDED_LABEL)
    if graph.nodes_of_kind(_GENERIC_KINDS):
        blockers.add(FlattenBlocker.GENERIC_SITE)
    if not graph.nodes_of_kind(_ATOM_KINDS):
        blockers.add(FlattenBlocker.NO_ATOMS)

    ordered = tuple(sorted(blockers, key=lambda blocker: blocker.value))
    if not ordered:
        return GraphResolution(resolvability=Resolvability.RESOLVED, blockers=())
    # A generic site alone still yields a usable substructure query; anything else
    # means information was lost or never determined, so no structure fact at all.
    if ordered == (FlattenBlocker.GENERIC_SITE,):
        return GraphResolution(resolvability=Resolvability.GENERIC, blockers=ordered)
    return GraphResolution(resolvability=Resolvability.UNRESOLVABLE, blockers=ordered)
