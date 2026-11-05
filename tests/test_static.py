from blastradius.static.analyzer import build_index, symbols_for_source


def test_decorators_and_nested_same_name_are_distinct() -> None:
    source = """\
def deco(fn):
    return fn

@deco
def outer():
    def repeat():
        return 1
    return repeat()

def repeat():
    return 2
"""
    symbols, _ = symbols_for_source("abc", "pkg/mod.py", source)
    by_name = {symbol.id.qualified_name: symbol for symbol in symbols}
    assert by_name["outer"].start_line == 4
    assert by_name["outer.repeat"].id.key != by_name["repeat"].id.key


def test_edges_are_caller_to_callee_and_cycles_are_preserved() -> None:
    source = """\
def changed():
    return helper()

def helper():
    return changed()

def entry():
    return changed()
"""
    index = build_index("abc", {"pkg/mod.py": source})
    pairs = {(edge.caller, edge.callee) for edge in index.edges}
    assert ("pkg/mod.py:entry:function:0", "pkg/mod.py:changed:function:0") in pairs
    assert ("pkg/mod.py:changed:function:0", "pkg/mod.py:helper:function:0") in pairs
    assert ("pkg/mod.py:helper:function:0", "pkg/mod.py:changed:function:0") in pairs
