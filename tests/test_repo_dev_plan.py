"""C4 repo-developer plan decomposition: a big NON-repair task is proposed as an ordered plan of
atomic steps and executed step-by-step (each a fresh, BOUNDED session building on the accumulated
files); a repair stays a single focused session; every session gets a finite turn/time ceiling so a
non-converging model can't run away (the 10k-call / multi-hour spin this fixes)."""
from __future__ import annotations

import sys
from pathlib import Path

from looplab.core.models import Idea
from looplab.adapters.repo_task import EvalSpec, RepoTask

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}


def _task():
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.py"], protect=[],
                    eval=EvalSpec(command=[sys.executable, "main.py"], metric=_M))


def _dev(**kw):
    from looplab.adapters.repo_task import LLMRepoDeveloper
    return LLMRepoDeveloper(object(), _task(), **kw)


def _install_fake_loop(monkeypatch, plan_steps, record):
    """Patch drive_tool_loop: the plan-phase call returns `plan_steps`; every `done` session writes
    one file (named after the step index) via the real write tool, so file ACCUMULATION is exercised.
    Records (emit_name, max_turns, time_budget) per call."""
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        record.append((name, opts.get("max_turns"), opts.get("time_budget_s")))
        if name == "declare_stages":     # the mandatory stages phase (fresh repo implement) — declare a train stage
            return finalize({"stages": [{"name": "train", "command": ["python", "train.py"]}]})
        if name == "propose_plan":
            return finalize({"steps": plan_steps})
        # a `done` session (a step, or the single-session fallback): write a file via the write tool
        idx = sum(1 for r in record if r[0] == "done")
        try:
            tools.execute("write_file", {"path": f"stage{idx}.py", "content": f"# step {idx}\nprint(1)\n"})
        except Exception:  # noqa: BLE001 — some emit specs are toolless
            pass
        return finalize({"summary": f"wrote stage{idx}"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)


def test_multistep_plan_runs_atomic_steps_and_accumulates(monkeypatch):
    rec: list = []
    _install_fake_loop(monkeypatch, [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"},
                                     {"title": "C", "detail": "c"}], rec)
    dev = _dev(plan_decompose=True, plan_min_steps=2, session_max_turns=7, session_time_budget_s=123.0)
    dev.implement(Idea(operator="draft", params={"lr": 0.1}, rationale="a big multi-part change"))

    names = [r[0] for r in rec]
    assert names == ["declare_stages", "propose_plan", "done", "done", "done"]  # stages + plan + one per step
    # files accumulated across the 3 step sessions (+ the manifest the mandatory stages phase wrote)
    assert set(dev.last_files) == {"looplab_stages.json", "stage1.py", "stage2.py", "stage3.py"}
    # every step session is BOUNDED with the configured ceiling (never 0/unlimited)
    step_calls = [r for r in rec if r[0] == "done"]
    assert all(mt == 7 and tb == 123.0 for _, mt, tb in step_calls)
    # the plan phase is bounded too (its own, tighter cap)
    plan_call = [r for r in rec if r[0] == "propose_plan"][0]
    assert plan_call[1] and plan_call[1] > 0 and plan_call[2] and plan_call[2] > 0


def test_repair_stays_single_session_no_plan(monkeypatch):
    rec: list = []
    _install_fake_loop(monkeypatch, [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}], rec)
    dev = _dev(plan_decompose=True, plan_min_steps=2, session_max_turns=9)
    dev.repair(Idea(operator="debug", params={}, rationale="fix it"), code="", error="boom")
    # a repair NEVER plans — exactly one bounded `done` session, no propose_plan
    assert [r[0] for r in rec] == ["done"]
    assert rec[0][1] == 9                                         # bounded


def test_trivial_plan_falls_back_to_single_bounded_session(monkeypatch):
    rec: list = []
    _install_fake_loop(monkeypatch, [{"title": "only step", "detail": "trivial"}], rec)   # 1 step < min_steps
    dev = _dev(plan_decompose=True, plan_min_steps=2, session_max_turns=11)
    dev.implement(Idea(operator="draft", params={}, rationale="tiny change"))
    # stages (mandatory) then planned (1 step), but < min_steps -> single session fallback (still bounded)
    assert [r[0] for r in rec] == ["declare_stages", "propose_plan", "done"]
    assert rec[-1][1] == 11


def test_decompose_off_is_single_session(monkeypatch):
    rec: list = []
    _install_fake_loop(monkeypatch, [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}], rec)
    dev = _dev(plan_decompose=False, session_max_turns=13)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    # stages phase is ALWAYS mandatory (even with decompose off); then the single implement session
    assert [r[0] for r in rec] == ["declare_stages", "done"]      # no PLAN phase when off, but stages stays
    assert rec[-1][1] == 13


def test_stages_phase_cmd_context_and_prompt():
    dev = _dev()
    idea = Idea(operator="draft", params={"lr": 0.1}, rationale="x")
    # the task carries an operator cmd -> _cmd_context sees it; the prompt shows it as FIXED + reserves score
    ev, has_cmd = dev._cmd_context()
    assert has_cmd and ev.get("command")
    u = dev._stages_user(idea, ev, has_cmd)
    assert "FIXED" in u and "reserved" in u.lower() and "train" in u.lower()
    # NO operator cmd (has_cmd=False) -> the developer must declare the FULL pipeline (score NOT reserved)
    u2 = dev._stages_user(idea, {}, False)
    assert "FULL pipeline" in u2


def test_stages_phase_treats_onboard_command_as_the_cmd():
    """Onboard task: eval is None (adapter not ratified yet) but the onboard COMMAND is the scorer — the
    stages phase must see it as the immutable cmd (declare PRECEDING stages), NOT ask for a full pipeline
    whose own score stage would fight the onboarder's frozen adapter (that broke the onboarding run)."""
    import sys as _sys
    t = RepoTask(id="onb", goal="g", direction="max", editable_path=str(FIXTURE),
                 edit_surface=["*.json"], protect=["ttrain.py"],
                 onboard=True, onboard_command=[_sys.executable, "ttrain.py"], eval=None)
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper(object(), t)
    ev, has_cmd = dev._cmd_context()
    assert has_cmd and ev.get("command") == [_sys.executable, "ttrain.py"]
