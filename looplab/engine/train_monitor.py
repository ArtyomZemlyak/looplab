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
from typing import Literal, Optional

from pydantic import BaseModel, Field

# The verdict schema the log observer returns. `status` drives everything downstream: a non-"healthy"
# verdict becomes an EV_TRAIN_MONITOR_ALERT (Phase 1) and, later, a gated early kill (Phase 3, "broken"
# only). Field descriptions are part of the schema the model sees — they ARE the classification contract.
class TrainingVerdict(BaseModel):
    status: Literal["healthy", "watch", "broken"] = Field(
        description="healthy = training is progressing normally (loss decreasing or stable, no errors); "
                    "watch = something looks off but not necessarily fatal (slow, plateauing, warnings); "
                    "broken = clear evidence the run is WASTED and cannot recover — diverged loss, a silent "
                    "CPU fallback while a GPU was expected, data-loader errors repeating in the loop, loss "
                    "stuck at its initialization value (not learning), or an uncaught exception.")
    reason: str = Field(description="One short sentence naming the SPECIFIC log evidence for the status.")
    confidence: float = Field(default=0.5, description="Confidence in the status, 0.0 to 1.0.")


# The observer's framing. A contract: it fixes the observer's role (the engineer who wrote THIS loop),
# what it may rely on (log evidence, NOT the unknown final metric), and its bias (flag EARLY, before the
# whole budget is burned — but do not cry wolf on a normal slow-but-progressing run).
_MONITOR_SYSTEM = (
    "You are the ML engineer who wrote this training script, watching its LIVE log during a long run to "
    "catch a wasted run EARLY — before its whole (often multi-hour) time budget is spent. Judge ONLY from "
    "the log evidence; the final metric is not known yet, so do not guess it. A run that is merely slow or "
    "plateauing but still progressing is 'watch', not 'broken' — reserve 'broken' for clear, cannot-recover "
    "evidence. Be concise and specific about the evidence you saw.")


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

    def _training_verdict(self, digest: str, context: str) -> Optional[TrainingVerdict]:
        """One-shot LLM judgment of the live log (SYNC — the caller runs it in a worker thread). Uses the
        Developer's client (the Developer wrote the loop, so it knows what its own logs should look like)
        with a fresh, STATELESS structured call — it never mutates the shared role object, so it is safe to
        fire while the eval thread runs. The client records its own usage/cost. Returns None when there is
        no client (offline / toy path) or the model output can't be parsed — advisory, never fatal."""
        client = getattr(getattr(self, "developer", None), "client", None)
        if client is None:
            return None
        messages = [
            {"role": "system", "content": _MONITOR_SYSTEM},
            {"role": "user", "content": ((context + "\n\n") if context else "")
             + "LIVE TRAINING LOG (recent tail):\n" + digest
             + "\n\nClassify this run's health from the log evidence above."},
        ]
        try:
            from looplab.core.parse import parse_structured
            return parse_structured(client, messages, TrainingVerdict)
        except Exception:  # noqa: BLE001 — a parser/endpoint failure means "no verdict this tick", not a crash
            return None

    async def _monitor_training(self, node_id: int, generation: int, workdir, cancel,
                                context: str = "") -> None:
        """Tail the live training log every `train_monitor_interval_s`, ask the Developer to judge its
        health, and record the verdict.

        Phase 1 (advisory): every tick with a CHANGED digest emits a `train_monitor` trace span carrying
        the verdict; a NON-healthy verdict additionally appends an EV_TRAIN_MONITOR_ALERT diagnostic event
        (fold-ignored — never touches node selection or replay — for the owner attention feed + audit).
        Healthy verdicts stay trace-only, so events.jsonl carries only actionable flags. NO intervention:
        the run is never killed here (that is Phase 3). With no LLM client wired it degrades to Phase 0
        (trace-only observation). Exits when the eval finishes (`cancel`, or the task group is cancelled);
        a per-tick hiccup skips the tick and never disables the watcher for the rest of a long eval."""
        import anyio

        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_TRAIN_MONITOR_ALERT
        # A tiny floor guards only against a pathological busy-spin from a mis-set sub-millisecond value
        # (the config field is gt=0; the product default is 600s). It is NOT a "minimum useful cadence".
        interval = max(0.02, float(getattr(self, "_train_monitor_interval_s", 600.0) or 600.0))
        last_digest: Optional[str] = None
        while True:
            await anyio.sleep(interval)      # only cancellation (eval finished) unwinds the task, from here
            if cancel.is_set():
                return
            try:
                tail = await anyio.to_thread.run_sync(read_training_tail, workdir)
                if not tail or tail == last_digest:
                    continue                 # no live log yet, or nothing new since last tick -> no LLM call
                last_digest = tail
                # Open the span BEFORE the LLM call so the observer's LLM turn bands under `train_monitor`
                # (not the enclosing `evaluate`) — the same trace-attribution fix `_triage_crash` uses.
                with self.tracer.span("train_monitor", node_id=node_id) as sp:
                    sp.set_many(generation=generation,
                                digest_lines=tail.count("\n") + 1, digest_chars=len(tail))
                    verdict = await anyio.to_thread.run_sync(self._training_verdict, tail, context)
                    if verdict is not None:
                        conf = max(0.0, min(1.0, float(verdict.confidence)))
                        # The reason is LLM text derived from the raw log; redact it before it lands in the
                        # trace / event log / attention feed, matching how `_evaluate` stores stderr tails.
                        _redact = getattr(self, "_redact", None)
                        reason = (verdict.reason or "")
                        reason = (_redact(reason) if callable(_redact) else reason)[:300]
                        sp.set_many(status=verdict.status, confidence=round(conf, 3), reason=reason[:200])
                        if verdict.status != "healthy":
                            # Advisory flag only. DIAGNOSTIC => the fold never reads it, so appending it from
                            # this concurrent monitor task is splice-neutral and replay-safe by construction.
                            assert EV_TRAIN_MONITOR_ALERT in DIAGNOSTIC_EVENTS
                            async with self._write_lock:
                                self.store.append(EV_TRAIN_MONITOR_ALERT, {
                                    "node_id": node_id, "generation": generation,
                                    "status": verdict.status, "reason": reason,
                                    "confidence": round(conf, 3)})
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation — must propagate, never be swallowed
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup (disk/LLM/tracer) SKIPS this tick;
                continue                     # it must never disable the watcher for the rest of a long eval
