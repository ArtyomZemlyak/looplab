"""M6 comparative lessons + memory reconciliation for the lessons cluster — extracted from
engine/lessons.py as a MIXIN (the Engine's own convention, see engine/novelty.py):
`class LessonMemory(…, LessonReconcileMixin)` inherits these methods unchanged, so there is ZERO
call-site churn and `self` here IS the LessonMemory — the bodies are verbatim moves, reading the
engine through `self._e` and sibling cluster methods through the Engine's thin delegators.

Two tightly-coupled concerns: the M6 credit-assigned PAIR lessons (`comparative_lessons` + the
`spent_pairs` ledger the mid-run cadence and run-end reflection exclude against) and the
node-re-eval → memory reconciliation seam (`reconcile_lessons` + the evidence-signature
helpers) that retires and re-derives any lesson whose grounding node's outcome later flipped.

Layering: like lessons.py, no runtime import of the orchestrator and never serve — only
engine.memory, events, core and stdlib (the memory/agent deps stay lazy, method-local
imports)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from looplab.core.models import RunState
from looplab.engine.lessons_priors import LESSON_ROLE_DEVELOPER, LESSON_ROLE_RESEARCHER
from looplab.events.eventstore import read_jsonl_lenient, write_jsonl_atomic
from looplab.events.replay import fold
from looplab.events.types import EV_LESSONS_DISTILLED, EV_LESSONS_RECONCILED


class LessonReconcileMixin:
    """The lessons cluster's comparative/reconcile half. See the module docstring for the mixin
    convention (`self` is the LessonMemory)."""

    def comparative_lessons(self, state: RunState, fp: list, exclude=()) -> tuple[list, list]:
        """M6 (MARS comparative reflective memory): credit-assigned lessons from solution PAIRS —
        which SPECIFIC difference made the child beat (or regress from) its parent, and what fixed
        a failure. One LLM call for ALL pairs (budget: same order as `_reflect_lessons`); offline,
        the deterministic param-diff credit stands in. Returns (lessons, pairs_used); ([], []) when
        there is nothing informative to compare. Best-effort — never raises."""
        from looplab.engine.memory import (code_diff, distilled_claim_stance,
                                           param_credit_statement, parse_credit_lessons,
                                           select_comparison_pairs)
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
                 "claim_stance": distilled_claim_stance(outcome),
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
                  "The lesson sentence itself must be a conclusion supported by the pair evidence; "
                  "[GOOD]/[BAD] controls reuse guidance, not whether the sentence is true. No preamble.")
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
            # keep_bad=True: stale_idx below is keyed by RAW line number, so placeholders must
            # hold the slot of every bad line for the index-keyed rewrite to stay aligned.
            rows: list = read_jsonl_lenient(path, keep_bad=True)
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
            # just the one that drifted. BUT only when re-derivation actually produced lessons: an
            # empty/failed LLM re-derivation must NOT nuke existing memory (then drop only the drifted).
            drop_all_reflect = reflect_stale and bool(fresh_reflect)
            _stale_pairs = {tuple(p) for p in stale_pairs}
            comp: list = []
            pairs_used: list = []
            if stale_pairs and self._e._comparative_lessons_on:
                # Un-spend the drifted pairs so `select_comparison_pairs` can re-pick them, then re-derive.
                exclude = [p for p in self._e._spent_pairs(state) if tuple(p) not in _stale_pairs]
                comp, pairs_used = self._e._comparative_lessons(state, fp, exclude=exclude)
            fresh = fresh_reflect + comp

            def _is_stale(o) -> bool:
                # Identify a stale row of THIS run by IDENTITY (evidence pair / staleness), NOT raw line
                # index — so the drop survives a RE-READ inside the lock. A concurrent run's O_APPEND
                # during the LLM window shifts line numbers, and it only ever appends OTHER runs' lessons
                # (run_id != ours), which _is_stale never matches, so they are preserved.
                if not isinstance(o, dict) or o.get("run_id") != state.run_id:
                    return False
                # Gate the comparative branch on SOURCE ALONE — matching the old expansion's
                # `source != "comparative"` exclusion. An UNATTRIBUTED comparative row (no valid
                # P-marker -> evidence != 2) is NOT pair-keyed, so it must be immune to the reflect
                # `drop_all_reflect` sweep (which only ever replaced non-comparative reflect rows);
                # falling through to the reflect branch would silently drop a valid non-stale lesson.
                if o.get("source") == "comparative":
                    ev = o.get("evidence") or []
                    return len(ev) == 2 and tuple(ev) in _stale_pairs
                return drop_all_reflect or self._lesson_evidence_stale(state, o)

            # Rewrite UNDER the lock, RE-READING the file INSIDE it: read + drop-stale + append-fresh must
            # be atomic vs a concurrent run's O_APPEND, or the whole-file replace clobbers a lesson that
            # landed in the read→write window (the pre-lock snapshot never saw it) — the exact race the
            # lock exists to prevent (JsonlCaseLibrary.add / append_lessons both re-read inside the lock).
            from looplab.events.eventstore import _interprocess_lock
            with _interprocess_lock(Path(str(path) + ".lock")):
                try:
                    cur = read_jsonl_lenient(path)   # authoritative current file, inside the lock
                except OSError:
                    cur = [o for o in rows if isinstance(o, dict)]
                kept = [o for o in cur if isinstance(o, dict) and not _is_stale(o)]
                n_retired = len(cur) - len(kept)   # rows ACTUALLY dropped (audit); reflect-sweep included
                write_jsonl_atomic(path, kept + fresh)
                # The write above COMMITTED the fresh comparative lessons to disk. Consolidate/compact
                # merely merge near-dups + cap the file AFTER that; they call the LLM merge agent /
                # embedder and can raise (transient error). Swallow that LOCALLY so a post-write failure
                # cannot skip the spend-ledger append below: the pairs are already on disk, and without
                # the `lessons_distilled(reconcile)` record run-end reflection re-derives them (a double
                # count). The write itself failing still falls to the outer guard (nothing committed →
                # nothing to spend) — code-review.
                try:
                    prompts, parser = self._merge_prompt_opts()
                    self._e._consolidate_lessons_file(path, client, self._e._embedder,
                                                      parser=parser, prompts=prompts)
                    self._e._compact_lessons(path)
                except Exception:  # noqa: BLE001 — best-effort post-processing; ledger recorded below
                    pass
        except Exception:  # noqa: BLE001 — reconciliation is best-effort; never fail the run for it
            # Nothing was actually reconciled (a transient LLM/network/write error), so RESET the
            # change-gate hash — mirroring the client-None branch above — else the next same-signature
            # cadence pass returns early at the gate and never retries. Forward progress (a new terminal)
            # flips the hash anyway; this only matters when the failure is the run's last activity.
            self._reconcile_sig_hash = None
            return state
        # Ledger consistency: the re-derived pairs are (re-)spent so run-end reflection won't double
        # them — recorded as a lessons_distilled(reconcile) exactly like the mid-run cadence.
        if pairs_used:
            self._e.store.append(EV_LESSONS_DISTILLED, {
                "at_node": len(state.nodes), "trigger": "reconcile", "count": len(comp),
                "pairs": [[pr["a"], pr["b"]] for pr in pairs_used],
                "lessons": [{"statement": lz["statement"], "outcome": lz["outcome"],
                             "claim_stance": lz.get("claim_stance"),
                             "evidence": lz.get("evidence")} for lz in comp]})
        # Audit sidecar (fold ignores it): what drifted and what replaced it.
        self._e.store.append(EV_LESSONS_RECONCILED, {
            "at_node": len(state.nodes), "n_retired": n_retired, "n_added": len(fresh),
            "reflect": reflect_stale, "pairs": [list(p) for p in stale_pairs],
            "lessons": [{"statement": lz.get("statement", ""),
                         "outcome": lz.get("outcome", ""),
                         "claim_stance": lz.get("claim_stance")}
                        for lz in fresh[:12] if isinstance(lz, dict)]})
        return fold(self._e.store.read_all())
