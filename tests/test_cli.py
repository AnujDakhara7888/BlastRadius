from blastradius.cli import build_parser


def test_help_exposes_only_implemented_top_level_commands() -> None:
    help_text = build_parser().format_help()
    assert "analyze" in help_text
    assert "coverage" in help_text
    assert "trace" in help_text
    assert "mutate" not in help_text


def test_analyze_help_exposes_cache_control() -> None:
    parser = build_parser()
    analyze_parser = next(
        action.choices["analyze"]
        for action in parser._actions
        if hasattr(action, "choices") and action.choices
    )
    help_text = analyze_parser.format_help()
    assert "--cache-file" in help_text
    assert "--no-cache" in help_text
