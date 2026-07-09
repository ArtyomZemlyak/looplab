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
from looplab.events.types import (EV_DEV_LESSONS_DISTILLED, EV_LESSONS_DISTILLED,
                                  EV_LESSONS_REFRESHED, EV_REFLECTION_NOTE)

if TYPE_CHECKING:  # engine type hint only — no runtime import of the orchestrator
    from looplab.engine.orchestrator import Engine


class LessonMemory:
    """The engine's cross-run memory / lessons / reflection cluster. See the module docstring
    for the `self._e` (engine handle) convention."""

    def __init__(self, engine: "Engine") -> None:
        self._e = engine
        self.seen_stamp = None   # (size, mtime_ns) of the store at the last read
        self.prior_note_text = ""   # E4: cross-run meta-review prior, loaded at run start

    def load_reflection_priors(self, exclude_run_id: Optional[str] = None) -> str:
        """E4 + M2/M3: build the cross-run prior injected into the proposal prompt. Two parts:
        (1) exact-task "what won" notes (meta_notes.jsonl — unchanged E4 warm-start), and
        (2) LESSONS retrieved by task-FINGERPRINT similarity (M2), so a *similar but new* task also
        benefits — including NEGATIVE lessons (what was tested/abandoned/failed, M3) so the search
        doesn't re-tread a known dead end. Empty unless enabled + present. `exclude_run_id` drops
        lessons THIS run wrote (M6 mid-run distillation / resume): a run must not read its own
        output back as another run's experience — those results are already in its digest."""
        if not (self._e._reflection_priors and self._e.memory_dir):
            return ""
        base = Path(self._e.memory_dir)
        out = ""
        # (1) exact-task meta notes (E4)
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
        if notes:
            out += "\nPrior-run insights for this task (meta-learned): " + " | ".join(notes[-3:])
        # (2) fingerprint-matched lessons (M2/M3), incl. negatives
        lpath = base / "lessons.jsonl"
        if lpath.exists():
            from looplab.engine.memory import fingerprint_similarity
            # Compare WITHOUT param: tokens: the writer stamps the winner's param names, but at
            # read time no winner exists yet, so those tokens only dilute the Jaccard overlap.
            fp = [t for t in self._e._task_fingerprint(self._e._empty_state_for_fp())
                  if not t.startswith("param:")]
            all_lessons: list[tuple[int, dict]] = []
            scored: list[tuple[float, int, dict]] = []
            for idx, line in enumerate(lpath.read_text(encoding="utf-8").splitlines()):
                try:
                    o = orjson.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(o, dict) or not o.get("statement"):
                    continue
                if exclude_run_id and o.get("run_id") == exclude_run_id:
                    continue                     # M6: never echo this run's own lessons back
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
                        all_lessons, query, self._e._lesson_abstractor, self._e._embedder):
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
                out += "\nLessons from related runs (what did/didn't work): " + "; ".join(picked)
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
        # Run-end reflection must run at MOST ONCE per run: it appends to cross-run memory
        # (meta_notes.jsonl + lessons.jsonl) and re-spends LLM calls (the causal note + M3 lessons).
        # A reopened run (EV_RESUME + EV_BUDGET_EXTEND) re-finishes and re-enters finalize, so without
        # this gate a second pass would double-append a run's hypothesis/failure lessons — which
        # `consolidate_lessons` groups by (statement, task_id) and sums, inflating `evidence_count` so
        # one run reads as "verified on 2 runs". `reflection_note` is the end-of-reflection marker
        # (a diagnostic sidecar the fold ignores), emitted unconditionally at the tail of this method;
        # its presence means we already reflected. Mirrors the M6 comparative-pair ledger's own guard.
        if any(e.type == EV_REFLECTION_NOTE for e in self._e.store.read_all()):
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
        for h in (final.hypotheses or {}).values():
            if h.status in ("supported", "tested", "abandoned"):
                lessons.append({
                    "task_id": final.task_id, "fingerprint": fp,
                    "kind": getattr(self._e.task, "kind", ""), "statement": h.statement,
                    "outcome": h.status, "delta": h.best_delta,
                    "confidence": 0.7 if h.status == "supported" else 0.5,
                    "run_id": final.run_id, "evidence": list(h.evidence)[:8]})
        # dominant failure theme (so a repeat run is warned off the same crash class)
        reasons: dict[str, int] = {}
        for n in final.nodes.values():
            if n.status is NodeStatus.failed and n.error_reason:
                reasons[n.error_reason] = reasons.get(n.error_reason, 0) + 1
        if reasons:
            top = max(reasons, key=reasons.get)
            # run_id stamped like every other lesson shape: the M6 own-run exclusion filters on it,
            # and a row without the key would be read back as another run's experience on resume.
            lessons.append({
                "task_id": final.task_id, "fingerprint": fp, "kind": getattr(self._e.task, "kind", ""),
                "statement": f"{reasons[top]} node(s) failed with reason '{top}'",
                "outcome": "failed", "delta": None, "confidence": 0.4, "run_id": final.run_id})
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
            "n_lessons": len(lessons), "n_skills": len(skills),
            "lessons": [{"statement": lz.get("statement", ""), "outcome": lz.get("outcome", "")}
                        if isinstance(lz, dict) else {"statement": str(lz), "outcome": ""}
                        for lz in lessons[:12]],
            "skills": skills[:8]})

    def write_dev_lessons(self, final: RunState) -> None:
        """D-memory run-end distillation: extract a few generalizable IMPLEMENTATION lessons from THIS
        run's build/repair history (crash classes, what a repair changed, an approach that worked) and
        append them to `<memory_dir>/dev_lessons.jsonl` — so a future run building on a SIMILAR repo
        reuses them. Distinct from `write_reflection_note` (which records WHICH experiment to run); this
        records HOW to build one. Gated ONCE per run by `EV_DEV_LESSONS_DISTILLED` (a reopened run must
        not double-append — `consolidate_lessons` would fold the dup into an inflated evidence_count).
        Best-effort; never raises."""
        if not (getattr(self._e, "_developer_memory", False) and self._e.memory_dir):
            return
        if any(e.type == EV_DEV_LESSONS_DISTILLED for e in self._e.store.read_all()):
            return
        fp = self._e._task_fingerprint(final, final.best())
        lessons = self._distill_dev_lessons(final, fp)
        n = 0
        try:
            from looplab.tools.dev_memory import append_dev_lessons
            n = append_dev_lessons(self._e.memory_dir, lessons)
        except Exception:  # noqa: BLE001 — a memory write must never abort finalization
            n = 0
        # Sidecar audit (fold ignores it) + the once-per-run gate — emitted even when n==0 so a resume
        # doesn't re-distill (mirrors EV_REFLECTION_NOTE).
        self._e.store.append(EV_DEV_LESSONS_DISTILLED, {
            "task_id": final.task_id, "fingerprint": fp, "n_lessons": n,
            "lessons": [{"statement": lz.get("statement", ""), "outcome": lz.get("outcome", "")}
                        for lz in lessons[:12]]})

    def _distill_dev_lessons(self, final: RunState, fp: list) -> list:
        """Build the IMPLEMENTATION lessons for `write_dev_lessons`: LLM-generalized from the build/
        repair history when a client is wired, else a deterministic fallback (the dominant crash class
        as a pitfall) so an offline run still records something transferable. [] when there's nothing to
        say (e.g. a clean toy run with no failures and no client)."""
        kind = getattr(self._e.task, "kind", "") or ""
        llm = self._llm_dev_lessons(final, fp, kind)
        if llm:
            return llm
        # Deterministic fallback: the dominant CODE-failure class → a pitfall to guard against.
        reasons: dict[str, int] = {}
        for n in final.nodes.values():
            if n.status is NodeStatus.failed and n.error_reason:
                reasons[n.error_reason] = reasons.get(n.error_reason, 0) + 1
        if not reasons:
            return []
        top = max(reasons, key=reasons.get)
        return [{"task_id": final.task_id, "fingerprint": fp, "kind": kind,
                 "statement": f"when building this kind of task, guard against '{top}' failures "
                              f"({reasons[top]} node(s) hit it this run)",
                 "outcome": "pitfall", "confidence": 0.4, "run_id": final.run_id,
                 "evidence": [], "source": "distilled"}]

    def _llm_dev_lessons(self, final: RunState, fp: list, kind: str) -> list:
        """LLM reflection over the run's build/repair history → 1-3 GENERALIZABLE implementation lessons
        (transferable coding gotchas/techniques, not this run's numbers). Grounded in the real nodes via
        the read-only run tools. [] on no-client / nothing to reflect / error."""
        client = self._e._reflect_client()
        if client is None:
            return []
        fails = [n for n in final.nodes.values() if n.status is NodeStatus.failed][:5]
        repaired = [n for n in final.nodes.values()
                    if n.operator == "debug" and n.status is NodeStatus.evaluated][:5]
        rev = (final.direction != "min")
        ok = sorted((n for n in final.evaluated_nodes() if n.metric is not None),
                    key=lambda n: n.metric, reverse=rev)[:3]
        if not (fails or repaired or ok):
            return []
        rows = [f"#{n.id} FAILED ({n.error_reason}): {(n.error or '')[:180]}" for n in fails]
        rows += [f"#{n.id} repaired → ok: {' '.join((n.idea.rationale or '').split())[:160]}"
                 for n in repaired]
        rows += [f"#{n.id} worked (metric={n.metric:.4g})" for n in ok]
        prompt = (
            "Distil reusable IMPLEMENTATION lessons for a coding agent that BUILDS ML experiments on "
            f"repositories of kind '{kind or 'ml-repo'}'. Use the read tools (read_code/read_logs/"
            "read_experiment) to inspect the nodes below if useful, then write 1-3 GENERALIZABLE coding "
            "lessons — dataset-loading traps, framework/version API quirks, build/train pitfalls, an "
            "orchestration that worked — NOT this run's exact numbers. Tag each [GOOD] (a technique to "
            "reuse) or [BAD] (a pitfall to avoid). One per line, no preamble.\n"
            f"Task: {final.goal}\nBuild / repair history:\n" + "\n".join(rows))
        try:
            from looplab.agents.agent import agentic_text
            out = agentic_text(client, self._reflect_tools(final),
                               [{"role": "user", "content": prompt}],
                               loop_opts=self._reflect_loop_opts(),
                               answer_desc="1-3 generalizable implementation lessons, one per line, "
                                           "each tagged [GOOD]/[BAD]") or ""
        except Exception:  # noqa: BLE001 — best-effort
            return []
        from looplab.engine.memory import parse_credit_lessons
        res = []
        for _, stmt, outcome in parse_credit_lessons(out, 0)[:3]:
            res.append({"task_id": final.task_id, "fingerprint": fp, "kind": kind, "statement": stmt,
                        "outcome": "pitfall" if outcome == "failed" else "technique",
                        "confidence": 0.6, "run_id": final.run_id, "evidence": [], "source": "distilled"})
        return res

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
                     "run_id": final.run_id, "evidence": [best.id]}]
        client = self._e._reflect_client()
        if client is None or best is None:
            return _winner_lesson()
        rev = (final.direction != "min")
        ok = sorted((n for n in final.evaluated_nodes() if n.metric is not None),
                    key=lambda n: n.metric, reverse=rev)[:5]
        bad = [n for n in final.nodes.values() if n.status is NodeStatus.failed][:3]
        rows = [f"#{n.id} {n.operator} metric={n.metric:.4g} params={n.idea.params}" for n in ok]
        fails = [f"#{n.id} {n.operator} failed: {n.error_reason}" for n in bad]
        prompt = ("Distil reusable LESSONS from a finished ML experiment run, to guide FUTURE runs on "
                  f"SIMILAR tasks.\nTask: {final.goal}\nWhat worked (best first):\n" + "\n".join(rows) +
                  ("\nWhat failed:\n" + "\n".join(fails) if fails else "") +
                  "\n\nWrite 1-3 GENERALIZABLE lessons — transferable findings, NOT these exact numbers "
                  "(e.g. 'a larger batch size aided convergence', 'polynomial features overfit on small "
                  "data'). Tag each [GOOD] (reuse this) or [BAD] (avoid this). One per line, no preamble.")
        try:
            from looplab.agents.agent import agentic_text
            out = agentic_text(client, self._reflect_tools(final), [{"role": "user", "content": prompt}],
                               loop_opts=self._reflect_loop_opts(),
                               answer_desc="1-3 generalizable lessons, one per line, each tagged [GOOD]/[BAD]") or ""
        except Exception:   # noqa: BLE001 - best-effort
            return _winner_lesson()
        from looplab.engine.memory import parse_credit_lessons
        res = [{"task_id": final.task_id, "fingerprint": fp,
                "kind": getattr(self._e.task, "kind", ""), "statement": stmt,
                "outcome": outcome, "delta": None, "confidence": 0.6,
                "run_id": final.run_id, "evidence": []}
               for _, stmt, outcome in parse_credit_lessons(out, 0)[:3]]
        return res or _winner_lesson()      # LLM gave nothing usable → keep the winner record

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
                self._e._consolidate_lessons_file(path, self._e._reflect_client(), self._e._embedder)
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
            return {"task_id": state.task_id, "fingerprint": fp,
                    "kind": getattr(self._e.task, "kind", ""), "statement": statement,
                    "outcome": outcome, "delta": pr.get("delta") if pr else None,
                    "confidence": conf, "run_id": state.run_id,
                    "evidence": [pr["a"], pr["b"]] if pr else [], "source": "comparative"}

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
        except Exception:  # noqa: BLE001 — reflection is best-effort, never fails the run
            return _fallback(), pairs
        lessons = []
        for idx, stmt, outcome in parse_credit_lessons(out, len(pairs)):
            pr = pairs[idx] if idx >= 0 else None
            lessons.append(_lesson(pr, stmt, outcome, 0.65 if idx >= 0 else 0.5))
        return (lessons or _fallback()), pairs

    @staticmethod
    def spent_pairs(state: RunState) -> list:
        """Every (child, parent) pair a prior distillation already spent — the ledger both the
        mid-run cadence and run-end reflection exclude against, folded from `lessons_distilled`
        events (incl. the run-end one, so a reopened run never re-distills)."""
        return [tuple(p) for d in (state.lessons_distilled or [])
                for p in (d.get("pairs") or [])]

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
        before = self.prior_note_text
        self.prior_note_text = self._e._load_reflection_priors(exclude_run_id=state.run_id or None)
        self._e.store.append(EV_LESSONS_REFRESHED, {
            "at_node": n, "chars": len(self.prior_note_text),
            "changed": self.prior_note_text != before})
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
    def consolidate_lessons_file(path: Path, client=None, embed=None) -> None:
        """D2: rewrite lessons.jsonl through `consolidate_lessons` — duplicate claims merge into
        an evidence_count and a contradicted verdict is retired (the newest observation wins). When a
        `client` is wired, a hybrid-retrieval + agent pass ALSO merges paraphrase-level duplicates the
        exact key misses. Atomic rewrite; best-effort (a hygiene failure must never fail the run)."""
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
            merged = consolidate_lessons(rows, client=client, embed=embed)
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
