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

**The rank comparison is EVIDENCE, not the decision.** A bare quantile test sees exactly one number and
ignores everything else the run is saying — the SHAPE of its curve, the spread of the peers it is being
compared against, the other metrics it prints, and whether the training-health monitor already has an
opinion about it. So when the deterministic gate fires, an LLM judge (`AshaVerdict`, schema-validated
like `train_monitor.py`'s own verdict) receives that whole picture and answers continue|watch|stop; only a
confident `stop` kills. The judge is consulted INSIDE the existing gate — after the grace window, the
min-siblings floor, the explicit `resource_key` and `asha_live_kill` have ALL held — so by construction it
can only ever stop FEWER nodes than the rank test alone, never a node the rank test would have spared. No
judge (no LLM client, an endpoint failure, an unparseable answer) means NO kill: the watchdog degrades to
the advisory observation it was before, never to a blind quantile stop.

Fragility is contained by construction:
 - metric extraction REUSES the eval's own metric contract (`command_eval.read_metric` — the same
   stdout_json/regex/file reader that scores the final result), so there is no bespoke log parsing;
   it returns None when nothing parses yet (no signal, never a false stop);
 - a min-siblings floor means it never ranks against too little evidence;
 - a grace period (`_ASHA_GRACE_TICKS`) protects a slow start — a kill never fires on the first acting
   ticks, only after the underperformance persists;
 - endpoint comparison remains useful diagnostics but cannot kill; missing same-resource evidence
   degrades safely to advisory-only observation;
 - and when NONE of that can fire — the metric contract makes a kill unreachable, or the training log
   never prints the objective at all — the watchdog SAYS SO once per eval instead of ticking silently
   for hours (`asha_inert_reason` / `_state_asha_inert`; measured: zero rows in seven real runs);
 - the judge's verdict is a DIAGNOSTIC (fold-ignored) `EV_ASHA_VERDICT` row, like the training monitor's
   alert: its position in the log is thread-dependent, so it must never reach folded state. The stop
   itself is recorded by the node's single ordinary terminal, which is what replay reads — replay NEVER
   re-invokes the judge.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel, Field

from looplab.core.llm_broker import in_llm_lane
# The confidence normalizer is the training monitor's, deliberately not a second spelling: both watchdogs
# must treat a NaN/absent/non-numeric model confidence the same way (observable at 0.0, never authority
# for a kill). train_monitor imports nothing from this module, so the module-level import is cycle-free.
from looplab.engine.train_monitor import (
    _MONITOR_LOOK_TURNS,
    _normalize_monitor_confidence,
    monitor_log_tools,
)

# A kill never fires until the node has been flagged underperforming for MORE than this many
# CONSECUTIVE acting ticks (a metric and enough same-resource sibling observations exist): the gate is
# `under_streak > _ASHA_GRACE_TICKS`, so these ticks are a grace WINDOW and the kill can fire no earlier
# than the next one (with 2, the 3rd consecutive underperforming tick). Protects a recovering start:
# a transient dip below the bar that recovers within the grace window never stops the run.
_ASHA_GRACE_TICKS = 2

# Per-node backstop on judge calls. The judge is only reached once the rank gate has fired for
# `_ASHA_GRACE_TICKS + 1` consecutive ticks, and a non-stop verdict re-arms that whole window (see the
# loop), so a realistic eval consults it a handful of times at most; this only bounds a pathological
# flapping curve. Past the cap the watchdog keeps OBSERVING (trace + advisory rank rows) but stops
# spending on the LLM — and therefore stops being able to kill, which is the safe direction.
_MAX_ASHA_JUDGE_CALLS = 20

_LOG = logging.getLogger(__name__)

# ------------------------------------------------------- SAYING SO WHEN THIS WATCHDOG CANNOT ACT
# WHY THIS EXISTS, measured over the seven event logs in `runs/` (docs/BACKLOG.md §0.15, re-derived
# 2026-08-19): `asha_rank` has ZERO rows corpus-wide, and so has `asha_verdict`. `asha_live=true` AND `asha_live_kill=true` in every snapshot, and this
# watchdog stopped nothing, ever, because the tick below `continue`s at `sample is None` — the task
# prints `RECALL@100:` exactly ONCE, on the last line of a 5-10 hour training. A successive-halving
# watchdog needs a CURVE to halve and was handed a single point at the end. The 2026-08-07 audit
# (`docs/audit/2026-08-07-search-loop.md` F3) already named the fix — "make the engine emit one
# diagnostic at eval start naming why the gate is inert" — and twelve days later an operator reading
# the config still saw `asha_live: true` and concluded underperformers were being stopped.
#
# The shape is the training monitor's `kill_reachable: false` (`train_monitor.py`, 2026-08-19), not a
# second vocabulary: the same attribute name, the same meaning ("nothing here can be stopped"), on
# this watchdog's own span. Two differences follow from the two watchdogs being unlike:
#   • train_monitor opens a span every acting tick, so it can afford to restate the fact every time.
#     This one opens a span only on a rank TRANSITION, and an inert watchdog has no transitions — the
#     silence WAS the defect — so the statement has to open a span of its own, at most once per
#     distinct reason per eval.
#   • it also goes to `_LOG.warning`, because an operator "reading the config" is reading a console,
#     not a trace. That is the GPU-pool lease's precedent (`engine/resources.py`, a wait that "used to
#     be completely silent, which reads as a deadlock and repeatedly got debugged as one").
# It writes NOTHING else: no event, no fold input, no change to what may kill a node. See
# `asha_inert_reason` on why the rule is a pure function rather than an `if` inside the loop.

# A kill needs a live sample carrying a resource coordinate AND sibling curve points at the same rung
# (`sibling_metrics_at_resource`), so both of those readers' preconditions are properties of the
# METRIC CONTRACT and are decidable before the first tick — exactly like the training monitor's role
# gate, "a property of the resolved PIPELINE, not of the run's health". Each string says what is
# unreachable and what would have to change; an operator who reads one should not have to open this
# file to learn what to declare.
_ASHA_INERT_NO_LIVE_READ = (
    "this metric kind is never read from a live log, so the ASHA watchdog observes nothing at all "
    "(it reads stdout_json / stdout_regex only)")
_ASHA_INERT_NO_JSON = (
    "a stdout_regex metric can be ranked against finished siblings but never killed: a same-resource "
    "comparison needs stdout_json records carrying the metric and its resource in ONE object")
_ASHA_INERT_NO_RESOURCE_KEY = (
    "the metric spec declares no `resource_key`, so no live sample can ever be compared against a "
    "sibling at the same amount of training and the kill path is unreachable")
# The observational rung, and the one the corpus actually measured. Not decidable from the contract:
# the spec can be perfect and the training still print its objective once, at the end.
_ASHA_INERT_NO_CURVE = (
    "the training log is being written but has never printed the objective metric, so there is no "
    "curve to halve — the ASHA watchdog will observe nothing for the rest of this eval")

# How many CONSECUTIVE ticks with a non-empty tail and no parseable metric before the watchdog says
# the curve does not exist. Counted only on ticks whose log is TALKING: an empty tail is "the stage
# has not started writing yet", which is the stall watchdog's question, not this one. At the default
# 600 s cadence three such ticks is ~30 minutes of a training that is printing steadily and has never
# named its objective — long past any warm-up, and short against the 5-10 hour evals this fires on.
_ASHA_SILENT_TICKS = 3


def asha_inert_reason(metric_spec) -> Optional[str]:
    """Why this watchdog's KILL path is structurally unreachable for this metric contract, or None.
    Pure/deterministic — a truth table (`tests/test_asha_inert_is_visible.py`) rather than a
    condition reachable only through a simulated multi-hour eval, which is how the reachability rule
    stayed unstated for twelve days after it was first written down.

    Three rungs, worst first, because the first that holds is the one an operator needs told: a kind
    with no live reader at all (`latest_intermediate` returns None for every tick, so not even the
    advisory rank exists), a readable kind that `sibling_metrics_at_resource` will not accept, and a
    declaration that is simply missing. It reports only what the CONTRACT decides; `asha_live_kill`
    being off is an operator's own choice on their own config line and is not inertness.
    """
    if not isinstance(metric_spec, dict):
        return None                    # nothing declared to judge — the caller has no contract to read
    kind = metric_spec.get("kind", "stdout_json")
    if kind not in ("stdout_json", "stdout_regex"):
        return _ASHA_INERT_NO_LIVE_READ
    if kind != "stdout_json":
        return _ASHA_INERT_NO_JSON
    if _declared_resource_key(metric_spec) is None:
        return _ASHA_INERT_NO_RESOURCE_KEY
    return None


# The stop-decision schema the judge returns — the ASHA sibling of `train_monitor.TrainingVerdict`, and
# schema-validated the same way. Field descriptions are part of what the model sees: they ARE the
# decision contract. Only `stop` (at sufficient confidence) may end a run; `continue`/`watch` differ
# only in what the diagnostic row tells a human, never in what happens to the node this tick.
class AshaVerdict(BaseModel):
    status: Literal["continue", "watch", "stop"] = Field(
        description="continue = this run deserves its remaining compute (it is improving, or the gap to "
                    "its peers is small/noisy, or the peers themselves are widely spread); "
                    "watch = it looks behind but the evidence is not yet conclusive — keep training and "
                    "re-examine later; "
                    "stop = the run is clearly and durably worse than peers that had the SAME amount of "
                    "training and shows no sign of catching up, so its remaining budget is better spent "
                    "on another idea.")
    reason: str = Field(description="One short sentence naming the SPECIFIC evidence for the status "
                                    "(curve shape, gap size vs peer spread, other metrics, log health).")
    confidence: float = Field(default=0.5, description="Confidence in the status, 0.0 to 1.0.")


# The judge's framing. A contract, like `_MONITOR_SYSTEM`: it fixes the role (the engineer running the
# sweep), what the deterministic flag actually means (evidence, already spent — not an instruction), and
# the bias (stopping frees compute, but a wrong stop destroys a good experiment that is merely behind).
_ASHA_JUDGE_SYSTEM = (
    "You are the ML engineer running this experiment sweep, deciding whether a STILL-RUNNING training is "
    "worth the compute it has left. A deterministic rank test has already flagged this run as behind its "
    "finished siblings at the SAME amount of training, and has held that flag past a grace window — that "
    "is the evidence you are given, not the decision. Read the whole picture: a run that is behind but "
    "still improving, one whose gap is small against how widely the peers themselves are spread, or one "
    "whose other metrics look healthy, deserves to keep going. Reserve 'stop' for a run that is clearly "
    "and durably worse with no sign of catching up. Stopping frees a slot for a better idea, but a wrong "
    "stop throws away an experiment that was only slow to start — when in doubt choose 'watch', which "
    "keeps the run alive and re-examines it later. Judge only from the evidence given; the final metric "
    "is not known yet, so do not guess it. Be concise and specific about what you saw.")

# Spliced into the USER turn only when log tools are wired, immediately before the question — the same
# additive pattern `train_monitor._LOOK_INVITATION` uses, so `train_monitor_tools=false` leaves this
# judge's message byte-identical to what it has always been. It names THIS judge's own blind spot
# rather than reusing the training monitor's wording: the rank evidence is 12 mined numbers with no
# time axis and no log text at all, so "is it still improving" is precisely what it cannot see.
_ASHA_LOOK_INVITATION = (
    "\nYOU CAN LOOK. The curve above is at most 12 points mined from a short tail, and it does not "
    "show you WHEN they happened or what the log said around them. Use `metric_series` to see what "
    "this run's numbers have done over its whole life at a granularity you choose — a run that is "
    "behind but still descending is the exact case you are told to keep alive — and `read_log` to "
    "search for the errors, warnings or fallbacks that would explain the gap.")


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


def rank_bar(population: list[float], direction: str, *, quantile: float = 0.5) -> Optional[float]:
    """The BAR a live value is compared against: the member of `population` sitting at fraction
    `quantile` along a WORST->BEST ordering. None when there is nothing to compare against or the
    quantile is out of range/unparseable, so the caller treats "unknown" as "do not act".

    Extracted from `asha_underperforming` so the LLM judge can be TOLD the bar it is being asked about
    without a second (drifting) spelling of the ordering. Behaviour-identical to the inline version."""
    if not population:
        return None
    try:
        q = float(quantile)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= q <= 1.0):
        return None
    # Order WORST -> BEST so q places the bar at fraction q along that axis (q=0 -> worst peer, most
    # conservative; q=1 -> best peer, most aggressive). For min the worst final is the largest.
    ordered = sorted(population, reverse=(str(direction) == "min"))   # min: descending (worst first)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


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
    most aggressive). SMALLER quantile => more conservative (fewer stops).

    This remains the whole deterministic decision surface, but it is now the EVIDENCE handed to the LLM
    judge rather than the kill decision itself (see the module docstring)."""
    if value is None:
        return None
    bar = rank_bar(population, direction, quantile=quantile)
    if bar is None:
        return None
    return (value > bar) if str(direction) == "min" else (value < bar)


def latest_extra_metrics(log_tail: str, metric_spec: dict, *, max_items: int = 8) -> dict[str, float]:
    """The OTHER numeric fields the run printed alongside its newest objective-metric record — loss, lr,
    val_*, throughput, whatever the Developer's training loop logs. The quantile test throws all of this
    away; the judge gets it, because "behind on the objective but the loss is still falling" and "behind
    and the loss is flat" are different runs.

    Same contract as the eval's own `extra_metrics` (`core.models.normalize_extra_metrics`: finite scalar
    numbers only, bounded), mined from the same `stdout_json` records `latest_intermediate_sample` reads.
    `{}` for any other metric kind or when nothing parses — the judge simply gets LESS context, never
    wrong context. Pure; never raises."""
    if not log_tail or not isinstance(metric_spec, dict):
        return {}
    if metric_spec.get("kind", "stdout_json") != "stdout_json":
        return {}
    from looplab.core.models import normalize_extra_metrics

    metric_key = metric_spec.get("key", "metric")
    resource_key = _declared_resource_key(metric_spec)
    for obj in _json_objects_newest_first(log_tail):
        if metric_key not in obj:
            continue
        extras = {k: v for k, v in obj.items() if k != metric_key and k != resource_key}
        return normalize_extra_metrics(extras, max_items=max_items)
    return {}


def latest_train_verdict(rows, node_id: int, generation: int) -> Optional[dict]:
    """The training-health monitor's most recent verdict for exactly THIS node lifecycle, as a small
    sanitized `{"status", "reason", "confidence"}` — or None when it never spoke (monitor off, no client,
    still healthy-and-quiet) or its newest row is unusable.

    Read off the RAW event rows, never from `RunState`: `EV_TRAIN_MONITOR_ALERT` is a DIAGNOSTIC
    (fold-ignored) event, so the fold cannot supply it. Rows are untrusted append-only data — a bool
    `node_id`, a string generation or an unknown status must degrade to "no verdict", never mislead the
    judge about which lifecycle it is looking at. The lifecycle half of that is `last_lifecycle_row`,
    shared with both watchdogs' resume recoveries (doc 25 EC-04); the status/confidence half is this
    function's own, because only the judge cares what a verdict MEANS. The reason text was already
    redacted at its source (`_monitor_training` redacts before appending).
    Pure; safe on an empty/None row list."""
    from looplab.engine.train_monitor import last_lifecycle_row
    from looplab.events.types import EV_TRAIN_MONITOR_ALERT

    data = last_lifecycle_row(rows, EV_TRAIN_MONITOR_ALERT, node_id, generation)
    if data is None:
        return None
    status = str(data.get("status") or "").strip().lower()
    if status not in ("healthy", "watch", "broken"):
        return None                # newest row for this lifecycle is unusable -> no verdict, not a guess
    confidence, confidence_valid = _normalize_monitor_confidence(data.get("confidence"))
    out: dict = {"status": status, "reason": str(data.get("reason") or "")[:300]}
    if confidence_valid:
        out["confidence"] = confidence
    return out


def _fmt(value) -> str:
    """Compact fixed-significance rendering for prompt text (no numpy repr, no 17-digit float noise)."""
    number = _finite_number(value)
    return "?" if number is None else f"{number:.6g}"


def asha_judge_context(*, node_id: int, generation: int, direction: str, metric_key: str,
                       sample: IntermediateSample, live_curve, comparable_population: list[float],
                       quantile: float, under_streak: int, endpoint_population: list[float],
                       extra_metrics: Optional[dict] = None,
                       train_verdict: Optional[dict] = None,
                       grace_ticks: int = _ASHA_GRACE_TICKS, max_shown: int = 12) -> str:
    """Render the complete stop-decision EVIDENCE as the judge's prompt context. Pure/deterministic and
    unit-testable — no I/O, no state access, no LLM — so what the model is told can be asserted directly.

    Everything the deterministic test used, plus everything it discarded: the direction of the objective,
    the node's own live curve (per-rung, so its SHAPE is visible), the latest sample, the other metrics
    printed with it, the same-resource sibling values WITH the computed bar and how spread out they are,
    the finished endpoints as background, how long the flag has held against the grace window, and the
    training monitor's latest health verdict when one exists. Every population is rendered sorted and
    bounded to `max_shown` values so a 500-node run cannot blow up the prompt."""
    is_min = str(direction) == "min"
    goal = "MINIMIZE" if is_min else "MAXIMIZE"
    lines = [f"Experiment #{node_id} (attempt {generation}) is still training.",
             f"Objective: {goal} `{metric_key}` ({'lower' if is_min else 'higher'} is better)."]
    points = [entry for entry in (live_curve or [])
              if isinstance(entry, (list, tuple)) and len(entry) == 2]
    if points:
        resource_name = sample.resource_key or "resource"
        shown = points[-max_shown:]
        lines.append("This run's live curve so far ({} -> {}): {}{}.".format(
            resource_name, metric_key,
            "" if len(shown) == len(points) else f"... ({len(points)} points, last {len(shown)}) ",
            ", ".join(f"{_fmt(r)} -> {_fmt(m)}" for r, m in shown)))
    if sample.resource_key is not None and sample.resource is not None:
        lines.append(f"Latest sample: {metric_key}={_fmt(sample.value)} "
                     f"at {sample.resource_key}={_fmt(sample.resource)}.")
    else:
        lines.append(f"Latest sample: {metric_key}={_fmt(sample.value)}.")
    extras = {k: v for k, v in (extra_metrics or {}).items()}
    if extras:
        # NAMED as what they are. These come out of the live log the candidate's own training script
        # writes — the same channel `runtime/sandbox.py::json_line_extras` auto-captures at the
        # terminal, and the same trust story `metric_salvage.py` states one file over. The judge is
        # deciding what to DO next (docs/36's first row), so it is right that it sees them; it is
        # also right that it is told they are the experiment's self-report and not a measurement the
        # engine stands behind, since a stop decision reasoned off a printed number is a decision
        # the candidate influenced.
        lines.append("Other metrics the experiment printed alongside it "
                     "(self-reported by the training script, unverified): "
                     + ", ".join(f"{k}={_fmt(v)}" for k, v in list(extras.items())[:max_shown]) + ".")
    bar = rank_bar(comparable_population, direction, quantile=quantile)
    if comparable_population:
        ordered = sorted(comparable_population, reverse=is_min)      # WORST -> BEST, as the bar is placed
        head = ordered[:max_shown]
        lines.append(
            "Finished siblings observed at the SAME amount of training (n={}, worst to best{}): {}. "
            "The {:.0%} bar is {}, and this run's latest sample is on the WORSE side of it.".format(
                len(ordered), "" if len(head) == len(ordered) else f", first {len(head)} shown",
                ", ".join(_fmt(v) for v in head), quantile, _fmt(bar)))
    if endpoint_population:
        finals = sorted(endpoint_population, reverse=is_min)
        lines.append(
            "For background only, those siblings' FINAL metrics (n={}, worst to best): {}. A run this "
            "early is naturally below a finished endpoint — do not treat that gap as evidence.".format(
                len(finals), ", ".join(_fmt(v) for v in finals[:max_shown])))
    lines.append(
        f"The deterministic rank test has flagged this run for {under_streak} consecutive checks "
        f"(it only acts after more than {grace_ticks}).")
    if train_verdict:
        confidence = train_verdict.get("confidence")
        reason = str(train_verdict.get("reason") or "").strip()
        lines.append("The training-log health monitor's latest verdict for this run: {}{}{}.".format(
            train_verdict.get("status"),
            f" (confidence {_fmt(confidence)})" if confidence is not None else "",
            f' — "{reason[:300]}"' if reason else ""))
    return "\n".join(lines)


def should_asha_kill(verdict: Optional["AshaVerdict"], *, enabled: bool, threshold: float,
                     rank_underperforming: Optional[bool]) -> bool:
    """Whether the watchdog may tree-kill this node. Pure/deterministic, the ASHA sibling of
    `train_monitor.should_monitor_kill`.

    `rank_underperforming` is the DETERMINISTIC evidence bit (the same-resource quantile test). It is a
    required conjunct, so this predicate can never authorize a stop the old rank test would not have:
    the LLM narrows the stop set, it cannot widen it. On top of that the intervention must be `enabled`
    and the judge must have answered `stop` with a finite confidence >= `threshold` — a missing verdict
    (no client / endpoint failure / unparseable answer) is NOT a kill."""
    if not enabled or rank_underperforming is not True or verdict is None:
        return False
    if getattr(verdict, "status", None) != "stop":
        return False
    confidence, confidence_valid = _normalize_monitor_confidence(getattr(verdict, "confidence", None))
    try:
        bar = float(threshold)
    except (TypeError, ValueError):
        return False
    if not (bar == bar):               # a NaN threshold must fail CLOSED, not compare False and kill
        return False
    return confidence_valid and confidence >= bar


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

    def _state_asha_inert(self, node_id: int, generation: int, reason: str, **facts) -> None:
        """Say ONCE, on the surfaces an operator already reads, that this watchdog cannot act.

        The training monitor's `kill_reachable: false` in this watchdog's spelling — see the block
        above `asha_inert_reason` for why the two sites differ in WHEN they fire and not in what they
        mean. Best-effort by construction: an inert watchdog failing to announce its inertness must
        never end a node, so a tracer/logging hiccup is swallowed exactly like a per-tick one.

        It is not a widening. Nothing here reads a model, nothing reaches the fold, and the kill
        conjuncts in `should_asha_kill` are untouched — this only makes an existing refusal legible.
        """
        try:
            with self.tracer.span("asha_monitor", node_id=node_id) as sp:
                sp.set_many(generation=generation, kill_reachable=False, inert_reason=reason, **facts)
            _LOG.warning("node %s: the ASHA early-stop watchdog is inert for this eval — %s",
                         node_id, reason)
        except Exception:  # noqa: BLE001 — a statement about inertness is never worth a raise
            pass

    def _asha_verdict(self, context: str, tools=None) -> Optional[AshaVerdict]:
        """One-shot LLM stop decision over the rank evidence (SYNC — the caller runs it in a worker
        thread), mirroring `train_monitor._training_verdict`. Uses the Developer's client (it wrote the
        training loop, so it knows what this curve should look like) through the SHARED judge-call
        contract (`trust/judge.py::structured_judge`, the same one both trust verifiers use) with a
        fresh, STATELESS structured call — it never mutates the shared role object, so it is safe to fire
        while the eval thread runs. The client records its own usage/cost.

        Returns None when there is no client (offline / toy path) or the model output cannot be parsed.
        Unlike the training monitor's advisory verdict, None here is LOAD-BEARING: `should_asha_kill`
        treats it as "do not stop", so a broken/absent judge can only ever spare a node.

        `tools` (`tools/log_tools.py`, built by the SHARED `train_monitor.monitor_log_tools` so both
        watchdogs honour one switch) is the fix for this judge's own pre-chewed slice, and it is a
        DIFFERENT slice from the training monitor's: this judge never sees a byte of log text at all.
        `asha_judge_context` hands it 12 curve points mined from a 128 KiB tail, ≤8 extra metrics and a
        300-char echo of the training monitor's reason — so the question "is this curve actually still
        improving, or did it plateau an hour ago" is asked of twelve numbers whose spacing it cannot
        see. `structured_judge` already took `tools`; it was simply never given any, so this is the
        argument that was missing rather than a new mechanism, and `tools=None` is the historical call.
        """
        client = getattr(getattr(self, "developer", None), "client", None)
        if client is None:
            return None
        messages = [
            {"role": "system", "content": _ASHA_JUDGE_SYSTEM},
            {"role": "user", "content": context
             + (("\n\n" + _ASHA_LOOK_INVITATION) if tools is not None else "")
             + "\n\nShould this still-running experiment continue, be watched, or be stopped?"},
        ]
        try:
            from looplab.trust.judge import structured_judge
            return structured_judge(client, messages, AshaVerdict, parser="tool_call",
                                    tools=tools, max_turns=_MONITOR_LOOK_TURNS)
        except Exception:  # noqa: BLE001 — a parser/endpoint failure means "no verdict", not a crash
            return None

    @in_llm_lane("enrichment")
    async def _monitor_asha(self, node_id: int, generation: int, workdir, cancel,
                            metric_spec: dict, direction: str,
                            kill_signal: Optional[dict] = None, log_snapshot=None,
                            log_plan=None) -> None:
        """Tail the live log on a timer, extract the intermediate objective metric, rank it against the
        completed sibling endpoints for diagnostics, and record advisory `EV_ASHA_RANK` transitions.
        Opt-in tree-kill requires persistent underperformance against enough sibling observations at the
        same declared resource AND a confident `stop` from the LLM judge, which sees that evidence plus
        the live curve, the run's other metrics and the training monitor's latest health verdict. Exits
        with the eval; a per-tick hiccup skips only that tick.

        `log_plan` (from `train_monitor.eval_log_plan`) confines the read to the stage logs that carry
        the LIVE curve: the dep install writes no metrics, and the final scorer's log carries the run's
        FINAL value, which is not an intermediate and must never be ranked against sibling mid-training
        checkpoints. That scorer is identified STRUCTURALLY (the last stage of the pipeline — where
        `command_eval` reads the metric), not by the name `score`, so an operator pipeline whose final
        stage is spelled `Score` is excluded here too; the plan's advisory/kill split
        (`LOG_ROLE_WORK` vs `LOG_ROLE_TRAINING`) is the TRAINING monitor's concern and does not narrow
        what this watchdog may read — a work stage is exactly where the live curve is printed."""
        # Local: `evaluate` imports this module, so a module-level import would be a cycle.
        from looplab.engine.evaluate import _watch_limiter
        import anyio

        from looplab.engine.train_monitor import (
            claim_watchdog_kill, last_lifecycle_row, read_training_tail_raw)
        from looplab.events.replay import fold
        from looplab.events.types import DIAGNOSTIC_EVENTS, EV_ASHA_RANK, EV_ASHA_VERDICT

        base = self._asha_cadence()
        # Read the configured values WITHOUT `or`-coercion, so a legitimate quantile=0.0 (most
        # conservative) is honoured rather than silently reset to the 0.5 default.
        _q = getattr(self, "_asha_live_quantile", 0.5)
        quantile = float(_q) if isinstance(_q, (int, float)) and not isinstance(_q, bool) else 0.5
        _ms = getattr(self, "_asha_live_min_siblings", 3)
        min_siblings = max(1, int(_ms)) if isinstance(_ms, (int, float)) and not isinstance(_ms, bool) else 3
        # Same no-`or`-coercion rule for the judge's confidence bar: `x or 0.0` would turn an unset/None
        # knob into a ZERO threshold — i.e. every 'stop' acts — which is the wrong direction to fail in.
        _kc = getattr(self, "_asha_live_kill_confidence", 0.8)
        kill_confidence = float(_kc) if isinstance(_kc, (int, float)) and not isinstance(_kc, bool) else 0.8
        last_flag: Optional[tuple[bool, Optional[bool]]] = None
        # preserve an open episode across process re-entry; otherwise an initially healthy
        # resumed curve looks like a first observation and never emits the recovery edge. Modern rows
        # retain endpoint/resource truth separately; legacy rows safely map their single bit to endpoint.
        try:
            prior_rows = await anyio.to_thread.run_sync(
                self.store.read_all, limiter=_watch_limiter())
            prior = last_lifecycle_row(prior_rows, EV_ASHA_RANK, node_id, generation)
            if prior is not None:
                endpoint = prior.get("endpoint_underperforming")
                resource = prior.get("resource_underperforming")
                if (isinstance(endpoint, bool)
                        and (resource is None or isinstance(resource, bool))):
                    last_flag = (endpoint, resource)
                else:
                    raw_flag = prior.get("underperforming", True)
                    last_flag = (raw_flag, None) if isinstance(raw_flag, bool) else None
        except Exception:  # noqa: BLE001 - advisory history lookup; the live monitor still proceeds
            pass

        def _observe():
            """ONE read of the log per tick, in the worker thread: the folded state the rank test needs
            plus the training monitor's latest verdict for this lifecycle (a DIAGNOSTIC row the fold
            cannot carry). Reading both from the SAME rows keeps the judge's health context consistent
            with the population it is judging, and costs one extra reverse scan over a list the fold
            already walks — cheaper than a second read_all at decision time."""
            rows = self.store.read_all()
            return fold(rows), latest_train_verdict(rows, node_id, generation)

        under_streak = 0
        judge_calls = 0
        # WHAT THIS WATCHDOG CANNOT DO, said once per distinct reason (see `asha_inert_reason`). The
        # structural reason is known before the first read, so it is stated on the first tick rather
        # than after the hours it takes for a rank transition that will never come; the observational
        # one can only be earned by ticking. `stated` bounds both to one statement each per eval.
        stated: set[str] = set()
        structural_inert = asha_inert_reason(metric_spec)
        silent_ticks = 0
        while True:
            await anyio.sleep(base)
            if cancel.is_set():
                return
            if structural_inert is not None and structural_inert not in stated:
                stated.add(structural_inert)
                self._state_asha_inert(node_id, generation, structural_inert,
                                       metric_kind=str(metric_spec.get("kind", "stdout_json"))
                                       if isinstance(metric_spec, dict) else "")
            try:
                tail = await anyio.to_thread.run_sync(
                    lambda: read_training_tail_raw(workdir, snapshot=log_snapshot, plan=log_plan),
                    limiter=_watch_limiter())
                sample = latest_intermediate_sample(tail, workdir, metric_spec)
                if sample is None:
                    # THE MEASURED DEFECT: this `continue` ran on every tick of every run in the
                    # corpus and left no trace whatsoever. A tick whose log is TALKING and still names
                    # no objective is evidence the metric is printed once at the end; an EMPTY tail is
                    # only "nothing written yet" and is the stall watchdog's business, so it does not
                    # count toward the streak.
                    if tail:
                        silent_ticks += 1
                        if (silent_ticks >= _ASHA_SILENT_TICKS
                                and _ASHA_INERT_NO_CURVE not in stated):
                            stated.add(_ASHA_INERT_NO_CURVE)
                            self._state_asha_inert(node_id, generation, _ASHA_INERT_NO_CURVE,
                                                   silent_ticks=silent_ticks)
                    continue
                silent_ticks = 0
                value = sample.value
                state, train_verdict = await anyio.to_thread.run_sync(
                    _observe, limiter=_watch_limiter())
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
                    # Dedup state moves only AFTER the edge is durably published, like the sibling
                    # train monitor's `last_event_status`. Committing it first meant a raising append
                    # (or tracer span) — swallowed by the per-tick handler — permanently deduped the
                    # transition away: the warning/recovery edge this block exists to publish was lost
                    # until the NEXT verdict change, leaving projections and Attention on a stale flag.
                    last_flag = diagnostic_key
                # OPT-IN kill — independent of the advisory dedup: reached once the underperformance has
                # PERSISTED past the grace window (a transient early dip that recovers resets the streak
                # to 0 and is never considered). Reuses the monitor's kill_signal + cancel; `_evaluate`
                # writes the single terminal (reason=asha_underperforming).
                if (kill_signal is not None and comparable_under is True
                        and under_streak > _ASHA_GRACE_TICKS
                        and getattr(self, "_asha_live_kill", False)):
                    # never promote a finished endpoint into a fake peer at the live
                    # resource. Without explicit same-resource curve evidence this is unreachable.
                    #
                    # THE RANK TEST HAS FIRED — and that is where its authority ends. The whole safety
                    # envelope (grace window, min_siblings, explicit resource_key, asha_live_kill) has
                    # already held, so everything below can only ever DECLINE to stop a node the old code
                    # would have stopped; it can never reach a node the rank test spared. The judge sees
                    # what the quantile comparison threw away — the curve's shape, the peer spread, the
                    # other metrics, the training monitor's health verdict — and decides.
                    verdict, kill, stop_decided = None, False, False
                    with self.tracer.span("asha_judge", node_id=node_id) as sp:
                        sp.set_many(generation=generation, intermediate=round(value, 6),
                                    quantile=round(quantile, 3), under_streak=under_streak,
                                    comparable_population=len(comparable_population),
                                    train_status=str((train_verdict or {}).get("status") or ""))
                        if judge_calls >= _MAX_ASHA_JUDGE_CALLS:
                            # Backstop only (see _MAX_ASHA_JUDGE_CALLS). Surfaced on the span because a
                            # silent cap would read as "the judge keeps sparing this node" when in fact
                            # nobody is asking any more.
                            sp.set("llm_capped", True)
                        elif cancel.is_set():
                            # The node is already ending — the eval finished, an operator aborted, or the
                            # SIBLING training monitor claimed the kill while this tick was reading the
                            # log and folding. `cancel` was only re-checked at the top of the loop, so a
                            # loser could still start a judge call here; because the call is deliberately
                            # un-abandonable below, `_evaluate`'s task-group cancel then had to WAIT a
                            # whole endpoint timeout for a verdict about a dead node — and pay for it.
                            # Node teardown was bounded by the endpoint timeout twice over, once per
                            # watchdog. Nothing is decided or recorded on this tick.
                            sp.set("cancelled_before_call", True)
                            return
                        else:
                            context = asha_judge_context(
                                node_id=node_id, generation=generation, direction=direction,
                                metric_key=str(metric_spec.get("key", "metric")), sample=sample,
                                live_curve=extract_resource_curve(tail, metric_spec),
                                comparable_population=comparable_population, quantile=quantile,
                                under_streak=under_streak, endpoint_population=population,
                                extra_metrics=latest_extra_metrics(tail, metric_spec),
                                train_verdict=train_verdict)
                            # the verdict decides whether to spend the rest of a possibly
                            # multi-hour budget, and its client usage is billable against shared run
                            # state. Join an in-flight call on eval cancellation (abandon_on_cancel=False)
                            # so no detached worker can emit cost after the node/run has finalized.
                            def _judge():
                                """The paid call AND the source derivation it needs, both in the worker.

                                `monitor_log_tools` is FILESYSTEM work — it globs the workdir for
                                `*.log`, opens + fstats every stage log the plan names, and
                                `attempt_byte_floor` probe-READS each one. As an ARGUMENT to
                                `run_sync` it was evaluated on the EVENT-LOOP thread, which is the
                                one place in this tick that pays for a slow mount: on the geesefs/S3
                                mounts `runs/` lives on, an `lstat` of a file that is NOT there
                                costs 105-950 ms (`core/fence.py::_warm_directory_lookup` measured
                                it), so one blocking derivation per tick per running eval stalls the
                                whole engine loop. The identical defect and the identical fix as
                                `train_monitor.py`'s `_judge`, and it has to be stated twice because
                                the two watchdogs build their tools at their own call sites. Still
                                built PER TICK, for the reason it always was: the log the judge
                                would read is being written while it thinks.
                                """
                                return self._asha_verdict(
                                    context, monitor_log_tools(self, workdir, log_plan, log_snapshot))

                            verdict = await anyio.to_thread.run_sync(
                                _judge, abandon_on_cancel=False)
                            judge_calls += 1
                        conf, confidence_valid = _normalize_monitor_confidence(
                            getattr(verdict, "confidence", None))
                        # LLM text derived from the run's own log — redact it before it lands in the
                        # trace / event log, exactly as the training monitor's reason is redacted.
                        _redact = getattr(self, "_redact", None)
                        reason = (getattr(verdict, "reason", "") or "")
                        reason = (_redact(reason) if callable(_redact) else reason)[:300]
                        # "unavailable" is NOT one of the model's three statuses: it records that nobody
                        # answered (no client / endpoint failure / cap), which is why the node lives on.
                        status = getattr(verdict, "status", None) or "unavailable"
                        stop_decided = should_asha_kill(
                            verdict, enabled=getattr(self, "_asha_live_kill", False),
                            threshold=kill_confidence, rank_underperforming=comparable_under)
                        # CLAIM BEFORE RECORDING, so the durable row can state what happened to the NODE
                        # rather than what this watchdog wanted. The claim used to run after the append,
                        # with its return value discarded: when the sibling training monitor won the race
                        # the terminal said `monitor_broken` while an `asha_verdict{kill:true}` row sat
                        # beside it, and any audit of "which watchdog stopped what" over-counted ASHA.
                        # The guard->update inside `claim_watchdog_kill` is await-free, so `kill` below
                        # is the exact outcome, not a prediction.
                        kill = stop_decided and claim_watchdog_kill(
                            kill_signal, cancel,
                            reason=(
                                (reason + " — " if reason else "")
                                + f"intermediate metric {value:.4g} at "
                                f"{sample.resource_key}={sample.resource:g} is worse than the "
                                f"{quantile:.0%} bar of {len(comparable_population)} sibling "
                                "observations at the same resource"
                            )[:400],
                            terminal_reason="asha_underperforming",
                            confidence=round(conf, 3))
                        sp.set_many(status=status, confidence=round(conf, 3), kill=kill,
                                    reason=reason[:200])
                        if stop_decided and not kill:
                            sp.set("kill_superseded", True)
                        if not confidence_valid:
                            sp.set("confidence_valid", False)
                        # DURABLE record of the decision (fold-IGNORED, like EV_ASHA_RANK): the rank rows
                        # say a node was behind, this says what was decided about it and why — including
                        # the far more common "the rank test fired and the judge kept the run alive",
                        # which otherwise leaves no trace at all.
                        assert EV_ASHA_VERDICT in DIAGNOSTIC_EVENTS
                        # TWO separate bits, because they answer different questions and CAN differ:
                        #   `stop_decided` — what this watchdog decided (the judge's confident `stop`);
                        #   `kill`         — whether it then WON the shared per-eval claim, i.e. whether
                        #                    its reason is the one `_evaluate` will terminalize with.
                        # Neither is "the node stopped": `_evaluate` converts a claim into a terminal
                        # only `if kill_signal.get("kill") and not ok`, so a kill claimed against an eval
                        # that had ALREADY produced a usable result still ends in `node_evaluated`. That
                        # outcome is not knowable here (it is decided after this task is cancelled), so
                        # the row records what it can prove and the node's single terminal stays the
                        # authority on what happened — see the EV_ASHA_VERDICT note in `events/types.py`.
                        decision = {
                            "node_id": node_id, "generation": generation, "status": status,
                            "reason": reason, "confidence": round(conf, 3),
                            "stop_decided": bool(stop_decided), "kill": kill,
                            "intermediate": round(value, 6), "quantile": round(quantile, 3),
                            "direction": str(direction), "under_streak": under_streak,
                            "comparable_population": len(comparable_population),
                        }
                        if not confidence_valid:
                            decision["confidence_valid"] = False
                        if stop_decided and not kill:
                            # The judge said stop and the claim LOST: the sibling watchdog owns the
                            # terminal. Naming its reason means attribution needs no guesswork.
                            decision["kill_superseded_by"] = str(
                                kill_signal.get("terminal_reason") or "")[:64]
                        if sample.resource_key is not None and sample.resource is not None:
                            decision.update({"resource_key": sample.resource_key,
                                             "resource": sample.resource})
                        if train_verdict:
                            decision["train_monitor_status"] = train_verdict.get("status")
                        # A decided stop means cancellation is already in flight (this claim or the
                        # sibling's), and `self._write_lock` is the next cancellation checkpoint — an
                        # unshielded append would be preempted and the ONLY durable record of the
                        # decision lost. Bounded (one append); same shielding `_evaluate` uses for its
                        # own promised terminal. A spared verdict keeps the plain best-effort append.
                        with anyio.CancelScope(shield=stop_decided):
                            async with self._write_lock:
                                self.store.append(EV_ASHA_VERDICT, decision)
                    if stop_decided:
                        return              # won or lost, this node is ending — stop watching it
                    # SPARED: re-arm the whole grace window instead of re-judging on the very next tick.
                    # The rank flag persists for as long as the node is behind, so without this the judge
                    # would be asked again every cadence — paying for the same question and pressing a
                    # 'stop' out of a borderline curve by sheer repetition. A node the judge kept alive
                    # must earn the next consult with another full streak of underperforming ticks.
                    under_streak = 0
            except anyio.get_cancelled_exc_class():
                raise                           # cooperative cancellation — must propagate
            except Exception:  # noqa: BLE001 — a transient per-tick hiccup skips this tick, never disables
                continue
