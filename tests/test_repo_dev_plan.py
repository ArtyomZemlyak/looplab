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


def _install_fake_loop(monkeypatch, plan_steps, record, capture=None, stages_emit=None):
    """Patch drive_tool_loop: the plan-phase call returns `plan_steps`; every `done` session writes
    one file (named after the step index) via the real write tool, so file ACCUMULATION is exercised.
    Records (emit_name, max_turns, time_budget) per call. `capture` (optional list) additionally gets
    the FULL per-call context ({name, tools, messages, opts}) so a test can pin the phase toolsets /
    prompts / validate wiring. `stages_emit` overrides the stages-phase emit args (e.g. an invalid
    manifest, to exercise the empty-declared degradation)."""
    import looplab.agents.agent as agent_mod

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        record.append((name, opts.get("max_turns"), opts.get("time_budget_s")))
        if capture is not None:
            capture.append({"name": name, "tools": tools, "messages": list(messages), "opts": opts})
        if name == "declare_stages":     # the mandatory stages phase (fresh repo implement) — declare a train stage
            return finalize(stages_emit if stages_emit is not None
                            else {"stages": [{"name": "train", "command": ["python", "train.py"]}]})
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


def test_stages_and_plan_phases_are_read_only(monkeypatch):
    """The first two phases must NOT be able to mutate the repo: their toolsets are the scouts + the
    env inspector only — a regression that passes the write-capable composite into the stages/plan
    loop would let a 'read-only' phase write code (mega-review A1)."""
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}],
                       rec, capture=cap)
    dev = _dev(plan_decompose=True, plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    by_name = {c["name"]: c for c in cap}
    for phase in ("declare_stages", "propose_plan"):
        names = {s["function"]["name"] for s in by_name[phase]["tools"].specs()}
        assert not names & {"write_file", "edit_file", "delete_file", "apply_patch"}, \
            f"{phase} phase got WRITE tools: {names}"
        assert "read_file" in names and "pkg_info" in names       # scouts + env inspector present
    # ... while the implement session IS write-capable, and can also FIX the stage manifest (a repair
    # whose root cause is a bad stage command/timeout has no other route — mega-review D1)
    done_names = {s["function"]["name"] for s in by_name["done"]["tools"].specs()}
    assert {"write_file", "edit_file", "declare_stages"} <= done_names


def test_stages_phase_validate_wiring_reserves_score(monkeypatch):
    """The stages loop must carry the shared validator so a malformed manifest (or a reserved `score`
    stage) is bounced BACK to the model with the reason — not silently accepted (mega-review A2)."""
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [], rec, capture=cap)
    dev = _dev(plan_decompose=False)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    stages_call = next(c for c in cap if c["name"] == "declare_stages")
    validate = stages_call["opts"].get("validate")
    assert callable(validate)                                     # wired into the loop
    err = validate({"stages": [{"name": "score", "command": ["python", "s.py"]}]})
    assert err and "score" in err.lower()                         # reserved name bounced with a reason
    assert validate({"stages": [{"name": "train", "command": ["python", "t.py"]}]}) is None


def test_manifest_written_in_the_shape_the_engine_consumes(monkeypatch):
    """looplab_stages.json must parse to {"stages": [{name, command}, …]} — the exact shape
    _resolve_stages reads back; a wrapperless or malformed manifest is silently ignored engine-side."""
    import json as _json
    rec: list = []
    _install_fake_loop(monkeypatch, [], rec)
    dev = _dev(plan_decompose=False)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    data = _json.loads(dev.last_files["looplab_stages.json"])
    assert isinstance(data, dict) and isinstance(data["stages"], list)
    assert data["stages"][0]["name"] == "train" and data["stages"][0]["command"]


def test_protected_manifest_skips_stages_phase_and_prompt_says_no_stages(monkeypatch):
    """protect: ['looplab_stages.json'] is the operator knob that disables Developer pipelines — the
    STAGES phase must be SKIPPED (its manifest could never materialize; the old code burned a full LLM
    loop whose output was silently dropped) and the implement prompt must say NO stages exist instead
    of asserting a train stage was declared (mega-review D2/D3)."""
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [], rec, capture=cap)
    t = RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                 edit_surface=["*.py"], protect=["looplab_stages.json"],
                 eval=EvalSpec(command=[sys.executable, "main.py"], metric=_M))
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper(object(), t, plan_decompose=False)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    assert [r[0] for r in rec] == ["done"]                        # no declare_stages loop at all
    user = cap[0]["messages"][1]["content"]
    assert "NO pipeline stages" in user and "protected" in user


def test_stage_note_states_the_declared_pipeline(monkeypatch):
    """The implement session is told the node's ACTUAL pipeline — never an assumed one."""
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [], rec, capture=cap)
    dev = _dev(plan_decompose=False)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    user = next(c for c in cap if c["name"] == "done")["messages"][1]["content"]
    assert "PIPELINE for this node" in user and "train" in user


def test_empty_stages_phase_degrades_the_prompt_not_asserts_train(monkeypatch):
    """A failed/empty stages phase (LLM never declared, or 3 invalid emits) must flip the implement
    prompt to 'cmd runs ALONE — the entrypoint must train then score', not assert a train stage that
    doesn't exist (which produced score-only entrypoints scoring stale checkpoints — mega-review D2)."""
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [], rec, capture=cap, stages_emit={"stages": []})
    dev = _dev(plan_decompose=False)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    user = next(c for c in cap if c["name"] == "done")["messages"][1]["content"]
    assert "NO pipeline stages" in user
    assert "train a FRESH model" in user


def test_operator_declared_stages_skip_the_stages_phase(monkeypatch):
    """When the OPERATOR already declared a full eval.stages pipeline, the engine uses it verbatim (a
    Developer manifest would be ignored by _resolve_stages) — so the mandatory STAGES phase is SKIPPED
    (no wasted declare_stages loop, no misleading 'cmd is appended' contract). Plan + implement still run."""
    rec: list = []
    _install_fake_loop(monkeypatch, [{"title": "A", "detail": "a"}, {"title": "B", "detail": "b"}], rec)
    t = RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE), edit_surface=["*.py"], protect=[],
                 eval=EvalSpec(stages=[{"name": "train", "command": ["python", "train.py"]},
                                       {"name": "score", "command": ["python", "test.py"]}], metric=_M))
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper(object(), t, plan_decompose=True, plan_min_steps=2)
    dev.implement(Idea(operator="draft", params={}, rationale="x"))
    names = [r[0] for r in rec]
    assert "declare_stages" not in names                 # STAGES phase skipped (operator owns the pipeline)
    assert names == ["propose_plan", "done", "done"]     # plan + implement still run


def test_repair_prompt_lists_the_seeded_working_set_not_last_files(monkeypatch):
    """P11: the repair block's 'Files in this node's working set' must be filled from the files
    ACTUALLY seeded for THIS repair (`write.files`, pre-loaded by repair_from from the failing
    node), never from the shared developer's `last_files` (whatever node it built LAST)."""
    from looplab.core.models import Node
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [], rec, capture=cap)
    dev = _dev(plan_decompose=False)
    dev.last_files = {"other.py": "WRONG = 1"}                    # the last-BUILT node's files
    node = Node(id=5, operator="improve", idea=Idea(operator="improve", params={}),
                files={"solution.py": "RIGHT = 1"})
    dev.repair_from(Idea(operator="improve", params={}, rationale="fix"), node, "boom")
    user = cap[0]["messages"][1]["content"]
    assert "working set: solution.py" in user                     # the node's OWN files
    assert "other.py" not in user                                 # not the wrong node's


def test_repair_prompt_restates_the_manifest_pipeline(monkeypatch):
    """P33: repair sessions are told the node's ACTUAL pipeline when it is knowable — here from the
    Developer manifest riding in the failing node's own files."""
    import json as _json
    from looplab.core.models import Node
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [], rec, capture=cap)
    dev = _dev(plan_decompose=False)
    manifest = _json.dumps({"stages": [{"name": "data_prep", "command": ["python", "p.py"]},
                                       {"name": "train", "command": ["python", "t.py"]}]})
    node = Node(id=5, operator="improve", idea=Idea(operator="improve", params={}),
                files={"solution.py": "X = 1", "looplab_stages.json": manifest})
    dev.repair_from(Idea(operator="improve", params={}, rationale="fix"), node, "boom")
    user = cap[0]["messages"][1]["content"]
    assert "PIPELINE for this node" in user
    assert "data_prep → train → score (operator cmd)" in user


def test_repair_on_operator_stages_task_notes_pipeline_and_refuses_declare_stages(monkeypatch):
    """P12 wiring: on a task whose pipeline is OPERATOR-declared (`cmd.stages`), a repair session's
    prompt restates that pipeline as immutable AND its declare_stages tool refuses (the engine
    ignores the Developer manifest — 'fixing' a stage via it would loop the identical failure)."""
    from looplab.core.models import Node
    rec: list = []
    cap: list = []
    _install_fake_loop(monkeypatch, [], rec, capture=cap)
    t = RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                 edit_surface=["*.py"], protect=[],
                 eval=EvalSpec(stages=[{"name": "train", "command": ["python", "train.py"]},
                                       {"name": "score", "command": ["python", "test.py"]}],
                               metric=_M))
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper(object(), t, plan_decompose=False)
    node = Node(id=5, operator="improve", idea=Idea(operator="improve", params={}),
                files={"solution.py": "X = 1"})
    dev.repair_from(Idea(operator="improve", params={}, rationale="fix"), node, "boom")
    user = cap[0]["messages"][1]["content"]
    assert "OPERATOR-declared, runs verbatim" in user and "train → score" in user
    msg = cap[0]["tools"].execute(
        "declare_stages", {"stages": [{"name": "train", "command": ["python", "t.py"]}]})
    assert msg.startswith("(refused") and "OPERATOR-declared" in msg
