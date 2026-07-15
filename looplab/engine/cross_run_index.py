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

import hashlib
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
                  universal: bool = True, facets: Optional[dict] = None) -> dict:
    """The task PASSPORT (§21.20.3): an IMMUTABLE task identity + fingerprint + salient goal terms.
    Deliberately derives ONLY from the immutable task (kind/direction/metric/goal) — NOT the winner's
    params, which are outcome-derived and would make identity shift when a new node wins or a run extends
    (CODEX). Result features belong on `run_facts` attempts, not the passport. `fp_mode`/`v` version the
    tokenizer+schema so records built under different modes are never silently joined.

    `facets` (from the AGENTIC `task_facets`, §21.20.2) is an OPTIONAL advisory overlay — a "facets" key is
    added ONLY when passed. The FINGERPRINT never depends on it, and the deterministic index path
    (`build_index`/`rebuild_index_from_run_root`) never passes it, so CR0 rebuild stays byte-identical."""
    from looplab.engine.memory import _goal_tokens, task_fingerprint
    out = {
        "v": SCOPE_SCHEMA_VERSION,
        "fp_mode": "universal" if universal else "legacy",
        "task_id": str(task_id or ""),
        "kind": str(kind or ""),
        "direction": str(direction or "min"),
        "metric": str(metric or ""),
        "fingerprint": task_fingerprint(kind, direction, goal, metric, universal=universal),
        "goal_terms": sorted(set(_goal_tokens(goal, universal=universal))),
    }
    if facets:
        out["facets"] = {k: str(v) for k, v in facets.items() if v}   # advisory overlay only
    return out


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


def _content_digest(record: dict) -> str:
    """A canonical content hash of a full facts record — the FINAL sort tie-break so two runs that are
    otherwise identical on (run_id, task_id, n_attempts, best-metric) but differ in their attempts still
    order deterministically by content, never by input/traversal order (CODEX)."""
    return hashlib.sha1(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def build_index(entries: list[tuple[RunState, str, str]], *, universal: bool = True) -> list[dict]:
    """Project (state, kind, metric) triples into run-facts records, sorted by run_id for a stable,
    order-independent index (the same set of runs always yields the same index)."""
    facts = [run_facts(st, kind=kind, metric=metric, universal=universal) for st, kind, metric in entries]
    # `run_id` is the unique run identity, but two folded logs COULD carry the same run_id (a copied dir);
    # sort by (run_id, task_id, n_attempts, best) and then the FULL-RECORD content digest so the canonical
    # order is input-order-INDEPENDENT even when copies share all coarse keys but differ in their attempts
    # (CODEX: stable-sort would otherwise leak traversal order). A source_uid dedup contract is the
    # portfolio-scale TODO (§21.20.3); this at least makes the published order content-deterministic.
    facts.sort(key=lambda f: (f["run_id"], f["scope"]["task_id"], f["n_attempts"],
                              str((f.get("best") or {}).get("metric")), _content_digest(f)))
    return facts


def rebuild_index_from_run_root(run_root: str | Path, *, universal: bool = True) -> list[dict]:
    """Migration/rebuild over EXISTING runs: fold every `<run_root>/*/events.jsonl` and project it. Pure and
    deterministic — this is the CR0 'rebuild from scratch' path; running it twice yields the same index.
    For the digest-cached, receipted variant see `build_index_incremental`."""
    return build_index_incremental(run_root, universal=universal)["index"]


# --------------------------------------------------------------------------- #
# Incremental rebuild (full-CR §21.20.13) — source-digest cached, receipted, atomically persistable.
# Only runs whose event log / snapshot CHANGED are re-folded; unchanged runs reuse cached facts, and a
# torn/unreadable run produces an explicit SKIP receipt instead of vanishing silently (CODEX).
# --------------------------------------------------------------------------- #

def run_source_digest(run_dir: str | Path) -> str:
    """A content digest over the two files a run's facts derive from — `events.jsonl` (folded) and
    `task.snapshot.json` (kind/metric) — so a change to either invalidates the cache. "" if no event log.
    Content-addressed (not size+mtime) so it is deterministic and copy-stable; hashing bytes is far cheaper
    than re-folding, which is what the cache actually saves."""
    d = Path(run_dir)
    ev = d / "events.jsonl"
    if not ev.exists():
        return ""
    h = hashlib.sha1()
    h.update(ev.read_bytes())
    h.update(b"\x00snapshot\x00")
    snap = d / "task.snapshot.json"
    if snap.exists():
        h.update(snap.read_bytes())
    return "s_" + h.hexdigest()


def build_index_incremental(run_root: str | Path, *, prior: Optional[dict] = None,
                            universal: bool = True) -> dict:
    """Rebuild the portfolio index over `<run_root>/*/events.jsonl`, REUSING cached facts for runs whose
    `run_source_digest` is unchanged vs `prior` (from a previous `build_index_incremental`/`load_index`).
    Returns `{"index", "runs": {dir: {digest, facts}}, "receipts": {"built", "cached", "skipped"}}` where
    `skipped` is a list of `{dir, reason}` for torn/unreadable/empty runs (explicit, not silent — CODEX).
    Pure w.r.t. the on-disk logs + the passed `prior`; deterministic (index is `build_index`-sorted)."""
    from looplab.events.eventstore import EventStore
    from looplab.events.replay import fold
    root = Path(run_root)
    prior_runs = (prior or {}).get("runs") or {}
    runs: dict[str, dict] = {}
    receipts = {"built": [], "cached": [], "skipped": []}
    entries: list[tuple[RunState, str, str]] = []
    for ev in sorted(root.glob("*/events.jsonl")):
        name = ev.parent.name                         # stable per-run cache key (the run dir name)
        digest = run_source_digest(ev.parent)
        cached = prior_runs.get(name)
        if digest and cached and cached.get("digest") == digest and cached.get("facts"):
            runs[name] = {"digest": digest, "facts": cached["facts"]}
            receipts["cached"].append(name)
            continue
        try:
            st = fold(EventStore(ev).read_all())
        except Exception as e:  # noqa: BLE001 — an unreadable run becomes an explicit skip receipt, not a gap
            receipts["skipped"].append({"dir": name, "reason": f"{type(e).__name__}: {e}"[:200]})
            continue
        kind, metric = _snapshot_kind_metric(ev.parent)
        facts = run_facts(st, kind=kind, metric=metric, universal=universal)
        # A torn log folds LENIENTLY to an identity-less empty state (no run_started parsed): the lenient
        # reader never raised, but a run with no run_id AND no attempts cannot be joined/deduped and would
        # otherwise index as a phantom "" run (CODEX). Report it as a skip, not silent portfolio evidence.
        if not facts["run_id"] and facts["n_attempts"] == 0:
            receipts["skipped"].append({"dir": name, "reason": "empty/unreadable projection (no run identity)"})
            continue
        runs[name] = {"digest": digest, "facts": facts}
        receipts["built"].append(name)
        entries.append((st, kind, metric))
    # Rebuild the canonical sorted index from ALL kept facts (cached + freshly built), so the output order is
    # identical to a from-scratch `build_index` regardless of which runs were cached this pass.
    all_facts = [r["facts"] for r in runs.values()]
    all_facts.sort(key=lambda f: (f["run_id"], f["scope"]["task_id"], f["n_attempts"],
                                  str((f.get("best") or {}).get("metric")), _content_digest(f)))
    return {"index": all_facts, "runs": runs, "receipts": receipts}


def save_index(path: str | Path, result: dict) -> None:
    """Persist an incremental-index result (`{"index","runs","receipts"}`) atomically as JSON — the cache a
    later `build_index_incremental(prior=load_index(path))` reads to skip unchanged runs."""
    from looplab.core.atomicio import atomic_write_bytes
    payload = {"v": SCOPE_SCHEMA_VERSION, "runs": result.get("runs") or {}}
    atomic_write_bytes(Path(path), json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def load_index(path: str | Path) -> Optional[dict]:
    """Load a persisted incremental-index cache as a `prior` for `build_index_incremental`. None if absent
    or unreadable (a corrupt cache just forces a full rebuild — it is never a source of truth)."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a torn cache forces a clean rebuild
        return None
    return {"runs": payload.get("runs") or {}} if isinstance(payload, dict) else None
