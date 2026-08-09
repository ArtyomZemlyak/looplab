"""RepoTask Phase 2 — framework mode: cli_overrides (Researcher params -> CLI overrides,
no code edits) and eval profiles (smoke/full; confirm forces full)."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

from looplab.runtime.command_eval import build_command
from looplab.core.config import Settings
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, NoOpRepoDeveloper, RepoParamResearcher, RepoTask
from looplab.runtime.sandbox import MAX_TIMEOUT_S, RunResult, SubprocessSandbox
from looplab.adapters.tasks import make_roles, validate_task

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}


# ------------------------------ build_command (unit) -------------------------

def test_build_command_cli_overrides_and_profiles():
    es = {
        "command": ["python", "ttrain_cli.py"],
        "params_style": "cli_overrides",
        "timeout": 600.0,
        "profiles": {"smoke": {"overrides": ["steps=10"], "timeout": 30},
                     "full": {"overrides": ["steps=500"], "timeout": 900}},
    }
    cmd, t = build_command(es, {"x": 2.0, "lr": 0.01}, "smoke")
    assert cmd == ["python", "ttrain_cli.py", "steps=10", "x=2", "lr=0.01"]  # int-float -> int
    assert t == 30
    cmd_full, t_full = build_command(es, {"x": 2.0}, "full")
    assert "steps=500" in cmd_full and t_full == 900
    # No explicit profile means the conventional search default: smoke when it is declared.
    assert build_command(es, {}, None)[0] == ["python", "ttrain_cli.py", "steps=10"]


def test_build_command_no_overrides_when_style_none():
    es = {"command": ["python", "x.py"], "params_style": "none", "timeout": 60}
    assert build_command(es, {"x": 1.0}, None) == (["python", "x.py"], 60)


def test_param_researcher_proposes_in_bounds_and_tags_profile():
    r = RepoParamResearcher({"x": (-5.0, 5.0)}, seed=0)
    idea = r.propose(None, None)
    assert -5.0 <= idea.params["x"] <= 5.0 and idea.eval_profile == "smoke"


@pytest.mark.parametrize(
    ("profiles", "expected_error"),
    [
        (None, "eval.profiles must be an object keyed by profile name"),
        ({"   ": {}}, "eval profile names must be non-empty strings"),
        ({1: {}}, "eval profile names must be non-empty strings"),
        ({"quick": []}, "eval profile 'quick' must be an object"),
        ({"quick": {"description": "cheap"}},
         "eval profile 'quick' has unsupported keys: 'description'"),
        ({"quick": {"overrides": "steps=1"}},
         "eval profile 'quick' overrides must be a list of strings"),
        ({"quick": {"overrides": ["steps=1", 2]}},
         "eval profile 'quick' overrides must be a list of strings"),
        ({"quick": {"timeout": True}},
         "eval profile 'quick' timeout must be a finite positive number"),
        ({"quick": {"timeout": 0}},
         "eval profile 'quick' timeout must be a finite positive number"),
        ({"quick": {"timeout": -1}},
         "eval profile 'quick' timeout must be a finite positive number"),
        ({"quick": {"timeout": float("inf")}},
         "eval profile 'quick' timeout must be a finite positive number"),
        ({"quick": {"timeout": float("nan")}},
         "eval profile 'quick' timeout must be a finite positive number"),
        ({"quick": {"timeout": 10 ** 400}},
         "eval profile 'quick' timeout must be a finite positive number"),
        ({"quick": {"timeout": "30"}},
         "eval profile 'quick' timeout must be a finite positive number"),
    ],
    ids=[
        "outer-object", "blank-name", "string-name", "profile-object", "closed-keys",
        "overrides-list", "overrides-strings", "timeout-bool", "timeout-zero",
        "timeout-negative", "timeout-infinite", "timeout-nan", "timeout-overflow",
        "timeout-numeric",
    ],
)
def test_eval_profiles_fail_loudly_at_pydantic_boundary(profiles, expected_error):
    with pytest.raises(ValueError) as exc_info:
        EvalSpec(command=[sys.executable, "-c", "pass"], metric=_M, profiles=profiles)
    assert expected_error in str(exc_info.value)


def _task_with_legacy_eval_profiles() -> dict:
    """A snapshot shape accepted before EvalSpec gained its strict authoring validator."""
    return {
        "kind": "repo",
        "id": "legacy-profile-snapshot",
        "goal": "test task loading",
        "editable_path": str(FIXTURE),
        "eval": {
            "command": [sys.executable, "-c", "pass"],
            "metric": _M,
            "profiles": {
                "tuple-zero": {
                    "overrides": ("steps=1",),
                    "timeout": 0,
                    "description": "legacy UI metadata",
                },
                "string-invalid": {
                    "overrides": "xy",
                    "timeout": "not-a-timeout",
                    "legacy": True,
                },
            },
        },
    }


def test_fresh_task_rejects_legacy_eval_profile_shape():
    """New submissions use the closed profile schema even when an old snapshot once allowed it."""
    with pytest.raises(ValueError) as exc_info:
        validate_task(_task_with_legacy_eval_profiles())
    assert "eval profile 'tuple-zero' has unsupported keys: 'description'" in str(exc_info.value)


def test_existing_run_grandfathers_legacy_eval_profiles_without_reinterpretation():
    """Reload keeps the recorded nested values and the defensive dispatch semantics they already had."""
    task = validate_task(_task_with_legacy_eval_profiles(), existing_run=True)
    profiles = task.eval.profiles

    assert profiles["tuple-zero"] == {
        "overrides": ("steps=1",),
        "timeout": 0,
        "description": "legacy UI metadata",
    }
    assert profiles["string-invalid"] == {
        "overrides": "xy",
        "timeout": "not-a-timeout",
        "legacy": True,
    }

    tuple_command, tuple_timeout = build_command(task.eval_spec(), {}, "tuple-zero")
    string_command, string_timeout = build_command(task.eval_spec(), {}, "string-invalid")
    assert tuple_command[-1:] == ["steps=1"] and tuple_timeout == 0
    assert string_command[-2:] == ["x", "y"] and string_timeout == 600


@pytest.mark.parametrize("profiles, expected", [
    ({"smoke": {"timeout": 10}}, "smoke"),
    ({"preview": {"timeout": 10}}, None),
    ({}, None),
])
def test_offline_repo_composition_emits_only_a_declared_smoke_profile(profiles, expected):
    task = RepoTask(
        id="offline-profile-contract", goal="tune", editable_path=str(FIXTURE),
        params={"x": (0.0, 2.0)},
        eval=EvalSpec(
            command=[sys.executable, "-c", "pass"], params_style="cli_overrides", metric=_M,
            profiles=profiles,
        ),
    )
    researcher, _ = task.build_roles()
    assert researcher.propose(None, None).eval_profile == expected


class _ProfileEmissionClient:
    """Recording structured-output client for the prompt -> Idea -> dispatch contract."""

    def __init__(self, profile):
        self.profile = profile
        self.messages = None
        self.schema = None

    def complete_tool(self, messages, json_schema):
        self.messages = messages
        self.schema = json_schema
        return {
            "operator": "improve",
            "params": {"x": 1.0},
            "rationale": "Use the selected evaluation depth.",
            "eval_profile": self.profile,
            "concept_mode": "full",
        }

    def complete_text(self, _messages):
        raise AssertionError("the valid tool emission must not need a text fallback")


def _llm_profile_task() -> RepoTask:
    return RepoTask(
        id="profile-contract", goal="select evaluation depth", direction="max",
        editable_path=str(FIXTURE), params={"x": (0.0, 2.0)},
        eval=EvalSpec(
            command=[sys.executable, "-c", "print('not executed in this test')"],
            params_style="cli_overrides", metric=_M, timeout=90,
            profiles={
                "smoke": {"overrides": ["mode=smoke"], "timeout": 11},
                "quick": {"overrides": ["mode=quick"], "timeout": 22},
            },
        ),
    )


@pytest.mark.parametrize(
    ("emitted_profile", "expected_tail", "expected_timeout"),
    [
        ("quick", ["mode=quick", "x=1"], 22.0),
        (None, ["mode=smoke", "x=1"], 11.0),
        ("invented", ["x=1"], 90.0),
    ],
)
def test_llm_eval_profile_prompt_emission_and_command_dispatch(
        tmp_path, monkeypatch, emitted_profile, expected_tail, expected_timeout):
    """The exact task profiles reach the LLM and its emission drives the real eval dispatcher.

    ``None`` keeps the conventional smoke default; an explicit unknown value stays compatible by
    running the base command/timeout rather than silently selecting a cheap named profile.
    """
    task = _llm_profile_task()
    client = _ProfileEmissionClient(emitted_profile)
    researcher, developer = task.llm_roles(client)
    from looplab.core.models import Node, RunState

    idea = researcher.propose(RunState(goal=task.goal, direction=task.direction), None)
    assert idea.eval_profile == emitted_profile
    assert isinstance(client.schema, dict) and "eval_profile" in client.schema["properties"]

    profile_hint = researcher.space_hint.split("Evaluation profile contract", 1)[1]
    assert all(f'"{name}"' in profile_hint for name in ("smoke", "quick"))
    assert '"full"' not in profile_hint and '"invented"' not in profile_hint
    assert "only to one of these exact declared names" in profile_hint
    assert "never invent one" in profile_hint
    assert "effective runtime timeout 11 seconds" in profile_hint
    assert "effective runtime timeout 22 seconds" in profile_hint
    assert any(researcher.space_hint in str(message.get("content", ""))
               for message in client.messages)

    engine = Engine(
        tmp_path / "run", task=task, researcher=researcher, developer=developer,
        sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
        auto_install_deps=False,
    )
    seen = {}

    def _capture(command, _cwd, timeout, _metric, *_args, **_kwargs):
        seen["command"] = command
        seen["timeout"] = timeout
        return RunResult(0, '{"metric": 1}', "", 1.0, False)

    monkeypatch.setattr("looplab.runtime.command_eval.run_command_eval", _capture)
    workdir = tmp_path / "eval-workdir"
    workdir.mkdir()
    result = engine._run_eval(
        Node(id=0, operator=idea.operator, idea=idea, code=""), workdir)

    assert result.metric == 1.0
    assert seen["command"][-len(expected_tail):] == expected_tail
    assert seen["timeout"] == expected_timeout


def test_repo_task_without_profiles_does_not_offer_eval_profile_to_researcher():
    task = RepoTask(
        id="no-profiles", goal="improve", editable_path=str(FIXTURE),
        eval=EvalSpec(command=[sys.executable, "-c", "pass"], metric=_M),
    )
    researcher, _ = task.llm_roles(object())
    assert "Evaluation profile contract" not in researcher.space_hint
    assert "`eval_profile`" not in researcher.space_hint


def test_code_edit_repo_researcher_gets_its_custom_profile_names_too():
    task = RepoTask(
        id="code-profiles", goal="improve code", editable_path=str(FIXTURE),
        eval=EvalSpec(
            command=[sys.executable, "-c", "pass"], metric=_M,
            profiles={"audit": {"overrides": ["--strict"], "timeout": 45}},
        ),
    )
    researcher, _ = task.llm_roles(object())
    profile_hint = researcher.space_hint.split("Evaluation profile contract", 1)[1]
    assert '"audit"' in profile_hint and "--strict" in profile_hint and "45 seconds" in profile_hint
    assert '"smoke"' not in profile_hint and '"full"' not in profile_hint
    assert "emitting null) uses the base eval" in profile_hint


def test_eval_profile_hint_reports_the_same_effective_timeout_as_dispatch():
    task = RepoTask(
        id="capped-profile-hint", goal="choose an eval", editable_path=str(FIXTURE),
        eval=EvalSpec(
            command=[sys.executable, "-c", "pass"], metric=_M,
            timeout=MAX_TIMEOUT_S * 3,
            profiles={
                "long": {"overrides": ["mode=long"], "timeout": MAX_TIMEOUT_S * 2},
                "inherits-base": {},
            },
        ),
    )
    researcher, _ = task.llm_roles(object())
    profile_hint = researcher.space_hint.split("Evaluation profile contract", 1)[1]

    for profile in ("long", "inherits-base"):
        _command, runtime_timeout = build_command(task.eval_spec(), {}, profile)
        assert runtime_timeout == MAX_TIMEOUT_S
    assert profile_hint.count(
        f"effective runtime timeout {MAX_TIMEOUT_S:g} seconds") == 2
    assert f"runtime cap of {MAX_TIMEOUT_S:g} seconds" in profile_hint
    assert f"{MAX_TIMEOUT_S * 2:g} seconds" not in profile_hint


# ------------------------- end-to-end hyperparameter search ------------------

def _framework_task() -> RepoTask:
    return RepoTask(
        id="fw", goal="tune x to maximize metric (max at x=3)", direction="max", seed=1,
        editable_path=str(FIXTURE), edit_surface=["*.json"], protect=["ttrain_cli.py"],
        params={"x": (-5.0, 5.0)},
        eval=EvalSpec(command=[sys.executable, "ttrain_cli.py"], params_style="cli_overrides",
                      metric={"kind": "stdout_json", "key": "metric"},
                      profiles={"smoke": {"overrides": ["steps=10"], "timeout": 60},
                                "full": {"overrides": ["steps=200"], "timeout": 60}}))


def test_engine_hyperparameter_search_via_cli_overrides(tmp_path):
    t = _framework_task()
    researcher, developer = t.build_roles()      # RepoParamResearcher + NoOp (no code edits)
    engine = Engine(tmp_path / "run", task=t, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=4, max_nodes=12))
    state = anyio.run(engine.run)
    assert state.finished
    best = state.best()
    # The framework was driven purely by CLI overrides (no file edits) and the search moved
    # x toward the optimum (metric -> 0 at x=3).
    assert best is not None and best.metric > -1.0
    assert abs(best.idea.params["x"] - 3.0) < 1.0


def test_confirm_phase_uses_full_profile(tmp_path):
    # With confirmation enabled the leaders are re-evaluated; the run completes and selects
    # a confirmed best. (Confirm forces the "full" profile via _run_eval.)
    t = _framework_task()
    researcher, developer = t.build_roles()
    engine = Engine(tmp_path / "run", task=t, researcher=researcher, developer=developer,
                    sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=3, max_nodes=6),
                    confirm_top_k=2, confirm_seeds=2)
    state = anyio.run(engine.run)
    assert state.finished and state.best() is not None


# --- cli_overrides param-search must NOT wire the editing agent; build_command profile handling ----

# #3 — cli_overrides param-search must NOT wire the editing agent even with a preset backend
def test_make_roles_param_search_ignores_agent_backend():
    s = Settings()
    s.backend, s.developer_backend, s.unified_agent = "llm", "opencode", False
    t = RepoTask(id="p", direction="max", editable_path=str(FIXTURE), protect=["ttrain_cli.py"],
                 params={"x": (-5.0, 5.0)},
                 eval=EvalSpec(command=[sys.executable, "ttrain_cli.py"],
                               params_style="cli_overrides", metric=_M))
    _, dev = make_roles(t, s)
    assert isinstance(dev, NoOpRepoDeveloper)           # baseline, not a ValidatingDeveloper


# #4 — confirm's requested 'full' profile, when undefined, runs the BASE command (full eval),
#      never silently the cheap 'smoke' overrides
def test_build_command_missing_requested_profile_uses_base():
    es = {"command": ["python", "t.py"], "timeout": 999,
          "profiles": {"smoke": {"overrides": ["s=1"], "timeout": 5}}}
    cmd, t = build_command(es, {}, "full")             # 'full' not defined
    assert cmd == ["python", "t.py"] and t == 999      # base command + base timeout, not s=1/5


# #10 — a configured timeout of 0 is honored (not coerced to the 600 default)
def test_build_command_zero_timeout_honored():
    es = {"command": ["python", "t.py"], "profiles": {"smoke": {"overrides": [], "timeout": 0}}}
    assert build_command(es, {}, "smoke")[1] == 0
