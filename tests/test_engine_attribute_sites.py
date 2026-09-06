"""Every attribute the `Engine` family reads has exactly one DECLARING site (doc 52 row 21, doc 50 XP-08).

The registry and the whole argument are in `looplab/engine/attribute_sites.py`. What this file adds is
the CENSUS — an AST walk over every class in `Engine.__mro__` — and the four rules over it:

  1. every name read on `self` (bare, `getattr`, `hasattr`) is declared in `Engine.__init__`, at class
     level, or registered as lazily minted;
  2. every registered row names EXACTLY the methods that assign the name, and none of them is `__init__`
     (a declared attribute may not keep a row — the table is a shrink-only backlog);
  3. every attribute minted outside `__init__` and not declared there is registered — a NEW lazy
     attribute goes red until somebody declares it or owns the row;
  4. the set of declared attributes read through `getattr` with INCONSISTENT defaults is exactly the
     registered drift set.

The census is a function of class SOURCES so the last test can drive it on a synthetic family and
prove each rule bites — the guard ladder's tier 1 for a guard that is otherwise tier 3.
"""
from __future__ import annotations

import ast
import collections
import inspect
from dataclasses import dataclass, field
from pathlib import Path

from looplab.engine.attribute_sites import GETATTR_DEFAULT_DRIFT, LAZY_ENGINE_ATTRIBUTES
from looplab.engine.orchestrator import Engine


@dataclass
class Census:
    reads: dict = field(default_factory=lambda: collections.defaultdict(set))    # name -> sites
    writes: dict = field(default_factory=lambda: collections.defaultdict(set))   # name -> sites
    getattr_defaults: dict = field(default_factory=lambda: collections.defaultdict(set))
    init_declared: set = field(default_factory=set)
    class_declared: set = field(default_factory=set)
    methods: set = field(default_factory=set)

    @property
    def declared(self) -> set:
        return self.init_declared | self.class_declared

    @property
    def data_names(self) -> set:
        return (set(self.reads) | set(self.writes)) - self.methods - self.class_declared

    def lazy(self) -> dict:
        """name -> minting sites, for every attribute assigned somewhere other than `__init__`/class level."""
        return {n: set(s) for n, s in self.writes.items()
                if n not in self.declared and n not in self.methods}

    def undeclared_reads(self, registered) -> dict:
        return {n: sorted(s) for n, s in self.reads.items()
                if n not in self.declared and n not in self.methods and n not in registered}

    def default_drift(self) -> dict:
        return {n: tuple(sorted(d)) for n, d in self.getattr_defaults.items()
                if n in self.declared and len(d) > 1}


def _self_string_call(node: ast.AST, *names: str):
    """`getattr(self, "<lit>", …)` / `hasattr(self, "<lit>")` / `setattr(self, "<lit>", …)` -> the name."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in names
            and len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id == "self"
            and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
        return node.args[1].value
    return None


def census(sources: dict, *, init_owner: str) -> Census:
    """*sources*: class name -> (module file name, class source). *init_owner* is the class whose
    `__init__` is THE declaring site (`Engine`); every other `__init__` is an ordinary method."""
    c = Census()
    for cname, (fname, text) in sources.items():
        cls = ast.parse(text).body[0]
        assert isinstance(cls, ast.ClassDef), cname
        for stmt in cls.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                for t in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]):
                    if isinstance(t, ast.Name):
                        c.class_declared.add(t.id)
                continue
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            c.methods.add(stmt.name)
            site = f"{fname}::{stmt.name}"
            declaring = cname == init_owner and stmt.name == "__init__"
            for node in ast.walk(stmt):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                        and node.value.id == "self"):
                    if isinstance(node.ctx, ast.Store):
                        c.writes[node.attr].add(site)
                        if declaring:
                            c.init_declared.add(node.attr)
                    else:
                        c.reads[node.attr].add(site)
                    continue
                name = _self_string_call(node, "getattr", "hasattr")
                if name is not None:
                    c.reads[name].add(site)
                    if node.func.id == "getattr" and len(node.args) > 2:
                        c.getattr_defaults[name].add(ast.unparse(node.args[2]))
                    continue
                name = _self_string_call(node, "setattr")
                if name is not None:
                    c.writes[name].add(site)
                    if declaring:
                        c.init_declared.add(name)
    return c


def engine_sources() -> dict:
    """Every class in `Engine.__mro__` that lives in `looplab.engine` — the REAL family, not a hand list."""
    out = {}
    for cls in Engine.__mro__:
        if not cls.__module__.startswith("looplab.engine"):
            continue
        out[cls.__name__] = (Path(inspect.getsourcefile(cls)).name, inspect.getsource(cls))
    assert len(out) >= 20, sorted(out)
    return out


def _engine_census() -> Census:
    return census(engine_sources(), init_owner="Engine")


# ---------------------------------------------------------------------------- the four rules

def test_every_engine_attribute_read_has_a_declaring_site():
    c = _engine_census()
    missing = c.undeclared_reads(LAZY_ENGINE_ATTRIBUTES)
    assert not missing, (
        "attribute(s) read on the Engine that NO site declares — a typo'd name, or a setting that "
        f"never reached the engine (the `single_command_divergence_watch` shape): {missing}. Declare it "
        "in `Engine.__init__`, or register its minting site in engine/attribute_sites.py")


def test_every_registered_lazy_attribute_is_minted_exactly_where_the_row_says():
    c = _engine_census()
    stale = sorted(n for n in LAZY_ENGINE_ATTRIBUTES if n in c.declared)
    assert not stale, f"declared in __init__/class level AND registered as lazy — delete the row(s): {stale}"
    drift = {}
    for name, sites in LAZY_ENGINE_ATTRIBUTES.items():
        observed = c.writes.get(name, set())
        if observed != set(sites):
            drift[name] = {"registered": sorted(sites), "observed": sorted(observed)}
    assert not drift, f"registry rows that no longer name exactly the minting site(s): {drift}"


def test_every_lazily_minted_attribute_is_registered():
    c = _engine_census()
    unregistered = {n: sorted(s) for n, s in c.lazy().items() if n not in LAZY_ENGINE_ATTRIBUTES}
    assert not unregistered, (
        "attribute(s) minted outside `Engine.__init__` with no registry row — declare each in "
        f"`__init__` with its real default, or own a row in engine/attribute_sites.py: {unregistered}")


def test_inconsistent_getattr_defaults_are_exactly_the_registered_drift():
    c = _engine_census()
    assert c.default_drift() == GETATTR_DEFAULT_DRIFT, (
        "a DECLARED attribute is read through getattr with different defaults at different sites: "
        f"{c.default_drift()} — a new pair is a drift (read the declared attribute instead); a pair that "
        "no longer exists must delete its row")


# ---------------------------------------------------------------------------- the census has teeth

_FAMILY = {
    "E": ("e.py", "class E:\n"
                  "    def __init__(self):\n"
                  "        self._a = 1\n"
                  "        setattr(self, '_s', 2)\n"),
    "M": ("m.py", "class M:\n"
                  "    TABLE = ()\n"
                  "    def f(self):\n"
                  "        return (self._a, getattr(self, '_typo', False), hasattr(self, '_h'),\n"
                  "                getattr(self, '_a', None), getattr(self, '_a', []), self.TABLE, self.g())\n"
                  "    def g(self):\n"
                  "        self._lazy = 3\n"
                  "        setattr(self, '_lazy2', 4)\n"
                  "        return self._s\n"),
}


def test_the_census_sees_each_shape_the_rules_are_about():
    c = census(_FAMILY, init_owner="E")
    assert c.init_declared == {"_a", "_s"}, "bare AND string-form assignments in __init__ declare"
    assert c.class_declared == {"TABLE"} and "g" in c.methods
    assert c.undeclared_reads({}) == {"_h": ["m.py::f"], "_typo": ["m.py::f"]}, \
        "a getattr/hasattr of a name nothing assigns is the silent-typo shape"
    assert c.undeclared_reads({"_typo": ("m.py::g",)}) == {"_h": ["m.py::f"]}, "a registered name is not undeclared"
    assert c.lazy() == {"_lazy": {"m.py::g"}, "_lazy2": {"m.py::g"}}, "bare and setattr mints outside __init__"
    assert c.default_drift() == {"_a": ("None", "[]")}, "two defaults for one declared name is a drift"
    assert "TABLE" not in c.data_names and "g" not in c.data_names, "class names and methods are not data"
