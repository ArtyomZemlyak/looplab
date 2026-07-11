"""Cross-run prior loading (E4 + M2/M3 read side) for the lessons cluster — extracted from
engine/lessons.py as a MIXIN (the Engine's own convention, see engine/novelty.py):
`class LessonMemory(LessonPriorsMixin, …)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the LessonMemory — the bodies are verbatim moves, reading the
engine through `self._e` and sibling cluster methods through the Engine's thin delegators,
exactly as they did inside the class.

The READ side of cross-run memory: ONE store scan (`_scan_prior_context`) feeds both per-role
prior renders (`_render_role_prior`), with a per-build memoized embedder (`_memoized_embed`) so
a shared/untagged lesson embeds once, not once per role. The role constants live here with the
renderer that filters on them; lessons.py re-exports them for back-compat.

Layering: like lessons.py, no runtime import of the orchestrator (or lessons.py — the mixin is
consumed there) and never serve — only engine.memory, events, core and stdlib (the retrieval/
ranking deps stay lazy, method-local imports)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from looplab.events.eventstore import read_jsonl_lenient

# Which ROLE a cross-run lesson is for, so the two contexts stay separate (the Researcher gets only
# R&D / "what technique to try" lessons, the Developer only its own "what code change fixed a crash"
# lessons). Stamped on the record at distillation; `load_reflection_priors(role=...)` filters on it.
# An UNTAGGED (legacy) lesson is SHARED — both roles see it — so old stores keep working unchanged.
LESSON_ROLE_RESEARCHER = "researcher"
LESSON_ROLE_DEVELOPER = "developer"


def _memoized_embed(embed):
    """Wrap an embedder in a per-build content memo. The two role priors (built together at run start
    and each refresh) share every UNTAGGED lesson, so without this each shared lesson is re-embedded
    once per role — the dominant cost when a real semantic embedder is configured. Transparent: the
    same text always maps to the same vector, so per-role retrieval is byte-identical to the
    un-memoized build; it only elides the duplicate embed call."""
    cache: dict[str, object] = {}

    def _memo(text):
        key = text if isinstance(text, str) else str(text)
        if key not in cache:
            cache[key] = embed(text)
        return cache[key]

    return _memo


class LessonPriorsMixin:
    """The lessons cluster's cross-run prior loader/renderer. See the module docstring for
    the mixin convention (`self` is the LessonMemory)."""

    def load_reflection_priors(self, exclude_run_id: Optional[str] = None,
                               role: Optional[str] = None) -> str:
        """E4 + M2/M3: build the cross-run prior injected into a role's prompt. Two parts:
        (1) exact-task "what won" notes (meta_notes.jsonl — unchanged E4 warm-start), and
        (2) LESSONS retrieved by task-FINGERPRINT similarity (M2), so a *similar but new* task also
        benefits — including NEGATIVE lessons (what was tested/abandoned/failed, M3) so the search
        doesn't re-tread a known dead end. Empty unless enabled + present. `exclude_run_id` drops
        lessons THIS run wrote (M6 mid-run distillation / resume): a run must not read its own
        output back as another run's experience — those results are already in its digest.

        `role` (§role-split): return only the lessons FOR that role — the Researcher gets R&D
        "what technique to try" lessons, the Developer only its own "what code change fixed a crash"
        lessons, so the two contexts stay separate. An UNTAGGED (legacy) lesson is shared. The
        research-flavoured meta-notes (part 1) are skipped for the Developer. role=None -> everything."""
        if not (self._e._reflection_priors and self._e.memory_dir):
            return ""
        return self._render_role_prior(self._scan_prior_context(exclude_run_id), role)

    def load_reflection_priors_both(self, exclude_run_id: Optional[str] = None) -> tuple[str, str]:
        """Build BOTH role priors off ONE scan of the store — the run-start load and every refresh
        need the Researcher AND the Developer prior, and calling `load_reflection_priors` twice would
        re-read + re-fingerprint + re-embed the whole lessons store a second time. Returns
        `(researcher_text, developer_text)`; each is byte-identical to the standalone call."""
        if not (self._e._reflection_priors and self._e.memory_dir):
            return "", ""
        ctx = self._scan_prior_context(exclude_run_id)
        return (self._render_role_prior(ctx, LESSON_ROLE_RESEARCHER),
                self._render_role_prior(ctx, LESSON_ROLE_DEVELOPER))

    def _scan_prior_context(self, exclude_run_id: Optional[str]):
        """Read the cross-run stores ONCE for a prior build and return everything the per-role render
        needs: the exact-task meta-notes, the parsed lessons (role-agnostic — filtered per role in
        `_render_role_prior`), the current task fingerprint, and a per-build memoized embedder shared
        across the role renders. Nothing here is role-aware, so both role priors reuse this one scan."""
        base = Path(self._e.memory_dir)
        # (1) exact-task meta notes (E4) — research-flavoured "what won" config (rendered for the
        # Researcher only; the Developer render drops them below).
        notes: list[str] = []
        npath = base / "meta_notes.jsonl"
        for o in read_jsonl_lenient(npath):
            if o.get("task_id") == self._e.task.id and o.get("note"):
                notes.append(str(o["note"]))
        # (2) fingerprint-matched lessons (M2/M3), incl. negatives — parsed once; the role filter and
        # similarity scoring happen per role in `_render_role_prior`.
        parsed: list[tuple[int, dict]] = []
        lpath = base / "lessons.jsonl"
        # keep_bad=True: idx must stay the RAW on-disk line number (stable lesson identity).
        for idx, o in enumerate(read_jsonl_lenient(lpath, keep_bad=True)):
            if o is None or not o.get("statement"):
                continue
            if exclude_run_id and o.get("run_id") == exclude_run_id:
                continue                     # M6: never echo this run's own lessons back
            parsed.append((idx, o))
        # Compare WITHOUT param: tokens: the writer stamps the winner's param names, but at read
        # time no winner exists yet, so those tokens only dilute the Jaccard overlap.
        fp = [t for t in self._e._task_fingerprint(self._e._empty_state_for_fp())
              if not t.startswith("param:")]
        return notes, parsed, fp, _memoized_embed(self._e._embedder)

    def _render_role_prior(self, ctx, role: Optional[str]) -> str:
        """Render ONE role's prior text from a shared `_scan_prior_context` scan: filter the parsed
        lessons to that role (untagged = shared), score by fingerprint similarity, splice in Memora
        harmonic recall, apply D2 read-time hygiene + ranking, and pick the top 5 with a role label."""
        notes, parsed, fp, embed = ctx
        out = ""
        # (1) meta-notes — research-flavoured, so the Developer never sees them.
        if notes and role != LESSON_ROLE_DEVELOPER:
            out += "\nPrior-run insights for this task (meta-learned): " + " | ".join(notes[-3:])
        if not parsed:
            return out
        # (2) fingerprint-matched lessons (M2/M3), incl. negatives
        from looplab.engine.memory import fingerprint_similarity
        all_lessons: list[tuple[int, dict]] = []
        scored: list[tuple[float, int, dict]] = []
        for idx, o in parsed:
            lrole = o.get("role")
            if role is not None and lrole is not None and lrole != role:
                continue                     # §role-split: a lesson EXPLICITLY for the OTHER role
                #                              stays out of this role's context (untagged = shared)
            all_lessons.append((idx, o))
            stored_fp = o.get("fingerprint")
            stored_fp = ([t for t in stored_fp if not str(t).startswith("param:")]
                         if isinstance(stored_fp, list) else [])
            exact = o.get("task_id") == self._e.task.id
            sim = 1.0 if exact else fingerprint_similarity(fp, stored_fp)
            if sim >= 0.34:                    # a related task (Jaccard) or the same one
                scored.append((sim, idx, o))
        # Full synergy with Memora: harmonic recall reaches lessons a differently-worded but
        # anchor-linked task shares — the ones token-overlap (Jaccard ≥ 0.34) misses. Splice
        # them into the SAME candidate pool so the D2 hygiene + ranking below apply uniformly.
        # No-op unless a Memora abstractor is wired (memora on); then it uses the T5 embedder.
        if self._e._lesson_abstractor is not None and all_lessons:
            from looplab.engine.memory import retrieve_lessons_harmonic
            by_idx = {i: o for i, o in all_lessons}
            already = {i for _, i, _ in scored}
            query = " ".join(fp) + " " + (getattr(self._e.task, "goal", "") or "")
            for hsim, hidx in retrieve_lessons_harmonic(
                    all_lessons, query, self._e._lesson_abstractor, embed):
                if hidx not in already and hidx in by_idx:
                    scored.append((hsim, hidx, by_idx[hidx]))
                    already.add(hidx)
        # D2 hygiene at read time: quarantine any lesson whose claim a NEWER run reversed
        # (an old "supported" vs a later "tested/abandoned" of the same statement) — the
        # misevolution guard: memory must not keep pushing a refuted correlation.
        from looplab.engine.memory import filter_contradicted, lesson_rank_key
        scored = filter_contradicted(scored)
        # Rank: similarity, then confidence × corroboration (evidence_count), then recency —
        # so a twice-confirmed lesson from a related task beats a one-off at equal similarity.
        scored.sort(key=lambda t: lesson_rank_key(*t))
        seen: set[str] = set()
        picked: list[str] = []
        for _, _, o in scored:
            key = (o.get("statement", "")[:80], o.get("outcome"))
            if key in seen:
                continue
            seen.add(key)
            d = o.get("delta")
            dtxt = f" Δ{d:+.3g}" if isinstance(d, (int, float)) else ""
            stmt = " ".join(str(o["statement"]).split())[:200]   # cap + collapse newlines:
            picked.append(f"{stmt} [{o.get('outcome', '?')}{dtxt}]")   # store is shared/free-text
            if len(picked) >= 5:
                break
        if picked:
            # The Developer label must NOT over-claim "code fixes": its pool is its own code-fix
            # lessons PLUS any untagged/shared rows (winner records, failure themes) that are not code
            # fixes — so the header names both instead of asserting everything is a fix.
            label = ("Implementation & shared lessons from related runs (code fixes and prior "
                     "findings that did/didn't work)"
                     if role == LESSON_ROLE_DEVELOPER
                     else "Lessons from related runs (what did/didn't work)")
            out += "\n" + label + ": " + "; ".join(picked)
        return out
