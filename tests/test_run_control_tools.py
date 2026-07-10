"""RunControlTools: the assistant's run-lifecycle verbs (finalize/stop/resume/reset/delete node/run).
Mode-gated (deny in plan, inline in auto) + destructive verbs refuse a live engine + delete_node takes
the whole subtree so no parent link is orphaned."""
from __future__ import annotations

import json

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.tools.machine_runs_tools import RunControlTools


def _run(rd, nodes=(0, 1, 2)):
    rd.mkdir(parents=True, exist_ok=True)
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": rd.name, "task_id": "t", "goal": "g", "direction": "min"})
    parent = []
    for nid in nodes:
        s.append("node_created", {"node_id": nid, "parent_ids": parent, "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": float(nid)}}, "code": "c"})
        s.append("node_evaluated", {"node_id": nid, "metric": float(nid)})
        parent = [nid]                       # a chain 0 <- 1 <- 2
    return s


def test_finalize_appends_run_abort_auto_mode(tmp_path):
    rd = tmp_path / "r1"
    _run(rd)
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto")   # auto = inline, no approver
    out = t.execute("finalize_run", {"run_id": "r1"})
    assert "recorded" in out
    types = [e.type for e in EventStore(rd / "events.jsonl").read_all()]
    assert "run_abort" in types


def test_plan_mode_denies(tmp_path):
    _run(tmp_path / "r2")
    t = RunControlTools(tmp_path, mode="plan")
    assert "plan mode" in t.execute("finalize_run", {"run_id": "r2"})


def test_delete_node_takes_subtree_and_heals_best(tmp_path):
    rd = tmp_path / "r3"
    _run(rd, nodes=(0, 1, 2)).append("pause", {})   # settled → the fresh-write live backstop stands down
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto")
    out = t.execute("delete_node", {"run_id": "r3", "node_id": 1})   # deletes 1 AND its descendant 2
    assert "deleted node(s) [1, 2]" in out
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert set(st.nodes) == {0}                                     # only #0 remains
    assert not [p for n in st.nodes.values() for p in n.parent_ids if p not in st.nodes]  # no broken links
    assert (rd / "events.jsonl.bak-del1").exists()                  # recoverable backup


def test_reset_node_spec_accepts_any_stage_name(tmp_path):
    """F-reset-enum: prompts/executor/HTTP route accept ANY eval-pipeline stage name (train,
    data_prep, …), so the spec must not hard-code an enum — and the tool must actually queue a
    non-classic stage."""
    rd = tmp_path / "r5"
    _run(rd)
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto")
    spec = next(s for s in t.specs() if s["function"]["name"] == "reset_node")
    stage = spec["function"]["parameters"]["properties"]["stage"]
    assert "enum" not in stage                                  # no hard-coded stage list
    assert "eval-pipeline stage" in stage["description"]        # accepted values described instead
    out = t.execute("reset_node", {"run_id": "r5", "node_id": 1, "stage": "train"})
    assert "re-run from train" in out
    ev = [e for e in EventStore(rd / "events.jsonl").read_all() if e.type == "node_reset"]
    assert ev and ev[-1].data["from_stage"] == "train"


def test_destructive_refuses_live_engine(tmp_path):
    _run(tmp_path / "r4")
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: True, mode="auto")   # engine "live"
    assert "LIVE" in t.execute("delete_run", {"run_id": "r4"})
    assert (tmp_path / "r4").exists()                               # not deleted


def test_traversal_and_unknown_run_rejected(tmp_path):
    t = RunControlTools(tmp_path, mode="auto", alive_fn=lambda _rd: False)
    assert "no such run" in t.execute("finalize_run", {"run_id": "../etc"})
    assert "no such run" in t.execute("finalize_run", {"run_id": "nope"})


def test_delete_refuses_fresh_write_even_when_flock_says_dead(tmp_path):
    # security backstop: on a FUSE mount flock (alive_fn) can wrongly say "dead"; a fresh events.jsonl
    # write on a non-settled run must still be treated as LIVE so the log isn't rewritten under it.
    _run(tmp_path / "r5", nodes=(0, 1))     # just written, not paused/finished
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto")   # flock lies: "dead"
    assert "LIVE" in t.execute("delete_node", {"run_id": "r5", "node_id": 1})
    assert not (tmp_path / "r5" / "events.jsonl.bak-del1").exists()          # never rewrote the log


# --- live settings tools (the assistant CAN change certain run settings) --------------------------

def _tools(tmp_path):
    return RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto")


def test_extend_budget_appends_budget_extend(tmp_path):
    rd = tmp_path / "b1"
    _run(rd)
    out = _tools(tmp_path).execute("extend_budget", {"run_id": "b1", "add_nodes": 5,
                                                     "max_eval_seconds": 1200})
    assert "budget extended" in out
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.budget_overrides.get("add_nodes") == 5
    assert st.budget_overrides.get("max_eval_seconds") == 1200.0


def test_extend_budget_records_but_does_not_reopen_a_finished_run(tmp_path):
    # Reopening a finished run with no engine attached would leave it in limbo (not running, and
    # reset_run refuses a non-finished run). So the budget is RECORDED and the run stays finished/
    # resettable until an explicit resume applies it.
    rd = tmp_path / "b2"
    _run(rd).append("run_finished", {})
    assert fold(EventStore(rd / "events.jsonl").read_all()).finished is True
    out = _tools(tmp_path).execute("extend_budget", {"run_id": "b2", "add_nodes": 3})
    assert "FINISHED" in out and "resume" in out.lower()
    types = [e.type for e in EventStore(rd / "events.jsonl").read_all()]
    assert "run_reopened" not in types and "budget_extend" in types
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.finished is True and st.budget_overrides.get("add_nodes") == 3   # stays finished


def test_extend_budget_rejects_nonfinite_negative_and_empty(tmp_path):
    _run(tmp_path / "b3")
    t = _tools(tmp_path)
    assert "finite" in t.execute("extend_budget", {"run_id": "b3", "max_seconds": float("inf")})
    assert "at least one" in t.execute("extend_budget", {"run_id": "b3"})
    # a negative add_nodes would SHRINK the budget (base + add_nodes) — reject it
    assert "positive" in t.execute("extend_budget", {"run_id": "b3", "add_nodes": -50})
    assert "positive" in t.execute("extend_budget", {"run_id": "b3", "add_nodes": 0})


def test_set_directive_appends_hint(tmp_path):
    rd = tmp_path / "d1"
    _run(rd)
    out = _tools(tmp_path).execute("set_directive", {"run_id": "d1", "text": "use only sklearn"})
    assert "directive recorded" in out
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert st.pending_hints and st.pending_hints[-1]["text"] == "use only sklearn"


def test_set_trust_gate_applies(tmp_path):
    rd = tmp_path / "g1"
    _run(rd)
    t = _tools(tmp_path)
    assert "must be audit" in t.execute("set_trust_gate", {"run_id": "g1", "trust_gate": "nonsense"})
    out = t.execute("set_trust_gate", {"run_id": "g1", "trust_gate": "block"})
    assert "trust_gate set to block" in out
    assert fold(EventStore(rd / "events.jsonl").read_all()).trust_gate == "block"


def test_settings_tools_denied_in_plan_mode(tmp_path):
    _run(tmp_path / "p1")
    t = RunControlTools(tmp_path, mode="plan")
    for name, args in (("extend_budget", {"run_id": "p1", "add_nodes": 1}),
                       ("set_directive", {"run_id": "p1", "text": "x"}),
                       ("set_trust_gate", {"run_id": "p1", "trust_gate": "gate"})):
        assert "plan mode" in t.execute(name, args)
