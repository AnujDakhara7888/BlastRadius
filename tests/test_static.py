from blastradius.models import EdgeKind
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


def test_import_alias_and_module_attribute_calls_resolve_exact_targets() -> None:
    sources = {
        "src/pkg/core.py": "def price():\n    return 1\n",
        "src/pkg/alias_user.py": (
            "from pkg.core import price as compute\n\ndef checkout():\n    return compute()\n"
        ),
        "src/pkg/module_user.py": (
            "import pkg.core as core\n\ndef refund():\n    return core.price()\n"
        ),
    }
    index = build_index("abc", sources)
    calls = {(edge.caller, edge.callee, edge.provenance) for edge in index.edges}
    assert (
        "src/pkg/alias_user.py:checkout:function:0",
        "src/pkg/core.py:price:function:0",
        "static_import_alias",
    ) in calls
    assert (
        "src/pkg/module_user.py:refund:function:0",
        "src/pkg/core.py:price:function:0",
        "static_import_attribute",
    ) in calls


def test_relative_imports_and_submodule_bindings_resolve() -> None:
    sources = {
        "src/pkg/__init__.py": "",
        "src/pkg/core.py": "def price():\n    return 1\n",
        "src/pkg/direct.py": ("from .core import price\n\ndef direct():\n    return price()\n"),
        "src/pkg/through_module.py": (
            "from . import core\n\ndef indirect():\n    return core.price()\n"
        ),
    }
    index = build_index("abc", sources)
    pairs = {(edge.caller, edge.callee) for edge in index.edges}
    target = "src/pkg/core.py:price:function:0"
    assert ("src/pkg/direct.py:direct:function:0", target) in pairs
    assert ("src/pkg/through_module.py:indirect:function:0", target) in pairs


def test_self_decorator_and_inheritance_edges_have_distinct_kinds() -> None:
    sources = {
        "src/pkg/base.py": "class Base:\n    pass\n",
        "src/pkg/model.py": (
            "from pkg.base import Base\n\n"
            "def register(value):\n"
            "    return value\n\n"
            "@register\n"
            "class Model(Base):\n"
            "    def save(self):\n"
            "        return self.validate()\n\n"
            "    def validate(self):\n"
            "        return True\n"
        ),
    }
    index = build_index("abc", sources)
    edges = {(edge.caller, edge.callee, edge.kind) for edge in index.edges}
    model = "src/pkg/model.py:Model:class:0"
    assert (
        model,
        "src/pkg/model.py:register:function:0",
        EdgeKind.DECORATOR,
    ) in edges
    assert (
        model,
        "src/pkg/base.py:Base:class:0",
        EdgeKind.INHERITANCE,
    ) in edges
    assert (
        "src/pkg/model.py:Model.save:function:0",
        "src/pkg/model.py:Model.validate:function:0",
        EdgeKind.CALL,
    ) in edges


def test_typing_only_and_dynamic_relationships_are_explicit_uncertainty() -> None:
    sources = {
        "src/pkg/types.py": "class Payload:\n    pass\n",
        "src/pkg/use.py": (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg.types import Payload\n\n"
            "def use(obj):\n"
            "    return obj.dynamic()\n"
        ),
        "src/pkg/star.py": "from pkg.types import *\n",
    }
    index = build_index("abc", sources)
    assert any("typing_only_relationship" in site for site in index.uncertainty_sites)
    assert any("dynamic_dispatch:obj.dynamic" in site for site in index.uncertainty_sites)
    assert any("star_import:pkg.types" in site for site in index.uncertainty_sites)
    typing_edges = [edge for edge in index.edges if edge.provenance == "typing_only_import"]
    assert typing_edges
    assert all(edge.confidence == 0.15 for edge in typing_edges)


def test_local_parameter_shadowing_does_not_create_false_import_edge() -> None:
    sources = {
        "src/pkg/core.py": "def compute():\n    return 1\n",
        "src/pkg/use.py": (
            "from pkg.core import compute\n\ndef use(compute):\n    return compute()\n"
        ),
    }
    index = build_index("abc", sources)
    false_edge = (
        "src/pkg/use.py:use:function:0",
        "src/pkg/core.py:compute:function:0",
    )
    assert false_edge not in {(edge.caller, edge.callee) for edge in index.edges}
    assert any("unresolved_name:compute" in site for site in index.uncertainty_sites)
