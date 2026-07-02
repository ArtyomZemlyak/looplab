"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. `best` is recomputed deterministically from
evaluated nodes (tie-break by id), so no separate `best_updated` event is needed.
"""
from __future__ import annotations

from typing import Iterable

from .models import (Event, Hypothesis, Idea, Node, NodeStatus, RunState, Trial,
                     hypothesis_id)


def fold(events: Iterable[Event]) -> RunState:
    st = RunState()
    best_confirmed: int | None = None
    for e in events:
        d = e.data
        t = e.type
        if t == "run_started":
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
        elif t == "trust_gate_changed":
            # Operator edited the run's trust gate after launch (server config edit). Last write
            # wins so the change engages in every fold — live view, resume, reset — immediately.
            _tg = str(d.get("trust_gate", "")).strip().lower()
            if _tg in ("audit", "gate", "block"):
                st.trust_gate = _tg
        elif t == "node_created":
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
            except Exception:
                continue
            st.nodes[n.id] = n
        elif t == "node_evaluated":
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
        elif t == "node_failed":
            n = st.nodes.get(d.get("node_id"))
            if n is not None:
                first_terminal = n.status is NodeStatus.pending
                n.status = NodeStatus.failed
                n.error = d.get("error", "")
                n.error_reason = d.get("reason", "")
                n.eval_seconds = d.get("eval_seconds")
                if first_terminal:
                    st.total_eval_seconds += d.get("eval_seconds") or 0.0
        elif t == "node_repaired":
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
        elif t == "confirm_eval":
            st.total_eval_seconds += d.get("eval_seconds") or 0.0   # confirm-seed eval cost
            if "node_id" in d and "seed" in d:                       # per-seed resume memo (#0)
                st.confirm_seed_results.setdefault(d["node_id"], {})[d["seed"]] = d.get("metric")
        elif t == "node_confirmed":
            n = st.nodes.get(d.get("node_id"))
            if n is not None:
                n.confirmed_mean = d.get("mean")
                n.confirmed_std = d.get("std")
                n.confirmed_seeds = d.get("seeds")
        elif t == "agent_validated":
            n = st.nodes.get(d.get("node_id"))
            if n is not None:                       # audit only; never affects selection
                n.agent_report = {
                    "ok": d.get("ok"), "checks": d.get("checks", []),
                    "fell_back": d.get("fell_back"), "attempts": d.get("attempts"),
                    "shipped_ok": d.get("shipped_ok"),
                }
        elif t == "data_profiled":
            st.data_profile = d.get("columns")
        elif t == "data_provenance":
            st.data_provenance = d   # D4: pinned dataset/asset content hashes
        elif t == "host_grading":
            st.host_grading = d      # out-of-process host-side grading active (audit; no labels)
        elif t == "data_leakage":
            st.leakage = d
        elif t == "approval_requested":
            st.awaiting_approval = True
        elif t == "approval_granted":
            st.awaiting_approval = False
            st.approved = True
        elif t == "spec_proposed":
            st.proposed_spec = d
        elif t == "spec_approval_requested":
            st.spec_approval_requested = True
        elif t == "spec_approved":
            st.spec_confirmed = True
        elif t == "spec_drift":
            st.drifts.append(d)                         # audit only; metric already discarded
        elif t == "workspace_changed":
            st.workspace_changed = True                 # resume saw the source repo/data change
        elif t == "diversity_archive":
            st.archive = d
        elif t == "llm_cost":
            st.llm_cost = d
        elif t == "ablate":
            st.ablations.append(d)   # {parent_id, impacts} — parameter-sensitivity audit
        elif t == "policy_decision":
            _scores = {}
            for k, v in (d.get("scores") or {}).items():
                try:
                    _scores[int(k)] = v                 # a non-integer key (corrupt log) is skipped
                except (TypeError, ValueError):
                    continue
            st.policy_scores = _scores
            st.policy_chosen = d.get("chosen")
            st.policy_reason = d.get("reason") or ""
        elif t == "strategy_decision":
            # A7 Strategist (audit-only): the engine recorded the chosen Strategy. Replay rebuilds
            # active_strategy WITHOUT re-calling the LLM (the decision is config, not selection).
            st.active_strategy = d.get("strategy")
            st.strategy_history.append({"strategy": d.get("strategy"), "at_node": d.get("at_node"),
                                        "ctx": d.get("ctx")})
        elif t == "rung_promoted":
            st.rungs.append({"rung": d.get("rung"), "survivors": d.get("survivors", [])})
        elif t == "agent_decision":
            # Self-driving unified agent (audit-only): records WHICH legal macro action the agent
            # chose and why. NEVER drives selection — the effect is the subsequent node_created,
            # folded as usual. Additive & non-load-bearing: an old log without it folds identically.
            st.agent_decisions.append(d)
        elif t == "reward_hack_suspected":
            st.reward_hacks.append({"node_id": d.get("node_id"), "signals": d.get("signals", [])})
        elif t == "novelty_rejected":
            st.novelty_events.append(d)   # E1: a near-duplicate proposal nudged off (audit)
        elif t == "hypothesis_added":
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
        elif t == "hypothesis_updated":
            # Carries a status override (human/agent drops — or reopens — a line of inquiry).
            # Last write wins: "abandoned" adds the override, any other status clears it.
            hid = d.get("id")
            if hid:
                if d.get("status") == "abandoned":
                    if hid not in st.hypotheses_abandoned:
                        st.hypotheses_abandoned.append(hid)
                elif hid in st.hypotheses_abandoned:
                    st.hypotheses_abandoned.remove(hid)
        elif t == "proxy_scored":
            # A6 proxy/predictive scoring (audit-only): early-signal rank + which nodes were skipped.
            nid = d.get("node_id")
            if nid is not None and d.get("score") is not None:
                st.proxy_scores[nid] = d["score"]
            if d.get("skipped") and nid is not None and nid not in st.proxy_skipped:
                st.proxy_skipped.append(nid)
        elif t == "best_confirmed":
            best_confirmed = d.get("node_id", best_confirmed)
            st.confirmed_done = True   # the confirmation phase ran to completion
        elif t == "run_finished":
            st.finished = True
            st.stop_reason = d.get("reason")
        elif t == "run_reopened":
            # An operator added an experiment to a FINISHED run and wants to continue it: clear the
            # terminal flags so re-entering the loop (resume) processes the new node(s) and then
            # re-finishes. Deterministic under replay — a later run_finished simply sets it again.
            st.finished = False
            st.stop_reason = None
        # --- live operator control events (UI intervention). Intent only; the engine reads
        # these and writes the matching domain effect. Deterministic under replay. ---
        elif t == "run_abort":
            st.stop_requested = d.get("reason", "operator")
        elif t == "pause":
            st.paused = True
        elif t == "resume":
            st.paused = False
        elif t == "node_abort":
            nid = d.get("node_id")
            if nid is not None and nid not in st.aborted_nodes:
                st.aborted_nodes.append(nid)
        elif t == "budget_extend":
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
        elif t == "hint":
            # Append-only by default; a `replace` hint supersedes all prior standing directives
            # (mirrors set_strategy/pending_strategy) so the boss can rewrite the single directive
            # instead of accumulating contradictory ones. Replay-safe: deterministic over the log.
            if d.get("replace"):
                st.pending_hints = [d]
            else:
                st.pending_hints.append(d)
        elif t == "set_strategy":
            # A7 operator override (HITL parity with pause/hint): the human pins a Strategy. The
            # engine applies it before consulting the Strategist, so a human always wins. The pin owns
            # only the fields it names (policy/policy_params/fidelity) and STAYS in force for the rest
            # of the run (it is not cleared on apply) — a later set_strategy overwrites it; the
            # Strategist keeps tuning everything else (see Engine._maybe_consult_strategist).
            st.pending_strategy = d.get("strategy")
        elif t == "force_confirm":
            if d.get("node_id") is not None:
                st.confirm_requests.append(d["node_id"])
        elif t == "force_ablate":
            if d.get("node_id") is not None:
                st.ablate_requests.append(d["node_id"])
        elif t == "fork":
            st.fork_requests.append(d)
        elif t == "fork_done":
            st.forks_done += 1   # one per processed fork request (gate for replay-safe fulfillment)
        elif t == "inject_node":
            st.inject_requests.append(d)        # operator-authored experiment (manual tree edit)
        elif t == "inject_done":
            st.injects_done += 1                 # one per processed inject (replay-safe gate)
        elif t == "deep_research":
            st.research_requests.append(d)       # manual "go think hard" request (control event)
        elif t == "research_completed":
            # Deep-Research memo (audit-only sidecar; NEVER touches nodes/best). `served_manual`
            # advances the manual-request gate so a resume never re-runs a served request.
            st.research.append(d.get("memo") or d)
            if d.get("served_manual"):
                st.research_served += 1
        elif t == "report_generated":
            # Agent-authored run report (audit-only sidecar; NEVER touches nodes/best). Latest wins —
            # the cadence and manual-refresh paths both append this; the freshest narrative stands.
            st.report = d.get("content") or d
        elif t == "confirm_done":
            nid = d.get("node_id")   # forced-confirm finished for this node (gate; selection untouched)
            if nid is not None and nid not in st.confirmed_forced:
                st.confirmed_forced.append(nid)
        elif t == "annotation":
            nid = d.get("node_id")
            if nid is not None:
                st.annotations.setdefault(nid, []).append(d.get("text", ""))
        elif t == "promote":
            st.promotions.append(d)
            if d.get("alias", "champion") == "champion":
                st.champion = d.get("node_id")
        # unknown event types (e.g. "budget") are ignored for state — forward-compat

    # T2 trust enforcement: under "gate"/"block", a node flagged for a reward-hack or data-leakage
    # signal must not be selectable as best (closes "a hacked/leaky node can win"). Gate ONLY on the
    # high-precision cheating/leakage signals — the heuristic `critic:` signal stays advisory in
    # every mode. Order-independent: computed from the folded `reward_hacks` after the full pass.
    def _has_hard_signal(rh: dict) -> bool:
        # `critic:` AND `perfect_metric` stay advisory: perfect_metric flags metric<=0 (min) /
        # >=1 (max), which legitimately-perfect scores (accuracy 1.0, an achievable 0.0 floor,
        # negative-valued objectives) hit — gating on it can exclude every honest winner.
        return any(not str(s.get("signal", "")).startswith(("critic:", "perfect_metric"))
                   for s in (rh.get("signals") or []))
    flagged = ({r.get("node_id") for r in st.reward_hacks if _has_hard_signal(r)}
               if st.trust_gate in ("gate", "block") else set())
    # "block" additionally bars the policy from breeding a flagged node forward (feasible=False also
    # removes it from `feasible_nodes()` used by the search policies), not just from winning.
    if st.trust_gate == "block":
        for nid in flagged:
            nb = st.nodes.get(nid)
            if nb is not None:
                nb.feasible = False

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

    _derive_hypotheses(st)   # P1: audit-only ledger (after best is known); never touches selection
    return st


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

    st.hypotheses = hyps
