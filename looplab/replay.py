"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. `best` is recomputed deterministically from
evaluated nodes (tie-break by id), so no separate `best_updated` event is needed.
"""
from __future__ import annotations

from typing import Iterable

from .models import Event, Idea, Node, NodeStatus, RunState


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
        elif t == "node_created":
            n = Node(
                id=d["node_id"],
                parent_ids=d.get("parent_ids", []),
                operator=d["operator"],
                idea=Idea(**d["idea"]),
                code=d.get("code", ""),
                files=d.get("files", {}) or {},
                deleted=d.get("deleted", []) or [],
            )
            st.nodes[n.id] = n
        elif t == "node_evaluated":
            n = st.nodes.get(d["node_id"])              # tolerate an event for an unknown node
            if n is not None:                           # (corrupt/hand-edited log) — skip, don't crash
                # Idempotent (C4): only a node's FIRST terminal event contributes its eval time, so
                # a duplicate node_evaluated/node_failed (corrupt log / double-fold) can't inflate
                # total_eval_seconds or make the budget order-dependent.
                first_terminal = n.status is NodeStatus.pending
                n.metric = d["metric"]
                n.status = NodeStatus.evaluated
                n.stdout_tail = d.get("stdout_tail", "")
                n.eval_seconds = d.get("eval_seconds")
                n.extra_metrics = d.get("extra_metrics", {}) or {}
                n.violations = d.get("violations", []) or []
                n.feasible = not n.violations           # #5: constraint-violating -> infeasible
                if first_terminal:
                    st.total_eval_seconds += d.get("eval_seconds") or 0.0
        elif t == "node_failed":
            n = st.nodes.get(d["node_id"])
            if n is not None:
                first_terminal = n.status is NodeStatus.pending
                n.status = NodeStatus.failed
                n.error = d.get("error", "")
                n.error_reason = d.get("reason", "")
                n.eval_seconds = d.get("eval_seconds")
                if first_terminal:
                    st.total_eval_seconds += d.get("eval_seconds") or 0.0
        elif t == "confirm_eval":
            st.total_eval_seconds += d.get("eval_seconds") or 0.0   # confirm-seed eval cost
            if "node_id" in d and "seed" in d:                       # per-seed resume memo (#0)
                st.confirm_seed_results.setdefault(d["node_id"], {})[d["seed"]] = d.get("metric")
        elif t == "node_confirmed":
            n = st.nodes.get(d["node_id"])
            if n is not None:
                n.confirmed_mean = d["mean"]
                n.confirmed_std = d.get("std")
                n.confirmed_seeds = d.get("seeds")
        elif t == "agent_validated":
            n = st.nodes.get(d["node_id"])
            if n is not None:                       # audit only; never affects selection
                n.agent_report = {
                    "ok": d.get("ok"), "checks": d.get("checks", []),
                    "fell_back": d.get("fell_back"), "attempts": d.get("attempts"),
                    "shipped_ok": d.get("shipped_ok"),
                }
        elif t == "data_profiled":
            st.data_profile = d.get("columns")
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
            st.policy_scores = {int(k): v for k, v in (d.get("scores") or {}).items()}
            st.policy_chosen = d.get("chosen")
        elif t == "strategy_decision":
            # A7 Strategist (audit-only): the engine recorded the chosen Strategy. Replay rebuilds
            # active_strategy WITHOUT re-calling the LLM (the decision is config, not selection).
            st.active_strategy = d.get("strategy")
            st.strategy_history.append({"strategy": d.get("strategy"), "at_node": d.get("at_node"),
                                        "ctx": d.get("ctx")})
        elif t == "rung_promoted":
            st.rungs.append({"rung": d.get("rung"), "survivors": d.get("survivors", [])})
        elif t == "reward_hack_suspected":
            st.reward_hacks.append({"node_id": d.get("node_id"), "signals": d.get("signals", [])})
        elif t == "novelty_rejected":
            st.novelty_events.append(d)   # E1: a near-duplicate proposal nudged off (audit)
        elif t == "proxy_scored":
            # A6 proxy/predictive scoring (audit-only): early-signal rank + which nodes were skipped.
            nid = d.get("node_id")
            if nid is not None and d.get("score") is not None:
                st.proxy_scores[nid] = d["score"]
            if d.get("skipped") and nid is not None and nid not in st.proxy_skipped:
                st.proxy_skipped.append(nid)
        elif t == "best_confirmed":
            best_confirmed = d["node_id"]
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
            for _k in ("max_seconds", "max_eval_seconds"):
                if d.get(_k) is not None:
                    st.budget_overrides[_k] = d[_k]
        elif t == "hint":
            st.pending_hints.append(d)
        elif t == "set_strategy":
            # A7 operator override (HITL parity with pause/hint): the human pins a Strategy. The
            # engine applies it before consulting the Strategist, so a human always wins. Cleared by
            # the engine recording the matching strategy_decision (source="operator").
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

    # Multi-objective (#5): a constraint-violating node is excluded from selection — it keeps
    # its metric for the audit trail but can never be chosen best. If NOTHING is feasible,
    # there is no valid best (best_node_id stays None).
    evaluated = [n for n in st.evaluated_nodes() if n.feasible]
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
            and st.nodes[best_confirmed].feasible):
        st.best_node_id = best_confirmed
    return st
