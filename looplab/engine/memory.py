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


# --------------------------------------------------------------------------- #
# D2 · Memory hygiene (Phase 3 dep): consolidation, contradiction-quarantine, forgetting.
# Misevolution (ICLR 2026, arXiv:2509.26354) shows append-only cross-run memory causes
# deployment-time reward hacking — agents repeat actions that merely correlated with past
# positive feedback. The memory surveys (arXiv:2512.13564) make consolidation & forgetting
# first-class lifecycle operations. These pure helpers implement both for lessons.jsonl.
# --------------------------------------------------------------------------- #

# Outcomes that CONFLICT with a positive lesson: if the SAME statement was later tested and
# didn't hold (or was abandoned), the earlier "supported" must not be injected any more.
_NEGATIVE = {"tested", "abandoned", "failed", "refuted"}


def normalize_statement(s: str) -> str:
    """Identity of a lesson claim: collapsed whitespace, lowercased, capped."""
    return " ".join(str(s or "").split()).lower()[:160]


def consolidate_lessons(lessons: list[dict]) -> list[dict]:
    """Merge near-duplicate lessons and resolve contradictions — the write-path hygiene pass.
    Input: lessons in FILE ORDER (oldest first). For each (normalized statement, task_id) group:
    the NEWEST entry wins (its outcome is the current verdict — forgetting the stale one), and it
    absorbs the group's support as `evidence_count`. A newer NEGATIVE verdict silently retires an
    older positive duplicate (contradiction resolution), and vice versa — last observation is the
    truth, prior observations only add confidence when they AGREE."""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for o in lessons:
        key = (normalize_statement(o.get("statement", "")), o.get("task_id"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(o)
    out: list[dict] = []
    for key in order:
        grp = groups[key]
        newest = grp[-1]
        merged = dict(newest)
        # Accumulate ACROSS runs: sum the stored evidence_count of every group member that AGREES
        # with the current (newest) verdict, so a lesson re-confirmed by N runs ends at ~N — not
        # capped at 2. A prior consolidated row already carries its accumulated count; a fresh
        # append carries 1. (Members with a conflicting verdict don't add support.)
        merged["evidence_count"] = sum(int(o.get("evidence_count", 1) or 1) for o in grp
                                       if o.get("outcome") == newest.get("outcome"))
        out.append(merged)
    return out


def filter_contradicted(scored: list[tuple[float, int, dict]]) -> list[tuple[float, int, dict]]:
    """Read-path quarantine: drop any lesson whose SAME-TASK statement carries a NEWER conflicting
    verdict elsewhere in the candidate set (e.g. an old 'supported' vs a later 'tested'/'abandoned'
    of the same claim ON THE SAME TASK). `scored` = (similarity, file_index, lesson); a higher
    file_index is newer. Keyed by (statement, task_id) — a technique that worked on task A but was
    abandoned on a DIFFERENT task B is NOT a reversal (both verdicts are legitimately kept, matching
    how `consolidate_lessons` groups). The newer verdict itself always stays — negative knowledge is
    exactly what M3 keeps."""
    latest: dict[tuple, tuple[int, str]] = {}
    for _, idx, o in scored:
        key = (normalize_statement(o.get("statement", "")), o.get("task_id"))
        cur = latest.get(key)
        if cur is None or idx > cur[0]:
            latest[key] = (idx, str(o.get("outcome", "")))
    keep: list[tuple[float, int, dict]] = []
    for sim, idx, o in scored:
        key = (normalize_statement(o.get("statement", "")), o.get("task_id"))
        newest_idx, newest_out = latest[key]
        mine = str(o.get("outcome", ""))
        if idx < newest_idx and ((mine == "supported" and newest_out in _NEGATIVE)
                                 or (mine in _NEGATIVE and newest_out == "supported")):
            continue                       # quarantined: a newer run reversed this verdict
        keep.append((sim, idx, o))
    return keep


def lesson_rank_key(sim: float, idx: int, o: dict):
    """Retrieval ranking: similarity first, then confidence × corroboration, then recency —
    so a twice-confirmed lesson from a related task beats a one-off with equal similarity."""
    conf = float(o.get("confidence", 0.5) or 0.5)
    ev = min(3, int(o.get("evidence_count", 1) or 1))
    return (-sim, -(conf * ev), -idx)


# --------------------------------------------------------------------------- #
# M4 · Auto-distilled skills: episodic → procedural memory. A technique that repeatedly won
# in a run is drafted as a candidate SKILL.md under <memory_dir>/skills/; it is PROMOTED when
# a later run with a DIFFERENT task fingerprint confirms it (won on two distinct tasks).
# --------------------------------------------------------------------------- #

def skill_slug(statement: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", "-", normalize_statement(statement)).strip("-")[:48]
    return norm or "skill"


def write_auto_skill(skills_dir: str | Path, statement: str, body: str,
                     fingerprint: list[str], task_id: str) -> Optional[Path]:
    """Draft/refresh an auto-distilled skill. New claim -> status: candidate. If a candidate
    with the same slug already exists from a DIFFERENT task fingerprint (Jaccard < 0.6), the
    technique generalized -> status: promoted. Never raises (best-effort memory)."""
    try:
        d = Path(skills_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"auto-{skill_slug(statement)}.md"
        status, fps = "candidate", [fingerprint]
        if p.exists():
            head = p.read_text(encoding="utf-8")
            m = re.search(r"fingerprints:\s*(\[.*?\])\s*$", head, re.M | re.S)
            if m:
                try:
                    fps = json.loads(m.group(1))
                except json.JSONDecodeError:
                    fps = []
            prior_status = "promoted" if "status: promoted" in head else "candidate"
            different = any(fingerprint_similarity(fingerprint, old) < 0.6 for old in fps if old)
            status = "promoted" if (different or prior_status == "promoted") else "candidate"
            if fingerprint not in fps:
                fps = (fps + [fingerprint])[-6:]
        text = ("---\n"
                f"name: auto-{skill_slug(statement)}\n"
                f"description: {normalize_statement(statement)[:120]}\n"
                "provenance: auto\n"
                f"status: {status}\n"
                f"source_task: {task_id}\n"
                f"fingerprints: {json.dumps(fps)}\n"
                "---\n\n"
                f"# {statement.strip()}\n\n{body.strip()}\n")
        atomic_write_text(p, text)
        return p
    except Exception:  # noqa: BLE001 — skill distillation is best-effort, never fails a run
        return None


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
