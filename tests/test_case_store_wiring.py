"""Which case store the engine actually uses (doc 25 EM-11), and what reads a case back.

Two classes claimed the same I19/ADR-10 role in their docstrings — `CaseLibrary` (vector-backed,
Memora-capable) and `JsonlCaseLibrary` (on-disk JSONL) — and only one of them is reachable from a
run. Resolving that used to require grepping for constructors. The docstrings say it now; this keeps
them honest, in both directions.

The second half of the file pins the READ side, which is what the operator-facing labels claim.
`docs/guide/memory.md` and the Memory panel now both tell an operator that a case is NOT injected
into any prompt — it reaches a model only if a tool-using role calls `kb_search` — while a meta-note
for the same finished run IS injected verbatim. That distinction is the only actionable difference
between the two tiers, so it is driven here against a real memory dir rather than asserted in prose.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from _source_scan import iter_trees

from looplab.adapters.toytask import ToyTask
from looplab.engine.orchestrator import Engine
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

_PKG = Path(__file__).resolve().parents[1] / "looplab"
_TASK = Path(__file__).resolve().parents[1] / "examples" / "toy_task.json"


def _constructor_sites(name: str) -> list[str]:
    """Every `name(...)` call under looplab/, excluding the class statement itself."""
    sites = []
    for path, tree in iter_trees(_PKG):
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


def _memory_dir_with_one_of_each(tmp_path, task_id: str, fingerprint: list[str]):
    """A real cross-run memory dir holding one case, one meta-note and one lesson for the SAME task.

    All three describe the same finished run, so any of the three could plausibly be the thing a
    later run is warm-started from. Each carries a distinctive marker token so the rendered prior can
    be attributed to exactly one tier.
    """
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)         # called twice: once to build the engine, once to re-key on its fingerprint
    (mem / "cases.jsonl").write_text(json.dumps({
        "task_id": task_id, "goal": "minimize the toy quadratic",
        "direction": "min", "params": {"x": 3.0, "y": -1.0}, "metric": 0.0,
        "rationale": "CASEMARKER evaluate at the closed-form minimizer",
    }) + "\n", encoding="utf-8")
    (mem / "meta_notes.jsonl").write_text(json.dumps({
        "task_id": task_id, "run_id": "earlier",
        "direction": "min",
        "note": "NOTEMARKER best metric 0 via op 'draft' params {'x': 3.0, 'y': -1.0}",
    }) + "\n", encoding="utf-8")
    (mem / "lessons.jsonl").write_text(json.dumps({
        "task_id": task_id, "fingerprint": fingerprint, "run_id": "earlier",
        "direction": "min",
        "statement": "LESSONMARKER moving x toward 3 improves the metric",
        "outcome": "supported", "confidence": 0.7, "delta": 1.0, "role": "researcher",
    }) + "\n", encoding="utf-8")
    return mem


def _engine_over(tmp_path, mem):
    task = ToyTask.load(_TASK)
    researcher, developer = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                  reflection_priors=True, memory_dir=str(mem))


def test_the_injected_prior_carries_notes_and_lessons_but_never_a_case(tmp_path):
    """The one difference between the three Memory tabs an operator can act on.

    Lessons and meta-notes are pasted into the next run's prompt; a case is not, and the Memory
    panel + `docs/guide/memory.md` both say so in as many words. If a case ever starts reaching the
    prior, that copy is wrong and an operator reading "not pasted into any prompt" is being misled
    about where a proposal came from.
    """
    task = ToyTask.load(_TASK)
    mem = _memory_dir_with_one_of_each(tmp_path, task.id, [])
    engine = _engine_over(tmp_path, mem)
    # Re-key the lesson on THIS task's own fingerprint so the M2 similarity gate cannot be what
    # excludes it — the test must fail for the case alone, not for a fingerprint miss.
    fp = [t for t in engine._task_fingerprint(engine._empty_state_for_fp())
          if not t.startswith("param:")]
    _memory_dir_with_one_of_each(tmp_path, task.id, fp)

    prior = _engine_over(tmp_path, mem)._load_reflection_priors()

    assert "NOTEMARKER" in prior, "exact-task meta-notes are the E4 warm start; they must reach the prior"
    assert "LESSONMARKER" in prior, "fingerprint-matched lessons are the M2/M3 transfer path"
    assert "CASEMARKER" not in prior, (
        "a case reached the injected prior — the Memory panel and docs/guide/memory.md both tell the "
        "operator a case is reachable ONLY through kb_search; update that copy in the same change")


def test_a_case_is_still_reachable_through_kb_search(tmp_path):
    """The other half: "not injected" must not quietly become "not read at all".

    `agents/factory.py` hands `cases.jsonl` to KnowledgeTools, which is what makes a past winner
    retrievable by a tool-using role. If this goes silent the case store is write-only and the
    Cases tab should say so instead.
    """
    from looplab.tools.knowledge_tools import KnowledgeTools

    task = ToyTask.load(_TASK)
    mem = _memory_dir_with_one_of_each(tmp_path, task.id, [])
    tools = KnowledgeTools(None, cases_path=str(mem / "cases.jsonl"))
    out = tools.execute("kb_search", {"query": "minimize the toy quadratic"})

    assert "CASEMARKER" in out and "PAST CASE" in out


def test_bound_kb_search_scopes_cases_before_indexing(tmp_path):
    from types import SimpleNamespace
    from looplab.tools.knowledge_tools import KnowledgeTools

    path = tmp_path / "cases.jsonl"
    rows = [
        {"task_id": "same", "goal": "optimize shared objective", "direction": "min",
         "params": {"x": 1}, "metric": 1.0, "rationale": "MIN_CASE"},
        {"task_id": "same", "goal": "optimize shared objective", "direction": "max",
         "params": {"x": 9}, "metric": 9.0, "rationale": "MAX_CASE"},
        {"task_id": "foreign", "goal": "optimize shared objective", "direction": "max",
         "params": {"x": 7}, "metric": 7.0, "rationale": "FOREIGN_CASE"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    tools = KnowledgeTools(None, cases_path=str(path))
    tools.bind_state(SimpleNamespace(run_id="live", task_id="same", direction="max",
                                     goal="optimize shared objective"))

    out = tools.execute("kb_search", {"query": "shared objective"})
    assert "MAX_CASE" in out
    assert "MIN_CASE" not in out and "FOREIGN_CASE" not in out
    assert "scope=run" in out and "objective=max" in out


def test_kb_search_refreshes_when_a_source_file_changes(tmp_path):
    from looplab.tools.knowledge_tools import KnowledgeTools

    note = tmp_path / "first.md"
    note.write_text("# First\n\nalpha-only memory", encoding="utf-8")
    tools = KnowledgeTools(str(tmp_path))
    assert "alpha-only" in tools.execute("kb_search", {"query": "alpha-only"})

    note.write_text("# First\n\nbeta-only refreshed memory", encoding="utf-8")
    out = tools.execute("kb_search", {"query": "beta-only"})
    assert "beta-only refreshed" in out
    assert "KB_INDEX: revision=" in out
