"""Live LLM integration (I2 go-live). Auto-skips unless a local Ollama with the
target model is reachable, so the default suite stays offline. When the model is
present, this drives the *real* loop with the LLM as the Researcher."""
from __future__ import annotations

import json
import urllib.request

import anyio
import pytest

from autornd.config import Settings
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.sandbox import SubprocessSandbox
from autornd.tasks import load_task, make_roles

MODEL = "qwen3:8b"


def _ollama_has(model: str) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            tags = json.loads(r.read())
        return any(model in m.get("name", "") for m in tags.get("models", []))
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ollama_has(MODEL),
                                reason=f"Ollama with {MODEL} not reachable")

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _llm_settings():
    s = Settings()
    s.backend = "llm"
    s.llm_model = MODEL
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
    s.knowledge_dir = str(ROOT / "examples" / "knowledge")
    task = load_task(ROOT / "examples" / "regression_task.json")
    researcher, _ = make_roles(task, s)
    from autornd.agent import ToolUsingResearcher
    from autornd.models import RunState
    assert isinstance(researcher, ToolUsingResearcher)
    idea = researcher.propose(RunState(goal=task.goal, direction="min"), None)
    assert 0.0 <= idea.params.get("degree", 0.0) <= 6.0  # valid, in-bounds Idea


def test_live_researcher_with_skills(tmp_path):
    """Composite tools (knowledge + skills) reach the live Researcher; it returns a
    valid Idea after optionally consulting them."""
    s = _llm_settings()
    s.knowledge_dir = str(ROOT / "examples" / "knowledge")
    s.skills_dir = str(ROOT / "examples" / "skills")
    task = load_task(ROOT / "examples" / "regression_task.json")
    researcher, _ = make_roles(task, s)
    from autornd.agent import CompositeTools, ToolUsingResearcher
    from autornd.models import RunState
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
