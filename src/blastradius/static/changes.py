"""Map Git hunks to symbols and match old/new correspondence conservatively."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from blastradius.models import ChangedSymbol, ChangeKind, Symbol
from blastradius.static.analyzer import StaticIndex
from blastradius.vcs.git import FileDiff


@dataclass(slots=True)
class _Hit:
    symbol: Symbol
    lines: set[int] = field(default_factory=set)


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


def _add_hit(hits: dict[str, _Hit], symbol: Symbol, lines: set[int] | None = None) -> None:
    hit = hits.setdefault(symbol.id.key, _Hit(symbol))
    hit.lines.update(lines or set())


def _unique_fingerprint_matches(
    base_hits: dict[str, _Hit],
    head_hits: dict[str, _Hit],
    unmatched_base: set[str],
    unmatched_head: set[str],
    fingerprint: Callable[[Symbol], str],
) -> list[tuple[str, str]]:
    base_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    head_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key in sorted(unmatched_base):
        symbol = base_hits[key].symbol
        base_groups[(symbol.id.kind, fingerprint(symbol))].append(key)
    for key in sorted(unmatched_head):
        symbol = head_hits[key].symbol
        head_groups[(symbol.id.kind, fingerprint(symbol))].append(key)
    result: list[tuple[str, str]] = []
    for group in sorted(base_groups.keys() & head_groups.keys()):
        old = base_groups[group]
        new = head_groups[group]
        if len(old) == 1 and len(new) == 1:
            result.append((old[0], new[0]))
    return result


def map_changes(diffs: list[FileDiff], base: StaticIndex, head: StaticIndex) -> list[ChangedSymbol]:
    base_by_path: dict[str, list[Symbol]] = defaultdict(list)
    head_by_path: dict[str, list[Symbol]] = defaultdict(list)
    for symbol in base.symbols.values():
        base_by_path[symbol.id.path].append(symbol)
    for symbol in head.symbols.values():
        head_by_path[symbol.id.path].append(symbol)

    base_hits: dict[str, _Hit] = {}
    head_hits: dict[str, _Hit] = {}
    local_pairs: list[tuple[str, str, float]] = []
    for diff in diffs:
        local_base: dict[str, _Hit] = {}
        local_head: dict[str, _Hit] = {}
        for line in diff.old_lines:
            base_symbol = _innermost(base_by_path.get(diff.old_path or "", []), line)
            if base_symbol:
                _add_hit(local_base, base_symbol, {line})
                _add_hit(base_hits, base_symbol, {line})
        for line in diff.new_lines:
            head_symbol = _innermost(head_by_path.get(diff.new_path or "", []), line)
            if head_symbol:
                _add_hit(local_head, head_symbol, {line})
                _add_hit(head_hits, head_symbol, {line})

        is_file_move = diff.status == "R" and diff.old_path != diff.new_path
        if is_file_move:
            for symbol in base_by_path.get(diff.old_path or "", []):
                _add_hit(local_base, symbol)
                _add_hit(base_hits, symbol)
            for symbol in head_by_path.get(diff.new_path or "", []):
                _add_hit(local_head, symbol)
                _add_hit(head_hits, symbol)

        base_identity = {
            (hit.symbol.id.qualified_name, hit.symbol.id.kind, hit.symbol.id.occurrence): key
            for key, hit in local_base.items()
        }
        head_identity = {
            (hit.symbol.id.qualified_name, hit.symbol.id.kind, hit.symbol.id.occurrence): key
            for key, hit in local_head.items()
        }
        for identity in sorted(base_identity.keys() & head_identity.keys()):
            old_key = base_identity[identity]
            new_key = head_identity[identity]
            confidence = 1.0
            if is_file_move:
                old_symbol = local_base[old_key].symbol
                new_symbol = local_head[new_key].symbol
                confidence = (
                    0.99
                    if old_symbol.source_hash == new_symbol.source_hash
                    else (0.95 if old_symbol.structure_hash == new_symbol.structure_hash else 0.75)
                )
            local_pairs.append(
                (
                    old_key,
                    new_key,
                    confidence,
                )
            )

    unmatched_base = set(base_hits)
    unmatched_head = set(head_hits)
    matched: list[tuple[str, str, float]] = []
    for old_key, new_key, confidence in local_pairs:
        if old_key in unmatched_base and new_key in unmatched_head:
            matched.append((old_key, new_key, confidence))
            unmatched_base.remove(old_key)
            unmatched_head.remove(new_key)

    exact_matches = _unique_fingerprint_matches(
        base_hits,
        head_hits,
        unmatched_base,
        unmatched_head,
        lambda symbol: symbol.source_hash,
    )
    for old_key, new_key in exact_matches:
        matched.append((old_key, new_key, 0.98))
        unmatched_base.remove(old_key)
        unmatched_head.remove(new_key)

    structural_matches = _unique_fingerprint_matches(
        base_hits,
        head_hits,
        unmatched_base,
        unmatched_head,
        lambda symbol: symbol.structure_hash,
    )
    for old_key, new_key in structural_matches:
        matched.append((old_key, new_key, 0.85))
        unmatched_base.remove(old_key)
        unmatched_head.remove(new_key)

    result: list[ChangedSymbol] = []
    for old_key, new_key, confidence in matched:
        old = base_hits[old_key]
        new = head_hits[new_key]
        moved_or_renamed = (
            old.symbol.id.path != new.symbol.id.path
            or old.symbol.id.qualified_name != new.symbol.id.qualified_name
        )
        result.append(
            ChangedSymbol(
                ChangeKind.RENAMED if moved_or_renamed else ChangeKind.MODIFIED,
                old.symbol,
                new.symbol,
                tuple(sorted(old.lines)),
                tuple(sorted(new.lines)),
                confidence,
            )
        )

    def ambiguity_for(hit: _Hit, candidates: set[str], other: dict[str, _Hit]) -> str | None:
        exact = [
            key
            for key in candidates
            if other[key].symbol.id.kind == hit.symbol.id.kind
            and other[key].symbol.source_hash == hit.symbol.source_hash
        ]
        structural = [
            key
            for key in candidates
            if other[key].symbol.id.kind == hit.symbol.id.kind
            and other[key].symbol.structure_hash == hit.symbol.structure_hash
        ]
        if len(exact) > 1:
            return "multiple exact-source correspondence candidates"
        if len(structural) > 1:
            return "multiple structural correspondence candidates"
        return None

    for key in sorted(unmatched_base):
        hit = base_hits[key]
        result.append(
            ChangedSymbol(
                ChangeKind.DELETED,
                hit.symbol,
                None,
                tuple(sorted(hit.lines)),
                (),
                0.0 if ambiguity_for(hit, unmatched_head, head_hits) else 1.0,
                ambiguity_for(hit, unmatched_head, head_hits),
            )
        )
    for key in sorted(unmatched_head):
        hit = head_hits[key]
        result.append(
            ChangedSymbol(
                ChangeKind.ADDED,
                None,
                hit.symbol,
                (),
                tuple(sorted(hit.lines)),
                0.0 if ambiguity_for(hit, unmatched_base, base_hits) else 1.0,
                ambiguity_for(hit, unmatched_base, base_hits),
            )
        )

    def sort_key(item: ChangedSymbol) -> tuple[str, str, str]:
        current = item.head or item.base
        return (
            current.id.path if current else "",
            current.id.qualified_name if current else "",
            item.change.value,
        )

    return sorted(result, key=sort_key)
