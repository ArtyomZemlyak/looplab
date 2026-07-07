"""RunControlTools: the assistant's run-lifecycle verbs (finalize/stop/resume/reset/delete node/run).
Mode-gated (deny in plan, inline in auto) + destructive verbs refuse a live engine + delete_node takes
the whole subtree so no parent link is orphaned."""
from __future__ import annotations

import json

from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.tools.runs_tools import RunControlTools


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
    _run(rd, nodes=(0, 1, 2))               # chain; #2 is best (max metric under… direction min → best is #0)
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: False, mode="auto")
    out = t.execute("delete_node", {"run_id": "r3", "node_id": 1})   # deletes 1 AND its descendant 2
    assert "deleted node(s) [1, 2]" in out
    st = fold(EventStore(rd / "events.jsonl").read_all())
    assert set(st.nodes) == {0}                                     # only #0 remains
    assert not [p for n in st.nodes.values() for p in n.parent_ids if p not in st.nodes]  # no broken links
    assert (rd / "events.jsonl.bak-del1").exists()                  # recoverable backup


def test_destructive_refuses_live_engine(tmp_path):
    _run(tmp_path / "r4")
    t = RunControlTools(tmp_path, alive_fn=lambda _rd: True, mode="auto")   # engine "live"
    assert "LIVE" in t.execute("delete_run", {"run_id": "r4"})
    assert (tmp_path / "r4").exists()                               # not deleted


def test_traversal_and_unknown_run_rejected(tmp_path):
    t = RunControlTools(tmp_path, mode="auto", alive_fn=lambda _rd: False)
    assert "no such run" in t.execute("finalize_run", {"run_id": "../etc"})
    assert "no such run" in t.execute("finalize_run", {"run_id": "nope"})
