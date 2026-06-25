"""Run digest + similarity helpers (context engineering for the Researcher).

A pure, dependency-light reuse hub: turns a `RunState` into the compact, high-signal "working set"
the Researcher sees every turn (`experiments_digest`), and the similarity primitive used both by the
novelty gate and the `find_analogous` tool (`param_distance`). No heavy imports — importable from
`roles.py`, `run_tools.py`, `server.py`, and `orchestrator.py` without cycles.
"""
from __future__ import annotations

import math
from typing import Optional

from .models import NodeStatus, RunState


def _numeric(params: dict) -> dict:
    return {k: float(v) for k, v in params.items() if isinstance(v, (int, float))}


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
        m = n.confirmed_mean if n.confirmed_mean is not None else n.metric
        e = out.setdefault(theme, {"count": 0, "best_metric": None})
        e["count"] += 1
        if m is not None and (e["best_metric"] is None or better(m, e["best_metric"])):
            e["best_metric"] = m
    return out


def node_metric(n) -> Optional[float]:
    """The metric used for ranking/display: the robust confirmed mean when present, else the raw."""
    return n.confirmed_mean if n.confirmed_mean is not None else n.metric


def top_nodes(state: RunState, k: int, *, worst: bool = False) -> list:
    """Top-K (or bottom-K when `worst`) FEASIBLE evaluated nodes by metric, per direction."""
    feasible = [n for n in state.feasible_nodes() if node_metric(n) is not None]
    asc = (state.direction == "min")        # ascending = best-first for minimization
    if worst:
        asc = not asc
    feasible.sort(key=lambda n: (node_metric(n), n.id), reverse=not asc)
    return feasible[:k]


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "?"
    return f"{v:.4g}"


def _fmt_params(params: dict, max_k: int = 4) -> str:
    items = list(params.items())[:max_k]
    body = ", ".join(f"{k}={_fmt_num(float(v)) if isinstance(v, (int, float)) else v}" for k, v in items)
    extra = "" if len(params) <= max_k else f", +{len(params) - max_k} more"
    return f"[{body}{extra}]" if body else "[]"


def _node_line(n) -> str:
    if n.status is NodeStatus.failed:
        outcome = f"FAILED ({n.error_reason or 'error'})"
    else:
        outcome = f"metric={_fmt_num(node_metric(n))}"
    theme = f" {{{n.idea.theme}}}" if getattr(n.idea, "theme", None) else ""
    return f"  #{n.id} {n.operator} {outcome} {_fmt_params(n.idea.params)}{theme}"


def experiments_digest(state: RunState, top_k: int = 5, worst_n: int = 3,
                       char_cap: int = 1200) -> str:
    """A compact, budgeted snapshot of the whole search appended to the Researcher's prompt — its
    always-on "working set". Lists the strongest experiments, the weakest + recent failures (so the
    model doesn't repeat dead ends), and the theme map. Depth lives behind the run-introspection
    tools; this stays small (hard `char_cap`)."""
    nodes = state.nodes
    if not nodes:
        return ""
    n_fail = sum(1 for n in nodes.values() if n.status is NodeStatus.failed)
    lines = [f"\nSearch so far — {len(nodes)} experiment(s), {n_fail} failed:"]

    winners = top_nodes(state, top_k)
    if winners:
        lines.append("Strongest:")
        lines += [_node_line(n) for n in winners]

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
            f"{t} ×{d['count']}" + (f" (best {_fmt_num(d['best_metric'])})" if d['best_metric'] is not None else "")
            for t, d in sorted(themes.items(), key=lambda kv: -kv[1]["count"]))
        lines.append(f"Themes: {chips}")

    out = "\n".join(lines)
    if len(out) > char_cap:
        out = out[:char_cap].rstrip() + " …"
    return out
