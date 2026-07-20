"""ASHA live-curve early-stop watchdog (advisory + opt-in kill) — a sibling of `train_monitor.py`.

Where the training monitor judges the run's HEALTH from the log, this watchdog judges its RANK: it
reads the latest INTERMEDIATE value of the run's objective metric off the live log and compares it to
the FINAL metrics of already-completed sibling nodes. A node whose intermediate value is already worse
than a configured quantile (default the median) of its siblings' finals is very unlikely to catch up —
successive-halving's core idea, applied to the live curve while the node is still training.

Advisory by default: a non-healthy rank records a fold-IGNORED `EV_ASHA_RANK` diagnostic event (so its
thread-schedule-dependent position never touches replay/selection — splice-neutral by construction) and
a `asha_monitor` trace span. An opt-in `asha_live_kill` lets it tree-kill an underperformer early,
reusing the training monitor's `kill_signal` + the single `_evaluate` terminal (reason
`asha_underperforming`), so replay reads that terminal and never re-invokes this watchdog.

Fragility is contained by construction:
 - metric extraction REUSES the eval's own metric contract (`command_eval.read_metric` — the same
   stdout_json/regex/file reader that scores the final result), so there is no bespoke log parsing;
   it returns None when nothing parses yet (no signal, never a false stop);
 - a min-siblings floor means it never ranks against too little evidence;
 - a grace period (`_ASHA_GRACE_TICKS`) protects a slow start — a kill never fires on the first acting
   ticks, only after the underperformance persists;
 - comparing an INTERMEDIATE value to sibling FINALS is deliberately optimistic-biased AGAINST stopping
   for direction-min-vs-max symmetry: the node has to be worse than a *finished* peer to even flag, and
   the quantile is the operator's dial (lower = only stop the truly doomed).
"""
from __future__ import annotations

from typing import Optional

# A kill never fires until the node has been flagged underperforming on this many CONSECUTIVE acting
# ticks (ticks where a metric parsed AND enough siblings exist). Protects a slow-but-recovering start:
# a transient dip below the bar that recovers within the grace window never stops the run.
_ASHA_GRACE_TICKS = 2


def latest_intermediate(log_tail: str, workdir, metric_spec: dict) -> Optional[float]:
    """The most recent intermediate value of the objective metric in the live-log tail, using the
    eval's own stdout metric reader (`command_eval.read_metric` reads the LAST match — the newest
    epoch/step). None when nothing parses yet or the reader errors. Best-effort; never raises.

    ONLY stdout-based metric kinds (`stdout_json` / `stdout_regex`) are read here: they parse the log
    TEXT directly and ignore the sandbox `wrap` + the workdir-reuse `since` freshness gate. The
    file_*/adapter/host_score kinds read a workdir FILE or EXEC agent code — running those every tick on
    the raw tail would (a) bypass the eval's Docker sandbox wrap (adapter execs on the host), (b) block
    the event loop (a synchronous subprocess), and (c) on a workdir-reuse re-eval read a STALE
    prior-attempt file as the live value. Those kinds simply get no live-curve signal (return None)."""
    if not log_tail or not isinstance(metric_spec, dict):
        return None
    if metric_spec.get("kind", "stdout_json") not in ("stdout_json", "stdout_regex"):
        return None
    try:
        from looplab.runtime import command_eval
        val = command_eval.read_metric(log_tail, str(workdir), metric_spec)
    except Exception:  # noqa: BLE001 — a reader hiccup means "no intermediate this tick", not a crash
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return None
    return float(val) if val == val and abs(val) != float("inf") else None   # drop NaN/inf


def sibling_final_metrics(state, node_id: int) -> list[float]:
    """Final metrics of OTHER promotion-eligible nodes in this run.

    Tombstoned, aborted, infeasible and trust-gated outcomes are audit evidence, not a population that
    may stop fresh compute. Reuse the selector's eligibility boundary so ASHA cannot be stricter than
    the search policy merely because a discarded sibling retained an attractive metric.
    """
    from looplab.events.replay import promotion_eligible_nodes

    out: list[float] = []
    for node in promotion_eligible_nodes(state):
        if node.id == node_id:
            continue
        m = getattr(node, "metric", None)
        if isinstance(m, bool) or not isinstance(m, (int, float)):
            continue
        m = float(m)
        if m == m and abs(m) != float("inf"):
            out.append(m)
    return out


def asha_underperforming(value: Optional[float], population: list[float], direction: str, *,
                         quantile: float = 0.5) -> Optional[bool]:
    """Is `value` (an intermediate metric) already WORSE than the `quantile` of the completed
    `population` finals, given the optimization `direction`? Pure/deterministic.

    Returns None when it cannot decide (no value, empty population, or a bad quantile) so the caller
    treats "unknown" as "do not act". The population is ordered WORST->BEST and the bar sits at fraction
    `quantile` along that axis; `value` underperforms if it is strictly worse than the bar. So
    quantile=0.5 = the median, quantile=0.0 = the WORST finished peer (only a value worse than the worst
    is flagged — most conservative), quantile=1.0 = the BEST peer (almost everything below top flags —
    most aggressive). SMALLER quantile => more conservative (fewer stops)."""
    if value is None or not population:
        return None
    try:
        q = float(quantile)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= q <= 1.0):
        return None
    is_min = str(direction) == "min"
    # Order WORST -> BEST so q places the bar at fraction q along that axis (q=0 -> worst peer, most
    # conservative; q=1 -> best peer, most aggressive). For min the worst final is the largest.
    ordered = sorted(population, reverse=is_min)      # min: descending (worst first); max: ascending
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    bar = ordered[idx]
    return (value > bar) if is_min else (value < bar)


class AshaMonitorMixin:
    """The engine's ASHA live-curve watchdog. `self` IS the Engine (mixin convention — see
    orchestrator.py). Gated on `self._asha_live`; started as a sibling task in `_evaluate`'s task group
    so it lives exactly as long as the eval and is cancelled with it. Reuses the training monitor's
    log-tail reader, adaptive cadence, and `kill_signal`."""

    def _asha_cadence(self) -> float:
        """Base check interval — reuse the training monitor's budget-derived cadence when present (a
        short training is watched often, a multi-hour one sparsely), else a safe default. The ASHA
        check is cheap (a fold + the metric reader, no LLM), so the same cadence is comfortable."""
        fn = getattr(self, "_monitor_cadence", None)
        if callable(fn):
            try:
                c = fn()
                if isinstance(c, (int, float)) and not isinstance(c, bool) and c > 0:
                    return float(c)
            except Exception:  # noqa: BLE001 — advisory cadence; fall back
                pass
        return 600.0

    async def _monitor_asha(self, node_id: int, generation: int, workdir, cancel,
                            metric_spec: dict, direction: str,
                            kill_signal: Optional[dict] = None) -> None:
        """Tail the live log on a timer, extract the intermediate objective metric, rank it against the
        completed siblings, record an advisory `EV_ASHA_RANK` when it underperforms, and (opt-in) tree-
        kill a persistently-underperforming node. See the module docstring for the safety rails. Exits
        when the eval finishes (`cancel` / task-group cancel); a per-tick hiccup skips the tick."""
        import anyio

        from looplab.engine.train_monitor import read_training_tail_raw
        from looplab.events.replay import fold
        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_ASHA_RANK

        base = self._asha_cadence()
        # Read the configured values WITHOUT `or`-coercion, so a legitimate quantile=0.0 (most
        # conservative) is honoured rather than silently reset to the 0.5 default.
        _q = getattr(self, "_asha_live_quantile", 0.5)
        quantile = float(_q) if isinstance(_q, (int, float)) and not isinstance(_q, bool) else 0.5
        _ms = getattr(self, "_asha_live_min_siblings", 3)
        min_siblings = max(1, int(_ms)) if isinstance(_ms, (int, float)) and not isinstance(_ms, bool) else 3
        last_flag: Optional[bool] = None
        under_streak = 0
        while True:
            await anyio.sleep(base)
            if cancel.is_set():
                return
            try:
                tail = await anyio.to_thread.run_sync(read_training_tail_raw, workdir)
                value = latest_intermediate(tail, workdir, metric_spec)
                if value is None:
                    continue
                state = await anyio.to_thread.run_sync(lambda: fold(self.store.read_all()))
                population = sibling_final_metrics(state, node_id)
                if len(population) < min_siblings:
                    continue                    # not enough finished peers to rank against yet
                under = asha_underperforming(value, population, direction, quantile=quantile)
                if under is None:
                    continue
                under_streak = under_streak + 1 if under else 0
                # ADVISORY record — only when the verdict CHANGES (no log/feed spam), kept separate from
                # the kill decision below so a persistent-underperform streak still reaches the kill check.
                if under != last_flag:
                    last_flag = under
                    with self.tracer.span("asha_monitor", node_id=node_id) as sp:
                        sp.set_many(generation=generation, intermediate=round(value, 6),
                                    underperforming=bool(under), population=len(population),
                                    quantile=round(quantile, 3))
                        if under:
                            # DIAGNOSTIC => the fold never reads it, so appending it from this concurrent
                            # watchdog is splice-neutral and replay-safe by construction.
                            assert EV_ASHA_RANK in DIAGNOSTIC_EVENTS
                            async with self._write_lock:
                                self.store.append(EV_ASHA_RANK, {
                                    "node_id": node_id, "generation": generation,
                                    "intermediate": round(value, 6), "quantile": round(quantile, 3),
                                    "population": len(population), "direction": str(direction)})
                # OPT-IN kill — independent of the advisory dedup: fires once the underperformance has
                # PERSISTED past the grace window (a transient early dip that recovers resets the streak
                # to 0 and is never stopped). Reuses the monitor's kill_signal + cancel; `_evaluate`
                # writes the single terminal (reason=asha_underperforming).
                if (kill_signal is not None and under and under_streak > _ASHA_GRACE_TICKS
                        and getattr(self, "_asha_live_kill", False)):
                    kill_signal["kill"] = True
                    kill_signal["reason"] = (
                        f"intermediate metric {value:.4g} is below the "
                        f"{quantile:.0%} bar of {len(population)} finished siblings")
                    kill_signal["terminal_reason"] = "asha_underperforming"
                    cancel.set()
                    return
            except anyio.get_cancelled_exc_class():
                raise                           # cooperative cancellation — must propagate
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup skips this tick, never disables
                continue
