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
  * every file is written to a sibling `<name>.<pid>.<n>.mirror-tmp`, unique per ATTEMPT, and
    renamed over the copy, so a reader of the destination never sees half a file and the last good
    copy is never truncated by the next one; a file at most `atomic_max_bytes` is buffered in memory
    first, a larger one is streamed into its temp file. An attempt abandoned on its deadline never
    publishes (see `copy_one`);
  * the PER-RUN MANIFEST `dst_root/<run>/.looplab-mirror.json` (written last, atomically) lists every
    mirrored file with its size, mtime and SHA-256 computed while copying. It is what a restore
    trusts: a file the manifest does not vouch for, or whose bytes do not hash to the manifest, is
    reported, never silently restored;
  * lock files, SQLite read models (rebuilt from the log), `__pycache__`, the eval canary's scratch
    and the run's private code repository (`code/repo.git`, `code/worktrees`, rebuilt from the log by
    `code_identity.sync_run`; the binary-overlay store `code/blobs` IS copied)
    are skipped, as is any file over `max_file_bytes` (named in the manifest's `skipped`);
  * what the log CANNOT rebuild — an external agent's unsubmitted worktree: its `agent/<name>`
    branch, its metadata and its uncommitted bytes — rides as `MirrorConfig.run_extras`, files
    derived on the source side into the mirror's spool and mirrored under `code/agent_backup/`
    (`engine/code_worktree.py::agent_backup`; `looplab code-status --sync` re-creates them);
  * a file that disappeared from the source drops its manifest row and DIRTIES the manifest, so a
    deletion-only pass rewrites it and a restore never resurrects the file.

THROTTLED. A token bucket bounds bytes per second and destination file operations per second; the
big span logs are copied only every `slow_every` passes. Nothing ever LISTS or `stat`s the
destination during a pass (the state file is the memory of what was copied), and each destination
directory is created once per process. That is the metadata-storm rule for an S3 mount.

NEVER BLOCKS THE ENGINE. The mirror is its own process (`looplab run-mirror`); it takes no lock the
engine takes and only reads the source. It takes ONE lock of its own (doc 82 П6):
`src_root/.looplab-mirror/mirror.lock`, held non-blocking for a whole loop (or one pass), so two
mirrors of one root — an operator's `run-mirror --loop` and the one `looplab ui` supervises under
`LOOPLAB_RUN_MIRROR_DST` — never copy at once: the second refuses with `MirrorBusy`. Every destination operation runs in a worker thread under a
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
import itertools
import json
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

STATE_DIR = ".looplab-mirror"
STATE_FILE = "state.json"
LOCK_FILE = "mirror.lock"
MANIFEST = ".looplab-mirror.json"
TMP_SUFFIX = ".mirror-tmp"
_STATE_VERSION = 1

SKIP_SUFFIXES = (".lock", TMP_SUFFIX, ".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal",
                 ".pyc", ".tmp")
SKIP_NAMES = frozenset({"engine.lock"})
# Directories of a run that are rebuildable or scratch: the private code repository and its agent
# worktrees are rebuilt from the log (`engine/code_identity.py::sync_run`), the canary tree is
# disposable by design. `code/blobs/` (binary overlay files) is NOT rebuildable and IS mirrored.
# An agent worktree's UNSUBMITTED work is not in the log either: it is carried by the
# `code/agent_backup/` bundle a `run_extras` producer derives (the CLI wires one), never by copying
# these two trees file by file (a live git directory copied mid-write is not a repository).
SKIP_TOP_DIRS = frozenset({"canary", STATE_DIR})
SKIP_SUBTREES = frozenset({"code/repo.git", "code/worktrees"})
SKIP_ANY_DIRS = frozenset({"__pycache__", ".git"})
# The large append-only trace logs: worth keeping, not worth re-uploading every pass.
SLOW_FILES = frozenset({"spans.jsonl", ".spans-append.jsonl", "trace.json"})
# Copied first: without these a mirrored run cannot be resumed at all.
_FIRST = ("events.jsonl", "task.snapshot.json", "config.snapshot.json")
_ERRORS_KEPT = 20
# One temp name per copy ATTEMPT (`_attempt_tmp`): a writer abandoned on its deadline still holds its
# descriptor, and with one shared `<name>.mirror-tmp` the next pass reopened that same inode, renamed
# it over the copy, and the late writer then wrote stale bytes into the PUBLISHED file (review g8).
_TMP_SEQ = itertools.count()


class MirrorStuck(RuntimeError):
    """A destination operation did not return within its deadline."""


class MirrorBusy(RuntimeError):
    """Another mirror holds this source root's mirror lock."""


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
    # `run_extras(run_dir, spool_dir) -> {rel: path}`: files a run's mirror carries that are NOT
    # files of the run directory — derived on the source side into `spool_dir` (the mirror's own
    # territory under the state directory, so the mirror still writes nothing under a run) and
    # copied to `dst_root/<run>/<rel>` like any other file, manifest row included. `events/` cannot
    # import the engine, so the CLI injects the engine's producer: the agent-worktree backup
    # (`engine/code_worktree.py::agent_backup`, weekly-worktree-restore-loss, closed 2026-10-16).
    # None (a bare `MirrorConfig`) mirrors the run directory alone.
    run_extras: Optional[Callable[[Path, Path], dict]] = None

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
    if len(parts) > 2 and "/".join(parts[:2]) in SKIP_SUBTREES:
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


def _plan_file(rel: str, path: Path, cfg: MirrorConfig, copied: dict, pass_no: int,
               todo: list, skipped: dict) -> None:
    """Plan ONE candidate file: append `[rel, stat_result]` to `todo` when it changed since `copied`
    recorded it, or name it in `skipped` when it is too large. Shared by the run walk and the
    `MirrorConfig.run_extras` files, so both obey the same size cap and change tier."""
    try:
        st = path.lstat()
    except OSError:
        return
    if not os.path.isfile(path) or path.is_symlink():
        return
    if st.st_size > cfg.max_file_bytes:
        skipped[rel] = f"larger than {cfg.max_file_bytes} bytes ({st.st_size})"
        return
    if rel.rsplit("/", 1)[-1] in SLOW_FILES and cfg.slow_every > 1 and pass_no % cfg.slow_every:
        if rel in copied:
            return
    prior = copied.get(rel)
    # (size, mtime) is deliberately the WEAK change tier: the mirror re-copies what moved,
    # and a same-size same-mtime rewrite is caught by the next manifest SHA mismatch only
    # at restore. `st` itself rides along; the copy reads its two fields. The SOURCE's size
    # is compared: a `*.jsonl` cut at its last newline records the cut as `size` and the
    # source's own as `source_size`, and comparing the cut re-copied a torn-tailed log on
    # every pass forever (review g8).
    prior_size = prior.get("source_size", prior.get("size")) if prior else None
    if prior and prior_size == st.st_size and prior.get("mtime_ns") == st.st_mtime_ns:
        return
    todo.append([rel, st])


def plan_run(run_dir: Path, cfg: MirrorConfig, copied: dict, pass_no: int,
             extras: Optional[dict] = None) -> tuple[list, dict]:
    """`(todo, skipped)`: files of `run_dir` that changed since `copied` recorded them, in priority
    order, as `[rel, stat_result]`; and `{rel: reason}` for what is never mirrored. `extras`
    (`{rel: path}`, from `MirrorConfig.run_extras`) are planned beside the walk and WIN over a run
    file of the same name: the copy is read from `extras[rel]`, never from `run_dir / rel`."""
    extras = extras or {}
    todo, skipped = [], {}
    for dirpath, dirnames, filenames in os.walk(run_dir, followlinks=False):
        base = Path(dirpath)
        rel_dir = base.relative_to(run_dir).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_ANY_DIRS
                             and not (rel_dir == "" and d in SKIP_TOP_DIRS)
                             and (rel_dir + d) not in SKIP_SUBTREES
                             and not (base / d).is_symlink())
        for name in sorted(filenames):
            rel = rel_dir + name
            if rel in extras or _skipped(rel, cfg) is not None:
                continue
            _plan_file(rel, base / name, cfg, copied, pass_no, todo, skipped)
    for rel in sorted(extras):
        _plan_file(rel, extras[rel], cfg, copied, pass_no, todo, skipped)
    todo.sort(key=lambda row: _priority(row[0]))
    return todo, skipped


def _valid_extras(extras, cfg: MirrorConfig) -> dict:
    """The `{rel: Path}` a `run_extras` hook returned, kept only where `rel` is a plain run-relative
    path the mirror would not skip anyway (a hook may not smuggle a lock file, a `..` or an absolute
    path into a manifest that `restore_run` writes back under the run)."""
    out = {}
    for rel, path in (extras or {}).items():
        if (not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel
                or any(p in ("", ".", "..") for p in rel.split("/"))
                or _skipped(rel, cfg) is not None):
            continue
        out[rel] = Path(path)
    return out


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


def _attempt_tmp(dst: Path) -> Path:
    """`dst`'s temp sibling for ONE copy attempt (`_TMP_SEQ`). Still ends in `TMP_SUFFIX`, so a
    destination mirrored back as a source skips it. A temp file an abandoned attempt never got to
    remove stays behind unreferenced: no manifest lists it, so no restore reads it, and removing it
    would mean LISTING the destination, which a pass never does (the metadata-storm rule)."""
    return dst.with_name(f"{dst.name}.{os.getpid()}.{next(_TMP_SEQ)}{TMP_SUFFIX}")


def copy_one(src: Path, dst: Path, *, size: int, mtime_ns: int, cfg: MirrorConfig,
             bytes_bucket: _Bucket) -> dict:
    """Copy one file and return its manifest row `{size, mtime_ns, sha256}`.

    The SOURCE is read in this thread (tmpfs, cannot hang); every DESTINATION operation goes through
    `_deadline_call`. A `*.jsonl` is cut at its last newline. Every attempt writes its OWN temp
    file and renames it over `dst`, so the copy a previous pass published stays whole until the new
    one is complete. An attempt abandoned on its deadline is flagged and, if its worker ever wakes,
    removes its temp file instead of publishing stale bytes over a later pass's copy (the flag is
    read just before the rename, so only a worker waking inside that one check can still race)."""
    cut = _jsonl_cut(src, size) if src.name.endswith(".jsonl") else size
    digest = hashlib.sha256()
    timeout = cfg.op_timeout_s + cut / max(1.0, cfg.max_bytes_per_s or 1.0) * 4
    tmp = _attempt_tmp(dst)
    abandoned = threading.Event()

    def publish():
        if abandoned.is_set():
            raise MirrorStuck(f"writing {dst} was abandoned on its deadline")
        os.replace(tmp, dst)
        os.utime(dst, ns=(mtime_ns, mtime_ns))

    def removing_tmp_on_failure(fn):
        def run():
            try:
                fn()
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass                   # never created, or the destination is failing anyway
                raise
        return run

    streamed = cut > cfg.atomic_max_bytes
    if not streamed:
        chunks = []
        for data in _read_source(src, cut, bytes_bucket):
            digest.update(data)
            chunks.append(data)
        payload = b"".join(chunks)

        def write():
            with open(tmp, "wb") as fh:
                fh.write(payload)
            publish()
        work, cut = write, len(payload)
    else:
        # Too big to hold in memory: STREAMED into the temp file, then renamed like the small path.
        # Until 2026-10-15 this opened `dst` itself with "wb", truncating the only durable copy for
        # the whole throttled transfer (minutes at the default 16 MiB/s). Nothing avoided the rename
        # on purpose — the split was about memory; on an S3 mount the rename is a server-side copy
        # of one changed file under `max_file_bytes` (review g8).
        written = [0]

        def write_stream():
            with open(tmp, "wb") as fh:
                for data in _read_source(src, cut, bytes_bucket):
                    if abandoned.is_set():
                        raise MirrorStuck(f"writing {dst} was abandoned on its deadline")
                    digest.update(data)
                    fh.write(data)
                    written[0] += len(data)
            publish()
        work = write_stream
    try:
        _deadline_call(removing_tmp_on_failure(work), timeout, f"writing {dst}")
    except MirrorStuck:
        abandoned.set()
        raise
    if streamed:
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


def _manifest_rows(cfg: MirrorConfig, run: str) -> tuple[Optional[dict], list]:
    """The destination manifest's `files` map for `run` (None if absent or unreadable) and the rels
    it marks as `extras`, read once under a deadline. Seeds a missing local state so a restart
    does not re-upload everything — and still knows which rows a `run_extras` producer owns."""
    path = cfg.dst_root / run / MANIFEST
    try:
        data = _deadline_call(lambda: _read_json(path, {}), cfg.op_timeout_s, f"reading {path}")
    except MirrorStuck:
        raise
    except OSError:
        return None, []
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return None, []
    extras = data.get("extras")
    return files, [r for r in extras if isinstance(r, str) and r in files] if isinstance(
        extras, list) else []


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
        seeded, seeded_extras = _manifest_rows(cfg, run)
        copied = seeded or {}
        rstate["files"] = copied
        rstate["extras"] = seeded_extras
    errors = []
    extras, extras_failed = {}, False
    if cfg.run_extras is not None:
        # A failed producer (git failing or timing out) costs only that backup's freshness this
        # pass: its last copy stays listed (`present` below), the run's own files are still
        # mirrored, and the error is reported with the pass. Not a blind handler: the engine's git
        # failures are `CodeRepoError` (a RuntimeError), I/O is OSError; a producer BUG stays loud.
        try:
            extras = _valid_extras(cfg.run_extras(run_dir, cfg.state_path.parent / "spool" / run),
                                   cfg)
        except (OSError, RuntimeError, ValueError) as exc:
            extras_failed = True
            errors.append({"run": run, "path": "<run_extras>",
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
    prior_extras = set(rstate.get("extras") or ())
    todo, skipped = plan_run(run_dir, cfg, copied, pass_no, extras)
    dst_run = cfg.dst_root / run
    n = nbytes = 0
    for rel, st in todo:
        size, mtime_ns = st.st_size, st.st_mtime_ns
        dst = dst_run / rel
        parent = dst.parent
        if str(parent) not in made_dirs:
            ops_bucket.take(1)
            _deadline_call(lambda p=parent: p.mkdir(parents=True, exist_ok=True),
                           cfg.op_timeout_s, f"mkdir {parent}")
            made_dirs.add(str(parent))
        ops_bucket.take(1)
        try:
            row = copy_one(extras.get(rel, run_dir / rel), dst, size=size, mtime_ns=mtime_ns,
                           cfg=cfg, bytes_bucket=bytes_bucket)
        except MirrorStuck:
            raise
        except FileNotFoundError:
            continue                       # removed between the walk and the copy
        except OSError as exc:
            errors.append({"run": run, "path": rel, "error": f"{type(exc).__name__}: {exc}"[:300]})
            continue
        copied[rel] = row
        # The state now vouches for bytes the destination MANIFEST does not list yet. Flagged in the
        # state itself, so a pass that dies (stuck, crashed) between this copy and the manifest
        # write still rewrites the manifest next time: the row makes the file "unchanged", and
        # without the flag no later pass rewrote the manifest at all — `restore_run` then refused
        # that file's old SHA forever (review g8).
        rstate["manifest_stale"] = True
        n += 1
        nbytes += row["size"]
    # Rows for files that no longer exist at the source are dropped from the manifest (their stale
    # copy stays in the destination, unreferenced: deleting on S3 is a cost this job does not pay).
    # An extra is present while its producer still returns it; when the producer FAILED this pass,
    # every row not under the run directory is kept as it is rather than read as deleted — not
    # only the extras the state remembers: a state file lost (tmpfs) and seeded from a manifest an
    # older build wrote without `extras` knows none, and popping them dropped the whole agent
    # backup from the manifest a restore trusts (review 2026-10-16). A run file deleted during
    # such a pass leaves the manifest one pass later.
    def present(rel: str) -> bool:
        if rel in extras:
            return extras[rel].is_file()
        if extras_failed:
            return True
        return (run_dir / rel).is_file()
    retired_extras = []
    for rel in list(copied):
        if not present(rel):
            copied.pop(rel, None)
            if rel in prior_extras and not extras_failed:
                retired_extras.append(rel)
            # A REMOVAL dirties the manifest exactly like a copy does. Until 2026-10-16 a pass whose
            # only change was a deletion popped the row here and then skipped the manifest write
            # (nothing copied, nothing newly skipped), so the destination manifest kept listing the
            # deleted file and `restore_run` brought it back — a deleted `gpu_gate.json` with
            # `hold=true` returned on restore (review 2026-10-15, weekly-mirror-deletion). Flagged
            # in the STATE, like a copy: a pass that dies before the write still rewrites the
            # manifest next time, and a restarted mirror that re-seeds `copied` from the stale
            # manifest pops the same row again and so dirties it again.
            rstate["manifest_stale"] = True
    owned = sorted(prior_extras if extras_failed else extras)
    if owned != sorted(prior_extras):
        rstate["manifest_stale"] = True    # the manifest names its extras: a new set rewrites it
    rstate["extras"] = owned
    if (n or rstate.get("manifest_stale") or not rstate.get("manifest_written")
            or skipped != rstate.get("skipped", {})):
        # `extras` names the rows a `run_extras` producer owns, so a mirror whose state file was
        # lost still retires an old one (and never reads it as a deleted run file).
        manifest = {"version": 1, "run": run, "source": str(run_dir), "updated_at": time.time(),
                    "files": copied, "skipped": skipped,
                    "extras": [rel for rel in owned if rel in copied]}
        ops_bucket.take(1)
        # `_write_json_atomic` creates the run's destination directory itself (an unchanged run
        # seeded from a state file may never have made it in this process).
        _deadline_call(lambda: _write_json_atomic(dst_run / MANIFEST, manifest),
                       cfg.op_timeout_s, f"writing {dst_run / MANIFEST}")
        rstate["changed"] = True
        rstate["manifest_written"] = True
        rstate.pop("manifest_stale", None)
        rstate["skipped"] = skipped
        # The one exception to "stale copies stay": an EXTRA the producer stopped returning. It is
        # the mirror's own derived file, not the run's — the agent backup's plain files carry
        # their version in their name, so a checkpoint rewritten every step would otherwise leave
        # every version behind in the destination. Deleted only AFTER the manifest that no longer
        # lists it is written (a restore never reads a manifest naming a missing file); a delete
        # that fails leaves one more unreferenced copy, which is what every other removal leaves.
        for rel in retired_extras:
            ops_bucket.take(1)
            try:
                _deadline_call(lambda rel=rel: (dst_run / rel).unlink(missing_ok=True),
                               cfg.op_timeout_s, f"removing {dst_run / rel}")
            except OSError as exc:
                errors.append({"run": run, "path": rel, "error": f"{type(exc).__name__}: {exc}"})
    newest = max((row.get("mtime_ns", 0) for row in copied.values()), default=0)
    rstate.update({"last_mirrored_at": time.time(), "newest_copied_mtime_ns": newest,
                   "file_count": len(copied)})
    return {"copied": n, "bytes": nbytes, "pending_before": len(todo), "skipped": len(skipped),
            "errors": errors}


def mirror_lock_path(cfg: MirrorConfig) -> Path:
    return cfg.src_root / STATE_DIR / LOCK_FILE


@contextmanager
def mirror_lock(cfg: MirrorConfig):
    """Hold the source root's mirror lock, or raise `MirrorBusy` at once (never waits)."""
    from looplab.events.eventstore import InterprocessLockContended, interprocess_lock

    path = mirror_lock_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with interprocess_lock(path, required=True, blocking=False):
            yield path
    except InterprocessLockContended as exc:
        raise MirrorBusy(f"another mirror of {cfg.src_root} is running (lock {path})") from exc


def mirror_pass(cfg: MirrorConfig, *, state: Optional[dict] = None,
                clock=time.monotonic, sleep=time.sleep, locked: bool = False) -> PassReport:
    """One pass over every run under `cfg.src_root`. Saves the state after each run.

    `locked`: the caller already holds `mirror_lock` (a loop holds it for its lifetime); otherwise
    this pass takes it, and raises `MirrorBusy` while another mirror holds it."""
    if not locked:
        with mirror_lock(cfg):
            return mirror_pass(cfg, state=state, clock=clock, sleep=sleep, locked=True)
    state = load_state(cfg) if state is None else state
    pass_no = int(state.get("pass", 0))
    report = PassReport()
    t0 = clock()
    bytes_bucket = _Bucket(cfg.max_bytes_per_s, clock=clock, sleep=sleep)
    ops_bucket = _Bucket(cfg.max_files_per_s, clock=clock, sleep=sleep)
    made_dirs: set = set()
    # A pre-upgrade state may still carry the latest timeout; retain it before a clean pass clears it.
    if not state.get("last_failure") and isinstance(state.get("stuck"), dict):
        old = state["stuck"]
        state["last_failure"] = {"kind": "stuck", "at": old.get("at"),
                                 "run": str(old.get("run") or "")[:256],
                                 "detail": str(old.get("detail") or "")[:1024]}
    state["last_pass_started_at"] = time.time()
    completed = False
    unexpected_failure = None
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
            completed = True
    except Exception as exc:
        # Preserve the original exception and propagation; the source-local state is best effort.
        unexpected_failure = {"kind": "exception", "at": time.time(),
                              "detail": f"{type(exc).__name__}: {exc}"[:1024]}
        raise
    finally:
        report.seconds = clock() - t0
        state["pass"] = pass_no + 1
        state["last_pass_finished_at"] = time.time()
        if unexpected_failure:
            state["last_failure"] = unexpected_failure
        elif report.stuck:
            state["last_failure"] = {"kind": "stuck", "at": state["stuck"]["at"],
                                     "run": str(state["stuck"]["run"])[:256],
                                     "detail": str(report.stuck)[:1024]}
        elif report.errors:
            error = report.errors[-1]
            state["last_failure"] = {"kind": "error", "at": time.time(),
                                     "run": str(error.get("run") or "")[:256],
                                     "path": str(error.get("path") or "")[:1024],
                                     "detail": str(error.get("error") or "")[:1024]}
        if completed and not report.errors:
            state["last_successful_pass_finished_at"] = state["last_pass_finished_at"]
        state["last_pass"] = {"completed": completed, "error_count": len(report.errors),"seconds": round(report.seconds, 3), "stuck": report.stuck,
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
    `exit_on_stuck` (so a supervisor restarts a process that may hold a thread stuck in FUSE).
    Holds the mirror lock for the whole loop; raises `MirrorBusy` when another mirror holds it."""
    with mirror_lock(cfg):
        return _mirror_loop_locked(cfg, passes=passes, exit_on_stuck=exit_on_stuck, log=log,
                                   sleep=sleep, stop=stop)


def _mirror_loop_locked(cfg, *, passes, exit_on_stuck, log, sleep, stop) -> int:
    done = 0
    while True:
        report = mirror_pass(cfg, locked=True)
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
            "last_successful_pass_finished_at": state.get("last_successful_pass_finished_at"),
            "stuck": state.get("stuck"), "last_failure": state.get("last_failure"),
            "errors": state.get("errors", [])[-10:], "runs": runs}


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
            "next": f"looplab resume {target}",
            # Unsubmitted agent worktrees come back only at this explicit restore point.
            **({"agent_worktrees": f"looplab code-status --sync {target}"}
               if any(rel.startswith("code/agent_backup/") for rel in files) else {})}


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
