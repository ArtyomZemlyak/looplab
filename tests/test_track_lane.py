"""Evaluation tracks on a LIVE run (doc 73 §1.4, `engine/track_lane.py`).

`looplab evaluate-track` answers "what is node N at @200 / on drift week 7?" only on a stopped run
(it holds `engine.lock`). A `track_requested` intent is served by the live engine instead: a worker
runs the operator's argv, the MAIN task records `extra_metrics_imported` + `track_done`.
"""
from __future__ import annotations

import sys

import anyio

from factories import make_engine
from looplab.engine import track_lane
from looplab.engine.evaluate import workdir_manifest_digest
from looplab.events.replay import fold

_TRACK = ("import json, pathlib, sys; w = pathlib.Path(sys.argv[1]); "
          "print(json.dumps({'FUR@200': float((w / 'ckpt.txt').read_text())}))")


def _engine(tmp_path, *, tracks=None):
    eng = make_engine(tmp_path / "run")
    eng._eval_spec = {"tracks": tracks if tracks is not None else {
        "at200": {"command": [sys.executable, "-c", _TRACK, "{workdir}"], "keys": ["FUR@200"]}}}
    for nid, ckpt in ((0, "0.36"), (1, "0.38")):
        eng.store.append("node_created", {"node_id": nid, "parent_ids": [], "operator": "draft",
                                          "idea": {"operator": "draft", "params": {}, "rationale": "r"},
                                          "code": f"print({nid})"})
        eng.store.append("node_evaluated", {"node_id": nid, "generation": 0, "metric": 0.1,
                                            "violations": []})
    st = fold(eng.store.read_all())
    for nid, ckpt in ((0, "0.36"), (1, "0.38")):
        wd = tmp_path / "run" / "nodes" / f"node_{nid}"
        wd.mkdir(parents=True)
        (wd / "ckpt.txt").write_text(ckpt)
        (wd / ".looplab-manifest").write_text(workdir_manifest_digest(st.nodes[nid]), encoding="ascii")
    return eng


def _serve_until_done(eng):
    async def go():
        for _ in range(400):
            st = fold(eng.store.read_all())
            if await track_lane.serve_track_requests(eng, st):
                return
            await anyio.sleep(0.02)
        raise AssertionError("the track was never harvested")
    anyio.run(go)


def test_a_queued_track_is_run_by_the_live_engine_and_recorded_by_its_main_task(tmp_path):
    eng = _engine(tmp_path)
    eng.store.append("track_requested", {"track": "at200", "node_ids": "all"})
    _serve_until_done(eng)
    st = fold(eng.store.read_all())
    assert st.tracks_done == 1
    assert st.nodes[0].extra_metrics == {"FUR@200": 0.36}
    assert st.nodes[1].extra_metrics == {"FUR@200": 0.38}
    done = [e.data for e in eng.store.read_all() if e.type == "track_done"]
    assert done == [{"idx": 0, "track": "at200", "recorded": 2}]


def test_a_refused_node_and_an_undeclared_track_are_receipted_not_raised(tmp_path):
    eng = _engine(tmp_path)
    (tmp_path / "run" / "nodes" / "node_1" / ".looplab-manifest").write_text("0" * 64)
    eng.store.append("track_requested", {"track": "at200", "node_ids": [0, 1, 7]})
    _serve_until_done(eng)
    eng.store.append("track_requested", {"track": "w07", "node_ids": "all"})
    _serve_until_done(eng)
    done = [e.data for e in eng.store.read_all() if e.type == "track_done"]
    assert done[0]["recorded"] == 1 and set(done[0]["refused"]) == {"1", "7"}
    assert done[1]["recorded"] == 0 and "eval.tracks.w07" in done[1]["failed"]["-1"]
    assert fold(eng.store.read_all()).tracks_done == 2


def test_a_cancelled_job_leaves_its_request_queued_for_the_next_engine(tmp_path):
    eng = _engine(tmp_path, tracks={"slow": {"command": [sys.executable, "-c",
                                                         "import time; time.sleep(30)"]}})
    eng.store.append("track_requested", {"track": "slow", "node_ids": [0]})

    async def go():
        st = fold(eng.store.read_all())
        assert await track_lane.serve_track_requests(eng, st) is False      # started
        eng._track_lane.cancel()
        for _ in range(400):
            if eng._track_lane.job.done:
                break
            await anyio.sleep(0.02)
        assert await track_lane.serve_track_requests(eng, fold(eng.store.read_all())) is False
    anyio.run(go)
    assert fold(eng.store.read_all()).tracks_done == 0
    assert not [e for e in eng.store.read_all() if e.type == "track_done"]


def test_a_finishing_run_answers_the_queue_before_it_closes(tmp_path):
    eng = _engine(tmp_path)
    eng.store.append("track_requested", {"track": "at200", "node_ids": [0]})
    eng.store.append("track_requested", {"track": "at200", "node_ids": [1]})
    anyio.run(track_lane.drain_track_requests, eng, fold)
    st = fold(eng.store.read_all())
    assert st.tracks_done == 2 and "FUR@200" in st.nodes[1].extra_metrics


def test_a_paused_run_starts_no_track():
    from looplab.core.models import RunState
    st = RunState(paused=True)
    st.track_requests = [{"track": "at200", "node_ids": "all"}]

    class _E:
        _track_lane = track_lane.TrackLane()

    assert anyio.run(track_lane.serve_track_requests, _E(), st) is False
    assert _E._track_lane.job is None


def test_a_gpu_track_leases_from_the_runs_pool_and_gives_the_devices_back(tmp_path, monkeypatch):
    show = ("import json, os; print(json.dumps({'dev': float(os.environ['CUDA_VISIBLE_DEVICES'] "
            "or -1)}))")
    eng = _engine(tmp_path, tracks={"g": {"command": [sys.executable, "-c", show], "gpus": 1}})
    calls = []
    monkeypatch.setattr(eng, "_gpu_ids", [0, 1], raising=False)
    monkeypatch.setattr(eng, "_acquire_gpus", lambda n, mem=None: calls.append(("take", n)) or [1])
    monkeypatch.setattr(eng, "_release_gpus", lambda ids: calls.append(("give", list(ids))))
    monkeypatch.setattr(eng, "_physical_gpu_ids", lambda ids: [str(7 + i) for i in ids])
    monkeypatch.setattr(eng, "_gpu_pool_epoch", lambda: 0)
    eng.store.append("track_requested", {"track": "g", "node_ids": [0]})
    _serve_until_done(eng)
    assert fold(eng.store.read_all()).nodes[0].extra_metrics == {"dev": 8.0}, "fenced to its lease"
    assert calls == [("take", 1), ("give", [1])]


def test_a_node_reset_after_the_request_was_accepted_is_refused_not_measured(tmp_path, monkeypatch):
    """The copy-out's rule (`engine/artifact_sync.py`): the workdir stamp is read when the request is
    ACCEPTED and again when the node's turn comes. A reset while the job waited for its GPU used to
    measure the next lifecycle's files and record them under the lifecycle the request named."""
    eng = _engine(tmp_path, tracks={"g": {"command": [sys.executable, "-c", _TRACK, "{workdir}"],
                                          "keys": ["FUR@200"], "gpus": 1}})
    manifest = tmp_path / "run" / "nodes" / "node_0" / ".looplab-manifest"

    def take(n, mem=None):
        manifest.write_text("1" * 64)           # the reset lands while the job waits for its lease
        return [0]

    monkeypatch.setattr(eng, "_gpu_ids", [0], raising=False)
    monkeypatch.setattr(eng, "_acquire_gpus", take)
    monkeypatch.setattr(eng, "_release_gpus", lambda ids: None)
    monkeypatch.setattr(eng, "_physical_gpu_ids", lambda ids: [str(i) for i in ids])
    monkeypatch.setattr(eng, "_gpu_pool_epoch", lambda: 0)
    eng.store.append("track_requested", {"track": "g", "node_ids": [0, 1]})
    _serve_until_done(eng)
    done = [e.data for e in eng.store.read_all() if e.type == "track_done"][-1]
    assert done["recorded"] == 1 and done["refused"] == {"0": track_lane._REBUILT_BEFORE}
    assert fold(eng.store.read_all()).nodes[0].extra_metrics in (None, {})


def test_a_workdir_rebuilt_while_its_command_ran_is_refused(tmp_path):
    rewrite = ("import json, pathlib, sys; w = pathlib.Path(sys.argv[1]); "
               "(w / '.looplab-manifest').write_text('2' * 64); print(json.dumps({'v': 1.0}))")
    eng = _engine(tmp_path, tracks={"r": {"command": [sys.executable, "-c", rewrite, "{workdir}"]}})
    eng.store.append("track_requested", {"track": "r", "node_ids": [0]})
    _serve_until_done(eng)
    done = [e.data for e in eng.store.read_all() if e.type == "track_done"][-1]
    assert done["recorded"] == 0 and done["refused"] == {"0": track_lane._REBUILT_DURING}


def test_the_track_log_and_failure_detail_are_masked_in_every_spelling(tmp_path, monkeypatch):
    from urllib.parse import quote
    secret = "zq8Vn3kPw7Lr2Xt9/+x"
    monkeypatch.setenv("MC_HOST_minio", secret)
    leak = ("import os, sys, urllib.parse as u; s = os.environ['MC_HOST_minio']; "
            "print('GET https://h/?k=' + u.quote(s, safe='')); sys.stderr.write('bad ' + s); "
            "sys.exit(3)")
    eng = _engine(tmp_path, tracks={"m": {"command": [sys.executable, "-c", leak],
                                          "env_passthrough": ["MC_HOST_minio"]}})
    eng.store.append("track_requested", {"track": "m", "node_ids": [0]})
    _serve_until_done(eng)
    log = (tmp_path / "run" / "track_m.log").read_text()
    done = [e.data for e in eng.store.read_all() if e.type == "track_done"][-1]
    for text in (log, str(done["failed"])):
        assert secret not in text and quote(secret, safe="") not in text, text
    assert "***REDACTED_ENV***" in log
