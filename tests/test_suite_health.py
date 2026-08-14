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


REPO = TESTS.parent


def test_the_docs_index_lists_each_document_once_under_its_own_number():
    """A merge can RESURRECT a row somebody deleted, and nothing downstream notices.

    Both halves of this have already happened. Two branches gave `docs/` two different documents
    numbered 35, so `doc 35 §4b` in `core/config.py` and `(mega-review, doc 35)` in
    `docs/guide/concepts.md` pointed at different files. And when the duplicate rows for 31/32/33
    were merged into one apiece, a later `merge master into F1` brought the deleted copies straight
    back — within fourteen commits. mkdocs is happy either way: the links resolve, so `--strict`
    says nothing.

    Three properties, all cheap: one row per document, one document per number, and a document's own
    H1 number agreeing with its filename (the mega-review's H1 still said 35 after it moved to 40).
    """
    import re

    docs = REPO / "docs"
    index = (docs / "00-INDEX.md").read_text(encoding="utf-8")

    by_file, by_number = collections.defaultdict(list), collections.defaultdict(set)
    for line in index.splitlines():
        row = re.match(r"\|\s*(\d+)\s*\|\s*\*\*\[([^\]]+)\]", line)
        if row:
            by_file[row.group(2)].append(row.group(1))
            by_number[row.group(1)].add(row.group(2))

    assert {name: rows for name, rows in by_file.items() if len(rows) > 1} == {}, \
        "a document is listed more than once — a merge most likely resurrected a deleted row"
    assert {num: sorted(names) for num, names in by_number.items() if len(names) > 1} == {}, \
        "two documents share a number — every `doc N` reference in the tree is now ambiguous"

    numbered = {p.name: re.match(r"^(\d+)-", p.name).group(1)
                for p in docs.glob("*.md")
                if re.match(r"^\d+-", p.name) and p.name != "00-INDEX.md"}
    assert sorted(n for n in numbered if n not in index) == [], "a numbered document is not indexed"

    mismatched = []
    for name, number in sorted(numbered.items()):
        heading = re.match(r"^#\s*(\d+)\b", (docs / name).read_text(encoding="utf-8").splitlines()[0])
        if heading and heading.group(1) != number.lstrip("0"):
            mismatched.append(f"{name} opens with '# {heading.group(1)}'")
    assert mismatched == [], mismatched
