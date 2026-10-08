"""Is the BOX still there? The engine's own out-of-band answer to "did this eval fail of the box?".

WHY THIS EXISTS (incident, 2026-10-06). A container restart wiped `/var/tmp` (the conda env, the
datasets, the HF models) and the operator's network data mount then answered every access with
`OSError: [Errno 107] Transport endpoint is not connected` for hours. Every eval that ran meanwhile
died non-zero, which `_failure_reason` classifies `crash`, and `crash` is DIAGNOSABLE and
REPAIRABLE: each node bought a paid triage call, then Developer repairs of a training script that
had nothing wrong with it, and a triage that answered `reject_idea` closed the idea's whole lineage
over a mount. Nothing in the failure path ever asked whether the box was healthy.

THE RULE `engine/failure_diagnosis.py` STATES, applied: text may NOMINATE, never DECIDE, and when an
OUT-OF-BAND CHANNEL EXISTS, use it. The candidate's stderr saying "Transport endpoint is not
connected" is text the candidate writes (and a candidate can print it on purpose to buy itself an
unrepaired, uncharged retry). The channel that does exist is the engine asking the filesystem
ITSELF, after the eval died, about the paths the OPERATOR declared: the `data:` / `references:`
mount sources, the editable source roots, the run directory the record is written into, and the
eval interpreter. The candidate cannot make the engine's own `stat` of a declared mount fail, so the
answer is the engine's, like `is_present`'s.

WHAT COUNTS, per target, and why the strictness differs:

| role | probe | a fault is |
|---|---|---|
| `mount` (a declared data/reference source) | `stat` + list one entry | ANY `OSError`; a MISSING source only once the box was seen working — a node evaluated, a stage ran `ok`, a run setup finished (`admissible_faults`: a `run_setup` may create it) |
| `editable` (a declared source root) | `stat` + list one entry | ANY `OSError` |
| `run_dir` | create, write one byte, unlink a probe file | ANY `OSError` (ENOSPC, EROFS, ENOTCONN…) |
| `interpreter` (the sandbox's, and the task's own `eval.python`) | `stat`, executable bit | not executable; missing only once the box was seen working |
| `env_path` (an absolute path in the DECLARED eval env) | `stat` | only an INFRA errno or a hang: a declared OUTPUT path that does not exist yet is not a fault |

A HANG IS A FAULT. A dead NFS/FUSE mount often blocks a `stat` in uninterruptible sleep instead of
raising, and a probe that waits on it would hang the engine exactly where it is meant to notice the
box. Each probe runs in a DAEMON thread joined with a timeout; a thread that never returns is
abandoned (it cannot be killed — nothing in user space can wake a D-state syscall) and REMEMBERED,
so the next probe of the same path answers `hung` at once instead of stacking a second stuck
thread behind it. The memo EXPIRES (review 2026-10-08): a hard NFS mount that was replaced under
the stuck thread leaves that thread in D-state for the life of the process, and an in-process
resume answered `hung` forever over a path that had long been healthy. A hung path is asked
afresh at most once per `HUNG_RETRY_S`, and never with more than `MAX_STUCK_PER_PATH` abandoned
threads of its own alive — at that cap it answers `hung` until one of them returns, so the threads
a flapping mount strands are bounded by the declared targets times the cap, never by time.

A FULL RUN DIRECTORY IS ONE RULE AT ALL THREE SITES (review 2026-10-08, round 3). The run directory
is the one probed target a CANDIDATE can break by itself — its workdir lives under it — and it is
SHARED: with `max_parallel > 1` the node that filled it and every sibling that then died ENOSPC see
the same probe answer. Three sites read it — the containment of an engine-side OSError
(`engine/evaluate.py::_contain_eval_crash`), the probe after a failed attempt, the probe before a
launch — and before this rule each answered differently: the containment paused with no terminal
(a candidate filling the disk looped pause → resume → refill forever), the post-failure probe
blamed EVERY node that met the full disk (bystanders bought a triage whose prompt said "your
writes may have filled it", and a `reject_idea` closed lineages that wrote nothing), and the
pre-launch probe after that blame paused the whole run as a box fault over the files the blamed
lifecycle had itself just written. The rule, `run_dir_full_blames`:

  * A full run dir is the BOX's by default: the run pauses `infra_unavailable`, the node stays
    pending, the withheld row records `fault: run_dir_full` (`RUN_DIR_FULL`), and the pause row and
    the attention item name the largest node workdir (`node_workdir_usage`, bounded) — "node_7's
    workdir holds 412.0 GB" is what the operator has to act on.
  * A lifecycle is blamed only on EVIDENCE tying the condition to it, and only after something of
    it RAN: its workdir is the LARGEST node workdir measured (strictly) AND either it holds at least
    half the bytes used on the filesystem (`DOMINANT_SHARE`), or this same lifecycle already met a
    full run dir (a durable `run_dir_full` withheld row — read from the log, so a resume keeps it).
    A bystander is never the largest, so it never reaches a triage over a full disk; the filler is
    blamed at the latest on its second meeting, so pause → resume → refill is bounded at ONE pause
    per lifecycle. Blamed, it takes the ordinary failure path with the probe's sentence as
    evidence (the containment, which has no attempt result to repair, writes a `crash` terminal).
  * Before a launch nothing of the attempt has run, so the probe there never blames; but an attempt
    re-launched in a workdir its own lifecycle already ran in re-materializes that workdir once and
    asks again before it calls the full disk the box's (`engine/evaluate.py::_eval_infra_pause`).

Layering: `runtime` imports nothing above `core`; this module imports only stdlib.
"""
from __future__ import annotations

import errno
import os
import re
import stat as _stat
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

# The errnos that describe the STORAGE or the TRANSPORT to it rather than a path the caller got
# wrong. ENOENT is deliberately absent: for a strict target (a declared mount) any OSError is a
# fault anyway, and for a lenient one (an env path) a missing path is the ordinary answer.
INFRA_ERRNOS: frozenset[int] = frozenset(
    code for code in (getattr(errno, name, None) for name in (
        "ENOTCONN", "EIO", "ESTALE", "EHOSTDOWN", "EHOSTUNREACH", "ENODEV", "ENXIO",
        "ETIMEDOUT", "ENOSPC", "EROFS", "EREMOTEIO", "ECONNABORTED", "ECONNRESET", "ENOLINK",
        "EDQUOT"))
    if code is not None)

PROBE_ROLES: tuple[str, ...] = ("mount", "editable", "run_dir", "interpreter", "env_path")
_STRICT_ROLES = frozenset({"mount", "editable", "run_dir", "interpreter"})

DEFAULT_TIMEOUT_S = 10.0
_PROBE_FILE = ".looplab-infra-probe"

# Paths whose probe thread never came back, keyed on (role, path) -> `_Stuck`. Process-wide on
# purpose: the thread outlives the call that started it, and so does the fact that the path hangs —
# until it expires (`HUNG_RETRY_S`) or one of the threads returns.
_HUNG: dict = {}
_HUNG_LOCK = threading.Lock()
# How long a path known to hang answers `hung` before ONE fresh probe is allowed, and how many of its
# own abandoned threads may be alive at once (at the cap it answers `hung` without starting another).
HUNG_RETRY_S = 60.0
MAX_STUCK_PER_PATH = 3


@dataclass
class _Stuck:
    threads: list            # this path's abandoned probe threads (pruned as they finish)
    since: float             # monotonic time of the latest probe that did not return
    blocking: bool = True    # False once a fresh probe of the path answered in time


@dataclass(frozen=True)
class InfraFault:
    """One declared path the engine could not reach. `cause` is an errno NAME (`ENOTCONN`), or
    `timeout` (the probe did not return in time), `hung` (an earlier probe of it still has not),
    `missing` / `not_executable` (an interpreter)."""
    role: str
    path: str
    cause: str
    detail: str = ""

    def sentence(self) -> str:
        tail = f" ({self.detail})" if self.detail else ""
        return f"{self.role} {self.path}: {self.cause}{tail}"


def _errno_name(exc: OSError) -> str:
    code = getattr(exc, "errno", None)
    return errno.errorcode.get(code, f"errno {code}") if isinstance(code, int) else type(exc).__name__


def _touch_dir(path: str) -> None:
    os.stat(path)
    if os.path.isdir(path):
        with os.scandir(path) as it:
            next(it, None)


def _touch_writable(path: str) -> None:
    probe = os.path.join(path, f"{_PROBE_FILE}-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, b"x")
    finally:
        os.close(fd)
        try:
            os.unlink(probe)
        except FileNotFoundError:
            pass


def _check_interpreter(path: str) -> Optional[str]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing"
    if not _stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
        return "not_executable"
    return None


def _probe_one(role: str, path: str) -> Optional[InfraFault]:
    """The blocking probe of ONE target. Raises nothing: every answer is a fault or None."""
    try:
        if role == "interpreter":
            cause = _check_interpreter(path)
            return InfraFault(role, path, cause) if cause else None
        if role == "run_dir":
            _touch_writable(path)
        elif role == "env_path":
            os.stat(path)
        else:
            _touch_dir(path)
    except OSError as exc:
        if role in _STRICT_ROLES or getattr(exc, "errno", None) in INFRA_ERRNOS:
            return InfraFault(role, path, _errno_name(exc), str(exc)[:200])
    return None


def probe_target(role: str, path: str, *, timeout: float = DEFAULT_TIMEOUT_S) -> Optional[InfraFault]:
    """Probe one target under a deadline. A probe that does not return in `timeout` seconds is a
    `timeout` fault, and the path is remembered as hung until a later probe of it finishes."""
    assert role in PROBE_ROLES, role
    key = (role, path)
    with _HUNG_LOCK:
        stuck = _HUNG.get(key)
        if stuck is not None:
            stuck.threads = [t for t in stuck.threads if t.is_alive()]
            if not stuck.threads:
                _HUNG.pop(key, None)       # every abandoned probe came back: the memo is spent
            elif (len(stuck.threads) >= MAX_STUCK_PER_PATH
                  or (stuck.blocking and time.monotonic() - stuck.since < HUNG_RETRY_S)):
                return InfraFault(role, path, "hung", "an earlier probe of this path has not returned")
            else:
                # Expired (or a fresh probe already answered): ask again, ONE thread at a time —
                # `since` moves now, so a concurrent caller still answers `hung` meanwhile.
                stuck.since = time.monotonic()
    box: dict = {}

    def _run() -> None:
        box["fault"] = _probe_one(role, path)

    worker = threading.Thread(target=_run, name=f"looplab-infra-probe:{role}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        with _HUNG_LOCK:
            stuck = _HUNG.get(key)
            if stuck is None:
                _HUNG[key] = _Stuck([worker], time.monotonic())
            else:
                stuck.threads.append(worker)
                stuck.since, stuck.blocking = time.monotonic(), True
        return InfraFault(role, path, "timeout", f"no answer in {timeout:g}s")
    with _HUNG_LOCK:
        stuck = _HUNG.get(key)
        if stuck is not None:
            stuck.blocking = False         # the path answers now; old threads only count to the cap
    return box.get("fault")


def declared_targets(*, run_dir=None, repo_spec: Optional[dict] = None,
                     interpreter: Optional[str] = None,
                     env: Optional[dict] = None) -> list[tuple[str, str]]:
    """`[(role, path)]`, de-duplicated in declaration order: what the OPERATOR declared, never a
    path the candidate named. A non-absolute env value, or one that is not a plain path, is not a
    target — `VS_LOCAL_DATA_ROOT=/data/x` is one, `NO_PROXY=a,b` and `LR=3e-4` are not."""
    out: list[tuple[str, str]] = []
    seen: set = set()

    def _add(role: str, path) -> None:
        if not isinstance(path, (str, os.PathLike)):
            return
        text = os.fspath(path)
        if not text or "\x00" in text or (role, text) in seen:
            return
        seen.add((role, text))
        out.append((role, text))

    if run_dir:
        _add("run_dir", run_dir)
    spec = repo_spec or {}
    for ed in spec.get("editables") or []:
        if isinstance(ed, dict):
            _add("editable", ed.get("path"))
    for _name, ds in (spec.get("data") or {}).items():
        _add("mount", ds.get("path") if isinstance(ds, dict) else ds)
    for ref in spec.get("references") or []:
        # Only a MOUNTED reference is something the eval reads; a context-only one
        # (`ReferenceSpec.mount` False, the default) is read by agents at build time, and its
        # absence must not pause an evaluation that never touches it.
        if isinstance(ref, dict) and ref.get("mount") is True:
            _add("mount", ref.get("path"))
    if interpreter:
        _add("interpreter", interpreter)
    for value in (env or {}).values():
        if (isinstance(value, str) and os.path.isabs(value) and os.pathsep not in value
                and "," not in value and "\n" not in value):
            _add("env_path", value)
    return out


# A path that does not exist (yet) — the two spellings a probe reports for it.
_ABSENT_CAUSES = frozenset({"ENOENT", "missing"})


def admissible_faults(faults: Iterable[InfraFault], *, seen_working: bool) -> list[InfraFault]:
    """The faults that may pause a run, given whether its box was ever seen working
    (`engine/evaluate.py::box_seen_working`: a node evaluated, a stage ran `ok`, a setup finished).

    A declared mount or the task's own interpreter that does not EXIST is a box fault only once the
    box has been seen working (critic 2026-10-08): before the first evaluated node, an operator's
    `run_setup` may still be about to download the data or build the env (`_ensure_run_setup` runs
    inside the launch, after the pre-launch probe), and pausing there would pause on every resume,
    forever. A path that exists but does not ANSWER (ENOTCONN, EIO, a hang) is a fault either way —
    that is the incident's shape, and no setup step produces it.

    The CONTAINMENT of an engine-side OSError does not apply this filter (round 3,
    `engine/evaluate.py::_contain_eval_crash` passes `seen_working=True`): `_materialize` runs before
    `_ensure_run_setup`, so on that path no setup step can still be about to create the path, and
    filtering it ended the node `engine_error` AND paused the run — one node per resume."""
    return [f for f in faults
            if seen_working or not (f.role in ("mount", "interpreter") and f.cause in _ABSENT_CAUSES)]


def probe(targets: Iterable[tuple[str, str]], *,
          timeout: float = DEFAULT_TIMEOUT_S) -> list[InfraFault]:
    """Every fault among `targets`, in their order. Empty means the box answered for all of them.

    Sequential and short-circuit-free on purpose: the report names EVERY unreachable path, because
    "the data mount AND the run directory are gone" is a different repair than either alone. The
    worst case is bounded by `timeout` per target, and a path already known to hang costs nothing."""
    faults = []
    for role, path in targets:
        fault = probe_target(role, path, timeout=timeout)
        if fault is not None:
            faults.append(fault)
    return faults


# The faults a CANDIDATE can cause by itself on a healthy box: its workdir lives under the run
# directory, so a candidate that writes checkpoints until the disk (or the user's quota) is full makes
# the engine's own `run_dir` probe answer exactly these. Nothing else in the probe table writes, so no
# other role can see them from a candidate's writes.
CANDIDATE_FILLABLE_ERRNOS: frozenset[str] = frozenset({"ENOSPC", "EDQUOT"})

# The fault CLASS a withheld row and a pause row record for a full run directory (`fault` on
# `eval_attempt_withheld`, read back by `engine/evaluate.py::run_dir_full_met`).
RUN_DIR_FULL = "run_dir_full"


def candidate_may_have_caused(faults: Iterable[InfraFault]) -> bool:
    """True when EVERY fault is the run directory being full (`CANDIDATE_FILLABLE_ERRNOS`) — the one
    probe answer a candidate's own writes CAN produce — and there is at least one. Any other fault
    beside it (a dead mount, an `EIO`, a vanished interpreter) keeps the whole answer the box's: "a
    dead mount never blames the candidate".

    It NOMINATES, never decides (round 3): the run directory is shared, so every sibling that died
    ENOSPC after one node filled it sees exactly this answer too. Whether the condition is tied to
    a given lifecycle is `run_dir_full_blames`, on the evidence the module docstring lists."""
    faults = list(faults)
    return bool(faults) and all(f.role == "run_dir" and f.cause in CANDIDATE_FILLABLE_ERRNOS
                                for f in faults)


# ------------------------------------------------------------- who holds the bytes (bounded)

# The whole walk's bounds: entries `lstat`ed across every node workdir (shared evenly, so one
# workdir of a million small files cannot starve the others' measurement), wall-clock seconds
# (likewise shared), and node directories considered at all. A measurement cut short by any of them
# is a LOWER BOUND and says so (`WorkdirUsage.complete`).
USAGE_MAX_ENTRIES = 200_000
USAGE_DEADLINE_S = 5.0
USAGE_MAX_DIRS = 4096
# A workdir holding at least this share of the bytes used on the run directory's filesystem is the
# DOMINANT consumer: on that alone it is tied to the full disk at its first meeting.
DOMINANT_SHARE = 0.5
_NODE_DIR = re.compile(r"node_(\d{1,9})\Z")


@dataclass(frozen=True)
class WorkdirUsage:
    """The bytes ONE node workdir (`<run>/nodes/node_<id>`) holds on disk, as allocated (`st_blocks`,
    so a sparse file counts what it occupies), never following a link. `complete` False: the walk
    hit a bound, and `bytes` is a lower bound."""
    node_id: int
    bytes: int
    complete: bool = True


def _allocated(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return int(blocks) * 512 if isinstance(blocks, int) else max(0, int(st.st_size))


def _tree_bytes(root: str, budget: int, deadline: float) -> tuple[int, bool]:
    """Bytes under `root`, iteratively (no recursion limit), `lstat` only (a FIFO or a device is
    never opened, a link never followed), bounded by `budget` entries and the `deadline`."""
    total, seen, stack = 0, 0, [root]
    while stack:
        if seen >= budget or time.monotonic() >= deadline:
            return total, False
        top = stack.pop()
        try:
            with os.scandir(top) as it:
                for entry in it:
                    seen += 1
                    if seen > budget:
                        return total, False
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += _allocated(st)
                    if _stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
        except OSError:
            if top == root:
                return total, False
            continue
    return total, True


def node_workdir_usage(nodes_root, *, max_entries: int = USAGE_MAX_ENTRIES,
                       deadline_s: float = USAGE_DEADLINE_S) -> list[WorkdirUsage]:
    """What every node workdir under `nodes_root` holds, in node-id order; `[]` when the directory
    cannot be listed. EVERY node, not only the pending ones: a node that already reached its
    terminal keeps its workdir, and when its 400 GB are what fills the disk the operator must be told
    that, and the pending lifecycle that met the full disk must not be measured as the largest.
    Raises nothing."""
    dirs: list[tuple[int, str]] = []
    try:
        with os.scandir(os.fspath(nodes_root)) as it:
            for entry in it:
                match = _NODE_DIR.match(entry.name)
                try:
                    is_dir = match is not None and entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir:
                    dirs.append((int(match.group(1)), entry.path))
                    if len(dirs) >= USAGE_MAX_DIRS:
                        break
    except (OSError, TypeError, ValueError):
        return []
    if not dirs:
        return []
    budget = max(64, int(max_entries) // len(dirs))
    slice_s = max(0.0, float(deadline_s)) / len(dirs)
    out = []
    for node_id, path in sorted(dirs):
        total, complete = _tree_bytes(path, budget, time.monotonic() + slice_s)
        out.append(WorkdirUsage(node_id, total, complete))
    return out


def filesystem_used_bytes(path) -> Optional[int]:
    """Bytes in use on the filesystem holding `path` (`statvfs`: blocks minus free blocks), or None
    where it cannot be asked (no `statvfs`, an OSError). Raises nothing."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    try:
        st = statvfs(os.fspath(path))
    except (OSError, TypeError, ValueError):
        return None
    used = (int(st.f_blocks) - int(st.f_bfree)) * int(st.f_frsize)
    return used if used > 0 else None


def largest_workdir(usage: Iterable[WorkdirUsage]) -> Optional[WorkdirUsage]:
    """The node workdir holding the most bytes, or None (nothing measured, or every one empty)."""
    best = None
    for u in usage:
        if u.bytes > 0 and (best is None or u.bytes > best.bytes):
            best = u
    return best


def run_dir_full_blames(node_id: int, usage: Iterable[WorkdirUsage], *, fs_used: Optional[int],
                        met_before: bool) -> bool:
    """Does the evidence tie a FULL run directory to node `node_id`'s lifecycle? (The module
    docstring's rule; the caller asks only after something of the lifecycle ran.)

    Its workdir must be the LARGEST measured — strictly: a tie, an empty workdir or an unmeasured
    one is a bystander — AND either DOMINANT (at least `DOMINANT_SHARE` of the bytes used on the
    filesystem; a quota's `EDQUOT` rarely is, which is what the second clause is for) or
    `met_before`: this same lifecycle already met a full run dir, the space was freed by its
    re-materialization on the resume, and it filled it again."""
    usage = list(usage)
    own = next((u for u in usage if u.node_id == node_id), None)
    if own is None or own.bytes <= 0:
        return False
    if any(u.node_id != node_id and u.bytes >= own.bytes for u in usage):
        return False
    if met_before:
        return True
    return isinstance(fs_used, int) and fs_used > 0 and own.bytes >= DOMINANT_SHARE * fs_used


def human_bytes(n: int) -> str:
    """`412.0 GB`, `3.5 MB`, `512 B` — decimal units, one decimal place above bytes."""
    n = max(0, int(n))
    if n < 1000:
        return f"{n} B"
    value = float(n)
    for unit in ("kB", "MB", "GB", "TB", "PB"):
        value /= 1000.0
        if value < 1000.0 or unit == "PB":
            return f"{value:.1f} {unit}"
    return f"{n} B"                                          # unreachable


def occupancy_sentence(usage: Iterable[WorkdirUsage]) -> str:
    """One clause naming who holds the bytes, for a pause row's `detail` and the log:
    "node_7's workdir holds 412.0 GB, the largest of 3 node workdirs". "" when nothing was measured."""
    usage = list(usage)
    top = largest_workdir(usage)
    if top is None:
        return ""
    cut = "" if all(u.complete for u in usage) else " (at least: the measurement was bounded)"
    return (f"node_{top.node_id}'s workdir holds {human_bytes(top.bytes)}{cut}, the largest of "
            f"{len(usage)} node workdir{'' if len(usage) == 1 else 's'}")


def describe(faults: Iterable[InfraFault]) -> str:
    """One line naming every fault, for a pause row's `detail` and the log."""
    return "; ".join(f.sentence() for f in faults)
