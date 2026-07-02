"""Cross-run memory (I19, ADR-10): an episodic case library over a VectorStore.
Cases are keyed by a task description embedding; `retain_if_improved` keeps a case
only when its metric beats the stored one (retain-on-improvement). This is the
top-system differentiator — solved tasks make later similar tasks easier.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from looplab.core.atomicio import atomic_write_text
from looplab.tools.vectorstore import Hit, Item, VectorStore, hash_embed

_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "on", "in", "with", "from", "predict",
         "using", "use", "data", "dataset", "model", "target", "column", "columns", "features",
         "given", "this", "that", "is", "are", "by", "your", "my", "it", "as", "at", "be"}


def task_fingerprint(kind: str, direction: str, goal: str, metric: str = "",
                     param_names: Optional[list[str]] = None) -> list[str]:
    """A cheap, deterministic content fingerprint of a task as a token SET (M2). Cross-run transfer
    should reach a *similar* task, not only the exact same `task_id` — so we key priors/lessons on the
    overlap of these tokens (Jaccard, `fingerprint_similarity`) instead of an exact id match. Tokens:
    the kind/direction/metric (weighted by prefixing), plus salient goal keywords and param names."""
    toks = {f"kind:{(kind or '').lower()}", f"dir:{(direction or '').lower()}"}
    if metric:
        toks.add(f"metric:{str(metric).lower()}")
    for w in re.findall(r"[a-z0-9]+", (goal or "").lower()):
        if len(w) > 2 and w not in _STOP:
            toks.add(w)
    for p in (param_names or []):
        toks.add(f"param:{str(p).lower()}")
    return sorted(toks)


def fingerprint_similarity(a: list[str], b: list[str]) -> float:
    """Jaccard overlap of two fingerprints in [0,1]. 1.0 = identical token sets."""
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class JsonlCaseLibrary:
    """Persistent case store (I19, ADR-10): cases on disk as JSONL, keyed by task_id
    with retain-on-improvement. Loads existing cases on init so it accumulates across
    runs. `search` does a keyword/recency lookup (no embedding dependency)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cases: list[dict] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:                       # one malformed/truncated line must not make the whole
                    self.cases.append(json.loads(line))   # cross-run memory permanently unloadable
                except json.JSONDecodeError:
                    continue

    def _flush(self) -> None:
        # Atomic (temp + os.replace): the file is rewritten WHOLE on every add(), so a non-atomic
        # write killed mid-flush would truncate and lose the entire accumulated case library.
        atomic_write_text(
            self.path, "\n".join(json.dumps(c) for c in self.cases) + "\n")

    def add(self, case: dict) -> bool:
        """Upsert by task_id, retaining the better metric. Returns True if stored."""
        tid = case.get("task_id")
        direction = case.get("direction", "min")
        metric = case.get("metric")
        prev = next((c for c in self.cases if c.get("task_id") == tid), None)
        if prev is not None:
            # Keep the old case only when both metrics are comparable and the new one is not better.
            if metric is not None and prev.get("metric") is not None:
                better = metric < prev["metric"] if direction == "min" else metric > prev["metric"]
                if not better:
                    return False
            self.cases.remove(prev)   # replace (incl. metric-None cases) — upsert by task_id
        self.cases.append(case)
        self._flush()
        return True

    def search(self, query: str, k: int = 3) -> list[dict]:
        q = set(query.lower().split())
        scored = [(len(q & set((c.get("goal", "") + " " + c.get("task_id", "")).lower().split())), c)
                  for c in self.cases]
        scored.sort(key=lambda t: -t[0])
        return [c for _, c in scored[:k]]

    def all(self) -> list[dict]:
        return list(self.cases)


class CaseLibrary:
    """Episodic case store over a `VectorStore`. Optionally *harmonic* (Memora): pass an `abstract`
    callable (see `tools.memora.make_abstractor`) to index each case by a short abstraction + cue
    anchors instead of its raw task text, CONSOLIDATE a near-duplicate case into the existing entry on
    `add`, and EXPAND `retrieve` through the top hits' anchors. With `abstract=None` (the default) every
    method is byte-identical to the pre-Memora behavior."""

    def __init__(self, store: VectorStore, embed: Callable[[str], list[float]] = hash_embed,
                 index: str = "cases", abstract: Optional[Callable[[str], object]] = None,
                 consolidate_threshold: float = 0.86, expand: bool = True):
        self.store = store
        self.embed = embed
        self.index = index
        self.abstract = abstract
        self.consolidate_threshold = consolidate_threshold
        self.expand = expand

    @staticmethod
    def _content(task_desc: str, payload: dict) -> str:
        """The rich memory VALUE the abstraction summarizes: the task plus the case's own words."""
        extra = " ".join(str(payload.get(k, "")) for k in ("rationale", "params", "operator"))
        return f"{task_desc} {extra}".strip()

    def _harmonic_item(self, case_id: str, task_desc: str, payload: dict):
        """Build the `(vector, payload, abstraction)` for a harmonic case: embed the abstraction+anchors
        (not the raw text) and carry the anchors in the payload so retrieval can expand through them."""
        ab = self.abstract(self._content(task_desc, payload))  # type: ignore[misc]
        vec = self.embed(ab.index_text())
        p = {**payload, "abstraction": ab.primary, "anchors": list(ab.anchors)}
        return Item(case_id, vec, p), ab

    def add(self, case_id: str, task_desc: str, payload: dict) -> None:
        if self.abstract is None:                       # legacy path — byte-identical to before
            self.store.upsert(self.index, [Item(case_id, self.embed(task_desc), payload)])
            return
        item, ab = self._harmonic_item(case_id, task_desc, payload)
        # Consolidation: if a stored case sits at/above the threshold under the SAME abstraction, merge
        # into it rather than growing a chain of near-duplicates (Memora: ~half the entries of a flat
        # store). Never merge onto self (a re-add of the same id is a plain upsert).
        near = self.store.search(self.index, item.vector, 1)
        if near and near[0].id != case_id and near[0].score >= self.consolidate_threshold:
            self._consolidate(near[0], ab, payload)
            return
        self.store.upsert(self.index, [item])

    def _consolidate(self, target: Hit, ab, payload: dict) -> None:
        """Fold a new case into `target`: union the anchors, keep the richer abstraction, keep the
        better metric, and re-embed the merged abstraction under the target's id."""
        from looplab.tools.memora import Abstraction
        prev = Abstraction(str(target.payload.get("abstraction", "")),
                           list(target.payload.get("anchors", [])))
        merged_ab = prev.merge(ab)
        direction = payload.get("direction") or target.payload.get("direction") or "min"
        p = {**target.payload, **payload}               # newer content wins for scalar fields
        om, nm = target.payload.get("metric"), payload.get("metric")
        if om is not None and nm is not None:
            p["metric"] = min(om, nm) if direction == "min" else max(om, nm)
        elif om is not None:
            p["metric"] = om
        p["abstraction"] = merged_ab.primary
        p["anchors"] = list(merged_ab.anchors)
        p["merged"] = int(target.payload.get("merged", 1)) + 1
        self.store.upsert(self.index, [Item(target.id, self.embed(merged_ab.index_text()), p)])

    def retrieve(self, task_desc: str, k: int = 3) -> list[Hit]:
        hits = self.store.search(self.index, self.embed(task_desc), k)
        if self.abstract is None or not self.expand:    # legacy: exactly k, no expansion
            return hits
        from looplab.tools.memora import expand_by_anchors
        extra = expand_by_anchors(self.store, self.index, hits, self.embed, k=k)
        seen = {h.id for h in hits}
        return hits + [h for h in extra if h.id not in seen]

    def retain_if_improved(self, case_id: str, task_desc: str, payload: dict,
                           metric: float, direction: str = "min") -> bool:
        """Store/replace only if better than the existing case. Returns True if stored. Keyed by
        `case_id` (no consolidation here — that would break the id-based lookup); when harmonic, the
        stored entry still carries abstraction+anchors so retrieval can expand through them."""
        existing: Optional[Hit] = None
        getter = getattr(self.store, "get", None)
        if callable(getter):
            existing = getter(self.index, case_id)
        if existing is not None:
            prev = existing.payload.get("metric")
            if prev is not None:
                better = metric < prev if direction == "min" else metric > prev
                if not better:
                    return False
        if self.abstract is None:                       # legacy: unchanged payload shape
            self.store.upsert(self.index, [Item(case_id, self.embed(task_desc),
                                                {**payload, "metric": metric})])
        else:
            item, _ = self._harmonic_item(case_id, task_desc,
                                          {**payload, "metric": metric, "direction": direction})
            self.store.upsert(self.index, [item])
        return True
