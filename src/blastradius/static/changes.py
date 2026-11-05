"""Map Git hunks to the innermost relevant symbols on both revisions."""

from __future__ import annotations

from collections import defaultdict

from blastradius.models import ChangedSymbol, ChangeKind, Symbol
from blastradius.static.analyzer import StaticIndex
from blastradius.vcs.git import FileDiff


def _innermost(symbols: list[Symbol], line: int) -> Symbol | None:
    matches = [symbol for symbol in symbols if symbol.start_line <= line <= symbol.end_line]
    if not matches:
        return None
    return min(
        matches,
        key=lambda symbol: (
            symbol.end_line - symbol.start_line,
            symbol.id.kind == "module",
            -symbol.start_line,
        ),
    )


def map_changes(diffs: list[FileDiff], base: StaticIndex, head: StaticIndex) -> list[ChangedSymbol]:
    base_by_path: dict[str, list[Symbol]] = defaultdict(list)
    head_by_path: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in base.symbols.values():
        base_by_path[symbol.id.path].append(symbol)
    for symbol in head.symbols.values():
        head_by_path[symbol.id.path].append(symbol)

    result: list[ChangedSymbol] = []
    seen: set[tuple[str | None, str | None]] = set()
    for diff in diffs:
        base_hits: dict[str, tuple[Symbol, set[int]]] = {}
        head_hits: dict[str, tuple[Symbol, set[int]]] = {}
        for line in diff.old_lines:
            base_symbol = _innermost(base_by_path.get(diff.old_path or "", []), line)
            if base_symbol:
                base_hits.setdefault(base_symbol.id.key, (base_symbol, set()))[1].add(line)
        for line in diff.new_lines:
            head_symbol = _innermost(head_by_path.get(diff.new_path or "", []), line)
            if head_symbol:
                head_hits.setdefault(head_symbol.id.key, (head_symbol, set()))[1].add(line)

        base_by_name = {item[0].id.qualified_name: item for item in base_hits.values()}
        head_by_name = {item[0].id.qualified_name: item for item in head_hits.values()}
        for name in sorted(base_by_name.keys() | head_by_name.keys()):
            old = base_by_name.get(name)
            new = head_by_name.get(name)
            pair = (old[0].id.key if old else None, new[0].id.key if new else None)
            if pair in seen:
                continue
            seen.add(pair)
            kind = (
                ChangeKind.MODIFIED
                if old and new
                else (ChangeKind.DELETED if old else ChangeKind.ADDED)
            )
            result.append(
                ChangedSymbol(
                    kind,
                    old[0] if old else None,
                    new[0] if new else None,
                    tuple(sorted(old[1])) if old else (),
                    tuple(sorted(new[1])) if new else (),
                )
            )

    def sort_key(item: ChangedSymbol) -> tuple[str, str]:
        current = item.head or item.base
        return (current.id.path, current.id.qualified_name) if current else ("", "")

    return sorted(result, key=sort_key)
