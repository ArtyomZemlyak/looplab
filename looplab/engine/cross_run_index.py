"""PART IV cross-run Step 1 / CR0 (§21.20.3) — the run "passport" + "facts" contracts, and a
deterministic index rebuilt from the run event logs (the migration over existing runs).

This is the lean foundation the design sanctions ("append-only ledgers + a rebuildable projection before
an external database is justified"): every record here is a PURE, DETERMINISTIC projection of what the run
already recorded — the append-only `events.jsonl` (folded via the existing `fold`) plus the run's
`task.snapshot.json`. Nothing here is a new source of truth; deleting the index and rebuilding it from the
logs yields a byte-identical result (the §21.20.10 CR0 gate, pinned by `tests/test_cross_run_index.py`).

- `scope_profile` — the task PASSPORT: identity + the universal `task_fingerprint` (Step 0) + goal terms.
  It deliberately does NOT invent hardcoded facet classifications (interaction/domain/language buckets) —
  that agentic faceting is the deferred `ScopeProfile`-facets work (§21.20.2); this is the honest,
  universal, deterministic core.
- `run_facts` — the run FACTS: its scope + attempts (nodes) and measurements (terminal metrics), a pure
  projection over a folded `RunState`. `ExecutionAttempt`/`Measurement` in lean, JSON-flat form.
- `build_index` / `rebuild_index_from_run_root` — build the portfolio index from folded states / by
  scanning a run root's `*/events.jsonl` (the migration over old logs).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from looplab.core.models import RunState


def scope_profile(*, task_id: str, kind: str, direction: str, goal: str, metric: str = "",
                  param_names: Optional[list[str]] = None, universal: bool = True) -> dict:
    """The task PASSPORT (§21.20.3): stable identity + the universal task fingerprint + salient goal terms.
    Deterministic and universal (no hardcoded facet lists). `param_names` (the winner's params) sharpen the
    fingerprint exactly as in `task_fingerprint`."""
    from looplab.engine.memory import _goal_tokens, task_fingerprint
    return {
        "task_id": str(task_id or ""),
        "kind": str(kind or ""),
        "direction": str(direction or "min"),
        "metric": str(metric or ""),
        "fingerprint": task_fingerprint(kind, direction, goal, metric, param_names, universal=universal),
        "goal_terms": sorted(set(_goal_tokens(goal, universal=universal))),
    }


def run_facts(state: RunState, *, kind: str = "", metric: str = "", universal: bool = True) -> dict:
    """The run FACTS (§21.20.3): the run's scope passport + its attempts (nodes) and measurements (terminal
    metrics), as a PURE projection of the folded `RunState`. `kind`/`metric` come from the run's
    `task.snapshot.json` (not carried on `RunState`); everything else is folded. Deterministic: attempts are
    emitted in node-id order, all sets sorted. This is `ExecutionAttempt`/`Measurement` in lean JSON form."""
    best = state.best()
    pnames = list((best.idea.params or {}).keys()) if best is not None and best.idea else []
    scope = scope_profile(task_id=state.task_id, kind=kind, direction=state.direction, goal=state.goal,
                          metric=metric, param_names=pnames, universal=universal)
    node_concepts = getattr(state, "node_concepts", None) or {}
    attempts = []
    for nid in sorted(state.nodes):
        nd = state.nodes[nid]
        idea = getattr(nd, "idea", None)
        concepts = node_concepts.get(nid) or node_concepts.get(str(nid)) or []
        attempts.append({
            "node_id": nid,
            "operator": str(getattr(idea, "operator", "") or "") if idea is not None else "",
            "params": dict(getattr(idea, "params", {}) or {}) if idea is not None else {},
            "status": str(getattr(nd, "status", "") or ""),
            "metric": getattr(nd, "robust_metric", None),
            "concepts": sorted(str(c) for c in concepts),
        })
    return {
        "run_id": str(getattr(state, "run_id", "") or ""),
        "scope": scope,
        "n_attempts": len(attempts),
        "attempts": attempts,
        "best": ({"node_id": best.id, "metric": best.robust_metric} if best is not None else None),
    }


def _snapshot_kind_metric(run_dir: Path) -> tuple[str, str]:
    """Best-effort (kind, metric) from a run's `task.snapshot.json` — tolerant of the legacy `kind` enum
    and the newer nested `metric.{reader,kind}` spelling (see adapters/tasks.py). Never raises."""
    try:
        snap = json.loads((run_dir / "task.snapshot.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing/garbled snapshot just yields empty facets
        return "", ""
    kind = str(snap.get("kind") or "")
    m = snap.get("metric")
    if isinstance(m, dict):
        metric = str(m.get("name") or m.get("reader") or m.get("kind") or "")
    else:
        metric = str(m or "")
    return kind, metric


def build_index(entries: list[tuple[RunState, str, str]], *, universal: bool = True) -> list[dict]:
    """Project (state, kind, metric) triples into run-facts records, sorted by run_id for a stable,
    order-independent index (the same set of runs always yields the same index)."""
    facts = [run_facts(st, kind=kind, metric=metric, universal=universal) for st, kind, metric in entries]
    facts.sort(key=lambda f: (f["run_id"], f["scope"]["task_id"]))
    return facts


def rebuild_index_from_run_root(run_root: str | Path, *, universal: bool = True) -> list[dict]:
    """Migration/rebuild over EXISTING runs: fold every `<run_root>/*/events.jsonl` and project it. Pure and
    deterministic — this is the CR0 'rebuild from scratch' path; running it twice yields the same index."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    root = Path(run_root)
    entries = []
    for ev in sorted(root.glob("*/events.jsonl")):
        try:
            st = fold(EventStore(ev).read_all())
        except Exception:  # noqa: BLE001 — one unreadable run must not sink the whole rebuild
            continue
        kind, metric = _snapshot_kind_metric(ev.parent)
        entries.append((st, kind, metric))
    return build_index(entries, universal=universal)
