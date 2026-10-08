"""The operator's COPY-OUT of a finished node's workdir (`eval.artifact_sync`).

WHY (incident 2026-10-06). A 1.5B training ran ten hours on an 8-GPU box; its checkpoint lived on a
network mount that went away, and the only way forward was to train again from zero. LoopLab kept
every byte of the run's RECORD, and none of the artifacts the record is about: `export-bundle`
deliberately leaves node workdirs out ("data mounts and checkpoints are the box's"), and nothing else
ever copied one anywhere.

WHAT. When the task declares `eval.artifact_sync: {command: [...], timeout: …}`, the engine runs that
argv after a node's terminal — the operator's own `mc cp` / `aws s3 cp` / `rsync`, with the host
environment `run_setup` gets, so the tool finds its own credentials — over the node's workdir. Each
argument may name `{workdir}`, `{run_dir}`, `{run_id}`, `{node_id}` and `{generation}`; nothing else in
it is interpreted (no shell, no `str.format`: a literal brace in an argument stays a brace).

THREE PROPERTIES, each the reason for a choice:

* IN A BOUNDED BACKGROUND POOL, after the terminal. A copy of gigabytes is minutes of wall clock, and
  doing it inside `_evaluate` would hold the eval slot — the GPU lease — for an upload. At most
  `MAX_CONCURRENT_SYNCS` copies run at once (round 3: every finished node used to start its own
  thread, so a wide run fanned out as many concurrent uploads as it had evals finishing, all on one
  uplink); the rest wait in this process's queue, in terminal order. The workers are NON-daemon, so
  the interpreter waits for the queued and in-flight copies at exit (each bounded by its `timeout`)
  instead of killing the copy of the run's last node; a worker exits as soon as the queue is empty.
  A QUEUED COPY IS CHECKED AGAIN WHEN IT STARTS (round 3, critic c3 item 1): the workdir stamp is
  captured when the copy is ACCEPTED, so a node reset while its copy waited in the queue is refused
  (`skipped: "workdir_changed"`) instead of uploading the next lifecycle under this one's
  `{generation}`; and a QUEUED copy whose engine's lifetime has ended (`engine_owns_run`) is not
  started at all — another engine may own the run directory by then. Its start row stays open, which
  `unfinished_syncs` reports (`looplab inspect`, the attention feed); a copy already running still
  closes its own row.
* A RECEIPT, NEVER A VERDICT, OPENED BEFORE IT IS CLOSED. The `artifact_sync_started` row is written
  when the copy is accepted (round 3), the `artifact_synced` row when it ends, both keyed by
  `sync_id`; a started row with no synced row is a copy an engine death interrupted, queued or
  running (`unfinished_syncs`). Both rows are DIAGNOSTIC (invariant #1: appendable from a thread,
  fold-ignored, fence-neutral). A failed copy is reported in it and in `artifact_sync.log`; it never
  fails, pauses or re-runs the node, whose metric is already on its terminal — and an interrupted
  copy is NOT retried on resume: the operator's command is not known to be idempotent, so the
  started row is what tells them to run it again.
* WHAT THE OPERATOR DECLARED, ONLY. The engine never picks a destination or a tool; an absent
  declaration runs nothing.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote, quote_plus

from looplab.core.node_evidence import node_workdir
# `unfinished_syncs` — the started-and-never-closed pairing — lives beside the fold so the CLI and
# the attention feed read it without the engine; re-exported here as the SAME function.
from looplab.events.replay import unfinished_syncs  # noqa: F401 — re-exported
from looplab.events.types import EV_ARTIFACT_SYNC_STARTED, EV_ARTIFACT_SYNCED

_LOG = logging.getLogger(__name__)

PLACEHOLDERS: tuple[str, ...] = ("workdir", "run_dir", "run_id", "node_id", "generation")
_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDERS) + r")\}")
_STDERR_TAIL = 400

# How many operator copy-outs run at once in one process. Two, not one: a slow upload of one
# checkpoint must not hold every later node's copy behind it, and not more, because the copies share
# the box's uplink and the operator's object store — N concurrent multi-gigabyte uploads finish no
# sooner than two at a time and each one is N times as likely to hit its own `timeout`. A module
# constant rather than a `Settings` field, like `eval_dispatch.py::_MAX_NODE_DEP_SYNCS`: a runaway
# bound, not a knob; the knob is whether `eval.artifact_sync` is declared at all.
MAX_CONCURRENT_SYNCS = 2

# The copies accepted in THIS process and not yet finished (queued + running), the queue the
# workers drain, and the live worker count — one condition guards all three, so `wait_for_inflight`
# can wait for "nothing pending" without polling.
_QUEUE: collections.deque = collections.deque()
_PENDING = 0
_WORKERS = 0
_COND = threading.Condition()
# …and the same pending count PER ACCEPTING ENGINE (`id(engine)`; the queued job holds the engine, so
# the id cannot be reused while its count is positive), so an engine ending waits for ITS copies and
# never for another engine's in the same process (`drain_before_release`).
_PENDING_BY_OWNER: dict[int, int] = {}

# How long an ending engine waits for the copy-outs it accepted before it gives up its run
# (`drain_before_release`). A queued copy found after the end is never started (`engine_owns_run`),
# so without the wait the copies of a run's LAST nodes — queued behind the bound when the search
# finished — were exactly the ones skipped. Bounded, and by the default copy `timeout`: one slow
# upload must not hold a finished run's lock forever; what is left open is reported unfinished.
FINAL_DRAIN_S = 1800.0


def render_argv(argv, values: dict) -> list[str]:
    """`argv` with every KNOWN placeholder replaced; anything else is left exactly as written."""
    return [_PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), str(arg)) for arg in argv]


def sync_spec(eval_spec) -> Optional[dict]:
    """The declared `eval.artifact_sync`, or None when there is none to run."""
    spec = (eval_spec or {}).get("artifact_sync") if isinstance(eval_spec, dict) else None
    if not isinstance(spec, dict):
        return None
    command = spec.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(a, str) for a in command):
        return None
    return spec


def passthrough_env(spec) -> dict:
    """`{NAME: value}` for every name the declaration's `env_passthrough` lists that the ENGINE's
    environment holds (`adapters/repo_task.py::ArtifactSyncSpec.env_passthrough`). Shared with
    `maintenance/evaluate_track.py`; a value never leaves this dict for a record."""
    names = spec.get("env_passthrough") if isinstance(spec, dict) else None
    if not isinstance(names, list):
        return {}
    return {n: os.environ[n] for n in names if isinstance(n, str) and n in os.environ}


def sync_cwd(workdir, run_dir, spec) -> str:
    """Where an operator copy-out or track runs: the node's WORKDIR, as documented (a relative
    `./` or `score.py` names the node's files) — except when its declaration hands it credentials
    (`env_passthrough`), which run from the RUN directory (critic 2026-10-08): the workdir is the
    candidate's, and a `python -m <tool>` started there imports whatever module the candidate left
    beside the key. Two rounds of review: always-run-dir broke every workdir-relative command.

    Decided by the DECLARATION, never by which of the declared names this host's environment holds
    (round 3): keyed on `passthrough_env(spec)`, the same task ran from the run dir on a box that
    had the key and from the workdir on one that did not, and a relative argv failed on one of them
    with a misleading "file not found". `spec` is the declaration (`eval.artifact_sync` or a track)."""
    names = spec.get("env_passthrough") if isinstance(spec, dict) else None
    declared = isinstance(names, list) and any(isinstance(n, str) and n for n in names)
    return str(run_dir if declared else workdir)


def workdir_stamp(workdir: Path) -> Optional[bytes]:
    """The bytes of a node workdir's `.looplab-manifest` stamp, or None. Through THE reader of a file
    a candidate can write (`core/node_evidence.py::read_bounded_regular_file`): the stamp is written
    before the candidate's eval runs in that same directory, which may replace it with a FIFO or a
    multi-gigabyte file. Shared with `maintenance/evaluate_track.py`."""
    from looplab.core.node_evidence import read_bounded_regular_file
    return read_bounded_regular_file(Path(workdir) / ".looplab-manifest", 256)


def engine_owns_run(engine) -> bool:
    """Whether `engine` is still inside its run's lifetime — the window in which it holds
    `engine.lock` and may append to the event log (round 3, critic c3 item 1).

    The signal is the engine's own trace exporter: `Engine.retire_tracer` is THE terminal barrier
    that "must end before the lifecycle lock may be released" (`engine/orchestrator.py::Engine.run`),
    so a shut-down exporter means the owner is done and its lock is released or about to be. A
    queued copy found here after that is NOT started: a resumed engine or a Replay may own the run
    directory by then, and would be resetting the very workdir the copy reads. Conservative by
    design — the CLI may still hold the lock for its finish report — because the cost of the miss is
    one open `artifact_sync_started` row the operator re-runs, and the cost of the other error is an
    upload of bytes another owner is rewriting. A host with no tracer (a hand-built test engine) owns
    its run for as long as it exists."""
    exporter = getattr(getattr(engine, "tracer", None), "exporter", None)
    metrics = getattr(exporter, "metrics", None)
    if not callable(metrics):
        return True
    try:
        return not bool(metrics().get("shutdown", False))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return True


def start_artifact_sync(engine, node_id: int, generation: int) -> Optional[str]:
    """Queue the declared copy-out of node `node_id`'s workdir on the bounded pool and return its
    `sync_id`, or None (nothing declared, or no workdir to copy). Never raises."""
    spec = sync_spec(getattr(engine, "_eval_spec", None))
    if spec is None:
        return None
    run_dir = Path(engine.run_dir)
    # A REAL directory at both components, never a link (review 2026-10-08): `is_dir()` followed a
    # linked `nodes/node_N`, and the operator's command — run with the host's credentials — would
    # have uploaded whatever the link names. `node_workdir` is the refusal the log readers and
    # `_parent_workdirs_env` already apply; a missing workdir is None too. Never raises.
    try:
        workdir = node_workdir(run_dir, node_id)
    except (OSError, TypeError, ValueError):
        return None
    if workdir is None:
        return None
    values = {"workdir": str(workdir), "run_dir": str(run_dir), "run_id": run_dir.name,
              "node_id": node_id, "generation": generation}
    argv = render_argv(spec["command"], values)
    try:
        timeout = float(spec.get("timeout") or 1800.0)
    except (TypeError, ValueError):
        timeout = 1800.0
    sync_id = uuid.uuid4().hex[:16]
    # THE STAMP OF THE LIFECYCLE THIS COPY IS FOR, taken NOW, at acceptance (critic c3 item 1): with
    # the bounded pool the copy may wait in the queue for minutes, and a stamp read when it STARTS
    # would vouch for whatever lifecycle a reset re-materialized meanwhile.
    accepted_stamp = workdir_stamp(workdir)
    # THE START ROW FIRST, then the queue: a copy the log does not say was accepted must never run,
    # and one it does say was accepted is visible as unfinished from this instant — queued behind
    # the bound or running — until its `artifact_synced` row lands.
    try:
        engine.store.append(EV_ARTIFACT_SYNC_STARTED, {
            "node_id": node_id, "generation": generation, "sync_id": sync_id})
    except OSError:
        _LOG.warning("artifact sync of node %s: could not record its start; not running it",
                     node_id, exc_info=True)
        return None
    _submit((engine, node_id, generation, argv, workdir, run_dir, timeout,
             passthrough_env(spec), sync_id, sync_cwd(workdir, run_dir, spec), accepted_stamp))
    return sync_id


def _submit(job) -> None:
    """Queue `job` and start a worker when fewer than `MAX_CONCURRENT_SYNCS` are alive."""
    global _PENDING, _WORKERS
    with _COND:
        _QUEUE.append(job)
        _PENDING += 1
        _PENDING_BY_OWNER[id(job[0])] = _PENDING_BY_OWNER.get(id(job[0]), 0) + 1
        if _WORKERS >= MAX_CONCURRENT_SYNCS:
            return
        _WORKERS += 1
    _spawn()


def _spawn() -> None:
    """Start one worker, whose slot the caller already counted in `_WORKERS`.

    NO THREAD TO BE HAD (interpreter shutting down, a thread limit) gives the slot back and LEAVES
    THE QUEUE AS IT IS (critic c3 item 7): this used to drain the whole queue inline, and the caller
    is `_evaluate` on the event loop — minutes of uploads with every session turn, watcher and
    heartbeat frozen behind them. A copy left queued keeps its open start row; the next `_submit`
    (or a finishing worker) starts a worker for it, and a process that ends first leaves it reported
    as unfinished, which is what the start row is for."""
    global _WORKERS
    try:
        threading.Thread(target=_worker, name="looplab-artifact-sync", daemon=False).start()
    except RuntimeError:
        with _COND:
            _WORKERS -= 1
            _COND.notify_all()
        _LOG.warning("artifact sync: no worker thread could be started; %d copy-out(s) stay queued "
                     "(their artifact_sync_started rows stay open)", len(_QUEUE))


def _worker() -> None:
    """Drain the queue, then exit — an idle worker holds nothing, so exit never waits on one.

    The worker gives its slot back IN THE SAME critical section that finds the queue empty, so a
    `_submit` racing that moment either sees the job taken or sees a free slot — never a full pool
    of workers that are all on their way out. A copy that raises past `_run`'s own handlers ends
    this worker; its `finally` hands any queued copy to a replacement rather than stranding it."""
    global _PENDING, _WORKERS
    clean = False
    try:
        while True:
            with _COND:
                if not _QUEUE:
                    _WORKERS -= 1
                    clean = True
                    _COND.notify_all()
                    return
                job = _QUEUE.popleft()
            try:
                _run(*job[:8], sync_id=job[8], cwd=job[9],
                     accepted_stamp=job[10] if len(job) > 10 else None)
            finally:
                with _COND:
                    _PENDING -= 1
                    owner = id(job[0])
                    left = _PENDING_BY_OWNER.get(owner, 1) - 1
                    if left > 0:
                        _PENDING_BY_OWNER[owner] = left
                    else:
                        _PENDING_BY_OWNER.pop(owner, None)
                    _COND.notify_all()
    finally:
        if not clean:
            with _COND:
                _WORKERS -= 1
                restart = bool(_QUEUE) and _WORKERS < MAX_CONCURRENT_SYNCS
                if restart:
                    _WORKERS += 1
            if restart:
                _spawn()


# One writer at a time on a shared tool log: two pool workers finish together and append to the same
# `artifact_sync.log`.
_LOG_WRITE_LOCK = threading.Lock()
_MASK = "***REDACTED_ENV***"
# The shortest piece of a passthrough value masked where a CUT left only part of it (critic c3 item
# 5): `run_argv` keeps the last 64 KB of each stream, so a secret printed across that boundary
# survives as its suffix at the stream's head (a timeout kill can leave a prefix at its tail).
# Six characters, not four: a fragment is matched only AT the cut, but a shorter one is common
# enough in ordinary output that the log would lose words that were never the secret.
_MIN_CUT_FRAGMENT = 6


def _spellings(value: str) -> set:
    """`value` as a tool may print it: verbatim, and the percent-encoded forms a URL or a query
    string carries (`quote` with and without `/` kept, `quote_plus`) — critic c3 item 5: an
    `MC_HOST_*` key echoed inside a URL is the encoded spelling, which an identity match missed."""
    return {value, quote(value, safe=""), quote(value), quote_plus(value)}


def _mask_cut_edges(text: str, spellings) -> str:
    """Mask a fragment of any spelling that a cut left at the HEAD (its suffix) or the TAIL (its
    prefix) of `text`: the longest one at least `_MIN_CUT_FRAGMENT` long, per edge."""
    head = max((k for s in spellings for k in range(_MIN_CUT_FRAGMENT, len(s))
                if text.startswith(s[-k:])), default=0)
    if head:
        text = _MASK + text[head:]
    tail = max((k for s in spellings for k in range(_MIN_CUT_FRAGMENT, len(s))
                if text.endswith(s[:k])), default=0)
    if tail and len(text) - tail >= (len(_MASK) if head else 0):
        text = text[:len(text) - tail] + _MASK
    return text


def mask_tool_text(text, env=None) -> str:
    """`text` as it may be WRITTEN anywhere: the passthrough VALUES this command was handed masked by
    identity — whatever their names, which `core/redact.py`'s env screen judges by shape and so would
    miss for `MC_HOST_minio` — in every spelling a tool prints them (`_spellings`), and any fragment
    of one a capture cut left at the text's edge (`_mask_cut_edges`); then the always-on screen
    (`redact_output_tail`: this box's own secret env values and every known credential shape; the
    entropy pass is not applied to a tool log, whose paths and hashes it would eat). A value under 4
    characters is not masked: it is no credential, and replacing it everywhere would garble the log.

    `text` must be ONE captured stream (or one argv): the cut sits at its edges, so a caller holding
    several masks each before joining them (`append_tool_log`)."""
    from looplab.core.redact import redact_output_tail
    text = str(text or "")
    values = {v for v in (env or {}).values() if isinstance(v, str) and len(v) >= 4}
    spellings = {sp for v in values for sp in _spellings(v)}
    for value in sorted(spellings, key=len, reverse=True):
        if value in text:
            text = text.replace(value, _MASK)
    if spellings:
        text = _mask_cut_edges(text, spellings)
    return redact_output_tail(text, entropy=False)


def append_tool_log(path, argv, out, err, env=None) -> None:
    """Append one finished run of an operator tool to its log, MASKED (`mask_tool_text`).

    WHY NOT `run_argv(log_path=…)` (round 3): that mirrors the child's raw bytes live, so a tool that
    echoes its environment or a URL carrying the key (`set -x`, an `MC_HOST_*` in an error) left the
    passthrough secret in a run-directory file every later eval may read. The output is captured
    (bounded, as `run_argv` returns it) and written once, after the process exits, before anything
    else can read it; the price is that the log is no longer tail-able while the copy runs."""
    # EACH STREAM MASKED ON ITS OWN, then joined (critic c3 item 5): the 64 KB capture cut sits at
    # the head of `out` and of `err`, which are the middle of the joined body, where an edge
    # fragment can no longer be told from ordinary text.
    body = (f"$ {mask_tool_text(' '.join(str(a) for a in argv), env)}\n"
            + (f"{mask_tool_text(out, env)}\n" if out else "")
            + (f"{mask_tool_text(err, env)}\n" if err else ""))
    with _LOG_WRITE_LOCK, open(path, "a", encoding="utf-8") as fh:
        fh.write(body)


def wait_for_inflight(timeout: Optional[float] = None, engine=None) -> bool:
    """Wait until every copy this process accepted — or, given `engine`, every copy THAT engine
    accepted — has finished; True when none is pending."""
    with _COND:
        if engine is None:
            return _COND.wait_for(lambda: _PENDING == 0, timeout)
        return _COND.wait_for(lambda: not _PENDING_BY_OWNER.get(id(engine)), timeout)


def drain_before_release(engine, timeout: Optional[float] = None) -> bool:
    """Called by an ending engine BEFORE its terminal barrier (`engine/orchestrator.py::Engine.
    retire_tracer`), while it still owns the run: wait, bounded, for the copy-outs it accepted, so a
    copy still queued behind `MAX_CONCURRENT_SYNCS` when the search ended starts under the owner that
    accepted it instead of being skipped by `engine_owns_run` the moment the barrier closes. True
    when nothing of it is pending; on the bound, what is left keeps its open start row
    (`unfinished_syncs`), and a copy already running still closes its own. No copy accepted, no
    wait."""
    with _COND:
        pending = _PENDING_BY_OWNER.get(id(engine), 0)
    if not pending:
        return True
    timeout = FINAL_DRAIN_S if timeout is None else timeout
    _LOG.info("waiting up to %.0fs for %d artifact copy-out(s) before the run is released",
              timeout, pending)
    done = wait_for_inflight(timeout, engine=engine)
    if not done:
        _LOG.warning("artifact sync: copy-outs still pending after %.0fs; the run is released and "
                     "a queued copy will not start (its artifact_sync_started row stays open)",
                     timeout)
    return done


def _run(engine, node_id, generation, argv, workdir, run_dir, timeout, env=None,
         sync_id=None, cwd=None, accepted_stamp=None) -> None:
    from looplab.runtime.sandbox import _run_argv
    _engine_redact = getattr(engine, "_redact", None) or (lambda text: text)

    def redact(text):
        # The passthrough values first (`mask_tool_text`), then the engine's own funnel.
        return _engine_redact(mask_tool_text(text, env))

    # A COPY WHOSE ENGINE IS GONE IS NOT STARTED (critic c3 item 1). The non-daemon workers outlive
    # `Engine.run`, and a copy still queued then would run — and append its receipt — after the lock
    # was released, when a resumed engine or a Replay may own the run directory. Its start row stays
    # open: `unfinished_syncs` reports it, and the operator re-runs the copy.
    if not engine_owns_run(engine):
        _LOG.warning("artifact sync of node %s (lifecycle %s) not started: the engine that accepted "
                     "it has ended; its artifact_sync_started row (sync_id %s) stays open — run the "
                     "copy again", node_id, generation, sync_id)
        return
    started = time.monotonic()
    before = workdir_stamp(workdir)
    # THE LIFECYCLE THE COPY WAS ACCEPTED FOR IS NO LONGER THERE (critic c3 item 1, driven): a reset
    # while the copy waited in the queue re-materialized the workdir for the NEXT lifecycle, and the
    # argv's `{generation}` and this receipt both name the old one. Never run it: the receipt says it
    # was refused, and why. (A stamp that was unreadable at acceptance cannot be compared, and the
    # copy runs as it always did.)
    refused = accepted_stamp is not None and before != accepted_stamp
    out, err, rc, timed = "", "", None, False
    if not refused:
        try:
            rc, out, err, timed = _run_argv(argv, cwd or str(workdir), timeout,
                                            env=dict(env or {}))
        except (OSError, ValueError) as exc:          # no such tool, an unusable cwd
            rc, err, timed = -1, f"{type(exc).__name__}: {exc}", False
        try:
            append_tool_log(run_dir / "artifact_sync.log", argv, out, err, env)
        except OSError:
            _LOG.warning("artifact sync of node %s: could not write artifact_sync.log", node_id,
                         exc_info=True)
    seconds = round(time.monotonic() - started, 3)
    # A reset of this node re-materializes the workdir while the copy reads it; the receipt says so
    # instead of vouching for a tree that may mix two lifecycles (critic 2026-10-08).
    changed = refused or (before is not None and workdir_stamp(workdir) != before)
    if refused:
        _LOG.warning("artifact sync of node %s (lifecycle %s) not run: the workdir was rebuilt "
                     "while the copy was queued", node_id, generation)
    elif rc != 0 or timed:
        _LOG.warning("artifact sync of node %s failed (exit %s%s); the node is unaffected — see "
                     "artifact_sync.log", node_id, rc, ", timed out" if timed else "")
    # A copy that was already RUNNING when its engine ended still closes its own start row: the
    # receipt is what says the bytes left the box, and leaving it open would send the operator to
    # upload a finished checkpoint again. The append is safe without `engine.lock` — the store
    # derives the seq from the file's tail under its own interprocess lock, and the Replay and
    # deletion write fences refuse a late writer (`events/eventstore.py::EventStore._locked_append`)
    # — which is why only STARTING a copy is gated on ownership above.
    try:
        engine.store.append(EV_ARTIFACT_SYNCED, {
            "node_id": node_id, "generation": generation,
            **({"sync_id": sync_id} if sync_id else {}),
            "command": [redact(a) for a in argv], "exit_code": rc, "timed_out": bool(timed),
            **({"workdir_changed": True} if changed else {}),
            **({"skipped": "workdir_changed"} if refused else {}),
            "seconds": seconds,
            # Redacted WHOLE, then cut (`engine/audit.py::Engine._redact`): a cut first can leave
            # the tail of a secret that straddled it unmasked.
            "stderr_tail": redact(str(err or ""))[-_STDERR_TAIL:]})
    except (OSError, RuntimeError):     # a full disk; a Replay / deletion fence refusing the writer
        _LOG.warning("artifact sync of node %s: could not record its receipt", node_id,
                     exc_info=True)
