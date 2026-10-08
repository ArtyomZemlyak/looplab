"""`looplab evaluate-track`: a declared evaluation TRACK over settled nodes' preserved artifacts.

2026-10-06: the questions about a finished search (@200 where it scored @20; drift weeks) were
evaluations it never ran, answered by hand-run jobs whose numbers had no way back into the record.
"""
from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from looplab.cli import app
from looplab.core.models import extra_metric_key_is_backfilled, objective_coverage
from looplab.engine.evaluate import _workdir_manifest_digest
from looplab.events.eventstore import EventStore
from looplab.events.replay import fold
from looplab.maintenance.evaluate_track import parse_track_output

_TRACK = ("import json, pathlib, sys; w = pathlib.Path(sys.argv[1]); "
          "print('noise'); print(json.dumps({'FUR@200': float((w / 'ckpt.txt').read_text()), "
          "'beams': 200, 'note': 'x'}))")


def _run(tmp_path, *, stamps=(True, True)):
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "task.snapshot.json").write_text(json.dumps({
        "kind": "repo", "id": "t", "direction": "max", "editable_path": str(tmp_path),
        "eval": {"command": ["python", "x.py"],
                 "metric": {"kind": "stdout_regex", "pattern": "m=([0-9.]+)"},
                 "tracks": {"at200": {"command": [sys.executable, "-c", _TRACK, "{workdir}"],
                                      "keys": ["FUR@200"]}}}}))
    store = EventStore(rd / "events.jsonl")
    store.append("run_started", {"run_id": "run", "task_id": "t", "goal": "max", "direction": "max"})
    for nid, metric, ckpt in ((0, 0.13, "0.36"), (1, 0.12, "0.38")):
        store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                      "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                      "code": f"print({nid})"})
        store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": metric,
                                        "violations": [], "extra_metrics": {"FUR@20": metric},
                                        "extra_metrics_provenance": {"FUR@20": "declared"}})
        wd = rd / "nodes" / f"node_{nid}"
        wd.mkdir(parents=True)
        (wd / "ckpt.txt").write_text(ckpt)
    st = fold(store.read_all())
    for nid, ok in zip((0, 1), stamps):
        stamp = _workdir_manifest_digest(st.nodes[nid]) if ok else "0" * 64
        (rd / "nodes" / f"node_{nid}" / ".looplab-manifest").write_text(stamp, encoding="ascii")
    return rd, store


def _cli(rd, *extra):
    return CliRunner().invoke(app, ["evaluate-track", str(rd), "at200", *extra])


def test_a_dry_run_executes_nothing(tmp_path):
    rd, store = _run(tmp_path)
    out = _cli(rd)
    assert out.exit_code == 0, out.output
    assert "dry run, nothing executed" in out.output and "node 0: would run" in out.output
    assert not [e for e in store.read_all() if e.type == "extra_metrics_imported"]


def test_a_track_records_beside_the_live_metric_and_can_rank_the_run(tmp_path):
    rd, store = _run(tmp_path)
    out = _cli(rd, "--apply")
    assert out.exit_code == 0, out.output
    assert "recorded on 2 node(s)" in out.output
    st = fold(store.read_all())
    assert st.nodes[0].extra_metrics == {"FUR@20": 0.13, "FUR@200": 0.36}
    assert extra_metric_key_is_backfilled(st.nodes[0], "FUR@200")
    assert not extra_metric_key_is_backfilled(st.nodes[0], "FUR@20")
    rows = [e.data for e in store.read_all() if e.type == "extra_metrics_imported"]
    assert {r["source"] for r in rows} == {"track at200"}
    store.append("metric_retarget", {"key": "FUR@200"})
    st = fold(store.read_all())
    assert objective_coverage(st) == ("FUR@200", [0, 1], []) and st.best_node_id == 1
    assert (rd / "track_at200.log").exists()


def test_a_workdir_that_is_not_the_evaluated_code_is_refused(tmp_path):
    rd, store = _run(tmp_path, stamps=(True, False))
    out = _cli(rd, "--apply", "--nodes", "0,1")
    assert "node 1: refused — its workdir holds another lifecycle's or code's files" in out.output
    st = fold(store.read_all())
    assert "FUR@200" in st.nodes[0].extra_metrics and "FUR@200" not in st.nodes[1].extra_metrics


def test_an_undeclared_track_is_refused_with_the_declared_ones(tmp_path):
    rd, _store = _run(tmp_path)
    out = CliRunner().invoke(app, ["evaluate-track", str(rd), "w07"])
    assert out.exit_code == 2 and "declares no eval.tracks.w07 (declared: at200)" in out.output


def test_the_last_json_object_is_read_and_filtered():
    stdout = '{"a": 1}\nprogress...\n{"FUR@200": 0.3, "beams": 200, "flag": true, "nan": NaN}\n'
    assert parse_track_output(stdout) == {"FUR@200": 0.3, "beams": 200.0}
    assert parse_track_output(stdout, keys=["FUR@200"], prefix="w07/") == {"w07/FUR@200": 0.3}
    assert parse_track_output("no json here") == {}


def test_a_bad_input_is_a_refusal_and_a_bug_keeps_its_traceback(tmp_path, monkeypatch):
    """The operator's own input is refused on purpose (`ConfigRefusal`, one line, exit 2); a plain
    `ValueError` from anywhere else is a bug and is never printed as a tidy refusal."""
    rd, _store = _run(tmp_path)
    out = _cli(rd, "--nodes", "three")
    assert out.exit_code == 2 and "--nodes must be 'all'" in out.output
    from looplab.maintenance import evaluate_track as module

    def _bug(*_a, **_k):
        raise ValueError("an internal defect")

    monkeypatch.setattr(module, "evaluate_track", _bug)
    out = _cli(rd)
    assert out.exit_code == 1 and isinstance(out.exception, ValueError)


def test_a_fifo_stamp_is_refused_not_waited_on(tmp_path):
    """critic 2026-10-08: the candidate can replace its own workdir's stamp; a FIFO there blocked a
    plain read forever while `--apply` held `engine.lock`."""
    import os

    import pytest
    from looplab.maintenance.evaluate_track import track_refusal
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs on this platform")
    rd, store = _run(tmp_path)
    stamp = rd / "nodes" / "node_0" / ".looplab-manifest"
    stamp.unlink()
    os.mkfifo(stamp)
    node = fold(store.read_all()).nodes[0]
    assert track_refusal(rd, node) == "its workdir carries no readable manifest stamp"


def test_an_imported_key_carries_what_measured_it(tmp_path):
    """critic 2026-10-08: `--source` / `track <name>` was written on the row and read by nothing,
    so every surface said the value came from the run's own score log."""
    from looplab.core.models import extra_metric_source
    rd, store = _run(tmp_path)
    assert _cli(rd, "--apply").exit_code == 0
    st = fold(store.read_all())
    assert extra_metric_source(st.nodes[0], "FUR@200") == "track at200"
    assert extra_metric_source(st.nodes[0], "FUR@20") is None


def test_a_workdir_relative_track_script_runs(tmp_path):
    """critic 2026-10-08, second round (driven): the documented `python score_service.py` names a
    file in the node's workdir, which is where a track without credentials runs."""
    rd, store = _run(tmp_path)
    snap = json.loads((rd / "task.snapshot.json").read_text())
    snap["eval"]["tracks"]["at200"]["command"] = [sys.executable, "score_service.py", "--ckpt",
                                                  "{workdir}/ckpt.txt"]
    (rd / "task.snapshot.json").write_text(json.dumps(snap))
    for nid in (0, 1):
        (rd / "nodes" / f"node_{nid}" / "score_service.py").write_text(
            "import json,sys; print(json.dumps({'FUR@200': float(open(sys.argv[2]).read())}))")
    out = _cli(rd, "--apply")
    assert "recorded on 2 node(s)" in out.output, out.output
