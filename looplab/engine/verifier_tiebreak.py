"""R1-c: the calibrated §12-verifier metric tie-break — split out of `engine/strategy.py` as its own
MIXIN (doc 25 EC-09): `class Engine(…, VerifierTiebreakMixin)` inherits these methods unchanged, so
there is ZERO call-site churn and `self` here IS the engine, exactly as in every other engine mixin.

This is SELECTION machinery, not strategist cadence. It shared a file with the Strategist consult
only because both run from `_run_cadences`; what it actually reads and writes belongs to the
champion decision — `events/replay.py::verifier_tie_groups` (the pure contract the producer and the
replay validator share), `core/fitness.py::VERIFIER_SELECTION_CONTRACT`, and one atomic
`verifier_group_scored` record the fold consumes ONLY as a tie-break. Filing it under "strategist
cadence" hid that a change here can move the run's reported champion.

Layering: no runtime import of the orchestrator and never serve — only core, events, trust and
stdlib (the trust verifier / role parser stay lazy, method-local, as they were)."""
from __future__ import annotations

from typing import Optional

from looplab.core.fitness import (VERIFIER_SELECTION_CONTRACT, verifier_evidence_digest,
                                  verifier_evidence_snapshot)
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import RunState
from looplab.events.replay import fold
from looplab.events.types import EV_VERIFIER_GROUP_SCORED


class VerifierTiebreakMixin:
    """The engine's calibrated-verifier tie-break cluster. See the module docstring for the mixin
    convention (`self` is the Engine)."""

    # --- R1-c: calibrated-verifier metric-tie-break -------------------------------------------------
    @in_llm_lane("enrichment")
    def _maybe_verify_ties(self, state: RunState) -> RunState:
        """R1-c: the calibrated §12-verifier metric-tie-break (opt-in). Find the complete selector-reachable
        exact/CI tie that is not yet resolvable and re-score every member against one evidence revision
        (`selection_criteria`) so the fold can break it by soundness. Lazy, bounded per cadence, and
        best-effort (no client / any failure -> skip). Emits one atomic
        `verifier_group_scored` record; the fold reads it ONLY as a tie-break — it can never override
        a strictly-better metric (§21.7). No-op when `select_verifier` is off. Runs in the sync cadence
        (like the Strategist consult), so a blocking LLM call here matches the established pattern."""
        if not state.select_verifier_tiebreak:
            return state
        # The producer and replay validator must agree on the selection contract before any paid
        # verification work starts.  A future/unknown recorded contract is intentionally fail-closed:
        # this process cannot safely emit a v1 treatment for selection rules it does not understand.
        if state.select_verifier_contract != VERIFIER_SELECTION_CONTRACT:
            return state
        groups = self._metric_tie_groups(state)
        if not groups:
            return state
        try:
            client = self._reflect_client()
        except Exception:  # noqa: BLE001
            client = None
        if client is None:
            return state
        # Process-local FAILURE guard: record a (node, generation, evidence revision) whose verify returned
        # None so a degraded client can't re-verify the same tie every cadence (a success sets
        # verifier_score, which _metric_tie_groups already excludes). In-memory only (verify is live, never
        # replayed); a fresh
        # process on resume may retry, which is fine (bounded).
        attempted = getattr(self, "_verify_attempted", None)
        if attempted is None:
            attempted = self._verify_attempted = set()
        budget = 8                       # per-cadence NODE cap so a big tie cluster can't burst cost
        done = False
        for group in groups:
            # ATOMIC per group: score EVERY unscored member of a tie or NONE of it. A half-scored group
            # would leave an unscored sibling at the neutral 0.5 midpoint, which could outrank a
            # verified-but-low member — deciding the tie by verify TIMING/BUDGET rather than soundness. So
            # a group with a prior FAILED member (can never be fully scored) is skipped entirely (its tie
            # falls back to the id tie-break), and a group larger than the cadence budget is left for a
            # later cadence (a group larger than the cap is never verified — honest + bounded).
            attempted_keys = {
                n.id: (n.id, n.attempt, verifier_evidence_digest(state.direction, n)) for n in group}
            if any(attempted_keys[n.id] in attempted for n in group):
                continue
            # Re-score the complete current tie. Carrying an older member score into a newly expanded group
            # would mix treatments and let one node influence two incompatible evidence snapshots.
            todo = list(group)
            if len(todo) > budget:
                continue
            verdicts, failed = [], False
            for n in todo:
                v = self._verifier_soundness(state, n, client)
                budget -= 1
                if v is None:
                    attempted.add(attempted_keys[n.id])  # failure abstains this evidence revision hereafter
                    failed = True
                    break
                verdicts.append((n, v))
            if not failed:
                # Publish the complete selector-reachable tie group in one durable event;
                # per-node appends expose crash prefixes that can change the winner during replay.
                self.store.append(EV_VERIFIER_GROUP_SCORED, {
                    "v": 1, "contract": VERIFIER_SELECTION_CONTRACT,
                    "requested_samples": state.select_verifier_samples,
                    "members": [{
                        "node_id": n.id, "generation": n.attempt,
                        "score": round(v["score"], 4), "n_samples": v["n_samples"],
                        "agreement": v["agreement"], "method": v["method"],
                        "evidence_digest": verifier_evidence_digest(state.direction, n),
                    } for n, v in verdicts],
                })
                done = True
            if budget <= 0:
                break
        return fold(self.store.read_all()) if done else state

    def _metric_tie_groups(self, state: RunState) -> list:
        """The sole complete tie-set that can affect `_select_best`'s final champion.

        The replay helper owns pool/holdout/CI precedence as one pure contract shared by the event producer
        and validator. Recorded run state is authoritative here: live engine fields may not silently change
        selection semantics after resume or a config edit.
        """
        # Use folded run flags and the validator's helper; live engine config must not produce
        # a treatment that replay rejects or select a tie shadowed by the final holdout selector.
        from looplab.events.replay import verifier_tie_groups
        return verifier_tie_groups(state)

    def _verifier_soundness(self, state: RunState, node, client) -> Optional[dict]:
        """The calibrated §12-verifier soundness verdict for a node's REALIZED result, or None on any
        failure / too-noisy a verdict. Returns `{score, n_samples, agreement}` — `score` is the
        `result_sound` criterion mean in [0,1] (grounded on the node's idea + metric + confirm/holdout
        signals); the provenance rides on the audit event. Best-effort — never raises.

        ABSTAINS (None) when cross-sample AGREEMENT is not a strict majority (only measurable with >1 sample):
        a high-variance verdict — the single-shot noise §21.12 measured — must not decide a tie. Evidence
        is scalar-summary only (the hard leakage/gaming/overfit signals stay the job of the trust layer's
        reward-hack / leakage detectors); this advisory tie-break asks only "does the reported result look
        sound", and abstains rather than over-claiming when the judgment is unstable."""
        try:
            from looplab.trust.verifier import selection_criteria, verify
            from looplab.agents.roles import resolve_role_parser

            parser = resolve_role_parser(getattr(self, "researcher", None),
                                         getattr(self, "developer", None))
            snapshot = verifier_evidence_snapshot(state.direction, node)
            subject = (f"Experiment #{node.id} reported metric={snapshot['metric']} on the task (optimize "
                       f"direction: {state.direction}); its result is genuinely sound and will hold up.")
            evidence = (f"What it did: {snapshot['rationale']}\n"
                        f"Metric: {snapshot['metric']}"
                        + (f"; confirmed mean over {snapshot['confirmed_seeds']} seeds: "
                           f"{snapshot['confirmed_mean']}" if snapshot['confirmed_mean'] is not None else "")
                        + (f"; holdout metric: {snapshot['holdout_metric']}"
                           if snapshot['holdout_metric'] is not None else "")
                        + (f"; generalization gap: {snapshot['generalization_gap']}"
                           if snapshot['generalization_gap'] is not None else ""))
            samples = state.select_verifier_samples
            rep = verify(subject, evidence, selection_criteria(), client=client,
                         samples=samples, parser=parser)
            if rep is None or rep.method == "unavailable":
                return None
            crit = (rep.per_criterion or {}).get("result_sound") or {}
            m = crit.get("mean")
            score = float(m) if m is not None else (float(rep.score) if rep.score is not None else None)
            if score is None or score != score or not 0.0 <= score <= 1.0:
                return None
            # Repeated verification needs a strict majority of the REQUESTED samples to
            # survive parsing as well as a strict modal majority. One lucky parsed answer out of three is
            # not a repeated verdict and must not become selection-affecting evidence.
            if (rep.n_samples > samples or rep.n_samples * 2 <= samples
                    or (samples > 1 and rep.agreement <= 0.5)):
                return None
            return {"score": score, "n_samples": rep.n_samples, "agreement": rep.agreement,
                    "method": str(rep.method or "")[:80]}
        except Exception:  # noqa: BLE001 — advisory tie-break: any failure just skips (id tie-break stands)
            return None
