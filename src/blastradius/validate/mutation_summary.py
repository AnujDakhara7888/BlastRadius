"""Explicit denominators for bounded mutation experiments, without equivalence claims."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from blastradius.errors import BlastRadiusError
from blastradius.models import (
    MutantExecutionResult,
    MutantGenerationReport,
    MutationComparison,
    MutationGenerationCounts,
    MutationOutcome,
    MutationSuiteSummary,
    MutationSummary,
    SuiteExecution,
    SuiteStatus,
)

_CONCLUSIVE = {MutationOutcome.KILLED, MutationOutcome.SURVIVED}


def generation_counts(generation: MutantGenerationReport) -> MutationGenerationCounts:
    """Reject inconsistent ledgers before executing target code or reporting a score."""
    counts = MutationGenerationCounts(
        generation.total_generated_candidates,
        generation.syntax_rejected_candidates,
        generation.total_valid_candidates,
        len(generation.mutants),
        generation.truncated_candidates,
    )
    if (
        any(
            type(value) is not int or value < 0
            for value in (
                counts.generated,
                counts.syntax_rejected,
                counts.valid,
                counts.truncated,
            )
        )
        or type(generation.max_mutants) is not int
        or not 1 <= generation.max_mutants <= 10_000
        or counts.generated != counts.valid + counts.syntax_rejected
        or counts.valid != counts.sampled + counts.truncated
        or counts.sampled > generation.max_mutants
        or len({mutant.mutant_id for mutant in generation.mutants}) != counts.sampled
    ):
        raise BlastRadiusError("inconsistent mutation generation counts or duplicate mutant IDs")
    return counts


def summarize_mutations(
    generation: MutantGenerationReport,
    baselines: Mapping[str, SuiteExecution],
    results: Sequence[MutantExecutionResult],
) -> MutationSummary:
    """Use killed/(killed+survived) per suite and a shared conclusive cohort for comparison.

    Ratios are fractions in [0, 1], or None for a zero denominator. Exclusions remain visible
    in the per-mutant results; neither surviving nor excluded mutants are called equivalent.
    """
    counts = generation_counts(generation)
    expected = {mutant.mutant_id: mutant for mutant in generation.mutants}
    if (
        set(baselines) != {"selected", "full"}
        or len(results) != counts.sampled
        or len({result.mutant.mutant_id for result in results}) != counts.sampled
        or any(expected.get(result.mutant.mutant_id) != result.mutant for result in results)
    ):
        raise BlastRadiusError("mutation summary requires exactly one matched result per sample")
    summaries: dict[str, MutationSuiteSummary] = {}
    for suite in ("selected", "full"):
        baseline = baselines[suite]
        eligible = baseline.status == SuiteStatus.PASSED
        outcomes = {outcome: 0 for outcome in MutationOutcome}
        executed = 0
        for result in results:
            observation = result.selected if suite == "selected" else result.full
            execution = observation.execution
            if eligible:
                if (
                    execution is None
                    or observation.outcome == MutationOutcome.BASELINE_FAILED
                    or (
                        observation.outcome == MutationOutcome.KILLED
                        and execution.status != SuiteStatus.TEST_FAILURE
                    )
                    or (
                        observation.outcome == MutationOutcome.SURVIVED
                        and execution.status != SuiteStatus.PASSED
                    )
                ):
                    raise BlastRadiusError("mutation result lacks eligible execution evidence")
            else:
                expected_outcome = (
                    MutationOutcome.BASELINE_FAILED
                    if baseline.status == SuiteStatus.TEST_FAILURE
                    else MutationOutcome.INFRASTRUCTURE_ERROR
                    if baseline.status == SuiteStatus.INFRASTRUCTURE_ERROR
                    else MutationOutcome.INCONCLUSIVE
                )
                if execution is not None or observation.outcome != expected_outcome:
                    raise BlastRadiusError("mutation result contradicts its ineligible baseline")
            outcomes[observation.outcome] += 1
            executed += execution is not None
        killed = outcomes[MutationOutcome.KILLED]
        survived = outcomes[MutationOutcome.SURVIVED]
        denominator = killed + survived
        summaries[suite] = MutationSuiteSummary(
            baseline.status,
            eligible,
            counts.sampled if eligible else 0,
            executed,
            killed,
            survived,
            {
                outcome.value: outcomes[outcome]
                for outcome in MutationOutcome
                if outcome not in _CONCLUSIVE
            },
            denominator,
            killed / denominator if denominator else None,
        )
    paired = [
        result
        for result in results
        if result.selected.outcome in _CONCLUSIVE and result.full.outcome in _CONCLUSIVE
    ]
    both_killed = sum(
        result.selected.outcome == result.full.outcome == MutationOutcome.KILLED
        for result in paired
    )
    selected_only = sum(
        result.selected.outcome == MutationOutcome.KILLED
        and result.full.outcome == MutationOutcome.SURVIVED
        for result in paired
    )
    full_only = sum(
        result.selected.outcome == MutationOutcome.SURVIVED
        and result.full.outcome == MutationOutcome.KILLED
        for result in paired
    )
    full_kills = both_killed + full_only
    return MutationSummary(
        counts,
        summaries,
        MutationComparison(
            all(summary.baseline_eligible for summary in summaries.values()),
            len(paired),
            counts.sampled - len(paired),
            both_killed,
            selected_only,
            full_only,
            len(paired) - both_killed - selected_only - full_only,
            full_kills,
            both_killed / full_kills if full_kills else None,
        ),
    )
