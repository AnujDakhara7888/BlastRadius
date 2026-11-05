from blastradius.models import ChangeKind
from blastradius.static.analyzer import build_index
from blastradius.static.changes import map_changes
from blastradius.vcs.git import FileDiff


def test_maps_hunk_to_innermost_symbol_and_decorator() -> None:
    base_source = """\
def deco(fn):
    return fn

@deco
def outer():
    def inner():
        return 1
    return inner()
"""
    head_source = base_source.replace("return 1", "return 2")
    base = build_index("base", {"pkg/mod.py": base_source})
    head = build_index("head", {"pkg/mod.py": head_source})
    changes = map_changes([FileDiff("pkg/mod.py", "pkg/mod.py", (7,), (7,), "M")], base, head)
    assert len(changes) == 1
    assert changes[0].head is not None
    assert changes[0].head.id.qualified_name == "outer.inner"


def test_blank_line_outside_function_maps_to_module() -> None:
    source = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
    base = build_index("base", {"mod.py": source})
    head = build_index("head", {"mod.py": source})
    changes = map_changes([FileDiff("mod.py", "mod.py", (4,), (4,), "M")], base, head)
    assert changes[0].head is not None
    assert changes[0].head.id.qualified_name == "<module>"


def test_function_rename_uses_unique_name_independent_structure() -> None:
    base_source = "def old_name(value):\n    return value + 1\n"
    head_source = "def new_name(value):\n    return value + 1\n"
    base = build_index("base", {"mod.py": base_source})
    head = build_index("head", {"mod.py": head_source})
    changes = map_changes([FileDiff("mod.py", "mod.py", (1,), (1,), "M")], base, head)
    assert len(changes) == 1
    assert changes[0].change == ChangeKind.RENAMED
    assert changes[0].base is not None and changes[0].head is not None
    assert changes[0].base.id.qualified_name == "old_name"
    assert changes[0].head.id.qualified_name == "new_name"
    assert changes[0].match_confidence == 0.85


def test_file_move_without_hunks_retains_all_symbol_correspondence() -> None:
    source = "def helper():\n    return 1\n"
    base = build_index("base", {"old.py": source})
    head = build_index("head", {"new.py": source})
    changes = map_changes([FileDiff("old.py", "new.py", (), (), "R")], base, head)
    helper = next(
        change for change in changes if change.head and change.head.id.qualified_name == "helper"
    )
    assert helper.change == ChangeKind.RENAMED
    assert helper.base is not None and helper.base.id.path == "old.py"
    assert helper.match_confidence == 0.99


def test_ambiguous_structural_matches_are_not_asserted() -> None:
    base = build_index("base", {"mod.py": "def old():\n    return 1\n"})
    head = build_index(
        "head",
        {"mod.py": "def first():\n    return 1\n\ndef second():\n    return 1\n"},
    )
    changes = map_changes(
        [FileDiff("mod.py", "mod.py", (1, 2), (1, 2, 4, 5), "M")],
        base,
        head,
    )
    deleted = next(change for change in changes if change.change == ChangeKind.DELETED)
    assert deleted.match_confidence == 0.0
    assert deleted.ambiguity == "multiple structural correspondence candidates"
    assert sum(change.change == ChangeKind.ADDED for change in changes) == 2
