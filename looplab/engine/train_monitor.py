"""Training-log monitor — a per-eval background observer of the LIVE training log (I-series watchdog
family, sibling of `runtime/sandbox._StageHealthMonitor`).

A repo eval's declared training stage runs for a long time (often multi-hour) while the engine's async
loop is otherwise idle — `_evaluate` runs the eval in a worker thread. This mixin adds a periodic task
in that same task group (alongside the mid-eval intervention `_watch`) that tails the stage's live log
and, per phase:

- Phase 0 (here): read the live-log tail on a timer and emit a `train_monitor` TRACE span. No LLM, no
  domain events, no intervention — pure observability, so `off == today` and even ON it is byte-identical
  in events.jsonl (spans are a sidecar, never folded).

Design constraints this file must keep (engine invariants):
- **The runtime never calls the LLM** (layering: `runtime` imports nothing above itself), so an LLM-driven
  watchdog CANNOT live in the sandbox — it lives here, in the engine, reading the log FILE the sandbox
  already writes (`_tee_drain(log_path=…)`).
- Later phases record a VERDICT: it will be a DIAGNOSTIC event (fold-ignored) so its thread-dependent
  splice position never changes folded state, and any early-KILL will reuse the existing `cancel` →
  tree-kill path so the node still emits exactly ONE terminal event (`node_failed`); replay reads that
  terminal and NEVER re-invokes the LLM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def training_log_digest(text: str, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Reduce a raw training-log tail to a compact digest that preserves the recent TRAJECTORY (the LLM
    context in Phase 1).

    Two kinds of repetition, handled differently:
    - A tqdm/epoch bar overwrites ONE line in place with carriage returns (no newline until it finishes),
      so within a newline-delimited record we keep only the LAST `\\r` segment — the bar's final rendered
      state — collapsing thousands of snapshots to one.
    - Distinct per-step log LINES ("step 1 loss: 0.5", "step 2 loss: 0.4", …) are separate newline
      records and are KEPT: their sequence IS the loss trajectory the monitor must reason over. We keep
      the last `max_lines` of them (the recent trend), then bound to `max_chars`.
    Pure and deterministic — no I/O — so it is unit-testable and safe to reuse anywhere."""
    if not text:
        return ""
    records: list[str] = []
    for rec in text.split("\n"):
        seg = rec.split("\r")[-1].rstrip()   # in-place re-renders: keep the final rendered segment only
        if seg.strip():
            records.append(seg)
    out = "\n".join(records[-max_lines:])
    return out[-max_chars:] if len(out) > max_chars else out


def active_training_log(workdir) -> Optional[Path]:
    """The workdir's most-recently-written `<stage>.log` — the live log of whichever stage is running
    NOW (the sandbox writes one `<stage>.log` per stage; during training the train stage's file is the
    freshest). None when there is no per-stage log (the solution.py path writes none)."""
    try:
        logs = list(Path(workdir).glob("*.log"))
    except OSError:
        return None
    if not logs:
        return None
    try:
        return max(logs, key=lambda f: f.stat().st_mtime)
    except OSError:
        return None


def read_training_tail(workdir, *, max_read_bytes: int = 131_072,
                       max_lines: int = 40, max_chars: int = 4000) -> str:
    """Read only the LAST `max_read_bytes` of the active stage log and digest it. Bounded read (seek to
    the tail) so a multi-GB training log never loads into memory; a torn leading line is dropped by the
    utf-8 'replace' decode + the digest keeping only whole trailing lines. '' when there is no log yet."""
    path = active_training_log(workdir)
    if path is None:
        return ""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_read_bytes:
                fh.seek(size - max_read_bytes)
            raw = fh.read()
    except OSError:
        return ""
    return training_log_digest(raw.decode("utf-8", "replace"),
                               max_lines=max_lines, max_chars=max_chars)


class TrainingMonitorMixin:
    """The engine's training-log monitor cluster. `self` IS the Engine (mixin convention — see
    orchestrator.py). Gated on `self._train_monitor`; started as a sibling task in `_evaluate`'s task
    group so it lives exactly as long as the eval and is cancelled with it."""

    async def _monitor_training(self, node_id: int, generation: int, workdir, cancel) -> None:
        """Tail the live training log every `train_monitor_interval_s` and record an observation.

        Phase 0: emit a `train_monitor` trace span per tick (pure observability — no LLM, no event, no
        intervention). Exits when the eval finishes (`cancel` set by `_evaluate`, or the task group is
        cancelled). Never raises out: a monitor hiccup must not fail the eval it only watches."""
        import anyio
        # A tiny floor guards only against a pathological busy-spin from a mis-set sub-millisecond value
        # (the config field is gt=0; the product default is 600s). It is NOT a "minimum useful cadence".
        interval = max(0.02, float(getattr(self, "_train_monitor_interval_s", 600.0) or 600.0))
        while True:
            await anyio.sleep(interval)      # only cancellation (eval finished) unwinds the task, from here
            if cancel.is_set():
                return
            try:
                tail = await anyio.to_thread.run_sync(read_training_tail, workdir)
                if not tail:
                    continue                 # no live log yet (stage not started / solution.py path)
                with self.tracer.span("train_monitor", node_id=node_id) as sp:
                    sp.set_many(generation=generation,
                                digest_lines=tail.count("\n") + 1,
                                digest_chars=len(tail))
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation — must propagate, never be swallowed
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup (disk/tracer) SKIPS this tick;
                continue                     # it must never disable the watcher for the rest of a long eval
