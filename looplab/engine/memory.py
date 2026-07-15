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
    # CODEX AGENT: kind/direction/metric are merely Jaccard tokens, not compatibility gates; reproduced
    # incompatible min/RMSE and max/recall tasks clear the live 0.3 threshold. Separate immutable hard
    # facets from fuzzy retrieval text and version the tokenizer/fingerprint/schema.
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
    from looplab.tools.vectorstore import Hit, InMemoryVectorStore, Item

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
        # dicts_only=False: the historical reload kept any parsed JSON value, not just objects.
        self.cases = read_jsonl_lenient(self.path, loads=json.loads, dicts_only=False)

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
        q = set(query.lower().split())
        scored = [(len(q & set((c.get("goal", "") + " " + c.get("task_id", "")).lower().split())), c)
                  for c in self.cases]
        scored.sort(key=lambda t: -t[0])
        return [c for _, c in scored[:k]]

    def all(self) -> list[dict]:
        return list(self.cases)


def build_concept_capsule(*, run_id: str, fingerprint: list[str], direction: str,
                          concepts, best_metric=None, concept_outcomes: Optional[dict] = None) -> dict:
    """A compact per-run CONCEPT capsule — the cross-run bridge (§21.20 Step 2). It records WHICH
    concepts a run explored (the shipped per-run `node_concepts` tags — no new tagger) and how it went,
    keyed by `task_fingerprint`, so a later SIMILAR run can answer "was this tried across runs, and
    with what result?" and feed `grade_novelty`'s `prior_concepts` (D3 level 3 = surface prior, never
    reject). Deliberately small and JSON-flat: this is memory-store data, not a fold event."""
    # CODEX AGENT: This durable cross-run record has no schema/fingerprint/concept version, task/scope,
    # evidence node refs, visibility/retention policy, or purge key. A deleted/private run keeps affecting
    # later runs, and readers cannot distinguish or migrate incompatible record generations.
    return {
        "run_id": str(run_id or ""),
        "fingerprint": sorted(set(fingerprint or [])),
        "direction": str(direction or "min"),
        "concepts": sorted({str(c) for c in (concepts or []) if c}),
        "best_metric": best_metric,
        "concept_outcomes": dict(concept_outcomes or {}),
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

    def _reload(self) -> None:
        # CODEX AGENT: `dicts_only` is not schema validation. A dict with an integer fingerprint or a
        # string `concepts` field can poison retrieval (or become character concepts); validate and
        # quarantine each bounded row rather than letting one row disable the advisory feature.
        self.capsules = read_jsonl_lenient(self.path, loads=json.loads, dicts_only=True)

    def add(self, capsule: dict) -> bool:
        """Upsert by `run_id` under the same interprocess lock the case/lesson stores use, re-reading
        inside the lock so a concurrent run's capsule survives. Returns True once stored."""
        from looplab.events.eventstore import _interprocess_lock
        rid = str(capsule.get("run_id") or "")
        if not rid:
            return False
        with _interprocess_lock(Path(str(self.path) + ".lock")):
            self._reload()
            self.capsules = [c for c in self.capsules if str(c.get("run_id") or "") != rid]
            self.capsules.append(capsule)
            write_jsonl_atomic(self.path, self.capsules, dumps=json.dumps)
        return True

    def prior_capsules(self, fingerprint: list[str], *, min_sim: float = 0.3,
                       exclude_run_id: str = "") -> list[tuple[float, dict]]:
        """Prior-run capsules whose fingerprint is at least `min_sim` similar (Jaccard, universal-aware
        because the fingerprint itself already is), most-similar first, excluding this run. Bounded by
        the caller; each tuple is (similarity, capsule) so a surfacing cue can rank + cite by run."""
        # CODEX AGENT: This materializes and sorts the whole portfolio for every proposal. Combined with
        # construction-time full-file reload and finalization-time whole-file rewrite, cost grows with
        # every run; query a bounded indexed snapshot keyed by compatible scope/version instead.
        out = []
        for c in self.capsules:
            if exclude_run_id and str(c.get("run_id") or "") == str(exclude_run_id):
                continue
            sim = fingerprint_similarity(fingerprint, c.get("fingerprint") or [])
            if sim >= min_sim:
                out.append((sim, c))
        out.sort(key=lambda t: -t[0])
        return out

    def prior_concepts(self, fingerprint: list[str], *, min_sim: float = 0.3,
                       exclude_run_id: str = "") -> set[str]:
        """The UNION of concepts explored by similar prior runs — exactly the `set[str]` shape
        `grade_novelty(prior_concepts=…)` consumes to fire D3 level 3 ("tried across runs")."""
        acc: set[str] = set()
        for _sim, c in self.prior_capsules(fingerprint, min_sim=min_sim, exclude_run_id=exclude_run_id):
            acc.update(str(x) for x in (c.get("concepts") or []))
        return acc

    def all(self) -> list[dict]:
        return list(self.capsules)


def portfolio_concept_overview(capsules: list[dict]) -> dict:
    """A cross-run portfolio read-model over concept capsules (§21.20 Step 3 — 'what has been tried across
    the portfolio'). Pure/deterministic, no LLM/IO, drillable to `run_id`. For each concept it lists the
    runs that explored it with THEIR OWN outcome (run_id, metric, direction) — deliberately NOT a single
    cross-run 'best', because raw metrics from different tasks/directions are not comparable without a
    shared contract (§21.20.1). Also emits a per-run card (concept count + the run's own best_metric)."""
    per_concept: dict[str, dict] = {}
    for c in capsules:
        rid = str(c.get("run_id") or "")
        oc = c.get("concept_outcomes") or {}
        direction = str(c.get("direction") or "min")
        for concept in (c.get("concepts") or []):
            key = str(concept)
            e = per_concept.setdefault(key, {"concept": key, "runs": []})
            e["runs"].append({"run_id": rid, "metric": oc.get(concept), "direction": direction})
    concepts = []
    for e in per_concept.values():
        e["runs"].sort(key=lambda r: r["run_id"])
        e["n_runs"] = len({r["run_id"] for r in e["runs"]})
        concepts.append(e)
    concepts.sort(key=lambda e: (-e["n_runs"], e["concept"]))   # most-explored first, then name
    cards = [{"run_id": str(c.get("run_id") or ""),
              "n_concepts": len({str(x) for x in (c.get("concepts") or [])}),
              "best_metric": c.get("best_metric"),
              "direction": str(c.get("direction") or "min"),
              "concepts": sorted({str(x) for x in (c.get("concepts") or [])})}
             for c in capsules]
    cards.sort(key=lambda k: k["run_id"])
    return {"n_runs": len(capsules), "n_concepts": len(concepts), "concepts": concepts, "runs": cards}


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
