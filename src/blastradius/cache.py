"""Versioned SQLite cache for immutable static-analysis indexes."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from pathlib import Path
from typing import Any

from blastradius import __version__
from blastradius.models import (
    SCHEMA_VERSION,
    DependencyEdge,
    EdgeKind,
    RevisionIdentity,
    Symbol,
    SymbolId,
    to_primitive,
)
from blastradius.static.analyzer import StaticIndex

STATIC_ANALYZER_VERSION = "static-v2"


def static_cache_key(identity: RevisionIdentity) -> str:
    identity_payload = {
        "repository": identity.repository,
        "revision": identity.sha,
        "tree": identity.tree,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "tool_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "analyzer_version": STATIC_ANALYZER_VERSION,
        "settings": {
            "language": "python",
            "source_scope": "all_tracked_python",
            "include_tests": True,
        },
    }
    encoded = json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _serialize(index: StaticIndex) -> str:
    payload = {
        "symbols": [to_primitive(symbol) for _, symbol in sorted(index.symbols.items())],
        "edges": [to_primitive(edge) for edge in index.edges],
        "unresolved_calls": index.unresolved_calls,
        "uncertainty_sites": index.uncertainty_sites,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _deserialize(payload: str, expected_revision: str) -> StaticIndex:
    raw: dict[str, Any] = json.loads(payload)
    index = StaticIndex()
    for item in raw["symbols"]:
        symbol_id = SymbolId(**item["id"])
        if symbol_id.revision != expected_revision:
            raise ValueError("cached symbol revision does not match cache identity")
        symbol = Symbol(
            id=symbol_id,
            start_line=item["start_line"],
            end_line=item["end_line"],
            body_start_line=item["body_start_line"],
            source_hash=item["source_hash"],
            structure_hash=item["structure_hash"],
            module=item["module"],
            parent_scope=item.get("parent_scope"),
        )
        index.symbols[symbol.id.key] = symbol
    index.edges = [
        DependencyEdge(
            caller=item["caller"],
            callee=item["callee"],
            kind=EdgeKind(item["kind"]),
            provenance=item["provenance"],
            confidence=item["confidence"],
            observed_count=item.get("observed_count"),
        )
        for item in raw["edges"]
    ]
    index.unresolved_calls = list(raw["unresolved_calls"])
    index.uncertainty_sites = list(raw["uncertainty_sites"])
    return index


class StaticIndexCache:
    """Small process-safe cache; callers own key construction and rebuild policy."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS static_indexes (
                cache_key TEXT PRIMARY KEY,
                revision TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        return connection

    def load(self, key: str, revision: str) -> tuple[StaticIndex | None, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT revision, payload FROM static_indexes WHERE cache_key = ?", (key,)
            ).fetchone()
            if row is None:
                return None, "miss"
            try:
                if row[0] != revision:
                    raise ValueError("cached row revision mismatch")
                return _deserialize(str(row[1]), revision), "hit"
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                connection.execute("DELETE FROM static_indexes WHERE cache_key = ?", (key,))
                return None, "corrupt"

    def store(self, key: str, revision: str, index: StaticIndex) -> None:
        payload = _serialize(index)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO static_indexes (cache_key, revision, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    revision = excluded.revision,
                    payload = excluded.payload,
                    created_at = CURRENT_TIMESTAMP
                """,
                (key, revision, payload),
            )
