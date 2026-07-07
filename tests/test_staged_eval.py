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
