"""JupyterHub / FUSE-mount compatibility regressions.

These guard the hardening that lets LoopLab launch in JupyterHub and survive an object-store FUSE
home (geesefs/s3fs): best-effort fsync (an unsupported-fs fsync must not abort a write), unique
atomic-write temps (two writers can't collide on a fixed `.tmp`), and the jupyter-server-proxy
launch spec.
"""
from __future__ import annotations

import os

import pytest

from looplab.core.atomicio import (
    atomic_write_bytes,
    atomic_write_text,
    best_effort_fsync,
    strict_fsync,
)


def test_best_effort_fsync_swallows_unsupported(monkeypatch):
    """On a FUSE/S3 mount fsync can raise OSError (ENOTSUP/EINVAL/EIO) — that MUST be swallowed, else
    the per-event append (eventstore) and every snapshot write would abort the engine mid-run."""
    def _raise(_fd):
        raise OSError("fsync not supported on this fs")
    monkeypatch.setattr(os, "fsync", _raise)
    best_effort_fsync(0)            # must NOT raise
    # And it must not break a real atomic write either (the write reaches the OS buffer regardless).
    import tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.json")
    atomic_write_text(p, '{"ok": true}')
    assert open(p, encoding="utf-8").read() == '{"ok": true}'


def test_strict_fsync_fails_closed_when_sync_is_unsupported(tmp_path, monkeypatch):
    """A paid-work claim must never degrade to best effort before the provider starts."""
    target = tmp_path / "claim"
    with target.open("wb") as handle:
        handle.write(b"claim")
        handle.flush()
        monkeypatch.setattr(
            os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("unsupported")))
        with pytest.raises(OSError, match="durable fsync failed"):
            strict_fsync(handle.fileno())


def test_durable_append_line_written_then_fsync_error_does_not_reuse_seq(tmp_path, monkeypatch):
    """A failed durability receipt stops paid work, but cannot make a written seq reusable."""
    import looplab.events.eventstore as eventstore_module
    from looplab.events.eventstore import EventStore, iter_jsonl

    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("run_started", {"run_id": "r"})
    assert [event.seq for event in store.read_all()] == [0]  # warm a non-empty cache
    calls = 0

    def fail_once(_fileno):
        nonlocal calls
        calls += 1
        # append() writes and flushes the complete line before asking for the durability receipt.
        assert path.read_bytes().endswith(b"\n")
        if calls == 1:
            raise OSError("sync receipt unavailable")

    monkeypatch.setattr(eventstore_module, "strict_fsync", fail_once)
    with pytest.raises(OSError, match="sync receipt unavailable"):
        store.append("paid_work_claimed", {"attempt": 1}, require_durable=True)

    # The COMPLETE line is on disk here, so `_disk_last_seq` already blocks reuse and the in-memory
    # reservation is not the mechanism (it is now taken only for a TORN partial, which `_disk_last_seq`
    # cannot see — reserving unconditionally opened a durable GAP whenever the buffered write never
    # reached the file at all). What must hold is the CONTRACT below: the written seq is never reused.
    assert store._cache == []
    assert store._cache_bytes == 0
    assert [event.seq for event in store.read_all()] == [0, 1]

    retry = store.append("paid_work_claimed", {"attempt": 2}, require_durable=True)
    assert retry.seq == 2
    assert [row["seq"] for row in iter_jsonl(path)] == [0, 1, 2]


def test_fsync_timeout_env_parse_tolerates_garbage(monkeypatch):
    """LOOPLAB_FSYNC_TIMEOUT is read at import; atomicio is imported transitively everywhere, so a
    garbage override (LOOPLAB_FSYNC_TIMEOUT=abc) must degrade to the default, not crash the app at
    load. A valid override is still honored. (`_fsync_timeout` reads the env live, so no reload.)"""
    import looplab.core.atomicio as aio
    assert aio._fsync_timeout() == 5.0              # default when unset
    monkeypatch.setenv("LOOPLAB_FSYNC_TIMEOUT", "abc")
    assert aio._fsync_timeout() == 5.0              # garbage -> default, no ValueError at import
    monkeypatch.setenv("LOOPLAB_FSYNC_TIMEOUT", "12.5")
    assert aio._fsync_timeout() == 12.5             # valid -> honored
    for invalid in ("0", "-1", "nan", "inf"):
        monkeypatch.setenv("LOOPLAB_FSYNC_TIMEOUT", invalid)
        assert aio._fsync_timeout() == 5.0


def test_atomic_write_uses_unique_temp_and_leaves_no_leftover(tmp_path):
    """atomic_write_bytes must use a UNIQUE temp (mkstemp), not a fixed `<name>.tmp` two concurrent
    writers would collide on, and must leave no stray temp behind after a successful write."""
    p = tmp_path / "data.json"
    atomic_write_bytes(p, b"first")
    atomic_write_bytes(p, b"second")
    assert p.read_bytes() == b"second"
    # No fixed-name temp and no leftover dot-temp files in the dir.
    assert not (tmp_path / "data.json.tmp").exists()
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == [], f"stray temp files left behind: {leftovers}"


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    """If os.replace fails (a FUSE rename hiccup), the temp must be cleaned up, not orphaned."""
    p = tmp_path / "data.json"
    def _boom(*a, **k):
        raise OSError("rename failed")
    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_write_bytes(p, b"x")
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == [], f"temp not cleaned on failure: {leftovers}"


def test_jupyter_serverproxy_spec_is_valid(monkeypatch):
    """The jupyter-server-proxy entry point must return a launch spec jsp can use: a {port}-templated
    command that runs `looplab ui --no-build` with a pinned run-root, prefix-stripping (absolute_url
    False), and a Launcher tile."""
    monkeypatch.delenv("LOOPLAB_UI_TOKEN", raising=False)
    from looplab.serve.jupyter import setup_looplab
    spec = setup_looplab()
    assert spec["command"][:2] == ["looplab", "ui"]
    assert "{port}" in spec["command"]
    assert "--no-build" in spec["command"]            # never build on a noexec/FUSE home
    assert "--run-root" in spec["command"]
    assert spec["absolute_url"] is False              # jsp strips the prefix; backend sees /api/...
    assert spec["new_browser_tab"] is False           # anonymous local shell may be framed
    assert spec["launcher_entry"]["title"] == "LoopLab"


def test_jupyter_protected_shell_opens_outside_frame(monkeypatch):
    """The protected shell denies framing, so its Launcher entry must not target an iframe."""
    monkeypatch.setenv("LOOPLAB_UI_TOKEN", "owner-secret")
    from looplab.serve.jupyter import setup_looplab

    assert setup_looplab()["new_browser_tab"] is True


def test_compose_protected_ui_wires_host_allowlist_and_public_healthcheck():
    """The documented protected Compose mode must stay reachable and healthy with auth enabled."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    ui_service = compose.split("  ui:\n", 1)[1].split("\n  run:\n", 1)[0]
    env_example = (root / ".env.example").read_text(encoding="utf-8")

    assert "LOOPLAB_UI_HOSTS: \"${LOOPLAB_UI_HOSTS:-}\"" in ui_service
    assert "localhost:8765/api/health" in ui_service
    assert "localhost:8765/api/runs" not in ui_service
    assert "LOOPLAB_UI_HOSTS=" in env_example


def test_run_root_honors_env(monkeypatch):
    monkeypatch.setenv("LOOPLAB_RUN_ROOT", "/data/looplab")
    from looplab.serve.jupyter import _run_root
    assert _run_root() == "/data/looplab"


def test_the_pod_oom_kill_reaches_a_diagnostician_that_can_still_see_the_kill():
    """A pod cgroup-memory OOM-kill (SIGKILL -> exit -9/137, no Python traceback) must still end in
    the memory-reduction directive rather than a generic abandon. WHO DECIDES THAT CHANGED on
    2026-08-20 and this test drives the new answer end to end, because the property is specific to
    this deployment: `jupyterhub-ml-azemlyak-test` is the host in the v3 corpus stderr, and a
    memory-capped pod SIGKILLing a too-big eval is a recurring failure here, not a corner case.

    THE ENGINE NO LONGER CLAIMS IT, and that is right on the ownership question: the KERNEL issued
    that kill, the engine did not, and nothing of ours observed it — so "was this a memory kill?" is
    a judgement, not a fact. The rule that used to answer it (`exit_code in (-9, 137)` AND
    `"Traceback" not in stderr`) was also a measured CONFLATION: both watchdog tree-kills produce
    byte-identical evidence, which on v6 node 5 bought three memory-reduction rounds for a run that
    was diverging.

    BUT THE ENGINE MUST STILL HAND OVER WHAT IT SAW. `_eval_failure_text` surfaces `exit=` only in
    its blank-stderr fallback, so a `137` with a "Killed" line was handing the judge that one word
    and nothing else — the diagnostician could not see the kill at all, and the capability would
    have been lost rather than moved. `engine_observed_facts` is the fix, and it is the `setup`
    lesson one rung along: a fact the engine holds must not be left to be re-inferred from the
    candidate's own text.

    AND THE INFERENCE IS NOW STRONGER THAN THE RULE IT REPLACES, which is the part worth keeping in
    front of a future reader. The one confounder that made `exit -9, no output` a bad rule — the
    watchdog kills — is excluded BY CONSTRUCTION before the diagnostician is asked, because a
    watchdog kill is ENGINE-FINAL and never put to a model. Same signal, minus its confounder, in
    front of something that can also read the log and the batch size in the code.
    """
    from types import SimpleNamespace
    from looplab.engine.failure_diagnosis import (REASON_SOURCE_ENGINE, REASON_SOURCE_TRIAGE,
                                                  diagnosed_failure_reason, engine_observed_facts)
    from looplab.engine.orchestrator import _failure_reason, _rule_triage

    def res(exit_code, stderr, timed_out=False, **kw):
        return SimpleNamespace(drift=None, timed_out=timed_out, stderr=stderr,
                               exit_code=exit_code, **kw)

    # 1. THE ENGINE'S HONEST RESIDUAL. All four are `crash` — "the process exited non-zero" — and
    #    the two kill shapes are no longer distinguished from an ordinary failure by their TEXT.
    for r in (res(-9, ""), res(137, "Killed"),
              res(-9, "Traceback (most recent call last):\n..."), res(1, "ValueError: x")):
        assert _failure_reason(r) == "crash"
    # ...and a deadline kill is STILL caught earlier and is still ENGINE-FINAL: the engine's own
    # clock fired, so no model is asked and none could move it.
    assert _failure_reason(res(-9, "", timed_out=True)) == "timeout"
    assert diagnosed_failure_reason("timeout", {"action": "repair", "failure_kind": "oom"}) == (
        "timeout", REASON_SOURCE_ENGINE)

    # 2. THE KILL IS STILL VISIBLE, because the engine states what it observed instead of leaving it
    #    to be read out of the candidate's own bytes. Both shapes, including the one whose stderr is
    #    non-empty and therefore never reached `_eval_failure_text`'s `exit=` fallback.
    for r, expect in ((res(-9, ""), "SIGKILL"), (res(137, "Killed"), "SIGKILL")):
        facts = engine_observed_facts(r)
        assert expect in facts and "exit code" in facts
        assert "No watchdog of ours claimed this run" in facts, (
            "the excluded confounder is what makes the remaining inference sound; say it")
    # It states the FACT and never the conclusion — a hint phrased as a verdict is the deleted rule
    # wearing a prompt.
    assert "oom" not in engine_observed_facts(res(-9, "")).lower()
    assert "memory" not in engine_observed_facts(res(-9, "")).lower()

    # 3. AND THE DIAGNOSIS STILL REACHES THE MEMORY DIRECTIVE.
    verdict = {"action": "repair", "failure_kind": "oom",
               "rationale": "SIGKILL with no output on a run no watchdog claimed: cgroup OOM"}
    assert diagnosed_failure_reason(_failure_reason(res(-9, "")), verdict) == (
        "oom", REASON_SOURCE_TRIAGE)
    assert _rule_triage("oom", "", attempt=1, max_attempts=1)["action"] == "repair"

    # 4. WITH NO DIAGNOSTICIAN WIRED nothing regresses into an abandon: the rule path repairs a
    #    `crash` blind, which is what a memory-capped pod's kill now takes.
    assert _rule_triage("crash", "", attempt=1, max_attempts=12)["action"] == "repair"


def test_deps_install_stops_after_repeated_egress_timeouts(monkeypatch):
    """On a no-egress pod pip times out on EVERY missing lib; after a few CONSECUTIVE timeouts install()
    must SHORT-CIRCUIT rather than hang the full timeout × N. A single transient timeout must NOT
    disable self-prep, and any pip RESPONSE resets the latch."""
    import subprocess
    import looplab.runtime.deps as deps
    monkeypatch.setattr(deps, "_consecutive_install_timeouts", 0)  # isolate from other tests

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pip", timeout=1)
    monkeypatch.setattr(subprocess, "run", _timeout)

    # The first LATCH attempts actually run and time out (no short-circuit yet) — a single timeout
    # must not have latched.
    for _ in range(deps._EGRESS_TIMEOUT_LATCH):
        r = deps.install("torch", timeout=1)
        assert r.ok is False and r.timed_out is True
    # Latch tripped: a further install short-circuits WITHOUT calling subprocess again.
    r = deps.install("xgboost", timeout=1)
    assert r.ok is False and "skipped" in r.output

    # A pip RESPONSE (here a clean "no matching distribution" — egress works) resets the counter, so a
    # transient blip can't disable self-prep for the rest of the run.
    monkeypatch.setattr(deps, "_consecutive_install_timeouts", 1)

    class _P:
        returncode = 1
        stdout = "ERROR: No matching distribution found"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    deps.install("nope", timeout=1)
    assert deps._consecutive_install_timeouts == 0


@pytest.mark.skipif(not hasattr(os, "sched_getaffinity"), reason="sched_getaffinity is POSIX/Linux only")
def test_sandbox_caps_blas_threads_to_cpu_quota(tmp_path):
    """On Linux the sandbox must bound BLAS/OpenMP thread pools to the CPU quota so one eval can't
    oversubscribe a cgroup-limited pod. We assert the env reaches the child by having it echo the var."""
    from looplab.runtime.sandbox import _run_argv
    import sys
    code = "import os; print(os.environ.get('OMP_NUM_THREADS', 'UNSET'))"
    exit_code, out, err, timed_out = _run_argv([sys.executable, "-c", code], tmp_path, timeout=30)
    assert exit_code == 0, err
    assert out.strip() == str(len(os.sched_getaffinity(0)))


def test_kill_process_tree_is_pid_recycle_safe_on_bogus_pid():
    """_kill_process_tree must never raise — and must refuse to signal a pid that isn't a looplab
    engine (PID-recycle guard). A almost-certainly-dead/foreign pid is a safe smoke test."""
    from looplab.serve.server import _kill_process_tree
    _kill_process_tree(999999)   # nonexistent pid -> no-op, no raise


def test_oom_repair_directive_says_reduce_memory(tmp_path):
    """The OOM repair must hand the LLM a MEMORY-reduction directive (the whole point of the 'oom'
    reason). Before the fix it fell to the generic 'diagnose the root cause' text — useless when the
    OOM-kill left no traceback — so repairs re-OOM'd. Distinct from the timeout (compute) directive."""
    from pathlib import Path
    from looplab.engine.orchestrator import Engine
    from looplab.search.policy import GreedyTree
    from looplab.runtime.sandbox import SubprocessSandbox
    from looplab.adapters.toytask import ToyTask
    task = ToyTask.load(Path(__file__).resolve().parents[1] / "examples" / "toy_task.json")
    r, d = task.build_roles()
    eng = Engine(tmp_path / "demo", task=task, researcher=r, developer=d,
                 sandbox=SubprocessSandbox(), policy=GreedyTree(n_seeds=2, max_nodes=4))
    oom = eng._repair_error_context("oom", "")
    assert "[failure kind: oom]" in oom
    assert "memory" in oom.lower() and "batch" in oom.lower()      # actionable memory-reduction
    timeout = eng._repair_error_context("timeout", "")
    assert "memory" not in timeout.lower()                          # the two directives stay distinct


def test_an_append_whose_bytes_never_landed_does_not_open_a_durable_seq_gap(tmp_path, monkeypatch):
    """`f.write` is BUFFERED, so a flush/fsync that raises (ENOSPC, EIO) can mean NOTHING reached the
    file. Reserving the seq anyway made this store's NEXT append write seq+1 onto a tail still at
    seq-1 — and the dense fence (`event_sequence_continues`) classifies that gap as CORRUPTION: the
    next append returns SUCCESS while its event is invisible to the fold, and every append after it
    raises EventLogCorruptionError. A self-inflicted brick, and `append_many` reserves the whole
    batch's last seq, up to 4096 at a time."""
    import looplab.events.eventstore as eventstore_module
    from looplab.events.eventstore import EventStore, iter_jsonl
    from looplab.events.replay import fold

    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    store.append("run_started", {"run_id": "r", "task_id": "t", "direction": "min"})


    class _NoSpace:
        """A handle whose buffered write is silently discarded and whose flush then fails."""
        def __init__(self, real):
            self._real = real

        def write(self, _data):
            return len(_data)                # accepted into a buffer that never reaches the file

        def flush(self):
            raise OSError(28, "No space left on device")

        def fileno(self):
            return self._real.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._real.close()
            return False

    def _open(p, mode="r", *a, **kw):
        handle = open(p, mode, *a, **kw)
        return _NoSpace(handle) if "a" in mode and str(p) == str(path) else handle

    # A module-level `open` shadows the builtin for code in that module (local -> global -> builtins).
    monkeypatch.setattr(eventstore_module, "open", _open, raising=False)
    with pytest.raises(OSError):
        store.append("pause", {})
    monkeypatch.undo()

    # Nothing landed, so the next append must take the seq that was never written — not skip past it.
    nxt = store.append("resume", {})
    assert nxt.seq == 1, f"a durable gap was opened: next seq is {nxt.seq}, disk holds [0]"
    assert [row["seq"] for row in iter_jsonl(path)] == [0, 1]
    assert len(fold(store.read_all()).nodes) == 0        # …and the log still folds at all


def test_a_durable_append_syncs_the_parent_directory_too(tmp_path, monkeypatch):
    """`require_durable` is the "this claim survives a crash before the external side effect" flag,
    but syncing only the FILE CONTENTS left the directory ENTRY unsynced. When the append is the one
    that created events.jsonl — or an earlier best-effort append created the link and it has not been
    flushed — a power loss can lose the WHOLE FILE despite the strict receipt, so a durable paid claim
    vanishes and is re-billed. `strict_atomic_write_bytes` has always paired the two."""
    import looplab.events.eventstore as eventstore_module
    from looplab.events.eventstore import EventStore

    parents = []
    monkeypatch.setattr(eventstore_module, "strict_fsync_parent", lambda p: parents.append(str(p)))

    # A durable append that CREATES the log publishes its directory entry.
    fresh = tmp_path / "fresh" / "events.jsonl"
    fresh.parent.mkdir()
    created = EventStore(fresh)
    created.append("run_started", {"run_id": "r"}, require_durable=True)
    assert parents == [str(fresh)], parents

    # ...once. `strict_fsync` is process-wide SINGLE-FLIGHT and spawns a worker thread per call, so
    # repeating it on every durable append would double traffic through that serialized resource on
    # the hot claim path without adding durability — the entry is already published.
    created.append("paid_work_claimed", {"attempt": 1}, require_durable=True)
    created.append_many([("pause", {}), ("resume", {})], require_durable=True)
    assert parents == [str(fresh)], parents

    # A log that already exists needs no publication: some earlier append made the entry durable.
    existing = tmp_path / "old" / "events.jsonl"
    existing.parent.mkdir()
    existing.write_text('{"seq":0,"type":"run_started","data":{}}\n', encoding="utf-8")
    parents.clear()
    EventStore(existing).append("paid_work_claimed", {"attempt": 1}, require_durable=True)
    assert parents == []

    # ...and a BEST-EFFORT append never publishes, whether or not it creates the file.
    besteffort = tmp_path / "cheap" / "events.jsonl"
    besteffort.parent.mkdir()
    EventStore(besteffort).append("run_started", {"run_id": "r"})
    assert parents == []
