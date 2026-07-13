"""Phase 1 — multi-stage eval pipeline (data_prep → train → eval). Stages run in order in ONE workdir
(artifacts persist), each pass/fail tracked; the first failure stops the pipeline and pinpoints the
stage; the last stage's stdout is read for the metric. The fold records per-node stage outcomes (+ the
failed stage) and a stage-scoped reset clears them."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from looplab.core.models import Event, NodeStatus
from looplab.events.replay import fold
from looplab.runtime.command_eval import run_command_eval


def _ev(t, d, s):
    return Event(seq=s, ts=0.0, type=t, data=d)


# --------------------------------------------------------------- runtime (command_eval)

def test_stage_failure_stops_pipeline_and_pinpoints():
    d = tempfile.mkdtemp()
    stages = [{"name": "data_prep", "command": ["python", "-c", "print('ok')"]},
              {"name": "train", "command": ["python", "-c", "import sys; sys.exit(2)"]},
              {"name": "eval", "command": ["python", "-c", "print('{\"m\": 1}')"]}]
    r = run_command_eval(["true"], d, 30, {"kind": "stdout_json", "key": "m"}, stages=stages)
    assert r.metric is None and r.failed_stage == "train"
    assert [s["status"] for s in r.stages] == ["ok", "fail"]     # eval never ran
    assert all(s["name"] != "eval" for s in r.stages)


def test_all_stages_pass_metric_from_last_and_artifacts_persist():
    d = tempfile.mkdtemp()
    Path(d, "prep.py").write_text("open('data','w').write('x'); print('prep')")
    Path(d, "ev.py").write_text("import os,json; assert os.path.exists('data'); print(json.dumps({'m': 0.5}))")
    stages = [{"name": "prep", "command": ["python", "prep.py"]},
              {"name": "eval", "command": ["python", "ev.py"]}]
    r = run_command_eval(["true"], d, 30, {"kind": "stdout_json", "key": "m"}, stages=stages)
    assert r.metric == 0.5 and r.failed_stage is None            # prep artifact reached eval
    assert [s["status"] for s in r.stages] == ["ok", "ok"]


# --------------------------------------------------------------- fold

def _created(nid=0):
    return _ev("node_created", {"node_id": nid, "operator": "draft",
                                "idea": {"operator": "draft", "params": {}}, "code": "c"}, nid)


def test_fold_records_stage_outcomes_and_failed_stage():
    st = fold([_created(0),
               _ev("stage_finished", {"node_id": 0, "name": "prep", "status": "ok"}, 1),
               _ev("stage_finished", {"node_id": 0, "name": "train", "status": "fail"}, 2),
               _ev("node_failed", {"node_id": 0, "error": "boom", "reason": "crash",
                                   "failed_stage": "train"}, 3)])
    n = st.nodes[0]
    assert [s["name"] for s in n.stages] == ["prep", "train"]
    assert n.failed_stage == "train" and n.status is NodeStatus.failed


def test_fold_stage_is_last_wins_by_name_and_reset_clears():
    # a stage re-run replaces the prior outcome (not append); a from-implement reset wipes stages.
    base = [_created(0),
            _ev("stage_finished", {"node_id": 0, "name": "train", "status": "fail"}, 1),
            _ev("stage_finished", {"node_id": 0, "name": "train", "status": "ok"}, 2)]      # re-ran, now ok
    st = fold(base)
    assert len(st.nodes[0].stages) == 1 and st.nodes[0].stages[0]["status"] == "ok"
    st2 = fold(base + [_ev("node_reset", {"node_id": 0, "from_stage": "implement"}, 3)])
    assert st2.nodes[0].stages == [] and st2.nodes[0].failed_stage is None


# --------------------------------------------------------------- Phase 2: stage-scoped re-run

def test_reset_from_pipeline_stage_sets_rerun_stage_and_clears_on_terminal():
    base = [_created(0),
            _ev("stage_finished", {"node_id": 0, "name": "train", "status": "ok"}, 1),
            _ev("stage_finished", {"node_id": 0, "name": "eval", "status": "fail"}, 2),
            _ev("node_failed", {"node_id": 0, "error": "e", "reason": "crash", "failed_stage": "eval"}, 3)]
    # reset from the 'eval' stage → node pending-with-code, rerun_stage='eval' (skip train on re-run)
    st = fold(base + [_ev("node_reset", {"node_id": 0, "from_stage": "eval"}, 4)])
    assert st.nodes[0].status is NodeStatus.pending
    assert st.nodes[0].rerun_stage == "eval" and st.nodes[0].rerun_from is None
    assert st.nodes[0].failed_stage is None and st.nodes[0].code == "c"   # code kept (eval-type)
    # the re-run's terminal clears the marker
    st2 = fold(base + [_ev("node_reset", {"node_id": 0, "from_stage": "eval"}, 4),
                        _ev("stage_finished", {"node_id": 0, "generation": 1,
                                               "name": "eval", "status": "ok"}, 5),
                        _ev("node_evaluated", {"node_id": 0, "generation": 1,
                                               "metric": 0.9}, 6)])
    assert st2.nodes[0].rerun_stage is None and st2.nodes[0].metric == 0.9


# --------------------------------------------------------------- Phase 3: inter-stage verify

def test_inter_stage_check_stops_pipeline_on_concern():
    d = tempfile.mkdtemp()
    Path(d, "ev.py").write_text("import json; print(json.dumps({'m': 0.9}))")
    stages = [{"name": "train", "command": ["python", "-c", "print('flat loss')"], "check": True},
              {"name": "eval", "command": ["python", "ev.py"]}]
    seen = []

    def checker(name, tail):
        seen.append(name)
        return "train produced no checkpoint" if name == "train" else None
    r = run_command_eval(["true"], d, 30, {"kind": "stdout_json", "key": "m"},
                         stages=stages, check_fn=checker)
    assert seen == ["train"]                                  # eval's check never reached
    assert r.metric is None and r.failed_stage == "train"
    assert [s["status"] for s in r.stages] == ["check_failed"]   # eval never ran
    assert "verification" in r.stderr


def test_inter_stage_check_ok_lets_pipeline_continue():
    d = tempfile.mkdtemp()
    Path(d, "ev.py").write_text("import json; print(json.dumps({'m': 0.7}))")
    stages = [{"name": "train", "command": ["python", "-c", "print('ok')"], "check": True},
              {"name": "eval", "command": ["python", "ev.py"]}]
    r = run_command_eval(["true"], d, 30, {"kind": "stdout_json", "key": "m"},
                         stages=stages, check_fn=lambda name, tail: None)   # always OK
    assert r.metric == 0.7 and [s["status"] for s in r.stages] == ["ok", "ok"]


def test_stage_check_is_sanity_only_and_names_the_objective(tmp_path, monkeypatch):
    # Regression (node-21 incident): the inter-stage checker FAILED the run's best model — it read the
    # loss MAGNITUDE (~14.6) as "no learning" and a bystander recall@50 (0.79) as "the metric, below
    # best". The check must be SANITY-only (hard failures), NAME the objective metric so it can't grab a
    # bystander scalar, and never judge quality/ranking — a healthy-but-high-loss stage must PASS.
    from looplab.core.models import Idea, Node
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    r, d = task.build_roles()
    eng = Engine(tmp_path / "r", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=3))
    eng._eval_spec = {"metric": {"reader": "stdout_regex", "pattern": "RECALL@100: ([0-9.]+)"}}
    node = Node(id=1, operator="improve",
                idea=Idea(operator="improve", params={}, rationale="two-stage temperature schedule"))

    class Fake:
        def __init__(self, reply):
            self.reply = reply
            self.seen = []

        def complete_text(self, msgs):
            self.seen.append(msgs)
            return self.reply

    ok = Fake("OK")
    monkeypatch.setattr(eng, "_reflect_client", lambda: ok)
    res = eng._stage_check_fn(node)("train", "Epoch 29 loss=14.6 v_num=0\n0.8549\n0.7902\n")
    assert res is None                                   # healthy-but-high-loss stage PASSES, not a concern
    blob = " ".join(m["content"] for m in ok.seen[0]).upper()
    assert "RECALL@100" in blob                          # objective NAMED, not guessed from bare scalars
    assert "SANITY" in blob and "MAGNITUDE" in blob      # loss magnitude explicitly not a failure signal
    assert "QUALITY" in blob or "RANKING" in blob        # must not judge quality / beat-the-best

    boom = Fake("no checkpoint saved — silent fallback to the pretrained model")
    monkeypatch.setattr(eng, "_reflect_client", lambda: boom)
    res2 = eng._stage_check_fn(node)("train", "Traceback (most recent call last): ...")
    assert res2 and "checkpoint" in res2                 # a HARD failure still stops the pipeline
