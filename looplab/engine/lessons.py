"""Cross-run memory / lessons / reflection for the engine (extracted from orchestrator.py):
the E4 meta-note prior, M2/M3 fingerprint-keyed lessons (incl. negatives), M6 comparative
(credit-assigned pair) lessons with their mid-run distill/refresh cadences, M4 auto-distilled
skills, D2 store hygiene (consolidate/compact), and the I19 case library write.

`LessonMemory` wraps the engine instance (`self._e`) rather than owning copies of its state:
the method bodies are verbatim moves from the Engine, reading the engine's knobs/store/task
through `self._e` and calling sibling cluster methods through the Engine's thin delegators
(so a test monkeypatching e.g. `engine._reflect_client` still intercepts every internal call).
Only the purely lessons-owned mutable state (`seen_stamp`, `prior_note_text`) lives here; the
Engine exposes them back under the original attribute names via properties.

Layering: this module must not import the orchestrator (TYPE_CHECKING only) and never imports
serve — it touches only engine.memory, events, core and stdlib/orjson."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

import orjson

from looplab.core.models import NodeStatus, RunState
from looplab.engine.memory import JsonlCaseLibrary
from looplab.events.replay import fold
from looplab.events.types import (EV_LESSONS_DISTILLED, EV_LESSONS_RECONCILED,
                                  EV_LESSONS_REFRESHED, EV_REFLECTION_NOTE)

# Which ROLE a cross-run lesson is for, so the two contexts stay separate (the Researcher gets only
# R&D / "what technique to try" lessons, the Developer only its own "what code change fixed a crash"
# lessons). Stamped on the record at distillation; `load_reflection_priors(role=...)` filters on it.
# An UNTAGGED (legacy) lesson is SHARED — both roles see it — so old stores keep working unchanged.
LESSON_ROLE_RESEARCHER = "researcher"
LESSON_ROLE_DEVELOPER = "developer"

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


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


class LessonMemory:
    """The engine's cross-run memory / lessons / reflection cluster. See the module docstring
    for the `self._e` (engine handle) convention."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine
        self.seen_stamp = None   # (size, mtime_ns) of the store at the last read
        self.prior_note_text = ""   # E4: cross-run RESEARCHER prior (R&D lessons), loaded at run start
        self.dev_prior_note_text = ""   # §role-split: cross-run DEVELOPER prior (code-fix lessons)
        # Reconcile gate: a hash of {node_id -> outcome-signature} at the last reconcile scan. Recomputed
        # each cadence pass (cheap, no I/O); when it CHANGES (a node reached / left / flipped a terminal —
        # in particular a node_reset re-eval that altered a metric or status), we re-read the lesson file
        # and re-derive any of THIS run's lessons whose evidence sig moved. None on start → first pass
        # always scans (verifies the store against the folded state after a restart/resume).
        self._reconcile_sig_hash = None

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
        if npath.exists():
            for line in npath.read_text(encoding="utf-8").splitlines():
                try:
                    o = orjson.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(o, dict):       # valid JSON but not an object (corrupt line)
                    continue
                if o.get("task_id") == self._e.task.id and o.get("note"):
                    notes.append(str(o["note"]))
        # (2) fingerprint-matched lessons (M2/M3), incl. negatives — parsed once; the role filter and
        # similarity scoring happen per role in `_render_role_prior`.
        parsed: list[tuple[int, dict]] = []
        lpath = base / "lessons.jsonl"
        if lpath.exists():
            for idx, line in enumerate(lpath.read_text(encoding="utf-8").splitlines()):
                try:
                    o = orjson.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(o, dict) or not o.get("statement"):
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

    def empty_state_for_fp(self) -> RunState:
        """Minimal RunState carrying just what `_task_fingerprint` reads at run START (before any
        node), so the prior loader can fingerprint the current task the same way the writer will."""
        return RunState(task_id=self._e.task.id, goal=getattr(self._e.task, "goal", ""),
                        direction=getattr(self._e.task, "direction", "min"))

    def task_fingerprint(self, final: RunState, best=None) -> list[str]:
        """M2: content fingerprint of this task so cross-run transfer reaches SIMILAR tasks, not only
        the exact same task_id. Built from kind/direction/metric/goal keywords + the winner's params."""
        from looplab.engine.memory import task_fingerprint
        pnames = list((best.idea.params or {}).keys()) if best is not None and best.idea else []
        return task_fingerprint(getattr(self._e.task, "kind", ""), final.direction,
                                final.goal or getattr(self._e.task, "goal", ""),
                                metric=str(getattr(self._e.task, "metric", "") or ""),
                                param_names=pnames)

    def write_reflection_note(self, final: RunState) -> None:
        """E4 + M2/M3: distill this run's cross-run memory. Writes (1) the one-line "what won" note to
        meta_notes.jsonl (E4, exact-task warm-start — unchanged), and (2) structured LESSONS to
        lessons.jsonl (M3) — including NEGATIVE results (tested/abandoned hypotheses, failure themes),
        each stamped with a task fingerprint (M2) so a later SIMILAR task can retrieve them."""
        if not (self._e._reflection_priors and self._e.memory_dir):
            return
        # Run-end reflection re-spends LLM calls and appends to cross-run memory, so it must not run
        # redundantly. But a REOPENED run (EV_RESUME + EV_BUDGET_EXTEND) that added nodes and found a
        # BETTER winner MUST re-reflect — else cross-run memory keeps the stale first-finalize conclusion
        # forever (the extension's winner/hypotheses never distilled). So gate on NODE COUNT, not mere
        # presence: skip only when a prior reflection already covered at least this many nodes (a plain
        # re-finalize with nothing new). A grown re-reflection re-appends the run's lessons, but that no
        # longer inflates `evidence_count` — `consolidate_lessons` now counts DISTINCT run_ids, so a run
        # re-appending its own lesson still counts once. `reflection_note` (a diagnostic sidecar the fold
        # ignores) carries `at_nodes`; the highest prior value is the coverage watermark.
        _reflected_at = [int(e.data.get("at_nodes", 0) or 0)
                         for e in self._e.store.read_all() if e.type == EV_REFLECTION_NOTE]
        if _reflected_at and len(final.nodes) <= max(_reflected_at):
            return
        best = final.best()
        base = Path(self._e.memory_dir)
        base.mkdir(parents=True, exist_ok=True)
        note = ""            # the causal "why the winner won" summary (set below when there's a winner)
        # The winner note needs a winner — but hypothesis/failure lessons below do NOT: a run in
        # which every node failed is exactly the negative lesson M3 exists to record.
        if best is not None:
            stats = (f"best metric {best.metric:.4g} via op '{best.operator}' params "
                     f"{best.idea.params}; {len(final.nodes)} nodes, "
                     f"{len(final.evaluated_nodes())} evaluated")
            # A meta-note's purpose is the CAUSE — WHY the winner won — not the raw config (that's the
            # case). Distil a causal summary with the LLM; fall back to the stats line if there's no
            # client / on any error (reflection is best-effort, never fails the run).
            note = self._e._causal_meta_note(final, best) or stats
            with open(base / "meta_notes.jsonl", "a", encoding="utf-8") as f:
                f.write(orjson.dumps({"task_id": final.task_id, "note": note}).decode() + "\n")

        # M3 · lessons (incl. failures) with an M2 fingerprint. Memory of what DIDN'T work is as
        # valuable as what did (DS-Agent / MARS / ML-Master): it stops a later run re-treading a dead
        # end. Sources: the winner, each resolved hypothesis (the P1 ledger gives negative results for
        # free), and the dominant failure reason.
        fp = self._e._task_fingerprint(final, best)
        lessons: list[dict] = []
        # A lesson should be a GENERALIZABLE finding (DS-Agent / MARS reflective memory), not the raw
        # winning config (that's the case) — so instead of a templated "op X params Y reached Z" line we
        # LLM-reflect over the whole run for transferable good/bad takeaways. Fingerprint-keyed, so a
        # later SIMILAR task retrieves them; consolidation then merges repeats into "verified on N runs".
        lessons.extend(self._e._reflect_lessons(final, best, fp))
        # M6 comparative lessons at run end: credit-assigned pair distillation over whatever pairs
        # the mid-run cadence did NOT already spend. Run-end spends are recorded as a
        # `lessons_distilled` event too — a run reopened later (budget_extend/add_nodes) must not
        # re-distill these pairs any more than a resumed one may re-distill the mid-run ones.
        if self._e._comparative_lessons_on:
            comp, pairs = self._e._comparative_lessons(final, fp,
                                                       exclude=self._e._spent_pairs(final))
            if pairs:
                self._e.store.append(EV_LESSONS_DISTILLED, {
                    "at_node": len(final.nodes), "trigger": "run_end", "count": len(comp),
                    "pairs": [[pr["a"], pr["b"]] for pr in pairs],
                    "lessons": [{"statement": lz["statement"], "outcome": lz["outcome"],
                                 "evidence": lz.get("evidence")} for lz in comp]})
            lessons.extend(comp)
        # NOTE: lessons are now EXCLUSIVELY LLM-authored (the `_reflect_lessons` consolidation above +
        # the M6 comparative pass). We deliberately NO LONGER append (a) each hypothesis's statement
        # VERBATIM — that dumped the Researcher's raw, often run-on / "Experiment A:"-labelled proposal
        # text and one near-duplicate per trial into the store — nor (b) a templated
        # "N node(s) failed with reason X" line. Both were look-alike-hypothesis noise, not distilled
        # findings; the LLM reflection is fed the full hypothesis+outcome+Δ record and the failure
        # themes (see `reflect_lessons`), so it consolidates the SAME signal into one lesson per theme.
        # §role-split: every surviving producer is role-tagged at its source — reflect_lessons →
        # researcher (R&D findings), comparative → developer for debug pairs / researcher otherwise.
        self._e._append_lessons(lessons)

        # M4 · auto-distilled skills (episodic → procedural memory): a supported hypothesis that
        # actually moved the metric becomes a candidate SKILL.md; a later run on a DIFFERENT task
        # fingerprint that re-confirms it promotes it. Best-effort; never fails the run.
        from looplab.engine.memory import write_auto_skill
        sk_dir = base / "skills"
        skills: list[str] = []
        for h in (final.hypotheses or {}).values():
            if h.status == "supported" and (h.best_delta or 0) > 0:
                ev = [final.nodes[i] for i in h.evidence if i in final.nodes]
                write_auto_skill(sk_dir, h.statement,
                                 self._e._distill_skill_body(final, h, ev), fp, final.task_id)
                skills.append(h.statement)

        # Audit the run-end distillation in the event log (diagnostic sidecar — fold ignores it). These
        # LLM artifacts (the causal note, the generalizable lessons, the auto-promoted skills) shape
        # FUTURE runs' priors/skills yet otherwise leave no trace in THIS run's events.jsonl — only in
        # cross-run files. One summary event makes "what this run concluded & wrote to memory" visible.
        self._e.store.append(EV_REFLECTION_NOTE, {
            "task_id": final.task_id, "fingerprint": fp, "note": note,
            "at_nodes": len(final.nodes),      # coverage watermark: re-reflect only if a reopen grows past it
            "n_lessons": len(lessons), "n_skills": len(skills),
            "lessons": [{"statement": lz.get("statement", ""), "outcome": lz.get("outcome", "")}
                        if isinstance(lz, dict) else {"statement": str(lz), "outcome": ""}
                        for lz in lessons[:12]],
            "skills": skills[:8]})

    def _reflect_tools(self, state: RunState):
        """Read-only run-introspection tools so reflection / distillation READS the real experiments
        (read_experiment / read_code / read_logs / list_experiments) to ground its output, instead of
        distilling blind from the aggregate summary in the prompt. None on any failure => plain call."""
        try:
            from looplab.tools.run_tools import RunTools
            from looplab.agents.agent import CompositeTools
            rt = RunTools()
            rt.bind_state(state, None)
            return CompositeTools([rt])
        except Exception:  # noqa: BLE001
            return None

    def _reflect_loop_opts(self) -> dict:
        """Bounded tool-loop opts for the auxiliary agentic passes (reflection/distillation) — the same
        config-driven B1/C1/C2 options as the main agents, plus a tight turn cap (these READ a bit then
        emit, they don't investigate for 300 turns)."""
        try:
            from looplab.agents.agent import loop_opts_from_settings
            opts = loop_opts_from_settings(getattr(self._e, "settings", None))
        except Exception:  # noqa: BLE001
            opts = {}
        opts["max_turns"] = 15
        return opts

    def reflect_lessons(self, final: RunState, best, fp: list) -> list:
        """LLM reflection over the whole run → 1-3 GENERALIZABLE lessons (transferable good/bad
        takeaways), the DS-Agent/MARS reflective-memory idea — not per-run specifics. [] on no-client
        / error, so the hypothesis-derived + failure lessons still stand."""
        def _winner_lesson():
            # Offline/toy fallback (no LLM to generalize): keep a minimal winner record so the
            # fingerprint-keyed store still captures this run for retrieval + consolidation.
            if best is None:
                return []
            return [{"task_id": final.task_id, "fingerprint": fp, "kind": getattr(self._e.task, "kind", ""),
                     "statement": (f"op '{best.operator}' with params {best.idea.params} "
                                   f"reached {best.metric:.4g}"),
                     "outcome": "supported", "delta": None, "confidence": 0.7,
                     "run_id": final.run_id, "evidence": [best.id], "role": LESSON_ROLE_RESEARCHER,
                     "evidence_sig": self._evidence_sig_map(final, [best.id])}]
        client = self._e._reflect_client()
        # `_winner_lesson` is the OFFLINE/toy safety net ONLY (no LLM at all). In a real run — an LLM
        # IS wired — lessons are ALWAYS LLM-authored: on error or empty output we write NOTHING rather
        # than a templated "op X reached Y" line (which polluted the real store as look-alike noise).
        if client is None or best is None:
            return _winner_lesson()
        rev = (final.direction != "min")
        ok = sorted((n for n in final.evaluated_nodes() if n.metric is not None),
                    key=lambda n: n.metric, reverse=rev)[:5]
        bad = [n for n in final.nodes.values() if n.status is NodeStatus.failed][:3]
        rows = [f"#{n.id} {n.operator} metric={n.metric:.4g} params={n.idea.params}" for n in ok]
        fails = [f"#{n.id} {n.operator} failed: {n.error_reason}" for n in bad]
        # PROVENANCE for reconciliation: the concrete nodes this whole-run reflection is grounded in
        # (the winning rows + the failure rows fed into the prompt). Stamped on every lesson below so a
        # later re-eval that FLIPS any of these outcomes (a false-failure re-scored to evaluated, a
        # champion demoted) is detected by `reconcile_lessons` and the batch is re-derived from the
        # corrected state. Coarse on purpose: a whole-run generalization can't be attributed per-node, so
        # ANY fed-node change invalidates the batch (one LLM re-reflection — cheap, correct).
        ev_ids = [n.id for n in ok] + [n.id for n in bad]
        ev_sig = self._evidence_sig_map(final, ev_ids)
        # The full experimental record — every RESOLVED hypothesis with its outcome + Δ — so the LLM can
        # CONSOLIDATE many trials of the SAME theme (e.g. every temperature experiment) into ONE lesson,
        # instead of the old one-verbatim-hypothesis-per-lesson dump that filled the store with near-dupes.
        hyps = [f"[{h.status}{f' Δ{h.best_delta:+.4g}' if isinstance(h.best_delta, (int, float)) else ''}] "
                f"{' '.join((h.statement or '').split())[:160]}"
                for h in (final.hypotheses or {}).values()
                if h.status in ("supported", "tested", "abandoned") and h.statement]
        prompt = ("Distil reusable LESSONS from a finished ML experiment run, to guide FUTURE runs on "
                  f"SIMILAR tasks.\nTask: {final.goal}\nWhat worked (best first):\n" + "\n".join(rows) +
                  ("\nWhat failed:\n" + "\n".join(fails) if fails else "") +
                  ("\nHypotheses tested (outcome, Δ):\n" + "\n".join(hyps) if hyps else "") +
                  "\n\nWrite GENERALIZABLE lessons — transferable findings, NOT these exact numbers "
                  "(e.g. 'a larger batch size aided convergence'). CONSOLIDATE every finding about the "
                  "SAME parameter/technique/theme into ONE lesson (e.g. merge all temperature trials "
                  "into a single lesson stating the sweet spot AND what hurt); keep genuinely UNRELATED "
                  "findings as SEPARATE lessons — one lesson per distinct theme. Each lesson is ONE "
                  "self-contained sentence (no run-on chains, no 'Experiment A:' labels). Tag each "
                  "[GOOD] (reuse this) or [BAD] (avoid this). One per line, no preamble.")
        try:
            from looplab.agents.agent import agentic_text
            out = agentic_text(client, self._reflect_tools(final), [{"role": "user", "content": prompt}],
                               loop_opts=self._reflect_loop_opts(),
                               answer_desc="generalizable lessons, one theme per line, each tagged [GOOD]/[BAD]") or ""
        except Exception:   # noqa: BLE001 - best-effort; a real run writes NO templated fallback
            return []
        from looplab.engine.memory import parse_credit_lessons
        # No fixed cap: one lesson per distinct theme (consolidation keeps this small); bound at 8 as a
        # runaway guard, not a target. §role-split: these are generalizable technique/strategy takeaways
        # → the RESEARCHER's context (what to try next).
        res = [{"task_id": final.task_id, "fingerprint": fp,
                "kind": getattr(self._e.task, "kind", ""), "statement": stmt,
                "outcome": outcome, "delta": None, "confidence": 0.6,
                "run_id": final.run_id, "evidence": list(ev_ids), "evidence_sig": ev_sig,
                "role": LESSON_ROLE_RESEARCHER}
               for _, stmt, outcome in parse_credit_lessons(out, 0)[:8]]
        return res      # LLM gave nothing usable → [] (a real run never writes a templated lesson)

    def append_lessons(self, lessons: list, *, hygiene: bool = True) -> None:
        """Append lessons to the SHARED cross-run store. Used by run-end reflection AND the M6
        mid-run distillation, so a lesson distilled mid-flight is visible to a concurrent run's
        refresh immediately. Concurrency: the whole append (and the optional hygiene rewrite) runs
        under the same best-effort interprocess lock the event store uses — the D2 consolidate/
        compact pass is a full-file read-modify-write, and without the lock a concurrent run's
        O_APPEND between our read and our rewrite would be silently clobbered (losing exactly the
        cross-run lesson the live share exists to propagate). `hygiene=False` (the mid-run path)
        skips consolidate/compact entirely: the read path already dedups and quarantines, so
        hygiene can wait for run end instead of rewriting a shared file every few nodes."""
        if not (lessons and self._e.memory_dir):
            return
        from looplab.events.eventstore import _interprocess_lock
        base = Path(self._e.memory_dir)
        base.mkdir(parents=True, exist_ok=True)
        path = base / "lessons.jsonl"
        payload = "".join(orjson.dumps(lz).decode() + "\n" for lz in lessons)
        with _interprocess_lock(Path(str(path) + ".lock")):
            with open(path, "a", encoding="utf-8") as f:
                f.write(payload)
            if hygiene:
                # D2 hygiene: consolidate the store after appending — merge duplicate claims into
                # an evidence_count, retire contradicted verdicts (newest wins), THEN bound size. The
                # Researcher client + embedder enable the hybrid+agent paraphrase-merge pass (run end
                # only — hygiene=False mid-run skips it, so the shared file isn't rewritten every node).
                # prompts/parser travel WITH the client so a merge_system.md override and the run's
                # configured structured-output parser reach the merge's adjudication call (I18/ADR-8).
                prompts, parser = self._merge_prompt_opts()
                self._e._consolidate_lessons_file(path, self._e._reflect_client(), self._e._embedder,
                                                  parser=parser, prompts=prompts)
                self._e._compact_lessons(path)

    def comparative_lessons(self, state: RunState, fp: list, exclude=()) -> tuple[list, list]:
        """M6 (MARS comparative reflective memory): credit-assigned lessons from solution PAIRS —
        which SPECIFIC difference made the child beat (or regress from) its parent, and what fixed
        a failure. One LLM call for ALL pairs (budget: same order as `_reflect_lessons`); offline,
        the deterministic param-diff credit stands in. Returns (lessons, pairs_used); ([], []) when
        there is nothing informative to compare. Best-effort — never raises."""
        from looplab.engine.memory import (code_diff, param_credit_statement,
                                           parse_credit_lessons, select_comparison_pairs)
        pairs = select_comparison_pairs(state, k=3, exclude=exclude)
        if not pairs:
            return [], []

        def _lesson(pr: Optional[dict], statement: str, outcome: str, conf: float) -> dict:
            # `evidence` [child, parent] IS the credited pair (no separate `pair` field to drift).
            # pr=None = the LLM line carried no usable P<n> marker: record the lesson UNATTRIBUTED
            # rather than stamping it with an arbitrary pair's nodes/delta (wrong provenance).
            # ROLE-split: a DEBUG pair's lesson is "what code change fixed this crash" → the DEVELOPER's
            # context; an improve/regress pair credits a param/technique change → the RESEARCHER's. An
            # unattributed line (pr=None) stays untagged/shared. (§role-split lessons)
            ev = [pr["a"], pr["b"]] if pr else []
            d = {"task_id": state.task_id, "fingerprint": fp,
                 "kind": getattr(self._e.task, "kind", ""), "statement": statement,
                 "outcome": outcome, "delta": pr.get("delta") if pr else None,
                 "confidence": conf, "run_id": state.run_id,
                 "evidence": ev, "evidence_sig": self._evidence_sig_map(state, ev),
                 "source": "comparative"}
            if pr is not None:
                d["role"] = (LESSON_ROLE_DEVELOPER if pr.get("kind") == "debug"
                             else LESSON_ROLE_RESEARCHER)
            return d

        def _fallback() -> list:
            out = []
            for pr in pairs:
                a, b = state.nodes[pr["a"]], state.nodes[pr["b"]]
                if pr["kind"] == "debug":
                    why = " ".join((a.idea.rationale or "").split())[:90]
                    out.append(_lesson(pr, f"a node failing with '{b.error_reason or 'error'}' "
                                           f"was fixed" + (f": {why}" if why else ""),
                                       "supported", 0.5))
                    continue
                stmt = param_credit_statement(a, b, pr["delta"] or 0.0)
                if stmt:   # no clean single-factor credit -> no lesson (beats a mushy lesson)
                    out.append(_lesson(pr, stmt,
                                       "supported" if (pr["delta"] or 0) > 0 else "failed", 0.55))
            return out

        client = self._e._reflect_client()
        if client is None:
            return _fallback(), pairs
        blocks = []
        for i, pr in enumerate(pairs, 1):
            a, b = state.nodes[pr["a"]], state.nodes[pr["b"]]
            if pr["kind"] == "debug":
                head = (f"P{i} (debug): #{b.id} FAILED with '{b.error_reason or 'error'}'; its "
                        f"repair #{a.id} reached metric={a.metric:.4g}.")
            else:
                verb = "IMPROVED on" if (pr["delta"] or 0) > 0 else "REGRESSED from"
                head = (f"P{i}: #{a.id} (metric={a.metric:.4g}, params={a.idea.params}) {verb} "
                        f"#{b.id} (metric={b.metric:.4g}, params={b.idea.params}) "
                        f"by {abs(pr['delta'] or 0):.4g}.")
            diff = code_diff(b.code or "", a.code or "")
            blocks.append(head + (f"\nCode diff (#{b.id} -> #{a.id}):\n{diff[:2000]}"
                                  if diff else ""))
        prompt = ("Assign CREDIT for each experiment-pair outcome below: identify WHICH specific "
                  "difference (code or params) caused the change, then state it as ONE "
                  "generalizable lesson for future runs on SIMILAR tasks.\n"
                  f"Task: {state.goal}\n\n" + "\n\n".join(blocks) +
                  "\n\nFor EACH pair output exactly one line: `P<n> [GOOD] <lesson>` if the "
                  "credited change should be reused, or `P<n> [BAD] <lesson>` if it should be "
                  "avoided. Credit the SPECIFIC difference, stated generally (no exact numbers). "
                  "No preamble.")
        try:
            from looplab.agents.agent import agentic_text
            out = agentic_text(client, self._reflect_tools(state), [{"role": "user", "content": prompt}],
                               loop_opts=self._reflect_loop_opts(),
                               answer_desc="one credited lesson per pair: `P<n> [GOOD]/[BAD] <lesson>`") or ""
        except Exception:  # noqa: BLE001 — reflection is best-effort; a real run writes NO templated lesson
            return [], pairs
        lessons = []
        for idx, stmt, outcome in parse_credit_lessons(out, len(pairs)):
            pr = pairs[idx] if idx >= 0 else None
            lessons.append(_lesson(pr, stmt, outcome, 0.65 if idx >= 0 else 0.5))
        # `_fallback` (deterministic param-diff) is the OFFLINE/toy path only (client is None above); a
        # real run whose LLM returned nothing usable writes no comparative lesson rather than a template.
        return lessons, pairs

    @staticmethod
    def spent_pairs(state: RunState) -> list:
        """Every (child, parent) pair a prior distillation already spent — the ledger both the
        mid-run cadence and run-end reflection exclude against, folded from `lessons_distilled`
        events (incl. the run-end one, so a reopened run never re-distills)."""
        return [tuple(p) for d in (state.lessons_distilled or [])
                for p in (d.get("pairs") or [])]

    # ------------------------------------------------------------ reconciliation (node re-eval → memory)
    @staticmethod
    def _coerce_id(k):
        """evidence_sig keys round-trip through JSON as strings; node ids are ints. Coerce back so
        `state.nodes.get()` hits (leave non-numeric keys alone — forward-compat)."""
        try:
            return int(k)
        except (TypeError, ValueError):
            return k

    @staticmethod
    def _node_sig(node) -> Optional[str]:
        """A compact OUTCOME signature for a node — what its terminal looks like right now. A lesson
        grounded in a node is 'in sync' iff its stored sig still equals this. Captures status + the
        metric (ROUNDED, so float jitter never trips a re-derive) or the failure reason. None when the
        node is pending/absent: there is no terminal to ground a lesson on, so a lesson citing it is
        treated as 'not yet resolved' (wait), never as drifted."""
        if node is None:
            return None
        status = getattr(node.status, "value", None) or str(node.status)
        if status == "pending":
            return None
        m = getattr(node, "metric", None)
        if m is not None:
            return f"{status}:{round(float(m), 4)}"
        reason = getattr(node, "error_reason", "") or ""
        return f"{status}:{reason}" if reason else status

    def _evidence_sig_map(self, state: RunState, node_ids) -> dict:
        """{str(node_id) -> current outcome sig} for a lesson's grounding nodes, stamped at write time.
        `reconcile_lessons` recomputes it from the live state and re-derives on any diff. Skips ids with
        no terminal (None) — a lesson isn't grounded in a pending node."""
        out: dict = {}
        for nid in node_ids or []:
            sig = self._node_sig(state.nodes.get(nid))
            if sig is not None:
                out[str(nid)] = sig
        return out

    def _lesson_evidence_stale(self, state: RunState, o: dict) -> bool:
        """True iff a lesson's grounding nodes no longer match the OUTCOME SIGNATURE it was distilled
        from — a re-eval FLIPPED something it depends on. Requires the exact `evidence_sig`: a node now
        pending/absent (sig None) is 'not yet resolved', NOT drift (wait for its re-eval). LEGACY rows
        (written before sigs) carry NO reliable provenance and are NEVER judged stale — an outcome-only
        heuristic is unsound here: a lesson's `outcome` is a VERDICT (a comparative 'failed' means the
        change REGRESSED, a reflect '[BAD]' means 'avoid this technique'), NOT the node's crash/eval
        STATUS, so comparing the two mis-fires on legitimate lessons whose nodes are evaluated (observed
        in prod: it retired two valid 'this change regressed' comparative lessons). Legacy rows age out
        via normal consolidation instead; everything written from here on carries a sig."""
        sig = o.get("evidence_sig")
        if not (isinstance(sig, dict) and sig):
            return False
        return any((cur := self._node_sig(state.nodes.get(self._coerce_id(k)))) is not None
                   and cur != v for k, v in sig.items())

    def reconcile_lessons(self, state: RunState) -> RunState:
        """Memory reconciliation on a CHANGED OUTCOME (the node_reset / re-eval seam). Fold-derived
        memory (hypotheses/champion/leaderboard) self-corrects every fold, but DISTILLED lessons are
        written to the cross-run file and go stale when a node's outcome later flips — a false-failure
        re-scored to evaluated, a demoted champion. This re-aligns THIS run's lessons with the folded
        state: every lesson whose grounding-node signature moved is RETIRED and RE-DERIVED from the
        corrected state (same conclusion → an identical lesson reappears = no-op; different → the stale
        row is replaced — the 'find the old one by its evidence node id and rewrite it' the design asks
        for). Cheap gate: a hash of the run's {node -> sig}; the file is touched only when a signature
        actually moved and the LLM only when a lesson is genuinely stale. Best-effort — a reconcile
        failure never fails the run. No-op offline (can't re-derive) or when reflection memory is off."""
        if not (self._e._reflection_priors and self._e.memory_dir):
            return state
        # Change-gate: only scan when some node's outcome sig moved since the last look. A node_reset
        # re-eval that alters a metric/status flips the hash; plain forward progress (a new terminal)
        # flips it too — harmless, the scan then finds nothing stale. None on start → the first pass
        # always scans, so a resume/restart verifies the store against the folded state once.
        sig_items = tuple(sorted((nid, self._node_sig(n)) for nid, n in state.nodes.items()))
        h = hash(sig_items)
        if h == self._reconcile_sig_hash:
            return state
        self._reconcile_sig_hash = h
        path = Path(self._e.memory_dir) / "lessons.jsonl"
        if not path.exists():
            return state
        try:
            rows: list = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(orjson.loads(line))
                except Exception:  # noqa: BLE001
                    rows.append(None)
        except OSError:
            return state
        # Which of THIS run's lessons drifted? Reflect-type (whole-run generalization) vs comparative
        # (per-pair). A comparative row is keyed by its [child, parent] evidence pair.
        stale_idx: set[int] = set()
        stale_pairs: list[tuple] = []
        reflect_stale = False
        for idx, o in enumerate(rows):
            if not isinstance(o, dict) or o.get("run_id") != state.run_id:
                continue
            if not self._lesson_evidence_stale(state, o):
                continue
            stale_idx.add(idx)
            if o.get("source") == "comparative" and len(o.get("evidence") or []) == 2:
                stale_pairs.append(tuple(o["evidence"]))
            else:
                reflect_stale = True
        if not stale_idx:
            return state
        # Lessons are LLM-only; re-derivation needs the client. Offline → leave the stale rows (a
        # templated stand-in is exactly what this module refuses to write) and try again when wired.
        client = self._e._reflect_client()
        if client is None:
            self._reconcile_sig_hash = None   # not truly reconciled — re-check once a client appears
            return state
        try:
            fp = self._e._task_fingerprint(state, state.best())
            fresh_reflect = self._e._reflect_lessons(state, state.best(), fp) if reflect_stale else []
            # A reflect (whole-run) lesson drifting invalidates the WHOLE reflect batch for this run —
            # the generalization spans all its fed nodes — so replace EVERY reflect row of this run, not
            # just the one that drifted (retiring one would leave near-dup siblings to merge against the
            # fresh batch). BUT only when re-derivation actually produced lessons: an empty/failed LLM
            # re-derivation must NOT nuke existing memory — in that case we still drop the specifically
            # drifted rows (already in `stale_idx`; they're wrong) but keep the non-drifted siblings.
            if reflect_stale and fresh_reflect:
                for idx, o in enumerate(rows):
                    if (isinstance(o, dict) and o.get("run_id") == state.run_id
                            and o.get("source") != "comparative"):
                        stale_idx.add(idx)
            comp: list = []
            pairs_used: list = []
            if stale_pairs and self._e._comparative_lessons_on:
                # Un-spend the drifted pairs so `select_comparison_pairs` can re-pick them, then re-derive.
                _stale = {tuple(p) for p in stale_pairs}
                exclude = [p for p in self._e._spent_pairs(state) if tuple(p) not in _stale]
                comp, pairs_used = self._e._comparative_lessons(state, fp, exclude=exclude)
            fresh = fresh_reflect + comp
            # Rewrite: drop the stale rows, keep the rest, append the fresh re-derivations — under the
            # SAME interprocess lock append_lessons uses (a concurrent run's O_APPEND between our read
            # and rewrite would otherwise be clobbered). Then the D2 hygiene pass consolidates/compacts.
            from looplab.events.eventstore import _interprocess_lock
            from looplab.core.atomicio import atomic_write_text
            kept = [o for i, o in enumerate(rows) if isinstance(o, dict) and i not in stale_idx]
            with _interprocess_lock(Path(str(path) + ".lock")):
                atomic_write_text(path, "".join(orjson.dumps(o).decode() + "\n"
                                                for o in kept + fresh))
                prompts, parser = self._merge_prompt_opts()
                self._e._consolidate_lessons_file(path, client, self._e._embedder,
                                                  parser=parser, prompts=prompts)
                self._e._compact_lessons(path)
        except Exception:  # noqa: BLE001 — reconciliation is best-effort; never fail the run for it
            return state
        # Ledger consistency: the re-derived pairs are (re-)spent so run-end reflection won't double
        # them — recorded as a lessons_distilled(reconcile) exactly like the mid-run cadence.
        if pairs_used:
            self._e.store.append(EV_LESSONS_DISTILLED, {
                "at_node": len(state.nodes), "trigger": "reconcile", "count": len(comp),
                "pairs": [[pr["a"], pr["b"]] for pr in pairs_used],
                "lessons": [{"statement": lz["statement"], "outcome": lz["outcome"],
                             "evidence": lz.get("evidence")} for lz in comp]})
        # Audit sidecar (fold ignores it): what drifted and what replaced it.
        self._e.store.append(EV_LESSONS_RECONCILED, {
            "at_node": len(state.nodes), "n_retired": len(stale_idx), "n_added": len(fresh),
            "reflect": reflect_stale, "pairs": [list(p) for p in stale_pairs]})
        return fold(self._e.store.read_all())

    def maybe_distill_lessons(self, state: RunState) -> RunState:
        """M6 write side (doc 13 §7 items 2+5): every `lessons_every` NEW nodes, distill
        comparative lessons and append them to the SHARED cross-run store IMMEDIATELY — a
        concurrent run's refresh (read side below) can pick them up mid-flight, the AgentRxiv
        live-share pattern. The `lessons_distilled` event is the replay-safe gate (at_node +
        the pair ids already spent); fires only at a creation decision point (no pending evals),
        mirroring deep-research. No-op when the cadence is 0 or reflection memory is off."""
        if (self._e.lessons_every <= 0 or not self._e._comparative_lessons_on
                or not (self._e._reflection_priors and self._e.memory_dir)):
            return state
        if state.pending_nodes():
            return state
        n = len(state.nodes)
        last = max((int(d.get("at_node") or 0) for d in state.lessons_distilled), default=0)
        if not self._e._cadence_due(n, last, self._e.lessons_every):
            return state
        fp = self._e._task_fingerprint(state, state.best())
        lessons, pairs = self._e._comparative_lessons(state, fp, exclude=self._e._spent_pairs(state))
        # Event BEFORE the store write, and always — even with 0 lessons — so the at_node gate
        # advances and the loop doesn't retry this node-count. Event-first ordering: if the
        # process dies between the two writes, a resume sees the gate advanced and skips — the
        # store misses one batch (best-effort memory) instead of re-invoking the LLM and
        # appending the same lessons twice. The statements ride in the event for audit.
        self._e.store.append(EV_LESSONS_DISTILLED, {
            "at_node": n, "trigger": "cadence", "count": len(lessons),
            "pairs": [[pr["a"], pr["b"]] for pr in pairs],
            "lessons": [{"statement": lz["statement"], "outcome": lz["outcome"],
                         "evidence": lz.get("evidence")} for lz in lessons]})
        # Hygiene deferred to run end: the read path dedups/quarantines already, and a full-file
        # rewrite of the shared store every few nodes would race other runs' appends for nothing.
        self._e._append_lessons(lessons, hygiene=False)
        return fold(self._e.store.read_all())

    def lessons_store_stamp(self):
        """(size, mtime_ns) of the shared lessons store, or None — the cheap change detector the
        refresh gate uses to skip a full re-read/re-score when no run has written since."""
        if not self._e.memory_dir:
            return None
        try:
            st = (Path(self._e.memory_dir) / "lessons.jsonl").stat()
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def maybe_refresh_lessons(self, state: RunState) -> RunState:
        """M6 read side (doc 13 §7 item 5): every `lessons_refresh_every` NEW nodes, re-read the
        SHARED cross-run store and rebuild the proposal prior — so lessons a CONCURRENT run
        distilled after this run started reach this run's next proposals (pre-M6, the store was
        read at run start only). No LLM call; this run's own lessons are excluded (they're already
        in the digest). The `lessons_refreshed` event is the replay-safe cadence gate. When the
        store file is UNCHANGED since the last look (stat stamp), the rebuild — a full re-read +
        re-score (+ harmonic re-embed) of every lesson — is skipped; the gate still advances.
        No-op when the cadence is 0 or reflection memory is off."""
        if self._e.lessons_refresh_every <= 0 or not (self._e._reflection_priors and self._e.memory_dir):
            return state
        n = len(state.nodes)
        last = max((int(d.get("at_node") or 0) for d in state.lessons_refreshed), default=0)
        if not self._e._cadence_due(n, last, self._e.lessons_refresh_every):
            return state
        stamp = self._e._lessons_store_stamp()
        if stamp == self.seen_stamp:
            self._e.store.append(EV_LESSONS_REFRESHED, {"at_node": n, "skipped": "unchanged"})
            return fold(self._e.store.read_all())
        self.seen_stamp = stamp
        before = (self.prior_note_text, self.dev_prior_note_text)
        rid = state.run_id or None
        # ONE scan for BOTH role priors (see load_reflection_priors_both). `changed` must reflect
        # EITHER prior moving — a concurrent run distilling only developer-tagged code-fix lessons
        # updates just dev_prior_note_text, and the refresh audit signal must not report that as
        # unchanged. `chars` sums both priors so the size delta is likewise visible for either role.
        self.prior_note_text, self.dev_prior_note_text = \
            self._e._load_reflection_priors_both(exclude_run_id=rid)
        self._e.store.append(EV_LESSONS_REFRESHED, {
            "at_node": n, "chars": len(self.prior_note_text) + len(self.dev_prior_note_text),
            "changed": (self.prior_note_text, self.dev_prior_note_text) != before})
        return fold(self._e.store.read_all())

    def distill_skill_body(self, final: RunState, h, ev: list) -> str:
        """A skill is a reusable BEST PRACTICE — the technique + a MINIMAL snippet/script the agent can
        reuse, NOT a dump of the whole solution. LLM-distil the essential lines from the winning code;
        fall back to a code-free evidence summary when there's no client / no code."""
        ev_txt = "\n".join(f"- #{n.id} {n.operator} metric={n.metric} params={n.idea.params}: "
                           f"{' '.join((n.idea.rationale or '').split())[:120]}" for n in ev[:4])
        base = (f"Verified on task `{final.task_id}` (best Δ={h.best_delta:+.4g}).\n\n"
                f"Evidence:\n{ev_txt}\n\nApply when the task matches this technique's preconditions; "
                "re-validate with the eval before trusting it.")
        client = self._e._reflect_client()
        code_node = max((n for n in ev if getattr(n, "code", None)),
                        key=lambda n: (n.metric if n.metric is not None else -1e18), default=None)
        if client is None or code_node is None or not code_node.code:
            return base
        prompt = (f"A technique that worked: {h.statement}\n\nThe winning solution's code:\n"
                  f"```\n{code_node.code[:4000]}\n```\n\n"
                  "Write a SHORT, REUSABLE skill card for THIS technique — not the whole script. Include:\n"
                  "1. The technique in 1-2 sentences (what it is + why it helps).\n"
                  "2. A MINIMAL code snippet — ONLY the essential, generalized lines that implement the "
                  "technique (a few lines), not the full solution.\n"
                  "3. When to use it (preconditions) and when NOT to.\n"
                  "Keep it concise — a card someone reuses, never a code dump.")
        try:
            from looplab.agents.agent import agentic_text
            out = (agentic_text(client, self._reflect_tools(final), [{"role": "user", "content": prompt}],
                                loop_opts=self._reflect_loop_opts(),
                                answer_desc="a short reusable skill card: technique + minimal snippet + when to use")
                   or "").strip()
            return (f"{out[:1800]}\n\n_Verified on `{final.task_id}` (Δ={h.best_delta:+.4g})._"
                    if out else base)
        except Exception:   # noqa: BLE001 - best-effort
            return base

    def reflect_client(self):
        """The LLM client to use for run-end distillation — the Researcher's (unwrapping any
        surrogate/fallback), else the Developer's. None when no LLM client is wired (toy backends)."""
        r = getattr(self._e, "researcher", None)
        for obj in (r, getattr(r, "inner", None), getattr(r, "fallback", None),
                    getattr(self._e, "developer", None)):
            c = getattr(obj, "client", None)
            if c is not None and hasattr(c, "complete_text"):
                return c
        return None

    def _merge_prompt_opts(self) -> tuple:
        """(prompts, parser) for the hybrid+agent lesson merge. The PromptStore and the configured
        structured-output parser live on the ROLES, not the engine (tasks.py wires
        `researcher.prompts` from `prompt_dir`; LLMResearcher carries `parser` from
        `settings.llm_parser`) — so unwrap the same researcher→inner→fallback→developer chain
        `reflect_client` walks. (None, "tool_call") when nothing is wired (toy backends) — exactly
        the defaults `agent_merge` assumed before these were threaded."""
        r = getattr(self._e, "researcher", None)
        chain = (r, getattr(r, "inner", None), getattr(r, "fallback", None),
                 getattr(self._e, "developer", None))
        prompts = next((p for o in chain if (p := getattr(o, "prompts", None)) is not None), None)
        parser = next((p for o in chain if (p := getattr(o, "parser", None))), "tool_call")
        return prompts, parser

    def causal_meta_note(self, final: RunState, best) -> Optional[str]:
        """LLM-distilled 'WHY the winner won' — a reusable causal note (the meta-note's real purpose,
        distinct from the case's raw config). Returns None on no-client / any error → caller falls back
        to the stats line, so this never fails the run."""
        client = self._e._reflect_client()
        if client is None:
            return None
        rev = (final.direction != "min")
        ev = sorted((n for n in final.evaluated_nodes() if n.metric is not None),
                    key=lambda n: n.metric, reverse=rev)[:6]
        rows = [f"#{n.id} {n.operator} metric={n.metric:.4g} params={n.idea.params}"
                + (f" — {' '.join((n.idea.rationale or '').split())[:90]}" if n.idea.rationale else "")
                for n in ev]
        prompt = (f"Task goal: {final.goal}\nObjective: {'maximize' if rev else 'minimize'} the metric.\n"
                  f"Experiments (best first):\n" + "\n".join(rows) +
                  f"\n\nThe winner is #{best.id}. In 2-3 sentences, state WHY it won: the KEY factors "
                  "that mattered and what did NOT help — a reusable CAUSAL note a future run on this "
                  "task can learn from. Be specific and concise; no preamble, don't just restate params.")
        try:
            from looplab.agents.agent import agentic_text
            out = (agentic_text(client, self._reflect_tools(final), [{"role": "user", "content": prompt}],
                                loop_opts=self._reflect_loop_opts(),
                                answer_desc="a 2-3 sentence reusable causal note on WHY the winner won")
                   or "").strip()
            return out[:700] or None
        except Exception:   # noqa: BLE001 - reflection is best-effort
            return None

    @staticmethod
    def consolidate_lessons_file(path: Path, client=None, embed=None,
                                 parser: str = "tool_call", prompts=None) -> None:
        """D2: rewrite lessons.jsonl through `consolidate_lessons` — duplicate claims merge into
        an evidence_count and a contradicted verdict is retired (the newest observation wins). When a
        `client` is wired, a hybrid-retrieval + agent pass ALSO merges paraphrase-level duplicates the
        exact key misses (`parser`/`prompts` configure that pass's structured-output parser and
        merge_system override). Atomic rewrite; best-effort (a hygiene failure must never fail the run)."""
        try:
            from looplab.engine.memory import consolidate_lessons
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    o = orjson.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(o, dict):
                    rows.append(o)
            merged = consolidate_lessons(rows, client=client, embed=embed,
                                         parser=parser, prompts=prompts)
            if len(merged) < len(rows):
                from looplab.core.atomicio import atomic_write_text
                atomic_write_text(path, "".join(orjson.dumps(o).decode() + "\n" for o in merged))
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def compact_lessons(path: Path, max_lines: int = 4000, keep: int = 2000) -> None:
        """Bound the shared lessons store: it is re-read and scored at every run start, and grows by
        a few lines per finished run forever. Past `max_lines`, keep the most recent `keep` (recency
        also wins ties at retrieval, so the dropped prefix is the least useful part)."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) > max_lines:
                from looplab.core.atomicio import atomic_write_text
                atomic_write_text(path, "\n".join(lines[-keep:]) + "\n")
        except Exception:  # noqa: BLE001 — compaction is best-effort; never fail the run for it
            pass

    def store_case(self, final: RunState) -> None:
        """Cross-run memory (I19): persist the best result as a retrievable case."""
        if not self._e.memory_dir:
            return
        best = final.best()
        if best is None:
            return
        lib = JsonlCaseLibrary(Path(self._e.memory_dir) / "cases.jsonl")
        lib.add({
            "task_id": final.task_id,
            "goal": final.goal,
            "direction": final.direction,
            "params": best.idea.params,
            "metric": best.confirmed_mean if best.confirmed_mean is not None else best.metric,
            "rationale": best.idea.rationale,
        })
