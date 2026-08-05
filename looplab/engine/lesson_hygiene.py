"""Lesson HYGIENE — consolidation, contradiction filtering and harmonic retrieval (doc 25 EM-10).

`memory.py` is named for the episodic case library its docstring describes, and had grown to hold
five unrelated subsystems. This is the D2 lesson-hygiene one: what makes a distilled lesson survive
(`consolidate_lessons`, `_agentic_merge_lessons`), what disqualifies it (`filter_contradicted`), and
how the surviving set is retrieved and ranked (`retrieve_lessons_harmonic`, `lesson_rank_key`).

Moved VERBATIM. `_VERDICTS` and `_NEGATIVE` come along because they are the vocabulary these
functions are written in — a lesson's verdict base and the set of outcomes that count as negative —
and nothing else here decides what those mean.

`memory.py` re-exports every name, so both spellings resolve to the SAME objects and existing imports
and monkeypatch seams (notably `claims_health`'s `_NEGATIVE` / `normalize_statement`) are unaffected.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Optional

from looplab.core.text import WORD_RE
from looplab.tools.vectorstore import Hit

_NEGATIVE = {"tested", "abandoned", "failed", "refuted"}

_VERDICTS = _NEGATIVE | {"supported"}

def distilled_claim_stance(outcome: str) -> str:
    """Relation of newly distilled evidence to its literal statement.

    ``outcome`` remains action guidance (reuse/avoid), so both a GOOD conclusion and a BAD
    conclusion such as "raising LR regressed validation" support the sentence they assert.
    Untagged/unknown conclusions are neutral rather than silently promoted.
    """
    return "support" if str(outcome or "") in _VERDICTS else "neutral"

def _verdict_base(rows_newest_last: list[dict]) -> dict:
    """The ONE write-path rule for which row of a duplicate/paraphrase group carries the group's
    verdict: the NEWEST row whose outcome is a KNOWN VERDICT (`_VERDICTS`), falling back to the
    newest row when no verdict-carrying row exists. Everything else is INERT — `_NEUTRAL` ("noted",
    the untagged-reflection outcome), a missing/empty outcome (a legacy row written before the
    field existed), or an unrecognized string — because none of them is evidence the claim was
    re-adjudicated: letting such a newer row win would retire a real verdict and zero its
    accumulated evidence. A group with no verdict at all keeps its newest row (only-noted stays
    "noted"). Shared by BOTH `consolidate_lessons` (exact-key pass) and `_agentic_merge_lessons`
    (paraphrase pass) so the two passes can never drift apart."""
    return next((o for o in reversed(rows_newest_last)
                 if str(o.get("outcome") or "") in _VERDICTS), rows_newest_last[-1])

def normalize_statement(s: str) -> str:
    """Identity of a lesson claim: collapsed whitespace, lowercased, capped."""
    return " ".join(str(s or "").split()).lower()[:160]

def consolidate_lessons(lessons: list[dict], *, client=None, embed=None,
                        parser: str = "tool_call", prompts=None) -> list[dict]:
    """Merge near-duplicate lessons and resolve contradictions — the write-path hygiene pass.
    Input: lessons in FILE ORDER (oldest first). For each (normalized statement, task_id) group:
    the NEWEST VERDICT-CARRYING entry wins (its outcome is the current verdict — forgetting the
    stale one), and it absorbs the group's support as `evidence_count`. A newer NEGATIVE verdict
    silently retires an older positive duplicate (contradiction resolution), and vice versa —
    last observation is the truth, prior observations only add confidence when they AGREE.
    "noted" is neutral here exactly as on the read path (see `_verdict_base`): a newer "noted"
    duplicate never overrides an existing verdict; a group of only-noted rows keeps "noted".

    The exact-normalized pass above is the deterministic BASE. When a `client` is supplied, a second
    HYBRID + AGENT pass then merges PARAPHRASE-level duplicates the exact key misses ('raise the LR' vs
    'increase the learning rate'): per task_id, hybrid retrieval (lexical+BM25+vector) clusters
    candidates and the agent decides the true merges + a synthesized statement. Agreeing evidence is
    summed across the merged rows; a conflicting verdict never absorbs support. No client -> identical
    to the old deterministic behavior (we never merge paraphrases on the blind signal alone)."""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for o in lessons:
        # §role-split: role is part of the identity. A Researcher lesson and a Developer lesson with
        # the same statement on the same task are DIFFERENT rows (they route to different contexts) —
        # merging them would collapse both into the newest row's role and silently drop the other
        # role's copy. Same-role duplicates still merge; an untagged (shared) row stays its own group,
        # so a newer tagged same-statement row can never flip it role-restricted.
        key = (normalize_statement(o.get("statement", "")), o.get("task_id"), o.get("role"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(o)
    out: list[dict] = []
    for key in order:
        grp = groups[key]
        # The verdict-carrying base row — see `_verdict_base` for the shared rule (neutral/legacy/
        # unknown outcomes are inert). Deterministic: a pure file-order scan.
        newest = _verdict_base(grp)
        merged = dict(newest)
        # Accumulate ACROSS runs: sum the stored evidence_count of every group member that AGREES
        # with the current (newest) verdict, so a lesson re-confirmed by N runs ends at ~N — not
        # capped at 2. A prior consolidated row already carries its accumulated count; a fresh
        # append carries 1. (Members with a conflicting verdict don't add support.) De-dup by run_id
        # among the fresh single-evidence rows: a run that re-reflects (a reopened + budget-extended
        # run re-enters finalize and re-appends its own lessons) must count ONCE, not inflate the
        # count. Pre-consolidated rows (evidence_count>1) already fold multiple runs, so they always
        # add their stored weight; only raw ev==1 rows sharing a run_id collapse.
        total = 0
        seen_runs: set = set()
        for o in grp:
            if o.get("outcome") != newest.get("outcome"):
                continue
            ev = int(o.get("evidence_count", 1) or 1)
            rid = o.get("run_id")
            # Skip a FRESH single-evidence row whose run already contributed (a run re-reflecting itself).
            # A pre-consolidated row (ev>1) folds multiple runs, so it always adds its stored weight and
            # marks its representative run as seen — a later fresh re-append of that same run then dedups.
            if rid is not None and rid in seen_runs and ev == 1:
                continue
            total += ev
            if rid is not None:
                seen_runs.add(rid)
        merged["evidence_count"] = total
        out.append(merged)
    if client is None or len(out) < 2:
        return out
    return _agentic_merge_lessons(out, client=client, embed=embed, parser=parser, prompts=prompts)

def _agentic_merge_lessons(rows: list[dict], *, client, embed=None,
                           parser: str = "tool_call", prompts=None) -> list[dict]:
    """Second-pass paraphrase merge (hybrid retrieval + agent decision), per task_id, over already
    exact-deduped lesson rows. Best-effort: any failure returns `rows` unchanged. Order-preserving by
    each merged group's earliest row. `parser`/`prompts` reach the agent adjudication call (the
    run's structured-output parser + any merge_system.md PromptStore override)."""
    from looplab.search.hybrid_merge import consolidate
    # Cluster paraphrases within a (task, role) bucket — NOT across roles: the agent must never fold a
    # Researcher lesson into a Developer one (or vice versa), which `_verdict_base` below would then
    # collapse to a single role, breaking the §role-split routing. Untagged (shared) rows form their
    # own bucket and stay shared.
    by_task: dict[object, list[int]] = {}
    for i, o in enumerate(rows):
        by_task.setdefault((o.get("task_id"), o.get("role")), []).append(i)
    keep: list[tuple[int, dict]] = []                          # (earliest original index, row)
    try:
        for _tid, idxs in by_task.items():
            if len(idxs) < 2:
                keep.append((idxs[0], rows[idxs[0]]))
                continue
            texts = [str(rows[i].get("statement", "")) for i in idxs]
            for g in consolidate(texts, client, kind="research lessons", embed=embed,
                                 parser=parser, prompts=prompts):
                members = [idxs[j] for j in g["members"]]      # back to original rows indices
                # Newest wins for non-statement fields — same base rule as the exact pass above
                # (see `_verdict_base`): the newest KNOWN-verdict member carries the verdict.
                base = _verdict_base([rows[m] for m in members])
                row = dict(base)
                if len(members) > 1:
                    row["statement"] = g["merged"]
                    row["evidence_count"] = sum(int(rows[m].get("evidence_count", 1) or 1) for m in members
                                                if rows[m].get("outcome") == base.get("outcome"))
                keep.append((min(members), row))
        keep.sort(key=lambda t: t[0])
        return [row for _i, row in keep]
    except Exception:  # noqa: BLE001 — hygiene is best-effort; never drop lessons on a merge hiccup
        return rows

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

def _lesson_index_text(o: dict) -> str:
    """The memory VALUE a lesson is abstracted from for the harmonic index: its ORIGIN-TASK cues
    (the stored fingerprint tokens — kind/direction/goal-keywords) plus the lesson statement. This
    keeps the query (current task descriptor) and the indexed lessons in the same 'task-cue' space,
    so anchors actually align."""
    fp = o.get("fingerprint")
    fp_txt = " ".join(str(t) for t in fp if not str(t).startswith("param:")) if isinstance(fp, list) else ""
    return f"{fp_txt} {o.get('statement', '')}".strip()

def retrieve_lessons_harmonic(candidates, query_text, abstract, embed, *, k: int = 8,
                              min_score: float = 0.15):
    """Memora-powered lesson recall that reaches BEYOND the fingerprint-Jaccard gate: index every
    lesson by a short abstraction + cue anchors (`tools.memora`), then retrieve for the current task
    and EXPAND through the top hits' anchors — surfacing a lesson from a differently-worded but
    anchor-linked task that token-overlap (Jaccard) would miss. Returns [(similarity, idx)] for the
    matched lessons, capped just under 1.0 so an exact-task Jaccard match always outranks a
    harmonic-only hit. No-op ([]) when `abstract` is None (memora off) — the caller stays legacy.

    `candidates` = list[(idx, lesson_dict)] (all parsed lessons). Pure w.r.t. the store (a fresh
    in-memory index per call); the LLM abstractor, when used, is content-cached by memora."""
    if abstract is None or not candidates:
        return []
    from looplab.tools.memora import Abstraction, expand_by_anchors
    from looplab.tools.vectorstore import InMemoryVectorStore, Item

    store = InMemoryVectorStore()
    items: list[Item] = []
    idx_by_id: dict[str, int] = {}
    for idx, o in candidates:
        try:
            ab = abstract(_lesson_index_text(o))
            if not isinstance(ab, Abstraction):
                continue
            sid = str(idx)
            idx_by_id[sid] = idx
            items.append(Item(sid, embed(ab.index_text()),
                              {"anchors": list(ab.anchors)}))
        except Exception:  # noqa: BLE001 — one bad lesson must not sink the whole retrieval
            continue
    if not items:
        return []
    store.upsert("lessons", items)
    try:
        qab = abstract(query_text)
        qvec = embed(qab.index_text() if isinstance(qab, Abstraction) else str(query_text))
        hits: list[Hit] = store.search("lessons", qvec, k)
        hits = hits + expand_by_anchors(store, "lessons", hits, embed, k=k,
                                        exclude={h.id for h in hits})
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[float, int]] = []
    seen: set[int] = set()
    for h in hits:
        i = idx_by_id.get(h.id)
        if i is None or i in seen or h.score < min_score:
            continue
        seen.add(i)
        out.append((min(0.9, float(h.score)), i))   # cap < exact-task 1.0
    return out

def lesson_rank_key(sim: float, idx: int, o: dict):
    """Retrieval ranking: similarity first, then confidence × corroboration, then recency —
    so a twice-confirmed lesson from a related task beats a one-off with equal similarity."""
    conf = float(o.get("confidence", 0.5) or 0.5)
    ev = min(3, int(o.get("evidence_count", 1) or 1))
    return (-sim, -(conf * ev), -idx)
