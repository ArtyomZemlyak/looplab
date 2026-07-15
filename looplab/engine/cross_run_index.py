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


# Schema/mode version for the passport. `fp_mode` records the tokenizer generation IN the record (CODEX:
# "version the tokenizer/fingerprint/schema") so a reader never joins fingerprints built under different
# modes by accident. The CR0 index uses ONE consistent mode (universal) for every run it folds — it does
# not inherit each run's live `fingerprint_universal` flag, so the index is internally self-consistent.
SCOPE_SCHEMA_VERSION = 1


def scope_profile(*, task_id: str, kind: str, direction: str, goal: str, metric: str = "",
                  universal: bool = True) -> dict:
    """The task PASSPORT (§21.20.3): an IMMUTABLE task identity + fingerprint + salient goal terms.
    Deliberately derives ONLY from the immutable task (kind/direction/metric/goal) — NOT the winner's
    params, which are outcome-derived and would make identity shift when a new node wins or a run extends
    (CODEX). Result features belong on `run_facts` attempts, not the passport. `fp_mode`/`v` version the
    tokenizer+schema so records built under different modes are never silently joined."""
    from looplab.engine.memory import _goal_tokens, task_fingerprint
    return {
        "v": SCOPE_SCHEMA_VERSION,
        "fp_mode": "universal" if universal else "legacy",
        "task_id": str(task_id or ""),
        "kind": str(kind or ""),
        "direction": str(direction or "min"),
        "metric": str(metric or ""),
        "fingerprint": task_fingerprint(kind, direction, goal, metric, universal=universal),
        "goal_terms": sorted(set(_goal_tokens(goal, universal=universal))),
    }


def run_facts(state: RunState, *, kind: str = "", metric: str = "", universal: bool = True) -> dict:
    """The run FACTS (§21.20.3): the run's scope passport + its attempts (nodes) and measurements (terminal
    metrics), as a PURE projection of the folded `RunState`. `kind`/`metric` come from the run's
    `task.snapshot.json` (not carried on `RunState`); everything else is folded. Deterministic: attempts are
    emitted in node-id order, all sets sorted. This is `ExecutionAttempt`/`Measurement` in lean JSON form."""
    best = state.best()
    # The passport derives ONLY from the immutable task (no winner params) — see scope_profile (CODEX).
    scope = scope_profile(task_id=state.task_id, kind=kind, direction=state.direction, goal=state.goal,
                          metric=metric, universal=universal)
    node_concepts = getattr(state, "node_concepts", None) or {}
    attempts = []
    # NOTE (CODEX): these attempts are folded LATEST-generation facts — a `node_reset` (.1 -> reset -> .9)
    # collapses to one attempt at .9, and concept labels are raw (no concept_uid/taxonomy). Immutable
    # per-generation attempt/measurement facts (with trust/feasibility/holdout/uncertainty + concept UIDs)
    # are the full-CR0 TODO (§21.20.13); this lean projection is the deterministic-rebuild foundation.
    for nid in sorted(state.nodes):
        nd = state.nodes[nid]
        idea = getattr(nd, "idea", None)
        concepts = node_concepts.get(nid) or node_concepts.get(str(nid)) or []
        st = getattr(nd, "status", "")
        attempts.append({
            "node_id": nid,
            "operator": str(getattr(idea, "operator", "") or "") if idea is not None else "",
            "params": dict(getattr(idea, "params", {}) or {}) if idea is not None else {},
            # `.value` (a NodeStatus is a `str, Enum`) — `str(status)` would emit "NodeStatus.evaluated" (CODEX).
            "status": str(getattr(st, "value", None) or getattr(st, "name", None) or st or ""),
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
    except Exception:  # noqa: BLE001 — a missing/garbled snapshot just yields empty facets (see NOTE below)
        # NOTE (CODEX): "" here is indistinguishable from a genuinely empty facet; a full record would carry
        # explicit degraded/error provenance so an incomplete passport isn't treated as compatible evidence.
        return "", ""
    kind = str(snap.get("kind") or "")

    def _metric_of(v) -> str:
        if isinstance(v, dict):
            return str(v.get("name") or v.get("reader") or v.get("kind") or v.get("metric") or "")
        return str(v or "")

    # Metric location varies by adapter contract: top-level `metric` (dataset tasks — real snapshots use a
    # bare string here), or nested under `eval`/`cmd` for repo/cmd tasks (CODEX). Try each in order.
    metric = _metric_of(snap.get("metric"))
    for key in ("eval", "cmd"):
        if not metric and isinstance(snap.get(key), dict):
            metric = _metric_of(snap[key].get("metric"))
    return kind, metric


def build_index(entries: list[tuple[RunState, str, str]], *, universal: bool = True) -> list[dict]:
    """Project (state, kind, metric) triples into run-facts records, sorted by run_id for a stable,
    order-independent index (the same set of runs always yields the same index)."""
    facts = [run_facts(st, kind=kind, metric=metric, universal=universal) for st, kind, metric in entries]
    # `run_id` is the unique run identity, but two folded logs COULD carry the same run_id (a copied dir);
    # add a content tie-break (n_attempts, best) so the canonical order is input-order-INDEPENDENT even
    # then, instead of relying on stable-sort to preserve arrival order (CODEX). A source_uid dedup contract
    # is the portfolio-scale TODO (§21.20.3).
    # CODEX AGENT: this tie-break is still incomplete. Copied logs can share run/task/count/best metric yet
    # contain different attempts; Python's stable sort then preserves input/traversal order, so rebuilding
    # [A,B] differs from [B,A]. Include a canonical full-record/source digest and define duplicate identity
    # rejection/dedup semantics rather than silently publishing two order-dependent versions of one run.
    facts.sort(key=lambda f: (f["run_id"], f["scope"]["task_id"], f["n_attempts"],
                              str((f.get("best") or {}).get("metric"))))
    return facts


def rebuild_index_from_run_root(run_root: str | Path, *, universal: bool = True) -> list[dict]:
    """Migration/rebuild over EXISTING runs: fold every `<run_root>/*/events.jsonl` and project it. Pure and
    deterministic — this is the CR0 'rebuild from scratch' path; running it twice yields the same index."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    root = Path(run_root)
    entries = []
    # NOTE (CODEX, full-CR TODO §21.20.13): this projects one folded state at a time but holds them all
    # until build_index runs (fine for an on-demand inspector at tens–hundreds of runs); the INCREMENTAL,
    # atomic, source-digest/watermark-keyed index that streams per-run and carries skipped/incomplete
    # receipts is the CR1a substrate. Today an unreadable/torn run is SILENTLY skipped (best-effort);
    # a production rebuild must instead return explicit skip receipts so a partial corpus isn't read as complete.
    for ev in sorted(root.glob("*/events.jsonl")):
        try:
            st = fold(EventStore(ev).read_all())
        except Exception:  # noqa: BLE001 — one unreadable run must not sink the whole rebuild (see NOTE)
            continue
        kind, metric = _snapshot_kind_metric(ev.parent)
        entries.append((st, kind, metric))
    return build_index(entries, universal=universal)
