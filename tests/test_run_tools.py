"""Run-introspection tools + the richer Researcher digest (context engineering).
Offline — synthetic RunState + a fake chat client, no model needed."""
from __future__ import annotations

import json
import math

from looplab import digest
from looplab.agent import ToolUsingResearcher
from looplab.config import Settings
from looplab.models import Idea, Node, NodeStatus, RunState, Trial
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


# --------------------------------------------------------------------- intra-node sweep surfacing
def _sweep_st() -> RunState:
    """A run whose best node is a hyperparameter sweep: 12 finite trials + 1 that diverged."""
    st = RunState(goal="minimize loss", direction="min")
    grid = [(0.05, 0.061), (0.02, 0.22), (0.01, 0.30), (0.1, 0.075), (0.2, 0.12), (0.3, 0.18),
            (0.4, 0.25), (0.5, 0.40), (0.7, 0.50), (1.0, 0.65), (1.5, 0.80), (2.0, 0.90)]
    trials = [Trial(params={"lr": lr}, metric=m, seconds=0.1) for lr, m in grid]
    trials.append(Trial(params={"lr": 3.0}, metric=None, error="diverged: nan loss"))
    st.nodes = {
        5: Node(id=5, operator="improve",
                idea=Idea(operator="improve", params={"warmup": 100.0}, theme="hpo",
                          space={"lr": [lr for lr, _ in grid] + [3.0]}),
                metric=0.061, status=NodeStatus.evaluated, trials=trials),
    }
    st.best_node_id = 5
    return st


def test_select_trials_covers_range_bounded_and_deterministic():
    trials = _sweep_st().nodes[5].trials
    sel = digest.select_trials(trials, 5, "min")
    assert len(sel) == 5
    assert sel[0].metric == 0.061                         # best first (min direction)
    assert sel[-1].metric == 0.90                         # worst kept → range covered
    assert all(t.metric is not None for t in sel)         # the diverged trial is dropped
    assert sel == digest.select_trials(trials, 5, "min")  # deterministic
    allsel = digest.select_trials(trials, 999, "min")     # k >= count → all finite, sorted
    assert len(allsel) == 12 and [t.metric for t in allsel] == sorted(t.metric for t in allsel)
    assert digest.select_trials(trials, 3, "max")[0].metric == 0.90   # max direction flips best


def test_digest_surfaces_sweep_flag_and_tuning_block():
    st = _sweep_st()
    d = digest.experiments_digest(st, char_cap=4000)
    assert "swept ×13" in d                               # node line flags the sweep (12 + 1 nometric)
    assert "Tuning of #5 (13 trials, showing 10 of 12 best→worst)" in d
    assert "→ 0.061" in d                                 # best trial's metric shown
    expected = len(digest.select_trials(st.nodes[5].trials, digest.DEFAULT_TRIAL_K, "min"))
    assert d.count(" → ") == expected <= digest.DEFAULT_TRIAL_K   # bounded representative sample


def test_read_experiment_trial_selection_default_number_and_all():
    rt = RunTools()
    rt.bind_state(_sweep_st())
    default = rt.execute("read_experiment", {"node_id": 5})
    assert "sweep: 13 trials" in default and "best [lr=0.05] metric=0.061" in default
    assert "(+1 no-metric)" in default
    assert default.count(" → ") == digest.DEFAULT_TRIAL_K   # 10-trial sample by default

    three = rt.execute("read_experiment", {"node_id": 5, "trials": "3"})
    assert three.count(" → ") == 3                          # explicit count honored

    allt = rt.execute("read_experiment", {"node_id": 5, "trials": "all"})
    assert allt.count(" → ") == 13                          # every trial incl the no-metric one
    assert "no metric" in allt and "diverged" in allt     # the failed trial is shown, with its error

    bogus = rt.execute("read_experiment", {"node_id": 5, "trials": "lots"})
    assert bogus.count(" → ") == digest.DEFAULT_TRIAL_K     # unparseable → falls back to default


def test_select_trials_k1_and_tool_never_raises_on_edge_selectors():
    """Regression: k==1 must not hit the k-1 divisor (ZeroDivisionError); and the tool must return a
    STRING (never raise) for selectors that clamp to 1 or overflow int()."""
    trials = _sweep_st().nodes[5].trials
    one = digest.select_trials(trials, 1, "min")
    assert len(one) == 1 and one[0].metric == 0.061           # the single best, no crash

    rt = RunTools()
    rt.bind_state(_sweep_st())
    for sel in ("1", "0", "0.4", "-5", "inf", "1e999", "nan"):
        out = rt.execute("read_experiment", {"node_id": 5, "trials": sel})
        assert isinstance(out, str) and "experiment #5" in out  # soft-fails to a string, never raises
    assert rt.execute("read_experiment", {"node_id": 5, "trials": "1"}).count(" → ") == 1


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


class _SentinelTask:
    def assets(self):
        return {"train.csv": "x,y\n1.0,NaN\n2.0,inf\n3.0,Infinity\n"}


def test_csv_nan_inf_sentinels_not_numeric():
    """A column of textual NaN/inf sentinels must read as categorical (needs handling), not numeric
    with NaN/inf-poisoned stats."""
    dt = DataTools(_SentinelTask())
    dt.bind_state(_st())
    sch = dt.execute("data_schema", {})
    assert "x (numeric)" in sch and "y (categorical)" in sch
    prof = dt.execute("data_profile", {})
    assert "x: numeric" in prof
    assert "y: categorical" in prof and "nan" not in prof.lower().split("y:")[1]


class _RaggedTask:
    def assets(self):                                # header a,b,c,d ; rows 2-4 truncated before d
        return {"train.csv": "a,b,c,d\n1,2,3,9\n1,2\n1,2\n1,2\n"}


def test_csv_ragged_rows_count_truncated_as_missing():
    dt = DataTools(_RaggedTask())
    dt.bind_state(_st())
    prof = dt.execute("data_profile", {})
    dline = [l for l in prof.splitlines() if l.strip().startswith("d:")][0]
    assert "missing=0.75" in dline                   # d present in only 1 of 4 rows, not 0.00


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
    # A plain researcher needs EVERY tool source off: run-introspection AND the now-default-on
    # memory + knowledge stores (which also wrap the researcher as a tool-using agent).
    researcher, _ = make_roles(ToyTask(), Settings(
        backend="llm", unified_agent=False, researcher_tools=False,
        memory_enabled=False, knowledge_enabled=False))
    assert isinstance(researcher, LLMResearcher)
