"""Regression tests for the second code-review round (feasibility gate, NaN metrics, tracing)."""
from __future__ import annotations

import sys

import anyio
import orjson

from looplab.command_eval import _drift, run_command_eval
from looplab.models import Idea, Node, NodeStatus, RunState
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.repo_task import EvalSpec, RepoTask
from looplab.sandbox import SubprocessSandbox, _parse_metric
from looplab.tracing import JsonlSpanExporter, Tracer

_M = {"kind": "stdout_json", "key": "metric"}
_LAT = {"kind": "stdout_json", "key": "latency"}


# #1/#2 — an infeasible node must NOT become best even via the confirm phase
def test_infeasible_node_not_promoted_by_confirm(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    # metric is great but latency violates the constraint -> infeasible
    (repo / "run.py").write_text(
        'import json; print(json.dumps({"metric": 100.0, "latency": 999}))\n', encoding="utf-8")
    t = RepoTask(id="c", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M,
                               constraints=[{**_LAT, "name": "latency", "max": 100}]))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=3),
                 confirm_top_k=2, confirm_seeds=2)
    state = anyio.run(eng.run)
    assert state.finished
    assert all(not n.feasible for n in state.evaluated_nodes())
    assert state.best() is None                       # confirm cannot promote an infeasible node


def test_feasible_nodes_helper():
    st = RunState(direction="max")
    a = Node(id=0, operator="draft", idea=Idea(operator="draft"), metric=1.0,
             status=NodeStatus.evaluated, feasible=True)
    b = Node(id=1, operator="draft", idea=Idea(operator="draft"), metric=9.0,
             status=NodeStatus.evaluated, feasible=False)
    st.nodes = {0: a, 1: b}
    assert [n.id for n in st.feasible_nodes()] == [0]


# #3 — NaN/inf metric is rejected at read time (never enters best-selection)
def test_nan_metric_rejected(tmp_path):
    (tmp_path / "p.py").write_text(
        'import json; print(json.dumps({"metric": float("nan")}))\n', encoding="utf-8")
    res = run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _M)
    assert res.metric is None                          # NaN -> no metric, not a NaN best


def test_inf_metric_rejected_in_solution_path():
    assert _parse_metric('{"metric": Infinity}') is None
    assert _parse_metric('{"metric": 1.5}') == 1.5


def test_drift_nan_is_not_corroborated():
    assert _drift(float("nan"), float("nan"), 1e-6) is True   # NaN never "agrees"
    assert _drift(1.0, 1.0, 1e-6) is False


# #5 — an exception inside a span marks it ERROR (and re-raises)
def test_span_error_status(tmp_path):
    tr = Tracer(JsonlSpanExporter(tmp_path / "s.jsonl"), run_id="r")
    try:
        with tr.span("boom", new_trace=True):
            raise ValueError("x")
    except ValueError:
        pass
    rec = orjson.loads((tmp_path / "s.jsonl").read_bytes().splitlines()[0])
    assert rec["status"] == "ERROR"


# #9 — confirm events are correlated to their span (carry a trace_id)
def test_confirm_events_carry_trace_id(tmp_path):
    from looplab.eventstore import EventStore
    repo = tmp_path / "repo"; repo.mkdir()
    (repo / "run.py").write_text('import json; print(json.dumps({"metric": 1.0}))\n',
                                 encoding="utf-8")
    t = RepoTask(id="ce", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_M))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3),
                 confirm_top_k=1, confirm_seeds=1)
    anyio.run(eng.run)
    events = list(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    confirm_evs = [e for e in events if e.type == "confirm_eval"]
    assert confirm_evs and all(e.trace_id for e in confirm_evs)


# #8 — large data/ref mounts use a cheap shallow fingerprint (no recursive walk), still
# catching top-level add/remove; missing path -> "absent".
def test_shallow_fingerprint(tmp_path):
    from looplab.orchestrator import _shallow_fingerprint
    d = tmp_path / "data"; d.mkdir()
    (d / "a.bin").write_text("x", encoding="utf-8")
    fp1 = _shallow_fingerprint(str(d))
    assert fp1.startswith("dir:")
    (d / "b.bin").write_text("y", encoding="utf-8")          # top-level add -> changes
    assert _shallow_fingerprint(str(d)) != fp1
    assert _shallow_fingerprint(str(tmp_path / "nope")) == "absent"


# live-surfaced bug — the agentic Researcher must survive a junk emit (non-numeric params)
def test_tool_researcher_finalize_drops_nonnumeric_params():
    from looplab.agent import ToolUsingResearcher
    r = ToolUsingResearcher(client=None, tools=None, bounds=None)
    # the live model returned this and crashed the run before the fix
    idea = r._finalize({"operator": "modify_metric", "params": {"new_metric": "linear"},
                        "rationale": "switch reward landscape"})
    assert idea.params == {} and idea.rationale == "switch reward landscape"
    # numeric params survive; bounds still fill/clamp
    r2 = ToolUsingResearcher(client=None, tools=None, bounds={"x": (0.0, 10.0)})
    idea2 = r2._finalize({"operator": "improve", "params": {"x": "99", "junk": "nope"}})
    assert idea2.params == {"x": 10.0}                       # "99" clamped to 10, junk dropped


# latent fix — both sandbox tiers tolerate tier-specific kwargs (symmetry)
def test_make_sandbox_tolerates_extra_kwargs():
    from looplab.sandbox import SubprocessSandbox, make_sandbox
    s = make_sandbox("trusted_local", image="ignored", max_output_bytes=1000)
    assert isinstance(s, SubprocessSandbox) and s.max_output_bytes == 1000
