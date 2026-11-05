"""Conservative symbol and direct-call extraction using the standard AST."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from blastradius.models import DependencyEdge, EdgeKind, Symbol, SymbolId


@dataclass(slots=True)
class StaticIndex:
    symbols: dict[str, Symbol] = field(default_factory=dict)
    edges: list[DependencyEdge] = field(default_factory=list)
    unresolved_calls: list[str] = field(default_factory=list)


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


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, revision: str, path: str, source: str) -> None:
        self.revision = revision
        self.path = path
        self.source = source
        self.module = module_name(path)
        self.scope: list[str] = []
        self.symbols: list[Symbol] = []
        self.nodes: list[tuple[ast.AST, Symbol]] = []
        self.occurrences: dict[tuple[str, str], int] = {}

    def _visit_definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
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
            _source_hash(self.source, node),
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
        hashlib.sha256(source.encode()).hexdigest(),
        module_name(path),
    )
    return [module_symbol, *visitor.symbols], visitor.nodes


def build_index(revision: str, sources: dict[str, str]) -> StaticIndex:
    index = StaticIndex()
    node_pairs: dict[str, list[tuple[ast.AST, Symbol]]] = {}
    by_simple_name: dict[str, list[str]] = {}
    for path, source in sorted(sources.items()):
        symbols, pairs = symbols_for_source(revision, path, source)
        node_pairs[path] = pairs
        for symbol in symbols:
            index.symbols[symbol.id.key] = symbol
            if symbol.id.qualified_name != "<module>":
                by_simple_name.setdefault(symbol.id.qualified_name.rsplit(".", 1)[-1], []).append(
                    symbol.id.key
                )

    for path, pairs in sorted(node_pairs.items()):
        local = {
            symbol.id.qualified_name.rsplit(".", 1)[-1]: symbol.id.key
            for symbol in index.symbols.values()
            if symbol.id.path == path
        }
        for node, caller in pairs:
            nested_ranges = [
                (child.lineno, child.end_lineno or child.lineno)
                for child in ast.iter_child_nodes(node)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ]
            for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
                if any(start <= call.lineno <= end for start, end in nested_ranges):
                    continue
                target_name: str | None = None
                if isinstance(call.func, ast.Name):
                    target_name = call.func.id
                elif (
                    isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id in {"self", "cls"}
                ):
                    target_name = call.func.attr
                if not target_name:
                    index.unresolved_calls.append(f"{path}:{call.lineno}")
                    continue
                candidates = (
                    [local[target_name]]
                    if target_name in local
                    else by_simple_name.get(target_name, [])
                )
                if len(candidates) == 1:
                    index.edges.append(
                        DependencyEdge(
                            caller.id.key,
                            candidates[0],
                            EdgeKind.CALL,
                            "static_ast",
                            0.9 if target_name in local else 0.55,
                        )
                    )
                else:
                    index.unresolved_calls.append(f"{path}:{call.lineno}:{target_name}")
    index.edges.sort(key=lambda edge: (edge.caller, edge.callee, edge.kind.value))
    index.unresolved_calls.sort()
    return index
