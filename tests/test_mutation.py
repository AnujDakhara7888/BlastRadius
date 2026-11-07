from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git

from blastradius.errors import BlastRadiusError
from blastradius.validate.mutate import apply_mutant, generate_mutants

BASE = """\
def calculate(value):
    if value > 1:
        return value + 1
    return 0

def unchanged(value):
    return value * 2
"""


def _changed_repo(
    repo_factory: Callable[[dict[str, str]], Path], source: str = BASE
) -> tuple[Path, str, str]:
    repo = repo_factory({"logic.py": source})
    base = git(repo, "rev-parse", "HEAD")
    path = repo / "logic.py"
    path.write_text(source.replace("value > 1", "value >= 1").replace("value + 1", "value + 2"))
    git(repo, "add", "logic.py")
    git(repo, "commit", "-q", "-m", "change boundaries")
    return repo, base, git(repo, "rev-parse", "HEAD")


def test_mutants_are_deterministic_valid_and_restricted_to_changed_lines(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, base, head = _changed_repo(repo_factory)

    first = generate_mutants(repo, base, head, seed=17)
    second = generate_mutants(repo, base, head, seed=17)
    source = (repo / "logic.py").read_text()

    assert first == second
    assert first.mutants
    assert first.total_generated_candidates == first.total_valid_candidates
    assert first.syntax_rejected_candidates == 0
    assert first.total_valid_candidates == len(first.mutants)
    changed_lines = set(first.changed_regions["logic.py"])
    operators = {mutant.operator.split(":", 1)[0] for mutant in first.mutants}
    assert {
        "binary",
        "compare",
        "condition_negation",
        "boundary_constant",
        "return_value",
    } <= operators
    for mutant in first.mutants:
        assert mutant.path == "logic.py"
        assert mutant.start_line in changed_lines
        mutated = apply_mutant(source, mutant)
        compile(mutated, mutant.path, "exec")
        assert "unchanged(value)" in mutated


def test_seed_only_changes_stable_sampling_order_and_cap(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, base, head = _changed_repo(repo_factory)

    all_mutants = generate_mutants(repo, base, head, seed=17)
    capped = generate_mutants(repo, base, head, seed=99, max_mutants=2)

    assert len(capped.mutants) == 2
    assert capped.total_valid_candidates == all_mutants.total_valid_candidates
    assert capped.total_generated_candidates == all_mutants.total_generated_candidates
    assert capped.truncated_candidates == capped.total_valid_candidates - 2
    assert all(mutant.seed == 99 for mutant in capped.mutants)
    assert {mutant.mutant_id for mutant in capped.mutants} <= {
        mutant.mutant_id for mutant in all_mutants.mutants
    }


def test_operator_location_ignores_comment_tokens_and_handles_unicode_offsets(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    source = """\
def calculate(value):
    label = "é"
    return value + (  # + is mentioned here
        1
    )
"""
    repo = repo_factory({"logic.py": source})
    base = git(repo, "rev-parse", "HEAD")
    path = repo / "logic.py"
    path.write_text(source.replace("value + (", "value - ("))
    git(repo, "add", "logic.py")
    git(repo, "commit", "-q", "-m", "change operator")
    head = git(repo, "rev-parse", "HEAD")

    report = generate_mutants(repo, base, head)
    binary = next(mutant for mutant in report.mutants if mutant.operator.startswith("binary:"))
    mutated = apply_mutant(path.read_text(), binary)

    assert "value + (  # + is mentioned here" in mutated
    compile(mutated, "logic.py", "exec")


def test_materialization_rejects_stale_source(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo, base, head = _changed_repo(repo_factory)
    mutant = generate_mutants(repo, base, head).mutants[0]

    with pytest.raises(BlastRadiusError, match="source hash"):
        apply_mutant("def other():\n    return 1\n", mutant)


def test_multi_token_comparison_preserves_validity_with_noncanonical_spacing(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    repo = repo_factory({"logic.py": "def present(value):\n    return value is not None\n"})
    base = git(repo, "rev-parse", "HEAD")
    path = repo / "logic.py"
    path.write_text("def present(value):\n    return value is  not None\n")
    git(repo, "add", "logic.py")
    git(repo, "commit", "-q", "-m", "reformat comparison")

    report = generate_mutants(repo, base, "HEAD")
    comparison = next(
        mutant for mutant in report.mutants if mutant.operator == "compare:is not->is"
    )
    mutated = apply_mutant(path.read_text(), comparison)

    assert "value is None" in mutated
    compile(mutated, "logic.py", "exec")


def test_syntax_rejections_are_counted_before_sampling(
    repo_factory: Callable[[dict[str, str]], Path],
) -> None:
    source = (
        "def keys(value):\n"
        "    match value:\n"
        "        case {0: left, 2: right}:\n"
        "            return left\n"
    )
    repo = repo_factory({"logic.py": source})
    base = git(repo, "rev-parse", "HEAD")
    (repo / "logic.py").write_text(source.replace("2: right", "1: right"))
    git(repo, "add", "logic.py")
    git(repo, "commit", "-q", "-m", "change mapping key")

    report = generate_mutants(repo, base, "HEAD", max_mutants=1)

    # Both 0->1 and 1->0 introduce duplicate mapping-pattern keys: compile rejects them.
    assert report.total_generated_candidates == 2
    assert report.syntax_rejected_candidates == 2
    assert report.total_valid_candidates == 0
    assert report.truncated_candidates == 0
    assert report.mutants == []
