"""DURABLE MIRROR of run directories that live in tmpfs, and their restore (doc 80 P2, CX §7).

WHY. On the MiniOneRec box the run root is `/dev/shm/looplab-runs`: the engine's hot appends, lock
files and mkdirs must not touch the S3 FUSE mount (`/home/jovyan/data`, geesefs), which has hung a
directory for days (2026-09-29) and measures 105-950 ms per absent-file `lstat`. tmpfs is wiped by a
pod restart, and the pod restarted four times on 2026-10-07/08. Each operator kept a copy alive with
an ad-hoc script — `restart-control/run-mirror.py` (107 lines, whole-file copies, no checksum, a
torn last line copied as is) and ~730 lines of `persist`/`mirror` in the external agent's own
tooling. This module is that job done once, inside LoopLab.

WHAT. `mirror_pass` copies each run directory under `src_root` to `dst_root/<run>`:

  * only files whose (size, mtime) changed since the last copy (state in `src_root/.looplab-mirror/
    state.json`, which lives in tmpfs and is SEEDED from the destination manifests when it is
    missing, so the first pass after a restore does not re-upload everything);
  * an append-only `*.jsonl` is copied up to its LAST NEWLINE, so a copy taken while the engine
    appends never carries a torn line (`looplab resume` would refuse it, `repair-log` exists for it);
  * a file at most `atomic_max_bytes` is written to `<name>.mirror-tmp` and renamed over the copy,
    so a reader of the destination never sees half a file; a larger one is written in place and is
    only trusted through the manifest below;
  * the PER-RUN MANIFEST `dst_root/<run>/.looplab-mirror.json` (written last, atomically) lists every
    mirrored file with its size, mtime and SHA-256 computed while copying. It is what a restore
    trusts: a file the manifest does not vouch for, or whose bytes do not hash to the manifest, is
    reported, never silently restored;
  * lock files, SQLite read models (rebuilt from the log), `__pycache__`, the eval canary's scratch
    and the run's private code repository (`code/`, rebuilt from the log by `code_identity.sync_run`)
    are skipped, as is any file over `max_file_bytes` (named in the manifest's `skipped`).

THROTTLED. A token bucket bounds bytes per second and destination file operations per second; the
big span logs are copied only every `slow_every` passes. Nothing ever LISTS or `stat`s the
destination during a pass (the state file is the memory of what was copied), and each destination
directory is created once per process. That is the metadata-storm rule for an S3 mount.

NEVER BLOCKS THE ENGINE. The mirror is its own process (`looplab run-mirror`); it takes no lock the
engine takes and only reads the source. Every destination operation runs in a worker thread under a
deadline (`op_timeout_s`, scaled by size): a FUSE call that does not return marks the pass STUCK —
recorded in the state with the path — and the pass stops touching the destination. With
`exit_on_stuck` the loop then exits non-zero so a supervisor can restart it; a thread stuck in the
kernel cannot be interrupted, but it can only ever hold up this process.

RESTORE. `restore_run` copies a mirrored run back (into a hidden staging directory, renamed into
place only when every file verified), preserves mtimes, and refuses to overwrite an existing run
directory. The engine is replay-based: `events.jsonl` plus the snapshots ARE the run, so a restored
copy resumes with `looplab resume`.

Imports nothing above `core`.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

STATE_DIR = ".looplab-mirror"
STATE_FILE = "state.json"
MANIFEST = ".looplab-mirror.json"
TMP_SUFFIX = ".mirror-tmp"
_STATE_VERSION = 1

SKIP_SUFFIXES = (".lock", TMP_SUFFIX, ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
                 ".pyc", ".tmp")
SKIP_NAMES = frozenset({"engine.lock"})
# Top-level directories of a run that are rebuildable or scratch: the private code repository is
# rebuilt from the log (`engine/code_identity.py::sync_run`), the canary tree is disposable by design.
SKIP_TOP_DIRS = frozenset({"code", "canary", STATE_DIR})
SKIP_ANY_DIRS = frozenset({"__pycache__", ".git"})
# The large append-only trace logs: worth keeping, not worth re-uploading every pass.
SLOW_FILES = frozenset({"spans.jsonl", ".spans-append.jsonl", "trace.json"})
# Copied first: without these a mirrored run cannot be resumed at all.
_FIRST = ("events.jsonl", "task.snapshot.json", "config.snapshot.json")
_ERRORS_KEPT = 20


class MirrorStuck(RuntimeError):
    """A destination operation did not return within its deadline."""


@dataclass
class MirrorConfig:
    src_root: Path
    dst_root: Path
    state_path: Optional[Path] = None
    interval_s: float = 300.0
    max_bytes_per_s: float = 16 * 1024 * 1024
    max_files_per_s: float = 20.0
    max_file_bytes: int = 256 * 1024 * 1024
    atomic_max_bytes: int = 64 * 1024 * 1024
    slow_every: int = 6
    op_timeout_s: float = 120.0
    runs: tuple = ()
    exclude: tuple = ()

    def __post_init__(self):
        self.src_root = Path(self.src_root)
        self.dst_root = Path(self.dst_root)
        if self.state_path is None:
            self.state_path = self.src_root / STATE_DIR / STATE_FILE
        self.state_path = Path(self.state_path)
        if self.src_root.resolve() == self.dst_root.resolve():
            raise ValueError("the mirror's source and destination are the same directory")
        if self.dst_root.resolve().is_relative_to(self.src_root.resolve()):
            raise ValueError("the mirror's destination may not live inside its source")


# ------------------------------------------------------------------------------------- pacing
class _Bucket:
    """A token bucket: `take(n)` sleeps until `n` tokens are available at `rate` per second."""

    def __init__(self, rate: float, *, clock=time.monotonic, sleep=time.sleep):
        self.rate = float(rate)
        self.clock, self.sleep = clock, sleep
        self.tokens = self.rate
        self.at = clock()

    def take(self, n: float) -> None:
        if self.rate <= 0:
            return
        now = self.clock()
        self.tokens = min(self.rate, self.tokens + (now - self.at) * self.rate)
        self.at = now
        self.tokens -= n
        if self.tokens < 0:
            wait = -self.tokens / self.rate
            self.sleep(wait)
            self.at = self.clock()
            self.tokens = 0.0


def _deadline_call(fn: Callable, timeout: float, what: str):
    """Run `fn()` in a worker thread; raise `MirrorStuck` if it has not returned by `timeout`.

    The worker is a daemon: a FUSE call stuck in the kernel cannot be cancelled, so it is
    abandoned, and the caller stops touching the destination for the rest of the pass."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 — handed back to the caller, re-raised there
            box["error"] = exc

    worker = threading.Thread(target=run, name="looplab-mirror-op", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise MirrorStuck(f"{what} did not return within {timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ------------------------------------------------------------------------------------- planning
def _skipped(rel: str, cfg: MirrorConfig) -> Optional[str]:
    parts = rel.split("/")
    name = parts[-1]
    if parts[0] in SKIP_TOP_DIRS and len(parts) > 1:
        return "rebuildable"
    if any(p in SKIP_ANY_DIRS for p in parts[:-1]):
        return "rebuildable"
    if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
        return "transient"
    if any(fnmatch.fnmatch(rel, pat) for pat in cfg.exclude):
        return "excluded"
    return None


def _priority(rel: str) -> tuple:
    if rel in _FIRST:
        return (0, _FIRST.index(rel), rel)
    top = "/" not in rel
    if top and rel.endswith((".jsonl", ".json")) and rel not in SLOW_FILES:
        return (1, 0, rel)
    if top and rel not in SLOW_FILES:
        return (2, 0, rel)
    if rel.startswith(("base_snapshots/", "attempt_logs/", "per_query/", ".commands/")):
        return (3, 0, rel)
    if rel in SLOW_FILES:
        return (5, 0, rel)
    return (4, 0, rel)


def list_runs(cfg: MirrorConfig) -> list[Path]:
    try:
        children = sorted(p for p in cfg.src_root.iterdir()
                          if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))
    except OSError:
        return []
    if cfg.runs:
        wanted = set(cfg.runs)
        children = [p for p in children if p.name in wanted]
    # A run is only a run when it has an event log; anything else under the root is not ours.
    return [p for p in children if (p / "events.jsonl").is_file()]


def plan_run(run_dir: Path, cfg: MirrorConfig, copied: dict, pass_no: int) -> tuple[list, dict]:
    """`(todo, skipped)`: files of `run_dir` that changed since `copied` recorded them, in priority
    order, as `(rel, size, mtime_ns)`; and `{rel: reason}` for what is never mirrored."""
    todo, skipped = [], {}
    for dirpath, dirnames, filenames in os.walk(run_dir, followlinks=False):
        base = Path(dirpath)
        rel_dir = base.relative_to(run_dir).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_ANY_DIRS
                             and not (rel_dir == "" and d in SKIP_TOP_DIRS)
                             and not (base / d).is_symlink())
        for name in sorted(filenames):
            rel = rel_dir + name
            why = _skipped(rel, cfg)
            if why is not None:
                continue
            path = base / name
            try:
                st = path.lstat()
            except OSError:
                continue
            if not os.path.isfile(path) or path.is_symlink():
                continue
            if st.st_size > cfg.max_file_bytes:
                skipped[rel] = f"larger than {cfg.max_file_bytes} bytes ({st.st_size})"
                continue
            if name in SLOW_FILES and cfg.slow_every > 1 and pass_no % cfg.slow_every:
                if rel in copied:
                    continue
            prior = copied.get(rel)
            if prior and prior.get("size") == st.st_size and prior.get("mtime_ns") == st.st_mtime_ns:
                continue
            todo.append((rel, st.st_size, st.st_mtime_ns))
    todo.sort(key=lambda row: _priority(row[0]))
    return todo, skipped


# ------------------------------------------------------------------------------------- copying
def _jsonl_cut(path: Path, size: int) -> int:
    """The byte count of `path`'s first `size` bytes that ends at its last newline (0 if none)."""
    if size <= 0:
        return 0
    window = min(size, 1 << 20)
    with open(path, "rb") as fh:
        fh.seek(size - window)
        tail = fh.read(window)
    nl = tail.rfind(b"\n")
    if nl >= 0:
        return size - window + nl + 1
    if window == size:
        return 0
    # A single line longer than the window: fall back to a full forward scan (rare, bounded by size).
    last = 0
    with open(path, "rb") as fh:
        pos = 0
        while pos < size:
            chunk = fh.read(min(1 << 20, size - pos))
            if not chunk:
                break
            idx = chunk.rfind(b"\n")
            if idx >= 0:
                last = pos + idx + 1
            pos += len(chunk)
    return last


def _read_source(path: Path, size: int, bucket: _Bucket, chunk: int = 1 << 20):
    """Yield the first `size` bytes of `path` in chunks, paced by `bucket` (bytes)."""
    with open(path, "rb") as fh:
        left = size
        while left > 0:
            data = fh.read(min(chunk, left))
            if not data:
                break
            bucket.take(len(data))
            left -= len(data)
            yield data


def copy_one(src: Path, dst: Path, *, size: int, mtime_ns: int, cfg: MirrorConfig,
             bytes_bucket: _Bucket) -> dict:
    """Copy one file and return its manifest row `{size, mtime_ns, sha256}`.

    The SOURCE is read in this thread (tmpfs, cannot hang); every DESTINATION operation goes through
    `_deadline_call`. A `*.jsonl` is cut at its last newline."""
    cut = _jsonl_cut(src, size) if src.name.endswith(".jsonl") else size
    digest = hashlib.sha256()
    timeout = cfg.op_timeout_s + cut / max(1.0, cfg.max_bytes_per_s or 1.0) * 4
    if cut <= cfg.atomic_max_bytes:
        chunks = []
        for data in _read_source(src, cut, bytes_bucket):
            digest.update(data)
            chunks.append(data)
        payload = b"".join(chunks)

        def write():
            tmp = dst.with_name(dst.name + TMP_SUFFIX)
            with open(tmp, "wb") as fh:
                fh.write(payload)
            os.replace(tmp, dst)
            os.utime(dst, ns=(mtime_ns, mtime_ns))
        _deadline_call(write, timeout, f"writing {dst}")
        cut = len(payload)
    else:
        # Too big to hold: stream it straight into place; the manifest is the only trust anchor.
        written = [0]

        def write_stream():
            with open(dst, "wb") as fh:
                for data in _read_source(src, cut, bytes_bucket):
                    digest.update(data)
                    fh.write(data)
                    written[0] += len(data)
            os.utime(dst, ns=(mtime_ns, mtime_ns))
        _deadline_call(write_stream, timeout, f"writing {dst}")
        cut = written[0]
    return {"size": cut, "mtime_ns": mtime_ns, "sha256": digest.hexdigest(),
            **({"source_size": size} if cut != size else {})}


# ------------------------------------------------------------------------------------- state
def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def load_state(cfg: MirrorConfig) -> dict:
    state = _read_json(cfg.state_path, {})
    if state.get("version") != _STATE_VERSION or state.get("dst_root") != str(cfg.dst_root):
        state = {"version": _STATE_VERSION, "dst_root": str(cfg.dst_root), "pass": 0,
                 "runs": {}, "errors": []}
    state.setdefault("runs", {})
    state.setdefault("errors", [])
    return state


def save_state(cfg: MirrorConfig, state: dict) -> None:
    _write_json_atomic(cfg.state_path, state)


def _manifest_rows(cfg: MirrorConfig, run: str) -> Optional[dict]:
    """The destination manifest's `files` map for `run`, read once under a deadline (None if absent
    or unreadable). Seeds a missing local state so a restart does not re-upload everything."""
    path = cfg.dst_root / run / MANIFEST
    try:
        data = _deadline_call(lambda: _read_json(path, {}), cfg.op_timeout_s, f"reading {path}")
    except MirrorStuck:
        raise
    except OSError:
        return None
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else None


# ------------------------------------------------------------------------------------- one pass
@dataclass
class PassReport:
    runs: dict = field(default_factory=dict)
    stuck: Optional[str] = None
    errors: list = field(default_factory=list)
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"runs": self.runs, "stuck": self.stuck, "errors": self.errors,
                "seconds": round(self.seconds, 3)}


def mirror_run(run_dir: Path, cfg: MirrorConfig, state: dict, *, pass_no: int,
               bytes_bucket: _Bucket, ops_bucket: _Bucket, made_dirs: set) -> dict:
    """Mirror one run; returns its row for the pass report. Raises `MirrorStuck`."""
    run = run_dir.name
    rstate = state["runs"].setdefault(run, {})
    copied = rstate.get("files")
    if not isinstance(copied, dict):
        copied = _manifest_rows(cfg, run) or {}
        rstate["files"] = copied
    todo, skipped = plan_run(run_dir, cfg, copied, pass_no)
    dst_run = cfg.dst_root / run
    n = nbytes = 0
    errors = []
    for rel, size, mtime_ns in todo:
        dst = dst_run / rel
        parent = dst.parent
        if str(parent) not in made_dirs:
            ops_bucket.take(1)
            _deadline_call(lambda p=parent: p.mkdir(parents=True, exist_ok=True),
                           cfg.op_timeout_s, f"mkdir {parent}")
            made_dirs.add(str(parent))
        ops_bucket.take(1)
        try:
            row = copy_one(run_dir / rel, dst, size=size, mtime_ns=mtime_ns, cfg=cfg,
                           bytes_bucket=bytes_bucket)
        except MirrorStuck:
            raise
        except FileNotFoundError:
            continue                       # removed between the walk and the copy
        except OSError as exc:
            errors.append({"run": run, "path": rel, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        copied[rel] = row
        n += 1
        nbytes += row["size"]
    # Rows for files that no longer exist at the source are dropped from the manifest (their stale
    # copy stays in the destination, unreferenced: deleting on S3 is a cost this job does not pay).
    present = {rel for rel in copied if (run_dir / rel).is_file()}
    for rel in list(copied):
        if rel not in present:
            copied.pop(rel, None)
    if n or not rstate.get("manifest_written") or skipped != rstate.get("skipped", {}):
        manifest = {"version": 1, "run": run, "source": str(run_dir), "updated_at": time.time(),
                    "files": copied, "skipped": skipped}
        ops_bucket.take(1)
        # `_write_json_atomic` creates the run's destination directory itself (an unchanged run
        # seeded from a state file may never have made it in this process).
        _deadline_call(lambda: _write_json_atomic(dst_run / MANIFEST, manifest),
                       cfg.op_timeout_s, f"writing {dst_run / MANIFEST}")
        rstate["changed"] = True
        rstate["manifest_written"] = True
        rstate["skipped"] = skipped
    newest = max((row.get("mtime_ns", 0) for row in copied.values()), default=0)
    rstate.update({"last_mirrored_at": time.time(), "newest_copied_mtime_ns": newest,
                   "file_count": len(copied)})
    return {"copied": n, "bytes": nbytes, "pending_before": len(todo), "skipped": len(skipped),
            "errors": errors}


def mirror_pass(cfg: MirrorConfig, *, state: Optional[dict] = None,
                clock=time.monotonic, sleep=time.sleep) -> PassReport:
    """One pass over every run under `cfg.src_root`. Saves the state after each run."""
    state = load_state(cfg) if state is None else state
    pass_no = int(state.get("pass", 0))
    report = PassReport()
    t0 = clock()
    bytes_bucket = _Bucket(cfg.max_bytes_per_s, clock=clock, sleep=sleep)
    ops_bucket = _Bucket(cfg.max_files_per_s, clock=clock, sleep=sleep)
    made_dirs: set = set()
    state["last_pass_started_at"] = time.time()
    try:
        for run_dir in list_runs(cfg):
            try:
                row = mirror_run(run_dir, cfg, state, pass_no=pass_no, bytes_bucket=bytes_bucket,
                                 ops_bucket=ops_bucket, made_dirs=made_dirs)
            except MirrorStuck as exc:
                report.stuck = str(exc)
                state["stuck"] = {"at": time.time(), "run": run_dir.name, "detail": str(exc)}
                break
            except OSError as exc:
                row = {"errors": [{"run": run_dir.name, "error": f"{type(exc).__name__}: {exc}"[:300]}]}
            report.runs[run_dir.name] = row
            report.errors.extend(row.get("errors") or [])
            # Saved after every run that CHANGED, so a crash mid-pass loses at most one run's
            # progress (and re-copies it, which is idempotent); an unchanged run costs no rewrite.
            if (state["runs"].get(run_dir.name) or {}).pop("changed", False):
                save_state(cfg, state)
        else:
            state.pop("stuck", None)
    finally:
        report.seconds = clock() - t0
        state["pass"] = pass_no + 1
        state["last_pass_finished_at"] = time.time()
        state["last_pass"] = {"seconds": round(report.seconds, 3), "stuck": report.stuck,
                              "copied": sum(r.get("copied", 0) for r in report.runs.values()),
                              "bytes": sum(r.get("bytes", 0) for r in report.runs.values())}
        state["errors"] = (state.get("errors", []) + [dict(e, at=time.time())
                                                      for e in report.errors])[-_ERRORS_KEPT:]
        save_state(cfg, state)
    return report


def mirror_loop(cfg: MirrorConfig, *, passes: Optional[int] = None, exit_on_stuck: bool = False,
                log: Callable[[str], None] = print, sleep=time.sleep,
                stop: Optional[threading.Event] = None) -> int:
    """Run passes every `cfg.interval_s` seconds; returns 0, or 3 on a stuck destination with
    `exit_on_stuck` (so a supervisor restarts a process that may hold a thread stuck in FUSE)."""
    done = 0
    while True:
        report = mirror_pass(cfg)
        copied = sum(r.get("copied", 0) for r in report.runs.values())
        log(f"mirror pass: {len(report.runs)} run(s), {copied} file(s) copied, "
            f"{len(report.errors)} error(s), {report.seconds:.1f}s"
            + (f"; STUCK: {report.stuck}" if report.stuck else ""))
        done += 1
        if report.stuck and exit_on_stuck:
            return 3
        if passes is not None and done >= passes:
            return 0
        if stop is not None:
            if stop.wait(cfg.interval_s):
                return 0
        else:
            sleep(cfg.interval_s)


# ------------------------------------------------------------------------------------- status
def mirror_status(cfg: MirrorConfig) -> dict:
    """Read-only: last pass, stuck flag, recent errors and per-run LAG — how much newer the source
    is than the last copy (seconds), from the source alone (never the destination)."""
    state = load_state(cfg)
    runs = {}
    for run_dir in list_runs(cfg):
        rstate = state["runs"].get(run_dir.name) or {}
        try:
            newest_src = max((p.stat().st_mtime_ns for p in (run_dir / n for n in _FIRST)
                              if p.exists()), default=0)
        except OSError:
            newest_src = 0
        copied = (rstate.get("files") or {})
        ev = copied.get("events.jsonl") or {}
        lag = None
        if ev:
            lag = max(0.0, (newest_src - int(ev.get("mtime_ns", 0))) / 1e9)
        runs[run_dir.name] = {"mirrored": bool(ev), "files": len(copied),
                              "last_mirrored_at": rstate.get("last_mirrored_at"),
                              "lag_s": lag, "skipped": rstate.get("skipped") or {}}
    return {"src_root": str(cfg.src_root), "dst_root": str(cfg.dst_root),
            "passes": state.get("pass", 0), "last_pass": state.get("last_pass"),
            "last_pass_finished_at": state.get("last_pass_finished_at"),
            "stuck": state.get("stuck"), "errors": state.get("errors", [])[-10:], "runs": runs}


# ------------------------------------------------------------------------------------- restore
def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mirrored_runs(dst_root: Path) -> list[str]:
    """Runs the destination holds a manifest for (one listing of the destination root)."""
    try:
        return sorted(p.name for p in Path(dst_root).iterdir()
                      if p.is_dir() and (p / MANIFEST).is_file())
    except OSError:
        return []


def restore_run(dst_root, run: str, src_root, *, as_name: Optional[str] = None,
                verify: bool = True) -> dict:
    """Copy mirrored run `run` back to `src_root/<as_name or run>`.

    Refuses an existing target. Every file the manifest lists is copied into a hidden staging
    directory, verified against its SHA-256 (unless `verify=False`) and given its recorded mtime;
    only a staging tree with no failure is renamed into place. Returns a report with the files
    restored, the failures and the command that resumes the run."""
    src_root, dst_root = Path(src_root), Path(dst_root)
    name = as_name or run
    if not name or "/" in name or name.startswith("."):
        raise ValueError(f"invalid run name {name!r}")
    target = src_root / name
    if target.exists():
        raise FileExistsError(f"{target} exists: refusing to overwrite a run directory")
    mirror = dst_root / run
    manifest = _read_json(mirror / MANIFEST, {})
    files = manifest.get("files")
    if not isinstance(files, dict) or "events.jsonl" not in files:
        raise FileNotFoundError(f"{mirror} has no usable mirror manifest (no events.jsonl listed)")
    staging = src_root / f".{name}.restoring-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    failures, restored, nbytes = [], 0, 0
    try:
        for rel in sorted(files):
            row = files[rel]
            if rel.startswith("/") or ".." in rel.split("/"):
                failures.append({"path": rel, "error": "unsafe path in manifest"})
                continue
            src, out = mirror / rel, staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(src, out)
            except OSError as exc:
                failures.append({"path": rel, "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            if verify and _hash_file(out) != row.get("sha256"):
                failures.append({"path": rel, "error": "content does not match the manifest"})
                continue
            if isinstance(row.get("mtime_ns"), int):
                os.utime(out, ns=(row["mtime_ns"], row["mtime_ns"]))
            restored += 1
            nbytes += out.stat().st_size
        critical = [f for f in failures if f["path"] in _FIRST]
        if critical:
            raise RuntimeError("restore refused: " + "; ".join(
                f"{f['path']}: {f['error']}" for f in critical))
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"run": name, "from": str(mirror), "to": str(target), "files": restored,
            "bytes": nbytes, "failures": failures,
            "skipped_at_mirror": manifest.get("skipped") or {},
            "next": f"looplab resume {target}"}


def restore_missing(dst_root, src_root, *, verify: bool = True,
                    runs: Iterable[str] = ()) -> list[dict]:
    """Restore every mirrored run that is absent from `src_root` (after a pod restart)."""
    wanted = set(runs)
    out = []
    for run in mirrored_runs(dst_root):
        if wanted and run not in wanted:
            continue
        if (Path(src_root) / run).exists():
            continue
        try:
            out.append(restore_run(dst_root, run, src_root, verify=verify))
        except (OSError, RuntimeError, ValueError) as exc:
            out.append({"run": run, "error": f"{type(exc).__name__}: {exc}"[:500]})
    return out
