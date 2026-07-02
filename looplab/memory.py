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

from .atomicio import atomic_write_text
from .vectorstore import Hit, Item, VectorStore, hash_embed

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
    def __init__(self, store: VectorStore, embed: Callable[[str], list[float]] = hash_embed,
                 index: str = "cases"):
        self.store = store
        self.embed = embed
        self.index = index

    def add(self, case_id: str, task_desc: str, payload: dict) -> None:
        self.store.upsert(self.index, [Item(case_id, self.embed(task_desc), payload)])

    def retrieve(self, task_desc: str, k: int = 3) -> list[Hit]:
        return self.store.search(self.index, self.embed(task_desc), k)

    def retain_if_improved(self, case_id: str, task_desc: str, payload: dict,
                           metric: float, direction: str = "min") -> bool:
        """Store/replace only if better than the existing case. Returns True if stored."""
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
        self.add(case_id, task_desc, {**payload, "metric": metric})
        return True
