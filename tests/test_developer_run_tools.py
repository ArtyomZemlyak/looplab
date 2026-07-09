"""D-context: the in-house repo Developer gets the SAME read-only run-introspection tools the
Researcher has (own run + gated sibling/all-runs), so it can read how a prior/merged/failed node was
implemented, what it scored, and why it broke. Offline — synthetic RunState + a scripted client.

Covers: the failure banner on read_code; `_run_intro_tools`/`_run_tools_prompt` gating (bound+enabled
vs unbound/disabled); the tools actually reaching the model's tool specs in a Developer session; and
`make_roles` wiring the flags through.
"""
from __future__ import annotations

from looplab.core.models import Idea, Node, NodeStatus, RunState
from looplab.tools.run_tools import RunTools


def _st() -> RunState:
    st = RunState(goal="minimize loss", direction="min", run_id="runA", task_id="taskX")
    st.nodes = {
        0: Node(id=0, operator="draft", code="print('zero')",
                idea=Idea(operator="draft", params={"x": 0.0}, theme="seed"),
                metric=10.0, status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve", code="print('one')",
                idea=Idea(operator="improve", params={"x": 2.0}, rationale="closer"),
                metric=4.0, status=NodeStatus.evaluated),
        3: Node(id=3, operator="draft", code="raise RuntimeError('boom')",
                idea=Idea(operator="draft", params={"x": 9.0}),
                status=NodeStatus.failed, error_reason="crash", error="Traceback: boom"),
    }
    st.best_node_id = 1
    return st


# --------------------------------------------------------------------- read_code banner
def test_read_code_flags_a_failed_node():
    rt = RunTools()
    rt.bind_state(_st(), None)
    failed = rt.execute("read_code", {"node_id": 3})
    assert "FAILED" in failed and "read_logs(3)" in failed        # the broken version is flagged
    assert "raise RuntimeError" in failed                          # the actual (failing) code shown
    ok = rt.execute("read_code", {"node_id": 1})
    assert "FAILED" not in ok and "metric=" in ok                  # a healthy node shows its metric


# --------------------------------------------------------------------- dev tool assembly
def _bare_dev(*, run_dir=None, run_tools=True, cross=True, allr=True):
    """A __new__-built LLMRepoDeveloper (bypass __init__) with just the D-context attrs set —
    the same construction style tests/test_parent_aware_developer.py uses."""
    from looplab.adapters.repo_task import LLMRepoDeveloper
    dev = LLMRepoDeveloper.__new__(LLMRepoDeveloper)
    dev._editables = []
    dev._run_dir = run_dir
    dev._run_tools = run_tools
    dev._cross_run_tools = cross
    dev._all_runs_tools = allr
    dev._bound_state = None
    dev._bound_parent = None
    return dev


def _tool_names(providers) -> set:
    return {s["function"]["name"] for p in providers for s in p.specs()}


def test_run_intro_tools_empty_until_bound():
    dev = _bare_dev(run_dir="/runs/runA")
    assert dev._run_intro_tools() == []                # unbound → no tools (parity)
    assert dev._run_tools_prompt() == ""
    dev.bind_state(_st(), None)
    names = _tool_names(dev._run_intro_tools())
    assert {"read_experiment", "read_code", "read_logs", "list_experiments"} <= names   # own run
    assert {"list_all_runs", "read_run_code", "read_run_experiment"} <= names            # all-runs
    assert {"list_sibling_runs", "read_sibling_code"} <= names                           # siblings


def test_run_intro_tools_respect_gates():
    # own-run reader only when cross/all are off — even with a run_dir.
    dev = _bare_dev(run_dir="/runs/runA", cross=False, allr=False)
    dev.bind_state(_st(), None)
    names = _tool_names(dev._run_intro_tools())
    assert "read_experiment" in names
    assert "list_all_runs" not in names and "list_sibling_runs" not in names
    # master switch off → nothing at all.
    off = _bare_dev(run_dir="/runs/runA", run_tools=False)
    off.bind_state(_st(), None)
    assert off._run_intro_tools() == [] and off._run_tools_prompt() == ""
    # no run_dir (factory/unit build) → own-run reader, but no cross-run tools.
    nod = _bare_dev(run_dir=None)
    nod.bind_state(_st(), None)
    names2 = _tool_names(nod._run_intro_tools())
    assert "read_experiment" in names2 and "list_all_runs" not in names2


def test_run_tools_prompt_mentions_the_readers():
    dev = _bare_dev(run_dir="/runs/runA")
    dev.bind_state(_st(), None)
    block = dev._run_tools_prompt()
    assert "PAST EXPERIMENTS" in block
    assert "read_logs(node_id)" in block and "read_code(node_id)" in block
    assert "list_all_runs" in block                    # the cross-run sentence is appended


# --------------------------------------------------------------------- end-to-end session
class _DoneClient:
    """Scripted client: records the tool specs it was offered, then immediately calls `done`."""
    def __init__(self):
        self.offered: list[str] = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.offered = [t["function"]["name"] for t in tools]
        self.last_messages = messages
        return {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "done", "arguments": '{"summary":"ok"}'}}]}


def test_developer_session_offers_the_run_tools():
    """A Developer implement session (empty editables → single terminal session) exposes the run
    tools to the model AND names them in the system prompt."""
    dev = _bare_dev(run_dir="/runs/runA")
    dev.client = _DoneClient()
    dev.brief = "brief"
    dev.last_files, dev.last_deleted = {}, []
    dev.loop_opts = {}
    dev._surface, dev._protected, dev._prefixes = ["**/*.py"], set(), ()
    dev._recipes = lambda: "(none)"
    dev._results_context = lambda: ""
    dev._repo_context = lambda: "(repo)"
    dev.bind_state(_st(), None)
    dev.implement(Idea(operator="draft", params={}, rationale="build it"))
    assert {"read_experiment", "read_code", "read_logs", "list_all_runs"} <= set(dev.client.offered)
    sys_prompt = dev.client.last_messages[0]["content"]
    assert "PAST EXPERIMENTS" in sys_prompt


# --------------------------------------------------------------------- make_roles wiring
def test_make_roles_wires_developer_run_tools(tmp_path):
    from looplab.adapters.tasks import make_roles
    from looplab.core.config import Settings
    from looplab.adapters.repo_task import RepoTask, LLMRepoDeveloper

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "train.py").write_text("print('hi')\n")
    task = RepoTask(goal="improve", direction="max", editable_path=str(repo),
                    eval={"command": ["python", "test_looplab.py"],
                          "metric": {"kind": "stdout_json", "key": "metric"}})
    # Split-role path (unified_agent off) so `developer` IS the LLMRepoDeveloper we wired.
    settings = Settings(backend="llm", llm_base_url="http://x/v1", llm_model="m",
                        unified_agent=False)
    run_dir = tmp_path / "runs" / "runA"
    _researcher, developer = make_roles(task, settings, str(run_dir))
    assert isinstance(developer, LLMRepoDeveloper)
    assert developer._run_tools is True                # developer_run_tools default on
    assert developer._cross_run_tools is True and developer._all_runs_tools is True
    assert developer._run_dir == str(run_dir)
    # And the opt-out disables it.
    off = Settings(backend="llm", llm_base_url="http://x/v1", llm_model="m",
                   unified_agent=False, developer_run_tools=False)
    _r2, dev2 = make_roles(task, off, str(run_dir))
    assert dev2._run_tools is False


def test_bind_developer_walks_wrappers():
    """Engine._bind_developer reaches the inner LLMRepoDeveloper even through a UnifiedAgent-style
    wrapper (the default config wraps both roles into one object exposing `.developer`)."""
    from looplab.engine.orchestrator import Engine

    class _Inner:
        def __init__(self):
            self.bound = None

        def bind_state(self, state, parent=None):
            self.bound = (state, parent)

    class _Wrap:                    # mimics UnifiedAgent / ValidatingDeveloper (holds the real dev)
        def __init__(self, inner):
            self.developer = inner
            self.inner = inner

    inner = _Inner()
    eng = Engine.__new__(Engine)
    eng.developer = _Wrap(inner)
    st = _st()
    eng._bind_developer(st, None)
    assert inner.bound is not None and inner.bound[0] is st
