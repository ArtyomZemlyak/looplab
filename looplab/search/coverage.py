"""Coverage / diversity read-model — the run's BREADTH signal (extends the I22 diversity archive).

A PURE, fold-derived summary of HOW BROADLY the run has explored the hypothesis space so far:
distinct themes and parameter-niches, operator spread, and two narrowing SCALARS — the normalized
theme entropy (1 = spread evenly across themes, 0 = everything on one theme) and the dominant-theme
fraction (how concentrated the run is on its single most-explored theme). It is *context*, never a
decision: the Strategist reads it to decide how much novelty pressure to apply (the LLM decides —
this only informs it), and it is recorded at the strategist cadence as a `coverage_snapshot`
sidecar event so the run's narrowing curve is queryable over the log and replayable historically.

No I/O, no embeddings, no wall-clock — deterministic over `RunState`, so a replay recomputes it
byte-identically and a historical log can be re-measured offline (fold -> coverage_signal). This is
why the signal deliberately uses only structural facts (themes/params/operators the fold already
holds) and NOT embedding similarity, which would need a model call and break replay determinism.

Motivation: current research agents tend to NARROW rather than broaden the hypotheses they explore
(arXiv 2605.27905). LoopLab's default path is seed-then-hill-climb whose only breadth signal to the
meta-controller was metric STAGNATION (`improves_since_best`) — blind to coverage collapse. This
gives the controller eyes on the space it is (or isn't) covering.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

from looplab.core.models import RunState
from looplab.events.digest import node_axes, node_theme, theme_rollup
from looplab.search.archive import DiversityArchive


def _projection_value(value):
    """Canonical JSON-safe value for the bounded current-analytics identity."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return [[str(key), _projection_value(item)]
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))]
    if isinstance(value, (list, tuple)):
        return [_projection_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_projection_value(item) for item in value), key=repr)
    raw = getattr(value, "value", value)
    return raw if raw is not value else str(value)


def analytics_projection_token(state: RunState) -> str:
    """Stable digest of every current input that Part-IV coverage/lock-in snapshots can summarize."""
    aborted = set(getattr(state, "aborted_nodes", None) or [])
    memberships = getattr(state, "node_concepts", None)
    memberships = memberships if isinstance(memberships, dict) else {}
    rows = []
    for node in sorted((getattr(state, "nodes", None) or {}).values(), key=lambda item: item.id):
        idea = getattr(node, "idea", None)
        if idea is None or node.id in aborted or getattr(node, "tombstoned", False):
            continue
        rows.append({
            "id": node.id,
            "attempt": getattr(node, "attempt", 0),
            "status": _projection_value(getattr(node, "status", None)),
            "feasible": getattr(node, "feasible", True),
            "metric": _projection_value(getattr(node, "robust_metric", None)),
            "operator": str(getattr(node, "operator", "") or ""),
            "theme": str(getattr(idea, "theme", "") or ""),
            "rationale": str(getattr(idea, "rationale", "") or ""),
            "hypothesis": str(getattr(idea, "hypothesis", "") or ""),
            "eval_profile": str(getattr(idea, "eval_profile", "") or ""),
            "params": _projection_value(getattr(idea, "params", None) or {}),
            "space": _projection_value(getattr(idea, "space", None) or {}),
            "authored_concepts": _projection_value(getattr(idea, "concepts", None) or []),
            "folded_concepts": _projection_value(memberships.get(node.id, [])),
        })
    payload = {
        "v": 1,
        "nodes": rows,
        "consolidation": _projection_value(getattr(state, "concept_consolidation", None) or {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_matches_analytics_projection(state: RunState, snapshot: object) -> bool:
    """Whether a snapshot describes the exact current live projection rather than audit history."""
    if not isinstance(snapshot, dict):
        return False
    token = snapshot.get("projection_token")
    if token is None:
        # Pre-token snapshots remain readable on an untouched legacy run. Once lifecycle identity changes,
        # their source boundary is unknowable and they must not steer a proposal.
        # #5 review: ALSO bind the tokenless path to the node-count boundary (as the tokened path does).
        # Without it a tokenless node-1 snapshot matched an untouched node-20 run, and the shared reverse
        # scan (`latest_live_snapshot`) can now revive that ancient row when it is no longer the last one
        # (behind a newer stale receipt). Fail closed on an absent/non-canonical `at_node`.
        at_node = snapshot.get("at_node")
        if (not isinstance(at_node, int) or isinstance(at_node, bool)
                or at_node != len((getattr(state, "nodes", None) or {}))):
            return False
        aborted = getattr(state, "aborted_nodes", None) or []
        return (not aborted
                and not any(getattr(node, "tombstoned", False)
                            or int(getattr(node, "attempt", 0) or 0) > 0
                            for node in (getattr(state, "nodes", None) or {}).values()))
    if (not isinstance(token, str) or len(token) != 64
            or snapshot.get("at_node") != len((getattr(state, "nodes", None) or {}))):
        return False
    # CODEX AGENT: node count is not lifecycle identity. Abort/reset/tag edits can change live steering
    # inputs without allocating a node, so every behavioral snapshot is bound to this exact projection.
    return token == analytics_projection_token(state)


def latest_live_snapshot(state: RunState, snapshots) -> dict:
    """The newest recorded coverage snapshot whose projection receipt STILL matches the live analytics
    projection — the one the current node-count/tagging/lifecycle would reproduce — or ``{}`` when none
    does (stale/absent => no coverage signal). The single "is this snapshot current?" reader shared by
    every consumer (proposal cues, lock-in, the Strategist context), for both `coverage_snapshots` and
    `concept_coverage_snapshots`.

    It REVERSE-SCANS rather than reading only `snapshots[-1]`: the producer de-duplicates against ANY
    prior exact receipt, so after an A→B→A retag at one node count the cadence reuses the earlier A
    snapshot while B stays last — a `[-1]`-only check would then see B fail the projection match and
    wrongly discard a still-current A (suppressing the explore/pivot cue and capability-expansion). The
    reverse scan finds the still-live A, so production and consumption pick the SAME snapshot."""
    for snapshot in reversed(snapshots or []):
        if snapshot_matches_analytics_projection(state, snapshot):
            return snapshot
    return {}


def _node_theme(node):
    """A node's theme exactly as the canonical `theme_rollup` reads it — delegated to the ONE shared
    derivation (`events.digest.node_theme`: legacy `idea.theme`, else the first concept's axis) so the
    coverage themes the Strategist sees match the digest's theme map and the /runs API. Phase 0
    (bd816a5) moved authoring from `theme` to `concepts`; sharing the helper keeps the fallback identical
    across surfaces instead of drifting."""
    return node_theme(node)


def normalized_entropy(counts) -> float:
    """Shannon entropy of a category distribution, normalized to [0,1] by log(#NON-EMPTY categories).
    1.0 = perfectly even spread across the observed (non-zero) categories; 0.0 = all mass on one
    (or <=1 non-empty category). Empty bins are dropped, not treated as categories — so this measures
    concentration among the themes ACTUALLY tried. Deterministic; empty / single-category -> 0.0."""
    total = sum(counts)
    cats = [c for c in counts if c > 0]
    if total <= 0 or len(cats) <= 1:
        return 0.0
    h = -sum((c / total) * math.log(c / total) for c in cats)
    return round(h / math.log(len(cats)), 4)


def coverage_signal(state: RunState, *, resolution: float = 1.0, recent: int = 4) -> dict:
    """Compact, deterministic breadth read over the folded run (see module docstring). Every key is
    additive/reader-defaulted; an empty run yields zeros. `recent` bounds the recency window for the
    trailing "narrowing NOW?" signal.

    PART V Phase 6a: the breadth VOCABULARY is now the folded CONCEPT AXES (`events.digest.theme_rollup`
    → `node_axes`, MULTI-membership: a node is counted under every axis it occupies, post-rename), so
    the breadth the Strategist reads matches the /concepts map and the /runs API exactly and no longer
    depends on the Researcher's first-concept AUTHORING ORDER. The dict keys keep the historical
    `theme_*` names (a wire contract the Strategist prompt / proposal_cues / UI string-match) — only the
    derivation moved. The dominant-* FRACTIONS divide by ``max(idea_nodes, axis_memberships)``: untagged
    effort still dilutes concentration, while multi-axis mass prevents a run where every node spans several
    independent axes from looking 100% concentrated merely because one of those axes is shared.

    Keys:
      nodes                - ideas proposed so far (nodes that carry an idea)
      themes               - distinct concept AXES occupied (canonical theme_rollup vocabulary)
      niches               - distinct parameter-niches among evaluated nodes (DiversityArchive)
      operators            - distinct operator kinds used
      theme_entropy        - in [0,1], 1 = spread across axes, 0 = single axis (broad<->narrow)
      dominant_theme_frac  - in [0,1], share of ALL idea-nodes touching the most-explored axis (narrowing)
      recent_dominant_frac - in [0,1], same over the last `recent` idea-nodes (narrowing NOW?)
      top_themes           - [[axis, count], ...] top few, so the LLM can name the concentration
    """
    aborted = set(getattr(state, "aborted_nodes", None) or [])
    # CODEX AGENT: breadth is live steering context. Aborted attempts remain in the event audit trail but
    # cannot dilute current coverage or the recent-window denominator.
    nodes = [n for n in state.nodes.values()
             if (getattr(n, "idea", None) is not None and n.id not in aborted
                 and not getattr(n, "tombstoned", False))]
    if not nodes:
        return {"nodes": 0, "themes": 0, "niches": 0, "operators": 0,
                "theme_entropy": 0.0, "dominant_theme_frac": 0.0,
                "recent_dominant_frac": 0.0, "top_themes": []}
    rollup = theme_rollup(state)                        # {theme: {count, best_metric}} — canonical
    counts = [v["count"] for v in rollup.values()]
    ops = {getattr(n, "operator", None) for n in nodes}
    ops.discard(None)
    niches = len(DiversityArchive(resolution).build(state))
    dominant = max(counts) if counts else 0
    # Recency window: the last `recent` NODES (by id) — then how concentrated their THEMES are. Taking
    # nodes-first (not themed-first) keeps this a genuine "narrowing NOW?" measure: a window of fresh
    # UNTITLED drafts reads as un-concentrated (0.0), not as the dominant OLD theme reaching forward.
    recent_nodes = sorted(nodes, key=lambda n: n.id)[-max(1, recent):]
    recent_axes: Counter = Counter()
    for n in recent_nodes:
        for a in node_axes(state, n):                   # count a recent node under EVERY axis it touches
            recent_axes[a] += 1
    recent_dom = recent_axes.most_common(1)
    recent_dom_count = recent_dom[0][1] if recent_dom else 0
    membership_count = sum(counts)
    recent_membership_count = sum(recent_axes.values())
    top = sorted(rollup.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:3]
    return {
        "nodes": len(nodes),
        "themes": len(rollup),
        "niches": niches,
        "operators": len(ops),
        "theme_entropy": normalized_entropy(counts),
        "dominant_theme_frac": round(dominant / max(len(nodes), membership_count), 4),
        "recent_dominant_frac": round(
            recent_dom_count / max(len(recent_nodes), recent_membership_count), 4),
        "top_themes": [[t, v["count"]] for t, v in top],
    }
