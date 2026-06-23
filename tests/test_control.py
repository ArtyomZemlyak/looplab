"""Live operator control seam (UI intervention via the event log): pause/resume, run_abort,
node_abort, budget_extend, plus the fold of all control events. Offline — mirrors the HITL
pattern in test_archive_hitl.py: append a control event, run the engine, assert folded state +
emitted domain effects. The engine stays the sole writer of domain events.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import anyio

from looplab.eventstore import EventStore
from looplab.models import NodeStatus
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
                  policy=GreedyTree(n_seeds=2, max_nodes=4), **kw)


# ---- pure fold of control events ----
def test_fold_control_events(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("pause", {})
    store.append("budget_extend", {"max_eval_seconds": 123.0})
    store.append("node_abort", {"node_id": 2, "reason": "ui"})
    store.append("hint", {"text": "try degree 5"})
    store.append("annotation", {"node_id": 1, "text": "looks promising"})
    store.append("force_confirm", {"node_id": 3})
    store.append("force_ablate", {"node_id": 1})
    store.append("fork", {"from_node_id": 1})
    store.append("promote", {"node_id": 4, "alias": "champion"})
    st = fold(store.read_all())
    assert st.paused is True
    assert st.budget_overrides == {"max_eval_seconds": 123.0}
    assert st.aborted_nodes == [2]
    assert st.pending_hints and st.pending_hints[0]["text"] == "try degree 5"
    assert st.annotations == {1: ["looks promising"]}
    assert st.confirm_requests == [3] and st.ablate_requests == [1]
    assert st.fork_requests and st.fork_requests[0]["from_node_id"] == 1
    assert st.champion == 4
    # resume clears the pause; replay stays deterministic
    store.append("resume", {})
    st2 = fold(store.read_all())
    assert st2.paused is False
    assert fold(store.read_all()).model_dump() == fold(store.read_all()).model_dump()


# ---- run_abort: terminal ----
def test_run_abort_finishes_immediately(tmp_path):
    rd = tmp_path / "run"
    EventStore(rd / "events.jsonl").append("run_abort", {"reason": "ui_stop"})
    state = anyio.run(_engine(rd).run)
    assert state.finished and state.stop_reason == "aborted"
    assert not state.nodes  # stopped before any node was created


# ---- pause -> resume: resumable ----
def test_pause_breaks_then_resume_finishes(tmp_path):
    rd = tmp_path / "run"
    EventStore(rd / "events.jsonl").append("pause", {})
    s1 = anyio.run(_engine(rd).run)
    assert not s1.finished and s1.paused
    events = list(EventStore(rd / "events.jsonl").read_all())
    assert not any(e.type == "run_finished" for e in events)

    EventStore(rd / "events.jsonl").append("resume", {})
    s2 = anyio.run(_engine(rd).run)
    assert s2.finished and not s2.paused and s2.best() is not None


# ---- node_abort: cooperative pre-eval skip -> node_failed reason="aborted", excluded from best ----
def test_node_abort_skips_eval_and_excludes_from_best(tmp_path):
    rd = tmp_path / "run"
    EventStore(rd / "events.jsonl").append("node_abort", {"node_id": 0, "reason": "ui"})
    state = anyio.run(_engine(rd).run)
    assert state.finished
    n0 = state.nodes[0]
    assert n0.status is NodeStatus.failed and n0.error_reason == "aborted"
    assert n0.metric is None
    assert state.best_node_id != 0  # an aborted node can never be best
    # the run still produced a real winner from the other seeds
    assert state.best() is not None and state.best().status is NodeStatus.evaluated


# ---- budget_extend: override raises the eval-compute cap so the run continues ----
def test_budget_extend_raises_eval_cap(tmp_path):
    # Without an extension a zero eval budget stops the run immediately (0 nodes).
    rd0 = tmp_path / "run0"
    s0 = anyio.run(_engine(rd0, max_eval_seconds=0.0).run)
    assert s0.finished and s0.stop_reason == "eval_budget" and not s0.evaluated_nodes()

    # A budget_extend control event raises the cap, so the same run proceeds normally.
    rd1 = tmp_path / "run1"
    EventStore(rd1 / "events.jsonl").append("budget_extend", {"max_eval_seconds": 9999.0})
    s1 = anyio.run(_engine(rd1, max_eval_seconds=0.0).run)
    assert s1.finished and s1.stop_reason != "eval_budget" and s1.evaluated_nodes()


# ---- forced steering (Phase 5): force_ablate / fork / force_confirm while a run is paused ----
def _paused(rd):
    """A run that pauses awaiting approval (so forced steering can be appended mid-run)."""
    return anyio.run(_engine(rd, require_approval=True).run)


def test_force_ablate_runs_and_is_replay_safe(tmp_path):
    rd = tmp_path / "run"
    s1 = _paused(rd)
    assert s1.awaiting_approval and not s1.finished
    nid = s1.best().id
    EventStore(rd / "events.jsonl").append("force_ablate", {"node_id": nid})
    s2 = _paused(rd)
    assert any(a["parent_id"] == nid for a in s2.ablations)          # ablation ran
    n_abl = len(s2.ablations)
    s3 = _paused(rd)                                                  # resume again
    assert len(s3.ablations) == n_abl                                # not repeated (replay-safe)


def test_fork_creates_improve_node(tmp_path):
    rd = tmp_path / "run"
    s1 = _paused(rd)
    nid = s1.best().id
    n0 = len(s1.nodes)
    EventStore(rd / "events.jsonl").append("fork", {"from_node_id": nid})
    s2 = _paused(rd)
    assert s2.forks_done == 1 and len(s2.nodes) > n0
    # the new node descends from the forked parent
    assert any(nid in n.parent_ids for n in s2.nodes.values() if n.id >= n0)
    s3 = _paused(rd)
    assert s3.forks_done == 1   # processed exactly once across resumes


def test_force_confirm_records_robustness_without_hijacking_best(tmp_path):
    rd = tmp_path / "run"
    s1 = _paused(rd)
    nid = s1.best().id
    EventStore(rd / "events.jsonl").append("force_confirm", {"node_id": nid})
    s2 = _paused(rd)
    assert nid in s2.confirmed_forced                 # gate closed -> done once
    assert nid in s2.confirm_seed_results             # per-seed results recorded for the UI
    assert s2.nodes[nid].confirmed_mean is None       # selection pool untouched (no node_confirmed)
    s3 = _paused(rd)
    assert s3.confirmed_forced.count(nid) == 1        # not re-confirmed on resume


# ---- mid-eval kill primitive (v2): cancel Event tree-kills an in-flight subprocess ----
def test_run_argv_cancel_kills_inflight(tmp_path):
    from looplab.sandbox import _run_argv
    cancel = threading.Event()
    timer = threading.Timer(0.4, cancel.set)
    timer.start()
    t0 = time.time()
    rc, out, err, to = _run_argv(
        [sys.executable, "-c", "import time; time.sleep(20)"], str(tmp_path),
        timeout=30.0, cancel=cancel)
    dt = time.time() - t0
    timer.cancel()
    assert dt < 6.0 and to   # killed ~0.4s in, NOT after the 20s sleep or 30s timeout


# ---- regression: _ablate must emit a gate-closing event on repo/eval-spec runs (no inf-loop) ----
def test_ablate_emits_event_on_eval_spec_run(tmp_path):
    rd = tmp_path / "run"
    eng = _engine(rd)
    eng._eval_spec = {"metric": {"kind": "stdout_json", "key": "metric"}}  # force the early-return branch
    store = eng.store
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": 1.0}}, "code": ""})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0, "stdout_tail": "", "eval_seconds": 0.1})
    anyio.run(eng._ablate, 0)
    abl = [e for e in store.read_all() if e.type == "ablate" and e.data.get("parent_id") == 0]
    assert abl and abl[0].data.get("skipped") == "repo_or_eval_spec"  # gate closes -> force_ablate terminates


# ---- regression: two EventStores on one file (engine + UI server) assign unique, monotonic seqs ----
def test_concurrent_eventstores_unique_seqs(tmp_path):
    p = tmp_path / "events.jsonl"
    s1, s2 = EventStore(p), EventStore(p)
    s1.append("a", {}); s2.append("b", {}); s1.append("c", {}); s2.append("d", {})
    seqs = [e.seq for e in EventStore(p).read_all()]
    assert seqs == [0, 1, 2, 3]   # no collisions despite independent in-memory counters
