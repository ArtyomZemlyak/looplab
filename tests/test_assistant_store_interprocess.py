"""Two SERVERS over one run root: the assistant stores serialize across processes, not only threads.

Review 2026-09-22, SRV1-03. `WatchStore` and `SessionStore` ordered every read-modify-write with a
`threading.Lock` — one process's threads and nothing else — while a second server over the same
run root is a first-class path (`looplab tui` starts one through `ensure_server`; a hub restart
overlaps the old process). Reproduced by the review: two stores each `claim` one due watch and both
win (two paid wake-up turns for one wake-up), and a starting server's `reconcile_on_start` re-arms
a watch the other, LIVE server is mid-wake on.

Every test here is a real second PROCESS, because a second thread is exactly what the old lock
already handled: the child pauses INSIDE the store's own read-modify-write (a patched read that
waits for a file the parent writes), and the parent drives the same operation meanwhile. The
properties are orderings, never durations — each wait below is a bound, not the thing measured.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from looplab.serve.assistant import SessionStore
from looplab.serve.assistant_watch import WatchStore

_REPO = Path(__file__).resolve().parents[1]
_BOUND_S = 60.0


def _spawn(script: str, *args) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", script, *map(str, args)], cwd=_REPO,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for(path: Path, child: subprocess.Popen) -> None:
    deadline = time.monotonic() + _BOUND_S
    while not path.exists():
        if child.poll() is not None:
            raise AssertionError(f"child exited early: {child.communicate()}")
        assert time.monotonic() < deadline, "the child never reached its paused section"
        time.sleep(0.01)


def _finish(child: subprocess.Popen) -> dict:
    out, err = child.communicate(timeout=_BOUND_S)
    assert child.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def _race_in_thread(fn, *, hold_s: float = 1.0):
    """Run `fn` on a thread and give it `hold_s` to finish. Returns (thread, box): with no
    cross-process fence it returns at once; with one it is still waiting on the child's section."""
    box: dict = {}
    thread = threading.Thread(target=lambda: box.setdefault("value", fn()), daemon=True)
    thread.start()
    thread.join(hold_s)
    return thread, box


# The child's half of every race: a store whose READ pauses (after reading) until `go` exists, so it
# sits inside the read-modify-write with the record it read in hand.
_PAUSED_READ = """
import json, os, sys, time
from pathlib import Path

def pause_after(cls, name, entered, go):
    real = getattr(cls, name)
    def paused(self, *args, **kwargs):
        value = real(self, *args, **kwargs)
        Path(entered).write_text("1")
        deadline = time.time() + 60
        while not os.path.exists(go) and time.time() < deadline:
            time.sleep(0.01)
        return value
    setattr(cls, name, paused)
"""


# --------------------------------------------------------------------------- WatchStore.claim

def test_two_servers_cannot_both_claim_one_due_watch(tmp_path):
    """THE DEFECT: each process read `armed` and each wrote `waking`, so one wake-up paid twice.
    MUTATION: `claim` back under `self._lock` alone -> both processes claim."""
    store = WatchStore(tmp_path)
    rec = store.arm(session="0123456789abcdef", instruction="tell me when it finishes",
                    trigger={"kind": "schedule", "every_s": 60}, mode="plan")
    store.update(rec["id"], next_due=0)
    entered, go = tmp_path / "entered", tmp_path / "go"
    child = _spawn(_PAUSED_READ + """
from looplab.serve.assistant_watch import WatchStore
root, wid, entered, go = sys.argv[1:5]
pause_after(WatchStore, "_read", entered, go)
print(json.dumps({"claimed": WatchStore(root).claim(wid) is not None}))
""", tmp_path, rec["id"], entered, go)
    try:
        _wait_for(entered, child)          # the child holds `armed` in hand, inside its claim
        thread, box = _race_in_thread(lambda: WatchStore(tmp_path).claim(rec["id"]))
    finally:
        go.write_text("1")
    thread.join(_BOUND_S)
    child_claimed = _finish(child)["claimed"]
    parent_claimed = box.get("value") is not None

    assert [child_claimed, parent_claimed].count(True) == 1, (child_claimed, parent_claimed)
    assert WatchStore(tmp_path).get(rec["id"])["status"] == "waking"


# --------------------------------------------------------------------------- reconcile_on_start

_CLAIM_AND_HOLD = """
import json, os, sys, time
from pathlib import Path
from looplab.serve.assistant_watch import WatchStore
root, wid, claimed, go = sys.argv[1:5]
store = WatchStore(root)
record = store.claim(wid)
Path(claimed).write_text(json.dumps({"claimed_by": (record or {}).get("claimed_by")}))
deadline = time.time() + 60
while not os.path.exists(go) and time.time() < deadline:   # "mid-wake": the turn is running
    time.sleep(0.01)
print(json.dumps({"ok": record is not None}))
"""


@pytest.mark.parametrize("mode, settled", [("plan", "armed"), ("acceptEdits", "interrupted")])
def test_a_starting_server_leaves_a_live_servers_wake_up_alone(tmp_path, mode, settled):
    """A `waking` record is a dead process's leftover ONLY if its claimer is dead. While the child
    that claimed it lives, a starting sibling must not re-arm it (the same wake-up again, paid
    twice) nor mark it `interrupted` (the owner's own settle then lands on a terminal and its
    result is lost). Once the child exits, the kernel drops its lease and the same reconcile
    settles the record exactly as a restart always did.

    MUTATION: drop the `_claim_owner_alive` check -> the first reconcile settles the live claim."""
    store = WatchStore(tmp_path)
    rec = store.arm(session="0123456789abcdef", instruction="look", mode=mode,
                    trigger={"kind": "schedule", "every_s": 60})
    store.update(rec["id"], next_due=0)
    claimed, go = tmp_path / "claimed", tmp_path / "go"
    child = _spawn(_CLAIM_AND_HOLD, tmp_path, rec["id"], claimed, go)
    try:
        _wait_for(claimed, child)
        assert json.loads(claimed.read_text())["claimed_by"], "the claim names no owner lease"

        changed = WatchStore(tmp_path).reconcile_on_start()      # a sibling server starting NOW
        assert changed == []
        assert WatchStore(tmp_path).get(rec["id"])["status"] == "waking"
    finally:
        go.write_text("1")
    assert _finish(child)["ok"] is True

    changed = WatchStore(tmp_path).reconcile_on_start()          # the claimer is gone now
    assert [c["id"] for c in changed] == [rec["id"]]
    assert WatchStore(tmp_path).get(rec["id"])["status"] == settled
    # ...and the dead owner's single-use lease file went with it.
    assert not list((tmp_path / "assistant" / ".watches").glob(".owner-*.lock"))


def test_a_claim_naming_no_lease_settles_as_it_always_did(tmp_path):
    """Records written before the lease existed (or on a mount without advisory locks) carry no
    `claimed_by`: nothing proves their claimer alive, so a restart settles them unchanged."""
    store = WatchStore(tmp_path)
    rec = store.arm(session="0123456789abcdef", instruction="look", mode="plan",
                    trigger={"kind": "schedule", "every_s": 60})
    store.update(rec["id"], status="waking")                     # a legacy claim: no owner named
    assert "claimed_by" not in store.get(rec["id"])
    assert [c["id"] for c in WatchStore(tmp_path).reconcile_on_start()] == [rec["id"]]
    assert store.get(rec["id"])["status"] == "armed"


def test_a_dead_servers_lease_file_is_retired_at_the_next_start(tmp_path):
    """A server that claimed once leaves its (empty, unlocked) lease file behind when it exits;
    the next start sweeps it, and never a lease a live store still holds."""
    live = WatchStore(tmp_path)
    rec = live.arm(session="0123456789abcdef", instruction="x",
                   trigger={"kind": "schedule", "every_s": 60})
    live.update(rec["id"], next_due=0)
    assert live.claim(rec["id"]) is not None
    child = subprocess.run([sys.executable, "-c", """
import sys
from looplab.serve.assistant_watch import WatchStore
store = WatchStore(sys.argv[1])
rec = store.arm(session="fedcba9876543210", instruction="y",
                trigger={"kind": "schedule", "every_s": 60})
store.update(rec["id"], next_due=0)
assert store.claim(rec["id"]) is not None
store.update(rec["id"], status="armed")
""", str(tmp_path)], cwd=_REPO, capture_output=True, text=True, timeout=_BOUND_S)
    assert child.returncode == 0, child.stderr
    leases = sorted((tmp_path / "assistant" / ".watches").glob(".owner-*.lock"))
    assert len(leases) == 2

    WatchStore(tmp_path).reconcile_on_start()

    assert sorted((tmp_path / "assistant" / ".watches").glob(".owner-*.lock")) == [
        live._lease_path(live._owner)]


# --------------------------------------------------------------------------- SessionStore

def test_two_servers_cannot_both_pass_one_length_check(tmp_path):
    """`append_if_len` exists so a stale reply cannot interleave into a newer turn. Across two
    processes the check and the append were not atomic: both saw length 1, both appended.
    MUTATION: `append_if_len` back under `self._append_lock` alone -> two replies at index 1."""
    sessions = SessionStore(tmp_path)
    sid = sessions.create("chat")["id"]
    sessions.append(sid, {"role": "user", "content": "u1"})
    entered, go = tmp_path / "entered", tmp_path / "go"
    child = _spawn(_PAUSED_READ + """
import looplab.serve.assistant as assistant
root, sid, entered, go = sys.argv[1:5]
real_iter = assistant.iter_jsonl
def paused_iter(path):
    rows = list(real_iter(path))
    Path(entered).write_text("1")
    deadline = time.time() + 60
    while not os.path.exists(go) and time.time() < deadline:
        time.sleep(0.01)
    return iter(rows)
assistant.iter_jsonl = paused_iter
ok = assistant.SessionStore(root).append_if_len(sid, {"role": "assistant", "content": "from B"}, 1)
print(json.dumps({"appended": ok}))
""", tmp_path, sid, entered, go)
    try:
        _wait_for(entered, child)          # the child has counted 1 and not yet appended
        thread, box = _race_in_thread(lambda: SessionStore(tmp_path).append_if_len(
            sid, {"role": "assistant", "content": "from A"}, 1))
    finally:
        go.write_text("1")
    thread.join(_BOUND_S)
    child_appended = _finish(child)["appended"]

    assert [child_appended, box.get("value")].count(True) == 1, (child_appended, box)
    assert len(SessionStore(tmp_path).messages(sid)) == 2


def test_two_servers_updating_one_chats_meta_keep_both_fields(tmp_path):
    """A Share click on one server and a mode switch on the other each read the same meta and
    wrote it back — the second write dropped the first's field.
    MUTATION: `update_meta` back under `self._meta_lock` alone -> one field is lost."""
    sessions = SessionStore(tmp_path)
    sid = sessions.create("chat")["id"]
    entered, go = tmp_path / "entered", tmp_path / "go"
    child = _spawn(_PAUSED_READ + """
from looplab.serve.assistant import SessionStore
root, sid, entered, go = sys.argv[1:5]
pause_after(SessionStore, "_read_meta", entered, go)
print(json.dumps({"meta": SessionStore(root).update_meta(sid, title="renamed by B")}))
""", tmp_path, sid, entered, go)
    try:
        _wait_for(entered, child)
        thread, _box = _race_in_thread(
            lambda: SessionStore(tmp_path).update_meta(sid, mode="acceptEdits"))
    finally:
        go.write_text("1")
    thread.join(_BOUND_S)
    _finish(child)

    meta = SessionStore(tmp_path).validated_meta(sid)
    assert meta["title"] == "renamed by B" and meta["mode"] == "acceptEdits", meta


def test_the_fence_is_not_re_entrant_and_says_so(tmp_path):
    """A nested acquisition on one thread would wait on its OWN descriptor until the timeout and
    then report cross-process contention that never happened — refused by name instead."""
    import threading as _threading

    from looplab.serve.capability_store import polled_store_lock

    lock = _threading.RLock()
    with polled_store_lock(lock, tmp_path / "x.lock"):
        with pytest.raises(RuntimeError, match="re-entered"):
            with polled_store_lock(lock, tmp_path / "x.lock"):
                pass


def test_a_session_removed_under_the_fence_reads_as_gone_not_as_a_crash(tmp_path):
    """The lock file lives in the session's own directory, so a deleted session cannot take a
    lock — and must answer exactly what it answered before the fence existed."""
    import shutil

    sessions = SessionStore(tmp_path)
    sid = sessions.create("chat")["id"]
    shutil.rmtree(tmp_path / "assistant" / sid)
    assert sessions.update_meta(sid, title="x") is None
    assert sessions.append_if_len(sid, {"role": "user", "content": "u"}, 0) is False
    assert not (tmp_path / "assistant" / sid).exists()
    assert sessions.update_meta("not-a-session-id", title="x") is None
    assert sorted(p.name for p in (tmp_path / "assistant").iterdir()) == [], (
        "a refused update left a lock file behind")
