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
    recheck_after_s: Optional[float] = Field(
        default=None,
        description="Optional: how many seconds until you want to look again. Use a LARGER value when the "
                    "run is healthy and steady, so a boring, well-behaved run is not watched closely. You "
                    "cannot look sooner than the automatic cadence (values below it are ignored); the "
                    "automatic pace already tightens on shorter runs. Omit to keep the default cadence.")


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


# Phase 2 self-pacing constants. After this many CONSECUTIVE healthy verdicts (and no explicit
# agent-requested recheck), the monitor geometrically backs OFF — a steadily-healthy run does not need
# close watching — capped so it never fully stops (a late failure is still caught, just cheaply).
_HEALTHY_BACKOFF_K = 3
_MONITOR_CADENCE_CAP_S = 3600.0     # never wait more than an hour between checks (stays safe on late failures)
# Per-node LLM-call backstop. The adaptive cadence + healthy backoff already bound calls to ~budget/base
# (≈150 even for a 24h eval); this is a never-normally-hit ceiling for a pathological always-changing,
# never-healthy log. Past it the monitor keeps observing (trace) but stops spending on the LLM.
_MAX_MONITOR_LLM_CALLS = 200


def next_monitor_sleep(base: float, *, status: Optional[str] = None,
                       recheck_after_s: Optional[float] = None, healthy_streak: int = 0,
                       backoff_after: int = _HEALTHY_BACKOFF_K, cap: float = _MONITOR_CADENCE_CAP_S) -> float:
    """The delay until the NEXT check, given the base cadence and the latest verdict. Pure/deterministic.

    Precedence: an explicit agent-requested `recheck_after_s` wins (the observer self-paces — but never
    faster than `base`, to bound LLM cost, and never slower than `cap`); otherwise a run that has been
    healthy for `backoff_after`+ consecutive checks backs off geometrically (×2 each extra healthy tick,
    bounded); everything else keeps the base cadence."""
    if isinstance(recheck_after_s, (int, float)) and not isinstance(recheck_after_s, bool) and recheck_after_s > 0:
        return min(cap, max(base, float(recheck_after_s)))
    if status == "healthy" and healthy_streak >= backoff_after:
        return min(cap, base * (2.0 ** min(healthy_streak - backoff_after + 1, 6)))
    return base


def should_monitor_kill(verdict: Optional["TrainingVerdict"], *, enabled: bool, threshold: float) -> bool:
    """Whether a verdict warrants an EARLY KILL (Phase 3). Pure/deterministic. Kills only on a 'broken'
    verdict (the prompt makes a slow/plateauing-but-progressing run 'watch', never 'broken') with
    confidence >= `threshold`, and only when the intervention is `enabled` (opt-in). 'watch' and 'healthy'
    never kill — the monitor stays advisory for them."""
    if not enabled or verdict is None or verdict.status != "broken":
        return False
    try:
        conf = max(0.0, min(1.0, float(verdict.confidence)))
    except (TypeError, ValueError):
        return False
    return conf >= threshold


def active_training_log(workdir) -> Optional[Path]:
    """The workdir's most-recently-written `*.log` — a proxy for the live log of whichever stage is running
    NOW (the sandbox writes one `<stage>.log` per stage; during training the train stage's file is the
    freshest). The mtime heuristic follows the moving active stage without coupling to the sandbox's live
    stage cursor (unobservable from the engine's worker thread); if the solution code also drops its OWN
    `*.log`, the freshest one still tracks the most recent training output, which is what the observer
    wants. None when there is no `*.log` at all (the solution.py path writes none)."""
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

    def _monitor_cadence(self) -> float:
        """The BASE check interval, derived from the per-experiment time budget so a short training is
        watched often and a multi-hour one sparsely (a fixed 600s would miss a 5-minute run entirely and
        over-watch a 5-hour one). ~10% of the budget, clamped to [30s, 30min], then floored by the config
        `train_monitor_interval_s` so the user can force MORE-frequent checks. Falls back to the config
        interval when no budget is known (solution.py path / no eval_spec)."""
        cfg = max(0.02, float(getattr(self, "_train_monitor_interval_s", 600.0) or 600.0))
        budget = None
        fn = getattr(self, "_experiment_time_budget", None)
        if callable(fn):
            try:
                budget = fn()
            except Exception:  # noqa: BLE001 — cadence is advisory; a budget hiccup just uses the config
                budget = None
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
            derived = min(1800.0, max(30.0, float(budget) * 0.1))
            return min(cfg, derived)         # config is an upper bound: the user can only tighten it
        return cfg

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
                                context: str = "", kill_signal: Optional[dict] = None) -> None:
        """Tail the live training log every `train_monitor_interval_s`, ask the Developer to judge its
        health, record the verdict, and (opt-in) kill a broken run early.

        Advisory (always): every tick with a CHANGED digest emits a `train_monitor` trace span carrying
        the verdict; a NON-healthy verdict additionally appends an EV_TRAIN_MONITOR_ALERT diagnostic event
        (fold-ignored — never touches node selection or replay — for the owner attention feed + audit).
        Healthy verdicts stay trace-only, so events.jsonl carries only actionable flags.

        Intervention (Phase 3, only when `_train_monitor_kill` is on): on a confident 'broken' verdict the
        monitor records the reason into `kill_signal` and sets `cancel` — the SAME tree-kill path an
        operator abort uses — then stops. `_evaluate` sees the killed eval and writes the node's single
        terminal `node_failed` (reason='monitor_broken'); replay reconstructs the node from that terminal
        and never re-invokes the LLM. A plateau is 'watch', never 'broken', so it is never killed.

        With no LLM client wired it degrades to trace-only observation. Exits when the eval finishes
        (`cancel`, or the task group is cancelled); a per-tick hiccup skips the tick and never disables the
        watcher for the rest of a long eval."""
        import anyio

        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_TRAIN_MONITOR_ALERT
        # Base cadence derived from the per-experiment time budget (Phase 2): a short training is watched
        # often, a multi-hour one sparsely. The next delay adapts per verdict — the observer self-paces
        # (LLM `recheck_after_s`) and a steadily-healthy run backs off — via `next_monitor_sleep`.
        base = self._monitor_cadence()
        next_sleep = base
        last_digest: Optional[str] = None
        healthy_streak = 0
        llm_calls = 0
        while True:
            await anyio.sleep(next_sleep)    # only cancellation (eval finished) unwinds the task, from here
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
                    # Per-node backstop on LLM cost (the adaptive cadence + healthy-backoff are the primary
                    # budget control; this only bounds a pathological run whose digest keeps changing while
                    # staying non-healthy). Past the cap we keep OBSERVING (trace-only) but stop calling the
                    # LLM. Surfaced on the span — a silent cap would read as "all healthy" when it isn't.
                    verdict = None
                    if llm_calls >= _MAX_MONITOR_LLM_CALLS:
                        sp.set("llm_capped", True)
                    else:
                        # abandon_on_cancel=True: when the eval finishes and the task group cancels, node
                        # completion must NOT wait for an in-flight LLM call (an endpoint timeout+retry could be
                        # minutes). Cancel unwinds immediately; the abandoned thread finishes in the background
                        # and its (now-moot) verdict is discarded. The verdict is advisory, so losing it is fine.
                        verdict = await anyio.to_thread.run_sync(
                            self._training_verdict, tail, context, abandon_on_cancel=True)
                        llm_calls += 1
                    if verdict is not None:
                        conf = max(0.0, min(1.0, float(verdict.confidence)))
                        # The reason is LLM text derived from the raw log; redact it before it lands in the
                        # trace / event log / attention feed, matching how `_evaluate` stores stderr tails.
                        _redact = getattr(self, "_redact", None)
                        reason = (verdict.reason or "")
                        reason = (_redact(reason) if callable(_redact) else reason)[:300]
                        # The durable event keeps the fuller reason (300); the trace span carries a shorter
                        # preview (200) — spans are a high-volume sidecar, the event is the authoritative record.
                        sp.set_many(status=verdict.status, confidence=round(conf, 3), reason=reason[:200])
                        healthy_streak = healthy_streak + 1 if verdict.status == "healthy" else 0
                        next_sleep = next_monitor_sleep(
                            base, status=verdict.status, recheck_after_s=verdict.recheck_after_s,
                            healthy_streak=healthy_streak)
                        sp.set("next_check_s", round(next_sleep, 2))
                        if verdict.status != "healthy":
                            # Advisory flag only. DIAGNOSTIC => the fold never reads it, so appending it from
                            # this concurrent monitor task is splice-neutral and replay-safe by construction.
                            assert EV_TRAIN_MONITOR_ALERT in DIAGNOSTIC_EVENTS
                            async with self._write_lock:
                                self.store.append(EV_TRAIN_MONITOR_ALERT, {
                                    "node_id": node_id, "generation": generation,
                                    "status": verdict.status, "reason": reason,
                                    "confidence": round(conf, 3)})
                        # Phase 3 intervention (opt-in): a confident 'broken' run is tree-killed EARLY. Hand
                        # the reason to `_evaluate` via `kill_signal`, set `cancel` (same path as an operator
                        # abort), and stop watching — `_evaluate` writes the single terminal node_failed.
                        if kill_signal is not None and should_monitor_kill(
                                verdict, enabled=getattr(self, "_train_monitor_kill", False),
                                threshold=float(getattr(self, "_train_monitor_kill_confidence", 0.8) or 0.0)):
                            kill_signal["kill"] = True
                            kill_signal["reason"] = reason
                            kill_signal["confidence"] = round(conf, 3)
                            cancel.set()
                            return
            except anyio.get_cancelled_exc_class():
                raise                        # cooperative cancellation — must propagate, never be swallowed
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup (disk/LLM/tracer) SKIPS this tick;
                continue                     # it must never disable the watcher for the rest of a long eval
