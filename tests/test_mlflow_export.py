"""G5 MLflow export bridge (optional dep)."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from looplab.events.eventstore import EventStore
from looplab.events.mlflow_export import available, export_run
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.events.replay import fold
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.adapters.toytask import ToyTask

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "examples" / "toy_task.json"


def test_available_is_bool():
    assert isinstance(available(), bool)


def test_export_raises_clear_error_without_mlflow():
    if available():
        pytest.skip("mlflow installed — error path not exercised")
    from looplab.core.models import RunState
    with pytest.raises(RuntimeError, match="mlflow"):
        export_run(RunState(run_id="r", task_id="t"))


@pytest.mark.skipif(not available(), reason="mlflow not installed")
def test_export_logs_champion(tmp_path):
    task = ToyTask.load(TASK)
    r, d = task.build_roles()
    eng = Engine(tmp_path / "run", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    anyio.run(eng.run)
    state = fold(EventStore(tmp_path / "run" / "events.jsonl").read_all())
    rid = export_run(state, tracking_uri=f"file:{tmp_path / 'mlruns'}", experiment="looplab-test")
    assert isinstance(rid, str) and rid


def test_export_run_dir_logs_champion_not_best(tmp_path, monkeypatch):
    # Regression: when a pinned champion differs from the metric-best node, export_run_dir must log
    # the CHAMPION's params + code together (threaded via node=champ) — not best()'s — so the exported
    # solution.py and the logged hyperparameters describe ONE node. Uses a fake mlflow (no real dep).
    import sys
    import types

    import looplab.events.mlflow_export as mod

    rd = tmp_path / "run"
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 0.0}, "rationale": ""}, "code": "# n0 best"})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0})   # metric-best (min direction)
    s.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                              "idea": {"operator": "improve", "params": {"x": 9.0}, "rationale": ""}, "code": "# n1 champ"})
    s.append("node_evaluated", {"node_id": 1, "metric": 5.0})   # worse metric
    s.append("promote", {"node_id": 1})                          # pin champion to the NON-best node
    state = fold(s.read_all())
    assert state.best_node_id == 0 and state.champion == 1       # champion != best

    logged = {"params": {}, "texts": {}}
    fake = types.ModuleType("mlflow")
    fake.set_tracking_uri = lambda *a, **k: None
    fake.set_experiment = lambda *a, **k: None
    fake.set_tags = lambda *a, **k: None
    fake.log_param = lambda k, v: logged["params"].__setitem__(str(k), v)
    fake.log_metric = lambda *a, **k: None
    fake.log_text = lambda t, p: logged["texts"].__setitem__(p, t)

    class _Run:
        class info:
            run_id = "fake-1"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake.start_run = lambda *a, **k: _Run()
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    rid = mod.export_run_dir(rd)
    assert rid == "fake-1"
    assert logged["params"] == {"x": 9.0}                       # champion's params, NOT best()'s {"x": 0.0}
    assert logged["texts"]["solution.py"] == "# n1 champ"       # champion's code, not best's


def test_exported_champion_code_is_redacted_at_the_egress_boundary(tmp_path, monkeypatch):
    """An external tracking server is an egress boundary, and node code is secret-BEARING: a
    repo-mode Developer that read a checked-in .env or token through its tools can echo it into the
    solution. Every other egress in the codebase redacts (serve/reviews.py before disclosure,
    core/tracing.py for the OTLP exporter); this one shipped the code verbatim."""
    import sys
    import types

    import looplab.events.mlflow_export as mod

    secret = "sk-livekey-4f2b91ce77a04d3e8b615ca2d09f7e31aa5c"
    rd = tmp_path / "run"
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    s.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                              "idea": {"operator": "draft", "params": {"x": 0.0}, "rationale": ""},
                              "code": f'API_KEY = "{secret}"\nprint("train")\n'})
    s.append("node_evaluated", {"node_id": 0, "metric": 1.0})

    logged = {}
    fake = types.ModuleType("mlflow")
    fake.set_tracking_uri = fake.set_experiment = fake.set_tags = lambda *a, **k: None
    fake.log_param = fake.log_metric = lambda *a, **k: None
    fake.log_text = lambda t, p: logged.__setitem__(p, t)

    class _Run:
        class info:
            run_id = "fake-redact"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake.start_run = lambda *a, **k: _Run()
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    assert mod.export_run_dir(rd) == "fake-redact"
    assert secret not in logged["solution.py"], "the champion's credential was shipped verbatim"
    assert "***" in logged["solution.py"]                 # masked, not silently dropped
    assert 'print("train")' in logged["solution.py"]      # the code itself still exports


def test_a_channel_tag_is_never_published_for_a_metric_that_did_not_land(tmp_path, monkeypatch):
    """The tag is what an operator reads to tell an auto-captured value from the protected
    `best_metric`, so publishing one for a number that is not in the run's metric table answers a
    question about nothing.

    CORRECTION TO THE FINDING, and it is why this test builds its state in memory: through the FOLD
    the state is unreachable — `node_evaluated` with `extra_metrics={"bad": "not-a-number"}` folds
    to `{}` for that key, so `float()` never raises in production and the `log_metric` containment
    here is defensive only. The gate is still right (it costs nothing and removes a way for the two
    surfaces to disagree), but it closes a shape the reader is protected from one layer up, not a
    live defect. Driven at the branch itself rather than claimed through a path that cannot reach
    it — a test that "passes" on the pre-fix code proves nothing, and this one did.
    """
    import sys
    import types

    from looplab.core.models import Idea, Node, RunState
    import looplab.events.mlflow_export as mod

    state = RunState(run_id="r", task_id="t", direction="min")
    node = Node(id=0, operator="draft", idea=Idea(operator="draft", params={}, rationale=""),
                code="print(1)", metric=1.0)
    node.extra_metrics = {"good": 0.5, "bad": "not-a-number"}
    state.nodes[0] = node

    metrics: dict = {}
    tags: dict = {}
    fake = types.ModuleType("mlflow")
    fake.set_tracking_uri = fake.set_experiment = lambda *a, **k: None
    fake.set_tags = lambda d: tags.update(d)
    fake.set_tag = lambda k, v: tags.__setitem__(k, v)
    fake.log_param = fake.log_text = lambda *a, **k: None
    fake.log_metric = lambda k, v: metrics.__setitem__(str(k), v)

    class _Run:
        class info:
            run_id = "fake-1"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake.start_run = lambda *a, **k: _Run()
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    mod.export_run(state, node=node, experiment="x")
    assert metrics.get("good") == 0.5 and "bad" not in metrics
    assert "looplab.extra_metric_channel.good" in tags
    assert "looplab.extra_metric_channel.bad" not in tags, tags


# ---------------------------------------------------------------- the AUTOLOGGING half (§16)
def _fake_mlflow(record):
    """A stand-in for the optional dependency, recording what a real server would have received.

    CI runs with mlflow ABSENT (it is an optional extra), so every autolog test drives the mirror
    against this — which is also the only way to assert what a tracking server would SEE."""
    import types

    fake = types.ModuleType("mlflow")
    fake.set_tracking_uri = lambda uri: record.setdefault("uris", []).append(uri)
    fake.set_experiment = lambda name: record.setdefault("experiments", []).append(name)
    fake.set_tags = lambda d: record.setdefault("tags", {}).update(d)
    fake.set_tag = lambda k, v: record.setdefault("tags", {}).__setitem__(k, v)
    fake.log_param = lambda k, v: record.setdefault("params", {}).__setitem__(str(k), v)
    fake.log_text = lambda t, p: record.setdefault("texts", {}).__setitem__(p, t)
    fake.end_run = lambda: record.setdefault("ended", []).append(True)

    def _log_metric(k, v, step=None):
        record.setdefault("metrics", []).append((str(k), float(v), step))

    fake.log_metric = _log_metric

    class _Run:
        class info:
            run_id = "mlflow-parent"

        def __init__(self, name=None, nested=False):
            self.name, self.nested = name, nested

        def __enter__(self):
            record.setdefault("runs", []).append((self.name, self.nested))
            return self

        def __exit__(self, *a):
            return False

    def _start(run_name=None, nested=False):
        run = _Run(run_name, nested)
        if not nested:                      # the parent is opened, not entered: mirror the real API
            record.setdefault("runs", []).append((run_name, False))
        return run

    fake.start_run = _start
    return fake


def _run_log(rd, *, nodes=(("0", 1.0), ("1", 0.5)), finished=False):
    s = EventStore(rd / "events.jsonl")
    s.append("run_started", {"run_id": "r-live", "task_id": "t", "direction": "min", "goal": "g"})
    for nid, metric in nodes:
        s.append("node_created", {"node_id": int(nid), "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {"x": float(nid)},
                                           "rationale": ""},
                                  "code": f"# node {nid}"})
        s.append("node_evaluated", {"node_id": int(nid), "metric": metric})
    if finished:
        s.append("run_finished", {"reason": "budget"})
    return s


def test_autolog_is_a_no_op_without_a_uri_and_without_mlflow(tmp_path):
    """Both defaults, driven: an empty URI is OFF (egress needs an operator to name a server) and an
    absent optional dependency degrades to today's export instead of failing a run."""
    from looplab.events.mlflow_export import LiveTracker, autolog, available

    _run_log(tmp_path / "run")
    with autolog(tmp_path / "run", tracking_uri="") as tracker:
        assert tracker is None
    if not available():                         # the CI condition: mlflow is not installed
        with autolog(tmp_path / "run", tracking_uri="file:/tmp/whatever") as tracker:
            assert tracker is None
        t = LiveTracker(tmp_path / "run", tracking_uri="file:/tmp/whatever")
        assert t.sync() == -1                   # gives up, and says so, rather than raising


def test_the_mirror_publishes_each_node_as_it_lands_and_never_twice(tmp_path, monkeypatch):
    """The property the item is about: MLflow receives the run WHILE IT RUNS. The log grows between
    syncs exactly as a live run's does, and the mirror publishes only what is new."""
    import sys

    from looplab.events.mlflow_export import LiveTracker

    record: dict = {}
    monkeypatch.setitem(sys.modules, "mlflow", _fake_mlflow(record))
    rd = tmp_path / "run"
    store = _run_log(rd, nodes=(("0", 1.0),))
    tracker = LiveTracker(rd, tracking_uri="file:/mlruns", experiment="looplab")
    assert tracker.sync() == 1
    assert record["uris"] == ["file:/mlruns"] and record["experiments"] == ["looplab"]
    assert record["tags"]["looplab.run_id"] == "r-live"
    assert record["tags"]["looplab.autologged"] == "true"
    assert ("node-0", True) in record["runs"]
    assert ("node_metric", 1.0, 0) in record["metrics"]
    assert ("best_metric", 1.0, 0) in record["metrics"]

    assert tracker.sync() == 0                   # nothing new: no duplicate child run
    store.append("node_created", {"node_id": 1, "parent_ids": [0], "operator": "improve",
                                  "idea": {"operator": "improve", "params": {"x": 2.0},
                                           "rationale": ""}, "code": "# node 1"})
    store.append("node_evaluated", {"node_id": 1, "metric": 0.25})
    assert tracker.sync() == 1                   # the live append is mirrored on the next poll
    assert ("best_metric", 0.25, 1) in record["metrics"]   # min direction: the running best moved
    assert [n for n, _ in record["runs"]].count("node-0") == 1


def test_the_mirror_closes_with_the_champion_and_redacts_its_code(tmp_path, monkeypatch):
    """The second egress of node code in this module. It goes through the export's own
    `_log_solution`, so a credential a repo-mode Developer echoed into the solution is masked here
    too — the copy-paste that would have shipped it verbatim is what this test refuses."""
    import sys

    from looplab.events.mlflow_export import LiveTracker

    secret = "sk-livekey-4f2b91ce77a04d3e8b615ca2d09f7e31aa5c"
    record: dict = {}
    monkeypatch.setitem(sys.modules, "mlflow", _fake_mlflow(record))
    rd = tmp_path / "run"
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "r-live", "task_id": "t", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""},
                                  "code": f'API_KEY = "{secret}"\nprint("train")\n'})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0})
    tracker = LiveTracker(rd, tracking_uri="file:/mlruns")
    tracker.sync()
    tracker.close()
    assert secret not in record["texts"]["solution.py"]
    assert 'print("train")' in record["texts"]["solution.py"]
    assert record["tags"]["looplab.champion_node_id"] == "0"
    assert record["ended"] == [True]


def test_a_dead_tracking_server_costs_the_mirror_and_not_the_run(tmp_path, monkeypatch):
    """The mirror is an observer. A tracking server that raises on every call must not raise into
    the caller, and must STOP rather than retry for the length of a run."""
    import sys
    import types

    from looplab.events.mlflow_export import LiveTracker, follow_run_dir

    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("tracking server is down")

    dead = types.ModuleType("mlflow")
    dead.set_tracking_uri = dead.set_experiment = dead.set_tags = _boom
    dead.start_run = _boom
    monkeypatch.setitem(sys.modules, "mlflow", dead)
    _run_log(tmp_path / "run")
    tracker = LiveTracker(tmp_path / "run", tracking_uri="http://127.0.0.1:1/never")
    for _ in range(6):
        assert tracker.sync() in (0, -1)         # never raises
    assert tracker.failures >= 3 and tracker.sync() == -1
    assert calls["n"] <= 3, "the mirror kept hammering a dead server"
    # …and the follower loop ENDS on that give-up instead of polling forever.
    assert follow_run_dir(tmp_path / "run", tracking_uri="http://127.0.0.1:1/never",
                          poll_s=0.01).failures >= 3


def test_the_mirror_never_writes_to_the_run_log(tmp_path, monkeypatch):
    """Engine invariant #1: the engine is the sole writer of domain events. The mirror is a READER —
    driven by comparing the log's bytes before and after a full mirror cycle."""
    import sys

    from looplab.events.mlflow_export import LiveTracker

    monkeypatch.setitem(sys.modules, "mlflow", _fake_mlflow({}))
    rd = tmp_path / "run"
    _run_log(rd, finished=True)
    before = (rd / "events.jsonl").read_bytes()
    tracker = LiveTracker(rd, tracking_uri="file:/mlruns")
    tracker.sync()
    tracker.close()
    assert (rd / "events.jsonl").read_bytes() == before


def test_the_cli_drive_mirrors_a_real_run_while_it_runs(tmp_path, monkeypatch):
    """WIRED, driven end to end rather than pinned in source: `cli/run_cmds._run_engine_guarded` is
    what both `run` and `resume` drive the loop through, so a real toy run through it must reach the
    fake tracking server with the nodes it actually evaluated — and must reach it with no
    `export-mlflow` command anywhere. An empty URI leaves the server untouched."""
    import sys

    from looplab.cli import run_cmds
    from tests.factories import make_engine

    record: dict = {}
    monkeypatch.setitem(sys.modules, "mlflow", _fake_mlflow(record))
    eng = make_engine(tmp_path / "mirrored", n_seeds=2, max_nodes=3)
    state = run_cmds._run_engine_guarded(eng, mlflow_uri="file:/mlruns")
    assert state.finished and state.evaluated_nodes()
    mirrored = {name for name, nested in record.get("runs", []) if nested}
    assert mirrored == {f"node-{n.id}" for n in state.evaluated_nodes()}
    assert record["tags"]["looplab.autologged"] == "true"
    assert any(k == "best_metric" for k, _v, _s in record["metrics"])

    off: dict = {}
    monkeypatch.setitem(sys.modules, "mlflow", _fake_mlflow(off))
    eng2 = make_engine(tmp_path / "unmirrored", n_seeds=2, max_nodes=3)
    run_cmds._run_engine_guarded(eng2)                     # the shipped default: no URI, no mirror
    assert off == {}


def test_the_fold_is_what_really_keeps_a_non_numeric_extra_metric_out():
    """The rung above the export, stated so the defensive gate is not mistaken for the protection.
    A candidate can print anything; `node_evaluated` folding is where that stops being a number."""
    from looplab.events.eventstore import EventStore

    import tempfile
    from pathlib import Path as _P

    store = EventStore(_P(tempfile.mkdtemp()) / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})
    store.append("node_created", {"node_id": 0, "parent_ids": [], "operator": "draft",
                                  "idea": {"operator": "draft", "params": {}, "rationale": ""},
                                  "code": "x"})
    store.append("node_evaluated", {"node_id": 0, "metric": 1.0,
                                    "extra_metrics": {"good": 0.5, "bad": "not-a-number"}})
    assert fold(store.read_all()).best().extra_metrics == {"good": 0.5}
