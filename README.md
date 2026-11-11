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

## Bounded mutation experiments

Compare analysis-selected tests with the full pytest suite against sampled mutations of exact
changed head regions:

```bash
blastradius mutate \
  --repo /path/to/repo --base BASE_SHA --head HEAD_SHA \
  --coverage /tmp/base-coverage.json --mode conservative \
  --seed 17 --max-mutants 20 --timeout 30 --json mutation.json
```

Use the target repository's compatible Python environment with pytest installed. The command
generates mutants, analyzes the same immutable revision pair, and runs separate clean-head
baselines before executing mutants. The JSON contains `analysis` (revision identities, selection
policy, reasons, uncovered/truncated obligations, warnings, and cache state) and `execution`
(generation ledger, actual suite selectors, baselines, individual outcomes, limits, environment,
warnings, and score denominators). `--runtime`, `--cache-file`, and `--no-cache` work as in analysis.

Missing coverage or another full-suite fallback uses default pytest discovery for **both** suites,
honoring the head revision's pytest configuration, rather than treating known profile tests as the
entire suite. This is recorded as `execution.selected_full_suite=true`; empty selector lists then
mean default discovery. Without fallback, an empty selected list stays empty and is inconclusive.
Full-suite discovery always uses the archived head. Dirty working-tree code is not executed.

The deterministic seed defaults to 17; `--max-mutants` defaults to 100 (range 1–10000).
`--timeout` defaults to 30 seconds per baseline or mutant suite (range 1–1800), not a total
experiment deadline. Up to two baselines and two suite executions per sampled mutant may run.
Start with a small cap. Exit status 0 means the report was written; inspect baseline statuses,
exclusions, and denominators to assess the experiment. Invalid inputs produce status 2.
Target code executes locally; use only trusted, side-effect-controlled repositories.

The execution report includes an explicit `summary` for bounded changed-region experiments:

- `generation.generated` counts unique candidate transformations before compilation;
  `syntax_rejected` counts compile failures, and `valid` counts syntax-valid candidates.
  Thus `generated = syntax_rejected + valid`. Syntax validity does not establish semantic validity.
- `sampled` counts the deterministically seeded, capped mutant list; `truncated` counts valid
  candidates omitted by the cap. Thus `valid = sampled + truncated`. Rejected and truncated
  candidates never enter an execution denominator.
- Each suite reports its own clean-head baseline status, eligibility, eligible and executed mutant
  counts, kills, survivals, and exclusions by `baseline_failed`, `infrastructure_error`, or
  `inconclusive`. Only a passing baseline permits execution. `score_denominator = killed + survived`;
  `kill_ratio = killed / score_denominator`. `executed` counts recorded mutant execution attempts,
  including infrastructure failures after a passing baseline; it excludes baseline executions.
- The selected/full comparison uses only mutants with conclusive outcomes in **both** suites.
  It reports both killed, selected-only killed, full-only killed, both survived, and excluded counts.
  `full_kill_denominator = both_killed + full_only_killed` on that shared cohort, and
  `kill_retention_ratio = both_killed / full_kill_denominator`. Excluded full-suite kills remain
  visible in individual results and the full-suite summary; they are not silently treated as
  selected-suite survivals. No subset relationship between suite selectors is assumed.

Ratios are fractions from 0 to 1; a zero denominator produces JSON `null`, including a comparison
with no full-suite kills. Every sampled mutant retains both outcomes and exclusion reasons in
`results`. These are observations about the sampled changed-region mutants, not estimates of a
whole-project mutation score or evidence that survivors are equivalent. Old internal generation
reports without the count ledger must be regenerated; generator and runner versions are now `v2`.

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
- Deterministic, syntax-validated mutant specifications restricted to exact changed regions;
  matched selected/full pytest runners with clean-head baselines and explicit failure,
  infrastructure, timeout, and inconclusive outcomes; explicit generation and score denominators
  with paired outcome comparisons and a public CLI retaining revision and selection provenance.
- A transparent five-component risk model with visible inputs, weights, contributions, and warnings.

## Measured limitations

- Python reflection, dynamic dispatch, monkeypatching, import hooks, wildcard imports, and unresolved
  attribute calls are retained as location-specific uncertainty; this is not a sound or complete
  Python call graph. Typing-only relationships are distinguished with lower confidence.
- Base coverage cannot see newly introduced control flow. Conservative mode falls back when affected
  obligations are uncovered, but “conservative” does not mean formally safe.
- Runtime tracing uses `sys.setprofile` in-process for this baseline. It records scoped call edges and
  counts, not arguments, and makes no low-overhead claim.
- Replay, persistent caching, and the bounded mutation CLI were added after v0.1.0. HTML,
  benchmark campaigns, and GitHub PR reporting are not implemented yet.
- Mutation runs use the current interpreter's dependencies with pytest plugin autoload
  disabled. Each suite must pass on the immutable clean head before its mutants execute, with the
  same timeout and environment policy. A kill requires pytest test-phase failure; collection errors
  are infrastructure failures, and timeouts, empty/skipped-only suites, or changed passing test sets
  are inconclusive. Fresh snapshots and sanitized processes are not a security sandbox. POSIX runs
  clean up worker process groups; other platforms terminate only the direct worker. Output is polled
  against a limit and retained logs are truncated; this is not a hard disk-resource quota.
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
