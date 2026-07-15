"""Action-space lock-in detector (PART IV D7, §21.8/§21.10 — Phase 1a, offline analytic).

**Why this exists.** The deepest cause of the `rubertlite` collapse: with full freedom to edit any file,
the agent only ever touched ONE subsystem (`loss.py`). node_48's champion was expanded 19 times, 14 of
its 17 children DCL-lineage, the longest consecutive same-lever streak = 12 (§21.10). The Strategist's
flat coverage signal never saw it (it read the run as *increasingly diverse*, §21.10). What was missing
is a signal that fires on **staying inside one region of the action space** — independent of whether the
metric is still improving.

**What this is.** A pure, deterministic read over the 0a concept graph: it maps each experiment to the
axis-region(s) of the concept DAG it touches (the "lever" / subsystem proxy — a D5 branch, §21.11's
"different D5 branch"), then finds the longest run of CONSECUTIVE experiments confined to one axis. A
"≥N consecutive same-lever" detector fires early: on the `rubertlite` replay it trips at ~node_29 with
~38 same-lever nodes still to come (§21.10). It also reports how concentrated the *recent* window is on
a single axis (the "narrowing NOW" flavour) — both from the graph, so they are legible per D5 branch, not
just "the metric plateaued".

**Discipline.** Offline analytic (early lane, §6.6): pure and deterministic over `(RunState, ConceptGraph,
tags)` — no I/O, no LLM, no wall-clock — so a replay recomputes it byte-identically. It reads the concept
tags (deterministic heuristic by default, or the LLM tags 0a produced); it writes nothing and never
touches selection. Wiring the alarm into the live Strategist pivot / a forced-jump operator is Phase 2
(2a/2b) and inherits the R-gates.
"""
from __future__ import annotations

from typing import Optional

from looplab.core.models import RunState
from looplab.search.concept_graph import ConceptGraph, _experiment_nodes, tag_nodes_heuristic


def _node_axes(graph: ConceptGraph, cids) -> frozenset[str]:
    """The set of axis-regions (D5 branches) an experiment's concept tags touch."""
    out: set[str] = set()
    for cid in cids:
        out.update(graph.axes_of(cid))
    return frozenset(out)


def lock_in_signal(state: RunState, graph: ConceptGraph,
                   tags: Optional[dict[int, frozenset[str]]] = None, *,
                   streak_threshold: int = 5, recent: int = 8) -> dict:
    """The action-space lock-in read-model (§21.8). Pure and deterministic; an empty/untagged run yields
    a not-fired signal. When `tags` is omitted the deterministic heuristic tagger is used (no LLM).

    A node is "on axis A" when any of its concept tags sits under axis A. The **lock-in streak** for an
    axis is the longest run of CONSECUTIVE experiments (by node id) that are each on that axis; the
    signal's streak is the max over axes. Untagged experiments break every streak (effort not localized
    to a region can't extend a same-lever run).

    Keys:
      experiments      - idea-carrying nodes (the run's experiments)
      tagged           - experiments with >=1 concept tag
      locked_axis      - the axis with the longest consecutive same-lever streak (None if none)
      streak           - length of that longest consecutive run
      streak_start_node- the node id where that run begins (so "fires at node_29" is legible)
      fired            - streak >= streak_threshold
      recent_axis      - the axis most of the last `recent` experiments touch (None if none)
      recent_frac      - in [0,1], share of the last `recent` experiments on `recent_axis` (narrowing NOW)
      current_streak   - the run's same-lever streak ENDING at the latest experiment (0 if the last is
                         untagged) — how locked-in the search is right now
    """
    nodes = _experiment_nodes(state)
    if tags is None:
        tags = tag_nodes_heuristic(state, graph)
    n = len(nodes)
    tagged = sum(1 for nd in nodes if tags.get(nd.id))
    base = {"experiments": n, "tagged": tagged, "locked_axis": None, "streak": 0,
            "streak_start_node": None, "fired": False, "recent_axis": None,
            "recent_frac": 0.0, "current_streak": 0}
    if n == 0:
        return base

    # per-experiment axis set, in id order
    axis_sets = [_node_axes(graph, tags.get(nd.id, frozenset())) for nd in nodes]
    all_axes = sorted({a for s in axis_sets for a in s})

    # Longest consecutive run per axis. Deterministic tie-break: longer wins; on equal length the run
    # that STARTS EARLIER wins; on an equal start the smaller axis name wins (axes iterated sorted, and
    # the replace test is STRICT on an earlier start, so the first/smaller axis already set is kept).
    best = {"axis": None, "len": 0, "start_idx": None}
    for ax in all_axes:
        run = 0
        run_start = None
        for i, s in enumerate(axis_sets):
            # KNOWN LIMITATION (intentional, advisory-only): the streak is axis-MEMBERSHIP based. Because
            # `_node_axes` expands each tag up its ancestor chain, a single-subsystem node legitimately
            # carries several axis labels, so multi-membership does NOT by itself mean multi-branch
            # exploration — and conversely a genuinely multi-branch node contributes to more than one axis'
            # streak. This reads as "how long SOME subsystem stayed continuously in play" — a deliberate
            # over-estimate for a hint. Distinguishing one ancestor-chain from several independent branches
            # needs the DAG parent map here; not worth it for a diagnostic that NEVER touches selection.
            if ax in s:
                if run == 0:
                    run_start = i
                run += 1
                if run > best["len"] or (run == best["len"]
                                         and best["start_idx"] is not None and run_start < best["start_idx"]):
                    best = {"axis": ax, "len": run, "start_idx": run_start}
            else:
                run = 0
                run_start = None

    # current streak: the same-lever run ending at the LAST experiment (which axis it is on, extended back)
    current = 0
    if axis_sets and axis_sets[-1]:
        # pick the axis of the last node that extends furthest back
        for ax in sorted(axis_sets[-1]):
            c = 0
            for s in reversed(axis_sets):
                if ax in s:
                    c += 1
                else:
                    break
            current = max(current, c)

    # recent-window concentration on one axis (deterministic tie-break: most experiments, then smallest
    # axis name — Counter.most_common ties follow insertion order, which we must not depend on).
    from collections import Counter
    win = axis_sets[-max(1, recent):]
    rc = Counter(a for s in win for a in s)
    recent_axis, recent_count = None, 0
    if rc:
        top = max(rc.values())
        recent_axis = min(a for a, c in rc.items() if c == top)
        recent_count = top

    return {
        "experiments": n,
        "tagged": tagged,
        "locked_axis": best["axis"],
        "streak": best["len"],
        "streak_start_node": (nodes[best["start_idx"]].id if best["start_idx"] is not None else None),
        "fired": best["len"] >= streak_threshold,
        "recent_axis": recent_axis,
        "recent_frac": round(recent_count / len(win), 4) if win else 0.0,
        "current_streak": current,
    }


def lock_in_report(state: RunState, graph: ConceptGraph,
                   tags: Optional[dict[int, frozenset[str]]] = None, *,
                   streak_threshold: int = 5, recent: int = 8) -> str:
    """A compact text diagnostic over the lock-in signal — for the CLI. Pure."""
    sig = lock_in_signal(state, graph, tags, streak_threshold=streak_threshold, recent=recent)
    lines = [f"Action-space lock-in  (threshold={streak_threshold} consecutive same-lever nodes)",
             f"  experiments: {sig['experiments']}  tagged: {sig['tagged']}"]
    if sig["locked_axis"]:
        lines.append(f"  longest same-lever streak: {sig['streak']} on axis '{sig['locked_axis']}' "
                     f"(from node {sig['streak_start_node']}); current streak: {sig['current_streak']}")
    if sig["recent_axis"]:
        lines.append(f"  recent window: {sig['recent_frac']} of the last {recent} experiments on "
                     f"'{sig['recent_axis']}'")
    lines.append("")
    if sig["fired"]:
        lines.append(f"  ⚠ LOCK-IN ALARM — the search has stayed inside one subsystem ('{sig['locked_axis']}') "
                     f"for {sig['streak']} consecutive experiments.")
        lines.append("    Require the next batch to modify a DIFFERENT D5 branch (data / negatives / "
                     "eval / model), not another variant of the same lever.")
    else:
        lines.append("  lock-in alarm: (not fired — no long single-axis run)")
    return "\n".join(lines)
