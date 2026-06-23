"""MLEBench-style competition adapter (I20): held-out grading, leakage-clean split, and
an offline end-to-end run scored by the private grader."""
from __future__ import annotations

import json
from pathlib import Path

import anyio

from autornd.leakage import train_test_contamination
from autornd.mlebench import MLEBenchTask
from autornd.models import Idea
from autornd.orchestrator import Engine
from autornd.policy import GreedyTree
from autornd.sandbox import SubprocessSandbox
from autornd.tasks import TaskAdapter, load_task

ROOT = Path(__file__).resolve().parents[1]
TASK_FILE = ROOT / "examples" / "mlebench_task.json"


def test_loads_and_conforms():
    task = load_task(TASK_FILE)
    assert isinstance(task, MLEBenchTask)
    assert isinstance(task, TaskAdapter)          # id/goal/direction/build_roles
    assert task.direction == "max"


def test_assets_hold_out_labels_and_grader_scores():
    task = MLEBenchTask()
    a = task.assets()
    assert set(a) >= {"train.json", "test.json", "grader.py"}
    tr, te = json.loads(a["train.json"]), json.loads(a["test.json"])
    assert "y" in tr and "y" not in te            # test labels are withheld
    assert len(te["X"]) == task.n_test and len(tr["X"]) == len(tr["y"]) == task.n_train

    _, _, _, yte = task._data()
    ns: dict = {}
    exec(a["grader.py"], ns)                        # the private grader
    assert ns["score"](yte) == 1.0                  # perfect submission
    assert ns["score"](yte[:-1]) == 0.0            # wrong length -> worst
    assert 0.0 <= ns["score"]([0] * len(yte)) <= 1.0


def test_leakage_split_is_disjoint():
    task = MLEBenchTask()
    inp = task.leakage_inputs()
    assert not train_test_contamination(inp["train_rows"], inp["test_rows"])["leak"]


def test_columns_expose_features_and_label():
    task = MLEBenchTask()
    cols = task.columns()
    assert set(cols) == {f"f{j}" for j in range(task.n_features)} | {"label"}
    assert all(len(v) == task.n_train for v in cols.values())


def test_grader_handles_malformed_predictions():
    task = MLEBenchTask()
    ns: dict = {}
    exec(task.assets()["grader.py"], ns)
    _, _, _, yte = task._data()
    assert ns["score"]([None] * len(yte)) == 0.0          # non-int elements -> worst
    assert ns["score"](["nope"] * len(yte)) == 0.0
    assert ns["score"](["1"] * len(yte)) == ns["score"]([1] * len(yte))  # str ints OK


def test_test_key_independent_of_train_size():
    # Changing n_train must NOT change the held-out test set (independent RNG streams).
    a = MLEBenchTask(seed=0, n_train=20, n_test=10)._data()
    b = MLEBenchTask(seed=0, n_train=80, n_test=10)._data()
    assert a[2] == b[2] and a[3] == b[3]                  # X_test, y_test identical


def test_llm_roles_pass_hyperparameter_and_grader_brief():
    # The LLM Developer must receive the proposed 'k' (regression of the hardcoded
    # degree/lam bug) and a brief that wires the held-out grader.
    from autornd.roles import LLMDeveloper, LLMResearcher

    captured: dict = {}

    class _FakeClient:
        def complete_text(self, messages):
            captured["user"] = messages[-1]["content"]
            return '```python\nprint("{\\"metric\\": 0.5}")\n```'

    r, d = MLEBenchTask().llm_roles(_FakeClient())
    assert isinstance(r, LLMResearcher) and isinstance(d, LLMDeveloper)
    assert "grader" in d.brief and "test.json" in d.brief and "train.json" in d.brief
    d.implement(Idea(operator="draft", params={"k": 7.0}, rationale="try k=7"))
    assert "k=7" in captured["user"]                      # k flows to the developer


def test_offline_engine_run_grades_held_out(tmp_path):
    task = load_task(TASK_FILE)
    researcher, developer = task.build_roles()      # offline templated k-NN
    engine = Engine(tmp_path / "run", task=task, researcher=researcher,
                    developer=developer, sandbox=SubprocessSandbox(),
                    policy=GreedyTree(n_seeds=3, max_nodes=6))
    state = anyio.run(engine.run)
    assert state.finished
    best = state.best()
    assert best is not None and 0.0 <= best.metric <= 1.0
    assert best.metric > 0.6                         # separable blobs beat chance (0.5)
    # The grader + data assets were materialized into the eval workdir.
    nd0 = tmp_path / "run" / "nodes" / "node_0"
    assert (nd0 / "grader.py").exists() and (nd0 / "train.json").exists()
    assert (nd0 / "test.json").exists()
