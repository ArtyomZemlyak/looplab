"""I14 JSONL span exporter + I22 EvolutionaryPolicy (pluggable algorithm seam)."""
from __future__ import annotations

from pathlib import Path

import anyio
import orjson

from looplab.orchestrator import Engine
from looplab.policy import EvolutionaryPolicy, MCTSPolicy, make_policy
from looplab.sandbox import SubprocessSandbox
from looplab.toytask import ToyTask
from looplab.tracing import JsonlSpanExporter, Tracer

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "toy_task.json"


# ------------------------------- I14 tracing ------------------------------- #
def test_span_exporter_writes_jsonl(tmp_path):
    tracer = Tracer(JsonlSpanExporter(tmp_path / "spans.jsonl"), run_id="r")
    with tracer.span("evaluate", new_trace=True, node_id=3) as sp:
        sp.set("extra", True)
    lines = (tmp_path / "spans.jsonl").read_bytes().splitlines()
    assert len(lines) == 1
    rec = orjson.loads(lines[0])
    assert rec["name"] == "evaluate"
    assert rec["attributes"]["node_id"] == 3 and rec["attributes"]["extra"] is True
    assert "duration_s" in rec and rec["duration_s"] >= 0.0
    assert rec["trace_id"] and rec["status"] == "OK"


# --------------------------- I22 evolutionary ------------------------------ #
def test_make_policy_selects_evolutionary():
    pol = make_policy("evolutionary", n_seeds=4, max_nodes=12)
    assert isinstance(pol, EvolutionaryPolicy)


def test_make_policy_selects_mcts():
    assert isinstance(make_policy("mcts", n_seeds=3, max_nodes=12), MCTSPolicy)


def test_mcts_policy_runs_end_to_end(tmp_path):
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=MCTSPolicy(n_seeds=3, max_nodes=14))
    state = anyio.run(eng.run)
    assert state.finished and len(state.nodes) == 14
    assert state.best() is not None and state.best().metric < 5.0
    # UCB1 explores more than one subtree (not just the single best).
    improve_parents = {n.parent_ids[0] for n in state.nodes.values()
                       if n.operator == "improve" and n.parent_ids}
    assert len(improve_parents) >= 2


def test_evolutionary_policy_runs_end_to_end(tmp_path):
    """The alternative policy plugs into the UNCHANGED engine and optimizes the toy."""
    task = ToyTask.load(TASK_FILE)
    researcher, developer = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(),
                 policy=EvolutionaryPolicy(pop=4, max_nodes=14))
    state = anyio.run(eng.run)
    assert state.finished and len(state.nodes) == 14
    assert state.best() is not None and state.best().metric < 5.0
    # Evolution explores multiple elites + crossover: expect merges and improves off
    # more than one distinct parent (broader than greedy's single-best exploitation).
    merges = [n for n in state.nodes.values() if n.operator == "merge"]
    improve_parents = {n.parent_ids[0] for n in state.nodes.values()
                       if n.operator == "improve" and n.parent_ids}
    assert merges
    assert len(improve_parents) >= 2
