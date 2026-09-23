"""The DECOMPOSED repo build hands every phase the decision context the single session always had.

THE DEFECT (review 2026-09-23, Q-2; rendered and metered through the real `LLMRepoDeveloper` phases).
A fresh repo build runs STAGES -> PLAN -> one session per plan STEP whenever the plan has at least
`developer_plan_min_steps` (2) steps — the product default (`developer_plan_decompose=True`). The
single-session implement it replaced (`_run`'s `user`) carries four blocks the engine and the task
own, and the decomposition dropped them on the way to the sessions that now decide and write the code:

  block                                   single session   stages   plan   every step
  WALL-CLOCK BUDGET (`_time_budget_note`)       yes          yes      no       no
  GPU fence + count (`_gpu_footprint_note`)     yes          yes      no       no
  CO-PARENT SOLUTIONS (`co_parent_block`)       yes          no       no       no
  the declared PIPELINE (`_stage_note`)         yes          n/a      no       yes

So on the default path an ENSEMBLE merge's recombination never saw the other lineage at all (the
metered ensemble build and the metered improve build sent byte-identical prompts, 1,621,642
characters each), and the two notes written for the code-writing session — the fence note for a
node whose own training launcher overwrote `CUDA_VISIBLE_DEVICES`, the budget note for a 48-hour
`train` on a 6-hour budget — never reached the session that writes that launcher and that loop.
The plan phase decomposed the change without being told the pipeline the stages phase had just
declared, or which files the node's working set already holds; and each step was shown only its
OWN title while told "later steps handle the rest".

And one contradiction of the same shape: the STAGES phase is read-only (scouts + env inspection +
its `declare_stages` emit), but its system prompt opens "You improve an existing experiment
repository by WRITING code with the write_file and edit_file tools". The plan phase has been handed
`read_only_intro(system)` for exactly that sentence since 2026-09-04 (doc 56 §153: 51 `write_file`
calls from `plan`, all 51 errors); the stages phase never was.

THE FIX is `Settings.developer_phase_context` — a PROMPT change, so ONE flag, OFF at the constructor
and as the class default, a `LEGACY_CONFIG_SNAPSHOT_DEFAULTS` row False, one reader
(`adapters/repo_developer.py::phase_context_enabled`). OFF, every phase renders byte for byte what
it rendered before (pinned below by sha256, computed on the unmodified tree).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import looplab.agents.agent as agent_mod
from looplab.adapters.repo_task import EvalSpec, LLMRepoDeveloper, RepoTask
from looplab.core.models import Idea

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}
_MANIFEST = json.dumps({"stages": [{"name": "train", "command": ["python", "ttrain.py"]}]}, indent=1)
_PLAN = [{"title": "Tune the config", "detail": "set x in config.json"},
         {"title": "Print the metric", "detail": "make ttrain.py print it"}]


def _task():
    # A literal "python", not `sys.executable`: the operator command is quoted into the stages
    # prompt, and the pins below must not depend on where this interpreter lives.
    return RepoTask(id="r", goal="g", direction="max", editable_path=str(FIXTURE),
                    edit_surface=["*.py", "*.json"], protect=[],
                    eval=EvalSpec(command=["python", "ttrain.py"], metric=_M, timeout=3600))


def _dev(**kw):
    return LLMRepoDeveloper(object(), _task(), **kw)


def _idea():
    return Idea(operator="improve", params={"x": 2.0}, rationale="move x toward three",
                footprint={"gpus": 1})


def _parent():
    return SimpleNamespace(
        id=3, files={"config.json": '{"x": 1.0}\n', "looplab_stages.json": _MANIFEST},
        deleted=[], metric=-4.0, operator="improve",
        idea=Idea(operator="improve", params={"x": 1.0}, rationale="x=1"),
        stages=[{"stage": "train", "status": "ok", "seconds": 12.0}], repairs=0, error="")


def _co_parent():
    return SimpleNamespace(
        id=5, files={"config.json": '{"x": 2.5}\n', "ttrain.py": "# CO-PARENT VARIANT\nprint(1)\n"},
        deleted=[], metric=-0.25, operator="improve",
        idea=Idea(operator="improve", params={"x": 2.5}, rationale="x=2.5 with a new entrypoint"),
        stages=[{"stage": "train", "status": "ok", "seconds": 11.0}], repairs=1, error="")


def _install(monkeypatch, capture):
    """Replace the tool loop at its documented seam with a scripted one that RECORDS what each
    phase is handed (after `run_phase` has done its insertions) and answers each emit."""
    import looplab.core.hardware as hardware
    # The live hardware line is the only machine-dependent text in a Developer prompt.
    monkeypatch.setattr(hardware, "operational_attention_points", lambda **_: "<ATTENTION>")

    def fake_loop(client, tools, messages, emit_spec, *, finalize, fallback, **opts):
        name = emit_spec["function"]["name"]
        capture.append({"label": opts.get("phase_label", ""), "emit": name,
                        "messages": [dict(m) for m in messages],
                        "tools": sorted((s.get("function") or {}).get("name", "")
                                        for s in (tools.specs() if tools is not None else []))})
        if name == "declare_stages":
            return finalize({"stages": [{"name": "train", "command": ["python", "ttrain.py"]}]})
        if name == "propose_plan":
            return finalize({"steps": _PLAN})
        n = sum(1 for c in capture if c["emit"] == "done")
        tools.execute("write_file", {"path": f"step{n}.py", "content": f"print({n})\n"})
        return finalize({"summary": "ok"})

    monkeypatch.setattr(agent_mod, "drive_tool_loop", fake_loop)


def _norm(text: str) -> str:
    return str(text or "").replace(str(FIXTURE), "<REPO>")


def _render(monkeypatch, scenario: str, **dev_kw) -> list:
    capture: list = []
    _install(monkeypatch, capture)
    dev = _dev(**dev_kw)
    if scenario == "first":
        dev.implement(_idea())
    elif scenario == "improve":
        dev.implement_from(_idea(), _parent())
    elif scenario == "ensemble":
        dev.implement_from(_idea(), _parent(), co_parents=(_co_parent(),))
    elif scenario == "ensemble-single":
        dev.implement_from(_idea(), _parent(), co_parents=(_co_parent(),))
    elif scenario == "repair":
        dev.repair_from(_idea(), _parent(), "Traceback: KeyError: 'x'")
    else:  # pragma: no cover - a typo in a parametrization
        raise AssertionError(scenario)
    return capture


def _digest(capture: list) -> str:
    rows = [[c["label"], c["emit"], [[m.get("role"), _norm(m.get("content"))] for m in c["messages"]]]
            for c in capture]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def _user(call) -> str:
    return "\n".join(_norm(m.get("content")) for m in call["messages"] if m.get("role") == "user")


def _system(call) -> str:
    return "\n".join(_norm(m.get("content")) for m in call["messages"] if m.get("role") == "system")


# THE HISTORICAL PROMPTS, sha256 over every phase's (label, emit, [(role, content)]) with the fixture
# path and the live hardware line normalized — computed on the tree BEFORE this change (master
# d1ae0e65). OFF must keep rendering exactly these.
_HISTORICAL = {
    "first": "157b997227fbae55da4903478b9c50c8d1ffa1606a274e27f79ff2e2f0fe1b95",
    # IDENTICAL to "ensemble" on the historical tree — the defect in one line: on the default
    # decomposed path the co-parent reached no request at all.
    "improve": "a1449ea5ed224acfdca28d35005a22007564c57ed5b5d453d6dabf5aaaedbf7f",
    "ensemble": "a1449ea5ed224acfdca28d35005a22007564c57ed5b5d453d6dabf5aaaedbf7f",
    "ensemble-single": "70e988ba53214fe9a62bc3b50baf3acf80fda7b2bfb5fdd294f1fc287251356a",
    "repair": "85b0d178f6fb7bf1a1bc3bb03a66ca5900892fb975e4e3ab3cd13817109c73c0",
}


@pytest.mark.parametrize("scenario", sorted(_HISTORICAL))
def test_off_renders_every_phase_byte_for_byte_as_before(monkeypatch, scenario):
    kw = {"plan_decompose": scenario != "ensemble-single"}
    got = _digest(_render(monkeypatch, scenario, **kw))
    assert got == _HISTORICAL[scenario], (
        f"{scenario}: the OFF prompts moved (sha256 {got}); a pre-field snapshot resumes OFF and "
        "must be told exactly what its first half was told")


# ------------------------------------------------------------------ ON: what each phase now carries

_PROMISE = ("You improve an existing experiment repository by WRITING code with the write_file and "
            "edit_file tools")


def _calls(capture, emit):
    return [c for c in capture if c["emit"] == emit]


def test_on_the_plan_and_every_step_are_told_the_wall_clock_and_the_gpu_fence(monkeypatch):
    """The two notes written for the session that WRITES the launcher and the loop reach the
    sessions that write them on the decomposed path. MUTATION: drop the notes from the step's (or
    the plan's) user turn -> red."""
    capture = _render(monkeypatch, "first", phase_context=True)
    plan, steps = _calls(capture, "propose_plan"), _calls(capture, "done")
    assert len(plan) == 1 and len(steps) == len(_PLAN), [c["label"] for c in capture]
    for call in plan + steps:
        user = _user(call)
        assert "WALL-CLOCK BUDGET" in user, call["label"]
        assert "YOUR GPU FENCE ARRIVES IN THE ENVIRONMENT" in user, call["label"]
        assert "THIS NODE IS DECLARED 1 GPU" in user, call["label"]
        # Each exactly once: a note appended twice is re-sent bulk, not context.
        assert user.count("WALL-CLOCK BUDGET") == 1, call["label"]


def test_off_the_decomposed_sessions_lacked_them(monkeypatch):
    """The defect, measured on the OFF bytes (the historical prompts pinned above)."""
    capture = _render(monkeypatch, "ensemble")
    for call in _calls(capture, "propose_plan") + _calls(capture, "done"):
        user = _user(call)
        assert "WALL-CLOCK BUDGET" not in user and "YOUR GPU FENCE" not in user
        assert "CO-PARENT SOLUTIONS" not in user
    stages = _calls(capture, "declare_stages")[0]
    assert _PROMISE in _system(stages)
    assert not {"write_file", "edit_file"} & set(stages["tools"])


def test_on_an_ensemble_every_code_deciding_session_sees_the_other_lineage(monkeypatch):
    """The co-parent's differing file and its trace reach the plan and every step — no tool can
    read a co-parent's files (the scouts read this node's overlay and the seeded repo), so the
    prompt is the only way a step session can recombine it. MUTATION: pass no co-parents to
    `_run_fresh`'s phases -> red."""
    capture = _render(monkeypatch, "ensemble", phase_context=True)
    deciding = _calls(capture, "propose_plan") + _calls(capture, "done")
    assert len(deciding) == 1 + len(_PLAN)
    for call in deciding:
        user = _user(call)
        assert user.count("=== CO-PARENT SOLUTIONS") == 1, call["label"]
        assert "co-parent experiment #5" in user and "# CO-PARENT VARIANT" in user, call["label"]
    # …and an improve (no co-parents) is no longer indistinguishable from the ensemble.
    improve = _render(monkeypatch, "improve", phase_context=True)
    assert _digest(improve) != _digest(capture)
    assert not any("CO-PARENT" in _user(c) for c in improve)


def test_on_the_plan_is_told_the_declared_pipeline_and_the_working_set(monkeypatch):
    """The planner decomposes the change knowing the pipeline the stages phase just declared (the
    steps were already told it) and which files the node's working set holds — on an improve, the
    parent's. MUTATION: drop either line from the plan's user turn -> red."""
    capture = _render(monkeypatch, "improve", phase_context=True)
    plan = _user(_calls(capture, "propose_plan")[0])
    assert "PIPELINE for this node (declared by your STAGES phase): train" in plan
    working = [ln for ln in plan.splitlines()
               if ln.startswith("Files CURRENTLY in this node's working set")]
    assert len(working) == 1, plan
    assert "config.json" in working[0] and "looplab_stages.json" in working[0]
    # The pipeline sentence is the SAME one every step is handed, so plan and steps cannot disagree.
    step_note = [ln for ln in _user(_calls(capture, "done")[0]).splitlines()
                 if ln.startswith("PIPELINE for this node")]
    assert step_note and step_note[0] in plan


def test_on_every_step_sees_the_whole_plan_with_itself_marked(monkeypatch):
    """"Do the minimum for this step; later steps handle the rest" — and until now the step was
    shown only its own title, so "the rest" named nothing it could see. ON, each step session gets
    every title in order with its own marked, exactly once. MUTATION: mark the wrong index, or drop
    the outline -> red."""
    capture = _render(monkeypatch, "first", phase_context=True)
    steps = _calls(capture, "done")
    assert len(steps) == len(_PLAN)
    for idx, call in enumerate(steps, 1):
        user = _user(call)
        assert user.count("THE WHOLE PLAN") == 1, call["label"]
        rows = [ln for ln in user.splitlines() if ln.startswith("  ") and ". " in ln[:6]]
        assert [ln.split(". ", 1)[1].split("   <- ")[0] for ln in rows] == \
            [s["title"] for s in _PLAN], rows
        marked = [ln for ln in rows if ln.endswith("<- THIS STEP")]
        assert marked == [rows[idx - 1]], (idx, rows)
    # OFF: a step sees its own title only (the historical turn).
    off = _calls(_render(monkeypatch, "first"), "done")
    assert not any("THE WHOLE PLAN" in _user(c) for c in off)
    assert _PLAN[1]["title"] not in _user(off[0])


def test_on_the_read_only_stages_phase_is_not_promised_write_tools(monkeypatch):
    """The stages phase gets the SAME `read_only_intro` the plan phase already gets — and the
    sentence it now states is true of the toolset it is actually handed. MUTATION: skip
    `read_only_intro` in `_declare_stages_phase` -> red."""
    from looplab.adapters.repo_developer import read_only_intro
    capture = _render(monkeypatch, "first", phase_context=True)
    stages = _calls(capture, "declare_stages")[0]
    system = _system(stages)
    assert _PROMISE not in system
    assert "NOT in this phase: here you can only READ" in system
    assert not {"write_file", "edit_file"} & set(stages["tools"])
    # Exactly the plan phase's system prompt: one truth for the two read-only phases.
    plan = _calls(capture, "propose_plan")[0]
    assert system == _system(plan) == read_only_intro(_system(_calls(capture, "done")[0]))


def test_on_moves_nothing_on_the_paths_that_already_had_the_context(monkeypatch):
    """The single-session implement already carried every block, and a repair has neither a
    stages nor a plan phase: their sessions render byte for byte as before. Only the stages
    phase's system prompt moves on the single-session path. MUTATION: append the new blocks in
    `_run`'s shared `user` -> red."""
    for scenario, kw in (("ensemble-single", {"plan_decompose": False}), ("repair", {})):
        off = _render(monkeypatch, scenario, **kw)
        on = _render(monkeypatch, scenario, phase_context=True, **kw)
        assert _digest(_calls(on, "done")) == _digest(_calls(off, "done")), scenario
    assert _digest(_render(monkeypatch, "repair", phase_context=True)) == _HISTORICAL["repair"]


# ------------------------------------------------------------------ the switch

def test_the_flag_is_on_for_new_runs_and_off_for_a_pre_field_snapshot():
    """MUTATION: drop the LEGACY row -> red."""
    from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                     settings_from_snapshot)
    assert Settings().developer_phase_context is True
    assert LEGACY_CONFIG_SNAPSHOT_DEFAULTS["developer_phase_context"] is False
    legacy = Settings().masked_snapshot()
    for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
        legacy.pop(field, None)
    legacy.pop("config_snapshot_schema", None)
    assert settings_from_snapshot(legacy).developer_phase_context is False
    assert settings_from_snapshot(Settings().masked_snapshot()).developer_phase_context is True


def test_one_reader_and_every_default_off_path_reads_off():
    from looplab.adapters.repo_developer import phase_context_enabled
    from looplab.core.config import Settings
    assert phase_context_enabled(Settings()) is True
    assert phase_context_enabled(Settings(developer_phase_context=False)) is False
    assert phase_context_enabled(object()) is False                 # a duck-typed stub
    assert _dev()._phase_context is False                             # the constructor default
    assert LLMRepoDeveloper.__new__(LLMRepoDeveloper)._phase_context is False   # the class default


@pytest.mark.parametrize("case", ["on", "off", "pre_field_snapshot"])
def test_the_run_settings_reach_the_developer_through_the_factory(case):
    """Settings -> `agents/developer_backends.py::in_house_repo_developer` -> the Developer.
    MUTATION: drop the builder's keyword -> the `on` case is red."""
    from looplab.agents.developer_backends import in_house_repo_developer
    from looplab.core.config import (LEGACY_CONFIG_SNAPSHOT_DEFAULTS, Settings,
                                     settings_from_snapshot)
    if case == "pre_field_snapshot":
        snapshot = Settings(backend="llm").masked_snapshot()
        for field in LEGACY_CONFIG_SNAPSHOT_DEFAULTS:
            snapshot.pop(field, None)
        snapshot.pop("config_snapshot_schema", None)
        settings = settings_from_snapshot(snapshot)
    else:
        settings = Settings(backend="llm", developer_phase_context=(case == "on"))
    dev = in_house_repo_developer(_task(), settings, object(), param_search=False,
                                  established=None)
    assert isinstance(dev, LLMRepoDeveloper)
    assert dev._phase_context is (case == "on")
