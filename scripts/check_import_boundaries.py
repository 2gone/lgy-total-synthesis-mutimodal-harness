from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

RULE_IDS = (
    "IMP001_LEGACY_IMPORT",
    "IMP002_LAYER_IMPORT",
    "IMP003_CONTRACTS_DEPENDENCY",
    "IMP004_ENVIRONMENT_ACCESS",
    "IMP005_IMPORT_CYCLE",
)

_LAYERS = {"contracts", "lanes", "workflow", "adapters", "apps"}
_ALLOWED_TARGETS = {
    "contracts": {"contracts"},
    "lanes": {"contracts", "lanes"},
    "workflow": {"contracts", "lanes", "workflow"},
    "adapters": {"contracts", "adapters"},
    "apps": _LAYERS,
}
_CONTRACTS_FORBIDDEN = (
    "google.genai",
    "google.generativeai",
    "prefect",
    "sqlite3",
    "aiosqlite",
    "argparse",
    "click",
    "typer",
    "pydantic_settings",
    "tsdaf.apps",
    "tsdaf.adapters",
)


@dataclass(frozen=True, slots=True, order=True)
class ImportEdge:
    source: str
    target: str
    path: Path
    line: int


@dataclass(frozen=True, slots=True, order=True)
class Violation:
    path: Path
    line: int
    rule_id: str
    message: str


@dataclass(frozen=True, slots=True)
class BoundaryReport:
    modules: tuple[str, ...]
    edges: tuple[ImportEdge, ...]
    violations: tuple[Violation, ...]


class BoundaryCheckError(Exception):
    """Raised when a source tree cannot be checked reliably."""


@dataclass(frozen=True, slots=True)
class _ParsedModule:
    name: str
    path: Path
    is_package: bool
    tree: ast.Module


def _module_name(source_root: Path, path: Path) -> tuple[str, bool]:
    relative = path.relative_to(source_root)
    is_package = relative.name == "__init__.py"
    parts = relative.parts[:-1] if is_package else (*relative.parts[:-1], relative.stem)
    suffix = ".".join(parts)
    return ("tsdaf" if not suffix else f"tsdaf.{suffix}"), is_package


def _read_modules(source_root: Path) -> tuple[_ParsedModule, ...]:
    if not source_root.is_dir():
        raise BoundaryCheckError(f"source root is not a directory: {source_root}")
    paths = tuple(sorted(path for path in source_root.rglob("*.py") if path.is_file()))
    if not paths:
        raise BoundaryCheckError(f"source root contains no Python files: {source_root}")
    if not (source_root / "__init__.py").is_file():
        raise BoundaryCheckError(f"source root is not a tsdaf package: {source_root}")

    modules: list[_ParsedModule] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise BoundaryCheckError(f"cannot read UTF-8 source {path}: {exc}") from exc
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            line = exc.lineno or 0
            raise BoundaryCheckError(f"cannot parse {path}:{line}: {exc.msg}") from exc
        name, is_package = _module_name(source_root, path)
        modules.append(_ParsedModule(name, path, is_package, tree))
    return tuple(modules)


def _import_candidates(node: ast.Import | ast.ImportFrom, source: _ParsedModule) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)

    if node.level:
        package = source.name.split(".") if source.is_package else source.name.split(".")[:-1]
        ascend = node.level - 1
        if ascend > len(package):
            base = ""
        else:
            retained = package[: len(package) - ascend]
            imported = node.module.split(".") if node.module else []
            base = ".".join((*retained, *imported))
    else:
        base = node.module or ""
    candidates = [base] if base else []
    if base:
        candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return tuple(candidates)


def _matches_prefix(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(f"{prefix}.")


def _internal_target(candidate: str, module_names: set[str]) -> str | None:
    matches = [name for name in module_names if _matches_prefix(candidate, name)]
    return max(matches, key=len) if matches else None


def _layer(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[1] in _LAYERS else None


def _contracts_forbidden(candidate: str) -> str | None:
    return next(
        (prefix for prefix in _CONTRACTS_FORBIDDEN if _matches_prefix(candidate, prefix)),
        None,
    )


def _legacy_import(candidate: str) -> bool:
    return _matches_prefix(candidate, "harness") or _matches_prefix(
        candidate, "tests.legacy_harness"
    )


_Atom = str
_Binding = frozenset[_Atom]
_SAFE_BINDING: _Binding = frozenset()
_ATOMS = frozenset(
    {
        "os",
        "os.getenv",
        "os.environ",
        "pydantic_settings",
        "pydantic_settings.BaseSettings",
    }
)
_ENVIRONMENT_ATOMS = frozenset({"os.getenv", "os.environ", "pydantic_settings.BaseSettings"})
_FUNCTION_SCOPES = {"function", "lambda", "comprehension"}


def _import_bindings(node: ast.Import | ast.ImportFrom) -> tuple[tuple[str, _Binding], ...]:
    bindings: list[tuple[str, _Binding]] = []
    if isinstance(node, ast.Import):
        for imported in node.names:
            local = imported.asname or imported.name.split(".")[0]
            target = imported.name if imported.asname else imported.name.split(".")[0]
            bindings.append((local, frozenset((target,))))
    elif node.level == 0 and node.module:
        for imported in node.names:
            if imported.name != "*":
                target = f"{node.module}.{imported.name}"
                bindings.append((imported.asname or imported.name, frozenset((target,))))
    return tuple(bindings)


def _project_binding(binding: _Binding) -> _Binding:
    return frozenset(candidate for candidate in binding if candidate in _ATOMS)


def _attribute_binding(binding: _Binding, attribute: str) -> _Binding:
    projected: set[_Atom] = set()
    for atom in binding:
        if atom == "os" and attribute in {"getenv", "environ"}:
            projected.add(f"os.{attribute}")
        elif atom == "pydantic_settings" and attribute == "BaseSettings":
            projected.add("pydantic_settings.BaseSettings")
        elif atom in _ENVIRONMENT_ATOMS:
            projected.add(atom)
    return frozenset(projected)


class _DeclarationCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.bound.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        self.bound.update(name for name, _ in _import_bindings(node))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0:
            self.bound.update(name for name, _ in _import_bindings(node))
        else:
            self.bound.update(
                imported.asname or imported.name for imported in node.names if imported.name != "*"
            )

    def visit_Global(self, node: ast.Global) -> None:
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.nonlocals.update(node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.bound.add(node.name)
        for expression in _function_eager_expressions(node):
            self.visit(expression)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.bound.add(node.name)
        for expression in _function_eager_expressions(node):
            self.visit(expression)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.bound.add(node.name)
        for expression in _class_eager_expressions(node):
            self.visit(expression)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in _argument_expressions(node.args):
            self.visit(expression)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        return

    def visit_SetComp(self, node: ast.SetComp) -> None:
        return

    def visit_DictComp(self, node: ast.DictComp) -> None:
        return

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        return

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.bound.add(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.bound.add(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.bound.add(node.rest)
        for pattern in node.patterns:
            self.visit(pattern)


def _argument_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _argument_expressions(arguments: ast.arguments) -> tuple[ast.expr, ...]:
    expressions: list[ast.expr] = [*arguments.defaults]
    expressions.extend(default for default in arguments.kw_defaults if default is not None)
    expressions.extend(
        argument.annotation for argument in arguments.args if argument.annotation is not None
    )
    expressions.extend(
        argument.annotation for argument in arguments.posonlyargs if argument.annotation is not None
    )
    if arguments.vararg is not None and arguments.vararg.annotation is not None:
        expressions.append(arguments.vararg.annotation)
    expressions.extend(
        argument.annotation for argument in arguments.kwonlyargs if argument.annotation is not None
    )
    if arguments.kwarg is not None and arguments.kwarg.annotation is not None:
        expressions.append(arguments.kwarg.annotation)
    return tuple(expressions)


def _function_eager_expressions(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[ast.expr, ...]:
    returns = () if node.returns is None else (node.returns,)
    return (*node.decorator_list, *_argument_expressions(node.args), *returns)


def _class_eager_expressions(node: ast.ClassDef) -> tuple[ast.expr, ...]:
    return (
        *node.decorator_list,
        *node.bases,
        *(keyword.value for keyword in node.keywords),
    )


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return set().union(*(_target_names(item) for item in target.elts), set())
    return set()


@dataclass(frozen=True, slots=True, order=True)
class _Slot:
    scope_id: int
    name: str


@dataclass(frozen=True, slots=True)
class _Cell:
    aliases: _Binding = _SAFE_BINDING
    may_be_unbound: bool = True


@dataclass(frozen=True, slots=True)
class _ScopeInfo:
    scope_id: int
    kind: str
    parent_id: int | None
    locals: frozenset[str]
    parameters: frozenset[str]
    globals: frozenset[str]
    nonlocals: frozenset[str]


@dataclass(slots=True)
class _Unit:
    unit_id: int
    scope_id: int
    kind: str
    body: tuple[ast.AST, ...]
    visible_slots: frozenset[_Slot] = frozenset()


@dataclass(slots=True)
class _State:
    cells: dict[_Slot, _Cell]
    active_readers: frozenset[int] = frozenset()

    def copy(self) -> _State:
        return _State(self.cells.copy(), self.active_readers)


@dataclass(slots=True)
class _Flow:
    normal: _State | None = None
    breaks: _State | None = None
    continues: _State | None = None
    returns: _State | None = None
    raises: _State | None = None


@dataclass(slots=True)
class _ExprFlow:
    truthy: _State | None
    falsey: _State | None
    raises: _State | None
    binding: _Binding

    def normal(self) -> _State | None:
        return _join_state(self.truthy, self.falsey)


@dataclass(slots=True)
class _Summary:
    envelope: dict[int, dict[_Slot, _Binding]]
    effect: dict[int, dict[_Slot, _Binding]]
    active_units: set[int]


@dataclass(slots=True)
class _Context:
    unit_id: int
    report: bool
    old_effect: dict[int, dict[_Slot, _Binding]]
    summary: _Summary | None


def _scope_metadata(
    nodes: Sequence[ast.AST],
    parameters: set[str] | None = None,
    extra_locals: set[str] | None = None,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    declarations = _DeclarationCollector()
    for node in nodes:
        declarations.visit(node)
    local_names = declarations.bound | (parameters or set()) | (extra_locals or set())
    local_names -= declarations.globals | declarations.nonlocals
    return (
        frozenset(local_names),
        frozenset(declarations.globals),
        frozenset(declarations.nonlocals),
    )


class _ScopeGraphBuilder(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.scopes: dict[int, _ScopeInfo] = {}
        self.units: dict[int, _Unit] = {}
        self.node_scopes: dict[int, int] = {}
        self.node_units: dict[int, int] = {}
        self._scope_specs: dict[
            int, tuple[Sequence[ast.AST], set[str] | None, set[str] | None]
        ] = {}
        self._comprehension_walrus: dict[int, set[str]] = {}
        self.current_scope = 0
        self._next_scope = 0
        self._next_unit = 0

        module_scope = self._add_scope("module", None, tree.body)
        self.node_scopes[id(tree)] = module_scope
        self.units[0] = _Unit(0, module_scope, "module", tuple(tree.body))
        self._next_unit = 1
        for statement in tree.body:
            self.visit(statement)
        self._finalize_scopes()
        self.all_slots = self._build_slots()
        for unit in self.units.values():
            unit.visible_slots = frozenset(
                slot for slot in self.all_slots if slot.scope_id != unit.scope_id
            )

    def _add_scope(
        self,
        kind: str,
        parent_id: int | None,
        nodes: Sequence[ast.AST],
        parameters: set[str] | None = None,
        extra_locals: set[str] | None = None,
    ) -> int:
        scope_id = self._next_scope
        self._next_scope += 1
        self.scopes[scope_id] = _ScopeInfo(
            scope_id,
            kind,
            parent_id,
            frozenset(),
            frozenset(),
            frozenset(),
            frozenset(),
        )
        self._scope_specs[scope_id] = (nodes, parameters, extra_locals)
        return scope_id

    def _finalize_scopes(self) -> None:
        for scope_id, scope in tuple(self.scopes.items()):
            nodes, parameters, extra_locals = self._scope_specs[scope_id]
            local, globals_, nonlocals = _scope_metadata(nodes, parameters, extra_locals)
            local |= frozenset(self._comprehension_walrus.get(scope_id, set()))
            local -= globals_ | nonlocals
            self.scopes[scope_id] = _ScopeInfo(
                scope_id,
                scope.kind,
                scope.parent_id,
                local,
                frozenset(parameters or set()),
                globals_,
                nonlocals,
            )

    def _add_unit(self, node: ast.AST, scope_id: int, kind: str, body: tuple[ast.AST, ...]) -> None:
        unit_id = self._next_unit
        self._next_unit += 1
        self.units[unit_id] = _Unit(unit_id, scope_id, kind, body)
        self.node_units[id(node)] = unit_id

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expression in _function_eager_expressions(node):
            self.visit(expression)
        parent = self.current_scope
        scope_id = self._add_scope("function", parent, node.body, _argument_names(node.args))
        self.node_scopes[id(node)] = scope_id
        self._add_unit(node, scope_id, "function", tuple(node.body))
        self.current_scope = scope_id
        for statement in node.body:
            self.visit(statement)
        self.current_scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for expression in _argument_expressions(node.args):
            self.visit(expression)
        parent = self.current_scope
        scope_id = self._add_scope("lambda", parent, (node.body,), _argument_names(node.args))
        self.node_scopes[id(node)] = scope_id
        self._add_unit(node, scope_id, "lambda", (node.body,))
        self.current_scope = scope_id
        self.visit(node.body)
        self.current_scope = parent

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in _class_eager_expressions(node):
            self.visit(expression)
        parent = self.current_scope
        scope_id = self._add_scope("class", parent, node.body)
        self.node_scopes[id(node)] = scope_id
        self.current_scope = scope_id
        for statement in node.body:
            self.visit(statement)
        self.current_scope = parent

    def _visit_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        results: tuple[ast.AST, ...],
        *,
        generator: bool,
    ) -> None:
        self.visit(node.generators[0].iter)
        parent = self.current_scope
        targets = set().union(*(_target_names(item.target) for item in node.generators))
        scope_id = self._add_scope("comprehension", parent, (), extra_locals=targets)
        self.node_scopes[id(node)] = scope_id
        if generator:
            self._add_unit(node, scope_id, "generator", (node,))
        self.current_scope = scope_id
        first = node.generators[0]
        for condition in first.ifs:
            self.visit(condition)
        for item in node.generators[1:]:
            self.visit(item.iter)
            for condition in item.ifs:
                self.visit(condition)
        for result in results:
            self.visit(result)
        self.current_scope = parent

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, (node.elt,), generator=False)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, (node.elt,), generator=False)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, (node.key, node.value), generator=False)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, (node.elt,), generator=True)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        scope_id = self.current_scope
        if self.scopes[scope_id].kind == "comprehension":
            owner_id = scope_id
            while self.scopes[owner_id].kind == "comprehension":
                parent_id = self.scopes[owner_id].parent_id
                assert parent_id is not None
                owner_id = parent_id
            if isinstance(node.target, ast.Name):
                self._comprehension_walrus.setdefault(owner_id, set()).add(node.target.id)
        self.generic_visit(node)

    def _all_names(self) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.ExceptHandler) and node.name is not None:
                names.add(node.name)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.split(".")[0])
            else:
                name = getattr(node, "name", None)
                if isinstance(node, ast.MatchAs | ast.MatchStar) and name is not None:
                    names.add(name)
                rest = getattr(node, "rest", None)
                if isinstance(node, ast.MatchMapping) and rest is not None:
                    names.add(rest)
        for scope in self.scopes.values():
            names.update(scope.locals | scope.globals | scope.nonlocals)
        return names

    def _build_slots(self) -> frozenset[_Slot]:
        slots: set[_Slot] = set()
        names = self._all_names()
        for scope_id in self.scopes:
            for name in names:
                slots.add(self.resolve_slot(scope_id, name, store=False))
                slots.add(self.resolve_slot(scope_id, name, store=True))
                if self.scopes[scope_id].kind == "class":
                    slots.add(self.resolve_free_slot(scope_id, name))
        return frozenset(slots)

    def module_scope(self) -> int:
        return 0

    def resolve_free_slot(self, scope_id: int, name: str) -> _Slot:
        parent_id = self.scopes[scope_id].parent_id
        while parent_id is not None:
            parent = self.scopes[parent_id]
            if parent.kind in _FUNCTION_SCOPES and name in parent.locals:
                return _Slot(parent_id, name)
            if parent.kind == "module":
                return _Slot(parent_id, name)
            parent_id = parent.parent_id
        return _Slot(self.module_scope(), name)

    def resolve_slot(self, scope_id: int, name: str, *, store: bool) -> _Slot:
        scope = self.scopes[scope_id]
        if name in scope.globals:
            return _Slot(self.module_scope(), name)
        if name in scope.nonlocals:
            parent_id = scope.parent_id
            while parent_id is not None:
                parent = self.scopes[parent_id]
                if parent.kind in _FUNCTION_SCOPES and name in parent.locals:
                    return _Slot(parent_id, name)
                parent_id = parent.parent_id
            return _Slot(self.module_scope(), name)
        if scope.kind == "module":
            return _Slot(scope_id, name)
        if scope.kind == "class":
            return _Slot(scope_id, name)
        if name in scope.locals:
            return _Slot(scope_id, name)
        return self.resolve_free_slot(scope_id, name)


def _join_cell(left: _Cell, right: _Cell) -> _Cell:
    return _Cell(
        left.aliases | right.aliases,
        left.may_be_unbound or right.may_be_unbound,
    )


def _join_state(*states: _State | None) -> _State | None:
    reachable = [state for state in states if state is not None]
    if not reachable:
        return None
    keys = set().union(*(state.cells for state in reachable))
    cells: dict[_Slot, _Cell] = {}
    for key in keys:
        cell = reachable[0].cells.get(key, _Cell())
        for state in reachable[1:]:
            cell = _join_cell(cell, state.cells.get(key, _Cell()))
        cells[key] = cell
    readers = frozenset().union(*(state.active_readers for state in reachable))
    return _State(cells, readers)


def _join_flow(*flows: _Flow) -> _Flow:
    return _Flow(
        normal=_join_state(*(flow.normal for flow in flows)),
        breaks=_join_state(*(flow.breaks for flow in flows)),
        continues=_join_state(*(flow.continues for flow in flows)),
        returns=_join_state(*(flow.returns for flow in flows)),
        raises=_join_state(*(flow.raises for flow in flows)),
    )


def _is_irrefutable(pattern: ast.pattern) -> bool:
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _is_irrefutable(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return any(_is_irrefutable(item) for item in pattern.patterns)
    return False


class _EnvironmentResolver:
    def __init__(self, tree: ast.Module) -> None:
        self.graph = _ScopeGraphBuilder(tree)
        self.lines: set[int] = set()
        self.envelope: dict[int, dict[_Slot, _Binding]] = {
            unit_id: {} for unit_id in self.graph.units
        }
        self.effect: dict[int, dict[_Slot, _Binding]] = {
            unit_id: {} for unit_id in self.graph.units
        }
        self.active_units: set[int] = {0}

    def run(self) -> tuple[int, ...]:
        self._summarize_to_fixed_point()
        stable_effect = {unit_id: values.copy() for unit_id, values in self.effect.items()}
        module_context = _Context(0, True, stable_effect, None)
        self._execute_unit(self.graph.units[0], _State({}), module_context)
        for unit_id in sorted(self.active_units - {0}):
            context = _Context(unit_id, True, stable_effect, None)
            self._execute_unit(
                self.graph.units[unit_id],
                self._deferred_entry(unit_id),
                context,
            )
        return tuple(sorted(self.lines))

    def _summarize_to_fixed_point(self) -> None:
        slot_count = max(1, len(self.graph.all_slots))
        unit_count = len(self.graph.units)
        convergence_bound = 5 * (2 * slot_count * unit_count) + unit_count + 1
        for _ in range(convergence_bound):
            old_envelope = {unit_id: values.copy() for unit_id, values in self.envelope.items()}
            old_effect = {unit_id: values.copy() for unit_id, values in self.effect.items()}
            old_active = set(self.active_units)
            summary = _Summary(
                {unit_id: values.copy() for unit_id, values in old_envelope.items()},
                {unit_id: values.copy() for unit_id, values in old_effect.items()},
                set(old_active),
            )
            module_context = _Context(0, False, old_effect, summary)
            self._execute_unit(self.graph.units[0], _State({}), module_context)
            for unit_id in sorted(old_active - {0}):
                context = _Context(unit_id, False, old_effect, summary)
                self._execute_unit(
                    self.graph.units[unit_id],
                    self._deferred_entry(unit_id, old_envelope),
                    context,
                )
            if (
                summary.envelope == old_envelope
                and summary.effect == old_effect
                and summary.active_units == old_active
            ):
                self.envelope = summary.envelope
                self.effect = summary.effect
                self.active_units = summary.active_units
                return
            self.envelope = summary.envelope
            self.effect = summary.effect
            self.active_units = summary.active_units
        raise BoundaryCheckError("environment resolver summary did not converge")

    def _deferred_entry(
        self,
        unit_id: int,
        envelope: dict[int, dict[_Slot, _Binding]] | None = None,
    ) -> _State:
        unit = self.graph.units[unit_id]
        values = (self.envelope if envelope is None else envelope)[unit_id]
        cells = {
            slot: _Cell(binding, False)
            for slot, binding in values.items()
            if slot.scope_id != unit.scope_id
        }
        scope = self.graph.scopes[unit.scope_id]
        for name in scope.locals:
            cells[_Slot(scope.scope_id, name)] = _Cell()
        for name in scope.parameters:
            cells[_Slot(scope.scope_id, name)] = _Cell(_SAFE_BINDING, False)
        return _State(cells)

    def _record_union(
        self, table: dict[int, dict[_Slot, _Binding]], unit_id: int, slot: _Slot, aliases: _Binding
    ) -> None:
        if aliases:
            table[unit_id][slot] = table[unit_id].get(slot, _SAFE_BINDING) | aliases

    def _publish(self, slot: _Slot, aliases: _Binding, state: _State, context: _Context) -> None:
        if context.summary is None or not aliases:
            return
        for reader_id in state.active_readers:
            if slot in self.graph.units[reader_id].visible_slots:
                self._record_union(context.summary.envelope, reader_id, slot, aliases)

    def _strong_write_slot(
        self,
        slot: _Slot,
        aliases: _Binding,
        state: _State,
        context: _Context,
        *,
        external: bool = False,
    ) -> None:
        state.cells[slot] = _Cell(aliases, False)
        self._publish(slot, aliases, state, context)
        if external and context.summary is not None and context.unit_id != 0:
            self._record_union(context.summary.effect, context.unit_id, slot, aliases)

    def _weak_write_slot(
        self, slot: _Slot, aliases: _Binding, state: _State, context: _Context
    ) -> None:
        current = state.cells.get(slot, _Cell())
        state.cells[slot] = _Cell(current.aliases | aliases, current.may_be_unbound)
        self._publish(slot, aliases, state, context)

    def _activate(self, unit_id: int, state: _State, context: _Context) -> None:
        unit = self.graph.units[unit_id]
        if context.summary is not None:
            for slot in unit.visible_slots:
                aliases = state.cells.get(slot, _Cell()).aliases
                self._record_union(context.summary.envelope, unit_id, slot, aliases)
            context.summary.active_units.add(unit_id)
        state.active_readers |= frozenset((unit_id,))
        for slot, aliases in context.old_effect.get(unit_id, {}).items():
            self._weak_write_slot(slot, aliases, state, context)
            current_scope = self.graph.units[context.unit_id].scope_id
            if (
                context.summary is not None
                and context.unit_id != 0
                and slot.scope_id != current_scope
            ):
                self._record_union(context.summary.effect, context.unit_id, slot, aliases)

    def _read_name_cell(self, scope_id: int, name: str, state: _State) -> _Cell:
        slot = self.graph.resolve_slot(scope_id, name, store=False)
        cell = state.cells.get(slot, _Cell())
        aliases = cell.aliases
        may_be_unbound = cell.may_be_unbound
        scope = self.graph.scopes[scope_id]
        if (
            scope.kind == "class"
            and name not in scope.globals
            and name not in scope.nonlocals
            and cell.may_be_unbound
        ):
            free = self.graph.resolve_free_slot(scope_id, name)
            free_cell = state.cells.get(free, _Cell())
            aliases |= free_cell.aliases
            may_be_unbound = may_be_unbound and free_cell.may_be_unbound
        return _Cell(aliases, may_be_unbound)

    def _read_name(self, scope_id: int, name: str, state: _State) -> _Binding:
        return self._read_name_cell(scope_id, name, state).aliases

    def _write_name(
        self,
        scope_id: int,
        name: str,
        aliases: _Binding,
        state: _State,
        context: _Context,
    ) -> None:
        slot = self.graph.resolve_slot(scope_id, name, store=True)
        scope = self.graph.scopes[scope_id]
        unit_scope = self.graph.units[context.unit_id].scope_id
        external = slot.scope_id != unit_scope and (
            name in scope.globals or name in scope.nonlocals or scope.kind == "comprehension"
        )
        self._strong_write_slot(slot, aliases, state, context, external=external)

    def _note(self, node: ast.AST, binding: _Binding, context: _Context) -> None:
        if context.report and binding & _ENVIRONMENT_ATOMS:
            self.lines.add(node.lineno)

    @staticmethod
    def _unknown_outcome(
        state: _State, binding: _Binding = _SAFE_BINDING, raises: _State | None = None
    ) -> _ExprFlow:
        return _ExprFlow(state.copy(), state, raises, binding)

    def _eval_value(
        self, node: ast.AST, state: _State, scope_id: int, context: _Context
    ) -> tuple[_State | None, _Binding, _State | None]:
        outcome = self._eval_condition(node, state, scope_id, context)
        return outcome.normal(), outcome.binding, outcome.raises

    def _eval_ordered(
        self,
        nodes: Sequence[ast.AST],
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> tuple[_State | None, _State | None]:
        current: _State | None = state
        raises: _State | None = None
        for node in nodes:
            if current is None:
                break
            current, _, child_raises = self._eval_value(node, current, scope_id, context)
            raises = _join_state(raises, child_raises)
        return current, raises

    def _eval_protocol_child(
        self,
        node: ast.AST,
        state: _State,
        scope_id: int,
        context: _Context,
        *,
        checkpoint: bool,
    ) -> tuple[_State | None, _State | None]:
        normal, _, raises = self._eval_value(node, state, scope_id, context)
        if checkpoint and normal is not None:
            raises = _join_state(raises, normal.copy())
        return normal, raises

    def _eval_condition(
        self, node: ast.AST, state: _State, scope_id: int, context: _Context
    ) -> _ExprFlow:
        if isinstance(node, ast.Constant):
            return self._unknown_outcome(state)
        if isinstance(node, ast.Name):
            cell = self._read_name_cell(scope_id, node.id, state)
            self._note(node, cell.aliases, context)
            raises = state.copy() if cell.may_be_unbound else None
            return self._unknown_outcome(state, cell.aliases, raises)
        if isinstance(node, ast.Attribute):
            normal, parent, raises = self._eval_value(node.value, state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            binding = _attribute_binding(parent, node.attr)
            self._note(node, binding, context)
            return self._unknown_outcome(normal, binding, _join_state(raises, normal))
        if isinstance(node, ast.Subscript):
            normal, _, raises = self._eval_value(node.value, state, scope_id, context)
            if normal is not None:
                normal, _, slice_raises = self._eval_value(node.slice, normal, scope_id, context)
                raises = _join_state(raises, slice_raises)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=_join_state(raises, normal))
        if isinstance(node, ast.IfExp):
            test = self._eval_condition(node.test, state, scope_id, context)
            raises = _join_state(test.raises, test.normal())
            body = (
                self._eval_condition(node.body, test.truthy, scope_id, context)
                if test.truthy is not None
                else _ExprFlow(None, None, None, _SAFE_BINDING)
            )
            otherwise = (
                self._eval_condition(node.orelse, test.falsey, scope_id, context)
                if test.falsey is not None
                else _ExprFlow(None, None, None, _SAFE_BINDING)
            )
            return _ExprFlow(
                _join_state(body.truthy, otherwise.truthy),
                _join_state(body.falsey, otherwise.falsey),
                _join_state(raises, body.raises, otherwise.raises),
                body.binding | otherwise.binding,
            )
        if isinstance(node, ast.NamedExpr):
            value = self._eval_condition(node.value, state, scope_id, context)
            raises = value.raises
            truthy = value.truthy
            falsey = value.falsey
            if truthy is not None:
                truthy, target_raises = self._write_target(
                    node.target, value.binding, truthy, scope_id, context
                )
                raises = _join_state(raises, target_raises)
            if falsey is not None:
                falsey, target_raises = self._write_target(
                    node.target, value.binding, falsey, scope_id, context
                )
                raises = _join_state(raises, target_raises)
            return _ExprFlow(truthy, falsey, raises, value.binding)
        if isinstance(node, ast.Lambda):
            normal, raises = self._eval_arguments(node.args, state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            self._activate(self.graph.node_units[id(node)], normal, context)
            return self._unknown_outcome(normal, raises=raises)
        if isinstance(node, ast.GeneratorExp):
            return self._eval_comprehension(
                node,
                state,
                scope_id,
                context,
                (node.elt,),
                deferred=True,
                result_policy="generator",
            )
        if isinstance(node, ast.ListComp):
            return self._eval_comprehension(
                node,
                state,
                scope_id,
                context,
                (node.elt,),
                deferred=False,
                result_policy="list",
            )
        if isinstance(node, ast.SetComp):
            return self._eval_comprehension(
                node,
                state,
                scope_id,
                context,
                (node.elt,),
                deferred=False,
                result_policy="set",
            )
        if isinstance(node, ast.DictComp):
            return self._eval_comprehension(
                node,
                state,
                scope_id,
                context,
                (node.key, node.value),
                deferred=False,
                result_policy="dict",
            )
        if isinstance(node, ast.BoolOp):
            current = self._eval_condition(node.values[0], state, scope_id, context)
            raises = current.raises
            bindings = current.binding
            if isinstance(node.op, ast.And):
                completed = current.falsey
                cursor = current.truthy
                raises = _join_state(raises, current.normal())
                for value in node.values[1:]:
                    if cursor is None:
                        break
                    current = self._eval_condition(value, cursor, scope_id, context)
                    raises = _join_state(raises, current.raises)
                    bindings |= current.binding
                    completed = _join_state(completed, current.falsey)
                    cursor = current.truthy
                    if value is not node.values[-1]:
                        raises = _join_state(raises, current.normal())
                return _ExprFlow(cursor, completed, raises, bindings)
            completed = current.truthy
            cursor = current.falsey
            raises = _join_state(raises, current.normal())
            for value in node.values[1:]:
                if cursor is None:
                    break
                current = self._eval_condition(value, cursor, scope_id, context)
                raises = _join_state(raises, current.raises)
                bindings |= current.binding
                completed = _join_state(completed, current.truthy)
                cursor = current.falsey
                if value is not node.values[-1]:
                    raises = _join_state(raises, current.normal())
            return _ExprFlow(completed, cursor, raises, bindings)
        if isinstance(node, ast.Compare):
            normal, _, raises = self._eval_value(node.left, state, scope_id, context)
            falsey: _State | None = None
            truthy = normal
            for comparator in node.comparators:
                if truthy is None:
                    break
                truthy, _, child_raises = self._eval_value(comparator, truthy, scope_id, context)
                raises = _join_state(raises, child_raises, truthy)
                falsey = _join_state(falsey, truthy)
                truthy = truthy.copy() if truthy is not None else None
            return _ExprFlow(truthy, falsey, raises, _SAFE_BINDING)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            operand = self._eval_condition(node.operand, state, scope_id, context)
            return _ExprFlow(
                operand.falsey,
                operand.truthy,
                _join_state(operand.raises, operand.normal()),
                _SAFE_BINDING,
            )
        if isinstance(node, ast.UnaryOp):
            normal, _, raises = self._eval_value(node.operand, state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=_join_state(raises, normal))
        if isinstance(node, ast.Call):
            normal, raises = self._eval_protocol_child(
                node.func, state, scope_id, context, checkpoint=False
            )
            for argument in node.args:
                if normal is None:
                    break
                expression = argument.value if isinstance(argument, ast.Starred) else argument
                normal, child_raises = self._eval_protocol_child(
                    expression,
                    normal,
                    scope_id,
                    context,
                    checkpoint=isinstance(argument, ast.Starred),
                )
                raises = _join_state(raises, child_raises)
            for keyword in node.keywords:
                if normal is None:
                    break
                normal, child_raises = self._eval_protocol_child(
                    keyword.value,
                    normal,
                    scope_id,
                    context,
                    checkpoint=keyword.arg is None,
                )
                raises = _join_state(raises, child_raises)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=_join_state(raises, normal))
        if isinstance(node, ast.Dict):
            normal: _State | None = state
            raises: _State | None = None
            for key, value in zip(node.keys, node.values, strict=True):
                if normal is None:
                    break
                if key is None:
                    normal, child_raises = self._eval_protocol_child(
                        value, normal, scope_id, context, checkpoint=True
                    )
                    raises = _join_state(raises, child_raises)
                    continue
                normal, child_raises = self._eval_protocol_child(
                    key, normal, scope_id, context, checkpoint=False
                )
                raises = _join_state(raises, child_raises)
                if normal is not None:
                    normal, child_raises = self._eval_protocol_child(
                        value, normal, scope_id, context, checkpoint=False
                    )
                    raises = _join_state(raises, child_raises)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=_join_state(raises, normal))
        if isinstance(node, ast.BinOp):
            normal, raises = self._eval_ordered((node.left, node.right), state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=_join_state(raises, normal))
        if isinstance(node, ast.Slice):
            parts = tuple(part for part in (node.lower, node.upper, node.step) if part is not None)
            normal, raises = self._eval_ordered(parts, state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=raises)
        if isinstance(node, ast.JoinedStr):
            normal, raises = self._eval_ordered(node.values, state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=_join_state(raises, normal))
        if isinstance(node, ast.FormattedValue):
            values = (node.value,) if node.format_spec is None else (node.value, node.format_spec)
            normal, raises = self._eval_ordered(values, state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=_join_state(raises, normal))
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            normal: _State | None = state
            raises: _State | None = None
            for element in node.elts:
                if normal is None:
                    break
                expression = element.value if isinstance(element, ast.Starred) else element
                normal, child_raises = self._eval_protocol_child(
                    expression,
                    normal,
                    scope_id,
                    context,
                    checkpoint=isinstance(element, ast.Starred),
                )
                raises = _join_state(raises, child_raises)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            operation_raises = normal if isinstance(node, ast.Set) else None
            return self._unknown_outcome(normal, raises=_join_state(raises, operation_raises))
        if isinstance(node, ast.Starred):
            normal, _, raises = self._eval_value(node.value, state, scope_id, context)
            if normal is None:
                return _ExprFlow(None, None, raises, _SAFE_BINDING)
            return self._unknown_outcome(normal, raises=raises)

        children = tuple(
            child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)
        )
        normal, raises = self._eval_ordered(children, state, scope_id, context)
        if normal is None:
            return _ExprFlow(None, None, raises, _SAFE_BINDING)
        operation_raises = normal if children else None
        return self._unknown_outcome(normal, raises=_join_state(raises, operation_raises))

    def _eval_arguments(
        self,
        arguments: ast.arguments,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> tuple[_State | None, _State | None]:
        return self._eval_ordered(_argument_expressions(arguments), state, scope_id, context)

    def _write_target(
        self,
        target: ast.AST,
        binding: _Binding,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> tuple[_State | None, _State | None]:
        if isinstance(target, ast.Name):
            self._write_name(scope_id, target.id, binding, state, context)
            return state, None
        if isinstance(target, ast.Starred):
            return self._write_target(target.value, _SAFE_BINDING, state, scope_id, context)
        if isinstance(target, ast.Tuple | ast.List):
            current: _State | None = state
            raises: _State | None = None
            for item in target.elts:
                if current is None:
                    break
                current, target_raises = self._write_target(
                    item, _SAFE_BINDING, current, scope_id, context
                )
                raises = _join_state(raises, target_raises)
            return current, raises
        normal, _, raises = self._eval_value(target, state, scope_id, context)
        return normal, raises

    def _delete_target(
        self,
        target: ast.AST,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> tuple[_State | None, _State | None]:
        if isinstance(target, ast.Name):
            slot = self.graph.resolve_slot(scope_id, target.id, store=True)
            state.cells[slot] = _Cell()
            return state, state.copy()
        if isinstance(target, ast.Starred):
            return self._delete_target(target.value, state, scope_id, context)
        if isinstance(target, ast.Tuple | ast.List):
            current: _State | None = state
            raises: _State | None = None
            for item in target.elts:
                if current is None:
                    break
                current, target_raises = self._delete_target(item, current, scope_id, context)
                raises = _join_state(raises, target_raises)
            return current, raises
        normal, _, raises = self._eval_value(target, state, scope_id, context)
        return normal, _join_state(raises, normal)

    def _eval_comprehension(
        self,
        node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp,
        state: _State,
        scope_id: int,
        context: _Context,
        results: tuple[ast.AST, ...],
        *,
        deferred: bool,
        result_policy: str,
    ) -> _ExprFlow:
        first = node.generators[0]
        normal, raises = self._eval_protocol_child(
            first.iter, state, scope_id, context, checkpoint=True
        )
        if normal is None:
            return _ExprFlow(None, None, raises, _SAFE_BINDING)
        if deferred:
            self._activate(self.graph.node_units[id(node)], normal, context)
            return self._unknown_outcome(normal, raises=raises)
        comprehension_scope = self.graph.node_scopes[id(node)]
        completed, body_raises = self._run_comprehension_level(
            node.generators,
            results,
            0,
            normal,
            comprehension_scope,
            context,
            result_policy,
        )
        raises = _join_state(raises, body_raises)
        if completed is None:
            return _ExprFlow(None, None, raises, _SAFE_BINDING)
        completed.cells = {
            slot: cell
            for slot, cell in completed.cells.items()
            if slot.scope_id != comprehension_scope
        }
        return self._unknown_outcome(completed, raises=raises)

    def _run_comprehension_level(
        self,
        generators: list[ast.comprehension],
        results: tuple[ast.AST, ...],
        index: int,
        incoming: _State,
        scope_id: int,
        context: _Context,
        result_policy: str,
    ) -> tuple[_State | None, _State | None]:
        generator = generators[index]
        entry: _State | None = incoming
        raises: _State | None = None
        if index > 0:
            entry, raises = self._eval_protocol_child(
                generator.iter,
                incoming,
                scope_id,
                context,
                checkpoint=True,
            )
        if entry is None:
            return None, raises
        header = entry.copy()
        for _ in range(self._loop_bound()):
            raises = _join_state(raises, header.copy())
            iteration = header.copy()
            iteration, target_raises = self._write_target(
                generator.target,
                _SAFE_BINDING,
                iteration,
                scope_id,
                context,
            )
            raises = _join_state(raises, target_raises)
            cursor = iteration
            skipped: _State | None = None
            for condition in generator.ifs:
                if cursor is None:
                    break
                outcome = self._eval_condition(condition, cursor, scope_id, context)
                raises = _join_state(raises, outcome.raises)
                skipped = _join_state(skipped, outcome.falsey)
                cursor = outcome.truthy
            completed: _State | None = None
            if cursor is not None:
                if index + 1 < len(generators):
                    completed, nested_raises = self._run_comprehension_level(
                        generators,
                        results,
                        index + 1,
                        cursor,
                        scope_id,
                        context,
                        result_policy,
                    )
                    raises = _join_state(raises, nested_raises)
                else:
                    completed, result_raises = self._eval_comprehension_result(
                        results,
                        result_policy,
                        cursor,
                        scope_id,
                        context,
                    )
                    raises = _join_state(raises, result_raises)
            backedge = _join_state(skipped, completed)
            next_header = _join_state(entry, backedge)
            assert next_header is not None
            if next_header == header:
                return next_header, raises
            header = next_header
        raise BoundaryCheckError("environment resolver comprehension did not converge")

    def _eval_comprehension_result(
        self,
        results: tuple[ast.AST, ...],
        result_policy: str,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> tuple[_State | None, _State | None]:
        if result_policy == "dict":
            assert len(results) == 2
            normal, raises = self._eval_protocol_child(
                results[0], state, scope_id, context, checkpoint=False
            )
            if normal is not None:
                normal, value_raises = self._eval_protocol_child(
                    results[1], normal, scope_id, context, checkpoint=True
                )
                raises = _join_state(raises, value_raises)
            return normal, raises
        assert result_policy in {"list", "set", "generator"}
        assert len(results) == 1
        return self._eval_protocol_child(
            results[0],
            state,
            scope_id,
            context,
            checkpoint=result_policy == "set",
        )

    def _execute_unit(self, unit: _Unit, state: _State, context: _Context) -> _Flow:
        if unit.kind == "generator":
            node = unit.body[0]
            assert isinstance(node, ast.GeneratorExp)
            completed, raises = self._run_comprehension_level(
                node.generators,
                (node.elt,),
                0,
                state,
                unit.scope_id,
                context,
                "generator",
            )
            return _Flow(normal=completed, raises=raises)
        if unit.kind == "lambda":
            normal, _, raises = self._eval_value(unit.body[0], state, unit.scope_id, context)
            return _Flow(normal=normal, raises=raises)
        flow, _ = self._exec_block(unit.body, state, unit.scope_id, context)
        return flow

    def _exec_block(
        self,
        statements: Sequence[ast.AST],
        incoming: _State,
        scope_id: int,
        context: _Context,
        *,
        collect_prefixes: bool = False,
    ) -> tuple[_Flow, _State | None]:
        normal: _State | None = incoming.copy()
        breaks = continues = returns = raises = None
        prefixes: _State | None = incoming.copy() if collect_prefixes else None
        for statement in statements:
            if normal is None:
                break
            if collect_prefixes:
                prefixes = _join_state(prefixes, normal)
            one = self._exec_statement(statement, normal, scope_id, context)
            normal = one.normal
            breaks = _join_state(breaks, one.breaks)
            continues = _join_state(continues, one.continues)
            returns = _join_state(returns, one.returns)
            raises = _join_state(raises, one.raises)
            if collect_prefixes and normal is not None:
                prefixes = _join_state(prefixes, normal)
        return _Flow(normal, breaks, continues, returns, raises), prefixes

    def _exec_statement(
        self, node: ast.AST, state: _State, scope_id: int, context: _Context
    ) -> _Flow:
        if isinstance(node, ast.Pass | ast.Global | ast.Nonlocal):
            return _Flow(normal=state)
        if isinstance(node, ast.Expr):
            normal, _, raises = self._eval_value(node.value, state, scope_id, context)
            return _Flow(normal=normal, raises=raises)
        if isinstance(node, ast.Import | ast.ImportFrom):
            projected = {
                name: _project_binding(binding) for name, binding in _import_bindings(node)
            }
            if isinstance(node, ast.Import):
                names = [item.asname or item.name.split(".")[0] for item in node.names]
            else:
                names = [item.asname or item.name for item in node.names if item.name != "*"]
            for name in names:
                self._write_name(
                    scope_id,
                    name,
                    projected.get(name, _SAFE_BINDING),
                    state,
                    context,
                )
            return _Flow(normal=state)
        if isinstance(node, ast.Assign):
            normal, binding, raises = self._eval_value(node.value, state, scope_id, context)
            for target in node.targets:
                if normal is None:
                    break
                normal, target_raises = self._write_target(
                    target, binding, normal, scope_id, context
                )
                raises = _join_state(raises, target_raises)
            return _Flow(normal=normal, raises=raises)
        if isinstance(node, ast.AnnAssign):
            normal: _State | None = state
            raises: _State | None = None
            if node.value is not None:
                normal, binding, value_raises = self._eval_value(
                    node.value, state, scope_id, context
                )
                raises = _join_state(raises, value_raises)
                if normal is not None:
                    normal, target_raises = self._write_target(
                        node.target, binding, normal, scope_id, context
                    )
                    raises = _join_state(raises, target_raises)
            elif not isinstance(node.target, ast.Name):
                normal, _, target_raises = self._eval_value(node.target, state, scope_id, context)
                raises = _join_state(raises, target_raises)
            if normal is not None:
                normal, _, annotation_raises = self._eval_value(
                    node.annotation, normal, scope_id, context
                )
                raises = _join_state(raises, annotation_raises)
            return _Flow(normal=normal, raises=raises)
        if isinstance(node, ast.AugAssign):
            normal, _, raises = self._eval_value(node.target, state, scope_id, context)
            if normal is not None:
                normal, _, value_raises = self._eval_value(node.value, normal, scope_id, context)
                raises = _join_state(raises, value_raises, normal)
            if normal is not None:
                normal, target_raises = self._write_target(
                    node.target, _SAFE_BINDING, normal, scope_id, context
                )
                raises = _join_state(raises, target_raises)
            return _Flow(normal=normal, raises=raises)
        if isinstance(node, ast.Delete):
            normal: _State | None = state
            raises: _State | None = None
            for target in node.targets:
                if normal is None:
                    break
                normal, target_raises = self._delete_target(target, normal, scope_id, context)
                raises = _join_state(raises, target_raises)
            return _Flow(normal=normal, raises=raises)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            normal: _State | None = state
            raises: _State | None = None
            for decorator in node.decorator_list:
                if normal is None:
                    break
                normal, _, decorator_raises = self._eval_value(decorator, normal, scope_id, context)
                raises = _join_state(raises, decorator_raises)
            if normal is not None:
                normal, argument_raises = self._eval_arguments(node.args, normal, scope_id, context)
                raises = _join_state(raises, argument_raises)
            if normal is not None and node.returns is not None:
                normal, _, annotation_raises = self._eval_value(
                    node.returns, normal, scope_id, context
                )
                raises = _join_state(raises, annotation_raises)
            if normal is not None:
                self._write_name(scope_id, node.name, _SAFE_BINDING, normal, context)
                self._activate(self.graph.node_units[id(node)], normal, context)
            return _Flow(normal=normal, raises=raises)
        if isinstance(node, ast.ClassDef):
            return self._exec_class(node, state, scope_id, context)
        if isinstance(node, ast.If):
            test = self._eval_condition(node.test, state, scope_id, context)
            body = _Flow()
            if test.truthy is not None:
                body, _ = self._exec_block(node.body, test.truthy, scope_id, context)
            otherwise = _Flow()
            if test.falsey is not None:
                if node.orelse:
                    otherwise, _ = self._exec_block(node.orelse, test.falsey, scope_id, context)
                else:
                    otherwise.normal = test.falsey
            joined = _join_flow(body, otherwise)
            joined.raises = _join_state(joined.raises, test.raises)
            return joined
        if isinstance(node, ast.For | ast.AsyncFor):
            return self._exec_loop(node, state, scope_id, context)
        if isinstance(node, ast.While):
            return self._exec_loop(node, state, scope_id, context)
        if isinstance(node, ast.With | ast.AsyncWith):
            normal: _State | None = state
            raises: _State | None = None
            for item in node.items:
                if normal is None:
                    break
                normal, _, context_raises = self._eval_value(
                    item.context_expr, normal, scope_id, context
                )
                raises = _join_state(raises, context_raises)
                if normal is not None and item.optional_vars is not None:
                    normal, target_raises = self._write_target(
                        item.optional_vars,
                        _SAFE_BINDING,
                        normal,
                        scope_id,
                        context,
                    )
                    raises = _join_state(raises, target_raises)
            if normal is None:
                return _Flow(raises=raises)
            flow, _ = self._exec_block(node.body, normal, scope_id, context)
            flow.raises = _join_state(flow.raises, raises)
            return flow
        if isinstance(node, ast.Try | ast.TryStar):
            return self._exec_try(node, state, scope_id, context)
        if isinstance(node, ast.Match):
            return self._exec_match(node, state, scope_id, context)
        if isinstance(node, ast.Return):
            if node.value is not None:
                normal, _, raises = self._eval_value(node.value, state, scope_id, context)
                return _Flow(returns=normal, raises=raises)
            return _Flow(returns=state)
        if isinstance(node, ast.Raise):
            normal: _State | None = state
            raises: _State | None = None
            if node.exc is not None:
                normal, _, exc_raises = self._eval_value(node.exc, normal, scope_id, context)
                raises = _join_state(raises, exc_raises)
            if normal is not None and node.cause is not None:
                normal, _, cause_raises = self._eval_value(node.cause, normal, scope_id, context)
                raises = _join_state(raises, cause_raises)
            return _Flow(raises=_join_state(raises, normal))
        if isinstance(node, ast.Break):
            return _Flow(breaks=state)
        if isinstance(node, ast.Continue):
            return _Flow(continues=state)
        if isinstance(node, ast.Assert):
            test = self._eval_condition(node.test, state, scope_id, context)
            raises = _join_state(test.raises, test.falsey)
            if test.falsey is not None and node.msg is not None:
                message, _, message_raises = self._eval_value(
                    node.msg, test.falsey, scope_id, context
                )
                raises = _join_state(raises, message_raises, message)
            return _Flow(normal=test.truthy, raises=raises)

        children = tuple(
            child for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)
        )
        normal, raises = self._eval_ordered(children, state, scope_id, context)
        return _Flow(normal=normal, raises=raises)

    def _exec_class(
        self,
        node: ast.ClassDef,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> _Flow:
        normal: _State | None = state
        raises: _State | None = None
        for decorator in node.decorator_list:
            if normal is None:
                break
            normal, _, decorator_raises = self._eval_value(decorator, normal, scope_id, context)
            raises = _join_state(raises, decorator_raises)
        for base in node.bases:
            if normal is None:
                break
            expression = base.value if isinstance(base, ast.Starred) else base
            normal, base_raises = self._eval_protocol_child(
                expression,
                normal,
                scope_id,
                context,
                checkpoint=isinstance(base, ast.Starred),
            )
            raises = _join_state(raises, base_raises)
        for keyword in node.keywords:
            if normal is None:
                break
            normal, keyword_raises = self._eval_protocol_child(
                keyword.value,
                normal,
                scope_id,
                context,
                checkpoint=keyword.arg is None,
            )
            raises = _join_state(raises, keyword_raises)
        if normal is None:
            return _Flow(raises=raises)
        class_scope = self.graph.node_scopes[id(node)]
        flow, _ = self._exec_block(node.body, normal, class_scope, context)

        def strip(channel: _State | None) -> _State | None:
            if channel is None:
                return None
            return _State(
                {
                    slot: cell
                    for slot, cell in channel.cells.items()
                    if slot.scope_id != class_scope
                },
                channel.active_readers,
            )

        flow = _Flow(
            strip(flow.normal),
            strip(flow.breaks),
            strip(flow.continues),
            strip(flow.returns),
            _join_state(strip(flow.raises), raises),
        )
        if flow.normal is not None:
            self._write_name(scope_id, node.name, _SAFE_BINDING, flow.normal, context)
        return flow

    def _loop_bound(self) -> int:
        return len(self.graph.all_slots) * (len(_ATOMS) + 1) + len(self.graph.units) + 1

    def _exec_loop(
        self,
        node: ast.For | ast.AsyncFor | ast.While,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> _Flow:
        all_raises: _State | None = None
        if isinstance(node, ast.For | ast.AsyncFor):
            normal, _, iterable_raises = self._eval_value(node.iter, state, scope_id, context)
            all_raises = _join_state(all_raises, iterable_raises)
            if normal is None:
                return _Flow(raises=all_raises)
            state = normal
        entry = state.copy()
        header = entry.copy()
        all_breaks = all_returns = None
        exhausted: _State | None = header.copy()
        for _ in range(self._loop_bound()):
            if isinstance(node, ast.While):
                condition = self._eval_condition(node.test, header.copy(), scope_id, context)
                iteration = condition.truthy
                exhausted = condition.falsey
                all_raises = _join_state(all_raises, condition.raises)
            else:
                iteration, target_raises = self._write_target(
                    node.target,
                    _SAFE_BINDING,
                    header.copy(),
                    scope_id,
                    context,
                )
                all_raises = _join_state(all_raises, target_raises)
                exhausted = header.copy()
            if iteration is not None:
                body, _ = self._exec_block(node.body, iteration, scope_id, context)
            else:
                body = _Flow()
            all_breaks = _join_state(all_breaks, body.breaks)
            all_returns = _join_state(all_returns, body.returns)
            all_raises = _join_state(all_raises, body.raises)
            next_header = _join_state(entry, body.normal, body.continues)
            assert next_header is not None
            if next_header == header:
                header = next_header
                break
            header = next_header
        else:
            raise BoundaryCheckError("environment resolver loop did not converge")

        if exhausted is not None:
            else_flow, _ = self._exec_block(node.orelse, exhausted, scope_id, context)
        else:
            else_flow = _Flow()
        return _Flow(
            normal=_join_state(all_breaks, else_flow.normal),
            breaks=else_flow.breaks,
            continues=else_flow.continues,
            returns=_join_state(all_returns, else_flow.returns),
            raises=_join_state(all_raises, else_flow.raises),
        )

    def _exec_try(
        self,
        node: ast.Try | ast.TryStar,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> _Flow:
        body, prefixes = self._exec_block(
            node.body, state, scope_id, context, collect_prefixes=True
        )
        if body.normal is not None:
            else_flow, _ = self._exec_block(node.orelse, body.normal, scope_id, context)
        else:
            else_flow = _Flow()
        handler_seed = _join_state(prefixes, body.raises)
        if isinstance(node, ast.Try):
            remaining = handler_seed
            handled = _Flow()
            dispatch_raises: _State | None = None
            for handler in node.handlers:
                if remaining is None:
                    break
                if handler.type is None:
                    matched = remaining.copy()
                    remaining = None
                    exhaustive = True
                else:
                    type_normal, _, type_raises = self._eval_value(
                        handler.type, remaining, scope_id, context
                    )
                    dispatch_raises = _join_state(dispatch_raises, type_raises)
                    if type_normal is None:
                        remaining = None
                        break
                    matched = type_normal.copy()
                    remaining = type_normal.copy()
                    exhaustive = False
                if handler.name is not None:
                    self._write_name(
                        scope_id,
                        handler.name,
                        _SAFE_BINDING,
                        matched,
                        context,
                    )
                handler_flow, _ = self._exec_block(handler.body, matched, scope_id, context)
                if handler.name is not None:
                    handler_flow = self._clear_handler_target(handler_flow, scope_id, handler.name)
                handled = _join_flow(handled, handler_flow)
                if exhaustive:
                    break
            combined = _Flow(
                normal=_join_state(else_flow.normal, handled.normal),
                breaks=_join_state(body.breaks, else_flow.breaks, handled.breaks),
                continues=_join_state(
                    body.continues,
                    else_flow.continues,
                    handled.continues,
                ),
                returns=_join_state(body.returns, else_flow.returns, handled.returns),
                raises=_join_state(
                    else_flow.raises,
                    handled.raises,
                    dispatch_raises,
                    remaining,
                ),
            )
            return self._apply_finally(combined, node.finalbody, scope_id, context)

        pending_clean = handler_seed
        pending_deferred: _State | None = None
        star_done = _Flow()
        for handler in node.handlers:
            assert handler.type is not None
            next_clean: _State | None = None
            next_deferred: _State | None = None
            for pending, deferred in (
                (pending_clean, False),
                (pending_deferred, True),
            ):
                if pending is None:
                    continue
                type_normal, _, type_raises = self._eval_value(
                    handler.type, pending, scope_id, context
                )
                star_done.raises = _join_state(star_done.raises, type_raises)
                if type_normal is None:
                    continue
                if deferred:
                    next_deferred = _join_state(next_deferred, type_normal.copy())
                else:
                    next_clean = _join_state(next_clean, type_normal.copy())
                matched = type_normal.copy()
                if handler.name is not None:
                    self._write_name(
                        scope_id,
                        handler.name,
                        _SAFE_BINDING,
                        matched,
                        context,
                    )
                handler_flow, _ = self._exec_block(handler.body, matched, scope_id, context)
                if handler.name is not None:
                    handler_flow = self._clear_handler_target(handler_flow, scope_id, handler.name)
                star_done.breaks = _join_state(star_done.breaks, handler_flow.breaks)
                star_done.continues = _join_state(star_done.continues, handler_flow.continues)
                star_done.returns = _join_state(star_done.returns, handler_flow.returns)
                if deferred:
                    star_done.raises = _join_state(
                        star_done.raises,
                        handler_flow.normal,
                        handler_flow.raises,
                    )
                    next_deferred = _join_state(
                        next_deferred,
                        handler_flow.normal,
                        handler_flow.raises,
                    )
                else:
                    star_done.normal = _join_state(star_done.normal, handler_flow.normal)
                    star_done.raises = _join_state(star_done.raises, handler_flow.raises)
                    next_clean = _join_state(next_clean, handler_flow.normal)
                    next_deferred = _join_state(next_deferred, handler_flow.raises)
            pending_clean = next_clean
            pending_deferred = next_deferred
        star_done.raises = _join_state(star_done.raises, pending_clean, pending_deferred)
        combined = _Flow(
            normal=_join_state(else_flow.normal, star_done.normal),
            breaks=_join_state(body.breaks, else_flow.breaks, star_done.breaks),
            continues=_join_state(
                body.continues,
                else_flow.continues,
                star_done.continues,
            ),
            returns=_join_state(body.returns, else_flow.returns, star_done.returns),
            raises=_join_state(else_flow.raises, star_done.raises),
        )
        return self._apply_finally(combined, node.finalbody, scope_id, context)

    def _clear_handler_target(
        self,
        flow: _Flow,
        scope_id: int,
        name: str,
    ) -> _Flow:
        slot = self.graph.resolve_slot(scope_id, name, store=True)

        def clear(state: _State | None) -> _State | None:
            if state is None:
                return None
            cleaned = state.copy()
            cleaned.cells[slot] = _Cell()
            return cleaned

        return _Flow(
            normal=clear(flow.normal),
            breaks=clear(flow.breaks),
            continues=clear(flow.continues),
            returns=clear(flow.returns),
            raises=clear(flow.raises),
        )

    def _apply_finally(
        self,
        incoming: _Flow,
        finalbody: Sequence[ast.AST],
        scope_id: int,
        context: _Context,
    ) -> _Flow:
        if not finalbody:
            return incoming
        output = _Flow()
        for kind in ("normal", "breaks", "continues", "returns", "raises"):
            state = getattr(incoming, kind)
            if state is None:
                continue
            final, _ = self._exec_block(finalbody, state, scope_id, context)
            if final.normal is not None:
                setattr(
                    output,
                    kind,
                    _join_state(getattr(output, kind), final.normal),
                )
            output.breaks = _join_state(output.breaks, final.breaks)
            output.continues = _join_state(output.continues, final.continues)
            output.returns = _join_state(output.returns, final.returns)
            output.raises = _join_state(output.raises, final.raises)
        return output

    def _eval_pattern(
        self,
        pattern: ast.pattern,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> tuple[_State | None, _State | None]:
        normal: _State | None = state
        raises: _State | None = None
        if isinstance(pattern, ast.MatchValue):
            normal, _, raises = self._eval_value(pattern.value, state, scope_id, context)
        elif isinstance(pattern, ast.MatchClass):
            normal, _, raises = self._eval_value(pattern.cls, state, scope_id, context)
            for item in (*pattern.patterns, *pattern.kwd_patterns):
                if normal is None:
                    break
                normal, item_raises = self._eval_pattern(item, normal, scope_id, context)
                raises = _join_state(raises, item_raises)
        elif isinstance(pattern, ast.MatchMapping):
            for key in pattern.keys:
                if normal is None:
                    break
                normal, _, key_raises = self._eval_value(key, normal, scope_id, context)
                raises = _join_state(raises, key_raises)
            for item in pattern.patterns:
                if normal is None:
                    break
                normal, item_raises = self._eval_pattern(item, normal, scope_id, context)
                raises = _join_state(raises, item_raises)
        elif isinstance(pattern, ast.MatchSequence | ast.MatchOr):
            for item in pattern.patterns:
                if normal is None:
                    break
                normal, item_raises = self._eval_pattern(item, normal, scope_id, context)
                raises = _join_state(raises, item_raises)
        elif isinstance(pattern, ast.MatchAs) and pattern.pattern is not None:
            normal, raises = self._eval_pattern(pattern.pattern, state, scope_id, context)
        return normal, raises

    def _bind_pattern(
        self,
        pattern: ast.pattern,
        subject: _Binding,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> None:
        if isinstance(pattern, ast.MatchAs):
            if pattern.pattern is not None:
                self._bind_pattern(pattern.pattern, subject, state, scope_id, context)
            if pattern.name is not None:
                self._write_name(scope_id, pattern.name, subject, state, context)
        elif isinstance(pattern, ast.MatchStar):
            if pattern.name is not None:
                self._write_name(scope_id, pattern.name, subject, state, context)
        elif isinstance(pattern, ast.MatchMapping):
            for item in pattern.patterns:
                self._bind_pattern(item, subject, state, scope_id, context)
            if pattern.rest is not None:
                self._write_name(scope_id, pattern.rest, subject, state, context)
        elif isinstance(pattern, ast.MatchSequence | ast.MatchOr):
            for item in pattern.patterns:
                self._bind_pattern(item, subject, state, scope_id, context)
        elif isinstance(pattern, ast.MatchClass):
            for item in (*pattern.patterns, *pattern.kwd_patterns):
                self._bind_pattern(item, subject, state, scope_id, context)

    def _exec_match(
        self,
        node: ast.Match,
        state: _State,
        scope_id: int,
        context: _Context,
    ) -> _Flow:
        remaining, subject, subject_raises = self._eval_value(
            node.subject, state, scope_id, context
        )
        completed = _Flow(raises=subject_raises)
        for case in node.cases:
            if remaining is None:
                break
            pattern_normal, pattern_raises = self._eval_pattern(
                case.pattern, remaining.copy(), scope_id, context
            )
            completed.raises = _join_state(completed.raises, pattern_raises)
            irrefutable = _is_irrefutable(case.pattern)
            pattern_fail = None if irrefutable or pattern_normal is None else pattern_normal.copy()
            matched = pattern_normal.copy() if pattern_normal is not None else None
            if matched is not None:
                self._bind_pattern(case.pattern, subject, matched, scope_id, context)
            if case.guard is None:
                body_input = matched
                guard_false = None
            elif matched is not None:
                guard = self._eval_condition(case.guard, matched, scope_id, context)
                body_input = guard.truthy
                guard_false = guard.falsey
                completed.raises = _join_state(completed.raises, guard.raises)
            else:
                body_input = guard_false = None
            if body_input is not None:
                case_flow, _ = self._exec_block(case.body, body_input, scope_id, context)
                completed = _join_flow(completed, case_flow)
            remaining = _join_state(pattern_fail, guard_false)
            if case.guard is None and irrefutable:
                remaining = None
                break
        completed.normal = _join_state(completed.normal, remaining)
        return completed


def _environment_lines(module: _ParsedModule) -> tuple[int, ...]:
    if _layer(module.name) == "apps":
        return ()
    resolver = _EnvironmentResolver(module.tree)
    return resolver.run()


def _add_violation(
    findings: dict[tuple[Path, int, str], Violation],
    path: Path,
    line: int,
    rule_id: str,
    message: str,
) -> None:
    key = (path, line, rule_id)
    findings.setdefault(key, Violation(path, line, rule_id, message))


def _cycle_components(
    module_names: tuple[str, ...], edges: set[ImportEdge]
) -> tuple[tuple[str, ...], ...]:
    graph = {name: set() for name in module_names}
    for edge in edges:
        graph[edge.source].add(edge.target)

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low_links: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(module: str) -> None:
        nonlocal index
        indices[module] = index
        low_links[module] = index
        index += 1
        stack.append(module)
        on_stack.add(module)
        for target in sorted(graph[module]):
            if target not in indices:
                visit(target)
                low_links[module] = min(low_links[module], low_links[target])
            elif target in on_stack:
                low_links[module] = min(low_links[module], indices[target])
        if low_links[module] != indices[module]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == module:
                break
        ordered = tuple(sorted(component))
        if len(ordered) > 1 or ordered[0] in graph[ordered[0]]:
            components.append(ordered)

    for module in module_names:
        if module not in indices:
            visit(module)
    return tuple(sorted(components))


def _record_imports(
    module: _ParsedModule,
    module_names: set[str],
    edges: set[ImportEdge],
    findings: dict[tuple[Path, int, str], Violation],
) -> None:
    source_layer = _layer(module.name)
    for node in ast.walk(module.tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        candidates = tuple(sorted(set(_import_candidates(node, module))))
        if any(_legacy_import(candidate) for candidate in candidates):
            _add_violation(
                findings,
                module.path,
                node.lineno,
                "IMP001_LEGACY_IMPORT",
                "legacy imports are forbidden",
            )

        contracts_matches = (
            tuple(filter(None, (_contracts_forbidden(item) for item in candidates)))
            if source_layer == "contracts"
            else ()
        )
        if contracts_matches:
            _add_violation(
                findings,
                module.path,
                node.lineno,
                "IMP003_CONTRACTS_DEPENDENCY",
                f"contracts dependency is forbidden: {min(contracts_matches)}",
            )

        resolved_targets = {
            target
            for candidate in candidates
            if (target := _internal_target(candidate, module_names)) is not None
        }
        targets = {
            target
            for target in resolved_targets
            if not any(
                other != target and _matches_prefix(other, target) for other in resolved_targets
            )
        }
        for target in sorted(targets):
            edges.add(ImportEdge(module.name, target, module.path, node.lineno))
            target_layer = _layer(target)
            if target_layer is None:
                continue
            if module.name == "tsdaf.__main__" and target == "tsdaf.apps.cli":
                continue
            if source_layer == "adapters" and _matches_prefix(target, "tsdaf.workflow.ports"):
                continue
            contracts_specific = source_layer == "contracts" and target_layer in {
                "apps",
                "adapters",
            }
            allowed = source_layer is not None and target_layer in _ALLOWED_TARGETS[source_layer]
            if not allowed and not contracts_specific:
                _add_violation(
                    findings,
                    module.path,
                    node.lineno,
                    "IMP002_LAYER_IMPORT",
                    f"illegal layer import: {module.name} -> {target}",
                )


def analyze_source_tree(source_root: Path) -> BoundaryReport:
    """Parse and validate a real ``src/tsdaf`` source tree without importing it."""
    modules = _read_modules(source_root)
    module_names = tuple(sorted(module.name for module in modules))
    module_name_set = set(module_names)
    edges: set[ImportEdge] = set()
    findings: dict[tuple[Path, int, str], Violation] = {}

    for module in modules:
        _record_imports(module, module_name_set, edges, findings)
        for line in _environment_lines(module):
            _add_violation(
                findings,
                module.path,
                line,
                "IMP004_ENVIRONMENT_ACCESS",
                "process environment access is forbidden outside tsdaf.apps",
            )

    for component in _cycle_components(module_names, edges):
        members = set(component)
        component_edges = sorted(
            edge for edge in edges if edge.source in members and edge.target in members
        )
        anchor = component_edges[0]
        cycle = " -> ".join((*component, component[0]))
        _add_violation(
            findings,
            anchor.path,
            anchor.line,
            "IMP005_IMPORT_CYCLE",
            f"import cycle: {cycle}",
        )

    return BoundaryReport(module_names, tuple(sorted(edges)), tuple(sorted(findings.values())))


def _repository_root(source_root: Path) -> Path:
    resolved = source_root.resolve()
    if resolved.parent.name == "src":
        return resolved.parent.parent
    return Path.cwd().resolve()


def _display_path(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print(
            "BOUNDARY_ERROR: expected exactly one source-root argument",
            file=sys.stderr,
        )
        return 2
    source_root = Path(arguments[0])
    try:
        report = analyze_source_tree(source_root)
    except BoundaryCheckError as exc:
        print(f"BOUNDARY_ERROR: {exc}", file=sys.stderr)
        return 2
    repository_root = _repository_root(source_root)
    for violation in report.violations:
        path = _display_path(violation.path, repository_root)
        print(f"{path}:{violation.line}: {violation.rule_id} {violation.message}")
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
