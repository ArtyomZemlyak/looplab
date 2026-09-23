"""A knob's `getattr` DEFAULT is the value a real `Engine(...)` settles it to (review 2026-09-22, ENG1-03).

THE SHAPE. Every `EngineOptions` field lands on one Engine attribute
(`tests/test_engine_options.py::ATTR_BY_FIELD`). A reader that cannot be sure the attribute exists — a
module function handed an `engine`, a mixin method a test drives on `Engine.__new__(Engine)` — spells
the read `getattr(engine, "<knob>", <default>)`, and that default is a SECOND declaration of the knob's
value with nothing tying it to the first. The engine default moved — a feature shipped off and was
flipped on, a paid steward was flipped off — and the copies stayed where they were. Measured on
2026-09-23 over the whole package: 110 such reads on an engine handle, 33 whose default was not what a
bare `Engine(...)` settles the knob to (34 with the one whose handle was spelled `owner`), 27 of them
in behaviour rather than in a `None`/`''`/`{}` spelling of "unset":

  * `finalize.py::finalize_run` answered `_task_facets_finalize` with True against a settled False —
    the PAID facet steward, for any engine lacking the attribute;
  * the five log/code-tool gates in `train_monitor.py` and `failure_diagnosis.py::diagnosis_code_tools`
    answered False against a settled True;
  * the deadline-grace judge and its prompt clause answered 0.0 — OFF — against the AUTO -1.0 the
    feature ships as, and the live-log monitor answered None, which `resolve_deadline_grace` also
    reads as OFF;
  * `_memo_verdict_cue`, `_gpu_footprint_cue`, `concept_retag_every`, `eval_trust_mode`,
    `_novelty_mode`, `_policy_name`, `n_seeds`, `_holdout_fraction`, `_auto_install_deps`: each a
    pre-feature or pre-flip value no real Engine has held since.

WHO IT BIT. No real engine: each of these attributes is assigned in `Engine.__init__`, and a probe that
wrapped `getattr` over two full toy runs (bare, and product `EngineOptions`) recorded ZERO reads of a
knob default. The readers of a default are the DOUBLES — `Engine.__new__(Engine)` stubs, mixin
subclasses and duck-typed engines handed to module functions — and the same probe over the 2,690 tests
that drive them found 88 reaching a drifted default: each one a test exercising, and asserting on, a
knob set no real Engine runs (five of them scheduled the facet steward a real Engine skips).

THE RULE, total over the package: every such default equals the real engine's settled value, or the
site is an `engine/attribute_sites.py::UNSETTLED_KNOB_DEFAULTS` row — a reader that turns a MISSING
knob into "not knowable" on purpose (say nothing, fail closed), each row stating why. The set of
disagreeing sites is pinned EXACTLY, so a new copy that drifts is red and a fixed row must leave.

The census is a function of SOURCES so a test drives it on a synthetic family and proves each engine
handle spelling is seen — the guard ladder's tier 1 for what is otherwise a tier-3 AST scan. The
half that is not a scan compares against a REAL engine's values, never a hand-kept table; and the
module-level readers, which take any object as `engine`, are DRIVEN with a real engine and two
attribute-less doubles that must answer alike.
"""
from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from looplab.engine import cross_run_context, failure_diagnosis, train_monitor
from looplab.engine.attribute_sites import UNSETTLED_KNOB_DEFAULTS
from looplab.engine.orchestrator import Engine
from tests._source_scan import PKG, iter_trees
from tests.factories import make_engine
from tests.test_engine_options import ATTR_BY_FIELD

# A module function's engine parameter is spelled one of these — the package's convention, and the
# census teeth below prove a differently-named handle is invisible to it, which is why it matters.
HANDLE_NAMES = frozenset({"engine", "eng"})


@dataclass(frozen=True)
class Site:
    path: str          # under `looplab/`, e.g. "engine/finalize.py"
    function: str
    name: str          # the knob attribute read
    default: ast.AST
    line: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.function}::{self.name}"


def engine_family() -> frozenset:
    """Every class of the real `Engine`, from its MRO — not a hand list."""
    return frozenset(c.__name__ for c in Engine.__mro__ if c.__module__.startswith("looplab.engine"))


def holder_classes(trees) -> frozenset:
    """Classes that reach the engine as `self._e` — `LessonMemory`, `HoldoutGrader`,
    `WorkspaceSeeder` and the mixins they are made of — read off the source, so a mixin split out
    of a holder is covered without anybody listing it."""
    names = set()
    for _path, tree in trees:
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            if any(isinstance(n, ast.Attribute) and n.attr == "_e" and isinstance(n.value, ast.Name)
                   and n.value.id == "self" for n in ast.walk(cls)):
                names.add(cls.name)
    return frozenset(names)


class _Census(ast.NodeVisitor):
    """One module's `getattr(<engine handle>, "<knob>", <default>)` reads.

    An engine handle is `self` in a class of the engine family; `self._e`, or a local bound to it,
    in a holder class; or a name in `HANDLE_NAMES`."""

    def __init__(self, path: str, *, family, holders, knobs, out: list):
        self.path, self.family, self.holders, self.knobs, self.out = path, family, holders, knobs, out
        self.cls: list = [None]
        self.fn: list = [None]
        self.aliases: list = [frozenset()]

    def visit_ClassDef(self, node):
        self.cls.append(node.name)
        self.generic_visit(node)
        self.cls.pop()

    def visit_FunctionDef(self, node):
        aliases = set(self.aliases[-1])
        if self.cls[-1] in self.holders:
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name) and self._is_self_e(sub.value)):
                    aliases.add(sub.targets[0].id)
        self.fn.append(node.name)
        self.aliases.append(frozenset(aliases))
        self.generic_visit(node)
        self.fn.pop()
        self.aliases.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _is_self_e(self, expr) -> bool:
        return (isinstance(expr, ast.Attribute) and expr.attr == "_e"
                and isinstance(expr.value, ast.Name) and expr.value.id == "self")

    def _is_handle(self, expr) -> bool:
        if isinstance(expr, ast.Name):
            if expr.id == "self":
                return self.cls[-1] in self.family
            return expr.id in HANDLE_NAMES or expr.id in self.aliases[-1]
        return self.cls[-1] in self.holders and self._is_self_e(expr)

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) == 3
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in self.knobs
                and self._is_handle(node.args[0])):
            self.out.append(Site(self.path, self.fn[-1] or "<module>", node.args[1].value,
                                 node.args[2], node.lineno))
        self.generic_visit(node)


def knob_default_sites(trees, *, family, holders, knobs, root: Path = PKG) -> list:
    """Every knob read with a default on an engine handle, over *trees* ((path, tree) pairs)."""
    out: list = []
    for path, tree in trees:
        rel = Path(path).relative_to(root).as_posix() if Path(path).is_absolute() else str(path)
        _Census(rel, family=family, holders=holders, knobs=knobs, out=out).visit(tree)
    return out


def default_value(site: Site):
    """The default's VALUE: a literal, or a module-level constant of the reading module."""
    try:
        return ast.literal_eval(site.default)
    except ValueError:
        pass
    if isinstance(site.default, ast.Name):
        module = importlib.import_module("looplab." + site.path[:-3].replace("/", "."))
        return getattr(module, site.default.id)
    raise AssertionError(f"{site.key} (line {site.line}): the default "
                         f"`{ast.unparse(site.default)}` is neither a literal nor a module constant, "
                         "so no census can say what a double reads there — spell it as one")


def agrees(value, settled) -> bool:
    """Equal AND the same kind: `0` is not `False`, `''` is not `None`, `{}` is not `None`."""
    if isinstance(value, bool) or isinstance(settled, bool):
        return type(value) is type(settled) and value == settled
    if isinstance(value, (int, float)) and isinstance(settled, (int, float)):
        return value == settled
    return type(value) is type(settled) and value == settled


def knob_attributes() -> frozenset:
    return frozenset(ATTR_BY_FIELD.values())


# ---------------------------------------------------------------------------- the rule

def test_every_knob_default_is_what_a_real_engine_settles_the_knob_to(tmp_path):
    real = make_engine(tmp_path / "real")
    trees = list(iter_trees())
    sites = knob_default_sites(trees, family=engine_family(), holders=holder_classes(trees),
                               knobs=knob_attributes())
    # The census is looking at the package, not at nothing: 106 reads when it landed (110 before the
    # fixes it drove — six became bare reads and `advisory_enabled`'s two came into view).
    assert len(sites) >= 90, len(sites)
    disagreeing: dict = {}
    for site in sites:
        settled = getattr(real, site.name)
        value = default_value(site)
        if not agrees(value, settled):
            disagreeing.setdefault(site.key, []).append(
                f"line {site.line}: default {value!r} but a real Engine settles {settled!r}")
    unregistered = {k: v for k, v in sorted(disagreeing.items()) if k not in UNSETTLED_KNOB_DEFAULTS}
    assert not unregistered, (
        "a knob read with a default that is NOT the value a real Engine settles it to — every double "
        "that lacks the attribute runs a knob set no real Engine has. Spell the settled value (or read "
        "the attribute bare where it is always assigned); only a reader that turns a MISSING knob "
        "into 'not knowable' on purpose may register in engine/attribute_sites.py::"
        f"UNSETTLED_KNOB_DEFAULTS, with its reason: {unregistered}")
    stale = sorted(set(UNSETTLED_KNOB_DEFAULTS) - set(disagreeing))
    assert not stale, f"UNSETTLED_KNOB_DEFAULTS rows whose site no longer disagrees — delete them: {stale}"
    for key, why in UNSETTLED_KNOB_DEFAULTS.items():
        assert len(why.split()) >= 8, f"{key}: a registered unknown-default states WHY: {why!r}"


# ---------------------------------------------------------------------------- driven, not scanned

def _doubles():
    """Two engines that never ran `Engine.__init__`: a duck-typed one and a bare `__new__` stub."""
    return (("SimpleNamespace()", SimpleNamespace()), ("Engine.__new__(Engine)", Engine.__new__(Engine)))


@pytest.mark.parametrize("reader", ["monitor_log_tools", "repair_log_tools", "stage_check_tools"])
def test_the_log_tool_gates_answer_a_double_as_they_answer_a_real_engine(tmp_path, monkeypatch, reader):
    """The GATE is what is under test, so the provider construction is replaced by a marker: a gate
    that opens returns it, one that stays shut returns None."""
    marker = object()
    monkeypatch.setattr(train_monitor, "_log_query_tools", lambda *a, **k: marker)
    fn = getattr(train_monitor, reader)
    real = fn(make_engine(tmp_path / "real"), tmp_path)
    assert real is marker, f"{reader}: the gate is ON in a bare Engine — this test is about that value"
    for label, double in _doubles():
        assert fn(double, tmp_path) is real, f"{reader} answered {label} differently from a real engine"


@pytest.mark.parametrize("reader", [train_monitor.monitor_code_tools, failure_diagnosis.diagnosis_code_tools])
def test_the_code_scout_gates_answer_a_double_as_they_answer_a_real_engine(tmp_path, reader):
    real = reader(make_engine(tmp_path / "real"), tmp_path)
    assert real is not None, f"{reader.__name__}: ON in a bare Engine — this test is about that value"
    for label, double in _doubles():
        got = reader(double, tmp_path)
        assert type(got) is type(real), f"{reader.__name__} answered {label} with {got!r}"


def test_the_snapshot_rule_answers_a_double_as_it_answers_a_real_engine(tmp_path):
    spec = {"cmd": "python train.py"}
    real = train_monitor.needs_log_snapshot(make_engine(tmp_path / "real"), spec)
    assert real is True, "a bare Engine keeps the repair judge's snapshot — this test is about that"
    for label, double in _doubles():
        assert train_monitor.needs_log_snapshot(double, spec) is real, label


def test_the_cross_run_advisory_gate_answers_a_double_as_it_answers_a_real_engine(tmp_path):
    real = cross_run_context.advisory_enabled(make_engine(tmp_path / "real"))
    for label, double in _doubles():
        assert cross_run_context.advisory_enabled(double) is real, label


# ---------------------------------------------------------------------------- the census has teeth

_FAMILY_SOURCES = {
    "fam.py": ("class Fam:\n"
               "    def m(self):\n"
               "        return getattr(self, '_k', False), getattr(self, 'other', 1)\n"
               "    def n(self):\n"
               "        def inner():\n"
               "            return getattr(self, '_k', True)\n"
               "        return inner\n"),
    # `self` of a class OUTSIDE the family is not the engine: a researcher's own `_k` is its own.
    "stranger.py": ("class Stranger:\n"
                    "    def m(self):\n"
                    "        return getattr(self, '_k', False)\n"),
    "holder.py": ("class Holder:\n"
                  "    def __init__(self, engine):\n"
                  "        self._e = engine\n"
                  "    def m(self):\n"
                  "        e = self._e\n"
                  "        return getattr(e, '_k', 0), getattr(self._e, '_k', 1)\n"),
    # A module function sees `engine`/`eng`; a handle spelled otherwise is INVISIBLE, which is why
    # `cross_run_context.advisory_enabled` spells its parameter `engine`.
    "funcs.py": ("def f(engine, eng, owner):\n"
                 "    return getattr(engine, '_k', 2), getattr(eng, '_k', 3), getattr(owner, '_k', 4)\n"),
}


def test_the_census_sees_each_engine_handle_spelling():
    trees = [(name, ast.parse(src)) for name, src in _FAMILY_SOURCES.items()]
    sites = knob_default_sites(trees, family={"Fam"}, holders=holder_classes(trees), knobs={"_k"})
    seen = sorted((s.key, ast.unparse(s.default)) for s in sites)
    assert seen == [
        ("fam.py::inner::_k", "True"),       # a closure's `self` is still the engine
        ("fam.py::m::_k", "False"),
        ("funcs.py::f::_k", "2"),
        ("funcs.py::f::_k", "3"),
        ("holder.py::m::_k", "0"),          # a local bound to `self._e`
        ("holder.py::m::_k", "1"),          # `self._e` itself
    ], seen
    assert holder_classes(trees) == {"Holder"}
    assert agrees(False, False) and not agrees(0, False) and not agrees("", None)
    assert agrees(-1, -1.0) and not agrees(None, {}) and agrees({}, {})
