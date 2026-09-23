"""The live-log watchdog is `engine/train_monitor.py` PLUS its pure halves (review 2026-09-22, ENG3-13).

`train_monitor.py` had grown to ~3,300 lines holding five things with five reasons to change — the
verdict schema and prompts, the loss-trajectory measurement, the declared-contract reader, the log
plan and its attempt-bounded readers, the pure gates — around a ~750-line mixin loop, and every
importer outside it used only the pure halves (doc 50 EM-06). They moved VERBATIM into
`engine/loss_trajectory.py`, `engine/monitor_gates.py` and `engine/eval_log_plan.py`, and
`train_monitor` imports every moved name back, so each existing spelling resolves to the SAME object.
The move can silently break two things, and each has a guard here:

* **A monkeypatch must still reach the code that reads the name.** `monkeypatch.setattr(train_monitor,
  "x", …)` rebinds only `train_monitor`'s binding; a moved function keeps calling its OWN module's
  binding, so a patch aimed at the old home stops patching and says nothing. Every patch the suite
  aims at a watchdog module — in any spelling, a direct `module.x = …` assignment included — must
  target a name that module's own code DEFINES or READS, or one another module resolves THROUGH it
  at call time (a function-local `from <module> import x`, or `<module>.x` read off the module
  object). The same rule holds for the new homes, so a later move between siblings narrows nothing
  silently either.
* **The import direction.** The halves sit BELOW `train_monitor`, which imports all three; one of
  them importing it back is an ImportError at startup when module-level and a hidden cycle when
  deferred. Within the halves the order is `loss_trajectory` <- `monitor_gates` <- `eval_log_plan`.
  An annotation-only import under `if TYPE_CHECKING:` is not an edge and is allowed.

Both scanners are driven on synthetic sources below, so a pattern that stops matching reads as a
failure and not as a clean suite.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests._source_scan import PKG, iter_sources, iter_trees

TESTS = Path(__file__).resolve().parent
ENGINE = "looplab.engine"
FAMILY = ("train_monitor", "loss_trajectory", "monitor_gates", "eval_log_plan", "asha_monitor")
HALVES = ("loss_trajectory", "monitor_gates", "eval_log_plan")
PATCH_CALLS = frozenset({"setattr", "delattr", "setitem", "delitem", "patch", "object"})


def _spellings(stem: str) -> tuple[str, str]:
    """The two dotted names a watchdog module answers to: canonical, and the flat-import shim alias."""
    return (f"{ENGINE}.{stem}", f"looplab.{stem}")


def _aliases(tree: ast.AST, stem: str) -> set[str]:
    """Every local name bound to the module *stem*, at any depth (function-local imports too)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (ENGINE, "looplab"):
            names |= {alias.asname or alias.name for alias in node.names if alias.name == stem}
        elif isinstance(node, ast.Import):
            names |= {alias.asname for alias in node.names
                      if alias.name in _spellings(stem) and alias.asname}
    return names


def patched_names(tree: ast.AST, stem: str) -> list[tuple[str, int]]:
    """`(attribute of module *stem*, line)` for every patch in *tree* aimed at it.

    The spellings the suite uses: `monkeypatch.setattr(mod, "x", …)`, the dotted-string form
    `monkeypatch.setattr("looplab.engine.mod.x", …)` / `mock.patch("…")`, `patch.object(mod, "x")`,
    `monkeypatch.setitem(mod.x, key, …)` (which patches the CONTENTS of `x`, so `x` is the name whose
    reader must see it), the builtin `setattr(mod, "x", …)`, and the one W4-2's replay guard did not
    need: a DIRECT `mod.x = …` assignment, which `tests/test_train_monitor.py` uses twice.
    """
    aliases = _aliases(tree, stem)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            callee = (node.func.attr if isinstance(node.func, ast.Attribute)
                      else node.func.id if isinstance(node.func, ast.Name) else None)
            if callee not in PATCH_CALLS:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                for module in _spellings(stem):
                    if first.value.startswith(module + "."):
                        found.append((first.value[len(module) + 1:].split(".")[0], node.lineno))
            elif isinstance(first, ast.Name) and first.id in aliases:
                if (len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)):
                    found.append((node.args[1].value, node.lineno))
            elif (isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name)
                  and first.value.id in aliases):
                found.append((first.attr, node.lineno))
        elif isinstance(node, (ast.Assign, ast.AugAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                        and target.value.id in aliases):
                    found.append((target.attr, node.lineno))
    return found


def _own_reach(tree: ast.AST) -> set[str]:
    """What a patch on this module reaches IN IT: every top-level function and class it defines, and
    every name its own functions and classes read. Not a name it only imports (a re-export) and not
    an assignment alone: a constant whose readers all moved away is patched and read by nobody."""
    reached: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            reached.add(node.name)
            reached |= {n.id for n in ast.walk(node)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return reached


def _through_reach(tree: ast.AST, stem: str) -> set[str]:
    """What a patch on module *stem* reaches in ANOTHER module: a name it imports from *stem* inside
    a function body (the import runs at call time and reads the patched attribute), and any
    `<stem>.x` read off the module object. A module-level `from <stem> import x` binds at import
    time, so a later patch never reaches that module's uses of `x`."""
    reached: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module in _spellings(stem):
                reached |= {alias.name for alias in node.names}
    aliases = _aliases(tree, stem)
    reached |= {node.attr for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id in aliases and isinstance(node.ctx, ast.Load)}
    return reached


def reach_by_module(trees) -> dict[str, set[str]]:
    trees = list(trees)
    by_stem = {path.stem: tree for path, tree in trees
               if path.parent.name == "engine" and path.stem in FAMILY}
    reach = {stem: _own_reach(tree) for stem, tree in by_stem.items()}
    for path, tree in trees:
        for stem in FAMILY:
            if not (path.parent.name == "engine" and path.stem == stem):
                reach.setdefault(stem, set()).update(_through_reach(tree, stem))
    return reach


# ------------------------------------------------------------- a patch must reach what it patches

def test_every_patch_on_a_watchdog_module_reaches_the_code_that_reads_it():
    """MUTATION (driven while writing this): re-point `tests/test_watchdog_budget_stop.py`'s patch of
    `_MAX_MONITOR_LLM_CALLS` from `train_monitor` to `monitor_gates`, where the constant is now
    DEFINED — the per-node cap the loop reads never moves, and this names the site; so does moving
    `_log_query_tools` to `eval_log_plan` while `tests/test_engine_knob_defaults.py` still patches
    it on `train_monitor`."""
    reached = reach_by_module(iter_trees())
    assert set(FAMILY) <= set(reached), f"a watchdog module was not scanned: {sorted(reached)}"
    stray = []
    for path, text in iter_sources(TESTS):
        if path.name == Path(__file__).name or not any(stem in text for stem in FAMILY):
            continue
        tree = ast.parse(text, filename=str(path))
        for stem in FAMILY:
            stray += [f"{path.relative_to(TESTS)}:{line} patches {stem}.{name}"
                      for name, line in patched_names(tree, stem) if name not in reached[stem]]
    assert not stray, (
        "these patches rebind a name their module neither defines nor reads, and nothing resolves "
        "through it at call time — so the code that reads the name, in the module it moved to, "
        "never sees them. Patch the module that READS the name:\n  " + "\n  ".join(stray))


def test_the_patch_scan_sees_every_spelling_the_suite_uses():
    tree = ast.parse(
        "from looplab.engine import train_monitor as tm\n"
        "import looplab.engine.monitor_gates as G\n"
        "def test_x(monkeypatch):\n"
        "    from looplab.engine import eval_log_plan\n"
        "    monkeypatch.setattr(tm, 'a', None)\n"
        "    monkeypatch.setattr('looplab.engine.train_monitor.b', 0)\n"
        "    monkeypatch.setattr('looplab.train_monitor.c', 0)\n"
        "    monkeypatch.setitem(tm.d, 'k', None)\n"
        "    setattr(tm, 'e', 1)\n"
        "    tm.f = object()\n"
        "    G.g = 3\n"
        "    eval_log_plan.h = 4\n"
        "    mock.patch.object(G, 'i')\n")
    assert sorted(n for n, _ in patched_names(tree, "train_monitor")) == list("abcdef")
    assert sorted(n for n, _ in patched_names(tree, "monitor_gates")) == ["g", "i"]
    assert [n for n, _ in patched_names(tree, "eval_log_plan")] == ["h"]
    assert patched_names(ast.parse("tm.f = 1\nx.y = 2\n"), "train_monitor") == []   # no alias


def test_the_reach_counts_readers_in_the_module_and_through_it_and_nothing_else():
    home = ast.parse(
        "from looplab.engine.monitor_gates import _CAP, helper\n"
        "_UNREAD = 3\n"
        "def loop():\n"
        "    return _CAP + helper()\n")
    assert _own_reach(home) >= {"loop", "_CAP", "helper"}
    assert "_UNREAD" not in _own_reach(home)          # assigned, read by nobody
    other = ast.parse(
        "from looplab.engine.train_monitor import bound_at_import\n"
        "import looplab.engine.train_monitor as tm\n"
        "def later():\n"
        "    from looplab.engine.train_monitor import resolved_at_call\n"
        "    return tm.read_off_the_module\n")
    through = _through_reach(other, "train_monitor")
    assert {"resolved_at_call", "read_off_the_module"} <= through
    assert "bound_at_import" not in through           # a module-level import never sees a patch


# ------------------------------------------------------------------------ the import direction

# What each half may NOT import at runtime (module-level or function-local): everything above it.
FORBIDDEN_ABOVE = {
    "loss_trajectory": ("train_monitor", "monitor_gates", "eval_log_plan", "asha_monitor"),
    "monitor_gates": ("train_monitor", "eval_log_plan", "asha_monitor"),
    "eval_log_plan": ("train_monitor", "asha_monitor"),
}


def _runtime_imports_of(tree: ast.AST, stem: str) -> list[str]:
    """Every import in *tree* of module *stem* that runs at runtime — any depth, minus the bodies of
    `if TYPE_CHECKING:` blocks (an annotation, not an edge)."""
    typing_only: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            typing_only |= {id(n) for stmt in node.body for n in ast.walk(stmt)}
    hits = []
    for node in ast.walk(tree):
        if id(node) in typing_only:
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module in _spellings(stem):
                hits.append(f"line {node.lineno}: from {node.module} import ...")
            elif node.module in (ENGINE, "looplab"):
                hits += [f"line {node.lineno}: from {node.module} import {stem}"
                         for alias in node.names if alias.name == stem]
        elif isinstance(node, ast.Import):
            hits += [f"line {node.lineno}: import {alias.name}"
                     for alias in node.names if alias.name in _spellings(stem)]
    return hits


def test_no_half_imports_what_sits_above_it():
    trees = {path.stem: tree for path, tree in iter_trees()
             if path.parent.name == "engine" and path.stem in HALVES}
    assert set(trees) == set(HALVES), f"a half is missing on disk: {sorted(trees)}"
    offenders = {f"{half} -> {above}": hits
                 for half, forbidden in FORBIDDEN_ABOVE.items() for above in forbidden
                 if (hits := _runtime_imports_of(trees[half], above))}
    assert not offenders, f"a half of the watchdog reaches back up: {offenders}"


def test_the_import_scan_sees_every_runtime_spelling_and_skips_typing_only():
    for text in ("from looplab.engine.train_monitor import x",
                 "from looplab.engine import train_monitor",
                 "import looplab.engine.train_monitor as t",
                 "from looplab import train_monitor",
                 "def f():\n    from looplab.engine.train_monitor import TrainingVerdict\n"):
        assert _runtime_imports_of(ast.parse(text), "train_monitor"), text
    assert not _runtime_imports_of(ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from looplab.engine.train_monitor import TrainingVerdict\n"
        "from looplab.engine.loss_trajectory import LossTrajectory\n"), "train_monitor")


def test_the_halves_hold_no_engine_and_import_no_model():
    """What the three module docstrings promise, held: no function takes an engine (the package's
    `engine`/`eng` handle convention — the tool builders that do are `train_monitor`'s), and nothing
    imports a model client, a judge or an agent at any depth."""
    model_stacks = ("looplab.core.llm", "looplab.trust", "looplab.agents")
    problems = []
    for path, tree in iter_trees(PKG / "engine"):
        if path.stem not in HALVES:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = {a.arg for a in node.args.posonlyargs + node.args.args
                          + node.args.kwonlyargs}
                if params & {"engine", "eng", "self"} and not _is_method_of_a_dataclass(tree, node):
                    problems.append(f"{path.stem}.{node.name} takes an engine")
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(model_stacks):
                problems.append(f"{path.stem}:{node.lineno} imports {node.module}")
    assert not problems, problems


def _is_method_of_a_dataclass(tree: ast.AST, fn: ast.AST) -> bool:
    """`LossTrajectoryTracker`'s and `LossTrajectory`'s own methods take `self` — of a VALUE, not of
    an engine; a `self` counts only on a function that is not a method of a class in the module."""
    return any(isinstance(cls, ast.ClassDef) and fn in cls.body for cls in ast.walk(tree))
