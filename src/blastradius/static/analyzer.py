"""Conservative symbol, import, and direct-call extraction using the standard AST."""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from blastradius.models import DependencyEdge, EdgeKind, Symbol, SymbolId

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


@dataclass(slots=True)
class StaticIndex:
    symbols: dict[str, Symbol] = field(default_factory=dict)
    edges: list[DependencyEdge] = field(default_factory=list)
    unresolved_calls: list[str] = field(default_factory=list)
    uncertainty_sites: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _ImportBinding:
    target_module: str
    target_symbol: str | None
    typing_only: bool
    external: bool


def module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if "src" in parts:
        parts = parts[parts.index("src") + 1 :]
    return ".".join(parts)


def _source_hash(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node) or ast.dump(node, include_attributes=False)
    return hashlib.sha256(segment.encode()).hexdigest()


def _structure_hash(node: ast.AST) -> str:
    normalized = copy.deepcopy(node)
    if isinstance(normalized, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        normalized.name = "<symbol>"
    payload = ast.dump(normalized, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(payload.encode()).hexdigest()


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, revision: str, path: str, source: str) -> None:
        self.revision = revision
        self.path = path
        self.source = source
        self.module = module_name(path)
        self.scope: list[str] = []
        self.symbols: list[Symbol] = []
        self.nodes: list[tuple[Definition, Symbol]] = []
        self.occurrences: dict[tuple[str, str], int] = {}

    def _visit_definition(self, node: Definition) -> None:
        kind = (
            "class"
            if isinstance(node, ast.ClassDef)
            else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function")
        )
        qualified = ".".join([*self.scope, node.name])
        occurrence_key = (qualified, kind)
        occurrence = self.occurrences.get(occurrence_key, 0)
        self.occurrences[occurrence_key] = occurrence + 1
        decorator_lines = [item.lineno for item in node.decorator_list]
        start = min([node.lineno, *decorator_lines])
        symbol = Symbol(
            SymbolId(self.revision, self.path, qualified, kind, occurrence),
            start,
            node.end_lineno or node.lineno,
            node.body[0].lineno if node.body else node.lineno,
            _source_hash(self.source, node),
            _structure_hash(node),
            self.module,
            ".".join(self.scope) or None,
        )
        self.symbols.append(symbol)
        self.nodes.append((node, symbol))
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_definition
    visit_AsyncFunctionDef = _visit_definition
    visit_ClassDef = _visit_definition


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.typing_only = False
        self.imports: list[tuple[ast.Import | ast.ImportFrom, bool]] = []

    @staticmethod
    def _is_type_checking(test: ast.expr) -> bool:
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )

    def visit_If(self, node: ast.If) -> None:
        previous = self.typing_only
        if self._is_type_checking(node.test):
            self.typing_only = True
            for child in node.body:
                self.visit(child)
            self.typing_only = previous
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append((node, self.typing_only))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append((node, self.typing_only))


def symbols_for_source(
    revision: str, path: str, source: str
) -> tuple[list[Symbol], list[tuple[ast.AST, Symbol]]]:
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return [], []
    visitor = _SymbolVisitor(revision, path, source)
    visitor.visit(tree)
    module_symbol = Symbol(
        SymbolId(revision, path, "<module>", "module"),
        1,
        max(len(source.splitlines()), 1),
        1,
        hashlib.sha256(source.encode()).hexdigest(),
        hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest(),
        module_name(path),
    )
    return [module_symbol, *visitor.symbols], [(tree, module_symbol), *visitor.nodes]


def _resolve_relative_module(path: str, module: str | None, level: int) -> str:
    current = module_name(path)
    package = current if PurePosixPath(path).name == "__init__.py" else current.rpartition(".")[0]
    parts = package.split(".") if package else []
    keep = max(len(parts) - max(level - 1, 0), 0)
    prefix = ".".join(parts[:keep])
    return ".".join(item for item in (prefix, module or "") if item)


def _attribute_parts(expression: ast.expr) -> list[str] | None:
    if isinstance(expression, ast.Name):
        return [expression.id]
    if isinstance(expression, ast.Attribute):
        parent = _attribute_parts(expression.value)
        return [*parent, expression.attr] if parent else None
    return None


def _owned_nodes(node: ast.AST) -> list[ast.AST]:
    """Return nodes executed by this scope, excluding nested definition bodies."""
    result: list[ast.AST] = []

    def visit(current: ast.AST) -> None:
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            result.append(child)
            visit(child)

    visit(node)
    return result


def _shadowed_names(node: ast.AST) -> set[str]:
    names = {
        item.id
        for item in _owned_nodes(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del))
    }
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        names.update(argument.arg for argument in arguments)
        if node.args.vararg:
            names.add(node.args.vararg.arg)
        if node.args.kwarg:
            names.add(node.args.kwarg.arg)
    return names


def _lexical_candidates(caller: Symbol, name: str, symbols: dict[str, Symbol]) -> list[str]:
    scopes = caller.id.qualified_name.split(".") if caller.id.qualified_name != "<module>" else []
    qualified_options: list[str] = []
    for size in range(len(scopes), -1, -1):
        qualified_options.append(".".join([*scopes[:size], name]))
    return [
        symbol.id.key
        for qualified in qualified_options
        for symbol in symbols.values()
        if symbol.id.path == caller.id.path and symbol.id.qualified_name == qualified
    ]


def build_index(revision: str, sources: dict[str, str]) -> StaticIndex:
    index = StaticIndex()
    syntax_trees: dict[str, ast.Module] = {}
    node_pairs: dict[str, list[tuple[ast.AST, Symbol]]] = {}
    module_symbols: dict[str, str] = {}
    by_module_name: dict[tuple[str, str], list[str]] = {}

    for path, source in sorted(sources.items()):
        try:
            syntax_trees[path] = ast.parse(source, filename=path)
        except SyntaxError as exc:
            index.uncertainty_sites.append(f"{path}:{exc.lineno or 1}:syntax_error")
            continue
        symbols, pairs = symbols_for_source(revision, path, source)
        node_pairs[path] = pairs
        for symbol in symbols:
            index.symbols[symbol.id.key] = symbol
            if symbol.id.qualified_name == "<module>":
                previous = module_symbols.get(symbol.module)
                if previous:
                    index.uncertainty_sites.append(f"{path}:1:duplicate_module:{symbol.module}")
                module_symbols[symbol.module] = symbol.id.key
            else:
                by_module_name.setdefault((symbol.module, symbol.id.qualified_name), []).append(
                    symbol.id.key
                )

    bindings_by_path: dict[str, dict[str, _ImportBinding]] = {}
    for path, tree in sorted(syntax_trees.items()):
        bindings: dict[str, _ImportBinding] = {}
        import_visitor = _ImportVisitor()
        import_visitor.visit(tree)
        importer_module = module_name(path)
        importer_key = module_symbols.get(importer_module)
        for import_node, typing_only in import_visitor.imports:
            if isinstance(import_node, ast.Import):
                for alias in import_node.names:
                    binding_name = alias.asname or alias.name.split(".", 1)[0]
                    target_module = alias.name if alias.asname else alias.name.split(".", 1)[0]
                    external = target_module not in module_symbols
                    bindings[binding_name] = _ImportBinding(
                        target_module, None, typing_only, external
                    )
                    imported_key = module_symbols.get(alias.name) or module_symbols.get(
                        target_module
                    )
                    if importer_key and imported_key:
                        index.edges.append(
                            DependencyEdge(
                                importer_key,
                                imported_key,
                                EdgeKind.IMPORT,
                                "typing_only_import" if typing_only else "static_import",
                                0.15 if typing_only else 0.7,
                            )
                        )
            else:
                target_module = (
                    _resolve_relative_module(path, import_node.module, import_node.level)
                    if import_node.level
                    else import_node.module or ""
                )
                imported_module_key = module_symbols.get(target_module)
                if importer_key and imported_module_key:
                    index.edges.append(
                        DependencyEdge(
                            importer_key,
                            imported_module_key,
                            EdgeKind.IMPORT,
                            "typing_only_import" if typing_only else "static_import",
                            0.15 if typing_only else 0.7,
                        )
                    )
                for alias in import_node.names:
                    if alias.name == "*":
                        index.uncertainty_sites.append(
                            f"{path}:{import_node.lineno}:star_import:{target_module}"
                        )
                        continue
                    binding_name = alias.asname or alias.name
                    submodule = ".".join(item for item in (target_module, alias.name) if item)
                    if submodule in module_symbols:
                        bindings[binding_name] = _ImportBinding(submodule, None, typing_only, False)
                        if importer_key:
                            index.edges.append(
                                DependencyEdge(
                                    importer_key,
                                    module_symbols[submodule],
                                    EdgeKind.IMPORT,
                                    "typing_only_import" if typing_only else "static_import",
                                    0.15 if typing_only else 0.7,
                                )
                            )
                        continue
                    candidates = by_module_name.get((target_module, alias.name), [])
                    bindings[binding_name] = _ImportBinding(
                        target_module,
                        candidates[0] if len(candidates) == 1 else None,
                        typing_only,
                        target_module not in module_symbols,
                    )
                    if len(candidates) > 1:
                        index.uncertainty_sites.append(
                            f"{path}:{import_node.lineno}:ambiguous_import:{binding_name}"
                        )
            if typing_only:
                index.uncertainty_sites.append(
                    f"{path}:{import_node.lineno}:typing_only_relationship"
                )
        bindings_by_path[path] = bindings

    shadowed_by_caller = {
        caller.id.key: _shadowed_names(node)
        for pairs in node_pairs.values()
        for node, caller in pairs
    }

    def resolve_expression(
        expression: ast.expr, caller: Symbol, *, scope_sensitive: bool = True
    ) -> tuple[str, float, str] | None:
        bindings = bindings_by_path.get(caller.id.path, {})
        if isinstance(expression, ast.Name):
            if scope_sensitive and expression.id in shadowed_by_caller.get(caller.id.key, set()):
                return None
            lexical = _lexical_candidates(caller, expression.id, index.symbols)
            if lexical:
                return lexical[0], 0.95, "static_lexical"
            binding = bindings.get(expression.id)
            if binding and binding.target_symbol:
                return (
                    binding.target_symbol,
                    0.2 if binding.typing_only else 0.95,
                    "typing_only_import" if binding.typing_only else "static_import_alias",
                )
            return None

        parts = _attribute_parts(expression)
        if not parts:
            return None
        if parts[0] in {"self", "cls"} and len(parts) == 2:
            class_scopes = caller.id.qualified_name.split(".")[:-1]
            for size in range(len(class_scopes), 0, -1):
                qualified = ".".join([*class_scopes[:size], parts[1]])
                candidates = by_module_name.get((caller.module, qualified), [])
                if len(candidates) == 1:
                    return candidates[0], 0.9, "static_self"
            return None
        if scope_sensitive and parts[0] in shadowed_by_caller.get(caller.id.key, set()):
            return None

        binding = bindings.get(parts[0])
        if not binding:
            return None
        confidence = 0.2 if binding.typing_only else 0.9
        provenance = "typing_only_import" if binding.typing_only else "static_import_attribute"
        if binding.target_symbol:
            target = index.symbols[binding.target_symbol]
            qualified = ".".join([target.id.qualified_name, *parts[1:]])
            candidates = by_module_name.get((target.module, qualified), [])
            return (candidates[0], confidence, provenance) if len(candidates) == 1 else None

        remaining = parts[1:]
        for split in range(len(remaining), 0, -1):
            candidate_module = ".".join([binding.target_module, *remaining[: split - 1]])
            qualified = ".".join(remaining[split - 1 :])
            candidates = by_module_name.get((candidate_module, qualified), [])
            if len(candidates) == 1:
                return candidates[0], confidence, provenance
        return None

    for path, pairs in sorted(node_pairs.items()):
        for node, caller in pairs:
            owned = _owned_nodes(node)
            for call in (item for item in owned if isinstance(item, ast.Call)):
                resolved = resolve_expression(call.func, caller)
                if resolved:
                    target, confidence, provenance = resolved
                    index.edges.append(
                        DependencyEdge(
                            caller.id.key,
                            target,
                            EdgeKind.CALL,
                            provenance,
                            confidence,
                        )
                    )
                    continue
                parts = _attribute_parts(call.func)
                name = ".".join(parts) if parts else "dynamic"
                if isinstance(call.func, ast.Name) and call.func.id in dir(builtins):
                    continue
                binding = bindings_by_path.get(path, {}).get(parts[0] if parts else "")
                if binding and binding.external:
                    continue
                reason = (
                    "dynamic_dispatch"
                    if isinstance(call.func, ast.Attribute)
                    else "unresolved_name"
                )
                site = f"{path}:{call.lineno}:{reason}:{name}"
                index.unresolved_calls.append(site)
                index.uncertainty_sites.append(site)

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for decorator in node.decorator_list:
                    target_expr = decorator.func if isinstance(decorator, ast.Call) else decorator
                    resolved = resolve_expression(target_expr, caller, scope_sensitive=False)
                    if resolved:
                        target, confidence, provenance = resolved
                        index.edges.append(
                            DependencyEdge(
                                caller.id.key,
                                target,
                                EdgeKind.DECORATOR,
                                provenance,
                                confidence,
                            )
                        )
                    else:
                        index.uncertainty_sites.append(
                            f"{path}:{decorator.lineno}:unresolved_decorator"
                        )
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    resolved = resolve_expression(base, caller, scope_sensitive=False)
                    if resolved:
                        target, confidence, provenance = resolved
                        index.edges.append(
                            DependencyEdge(
                                caller.id.key,
                                target,
                                EdgeKind.INHERITANCE,
                                provenance,
                                confidence,
                            )
                        )
                    else:
                        index.uncertainty_sites.append(
                            f"{path}:{base.lineno}:unresolved_base_class"
                        )

    unique_edges = {
        (edge.caller, edge.callee, edge.kind, edge.provenance): edge for edge in index.edges
    }
    index.edges = sorted(
        unique_edges.values(),
        key=lambda edge: (edge.caller, edge.callee, edge.kind.value, edge.provenance),
    )
    index.unresolved_calls = sorted(set(index.unresolved_calls))
    index.uncertainty_sites = sorted(set(index.uncertainty_sites))
    return index
