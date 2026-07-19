"""Cross-run memory (I19, ADR-10): an episodic case library over a VectorStore.
Cases are keyed by a task description embedding; `retain_if_improved` keeps a case
only when its metric beats the stored one (retain-on-improvement). This is the
top-system differentiator — solved tasks make later similar tasks easier.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Callable, Optional

from looplab.core.atomicio import atomic_write_text
from looplab.core.models import NODE_CONCEPT_PROVENANCE_CLASSIFIER
from looplab.events.eventstore import read_jsonl_lenient, write_jsonl_atomic
from looplab.tools.vectorstore import Hit, Item, VectorStore, hash_embed

_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "on", "in", "with", "from", "predict",
         "using", "use", "data", "dataset", "model", "target", "column", "columns", "features",
         "given", "this", "that", "is", "are", "by", "your", "my", "it", "as", "at", "be"}


# Goal-keyword tokenizers. LEGACY (default): ASCII `[a-z0-9]+` — the original fingerprint. It has a
# silent train/serve skew for non-Latin goals: a Russian/CJK goal has ZERO `[a-z0-9]` runs, so its
# fingerprint collapses to just the kind/dir/metric/param tokens and cross-run transfer never reaches
# it (verified on the live `rubertlite` Russian run). UNIVERSAL (opt-in, `fingerprint_universal`):
# `[^\W_]+` under re.UNICODE = word runs of ANY script MINUS underscore — same splitting as the legacy
# regex (underscore stays a separator), just without the alphabet allowlist, over `.casefold()` for
# correct cross-script case folding. This is the CR Step-0 fix: remove the hardcoded charset, don't
# special-case one language. Flagged (not default) because it changes which stored fingerprints a
# LIVE run matches — a running portfolio must not silently re-key mid-flight (see docs/17 §21.20.12).
_WORD_ASCII = re.compile(r"[a-z0-9]+")
_WORD_UNICODE = re.compile(r"[^\W_]+", re.UNICODE)


def _goal_tokens(goal: str, *, universal: bool) -> list[str]:
    r"""Salient goal keywords, filtered to len>2 non-stopwords. `universal=False` is byte-identical to
    the original `[a-z0-9]+`/`.lower()`; `universal=True` keeps every script via `[^\W_]+`/`.casefold()`."""
    if universal:
        return [w for w in _WORD_UNICODE.findall((goal or "").casefold())
                if len(w) > 2 and w not in _STOP]
    return [w for w in _WORD_ASCII.findall((goal or "").lower())
            if len(w) > 2 and w not in _STOP]


def task_fingerprint(kind: str, direction: str, goal: str, metric: str = "",
                     param_names: Optional[list[str]] = None, *, universal: bool = False) -> list[str]:
    """A cheap, deterministic content fingerprint of a task as a token SET (M2). Cross-run transfer
    should reach a *similar* task, not only the exact same `task_id` — so we key priors/lessons on the
    overlap of these tokens (Jaccard, `fingerprint_similarity`) instead of an exact id match. Tokens:
    the kind/direction/metric (weighted by prefixing), plus salient goal keywords and param names.
    `universal` (opt-in) removes the ASCII-only allowlist on goal keywords so non-Latin goals are not
    silently dropped; default False keeps the legacy fingerprint byte-identical (see `_goal_tokens`)."""
    # NOTE (CODEX): kind/direction/metric are Jaccard TOKENS here, not hard compatibility gates — two
    # incompatible tasks (min/rmse vs max/recall) can clear the fuzzy floor on shared goal words. The live
    # cross-run consumer therefore applies a HARD `direction` gate on top (engine/novelty._cross_run_prior);
    # the full immutable-facet ComparisonContract that would make this rigorous is the CR0 TODO (§21.20.13).
    toks = {f"kind:{(kind or '').lower()}", f"dir:{(direction or '').lower()}"}
    if metric:
        toks.add(f"metric:{str(metric).lower()}")
    for w in _goal_tokens(goal, universal=universal):
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
# NOT here (deliberately): `_NEUTRAL` ("noted") — the neutral outcome an untagged reflection line
# gets in `parse_credit_lessons`. It is neither positive nor negative, so it must never quarantine
# a "supported" duplicate nor add support to one (an unknown/legacy outcome behaves the same way:
# not "supported" and not in this set == inert). The WRITE path honors the same neutrality via
# `_verdict_base` (the shared base-row rule for `consolidate_lessons` / `_agentic_merge_lessons`).
_NEGATIVE = {"tested", "abandoned", "failed", "refuted"}
_NEUTRAL = "noted"
# The full verdict vocabulary a row can carry; anything outside it never wins a duplicate group.
_VERDICTS = _NEGATIVE | {"supported"}
_CLAIM_STANCES = frozenset({"support", "oppose", "neutral"})


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


# --------------------------------------------------------------------------- #
# M6 · Comparative lesson distillation (MARS "Comparative Reflective Memory", doc 13 §7 item 2):
# credit-assigned lessons from PAIRS of solutions — a child vs. the parent it improved on or
# regressed from ("Solution Lessons"), and a repair vs. the failure it fixed ("Debugging
# Lessons") — instead of only one-shot reflection over a ranked list. Pure helpers: the
# orchestrator owns the LLM call; these select pairs, assign deterministic param-level credit
# (the offline fallback), render code diffs, and parse the LLM's per-pair verdicts.
# --------------------------------------------------------------------------- #

def _improvement(child_metric: float, parent_metric: float, direction: str) -> float:
    """Signed improvement of child over parent, positive = better (direction-aware)."""
    return ((child_metric - parent_metric) if direction == "max"
            else (parent_metric - child_metric))


def select_comparison_pairs(state, k: int = 3, exclude=None) -> list[dict]:
    """Deterministically pick the most informative parent→child pairs to distill from. Two kinds:
    `solution` (both evaluated — the biggest |Δ| wins and regressions are as informative as wins;
    exact ties are skipped: the outcome vocabulary has no 'no effect', so a Δ=0 pair could only be
    mislabeled) and `debug` (parent FAILED, child evaluated — what fixed it). `exclude` = (child,
    parent) id tuples already distilled (later firings must not re-spend LLM budget on the same
    pair). Sorted debug-first then by |Δ| then by ids, so the output is stable under replay."""
    from looplab.core.models import NodeStatus
    excl = {tuple(p) for p in (exclude or [])}
    pairs: list[dict] = []
    for n in state.nodes.values():
        if n.metric is None:
            continue
        for pid in n.parent_ids:
            p = state.nodes.get(pid)
            if p is None or (n.id, pid) in excl:
                continue
            if p.metric is not None:
                delta = _improvement(n.metric, p.metric, state.direction)
                if delta != 0:
                    pairs.append({"kind": "solution", "a": n.id, "b": pid, "delta": delta})
            elif p.status is NodeStatus.failed:
                pairs.append({"kind": "debug", "a": n.id, "b": pid, "delta": None})
    pairs.sort(key=lambda pr: (0 if pr["kind"] == "debug" else 1,
                               -abs(pr["delta"] or 0.0), pr["a"], pr["b"]))
    return pairs[:max(0, k)]


def param_credit_statement(winner, loser, delta: float):
    """Deterministic (offline) credit assignment for a solution pair: when the two ideas differ in
    a SMALL number of params, the changed params ARE the credited difference. None when the diff
    is empty, too wide to attribute cleanly (>3 params), or the metric didn't move (a Δ=0 change
    is neither GOOD nor BAD) — no lesson beats a mushy lesson."""
    if not delta:
        return None
    pa = dict(getattr(winner.idea, "params", None) or {})
    pb = dict(getattr(loser.idea, "params", None) or {})
    changed = [(name, pb.get(name), pa.get(name))
               for name in sorted(set(pa) | set(pb)) if pa.get(name) != pb.get(name)]
    if not changed or len(changed) > 3:
        return None
    diff_txt = ", ".join(f"{name} {old!r}->{new!r}" for name, old, new in changed)
    verb = "improved" if delta > 0 else "regressed"
    return f"changing {diff_txt} {verb} the metric by {abs(delta):.4g}"


def code_diff(old: str, new: str, max_lines: int = 60) -> str:
    """Compact unified diff of two solutions (the comparative prompt's evidence). Empty when
    either side has no code or the codes are identical."""
    import difflib
    if not (old or "").strip() or not (new or "").strip():
        return ""
    lines = list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(),
                                      fromfile="loser", tofile="winner", lineterm="", n=2))
    return "\n".join(lines[:max_lines])


_PAIR_LINE = re.compile(r"^P(\d+)\b\s*[:.\-]?\s*(.*)$", re.I)


def parse_credit_lessons(text: str, n_pairs: int, limit: Optional[int] = None) -> list[tuple[int, str, str]]:
    """Parse the LLM's per-pair verdict lines (`P<n> [GOOD|BAD] <lesson>`) into
    (pair_index, statement, outcome) tuples. pair_index is -1 when the line carries no usable
    P-marker (the lesson still counts, unattributed). Tolerant of bullets/numbering; capped.

    `n_pairs` only clamps index VALIDITY (a P-marker beyond the real pair count collapses to -1).
    The COUNT cap is `limit` (default `max(3, n_pairs)` for the comparative caller, whose lessons
    naturally track its pair count). The whole-run reflection caller passes n_pairs=0 (its lines
    carry no valid P-marker) and MUST pass an explicit limit — otherwise the default max(3,0)=3
    silently capped reflection lessons at 3 instead of the intended 8 (architecture-review M6)."""
    cap = limit if limit is not None else max(3, n_pairs)
    out: list[tuple[int, str, str]] = []
    for line in (text or "").splitlines():
        s = line.strip().lstrip("-*•0123456789.) ").strip()
        m = _PAIR_LINE.match(s)
        idx = (int(m.group(1)) - 1) if m else -1
        body = m.group(2) if m else s
        low = body.lower()
        good, bad = "[good]" in low, "[bad]" in low
        body = re.sub(r"\[(good|bad)\]", "", body, flags=re.I).strip(" :-–")
        if len(body) < 8:
            continue
        # An UNTAGGED line gets the NEUTRAL outcome `_NEUTRAL` ("noted") — the model didn't say
        # which way the evidence points, so the lesson must neither corroborate nor contradict
        # anything. The old default was "tested", which is in `_NEGATIVE`: one tag-noncompliant
        # reflection line could quarantine a matching "supported" lesson at read time
        # (filter_contradicted). "noted" is excluded from both sides by construction (not
        # "supported", not in _NEGATIVE); rows already stored with the old value keep their
        # (negative) meaning — no migration, readers tolerate both.
        out.append((idx if 0 <= idx < n_pairs else -1, body,
                    "failed" if bad else ("supported" if good else _NEUTRAL)))
        if len(out) >= cap:
            break
    return out


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
        self._reload()   # load existing cases (same malformed-line-tolerant parse used on every reload)

    def _reload(self) -> None:
        """(Re)read the on-disk cases into `self.cases`. Used by __init__ AND inside the interprocess
        lock on every add() so a concurrent run's cases aren't clobbered by this run's stale in-memory
        copy. One malformed/truncated line is skipped, never making the whole cross-run memory
        permanently unloadable."""
        rows = read_jsonl_lenient(self.path, loads=json.loads, dicts_only=True)
        # CODEX AGENT: a syntactically-valid scalar (or a dict with an incompatible metric/params
        # shape) is still a poisoned case row. Quarantine it here so search and the next locked
        # upsert cannot crash on ``.get`` or on a cross-type metric comparison.
        self.cases = [case for case in rows if self._valid_case(case)]

    @staticmethod
    def _valid_case(case) -> bool:
        if not isinstance(case, dict):
            return False
        task_id = case.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 500:
            return False
        for key, maximum in (("goal", 4000), ("rationale", 8000)):
            value = case.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                return False
        direction = case.get("direction", "min")
        if direction not in ("min", "max"):
            return False
        metric = case.get("metric")
        if not _finite_metric(metric):
            return False
        params = case.get("params")
        return params is None or isinstance(params, dict)

    def _flush(self) -> None:
        # Atomic (temp + os.replace): the file is rewritten WHOLE on every add(), so a non-atomic
        # write killed mid-flush would truncate and lose the entire accumulated case library.
        # (add() guarantees self.cases is non-empty here, so the output is byte-identical to the
        #  historical "\n".join(...) + "\n" form.)
        write_jsonl_atomic(self.path, self.cases, dumps=json.dumps)

    def add(self, case: dict) -> bool:
        """Upsert by task_id, retaining the better metric. Returns True if stored.

        Under the same best-effort interprocess lock the lessons store / event store use: `add` is a
        full-file read-modify-write, so two runs sharing `memory_dir` (the live-share scenario) would
        otherwise clobber each other's cases (the loser's case vanishes). We RE-READ inside the lock so
        this run's possibly-stale in-memory `self.cases` can't overwrite a concurrent run's write."""
        if not self._valid_case(case):
            return False
        from looplab.events.eventstore import _interprocess_lock
        with _interprocess_lock(Path(str(self.path) + ".lock")):
            self._reload()
            return self._add_locked(case)

    def _add_locked(self, case: dict) -> bool:
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
        try:
            limit = max(0, min(int(k), 64))
        except (TypeError, ValueError, OverflowError):
            return []
        if not isinstance(query, str) or not limit:
            return []
        q = set(query.lower().split())
        scored = [(len(q & set((c.get("goal", "") + " " + c.get("task_id", "")).lower().split())), c)
                  for c in self.cases]
        scored.sort(key=lambda t: -t[0])
        return [c for _, c in scored[:limit]]

    def all(self) -> list[dict]:
        return list(self.cases)


# Schema version for the durable capsule record — bump when the shape changes so a reader can migrate/
# reject incompatible generations instead of silently mis-reading them (a CODEX finding; the full record
# — evidence node-refs, visibility/retention/purge key, concept UID+taxonomy version — is the CR1a TODO).
# v2 makes the evidence producer explicit. V1 and unversioned rows predate the authored-vs-classifier
# trust boundary and cannot be upgraded honestly from their payload alone, so readers quarantine them.
CONCEPT_CAPSULE_VERSION = 2
_LEGACY_CONCEPT_CAPSULE_VERSION = 1

_MAX_CAPSULE_ID_CHARS = 500
_MAX_CAPSULE_TOKEN_CHARS = 500
_MAX_CAPSULE_FINGERPRINT = 256
_MAX_CAPSULE_CONCEPTS = 256
_MAX_CAPSULE_OUTCOMES = 256
_MAX_CAPSULE_SOURCE_ITEMS = (1 << 31) - 1
_MAX_OVERVIEW_CONCEPTS = 512
_MAX_OVERVIEW_RUNS_PER_CONCEPT = 64
_MAX_OVERVIEW_RUN_CARDS = 512
_MAX_OVERVIEW_CARD_CONCEPTS = 64


def _finite_metric(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _capsule_completeness(
        capsule: dict, stem: str, included: int,
) -> Optional[tuple[Optional[int], Optional[int], bool]]:
    """Read one additive capsule completeness triplet; old v2 rows are valid but UNKNOWN/partial."""
    total_key, omitted_key, complete_key = f"{stem}_total", f"{stem}_omitted", f"{stem}_complete"
    present = [key in capsule for key in (total_key, omitted_key, complete_key)]
    if not any(present):
        # Old v2 writers silently capped collections. Their retained observations remain useful, but neither
        # the original total nor completeness can be reconstructed honestly from the durable row.
        return None, None, False
    if not all(present):
        return None
    total, omitted, complete = capsule[total_key], capsule[omitted_key], capsule[complete_key]
    if (type(total) is not int or type(omitted) is not int or type(complete) is not bool
            or not 0 <= total <= _MAX_CAPSULE_SOURCE_ITEMS
            or not 0 <= omitted <= _MAX_CAPSULE_SOURCE_ITEMS
            or total < included or omitted != total - included or complete != (omitted == 0)):
        return None
    return total, omitted, complete


def _capsule_source_summary(capsules: list[dict]) -> dict:
    """Aggregate explicit source-omission receipts from already-validated capsules."""
    concept_omitted = outcome_omitted = partial = unknown = 0
    for capsule in capsules:
        concept_meta = _capsule_completeness(capsule, "concepts", len(capsule.get("concepts") or []))
        outcome_meta = _capsule_completeness(
            capsule, "concept_outcomes", len(capsule.get("concept_outcomes") or {}))
        # Callers pass validated rows; keep this total if a future caller violates that private contract.
        if concept_meta is None or outcome_meta is None:
            partial += 1
            continue
        concept_omitted += concept_meta[1] or 0
        outcome_omitted += outcome_meta[1] or 0
        unknown += int(concept_meta[0] is None or outcome_meta[0] is None)
        partial += int(not concept_meta[2] or not outcome_meta[2])
    return {
        "source_complete": partial == 0,
        "partial_capsules": partial,
        "source_unknown_capsules": unknown,
        "source_concepts_omitted": concept_omitted,
        "source_outcomes_omitted": outcome_omitted,
    }


def _capsule_fingerprint_scope_complete(capsule: dict) -> bool:
    """Whether a capsule's persisted fingerprint is an exact source projection.

    Related-task transfer treats this as an applicability boundary.  Exact ``task_id`` matches do not need
    the fuzzy fingerprint, but a capped or legacy-unknown fingerprint must never authorize a foreign task.
    """
    if not isinstance(capsule, dict):
        return False
    fingerprint = capsule.get("fingerprint")
    if not isinstance(fingerprint, list):
        return False
    meta = _capsule_completeness(capsule, "fingerprint", len(fingerprint))
    return meta is not None and meta[2] is True


def _valid_capsule_record(capsule) -> bool:
    """Validate one durable capsule without coercing semantic identity.

    Oversized or ill-typed rows are quarantined rather than truncated: truncating a run id or concept
    slug could alias two distinct durable entities.
    """
    if not isinstance(capsule, dict):
        return False
    # CODEX AGENT: missing `v` is legacy, never the current schema. Defaulting it to the current version
    # would silently bless old proposer-authored labels after a schema bump.
    version = capsule.get("v", _LEGACY_CONCEPT_CAPSULE_VERSION)
    run_id = capsule.get("run_id")
    task_id = capsule.get("task_id", "")
    fingerprint = capsule.get("fingerprint")
    concepts = capsule.get("concepts")
    outcomes = capsule.get("concept_outcomes", {})
    # Phase 1 profit signs are ADDITIVE over v2: a missing field defaults to {} (old capsules stay valid);
    # a present one must be a bounded dict of {concept -> -1|0|1} (bool excluded — it is an int subclass).
    signs = capsule.get("concept_signs", {})
    if (version != CONCEPT_CAPSULE_VERSION
            or capsule.get("concept_evidence") != NODE_CONCEPT_PROVENANCE_CLASSIFIER
            or not isinstance(run_id, str) or not run_id or len(run_id) > _MAX_CAPSULE_ID_CHARS
            or not isinstance(task_id, str) or len(task_id) > _MAX_CAPSULE_ID_CHARS
            or capsule.get("direction", "min") not in ("min", "max")
            or not _finite_metric(capsule.get("best_metric"))
            or not isinstance(fingerprint, list) or len(fingerprint) > _MAX_CAPSULE_FINGERPRINT
            or not isinstance(concepts, list) or len(concepts) > _MAX_CAPSULE_CONCEPTS
            or not isinstance(outcomes, dict) or len(outcomes) > _MAX_CAPSULE_OUTCOMES
            or not isinstance(signs, dict) or len(signs) > _MAX_CAPSULE_OUTCOMES):
        return False
    if any(not isinstance(value, str) or not value or len(value) > _MAX_CAPSULE_TOKEN_CHARS
           for value in fingerprint + concepts):
        return False
    from looplab.core.concepts import valid_concept_id
    concept_set = set(concepts)
    # CODEX AGENT: a capsule is a durable evidence boundary. Quarantine the entire poisoned row instead of
    # letting one invalid/out-of-membership key disagree with canonical run cards and concept projections.
    if (len(concept_set) != len(concepts)
            or any(not valid_concept_id(value) for value in concepts)
            or any(not valid_concept_id(key) or key not in concept_set for key in outcomes)
            or any(not valid_concept_id(key) or key not in outcomes for key in signs)):
        return False
    if any(not isinstance(key, str) or not key or len(key) > _MAX_CAPSULE_TOKEN_CHARS
           or type(value) is not int or value not in (-1, 0, 1) for key, value in signs.items()):
        return False   # `type(value) is not int` rejects bool (int subclass) AND float 1.0 in one test
    if not all(isinstance(key, str) and key and len(key) <= _MAX_CAPSULE_TOKEN_CHARS
               and _finite_metric(value) for key, value in outcomes.items()):
        return False
    return (_capsule_completeness(capsule, "fingerprint", len(fingerprint)) is not None
            and _capsule_completeness(capsule, "concepts", len(concepts)) is not None
            and _capsule_completeness(capsule, "concept_outcomes", len(outcomes)) is not None)


def _dedup_valid_capsules(capsules) -> list[dict]:
    """Quarantine + deterministically de-duplicate a raw capsule sequence: keep only valid records, collapse
    duplicate run ids to ONE, and return them in sorted-run-id order. The shared portfolio read-models feed
    this RAW decoded rows (a caller may concatenate shards or hand a pre-compaction file), so the collision
    winner must be INPUT-ORDER-INDEPENDENT — pick the row with the lexicographically-greatest canonical JSON
    (a stable representative), not "last seen in list order". The store path has unique run ids, so it never
    collides; this only bites the raw-row callers the docstring promises to tolerate."""
    by_run: dict[str, dict] = {}
    for capsule in capsules if isinstance(capsules, (list, tuple)) else []:
        if not _valid_capsule_record(capsule):
            continue
        rid = capsule["run_id"]
        prev = by_run.get(rid)
        if prev is None or json.dumps(capsule, sort_keys=True) > json.dumps(prev, sort_keys=True):
            by_run[rid] = capsule
    return [by_run[run_id] for run_id in sorted(by_run)]


# A concept whose outcome sits within this fraction of the run's own outcome SPREAD around the median is
# scored NEUTRAL, not forced onto a side. Without it a median split labels ~half the concepts helped and
# half hurt every run (a weak, self-balancing signal); the band lets only concepts that clearly out- or
# under-performed their run's field carry a sign, so the cross-run rollup is sparser and more meaningful.
_CONCEPT_NEUTRAL_BAND_FRAC = 0.10


def _concept_profit_signs(outcomes: dict, direction: str) -> dict:
    """PART V Phase 1: a direction-normalized, scale-free RANK-WITHIN-RUN profit sign per concept.

    Raw metrics do NOT compare across runs/tasks (the portfolio overview refuses to aggregate them), so the
    sign is deliberately RELATIVE, not a fixed-baseline profit: "did this concept's best outcome land in the
    BETTER or WORSE part of THIS run's own field of concepts, judged in THIS run's direction". That per-run
    rank IS direction-/scale-normalized, so it aggregates across runs into an advisory tendency (a concept
    that consistently ranks well across many DIFFERENT sibling sets is a decent bet) — but it is a rank, not
    causal proof, and the baseline-stable "did ADDING this concept beat the parent" signal is Phase 3's
    per-node delta. Baseline is the run's own MEDIAN outcome; a NEUTRAL BAND (a fraction of the run's outcome
    spread) around it keeps near-median concepts off both sides, so the split is not forced ~50/50. +1 = clearly
    better half, -1 = clearly worse half, 0 = neutral. Fewer than two outcomes → no signal (empty). Pure/
    deterministic; keys mirror `outcomes` (already bounded by the caller)."""
    values = sorted(v for v in outcomes.values() if isinstance(v, (int, float))
                    and not isinstance(v, bool) and math.isfinite(v))
    if len(values) < 2:
        return {}
    n = len(values)
    baseline = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2.0
    band = _CONCEPT_NEUTRAL_BAND_FRAC * (values[-1] - values[0])   # within-run, scale-relative neutral zone
    signs: dict[str, int] = {}
    for concept, metric in outcomes.items():
        if not (isinstance(metric, (int, float)) and not isinstance(metric, bool) and math.isfinite(metric)):
            continue
        if abs(metric - baseline) <= band:
            signs[concept] = 0
        elif (metric < baseline) if direction == "min" else (metric > baseline):
            signs[concept] = 1
        else:
            signs[concept] = -1
    return signs


def concept_profit_tendencies(concept_rows, *, limit: Optional[int] = None) -> dict:
    """Split rolled-up concept rows (each with n_helped/n_neutral/n_hurt, from `portfolio_concept_overview`)
    into CONSISTENT, multi-run help/hurt tendencies — the SINGLE source of truth for every advisory surface
    (the context pack, the cross_run_atlas tool, any future one), so the threshold can never silently diverge
    between them. A concept qualifies when it carried a sign in ≥2 runs, landed on ONE side in ≥2 of them, and
    net that way (n_helped>n_hurt for help, mirror for hurt) — so a concept can never be in both, and a
    mixed/thin one is in neither. Returns {"helps": [(concept, n_helped), …], "hurts": [(concept, n_hurt), …]},
    each ranked by that count desc then name. Pure/deterministic; ADVISORY tendency, never a selection input."""
    rows = concept_rows if isinstance(concept_rows, (list, tuple)) else []

    def _int(x) -> int:                          # torn/hand-built rows may carry null/str counts
        return x if isinstance(x, int) and not isinstance(x, bool) else 0

    def _pick(is_help: bool) -> list:
        out = []
        for e in rows:
            if not isinstance(e, dict):
                continue
            h, n, t = _int(e.get("n_helped")), _int(e.get("n_neutral")), _int(e.get("n_hurt"))
            if h + n + t < 2:
                continue
            if (h >= 2 and h > t) if is_help else (t >= 2 and t > h):
                out.append((str(e.get("concept") or ""), h if is_help else t))
        out.sort(key=lambda kv: (-kv[1], kv[0]))
        return out[:limit] if limit else out

    return {"helps": _pick(True), "hurts": _pick(False)}


def build_concept_capsule(*, run_id: str, fingerprint: list[str], direction: str,
                          concepts, best_metric=None, concept_outcomes: Optional[dict] = None,
                          task_id: str = "") -> dict:
    """A compact per-run CONCEPT capsule — the cross-run bridge (§21.20 Step 2). It records WHICH
    concepts a run explored (the shipped per-run `node_concepts` tags — no new tagger) and how it went,
    keyed by `task_fingerprint`, so a later SIMILAR run can answer "was this tried across runs, and
    with what result?" and feed `grade_novelty`'s `prior_concepts` (D3 level 3 = surface prior, never
    reject). Carries a schema `v` + `task_id` scope; deliberately small and JSON-flat (memory-store data,
    not a fold event)."""
    fingerprint_collection = isinstance(fingerprint, (list, tuple, set))
    fingerprint_source = fingerprint if fingerprint_collection else []
    concepts_collection = isinstance(concepts, (list, tuple, set))
    concepts_source = concepts if concepts_collection else []
    valid_fingerprint = sorted({token for token in fingerprint_source
                                if isinstance(token, str) and token
                                and len(token) <= _MAX_CAPSULE_TOKEN_CHARS})
    invalid_fingerprint = sum(
        not isinstance(token, str) or not token or len(token) > _MAX_CAPSULE_TOKEN_CHARS
        for token in fingerprint_source
    ) + int(not fingerprint_collection)
    bounded_fingerprint = valid_fingerprint[:_MAX_CAPSULE_FINGERPRINT]
    if not isinstance(direction, str) or direction not in ("min", "max"):
        # CODEX AGENT: direction controls both sign polarity and task-family scope. A writer typo must fail
        # closed, never be coerced to `min` and persisted as inverted cross-run evidence.
        raise ValueError("concept capsule direction must be exactly 'min' or 'max'")
    normalized_direction = direction
    from looplab.core.concepts import valid_concept_id
    valid_concepts = sorted({raw for raw in concepts_source if valid_concept_id(raw)})
    invalid_concepts = sum(not valid_concept_id(raw) for raw in concepts_source) + int(not concepts_collection)
    bounded_concepts = valid_concepts[:_MAX_CAPSULE_CONCEPTS]
    concept_set = set(valid_concepts)
    outcomes_mapping = isinstance(concept_outcomes, dict)
    raw_outcomes = concept_outcomes if outcomes_mapping else {}
    all_outcomes: dict[str, object] = {}
    for raw_key, value in sorted(raw_outcomes.items(), key=lambda item: str(item[0])):
        if not valid_concept_id(raw_key) or raw_key not in concept_set or not _finite_metric(value):
            continue
        all_outcomes[raw_key] = value
    # CODEX AGENT: compute the run-relative baseline over the COMPLETE valid source field. Truncating first
    # shifts its median/neutral band and can reverse the persisted sign of retained concepts.
    all_signs = _concept_profit_signs(all_outcomes, normalized_direction)
    bounded_concept_set = set(bounded_concepts)
    bounded_outcomes = dict(list((
        (key, value) for key, value in sorted(all_outcomes.items()) if key in bounded_concept_set
    ))[:_MAX_CAPSULE_OUTCOMES])
    concept_signs = {key: all_signs[key] for key in bounded_outcomes if key in all_signs}
    # Invalid source entries are omitted evidence too. Count them in the receipt so filtering cannot turn a
    # poisoned input into a capsule that claims its source was exact/complete.
    concepts_total = len(valid_concepts) + invalid_concepts
    concepts_omitted = concepts_total - len(bounded_concepts)
    outcomes_total = len(raw_outcomes) + int(concept_outcomes is not None and not outcomes_mapping)
    outcomes_omitted = outcomes_total - len(bounded_outcomes)
    fingerprint_total = len(valid_fingerprint) + invalid_fingerprint
    fingerprint_omitted = fingerprint_total - len(bounded_fingerprint)
    return {
        "v": CONCEPT_CAPSULE_VERSION,
        "concept_evidence": NODE_CONCEPT_PROVENANCE_CLASSIFIER,
        "run_id": str(run_id or ""),
        "task_id": str(task_id or ""),
        "fingerprint": bounded_fingerprint,
        "fingerprint_total": fingerprint_total,
        "fingerprint_omitted": fingerprint_omitted,
        "fingerprint_complete": fingerprint_omitted == 0,
        "direction": normalized_direction,
        "concepts": bounded_concepts,
        "concepts_total": concepts_total,
        "concepts_omitted": concepts_omitted,
        "concepts_complete": concepts_omitted == 0,
        "best_metric": best_metric if _finite_metric(best_metric) else None,
        "concept_outcomes": bounded_outcomes,
        "concept_outcomes_total": outcomes_total,
        "concept_outcomes_omitted": outcomes_omitted,
        "concept_outcomes_complete": outcomes_omitted == 0,
        # PART V Phase 1: a direction-normalized RANK-WITHIN-RUN sign per concept (+1 clearly-better-half /
        # 0 neutral / -1 clearly-worse-half vs this run's own field) — additive over v2 (old capsules lack
        # it, readers default {}). Relative rank, not causal profit; the per-node delta is Phase 3.
        "concept_signs": concept_signs,
    }


class ConceptCapsuleStore:
    """Cross-run CONCEPT memory: one capsule per run (see `build_concept_capsule`), keyed by
    task-fingerprint similarity rather than exact `task_id` so evidence reaches SIMILAR tasks. Mirrors
    `JsonlCaseLibrary` exactly — atomic whole-file write, malformed-line-tolerant reload, best-effort
    interprocess-locked upsert by `run_id` (a re-run REPLACES its own capsule, never duplicates it, and
    a concurrent run sharing `memory_dir` can't clobber it). Pure store: it does no tagging and holds no
    engine state, so it is trivially testable and stays off the live path unless a flag wires it in."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capsules: list[dict] = []
        self._reload()

    @staticmethod
    def _valid_capsule(c: dict) -> bool:
        """Per-row schema guard (CODEX): `dicts_only` alone lets a row with an int `fingerprint` or a
        string `concepts` poison retrieval (a string iterates into CHARACTER concepts). Quarantine the
        bad row instead of letting it disable the feature: require the list-typed fields to be lists and
        `run_id` to be a non-empty string. Unknown extra fields are fine (forward-compat)."""
        # Missing `v` and v1 predate concept-producer provenance. They cannot be distinguished from
        # proposer-authored self-labels, so both fail closed alongside explicit unknown versions.
        return _valid_capsule_record(c)

    def _reload(self) -> None:
        rows = read_jsonl_lenient(self.path, loads=json.loads, dicts_only=True)
        self.capsules = [c for c in rows if self._valid_capsule(c)]   # drop poisoned rows, keep the rest

    def add(self, capsule: dict) -> bool:
        """Upsert by `run_id` under the same interprocess lock the case/lesson stores use, re-reading
        inside the lock so a concurrent run's capsule survives. Returns True once stored."""
        from looplab.events.eventstore import _interprocess_lock
        if not self._valid_capsule(capsule):
            return False
        rid = str(capsule.get("run_id") or "")
        with _interprocess_lock(Path(str(self.path) + ".lock"), required=True):
            rows = read_jsonl_lenient(self.path, loads=json.loads, dicts_only=True)
            # CODEX AGENT: quarantine is a read policy, not permission to erase old/future durable data.
            # Preserve every decoded row we do not understand; an upsert supersedes only the exact run id.
            persisted = [row for row in rows if str(row.get("run_id") or "") != rid]
            persisted.append(capsule)
            write_jsonl_atomic(self.path, persisted, dumps=json.dumps)
            self.capsules = [row for row in persisted if self._valid_capsule(row)]
        return True

    def prior_capsules(self, fingerprint: list[str], *, min_sim: float = 0.3,
                       exclude_run_id: str = "", task_id: str = "") -> list[tuple[float, dict]]:
        """Prior-run capsules in an exact task or with an exact fingerprint clearing ``min_sim``.

        Fingerprint matching is Jaccard/universal-aware because the fingerprint itself already is.  Exact
        task identity, when supplied, scores 1.0 without consulting a potentially legacy/capped fingerprint.
        Results are most-similar first, excluding this run; each tuple is ``(similarity, capsule)`` so a
        surfacing cue can rank and cite by run.
        """
        # NOTE (CODEX, full-CR TODO §21.20.13 CR2a): O(portfolio) scan+sort per call, on top of the
        # whole-file reload/rewrite this store inherits from JsonlCaseLibrary — fine at tens–hundreds of
        # runs, replaced by a bounded scope/version-keyed index snapshot at portfolio scale.
        out = []
        for c in self.capsules:
            if exclude_run_id and str(c.get("run_id") or "") == str(exclude_run_id):
                continue
            exact_task = bool(task_id) and str(c.get("task_id") or "") == str(task_id)
            # CODEX AGENT: the writer bounds fingerprints.  A retained prefix (or a pre-receipt v2 row)
            # can inflate Jaccard and is not authority for related-task transfer.  Exact task identity is
            # still usable because it does not depend on the lossy fingerprint projection.
            if not exact_task and not _capsule_fingerprint_scope_complete(c):
                continue
            sim = 1.0 if exact_task else fingerprint_similarity(fingerprint, c.get("fingerprint") or [])
            if sim >= min_sim:
                out.append((sim, c))
        out.sort(key=lambda t: (-t[0], str(t[1].get("run_id") or "")))
        return out

    def prior_concepts(self, fingerprint: list[str], *, min_sim: float = 0.3,
                       exclude_run_id: str = "", task_id: str = "") -> set[str]:
        """The UNION of concepts explored by similar prior runs — exactly the `set[str]` shape
        `grade_novelty(prior_concepts=…)` consumes to fire D3 level 3 ("tried across runs")."""
        acc: set[str] = set()
        for _sim, c in self.prior_capsules(
                fingerprint, min_sim=min_sim, exclude_run_id=exclude_run_id, task_id=task_id):
            acc.update(str(x) for x in (c.get("concepts") or []))
        return acc

    def all(self) -> list[dict]:
        return list(self.capsules)


def portfolio_concept_overview(capsules: list[dict], *, aliases: Optional[dict] = None,
                               splits: Optional[dict] = None) -> dict:
    """A cross-run portfolio read-model over concept capsules (§21.20 Step 3 — 'what has been tried across
    the portfolio'). Pure/deterministic, no LLM/IO, drillable to `run_id`. For each concept it lists the
    runs that explored it with THEIR OWN outcome (run_id, metric, direction) — deliberately NOT a single
    cross-run 'best', because raw metrics from different tasks/directions are not comparable without a
    shared contract (§21.20.1). Also emits a per-run card (concept count + the run's own best_metric).
    `aliases` (from `load_concept_aliases`, CR1a) canonicalizes concept slugs at read time: merged aliases
    collapse to one concept and purged concepts drop; `splits` (from `load_concept_splits`) re-tags a coarse
    concept per that run's OWN sibling concepts. The raw per-run tags are untouched (non-destructive)."""
    from looplab.engine.concept_registry import canonicalize_concept, canonicalize_concepts

    valid_capsules = _dedup_valid_capsules(capsules)

    per_concept: dict[str, dict] = {}
    for c in valid_capsules:
        rid = str(c.get("run_id") or "")
        oc = c.get("concept_outcomes") or {}
        outcome_meta = _capsule_completeness(
            c, "concept_outcomes", len(c.get("concept_outcomes") or {}))
        # CODEX AGENT: pre-receipt v2 writers could truncate BEFORE computing rank signs. Keep their positive
        # concept/outcome observations, but never aggregate a sign whose comparison field may be incomplete.
        signs = (c.get("concept_signs") or {}) if outcome_meta and outcome_meta[2] else {}
        direction = str(c.get("direction") or "min")
        raw = list(c.get("concepts") or [])
        # Deterministic per-(canonical, run) aggregation: canonicalize each raw slug through the shared
        # alias-source -> split -> alias-target pipeline,
        # then collapse the run's raw concepts that map to the SAME canonical into ONE run-row, so a run
        # never appears twice for one concept (CODEX). The row's metric is the outcome of the sorted-first
        # raw concept that HAS an outcome (deterministic tie-break), else None.
        by_canon: dict[str, list] = {}
        for i, concept in enumerate(raw):
            key = canonicalize_concept(concept, sibling_concepts=raw[:i] + raw[i + 1:],
                                       aliases=aliases, splits=splits)
            if not key:
                continue                          # purged concept -> dropped from cross-run views
            by_canon.setdefault(key, []).append(concept)
        for key, raws in by_canon.items():
            observed = [oc[r] for r in sorted(raws) if r in oc and oc[r] is not None]
            # CODEX AGENT: governance asserts collapsed raw labels are one canonical technique. Its run
            # outcome is therefore the best retained observation in THIS run's direction, not the value of
            # whichever alias sorts first (which can present a losing sibling beside a winning canonical).
            metric = ((min(observed) if direction == "min" else max(observed)) if observed else None)
            # When several raw slugs collapse to ONE canonical (operator alias/split), COMBINE their signs
            # by NET rather than taking the sorted-first — else a merge silently drops the loser when two
            # raws landed on opposite sides of the run's median. sign(sum): majority side, tie -> neutral.
            run_signs = [signs.get(r) for r in sorted(raws) if signs.get(r) is not None]
            total = sum(run_signs)
            sign = None if not run_signs else (1 if total > 0 else -1 if total < 0 else 0)
            e = per_concept.setdefault(key, {"concept": key, "_runs": {}})
            e["_runs"][rid] = {"run_id": rid, "metric": metric, "direction": direction, "sign": sign}
    concepts = []
    for e in per_concept.values():
        all_runs = [e["_runs"][run_id] for run_id in sorted(e["_runs"])]
        # Phase 1 profit rollup: signs are direction-normalized, so counting them ACROSS runs is legitimate
        # even though raw metrics above are deliberately NOT aggregated (§21.20.1). Runs with no signal omit.
        row = {"concept": e["concept"], "n_runs": len(all_runs),
               "n_helped": sum(1 for r in all_runs if r.get("sign") == 1),
               "n_neutral": sum(1 for r in all_runs if r.get("sign") == 0),
               "n_hurt": sum(1 for r in all_runs if r.get("sign") == -1),
               "runs": all_runs[:_MAX_OVERVIEW_RUNS_PER_CONCEPT]}
        if len(all_runs) > len(row["runs"]):
            row["runs_omitted"] = len(all_runs) - len(row["runs"])
        concepts.append(row)
    concepts.sort(key=lambda e: (-e["n_runs"], e["concept"]))   # most-explored first, then name
    cards = []
    for c in valid_capsules:
        canonical = canonicalize_concepts(c.get("concepts") or [], aliases=aliases, splits=splits)
        concept_meta = _capsule_completeness(c, "concepts", len(c.get("concepts") or []))
        outcome_meta = _capsule_completeness(
            c, "concept_outcomes", len(c.get("concept_outcomes") or {}))
        assert concept_meta is not None and outcome_meta is not None  # validated by _dedup_valid_capsules
        # The overview must apply normalization even with empty governance maps; otherwise
        # `Hard-Neg` and `hard-neg` are one UID in the registry but two portfolio concepts/cards.
        card = {"run_id": str(c.get("run_id") or ""), "n_concepts": len(canonical),
                 "best_metric": c.get("best_metric"), "direction": str(c.get("direction") or "min"),
                 "concepts": canonical[:_MAX_OVERVIEW_CARD_CONCEPTS],
                 "source_concepts_total": concept_meta[0],
                 "source_concepts_omitted": concept_meta[1],
                 "source_concepts_complete": concept_meta[2],
                 "source_outcomes_total": outcome_meta[0],
                 "source_outcomes_omitted": outcome_meta[1],
                 "source_outcomes_complete": outcome_meta[2]}
        if len(canonical) > len(card["concepts"]):
            card["concepts_omitted"] = len(canonical) - len(card["concepts"])
        cards.append(card)
    cards.sort(key=lambda k: k["run_id"])
    # CODEX AGENT: every outward collection has an independent hard cap. Totals and explicit omission
    # counters describe the full validated snapshot, so a bounded response never masquerades as complete.
    result = {"n_runs": len(valid_capsules), "n_concepts": len(concepts),
               "concepts": concepts[:_MAX_OVERVIEW_CONCEPTS],
               "runs": cards[:_MAX_OVERVIEW_RUN_CARDS],
               **_capsule_source_summary(valid_capsules)}
    if len(concepts) > len(result["concepts"]):
        result["concepts_omitted"] = len(concepts) - len(result["concepts"])
    if len(cards) > len(result["runs"]):
        result["run_cards_omitted"] = len(cards) - len(result["runs"])
    return result


_MAX_GRAPH_CONCEPTS = 512
_MAX_GRAPH_EDGES = 2_048
_MAX_GRAPH_PER_CONCEPT_CONCEPTS = 256   # cap a single run's concept set before the O(k^2) pairing
_MAX_DIGEST_AXES = 512
_MAX_DIGEST_CONCEPTS_PER_AXIS = 64


def portfolio_concept_graph(capsules: list[dict], *, aliases: Optional[dict] = None,
                            splits: Optional[dict] = None, min_cooccurrence: int = 2) -> dict:
    """PART V Phase 4/5: the GLOBAL cross-run concept MAP — a portfolio-level graph aggregated over concept
    capsules. Pure/deterministic, no LLM/IO, drillable. This is the 'мега общая карта концептов': a shared
    taxonomy view across every returned run. ADVISORY — a read-model, never a selection input.

    Nodes: each canonical concept seen across runs, with `n_runs` (how many runs explored it). Edges:
      - `is_a` : the concept's immediate PATH parent (`a/b/c` -> `a/b`), so the map has a spine even with
                 one run — asserted structure, `n_runs` = runs where the child appears.
      - `co_occurs` : an UNORDERED concept PAIR that appeared TOGETHER in the same run's capsule, weighted by
                 the number of DISTINCT runs the pair co-occurred in (cross-run evidence — the same reason
                 the profit sign aggregates: a per-run boolean counted across runs). Only pairs meeting
                 `min_cooccurrence` (default 2 runs) are kept, so a single-run coincidence is not an edge.
    Canonicalized through aliases/splits like the overview (purged concepts drop; a split re-tags per that
    run's siblings). Everything is bounded; omission counters describe the full validated snapshot."""
    from looplab.engine.concept_registry import canonicalize_concepts

    valid_capsules = _dedup_valid_capsules(capsules)

    concept_runs: dict[str, int] = {}
    for c in valid_capsules:
        canon = sorted(set(canonicalize_concepts(
            (c.get("concepts") or [])[:_MAX_GRAPH_PER_CONCEPT_CONCEPTS], aliases=aliases, splits=splits)))
        for cid in canon:
            concept_runs[cid] = concept_runs.get(cid, 0) + 1

    # is_a spine from the concept PATHS (materialize each ancestor prefix as a node too).
    prefixes: set[str] = set()
    for cid in list(concept_runs):
        parts = cid.split("/")
        for depth in range(1, len(parts)):
            prefixes.add("/".join(parts[:depth]))
    for pfx in prefixes:
        concept_runs.setdefault(pfx, 0)

    concepts = sorted(concept_runs, key=lambda cid: (-concept_runs[cid], cid))
    kept = set(concepts[:_MAX_GRAPH_CONCEPTS])
    edges: list[dict] = []
    for cid in sorted(kept):
        parent = cid.rsplit("/", 1)[0] if "/" in cid else ""
        if parent and parent in kept:
            edges.append({"src": cid, "rel": "is_a", "dst": parent, "n_runs": concept_runs[cid]})
    is_a_candidates = len(edges)
    # CODEX AGENT: select the bounded node set BEFORE O(k^2) pairing. The old one-pass implementation
    # retained pair sets for every source concept even though at most 512 nodes could reach the response.
    pair_runs: dict[tuple[str, str], int] = {}
    for c in valid_capsules:
        canon = sorted(set(canonicalize_concepts(
            (c.get("concepts") or [])[:_MAX_GRAPH_PER_CONCEPT_CONCEPTS],
            aliases=aliases, splits=splits)) & kept)
        for i, a in enumerate(canon):                       # unordered sorted pair; one count per unique run
            for b in canon[i + 1:]:
                pair_runs[(a, b)] = pair_runs.get((a, b), 0) + 1
    try:
        threshold = max(1, int(min_cooccurrence))       # a per-pair run-count floor; <=0 means "keep all"
    except (TypeError, ValueError):
        threshold = 2                                    # a contract-violating caller falls back to the default
    cooc = sorted(((a, b, n_runs) for (a, b), n_runs in pair_runs.items()
                   if n_runs >= threshold),
                  key=lambda t: (-t[2], t[0], t[1]))
    for a, b, n_runs in cooc:
        if len(edges) >= _MAX_GRAPH_EDGES:
            break
        edges.append({"src": a, "rel": "co_occurs", "dst": b, "n_runs": n_runs})
    edge_candidates = is_a_candidates + len(cooc)
    return {
        "n_runs": len(valid_capsules),
        "n_concepts": len(concepts),
        "concepts": [{"concept": cid, "n_runs": concept_runs[cid]} for cid in concepts[:_MAX_GRAPH_CONCEPTS]],
        "edges": edges[:_MAX_GRAPH_EDGES],
        "concepts_omitted": max(0, len(concepts) - _MAX_GRAPH_CONCEPTS),
        "edges_omitted": max(0, edge_candidates - len(edges)),
        "pair_candidates": len(pair_runs),
        **_capsule_source_summary(valid_capsules),
    }


def portfolio_digest(capsules: list[dict], *, aliases: Optional[dict] = None,
                     splits: Optional[dict] = None) -> dict:
    """PART IV cross-run Step 7 (lean, GATED): a flat, display-only rollup above the concept overview.

    Concepts are grouped by the conventional prefix before ``/`` (for example
    ``data/hard-negative-mining`` -> ``data``). This is deterministic and does not claim to be a persisted
    semantic hierarchy. Per the §21.20.11 hierarchy gate it ships as inspector data only; it is not wired
    into prompts until a versioned taxonomy proves its value on the benchmark corpus.
    """
    from looplab.engine.concept_registry import canonicalize_concepts

    valid_capsules = _dedup_valid_capsules(capsules)
    clusters: dict[str, dict] = {}
    for capsule in valid_capsules:
        run_id = capsule["run_id"]
        for concept in canonicalize_concepts(
                capsule.get("concepts") or [], aliases=aliases, splits=splits):
            # This is an unenforced display convention, not a hierarchy: every unprefixed concept lands in
            # one bucket and changing a display slug can move it. Do not infer semantic ancestry from ``/``.
            axis = concept.split("/", 1)[0] if "/" in concept else "(ungrouped)"
            cl = clusters.setdefault(axis, {"axis": axis, "_concepts": set(), "_runs": set()})
            cl["_concepts"].add(concept)
            cl["_runs"].add(run_id)

    # CODEX AGENT: compute exact axis/concept/run totals from the full validated, de-duplicated retained
    # snapshot BEFORE bounding display collections. Building on `portfolio_concept_overview` silently capped
    # each axis at 64 run ids and the whole digest at 512 concepts while presenting both counts as exact.
    axes = []
    for cluster in clusters.values():
        concepts = sorted(cluster["_concepts"])
        retained = concepts[:_MAX_DIGEST_CONCEPTS_PER_AXIS]
        axes.append({
            "axis": cluster["axis"],
            "n_concepts": len(concepts),
            "n_runs": len(cluster["_runs"]),
            "concepts": retained,
            "concepts_omitted": len(concepts) - len(retained),
        })
    axes.sort(key=lambda c: (-c["n_concepts"], -c["n_runs"], c["axis"]))
    retained_axes = axes[:_MAX_DIGEST_AXES]
    n_concepts = sum(len(cluster["_concepts"]) for cluster in clusters.values())
    retained_concepts = sum(len(axis["concepts"]) for axis in retained_axes)
    return {
        "n_axes": len(axes),
        "n_concepts": n_concepts,
        "axes": retained_axes,
        "axes_omitted": len(axes) - len(retained_axes),
        "concepts_omitted": n_concepts - retained_concepts,
        **_capsule_source_summary(valid_capsules),
    }


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
