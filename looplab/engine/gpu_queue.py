"""CROSS-RUN GPU ORDER: a FIFO queue in front of the host GPU-pool lease, and the per-run GPU gate.

WHY (audit 2026-10-14, L-02). The host lease (`engine/resources.py`, one flock per OS user) made
co-hosted runs mutually exclusive but said nothing about ORDER: a waiting run re-polled the flock
every 0.5 s, so whichever process happened to poll first after a release won, and a lightly loaded
holder could take the pool straight back between two of its own evaluations. An external agent that
wanted "run B after run A" had to build that itself: 12 copies of `handoff-after-*.py` (pause A,
watch for its lease release, write an ack file, resume B), and one of them raced — the lease was
freed ~2 s before the ack file existed, B's guard ran first and the stage failed (incident C2).

WHAT, two independent mechanisms, both owned by LoopLab:

  * THE QUEUE. Before an engine may try the host lease it holds a TICKET: a small JSON file in
    `<lease>.queue/`, named by its creation time, and EXCLUSIVELY FLOCKED by the waiting process
    for as long as it waits. Only the HEAD of the queue (the oldest live ticket) tries the lease;
    on success it drops its ticket. A holder that releases and immediately needs the pool again
    takes a NEW ticket — behind everyone already waiting — so the hand-off goes to the run that
    waited longest, with no window in which a third process can overtake it. Liveness of a ticket
    is its flock, never a pid probe: a ticket whose lock nobody holds belongs to a dead process
    and is removed by whoever meets it (a crash leaves no permanent queue jam). A ticket is
    created under a dot-prefixed temporary name, locked, then renamed into place, so no reader can
    observe a ticket before its owner holds its lock.
  * THE GATE (`<run>/gpu_gate.json`). A run may declare `hold: true` (take no GPU until released)
    and `start_after: [<run dir>, ...]` (take no GPU until every named run's engine has EXITED —
    paused, finished, aborted or crashed: `run_lifecycle.engine_liveness` is False). The gate is a
    mutable SIDECAR, not a Setting: it is written before `looplab run` (`looplab gpu-gate`) or later
    through `PUT /api/runs/{run_id}/gpu-gate`, and the engine re-reads it on every admission poll,
    so releasing a hold takes effect within one poll. It delays only GPU admission: CPU-only work
    (`footprint {"gpus": 0}`), setup and the external agent's own commands proceed.

NOT HERE: which devices a run gets (that stays the engine's process-local free list), and any
fairness BETWEEN the evaluations of ONE engine (they share one lease). On Windows the queue is off
(the lease there is an `msvcrt` byte lock and the ticket liveness rule is a flock rule); the gate
works everywhere. Every failure of the queue's bookkeeping degrades to the historical unordered
lease — order is a courtesy, the lease is the safety property.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import file_identity

_LOG = logging.getLogger(__name__)

QUEUE_SUFFIX = ".queue"
HOLDER_SUFFIX = ".holder.json"
GATE_FILE = "gpu_gate.json"
# A ticket body is tiny; anything larger is not ours and is reported, never echoed.
_TICKET_MAX_BYTES = 4096
# A gate file is operator/agent input: bounded and strictly shaped.
_GATE_MAX_BYTES = 16 * 1024
MAX_START_AFTER = 16
# Temporary ticket names older than this were abandoned mid-creation by a dead process.
_STALE_TMP_S = 3600.0
# The predecessor probe is a flock attempt per named run; asked at most this often per engine.
_GATE_PROBE_INTERVAL_S = 2.0
# A waiting engine re-touches its ticket on every admission poll (every 0.5 s). A ticket whose owner
# is ALIVE but stopped polling — its waiting evaluation was cancelled, the run paused — would block
# everyone behind it for as long as that process lives; past this age it no longer counts as ahead
# (it keeps its file, and its place, should its owner come back to poll).
HEARTBEAT_STALE_S = 15.0
_HEARTBEAT_EVERY_S = 1.0


def queue_supported() -> bool:
    return os.name != "nt"


def queue_dir(lease_path) -> Path:
    return Path(f"{lease_path}{QUEUE_SUFFIX}")


def holder_path(lease_path) -> Path:
    return Path(f"{lease_path}{HOLDER_SUFFIX}")


def _flock(fd: int, flags: int) -> bool:
    """True when the lock was taken; False on contention. Other errors raise."""
    import fcntl

    try:
        fcntl.flock(fd, flags)
        return True
    except OSError as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EACCES):
            return False
        raise


def _ticket_alive(path: Path) -> Optional[bool]:
    """Is the process that created this ticket still waiting? None when it vanished meanwhile."""
    import fcntl

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        return True      # unreadable: never treat a ticket we cannot judge as dead
    try:
        if _flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB):
            # Nobody holds it: its owner exited (the OS released the lock). Remove the name while
            # we hold the descriptor, then let go.
            try:
                if os.path.samestat(os.fstat(fd), os.lstat(path)):
                    os.unlink(path)
            except OSError:
                pass
            return False
        return True
    except OSError:
        return True
    finally:
        os.close(fd)


def _read_json(path: Path, cap: int) -> Optional[dict]:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(cap + 1)
    except OSError:
        return None
    if len(raw) > cap:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _sweep_tmp(directory: Path) -> None:
    now = time.time()
    try:
        for entry in os.scandir(directory):
            if entry.name.startswith(".tmp-"):
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime > _STALE_TMP_S:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def live_tickets(lease_path, *, prune: bool = True) -> list[dict]:
    """The queue in FIFO order: `[{name, pid, run_dir, run_id, created_at, alive}]`.

    `prune` removes dead tickets it meets (the waiting engines prune; a read-only status reader
    passes False and simply reports `alive: false`)."""
    directory = queue_dir(lease_path)
    try:
        names = sorted(entry.name for entry in os.scandir(directory)
                       if entry.name.endswith(".json") and not entry.name.startswith("."))
    except OSError:
        return []
    rows = []
    now = time.time()
    for name in names:
        path = directory / name
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if prune:
            alive = _ticket_alive(path)
            if alive is None or alive is False:
                continue
        else:
            alive = _ticket_alive_readonly(path)
            if alive is None:
                continue
        body = _read_json(path, _TICKET_MAX_BYTES) or {}
        rows.append({"name": name, "pid": body.get("pid"), "run_dir": body.get("run_dir"),
                     "run_id": body.get("run_id"), "created_at": body.get("created_at"),
                     "alive": bool(alive),
                     "waiting": bool(alive) and age <= HEARTBEAT_STALE_S,
                     "heartbeat_age_s": round(max(0.0, age), 1)})
    return rows


def _ticket_alive_readonly(path: Path) -> Optional[bool]:
    """`_ticket_alive` without the unlink: for a status reader that must not mutate the queue."""
    import fcntl

    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        return True
    try:
        if _flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB):
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        return True
    except OSError:
        return True
    finally:
        os.close(fd)


class GpuQueueTicket:
    """One engine process's place in the host GPU queue. Not thread-safe by itself: the engine
    calls it only under its `_gpu_condition`."""

    def __init__(self, lease_path, *, run_dir=None, run_id: str = ""):
        self.lease_path = Path(lease_path)
        self.run_dir = str(run_dir) if run_dir is not None else ""
        self.run_id = run_id or (Path(self.run_dir).name if self.run_dir else "")
        self._fd: Optional[int] = None
        self._name: Optional[str] = None
        self._beat = 0.0
        self.disabled = not queue_supported()

    @property
    def name(self) -> Optional[str]:
        return self._name

    def held(self) -> bool:
        return self._fd is not None

    def ensure(self) -> bool:
        """Hold a ticket (idempotent). False when the queue is unusable here (then the caller falls
        back to the unordered lease); the reason is logged once."""
        if self.disabled:
            return False
        if self._fd is not None:
            now = time.monotonic()
            if now - self._beat >= _HEARTBEAT_EVERY_S:
                self._beat = now
                try:
                    os.utime(self._fd)
                except (OSError, NotImplementedError, TypeError):
                    pass
            return True
        import fcntl

        directory = queue_dir(self.lease_path)
        tmp = None
        fd = None
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            _sweep_tmp(directory)
            name = f"{time.time_ns():020d}-{os.getpid()}-{os.urandom(3).hex()}.json"
            tmp = directory / f".tmp-{name}"
            fd = os.open(tmp, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
            if not _flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB):
                raise OSError("fresh ticket already locked")
            body = {"pid": os.getpid(), "run_dir": self.run_dir, "run_id": self.run_id,
                    "created_at": time.time()}
            os.write(fd, json.dumps(body).encode("utf-8"))
            os.rename(tmp, directory / name)
            self._fd, self._name = fd, name
            self._beat = time.monotonic()
            fd = None
            return True
        except OSError as exc:
            _LOG.warning("GPU queue unavailable at %s (%s); falling back to the unordered host "
                         "lease", directory, exc)
            self.disabled = True
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    def ahead(self) -> list[dict]:
        """Live tickets older than ours (pruning dead ones on the way)."""
        if self._name is None:
            return []
        return [row for row in live_tickets(self.lease_path)
                if row["name"] < self._name and row["waiting"]]

    def is_head(self) -> bool:
        if self.disabled or self._name is None:
            return True
        return not self.ahead()

    def release(self) -> None:
        if self._fd is None:
            return
        name, fd = self._name, self._fd
        self._fd = self._name = None
        try:
            os.unlink(queue_dir(self.lease_path) / name)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def write_holder(lease_path, *, run_dir=None, run_id: str = "") -> None:
    """Describe the lease holder for status readers. Diagnostic only — the flock is the truth."""
    body = {"pid": os.getpid(), "run_dir": str(run_dir or ""),
            "run_id": run_id or (Path(str(run_dir)).name if run_dir else ""),
            "acquired_at": time.time()}
    path = holder_path(lease_path)
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def clear_holder(lease_path) -> None:
    path = holder_path(lease_path)
    body = _read_json(path, _TICKET_MAX_BYTES)
    if body is not None and body.get("pid") != os.getpid():
        return      # someone else's description; never erase it
    try:
        path.unlink()
    except OSError:
        pass


def lease_holder_pids(lease_path) -> Optional[set[int]]:
    """Pids the kernel lock table says hold the lease flock (Linux), None when unknowable."""
    from looplab.engine.resources import _flock_holders

    try:
        text = Path("/proc/locks").read_text()
    except OSError:
        return None
    return _flock_holders(lease_path, text)


def queue_status(lease_path) -> dict:
    """Read-only view for the UI/harness: who holds the pool and who waits, in order."""
    lease_path = Path(lease_path)
    pids = lease_holder_pids(lease_path)
    described = _read_json(holder_path(lease_path), _TICKET_MAX_BYTES)
    holder = None
    if pids:
        pid = next(iter(sorted(pids)))
        holder = {"pid": pid, "run_dir": None, "run_id": None, "acquired_at": None}
        if described and described.get("pid") == pid:
            holder.update({k: described.get(k) for k in ("run_dir", "run_id", "acquired_at")})
    elif pids is None and described:
        holder = {**{k: described.get(k) for k in ("pid", "run_dir", "run_id", "acquired_at")},
                  "unverified": True}
    tickets = live_tickets(lease_path, prune=False) if queue_supported() else []
    waiting = [row for row in tickets if row["waiting"]]
    return {"lease_path": str(lease_path), "queue_supported": queue_supported(),
            "lease_held": bool(pids) if pids is not None else None,
            "holder": holder, "queue": waiting,
            "queue_length": len(waiting)}


# --------------------------------------------------------------------------------- the GPU gate
def gate_path(run_dir) -> Path:
    return Path(run_dir) / GATE_FILE


def read_gate(run_dir) -> Optional[dict]:
    """The run's gate as `{hold, start_after: [abs dir], reason, updated_at}`, or None.

    A malformed file is a HOLD with a reason, never "no gate": an operator who wrote a gate meant
    to stop GPU work, and a typo must not silently start it."""
    path = gate_path(run_dir)
    if not path.exists():
        return None
    data = _read_json(path, _GATE_MAX_BYTES)
    if data is None:
        return {"hold": True, "start_after": [], "reason": "gpu_gate.json is unreadable or malformed",
                "malformed": True}
    hold = data.get("hold", False)
    after = data.get("start_after", [])
    if (not isinstance(hold, bool) or not isinstance(after, list)
            or not all(isinstance(item, str) and item for item in after)
            or len(after) > MAX_START_AFTER):
        return {"hold": True, "start_after": [], "reason": "gpu_gate.json has an invalid shape",
                "malformed": True}
    reason = data.get("reason") if isinstance(data.get("reason"), str) else ""
    return {"hold": hold, "start_after": list(after), "reason": reason[:500],
            "updated_at": data.get("updated_at"), "updated_by": data.get("updated_by")}


def write_gate(run_dir, *, hold: bool, start_after: list[str], reason: str = "",
               updated_by: str = "") -> dict:
    """Atomically publish a gate; `start_after` entries are run DIRECTORIES (absolute)."""
    if len(start_after) > MAX_START_AFTER:
        raise ValueError(f"start_after names at most {MAX_START_AFTER} runs")
    body = {"version": 1, "hold": bool(hold),
            "start_after": [str(Path(item).resolve()) for item in start_after],
            "reason": str(reason or "")[:500], "updated_by": str(updated_by or "")[:40],
            "updated_at": time.time()}
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = gate_path(run_dir)
    tmp = Path(f"{path}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return read_gate(run_dir) or body


def clear_gate(run_dir) -> None:
    try:
        gate_path(run_dir).unlink()
    except FileNotFoundError:
        pass


def gate_blockers(gate: Optional[dict], *, liveness=None) -> list[dict]:
    """What the gate is still waiting for: `[{kind: hold|start_after, ...}]`; empty = open.

    `liveness(Path) -> True|False|None` is `run_lifecycle.engine_liveness`; an INCONCLUSIVE probe
    keeps waiting (a predecessor we cannot judge has not provably released the GPUs)."""
    if not gate:
        return []
    blockers = []
    if gate.get("hold"):
        blockers.append({"kind": "hold", "reason": gate.get("reason") or "held"})
    if gate.get("start_after"):
        if liveness is None:
            from looplab.engine.run_lifecycle import engine_liveness as liveness
        for item in gate["start_after"]:
            alive = liveness(Path(item))
            if alive is not False:
                blockers.append({"kind": "start_after", "run_dir": item,
                                 "run_id": Path(item).name,
                                 "engine_running": alive})
    return blockers


class GateReader:
    """The engine's cached reader of its own gate: re-reads the file when it changes and probes
    predecessors at most every `_GATE_PROBE_INTERVAL_S` seconds."""

    def __init__(self, run_dir, *, clock=time.monotonic, liveness=None):
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self._clock = clock
        self._liveness = liveness
        self._stamp = None
        self._gate = None
        self._checked_at = None
        self._blockers: list[dict] = []
        self._announced = None

    def blockers(self) -> list[dict]:
        if self.run_dir is None:
            return []
        path = gate_path(self.run_dir)
        try:
            st = path.stat()
            stamp = file_identity(st)
        except OSError:
            stamp = None
        if stamp != self._stamp:
            self._stamp = stamp
            self._gate = read_gate(self.run_dir) if stamp is not None else None
            self._checked_at = None
        if not self._gate:
            self._blockers = []
            return []
        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= _GATE_PROBE_INTERVAL_S:
            self._checked_at = now
            self._blockers = gate_blockers(self._gate, liveness=self._liveness)
            key = tuple((b["kind"], b.get("run_dir")) for b in self._blockers)
            if key != self._announced:
                self._announced = key
                if self._blockers:
                    _LOG.warning("GPU admission is gated for %s: %s", self.run_dir,
                                 "; ".join(_describe(b) for b in self._blockers))
                else:
                    _LOG.warning("GPU gate for %s is open", self.run_dir)
        return list(self._blockers)


def _describe(blocker: dict) -> str:
    if blocker["kind"] == "hold":
        return f"hold ({blocker.get('reason') or 'held'})"
    return f"waiting for run {blocker.get('run_id')} to stop its engine"
