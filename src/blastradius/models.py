"""Versioned data contracts for profiles and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "1.0"
RISK_MODEL_VERSION = "heuristic-v1"


class ChangeKind(StrEnum):
    ADDED = "added"
    DELETED = "deleted"
    MODIFIED = "modified"
    RENAMED = "renamed"


class EdgeKind(StrEnum):
    CALL = "call"
    IMPORT = "import"
    INHERITANCE = "inheritance"
    DECORATOR = "decorator"
    RUNTIME_CALL = "runtime_call"


class FallbackState(StrEnum):
    NONE = "none"
    BROAD = "broad"
    FULL_SUITE = "full_suite"


class ReplayOutcome(StrEnum):
    IDENTICAL = "identical"
    CHANGED = "changed"
    EXECUTION_ERROR = "execution_error"
    UNSUPPORTED = "unsupported"
    DIVERGED = "diverged"


@dataclass(frozen=True, slots=True)
class RevisionIdentity:
    repository: str
    sha: str
    tree: str


@dataclass(frozen=True, slots=True)
class SymbolId:
    revision: str
    path: str
    qualified_name: str
    kind: str
    occurrence: int = 0

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qualified_name}:{self.kind}:{self.occurrence}"


@dataclass(frozen=True, slots=True)
class Symbol:
    id: SymbolId
    start_line: int
    end_line: int
    body_start_line: int
    source_hash: str
    structure_hash: str
    module: str
    parent_scope: str | None = None


@dataclass(frozen=True, slots=True)
class ChangedSymbol:
    change: ChangeKind
    base: Symbol | None
    head: Symbol | None
    base_lines: tuple[int, ...] = ()
    head_lines: tuple[int, ...] = ()
    match_confidence: float = 1.0
    ambiguity: str | None = None


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    caller: str
    callee: str
    kind: EdgeKind
    provenance: str
    confidence: float
    observed_count: int | None = None


@dataclass(slots=True)
class CoverageProfile:
    schema_version: str
    revision: RevisionIdentity
    created_at: str
    python: str
    coverage_version: str
    collection_fingerprint: str
    test_lines: dict[str, dict[str, list[int]]]
    phases: dict[str, list[str]]
    unmapped_contexts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeProfile:
    schema_version: str
    revision: RevisionIdentity
    workload_id: str
    workload_hash: str
    source_type: str
    created_at: str
    python: str
    edges: list[DependencyEdge]
    dropped_events: int = 0


@dataclass(frozen=True, slots=True)
class ImpactEvidence:
    changed_root: str
    affected_symbol: str
    dependency_path: tuple[str, ...]
    direction: str
    confidence: float
    sources: tuple[str, ...]


@dataclass(slots=True)
class TestSelection:
    policy: str
    available_tests: list[str]
    selected_tests: list[str]
    reasons: dict[str, list[str]]
    fallback: FallbackState
    uncovered_obligations: list[str]
    truncated_obligations: list[str]
    provenance: list[str]


@dataclass(slots=True)
class RiskScore:
    score: float
    model_version: str
    inputs: dict[str, float]
    weights: dict[str, float]
    contributions: dict[str, float]
    label: str = "heuristic prioritization score; not a failure probability"


@dataclass(slots=True)
class AnalysisReport:
    schema_version: str
    tool_version: str
    base: RevisionIdentity
    head: RevisionIdentity
    mode: str
    changed_symbols: list[ChangedSymbol]
    impact: list[ImpactEvidence]
    selection: TestSelection
    risk: RiskScore
    warnings: list[str]
    timings_ms: dict[str, float]
    cache_state: str = "disabled"


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    path: str
    function: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]

    @property
    def function_id(self) -> str:
        return f"{self.path}:{self.function}"


@dataclass(slots=True)
class ReplayResult:
    case_id: str
    function_id: str
    outcome: ReplayOutcome
    base_observation: dict[str, Any] | None
    head_observation: dict[str, Any] | None
    divergence: dict[str, Any] | None
    unsupported_reason: str | None
    durations_ms: dict[str, float]


@dataclass(slots=True)
class ReplayReport:
    schema_version: str
    adapter_version: str
    base: RevisionIdentity
    head: RevisionIdentity
    capture_revision: RevisionIdentity
    input_schema: str
    normalization: str
    environment: dict[str, str]
    limits: dict[str, int]
    results: list[ReplayResult]
    warnings: list[str]


def to_primitive(value: Any) -> Any:
    """Convert nested contracts into stable JSON-compatible primitives."""
    if hasattr(value, "__dataclass_fields__"):
        return to_primitive(asdict(value))
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): to_primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [to_primitive(item) for item in value]
    return value
