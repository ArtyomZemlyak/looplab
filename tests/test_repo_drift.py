"""Phase 4 drift cross-check (eval_trust_mode='ratify_freeze_drift').

The threat closed here: under plain ratify_freeze the metric adapter is frozen (the agent
can't edit it), but the agent's experiment code still RUNS as part of the eval, so it can
write the very file the adapter reads (e.g. a fake metrics.json) and fake the score. An
independent built-in reader pointed at a source the agent can't forge (the real process
stdout) catches the divergence.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
import pytest

from looplab.command_eval import run_command_eval
from looplab.models import Idea
from looplab.orchestrator import Engine
from looplab.policy import GreedyTree
from looplab.repo_task import EvalSpec, RepoTask
from looplab.sandbox import SubprocessSandbox

# A program that reports the TRUE metric on stdout but writes a FAKE (better) metrics.json —
# i.e. a reward hack through the eval workdir.
_HACK = (
    'import json\n'
    'open("metrics.json","w").write(json.dumps({"metric": 99.0}))   # faked\n'
    'print(json.dumps({"metric": 1.0}))                              # true (stdout)\n'
)
_HONEST = (
    'import json\n'
    'open("metrics.json","w").write(json.dumps({"metric": 1.0}))\n'
    'print(json.dumps({"metric": 1.0}))\n'
)
_FILE = {"kind": "file_json", "path": "metrics.json", "key": "metric"}   # forgeable
_STDOUT = {"kind": "stdout_json", "key": "metric"}                       # independent


def _run(prog: str, tmp_path: Path, enforce: bool):
    (tmp_path / "p.py").write_text(prog, encoding="utf-8")
    return run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _FILE,
                            cross_check=_STDOUT, drift_tolerance=1e-6, enforce_drift=enforce)


def test_drift_caught_when_enforced(tmp_path):
    res = _run(_HACK, tmp_path, enforce=True)
    assert res.metric is None                       # faked metric discarded
    assert res.drift == {"primary": 99.0, "cross": 1.0, "tolerance": 1e-6}


def test_no_drift_when_readers_agree(tmp_path):
    res = _run(_HONEST, tmp_path, enforce=True)
    assert res.metric == 1.0 and res.drift is None


def test_hole_open_without_enforcement(tmp_path):
    # Plain ratify_freeze (no drift enforcement) still reads the fake — proves the check matters.
    res = _run(_HACK, tmp_path, enforce=False)
    assert res.metric == 99.0 and res.drift is None


def test_drift_when_cross_reader_finds_nothing(tmp_path):
    # Adapter reports a metric the independent reader can't corroborate at all -> drift.
    prog = ('import json\nopen("metrics.json","w").write(json.dumps({"metric": 5.0}))\n'
            'print("no json here")\n')
    res = _run(prog, tmp_path, enforce=True)
    assert res.metric is None and res.drift["primary"] == 5.0 and res.drift["cross"] is None


def test_cross_check_adapter_kind_rejected(tmp_path):
    (tmp_path / "p.py").write_text(_HONEST, encoding="utf-8")
    with pytest.raises(ValueError, match="independent built-in reader"):
        run_command_eval([sys.executable, "p.py"], str(tmp_path), 60, _FILE,
                         cross_check={"kind": "adapter", "path": "x.py"}, enforce_drift=True)


def test_evalspec_rejects_adapter_cross_check():
    with pytest.raises(ValueError, match="independent built-in reader"):
        EvalSpec(command=["python", "t.py"], cross_check={"kind": "adapter", "path": "x.py"})


def test_engine_emits_spec_drift_and_fails_node(tmp_path):
    """End-to-end: a drifted eval records spec_drift, fails the node, never rewards it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text(_HACK, encoding="utf-8")
    t = RepoTask(id="d", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_FILE,
                               cross_check=_STDOUT))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 eval_trust_mode="ratify_freeze_drift")
    state = anyio.run(eng.run)
    assert state.drifts and state.drifts[0]["primary"] == 99.0
    assert state.best_node_id is None               # nothing trusted -> no winner


def test_drift_not_enforced_under_plain_ratify_freeze(tmp_path):
    """Same task under ratify_freeze (no drift): the fake passes (documents the trade-off)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text(_HACK, encoding="utf-8")
    t = RepoTask(id="d", direction="max", editable_path=str(repo), edit_surface=["*.txt"],
                 eval=EvalSpec(command=[sys.executable, "run.py"], metric=_FILE,
                               cross_check=_STDOUT))
    r, d = t.build_roles()
    eng = Engine(tmp_path / "run", task=t, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=1, max_nodes=2),
                 eval_trust_mode="ratify_freeze")
    state = anyio.run(eng.run)
    assert not state.drifts
    assert state.best() is not None and state.best().metric == 99.0
