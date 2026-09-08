"""The ASHA RUNG-CURVE instrument: did any run ever produce a curve this watchdog could halve?

`engine/asha_monitor.py` is a successive-halving watchdog, and successive halving needs a CURVE — a
sequence of intermediate objective observations at increasing amounts of training — plus SIBLINGS
observed at the same rung to rank against. The corpus measurement behind the deferral
(docs/BACKLOG.md §0.15, re-derived 2026-08-19) is that it never got one: `asha_rank` and
`asha_verdict` have zero rows corpus-wide while `asha_live` and `asha_live_kill` were true in every
snapshot, because the task prints its objective exactly ONCE, on the last line of a 5-10 hour
training. A watchdog configured to stop underperformers was handed a single point at the end.

This module READS that question off a finished run's own record, so "does ASHA have a curve to
halve on this task family yet?" is answerable by running a command instead of by re-deriving it.
It builds nothing, arms nothing and changes no gate: `Settings.asha_live_kill`, `resource_key` and
the min-siblings floor stay exactly where they are.

THE TWO RUNGS IT REPORTS ARE THE WATCHDOG'S OWN, in the watchdog's order:

* the CONTRACT rung — `asha_monitor.py::asha_inert_reason` decides, from the metric spec alone and
  before the first tick, whether a kill is even reachable (no live-readable kind, no stdout_json, no
  declared `resource_key`). The reason string is written where it is DECIDED: `_state_asha_inert`
  stamps it on an `asha_monitor` span as `inert_reason` + `kill_reachable=false`. This module reads
  that receipt rather than re-deriving the rule, so there is never a second spelling of it to drift.
* the OBSERVATIONAL rung — whether the training ever printed the objective more than once at
  distinct resource coordinates. `CURVE_MIN_POINTS` distinct resource values for one node is a
  curve; the same value seen for more than one node is a comparable RUNG, which is what the kill's
  `asha_live_min_siblings` floor is counted over.

WHAT ITS SILENCE MAY AND MAY NOT MEAN, stated because this is an instrument about an ABSENCE. The
samples it can see are the ones the watchdog PUBLISHED: an `asha_monitor` span opens on a verdict
CHANGE or to announce inertness, and `asha_rank` rows are appended only on a warning/recovery edge.
So zero observed samples is consistent with "the curve existed and never changed the verdict" as
well as with "there was no curve", and the report says which of its evidence is present rather than
concluding from emptiness. An `inert_reason` is the strong evidence — the engine said the kill was
unreachable before it looked at any log — and a run with no `spans.jsonl` at all is reported as
UNREADABLE, never as a run without a curve.

Pure and deterministic: it takes rows the caller read (events, spans, and the launch settings the
caller lifted off `config.snapshot.json`), does no I/O of its own, and imports only `core` +
`events.types`.
"""
from __future__ import annotations

from typing import Iterable, Optional

from looplab.core.run_identity import run_ref
from looplab.events.types import (EV_ASHA_RANK, EV_ASHA_VERDICT, EV_NODE_EVALUATED, EV_NODE_FAILED)

# How many DISTINCT resource coordinates one node must show before the observations are a curve
# rather than a point. Two is the floor for a shape at all; the kill additionally wants
# `asha_live_min_siblings` (shipped default 3) finished peers at the SAME coordinate, which is
# reported beside it as `max_rung_siblings` rather than folded into one verdict.
CURVE_MIN_POINTS = 2

# The node terminal reason the watchdog's own kill writes (`engine/evaluate.py` writes the single
# terminal; replay reads that and never re-invokes the judge). A row carrying it is the only proof
# the kill path ever completed.
ASHA_KILL_REASON = "asha_underperforming"

ASHA_CURVE_RULE = (
    "a CURVE is >= 2 distinct resource coordinates observed for one node; a comparable RUNG is one "
    "coordinate observed for more than one node. Observations are the ones the watchdog PUBLISHED "
    "(an `asha_monitor` span on a verdict change or an inertness announcement, an `asha_rank` row "
    "on a warning/recovery edge), so an empty reading is not proof of an absent curve — an "
    "`inert_reason` is the engine's own statement that the kill was unreachable.")

_MONITOR_SPAN = "asha_monitor"
_JUDGE_SPAN = "asha_judge"
_RUNGS_SHOWN = 12
_CURVE_NODES_SHOWN = 12
# The launch settings that decide whether an operator BELIEVED underperformers were being stopped —
# the contradiction the backlog entry is pointed at ("`asha_live: true` in every snapshot, and this
# watchdog stopped nothing, ever"). Reported verbatim, never interpreted.
ASHA_SETTINGS_KEYS = ("asha_live", "asha_live_kill", "asha_live_min_siblings")


def _number(value) -> Optional[float]:
    """A finite float, or None — a bool is NOT a resource coordinate."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _node_key(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def asha_run_curve(label: str, *, state=None, events: Iterable = (), spans: Optional[Iterable] = None,
                   settings: Optional[dict] = None) -> dict:
    """One run's reading. `spans=None` means `spans.jsonl` could not be read — not that it was empty.

    `events` are `Event` objects (or `(type, data)` pairs, as `events/prior_citations.py` accepts);
    `spans` are the raw `spans.jsonl` rows; `settings` is the launch config snapshot, from which
    only `ASHA_SETTINGS_KEYS` are carried through.
    """
    samples: dict[int, set[float]] = {}
    resource_keys: set[str] = set()
    inert: dict[str, int] = {}
    monitor_ticks = judge_spans = rank_rows = verdict_rows = kills = evals = 0

    for span in (spans or ()):
        if not isinstance(span, dict):
            continue
        name = span.get("name")
        attrs = span.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        if name == _JUDGE_SPAN:
            judge_spans += 1
            continue
        if name != _MONITOR_SPAN:
            continue
        reason = attrs.get("inert_reason")
        if isinstance(reason, str) and reason:
            # The receipt written where the decision is made. Counted per span: the watchdog says it
            # ONCE per eval, so the count is "how many evals were told they could not act".
            inert[reason] = inert.get(reason, 0) + 1
        if _number(attrs.get("intermediate")) is not None:
            monitor_ticks += 1
        node, resource = _node_key(attrs.get("node_id")), _number(attrs.get("resource"))
        if node is not None and resource is not None:
            samples.setdefault(node, set()).add(resource)
        key = attrs.get("resource_key")
        if isinstance(key, str) and key:
            resource_keys.add(key)

    for ev in events:
        etype = getattr(ev, "type", None) if not isinstance(ev, tuple) else ev[0]
        data = getattr(ev, "data", None) if not isinstance(ev, tuple) else ev[1]
        if not isinstance(data, dict):
            continue
        if etype == EV_ASHA_RANK:
            rank_rows += 1
            node, resource = _node_key(data.get("node_id")), _number(data.get("resource"))
            if node is not None and resource is not None:
                samples.setdefault(node, set()).add(resource)
            key = data.get("resource_key")
            if isinstance(key, str) and key:
                resource_keys.add(key)
        elif etype == EV_ASHA_VERDICT:
            verdict_rows += 1
        elif etype in (EV_NODE_EVALUATED, EV_NODE_FAILED):
            evals += 1
            if str(data.get("reason") or "") == ASHA_KILL_REASON:
                kills += 1

    # A coordinate seen for MORE THAN ONE node is the comparable population the kill ranks against.
    nodes_at_rung: dict[float, set[int]] = {}
    for node, values in samples.items():
        for value in values:
            nodes_at_rung.setdefault(value, set()).add(node)
    curve_nodes = sorted(({"node": node, "points": len(values),
                           "rungs": sorted(values)[:_RUNGS_SHOWN]}
                          for node, values in samples.items() if len(values) >= CURVE_MIN_POINTS),
                         key=lambda row: (-row["points"], row["node"]))
    ref = run_ref(getattr(state, "run_uid", "") or "", getattr(state, "run_id", "") or str(label))
    launch = {k: (settings or {}).get(k) for k in ASHA_SETTINGS_KEYS
              if isinstance(settings, dict) and k in settings}
    return {
        "run": str(label), "run_ref": ref,
        "spans_available": spans is not None,
        "settings": launch,
        "evals": evals, "monitor_ticks": monitor_ticks, "judge_spans": judge_spans,
        "rank_rows": rank_rows, "verdict_rows": verdict_rows, "kills": kills,
        "inert_reasons": dict(sorted(inert.items())),
        "resource_keys": sorted(resource_keys),
        "nodes_with_samples": len(samples),
        "rungs": sorted(nodes_at_rung)[:_RUNGS_SHOWN],
        "curve_nodes": curve_nodes[:_CURVE_NODES_SHOWN],
        "curve_node_count": len(curve_nodes),
        "max_rung_siblings": max((len(nodes) for nodes in nodes_at_rung.values()), default=0),
    }


def asha_curve_report(rows: Iterable[dict]) -> dict:
    """Aggregate the per-run readings into the corpus answer, deciding nothing beyond the counts."""
    rows = list(rows)
    inert: dict[str, int] = {}
    for row in rows:
        for reason, count in (row.get("inert_reasons") or {}).items():
            inert[reason] = inert.get(reason, 0) + count
    with_curve = [row for row in rows if row.get("curve_node_count")]
    return {
        "rule": ASHA_CURVE_RULE,
        "curve_min_points": CURVE_MIN_POINTS,
        "runs": len(rows),
        "runs_unreadable_spans": sum(1 for row in rows if not row.get("spans_available")),
        "runs_with_curve": len(with_curve),
        # THE ANSWER THE MARKER ASKS FOR, and the only claim this module makes: whether ANY run in
        # the corpus produced a curve at all. What to do about it — declare a `resource_key`, emit a
        # subsampled intermediate eval, leave the watchdog advisory — is not decided here.
        "curve_found": bool(with_curve),
        "curve_nodes": sum(int(row.get("curve_node_count") or 0) for row in rows),
        "max_rung_siblings": max((int(row.get("max_rung_siblings") or 0) for row in rows), default=0),
        "kills": sum(int(row.get("kills") or 0) for row in rows),
        "rank_rows": sum(int(row.get("rank_rows") or 0) for row in rows),
        "verdict_rows": sum(int(row.get("verdict_rows") or 0) for row in rows),
        "monitor_ticks": sum(int(row.get("monitor_ticks") or 0) for row in rows),
        "evals": sum(int(row.get("evals") or 0) for row in rows),
        "inert_reasons": dict(sorted(inert.items())),
        "runs_detail": rows,
    }
