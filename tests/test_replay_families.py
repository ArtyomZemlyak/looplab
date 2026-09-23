"""The fold is `events/replay.py` PLUS the handler families split out of it (review 2026-09-22, EVT-12).

`replay.py` had grown to ~4,900 lines and 112 handlers whose families change for different reasons —
the node lifecycle, the Part IV/V concept membership, the operator controls, the Card board, the
trust/selection post-pass, the audit journals. A family moves VERBATIM into `events/replay_<family>.py`
and `replay.py` keeps `fold`, `FoldCursor` and the `_HANDLERS` table `fold` dispatches through. The
move itself can silently break three things, and each has a guard here:

* **Every guard that reads the fold's SOURCE must still read all of it.** Those guards find the fold
  by ONE naming rule (`tests/_source_scan.py::fold_source_paths`) instead of naming a file; this holds
  the rule to the runtime table, so a handler defined anywhere else is red HERE rather than being a
  module every source guard quietly skips.
* **The import direction.** A family module may import `core`, the `events` leaves and another family
  — never `replay`, which imports every family. The same rule `card_ledger` keeps, for the same
  reason: a module-level edge back is an ImportError at startup, a function-local one a hidden cycle.
* **A monkeypatch must still reach the code that reads the name.** A moved function stays importable
  as `looplab.events.replay._x` so existing imports keep working — but `monkeypatch.setattr(replay,
  "_x", …)` then rebinds only that re-export, and every handler in the family keeps calling its own
  binding: a patch that stops patching and says nothing. So every patch the suite aims at
  `looplab.events.replay` is held to a name `replay.py` itself DEFINES, or READS in its own code.
"""
from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

from looplab.events import replay

from _source_scan import PKG, fold_source_paths, fold_trees, iter_sources

TESTS = Path(__file__).resolve().parent
FOLD_MODULE_NAMES = ("looplab.events.replay", "looplab.replay")   # canonical + flat shim alias
PATCH_CALLS = frozenset({"setattr", "delattr", "setitem", "delitem", "patch", "object"})


# ------------------------------------------------------------------ the modules the guards read

def test_every_handler_is_defined_in_a_module_the_source_guards_read():
    """MUTATION: register a handler defined in, say, `events/digest.py` -> named here."""
    read = {path.resolve() for path in fold_source_paths()}
    stray = {etype: inspect.getsourcefile(handler)
             for etype, handler in replay._HANDLERS.items()
             if Path(inspect.getsourcefile(handler)).resolve() not in read}
    assert not stray, (
        f"handlers outside every fold module the source guards read: {stray}. Name the module "
        "`events/replay_<family>.py` so `fold_source_paths` finds it")


def test_every_family_table_is_merged_into_the_dispatch_table():
    """A family keeps the rows of the events it folds in its own `HANDLERS`; a table `replay.py`
    forgot to merge folds NOTHING — each of its events becomes an unknown type and silently no-ops.
    MUTATION: drop `_CONCEPT_HANDLERS` from `replay._HANDLER_TABLES` -> named here."""
    unmerged = {}
    for path in fold_source_paths()[1:]:
        table = getattr(importlib.import_module(f"looplab.events.{path.stem}"), "HANDLERS", {})
        lost = sorted(etype for etype, handler in table.items()
                      if replay._HANDLERS.get(etype) is not handler)
        if lost:
            unmerged[path.name] = lost
    assert not unmerged, f"family handler rows `fold` never dispatches to: {unmerged}"


def test_the_fold_modules_are_the_ones_on_disk_and_replay_py_leads():
    paths = fold_source_paths()
    assert paths[0] == PKG / "events" / "replay.py"
    assert {path for path, _tree in fold_trees()} == set(paths), "a listed fold module is missing"


# ------------------------------------------------------------------------ the import direction

def _imports_of_the_fold_module(tree: ast.AST) -> list[str]:
    """Every import in *tree*, at ANY depth, that binds `looplab.events.replay` or a name from it."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in FOLD_MODULE_NAMES:
                found.append(f"line {node.lineno}: from {module} import ...")
            elif module in ("looplab.events", "looplab"):
                found += [f"line {node.lineno}: from {module} import replay"
                          for alias in node.names if alias.name == "replay"]
        elif isinstance(node, ast.Import):
            found += [f"line {node.lineno}: import {alias.name}"
                      for alias in node.names if alias.name in FOLD_MODULE_NAMES]
    return found


def test_no_family_module_imports_the_fold_module():
    offenders = {path.name: hits for path, tree in fold_trees()
                 if path.name != "replay.py" and (hits := _imports_of_the_fold_module(tree))}
    assert not offenders, f"a fold family reaches back into replay.py: {offenders}"


def test_the_import_scan_sees_every_spelling_of_the_edge():
    """A scan that matches nothing is indistinguishable from one whose pattern is broken."""
    spellings = ["from looplab.events.replay import fold", "from looplab.events import replay",
                 "import looplab.events.replay as r", "from looplab import replay",
                 "def f():\n    from looplab.events.replay import _HANDLERS\n"]
    for text in spellings:
        assert _imports_of_the_fold_module(ast.parse(text)), text
    assert not _imports_of_the_fold_module(ast.parse(
        "from looplab.events.card_ledger import derive_cards\nfrom looplab.events import types"))


# ------------------------------------------------------------- a patch must reach what it patches

def _fold_module_aliases(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("looplab.events", "looplab"):
            names |= {alias.asname or alias.name for alias in node.names if alias.name == "replay"}
        elif isinstance(node, ast.Import):
            names |= {alias.asname for alias in node.names
                      if alias.name in FOLD_MODULE_NAMES and alias.asname}
    return names


def patched_fold_names(tree: ast.AST) -> list[tuple[str, int]]:
    """`(attribute of the fold module, line)` for every patch in *tree* aimed at it.

    The spellings the suite uses: `monkeypatch.setattr(replay, "x", …)`, the dotted-string form
    `monkeypatch.setattr("looplab.events.replay.x", …)` / `mock.patch("…")`, `patch.object(replay,
    "x")`, and `monkeypatch.setitem(replay.x, key, …)` — which patches the CONTENTS of `x`, so `x`
    is the name whose reader must see it.
    """
    aliases = _fold_module_aliases(tree)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in PATCH_CALLS and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            for module in FOLD_MODULE_NAMES:
                if first.value.startswith(module + "."):
                    found.append((first.value[len(module) + 1:].split(".")[0], node.lineno))
        elif isinstance(first, ast.Name) and first.id in aliases:
            if (len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                found.append((node.args[1].value, node.lineno))
        elif (isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name)
              and first.value.id in aliases):
            found.append((first.attr, node.lineno))
    return found


def names_a_patch_reaches(tree: ast.AST) -> set[str]:
    """The module attributes a patch on this module can reach: every function and class it DEFINES
    (a caller resolves those through the module at call time), and every name its own functions and
    classes READ. Not an import no code of the module reads — a re-export — and not an assignment
    alone: a module-level alias whose readers all moved away is patched and read by nobody."""
    reached: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            reached.add(node.name)
            reached |= {n.id for n in ast.walk(node)
                        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return reached


def test_every_patch_on_the_fold_module_reaches_the_code_that_reads_it():
    """MUTATION: move `_on_card_enriched` into a family module and leave `tests/
    test_card_enrichment_writers.py` patching `looplab.events.replay.CARD_ENRICHMENT_JOURNAL_MAX`
    -> named here, instead of a lowered cap the fold never sees."""
    owned = names_a_patch_reaches(
        next(tree for path, tree in fold_trees() if path.name == "replay.py"))
    stray = []
    for path, text in iter_sources(TESTS):
        if "replay" not in text or not any(call in text for call in PATCH_CALLS):
            continue            # every spelling of a patch on the fold module names the module AND
            #                     a patch call; the rest of the suite is skipped unparsed
        tree = ast.parse(text, filename=str(path))
        stray += [f"{path.relative_to(TESTS)}:{line} patches replay.{name}"
                  for name, line in patched_fold_names(tree) if name not in owned]
    assert not stray, (
        "these patches rebind a name `replay.py` neither defines nor reads, so the fold code that "
        "reads it — in the family module it moved to — never sees them. Patch the module that "
        f"reads the name:\n  " + "\n  ".join(stray))


def test_the_patch_scan_sees_every_spelling_the_suite_uses():
    """Driven on a synthetic module, so a pattern that stopped matching reads as a failure here and
    not as a clean suite."""
    tree = ast.parse(
        "from looplab.events import replay\n"
        "import looplab.events.replay as R\n"
        "def test_x(monkeypatch):\n"
        "    monkeypatch.setattr(replay, 'fold', None)\n"
        "    monkeypatch.setattr('looplab.events.replay.CARD_ENRICHMENT_JOURNAL_MAX', 0)\n"
        "    monkeypatch.setitem(R._HANDLERS, 'x', None)\n"
        "    mock.patch('looplab.replay._clear_approval')\n"
        "    mock.patch.object(R, '_select_best')\n"
        "    monkeypatch.setattr(other, 'fold', None)\n")
    assert [name for name, _line in patched_fold_names(tree)] == [
        "fold", "CARD_ENRICHMENT_JOURNAL_MAX", "_HANDLERS", "_clear_approval", "_select_best"]


def test_the_reach_rule_admits_what_a_module_defines_or_reads_and_nothing_it_only_imports():
    tree = ast.parse(
        "from looplab.core.models import coerce_node_id as _coerce, EXPORTED, CAP, make\n"
        "ALIAS = make({})\n"
        "def reader(d):\n"
        "    return _coerce(d) + CAP\n"
        "class Cursor:\n"
        "    pass\n")
    reached = names_a_patch_reaches(tree)
    assert {"reader", "Cursor", "_coerce", "CAP"} <= reached
    assert not {"EXPORTED", "ALIAS", "make"} & reached, (
        "an import nothing reads, and an assignment alone, are what a patch cannot reach")
    # …and on the real module, the seams the suite patches today are reachable ones.
    real = names_a_patch_reaches(
        next(tree for path, tree in fold_trees() if path.name == "replay.py"))
    assert {"fold", "_HANDLERS", "FoldCursor"} <= real
