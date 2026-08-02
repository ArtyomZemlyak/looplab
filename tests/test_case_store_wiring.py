"""Which case store the engine actually uses (doc 25 EM-11).

Two classes claimed the same I19/ADR-10 role in their docstrings — `CaseLibrary` (vector-backed,
Memora-capable) and `JsonlCaseLibrary` (on-disk JSONL) — and only one of them is reachable from a
run. Resolving that used to require grepping for constructors. The docstrings say it now; this keeps
them honest, in both directions.
"""
from __future__ import annotations

import ast
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1] / "looplab"


def _constructor_sites(name: str) -> list[str]:
    """Every `name(...)` call under looplab/, excluding the class statement itself."""
    sites = []
    for path in sorted(_PKG.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file is a different test's problem
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == name):
                sites.append(f"{path.relative_to(_PKG.parent)}:{node.lineno}")
    return sites


def test_the_jsonl_store_is_the_one_a_run_reaches():
    """If this ever goes empty, the engine has no case store and cross-run recall is silently off."""
    assert _constructor_sites("JsonlCaseLibrary"), (
        "nothing constructs JsonlCaseLibrary — the engine's case path is disconnected")


def test_the_vector_store_is_still_unwired_or_its_docstring_is_now_wrong():
    """`CaseLibrary` is documented as UNWIRED and kept for the Memora path.

    Wiring it in is a fine thing to do — but it needs `JsonlCaseLibrary`'s durability contract
    (whole-file reload, quarantine-preserving rewrite, retain-on-improvement across runs), and the
    two docstrings have to stop pointing at each other. Failing here is the reminder.
    """
    sites = _constructor_sites("CaseLibrary")
    assert not sites, (
        "CaseLibrary is now constructed under looplab/ at "
        + ", ".join(sites)
        + " — update both class docstrings (memory.py) and give it the durability contract "
          "JsonlCaseLibrary has, or this is a case store that loses cases across runs")


def test_both_docstrings_name_the_other_so_neither_reads_as_the_live_one_alone():
    text = (_PKG / "engine" / "memory.py").read_text(encoding="utf-8")
    unwired = text.index("class CaseLibrary:")
    live = text.index("class JsonlCaseLibrary:")
    assert "UNWIRED" in text[unwired:unwired + 900]
    assert "JsonlCaseLibrary" in text[unwired:unwired + 900]
    assert "CaseLibrary` above" in text[live:live + 900]
