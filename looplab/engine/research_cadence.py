"""Research cadence (P2) for the engine — the Deep-Research stage (serial + concurrent seams),
the agentic open-hypothesis-board merge, and the run-report cadence — extracted from
orchestrator.py as a MIXIN: `class Engine(…, ResearchCadenceMixin)` inherits these methods
unchanged, so there is ZERO call-site churn and `self` here IS the engine. The method bodies are
verbatim moves and read engine attributes freely (`store` / `tracer` / `deep_researcher` /
`report_writer` / `_op_span` / `_cadence_due` / `_reflect_client` / `_embedder` / `lessons` /
`deep_research_every` / `report_every` / the `_research_verify`, `_track_hypotheses` knobs),
exactly as they did inside the class.

`_op_span` / `_cadence_due` / `_reflect_client` stay on the Engine (generic helpers / lessons
delegators); the moved methods call them as `self.…`, resolved on the Engine instance. The heavy
deps (ResearchMemo, verify_memo, hybrid_merge.consolidate) stay method-local imports, so a test
monkeypatching `looplab.trust.verify.verify_memo` etc. still intercepts them.

Layering: no runtime import of the orchestrator (TYPE_CHECKING only) and never serve — only core,
events and stdlib (the trust/search deps are lazy, method-local imports)."""
from __future__ import annotations

from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import RunState, idea_proposal_ref, normalize_researcher_footprint
from looplab.events.replay import fold
from looplab.events.types import (EV_HINT, EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED,
                                  EV_REPORT_GENERATED, EV_RESEARCH_COMPLETED,
                                  BACKGROUND_APPENDABLE,
                                  NON_CARD_SELECTION_BACKGROUND_APPENDABLE)


def research_memo_sig(memo) -> str:
    """Stable content signature of a research memo (its summary + recommended directions). PURE and
    deterministic. Used by the REPEATED concurrent-research loop to skip re-recording an identical
    memo: a long eval re-runs research on a timer, and when the analysis has converged the researcher
    returns the same conclusions — recording those again would bloat the log/hypothesis board without
    adding signal. Accepts a ResearchMemo (attr access) or a plain dict (the sanitized payload)."""
    import hashlib

    def _get(key):
        if isinstance(memo, dict):
            return memo.get(key)
        return getattr(memo, key, None)

    summary = str(_get("summary") or "").strip()
    directions = [str(d).strip() for d in (_get("recommended_directions") or []) if str(d).strip()]
    blob = summary + "\n" + "\n".join(directions)
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


class ResearchCadenceMixin:
    """The engine's research-cadence cluster (deep research + hypothesis merge + report). See the
    module docstring for the mixin convention (`self` is the Engine)."""

    # ---------------------------------------------------- research cadence (P2)
    def _maybe_deep_research(self, state: RunState) -> RunState:
        """Run the Deep-Research stage when there's demand, then re-fold. Three triggers, each gated
        for replay safety: a MANUAL `deep_research` control event (counter gate), a CADENCE
        (`deep_research_every`, once per node-count), or a Strategist `request_research` decided at
        this node-count. No-op when the stage is off or already served. Records a
        `research_completed` memo that is neutral for direct node/champion ranking and feeds its
        directions back as standing hints that can steer later proposals."""
        n = len(state.nodes)
        # Manual: serve outstanding requests first, regardless of node-count (operator asked now).
        if len(state.research_requests) > state.research_served:
            return self._run_deep_research(state, trigger="manual", manual=True)
        # Auto triggers only at a creation decision point (no pending evals), never re-firing at a
        # node-count already researched (the at_node gate makes resume a no-op).
        if state.pending_nodes() or n == 0 or self._already_researched_at(state, n):
            return state
        # Since-last cadence (not `n % every == 0`): a rung-0/seed batch that jumps the node count by
        # k>1 must not step over the only multiple and skip the whole window. The last researched
        # at_node is the marker; `_already_researched_at` above already de-dups the same-n resume.
        # `default=0` (no prior research → baseline at the run start, node 0): the first deep-research
        # fires a full `every` nodes in (n >= every), so the opening window is the SAME width as every
        # later one. (`default=-1` would fire it one node early — a narrower first window.)
        _last_research_n = max((int(m.get("at_node", -1)) for m in self._cadence_research_memos(state)
                                if m.get("at_node") is not None), default=0)
        if self._cadence_due(n, _last_research_n, self.deep_research_every):
            return self._run_deep_research(state, trigger="cadence", manual=False)
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return self._run_deep_research(state, trigger="strategist", manual=False)
        return state

    @staticmethod
    def _cadence_research_memos(state: RunState) -> list:
        """Research memos that COUNT toward the serial (node-count) cadence — everything EXCEPT the
        repeated concurrent-overlap memos (`trigger="repeat"`). Those fire on a TIME cadence during a
        long eval (`_research_overlap_loop`), so letting them advance the node-count marker would
        re-phase and suppress the between-nodes research pass — the one that runs with the freshest
        results at a no-pending decision point. Excluding them keeps the two mechanisms independent."""
        return [m for m in state.research
                if isinstance(m, dict) and (m or {}).get("trigger") != "repeat"]

    @classmethod
    def _already_researched_at(cls, state: RunState, n: int) -> bool:
        return any((m or {}).get("at_node") == n for m in cls._cadence_research_memos(state))

    def _run_deep_research(self, state: RunState, *, trigger: str, manual: bool) -> RunState:
        """Execute one Deep-Research step (serial path) and record it, then re-fold. Always records a
        `research_completed` event (even with no model wired, so a manual request's gate advances and
        the loop doesn't spin)."""
        # One trace for the whole serial step: compute WITHOUT its own inner span (trace=False) so the
        # research LLM spans + the research_completed append both live in THIS op-trace → the event is
        # stamped with it (UI scopes the event's trace to just the research, not a node).
        with self._op_span("deep_research", trigger=trigger):
            memo = self._compute_deep_research(state, trigger, trace=False)
            self._record_deep_research(memo, trigger=trigger, manual=manual)
        return fold(self.store.read_all())

    @in_llm_lane("deep_research")
    def _compute_deep_research(self, state: RunState, trigger: str, *, trace: bool = True):
        """PURE compute: run one Deep-Research step and RETURN the memo WITHOUT writing the event log,
        so it can run in a worker thread concurrently with an eval while the engine stays the sole
        writer. Best-effort — never raises (a crash/None model yields a stub so the gate still advances).
        `trace=False` skips the span: the tracer is not safe to write from the concurrent worker."""
        from looplab.core.models import ResearchMemo
        if self.deep_researcher is None:
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary="(deep research unavailable: no model configured)")
        try:
            if trace:
                with self.tracer.span("deep_research", new_trace=True, trigger=trigger):
                    return self.deep_researcher.research(state, trigger=trigger)
            return self.deep_researcher.research(state, trigger=trigger)
        except Exception as exc:  # noqa: BLE001 — best-effort research must never kill the run
            return ResearchMemo(at_node=len(state.nodes), trigger=trigger,
                                summary=f"(deep research failed: {exc})")

    # Every append below must stay in events.types.BACKGROUND_APPENDABLE: this method is invoked
    # from the CONCURRENT research task (`orchestrator._spawn_research`), the one enforced
    # exception to engine invariant #1 ("only the main task appends"). The assertions make a
    # future selection-affecting append here fail fast instead of racing the event order.
    @in_llm_lane("deep_research")
    def _record_deep_research(self, memo, *, trigger: str, manual: bool) -> None:
        """Append the memo to the event log. Called from BOTH the main-task cadence AND the
        concurrent research task — see the note above; every append here must stay in
        BACKGROUND_APPENDABLE."""
        from looplab.core.advisory_payloads import (
            research_claim_ref,
            research_memo_ref,
            sanitize_research_memo_payload,
        )
        # Verify the same canonical, redacted payload that can be persisted. Otherwise a custom
        # researcher can expose secrets/prompt controls to the verifier and receive a verdict over
        # evidence that is later truncated into a materially different durable memo.
        memo_payload = memo.model_dump(mode="json")
        # CODEX AGENT: ResearchMemo excludes the receipt from generic dumps for replay compatibility;
        # this durable writer must explicitly carry the original pre-cap denominator across sanitizers.
        if getattr(memo, "claims_receipt", None) is not None:
            memo_payload["claims_receipt"] = memo.claims_receipt
        memo_d = sanitize_research_memo_payload(memo_payload)
        # D8 · decoupled Verifier: check the memo's claims against their CITED evidence before the
        # memo is recorded — synthesis is the documented weak link (Kosmos: 57.9% accurate).
        # Deterministic layer always (refs exist? quoted numbers match?); LLM rubric pass when a
        # client is wired. Verdicts ride INSIDE the folded memo and cannot change this run's champion;
        # CODEX AGENT: finalize later uses their aligned support as the gate for positive D8 evidence.
        if self._research_verify and memo_d.get("claims"):
            try:
                from looplab.trust.verify import verify_memo
                state = fold(self.store.read_all())
                ver = verify_memo(memo_d, state,
                                  client=getattr(self.deep_researcher, "client", None),
                                  parser=getattr(self.deep_researcher, "parser", "tool_call"))
                if ver is not None:
                    memo_d["verification"] = ver
            except Exception:  # noqa: BLE001 — verification must never block the memo
                pass
        # The model, tool ledger, and verifier are all untrusted text producers. This
        # writer-side pass is the invariant: custom researchers cannot bypass redaction, control
        # stripping, list caps, or the aggregate text budget before any durable derivative.
        memo_d = sanitize_research_memo_payload(memo_d)
        # Layer 1b Card provenance is reference-only.  Mint the memo id from the FINAL canonical payload
        # (including verification) and bind every retained claim to that exact memo + positional slot.
        # The full bodies stay exclusively on the research timeline; Cards will carry only these ids.
        memo_id = research_memo_ref(memo_d)
        if memo_id is not None:
            memo_d["memo_id"] = memo_id
            for index, claim in enumerate(memo_d.get("claims", [])):
                claim_id = research_claim_ref(memo_id, index, claim)
                if claim_id is not None:
                    claim["claim_id"] = claim_id
        assert EV_RESEARCH_COMPLETED in BACKGROUND_APPENDABLE   # see the method-level note
        self.store.append(EV_RESEARCH_COMPLETED, {
            "memo": memo_d,
            **({"memo_id": memo_id} if memo_id is not None else {}),
            "at_node": memo.at_node, "trigger": trigger, "served_manual": manual})
        # Steer the next proposals: surface the memo's directions as a standing operator hint (the
        # same channel the Researcher already reads), so deep research actually informs planning.
        directions = [d for d in memo_d.get("recommended_directions", []) if str(d).strip()]
        if directions:
            assert EV_HINT in BACKGROUND_APPENDABLE             # see the method-level note
            self.store.append(EV_HINT, {
                "text": "deep-research directions: " + "; ".join(directions[:5]),
                "source": "deep_research"})
            # P1: also register each direction as an OPEN hypothesis so a deep-research idea is
            # tracked to a verdict (was fire-and-forget) — it accrues evidence when a matching node
            # runs, and shows on the board as an open question the search should resolve.
            if self._track_hypotheses:
                assert EV_HYPOTHESIS_ADDED in BACKGROUND_APPENDABLE   # see the method-level note
                for direction in directions[:5]:
                    self.store.append(EV_HYPOTHESIS_ADDED, {
                        "statement": str(direction).strip(), "source": "deep_research",
                        "at_node": memo.at_node})

    @staticmethod
    def _card_enrichment_subject(state: RunState, node_id: int):
        """Resolve one live node to its exact canonical native Card + proposal fence."""
        if type(node_id) is not int or node_id < 0:
            return None
        node = state.nodes.get(node_id)
        if (node is None or node.tombstoned or node_id in state.aborted_nodes
                or node.idea is None or not isinstance(node.idea.card_id, str)):
            return None
        raw_card_id = node.idea.card_id
        card_id = raw_card_id if raw_card_id in state.cards else None
        if card_id is None:
            matches = [cid for cid, card in state.cards.items()
                       if raw_card_id in (card.aliases or [])]
            if len(matches) == 1:
                card_id = matches[0]
        proposal_ref = idea_proposal_ref(node.idea)
        if card_id is None or proposal_ref is None:
            return None
        return card_id, node, proposal_ref

    def _sync_card_enrichments(self, state: RunState) -> RunState:
        """Write ref-only Card links for memos, research claims and distilled lessons.

        Research may finish on a background worker, but every Layer-1 Card event is deliberately a
        main-task write.  The run cadence calls this collector after folding all producers; it emits one
        exact card/node/generation/proposal-fenced snapshot per changed Card and then re-folds once.
        """
        from looplab.core.advisory_payloads import valid_advisory_ref
        from looplab.events.types import EV_CARD_ENRICHED

        desired_lessons: dict[str, set[str]] = {}
        desired_claims: dict[str, set[str]] = {}
        desired_origins: dict[str, str] = {}
        desired_footprints: dict[str, tuple] = {}
        subjects: dict[str, tuple] = {}

        # Establish a deterministic live subject per native Card.  It owns the enrichment envelope;
        # replay independently verifies this exact lifecycle and proposal digest before applying it.
        for node_id in sorted(state.nodes):
            subject = self._card_enrichment_subject(state, node_id)
            if subject is None:
                continue
            card_id, node, proposal_ref = subject
            subjects.setdefault(card_id, (node, proposal_ref))
            if node.footprint_finalized:
                footprint = normalize_researcher_footprint(node.idea.footprint)
                if footprint is not None:
                    # Sorted traversal deliberately makes the newest materialized node the Card's
                    # Developer-finalization receipt.  Operator pins are overlaid later by replay.
                    desired_footprints[card_id] = (
                        node, proposal_ref,
                        {**footprint, "proposed_by": "researcher", "finalized_by": "developer"})
            origin = node.research_origin if isinstance(node.research_origin, dict) else {}
            memo_id = origin.get("memo_id")
            if valid_advisory_ref(memo_id, "memo"):
                desired_origins.setdefault(card_id, memo_id)

        # Lesson audit rows now carry an opaque id plus the exact cited node generations.  Legacy rows
        # without both remain visible on their timeline but cannot be guessed onto a Card.
        for batch in list(state.lessons_distilled or [])[-512:]:
            if not isinstance(batch, dict):
                continue
            raw_lessons = batch.get("lessons")
            if not isinstance(raw_lessons, (list, tuple)):
                continue
            for lesson in raw_lessons[:64]:
                if not isinstance(lesson, dict):
                    continue
                lesson_id = lesson.get("lesson_id")
                refs = lesson.get("evidence_refs")
                if (not valid_advisory_ref(lesson_id, "lesson")
                        or not isinstance(refs, (list, tuple)) or len(refs) > 64):
                    continue
                for ref in refs:
                    if (not isinstance(ref, dict) or set(ref) != {"node_id", "generation"}
                            or type(ref.get("node_id")) is not int
                            or type(ref.get("generation")) is not int):
                        continue
                    subject = self._card_enrichment_subject(state, ref["node_id"])
                    if subject is None or subject[1].attempt != ref["generation"]:
                        continue
                    desired_lessons.setdefault(subject[0], set()).add(lesson_id)

        # Claims are aligned positionally with verifier verdicts.  Only exact verifier node_refs carry
        # a generation, so a legacy/bare numeric citation is intentionally not promoted onto a Card.
        for memo in list(state.research or [])[-256:]:
            if not isinstance(memo, dict):
                continue
            memo_id = memo.get("memo_id")
            if not valid_advisory_ref(memo_id, "memo"):
                continue
            claims = memo.get("claims")
            verification = memo.get("verification")
            verdicts = verification.get("verdicts") if isinstance(verification, dict) else None
            if not isinstance(claims, (list, tuple)) or not isinstance(verdicts, (list, tuple)):
                continue
            for index, claim in enumerate(claims[:64]):
                if not isinstance(claim, dict) or index >= len(verdicts):
                    continue
                claim_id = claim.get("claim_id")
                verdict = verdicts[index]
                if (not valid_advisory_ref(claim_id, "claim") or not isinstance(verdict, dict)
                        or verdict.get("statement") != claim.get("statement")):
                    continue
                evidence = verdict.get("evidence")
                refs = evidence.get("node_refs") if isinstance(evidence, dict) else None
                if not isinstance(refs, (list, tuple)) or len(refs) > 64:
                    continue
                for ref in refs:
                    if (not isinstance(ref, dict) or set(ref) != {"node_id", "generation"}
                            or type(ref.get("node_id")) is not int
                            or type(ref.get("generation")) is not int):
                        continue
                    subject = self._card_enrichment_subject(state, ref["node_id"])
                    if subject is None or subject[1].attempt != ref["generation"]:
                        continue
                    desired_claims.setdefault(subject[0], set()).add(claim_id)

        def _footprint_receipt_exists(card_id, node, proposal_ref, target) -> bool:
            """Compare against the durable enrichment rows, before operator-wins replay overlays."""
            for row in reversed(list(state.cards_enriched or [])):
                if (not isinstance(row, dict) or row.get("id") != card_id
                        or row.get("node_id") != node.id
                        or row.get("generation") != node.attempt
                        or row.get("proposal_ref") != proposal_ref
                        or not isinstance(row.get("footprint"), dict)):
                    continue
                raw = row["footprint"]
                quantitative = normalize_researcher_footprint(raw)
                persisted = ({**quantitative,
                              **({"proposed_by": "researcher"}
                                 if raw.get("proposed_by") == "researcher" else {}),
                              **({"finalized_by": "developer"}
                                 if raw.get("finalized_by") == "developer" else {})}
                             if quantitative is not None else None)
                return persisted == target
            return False

        appended = False
        wanted_cards = (set(desired_lessons) | set(desired_claims) | set(desired_origins)
                        | set(desired_footprints))
        for card_id in sorted(wanted_cards):
            footprint_subject = desired_footprints.get(card_id)
            subject = ((footprint_subject[0], footprint_subject[1])
                       if footprint_subject is not None else subjects.get(card_id))
            card = state.cards.get(card_id)
            if subject is None or card is None:
                continue
            lesson_refs = sorted(desired_lessons.get(card_id, ()))[:64]
            claim_refs = sorted(desired_claims.get(card_id, ()))[:64]
            origin = desired_origins.get(card_id)
            delta = {}
            if lesson_refs and lesson_refs != list(card.lesson_refs or []):
                delta["lesson_refs"] = lesson_refs
            if claim_refs and claim_refs != list(card.claim_refs or []):
                delta["claim_refs"] = claim_refs
            if origin is not None and origin != card.research_origin:
                delta["research_origin"] = origin
            if footprint_subject is not None:
                fp_node, fp_proposal_ref, footprint = footprint_subject
                if not _footprint_receipt_exists(
                        card_id, fp_node, fp_proposal_ref, footprint):
                    delta["footprint"] = footprint
            if not delta:
                continue
            node, proposal_ref = subject
            self.store.append(EV_CARD_ENRICHED, {
                "id": card_id,
                "node_id": node.id,
                "generation": node.attempt,
                "proposal_ref": proposal_ref,
                **delta,
            })
            appended = True
        return fold(self.store.read_all()) if appended else state

    def _due_research_trigger(self, state: RunState) -> str | None:
        """Is an AUTO deep-research trigger (cadence/strategist) due at the current node-count? Used by
        the concurrent-research seam to overlap the "think" with an in-flight eval. Mirrors the auto
        triggers in _maybe_deep_research but WITHOUT the no-pending gate (we overlap with pending evals
        on purpose). Manual requests stay on the serial path; the at_node gate (a memo recorded at this
        node-count) keeps the serial path from re-firing after the concurrent memo lands."""
        if self.deep_researcher is None:
            return None
        n = len(state.nodes)
        if n == 0 or self._already_researched_at(state, n):
            return None
        _last_research_n = max((int(m.get("at_node", -1)) for m in self._cadence_research_memos(state)
                                if m.get("at_node") is not None), default=0)
        if self._cadence_due(n, _last_research_n, self.deep_research_every):   # since-last, gap-safe
            return "cadence"
        hist = state.strategy_history
        if (hist and hist[-1].get("at_node") == n
                and (hist[-1].get("strategy") or {}).get("request_research")):
            return "strategist"
        return None

    @in_llm_lane("novelty_dedup")
    def _maybe_merge_hypotheses(self, state: RunState) -> RunState:
        """Agentic consolidation of the OPEN research-card belief board (P1+; 1 card = 1 hypothesis).
        The fold merges cards only by EXACT statement hash, so paraphrases of one belief pile up as
        separate open cards. Here —
        LIVE only, gated on `track_hypotheses` + a reflect client — hybrid retrieval clusters near-dups
        and the agent decides the true merges, appended as `hypothesis_merged` events that the fold
        applies deterministically (alias evidence -> canonical). Best-effort: never raises, never
        blocks the loop. Cadence: only when the open board has grown to >=4 and by >=2 since the last
        pass, so it doesn't re-run every node or thrash. Replay-safe — the engine only WRITES the
        decision here; on replay the fold reapplies the recorded merges with no model call, and a
        re-run finds already-merged aliases gone (converges).

        Phase 2: ALSO invoked from the concurrent eval-window background loop
        (`orchestrator._research_overlap_loop`, gated on `concurrent_consolidate`) so the board the
        repeated research keeps filling is deduped DURING a long eval, not only between nodes. That is
        safe only while Card-driven selection is disabled: `EV_HYPOTHESIS_MERGED` is in the explicit
        non-Card conditional background registry. Card mode invokes this method only from the joined
        main-task cadence, where ownership/readiness changes are serialized before selection. The
        background loop is cancelled before that serial pass, so the two never race on
        `_last_hyp_merge_n`."""
        if not self._track_hypotheses:
            return state
        client = self._reflect_client()
        if client is None:
            return state
        # 1 card = 1 hypothesis: consolidate the open BELIEF board — the Card equivalent of the old open
        # hypotheses (verdict 'open' == the old status 'open'). Exclude selection-ready native work-item
        # cards: this cadence merges near-duplicate research BELIEFS, and collapsing a receipt-backed
        # work item's action identity is not its job. Merges emit `hypothesis_merged` with card ids
        # (== the old hypothesis ids for a belief card), which `_derive_cards` applies unchanged.
        # CODEX AGENT: readiness is transient, not identity. A native Card becomes selection_ready=False
        # when it is stale, incomplete, in flight, or terminal, so this admits receipt-backed work items
        # to belief consolidation and can merge distinct action identities. Exclude native Cards by
        # identity regardless of their current blockers.
        open_hyps = [c for c in state.open_research_cards() if not c.selection_ready]
        n = len(open_hyps)
        if n < 4 or (n - getattr(self, "_last_hyp_merge_n", -1)) < 2:
            return state
        self._last_hyp_merge_n = n
        try:
            from looplab.search.hybrid_merge import consolidate
            texts = [h.statement for h in open_hyps]
            wrote = False
            # Own trace so each hypothesis_merged event (appended INSIDE) is stamped with THIS merge's
            # trace_id — the UI can then show only the merge's own retrieval+decision trace under it.
            with self._op_span("hypothesis_merge"):   # no node_id — see strategist_consult (avoids leaking into a node's trace)
                # merge_system.md override + configured structured-output parser live on the ROLES
                # (tasks.py wires them), not the engine — resolve both via the lessons helper that
                # already walks the researcher→inner→fallback→developer chain (one lookup path,
                # not a shallow re-derivation that misses wrapped roles). getattr guard: some
                # tests build Engine via __new__ (no `lessons`); (None, "tool_call") are exactly
                # the defaults `agent_merge` assumes when nothing is wired.
                _lm = getattr(self, "lessons", None)
                _prompts, _parser = (_lm._merge_prompt_opts() if _lm is not None
                                     else (None, "tool_call"))
                for g in consolidate(texts, client, kind="research hypotheses",
                                     embed=self._embedder, goal=state.goal,
                                     prompts=_prompts, parser=_parser):
                    if len(g["members"]) < 2:
                        continue
                    ids = [open_hyps[i].id for i in g["members"]]
                    assert EV_HYPOTHESIS_MERGED in NON_CARD_SELECTION_BACKGROUND_APPENDABLE
                    self.store.append(EV_HYPOTHESIS_MERGED, {
                        "canonical": ids[0], "aliases": ids[1:], "statement": g["merged"],
                        "at_node": len(state.nodes)})
                    wrote = True
        except Exception:  # noqa: BLE001 — advisory hygiene; a merge hiccup must not disturb the loop
            return state
        return fold(self.store.read_all()) if wrote else state

    def _maybe_refresh_report(self, state: RunState) -> RunState:
        """Regenerate the agent-authored run report on a node-count cadence, then re-fold. No-op when
        the writer is off, when there's nothing evaluated yet, or when the report is already current
        for this node-count (the `at_node` gate makes resume a no-op). Best-effort sidecar."""
        if self.report_writer is None or self.report_every <= 0:
            return state
        if state.pending_nodes() or not state.evaluated_nodes():
            return state
        n = len(state.nodes)
        last = int((state.report or {}).get("at_node") or 0)
        if not self._cadence_due(n, last, self.report_every):   # resume-safe since-last gate
            return state
        return self._write_report(state, trigger="cadence")

    def _write_report(self, state: RunState, *, trigger: str,
                      finalize_scope: str | None = None) -> RunState:
        """Generate one run report and record it as a `report_generated` event, then re-fold. Never
        raises — the writer itself degrades to a minimal report on any failure."""
        return self._write_report_with_seq(
            state, trigger=trigger, finalize_scope=finalize_scope)[0]

    @in_llm_lane("enrichment")
    def _write_report_with_seq(self, state: RunState, *, trigger: str,
                               finalize_scope: str | None = None) -> tuple[RunState, int | None]:
        """Write a report and return its event sequence for the natural-finish CAS."""
        if self.report_writer is None:
            return state, None
        with self.tracer.span("report", new_trace=True, trigger=trigger):
            content = self.report_writer.generate(state, trigger=trigger)
            # append INSIDE the span so report_generated is stamped with the report op-trace (UI scopes it).
            payload = {
                "content": content, "at_node": content.get("at_node"), "trigger": trigger,
            }
            if finalize_scope is not None:
                payload["finalize_scope"] = finalize_scope
            event = self.store.append(EV_REPORT_GENERATED, payload)
        return fold(self.store.read_all()), event.seq
