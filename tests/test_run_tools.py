"""Run-introspection tools + the richer Researcher digest (context engineering).
Offline — synthetic RunState + a fake chat client, no model needed."""
from __future__ import annotations

import json
import math

from looplab import digest
from looplab.agent import ToolUsingResearcher
from looplab.config import Settings
from looplab.models import Idea, Node, NodeStatus, RunState
from looplab.roles import LLMResearcher
from looplab.run_tools import DataTools, RunTools
from looplab.tasks import make_roles
from looplab.toytask import ToyTask


def _st() -> RunState:
    st = RunState(goal="minimize loss", direction="min")
    st.nodes = {
        0: Node(id=0, operator="draft", code="print(0)",
                idea=Idea(operator="draft", params={"x": 0.0, "y": 0.0}, theme="seed"),
                metric=10.0, status=NodeStatus.evaluated),
        1: Node(id=1, parent_ids=[0], operator="improve", code="print(1)",
                idea=Idea(operator="improve", params={"x": 2.0, "y": 1.0}, theme="hpo",
                          rationale="move toward the optimum"),
                metric=4.0, status=NodeStatus.evaluated),
        2: Node(id=2, parent_ids=[1], operator="improve",
                idea=Idea(operator="improve", params={"x": 3.0, "y": -1.0}, theme="hpo"),
                metric=1.0, status=NodeStatus.evaluated),
        3: Node(id=3, operator="draft",
                idea=Idea(operator="draft", params={"x": 9.0, "y": 9.0}, theme="seed"),
                status=NodeStatus.failed, error_reason="crash", error="boom"),
    }
    st.best_node_id = 2
    return st


# --------------------------------------------------------------------------- digest
def test_param_distance_matches_old_ndist():
    def _ndist(a, b):
        keys = set(a) & set(b)
        if not keys:
            return float("inf")
        return math.sqrt(sum((a[k] - b[k]) ** 2 for k in keys)) / math.sqrt(len(keys))

    a, b = {"x": 1.0, "y": 2.0}, {"x": 3.0, "y": -1.0}
    assert digest.param_distance(a, b) == _ndist(a, b)
    assert digest.param_distance({"x": 1.0}, {"z": 2.0}) == float("inf")


def test_experiments_digest_content_and_cap():
    st = _st()
    d = digest.experiments_digest(st)
    assert "Strongest" in d and "#2" in d                 # winners listed
    assert "fail" in d.lower() and "crash" in d           # failure surfaced to avoid repeating
    assert "hpo" in d                                     # theme map
    capped = digest.experiments_digest(st, char_cap=40)
    assert len(capped) <= 42                              # hard budget honored
    assert digest.experiments_digest(RunState()) == ""    # empty run → no digest


# --------------------------------------------------------------------------- RunTools
def test_run_tools_read_and_rank():
    rt = RunTools()
    rt.bind_state(_st())
    names = {f["function"]["name"] for f in rt.specs()}
    assert {"list_experiments", "read_experiment", "read_code", "find_analogous", "list_themes"} <= names

    best = rt.execute("list_experiments", {"sort": "best", "limit": 2})
    assert best.index("#2") < best.index("#1")            # min: lowest metric first
    assert "#0" in rt.execute("list_experiments", {"sort": "worst", "limit": 1})

    rd = rt.execute("read_experiment", {"node_id": 1})
    assert "operator=improve" in rd and "metric=4" in rd and "optimum" in rd
    assert "print(1)" in rt.execute("read_code", {"node_id": 1})

    ana = rt.execute("find_analogous", {"node_id": 2, "k": 2})
    assert "#1" in ana                                    # node 1 is nearest to node 2
    themes = rt.execute("list_themes", {})
    assert "hpo" in themes and "2 experiment" in themes
    assert "no experiment" in rt.execute("read_experiment", {"node_id": 99}).lower()


def test_run_tools_unbound_is_safe():
    assert "unavailable" in RunTools().execute("list_themes", {}).lower()


# --------------------------------------------------------------------------- DataTools
class _FakeTask:
    def columns(self):
        return {"f0": [1.0, 2.0, 3.0], "label": [0.0, 1.0, 0.0]}

    def assets(self):
        return {"train.json": '{"X": [[1, 2]], "y": [0]}'}


def test_data_tools_graceful_and_with_data():
    st = _st()
    bare = DataTools(object())
    bare.bind_state(st)
    assert "no structured schema" in bare.execute("data_schema", {}).lower()
    assert "no data assets" in bare.execute("read_asset", {}).lower()

    dt = DataTools(_FakeTask())
    dt.bind_state(st)
    sch = dt.execute("data_schema", {})
    assert "f0" in sch and "label" in sch
    assert "train.json" in dt.execute("read_asset", {})
    assert "X" in dt.execute("read_asset", {"name": "train.json"})
    assert "no data profile" in dt.execute("data_profile", {}).lower()


# --- schema/profile DERIVED from a tabular asset when the task declares no columns() -----------
class _CsvTask:
    """A task with no columns(), only a raw train.csv asset — like mlebench_real."""
    def assets(self):
        return {"train.csv": "id,height,city,target\n1,1.8,NY,0\n2,1.6,LA,1\n3,,NY,0\n",
                "test.csv": "id,height,city\n9,1.7,LA\n"}


def test_data_schema_inferred_from_csv_when_no_columns():
    dt = DataTools(_CsvTask())
    dt.bind_state(_st())
    sch = dt.execute("data_schema", {})
    assert "inferred from train.csv" in sch          # used the training table, not test.csv
    assert "height (numeric)" in sch                 # numeric column inferred
    assert "city (categorical)" in sch               # categorical column inferred
    assert "target" in sch


def test_data_profile_computed_from_csv_when_unrecorded():
    dt = DataTools(_CsvTask())
    dt.bind_state(_st())                              # _st() has no data_profile -> fall back to CSV
    prof = dt.execute("data_profile", {})
    assert "train.csv" in prof
    assert "height: numeric" in prof and "min=1.6" in prof and "max=1.8" in prof
    assert "missing=0.33" in prof                    # 1 of 3 height values is blank
    assert "city: categorical" in prof and "unique=2" in prof


# --------------------------------------------------------------------------- agent loop
class _FakeChatClient:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.turns = []

    def chat(self, messages, tools, tool_choice="auto"):
        self.turns.append(list(messages))
        return self.scripted.pop(0)


def _tool_call(name, args):
    return {"content": "", "tool_calls": [
        {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_agent_uses_run_tool_then_emits():
    st = _st()
    client = _FakeChatClient([
        _tool_call("read_experiment", {"node_id": 2}),                 # consult the best node
        _tool_call("emit", {"operator": "improve",
                            "params": {"x": 3.0, "y": -1.0}, "rationale": "refine the leader"}),
    ])
    r = ToolUsingResearcher(client, RunTools(),
                            bounds={"x": (-10.0, 10.0), "y": (-10.0, 10.0)})
    idea = r.propose(st, st.nodes[2])
    assert idea.operator == "improve"
    # bind_state ran: the tool returned the REAL node-2 detail, fed back as a tool message.
    tool_msgs = [m for m in client.turns[1] if m.get("role") == "tool"]
    assert tool_msgs and "operator=improve" in tool_msgs[0]["content"]


# --------------------------------------------------------------------------- wiring
def test_make_roles_wraps_tool_researcher_by_default():
    researcher, _ = make_roles(ToyTask(), Settings(backend="llm", unified_agent=False))
    assert isinstance(researcher, ToolUsingResearcher)


def test_make_roles_flag_off_is_plain_researcher():
    researcher, _ = make_roles(ToyTask(), Settings(backend="llm", unified_agent=False,
                                                   researcher_tools=False))
    assert isinstance(researcher, LLMResearcher)
