"""Concept ANALYTICS — the pure, deterministic read-models over (RunState, ConceptGraph, tags).

Split out of `concept_graph.py` (doc 25 SE-09). The discipline that module's header states — "PURE
and deterministic over `(RunState, ConceptGraph, tags)` — no I/O, no LLM, no wall-clock — so a
replay recomputes them byte-identically" — is a property of THESE functions, and it is now a
property of a whole file instead of a region inside one. The impure half (ASSIGNING the tags) lives
in `concept_tagging.py`, which this module imports only for the no-LLM default when a caller passes
no `tags`.

Nothing here may grow an LLM call or a filesystem read. `concept_report` is the CLI's text render of
the same numbers and is pure for the same reason.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from typing import Optional

from looplab.core.models import RunState
# The cluster reaches a sibling's FUNCTIONS through the MODULE object, never by name. A
# `from looplab.search.concept_tagging import tag_nodes_heuristic` binds the function OBJECT at
# import time, so `monkeypatch.setattr(concept_tagging, "tag_nodes_heuristic", ...)` would stop
# reaching this module — a seam that worked while caller and callee shared one namespace, and one
# the suite still uses (`tests/test_retro_tag_persist.py` forces a CAS race through it). Same
# hazard CLAUDE.md records for `serve/scope_actions.py` importing store names by value. Types and
# the pure `_normalize_concept_id` wrapper are exempt: nothing patches those.
from looplab.search import concept_tagging
from looplab.search.concept_graph import ConceptGraph


# --------------------------------------------------------------------------- #
# Analytics (pure, deterministic over (state, graph, tags))
# --------------------------------------------------------------------------- #

def concept_coverage(state: RunState, graph: ConceptGraph,
                     tags: Optional[dict[int, frozenset[str]]] = None) -> dict:
    """The graph coverage read-model — the validated concentration signals (§21.11). Pure and
    deterministic; an empty run yields zeros. When `tags` is omitted, the deterministic heuristic tagger
    is used (so the diagnostic runs with no LLM).

    Keys:
      experiments        - idea-carrying nodes (run's experiments, `coverage.py` denominator)
      tagged             - experiments that received >=1 concept tag
      untagged           - experiments no concept matched (effort not yet localized)
      concepts_touched   - distinct concepts with >=1 touch
      axes_touched       - distinct axes with >=1 touch
      axes_total         - skeleton axes in the graph
      top_concept        - {id, count, frac}: the most-touched concept and its share of TAGGED experiments
      dominant_clique    - {axes:[a,b], count, frac}: the most-common co-occurring AXIS pair and its share
      uncovered_axes     - skeleton axes with 0 touches across the whole run
      uncovered_concepts - real (non-placeholder) skeleton concepts with first_touch == None
      uncovered_key      - KEY (winning-region) concepts uncovered — the standing alarm's payload
      axis_touch         - {axis: experiment-count} rollup (an experiment counts once per axis it touches)
      concept_touch      - {concept_id: experiment-count}
      first_touch        - {concept_id: 0-based experiment index of first touch} (touched concepts only)
    """
    nodes = concept_tagging.experiment_nodes(state)
    if tags is None:
        tags = concept_tagging.tag_nodes_heuristic(state, graph)
    n = len(nodes)
    if n == 0:
        return _empty_coverage(graph)

    concept_touch: Counter = Counter()
    axis_touch: Counter = Counter()
    clique_pairs: Counter = Counter()
    first_touch: dict[str, int] = {}
    tagged = 0
    for idx, node in enumerate(nodes):
        cids = tags.get(node.id, frozenset())
        if cids:
            tagged += 1
        node_axes: set[str] = set()
        for cid in cids:
            concept_touch[cid] += 1
            first_touch.setdefault(cid, idx)
            node_axes.update(graph.axes_of(cid))
        for ax in node_axes:
            axis_touch[ax] += 1
        # An axis-clique is a co-occurring AXIS pair on ONE experiment (§21.11): the run lived inside the
        # `loss × regularization` clique. Count unordered pairs so the dominant clique is direction-free.
        for a, b in combinations(sorted(node_axes), 2):
            clique_pairs[(a, b)] += 1

    denom = tagged or n  # fraction over TAGGED experiments (untagged effort isn't ON a concept yet)
    # Deterministic argmax: highest count, ties broken by the SMALLEST key. `Counter.most_common` breaks
    # ties by insertion order — and the counters are filled by iterating each node's `frozenset` of tags,
    # whose order is PYTHONHASHSEED-randomized — so most_common(1) would make `top_concept`/`dominant_clique`
    # non-deterministic on a tie, violating the pure/replay-safe contract. Sorting on (-count, key) fixes it.
    top_cid, top_count = _argmax(concept_touch)
    clique, clique_count = _argmax(clique_pairs)

    all_axes = graph.axes()
    real_concepts = [c.id for c in graph.concepts() if not c.id.endswith("/*")]
    uncovered_axes = [ax for ax in all_axes if axis_touch.get(ax, 0) == 0]
    uncovered_concepts = [cid for cid in real_concepts if cid not in first_touch]
    uncovered_key = [cid for cid in graph.key_concepts() if cid not in first_touch]

    return {
        "experiments": n,
        "tagged": tagged,
        "untagged": n - tagged,
        "concepts_touched": len(concept_touch),
        "axes_touched": len(axis_touch),
        "axes_total": len(all_axes),
        "top_concept": ({"id": top_cid, "count": top_count, "frac": round(top_count / denom, 4)}
                        if top_cid else None),
        "dominant_clique": ({"axes": list(clique), "count": clique_count,
                             "frac": round(clique_count / denom, 4)} if clique else None),
        "uncovered_axes": uncovered_axes,
        "uncovered_concepts": uncovered_concepts,
        "uncovered_key": uncovered_key,
        "axis_touch": dict(sorted(axis_touch.items())),
        "concept_touch": dict(sorted(concept_touch.items())),
        "first_touch": dict(sorted(first_touch.items())),
    }


def _argmax(counter):
    """(key, count) of the max-count entry, ties broken by the smallest key — a DETERMINISTIC argmax
    (unlike `Counter.most_common`, whose tie order follows hash-seed-randomized insertion). (None, 0)
    when empty."""
    if not counter:
        return None, 0
    key = min(counter, key=lambda k: (-counter[k], k))
    return key, counter[key]


def _empty_coverage(graph: ConceptGraph) -> dict:
    all_axes = graph.axes()
    real = [c.id for c in graph.concepts() if not c.id.endswith("/*")]
    return {
        "experiments": 0, "tagged": 0, "untagged": 0, "concepts_touched": 0,
        "axes_touched": 0, "axes_total": len(all_axes),
        "top_concept": None, "dominant_clique": None,
        "uncovered_axes": all_axes, "uncovered_concepts": real,
        "uncovered_key": graph.key_concepts(),
        "axis_touch": {}, "concept_touch": {}, "first_touch": {},
    }


def _median(xs: list[float]) -> Optional[float]:
    """Deterministic median (sorted); None on empty. Used as the per-run outcome baseline."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def concept_metrics(state: RunState, graph: ConceptGraph,
                    tags: Optional[dict[int, frozenset[str]]] = None) -> dict:
    """Per-concept OUTCOME rollup — the metric/Δ view the concept table (View 1) needs, alongside the
    touch-only `concept_coverage`. PURE and deterministic over `(state, graph, tags)`: no I/O, no LLM,
    so it recomputes byte-identically on replay and ships to the UI via /state-derived reads.

    Joins each concept's touching experiments to their `robust_metric` (models.py) — WITHOUT dividing a
    multi-membership node's metric across its concepts: a node that touches loss AND architecture counts
    its FULL metric in BOTH (decided — we never fake breadth by splitting a real result). `delta_*` is
    SIGNED so positive always means "better than the run baseline" for the run's `direction`; the
    baseline is the run's MEDIAN robust_metric over feasible evaluated experiments (robust to outliers,
    so one lucky node can't move it). Failed / not-yet-evaluated nodes still count in `touched` (effort
    spent on the concept) but contribute no metric.

    Returns {"baseline", "direction", "rows": {concept_id: {touched, evaluated, first_touch, best,
    mean, worst, delta_best, delta_mean}}} — rows id-sorted; metric fields None when a concept has no
    evaluated node. Empty run / no tags -> empty rows."""
    nodes = concept_tagging.experiment_nodes(state)
    if tags is None:
        tags = concept_tagging.tag_nodes_heuristic(state, graph)
    direction = str(getattr(state, "direction", "max") or "max").lower()
    is_min = direction == "min"
    sign = -1.0 if is_min else 1.0
    touched: dict[str, int] = {}
    first: dict[str, int] = {}
    metrics: dict[str, list[float]] = {}
    all_metrics: list[float] = []
    for idx, node in enumerate(nodes):
        m = node.robust_metric
        # feasible + has a metric = an evaluated experiment that can carry an outcome; the rest still
        # count as effort (touched) but never as a metric sample.
        ok = m is not None and getattr(node, "feasible", True) is not False
        if ok:
            all_metrics.append(float(m))
        for cid in tags.get(node.id, frozenset()):
            touched[cid] = touched.get(cid, 0) + 1
            first.setdefault(cid, idx)
            if ok:
                metrics.setdefault(cid, []).append(float(m))
    baseline = _median(all_metrics)
    rows: dict[str, dict] = {}
    for cid in sorted(touched):
        ms = metrics.get(cid, [])
        row = {"touched": touched[cid], "evaluated": len(ms), "first_touch": first.get(cid),
               "best": None, "mean": None, "worst": None, "delta_best": None, "delta_mean": None}
        if ms:
            best = min(ms) if is_min else max(ms)
            worst = max(ms) if is_min else min(ms)
            mean = sum(ms) / len(ms)
            row["best"], row["worst"], row["mean"] = round(best, 6), round(worst, 6), round(mean, 6)
            if baseline is not None:
                row["delta_best"] = round(sign * (best - baseline), 6)
                row["delta_mean"] = round(sign * (mean - baseline), 6)
        rows[cid] = row

    # SUBTREE rollup (separate from `rows`, which stays leaf/direct for the frame parity invariant
    # set(touch)==set(rows)==set(experiment_refs)). For each concept it aggregates every experiment at or
    # BELOW it on the id-path — so an AXIS/parent row (`loss`) shows real touched/best/Δ instead of a blank
    # `·` when the tree collapses its children. UNION per node (a node tagged loss/contrastive AND
    # loss/triplet counts ONCE for `loss`). The UI reads this for every tree row; for a leaf it equals its
    # own `rows` entry. Also the basis for sorting concept chips by Δbest-from-baseline (View 2).
    agg_nodes: dict[str, set] = {}
    agg_metrics: dict[str, list[float]] = {}
    agg_first: dict[str, int] = {}
    for idx, node in enumerate(nodes):
        m = node.robust_metric
        ok = m is not None and getattr(node, "feasible", True) is not False
        seen: set[str] = set()
        for cid in tags.get(node.id, frozenset()):
            c = str(cid)
            while c and c not in seen:
                seen.add(c)
                c = c.rsplit("/", 1)[0] if "/" in c else ""
        for cid in seen:
            agg_nodes.setdefault(cid, set()).add(idx)
            if idx < agg_first.get(cid, idx + 1):
                agg_first[cid] = idx
            if ok:
                agg_metrics.setdefault(cid, []).append(float(m))
    rollup: dict[str, dict] = {}
    for cid in sorted(agg_nodes):
        ms = agg_metrics.get(cid, [])
        r = {"touched": len(agg_nodes[cid]), "evaluated": len(ms), "first_touch": agg_first.get(cid),
             "best": None, "mean": None, "worst": None, "delta_best": None, "delta_mean": None}
        if ms:
            best = min(ms) if is_min else max(ms)
            worst = max(ms) if is_min else min(ms)
            mean = sum(ms) / len(ms)
            r["best"], r["worst"], r["mean"] = round(best, 6), round(worst, 6), round(mean, 6)
            if baseline is not None:
                r["delta_best"] = round(sign * (best - baseline), 6)
                r["delta_mean"] = round(sign * (mean - baseline), 6)
        rollup[cid] = r

    return {"baseline": None if baseline is None else round(baseline, 6),
            "direction": direction, "rows": rows, "rollup": rollup}


def uncovered_regions(state: RunState, graph: ConceptGraph,
                      tags: Optional[dict[int, frozenset[str]]] = None) -> dict:
    """The decisive *uncovered winning-region* alarm (§21.11) — the single most actionable PART IV
    output. Reports which skeleton regions the search footprint NEVER entered, from the first node, as a
    ready-to-use Strategist pivot directive ("you have 0 coverage in {X} — go there", not "broaden").
    Pure. `fired` is True whenever a KEY winning-region concept is uncovered (or, absent a curated key
    set, whenever an entire axis is untouched)."""
    cov = concept_coverage(state, graph, tags)
    key_uncovered = cov["uncovered_key"]
    axes_uncovered = cov["uncovered_axes"]
    has_key = bool(graph.key_concepts())
    fired = bool(key_uncovered) if has_key else bool(axes_uncovered)
    # The directive names concrete regions: prefer the labelled key concepts; else the empty axes.
    targets = key_uncovered or axes_uncovered
    directive = ""
    if fired and targets:
        directive = ("0 coverage in {" + ", ".join(targets[:6]) + "} across all "
                     f"{cov['experiments']} experiments — direct the next proposals there "
                     "(not just 'broaden').")
    return {
        "fired": fired,
        "experiments": cov["experiments"],
        "uncovered_key": key_uncovered,
        "uncovered_axes": axes_uncovered,
        "directive": directive,
    }


# --------------------------------------------------------------------------- #
# Human-readable report (for the CLI diagnostic)
# --------------------------------------------------------------------------- #

def concept_report(state: RunState, graph: ConceptGraph,
                   tags: Optional[dict[int, frozenset[str]]] = None) -> str:
    """A compact text diagnostic over the concept graph — the offline CLI's output. Pure."""
    if tags is None:
        tags = concept_tagging.tag_nodes_heuristic(state, graph)
    cov = concept_coverage(state, graph, tags)
    lines = [
        f"Concept-graph coverage  (task-type={graph.task_type or 'generic'})",
        f"  experiments: {cov['experiments']}  tagged: {cov['tagged']}  untagged: {cov['untagged']}",
        f"  concepts touched: {cov['concepts_touched']}   axes touched: "
        f"{cov['axes_touched']}/{cov['axes_total']}",
    ]
    tc = cov["top_concept"]
    if tc:
        lines.append(f"  top concept: {tc['id']}  touch-fraction={tc['frac']} ({tc['count']} exps)")
    dc = cov["dominant_clique"]
    if dc:
        lines.append(f"  dominant axis-clique: {dc['axes'][0]} × {dc['axes'][1]}  "
                     f"share={dc['frac']} ({dc['count']} exps)")
    if cov["axis_touch"]:
        lines.append("  per-axis touch: "
                     + ", ".join(f"{ax}={c}" for ax, c in cov["axis_touch"].items()))
    alarm = uncovered_regions(state, graph, tags)
    lines.append("")
    if alarm["fired"]:
        lines.append("  ⚠ UNCOVERED-REGION ALARM")
        lines.append("    " + alarm["directive"])
        if alarm["uncovered_key"]:
            lines.append("    uncovered key regions: " + ", ".join(alarm["uncovered_key"]))
        if alarm["uncovered_axes"]:
            lines.append("    entirely-untouched axes: " + ", ".join(alarm["uncovered_axes"]))
    else:
        # The alarm keys on the WINNING-region (`key`) concepts, so `fired` can be False (all key regions
        # covered) while whole non-key AXES are still untouched — don't claim "all regions covered" then.
        if alarm["uncovered_axes"]:
            lines.append("  uncovered-region alarm: key regions covered, but entirely-untouched axes remain: "
                         + ", ".join(alarm["uncovered_axes"]))
        else:
            lines.append("  uncovered-region alarm: (not fired — all key/axis regions have coverage)")
    return "\n".join(lines)
