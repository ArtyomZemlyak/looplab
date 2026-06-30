"""Completed-partials: I14 spans, I13 budget, I10 gated promotion, I9 leakage gate,
I19 cross-run memory. All offline."""
from __future__ import annotations

import json
from pathlib import Path

import anyio
import orjson

from looplab.confirm import confirm_top_k
from looplab.eventstore import EventStore
from looplab.memory import JsonlCaseLibrary
from looplab.models import Idea, Node, NodeStatus
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.replay import fold
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _engine(rd, **kw):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    return Engine(rd, task=task, researcher=r, developer=d, sandbox=SubprocessSandbox(),
                  policy=GreedyTree(n_seeds=2, max_nodes=kw.pop("max_nodes", 6)), **kw)


# ---- I14: observability spans ----
def test_spans_emitted(tmp_path):
    anyio.run(_engine(tmp_path / "run").run)
    spans = (tmp_path / "run" / "spans.jsonl")
    assert spans.exists()
    recs = [orjson.loads(l) for l in spans.read_bytes().splitlines()]
    assert any(r["name"] == "evaluate" for r in recs)
    assert all("duration_s" in r for r in recs)


# ---- I13: budget ----
def test_budget_summary_event(tmp_path):
    state = anyio.run(_engine(tmp_path / "run").run)
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    assert any(e.type == "budget" for e in events)
    assert state.finished and state.stop_reason is None


def test_wall_clock_budget_aborts(tmp_path):
    state = anyio.run(_engine(tmp_path / "run", max_seconds=0.0).run)
    assert state.finished and state.stop_reason == "time_budget"
    assert len(state.nodes) == 0  # aborted before doing work


# ---- I10: variance-gated promotion ----
def _node(i, m):
    return Node(id=i, operator="improve", idea=Idea(operator="improve", params={"x": float(i)}),
                metric=m, status=NodeStatus.evaluated)


def test_within_noise_demotion_not_significant():
    # node 0 is the single-eval leader (0.5 < 0.6). Node 1 has a slightly lower MEAN
    # but within 1 SE -> selection still picks the robust mean (node 1), but the gate
    # flags the demotion as NOT statistically significant.
    nodes = [_node(0, 0.5), _node(1, 0.6)]

    def eval_fn(node, seed):
        if node.id == 0:
            return 1.0                                   # mean 1.0, std 0
        return [0.6, 1.2, 0.6, 1.2][seed % 4]            # mean 0.9, noticeable spread

    out = confirm_top_k(nodes, eval_fn, k=2, seeds=[0, 1, 2, 3], direction="min")
    assert out["best_node_id"] == 1          # robust-mean winner
    assert out["demoted_single_leader"] is True
    assert out["significant"] is False       # but not beyond 1 SE


def test_clearly_better_is_significant():
    nodes = [_node(0, 0.5), _node(1, 0.6)]   # node 0 is the single-eval leader

    def eval_fn(node, seed):
        return 2.0 if node.id == 0 else 0.2   # node 1 clearly + stably better

    out = confirm_top_k(nodes, eval_fn, k=2, seeds=[0, 1, 2, 3], direction="min")
    assert out["best_node_id"] == 1
    assert out["demoted_single_leader"] is True
    assert out["significant"] is True


def test_confirm_top_k_empty_is_safe():
    out = confirm_top_k([], lambda n, s: 0.0, k=3, seeds=[0, 1], direction="min")
    assert out["best_node_id"] is None and out["summaries"] == []


# ---- I9: leakage-first gate ----
class _LeakyTask(ToyTask):
    def leakage_inputs(self):
        # a feature perfectly correlated with the target = target leakage
        return {"features": {"leak": [0.0, 1.0, 2.0, 3.0]}, "target": [0.0, 1.0, 2.0, 3.0]}


class _CleanTask(ToyTask):
    def leakage_inputs(self):
        return {"features": {"ok": [3.0, 1.0, 4.0, 1.0]}, "target": [0.0, 1.0, 2.0, 3.0]}


def test_leakage_aborts_run(tmp_path):
    t = _LeakyTask()
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=6))
    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason == "leakage"
    assert len(state.nodes) == 0                 # refused to run on leaky data
    assert state.leakage and state.leakage["leak"] is True


def test_clean_data_proceeds(tmp_path):
    t = _CleanTask()
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=5))
    state = anyio.run(eng.run)
    assert state.finished and state.stop_reason is None
    assert state.leakage and state.leakage["leak"] is False
    assert len(state.nodes) == 5


# ---- I19: cross-run memory ----
def test_memory_persists_and_retains_best(tmp_path):
    mem = tmp_path / "mem"
    anyio.run(_engine(tmp_path / "r1", memory_dir=str(mem)).run)
    lib = JsonlCaseLibrary(mem / "cases.jsonl")
    assert len(lib.all()) == 1
    first_metric = lib.all()[0]["metric"]

    # A worse case for the same task is NOT retained; a better one replaces.
    lib2 = JsonlCaseLibrary(mem / "cases.jsonl")
    assert lib2.add({"task_id": "toy_quadratic", "goal": "g", "direction": "min",
                     "metric": first_metric + 100, "params": {}}) is False
    assert lib2.add({"task_id": "toy_quadratic", "goal": "g", "direction": "min",
                     "metric": first_metric - 100, "params": {}}) is True
    assert len(JsonlCaseLibrary(mem / "cases.jsonl").all()) == 1  # still one (upsert)


def test_past_cases_become_searchable_knowledge(tmp_path):
    """I19 retrieval: a stored case is indexed by KnowledgeTools so the Researcher can
    recall it via kb_search."""
    from looplab.knowledge_tools import KnowledgeTools
    cases = tmp_path / "cases.jsonl"
    JsonlCaseLibrary(cases).add({"task_id": "poly_regression", "direction": "min",
                                 "goal": "polynomial degree selection",
                                 "params": {"degree": 2.0}, "metric": 0.88,
                                 "rationale": "degree 2 best"})
    kt = KnowledgeTools(knowledge_dir=None, cases_path=str(cases))
    out = kt.execute("kb_search", {"query": "polynomial degree"})
    assert "PAST CASE" in out and "degree" in out.lower()
    # file tools degrade gracefully without a knowledge-base dir
    assert "no knowledge base" in kt.execute("grep", {"pattern": "x"}).lower()


class _FailsUnderConfirmDeveloper:
    """Normal eval succeeds; every confirm-seed run (LOOPLAB_EVAL_SEED set) fails."""
    def implement(self, idea):
        return ("import os, sys, json\n"
                "if os.environ.get('LOOPLAB_EVAL_SEED') is not None:\n"
                "    sys.exit(1)\n"
                "print(json.dumps({'metric': 1.0}))\n")


def test_confirm_terminates_when_all_seeds_fail(tmp_path):
    """Regression: a confirm pass where every seed run fails must still finish (it used
    to loop forever, emitting no best_confirmed)."""
    task = ToyTask.load(TASK)
    r, _ = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r,
                 developer=_FailsUnderConfirmDeveloper(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=4),
                 confirm_top_k=2, confirm_seeds=3)
    state = anyio.run(eng.run)            # must not hang
    assert state.finished and state.confirmed_done
    assert state.best() is not None       # falls back to the single-eval leader


def test_memory_search():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        lib = JsonlCaseLibrary(Path(d) / "cases.jsonl")
        lib.add({"task_id": "poly", "goal": "polynomial regression degree", "direction": "min", "metric": 1.0})
        lib.add({"task_id": "img", "goal": "image segmentation unet", "direction": "min", "metric": 2.0})
        hits = lib.search("polynomial degree selection", k=1)
        assert hits and hits[0]["task_id"] == "poly"
