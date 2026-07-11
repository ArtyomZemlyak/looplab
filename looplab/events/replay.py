"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. `best` is recomputed deterministically from
evaluated nodes (tie-break by id), so no separate `best_updated` event is needed.
"""
from __future__ import annotations

import math
from typing import Iterable

from looplab.core.models import (Event, Hypothesis, Idea, Node, NodeStatus, RunState, Trial,
                     hypothesis_id)
from looplab.events.types import (
    EV_ABLATE, EV_AGENT_DECISION, EV_AGENT_VALIDATED, EV_ANNOTATION, EV_APPROVAL_GRANTED,
    EV_APPROVAL_REQUESTED, EV_BEST_CONFIRMED, EV_BUDGET_EXTEND, EV_CONFIRM_DONE,
    EV_CONFIRM_EVAL, EV_DATA_LEAKAGE, EV_DATA_PROFILED, EV_DATA_PROVENANCE,
    EV_COVERAGE_SNAPSHOT, EV_DEEP_RESEARCH, EV_DIVERSITY_ARCHIVE, EV_FORCE_ABLATE, EV_FORCE_CONFIRM,
    EV_FORESIGHT_SELECTED, EV_FORK,
    EV_FORK_DONE, EV_HINT, EV_HOLDOUT_EVALUATED, EV_HOST_GRADING, EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED,
    EV_HYPOTHESIS_RANKED, EV_HYPOTHESIS_UPDATED, EV_INJECT_DONE, EV_INJECT_NODE, EV_LESSONS_DISTILLED,
    EV_LESSONS_REFRESHED, EV_LLM_COST, EV_NODE_ABORT, EV_NODE_BUILDING, EV_NODE_CONFIRMED,
    EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED, EV_NODE_RESET,
    EV_NOVELTY_REJECTED, EV_PAUSE, EV_STAGE_FINISHED,
    EV_POLICY_DECISION, EV_PROMOTE, EV_PROXY_SCORED, EV_REPORT_GENERATED,
    EV_RESEARCH_COMPLETED, EV_RESUME, EV_REWARD_HACK_SUSPECTED, EV_RUN_ABORT,
    EV_RUN_FINISHED, EV_RUN_REOPENED, EV_RUN_STARTED, EV_RUNG_PROMOTED, EV_SET_STRATEGY,
    EV_SETUP_FINISHED, EV_SPEC_APPROVAL_REQUESTED, EV_SPEC_APPROVED, EV_SPEC_DRIFT, EV_SPEC_PROPOSED,
    EV_STRATEGY_DECISION, EV_TRUST_GATE_CHANGED, EV_WORKSPACE_CHANGED)


def flagged_node_ids(st: RunState) -> set:
    """T2: node ids excluded from best/holdout selection under trust_gate gate/block — those with a
    HIGH-PRECISION cheating/leakage signal. The heuristic `critic:` and `perfect_metric` signals
    stay advisory in every mode (perfect_metric flags metric<=0 (min) / >=1 (max), which
    legitimately-perfect scores hit, so gating on it could exclude honest winners). Empty under
    `audit`. Shared by the fold and the engine's holdout-topk so both apply the SAME exclusion."""
    if st.trust_gate not in ("gate", "block"):
        return set()
    return hard_flagged_ids(st)


def is_hard_signal(sig: str) -> bool:
    """Is this reward-hack/leakage signal HIGH-PRECISION (gating + agent-facing), vs advisory noise?

    The single classifier shared by `hard_flagged_ids` (gate/block selection exclusion) AND
    `digest.trust_reflection._sigs` (which signals to NAME in the agent hint) — kept here so the two
    can't drift: before, `_sigs` stripped EVERY `critic:` signal while `hard_flagged_ids` promoted
    `critic:hardcoded_metric`, so a node hard-flagged ONLY for that rendered as "node N ()" (a
    contentless warning). `critic:hardcoded_metric` is HIGH-PRECISION (the critic requires a LITERAL
    metric value with no computed assignment anywhere), so it gates — closing the "hardcode a
    near-optimal metric and win under every built-in gate" bypass on self-report tasks. Other
    `critic:` issues and `perfect_metric` (which a legitimately-perfect score hits) stay advisory."""
    sig = str(sig)
    if sig == "critic:hardcoded_metric":
        return True
    # `protected_audit_unavailable` (the whole workdir-tamper audit threw) is fail-closed evidence
    # that the node is NOT verified-clean, but it is not itself proof of tampering — a transient FS
    # error should SURFACE to the operator/agent, not gate-exclude an honest node. So it stays
    # advisory alongside critic:*/perfect_metric. `protected_missing`/`protected_unreadable` (a
    # protected file we placed is gone/corrupt) ARE real tamper evidence and remain HARD (P1-6).
    return not sig.startswith(("critic:", "perfect_metric", "protected_audit_unavailable"))


def hard_flagged_ids(st: RunState) -> set:
    """Node ids carrying a HIGH-PRECISION (non-`critic:`, non-`perfect_metric`) cheating/leakage
    signal, INDEPENDENT of `trust_gate` mode. `flagged_node_ids` uses it for gate/block selection
    exclusion; the agent-facing trust-reflection hint (signal-delivery §1) uses it to warn the
    Researcher about a flagged lineage even under `audit`, where nothing is gate-excluded."""
    def _has_hard_signal(rh: dict) -> bool:
        return any(is_hard_signal(s.get("signal", "")) for s in (rh.get("signals") or []))
    return {r.get("node_id") for r in st.reward_hacks if _has_hard_signal(r)}


# --------------------------------------------------------------------------- fold dispatch
# One handler per event type (docs/15 §P5.1): the bodies below are the VERBATIM arms of the
# former 63-way if/elif chain, one function each, dedented — with exactly three mechanical
# adjustments, all noted in place: (a) `continue` became `return` in _on_node_created (same
# meaning: skip the rest of THIS event); (b) the EV_BEST_CONFIRMED arm writes the fold-local
# through `ctx` (the ONE cross-arm value, threaded explicitly instead of a closure variable);
# (c) the resume/reopen twin arm is ONE handler registered under both keys.
# Every handler is a pure `(st, e, d, ctx) -> None` mutation — no I/O, no LLM calls — invoked
# in log order by `fold`, so determinism/order-tolerance are structurally unchanged; unknown
# event types still no-op via `_HANDLERS.get`. The uniform signature keeps the registry
# mechanical; most handlers ignore `e`/`ctx`.


class _FoldCtx:
    """The fold's cross-arm state: `best_confirmed` (EV_BEST_CONFIRMED -> _select_best) is the
    only value that flows BETWEEN arms without living on `st` — threaded explicitly so every
    handler stays a pure function of its arguments."""
    __slots__ = ("best_confirmed",)

    def __init__(self):
        self.best_confirmed: int | None = None

def _on_run_started(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Read with defaults like every other fold handler (RunState already defaults these to ""): the
    # fold loop dispatches handlers with NO per-event try/except, so a bare d["run_id"] KeyError on a
    # malformed/hand-edited run_started would take down the WHOLE fold (every view/replay/resume of the
    # run) — the exact hand-edited-log-tolerance the _on_node_created guard was added to provide.
    st.run_id = d.get("run_id", "")
    st.task_id = d.get("task_id", "")
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

def _on_trust_gate_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Operator edited the run's trust gate after launch (server config edit). Last write
    # wins so the change engages in every fold — live view, resume, reset — immediately.
    _tg = str(d.get("trust_gate", "")).strip().lower()
    if _tg in ("audit", "gate", "block"):
        st.trust_gate = _tg

def _on_node_building(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Transient "a node is being built RIGHT NOW" marker (see EV_NODE_BUILDING docs): show it in
    # the UI the instant work starts, before node_created. NOT added to st.nodes, so id
    # allocation + resume are untouched. Superseded/cleared by this node's node_created below.
    st.building = {"node_id": d.get("node_id"), "operator": d.get("operator"),
                   "parent_ids": d.get("parent_ids", []), "started": e.ts}

def _on_node_created(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
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
        return   # (was `continue` in the loop arm: skip just this event)
    st.nodes[n.id] = n
    if st.building and st.building.get("node_id") == n.id:
        st.building = None          # the real node is here now — drop the "building" marker

def _on_node_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = st.nodes.get(d.get("node_id"))          # tolerate an event for an unknown/missing node
    if n is not None:                           # (corrupt/hand-edited log) — skip, don't crash
        # Idempotent (C4): only a node's FIRST terminal event contributes its eval time, so
        # a duplicate node_evaluated/node_failed (corrupt log / double-fold) can't inflate
        # total_eval_seconds or make the budget order-dependent.
        # Invariant #2 "first terminal wins" applies to the WHOLE node, not just eval-seconds:
        # gate every field mutation on `first_terminal` so a CONFLICTING second terminal
        # (node_evaluated then node_failed, from a corrupt / double-appended log) can't flip the
        # node's metric/status/feasibility last-wins. A `node_reset` returns status to pending,
        # so a legitimate re-evaluation still applies (it IS the first terminal after the reset).
        first_terminal = n.status is NodeStatus.pending
        if first_terminal:
            n.metric = d.get("metric")          # missing -> None (feasible_nodes filters it)
            n.status = NodeStatus.evaluated
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            n.stdout_tail = d.get("stdout_tail", "")
            n.eval_seconds = d.get("eval_seconds")
            n.extra_metrics = d.get("extra_metrics", {}) or {}
            n.violations = d.get("violations", []) or []
            n.feasible = not n.violations       # #5: constraint-violating -> infeasible
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
            st.total_eval_seconds += d.get("eval_seconds") or 0.0

def _on_node_failed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    if st.building and st.building.get("node_id") == d.get("node_id"):
        st.building = None
    n = st.nodes.get(d.get("node_id"))
    if n is not None:
        # First-terminal-wins for the whole node (see node_evaluated above): a conflicting
        # second terminal from a corrupt log must not flip an already-evaluated node to failed.
        first_terminal = n.status is NodeStatus.pending
        if first_terminal:
            n.status = NodeStatus.failed
            n.error = d.get("error", "")
            n.error_reason = d.get("reason", "")
            # Crash-triage verdict, when the LLM triage ran (signal-delivery §1): fold it onto
            # the node so the failure-reflection hint / digest can hand it to the next proposal.
            # Additive + reader-defaulted: absent on old logs / rule-triaged nodes -> stays "".
            if d.get("triage_rationale"):
                n.triage_rationale = str(d.get("triage_rationale"))
            n.eval_seconds = d.get("eval_seconds")
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            if d.get("failed_stage"):
                n.failed_stage = d.get("failed_stage")   # Phase 1: which pipeline stage broke
            st.total_eval_seconds += d.get("eval_seconds") or 0.0

def _on_node_repaired(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
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

def _on_node_reset(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
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
        n.triage_rationale = ""   # the crash-triage verdict describes the NOW-abandoned lifecycle
        n.eval_seconds = None
        n.stdout_tail = ""
        n.extra_metrics = {}
        n.violations = []
        n.feasible = True
        n.trials = []
        n.confirmed_mean = None
        n.confirmed_std = None
        n.confirmed_seeds = None
        # The PER-SEED confirm memo must reset with the node too: the confirm phase memo-skips
        # every seed already in `confirm_seed_results`, so a stale entry would re-emit
        # node_confirmed from PRE-reset seed metrics for the post-reset code without running a
        # single seed. (The force-confirm gate `confirmed_forced` deliberately stays — it pairs
        # a force_confirm REQUEST with its confirm_done, and a reset is not a new request.)
        st.confirm_seed_results.pop(n.id, None)
        n.failed_stage = None
        # Finish-time scores computed on the NOW-discarded code must not survive the reset, or a
        # holdout-gated best pick / generalization-gap audit keeps using a stale number the node
        # can no longer reproduce (holdout is append-only + skips already-scored ids, so it would
        # never be recomputed for this node).
        n.holdout_metric = None
        if n.id in st.holdout_evaluated_ids:
            st.holdout_evaluated_ids.remove(n.id)
        if stage in ("implement", "propose"):
            n.code = ""
            n.files = {}
            n.deleted = []
            n.stages = []                # a re-develop discards the old pipeline outcomes too
            n.rerun_from = stage
            n.rerun_stage = None
        else:
            # eval-type reset: pending-with-code, the eval loop re-scores it. `from_stage` names
            # the pipeline stage to RESTART from (Phase 2) — the eval re-runs from there, reusing
            # earlier stages' artifacts. Plain "eval" on a single-command node is a full re-score.
            n.rerun_from = None
            n.rerun_stage = stage
        if st.building and st.building.get("node_id") == n.id:
            st.building = None
        # A reset means there is work to do again, so it RE-OPENS a finished run — else the
        # loop would see the stale run_finished and exit before re-running/re-scoring the node.
        # (Mirrors EV_RESUME's finished-clear; a later run_finished sets it again. `paused` is
        # left alone — that's the operator's separate resume.)
        st.finished = False
        st.stop_reason = None
        st.stop_requested = None

def _on_stage_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Multi-stage eval pipeline (Phase 1): one stage of a node's declared pipeline finished.
    # Last-wins by stage name so a stage-scoped RE-RUN (Phase 2) replaces the prior outcome
    # rather than appending a duplicate.
    n = st.nodes.get(d.get("node_id"))
    if n is not None:
        rec = {"name": d.get("name"), "status": d.get("status"),
               "exit_code": d.get("exit_code"), "seconds": d.get("seconds")}
        for i, s in enumerate(n.stages):
            if s.get("name") == rec["name"]:
                # A "reused" marker means a re-eval SKIPPED this stage (an earlier attempt already
                # ran it) — it must NOT clobber that attempt's REAL completion record (its true
                # exit_code/seconds), else the node reads as if it trained in 0s. Keep the
                # informative record. Order-tolerant: a real record still replaces a prior reused.
                if rec["status"] == "reused" and s.get("status") not in (None, "reused"):
                    break
                n.stages[i] = rec
                break
        else:
            n.stages.append(rec)

def _on_confirm_eval(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # First-occurrence eval-cost accounting: `confirm_eval` is the one eval-cost contributor
    # without the node-terminal first-terminal guard (events/types.py), so a duplicated/
    # double-folded confirm_eval would inflate `total_eval_seconds` and make the budget
    # order-sensitive. Count the seconds only for the FIRST (node_id, seed) we see — the
    # per-seed memo below already keys on that pair — so a re-fold stays idempotent.
    keyed = "node_id" in d and "seed" in d
    first = not (keyed and d["seed"] in st.confirm_seed_results.get(d["node_id"], {}))
    # Only a KEYED event (node_id+seed) can participate in the per-seed memo that makes the eval-cost
    # add idempotent; an un-keyed confirm_eval has no memo slot, so a duplicate/re-fold would
    # double-count total_eval_seconds (order/duplication-sensitive — the fold must not be). The sole
    # emitter always writes both keys, so this only guards a future/foreign/hand-edited un-keyed event.
    if keyed and first:
        st.total_eval_seconds += d.get("eval_seconds") or 0.0   # confirm-seed eval cost
    if keyed:                                                # per-seed resume memo (#0)
        st.confirm_seed_results.setdefault(d["node_id"], {})[d["seed"]] = d.get("metric")

def _on_node_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = st.nodes.get(d.get("node_id"))
    if n is not None:
        n.confirmed_mean = d.get("mean")
        n.confirmed_std = d.get("std")
        n.confirmed_seeds = d.get("seeds")

def _on_holdout_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
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

def _on_agent_validated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = st.nodes.get(d.get("node_id"))
    if n is not None:                       # audit only; never affects selection
        n.agent_report = {
            "ok": d.get("ok"), "checks": d.get("checks", []),
            "fell_back": d.get("fell_back"), "attempts": d.get("attempts"),
            "shipped_ok": d.get("shipped_ok"),
        }

def _on_data_profiled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.data_profile = d.get("columns")

def _on_data_provenance(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.data_provenance = d   # D4: pinned dataset/asset content hashes

def _on_host_grading(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.host_grading = d      # out-of-process host-side grading active (audit; no labels)

def _on_setup_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-3: setup completed (task+data preflight, incl. the leakage hard-stop). Folded so resume can
    # tell "setup done" from "crashed mid-setup right after run_started" — the latter must re-run the
    # rest of preflight (leakage!) rather than skip it forever. Idempotent (a re-run re-appends it).
    st.setup_done = True

def _on_data_leakage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.leakage = d

def _on_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.awaiting_approval = True

def _on_approval_granted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.awaiting_approval = False
    st.approved = True

def _on_spec_proposed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.proposed_spec = d

def _on_spec_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.spec_approval_requested = True

def _on_spec_approved(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.spec_confirmed = True

def _on_spec_drift(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.drifts.append(d)                         # audit only; metric already discarded

def _on_workspace_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.workspace_changed = True                 # resume saw the source repo/data change

def _on_diversity_archive(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.archive = d

def _on_coverage_snapshot(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.coverage_snapshots.append(d)   # audit-only breadth curve; the at_node gate dedups on resume

def _on_llm_cost(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.llm_cost = d

def _on_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.ablations.append(d)   # {parent_id, impacts} — parameter-sensitivity audit

def _on_policy_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    _scores = {}
    for k, v in (d.get("scores") or {}).items():
        try:
            _scores[int(k)] = v                 # a non-integer key (corrupt log) is skipped
        except (TypeError, ValueError):
            continue
    st.policy_scores = _scores
    st.policy_chosen = d.get("chosen")
    st.policy_reason = d.get("reason") or ""

def _on_strategy_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A7 Strategist (audit-only): the engine recorded the chosen Strategy. Replay rebuilds
    # active_strategy WITHOUT re-calling the LLM (the decision is config, not selection).
    st.active_strategy = d.get("strategy")
    st.strategy_history.append({"strategy": d.get("strategy"), "at_node": d.get("at_node"),
                                "ctx": d.get("ctx")})

def _on_hypothesis_ranked(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT board prioritization (audit-only): the engine recorded how the world model
    # ordered the OPEN hypotheses (order of ids + confidence + analysis trace). Latest-wins
    # (like policy_scores); `_derive_hypotheses` stamps each card's `priority` from `order`.
    st.hypothesis_ranking = d

def _on_rung_promoted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.rungs.append({"rung": d.get("rung"), "survivors": d.get("survivors", [])})

def _on_agent_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Self-driving unified agent (audit-only): records WHICH legal macro action the agent
    # chose and why. NEVER drives selection — the effect is the subsequent node_created,
    # folded as usual. Additive & non-load-bearing: an old log without it folds identically.
    st.agent_decisions.append(d)

def _on_reward_hack_suspected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.reward_hacks.append({"node_id": d.get("node_id"), "signals": d.get("signals", [])})

def _on_foresight_selected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT predict-before-execute pick (audit-only). Kept so the world model can be
    # primed with its OWN calibration (did the picked node beat its parent?), closing the
    # predict→outcome loop. Store only the small fields the scoreboard needs; never selection.
    nid = d.get("node_id")
    if nid is not None:
        st.foresight_selected.append({"node_id": nid, "confidence": d.get("confidence")})

def _on_novelty_rejected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.novelty_events.append(d)   # E1: a near-duplicate proposal nudged off (audit)

def _on_hypothesis_merged(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1+: engine-written agentic merge — fold alias hypotheses into a canonical. Collected
    # here, APPLIED deterministically in `_derive_hypotheses` (no LLM in the fold). A malformed
    # entry is tolerated there; unknown on old logs -> skipped by the outer dispatch.
    if d.get("canonical") and d.get("aliases"):
        st.hypotheses_merged.append(d)

def _on_hypothesis_added(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
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

def _on_hypothesis_updated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Carries a status override (human/agent drops — or reopens — a line of inquiry).
    # Last write wins: "deleted" removes the card entirely (sticky); "abandoned" adds the
    # abandoned override; any other status clears the abandoned override (reopen).
    hid = d.get("id")
    if hid:
        status = d.get("status")
        if status == "deleted":
            if hid not in st.hypotheses_deleted:
                st.hypotheses_deleted.append(hid)
        elif status == "abandoned":
            if hid not in st.hypotheses_abandoned:
                st.hypotheses_abandoned.append(hid)
        elif hid in st.hypotheses_abandoned:
            st.hypotheses_abandoned.remove(hid)

def _on_proxy_scored(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A6 proxy/predictive scoring (audit-only): early-signal rank + which nodes were skipped.
    nid = d.get("node_id")
    if nid is not None and d.get("score") is not None:
        st.proxy_scores[nid] = d["score"]
    if d.get("skipped") and nid is not None and nid not in st.proxy_skipped:
        st.proxy_skipped.append(nid)

def _on_best_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    ctx.best_confirmed = d.get("node_id", ctx.best_confirmed)
    st.confirmed_done = True   # the confirmation phase ran to completion

def _on_run_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.finished = True
    st.stop_reason = d.get("reason")
    # Drop any dangling "building" marker: if a dev session died mid-build (no node_created /
    # node_failed) the marker would otherwise persist, and the UI would show a breathing
    # "building…" card + a false "working" pulse on a run that is over.
    st.building = None

def _on_resume_or_run_reopened(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
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
def _on_run_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FINALIZE: the loop turns stop_requested into a run_finished (which runs the end-of-run
    # finalization — report/lessons/case/cost). A bare `stop` uses EV_PAUSE instead (no finalize).
    st.stop_requested = d.get("reason", "operator")

def _on_pause(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # STOP: freeze WITHOUT finalizing (finalize.py gates the wrap-up on `finished`, which a pause
    # never sets). A later `finalize` (EV_RUN_ABORT) can still wrap it up; RESUME lifts it.
    st.paused = True

def _on_node_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = d.get("node_id")
    if nid is not None and nid not in st.aborted_nodes:
        st.aborted_nodes.append(nid)

def _on_budget_extend(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # max_seconds / max_eval_seconds are ABSOLUTE new ceilings (last write wins). add_nodes is
    # an ADDITIVE delta — "give the run N more nodes" — so several extensions accumulate; the
    # orchestrator folds it into the policy's effective max_nodes so a finished run, once
    # reopened, proposes more experiments instead of immediately re-finishing.
    # max_seconds/max_eval_seconds (budgets) + timeout/max_parallel (resource retune, gated by
    # the governance matrix at apply time) are ABSOLUTE new values (last write wins).
    # COERCE to number in the fold: a UI form / TUI can post a STRING ("600"), and the engine
    # compares these numerically (`total_eval_seconds >= max_es`), so an un-coerced string would
    # raise TypeError in the main loop — and because the event replays, EVERY resume re-crashes
    # (a permanent poison event). A non-numeric value is skipped, not stored.
    for _k, _cast in (("max_seconds", float), ("max_eval_seconds", float),
                      ("timeout", float), ("max_parallel", int)):
        if d.get(_k) is not None:
            try:
                _v = _cast(d[_k])
            except (TypeError, ValueError):
                continue
            # Reject NaN/Inf: `float("nan")`/`float("inf")` PASS the cast, but a ceiling of
            # nan makes `total_eval_seconds >= nan` always False (budget silently disabled) and
            # inf never trips — and the poison value re-folds on every resume, permanently. Skip
            # it (keep the prior ceiling) rather than store a budget-disabling value.
            if _cast is float and not math.isfinite(_v):
                continue
            st.budget_overrides[_k] = _v
    if d.get("add_nodes") is not None:
        try:
            st.budget_overrides["add_nodes"] = int(st.budget_overrides.get("add_nodes", 0)) + int(d["add_nodes"])
        except (TypeError, ValueError):
            pass

def _on_hint(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Append-only by default; a `replace` hint supersedes all prior standing directives
    # (mirrors set_strategy/pending_strategy) so the boss can rewrite the single directive
    # instead of accumulating contradictory ones. Replay-safe: deterministic over the log.
    if d.get("replace"):
        st.pending_hints = [d]
    else:
        st.pending_hints.append(d)

def _on_set_strategy(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # A7 operator override (HITL parity with pause/hint): the human pins a Strategy. The
    # engine applies it before consulting the Strategist, so a human always wins. The pin owns
    # only the fields it names (policy/policy_params/fidelity) and STAYS in force for the rest
    # of the run (it is not cleared on apply) — a later set_strategy overwrites it; the
    # Strategist keeps tuning everything else (see Engine._maybe_consult_strategist).
    st.pending_strategy = d.get("strategy")

def _on_force_confirm(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    if d.get("node_id") is not None:
        st.confirm_requests.append(d["node_id"])

def _on_force_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    if d.get("node_id") is not None:
        st.ablate_requests.append(d["node_id"])

def _on_fork(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.fork_requests.append(d)

def _on_fork_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.forks_done += 1   # one per processed fork request (gate for replay-safe fulfillment)

def _on_inject_node(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.inject_requests.append(d)        # operator-authored experiment (manual tree edit)

def _on_inject_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.injects_done += 1                 # one per processed inject (replay-safe gate)

def _on_deep_research(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.research_requests.append(d)       # manual "go think hard" request (control event)

def _on_research_completed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Deep-Research memo (audit-only sidecar; NEVER touches nodes/best). `served_manual`
    # advances the manual-request gate so a resume never re-runs a served request.
    st.research.append(d.get("memo") or d)
    if d.get("served_manual"):
        st.research_served += 1

def _on_lessons_distilled(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # M6 mid-run comparative-lesson distillation (audit-only sidecar; NEVER touches
    # nodes/best). at_node + pair ids are the replay-safe gates (cadence + no re-distill).
    st.lessons_distilled.append(d)

def _on_lessons_refreshed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.lessons_refreshed.append(d)   # M6 shared-store re-read (audit-only cadence gate)

def _on_report_generated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Agent-authored run report (audit-only sidecar; NEVER touches nodes/best). Latest wins —
    # the cadence and manual-refresh paths both append this; the freshest narrative stands.
    st.report = d.get("content") or d

def _on_confirm_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = d.get("node_id")   # forced-confirm finished for this node (gate; selection untouched)
    if nid is not None and nid not in st.confirmed_forced:
        st.confirmed_forced.append(nid)

def _on_annotation(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = d.get("node_id")
    if nid is not None:
        st.annotations.setdefault(nid, []).append(d.get("text", ""))

def _on_promote(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.promotions.append(d)
    if d.get("alias", "champion") == "champion":
        st.champion = d.get("node_id")

# The dispatch registry — event type -> handler. Unknown types are absent: they no-op.
_HANDLERS = {
    EV_RUN_STARTED: _on_run_started,
    EV_TRUST_GATE_CHANGED: _on_trust_gate_changed,
    EV_NODE_BUILDING: _on_node_building,
    EV_NODE_CREATED: _on_node_created,
    EV_NODE_EVALUATED: _on_node_evaluated,
    EV_NODE_FAILED: _on_node_failed,
    EV_NODE_REPAIRED: _on_node_repaired,
    EV_NODE_RESET: _on_node_reset,
    EV_STAGE_FINISHED: _on_stage_finished,
    EV_CONFIRM_EVAL: _on_confirm_eval,
    EV_NODE_CONFIRMED: _on_node_confirmed,
    EV_HOLDOUT_EVALUATED: _on_holdout_evaluated,
    EV_AGENT_VALIDATED: _on_agent_validated,
    EV_DATA_PROFILED: _on_data_profiled,
    EV_DATA_PROVENANCE: _on_data_provenance,
    EV_HOST_GRADING: _on_host_grading,
    EV_SETUP_FINISHED: _on_setup_finished,
    EV_DATA_LEAKAGE: _on_data_leakage,
    EV_APPROVAL_REQUESTED: _on_approval_requested,
    EV_APPROVAL_GRANTED: _on_approval_granted,
    EV_SPEC_PROPOSED: _on_spec_proposed,
    EV_SPEC_APPROVAL_REQUESTED: _on_spec_approval_requested,
    EV_SPEC_APPROVED: _on_spec_approved,
    EV_SPEC_DRIFT: _on_spec_drift,
    EV_WORKSPACE_CHANGED: _on_workspace_changed,
    EV_DIVERSITY_ARCHIVE: _on_diversity_archive,
    EV_COVERAGE_SNAPSHOT: _on_coverage_snapshot,
    EV_LLM_COST: _on_llm_cost,
    EV_ABLATE: _on_ablate,
    EV_POLICY_DECISION: _on_policy_decision,
    EV_STRATEGY_DECISION: _on_strategy_decision,
    EV_HYPOTHESIS_RANKED: _on_hypothesis_ranked,
    EV_RUNG_PROMOTED: _on_rung_promoted,
    EV_AGENT_DECISION: _on_agent_decision,
    EV_REWARD_HACK_SUSPECTED: _on_reward_hack_suspected,
    EV_FORESIGHT_SELECTED: _on_foresight_selected,
    EV_NOVELTY_REJECTED: _on_novelty_rejected,
    EV_HYPOTHESIS_MERGED: _on_hypothesis_merged,
    EV_HYPOTHESIS_ADDED: _on_hypothesis_added,
    EV_HYPOTHESIS_UPDATED: _on_hypothesis_updated,
    EV_PROXY_SCORED: _on_proxy_scored,
    EV_BEST_CONFIRMED: _on_best_confirmed,
    EV_RUN_FINISHED: _on_run_finished,
    EV_RESUME: _on_resume_or_run_reopened,
    EV_RUN_REOPENED: _on_resume_or_run_reopened,
    EV_RUN_ABORT: _on_run_abort,
    EV_PAUSE: _on_pause,
    EV_NODE_ABORT: _on_node_abort,
    EV_BUDGET_EXTEND: _on_budget_extend,
    EV_HINT: _on_hint,
    EV_SET_STRATEGY: _on_set_strategy,
    EV_FORCE_CONFIRM: _on_force_confirm,
    EV_FORCE_ABLATE: _on_force_ablate,
    EV_FORK: _on_fork,
    EV_FORK_DONE: _on_fork_done,
    EV_INJECT_NODE: _on_inject_node,
    EV_INJECT_DONE: _on_inject_done,
    EV_DEEP_RESEARCH: _on_deep_research,
    EV_RESEARCH_COMPLETED: _on_research_completed,
    EV_LESSONS_DISTILLED: _on_lessons_distilled,
    EV_LESSONS_REFRESHED: _on_lessons_refreshed,
    EV_REPORT_GENERATED: _on_report_generated,
    EV_CONFIRM_DONE: _on_confirm_done,
    EV_ANNOTATION: _on_annotation,
    EV_PROMOTE: _on_promote,
}


def fold(events: Iterable[Event]) -> RunState:
    st = RunState()
    ctx = _FoldCtx()
    for e in events:
        h = _HANDLERS.get(e.type)
        # unknown event types (e.g. "budget") are ignored for state — forward-compat
        if h is not None:
            h(st, e, e.data, ctx)

    flagged = _apply_trust_gate(st)
    _select_best(st, flagged, ctx.best_confirmed)

    _derive_hypotheses(st)   # P1: audit-only ledger (after best is known); never touches selection
    return st



def _apply_trust_gate(st: RunState) -> set:
    """T2 trust enforcement post-pass: under "gate"/"block", a node flagged for a reward-hack or
    data-leakage signal must not be selectable as best (closes "a hacked/leaky node can win").
    Order-independent: computed from the folded `reward_hacks` after the full pass (see
    `flagged_node_ids`). Returns the flagged node-id set for `_select_best`."""
    flagged = flagged_node_ids(st)
    # Bar the flagged set from BREEDING/confirm targets (§2.2): under `gate` the node stays feasible
    # (kept in the tree for diversity/audit, barred only from winning) but `breedable_nodes()` skips it
    # so the search doesn't sink budget improving a cheating lineage. `block` ALSO makes it infeasible
    # (feasible=False removes it from feasible_nodes() entirely), the stricter mode.
    st.breed_excluded = set(flagged)
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
                 and n.robust_metric is not None]
    if evaluated:
        # If any node has been confirmed (multi-seed), the final answer must be the
        # robust winner: rank confirmed nodes by confirmed_mean. With no confirmations
        # this is identical to ranking all evaluated nodes by their single metric.
        confirmed = [n for n in evaluated if n.confirmed_mean is not None]
        pool = confirmed if confirmed else evaluated
        chooser = min if st.direction == "min" else max

        def _key(n):
            v = n.robust_metric
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

    # A node "supported" its hypothesis by ADVANCING the run's SOTA — and a record it set STAYS a support
    # even after a later node overtakes it. Computing "is the CURRENT best" (a moving target) instead made
    # a draft-backed hypothesis flip supported→tested the moment something beat it (read as a board bug).
    # So mark record-SETTERS once, in creation order, and treat that as sticky evidence below.
    _record_setters: set[int] = set()
    _running: float | None = None
    for _n in sorted(st.nodes.values(), key=lambda x: x.id):
        if _n.status is NodeStatus.evaluated and _n.feasible and _n.metric is not None:
            if _running is None or better(_n.metric, _running):
                _record_setters.add(_n.id)          # first node ESTABLISHES the SOTA, or a later node
                _running = _n.metric                # BEATS the standing record — either is a real advance
                #                                     that stays supported even after being overtaken

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
            if n.id in _record_setters:            # a draft/node that advanced the run's SOTA (sticky —
                supported = True                   # stays supported even after a later node overtakes it)
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

    st.hypotheses = {k: v for k, v in hyps.items() if k not in st.hypotheses_deleted}
