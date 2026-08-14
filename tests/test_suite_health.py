"""The suite's own integrity: defects that make a TEST stop running without going red.

A test that no longer runs is worse than a deleted one — the suite still reports it in the pass
count, and the property it guarded is unguarded. Everything here is a scan over the test tree
itself, because none of these show up as a failure anywhere.
"""
from __future__ import annotations

import ast
import collections
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent


def _test_modules():
    for path in sorted(TESTS.glob("test_*.py")):
        # `utf-8-sig`: at least one tracked file carries a BOM, and a plain utf-8 read makes
        # `ast.parse` reject it. See `tests/_source_scan.py`, which documents the same accommodation.
        yield path, ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def test_no_test_function_is_shadowed_by_a_later_definition_of_its_name():
    """Two `def test_x` in one module is not a duplicate — the FIRST one silently stops existing.

    Python binds the later definition, pytest collects one function, and the count goes DOWN by one
    in a suite of ~9,700 where nobody would notice. This is a merge artifact: two branches each add
    a test for the same property, both land, and the weaker body wins by source position.

    It has already cost real coverage. `test_sandbox_gate.py` carried two
    `test_kill_tree_never_signals_an_already_reaped_process` definitions; the shadowed one was the
    only check that `_kill_tree` still GROUP-KILLS a live process, and removing the production
    fence left the surviving namesake green.
    """
    shadowed = []
    for path, tree in _test_modules():
        seen = collections.defaultdict(list)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    node.name.startswith("test"):
                seen[node.name].append(node.lineno)
        for name, lines in sorted(seen.items()):
            if len(lines) > 1:
                shadowed.append(f"{path.name}::{name} defined at {lines} — only the last runs")
    assert shadowed == [], "\n".join(shadowed)


def test_no_test_module_shares_a_basename_with_another():
    """Two `test_x.py` under different directories collide in `sys.modules` without `__init__.py`.

    pytest imports test modules by BASENAME in rootdir-relative mode, so the second one raises an
    import-file-mismatch error — or, worse, on some layouts silently resolves to the first. The
    suite is flat today; this keeps a subdirectory from quietly re-introducing the problem.
    """
    names = collections.defaultdict(list)
    for path in sorted(TESTS.rglob("test_*.py")):
        names[path.name].append(str(path.relative_to(TESTS)))
    collisions = {name: paths for name, paths in names.items() if len(paths) > 1}
    assert collisions == {}, f"colliding test module basenames: {collisions}"
