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
    """The copy-out's rule (`engine/artifact_sync.py`): the lifecycle is fixed when the request is
    ACCEPTED and the workdir held to it when the node's turn comes. A reset while the job waited for
    its GPU used to
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
    assert done["recorded"] == 1 and done["refused"] == {"0": track_lane._NOT_ITS_FILES}
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


# ------------------------------------------------------------------ review 2026-10-08


def _done(eng):
    return [e.data for e in eng.store.read_all() if e.type == "track_done"]


def test_a_worker_that_raises_is_receipted_with_its_cause_and_gives_its_gpus_back(tmp_path,
                                                                                    monkeypatch):
    """An exception past the per-node handlers (here the physical-id fence, `GpuPinUnenforceable`)
    used to kill the thread: the main task then appended `recorded: 0` and the cause was nowhere."""
    from looplab.engine.resources import GpuPinUnenforceable
    eng = _engine(tmp_path, tracks={"g": {"command": [sys.executable, "-c", "print(1)"], "gpus": 1}})
    given = []
    monkeypatch.setattr(eng, "_gpu_ids", [0], raising=False)
    monkeypatch.setattr(eng, "_acquire_gpus", lambda n, mem=None: [0])
    monkeypatch.setattr(eng, "_release_gpus", lambda ids: given.append(list(ids)))
    monkeypatch.setattr(eng, "_gpu_pool_epoch", lambda: 0)

    def _unenforceable(ids):
        raise GpuPinUnenforceable("reserved GPU has no trustworthy physical selector")

    monkeypatch.setattr(eng, "_physical_gpu_ids", _unenforceable)
    eng.store.append("track_requested", {"track": "g", "node_ids": [0]})
    _serve_until_done(eng)
    done = _done(eng)[-1]
    assert done["recorded"] == 0 and "GpuPinUnenforceable" in done["failed"]["-1"], done
    assert given == [[0]], "the lease is given back whatever the worker died of"


def test_one_nodes_defect_is_its_own_receipt_row_and_the_rest_are_measured(tmp_path, monkeypatch):
    eng = _engine(tmp_path)
    real = track_lane.track_refusal

    def _refusal(run_dir, node, generation=None):
        if node.id == 0:
            raise RecursionError("maximum recursion depth exceeded")
        return real(run_dir, node, generation)

    monkeypatch.setattr(track_lane, "track_refusal", _refusal)
    eng.store.append("track_requested", {"track": "at200", "node_ids": "all"})
    _serve_until_done(eng)
    done = _done(eng)[-1]
    assert done["recorded"] == 1 and "RecursionError" in done["failed"]["0"], done
    assert fold(eng.store.read_all()).nodes[1].extra_metrics == {"FUR@200": 0.38}


def test_a_deeply_nested_output_line_is_no_number_not_a_crash():
    deep = "{\"a\": " + "[" * 200_000 + "]" * 200_000 + "}"
    assert track_lane.parse_track_output(deep + "\n") == {}
    assert track_lane.parse_track_output('{"v": 1}\n' + deep) == {"v": 1.0}


def test_the_receipt_counts_what_the_fold_keeps(tmp_path):
    """A node that already carries every measured key gains nothing: no row, `recorded` excludes it,
    and the receipt says why (the fold drops a carried key; the writer used to count it)."""
    eng = _engine(tmp_path)
    eng.store.append("extra_metrics_imported", {
        "node_id": 0, "generation": 0, "extra_metrics": {"FUR@200": 0.5}, "source": "svc",
        "imported_at": 1.0})
    eng.store.append("track_requested", {"track": "at200", "node_ids": "all"})
    _serve_until_done(eng)
    done = _done(eng)[-1]
    assert done["recorded"] == 1 and done["refused"] == {"0": "it already carries every key measured"}
    rows = [e.data for e in eng.store.read_all() if e.type == "extra_metrics_imported"]
    assert [r["node_id"] for r in rows] == [0, 1]       # the operator's import, then node 1 only
    assert fold(eng.store.read_all()).nodes[0].extra_metrics == {"FUR@200": 0.5}


def test_start_reads_no_workdir_on_the_main_task(tmp_path, monkeypatch):
    """review 2026-10-08: `_start` read every node's stamp twice on the event loop. It decides on the
    fold alone; the worker holds each workdir to the accepted lifecycle."""
    from looplab.engine import artifact_sync
    eng = _engine(tmp_path, tracks={"slow": {"command": [sys.executable, "-c",
                                                         "import time; time.sleep(30)"]}})
    import threading
    main = threading.get_ident()
    reads = []
    real = artifact_sync.workdir_stamp
    monkeypatch.setattr(artifact_sync, "workdir_stamp",
                        lambda wd: reads.append(threading.get_ident()) or real(wd))
    eng.store.append("track_requested", {"track": "slow", "node_ids": "all"})
    job = track_lane._start(eng, fold(eng.store.read_all()), 0)
    try:
        assert main not in reads
    finally:
        eng._track_lane.job = job
        track_lane.cancel_track_lane(eng)


def test_a_cancelled_worker_is_joined_its_command_killed_and_nothing_logged(tmp_path):
    """review 2026-10-08: the worker was a daemon, cancelled and never joined, so its command could
    outlive the engine and its log land after `engine.lock` was released."""
    import time
    from looplab.serve.run_commands import _process_alive
    pidfile = tmp_path / "child.pid"
    slow = (f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
            "time.sleep(60); print('{\"v\": 1}')")
    eng = _engine(tmp_path, tracks={"slow": {"command": [sys.executable, "-c", slow]}})
    eng.store.append("track_requested", {"track": "slow", "node_ids": [0]})
    anyio.run(track_lane.serve_track_requests, eng, fold(eng.store.read_all()))
    job = eng._track_lane.job
    assert job.thread.daemon is False
    for _ in range(500):
        if pidfile.exists() and pidfile.read_text():
            break
        time.sleep(0.01)
    pid = int(pidfile.read_text())
    assert _process_alive(pid) is True, "the probe sees the command running before the cancel"
    track_lane.cancel_track_lane(eng)
    assert not job.thread.is_alive(), "joined before the engine lets go of its run"
    # Never `os.kill(pid, 0)`: on Windows signal 0 is CTRL_C_EVENT, not a probe (WinError 87 here).
    assert _process_alive(pid) is False, "the cancel tree-killed the command"
    assert not (tmp_path / "run" / "track_slow.log").exists()
    assert not _done(eng)


def test_the_drain_stops_on_a_pause_and_leaves_the_request_queued(tmp_path):
    eng = _engine(tmp_path, tracks={"slow": {"command": [sys.executable, "-c",
                                                         "import time; time.sleep(30)"]}})
    eng.store.append("track_requested", {"track": "slow", "node_ids": [0]})
    eng.store.append("track_requested", {"track": "slow", "node_ids": [1]})

    async def go():
        async with anyio.create_task_group() as tg:
            tg.start_soon(track_lane.drain_track_requests, eng, fold)
            await anyio.sleep(0.3)
            eng.store.append("pause", {"reason": "operator"})
    anyio.run(go)
    st = fold(eng.store.read_all())
    assert st.tracks_done == 0 and not _done(eng), "no receipt: the queue waits for an engine"
    assert eng._track_lane.job is None


def test_the_drain_stops_when_the_wall_clock_is_spent(tmp_path):
    import time
    eng = _engine(tmp_path)
    eng.max_seconds = 5.0
    eng.store.append("track_requested", {"track": "at200", "node_ids": [0]})
    anyio.run(lambda: track_lane.drain_track_requests(eng, fold, started_at=time.time() - 10))
    assert fold(eng.store.read_all()).tracks_done == 0 and not _done(eng)


def test_a_search_ended_finish_waits_for_the_queue_and_a_stop_does_not(tmp_path):
    from looplab.core.models import RunState
    eng = _engine(tmp_path)
    eng.store.append("track_requested", {"track": "at200", "node_ids": [0]})
    st = fold(eng.store.read_all())
    for reason in ("aborted", "time_budget", "leakage", "error", "budget_exhausted",
                   "stuck: node creation not converging", "systemic failure: 3 node(s) failed"):
        assert track_lane.refuse_finish_over_track_queue(eng, st, {"reason": reason}) is False
    assert track_lane.refuse_finish_over_track_queue(eng, RunState(paused=True), {}) is False
    assert not track_lane.track_drain_due(eng)
    for data in ({}, {"reason": "eval_budget"}, {"reason": "no_eligible_candidate"}):
        assert track_lane.refuse_finish_over_track_queue(eng, st, data) is True
    assert track_lane.track_drain_due(eng)
    anyio.run(lambda: track_lane.drain_track_requests(eng, fold))
    assert not track_lane.track_drain_due(eng)
    st = fold(eng.store.read_all())
    assert st.tracks_done == 1
    assert track_lane.refuse_finish_over_track_queue(eng, st, {}) is False


def test_a_finishing_run_records_its_tracks_before_its_finish_scope_opens(tmp_path, monkeypatch):
    """review 2026-10-08, driven through `Engine.run`: the queue used to be drained AFTER the loop,
    inside the open finalize scope, so its folded rows made the staged finish read as abandoned
    (`events/finalize_scope.py::finalize_scope_quiescent`). The receipt now lands first, the scope
    completes, and the run finishes."""
    from looplab.events.finalize_scope import incomplete_finalize_scope
    # Slow enough that the search finishes while it runs: the finish must wait for it.
    spec = {"command": [sys.executable, "-c", "import time; time.sleep(1.5); print('{\"v\": 1.0}')"]}
    monkeypatch.setattr(track_lane, "track_spec", lambda _es, _t: spec)
    # The toy task writes no manifest stamp; what is under test is WHEN the rows land.
    monkeypatch.setattr(track_lane, "track_refusal", lambda *_a, **_k: None)
    eng = make_engine(tmp_path / "run", n_seeds=1, max_nodes=1)
    store_append = eng.store.append

    def _append(kind, data, **kwargs):
        # The operator asks the moment the search's only node lands — the search then ends at once.
        row = store_append(kind, data, **kwargs)
        if kind == "node_evaluated":
            store_append("track_requested", {"track": "t", "node_ids": [data["node_id"]]})
        return row

    monkeypatch.setattr(eng.store, "append", _append)
    anyio.run(eng.run)
    events = eng.store.read_all()
    st = fold(events)
    assert st.finished and st.tracks_done == 1
    done = next(e for e in events if e.type == "track_done")
    assert done.data["recorded"] == 1, done.data
    begun = next(e.seq for e in events if e.type == "finalize_step"
                 and (e.data or {}).get("step") == "begun")
    assert done.seq < begun
    assert incomplete_finalize_scope(events) is None


def test_track_held_gpus_are_visible_to_the_dispatcher(tmp_path):
    import threading
    eng = _engine(tmp_path)
    assert track_lane.track_gpus_held(eng) is False
    gate = threading.Event()
    job = track_lane.TrackJob(idx=0, track="g", gpus_held=2)
    job.thread = threading.Thread(target=gate.wait, args=(10,))
    job.thread.start()
    eng._track_lane.job = job
    try:
        assert track_lane.track_gpus_held(eng) is True
    finally:
        gate.set()
        job.thread.join(5)
    assert track_lane.track_gpus_held(eng) is False, "a finished job holds nothing"


def test_a_relative_run_dir_renders_absolute_paths(tmp_path, monkeypatch):
    """review 2026-10-08, reproduced: `looplab run --out runs/demo` rendered `{workdir}` relative to
    a command whose cwd IS that workdir, so the path resolved twice and the scorer found nothing."""
    import os
    monkeypatch.chdir(tmp_path)
    eng = _engine(tmp_path)
    eng.run_dir = type(eng.run_dir)(os.path.relpath(eng.run_dir, tmp_path))
    assert not eng.run_dir.is_absolute()
    eng.store.append("track_requested", {"track": "at200", "node_ids": [0]})
    _serve_until_done(eng)
    assert fold(eng.store.read_all()).nodes[0].extra_metrics == {"FUR@200": 0.36}
