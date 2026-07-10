"""Run digest + similarity helpers (context engineering for the Researcher).

A pure, dependency-light reuse hub: turns a `RunState` into the compact, high-signal "working set"
the Researcher sees every turn (`experiments_digest`), and the similarity primitive used both by the
novelty gate and the `find_analogous` tool (`param_distance`). No heavy imports — importable from
`roles.py`, `run_tools.py`, `server.py`, and `orchestrator.py` without cycles.
"""
from __future__ import annotations

import math
from typing import Optional

from looplab.core.models import NodeStatus, RunState


def numeric_params(params: dict, keys=None) -> dict:
    """The NUMERIC (int/float — bools included, matching the historical isinstance check) subset of a
    param dict, coerced to float. `keys` optionally restricts to a key set (e.g. the search bounds).
    Shared by the novelty gate, the surrogate and the panel so "numeric params" means the same thing
    everywhere. NOTE: runtime/proxy.py deliberately keeps its own try/float() variant — it also
    accepts numeric STRINGS, which this helper must not start doing."""
    return {k: float(v) for k, v in params.items()
            if (keys is None or k in keys) and isinstance(v, (int, float))}


def _numeric(params: dict) -> dict:   # param_distance's pre-rename local shorthand
    return numeric_params(params)


def knn_idw(pairs, k: int):
    """Inverse-distance-weighted k-NN over pre-computed `(distance, value)` pairs — the shared CORE
    of the three empirical predictors (search/surrogate, serve/panel, runtime/proxy). The callers
    keep their own (deliberately different) neighbour-eligibility and distance computations; only
    the rank / zero-distance short-circuit / weighting steps are unified here, so those can't
    silently drift apart again.

    Returns `(prediction, nearest_distance)`, or None when `pairs` is empty (the caller's abstain
    path). A zero-distance sample short-circuits to that sample's value with nearest=0.0 (ties keep
    input order — `sorted` is stable, exactly like every pre-extraction copy)."""
    if not pairs:
        return None
    nn = sorted(pairs, key=lambda t: t[0])[: max(1, k)]
    # Exact-match short-circuit scans the WHOLE top-k, not just nn[0]: a NaN distance (reachable —
    # the proxy coerces string params, and a float('nan') param value is isinstance-numeric
    # everywhere) sorts unpredictably and can sit AHEAD of a genuine 0.0; checking only nn[0]
    # would then fall through to the 1/d weighting and divide by that hidden zero. With no zero
    # present, a NaN distance degrades to a NaN prediction exactly like every pre-extraction copy.
    for d, v in nn:
        if d == 0.0:
            return v, 0.0
    nearest = nn[0][0]
    wsum = sum(1.0 / d for d, _ in nn)
    return sum((1.0 / d) * v for d, v in nn) / wsum, nearest


def param_distance(a: dict, b: dict) -> float:
    """Normalized-L2 distance between two param dicts over their shared NUMERIC keys (inf if none).
    This is the exact metric the E1 novelty gate uses; `find_analogous` reuses it so "near" means the
    same thing everywhere."""
    a, b = _numeric(a), _numeric(b)
    keys = set(a) & set(b)
    if not keys:
        return float("inf")
    return math.sqrt(sum((a[k] - b[k]) ** 2 for k in keys)) / math.sqrt(len(keys))


def theme_rollup(state: RunState) -> dict:
    """Per-theme rollup: {theme: {count, best_metric}}. A node's theme is its `idea.theme`
    (Researcher-assigned); nodes without one are skipped. `best_metric` is the better value per the
    run's direction. Audit-only — never read by replay.fold."""
    better = (lambda a, b: a < b) if state.direction == "min" else (lambda a, b: a > b)
    out: dict[str, dict] = {}
    for n in state.nodes.values():
        theme = getattr(n.idea, "theme", None)
        if not theme:
            continue
        m = n.robust_metric
        e = out.setdefault(theme, {"count": 0, "best_metric": None})
        e["count"] += 1
        if m is not None and (e["best_metric"] is None or better(m, e["best_metric"])):
            e["best_metric"] = m
    return out


def node_metric(n) -> Optional[float]:
    """The metric used for ranking/display: the robust confirmed mean when present, else the raw."""
    return n.robust_metric


def top_nodes(state: RunState, k: int, *, worst: bool = False) -> list:
    """Top-K (or bottom-K when `worst`) FEASIBLE evaluated nodes by metric, per direction."""
    feasible = [n for n in state.feasible_nodes() if node_metric(n) is not None]
    asc = (state.direction == "min")        # ascending = best-first for minimization
    if worst:
        asc = not asc
    feasible.sort(key=lambda n: (node_metric(n), n.id), reverse=not asc)
    return feasible[:k]


def fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"{v:.4g}"


def fmt_params(params: dict, max_k: int = 4) -> str:
    items = list(params.items())[:max_k]
    body = ", ".join(f"{k}={fmt_num(float(v)) if isinstance(v, (int, float)) else v}" for k, v in items)
    extra = "" if len(params) <= max_k else f", +{len(params) - max_k} more"
    return f"[{body}{extra}]" if body else "[]"


# Default number of intra-node sweep trials surfaced in the always-on context (digest). A small,
# REPRESENTATIVE sample — best + worst plus an even spread between — conveys the tuning landscape
# (dispersion + how the metric moves with the params) without flooding the prompt budget. The
# read_experiment tool can return more, or every trial, on demand.
DEFAULT_TRIAL_K = 10


def finite_trials(trials) -> list:
    """Trials that produced a usable (finite) metric — the only ones carrying tuning signal."""
    return [t for t in trials if t.metric is not None and math.isfinite(t.metric)]




def select_trials(trials, k: int, direction: str) -> list:
    """Up to `k` trials chosen to COVER the metric range and show the tuning dynamics: sorted
    best→worst, ALWAYS keeping the best and the worst, the rest sampled at even rank-quantiles
    between them (so a flat region and a cliff both show up). Deterministic — rank-based, with a
    stable param tie-break — so replay/audit is reproducible. Trials with no finite metric are
    dropped; `k<=0` or `k>=count` returns the full sorted finite set."""
    scored = finite_trials(trials)
    scored.sort(key=lambda t: sorted(t.params.items()))            # stable, direction-independent tie-break
    scored.sort(key=lambda t: t.metric, reverse=(direction != "min"))
    if k <= 0 or len(scored) <= k:
        return scored
    if k == 1:                       # one slot → the best (can't span a range with a single point; also
        return scored[:1]            # guards the k-1==0 divisor in the quantile math below)
    idx = sorted({round(i * (len(scored) - 1) / (k - 1)) for i in range(k)})
    return [scored[i] for i in idx]


def trial_line(t) -> str:
    extra = f"  ({fmt_num(t.seconds)}s)" if getattr(t, "seconds", None) else ""
    return f"{fmt_params(t.params)} → {fmt_num(t.metric)}{extra}"




def _node_line(n) -> str:
    if n.status is NodeStatus.failed:
        outcome = f"FAILED ({n.error_reason or 'error'})"
    else:
        outcome = f"metric={fmt_num(node_metric(n))}"
    theme = f" {{{n.idea.theme}}}" if getattr(n.idea, "theme", None) else ""
    swept = f" swept ×{len(n.trials)}" if getattr(n, "trials", None) else ""
    # Signal-delivery (§1): surface the crash-triage verdict on a failed node so the "avoid
    # repeating" set carries the agent's judgment of WHY it failed, not just the error kind — the
    # next proposal then reacts to "the idea is unsound because X", not a bare taxonomy label.
    triage = getattr(n, "triage_rationale", "") if n.status is NodeStatus.failed else ""
    triage = f" — triage: {' '.join(triage.split())[:100]}" if triage else ""
    return f"  #{n.id} {n.operator} {outcome} {fmt_params(n.idea.params)}{swept}{theme}{triage}"


def trust_reflection(state: RunState, max_shown: int = 2) -> str:
    """Signal-delivery (§1): the agent-facing trust-reflection block — a recently trust-FLAGGED node
    surfaced to the NEXT proposal so the agent reacts to it instead of silently re-deriving the
    flagged approach (trust flags otherwise only bar a WIN; the agent never learns). Advisory wording
    (the detectors are heuristics): says what fired and to avoid it if unintended. Fires even under
    `audit` (nothing gate-excluded) — the warning is then the only channel the signal has to the
    agent. Pure projection of the folded `reward_hacks`; "" when nothing hard-flagged. Extracted here
    (not inline in the engine) so `tests/test_signal_delivery.py` can exercise it directly."""
    if not getattr(state, "reward_hacks", None):
        return ""
    # lazy import (avoid a cycle at module load): the SAME `is_hard_signal` classifier the gate uses,
    # so the names we render never diverge from the reason the node was hard-flagged (a node flagged
    # ONLY by `critic:hardcoded_metric` used to render as "node N ()" because _sigs stripped it).
    from looplab.events.replay import hard_flagged_ids, is_hard_signal
    hard = hard_flagged_ids(state)
    if not hard:
        return ""
    recent = sorted((r for r in state.reward_hacks if r.get("node_id") in hard),
                    key=lambda r: r.get("node_id") or -1, reverse=True)[:max_shown]

    def _sigs(r) -> str:
        # Name the HARD signals (the reason it gates); advisory `critic:`/`perfect_metric` noise stays
        # hidden. A hard-flagged node therefore always renders at least one signal, never "()".
        return "; ".join(str(s.get("signal", "")) for s in (r.get("signals") or [])
                         if is_hard_signal(s.get("signal", "")))
    items = "; ".join(f"node {r.get('node_id')} ({_sigs(r)})" for r in recent)
    gated = (" (EXCLUDED from winning under the active trust gate)"
             if getattr(state, "trust_gate", "audit") in ("gate", "block") else "")
    return ("\nTrust — a recent solution was flagged for a cheating/leakage pattern: " + items
            + gated + ". If unintended, ensure your next experiment does NOT read held-out "
            "answers/labels or fit on validation/test data.")


def auto_char_cap(state: RunState) -> int:
    """M5: scale the digest budget with the run instead of one flat cap — a 100-node MLE-bench
    run carries far more decision-relevant state than an 8-node toy run. Bounded so a huge run
    still can't flood the prompt (depth stays behind the run tools)."""
    return min(6000, max(1200, 60 * len(state.nodes)))


def sibling_digest(state: RunState, parent) -> str:
    """M1/A0c operator-scoped memory (aira-dojo MEM_OPS `sibling`): what the OTHER children of
    the node being operated on (or the other root drafts, when drafting) already tried — the
    diversity-pressure context for draft/improve ("your siblings already tried A/B/C; do
    something different"). Empty when there are no resolved siblings."""
    pid = parent.id if parent is not None else None
    sibs = [n for n in state.nodes.values()
            if n.status is not NodeStatus.pending
            and (pid in n.parent_ids if pid is not None else not n.parent_ids)
            and (parent is None or n.id != parent.id)]
    if not sibs:
        return ""
    sibs.sort(key=lambda n: n.id, reverse=True)
    lines = ["\nSiblings of this expansion (already tried — push diversity, don't repeat):"]
    for n in sibs[:5]:
        why = " ".join((n.idea.rationale or "").split())[:90]
        lines.append(_node_line(n) + (f" — {why}" if why else ""))
    return "\n".join(lines)


def lineage_lessons(state: RunState, parent, k: int = 5) -> str:
    """D6/3.2 insight backpropagation (Arbor's `Backpropagate` step, MARS cross-branch lessons):
    one-line outcomes distilled from the subtree UNDER the node being refined, so an expansion
    inherits what its lineage already learned instead of re-deriving it. Pure projection of the
    folded DAG — no new events. Ranked by |Δ over parent| so the most informative experiments
    (biggest win, biggest regression) surface first."""
    if parent is None:
        return ""
    # descendants of `parent`
    kids: dict[int, list[int]] = {}
    for n in state.nodes.values():
        for p in n.parent_ids:
            kids.setdefault(p, []).append(n.id)
    desc: list[int] = []
    stack = list(kids.get(parent.id, []))
    seen: set[int] = set()
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        desc.append(nid)
        stack.extend(kids.get(nid, []))
    lessons: list[tuple[float, str]] = []
    for nid in desc:
        n = state.nodes[nid]
        if n.status is NodeStatus.failed:
            lessons.append((0.5, f"  #{n.id} {n.operator} FAILED ({n.error_reason or 'error'}): "
                                 f"{' '.join((n.idea.rationale or '').split())[:70]}"))
            continue
        if n.metric is None:
            continue
        pm = [state.nodes[p].metric for p in n.parent_ids
              if p in state.nodes and state.nodes[p].metric is not None]
        if not pm:
            continue
        base = max(pm) if state.direction == "max" else min(pm)
        delta = (n.metric - base) if state.direction == "max" else (base - n.metric)
        sign = "improved" if delta > 0 else "regressed"
        lessons.append((abs(delta),
                        f"  #{n.id} {n.operator} {sign} {fmt_num(abs(delta))} vs parent: "
                        f"{' '.join((n.idea.rationale or '').split())[:70]}"))
    if not lessons:
        return ""
    lessons.sort(key=lambda t: -t[0])
    return "\nLessons from this lineage (inherited — build on wins, don't undo/redo):\n" + \
        "\n".join(line for _, line in lessons[:k])


def ancestral_repair_chain(state: RunState, node, k: int = 4) -> str:
    """M1/A0c operator-scoped memory (aira-dojo MEM_OPS `ancestral`): the chain of PRIOR repairs
    in this lineage, for the debug operator — so a fix doesn't oscillate undo↔redo with an
    earlier one. Walks ancestors collecting debug/repair nodes and what they hit."""
    if node is None:
        return ""
    chain: list = []
    seen: set[int] = set()
    stack = list(node.parent_ids)
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in state.nodes:
            continue
        seen.add(nid)
        n = state.nodes[nid]
        if n.operator == "debug" or n.error:
            chain.append(n)
        stack.extend(n.parent_ids)
    if not chain:
        return ""
    chain.sort(key=lambda n: n.id)
    lines = ["Prior repairs in this lineage (do NOT undo these fixes or re-introduce their bugs):"]
    for n in chain[-k:]:
        err = " ".join((n.error or "").split())[:80]
        outcome = ("still failing" if n.status is NodeStatus.failed
                   else f"fixed, metric={fmt_num(node_metric(n))}")
        lines.append(f"  #{n.id} {n.operator}: {err or n.error_reason or 'repair'} — {outcome}")
    return "\n".join(lines)


def ablation_attribution(state: RunState) -> dict:
    """P3 run-level ablation attribution: aggregate per-component impact across EVERY ablate
    event in the run — "which pipeline component moved the metric overall" (MLE-STAR's outer
    loop). {component: {"impact": summed |Δ|, "n": probes}} sorted by impact desc."""
    out: dict[str, dict] = {}
    for ab in state.ablations or []:
        for comp, imp in (ab.get("impacts") or {}).items():
            try:
                v = abs(float(imp))
            except (TypeError, ValueError):
                continue
            d = out.setdefault(str(comp), {"impact": 0.0, "n": 0})
            d["impact"] += v
            d["n"] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["impact"]))


def experiments_digest(state: RunState, top_k: int = 5, worst_n: int = 3,
                       char_cap: int = 0) -> str:
    """A compact, budgeted snapshot of the whole search appended to the Researcher's prompt — its
    always-on "working set". Lists the strongest experiments, the weakest + recent failures (so the
    model doesn't repeat dead ends), and the theme map. Depth lives behind the run-introspection
    tools; this stays small (hard `char_cap`; <=0 = auto-scale with the run size, M5)."""
    nodes = state.nodes
    if not nodes:
        return ""
    if char_cap <= 0:
        char_cap = auto_char_cap(state)
    n_fail = sum(1 for n in nodes.values() if n.status is NodeStatus.failed)
    lines = [f"\nSearch so far — {len(nodes)} experiment(s), {n_fail} failed:"]

    winners = top_nodes(state, top_k)
    if winners:
        lines.append("Strongest:")
        lines += [_node_line(n) for n in winners]

    # Tuning landscape of the best SWEPT experiment — a small representative sample (best→worst, even
    # spread) so the model reasons over the response surface, not just the winning point. Placed right
    # after the winners (before the weaker rows) so it's prioritized over them under the char budget;
    # depth/all is behind the read_experiment tool. Sourced from the best FEASIBLE evaluated sweep —
    # a failed/infeasible sweep still gets the inline "swept ×N" flag + on-demand read_experiment.
    swept = [n for n in top_nodes(state, len(state.nodes)) if getattr(n, "trials", None)]
    if swept:
        champ = swept[0]
        sel = select_trials(champ.trials, DEFAULT_TRIAL_K, state.direction)
        finite_n = len(finite_trials(champ.trials))
        cap = f", showing {len(sel)} of {finite_n} best→worst" if len(sel) < finite_n else ""
        lines.append(f"Tuning of #{champ.id} ({len(champ.trials)} trials{cap}):")
        lines += [f"  {trial_line(t)}" for t in sel]

    # Weakest feasible + the most recent failures — the "avoid repeating this" set.
    weak = [n for n in top_nodes(state, worst_n, worst=True) if n not in winners]
    fails = sorted((n for n in nodes.values() if n.status is NodeStatus.failed),
                   key=lambda n: n.id, reverse=True)[:worst_n]
    avoid = weak + [f for f in fails if f not in weak]
    if avoid:
        lines.append("Weakest / failures (avoid repeating):")
        lines += [_node_line(n) for n in avoid]

    themes = theme_rollup(state)
    if themes:
        chips = "; ".join(
            f"{t} ×{d['count']}" + (f" (best {fmt_num(d['best_metric'])})" if d['best_metric'] is not None else "")
            for t, d in sorted(themes.items(), key=lambda kv: -kv[1]["count"]))
        lines.append(f"Themes: {chips}")

    # P3: run-level component attribution — which parts of the pipeline actually moved the
    # metric, aggregated over every ablation probe (steers refinement toward high-yield parts).
    attr = ablation_attribution(state)
    if attr:
        top = list(attr.items())[:5]
        lines.append("Component attribution (summed ablation impact): " +
                     "; ".join(f"{c} {fmt_num(d['impact'])} (×{d['n']})" for c, d in top))

    out = "\n".join(lines)
    if len(out) > char_cap:
        out = out[:char_cap].rstrip() + " …"
    return out
