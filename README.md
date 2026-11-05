# BlastRadius

BlastRadius is a local Python CLI that explains what a code change may affect and which tests
exercise the affected code. It combines Git diff mapping, a conservative static call graph,
base-revision per-test coverage, and optional representative-workload observations.

This is a research prototype. Its risk score is a bounded **heuristic prioritization score**, not
a probability of failure. Static relationships are possible relationships, workload traces are
only observations from the named workload, and test execution does not prove assertion quality.

## Install

BlastRadius supports CPython 3.11 through 3.14. From a clean environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -c requirements-dev.lock -e '.[dev]'
blastradius --version
blastradius --help
```

The target repository must collect coverage in its own compatible Python environment. Installing
BlastRadius there adds `coverage`; its test environment must also provide `pytest` and `pytest-cov`.

Run the self-contained demo after installation:

```bash
python examples/run_demo.py --output demo-output
```

It creates a disposable two-commit repository, collects real base coverage, executes one supported
replay case against both revisions, and writes analysis and replay artifacts under `demo-output/`.

## End-to-end workflow

Collect evidence on the checked-out base revision:

```bash
blastradius coverage collect \
  --repo /path/to/repo --rev HEAD --target src/package \
  --profile-out /tmp/base-coverage.json -- tests
```

Optionally trace an explicit, deterministic representative workload on that revision:

```bash
blastradius trace \
  --repo /path/to/repo --rev HEAD --workload examples/workload.py \
  --profile-out /tmp/base-runtime.json
```

After making or checking out the head commit, analyze without rerunning the full suite:

```bash
blastradius analyze \
  --repo /path/to/repo --base BASE_SHA --head HEAD_SHA \
  --coverage /tmp/base-coverage.json --runtime /tmp/base-runtime.json \
  --mode conservative --json report.json --markdown report.md
```

Omit `--runtime` when no representative workload exists. Omit `--coverage` only to obtain an
explicit conservative full-suite fallback report; missing evidence never silently excludes code.

Selection modes are intentionally different:

- `conservative` takes the union of tests covering every mapped impact obligation and falls back
  to the full suite when affected obligations lack evidence.
- `balanced` takes that union through two incoming dependency edges, always retains direct and
  changed-test obligations, and reports every deeper obligation it truncated.
- `aggressive` applies deterministic greedy set cover to all explicit obligations. It reports
  uncovered obligations and is not claimed to be mathematically minimal. Coverage-equivalent tests
  can still contain different assertions.

Dependency, pytest configuration, and shared-fixture changes require a full-suite fallback in all
three modes.

Static indexes are cached in a Git-owned SQLite database resolved with `git rev-parse --git-path`,
so normal and linked worktrees do not assume that `.git` is a directory. Keys include repository,
full commit and tree identities, Python implementation/version, tool/schema/analyzer versions, and
selection-relevant analyzer settings. Reports expose base/head hit or miss state. Corrupt rows are
deleted and rebuilt; use `--no-cache` to disable persistence or `--cache-file PATH` to override the
location. Coverage and runtime profiles retain their own stricter revision and environment identity.

## Bounded differential replay

Replay accepts explicit strict-JSON calls to synchronous functions defined at module scope in flat
or conventional `src/` layouts. The cases file is tied to the exact base identity (copy the `base`
object from an analysis report):

```json
{
  "schema_version": "1.0",
  "adapter_version": "json-call-v1",
  "revision": {
    "repository": "file:///path/to/repo",
    "sha": "FULL_BASE_SHA",
    "tree": "FULL_BASE_TREE_SHA"
  },
  "normalization": "exact",
  "cases": [
    {
      "case_id": "rounding-example",
      "function": {"path": "shop.py", "name": "checkout"},
      "args": [1.236],
      "kwargs": {}
    }
  ]
}
```

Run the same captured inputs against actual immutable base and head snapshots:

```bash
blastradius replay \
  --repo /path/to/repo --base BASE_SHA --head HEAD_SHA \
  --cases replay-cases.json --timeout 5 --json replay.json
```

Each case and revision runs in a separate sanitized subprocess with a fresh extracted filesystem
snapshot. Exact JSON return values and captured stdout/stderr are compared without stripping fields.
Reports distinguish `identical`, `changed`, `execution_error`, `unsupported`, and `diverged`.
A changed result may be intentional; bounded replay is evidence, not a regression verdict.

The adapter rejects stale identities, nested or bound methods, async/generator functions,
non-JSON returns, non-importable paths, oversized inputs/outputs, symlinked archive entries, and
unsupported normalization. Timeouts and abnormal worker exits are reported as divergence. Target
code still executes locally: process/filesystem separation and environment sanitization are not a
security sandbox, so only trusted, side-effect-controlled code should be replayed.

## Implemented scope

- Immutable commit/tree identity and two-sided Git hunk parsing, including adds/deletes/renames.
- Decorator-aware, nested-symbol diff mapping.
- Lexical, import-alias, module-attribute, relative-import, and `self`/`cls` call edges, plus
  distinct module-import, decorator, and inheritance relationships.
- Reverse traversal to find dependents; cycles terminate deterministically.
- Base-revision pytest node-ID coverage with setup/run/teardown phases and identity validation.
- Conservative selection, changed-test inclusion, uncovered-code fallback, stable JSON, and Markdown.
- Representative-workload call counts labelled as such; absence never removes static possibilities.
- Bounded JSON differential replay against actual base/head snapshots with explicit outcome classes.
- A transparent five-component risk model with visible inputs, weights, contributions, and warnings.

## Measured limitations

- Python reflection, dynamic dispatch, monkeypatching, import hooks, wildcard imports, and unresolved
  attribute calls are retained as location-specific uncertainty; this is not a sound or complete
  Python call graph. Typing-only relationships are distinguished with lower confidence.
- Base coverage cannot see newly introduced control flow. Conservative mode falls back when affected
  obligations are uncovered, but “conservative” does not mean formally safe.
- Runtime tracing uses `sys.setprofile` in-process for this baseline. It records scoped call edges and
  counts, not arguments, and makes no low-overhead claim.
- Replay and persistent caching were added after v0.1.0. Mutation experiments, HTML, benchmark
  campaigns, and GitHub PR reporting are not implemented yet; the CLI does not expose them.
- Target code is executable. A virtual environment is dependency isolation, not a security sandbox.

## Development

```bash
ruff format --check .
ruff check .
mypy src
pytest
```

The tests create disposable Git repositories to verify dependency direction, cycles, stale-profile
rejection, fallback behavior, deterministic output, and the end-to-end CLI path.
