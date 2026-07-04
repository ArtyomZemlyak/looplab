"""Out-of-process host-side grading as a GENERAL engine capability (any task, not just MLEBench).

The candidate writes only predictions; the host scores them against held-out labels it never put on
the candidate FS, overriding the self-reported metric. Proven here with a synthetic task whose
solution LIES in stdout — the host's score wins.
"""
from __future__ import annotations

import anyio
from pydantic import BaseModel

from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.search.policy import GreedyTree
from looplab.runtime.sandbox import SubprocessSandbox


class _PredTask(BaseModel):
    kind: str = "predtest"
    id: str = "predtest"
    goal: str = "predict a held-out target"
    direction: str = "min"

    def host_grader(self) -> dict:
        return {"predictions": "predictions.json", "scorer": "rmse", "labels": [1.0, 2.0, 3.0]}


class _Stub:
    def propose(self, state, parent):
        return Idea(operator="draft", params={})


def _dev(code_body):
    class _D:
        def implement(self, idea):
            return code_body
    return _D()


def _run(tmp_path, code):
    # holdout_fraction=0 pins the LEGACY full-label scoring this file tests; the D1
    # label-partition path (search scored on the complement of a reserved holdout) has its
    # own coverage in tests/test_holdout.py.
    eng = Engine(tmp_path, task=_PredTask(), researcher=_Stub(), developer=_dev(code),
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=1),
                 holdout_fraction=0.0)
    return anyio.run(eng.run)


_PERFECT_BUT_LIES = (
    "import json\n"
    "json.dump([1.0, 2.0, 3.0], open('predictions.json', 'w'))\n"   # perfect predictions
    "print(json.dumps({'metric': 999.0}))\n"                         # ...but a lying self-report
)


def test_host_score_overrides_self_reported_metric(tmp_path):
    state = _run(tmp_path / "lie", _PERFECT_BUT_LIES)
    best = state.best()
    assert best is not None and best.metric == 0.0   # host RMSE of perfect preds, NOT the 999 lie
    assert state.host_grading and state.host_grading["scorer"] == "rmse"
    assert state.host_grading["n_labels"] == 3


def test_host_score_uses_real_predictions(tmp_path):
    code = ("import json\n"
            "json.dump([1.0, 2.0, 5.0], open('predictions.json', 'w'))\n"   # last pred wrong by 2
            "print(json.dumps({'metric': 0.0}))\n")                          # lies it's perfect
    best = _run(tmp_path / "wrong", code).best()
    # RMSE of [0,0,2] errors = sqrt(4/3) ≈ 1.1547 — NOT the claimed 0.0
    assert best is not None and abs(best.metric - (4 / 3) ** 0.5) < 1e-9


def test_missing_predictions_fails_node(tmp_path):
    # A candidate that self-reports but writes NO predictions cannot pass under host grading.
    code = "import json\nprint(json.dumps({'metric': 0.0}))\n"
    state = _run(tmp_path / "nopred", code)
    assert state.best() is None
    assert all(n.metric is None for n in state.nodes.values())


class _NoLabelsTask(_PredTask):
    kind: str = "nolabels"
    id: str = "nolabels"

    def host_grader(self) -> dict:
        return {"predictions": "predictions.json", "scorer": "rmse"}   # labels key omitted


def test_host_grader_without_labels_does_not_crash(tmp_path):
    # A host_grader() dict missing "labels" must yield no metric (node fails), not an uncaught
    # KeyError that crashes the eval worker.
    eng = Engine(tmp_path / "nolab", task=_NoLabelsTask(), researcher=_Stub(),
                 developer=_dev(_PERFECT_BUT_LIES), sandbox=SubprocessSandbox(),
                 policy=GreedyTree(n_seeds=1, max_nodes=1))
    state = anyio.run(eng.run)
    assert state.finished
    assert all(n.metric is None for n in state.nodes.values())
