"""The inline repair chain listens to the run, and a resumed chain pays for what it already ran.

Review 2026-09-22, ENG2-01 and ENG2-04, both driven through the real `_evaluate` before the fix:

* ENG2-01 — nothing in the chain looked at the run after admission. A PAUSE that landed while the
  first evaluation of a crash chain was running still bought 4 Developer repairs and 5 evaluations
  (`inline_repair_attempts=4`); an operator STOP (`run_abort`) the same.
* ENG2-04 — a node's terminal charged `total_eval`, which restarts at 0.0 in every process, and the
  fold charges `eval_seconds` off the terminal alone. Two 3,600 s attempts by a process that died,
  a resume, and the run's `total_eval_seconds` read 0.012 — the budget refunded, and the eval-budget
  stop inside the chain blind to the same seconds.
"""
from __future__ import annotations

import anyio

from factories import make_engine
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult


class _FixDev:
    """A Developer whose every repair still crashes — the chain would run to its cap."""

    last_files: dict = {}
    last_deleted: list = []

    def __init__(self):
        self.repairs = 0

    def repair(self, idea, code, err):
        self.repairs += 1
        return f"print({self.repairs})\nraise SystemExit(1)\n"


def _crash_chain(tmp_path, *, during_first_eval=None, max_es=None, prior_seconds=()):
    dev = _FixDev()
    engine = make_engine(tmp_path / "run", developer=dev)
    engine._inline_repair = True
    engine._inline_repair_attempts = 4
    engine._inline_repair_reasons = ("crash",)
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"},
        "code": "raise SystemExit(1)"})
    for attempt, seconds in enumerate(prior_seconds, start=1):
        # what a process that died mid-chain left behind: paid attempts, durable per-attempt rows
        engine.store.append("node_repaired", {
            "node_id": 0, "generation": 0, "attempt": attempt, "code": "raise SystemExit(1)",
            "files": {}, "deleted": [], "error_in": "boom", "triage_action": "repair",
            "rationale": "fix", "changed": ["<whole-file solution>"], "stages_passed": 0,
            "eval_seconds": seconds, "unparseable_repairs": 0})
    evals = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        evals.append(node.code)
        if len(evals) == 1 and during_first_eval is not None:
            engine.store.append(*during_first_eval)     # the operator acts mid-evaluation
        return RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                         stderr="Traceback: boom")

    engine._run_eval = fake_run_eval
    engine._triage_crash = lambda *a, **k: {"action": "repair", "rationale": "fix it",
                                            "failure_kind": "crash"}
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), max_es)
    events = engine.store.read_all()
    return engine, dev, evals, events, fold(events)


def test_a_pause_mid_chain_buys_no_further_repair_and_leaves_the_node_pending(tmp_path):
    _, dev, evals, events, st = _crash_chain(tmp_path, during_first_eval=("pause", {"reason": "operator"}))
    assert len(evals) == 1 and dev.repairs == 0, (
        f"{len(evals)} evaluations and {dev.repairs} repairs after the run was paused")
    assert st.paused
    assert st.nodes[0].status.value == "pending", "a pause is not a verdict on the node"
    assert not any(e.type in ("node_evaluated", "node_failed") for e in events)


def test_a_stop_mid_chain_settles_the_node_on_its_own_failure(tmp_path):
    _, dev, evals, events, st = _crash_chain(
        tmp_path, during_first_eval=("run_abort", {"reason": "operator"}))
    assert len(evals) == 1 and dev.repairs == 0
    terminals = [e for e in events if e.type in ("node_evaluated", "node_failed")]
    assert [t.type for t in terminals] == ["node_failed"], "exactly one terminal (invariant 2)"
    assert "run is stopping" in (st.nodes[0].triage_rationale or ""), st.nodes[0]


def test_an_unhalted_chain_still_repairs_to_its_cap(tmp_path):
    """The control arm: the check reads the RUN, and a run nobody paused repairs as before."""
    _, dev, evals, _events, st = _crash_chain(tmp_path)
    assert dev.repairs == 4 and len(evals) == 5
    assert st.nodes[0].status.value == "failed"


def test_a_resumed_chain_charges_the_attempts_its_dead_process_ran(tmp_path):
    engine = make_engine(tmp_path / "run")
    engine.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}, "code": "print(1)"})
    for attempt in (1, 2):
        engine.store.append("node_repaired", {
            "node_id": 0, "generation": 0, "attempt": attempt, "code": "print(1)", "files": {},
            "deleted": [], "error_in": "boom", "triage_action": "repair", "rationale": "fix",
            "changed": ["<whole-file solution>"], "stages_passed": 0, "eval_seconds": 3600.0,
            "unparseable_repairs": 0})
    engine._run_eval = lambda *a, **k: RunResult(exit_code=0, stdout='{"metric": 0.5}',
                                                 metric=0.5, timed_out=False, stderr="")
    anyio.run(engine._evaluate, 0, anyio.CapacityLimiter(1), None)
    st = fold(engine.store.read_all())
    assert st.nodes[0].status.value == "evaluated"
    assert st.total_eval_seconds >= 7200.0, (
        f"the run was charged {st.total_eval_seconds} s for a lifecycle that ran 7,200 s")


def test_the_budget_stop_inside_the_chain_counts_the_prior_attempts(tmp_path):
    """7,200 s already ran against a 5,000 s budget: the first new failure must not buy a repair."""
    _, dev, evals, _events, st = _crash_chain(tmp_path, max_es=5000.0,
                                              prior_seconds=(3600.0, 3600.0))
    assert len(evals) == 1 and dev.repairs == 0
    assert "eval budget exhausted" in (st.nodes[0].triage_rationale or ""), st.nodes[0]
