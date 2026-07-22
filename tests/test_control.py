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
from types import SimpleNamespace

import anyio
import pytest

from looplab.events.eventstore import EventStore
from looplab.core.models import NodeStatus
from looplab.engine.evaluate import _card_identity_spellings
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.runtime.sandbox import RunResult, SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def _engine(rd, **kw):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    sandbox = kw.pop("sandbox", None) or SubprocessSandbox()
    return Engine(rd, task=task, researcher=r, developer=d, sandbox=sandbox,
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
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 1})
    store.append("force_ablate", {"node_id": nid})
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
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 1})
    store.append("fork", {"from_node_id": nid})
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


# ---- inject_node: operator-authored experiment materializes into a real, evaluated node ----
def test_inject_node_creates_and_evaluates(tmp_path):
    rd = tmp_path / "run"
    # A manual experiment hand-added BEFORE the run starts: the engine creates it as a real node
    # and the policy evaluates it like any other (pending nodes are scheduled first).
    EventStore(rd / "events.jsonl").append("inject_node", {
        "idea": {"operator": "manual", "params": {"x": 0.5}, "rationale": "operator hunch",
                 "theme": "hand-tuned"}, "parent_id": None, "code": None})
    state = anyio.run(_engine(rd).run)
    assert state.finished and state.injects_done == 1
    inj = next(n for n in state.nodes.values() if n.operator == "manual")
    assert inj.status is NodeStatus.evaluated and inj.metric is not None
    assert inj.idea.params == {"x": 0.5} and inj.idea.theme == "hand-tuned"
    assert inj.idea.card_id is not None
    assert state.cards[inj.idea.card_id].identity.kind == "native"
    assert state.cards[inj.idea.card_id].evidence == [inj.id]


def test_inject_node_developer_crash_fails_and_pauses(tmp_path):
    # An operator inject with NO ready-made code builds via the Developer. If that session CRASHES
    # (the "(developer error: …)" sentinel — an LLM outage / hard error), the injected node must FAIL
    # as a developer_crash and trip the circuit-breaker (pause), NOT be created pending and evaluated
    # with the parent's carried-over solution (a false metric). Mirrors the guard _create_node and
    # _rerun_node already have.
    class _CrashingDev:
        def implement(self, idea):
            return "(developer error: LLM unreachable)"

    rd = tmp_path / "run"
    task = ToyTask.load(TASK)
    r, _ = task.build_roles()
    eng = Engine(rd, task=task, researcher=r, developer=_CrashingDev(), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=4))
    eng.store.append("run_started", {"run_id": "run", "task_id": "toy", "direction": "min"})
    eng._create_injected_node({
        "idea": {"operator": "manual", "params": {"x": 0.5}, "rationale": "hunch"},
        "parent_id": None, "code": None})
    events = eng.store.read_all()
    assert any(e.type == "node_failed" and e.data.get("reason") == "developer_crash" for e in events), \
        "a crashed-developer inject must fail as developer_crash, not evaluate a false metric"
    st = fold(events)
    assert st.paused, "a developer crash on an inject must trip the circuit-breaker (pause)"
    assert not any(n.status is NodeStatus.evaluated for n in st.nodes.values())


def test_inject_node_with_parent_and_replay_safe(tmp_path):
    rd = tmp_path / "run"
    s1 = _paused(rd)                                  # a paused run with evaluated nodes
    pid = s1.best().id
    n0 = len(s1.nodes)
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 1})
    store.append("inject_node", {
        "idea": {"operator": "improve", "params": {"x": 0.1}}, "parent_id": pid})
    s2 = _paused(rd)
    assert s2.injects_done == 1 and len(s2.nodes) > n0
    child = next(n for n in s2.nodes.values() if n.id >= n0)
    assert pid in child.parent_ids                    # branched from the chosen parent
    s3 = _paused(rd)
    assert s3.injects_done == 1                        # processed exactly once across resumes


def test_tombstoned_parent_is_rejected_by_inject_policy_build_and_ablation(tmp_path):
    rd = tmp_path / "run"
    eng = _engine(rd)
    eng.store.append("run_started", {
        "run_id": "run", "task_id": "toy", "direction": "min"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 0.0}}, "code": "c"})
    eng.store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0})
    eng.store.append("node_tombstoned", {"node_ids": [0]})

    with pytest.raises(ValueError, match="unavailable"):
        eng._create_injected_node({
            "idea": {"operator": "improve", "params": {"x": 0.1}}, "parent_id": 0})
    eng._create_node({"kind": "improve", "parent_id": 0})
    anyio.run(eng._ablate, 0)

    events = eng.store.read_all()
    assert not any(event.type == "ablate" for event in events)
    assert set(fold(events).nodes) == {0}


def test_inject_merge_with_parent_ids_builds_multiparent_node(tmp_path):
    # U3 drag-to-merge: an inject_node with a `parent_ids` list + operator "merge" and no code
    # materializes a REAL multi-parent node via the engine's merge/ensemble path (not a blank manual
    # node), so a canvas merge gesture produces a genuine combined child.
    rd = tmp_path / "run"
    s1 = _paused(rd)
    ids = sorted(s1.nodes)[:2]
    assert len(ids) == 2
    n0 = len(s1.nodes)
    store = EventStore(rd / "events.jsonl")
    store.append("budget_extend", {"add_nodes": 1})
    store.append("inject_node", {
        "idea": {"operator": "merge", "rationale": "canvas merge"}, "parent_ids": ids})
    s2 = _paused(rd)
    child = next(n for n in s2.nodes.values() if n.id >= n0 and len(n.parent_ids) >= 2)
    assert set(child.parent_ids) == set(ids) and child.operator == "merge"


def test_reopen_finished_run_with_injected_node(tmp_path):
    rd = tmp_path / "run"
    s1 = anyio.run(_engine(rd).run)
    assert s1.finished
    n0 = len(s1.nodes)
    # Operator adds an experiment to the FINISHED run: reopen clears the terminal flag, the inject
    # queues the node; re-entering the loop evaluates it and re-finishes.
    store = EventStore(rd / "events.jsonl")
    store.append("run_reopened", {})
    store.append("budget_extend", {"add_nodes": 1})
    store.append("inject_node", {"idea": {"operator": "manual", "params": {"x": 0.2},
                                          "rationale": "post-hoc idea"}, "parent_id": None})
    s2 = anyio.run(_engine(rd).run)
    assert s2.finished                                    # re-finished after processing the inject
    assert s2.injects_done == 1 and len(s2.nodes) == n0 + 1
    man = next(n for n in s2.nodes.values() if n.operator == "manual")
    assert man.status is NodeStatus.evaluated and man.metric is not None


def test_inject_node_ships_ready_made_code(tmp_path):
    rd = tmp_path / "run"
    EventStore(rd / "events.jsonl").append("inject_node", {
        "idea": {"operator": "manual", "params": {}},
        "code": "print('{\"metric\": 0.123}')"})
    state = anyio.run(_engine(rd).run)
    inj = next(n for n in state.nodes.values() if n.operator == "manual")
    assert inj.code == "print('{\"metric\": 0.123}')"   # ran the operator's code verbatim
    assert inj.status is NodeStatus.evaluated and inj.metric == 0.123


# ---- mid-eval kill primitive (v2): cancel Event tree-kills an in-flight subprocess ----
def test_run_argv_cancel_kills_inflight(tmp_path):
    from looplab.runtime.sandbox import _run_argv
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


class _BlockingCardEvalSandbox:
    """Deterministic eval that exposes the cancellation Event without spawning a process."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel = None

    def run(self, code, workdir, timeout=30.0, env=None, cancel=None):
        self.cancel = cancel
        self.started.set()
        while not self.release.wait(0.01):
            if cancel is not None and cancel.is_set():
                return RunResult(
                    exit_code=-9, stdout="", stderr="cancelled", metric=None, timed_out=True)
        return RunResult(
            exit_code=0, stdout='{"metric": 0.25}', stderr="", metric=0.25,
            timed_out=False)


def _seed_pending_card_eval(eng):
    eng.store.append("run_started", {
        "run_id": "r", "task_id": "t", "goal": "g", "direction": "min",
    })
    eng.store.append("card_added", {
        "id": "card-1", "statement": "long candidate", "source": "researcher",
    })
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {
            "operator": "draft", "hypothesis": "long candidate", "card_id": "card-1",
        },
        "code": "print(0.25)",
    })


async def _exercise_card_drop_watcher(eng, sandbox, event, before_start, expect_cancel):
    if before_start:
        eng.store.append("card_dropped", event)
    try:
        with anyio.fail_after(3.0):
            async with anyio.create_task_group() as tg:
                tg.start_soon(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
                started = await anyio.to_thread.run_sync(sandbox.started.wait, 1.0)
                assert started, "eval did not enter the sandbox"
                if not before_start:
                    eng.store.append("card_dropped", event)
                if expect_cancel:
                    cancelled = await anyio.to_thread.run_sync(sandbox.cancel.wait, 1.5)
                    assert cancelled, "operator Card drop did not reach the eval cancel Event"
                else:
                    await anyio.sleep(0.45)  # watcher cadence is 0.3s
                    assert sandbox.cancel is not None and not sandbox.cancel.is_set()
                    sandbox.release.set()
    finally:
        sandbox.release.set()


def test_operator_card_drop_tree_kills_matching_inflight_eval_and_charges_compute(tmp_path):
    sandbox = _BlockingCardEvalSandbox()
    eng = _engine(tmp_path / "operator-drop", sandbox=sandbox)
    _seed_pending_card_eval(eng)

    anyio.run(
        _exercise_card_drop_watcher,
        eng, sandbox,
        {"id": "card-1", "reason": "stop now", "dropped_by": "operator"},
        False, True,
    )

    terminal = [event for event in eng.store.read_all() if event.type == "node_failed"][-1]
    assert terminal.data["reason"] == "card_dropped"
    assert terminal.data["eval_seconds"] > 0
    assert "Card dropped by operator" in terminal.data["error"]


def test_operator_canonical_drop_kills_inflight_eval_linked_to_merged_alias(tmp_path):
    sandbox = _BlockingCardEvalSandbox()
    eng = _engine(tmp_path / "operator-drop-merged-alias", sandbox=sandbox)
    _seed_pending_card_eval(eng)
    eng.store.append("card_added", {
        "id": "card-2", "statement": "canonical candidate", "source": "researcher",
    })
    eng.store.append("card_merged", {"canonical": "card-2", "aliases": ["card-1"]})

    before = fold(eng.store.read_all())
    assert before.nodes[0].idea.card_id == "card-1"  # immutable proposal-time identity
    assert "card-1" in before.cards["card-2"].aliases

    anyio.run(
        _exercise_card_drop_watcher,
        eng, sandbox,
        {"id": "card-2", "reason": "stop merged work", "dropped_by": "operator"},
        False, True,
    )

    terminal = [event for event in eng.store.read_all() if event.type == "node_failed"][-1]
    assert terminal.data["reason"] == "card_dropped"
    assert terminal.data["eval_seconds"] > 0


def test_active_card_alias_resolution_fails_closed_for_ambiguous_owner():
    state = SimpleNamespace(cards={
        "card-2": SimpleNamespace(aliases=["card-1"]),
        "card-3": SimpleNamespace(aliases=["card-1"]),
    })

    assert _card_identity_spellings(state, "card-1") == frozenset()


@pytest.mark.parametrize(("event", "before_start"), [
    ({"id": "card-1", "reason": "engine decision", "dropped_by": "engine"}, False),
    ({"id": "card-other", "reason": "stop now", "dropped_by": "operator"}, False),
])
def test_card_drop_watcher_ignores_non_operator_and_unrelated_events(
        tmp_path, event, before_start):
    sandbox = _BlockingCardEvalSandbox()
    eng = _engine(tmp_path / f"ignored-{event['dropped_by']}-{event['id']}-{before_start}",
                  sandbox=sandbox)
    _seed_pending_card_eval(eng)

    anyio.run(
        _exercise_card_drop_watcher,
        eng, sandbox, event,
        before_start, False,
    )

    node = fold(eng.store.read_all()).nodes[0]
    assert node.status is NodeStatus.evaluated and node.metric == 0.25


def test_operator_card_drop_before_eval_closes_pending_node_without_compute(tmp_path):
    sandbox = _BlockingCardEvalSandbox()
    eng = _engine(tmp_path / "prestart-operator-card-drop", sandbox=sandbox)
    _seed_pending_card_eval(eng)
    eng.store.append("card_dropped", {
        "id": "card-1", "reason": "already visible", "dropped_by": "operator",
    })

    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)

    assert sandbox.started.is_set() is False
    terminal = [event for event in eng.store.read_all() if event.type == "node_failed"][-1]
    assert terminal.data["reason"] == "card_dropped"
    assert terminal.data["eval_seconds"] == 0.0
    assert fold(eng.store.read_all()).nodes[0].status is NodeStatus.failed


class _BlockingProbeSandbox:
    """A deterministic long probe that only exits through the Sandbox cancel contract."""

    def __init__(self):
        self.started = threading.Event()
        self.cancel = None

    def run(self, code, workdir, timeout=30.0, env=None, cancel=None):
        self.cancel = cancel
        self.started.set()
        if cancel is not None:
            cancel.wait(5.0)
        return RunResult(exit_code=-9, stdout="", stderr="cancelled", metric=None,
                         timed_out=bool(cancel and cancel.is_set()))


@pytest.mark.parametrize("intervention", ["node_abort", "node_reset"])
@pytest.mark.parametrize("code_blocks", [False, True])
def test_ablation_kills_probe_when_parent_lifecycle_changes(
        tmp_path, intervention, code_blocks):
    """Abort/reset must kill both parameter and code-block probes, not merely discard their result."""
    rd = tmp_path / f"run-{intervention}-{code_blocks}"
    sandbox = _BlockingProbeSandbox()
    eng = _engine(rd, sandbox=sandbox)
    eng._ablate_code_blocks = code_blocks
    store = eng.store
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}},
        "code": "x = 1\n\nprint(x)\n",
    })
    store.append("node_evaluated", {
        "node_id": 0, "generation": 0, "metric": 1.0,
        "stdout_tail": "", "eval_seconds": 0.1,
    })

    async def _exercise():
        async with anyio.create_task_group() as tg:
            tg.start_soon(eng._ablate, 0)
            started = await anyio.to_thread.run_sync(sandbox.started.wait, 2.0)
            assert started, "ablation probe did not start"
            store.append(intervention, {"node_id": 0, "generation": 0})

    t0 = time.monotonic()
    anyio.run(_exercise)
    assert time.monotonic() - t0 < 3.0
    assert sandbox.cancel is not None and sandbox.cancel.is_set()
    events = [e for e in store.read_all() if e.type == "ablate"]
    assert len(events) == 1 and events[0].data.get("superseded") is True


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
    s1.append("a", {})
    s2.append("b", {})
    s1.append("c", {})
    s2.append("d", {})
    seqs = [e.seq for e in EventStore(p).read_all()]
    assert seqs == [0, 1, 2, 3]   # no collisions despite independent in-memory counters


def test_natural_finish_uses_the_action_decision_sequence(tmp_path):
    """An inject landing inside policy selection must invalidate that stale no-actions decision."""
    rd = tmp_path / "run"
    eng = _engine(rd)
    original = eng.policy.next_actions
    raced = False

    def _next_actions(state):
        nonlocal raced
        actions = original(state)
        if not actions and state.nodes and not raced:
            raced = True
            eng.store.append("budget_extend", {"add_nodes": 1})
            eng.store.append("inject_node", {
                "idea": {"operator": "manual", "params": {"x": 0.25},
                         "rationale": "won the finish race"},
            })
            return []
        return actions

    eng.policy.next_actions = _next_actions
    state = anyio.run(eng.run)
    assert raced and state.finished and state.injects_done == 1
    assert any(n.operator == "manual" and n.status is NodeStatus.evaluated
               for n in state.nodes.values())


def test_live_engine_rebuilds_holdout_partition_when_race_rotates_epoch(tmp_path):
    """An epoch bump inside one Engine.run cannot keep scoring the prior hidden partition."""
    rd = tmp_path / "run"
    eng = _engine(rd)
    original_actions = eng.policy.next_actions
    original_build = eng._build_holdout_idx
    rebuilt_epochs = []
    raced = False

    def _build(fraction, epoch=0):
        rebuilt_epochs.append(epoch)
        return original_build(fraction, epoch)

    def _next_actions(state):
        nonlocal raced
        actions = original_actions(state)
        if not actions and state.best() is not None and not raced:
            raced = True
            best = state.best()
            eng.store.append("holdout_evaluated", {
                "node_id": best.id, "generation": best.attempt,
                "metric": best.metric, "search_epoch": state.search_epoch})
            eng.store.append("budget_extend", {"add_nodes": 1})
            eng.store.append("inject_node", {
                "idea": {"operator": "manual", "params": {"x": 0.33}}})
            return []
        return actions

    eng._build_holdout_idx = _build
    eng.policy.next_actions = _next_actions
    state = anyio.run(eng.run)
    assert raced and state.finished and state.search_epoch == 1
    assert eng._holdout_epoch == 1 and 1 in rebuilt_epochs


def test_finish_report_cannot_hide_a_concurrent_control(tmp_path):
    """The slow final report is part of the CAS chain, not a window that absorbs a new intent."""
    rd = tmp_path / "run"
    eng = _engine(rd)
    injected = False

    class _ReportWriter:
        def generate(self, state, trigger):
            nonlocal injected
            if trigger == "finish" and not injected:
                injected = True
                eng.store.append("budget_extend", {"add_nodes": 1})
                eng.store.append("inject_node", {
                    "idea": {"operator": "manual", "params": {"x": 0.75},
                             "rationale": "arrived during final report"},
                })
            return {"at_node": len(state.nodes), "summary": "ok"}

    eng.report_writer = _ReportWriter()
    eng.report_every = 1
    state = anyio.run(eng.run)
    assert injected and state.finished and state.injects_done == 1
    assert any(n.operator == "manual" and n.status is NodeStatus.evaluated
               for n in state.nodes.values())


def test_operator_finalize_writes_report_immediately_before_accepted_finish(tmp_path):
    """Finalize has the same report-before-finish CAS contract as natural completion."""
    rd = tmp_path / "run"

    class _ReportWriter:
        def generate(self, state, trigger):
            return {"at_node": len(state.nodes), "summary": "final", "trigger": trigger}

    eng = _engine(rd, report_writer=_ReportWriter(), report_every=1)
    eng.store.append("run_abort", {"reason": "operator"})
    state = anyio.run(eng.run)
    events = eng.store.read_all()
    finish = next(e for e in reversed(events) if e.type == "run_finished" and state.finished)
    report = next(e for e in reversed(events[:finish.seq]) if e.type == "report_generated")
    assert report.data["trigger"] == "finish"
    assert finish.data["after_seq"] == report.seq and finish.seq == report.seq + 1
    assert state.report and state.report["summary"] == "final"


def test_resume_wins_if_it_lands_after_finalize_decision(tmp_path):
    rd = tmp_path / "run"
    eng = _engine(rd)
    eng.store.append("run_abort", {"reason": "finalized"})
    original = eng._finish_if_quiescent
    raced = False

    def _finish(data, *, after_seq):
        nonlocal raced
        if data.get("reason") == "aborted" and not raced:
            raced = True
            eng.store.append("resume", {})
        return original(data, after_seq=after_seq)

    eng._finish_if_quiescent = _finish
    state = anyio.run(eng.run)
    assert raced and state.finished and state.stop_reason != "aborted"
    assert state.evaluated_nodes()
