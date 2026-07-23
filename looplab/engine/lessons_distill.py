"""LLM distillation for the lessons cluster — extracted from engine/lessons.py as a MIXIN (the
Engine's own convention, see engine/novelty.py): `class LessonMemory(…, LessonDistillMixin, …)`
inherits these methods unchanged, so there is ZERO call-site churn and `self` here IS the
LessonMemory — the bodies are verbatim moves, reading the engine through `self._e` and sibling
cluster methods through the Engine's thin delegators (so a test monkeypatching e.g.
`engine._reflect_client` still intercepts every internal call).

The WRITE side's LLM half: the whole-run reflection (`reflect_lessons`, M3), the E4 causal
meta-note, the M4 skill-card distillation (`distill_skill_body`), the run-end
`write_reflection_note` that stitches them together, and the shared reflection tool-loop
plumbing (`_reflect_tools` / `_reflect_loop_opts` / `_merge_prompt_opts`).

Layering: like lessons.py, no runtime import of the orchestrator and never serve — only
engine.memory, events, core and stdlib/orjson (the agent/tool deps stay lazy, method-local
imports)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import orjson

from looplab.core.atomicio import append_jsonl_bytes_locked
from looplab.core.models import NodeStatus, RunState, safe_lesson_node_count
from looplab.engine.lessons_priors import LESSON_ROLE_RESEARCHER
from looplab.events.eventstore import _interprocess_lock, read_jsonl_lenient
from looplab.events.types import EV_LESSONS_DISTILLED, EV_REFLECTION_NOTE


class LessonDistillMixin:
    """The lessons cluster's LLM-distillation half. See the module docstring for the mixin
    convention (`self` is the LessonMemory)."""

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
        _reflection_notes = [e.data for e in self._e.store.read_all()
                             if e.type == EV_REFLECTION_NOTE]
        _reflected_at = [parsed for d in _reflection_notes
                         if (parsed := safe_lesson_node_count(d.get("at_nodes"))) is not None]
        if _reflected_at and len(final.nodes) <= max(_reflected_at):
            return
        # Crash-idempotency (#3): finalization is RETRIED after a crash. If this EXACT finish already
        # committed a reflection marker, re-running would re-spend the reflection LLM (and, below, risk
        # a duplicate meta_notes line). A REOPENED run gets a NEW run_finished seq, so its later
        # reflection is not skipped here (it also grows the node count above). `last_finish_seq` is -1
        # only off the modern finalize path — then this gate is inert and the meta_notes de-dup below
        # still guards the file. This closes the COMMON retry (crash after the marker, during a later
        # finalize step); the narrow crash-DURING-reflection window is closed by the file de-dup below.
        finish_seq = final.last_finish_seq
        _has_finish_seq = finish_seq is not None and finish_seq >= 0
        if _has_finish_seq and any(d.get("finish_seq") == finish_seq for d in _reflection_notes):
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
            # Idempotent append (#3): a crash AFTER this line but BEFORE the reflection marker below
            # would, on the finalization retry, blindly append a SECOND identical note (polluting a
            # later run's warm-start prior + wasting the LLM call). The file itself is the durable
            # de-dup record, so it closes the crash window the event-log marker cannot (the marker isn't
            # written yet on that crash). De-dup key is (run_id, finish_seq): meta_notes.jsonl is a
            # CROSS-RUN file and finish_seq is only a per-run event-log sequence, so two different runs
            # can share one — run_id makes the key unique to THIS run's finish. A real reopen has a NEW
            # finish_seq, so its updated note still appends (gated by the node-count watermark above);
            # only a crash-retry of the SAME finish is skipped. Legacy/off-finalize-path notes (no
            # finish_seq, or -1) fall through to the historical blind append.
            npath = base / "meta_notes.jsonl"
            # The duplicate check and append are one transaction.  Concurrent finalizers can
            # otherwise both observe absence and append the same note, while a crash-torn last line
            # can swallow the next valid record for every line-oriented reader.
            with _interprocess_lock(Path(str(npath) + ".lock")):
                _dup = _has_finish_seq and any(
                    o.get("run_id") == final.run_id and o.get("finish_seq") == finish_seq
                    for o in read_jsonl_lenient(npath))
                if not _dup:
                    rec = {"task_id": final.task_id, "note": note}
                    if _has_finish_seq:
                        rec["run_id"] = final.run_id
                        rec["finish_seq"] = finish_seq
                    append_jsonl_bytes_locked(npath, orjson.dumps(rec))

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
                from looplab.core.advisory_payloads import research_lesson_receipt
                self._e.store.append(EV_LESSONS_DISTILLED, {
                    "at_node": len(final.nodes), "trigger": "run_end", "count": len(comp),
                    "pairs": [[pr["a"], pr["b"]] for pr in pairs],
                    "lessons": [research_lesson_receipt(lz, final) for lz in comp]})
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
        # 1 card = 1 hypothesis: distil skills from the single Card board. `verdict` is the research
        # status (byte-identical to the old Hypothesis.status via `_evidence_verdict`); `seed_statement`
        # is the immutable belief text (the stable skill identity, unaffected by an operator display edit).
        for h in final.research_cards():
            if h.verdict == "supported" and (h.best_delta or 0) > 0:
                ev = [final.nodes[i] for i in h.evidence if i in final.nodes]
                write_auto_skill(sk_dir, h.seed_statement,
                                 self._e._distill_skill_body(final, h, ev), fp, final.task_id)
                skills.append(h.seed_statement)

        # Audit the run-end distillation in the event log (diagnostic sidecar — fold ignores it). These
        # LLM artifacts (the causal note, the generalizable lessons, the auto-promoted skills) shape
        # FUTURE runs' priors/skills yet otherwise leave no trace in THIS run's events.jsonl — only in
        # cross-run files. One summary event makes "what this run concluded & wrote to memory" visible.
        self._e.store.append(EV_REFLECTION_NOTE, {
            "task_id": final.task_id, "fingerprint": fp, "note": note,
            "finish_seq": finish_seq,          # #3: crash-idempotency key for a finalization retry
            "at_nodes": len(final.nodes),      # coverage watermark: re-reflect only if a reopen grows past it
            "n_lessons": len(lessons), "n_skills": len(skills),
            "lessons": [{"statement": lz.get("statement", ""), "outcome": lz.get("outcome", ""),
                         "claim_stance": lz.get("claim_stance")}
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
                     "outcome": "supported", "claim_stance": "support",
                     "delta": None, "confidence": 0.7,
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
        aborted = set(getattr(final, "aborted_nodes", None) or [])
        bad = [n for n in final.nodes.values()
               if n.status is NodeStatus.failed and not n.tombstoned and n.id not in aborted][:3]
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
        # 1 card = 1 hypothesis: consolidate over the single Card board. `verdict` == the old
        # Hypothesis.status (RESOLVED lanes only); `seed_statement` is the immutable belief text.
        hyps = [f"[{h.verdict}{f' Δ{h.best_delta:+.4g}' if isinstance(h.best_delta, (int, float)) else ''}] "
                f"{' '.join((h.seed_statement or '').split())[:160]}"
                for h in final.research_cards()
                if h.verdict in ("supported", "tested", "abandoned") and h.seed_statement]
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
                  "[GOOD] (reuse this) or [BAD] (avoid this). The sentence itself must be a conclusion "
                  "supported by the run evidence; [GOOD]/[BAD] controls guidance, not truth. One per "
                  "line, no preamble.")
        try:
            from looplab.agents.agent import agentic_text
            out = agentic_text(client, self._reflect_tools(final), [{"role": "user", "content": prompt}],
                               loop_opts=self._reflect_loop_opts(),
                               answer_desc="generalizable lessons, one theme per line, each tagged [GOOD]/[BAD]") or ""
        except Exception:   # noqa: BLE001 - best-effort; a real run writes NO templated fallback
            return []
        from looplab.engine.memory import distilled_claim_stance, parse_credit_lessons
        # No fixed cap: one lesson per distinct theme (consolidation keeps this small); bound at 8 as a
        # runaway guard, not a target. §role-split: these are generalizable technique/strategy takeaways
        # → the RESEARCHER's context (what to try next). n_pairs=0 (reflection lines carry no valid
        # P-marker) — pass limit=8 explicitly: the parser's default cap is max(3, n_pairs)=3, which
        # used to silently drop themes 4-8 (the [:8] slice was dead) (architecture-review M6).
        res = [{"task_id": final.task_id, "fingerprint": fp,
                "kind": getattr(self._e.task, "kind", ""), "statement": stmt,
                "outcome": outcome, "delta": None, "confidence": 0.6,
                "claim_stance": distilled_claim_stance(outcome),
                "run_id": final.run_id, "evidence": list(ev_ids), "evidence_sig": ev_sig,
                "role": LESSON_ROLE_RESEARCHER}
               for _, stmt, outcome in parse_credit_lessons(out, 0, limit=8)]
        return res      # LLM gave nothing usable → [] (a real run never writes a templated lesson)

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
        # 1 card = 1 hypothesis: prefer the immutable `seed_statement` (matches the skill title the
        # caller writes) so an operator display edit can't drift the technique belief; fall back to
        # `statement` for any legacy caller that still passes a bare hypothesis-shaped object.
        belief = getattr(h, "seed_statement", "") or getattr(h, "statement", "")
        prompt = (f"A technique that worked: {belief}\n\nThe winning solution's code:\n"
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
