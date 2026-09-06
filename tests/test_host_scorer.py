"""Consistent HOST-SIDE scoring for the repo task (doc 52 row 10a; AIRA2's D_search half).

The repo family elected its champion on the number the candidate's own scorer printed: the
protected `cmd` froze the entry FILE, and everything it imported, every config it read and the
split it scored on lived inside the editable tree the candidate rewrites. `HostScorerSpec` is the
other half of the contract — the operator's own program, outside every editable root, appended by
the engine as the final protected `score` stage, its bytes digested onto every node's record, and
the candidate's own number kept beside the host's as `self_metric`.

What this file drives, in the order the risk runs:
  1. THE REFUSAL — a scorer the candidate could edit or import from is not a host scorer;
  2. THE PIPELINE SHAPE — the host stage is last in every shape, stamped, never declarable;
  3. THE RUNTIME — the host's number is the metric, the candidate's is `self_metric`, `%subject%`
     expands to the bound artifact, the receipt digests the program, a failing host scorer never
     falls back to the self-report;
  4. THE RECORD, end to end through a real Engine over the repo fixture: the folded champion carries
     the host number, the self-report, the derived gap and a receipt equal on every node, and the
     replay is byte-identical;
  5. a task that declares no host scorer is unchanged.
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

from looplab.adapters.repo_task import (EvalSpec, HostScorerSpec, RepoTask,
                                        host_scorer_outside_editables)
from looplab.adapters.tasks import validate_task
from looplab.core.models import Idea
from looplab.engine.orchestrator import Engine
from looplab.events.replay import fold
from looplab.runtime.command_eval import (HOST_STAGE_KEY, STAGE_KEYS, SUBJECT_TOKEN,
                                          expand_subject, host_program_token, run_command_eval,
                                          validate_stages)
from looplab.runtime.sandbox import SubprocessSandbox
from looplab.search.policy import GreedyTree

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "repo_fixture"
_M = {"kind": "stdout_json", "key": "metric"}


def _host_dir(tmp_path: Path, body: str) -> Path:
    """The operator's scorer, in a directory that is NOT the editable tree and NOT any workdir."""
    d = tmp_path / "host-scorers"
    d.mkdir(parents=True, exist_ok=True)
    (d / "score.py").write_text(body, encoding="utf-8")
    return d / "score.py"


# A host scorer that reads the candidate's config from the WORKDIR (its cwd) and scores it by a rule
# the candidate's own `ttrain.py` does not use: half the candidate's number.
HOST_HALF = ("import json\n"
             "x = float(json.load(open('config.json', encoding='utf-8')).get('x', 0.0))\n"
             "print(json.dumps({'metric': -((x - 3.0) ** 2) / 2.0}))\n")


# ------------------------------------------------------------------------------- 1. THE REFUSAL
def test_a_host_scorer_must_name_an_absolute_program():
    for argv in (["python", "score.py"], ["python", "-m", "scorers.score"], [], ["python", 3]):
        with pytest.raises(ValidationError):
            HostScorerSpec(command=argv)
    ok = HostScorerSpec(command=["python", "/opt/scorers/score.py", SUBJECT_TOKEN])
    assert host_program_token(ok.command) == "/opt/scorers/score.py"
    assert ok.timeout == 1800.0 and ok.env == {} and ok.metric is None
    with pytest.raises(ValidationError):
        HostScorerSpec(command=["python", "/opt/s.py"], metric={"kind": "adapter", "path": "m.py"})
    with pytest.raises(ValidationError):
        HostScorerSpec(command=["python", "/opt/s.py"], timeout=0)
    with pytest.raises(ValidationError):
        HostScorerSpec(command=["python", "/opt/s.py"], env={"OPENAI_API_KEY": "sk-x"})


def test_a_scorer_inside_the_editable_tree_is_refused_and_an_existing_run_still_loads(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    inside = repo / "score.py"
    inside.write_text(HOST_HALF, encoding="utf-8")
    task = {"kind": "repo", "id": "t", "goal": "g", "direction": "max", "editable_path": str(repo),
            "eval": {"host_scorer": {"command": [sys.executable, str(inside)]}, "metric": _M}}
    with pytest.raises(ValueError) as exc:
        validate_task(task)
    assert "INSIDE the editable tree" in str(exc.value)
    # a program that does not exist is refused too: the receipt could never digest it
    missing = dict(task, eval={"host_scorer": {"command": [sys.executable, str(tmp_path / "nope.py")]},
                               "metric": _M})
    with pytest.raises(ValueError) as exc:
        validate_task(missing)
    assert "not an existing file" in str(exc.value)
    # ... and a run that already recorded such a task stays resumable (`_grandfathered`)
    assert validate_task(task, existing_run=True).eval.host_scorer is not None
    assert "INSIDE" in (host_scorer_outside_editables(validate_task(task, existing_run=True)) or "")
    # the property itself, on a legal declaration
    outside = _host_dir(tmp_path, HOST_HALF)
    legal = dict(task, eval={"host_scorer": {"command": [sys.executable, str(outside)]}, "metric": _M})
    assert host_scorer_outside_editables(validate_task(legal)) is None


def test_the_host_scorer_is_the_score_stage_and_one_of_three_things_must_run(tmp_path):
    outside = _host_dir(tmp_path, HOST_HALF)
    hs = {"command": [sys.executable, str(outside)]}
    assert EvalSpec(host_scorer=hs, metric=_M).command == []          # a host scorer alone runs
    with pytest.raises(ValidationError):
        EvalSpec(metric=_M)                                           # nothing would run
    with pytest.raises(ValidationError) as exc:
        EvalSpec(host_scorer=hs, metric=_M,
                 stages=[{"name": "train", "command": ["python", "t.py"]},
                         {"name": "score", "command": ["python", "s.py"]}])
    assert "host scorer IS the final score stage" in str(exc.value)
    spec = EvalSpec(host_scorer=dict(hs, metric={"kind": "stdout_regex", "key": r"R: ([0-9.]+)"}),
                    metric=_M)
    assert ("eval.host_scorer.metric", "metric", spec.host_scorer.metric) in spec.readers()


def test_the_host_stage_key_is_engine_stamped_and_never_declarable():
    assert HOST_STAGE_KEY not in STAGE_KEYS
    _clean, err = validate_stages([{"name": "score", "command": ["python", "/opt/s.py"],
                                    HOST_STAGE_KEY: True}], allow_env=True)
    assert err and "unknown key" in err


# ------------------------------------------------------------------------- 2. THE PIPELINE SHAPE
def _engine(tmp_path, task, developer):
    researcher, _ = task.build_roles()
    return Engine(tmp_path / "run", task=task, researcher=researcher, developer=developer,
                  sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3))


class _Dev:
    """Sets x=2: the candidate's own scorer prints -1.0, the host scorer computes -0.5."""
    def implement(self, idea: Idea) -> str:
        return ""

    last_files = {"config.json": json.dumps({"x": 2.0})}


def _task(tmp_path, *, command=True, stages=None) -> RepoTask:
    outside = _host_dir(tmp_path, HOST_HALF)
    ev = {"host_scorer": {"command": [sys.executable, str(outside), "--tag", "%params%"]},
          "metric": _M, "timeout": 60}
    if command:
        ev["command"] = [sys.executable, "ttrain.py"]
    if stages:
        ev["stages"] = stages
    return RepoTask(id="fix", goal="maximize metric", direction="max",
                    editable_path=str(FIXTURE), edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(**ev))


def test_the_host_stage_is_last_in_every_pipeline_shape(tmp_path):
    from looplab.runtime import command_eval
    eng = _engine(tmp_path, _task(tmp_path), _Dev())
    es = eng._eval_spec
    wd = tmp_path / "wd"
    wd.mkdir()
    # no manifest, a candidate `command`: self_score then the host's score
    chain = eng._resolve_stages(str(wd), es, params={"lr": 0.5})
    assert [s["name"] for s in chain] == ["self_score", "score"]
    assert chain[-1][HOST_STAGE_KEY] is True and not chain[0].get(HOST_STAGE_KEY)
    assert chain[-1]["command"][-2:] == ["--lr", "0.5"]              # %params% expands
    assert chain[-1]["timeout"] == 1800.0
    # a Developer manifest supplies the preceding stages
    (wd / "looplab_stages.json").write_text(json.dumps(
        {"stages": [{"name": "train", "command": ["python", "train.py"]}]}), encoding="utf-8")
    chain = eng._resolve_stages(str(wd), es, params={})
    assert [s["name"] for s in chain] == ["train", "self_score", "score"]
    # a host scorer with no candidate command: the manifest, then the host
    eng2 = _engine(tmp_path / "b", _task(tmp_path, command=False), _Dev())
    chain = eng2._resolve_stages(str(wd), eng2._eval_spec, params={})
    assert [s["name"] for s in chain] == ["train", "score"] and chain[-1][HOST_STAGE_KEY]
    (wd / "looplab_stages.json").unlink()
    chain = eng2._resolve_stages(str(wd), eng2._eval_spec, params={})
    assert [s["name"] for s in chain] == ["score"]
    # the operator's own canonical pipeline: verbatim, then the host
    eng3 = _engine(tmp_path / "c", _task(tmp_path, command=False, stages=[
        {"name": "prep", "command": ["python", "p.py"]},
        {"name": "train", "command": ["python", "t.py"]}]), _Dev())
    chain = eng3._resolve_stages(str(wd), eng3._eval_spec, params={})
    assert [s["name"] for s in chain] == ["prep", "train", "score"] and chain[-1][HOST_STAGE_KEY]
    # under `require`, the derived `needs` lands on the HOST stage, the one that scores
    eng3.metric_subject = "require"
    eng3._eval_spec["metric"] = dict(_M, subject=["out/model.bin"])
    chain = eng3._resolve_stages(str(wd), eng3._eval_spec, params={})
    assert chain[-1]["needs"] == ["out/model.bin"] and "needs" not in chain[-2]
    assert command_eval.HOST_STAGE_KEY == "host"


# -------------------------------------------------------------------------------- 3. THE RUNTIME
_TRAIN = ("import json, os\n"
          "os.makedirs('out', exist_ok=True)\n"
          "open('out/model.bin', 'wb').write(b'weights-v1')\n"
          "print(json.dumps({'metric': 0.99}))\n")           # the candidate's own (over-)report
_HOST_SUBJECT = ("import hashlib, json, sys\n"
                 "data = open(sys.argv[1], 'rb').read()\n"
                 "print('scored', sys.argv[1])\n"
                 "print(json.dumps({'metric': round(len(data) / 40.0, 3)}))\n")   # 10 bytes -> 0.25


def _stages(host_py: Path, *host_args: str):
    return [{"name": "train", "command": [sys.executable, "train.py"]},
            {"name": "score", "command": [sys.executable, str(host_py), *host_args],
             "timeout": 60, HOST_STAGE_KEY: True}]


def test_the_hosts_number_is_the_metric_and_the_candidates_is_recorded_beside_it(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "train.py").write_text(_TRAIN, encoding="utf-8")
    host_py = _host_dir(tmp_path, _HOST_SUBJECT)
    res = run_command_eval(["true"], str(wd), 60, _M, stages=_stages(host_py, SUBJECT_TOKEN),
                           log_dir=str(wd), subject=["out/model.bin"], self_metric=_M)
    assert res.failed_stage is None and res.exit_code == 0
    assert res.metric == 0.25 and res.self_metric == 0.99
    # %subject% expanded to the ONE bound artifact, and the scorer really read it
    assert f"scored {(wd / 'out' / 'model.bin').resolve()}" in res.stdout
    assert res.metric_subject["subject_bound"] is True
    # the receipt: the program that ran, digested at the stage's start
    receipt = res.host_scorer
    assert receipt["program"] == str(host_py)
    assert receipt["program_sha256"] == hashlib.sha256(host_py.read_bytes()).hexdigest()
    assert receipt["program_size"] == len(host_py.read_bytes())
    assert receipt["argv"][1] == str(host_py) and SUBJECT_TOKEN not in receipt["argv"]
    rows = {row["name"]: row for row in res.stages}
    assert rows["train"]["status"] == "ok" and rows["score"]["status"] == "ok"


def test_an_unbound_subject_fails_the_host_stage_as_a_missing_input(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "train.py").write_text("print('{\"metric\": 0.99}')\n", encoding="utf-8")   # writes nothing
    host_py = _host_dir(tmp_path, _HOST_SUBJECT)
    res = run_command_eval(["true"], str(wd), 60, _M, stages=_stages(host_py, SUBJECT_TOKEN),
                           log_dir=str(wd), subject=["out/model.bin"], self_metric=_M)
    assert res.failed_stage == "score" and res.metric is None and res.self_metric is None
    rows = {row["name"]: row for row in res.stages}
    assert rows["score"]["status"] == "needs_failed" and "missing" in rows["score"]["concern"]
    assert "declared input contract" in res.stderr
    # and with no subject declared at all, the token cannot expand and says which fix
    res2 = run_command_eval(["true"], str(wd), 60, _M, stages=_stages(host_py, SUBJECT_TOKEN),
                            log_dir=str(wd), self_metric=_M)
    assert res2.failed_stage == "score" and "declares no" in res2.stderr


def test_a_failing_host_scorer_never_falls_back_to_the_self_report(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "train.py").write_text(_TRAIN, encoding="utf-8")
    host_py = _host_dir(tmp_path, "import sys\nprint('{\"metric\": 0.99}')\nsys.exit(3)\n")
    res = run_command_eval(["true"], str(wd), 60, _M, stages=_stages(host_py), log_dir=str(wd),
                           self_metric=_M)
    assert res.failed_stage == "score" and res.exit_code == 3
    # the stall-salvage read is over the HOST's stdout and gated on `stalled`, never the self-report
    assert res.self_metric is None
    assert res.host_scorer is None or res.host_scorer["program"] == str(host_py)


def test_expand_subject_truth_table():
    one = {"subject_bound": True, "subjects": [{"bound": True, "path": "/w/a"}]}
    assert expand_subject(["p", SUBJECT_TOKEN], one) == (["p", "/w/a"], None)
    rel = {"subject_bound": True, "subjects": [{"bound": True, "path": "out/a.bin"}]}
    assert expand_subject(["p", SUBJECT_TOKEN], rel, "/w") == (["p", "/w/out/a.bin"], None)
    assert expand_subject(["p", SUBJECT_TOKEN], rel) == (["p", "out/a.bin"], None)
    assert expand_subject(["p", "x"], None) == (["p", "x"], None)
    assert "declares no" in expand_subject(["p", SUBJECT_TOKEN], None)[1]
    two = {"subject_bound": True, "subjects": [{"bound": True, "path": "/w/a"},
                                                {"bound": True, "path": "/w/b"}]}
    assert "several" in expand_subject(["p", SUBJECT_TOKEN], two)[1]
    unbound = {"subject_bound": False, "unbound_reason": "stale", "subjects": [{"bound": False}]}
    assert "stale" in expand_subject(["p", SUBJECT_TOKEN], unbound)[1]
    assert host_program_token(["/usr/bin/python3", "-u", "/opt/s.py"]) == "/opt/s.py"
    assert host_program_token(["python", "s.py"]) is None


# --------------------------------------------------------------------- 4. THE RECORD, end to end
def test_the_folded_champion_carries_the_host_number_and_the_receipt(tmp_path):
    task = _task(tmp_path)
    engine = _engine(tmp_path, task, _Dev())
    host_py = Path(task.eval.host_scorer.command[1])
    before = host_py.read_bytes()
    state = anyio.run(engine.run)
    assert state.finished
    best = state.best()
    assert best is not None
    # the HOST's number is the metric; the candidate's own printed number rides beside it
    assert best.metric == -0.5 and best.self_metric == -1.0
    assert best.self_report_gap == pytest.approx(-0.5)         # under-reported, direction-aware
    receipt = best.metric_provenance["host_scorer"]
    assert receipt["program_sha256"] == hashlib.sha256(before).hexdigest()
    # every scored node was scored by the SAME program: the consistency claim, as a check
    scored = [n for n in state.nodes.values() if n.metric is not None]
    assert scored and len({n.metric_provenance["host_scorer"]["program_sha256"] for n in scored}) == 1
    assert all(n.self_metric == -1.0 and n.metric == -0.5 for n in scored)
    assert host_py.read_bytes() == before                        # nothing the run did touched it
    # the fold is the record: a fresh replay says exactly the same
    again = fold(engine.store.read_all())
    b2 = again.best()
    assert (b2.metric, b2.self_metric, b2.self_report_gap) == (best.metric, best.self_metric,
                                                                best.self_report_gap)
    assert again.nodes[best.id].metric_provenance["host_scorer"] == receipt
    kinds = [e.type for e in engine.store.read_all()]
    evaluated = [e for e in engine.store.read_all() if e.type == "node_evaluated"]
    assert evaluated and all("self_metric" in e.data for e in evaluated)
    assert "node_evaluated" in kinds


# ------------------------------------------------------------------------ 5. NEGATIVE CONTROL
def test_a_task_without_a_host_scorer_records_nothing_new(tmp_path):
    task = RepoTask(id="fix", goal="maximize metric", direction="max",
                    editable_path=str(FIXTURE), edit_surface=["*.json"], protect=["ttrain.py"],
                    eval=EvalSpec(command=[sys.executable, "ttrain.py"], metric=_M, timeout=60))
    engine = _engine(tmp_path, task, _Dev())
    state = anyio.run(engine.run)
    best = state.best()
    assert best is not None and best.metric == -1.0
    assert best.self_metric is None and best.self_report_gap is None
    assert not (best.metric_provenance or {}).get("host_scorer")
    assert all("self_metric" not in e.data for e in engine.store.read_all()
               if e.type == "node_evaluated")
