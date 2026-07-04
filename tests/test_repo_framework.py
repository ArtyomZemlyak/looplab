"""RepoTask Phase 2 — framework mode: cli_overrides (Researcher params -> CLI overrides,
no code edits) and eval profiles (smoke/full; confirm forces full)."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio

from looplab.runtime.command_eval import build_command
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoParamResearcher, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"


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
    # unknown/None profile -> falls back to smoke
    assert build_command(es, {}, None)[0] == ["python", "ttrain_cli.py", "steps=10"]


def test_build_command_no_overrides_when_style_none():
    es = {"command": ["python", "x.py"], "params_style": "none", "timeout": 60}
    assert build_command(es, {"x": 1.0}, None) == (["python", "x.py"], 60)


def test_param_researcher_proposes_in_bounds_and_tags_profile():
    r = RepoParamResearcher({"x": (-5.0, 5.0)}, seed=0)
    idea = r.propose(None, None)
    assert -5.0 <= idea.params["x"] <= 5.0 and idea.eval_profile == "smoke"


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
