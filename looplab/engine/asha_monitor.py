"""ASHA live-curve early-stop watchdog (advisory + opt-in kill) — a sibling of `train_monitor.py`.

Where the training monitor judges the run's HEALTH from the log, this watchdog judges its RANK. The
historical intermediate-vs-finished-endpoint comparison remains an ADVISORY signal. An opt-in kill is
stricter: the metric spec must explicitly name ``resource_key`` and enough completed siblings must
retain metric observations at exactly the same resource value. An early point is never treated as
comparable to a finished endpoint merely because both contain the objective metric.

Advisory by default: each rank-state transition records a fold-IGNORED `EV_ASHA_RANK` diagnostic event,
so its thread-schedule-dependent position cannot directly alter lifecycle/champion/replay. The raw
diagnostic may still advise a later Researcher prompt when `watchdog_reflection` is enabled. A matching
`asha_monitor` trace span is also emitted. An opt-in `asha_live_kill` lets it tree-kill an underperformer early,
reusing the training monitor's `kill_signal` + the single `_evaluate` terminal (reason
`asha_underperforming`), so replay reads that terminal and never re-invokes this watchdog.

Fragility is contained by construction:
 - metric extraction REUSES the eval's own metric contract (`command_eval.read_metric` — the same
   stdout_json/regex/file reader that scores the final result), so there is no bespoke log parsing;
   it returns None when nothing parses yet (no signal, never a false stop);
 - a min-siblings floor means it never ranks against too little evidence;
 - a grace period (`_ASHA_GRACE_TICKS`) protects a slow start — a kill never fires on the first acting
   ticks, only after the underperformance persists;
 - endpoint comparison remains useful diagnostics but cannot kill; missing same-resource evidence
   degrades safely to advisory-only observation.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Optional

# A kill never fires until the node has been flagged underperforming for MORE than this many
# CONSECUTIVE acting ticks (a metric and enough same-resource sibling observations exist): the gate is
# `under_streak > _ASHA_GRACE_TICKS`, so these ticks are a grace WINDOW and the kill can fire no earlier
# than the next one (with 2, the 3rd consecutive underperforming tick). Protects a recovering start:
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


@dataclass(frozen=True)
class IntermediateSample:
    """A live metric plus optional operator-declared resource evidence from the same JSON record."""

    value: float
    resource_key: Optional[str] = None
    resource: Optional[float] = None


def _finite_number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)                # a 400-digit JSON int overflows float() — degrade to "no
    except (OverflowError, ValueError):     # rung/observation", never crash the write-path caller
        return None                         # (extract_resource_curve runs inside _evaluate's write lock)
    return value if value == value and abs(value) != float("inf") else None


def _declared_resource_key(metric_spec: dict) -> Optional[str]:
    key = metric_spec.get("resource_key") if isinstance(metric_spec, dict) else None
    metric_key = metric_spec.get("key", "metric") if isinstance(metric_spec, dict) else "metric"
    return key if (isinstance(key, str) and 0 < len(key) <= 128 and key != metric_key) else None


def _resource_rung(resource) -> Optional[float]:
    """Snap a resource coordinate to a canonical geometric RUNG — the largest power of two <= it — so a
    live sample and completed-sibling curves compare at the SAME checkpoint across the WHOLE run, not
    only the dense early coordinates (#7 review). Steps 32..63 all map to rung 32, so a live node deep in
    the run still finds sibling values at its rung instead of the exact-coordinate gap the first-31+
    endpoint retention left. None for a non-finite / non-positive resource (no rung -> no comparison)."""
    r = _finite_number(resource)
    if r is None or r <= 0:
        return None
    return 2.0 ** math.floor(math.log2(r))


def _curve_metric_at(curve, resource) -> Optional[float]:
    """The metric persisted at the canonical RUNG of `resource` in a bounded `[[rung, metric], ...]`
    curve, or None. Both the live sample and the persisted curve snap to the SAME rung (`_resource_rung`),
    so a sample anywhere in a rung band finds its sibling checkpoint — not only at an exactly-matching
    coordinate. Tolerant of old logs (curve is None) and malformed rows — a missing rung degrades the
    sibling to "no comparable observation", never a crash."""
    rung = _resource_rung(resource)
    if rung is None or not isinstance(curve, list):
        return None
    for entry in curve:
        if (isinstance(entry, (list, tuple)) and len(entry) == 2
                and _finite_number(entry[0]) == rung):
            return _finite_number(entry[1])
    return None


def _json_objects_newest_first(log_tail: str):
    for line in reversed(log_tail.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(obj, dict):
            yield obj


def latest_intermediate_sample(log_tail: str, workdir, metric_spec: dict) -> Optional[IntermediateSample]:
    """Return the latest metric and resource only when both occur in the same JSON record.

    ``resource_key`` is operator-owned eval configuration. We deliberately do not guess that arbitrary
    ``step``/``epoch``-looking output is fidelity: without an explicit declaration the sample remains
    useful for endpoint diagnostics but is ineligible for an early kill.
    """
    value = latest_intermediate(log_tail, workdir, metric_spec)
    if value is None:
        return None
    resource_key = _declared_resource_key(metric_spec)
    if metric_spec.get("kind", "stdout_json") != "stdout_json" or resource_key is None:
        return IntermediateSample(value=value)
    metric_key = metric_spec.get("key", "metric")
    for obj in _json_objects_newest_first(log_tail):
        if metric_key not in obj:
            continue
        record_value = _finite_number(obj.get(metric_key))
        resource = _finite_number(obj.get(resource_key))
        if record_value == value and resource is not None:
            return IntermediateSample(value=value, resource_key=resource_key, resource=resource)
        return IntermediateSample(value=value)
    return IntermediateSample(value=value)


def extract_resource_curve(stdout: str, metric_spec, *, max_points: int = 32) -> Optional[list]:
    """A bounded per-RUNG curve ``[[rung, metric], ...]`` mined from the eval's CAPTURED stdout at
    node_evaluated time (#7). That capture is run_argv's bounded ~64 KB tail (and, for a staged eval, the
    final stage's output) — 128× the 500-char ``stdout_tail``, which retains only the FINAL epochs so a
    live node stopped earlier never found same-resource peers. Persisting this durable curve lets
    ``sibling_metrics_at_resource`` compare a fresh live sample against PAST EXPERIMENTS at the same rung.
    Reuses the eval's own metric contract (the operator/Developer-declared ``resource_key`` + metric key);
    returns None when the task declares no ``resource_key`` or the kind isn't ``stdout_json``.

    Coordinates are collapsed to a canonical geometric RUNG schedule (`_resource_rung`: the largest power
    of two <= the coordinate), keeping the EARLIEST value within each rung band — the band's START-of-band
    checkpoint — sorted by rung (#7 review). A shared rung schedule is what makes the comparison work
    across the WHOLE run: the live poller snaps its sample to the same rungs, so a node deep in the run
    finds a sibling checkpoint instead of the exact-coordinate gap the old first-31+endpoint retention
    left. EARLIEST (not latest) per band matters: a LIVE node just INTO a band sits at the band's LOW end,
    so comparing it against a sibling's END-of-band value (up to ~2× the training) would systematically
    false-flag a healthy improving node; the start-of-band checkpoint keeps the comparison conservative
    (a live sample later in the band only ever looks BETTER than the sibling's earlier checkpoint, never
    spuriously worse). Geometric rungs are O(log resource) — a 100-step run yields 7 rungs, ~1e6 steps ~20
    — so ``max_points`` (default 32) is a corruption bound that realistic runs never hit; when exceeded it
    keeps the earliest ``max_points - 1`` rungs plus the last. ``max_points < 2`` disables that bound."""
    if not stdout or not isinstance(metric_spec, dict):
        return None
    resource_key = _declared_resource_key(metric_spec)
    if resource_key is None or metric_spec.get("kind", "stdout_json") != "stdout_json":
        return None
    metric_key = metric_spec.get("key", "metric")
    by_rung: dict[float, float] = {}
    # Chronological order (reverse the newest-first generator) + `setdefault` so the EARLIEST value in a
    # rung band wins (the start-of-band checkpoint — see the docstring on why not the latest).
    for obj in reversed(list(_json_objects_newest_first(stdout))):
        rung = _resource_rung(obj.get(resource_key))
        value = _finite_number(obj.get(metric_key))
        if rung is not None and value is not None:
            by_rung.setdefault(rung, value)
    if not by_rung:
        return None
    coords = sorted(by_rung)
    if max_points >= 2 and len(coords) > max_points:
        # Corruption/pathology bound only (geometric rungs are O(log resource) — realistic runs stay far
        # below the cap). Keep the earliest `max_points-1` rungs + the last.
        keep = sorted(set(range(max_points - 1)) | {len(coords) - 1})
        coords = [coords[i] for i in keep]
    return [[r, by_rung[r]] for r in coords]


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


def sibling_metrics_at_resource(state, node_id: int, metric_spec: dict,
                                sample: IntermediateSample) -> list[float]:
    """Sibling curve values observed at the live sample's declared resource RUNG (`_resource_rung`).

    A completed node contributes ONLY its durable per-rung `resource_curve` START-of-band checkpoint.
    Missing/truncated/malformed curve evidence simply removes that sibling from the comparable
    population; a value is NEVER substituted from its stdout tail (which holds only final epochs, i.e.
    an end-of-band point that would false-flag a live node just into the band).
    """
    if sample.resource_key is None or sample.resource is None:
        return []
    if _resource_rung(sample.resource) is None:
        return []                      # a non-positive/degenerate resource maps to no rung -> no compare
    if _declared_resource_key(metric_spec) != sample.resource_key:
        return []
    if metric_spec.get("kind", "stdout_json") != "stdout_json":
        return []

    from looplab.events.replay import promotion_eligible_nodes

    out: list[float] = []
    for node in promotion_eligible_nodes(state):
        if node.id == node_id:
            continue
        # #7: read the durable per-RUNG curve mined from the eval's captured stdout (~64 KB tail) at
        # node_evaluated (extract_resource_curve). Both sides snap to a shared geometric rung schedule
        # (`_resource_rung`), so a live node anywhere in the run — not only at an exact early coordinate —
        # finds a sibling checkpoint at its rung. A sibling contributes ONLY via its persisted curve (the
        # START-of-band checkpoint): its 500-char stdout_tail retains only the FINAL epochs, so any in-band
        # value there is an END-of-band (~2× more-trained) point — substituting it would false-flag a live
        # node just into the band and mix start/end-of-band values in one population (peer review). A
        # sibling whose curve lacks this rung (a pre-#7 log, or a #7 curve whose early rung scrolled past
        # the ~64 KB extract window) is simply EXCLUDED, never substituted from the tail — exactly the
        # docstring's contract. The min_siblings floor then degrades to no-kill on thin same-rung evidence.
        value = _curve_metric_at(getattr(node, "resource_curve", None), sample.resource)
        if value is not None:
            out.append(value)
    return out


def asha_underperforming(value: Optional[float], population: list[float], direction: str, *,
                         quantile: float = 0.5) -> Optional[bool]:
    """Is `value` already WORSE than the `quantile` of `population`, given `direction`?

    The caller may supply completed endpoints for an advisory rank or same-resource curve values for
    an intervention decision; this pure ordering helper deliberately has no resource semantics.

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
                            kill_signal: Optional[dict] = None, log_snapshot=None) -> None:
        """Tail the live log on a timer, extract the intermediate objective metric, rank it against the
        completed sibling endpoints for diagnostics, and record advisory `EV_ASHA_RANK` transitions.
        Opt-in tree-kill requires persistent underperformance against enough sibling observations at the
        same declared resource. Exits with the eval; a per-tick hiccup skips only that tick."""
        import anyio

        from looplab.engine.train_monitor import claim_watchdog_kill, read_training_tail_raw
        from looplab.events.replay import fold
        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_ASHA_RANK

        base = self._asha_cadence()
        # Read the configured values WITHOUT `or`-coercion, so a legitimate quantile=0.0 (most
        # conservative) is honoured rather than silently reset to the 0.5 default.
        _q = getattr(self, "_asha_live_quantile", 0.5)
        quantile = float(_q) if isinstance(_q, (int, float)) and not isinstance(_q, bool) else 0.5
        _ms = getattr(self, "_asha_live_min_siblings", 3)
        min_siblings = max(1, int(_ms)) if isinstance(_ms, (int, float)) and not isinstance(_ms, bool) else 3
        last_flag: Optional[tuple[bool, Optional[bool]]] = None
        # preserve an open episode across process re-entry; otherwise an initially healthy
        # resumed curve looks like a first observation and never emits the recovery edge. Modern rows
        # retain endpoint/resource truth separately; legacy rows safely map their single bit to endpoint.
        try:
            prior_rows = await anyio.to_thread.run_sync(self.store.read_all)
            for event in reversed(prior_rows):
                data = getattr(event, "data", None) or {}
                if (getattr(event, "type", None) == EV_ASHA_RANK
                        and isinstance(data.get("node_id"), int)
                        and not isinstance(data.get("node_id"), bool)
                        and data.get("node_id") == node_id
                        and isinstance(data.get("generation"), int)
                        and not isinstance(data.get("generation"), bool)
                        and data.get("generation") == generation):
                    endpoint = data.get("endpoint_underperforming")
                    resource = data.get("resource_underperforming")
                    if (isinstance(endpoint, bool)
                            and (resource is None or isinstance(resource, bool))):
                        last_flag = (endpoint, resource)
                    else:
                        raw_flag = data.get("underperforming", True)
                        last_flag = (raw_flag, None) if isinstance(raw_flag, bool) else None
                    break
        except Exception:  # noqa: BLE001 - advisory history lookup; the live monitor still proceeds
            pass
        under_streak = 0
        while True:
            await anyio.sleep(base)
            if cancel.is_set():
                return
            try:
                tail = await anyio.to_thread.run_sync(
                    lambda: read_training_tail_raw(workdir, snapshot=log_snapshot))
                sample = latest_intermediate_sample(tail, workdir, metric_spec)
                if sample is None:
                    continue
                value = sample.value
                state = await anyio.to_thread.run_sync(lambda: fold(self.store.read_all()))
                population = sibling_final_metrics(state, node_id)
                if len(population) < min_siblings:
                    continue                    # not enough finished peers to rank against yet
                endpoint_under = asha_underperforming(
                    value, population, direction, quantile=quantile)
                if endpoint_under is None:
                    continue
                comparable_population = sibling_metrics_at_resource(
                    state, node_id, metric_spec, sample)
                comparable_under = (
                    asha_underperforming(value, comparable_population, direction, quantile=quantile)
                    if len(comparable_population) >= min_siblings else None)
                under_streak = under_streak + 1 if comparable_under is True else 0
                diagnostic_key = (endpoint_under, comparable_under)
                # ADVISORY record — only when the verdict CHANGES (no log/feed spam), kept separate from
                # the kill decision below so a persistent-underperform streak still reaches the kill check.
                previous_flag = last_flag
                if diagnostic_key != previous_flag:
                    last_flag = diagnostic_key
                    underperforming = bool(endpoint_under) or comparable_under is True
                    previous_underperforming = bool(
                        previous_flag
                        and (previous_flag[0] or previous_flag[1] is True)
                    )
                    with self.tracer.span("asha_monitor", node_id=node_id) as sp:
                        sp.set_many(generation=generation, intermediate=round(value, 6),
                                    underperforming=underperforming, population=len(population),
                                    quantile=round(quantile, 3),
                                    kill_comparable=comparable_under is not None,
                                    comparable_population=len(comparable_population))
                        if sample.resource_key is not None and sample.resource is not None:
                            sp.set_many(resource_key=sample.resource_key, resource=sample.resource)
                        # publish both warning and recovery edges for the combined endpoint /
                        # same-resource advisory. Otherwise digest and Attention retain a historical flag
                        # after this exact node generation recovers.
                        if underperforming or previous_underperforming:
                            assert EV_ASHA_RANK in DIAGNOSTIC_EVENTS
                            event = {
                                "node_id": node_id, "generation": generation,
                                "underperforming": underperforming,
                                "intermediate": round(value, 6), "quantile": round(quantile, 3),
                                "population": len(population), "direction": str(direction),
                                "endpoint_underperforming": bool(endpoint_under),
                                "kill_comparable": comparable_under is not None,
                                "comparable_population": len(comparable_population),
                                "resource_underperforming": comparable_under,
                            }
                            if sample.resource_key is not None and sample.resource is not None:
                                event.update({"resource_key": sample.resource_key,
                                              "resource": sample.resource})
                            async with self._write_lock:
                                self.store.append(EV_ASHA_RANK, event)
                # OPT-IN kill — independent of the advisory dedup: fires once the underperformance has
                # PERSISTED past the grace window (a transient early dip that recovers resets the streak
                # to 0 and is never stopped). Reuses the monitor's kill_signal + cancel; `_evaluate`
                # writes the single terminal (reason=asha_underperforming).
                if (kill_signal is not None and comparable_under is True
                        and under_streak > _ASHA_GRACE_TICKS
                        and getattr(self, "_asha_live_kill", False)):
                    # never promote a finished endpoint into a fake peer at the live
                    # resource. Without explicit same-resource curve evidence this is unreachable.
                    claim_watchdog_kill(
                        kill_signal, cancel,
                        reason=(
                            f"intermediate metric {value:.4g} at "
                            f"{sample.resource_key}={sample.resource:g} is worse than the "
                            f"{quantile:.0%} bar of {len(comparable_population)} sibling "
                            "observations at the same resource"
                        ),
                        terminal_reason="asha_underperforming")
                    return
            except anyio.get_cancelled_exc_class():
                raise                           # cooperative cancellation — must propagate
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup skips this tick, never disables
                continue
