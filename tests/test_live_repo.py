"""Live RepoTask integration. LLM-AGNOSTIC (runs against whatever LLM the environment configures —
Ollama, an OpenAI-compatible endpoint, …). Covers the live paths the offline suite can't: the LLM
hyperparameter Researcher over a real framework (P2), live onboarding where the Developer WRITES the
metric adapter (P3), and the agentic Researcher reading the editable repo via RepoTools (#3)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import pytest

from looplab.core.config import Settings
from looplab.core.models import RunState
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.adapters.repo_task import EvalSpec, RepoTask
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import make_roles
from tests.live.scenarios import live_llm_reachable

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}

# `live` marker: selection only (`-m "not live"`); the skipif is the enforcement gate. Universal across
# LLMs: opt in with LOOPLAB_LIVE_SCENARIOS=1 and any reachable configured LLM (not a hardcoded model).
pytestmark = [pytest.mark.live, pytest.mark.skipif(
    not (os.environ.get("LOOPLAB_LIVE_SCENARIOS") and live_llm_reachable()),
    reason="set LOOPLAB_LIVE_SCENARIOS=1 with a reachable configured LLM to run the live repo tests")]


def _llm() -> Settings:
    s = Settings()                  # the CONFIGURED LLM (llm_model / llm_base_url), whatever it is
    s.backend = "llm"
    return s


def test_live_framework_param_search(tmp_path):
    """P2: the live LLM Researcher proposes hyperparameters (x in [-5,5]) that drive an existing
    framework via cli_overrides; the loop runs and produces a real metric (max at x=3)."""
    t = RepoTask(id="fw", direction="max", editable_path=str(FIXTURE), edit_surface=["*.json"],
                 params={"x": (-5.0, 5.0)},
                 eval=EvalSpec(command=[sys.executable, "ttrain_cli.py"],
                               params_style="cli_overrides", metric=_M))
    researcher, developer = make_roles(t, _llm())          # LLMResearcher + NoOp (params mode)
    eng = Engine(tmp_path / "run", task=t, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=5), timeout=120.0)
    state = anyio.run(eng.run)
    assert state.finished
    assert state.best() is not None and state.best().metric is not None


def test_live_onboarding_writes_adapter_and_runs(tmp_path):
    """P3: the Developer WRITES a metric adapter for the framework via the live model; autonomous
    trust auto-confirms + freezes it; the loop then runs the operator's command and reads the
    metric through the agent-written adapter."""
    t = RepoTask(id="onb", goal="maximize the eval metric", direction="max",
                 editable_path=str(FIXTURE), edit_surface=["*.json"], protect=["ttrain.py"],
                 onboard=True, onboard_command=[sys.executable, "ttrain.py"], eval=None)
    researcher, developer = make_roles(t, _llm())
    onboarder = t.make_onboarder(_llm())
    assert onboarder is not None
    eng = Engine(tmp_path / "run", task=t, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 onboarder=onboarder, eval_trust_mode="autonomous", timeout=120.0)
    state = anyio.run(eng.run)
    assert state.spec_confirmed and state.finished          # adapter proposed, frozen, run done
    # the frozen adapter was written into the workspace as a protected asset
    assert "LOOPLAB_adapter.py" in eng._repo_spec.get("protected_names", [])


def test_live_repotools_researcher_reads_repo(tmp_path):
    """#3: a code-edit RepoTask wires the agentic Researcher with read-only RepoTools over the
    editable repo; the live model may grep/read the source, then emits a valid Idea."""
    t = RepoTask(id="rt", goal="improve the experiment to raise the metric", direction="max",
                 editable_path=str(FIXTURE), edit_surface=["*.json"], protect=["ttrain.py"],
                 eval=EvalSpec(command=[sys.executable, "ttrain.py"], metric=_M))
    s = _llm(); s.unified_agent = False     # this test asserts the split ToolUsingResearcher wiring
    researcher, _ = make_roles(t, s)
    from looplab.agents.agent import ToolUsingResearcher
    from looplab.tools.knowledge_tools import RepoTools
    assert isinstance(researcher, ToolUsingResearcher)
    provs = getattr(researcher.tools, "providers", [researcher.tools])
    assert any(isinstance(p, RepoTools) for p in provs)     # repo is readable by the proposer
    idea = researcher.propose(RunState(goal=t.goal, direction="max"), None)
    assert idea is not None and idea.operator               # emitted a valid Idea live
