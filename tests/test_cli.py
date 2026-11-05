from blastradius.cli import build_parser


def test_help_exposes_only_implemented_top_level_commands() -> None:
    help_text = build_parser().format_help()
    assert "analyze" in help_text
    assert "coverage" in help_text
    assert "trace" in help_text
    assert "mutate" not in help_text
