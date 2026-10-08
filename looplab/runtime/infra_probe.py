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
answer is the engine's, like `is_present`'s. The ONE exception is a FULL run directory: the
candidate's workdir shares its filesystem, so `disk_full_only` is believed once per lifecycle.

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
thread behind it.

Layering: `runtime` imports nothing above `core`; this module imports only stdlib.
"""
from __future__ import annotations

import errno
import os
import stat as _stat
import threading
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

# Paths whose probe thread never came back, keyed on (role, path). Process-wide on purpose: the
# thread outlives the call that started it, and so does the fact that the path hangs.
_HUNG: dict = {}
_HUNG_LOCK = threading.Lock()


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
        if stuck is not None and stuck.is_alive():
            return InfraFault(role, path, "hung", "an earlier probe of this path has not returned")
        _HUNG.pop(key, None)
    box: dict = {}

    def _run() -> None:
        box["fault"] = _probe_one(role, path)

    worker = threading.Thread(target=_run, name=f"looplab-infra-probe:{role}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        with _HUNG_LOCK:
            _HUNG[key] = worker
        return InfraFault(role, path, "timeout", f"no answer in {timeout:g}s")
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
_DISK_FULL_CAUSES = frozenset({"ENOSPC", "EDQUOT"})


def admissible_faults(faults: Iterable[InfraFault], *, seen_working: bool) -> list[InfraFault]:
    """The faults that may pause a run, given whether its box was ever seen working
    (`engine/evaluate.py::box_seen_working`: a node evaluated, a stage ran `ok`, a setup finished).

    A declared mount or the task's own interpreter that does not EXIST is a box fault only once the
    box has been seen working (critic 2026-10-08): before the first evaluated node, an operator's
    `run_setup` may still be about to download the data or build the env (`_ensure_run_setup` runs
    inside the launch, after the pre-launch probe), and pausing there would pause on every resume,
    forever. A path that exists but does not ANSWER (ENOTCONN, EIO, a hang) is a fault either way —
    that is the incident's shape, and no setup step produces it."""
    return [f for f in faults
            if seen_working or not (f.role in ("mount", "interpreter") and f.cause in _ABSENT_CAUSES)]


def disk_full_only(faults: Iterable[InfraFault]) -> bool:
    """Is every fault the run directory being FULL? That one the candidate can cause itself — its
    workdir is on the same filesystem, and `RLIMIT_FSIZE` bounds a file, not the sum — so the engine
    believes it once per lifecycle and then lets the failure take the ordinary repair path
    (`engine/evaluate.py::EvaluateMixin._eval_infra_pause`)."""
    faults = list(faults)
    return bool(faults) and all(f.role == "run_dir" and f.cause in _DISK_FULL_CAUSES for f in faults)


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


def describe(faults: Iterable[InfraFault]) -> str:
    """One line naming every fault, for a pause row's `detail` and the log."""
    return "; ".join(f.sentence() for f in faults)
