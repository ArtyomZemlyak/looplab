"""doc 82 П6: a tmpfs run root is said out loud, and `looplab ui` can keep it mirrored.

Three pieces, each driven here: the detector (`core/volatile_fs.py`, over a substituted
`/proc/mounts`), the ONE-mirror-per-root lock (`events/run_mirror.py::mirror_lock`: an operator's
`run-mirror --loop` and the UI's child never copy at once), and the UI server's supervisor of a
`run-mirror --loop --exit-on-stuck` child (`serve/mirror_supervisor.py`) with its read
`GET /api/run-mirror`, which the UI's banner reads.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from looplab.core import volatile_fs
from looplab.events.eventstore import EventStore
from looplab.events.run_mirror import MirrorBusy, MirrorConfig, mirror_lock, mirror_loop, mirror_pass
from looplab.serve.mirror_supervisor import MirrorSupervisor
from looplab.serve.server import make_app

MOUNTS = """\
overlay / overlay rw 0 0
tmpfs /dev/shm tmpfs rw 0 0
/dev/sda1 /dev/shm/durable ext4 rw 0 0
fuse /home/jovyan/data fuse.geesefs rw 0 0
tmpfs /mnt/with\\040space tmpfs rw 0 0
"""


@pytest.fixture
def mounts(tmp_path):
    path = tmp_path / "mounts"
    path.write_text(MOUNTS)
    return str(path)


@pytest.mark.posix_only("Linux /proc/mounts filesystem detection")
@pytest.mark.parametrize("path,kind,volatile", [
    ("/dev/shm/looplab-runs", "tmpfs", True), ("/dev/shm/durable/runs", "ext4", False),
    ("/dev/shm", "tmpfs", True), ("/home/jovyan/data/runs", "fuse.geesefs", False),
    ("/srv/runs", "overlay", False), ("/mnt/with space/runs", "tmpfs", True),
])
def test_the_longest_mount_point_decides(mounts, path, kind, volatile):
    assert volatile_fs.mount_type(path, mounts_file=mounts) == kind
    assert volatile_fs.is_volatile(path, mounts_file=mounts) is volatile


def test_no_mount_table_is_unknown_never_a_guess(tmp_path):
    assert volatile_fs.is_volatile("/dev/shm", mounts_file=str(tmp_path / "absent")) is None


def test_the_warning_names_the_fix_and_is_silent_with_a_mirror(monkeypatch, tmp_path):
    monkeypatch.setattr(volatile_fs, "is_volatile", lambda path, **kw: True)
    monkeypatch.delenv("LOOPLAB_RUN_MIRROR_DST", raising=False)
    text = volatile_fs.volatile_warning(tmp_path)
    assert "tmpfs" in text and "LOOPLAB_RUN_MIRROR_DST" in text and "run-mirror" in text
    monkeypatch.setenv("LOOPLAB_RUN_MIRROR_DST", str(tmp_path / "durable"))
    assert volatile_fs.volatile_warning(tmp_path) is None
    monkeypatch.delenv("LOOPLAB_RUN_MIRROR_DST")
    monkeypatch.setattr(volatile_fs, "is_volatile", lambda path, **kw: None)
    assert volatile_fs.volatile_warning(tmp_path) is None     # unknown is not a warning


def _root(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    store = EventStore(src / "r" / "events.jsonl")
    store.append("run_started", {"run_id": "r", "task_id": "t", "goal": "g", "direction": "min"})
    return MirrorConfig(src_root=src, dst_root=dst, interval_s=0.01)


def test_two_mirrors_of_one_root_never_copy_at_once(tmp_path):
    cfg = _root(tmp_path)
    with mirror_lock(cfg):
        with pytest.raises(MirrorBusy):
            mirror_pass(cfg)
        with pytest.raises(MirrorBusy):
            mirror_loop(cfg, passes=1, log=lambda line: None)
    report = mirror_pass(cfg)                       # released: the next pass copies
    assert report.runs["r"]["copied"] >= 1
    assert (cfg.dst_root / "r" / "events.jsonl").is_file()


def test_the_cli_mirror_refuses_while_another_holds_the_root(tmp_path):
    cfg = _root(tmp_path)
    with mirror_lock(cfg):
        proc = subprocess.run([sys.executable, "-m", "looplab.cli", "run-mirror",
                               "--src", str(cfg.src_root), "--dst", str(cfg.dst_root)],
                              capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2, proc.stderr
    assert "another mirror" in proc.stderr


class _Child:
    def __init__(self, code=None):
        self.code, self.pid, self.terminated = code, 4242, False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated, self.code = True, -15

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        self.code = -9


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_the_supervisor_restarts_a_stuck_mirror_and_waits_out_a_busy_one(tmp_path):
    children, clock = [], _Clock()

    def popen(argv, **kw):
        assert argv[3:4] == ["run-mirror"] and "--exit-on-stuck" in argv and "--loop" in argv
        assert kw["start_new_session"] and "LOOPLAB_UI_REAP_ON_EXIT" not in kw["env"]
        children.append(_Child())
        return children[-1]

    sup = MirrorSupervisor(tmp_path / "src", tmp_path / "dst", popen=popen, clock=clock,
                           busy_retry_s=300)
    sup.tick()
    assert len(children) == 1 and sup.status()["state"] == "running"
    children[0].code = 3                              # a hung destination call: exit-on-stuck
    sup.tick()
    assert sup.status()["state"] == "stuck" and sup.status()["last_exit_code"] == 3
    sup.tick()
    assert len(children) == 1                         # backoff first
    clock.t += 6
    sup.tick()
    assert len(children) == 2 and sup.status()["restarts"] == 1
    children[1].code = 2                              # another mirror holds the lock
    sup.tick()
    assert sup.status()["state"] == "external"
    clock.t += 299
    sup.tick()
    assert len(children) == 2                         # never a second copier beside it
    clock.t += 2
    sup.tick()
    assert len(children) == 3
    sup.stop()
    assert children[2].terminated and sup.status()["state"] == "stopped"


def test_the_banner_read_says_where_the_runs_live_and_what_the_mirror_did(tmp_path, monkeypatch):
    import looplab.core.volatile_fs as vf
    monkeypatch.setattr(vf, "is_volatile", lambda path, **kw: True)
    monkeypatch.delenv("LOOPLAB_RUN_MIRROR_DST", raising=False)
    client = TestClient(make_app(tmp_path / "runs"))
    bare = client.get("/api/run-mirror").json()
    assert bare["volatile"] is True and bare["dst_root"] is None and bare["supervisor"] is None
    assert bare["env"] == "LOOPLAB_RUN_MIRROR_DST"
    cfg = _root(tmp_path)
    mirror_pass(cfg)
    monkeypatch.setenv("LOOPLAB_RUN_MIRROR_DST", str(cfg.dst_root))
    app = make_app(cfg.src_root)
    sup = app.state.looplab.run_mirror
    assert isinstance(sup, MirrorSupervisor)          # built; started only by the lifespan
    view = TestClient(app).get("/api/run-mirror").json()
    assert view["dst_root"] == str(cfg.dst_root) and view["last_pass_finished_at"]
    assert view["supervisor"]["state"] == "starting" and view["stuck"] is None


def test_mirror_diagnostic_does_not_confuse_running_with_healthy():
    from looplab.serve.mirror_supervisor import mirror_diagnostic
    view = {"dst_root": "/mirror", "status_readable": True, "last_pass_finished_at": 990,
            "stuck": {"reason": "write timed out"}, "errors": 0,
            "supervisor": {"state": "running", "restarts": 92}}
    observed = mirror_diagnostic(view, now=1000)
    assert observed["state"] == "degraded"
    assert observed["last_pass_age_seconds"] == 10
    assert observed["supervisor_restarts"] == 92
    view["stuck"] = None
    assert mirror_diagnostic(view, now=1200)["state"] == "stale"
    view["status_readable"] = False
    assert mirror_diagnostic(view, now=1000)["state"] == "unknown"


def test_failed_pass_does_not_advance_success_or_disguise_degradation(tmp_path, monkeypatch):
    import looplab.events.run_mirror as mirror
    from looplab.serve.mirror_supervisor import mirror_diagnostic, mirror_view
    cfg = _root(tmp_path)
    mirror_pass(cfg)
    good = mirror.mirror_status(cfg)["last_successful_pass_finished_at"]
    def stuck(*args, **kwargs):
        raise mirror.MirrorStuck("simulated FUSE timeout")
    monkeypatch.setattr(mirror, "mirror_run", stuck)
    result = mirror_pass(cfg)
    status = mirror.mirror_status(cfg)
    assert result.stuck and status["last_pass"]["completed"] is False
    assert status["last_successful_pass_finished_at"] == good
    assert status["last_pass_finished_at"] >= good
    monkeypatch.setenv("LOOPLAB_RUN_MIRROR_DST", str(cfg.dst_root))
    sup = MirrorSupervisor(cfg.src_root, cfg.dst_root)
    sup.state = "running"
    view = mirror_view(cfg.src_root, sup)
    assert view["diagnostic"]["state"] == "degraded"
    assert view["last_successful_pass_finished_at"] == good
    assert mirror_diagnostic({"dst_root": "x", "status_readable": True,
                              "last_pass_finished_at": 990}, now=1000)["state"] == "unknown"


def test_stuck_retry_rate_is_bounded_and_recovers_automatically(tmp_path):
    children, clock = [], _Clock()
    def popen(*args, **kwargs):
        children.append(_Child())
        return children[-1]
    sup = MirrorSupervisor(tmp_path / "src", tmp_path / "dst", popen=popen, clock=clock)
    for delay in (6, 11):
        sup.tick()
        children[-1].code = 3
        sup.tick()
        clock.t += delay
    sup.tick()
    children[-1].code = 3
    sup.tick()
    assert len(children) == 3
    assert sup.status()["state"] == "stuck_cooldown"
    clock.t = 1899
    sup.tick()
    assert len(children) == 3
    clock.t = 1900
    sup.tick()
    assert len(children) == 4 and sup.status()["state"] == "running"


def test_uninterruptible_child_shutdown_is_bounded_and_reported(tmp_path):
    class CannotExit(_Child):
        def terminate(self):
            self.terminated = True
        def kill(self):
            pass
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("fake mirror", timeout)
    sup = MirrorSupervisor(tmp_path / "src", tmp_path / "dst")
    sup.child = CannotExit()
    sup.stop()
    assert sup.status()["state"] == "stop_unconfirmed"
    from looplab.serve.mirror_supervisor import mirror_diagnostic
    assert mirror_diagnostic({"dst_root": "x", "supervisor": sup.status()})["state"] == "degraded"


def test_freshness_uses_supervisor_interval_and_last_clean_pass():
    from looplab.serve.mirror_supervisor import mirror_diagnostic
    view = {"dst_root": "x", "status_readable": True, "last_pass_finished_at": 1000,
            "last_successful_pass_finished_at": 1000, "supervisor": {"state": "running", "interval_s": 300}}
    assert mirror_diagnostic(view, now=1400)["state"] == "observed_recent"
    assert mirror_diagnostic(view, now=1721)["state"] == "stale"
