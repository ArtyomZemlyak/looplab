"""Live LLM integration (I2 go-live). LLM-AGNOSTIC: it drives the *real* loop with whatever LLM the
environment configures (Settings.llm_model / llm_base_url — Ollama, an OpenAI-compatible endpoint, …),
gated by the SAME opt-in as the live scenarios so all live tests share one universal gate."""
from __future__ import annotations

import os

import anyio
import pytest

from looplab.core.config import Settings
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.tasks import load_task, make_roles
from tests.live.scenarios import live_llm_reachable

# `live` marker: selection only (`-m "not live"`); the skipif is the enforcement gate. Universal across
# LLMs: opt in with LOOPLAB_LIVE_SCENARIOS=1 and any reachable configured LLM (not a hardcoded model).
pytestmark = [pytest.mark.live, pytest.mark.skipif(
    not (os.environ.get("LOOPLAB_LIVE_SCENARIOS") and live_llm_reachable()),
    reason="set LOOPLAB_LIVE_SCENARIOS=1 with a reachable configured LLM to run the live LLM tests")]

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _llm_settings():
    s = Settings()                  # the CONFIGURED LLM (llm_model / llm_base_url), whatever it is
    s.backend = "llm"
    return s


def test_live_quadratic_loop(tmp_path):
    task = load_task(ROOT / "examples" / "toy_task.json")
    researcher, developer = make_roles(task, _llm_settings())
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=5), timeout=120.0)
    state = anyio.run(eng.run)
    assert state.finished
    best = state.best()
    assert best is not None and best.metric is not None  # the LLM-driven loop produced a result


def test_live_agentic_researcher(tmp_path):
    """The tool-using Researcher runs the real multi-turn tool protocol over Ollama:
    it may call grep/kb_search/read over the knowledge notes, then emits a valid Idea."""
    s = _llm_settings()
    s.unified_agent = False                 # this test asserts the split ToolUsingResearcher wiring
    s.knowledge_dir = str(ROOT / "examples" / "knowledge")
    task = load_task(ROOT / "examples" / "regression_task.json")
    researcher, _ = make_roles(task, s)
    from looplab.agents.agent import ToolUsingResearcher
    from looplab.core.models import RunState
    assert isinstance(researcher, ToolUsingResearcher)
    idea = researcher.propose(RunState(goal=task.goal, direction="min"), None)
    assert 0.0 <= idea.params.get("degree", 0.0) <= 6.0  # valid, in-bounds Idea


def test_live_researcher_with_skills(tmp_path):
    """Composite tools (knowledge + skills) reach the live Researcher; it returns a
    valid Idea after optionally consulting them."""
    s = _llm_settings()
    s.unified_agent = False                 # this test asserts the split ToolUsingResearcher wiring
    s.knowledge_dir = str(ROOT / "examples" / "knowledge")
    s.skills_dir = str(ROOT / "examples" / "skills")
    task = load_task(ROOT / "examples" / "regression_task.json")
    researcher, _ = make_roles(task, s)
    from looplab.agents.agent import CompositeTools, ToolUsingResearcher
    from looplab.core.models import RunState
    assert isinstance(researcher, ToolUsingResearcher)
    assert isinstance(researcher.tools, CompositeTools)
    idea = researcher.propose(RunState(goal=task.goal, direction="min"), None)
    assert 0.0 <= idea.params.get("degree", 0.0) <= 6.0


def test_live_code_loop(tmp_path):
    """The LLM WRITES the solution code (numpy regression on a data.json asset); the
    error-feedback debug operator repairs any failures."""
    task = load_task(ROOT / "examples" / "code_regression_task.json")
    researcher, developer = make_roles(task, _llm_settings())
    eng = Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                 sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=2, max_nodes=4, debug_depth=1), timeout=180.0)
    state = anyio.run(eng.run)
    assert state.finished
    # At least one LLM-written script ran and produced a metric.
    assert any(n.metric is not None for n in state.nodes.values())
