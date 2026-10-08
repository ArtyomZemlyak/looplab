"""The engine's own box probe withholds a launch whatever stage it would start at (review 2026-10-08).

`_pause_withholds_attempt` lets an attempt whose reuse point names a LATER stage run on through a
pause, because that reuse point lives in this process alone. For an OPERATOR pause that is right; for
the pause the pre-launch probe has just written over a dead mount it launched onto a broken box — a
hung mount held the GPU slot until the stage's own timeout and then failed into the same pause.
`box_fault=True` waives that one clause; a stop still drains and an intervention still owns its
terminal.
"""
from __future__ import annotations

import shutil

import anyio
import pytest

from factories import make_engine
from looplab.engine.evaluate import EvalAttempt
from looplab.engine.options import _UNSET
from looplab.events.replay import fold
from looplab.runtime.command_eval import RunResult


@pytest.mark.parametrize("next_start, controls, withheld", [
    ("score", ["pause"], True),                  # the waived clause: a reuse point on a dead box
    (_UNSET, ["pause"], True),
    ("score", ["pause", "run_abort"], False),    # a finalize still drains
    ("score", ["pause", "node_abort"], False),   # an intervention still owns the terminal
    ("score", ["pause", "node_reset"], False),
    ("score", [], False),                        # nothing paused: nothing withheld
])
def test_a_box_fault_pause_withholds_even_a_later_stage(tmp_path, next_start, controls, withheld):
    eng = make_engine(tmp_path / "run")
    eng._resolved_stages = lambda node, workdir: [{"name": "train"}, {"name": "score"}]
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"}, "code": "print(1)"})
    events = eng.store.read_all()
    a = EvalAttempt(node_id=0)
    a.generation, a.start_seq, a.node = 0, events[-1].seq, fold(events).nodes[0]
    a.next_start = next_start
    for control in controls:
        data = ({"node_id": 0, "generation": 0} if control.startswith("node_")
                else {"reason": "infra_unavailable"})
        if control == "node_reset":
            data["from_stage"] = "eval"
        eng.store.append(control, data)
    assert eng._pause_withholds_attempt(a, box_fault=True) is withheld
    if next_start == "score" and controls == ["pause"]:
        assert eng._pause_withholds_attempt(a) is False, "an OPERATOR pause keeps the old rule"


class _Dev:
    last_files: dict = {}
    last_deleted: list = []

    def __init__(self):
        self.repairs = 0

    def repair(self, idea, code, err):
        self.repairs += 1
        return code + "\n# fixed the score stage\n"


def test_a_score_only_repair_is_not_launched_onto_a_dead_mount(tmp_path, monkeypatch):
    """Driven: the first attempt fails in its score stage, the repair keeps `train` reusable
    (`next_start == "score"`), and the data mount dies during the repair. The pre-launch probe pauses
    the run — and the score stage must not start on that box."""
    data = tmp_path / "mnt" / "corpus"
    data.mkdir(parents=True)
    dev = _Dev()
    eng = make_engine(tmp_path / "run", developer=dev)
    eng._inline_repair = True
    eng._inline_repair_attempts = 2
    eng._inline_repair_reasons = ("crash",)
    eng._repo_spec = {"data": {"corpus": {"path": str(data), "mount": True}}}
    eng._resolved_stages = lambda node, workdir: [{"name": "train"}, {"name": "score"}]
    # The box has been seen WORKING (`engine/evaluate.py::box_seen_working`): a stage ran `ok`
    # on it before, so a declared mount that no longer exists WENT AWAY.
    eng.store.append("stage_finished", {"node_id": 9, "name": "prep", "status": "ok"})
    eng.store.append("node_created", {
        "node_id": 0, "parent_ids": [], "operator": "draft",
        "idea": {"operator": "draft", "params": {"x": 1.0}, "rationale": "r"},
        "code": "raise SystemExit(1)"})
    launches: list = []

    def fake_run_eval(node, workdir, env=None, profile=None, cancel=None, start_stage=None):
        launches.append(start_stage)
        return RunResult(exit_code=1, stdout="", metric=None, timed_out=False,
                         stderr="KeyError: 'score_col'")

    real_apply = eng._eval_apply_repair

    async def apply_then_lose_the_mount(a):
        sig = await real_apply(a)
        a.next_start = "score"             # the repair left `train` reusable…
        shutil.rmtree(data)                # …and the mount went away meanwhile
        return sig

    monkeypatch.setattr(eng, "_eval_apply_repair", apply_then_lose_the_mount)
    eng._run_eval = fake_run_eval
    eng._triage_crash = lambda *a, **k: {"action": "repair", "rationale": "fix the score stage",
                                         "failure_kind": "crash"}
    anyio.run(eng._evaluate, 0, anyio.CapacityLimiter(1), None)
    events = eng.store.read_all()
    st = fold(events)
    assert dev.repairs == 1
    assert len(launches) == 1, f"the repaired attempt launched onto a dead mount: {launches}"
    assert st.paused and st.pause_reason == "infra_unavailable"
    assert st.nodes[0].status.value == "pending"
    assert [(e.data["at"], e.data["reason"]) for e in events
            if e.type == "eval_attempt_withheld"] == [("before_launch", "infra_unavailable")]
