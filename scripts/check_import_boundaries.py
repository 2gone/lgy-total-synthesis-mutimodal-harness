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
    "openai",
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


class BoundaryCheckError(Exception):
    """Raised when a source tree cannot be checked reliably."""


@dataclass(frozen=True, slots=True)
class _ParsedModule:
    name: str
    path: Path
    is_package: bool
    tree: ast.Module


@dataclass(frozen=True, slots=True)
class _EnvironmentAliases:
    os_modules: frozenset[str]
    os_getenv: frozenset[str]
    os_environ: frozenset[str]
    settings_modules: frozenset[str]
    base_settings: frozenset[str]


def _module_name(source_root: Path, path: Path) -> tuple[str, bool]:
    relative = path.relative_to(source_root)
    is_package = relative.name == "__init__.py"
    parts = relative.parts[:-1] if is_package else (*relative.parts[:-1], relative.stem)
    suffix = ".".join(parts)
    return ("tsdaf" if not suffix else f"tsdaf.{suffix}"), is_package


def _read_modules(source_root: Path) -> tuple[_ParsedModule, ...]:
    if not source_root.is_dir():
        raise BoundaryCheckError(f"source root is not a directory: {source_root}")
    if not (source_root / "__init__.py").is_file():
        raise BoundaryCheckError(f"source root is not a tsdaf package: {source_root}")

    paths = tuple(sorted(path for path in source_root.rglob("*.py") if path.is_file()))
    if not paths:
        raise BoundaryCheckError(f"source root contains no Python files: {source_root}")

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
        retained = package[: len(package) - ascend] if ascend <= len(package) else []
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
        (prefix for prefix in _CONTRACTS_FORBIDDEN if _matches_prefix(candidate, prefix)), None
    )


def _legacy_import(candidate: str) -> bool:
    return _matches_prefix(candidate, "harness") or _matches_prefix(
        candidate, "tests.legacy_harness"
    )


def _environment_aliases(tree: ast.Module) -> _EnvironmentAliases:
    os_modules: set[str] = set()
    os_getenv: set[str] = set()
    os_environ: set[str] = set()
    settings_modules: set[str] = set()
    base_settings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                local = imported.asname or imported.name
                if imported.name == "os":
                    os_modules.add(local)
                elif imported.name == "pydantic_settings":
                    settings_modules.add(local)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for imported in node.names:
                local = imported.asname or imported.name
                if node.module == "os" and imported.name == "getenv":
                    os_getenv.add(local)
                elif node.module == "os" and imported.name == "environ":
                    os_environ.add(local)
                elif node.module == "pydantic_settings" and imported.name == "BaseSettings":
                    base_settings.add(local)
    return _EnvironmentAliases(
        frozenset(os_modules),
        frozenset(os_getenv),
        frozenset(os_environ),
        frozenset(settings_modules),
        frozenset(base_settings),
    )


def _environment_lines(module: _ParsedModule) -> tuple[int, ...]:
    if _layer(module.name) == "apps":
        return ()
    aliases = _environment_aliases(module.tree)
    lines: set[int] = set()
    for node in ast.walk(module.tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Name)
        ):
            if node.value.id in aliases.os_modules and node.attr in {"getenv", "environ"}:
                lines.add(node.lineno)
            elif node.value.id in aliases.settings_modules and node.attr == "BaseSettings":
                lines.add(node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in aliases.os_getenv | aliases.os_environ | aliases.base_settings:
                lines.add(node.lineno)
    return tuple(sorted(lines))


def _add_violation(
    findings: dict[tuple[Path, int, str], Violation],
    path: Path,
    line: int,
    rule_id: str,
    message: str,
) -> None:
    findings.setdefault((path, line, rule_id), Violation(path, line, rule_id, message))


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
        if source_layer == "contracts":
            matches = tuple(filter(None, (_contracts_forbidden(item) for item in candidates)))
            if matches:
                _add_violation(
                    findings,
                    module.path,
                    node.lineno,
                    "IMP003_CONTRACTS_DEPENDENCY",
                    f"contracts dependency is forbidden: {min(matches)}",
                )

        resolved = {
            target
            for candidate in candidates
            if (target := _internal_target(candidate, module_names)) is not None
        }
        targets = {
            target
            for target in resolved
            if not any(other != target and _matches_prefix(other, target) for other in resolved)
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


def _violations(source_root: Path) -> tuple[Violation, ...]:
    modules = _read_modules(source_root)
    module_names = tuple(sorted(module.name for module in modules))
    edges: set[ImportEdge] = set()
    findings: dict[tuple[Path, int, str], Violation] = {}
    for module in modules:
        _record_imports(module, set(module_names), edges, findings)
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
    return tuple(sorted(findings.values()))


def _repository_root(source_root: Path) -> Path:
    resolved = source_root.resolve()
    return resolved.parent.parent if resolved.parent.name == "src" else Path.cwd().resolve()


def _display_path(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("BOUNDARY_ERROR: expected exactly one source-root argument", file=sys.stderr)
        return 2
    source_root = Path(arguments[0])
    try:
        violations = _violations(source_root)
    except BoundaryCheckError as exc:
        print(f"BOUNDARY_ERROR: {exc}", file=sys.stderr)
        return 2
    repository_root = _repository_root(source_root)
    for violation in violations:
        print(
            f"{_display_path(violation.path, repository_root)}:{violation.line}: "
            f"{violation.rule_id} {violation.message}"
        )
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
