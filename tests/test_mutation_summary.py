from __future__ import annotations

from dataclasses import replace
from itertools import product

import pytest

from blastradius.errors import BlastRadiusError
from blastradius.models import (
    SCHEMA_VERSION,
    MutantExecutionResult,
    MutantGenerationReport,
    MutantSpec,
    MutationOutcome,
    MutationSuiteResult,
    RevisionIdentity,
    SuiteExecution,
    SuiteStatus,
    to_primitive,
)
from blastradius.validate.mutate import GENERATOR_VERSION
from blastradius.validate.mutation_summary import generation_counts, summarize_mutations


def _generation(sampled: int, *, truncated: int = 0, rejected: int = 0) -> MutantGenerationReport:
    identity = RevisionIdentity("file:///synthetic-fixture", "a" * 40, "b" * 40)
    # Synthetic observations isolate denominator arithmetic from subprocess timing.
    mutants = [
        MutantSpec(
            str(index),
            identity.sha,
            "return_value",
            "logic.py",
            1,
            0,
            1,
            1,
            0,
            1,
            "1",
            "None",
            "c" * 64,
            "d" * 64,
            17,
        )
        for index in range(sampled)
    ]
    return MutantGenerationReport(
        SCHEMA_VERSION,
        GENERATOR_VERSION,
        identity,
        identity,
        17,
        max(sampled, 1),
        {"logic.py": [1]},
        sampled + truncated + rejected,
        rejected,
        sampled + truncated,
        truncated,
        mutants,
        [],
    )


def _execution(status: SuiteStatus = SuiteStatus.PASSED) -> SuiteExecution:
    return SuiteExecution(
        status,
        0,
        1,
        int(status == SuiteStatus.PASSED),
        int(status == SuiteStatus.TEST_FAILURE),
        0,
        1,
        status.value,
        collected_tests=["tests/test_logic.py::test_value"],
        passed_tests=["tests/test_logic.py::test_value"] if status == SuiteStatus.PASSED else [],
    )


def _observation(outcome: MutationOutcome) -> MutationSuiteResult:
    status = {
        MutationOutcome.KILLED: SuiteStatus.TEST_FAILURE,
        MutationOutcome.SURVIVED: SuiteStatus.PASSED,
        MutationOutcome.INFRASTRUCTURE_ERROR: SuiteStatus.INFRASTRUCTURE_ERROR,
        MutationOutcome.INCONCLUSIVE: SuiteStatus.TIMEOUT,
    }[outcome]
    return MutationSuiteResult(outcome, outcome.value, _execution(status))


def test_scores_exclude_errors_and_comparison_uses_the_same_conclusive_cohort() -> None:
    outcomes = [
        MutationOutcome.KILLED,
        MutationOutcome.SURVIVED,
        MutationOutcome.INFRASTRUCTURE_ERROR,
        MutationOutcome.INCONCLUSIVE,
    ]
    pairs = list(product(outcomes, repeat=2))
    generation = _generation(len(pairs), truncated=3, rejected=2)
    results = [
        MutantExecutionResult(mutant, _observation(selected), _observation(full))
        for mutant, (selected, full) in zip(generation.mutants, pairs, strict=True)
    ]

    summary = summarize_mutations(
        generation, {"selected": _execution(), "full": _execution()}, results
    )

    assert to_primitive(summary.generation) == {
        "generated": 21,
        "syntax_rejected": 2,
        "valid": 19,
        "sampled": 16,
        "truncated": 3,
    }
    for suite in summary.suites.values():
        assert suite.baseline_eligible
        assert suite.eligible == suite.executed == 16
        assert suite.killed == suite.survived == 4
        assert suite.exclusions == {
            "baseline_failed": 0,
            "infrastructure_error": 4,
            "inconclusive": 4,
        }
        assert suite.score_denominator == 8
        assert suite.kill_ratio == 0.5
        assert suite.score_denominator + sum(suite.exclusions.values()) == 16
    assert to_primitive(summary.comparison) == {
        "baseline_eligible": True,
        "comparable": 4,
        "excluded": 12,
        "both_killed": 1,
        "selected_only_killed": 1,
        "full_only_killed": 1,
        "both_survived": 1,
        "full_kill_denominator": 2,
        "kill_retention_ratio": 0.5,
    }


def test_different_suite_denominators_do_not_inflate_kill_retention() -> None:
    generation = _generation(2)
    results = [
        MutantExecutionResult(
            generation.mutants[0],
            _observation(MutationOutcome.INCONCLUSIVE),
            _observation(MutationOutcome.KILLED),
        ),
        MutantExecutionResult(
            generation.mutants[1],
            _observation(MutationOutcome.SURVIVED),
            _observation(MutationOutcome.KILLED),
        ),
    ]
    summary = summarize_mutations(
        generation, {"selected": _execution(), "full": _execution()}, results
    )

    assert summary.suites["selected"].score_denominator == 1
    assert summary.suites["full"].score_denominator == 2
    assert summary.comparison.comparable == summary.comparison.excluded == 1
    assert summary.comparison.full_kill_denominator == 1
    assert summary.comparison.kill_retention_ratio == 0.0


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (SuiteStatus.TEST_FAILURE, MutationOutcome.BASELINE_FAILED),
        (SuiteStatus.INFRASTRUCTURE_ERROR, MutationOutcome.INFRASTRUCTURE_ERROR),
        (SuiteStatus.TIMEOUT, MutationOutcome.INCONCLUSIVE),
        (SuiteStatus.NO_TESTS, MutationOutcome.INCONCLUSIVE),
        (SuiteStatus.OUTPUT_LIMIT, MutationOutcome.INCONCLUSIVE),
    ],
)
def test_baseline_exclusion_is_independent_for_each_suite(
    status: SuiteStatus,
    outcome: MutationOutcome,
) -> None:
    generation = _generation(1)
    result = MutantExecutionResult(
        generation.mutants[0],
        MutationSuiteResult(outcome, "baseline", None),
        _observation(MutationOutcome.KILLED),
    )
    summary = summarize_mutations(
        generation, {"selected": _execution(status), "full": _execution()}, [result]
    )

    selected = summary.suites["selected"]
    assert not selected.baseline_eligible
    assert selected.eligible == selected.executed == selected.score_denominator == 0
    assert selected.exclusions[outcome.value] == 1
    assert selected.kill_ratio is None
    assert summary.suites["full"].kill_ratio == 1.0
    assert not summary.comparison.baseline_eligible
    assert summary.comparison.comparable == 0
    assert summary.comparison.excluded == 1
    assert summary.comparison.kill_retention_ratio is None


@pytest.mark.parametrize("sampled", [0, 1])
def test_no_full_kills_has_no_retention_ratio(sampled: int) -> None:
    generation = _generation(sampled)
    results = [
        MutantExecutionResult(
            mutant, _observation(MutationOutcome.SURVIVED), _observation(MutationOutcome.SURVIVED)
        )
        for mutant in generation.mutants
    ]
    summary = summarize_mutations(
        generation, {"selected": _execution(), "full": _execution()}, results
    )

    for suite in summary.suites.values():
        assert suite.score_denominator == sampled
        assert suite.kill_ratio == (0.0 if sampled else None)
    assert summary.comparison.comparable == summary.comparison.both_survived == sampled
    assert summary.comparison.full_kill_denominator == 0
    assert summary.comparison.kill_retention_ratio is None


def test_rejects_inconsistent_counts_and_unmatched_results() -> None:
    generation = _generation(1)
    for invalid in (
        replace(generation, total_generated_candidates=2),
        replace(generation, syntax_rejected_candidates=-1),
        replace(generation, total_valid_candidates=True),
        replace(generation, truncated_candidates=1),
        replace(generation, max_mutants=0),
        replace(
            generation,
            mutants=generation.mutants * 2,
            total_generated_candidates=2,
            total_valid_candidates=2,
            max_mutants=2,
        ),
    ):
        with pytest.raises(BlastRadiusError, match="generation counts"):
            generation_counts(invalid)
    result = MutantExecutionResult(
        generation.mutants[0],
        _observation(MutationOutcome.KILLED),
        _observation(MutationOutcome.SURVIVED),
    )
    baselines = {"selected": _execution(), "full": _execution()}
    for results in (
        [],
        [result, result],
        [replace(result, mutant=replace(result.mutant, mutant_id="unknown"))],
        [replace(result, mutant=replace(result.mutant, source_hash="stale"))],
    ):
        with pytest.raises(BlastRadiusError, match="matched result"):
            summarize_mutations(generation, baselines, results)
    with pytest.raises(BlastRadiusError, match="matched result"):
        summarize_mutations(generation, {"selected": _execution()}, [result])


def test_rejects_scores_without_matching_baseline_and_execution_evidence() -> None:
    generation = _generation(1)
    result = MutantExecutionResult(
        generation.mutants[0],
        _observation(MutationOutcome.KILLED),
        _observation(MutationOutcome.SURVIVED),
    )
    with pytest.raises(BlastRadiusError, match="ineligible baseline"):
        summarize_mutations(
            generation,
            {"selected": _execution(SuiteStatus.TEST_FAILURE), "full": _execution()},
            [result],
        )
    for observation in (
        replace(result.selected, execution=None),
        replace(result.selected, execution=_execution(SuiteStatus.TIMEOUT)),
        MutationSuiteResult(
            MutationOutcome.SURVIVED, "false survival", _execution(SuiteStatus.NO_TESTS)
        ),
        MutationSuiteResult(MutationOutcome.BASELINE_FAILED, "false baseline", _execution()),
    ):
        with pytest.raises(BlastRadiusError, match="eligible execution"):
            summarize_mutations(
                generation,
                {"selected": _execution(), "full": _execution()},
                [replace(result, selected=observation)],
            )
