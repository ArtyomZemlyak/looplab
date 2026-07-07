"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. `best` is recomputed deterministically from
evaluated nodes (tie-break by id), so no separate `best_updated` event is needed.
"""
from __future__ import annotations

from typing import Iterable

from looplab.core.models import (Event, Hypothesis, Idea, Node, NodeStatus, RunState, Trial,
                     hypothesis_id)
from looplab.events.types import (
    EV_ABLATE, EV_AGENT_DECISION, EV_AGENT_VALIDATED, EV_ANNOTATION, EV_APPROVAL_GRANTED,
    EV_APPROVAL_REQUESTED, EV_BEST_CONFIRMED, EV_BUDGET_EXTEND, EV_CONFIRM_DONE,
    EV_CONFIRM_EVAL, EV_DATA_LEAKAGE, EV_DATA_PROFILED, EV_DATA_PROVENANCE,
    EV_COVERAGE_SNAPSHOT, EV_DEEP_RESEARCH, EV_DIVERSITY_ARCHIVE, EV_FORCE_ABLATE, EV_FORCE_CONFIRM, EV_FORK,
    EV_FORK_DONE, EV_HINT, EV_HOLDOUT_EVALUATED, EV_HOST_GRADING, EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED,
    EV_HYPOTHESIS_RANKED, EV_HYPOTHESIS_UPDATED, EV_INJECT_DONE, EV_INJECT_NODE, EV_LESSONS_DISTILLED,
    EV_LESSONS_REFRESHED, EV_LLM_COST, EV_NODE_ABORT, EV_NODE_BUILDING, EV_NODE_CONFIRMED,
    EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED, EV_NODE_RESET,
    EV_NOVELTY_REJECTED, EV_PAUSE,
    EV_POLICY_DECISION, EV_PROMOTE, EV_PROXY_SCORED, EV_REPORT_GENERATED,
    EV_RESEARCH_COMPLETED, EV_RESUME, EV_REWARD_HACK_SUSPECTED, EV_RUN_ABORT,
    EV_RUN_FINISHED, EV_RUN_REOPENED, EV_RUN_STARTED, EV_RUNG_PROMOTED, EV_SET_STRATEGY,
    EV_SPEC_APPROVAL_REQUESTED, EV_SPEC_APPROVED, EV_SPEC_DRIFT, EV_SPEC_PROPOSED,
    EV_STRATEGY_DECISION, EV_TRUST_GATE_CHANGED, EV_WORKSPACE_CHANGED)


def flagged_node_ids(st: RunState) -> set:
    """T2: node ids excluded from best/holdout selection under trust_gate gate/block — those with a
    HIGH-PRECISION cheating/leakage signal. The heuristic `critic:` and `perfect_metric` signals
    stay advisory in every mode (perfect_metric flags metric<=0 (min) / >=1 (max), which
    legitimately-perfect scores hit, so gating on it could exclude honest winners). Empty under
    `audit`. Shared by the fold and the engine's holdout-topk so both apply the SAME exclusion."""
    if st.trust_gate not in ("gate", "block"):
        return set()

    def _has_hard_signal(rh: dict) -> bool:
        return any(not str(s.get("signal", "")).startswith(("critic:", "perfect_metric"))
                   for s in (rh.get("signals") or []))
    return {r.get("node_id") for r in st.reward_hacks if _has_hard_signal(r)}


def fold(events: Iterable[Event]) -> RunState:
    st = RunState()
    best_confirmed: int | None = None
    for e in events:
        d = e.data
        t = e.type
        if t == EV_RUN_STARTED:
            st.run_id = d["run_id"]
            st.task_id = d["task_id"]
            st.goal = d.get("goal", "")
            # `direction` drives is_better/best-selection for the whole run — a typo ("Max",
            # "maximize") must not silently invert the objective. Accept only the two valid values;
            # anything else falls back to the safe default rather than flipping optimization.
            _dir = str(d.get("direction", "min")).strip().lower()
            st.direction = _dir if _dir in ("min", "max") else "min"
            st.config_hash = d.get("config_hash", "")
            st.workspace = d.get("workspace")
            _tg = str(d.get("trust_gate", "audit")).strip().lower()
            st.trust_gate = _tg if _tg in ("audit", "gate", "block") else "audit"
            # D1: recorded at start so replay applies the same selection rule. Absent in old
            # logs -> False -> byte-identical legacy selection.
            st.holdout_select = bool(d.get("holdout_select", False))
            # The reserved-holdout fraction the run committed to (the split every search metric was
            # scored against). None in old logs; the engine re-uses it on resume so a changed live
            # setting can't make pre/post-resume metrics incomparable.
            _hf = d.get("holdout_fraction")
            st.holdout_fraction = float(_hf) if isinstance(_hf, (int, float)) else None
        elif t == EV_TRUST_GATE_CHANGED:
            # Operator edited the run's trust gate after launch (server config edit). Last write
            # wins so the change engages in every fold — live view, resume, reset — immediately.
            _tg = str(d.get("trust_gate", "")).strip().lower()
            if _tg in ("audit", "gate", "block"):
                st.trust_gate = _tg
        elif t == EV_NODE_BUILDING:
            # Transient "a node is being built RIGHT NOW" marker (see EV_NODE_BUILDING docs): show it in
            # the UI the instant work starts, before node_created. NOT added to st.nodes, so id
            # allocation + resume are untouched. Superseded/cleared by this node's node_created below.
            st.building = {"node_id": d.get("node_id"), "operator": d.get("operator"),
                           "parent_ids": d.get("parent_ids", []), "started": e.ts}
        elif t == EV_NODE_CREATED:
            # Defensive like the per-trial / unknown-node tolerance below: a malformed or incomplete
            # node_created (missing key, non-coercible idea param in a hand-edited / bring-your-own-script
            # log) must not crash the WHOLE fold — skip the bad event instead (the engine, sole writer,
            # always round-trips a validated Idea, so this only fires on a corrupt log).
            try:
                n = Node(
                    id=d["node_id"],
                    parent_ids=d.get("parent_ids", []),
                    operator=d["operator"],
                    idea=Idea(**d["idea"]),
                    code=d.get("code", ""),
                    files=d.get("files", {}) or {},
                    deleted=d.get("deleted", []) or [],
                    origin=d.get("origin"),   # cross-run provenance (None for ordinary nodes)
                    research_origin=d.get("research_origin"),   # 💡 proposed just after a deep-research memo
                )
            except (MemoryError, RecursionError):
                # A RESOURCE glitch is NOT a corrupt-data error: it must fail LOUD, not be swallowed.
                # A MemoryError silently caught here drops the node -> fold returns empty nodes ->
                # `_create_node` re-computes node_id=0 forever -> a 184MB node_created(0) runaway. Let
                # it propagate so a transient glitch surfaces instead of self-sustaining into a spin.
                raise
            except Exception:
                continue
            st.nodes[n.id] = n
            if st.building and st.building.get("node_id") == n.id:
                st.building = None          # the real node is here now — drop the "building" marker
        elif t == EV_NODE_EVALUATED:
            n = st.nodes.get(d.get("node_id"))          # tolerate an event for an unknown/missing node
            if n is not None:                           # (corrupt/hand-edited log) — skip, don't crash
                # Idempotent (C4): only a node's FIRST terminal event contributes its eval time, so
                # a duplicate node_evaluated/node_failed (corrupt log / double-fold) can't inflate
                # total_eval_seconds or make the budget order-dependent.
                first_terminal = n.status is NodeStatus.pending
                n.metric = d.get("metric")              # missing -> None (feasible_nodes filters it)
                n.status = NodeStatus.evaluated
                n.stdout_tail = d.get("stdout_tail", "")
                n.eval_seconds = d.get("eval_seconds")
                n.extra_metrics = d.get("extra_metrics", {}) or {}
                n.violations = d.get("violations", []) or []
                n.feasible = not n.violations           # #5: constraint-violating -> infeasible
                # Intra-node sweep: per-trial results (audit/UI only; node.metric is already the
                # best trial, set by the engine). Coerce defensively per trial so one malformed
                # entry in a hand-edited/bring-your-own-script log can't crash the whole fold.
                trials = []
                for t_d in (d.get("trials", []) or []):
                    try:
                        trials.append(Trial(**t_d))
                    except Exception:
                        continue
                n.trials = trials
                if first_terminal:
                    st.total_eval_seconds += d.get("eval_seconds") or 0.0
        elif t == EV_NODE_FAILED:
            if st.building and st.building.get("node_id") == d.get("node_id"):
                st.building = None
            n = st.nodes.get(d.get("node_id"))
            if n is not None:
                first_terminal = n.status is NodeStatus.pending
                n.status = NodeStatus.failed
                n.error = d.get("error", "")
                n.error_reason = d.get("reason", "")
                n.eval_seconds = d.get("eval_seconds")
                if first_terminal:
                    st.total_eval_seconds += d.get("eval_seconds") or 0.0
        elif t == EV_NODE_REPAIRED:
            # In-node inline repair (hybrid crash repair): a NON-terminal event that replaces the
            # node's code with the LLM-repaired version BEFORE the eval that follows it. Idempotent
            # and replay-safe: only mutates while the node is still pending (the single terminal
            # event emitted at the end of the repair loop flips status off pending), so a duplicate
            # or post-terminal node_repaired (corrupt/double-fold) is a no-op — mirrors the
            # `first_terminal` guard above. The LLM/subprocess are never re-invoked; the final code
            # and metric/status are reconstructed purely from this event + the terminal event.
            n = st.nodes.get(d.get("node_id"))
            if n is not None and n.status is NodeStatus.pending:
                n.code = d.get("code", n.code)
                if d.get("files"):
                    n.files = d["files"]
                if d.get("deleted"):
                    n.deleted = d["deleted"]
        elif t == EV_NODE_RESET:
            # Re-run an EXISTING node in place (no new id). Discard its state FROM `from_stage` so it
            # becomes pending again; the engine then re-runs just that stage, appending fresh events for
            # the SAME id (which land as the first-terminal-after-reset). Replay-safe: the reset marks
            # where the old lifecycle is abandoned. `eval` = keep idea+code, just re-score (the normal
            # eval loop picks a pending-with-code node up — no marker). `implement`/`propose` = also drop
            # the code and flag `rerun_from` so the engine re-develops (re-proposes for `propose`).
            n = st.nodes.get(d.get("node_id"))
            if n is not None:
                stage = d.get("from_stage", "eval")
                n.status = NodeStatus.pending
                n.metric = None
                n.error = ""
                n.error_reason = ""
                n.eval_seconds = None
                n.stdout_tail = ""
                n.extra_metrics = {}
                n.violations = []
                n.feasible = True
                n.trials = []
                n.confirmed_mean = None
                if stage in ("implement", "propose"):
                    n.code = ""
                    n.files = {}
                    n.deleted = []
                    n.rerun_from = stage
                else:
                    n.rerun_from = None          # eval-only: pending-with-code, the eval loop re-scores it
                if st.building and st.building.get("node_id") == n.id:
                    st.building = None
                # A reset means there is work to do again, so it RE-OPENS a finished run — else the
                # loop would see the stale run_finished and exit before re-running/re-scoring the node.
                # (Mirrors EV_RESUME's finished-clear; a later run_finished sets it again. `paused` is
                # left alone — that's the operator's separate resume.)
                st.finished = False
                st.stop_reason = None
                st.stop_requested = None
        elif t == EV_CONFIRM_EVAL:
            st.total_eval_seconds += d.get("eval_seconds") or 0.0   # confirm-seed eval cost
            if "node_id" in d and "seed" in d:                       # per-seed resume memo (#0)
                st.confirm_seed_results.setdefault(d["node_id"], {})[d["seed"]] = d.get("metric")
        elif t == EV_NODE_CONFIRMED:
            n = st.nodes.get(d.get("node_id"))
            if n is not None:
                n.confirmed_mean = d.get("mean")
                n.confirmed_std = d.get("std")
                n.confirmed_seeds = d.get("seeds")
        elif t == EV_HOLDOUT_EVALUATED:
            # D1 holdout-gated promotion: the engine re-scored this val-leader's predictions on
            # the FINAL holdout partition the search never saw. Tolerant like node_evaluated:
            # an event for an unknown node (corrupt log) is skipped, and a null metric (missing
            # predictions) records nothing — such a node simply can't win the holdout pick.
            nid = d.get("node_id")
            if nid is not None and nid not in st.holdout_evaluated_ids:
                st.holdout_evaluated_ids.append(nid)   # gate: attempted, even if metric is null
            n = st.nodes.get(nid)
            if n is not None and d.get("metric") is not None:
                try:
                    n.holdout_metric = float(d["metric"])
                except (TypeError, ValueError):
                    pass
        elif t == EV_AGENT_VALIDATED:
            n = st.nodes.get(d.get("node_id"))
            if n is not None:                       # audit only; never affects selection
                n.agent_report = {
                    "ok": d.get("ok"), "checks": d.get("checks", []),
                    "fell_back": d.get("fell_back"), "attempts": d.get("attempts"),
                    "shipped_ok": d.get("shipped_ok"),
                }
        elif t == EV_DATA_PROFILED:
            st.data_profile = d.get("columns")
        elif t == EV_DATA_PROVENANCE:
            st.data_provenance = d   # D4: pinned dataset/asset content hashes
        elif t == EV_HOST_GRADING:
            st.host_grading = d      # out-of-process host-side grading active (audit; no labels)
        elif t == EV_DATA_LEAKAGE:
            st.leakage = d
        elif t == EV_APPROVAL_REQUESTED:
            st.awaiting_approval = True
        elif t == EV_APPROVAL_GRANTED:
            st.awaiting_approval = False
            st.approved = True
        elif t == EV_SPEC_PROPOSED:
            st.proposed_spec = d
        elif t == EV_SPEC_APPROVAL_REQUESTED:
            st.spec_approval_requested = True
        elif t == EV_SPEC_APPROVED:
            st.spec_confirmed = True
        elif t == EV_SPEC_DRIFT:
            st.drifts.append(d)                         # audit only; metric already discarded
        elif t == EV_WORKSPACE_CHANGED:
            st.workspace_changed = True                 # resume saw the source repo/data change
        elif t == EV_DIVERSITY_ARCHIVE:
            st.archive = d
        elif t == EV_COVERAGE_SNAPSHOT:
            st.coverage_snapshots.append(d)   # audit-only breadth curve; the at_node gate dedups on resume
        elif t == EV_LLM_COST:
            st.llm_cost = d
        elif t == EV_ABLATE:
            st.ablations.append(d)   # {parent_id, impacts} — parameter-sensitivity audit
        elif t == EV_POLICY_DECISION:
            _scores = {}
            for k, v in (d.get("scores") or {}).items():
                try:
                    _scores[int(k)] = v                 # a non-integer key (corrupt log) is skipped
                except (TypeError, ValueError):
                    continue
            st.policy_scores = _scores
            st.policy_chosen = d.get("chosen")
            st.policy_reason = d.get("reason") or ""
        elif t == EV_STRATEGY_DECISION:
            # A7 Strategist (audit-only): the engine recorded the chosen Strategy. Replay rebuilds
            # active_strategy WITHOUT re-calling the LLM (the decision is config, not selection).
            st.active_strategy = d.get("strategy")
            st.strategy_history.append({"strategy": d.get("strategy"), "at_node": d.get("at_node"),
                                        "ctx": d.get("ctx")})
        elif t == EV_HYPOTHESIS_RANKED:
            # FOREAGENT board prioritization (audit-only): the engine recorded how the world model
            # ordered the OPEN hypotheses (order of ids + confidence + analysis trace). Latest-wins
            # (like policy_scores); `_derive_hypotheses` stamps each card's `priority` from `order`.
            st.hypothesis_ranking = d
        elif t == EV_RUNG_PROMOTED:
            st.rungs.append({"rung": d.get("rung"), "survivors": d.get("survivors", [])})
        elif t == EV_AGENT_DECISION:
            # Self-driving unified agent (audit-only): records WHICH legal macro action the agent
            # chose and why. NEVER drives selection — the effect is the subsequent node_created,
            # folded as usual. Additive & non-load-bearing: an old log without it folds identically.
            st.agent_decisions.append(d)
        elif t == EV_REWARD_HACK_SUSPECTED:
            st.reward_hacks.append({"node_id": d.get("node_id"), "signals": d.get("signals", [])})
        elif t == EV_NOVELTY_REJECTED:
            st.novelty_events.append(d)   # E1: a near-duplicate proposal nudged off (audit)
        elif t == EV_HYPOTHESIS_MERGED:
            # P1+: engine-written agentic merge — fold alias hypotheses into a canonical. Collected
            # here, APPLIED deterministically in `_derive_hypotheses` (no LLM in the fold). A malformed
            # entry is tolerated there; unknown on old logs -> skipped by the outer dispatch.
            if d.get("canonical") and d.get("aliases"):
                st.hypotheses_merged.append(d)
        elif t == EV_HYPOTHESIS_ADDED:
            # P1: an explicitly-registered hypothesis (human `add_hypothesis`, or a deep-research
            # direction) — may have no evidence yet. Evidence + verdict are DERIVED post-loop.
            if d.get("statement"):
                st.hypotheses_added.append(d)
                # Re-adding an abandoned statement reopens it (last write wins).
                try:
                    hid = str(d.get("id") or hypothesis_id(str(d["statement"])))
                    if hid in st.hypotheses_abandoned:
                        st.hypotheses_abandoned.remove(hid)
                except Exception:
                    pass
        elif t == EV_HYPOTHESIS_UPDATED:
            # Carries a status override (human/agent drops — or reopens — a line of inquiry).
            # Last write wins: "abandoned" adds the override, any other status clears it.
            hid = d.get("id")
            if hid:
                if d.get("status") == "abandoned":
                    if hid not in st.hypotheses_abandoned:
                        st.hypotheses_abandoned.append(hid)
                elif hid in st.hypotheses_abandoned:
                    st.hypotheses_abandoned.remove(hid)
        elif t == EV_PROXY_SCORED:
            # A6 proxy/predictive scoring (audit-only): early-signal rank + which nodes were skipped.
            nid = d.get("node_id")
            if nid is not None and d.get("score") is not None:
                st.proxy_scores[nid] = d["score"]
            if d.get("skipped") and nid is not None and nid not in st.proxy_skipped:
                st.proxy_skipped.append(nid)
        elif t == EV_BEST_CONFIRMED:
            best_confirmed = d.get("node_id", best_confirmed)
            st.confirmed_done = True   # the confirmation phase ran to completion
        elif t == EV_RUN_FINISHED:
            st.finished = True
            st.stop_reason = d.get("reason")
            # Drop any dangling "building" marker: if a dev session died mid-build (no node_created /
            # node_failed) the marker would otherwise persist, and the UI would show a breathing
            # "building…" card + a false "working" pulse on a run that is over.
            st.building = None
        elif t in (EV_RESUME, EV_RUN_REOPENED):
            # RESUME (the one operator "continue"): lift EVERY stopped state so re-entering the loop
            # keeps going — whether the run was PAUSED (stop, no finalize), ABORTED (finalize →
            # stop_requested → run_finished), or naturally FINISHED (budget exhausted, then reopened
            # with more budget). Clears paused + finished + stop_requested + stop_reason. Deterministic
            # under replay — a later run_finished simply sets `finished` again. EV_RUN_REOPENED is the
            # legacy alias of RESUME (kept so old logs + the UI's reopen path fold identically); the two
            # 3-verb operator controls are `stop` (EV_PAUSE) and `finalize` (EV_RUN_ABORT).
            st.paused = False
            st.finished = False
            st.stop_reason = None
            st.stop_requested = None
        # --- live operator control events (UI intervention). Intent only; the engine reads
        # these and writes the matching domain effect. Deterministic under replay. ---
        elif t == EV_RUN_ABORT:
            # FINALIZE: the loop turns stop_requested into a run_finished (which runs the end-of-run
            # finalization — report/lessons/case/cost). A bare `stop` uses EV_PAUSE instead (no finalize).
            st.stop_requested = d.get("reason", "operator")
        elif t == EV_PAUSE:
            # STOP: freeze WITHOUT finalizing (finalize.py gates the wrap-up on `finished`, which a pause
            # never sets). A later `finalize` (EV_RUN_ABORT) can still wrap it up; RESUME lifts it.
            st.paused = True
        elif t == EV_NODE_ABORT:
            nid = d.get("node_id")
            if nid is not None and nid not in st.aborted_nodes:
                st.aborted_nodes.append(nid)
        elif t == EV_BUDGET_EXTEND:
            # max_seconds / max_eval_seconds are ABSOLUTE new ceilings (last write wins). add_nodes is
            # an ADDITIVE delta — "give the run N more nodes" — so several extensions accumulate; the
            # orchestrator folds it into the policy's effective max_nodes so a finished run, once
            # reopened, proposes more experiments instead of immediately re-finishing.
            # max_seconds/max_eval_seconds (budgets) + timeout/max_parallel (resource retune, gated by
            # the governance matrix at apply time) are ABSOLUTE new values (last write wins).
            for _k in ("max_seconds", "max_eval_seconds", "timeout", "max_parallel"):
                if d.get(_k) is not None:
                    st.budget_overrides[_k] = d[_k]
            if d.get("add_nodes") is not None:
                try:
                    st.budget_overrides["add_nodes"] = int(st.budget_overrides.get("add_nodes", 0)) + int(d["add_nodes"])
                except (TypeError, ValueError):
                    pass
        elif t == EV_HINT:
            # Append-only by default; a `replace` hint supersedes all prior standing directives
            # (mirrors set_strategy/pending_strategy) so the boss can rewrite the single directive
            # instead of accumulating contradictory ones. Replay-safe: deterministic over the log.
            if d.get("replace"):
                st.pending_hints = [d]
            else:
                st.pending_hints.append(d)
        elif t == EV_SET_STRATEGY:
            # A7 operator override (HITL parity with pause/hint): the human pins a Strategy. The
            # engine applies it before consulting the Strategist, so a human always wins. The pin owns
            # only the fields it names (policy/policy_params/fidelity) and STAYS in force for the rest
            # of the run (it is not cleared on apply) — a later set_strategy overwrites it; the
            # Strategist keeps tuning everything else (see Engine._maybe_consult_strategist).
            st.pending_strategy = d.get("strategy")
        elif t == EV_FORCE_CONFIRM:
            if d.get("node_id") is not None:
                st.confirm_requests.append(d["node_id"])
        elif t == EV_FORCE_ABLATE:
            if d.get("node_id") is not None:
                st.ablate_requests.append(d["node_id"])
        elif t == EV_FORK:
            st.fork_requests.append(d)
        elif t == EV_FORK_DONE:
            st.forks_done += 1   # one per processed fork request (gate for replay-safe fulfillment)
        elif t == EV_INJECT_NODE:
            st.inject_requests.append(d)        # operator-authored experiment (manual tree edit)
        elif t == EV_INJECT_DONE:
            st.injects_done += 1                 # one per processed inject (replay-safe gate)
        elif t == EV_DEEP_RESEARCH:
            st.research_requests.append(d)       # manual "go think hard" request (control event)
        elif t == EV_RESEARCH_COMPLETED:
            # Deep-Research memo (audit-only sidecar; NEVER touches nodes/best). `served_manual`
            # advances the manual-request gate so a resume never re-runs a served request.
            st.research.append(d.get("memo") or d)
            if d.get("served_manual"):
                st.research_served += 1
        elif t == EV_LESSONS_DISTILLED:
            # M6 mid-run comparative-lesson distillation (audit-only sidecar; NEVER touches
            # nodes/best). at_node + pair ids are the replay-safe gates (cadence + no re-distill).
            st.lessons_distilled.append(d)
        elif t == EV_LESSONS_REFRESHED:
            st.lessons_refreshed.append(d)   # M6 shared-store re-read (audit-only cadence gate)
        elif t == EV_REPORT_GENERATED:
            # Agent-authored run report (audit-only sidecar; NEVER touches nodes/best). Latest wins —
            # the cadence and manual-refresh paths both append this; the freshest narrative stands.
            st.report = d.get("content") or d
        elif t == EV_CONFIRM_DONE:
            nid = d.get("node_id")   # forced-confirm finished for this node (gate; selection untouched)
            if nid is not None and nid not in st.confirmed_forced:
                st.confirmed_forced.append(nid)
        elif t == EV_ANNOTATION:
            nid = d.get("node_id")
            if nid is not None:
                st.annotations.setdefault(nid, []).append(d.get("text", ""))
        elif t == EV_PROMOTE:
            st.promotions.append(d)
            if d.get("alias", "champion") == "champion":
                st.champion = d.get("node_id")
        # unknown event types (e.g. "budget") are ignored for state — forward-compat

    flagged = _apply_trust_gate(st)
    _select_best(st, flagged, best_confirmed)

    _derive_hypotheses(st)   # P1: audit-only ledger (after best is known); never touches selection
    return st


def _apply_trust_gate(st: RunState) -> set:
    """T2 trust enforcement post-pass: under "gate"/"block", a node flagged for a reward-hack or
    data-leakage signal must not be selectable as best (closes "a hacked/leaky node can win").
    Order-independent: computed from the folded `reward_hacks` after the full pass (see
    `flagged_node_ids`). Returns the flagged node-id set for `_select_best`."""
    flagged = flagged_node_ids(st)
    # "block" additionally bars the policy from breeding a flagged node forward (feasible=False also
    # removes it from `feasible_nodes()` used by the search policies), not just from winning.
    if st.trust_gate == "block":
        for nid in flagged:
            nb = st.nodes.get(nid)
            if nb is not None:
                nb.feasible = False
    return flagged


def _select_best(st: RunState, flagged: set, best_confirmed: int | None) -> None:
    """Best-selection post-pass: derive `best_node_id` (mean-based pick -> variance-gated confirm
    override -> holdout-gated promotion) plus the audit-only generalization gap. Pure and
    deterministic over the folded state — the tail of `fold`, extracted verbatim."""
    # Multi-objective (#5): a constraint-violating node is excluded from selection — it keeps
    # its metric for the audit trail but can never be chosen best. If NOTHING is feasible,
    # there is no valid best (best_node_id stays None).
    # Exclude nodes with no usable metric: a hand-edited / BYO-script node_evaluated event can carry
    # metric=null yet fold to status=evaluated, and comparing None vs a float in the chooser below
    # would raise TypeError and brick every re-fold/resume. Such a node simply can't be "best".
    evaluated = [n for n in st.evaluated_nodes()
                 if n.feasible and n.id not in flagged
                 and (n.confirmed_mean if n.confirmed_mean is not None else n.metric) is not None]
    if evaluated:
        # If any node has been confirmed (multi-seed), the final answer must be the
        # robust winner: rank confirmed nodes by confirmed_mean. With no confirmations
        # this is identical to ranking all evaluated nodes by their single metric.
        confirmed = [n for n in evaluated if n.confirmed_mean is not None]
        pool = confirmed if confirmed else evaluated
        chooser = min if st.direction == "min" else max

        def _key(n):
            v = n.confirmed_mean if n.confirmed_mean is not None else n.metric
            return (v, n.id)

        st.best_node_id = chooser(pool, key=_key).id

    # The variance-gated confirmation decision (I10) overrides the mean-based pick — but never
    # past the feasibility gate (#5): a constraint-violating node must not become best even if
    # the confirm phase ran on it (the mean-based pick above already excluded infeasibles).
    if (best_confirmed is not None and best_confirmed in st.nodes
            and st.nodes[best_confirmed].feasible
            and best_confirmed not in flagged):
        st.best_node_id = best_confirmed

    # D1 holdout-gated promotion: when the run recorded holdout_select, the champion is the best
    # node ON THE HOLDOUT PARTITION among those that were holdout-scored (the val-top-k — so the
    # search metric still decides WHO gets a holdout eval, but the unseen signal decides who WINS).
    # Applied LAST: the holdout is a stronger discipline than the confirm mean (it is data/splits
    # the search never optimized against — AIRA: picking on the search signal overfits 9-13 pp).
    # Same guards as every other pick: feasibility + trust flags.
    if st.holdout_select and evaluated:
        hpool = [n for n in evaluated if n.holdout_metric is not None]
        if hpool:
            chooser = min if st.direction == "min" else max
            st.best_node_id = chooser(hpool, key=lambda n: (n.holdout_metric, n.id)).id

    # Derived generalization gap (audit-only, Trust panel): how much better the search metric
    # looked than the unseen-signal metric — holdout when present, else the confirmed mean.
    # Direction-aware so positive always means "overperformed on the signal the search saw".
    for n in st.nodes.values():
        robust = n.holdout_metric if n.holdout_metric is not None else n.confirmed_mean
        if robust is None or n.metric is None:
            continue
        n.generalization_gap = (n.metric - robust) if st.direction == "max" else (robust - n.metric)


def _derive_hypotheses(st: RunState) -> None:
    """Build the hypothesis ledger from the folded state (P1). DERIVED, not stored: every node whose
    `idea.hypothesis` is set contributes a hypothesis (id = slug of the statement) with itself as
    evidence, merged with any explicitly-added ones (`hypothesis_added`). The verdict is computed from
    evidence outcomes — supported if an experiment IMPROVED over its parent (or became the run best),
    tested if evaluated without improvement, testing while still running, open with no evidence.
    Audit-only: nothing here is read by best-selection."""
    better = (lambda a, b: a > b) if st.direction == "max" else (lambda a, b: a < b)
    hyps: dict[str, Hypothesis] = {}

    # 1) explicitly-added hypotheses (human / deep-research) — may start with no evidence.
    # Coerce defensively: control events arrive from the API verbatim, and one malformed entry
    # must not brick every subsequent fold of the run (same convention as node_created).
    for d in st.hypotheses_added:
        try:
            stmt = str(d.get("statement", "")).strip()
            if not stmt:
                continue
            hid = str(d.get("id") or hypothesis_id(stmt))
            if hid in hyps:
                continue
            try:
                at_node = int(d.get("at_node", 0) or 0)
            except (TypeError, ValueError):
                at_node = 0
            hyps[hid] = Hypothesis(id=hid, statement=stmt, source=str(d.get("source") or "human"),
                                   rationale=str(d.get("rationale", ""))[:400],
                                   created_at_node=at_node)
        except Exception:
            continue

    # 2) derive/merge from nodes that state a hypothesis (evidence = the node).
    for nid in sorted(st.nodes):
        n = st.nodes[nid]
        stmt = (n.idea.hypothesis or "").strip() if n.idea else ""
        if not stmt:
            continue
        hid = hypothesis_id(stmt)
        h = hyps.get(hid)
        if h is None:
            h = Hypothesis(id=hid, statement=stmt, source="researcher",
                           rationale=(n.idea.rationale or "")[:400], created_at_node=n.id)
            hyps[hid] = h
        if n.id not in h.evidence:
            h.evidence.append(n.id)

    # 2b) apply agentic merges (`hypothesis_merged` events): fold each ALIAS hypothesis's evidence into
    # its CANONICAL. Fully DETERMINISTIC — no LLM here (the decision was made + recorded by the engine);
    # order-tolerant (evidence is unioned then sorted); back-compat (no merge events -> untouched).
    alias: dict[str, str] = {}
    merged_stmt: dict[str, str] = {}
    for d in st.hypotheses_merged:
        # Per-entry guard: the dispatch only checks `aliases` is TRUTHY, so a malformed record (a
        # hand-edited log, a foreign/future writer where `aliases` is a scalar like `1`/`true`) would
        # make `for a in aliases` raise TypeError and — since `_derive_hypotheses` runs unwrapped —
        # brick EVERY subsequent fold of the run (no replay/resume/view). Tolerate it here, matching
        # the node_created / hypotheses_added handlers and the "malformed entry is tolerated" promise.
        try:
            canon = str(d.get("canonical") or "").strip()
            if not canon:
                continue
            s = str(d.get("statement", "")).strip()
            if s:
                merged_stmt[canon] = s
            for a in (d.get("aliases") or []):
                a = str(a).strip()
                if a and a != canon:
                    alias[a] = canon
        except Exception:  # noqa: BLE001 — one bad merge record must not brick the whole fold
            continue

    def _canon(x: str) -> str:                      # resolve alias chains a->b->c, cycle-safe
        seen: set[str] = set()
        while x in alias and x not in seen:
            seen.add(x)
            x = alias[x]
        return x

    if alias:
        folded: dict[str, Hypothesis] = {}
        for hid in list(hyps):
            cid = _canon(hid)
            tgt = folded.get(cid)
            if tgt is None:
                base = hyps.get(cid, hyps[hid])     # seed from the canonical row if it exists, else this
                tgt = Hypothesis(id=cid, statement=merged_stmt.get(cid, base.statement),
                                 source=base.source, rationale=base.rationale,
                                 created_at_node=base.created_at_node)
                folded[cid] = tgt
            for e in hyps[hid].evidence:
                if e not in tgt.evidence:
                    tgt.evidence.append(e)
        for tgt in folded.values():
            tgt.evidence.sort()
        hyps = folded

    # 3) compute a verdict per hypothesis from its evidence nodes.
    for h in hyps.values():
        ev = [st.nodes[i] for i in h.evidence if i in st.nodes]
        evaluated = [n for n in ev if n.status is NodeStatus.evaluated and n.feasible
                     and n.metric is not None]
        supported = False
        best_delta: float | None = None
        for n in evaluated:
            # parent metric = the best feasible-evaluated parent's metric (direction-aware)
            pmetrics = [st.nodes[p].metric for p in n.parent_ids
                        if p in st.nodes and st.nodes[p].metric is not None
                        and st.nodes[p].feasible]
            base = (max(pmetrics) if st.direction == "max" else min(pmetrics)) if pmetrics else None
            if base is not None:
                delta = (n.metric - base) if st.direction == "max" else (base - n.metric)
                best_delta = delta if best_delta is None else max(best_delta, delta)
                if better(n.metric, base):
                    supported = True
            if n.id == st.best_node_id:            # a draft with no parent that became the run best
                supported = True
        h.best_delta = best_delta
        pending = [n for n in ev if n.status is NodeStatus.pending]
        if h.id in st.hypotheses_abandoned:
            h.status = "abandoned"
        elif not ev:
            h.status = "open"
        elif supported:
            h.status = "supported"                 # at least one experiment improved — verdict stands
        elif pending:
            h.status = "testing"                   # still inconclusive: evidence running
        elif not evaluated:
            h.status = "open"                      # all evidence failed/infeasible — no verdict
        else:
            h.status = "tested"                    # all evidence evaluated, none improved

    # FOREAGENT board prioritization: stamp each ranked card's `priority` (0-based position in the
    # latest `hypothesis_ranked` order) so the UI kanban sorts open cards by predicted payoff. Derived,
    # not stored on the event's cards — the ranking is by hypothesis id, robust to a card changing lane.
    order = (st.hypothesis_ranking or {}).get("order") or []
    for rank_i, hid in enumerate(order):
        h = hyps.get(str(hid))
        if h is not None and h.status == "open":   # priority is the OPEN lane's ordering; None once resolved
            h.priority = rank_i

    st.hypotheses = hyps
