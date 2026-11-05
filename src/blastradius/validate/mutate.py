"""Deterministic, syntax-valid mutations restricted to changed head regions."""

from __future__ import annotations

import ast
import hashlib
import io
import math
import tokenize
from dataclasses import dataclass
from pathlib import Path

from blastradius.errors import BlastRadiusError
from blastradius.models import (
    SCHEMA_VERSION,
    MutantGenerationReport,
    MutantSpec,
)
from blastradius.vcs.git import diff_files, read_file, resolve_revision

GENERATOR_VERSION = "ast-changed-regions-v1"
DEFAULT_MAX_MUTANTS = 100

_BINARY_OPERATORS: dict[type[ast.operator], tuple[str, str]] = {
    ast.Add: ("+", "-"),
    ast.Sub: ("-", "+"),
    ast.Mult: ("*", "//"),
    ast.Div: ("/", "*"),
    ast.FloorDiv: ("//", "*"),
    ast.Mod: ("%", "*"),
    ast.Pow: ("**", "*"),
}
_COMPARE_OPERATORS: dict[type[ast.cmpop], tuple[str, str]] = {
    ast.Eq: ("==", "!="),
    ast.NotEq: ("!=", "=="),
    ast.Lt: ("<", "<="),
    ast.LtE: ("<=", "<"),
    ast.Gt: (">", ">="),
    ast.GtE: (">=", ">"),
    ast.Is: ("is", "is not"),
    ast.IsNot: ("is not", "is"),
    ast.In: ("in", "not in"),
    ast.NotIn: ("not in", "in"),
}


@dataclass(frozen=True, slots=True)
class _Candidate:
    operator: str
    byte_start: int
    byte_end: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    original: str
    replacement: str


class _Offsets:
    def __init__(self, source: str) -> None:
        self.source_bytes = source.encode()
        self.source_lines = source.splitlines(keepends=True)
        self.line_starts: list[int] = []
        offset = 0
        for line in source.splitlines(keepends=True):
            self.line_starts.append(offset)
            offset += len(line.encode())
        if not self.line_starts:
            self.line_starts.append(0)
        self.tokens: list[tuple[str, int, int]] = []
        ignored = {
            tokenize.ENCODING,
            tokenize.ENDMARKER,
            tokenize.INDENT,
            tokenize.DEDENT,
            tokenize.NEWLINE,
            tokenize.NL,
            tokenize.COMMENT,
        }
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in ignored:
                continue
            start_line, start_column = token.start
            end_line, end_column = token.end
            start = self.line_starts[start_line - 1] + len(
                self.source_lines[start_line - 1][:start_column].encode()
            )
            end = self.line_starts[end_line - 1] + len(
                self.source_lines[end_line - 1][:end_column].encode()
            )
            self.tokens.append((token.string, start, end))

    def node_span(self, node: ast.AST) -> tuple[int, int]:
        if not all(
            hasattr(node, name) for name in ("lineno", "col_offset", "end_lineno", "end_col_offset")
        ):
            raise BlastRadiusError("AST node lacks a complete source span")
        start = self.line_starts[node.lineno - 1] + node.col_offset  # type: ignore[attr-defined]
        end = self.line_starts[node.end_lineno - 1] + node.end_col_offset  # type: ignore[attr-defined]
        return start, end

    def location(self, start: int, end: int) -> tuple[int, int, int, int]:
        start_line = max(index for index, value in enumerate(self.line_starts) if value <= start)
        end_line = max(index for index, value in enumerate(self.line_starts) if value <= end)
        return (
            start_line + 1,
            start - self.line_starts[start_line],
            end_line + 1,
            end - self.line_starts[end_line],
        )

    def replacement(
        self, operator: str, start: int, end: int, original: str, replacement: str
    ) -> _Candidate:
        actual = self.source_bytes[start:end].decode()
        if actual != original:
            raise BlastRadiusError(
                f"mutation span mismatch: expected {original!r}, found {actual!r}"
            )
        location = self.location(start, end)
        return _Candidate(operator, start, end, *location, original, replacement)

    def between(
        self,
        operator: str,
        left: ast.AST,
        right: ast.AST,
        original: str,
        replacement: str,
    ) -> _Candidate:
        _, search_start = self.node_span(left)
        search_end, _ = self.node_span(right)
        tokens = [item for item in self.tokens if item[1] >= search_start and item[2] <= search_end]
        pieces = original.split()
        for index in range(len(tokens) - len(pieces) + 1):
            group = tokens[index : index + len(pieces)]
            if [item[0] for item in group] == pieces:
                start = group[0][1]
                end = group[-1][2]
                actual = self.source_bytes[start:end].decode()
                return self.replacement(operator, start, end, actual, replacement)
        raise BlastRadiusError(f"could not locate operator {original!r} in source")


class _Collector(ast.NodeVisitor):
    def __init__(self, offsets: _Offsets, changed_lines: set[int]) -> None:
        self.offsets = offsets
        self.changed_lines = changed_lines
        self.candidates: list[_Candidate] = []

    def _add(self, candidate: _Candidate) -> None:
        touched = set(range(candidate.start_line, candidate.end_line + 1))
        if touched & self.changed_lines:
            self.candidates.append(candidate)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        mutation = _BINARY_OPERATORS.get(type(node.op))
        if mutation:
            original, replacement = mutation
            self._add(
                self.offsets.between(
                    f"binary:{original}->{replacement}",
                    node.left,
                    node.right,
                    original,
                    replacement,
                )
            )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        left: ast.AST = node.left
        for operation, right in zip(node.ops, node.comparators, strict=True):
            mutation = _COMPARE_OPERATORS.get(type(operation))
            if mutation:
                original, replacement = mutation
                self._add(
                    self.offsets.between(
                        f"compare:{original}->{replacement}",
                        left,
                        right,
                        original,
                        replacement,
                    )
                )
            left = right
        self.generic_visit(node)

    def _negate(self, test: ast.expr, owner: str) -> None:
        start, end = self.offsets.node_span(test)
        original = self.offsets.source_bytes[start:end].decode()
        self._add(
            self.offsets.replacement(
                f"condition_negation:{owner}", start, end, original, f"not ({original})"
            )
        )

    def visit_If(self, node: ast.If) -> None:
        self._negate(node.test, "if")
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._negate(node.test, "if_expression")
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._negate(node.test, "while")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if isinstance(value, float) and not math.isfinite(value):
            return
        start, end = self.offsets.node_span(node)
        original = self.offsets.source_bytes[start:end].decode()
        replacement_value = 1 if value == 0 else 0 if value == 1 else value + 1
        replacement = repr(replacement_value)
        self._add(self.offsets.replacement("boundary_constant", start, end, original, replacement))

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None and not (
            isinstance(node.value, ast.Constant) and node.value.value is None
        ):
            start, end = self.offsets.node_span(node.value)
            original = self.offsets.source_bytes[start:end].decode()
            self._add(self.offsets.replacement("return_value", start, end, original, "None"))
        self.generic_visit(node)


def apply_mutant(source: str, mutant: MutantSpec) -> str:
    source_bytes = source.encode()
    if hashlib.sha256(source_bytes).hexdigest() != mutant.source_hash:
        raise BlastRadiusError("mutant source hash does not match the supplied source")
    actual = source_bytes[mutant.byte_start : mutant.byte_end].decode()
    if actual != mutant.original:
        raise BlastRadiusError("mutant source span no longer matches its original token")
    mutated = (
        source_bytes[: mutant.byte_start]
        + mutant.replacement.encode()
        + source_bytes[mutant.byte_end :]
    ).decode()
    if hashlib.sha256(mutated.encode()).hexdigest() != mutant.mutated_source_hash:
        raise BlastRadiusError("materialized mutant hash does not match its specification")
    return mutated


def _spec(
    revision: str, path: str, source: str, candidate: _Candidate, seed: int
) -> MutantSpec | None:
    source_bytes = source.encode()
    mutated_bytes = (
        source_bytes[: candidate.byte_start]
        + candidate.replacement.encode()
        + source_bytes[candidate.byte_end :]
    )
    try:
        compile(mutated_bytes.decode(), path, "exec")
    except (SyntaxError, UnicodeDecodeError):
        return None
    identity = (
        f"{revision}:{path}:{candidate.byte_start}:{candidate.byte_end}:"
        f"{candidate.operator}:{candidate.replacement}"
    )
    return MutantSpec(
        hashlib.sha256(identity.encode()).hexdigest()[:20],
        revision,
        candidate.operator,
        path,
        candidate.start_line,
        candidate.start_column,
        candidate.end_line,
        candidate.end_column,
        candidate.byte_start,
        candidate.byte_end,
        candidate.original,
        candidate.replacement,
        hashlib.sha256(source_bytes).hexdigest(),
        hashlib.sha256(mutated_bytes).hexdigest(),
        seed,
    )


def generate_mutants(
    repo: Path,
    base: str,
    head: str,
    *,
    seed: int = 17,
    max_mutants: int = DEFAULT_MAX_MUTANTS,
) -> MutantGenerationReport:
    if max_mutants < 1 or max_mutants > 10_000:
        raise BlastRadiusError("max_mutants must be between 1 and 10000")
    base_identity = resolve_revision(repo, base)
    head_identity = resolve_revision(repo, head)
    changed_regions: dict[str, list[int]] = {}
    candidates: list[MutantSpec] = []
    for file_diff in diff_files(repo, base_identity.sha, head_identity.sha):
        if file_diff.new_path is None or not file_diff.new_lines:
            continue
        source = read_file(repo, head_identity.sha, file_diff.new_path)
        if source is None:
            continue
        try:
            tree = ast.parse(source, filename=file_diff.new_path)
        except SyntaxError as exc:
            raise BlastRadiusError(
                f"cannot generate mutants for invalid head source {file_diff.new_path}: {exc}"
            ) from exc
        changed_lines = set(file_diff.new_lines)
        changed_regions[file_diff.new_path] = sorted(changed_lines)
        collector = _Collector(_Offsets(source), changed_lines)
        collector.visit(tree)
        for candidate in collector.candidates:
            spec = _spec(head_identity.sha, file_diff.new_path, source, candidate, seed)
            if spec is not None:
                candidates.append(spec)
    unique = {item.mutant_id: item for item in candidates}
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            hashlib.sha256(f"{seed}:{item.mutant_id}".encode()).hexdigest(),
            item.mutant_id,
        ),
    )
    selected = ordered[:max_mutants]
    return MutantGenerationReport(
        SCHEMA_VERSION,
        GENERATOR_VERSION,
        base_identity,
        head_identity,
        seed,
        max_mutants,
        dict(sorted(changed_regions.items())),
        len(ordered),
        max(len(ordered) - len(selected), 0),
        selected,
        [
            "Generated mutants are syntax-valid transformations, not proof of semantic validity.",
            (
                "Each mutant span intersects an exact changed head line and must be executed "
                "in isolation."
            ),
            "Reported columns and byte spans use UTF-8 byte offsets.",
            "Surviving mutants must not be labeled equivalent without separate evidence.",
        ],
    )
