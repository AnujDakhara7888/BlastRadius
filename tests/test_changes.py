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
