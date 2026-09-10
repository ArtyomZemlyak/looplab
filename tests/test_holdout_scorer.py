"""The WITHHELD host-side scorer for the repo task (doc 52 row 10a, slice (b)).

Row 10a's first half made the repo task's number CONSISTENT — one operator-owned program, outside
every editable root, the same bytes for every node (`test_host_scorer.py`). It did not make the
number UNSEEN: a scorer that reads a split the candidate can also read is consistent and nothing
more, so the champion was still elected on a number the candidate could overfit by looking at what
it was scored on. `cmd.holdout_scorer` is the other property — the operator's own program over a
split the HOST holds, run ONCE at finish over the val-top-k and never during the search, whose
number becomes the node's `holdout_metric` and, under `Settings.holdout_select`, elects the
champion among those leaders.

What this file drives, in the order the risk runs:
  1. THE REFUSALS — the same ones the consistent scorer gets, because it is the same property, and
     one more: a holdout scorer is not an eval, so it can never be the only thing that runs;
  2. IT IS NEVER A STAGE — no pipeline shape can run it, which is what "withheld" means mechanically;
  3. THE RUNTIME — it runs in the node's workdir under the declared environment, its stdout is read
     with its own reader, and a failure gives NO number rather than falling back to the search one;
  4. THE RECORD, end to end through a real Engine over the repo fixture: the withheld number
     OVERTURNS the search ranking (the whole point — the candidate's own metric stops being the
     selection scale), the row carries its protocol and the program's digest, and the replay is
     byte-identical;
  5. a task that declares no holdout scorer is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from looplab.adapters.repo_task import (EvalSpec, HoldoutScorerSpec, RepoTask,
                                        holdout_scorer_outside_editables)
from looplab.adapters.tasks import validate_task
from looplab.core.models import Idea
from looplab.events.replay import fold
from tests.factories import make_engine

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}

# The withheld scorer: it reads the candidate's own config out of the node workdir and scores it by
# a rule the candidate never sees — the optimum sits at x=9, while `ttrain.py` (the search metric)
# is maximized at x=3. A candidate that hill-climbs the search number therefore climbs AWAY from
# this one, which is exactly the disagreement an unseen split is bought to expose.
HOLDOUT_AT_NINE = ("import json\n"
                   "x = float(json.load(open('config.json', encoding='utf-8')).get('x', 0.0))\n"
                   "print(json.dumps({'metric': -((x - 9.0) ** 2)}))\n")


def _scorer(tmp_path: Path, body: str, name: str = "holdout.py") -> Path:
    """The operator's program, in a directory that is NOT the editable tree and NOT any workdir."""
    d = tmp_path / "host-scorers"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")
    return d / name


# ------------------------------------------------------------------------------- 1. THE REFUSALS

def test_a_holdout_scorer_must_name_an_absolute_program():
    for argv in (["python", "score.py"], ["python", "-m", "scorers.score"], [], ["python", 3]):
        with pytest.raises(ValidationError):
            HoldoutScorerSpec(command=argv)
    ok = HoldoutScorerSpec(command=["python", "/opt/scorers/holdout.py"])
    assert ok.timeout == 1800.0 and ok.env == {} and ok.metric is None
    with pytest.raises(ValidationError) as exc:
        HoldoutScorerSpec(command=["python", "/opt/s.py"], metric={"kind": "adapter",
                                                                  "path": "m.py"})
    assert "holdout_scorer.metric" in str(exc.value), "the refusal names the field as written"
    with pytest.raises(ValidationError):
        HoldoutScorerSpec(command=["python", "/opt/s.py"], timeout=0)
    with pytest.raises(ValidationError):
        HoldoutScorerSpec(command=["python", "/opt/s.py"], env={"OPENAI_API_KEY": "sk-x"})


def test_a_holdout_scorer_inside_the_editable_tree_is_refused(tmp_path):
    """A program the candidate can edit or import from is the candidate's own program — and for the
    WITHHELD half that is worse than for the consistent one: reading the scorer reads the split."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    inside = repo / "holdout.py"
    inside.write_text(HOLDOUT_AT_NINE, encoding="utf-8")
    task = {"kind": "repo", "id": "t", "goal": "g", "direction": "max", "editable_path": str(repo),
            "eval": {"command": [sys.executable, "ttrain.py"], "metric": _M,
                     "holdout_scorer": {"command": [sys.executable, str(inside)]}}}
    with pytest.raises(ValueError) as exc:
        validate_task(task)
    assert "INSIDE the editable tree" in str(exc.value) and "holdout_scorer" in str(exc.value)
    missing = dict(task, eval={"command": [sys.executable, "ttrain.py"], "metric": _M,
                               "holdout_scorer": {"command": [sys.executable,
                                                              str(tmp_path / "nope.py")]}})
    with pytest.raises(ValueError) as exc:
        validate_task(missing)
    assert "not an existing file" in str(exc.value)
    # …and a run that already recorded such a task stays resumable, warned rather than refused.
    grandfathered = validate_task(task, existing_run=True)
    assert grandfathered.eval.holdout_scorer is not None
    assert "INSIDE" in (holdout_scorer_outside_editables(grandfathered) or "")
    outside = _scorer(tmp_path, HOLDOUT_AT_NINE)
    legal = dict(task, eval={"command": [sys.executable, "ttrain.py"], "metric": _M,
                             "holdout_scorer": {"command": [sys.executable, str(outside)]}})
    assert holdout_scorer_outside_editables(validate_task(legal)) is None


def test_a_holdout_scorer_alone_is_not_an_eval(tmp_path):
    """It scores nothing during the run, so it cannot be the thing that runs. A task declaring only
    a holdout scorer would produce no search metric at all and every node would fail."""
    hs = {"command": [sys.executable, str(_scorer(tmp_path, HOLDOUT_AT_NINE))]}
    with pytest.raises(ValidationError):
        EvalSpec(holdout_scorer=hs, metric=_M)
    assert EvalSpec(command=[sys.executable, "t.py"], holdout_scorer=hs,
                    metric=_M).holdout_scorer is not None


def test_its_reader_is_enumerated_with_every_other_reader(tmp_path):
    """`readers()` is THE enumeration of where a reader can appear, so a rule about readers covers
    this half without being told about it separately."""
    hs = {"command": [sys.executable, str(_scorer(tmp_path, HOLDOUT_AT_NINE))],
          "metric": {"kind": "stdout_regex", "key": r"R: ([0-9.]+)"}}
    spec = EvalSpec(command=[sys.executable, "t.py"], holdout_scorer=hs, metric=_M)
    assert ("eval.holdout_scorer.metric", "metric", spec.holdout_scorer.metric) in spec.readers()


# --------------------------------------------------------------------------- 2. NEVER A STAGE

def _task(tmp_path, *, body=HOLDOUT_AT_NINE, holdout=True, **eval_extra) -> RepoTask:
    ev = {"command": [sys.executable, "ttrain.py"], "metric": _M, "timeout": 60, **eval_extra}
    if holdout:
        ev["holdout_scorer"] = {"command": [sys.executable, str(_scorer(tmp_path, body)),
                                            "--tag", "%params%"],
                                "timeout": 60}
    return RepoTask(id="fix", goal="maximize metric", direction="max",
                    editable_path=str(FIXTURE), edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(**ev))


def test_no_pipeline_shape_can_run_the_withheld_scorer(tmp_path):
    """"Withheld" is mechanical, not a promise: nothing in the eval pipeline reaches this program,
    so no candidate process ever shares a machine state with it."""
    task = _task(tmp_path)
    eng = _engine(tmp_path, task, _Dev())
    wd = tmp_path / "wd"
    wd.mkdir()
    chain = eng._resolve_stages(str(wd), eng._eval_spec, params={"x": 2.0}) or []
    argv = " ".join(str(a) for stage in chain for a in stage["command"])
    assert "holdout.py" not in argv
    (wd / "looplab_stages.json").write_text(json.dumps(
        {"stages": [{"name": "train", "command": ["python", "train.py"]}]}), encoding="utf-8")
    chain = eng._resolve_stages(str(wd), eng._eval_spec, params={}) or []
    assert "holdout.py" not in " ".join(str(a) for stage in chain for a in stage["command"])


# -------------------------------------------------------------------------------- 3. THE RUNTIME

class _Dev:
    """A repo Developer stub: each build writes a DIFFERENT x, so the run has a real ranking."""

    _XS = [2.0, 8.0, 4.0, 9.5, 1.0]

    def __init__(self):
        self.n = 0

    def implement(self, idea: Idea) -> str:
        self.last_files = {"config.json": json.dumps({"x": self._XS[self.n % len(self._XS)]})}
        self.n += 1
        return ""


def _engine(tmp_path, task, developer, **kw):
    researcher, _ = task.build_roles()
    return make_engine(tmp_path / "run", task=task, researcher=researcher,
                       developer=developer, n_seeds=2, max_nodes=3, **kw)


def test_the_scorer_runs_in_the_node_workdir_and_a_failure_yields_no_number(tmp_path):
    """The one thing it may never do is fail INTO a number: falling back to the search metric would
    put the number the search optimised into the field selection reads as unseen."""
    task = _task(tmp_path)
    eng = _engine(tmp_path, task, _Dev())
    node = type("N", (), {"id": 0, "idea": Idea(operator="draft", params={"x": 2.0})})()
    wd = eng.run_dir / "nodes" / "node_0"
    wd.mkdir(parents=True)
    (wd / "config.json").write_text(json.dumps({"x": 2.0}), encoding="utf-8")
    metric, digest = eng.holdout._run_holdout_scorer(node, eng.holdout.holdout_scorer())
    assert metric == pytest.approx(-49.0)                       # -(2-9)^2, the withheld rule
    assert digest == hashlib.sha256(
        Path(task.eval.holdout_scorer.command[1]).read_bytes()).hexdigest()

    # a program that exits non-zero -> no number, and the digest is still reported
    broken = _task(tmp_path, body="import sys\nsys.exit(3)\n")
    eng2 = _engine(tmp_path / "b", broken, _Dev())
    (eng2.run_dir / "nodes" / "node_0").mkdir(parents=True)
    metric2, digest2 = eng2.holdout._run_holdout_scorer(node, eng2.holdout.holdout_scorer())
    assert metric2 is None and digest2

    # …and a node whose workdir is gone is not an error either
    node9 = type("N", (), {"id": 9, "idea": Idea(operator="draft", params={})})()
    assert eng.holdout._run_holdout_scorer(node9, eng.holdout.holdout_scorer()) == (None, None)


def test_a_task_with_no_holdout_scorer_has_no_holdout_phase(tmp_path):
    eng = _engine(tmp_path, _task(tmp_path, holdout=False), _Dev())
    assert eng.holdout.holdout_scorer() is None
    assert eng._holdout_pending(fold(eng.store.read_all())) is False


# --------------------------------------------------------------------- 4. THE RECORD, end to end

def test_the_withheld_number_overturns_the_search_ranking(tmp_path):
    """THE property of the slice: the champion is no longer picked on the number the candidate's
    own run produced. The search metric still decides WHO is measured on the unseen split (the
    top-k), and the unseen number decides who WINS."""
    task = _task(tmp_path)
    program = Path(task.eval.holdout_scorer.command[1])
    before = program.read_bytes()
    engine = _engine(tmp_path, task, _Dev())
    state = anyio.run(engine.run)
    assert state.finished

    rows = [e.data for e in engine.store.read_all() if e.type == "holdout_evaluated"]
    assert rows, "the withheld scorer never ran"
    assert {r["protocol"] for r in rows} == {"holdout_scorer"}
    assert {r["program_sha256"] for r in rows} == {hashlib.sha256(before).hexdigest()}, (
        "every leader must be measured by the SAME withheld program")

    scored = {n.id: n for n in state.nodes.values() if n.holdout_metric is not None}
    assert len(scored) >= 2, "the top-k needs at least two leaders for the pick to mean anything"
    # `(metric, id)` and `(holdout_metric, id)` — the fold's own ranked-scalar keys
    # (`core/fitness.py::SearchFitness`), so "who the search would have elected" is the same
    # question the champion pick asks and not a second spelling of it.
    search_leader = max(scored.values(), key=lambda n: (n.metric, n.id))
    unseen_leader = max(scored.values(), key=lambda n: (n.holdout_metric, n.id))
    assert search_leader.id != unseen_leader.id, (
        "the fixture no longer makes the two rankings disagree, so it proves nothing")
    assert state.best_node_id == unseen_leader.id, (
        "the champion is still elected on the number the search optimised")
    # the gap is derived from the pair, direction-aware (positive = the search signal looked better)
    assert unseen_leader.generalization_gap == pytest.approx(
        unseen_leader.metric - unseen_leader.holdout_metric)
    assert program.read_bytes() == before, "nothing the run did touched the operator's program"

    # the fold IS the record: a fresh replay elects the same champion from the same rows
    again = fold(engine.store.read_all())
    assert again.best_node_id == state.best_node_id
    assert {nid: n.holdout_metric for nid, n in again.nodes.items() if n.holdout_metric is not None} \
        == {nid: n.holdout_metric for nid, n in scored.items()}
    assert again.model_dump(mode="json") == state.model_dump(mode="json")


def test_with_holdout_select_off_the_search_metric_still_decides(tmp_path):
    """The boundary: this slice supplies an unseen NUMBER; whether selection reads it is
    `Settings.holdout_select`, exactly as it is for a host-graded task's partition."""
    engine = _engine(tmp_path, _task(tmp_path), _Dev(), holdout_select=False)
    state = anyio.run(engine.run)
    assert state.finished
    rows = [e.data for e in engine.store.read_all() if e.type == "holdout_evaluated"]
    assert rows, "the scorer must still RUN — the record is the same, only the pick changes"
    assert [n for n in state.nodes.values() if n.holdout_metric is not None]
    # The champion is the run's own scalar leader again — the unseen number is recorded beside it
    # and read by nothing that selects.
    eligible = [n for n in state.nodes.values()
                if n.metric is not None and n.feasible and not n.tombstoned]
    assert state.best_node_id == max(eligible, key=lambda n: (n.metric, n.id)).id


# ------------------------------------------------------------------------ 5. NEGATIVE CONTROL

def test_a_run_without_the_declaration_records_no_holdout_row(tmp_path):
    engine = _engine(tmp_path, _task(tmp_path, holdout=False), _Dev())
    state = anyio.run(engine.run)
    assert state.finished
    assert [e for e in engine.store.read_all() if e.type == "holdout_evaluated"] == []
    assert all(n.holdout_metric is None and n.generalization_gap is None
               for n in state.nodes.values())
    # …and the fold of an UNDECLARED run is the fold it always was: nothing this slice added folds,
    # derives or selects when the task says nothing, which is the property every preserved repo run
    # on this box depends on.
    assert fold(engine.store.read_all()).model_dump(mode="json") == state.model_dump(mode="json")
