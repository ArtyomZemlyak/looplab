"""Pure fold: events -> RunState (I1/I6, ADR-12). Deterministic; the only producer
of RunState. Resume = re-fold the log. `best` is recomputed deterministically from
evaluated nodes (tie-break by id), so no separate `best_updated` event is needed.
"""
from __future__ import annotations

import math
from typing import Iterable

from looplab.core.fitness import SearchFitness
from looplab.core.models import (Event, Hypothesis, Idea, Node, NodeStatus, RunState, Trial,
                     hypothesis_id, run_setup_key)
from looplab.events.types import (
    EV_ABLATE, EV_AGENT_DECISION, EV_AGENT_VALIDATED, EV_ANNOTATION, EV_APPROVAL_GRANTED,
    EV_APPROVAL_REQUESTED, EV_BEST_CONFIRMED, EV_BUDGET_EXTEND, EV_CONFIRM_DONE,
    EV_CONFIRM_EVAL, EV_DATA_LEAKAGE, EV_DATA_PROFILED, EV_DATA_PROVENANCE, EV_ENV_CHANGED,
    EV_CONCEPT_COVERAGE_SNAPSHOT, EV_COVERAGE_SNAPSHOT, EV_DEEP_RESEARCH, EV_DIVERSITY_ARCHIVE,
    EV_FINALIZATION_FINISHED,
    EV_FORCE_ABLATE, EV_FORCE_CONFIRM,
    EV_FORESIGHT_SELECTED, EV_FORK,
    EV_FORK_DONE, EV_HINT, EV_HOLDOUT_EVALUATED, EV_HOST_GRADING, EV_HYPOTHESIS_ADDED, EV_HYPOTHESIS_MERGED,
    EV_HYPOTHESIS_RANKED, EV_HYPOTHESIS_UPDATED, EV_INJECT_DONE, EV_INJECT_NODE, EV_LESSONS_DISTILLED,
    EV_LESSONS_REFRESHED, EV_LLM_COST, EV_NODE_ABORT, EV_NODE_BUILDING, EV_NODE_CONFIRMED,
    EV_NODE_CREATED, EV_NODE_EVALUATED, EV_NODE_FAILED, EV_NODE_REPAIRED, EV_NODE_RESET,
    EV_NODE_TOMBSTONED, EV_NODE_VERIFIED, EV_NOVELTY_GRADED, EV_NOVELTY_REJECTED, EV_PAUSE, EV_STAGE_FINISHED,
    EV_POLICY_DECISION, EV_PROMOTE, EV_PROXY_SCORED, EV_REPORT_GENERATED,
    EV_RESEARCH_COMPLETED, EV_RESUME, EV_RESUME_REQUESTED, EV_RESUME_SERVED,
    EV_REWARD_HACK_SUSPECTED, EV_RUN_ABORT,
    EV_RUN_FINISHED, EV_RUN_REOPENED, EV_RUN_SETUP_FINISHED, EV_RUN_STARTED, EV_RUNG_PROMOTED,
    EV_SET_STRATEGY,
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
    def _has_current_hard_signal(rh: dict) -> bool:
        nid = _coerce_node_id(rh)
        n = st.nodes.get(nid) if nid is not None else None
        if n is None or rh.get("generation", n.attempt) != n.attempt:
            return False
        return any(is_hard_signal(s.get("signal", "")) for s in (rh.get("signals") or []))
    return {nid for r in st.reward_hacks
            if _has_current_hard_signal(r) and (nid := _coerce_node_id(r)) is not None}


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
    __slots__ = ("best_confirmed", "charged_terminal_generations", "charged_confirm_seeds",
                 "charged_ablation_ids", "pending_finish_report", "event_index")

    def __init__(self):
        self.best_confirmed: int | None = None
        # First terminal COST wins per (node,lifecycle), independently from whether that lifecycle is
        # still current. A reset may discard its metric/state, but cannot refund compute already spent.
        self.charged_terminal_generations: set[tuple[int, int]] = set()
        self.charged_confirm_seeds: set[tuple[int, int, int]] = set()
        self.charged_ablation_ids: set[str] = set()
        # (physical event seq, physical fold index, content). The index is needed for legacy logs
        # whose envelopes have no meaningful seq but whose report->finish adjacency is still valid.
        self.pending_finish_report: tuple[int, int, dict] | None = None
        self.event_index = -1

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
    st.env = d.get("env")   # P0-5 environment identity pinned at start (None on old logs)
    _di = d.get("dirty_inputs")
    st.dirty_inputs = _di if isinstance(_di, list) else []   # P0-5 uncommitted-input enumeration
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
    # R1-c: recorded at start so replay applies the same selection rule (config isn't available to the
    # pure fold). Absent in old logs -> False -> byte-identical legacy selection.
    # The fold stays pinned to the RECORDED value (never a live re-read); the engine re-pins its own
    # `_select_verifier` gate from this recorded value on resume (orchestrator `_reentry_repin`), so the
    # fold's tie-break rule and the live verify production can't diverge across a config edit (invariant #6).
    st.select_verifier_tiebreak = bool(d.get("select_verifier", False))

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
    nid = _coerce_node_id(d)
    if nid is None:
        return
    current = st.nodes.get(nid)
    if current is not None and (nid in st.aborted_nodes or current.tombstoned):
        if _building_matches_event(st, d, nid):
            st.building = None
        return
    if current is not None and not _generation_matches(current, d):
        return
    st.building = {"node_id": nid, "operator": d.get("operator"),
                   "parent_ids": d.get("parent_ids", []), "started": e.ts}
    generation = _event_generation(d)
    if generation is not _MISSING:
        st.building["generation"] = generation

def _on_node_created(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Don't let a duplicate node_created RESURRECT a settled node (invariant #2 "first terminal
    # wins"): if the id already exists AND is in a TERMINAL state (evaluated/failed), skip the event.
    # Overwriting a terminal node installed a fresh status=pending Node, which re-armed the
    # `first_terminal` guard so a following duplicate terminal RE-added its eval_seconds to
    # total_eval_seconds (cost double-charged) and could flip a settled metric/status/feasibility
    # last-wins — the exact idempotency `_on_node_evaluated` protects the terminal against.
    # A re-emit onto a PENDING id is legitimate and MUST apply: `node_reset` (propose/implement)
    # re-opens a node to pending and the engine re-develops it in place, emitting a SECOND
    # node_created for the same id (orchestrator `_rerun_reset_node`) whose new code/idea must land
    # and clear `rerun_from` — dropping it loops the engine forever re-developing. So the guard keys
    # on terminal status, not mere existence. A clean first build has no prior node -> applies.
    # Coerce BEFORE looking up the settled lifecycle. A numeric-string duplicate ("0") names the
    # same node as integer 0 and must not bypass first-terminal-wins by missing the raw dict key.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    existing = st.nodes.get(nid)
    if existing is not None and existing.status is not NodeStatus.pending:
        return
    # Defensive like the per-trial / unknown-node tolerance below: a malformed or incomplete
    # node_created (missing key, non-coercible idea param in a hand-edited / bring-your-own-script
    # log) must not crash the WHOLE fold — skip the bad event instead (the engine, sole writer,
    # always round-trips a validated Idea, so this only fires on a corrupt log).
    if not _parent_generation_map_matches(st, d):
        if _building_matches_event(st, d, nid):
            st.building = None
        return
    current = st.nodes.get(nid)
    if current is not None and (nid in st.aborted_nodes or current.tombstoned):
        if _building_matches_event(st, d, nid):
            st.building = None
        return
    generation = _event_generation(d)
    if generation is _MISSING:
        # Old node_created records were unstamped. On an initial create their generation is zero;
        # on a legacy in-place rebuild preserve the generation the preceding node_reset established.
        generation = current.attempt if current is not None else 0
    if generation is None or generation < 0:
        return
    if current is not None and generation != current.attempt:
        return                       # a late rebuild from a superseded lifecycle
    try:
        n = Node(
            id=nid,
            parent_ids=d.get("parent_ids", []),
            operator=d["operator"],
            idea=Idea(**d["idea"]),
            code=d.get("code", ""),
            files=d.get("files", {}) or {},
            deleted=d.get("deleted", []) or [],
            attempt=generation,
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
    if current is None:
        # A holdout score is a disclosed final-exam signal. If a genuinely NEW candidate lands
        # afterwards (an inject/fork/policy action won the finish CAS race), the search has become
        # adaptive to that signal. Rotate the hidden split before any later promotion can reuse it.
        _invalidate_disclosed_holdout(st, fresh_node_ids={n.id})
        # A genuinely new candidate invalidates any confirmation/approval completed for the prior
        # candidate set — including when it is created just AFTER best_confirmed was appended.
        ctx.best_confirmed = None
        st.confirmed_done = False
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
    if _building_matches_event(st, d, n.id):
        st.building = None          # the real node is here now — drop the "building" marker

def _nonneg_seconds(v) -> float:
    """Coerce a PERSISTED eval-cost value to a FINITE, NON-NEGATIVE float before it enters the
    cumulative budget. A hand-edited / foreign-writer log with eval_seconds="3" (str) would otherwise
    TypeError the WHOLE fold — taking down every view/replay/resume of the run — and a negative value
    would silently REDUCE total_eval_seconds, extending the budget (arch-review §5 P2). Normal engine
    emitters always produce a clean non-negative float, so this only guards malformed input."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if (math.isfinite(f) and f >= 0.0) else 0.0


def _charge_eval_seconds(st: RunState, kind: str, raw) -> None:
    """P1-2 budget buckets: add a coerced non-negative eval-seconds to the cumulative total AND to its
    category bucket (node|confirm). One helper so the total and the per-kind split can never drift."""
    secs = _nonneg_seconds(raw)
    st.total_eval_seconds += secs
    if secs:
        st.eval_seconds_by_kind[kind] = st.eval_seconds_by_kind.get(kind, 0.0) + secs


def _attempt_matches(n, d: dict) -> bool:
    """P0-1 attempt guard: a node terminal (node_evaluated/node_failed) is honored only if the
    `attempt` it was stamped with still matches the node's current attempt generation. `node_reset`
    bumps `n.attempt`, so a LATE terminal from an abandoned attempt (its eval was in flight when the
    reset happened) carries the OLD attempt and is dropped — it can't land as first-terminal-after-
    reset and accept a metric from discarded code (the real compute is still charged separately).
    Truly unstamped terminals predate reset generations and are accepted only for generation 0."""
    generation = _event_generation(d, legacy_attempt=True)
    # Unstamped terminals are legacy generation-0 records. Accepting one after reset would let a
    # delayed old writer impersonate the current lifecycle (ABA); all modern emitters are stamped.
    if generation is _MISSING:
        return n.attempt == 0
    return generation is not None and generation == n.attempt


def _coerce_node_id(d: dict, key: str = "node_id"):
    """Coerce a raw event `node_id` to an int for a fold KEY/membership op, or None if it isn't a usable
    node id. Several sanctioned /control events (`approval_granted`, `annotation`) are appended VERBATIM,
    so a forged `{"node_id":[999]}` (unhashable) / bool / non-numeric id must be rejected BEFORE it
    reaches a dict/set hash — else the fold raises `TypeError: unhashable` and bricks every replay. Rejects
    a bool (subclasses int, so int(True)==1 would spuriously match node 1) and anything non-coercible
    (incl. a non-finite float -> OverflowError). A missing/None id also returns None; each handler decides
    whether that means accept (a bare grant) or drop."""
    v = d.get(key)
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        # Never truncate 3.9 into node 3 at an approval/control boundary. JSON frontends may
        # legitimately encode an integer as 3.0, so accept only finite integral floats.
        return int(v) if math.isfinite(v) and v.is_integer() else None
    if not isinstance(v, str):
        return None
    try:
        return int(v.strip())
    except (TypeError, ValueError, OverflowError):
        return None


_MISSING = object()


def _event_generation(d: dict, *, legacy_attempt: bool = False):
    """Return an explicitly stamped lifecycle generation, `_MISSING` for a legacy unstamped event,
    or None for an invalid stamp. `node_repaired.data.attempt` predates lifecycle generations and is
    the INLINE-REPAIR ordinal, so callers opt into the terminal-only `attempt` compatibility alias."""
    if "generation" in d:
        raw = d.get("generation")
    elif legacy_attempt and "attempt" in d:
        raw = d.get("attempt")
    else:
        return _MISSING
    generation = _coerce_node_id({"node_id": raw})
    return generation if generation is not None and generation >= 0 else None


def _building_matches_event(st: RunState, d: dict, nid: int) -> bool:
    """Only let an event clear the transient marker for the same node lifecycle.

    Reruns reuse node ids. A late generation-1 failure must not erase a generation-2 build marker.
    Historical markers were unstamped, so they retain the legacy id-only clear behaviour.
    """
    marker = st.building
    if not marker or marker.get("node_id") != nid:
        return False
    marker_generation = _event_generation(marker)
    if marker_generation is _MISSING:
        return True
    event_generation = _event_generation(d, legacy_attempt=True)
    return (event_generation is not _MISSING and event_generation is not None
            and event_generation == marker_generation)


def _generation_matches(n: Node, d: dict, *, legacy_attempt: bool = False) -> bool:
    generation = _event_generation(d, legacy_attempt=legacy_attempt)
    return generation is _MISSING or (generation is not None and generation == n.attempt)


def _control_generation_matches(n: Node, d: dict) -> bool:
    """Match a lifecycle-mutating operator intent while preserving old persisted logs.

    Historical controls were unstamped and can legitimately contain several resets, so a missing
    stamp binds to the lifecycle visible at that point in the append-only replay. Modern producers
    always stamp and the HTTP boundary performs CAS before append; an explicit stale stamp is rejected.
    """
    generation = _event_generation(d)
    if generation is _MISSING:
        return True
    return generation is not None and generation == n.attempt


def _node_for_event(st: RunState, d: dict) -> Node | None:
    nid = _coerce_node_id(d)
    return st.nodes.get(nid) if nid is not None else None


def _generation_map_matches(st: RunState, d: dict) -> bool:
    """Validate the whole candidate-generation snapshot carried by a best_confirmed event.
    A confirmation pass spans several nodes; checking only the chosen node would still accept a
    winner computed using a reset competitor's stale seeds. Old events have no map and remain valid."""
    raw = d.get("generations", _MISSING)
    if raw is _MISSING:
        # Legacy best_confirmed (pre-generation-map). Modern producers ALWAYS stamp `generations`
        # (confirm_phase), so this branch is reached only by OLD persisted logs. Validate just the
        # CHOSEN winner: rejecting whenever ANY unrelated node was later aborted/tombstoned would
        # retroactively drop a legitimately-completed confirmation that the pre-batch fold accepted
        # (invariant 5b — an old log must fold as it did before). A winner that is itself
        # aborted/tombstoned is still correctly rejected.
        n = _node_for_event(st, d)
        return n is None or (not n.tombstoned and n.id not in st.aborted_nodes
                             and _generation_matches(n, d))
    if not isinstance(raw, dict):
        return False
    chosen = _coerce_node_id(d)
    seen: set[int] = set()
    for raw_nid, raw_generation in raw.items():
        nid = _coerce_node_id({"node_id": raw_nid})
        generation = _event_generation({"generation": raw_generation})
        if (nid is None or generation in (_MISSING, None)
                or nid not in st.nodes or nid in st.aborted_nodes
                or st.nodes[nid].tombstoned or st.nodes[nid].attempt != generation):
            return False
        seen.add(nid)
    if d.get("node_id") is not None and (chosen is None or chosen not in seen):
        return False
    # A candidate created while confirmation was running was absent from the snapshot and therefore
    # never compared. Do not mark confirmation complete until the snapshot exactly covers the current
    # candidate set (a reset is already caught by the per-entry generation checks above).
    active = {nid for nid, n in st.nodes.items()
              if nid not in st.aborted_nodes and not n.tombstoned}
    return seen == active


def _parent_generation_map_matches(st: RunState, d: dict) -> bool:
    """Atomically bind a derived node to the parent lifecycles used to build it.

    The engine captures this map before a potentially slow Researcher/Developer call. If a reset or
    abort lands before node_created, replay sees the changed parent first and rejects the stale child.
    Historical events may omit the map, but their declared parents must still exist and be active.
    """
    raw = d.get("parent_generations", _MISSING)
    parent_ids = d.get("parent_ids") or []
    if not isinstance(parent_ids, list):
        return False
    expected_parents: set[int] = set()
    for raw_parent in parent_ids:
        pid = _coerce_node_id({"node_id": raw_parent})
        if pid is None:
            return False
        expected_parents.add(pid)
    if raw is _MISSING:
        return all(pid in st.nodes and pid not in st.aborted_nodes
                   and not st.nodes[pid].tombstoned for pid in expected_parents)
    if not isinstance(raw, dict):
        return False
    seen: set[int] = set()
    for raw_pid, raw_generation in raw.items():
        pid = _coerce_node_id({"node_id": raw_pid})
        generation = _event_generation({"generation": raw_generation})
        parent = st.nodes.get(pid) if pid is not None else None
        if (pid is None or generation in (_MISSING, None) or parent is None
                or parent.tombstoned or parent.attempt != generation
                or pid in st.aborted_nodes):
            return False
        seen.add(pid)
    return seen == expected_parents


def _charge_terminal_cost(st: RunState, n: Node, d: dict, ctx: "_FoldCtx") -> None:
    """Charge eval compute once per lifecycle even when its terminal arrives after a reset. Generation
    guards protect state/selection, not the cumulative budget: discarding a metric must not refund the
    process time and make repeated resets a max_eval_seconds bypass."""
    generation = _event_generation(d, legacy_attempt=True)
    if generation is _MISSING:
        # Terminals have carried `attempt` since before lifecycle-wide `generation` stamps were
        # introduced. A truly unstamped terminal is therefore a legacy generation-0 record, not the
        # node's current generation (which could have advanced after a reset). Resolving it to the
        # current value would let one delayed duplicate charge the budget again under a fresh key.
        generation = 0
    # A late result may name an older lifecycle and its real compute still counts. An unknown/future
    # lifecycle is causally impossible, though, and must not be able to poison the budget.
    if generation is None or generation > n.attempt:
        return
    key = (n.id, generation)
    if key not in ctx.charged_terminal_generations:
        ctx.charged_terminal_generations.add(key)
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))


def _on_node_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)                  # tolerate an event for an unknown/missing node
    if n is not None:
        if n.id in st.aborted_nodes:
            _charge_terminal_cost(st, n, d, ctx)
            return
        matches = _attempt_matches(n, d)
        if not matches:
            _charge_terminal_cost(st, n, d, ctx)  # stale metric ignored; real compute still spent
            return
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
            _charge_terminal_cost(st, n, d, ctx)

def _on_node_failed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    nid = _coerce_node_id(d)
    if nid is not None and _building_matches_event(st, d, nid):
        st.building = None
    if n is not None:
        if n.id in st.aborted_nodes and d.get("reason") != "aborted":
            _charge_terminal_cost(st, n, d, ctx)
            return
        matches = _attempt_matches(n, d)
        if not matches:
            _charge_terminal_cost(st, n, d, ctx)
            return
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
            n.rerun_from = None
            n.rerun_stage = None                # any stage-scoped re-run has now landed
            if d.get("failed_stage"):
                n.failed_stage = d.get("failed_stage")   # Phase 1: which pipeline stage broke
            _charge_terminal_cost(st, n, d, ctx)

def _on_node_repaired(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # In-node inline repair (hybrid crash repair): a NON-terminal event that replaces the
    # node's code with the LLM-repaired version BEFORE the eval that follows it. Idempotent
    # and replay-safe: only mutates while the node is still pending (the single terminal
    # event emitted at the end of the repair loop flips status off pending), so a duplicate
    # or post-terminal node_repaired (corrupt/double-fold) is a no-op — mirrors the
    # `first_terminal` guard above. The LLM/subprocess are never re-invoked; the final code
    # and metric/status are reconstructed purely from this event + the terminal event.
    n = _node_for_event(st, d)
    if (n is not None and n.id not in st.aborted_nodes and not n.tombstoned
            and _generation_matches(n, d)
            and n.status is NodeStatus.pending):
        n.code = d.get("code", n.code)
        if d.get("files"):
            n.files = d["files"]
        if d.get("deleted"):
            n.deleted = d["deleted"]

def _requeue_partition_bound_results(st: RunState, *, fresh_node_ids: set[int]) -> None:
    """Make every surviving incumbent comparable on the newly-hidden partition.

    Host grading derives the ordinary search metric *and* every confirmation seed from the
    complement of ``_holdout_idx``.  Rotating that index while retaining those values mixes two
    different datasets in one ranking.  Re-open each evaluated incumbent as a fresh lifecycle so
    the normal eval path materializes its unchanged code on the new complement.  The generation
    bump is essential: it makes late epoch-N workers inert and gives the repeated physical eval its
    own cost-accounting key.  Nodes created/reset by the event that opened this epoch are already
    fresh and are excluded by ``fresh_node_ids``.
    """
    requeued: set[int] = set()
    for n in st.nodes.values():
        if (n.id in fresh_node_ids or n.id in st.aborted_nodes or n.tombstoned
                or n.status is not NodeStatus.evaluated):
            continue
        n.attempt += 1
        n.status = NodeStatus.pending
        n.metric = None
        n.error = ""
        n.error_reason = ""
        n.triage_rationale = ""
        n.stdout_tail = ""
        n.eval_seconds = None
        n.extra_metrics = {}
        n.violations = []
        n.feasible = True
        n.trials = []
        n.confirmed_mean = None
        n.confirmed_std = None
        n.confirmed_seeds = None
        n.holdout_metric = None
        n.generalization_gap = None
        n.verifier_score = None   # R1-c: a soundness score judged the OLD attempt's result — discard it
        n.stages = []
        n.failed_stage = None
        n.rerun_from = None
        n.rerun_stage = None
        requeued.add(n.id)

    if not requeued:
        return
    for nid in requeued:
        st.confirm_seed_results.pop(nid, None)
        st.proxy_scores.pop(nid, None)
    st.proxy_skipped = [nid for nid in st.proxy_skipped if nid not in requeued]
    st.confirm_requests = [nid for nid in st.confirm_requests if nid not in requeued]
    st.confirm_request_generations = [
        r for r in st.confirm_request_generations if r.get("node_id") not in requeued]
    st.ablate_requests = [nid for nid in st.ablate_requests if nid not in requeued]
    st.ablate_request_generations = [
        r for r in st.ablate_request_generations if r.get("node_id") not in requeued]
    st.policy_scores = {}
    st.policy_chosen = None
    st.policy_reason = ""


def _rotate_search_epoch(st: RunState, *, requeue_partition_scores: bool,
                         fresh_node_ids: set[int] | None = None) -> None:
    """Advance one epoch and invalidate every value bound to the disclosed partition."""
    st.search_epoch += 1
    st.holdout_evaluated_ids.clear()
    st.holdout_epoch_aware = False   # the disclosure is consumed; the new epoch has none yet
    for candidate in st.nodes.values():
        if candidate.tombstoned or candidate.id in st.aborted_nodes:
            continue                         # post-hoc audit evidence is not part of the new pool
        candidate.holdout_metric = None
        candidate.generalization_gap = None
    if requeue_partition_scores:
        _requeue_partition_bound_results(st, fresh_node_ids=fresh_node_ids or set())


def _invalidate_disclosed_holdout(
        st: RunState, *, fresh_node_ids: set[int] | None = None) -> bool:
    """Close a disclosed epoch once active search changes again."""
    if not st.holdout_evaluated_ids:
        return False
    # Requeue every incumbent (wiping its metric to force a re-eval on the newly-hidden complement)
    # ONLY when the disclosed holdout was epoch-aware. A legacy (pre-search-epoch) disclosure must
    # rotate WITHOUT the metric wipe, or replaying an old holdout_select log would drop incumbents the
    # pre-batch fold left intact and change the selected best (invariant 5b, F2).
    _rotate_search_epoch(
        st, requeue_partition_scores=st.holdout_epoch_aware, fresh_node_ids=fresh_node_ids)
    return True


def _on_node_tombstoned(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Append-only delete (§6.3): mark the listed node ids (a node + its descendant subtree, computed
    # by the writer so the fold stays a pure, order-tolerant set op) as logically deleted. They REMAIN
    # in st.nodes — so parent links still resolve, node-id allocation never reuses the id, and the
    # delete is reversible/auditable — but the evaluated/feasible/breedable/pending helpers skip a
    # tombstoned node, so it is excluded from best-pick, breeding, confirmation, and re-eval.
    # Idempotent: setting the flag twice (duplicate/overlapping tombstone events) is a no-op. Ids
    # coerced defensively — a forged/unhashable id in a hand-edited log is skipped, not a fold crash.
    affected: set[int] = set()
    # `node_ids` MUST be a list. A forged/hand-edited event with a truthy SCALAR (e.g. {"node_ids": 42})
    # would make `42 or []` -> `42` and `for raw in 42` raise TypeError — and the fold loop has no
    # per-event try/except, so that one bad record bricks EVERY replay/resume/view of the run. Guard the
    # type like `_parent_generation_map_matches` already does for `parent_ids` (fold must stay total).
    raw_ids = d.get("node_ids")
    for raw in (raw_ids if isinstance(raw_ids, list) else []):
        nid = _coerce_node_id({"node_id": raw})
        n = st.nodes.get(nid) if nid is not None else None
        if n is not None and not n.tombstoned:
            n.tombstoned = True
            n.rerun_from = None
            n.rerun_stage = None
            affected.add(n.id)
    if not affected:
        return
    # Remove only references/actions that name deleted lifecycles. A post-hoc delete of an already
    # finished run is an audit edit, not an implicit search reopen: the finish/report/finalization and
    # unaffected node evidence remain intact until an explicit resume creates the next epoch.
    st.confirm_requests = [nid for nid in st.confirm_requests if nid not in affected]
    st.confirm_request_generations = [
        r for r in st.confirm_request_generations if r.get("node_id") not in affected]
    st.ablate_requests = [nid for nid in st.ablate_requests if nid not in affected]
    st.ablate_request_generations = [
        r for r in st.ablate_request_generations if r.get("node_id") not in affected]
    if st.champion in affected:
        st.champion = None
    if st.approval_subject in affected:
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
    if st.approved_node_id in affected:
        st.approved = False
        st.approved_node_id = None
    if st.pause_node_id in affected:
        st.paused = False
        st.pause_node_id = None
        st.pause_generation = None
    if st.building and st.building.get("node_id") in affected:
        st.building = None
    if st.finished:
        if ctx.best_confirmed in affected:
            ctx.best_confirmed = None
        return

    # During an active search the candidate-set mutation invalidates completion certificates. If a
    # holdout was already disclosed, rotate now and re-evaluate every surviving incumbent.
    st.confirmed_done = False
    ctx.best_confirmed = None
    st.approved = False
    st.awaiting_approval = False
    st.approval_subject = None
    st.approval_generation = None
    st.approved_node_id = None
    _invalidate_disclosed_holdout(st)

def _on_node_reset(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Re-run an EXISTING node in place (no new id). Discard its state FROM `from_stage` so it
    # becomes pending again; the engine then re-runs just that stage, appending fresh events for
    # the SAME id (which land as the first-terminal-after-reset). Replay-safe: the reset marks
    # where the old lifecycle is abandoned. `eval` = keep idea+code, just re-score (the normal
    # eval loop picks a pending-with-code node up — no marker). `implement`/`propose` = also drop
    # the code and flag `rerun_from` so the engine re-develops (re-proposes for `propose`).
    n = _node_for_event(st, d)
    if n is not None and not n.tombstoned and _control_generation_matches(n, d):
        was_finished = st.finished
        holdout_was_disclosed = bool(st.holdout_evaluated_ids)
        old_generation = n.attempt
        stage = d.get("from_stage", "eval")
        # Bump the attempt generation (P0-1): the engine stamps this on the re-eval's terminal, and a
        # LATE terminal from the attempt this reset abandons carries the OLD generation and is dropped
        # by `_attempt_matches` — so an in-flight pre-reset eval can't land its metric on the new code.
        n.attempt += 1
        if st.pause_node_id == n.id and st.pause_generation == old_generation:
            st.paused = False
            st.pause_node_id = None
            st.pause_generation = None
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
        n.agent_report = None
        # The PER-SEED confirm memo must reset with the node too: the confirm phase memo-skips
        # every seed already in `confirm_seed_results`, so a stale entry would re-emit
        # node_confirmed from PRE-reset seed metrics for the post-reset code without running a
        # single seed. Pending force-confirm requests are lifecycle-scoped and are cancelled below;
        # completed fulfillment history stays for audit while its generation-aware twin prevents ABA.
        st.confirm_seed_results.pop(n.id, None)
        st.confirm_requests = [queued for queued in st.confirm_requests if queued != n.id]
        st.confirm_request_generations = [
            r for r in st.confirm_request_generations if r.get("node_id") != n.id]
        # Abort/proxy decisions belong to the lifecycle that was active when they were recorded.
        # Keeping them would immediately abort/skip every reset generation forever.
        st.aborted_nodes = [nid for nid in st.aborted_nodes if nid != n.id]
        st.proxy_scores.pop(n.id, None)
        st.proxy_skipped = [nid for nid in st.proxy_skipped if nid != n.id]
        st.ablate_requests = [nid for nid in st.ablate_requests if nid != n.id]
        st.ablate_request_generations = [
            r for r in st.ablate_request_generations if r.get("node_id") != n.id]
        if st.champion == n.id:
            st.champion = None
        ranked = st.hypothesis_ranking or {}
        if (ranked.get("node_id") == n.id
                and _event_generation(ranked) == old_generation):
            st.hypothesis_ranking = None
        n.failed_stage = None
        # Finish-time scores computed on the NOW-discarded code must not survive the reset, or a
        # holdout-gated best pick / generalization-gap audit keeps using a stale number the node
        # can no longer reproduce (holdout is append-only + skips already-scored ids, so it would
        # never be recomputed for this node). R1-c's verifier_score is exactly such a finish-time
        # score (a soundness judgment on the OLD attempt's result) — it must reset too, else the
        # tie-break would rank the new attempt by a score for a realization it no longer produces.
        n.holdout_metric = None
        n.verifier_score = None
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
            # Preserve only stages strictly BEFORE the requested restart boundary. A new lifecycle
            # that fails early must not retain a later-stage success from the abandoned generation.
            for i, prior in enumerate(n.stages):
                if prior.get("name") == stage:
                    n.stages = n.stages[:i]
                    break
            if holdout_was_disclosed:
                # Stage reuse can retain a model trained on the old search complement. A disclosed
                # partition forces a full freshly-materialized eval in the next epoch; source code
                # survives, but no old stage artifact or workdir checkpoint may be reused.
                n.rerun_stage = None
                n.stages = []
        if _building_matches_event(st, d, n.id):
            st.building = None
        # Reset itself clears `finished`, so a later resume cannot observe the old finished edge.
        # Invalidate the completed confirmation/approval epoch here, before clearing it.
        # Requeuing every OTHER incumbent (wiping its metric to force a re-eval on the newly-hidden
        # complement) is a NEW epoch-aware semantic. A legacy unstamped node_reset predates search
        # epochs; firing it there wipes surviving incumbents' metrics that the pre-batch fold left
        # intact — an invariant-5b divergence when replaying an old log. Gate the requeue-all on a
        # modern generation stamp. (A modern generation-0 reset that omits the stamp — allowed only at
        # attempt 0 — likewise skips it: a rare, benign fairness gap, never corruption.) The plain
        # finished-reopen epoch bump below is deliberately NOT gated: a reset is itself the reopen edge
        # and bumps the epoch regardless of stamp (it wipes no incumbent metric — requeue=False).
        reset_is_epoch_aware = _event_generation(d) is not _MISSING
        if holdout_was_disclosed and reset_is_epoch_aware:
            # The target is already a fresh pending generation. Every OTHER active incumbent must
            # also be re-evaluated on the newly-hidden complement; retaining its raw/confirm metric
            # would rank values measured on different partitions in one candidate pool.
            _rotate_search_epoch(
                st, requeue_partition_scores=True, fresh_node_ids={n.id})
        elif was_finished:
            # A reset is itself the actual reopen edge. With no disclosed partition there are no raw
            # scores to invalidate, but confirmation/approval still belong to the prior search epoch.
            _rotate_search_epoch(st, requeue_partition_scores=False)
        st.confirmed_done = False
        # `best_confirmed.generations` covers the whole candidate set. Resetting ANY competitor
        # invalidates the snapshot, even when the previously chosen winner itself was untouched.
        ctx.best_confirmed = None
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
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
    n = _node_for_event(st, d)
    if n is not None and n.id not in st.aborted_nodes and _generation_matches(n, d):
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
    nid = _coerce_node_id(d)
    seed = _coerce_node_id({"node_id": d.get("seed")}) if "seed" in d else None
    keyed = nid is not None and seed is not None
    n = st.nodes.get(nid) if nid is not None else None
    legacy_attempt = "generation" not in d and "attempt" in d
    generation = _event_generation(d, legacy_attempt=True)
    # Fresh master briefly emitted `attempt`; preserve its historical behavior (a stale attempt is
    # fully dropped). Canonical `generation` events use the stricter lifecycle rule below: stale state
    # is inert, but already-spent compute still counts against the budget.
    if legacy_attempt and n is not None and (generation is None or generation != n.attempt):
        return
    # Old logs did not stamp confirm events: bind those to the extant lifecycle visible at that point.
    # Cost is trusted only for an evaluated lifecycle, an intervention-invalidated lifecycle, or an
    # older generation whose worker actually ran before reset. A forged current-generation event on a
    # still-pending node cannot reserve a seed's dedupe key and suppress the later real compute cost.
    resolved_generation = (n.attempt if n is not None else 0) if generation is _MISSING else generation
    chargeable = (n is not None and isinstance(resolved_generation, int)
                  and resolved_generation <= n.attempt
                  and (resolved_generation < n.attempt
                       or n.status is NodeStatus.evaluated
                       or n.id in st.aborted_nodes or n.tombstoned))
    if keyed and chargeable and isinstance(resolved_generation, int):
        cost_key = (nid, resolved_generation, seed)
        if cost_key not in ctx.charged_confirm_seeds:
            ctx.charged_confirm_seeds.add(cost_key)
            _charge_eval_seconds(st, "confirm", d.get("eval_seconds"))
    if (n is None or n.status is not NodeStatus.evaluated
            or n.id in st.aborted_nodes or n.tombstoned):
        return
    if generation is not _MISSING and (
            n is None or generation is None or generation != n.attempt):
        return                    # stale metric/memo ignored; its real cost was charged above
    # Only a KEYED event (node_id+seed) can participate in the per-seed memo that makes the eval-cost
    # add idempotent; an un-keyed confirm_eval has no memo slot, so a duplicate/re-fold would
    # double-count total_eval_seconds (order/duplication-sensitive — the fold must not be). The sole
    # emitter always writes both keys, so this only guards a future/foreign/hand-edited un-keyed event.
    if keyed:                                                # per-seed resume memo (#0)
        st.confirm_seed_results.setdefault(nid, {})[seed] = d.get("metric")

def _on_node_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    if (n is not None and n.status is NodeStatus.evaluated
            and n.id not in st.aborted_nodes and not n.tombstoned
            and _generation_matches(n, d, legacy_attempt=True)):
        # A confirmation REFINES this node's result (more seeds → confirmed_mean) rather than DISCARDING
        # it, so its verifier_score (a soundness judgment on the same experiment) is kept as a reasonable
        # estimate — unlike a node_reset, which discards the result entirely and clears verifier_score.
        # (A confirmed_mean tie that newly emerges is still re-surfaced by _metric_tie_groups, which keys
        # on robust_metric = confirmed_mean, so any UNSCORED confirmed node in the tie is verified.)
        n.confirmed_mean = d.get("mean")
        n.confirmed_std = d.get("std")
        n.confirmed_seeds = d.get("seeds")

def _on_holdout_evaluated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # D1 holdout-gated promotion: the engine re-scored this val-leader's predictions on
    # the FINAL holdout partition the search never saw. Tolerant like node_evaluated:
    # an event for an unknown node (corrupt log) is skipped, and a null metric (missing
    # predictions) records nothing — such a node simply can't win the holdout pick.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is None or n.status is not NodeStatus.evaluated
            or n.id in st.aborted_nodes or n.tombstoned):
        return
    generation = _event_generation(d, legacy_attempt=True)
    if generation is not _MISSING and (
            n is None or generation is None or generation != n.attempt):
        return
    # A prior epoch's holdout was already disclosed; late scores from it cannot enter the newly
    # hidden partition's gate or metric pool. Missing epoch remains legacy-current.
    if d.get("search_epoch", st.search_epoch) != st.search_epoch:
        return
    if "search_epoch" in d:
        # A modern producer stamps `search_epoch` (holdout.py); a legacy holdout_evaluated does not.
        # Record that THIS disclosed holdout carries epoch semantics, so a later candidate change may
        # safely requeue incumbents onto the newly-hidden complement. A legacy (unstamped) disclosure
        # leaves this False, so the requeue-with-metric-wipe stays gated off (invariant-5b, F2).
        st.holdout_epoch_aware = True
    if nid is not None and nid not in st.holdout_evaluated_ids:
        st.holdout_evaluated_ids.append(nid)   # gate: attempted, even if metric is null
    if n is not None and d.get("metric") is not None:
        try:
            n.holdout_metric = float(d["metric"])
        except (TypeError, ValueError):
            pass

def _on_agent_validated(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    n = _node_for_event(st, d)
    if (n is not None and n.id not in st.aborted_nodes
            and _generation_matches(n, d)):   # audit only; never affects selection
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
    # P0-3 manifest: bind the completion to the material it verified (config/workspace/data digest).
    # Additive: absent on old logs -> "" -> resume falls back to the boolean (unchanged behavior).
    if d.get("manifest"):
        st.setup_manifest = str(d.get("manifest"))

def _on_run_setup_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # arch-review §5 P2: a SUCCESSFUL run-level `run_setup` (dep install) is folded (keyed by its
    # command) so a resume skips it instead of re-installing every time — crash-safe exactly-once. A
    # failed/timed-out setup is NOT recorded (the command must actually re-run). Old logs whose
    # run_setup_finished carried no `command` just don't populate the set (setup runs as before).
    if d.get("exit_code") == 0 and not d.get("timed_out") and d.get("command"):
        st.run_setup_done.add(run_setup_key(d.get("command")))

def _on_data_leakage(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.leakage = d

def _on_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    if "after_seq" in d:
        raw_after = d.get("after_seq")
        if isinstance(raw_after, bool):
            return
        try:
            after_seq = int(raw_after)
        except (TypeError, ValueError, OverflowError):
            return
        if e.seq is None or e.seq != after_seq + 1:
            return
    if st.approved:
        return                         # a grant that won the race cannot be re-opened by a stale request
    subject = _coerce_node_id(d)
    node = st.nodes.get(subject) if subject is not None else None
    if node is not None and (node.id in st.aborted_nodes or node.tombstoned):
        return
    generation = _event_generation(d)
    if (subject is not None and generation is not _MISSING
            and (node is None or not _generation_matches(node, d))):
        return
    st.awaiting_approval = True
    # P0-2: record WHICH node the request is for (the engine emits the current best) as audit context,
    # surfaced in the projection so the UI can show what is awaiting approval. This is NOT the grant
    # gate — `_on_approval_granted` binds to node existence, not to this subject (see there).
    st.approval_subject = subject
    st.approval_generation = node.attempt if node is not None else None

def _on_approval_granted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-2 approval gate: honor a grant that names a REAL node in the run — the current best OR an
    # operator-chosen node (`approve --node-id N` / the boss `approve` action both ratify a specific
    # node). A grant for a node that doesn't exist — a forged/typo'd `approval_granted(node_id=999)`, or
    # an unhashable/bool/non-numeric id — is ignored, so it can't globally flip `approved`; the run stays
    # awaiting the real approval. Binding to node EXISTENCE (deliberately NOT to the pending
    # `approval_subject`) closes the forged-id hole while still allowing a legitimate non-best `--node-id`
    # grant. The id is coerced/guarded by `_coerce_node_id` BEFORE the membership test so a forged
    # unhashable id can't raise inside the `in` and brick the fold. Back-compat: a bare grant with no
    # node_id (old logs / a direct grant) is accepted, so legacy HITL runs fold identically.
    if d.get("node_id") is not None:               # a TARGETED grant must name a real, coercible node
        subj = _coerce_node_id(d)
        if subj is None or subj not in st.nodes:
            return                                 # forged / unhashable / non-existent -> ignore
        node = st.nodes[subj]
        if node.id in st.aborted_nodes or node.tombstoned:
            return
        generation = _event_generation(d)
        if generation is not _MISSING and not _generation_matches(node, d):
            return
        st.approved_node_id = subj
    else:
        # Bare grants are legacy. Modern first-party producers always name + generation-stamp a node;
        # accepting this shape is solely persisted-log compatibility.
        st.approved_node_id = st.approval_subject
    st.awaiting_approval = False
    st.approved = True
    st.approval_subject = None
    st.approval_generation = None

def _on_spec_proposed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.proposed_spec = d

def _on_spec_approval_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.spec_approval_requested = True

def _on_spec_approved(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P0-2: ratify only a spec that was actually PROPOSED. A premature/forged `spec_approved` (no
    # preceding `spec_proposed`) would set `spec_confirmed=True` while `proposed_spec` is None,
    # skipping onboarding entirely. The real flow always folds `spec_proposed` first (the engine
    # gates the emit on it), so this only rejects an out-of-order ratification; old logs are
    # unaffected (they always carry the proposal).
    if st.proposed_spec is not None:
        st.spec_confirmed = True

def _on_spec_drift(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    generation = _event_generation(d)
    if generation is not _MISSING:
        n = _node_for_event(st, d)
        if n is None or n.id in st.aborted_nodes or not _generation_matches(n, d):
            return
    st.drifts.append(d)                         # audit only; metric already discarded

def _on_workspace_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.workspace_changed = True                 # resume saw the source repo/data change


def _on_env_changed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.env_changed = True                       # resume saw the Python/lib environment drift (F18)

def _on_diversity_archive(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.archive = d

def _on_coverage_snapshot(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.coverage_snapshots.append(d)   # audit-only breadth curve; the at_node gate dedups on resume


def _on_concept_coverage_snapshot(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # PART IV Phase 2a: audit-only concept-graph coverage / uncovered-region curve; the at_node gate
    # dedups on resume. NEVER touches selection (mirrors _on_coverage_snapshot).
    st.concept_coverage_snapshots.append(d)

def _on_llm_cost(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.llm_cost = d

def _on_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    pid = _coerce_node_id(d, "parent_id")
    n = st.nodes.get(pid) if pid is not None else None
    generation = _event_generation(d)
    resolved_generation = (n.attempt if n is not None else 0) if generation is _MISSING else generation
    valid = (generation is _MISSING
             or (n is not None and isinstance(resolved_generation, int)
                 and resolved_generation <= n.attempt))
    if pid is None or not valid or not isinstance(resolved_generation, int):
        return
    record = dict(d)
    record["parent_id"] = pid
    record.setdefault("generation", resolved_generation)
    st.ablations.append(record)   # historical audit; consumers/gates key it by lifecycle generation
    # Account the ablation probes' eval wall-clock against the cumulative budget (arch-review §4 P1-2:
    # ablation was wholly outside accounting, so a run could spend well past max_eval_seconds on
    # probes). Additive + reader-defaulted: old ablate events carry no eval_seconds -> +0.0.
    ablation_id = d.get("ablation_id")
    # New emitters identify one physical probe operation, so a duplicated append is idempotent while
    # two legitimate cadence runs on the same parent/generation both count. Legacy events had no id and
    # are therefore charged individually; collapsing them by parent would undercount real repeated work.
    if not isinstance(ablation_id, str) or not ablation_id:
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))
    elif ablation_id not in ctx.charged_ablation_ids:
        ctx.charged_ablation_ids.add(ablation_id)
        _charge_eval_seconds(st, "node", d.get("eval_seconds"))

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
    n = _node_for_event(st, d)
    generation = _event_generation(d)
    if generation is not _MISSING and (
            n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)):
        return
    st.hypothesis_ranking = d

def _on_rung_promoted(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.rungs.append({"rung": d.get("rung"), "survivors": d.get("survivors", [])})

def _on_agent_decision(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # Self-driving unified agent (audit-only): records WHICH legal macro action the agent
    # chose and why. NEVER drives selection — the effect is the subsequent node_created,
    # folded as usual. Additive & non-load-bearing: an old log without it folds identically.
    st.agent_decisions.append(d)

def _on_reward_hack_suspected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is not None and n.id in st.aborted_nodes:
        return
    generation = _event_generation(d)
    if generation is not _MISSING and (n is None or not _generation_matches(n, d)):
        return
    record = {"node_id": nid, "signals": d.get("signals", []),
              "evidence_version": d.get("evidence_version", 0),
              "code_digest": d.get("code_digest")}
    if n is not None:
        record["generation"] = n.attempt
    st.reward_hacks.append(record)

def _on_foresight_selected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FOREAGENT predict-before-execute pick (audit-only). Kept so the world model can be
    # primed with its OWN calibration (did the picked node beat its parent?), closing the
    # predict→outcome loop. Store only the small fields the scoreboard needs; never selection.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    generation = _event_generation(d)
    if generation is not _MISSING and (
            n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)):
        return
    if nid is not None:
        record = {"node_id": nid, "confidence": d.get("confidence")}
        if generation is not _MISSING:
            record["generation"] = generation
        st.foresight_selected.append(record)

def _on_novelty_rejected(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.novelty_events.append(d)   # E1: a near-duplicate proposal nudged off (audit)

def _on_novelty_graded(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    st.novelty_grades.append(d)   # D3: a graded-ALLOW (level-4/5) the flat gate would reject (audit)

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
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is not None and n.id in st.aborted_nodes:
        return
    generation = _event_generation(d)
    if generation is not _MISSING and (n is None or not _generation_matches(n, d)):
        return
    if nid is not None and d.get("score") is not None:
        st.proxy_scores[nid] = d["score"]
    if d.get("skipped") and nid is not None and nid not in st.proxy_skipped:
        st.proxy_skipped.append(nid)

def _on_node_verified(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # R1-c: freeze a node's calibrated §12-verifier soundness score (the LLM output can't be recomputed
    # in the deterministic fold). Generation-scoped exactly like proxy_scored: a score computed against a
    # reset-abandoned attempt (stale generation) is dropped, so a stale-attempt verification can't bias
    # selection. Audit sidecar — read ONLY as a metric-tie-break in _select_best; never a raw override.
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if n is None or n.id in st.aborted_nodes:
        return
    # node_verified is a BRAND-NEW selection-affecting event — no legacy log carries it, and the engine
    # always stamps `generation` (n.attempt) at emit — so REQUIRE the stamp (reject a missing OR mismatched
    # generation) rather than accept-a-missing-one as current. A forged/hand-edited unscoped score can't
    # then bias selection; this is strictly tighter than the additive-legacy pattern the older per-node
    # events must keep for their pre-generation logs.
    if _event_generation(d) is _MISSING or not _generation_matches(n, d):
        return
    score = d.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and 0.0 <= float(score) <= 1.0:
        n.verifier_score = float(score)

def _on_best_confirmed(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # R1 epoch identity: a confirmation certificate authorizes selection state (confirmed_done + the
    # confirm-override in _select_best), so it must be bound to the candidate-set epoch it was computed
    # against. A best_confirmed STAMPED with a stale epoch — e.g. an in-flight confirm pass that appends
    # AFTER a cross-writer reopen bumped search_epoch — is rejected, so an epoch-(N-1) confirmation can't
    # authorize state a fresh epoch N must re-decide. Additive/reader-defaulted: a missing stamp (legacy
    # logs / manual events) is treated as legacy-current, so old logs fold byte-identically. The
    # requeuing-reopen case is already caught by _generation_map_matches; this closes the NON-requeuing
    # reopen (no disclosed holdout), which leaves generations unchanged but still bumps the epoch.
    if "search_epoch" in d and d.get("search_epoch") != st.search_epoch:
        return
    if not _generation_map_matches(st, d):
        return
    nid = _coerce_node_id(d)
    ctx.best_confirmed = nid if "node_id" in d else ctx.best_confirmed
    st.confirmed_done = True   # the confirmation phase ran to completion

def _on_run_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    accepted_after_seq: int | None = None
    if "after_seq" in d:
        raw = d.get("after_seq")
        if isinstance(raw, bool):
            return
        try:
            after_seq = int(raw)
        except (TypeError, ValueError, OverflowError):
            return
        if e.seq is None or e.seq != after_seq + 1:
            return                    # an external event won the decision→finish race
        accepted_after_seq = after_seq
    pending = ctx.pending_finish_report
    if pending is not None:
        report_seq, report_index, report = pending
        # Modern events bind the report seq into run_finished.after_seq. Historical emitters had no
        # CAS payload, so accept only a physically adjacent report->finish pair. An intervening event,
        # including an unknown forward-compatible one, leaves the provisional narrative unpublished.
        modern_adjacent = accepted_after_seq is not None and report_seq == accepted_after_seq
        legacy_adjacent = (accepted_after_seq is None
                           and ctx.event_index == report_index + 1)
        if modern_adjacent or legacy_adjacent:
            st.report = report
        ctx.pending_finish_report = None
    st.finished = True
    if e.seq is not None:
        st.last_finish_seq = e.seq
        # Recovery is explicitly opted into by modern finish events. Markerless historical finishes
        # were already complete before this protocol existed and must never become synthetic work.
        if not bool(d.get("finalization_required", False)):
            st.finalized_finish_seq = e.seq
    st.stop_reason = d.get("reason")
    # Drop any dangling "building" marker: if a dev session died mid-build (no node_created /
    # node_failed) the marker would otherwise persist, and the UI would show a breathing
    # "building…" card + a false "working" pulse on a run that is over.
    st.building = None


def _on_finalization_finished(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    raw = d.get("finish_seq")
    if isinstance(raw, bool):
        return
    try:
        finish_seq = int(raw)
    except (TypeError, ValueError, OverflowError):
        return
    if st.finished and finish_seq == st.last_finish_seq:
        st.finalized_finish_seq = finish_seq

def _on_resume_or_run_reopened(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # RESUME (the one operator "continue"): lift EVERY stopped state so re-entering the loop
    # keeps going — whether the run was PAUSED (stop, no finalize), ABORTED (finalize →
    # stop_requested → run_finished), or naturally FINISHED (budget exhausted, then reopened
    # with more budget). Clears paused + finished + stop_requested + stop_reason. Deterministic
    # under replay — a later run_finished simply sets `finished` again. EV_RUN_REOPENED is the
    # legacy alias of RESUME (kept so old logs + the UI's reopen path fold identically); the two
    # 3-verb operator controls are `stop` (EV_PAUSE) and `finalize` (EV_RUN_ABORT).
    #
    # P0-2 search epoch: reopening a run that had already FINISHED (its confirmation/approval
    # promotion completed for the prior candidate set) begins a NEW search epoch. Any nodes added
    # after the reopen are a fresh candidate set, so the prior COMPLETION gates must not carry over:
    # clear `confirmed_done` (so the confirm phase re-runs and can confirm a better new candidate —
    # already-confirmed nodes are cheaply reused via their memoized `confirmed_mean`) and re-open
    # approval (so the possibly-new best is re-ratified rather than inheriting the old grant). A
    # resume from a mere PAUSE (finished never set) is the SAME epoch and leaves these gates intact.
    # Checked BEFORE clearing `finished` below. Back-compat: old logs without a reopen-after-finish
    # keep search_epoch=0 and fold identically.
    if st.finished or st.holdout_evaluated_ids:
        if st.holdout_evaluated_ids:
            # F2: requeue-with-metric-wipe only for an epoch-aware (modern) disclosure; a legacy
            # holdout log rotates without wiping surviving incumbents (invariant 5b).
            _rotate_search_epoch(st, requeue_partition_scores=st.holdout_epoch_aware)
        else:
            _rotate_search_epoch(st, requeue_partition_scores=False)
        # A reopen begins a new candidate epoch, so the prior epoch's confirmation certificate must not
        # keep authorizing selection. Clear BOTH the folded flag AND the threaded ctx.best_confirmed the
        # `_select_best` confirm-override reads — every other invalidation site (node_reset, tombstone,
        # new-candidate) pairs these two, and omitting the ctx clear here let an epoch-(N-1) certificate
        # keep overriding epoch-N's metric winner after confirmed_done reset.
        st.confirmed_done = False
        ctx.best_confirmed = None
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
        # P0-2 freshly-hidden per-epoch holdout: the prior epoch's holdout was DISCLOSED at the
        # finish (its scores drove the champion pick), so the reopened epoch must NOT re-score its
        # new candidates on that same partition — the engine rebuilds `_holdout_idx` for the new
        # epoch (a different, never-disclosed split). Clear the gate + the now-stale holdout metrics
        # so the holdout phase re-runs and re-scores every current leader on the fresh split (keeping
        # the champion comparable on ONE holdout). New holdout_evaluated events carry the new epoch;
        # a late one stamped with the prior epoch is dropped by the epoch guard in _on_holdout_evaluated.
    st.paused = False
    st.pause_node_id = None
    st.pause_generation = None
    st.finished = False
    st.stop_reason = None
    st.stop_requested = None

# --- live operator control events (UI intervention). Intent only; the engine reads
# these and writes the matching domain effect. Deterministic under replay. ---
def _on_resume_requested(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1-1 durable resume intent: record the request seq + time. A request seq newer than the last
    # `resume_served` (below) is an unfulfilled resume the reconciler re-spawns. Monotonic by seq, so a
    # duplicate/out-of-order fold is idempotent; the ts is the request event's own recorded time.
    if e.seq > st.last_resume_request_seq:
        st.last_resume_request_seq = e.seq
        st.last_resume_request_ts = float(getattr(e, "ts", 0.0) or 0.0)
        mode = d.get("mode")
        if mode in ("resume", "finalize"):
            st.last_resume_request_mode = mode
        elif not d.get("launch_claim"):
            # A real legacy request means ordinary resume. A claim-only record is transport metadata
            # and must preserve the pending intent's mode (especially finalize).
            st.last_resume_request_mode = "resume"
    if d.get("launch_claim") and e.seq > st.last_resume_launch_seq:
        st.last_resume_launch_seq = e.seq
        st.last_resume_launch_ts = float(getattr(e, "ts", 0.0) or 0.0)

def _on_resume_served(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # P1-1: the engine acquired the singleton lock and is driving the loop -> every resume requested
    # before this seq is fulfilled. Seq-gated so one serve satisfies several piled-up requests.
    if e.seq > st.last_resume_served_seq:
        st.last_resume_served_seq = e.seq
        if st.finished and st.last_resume_request_mode == "finalize":
            # A finalize hand-off that arrived after run_finished repairs/acknowledges the existing
            # wrap-up; it must not create a second finish. Consume its lingering stop intent once the
            # finalize-mode CLI actually owns the singleton lock.
            st.stop_requested = None

def _on_run_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # FINALIZE: the loop turns stop_requested into a run_finished (which runs the end-of-run
    # finalization — report/lessons/case/cost). A bare `stop` uses EV_PAUSE instead (no finalize).
    st.stop_requested = d.get("reason", "operator")
    if e.seq is not None:
        st.last_stop_request_seq = e.seq

def _on_pause(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # STOP: freeze WITHOUT finalizing (finalize.py gates the wrap-up on `finished`, which a pause
    # never sets). A later `finalize` (EV_RUN_ABORT) can still wrap it up; RESUME lifts it.
    if d.get("node_id") is not None:
        # A human STOP is stronger than the scoped developer-crash circuit breaker. If the operator
        # paused while a build was still failing, the later automatic pause must not take ownership:
        # node reset/abort may clear only an auto-pause, never the explicit operator stop.
        if st.paused and st.pause_node_id is None:
            return
        nid = _coerce_node_id(d)
        n = st.nodes.get(nid) if nid is not None else None
        if (n is None or n.id in st.aborted_nodes or not _generation_matches(n, d)
                or n.status is not NodeStatus.failed or n.error_reason != "developer_crash"):
            return
        st.pause_node_id = nid
        st.pause_generation = n.attempt
    else:
        st.pause_node_id = None
        st.pause_generation = None
    st.paused = True

def _on_node_abort(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    legacy_unknown = n is None and _event_generation(d) is _MISSING
    if (nid is not None
            and (legacy_unknown or (n is not None and _control_generation_matches(n, d)))
            and nid not in st.aborted_nodes):
        st.aborted_nodes.append(nid)
        if n is not None:
            n.rerun_from = None
            n.rerun_stage = None
        if _building_matches_event(st, d, nid):
            st.building = None
        if st.pause_node_id == nid:
            st.paused = False
            st.pause_node_id = None
            st.pause_generation = None
        st.ablate_requests = [queued for queued in st.ablate_requests if queued != nid]
        st.ablate_request_generations = [
            r for r in st.ablate_request_generations if r.get("node_id") != nid]
        st.confirm_requests = [queued for queued in st.confirm_requests if queued != nid]
        st.confirm_request_generations = [
            r for r in st.confirm_request_generations if r.get("node_id") != nid]
        if st.approval_subject == nid or st.approved_node_id == nid:
            st.awaiting_approval = False
            st.approved = False
            st.approval_subject = None
            st.approval_generation = None
            st.approved_node_id = None
        if st.champion == nid:
            st.champion = None
        if st.finished:
            if ctx.best_confirmed == nid:
                ctx.best_confirmed = None
            return
        st.confirmed_done = False
        ctx.best_confirmed = None
        st.approved = False
        st.awaiting_approval = False
        st.approval_subject = None
        st.approval_generation = None
        st.approved_node_id = None
        _invalidate_disclosed_holdout(st)

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
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        st.confirm_requests.append(nid)
        st.confirm_request_generations.append({"node_id": nid, "generation": n.attempt})
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        st.confirm_requests.append(nid)   # legacy queued-before-create intent

def _on_force_ablate(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        st.ablate_requests.append(nid)
        st.ablate_request_generations.append({"node_id": nid, "generation": n.attempt})
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        st.ablate_requests.append(nid)    # legacy queued-before-create intent

def _on_fork(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d, "from_node_id")
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and not n.tombstoned and nid not in st.aborted_nodes
            and _control_generation_matches(n, d)):
        record = dict(d)
        record["from_node_id"] = nid
        record.setdefault("generation", n.attempt)
        st.fork_requests.append(record)
    elif (nid is not None and nid not in st.aborted_nodes and n is None
          and _event_generation(d) is _MISSING):
        st.fork_requests.append(dict(d))  # legacy queued-before-create intent

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
    content = d.get("content") or d
    if d.get("trigger") == "finish":
        # Publish only if the immediately-adjacent run_finished accepts this report's CAS chain.
        ctx.pending_finish_report = (e.seq, ctx.event_index, content)
        return
    st.report = content

def _on_confirm_done(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)   # forced-confirm finished for this node (gate; selection untouched)
    n = st.nodes.get(nid) if nid is not None else None
    if (n is not None and nid not in st.aborted_nodes and _generation_matches(n, d)
            and nid not in st.confirmed_forced):
        st.confirmed_forced.append(nid)
    if n is not None and nid not in st.aborted_nodes and _generation_matches(n, d):
        key = {"node_id": nid, "generation": n.attempt}
        if key not in st.confirmed_forced_generations:
            st.confirmed_forced_generations.append(key)

def _on_annotation(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    # `annotation` is a sanctioned /control event appended VERBATIM, and `annotations` is keyed by int
    # node id (dict[int, list[str]]) — so a forged `{"node_id":[999]}` would make `setdefault` hash the
    # unhashable list and raise TypeError, bricking the fold (same class as the approval grant above).
    # `_coerce_node_id` guards the key (reject bool / unhashable / non-coercible) so it can never raise; a
    # null/garbage id simply drops the note.
    nid = _coerce_node_id(d)
    if nid is None:
        return
    st.annotations.setdefault(nid, []).append(d.get("text", ""))

def _on_promote(st: RunState, e: Event, d: dict, ctx: "_FoldCtx") -> None:
    nid = _coerce_node_id(d)
    n = st.nodes.get(nid) if nid is not None else None
    legacy_unknown = (n is None and nid not in st.aborted_nodes
                      and _event_generation(d) is _MISSING)
    if legacy_unknown or (n is not None and not n.tombstoned and nid not in st.aborted_nodes
                          and _control_generation_matches(n, d)):
        st.promotions.append(d)
        if d.get("alias", "champion") == "champion":
            st.champion = nid

# The dispatch registry — event type -> handler. Unknown types are absent: they no-op.
_HANDLERS = {
    EV_RUN_STARTED: _on_run_started,
    EV_TRUST_GATE_CHANGED: _on_trust_gate_changed,
    EV_NODE_BUILDING: _on_node_building,
    EV_NODE_CREATED: _on_node_created,
    EV_NODE_EVALUATED: _on_node_evaluated,
    EV_NODE_FAILED: _on_node_failed,
    EV_NODE_REPAIRED: _on_node_repaired,
    EV_NODE_TOMBSTONED: _on_node_tombstoned,
    EV_RESUME_REQUESTED: _on_resume_requested,
    EV_RESUME_SERVED: _on_resume_served,
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
    EV_RUN_SETUP_FINISHED: _on_run_setup_finished,
    EV_DATA_LEAKAGE: _on_data_leakage,
    EV_APPROVAL_REQUESTED: _on_approval_requested,
    EV_APPROVAL_GRANTED: _on_approval_granted,
    EV_SPEC_PROPOSED: _on_spec_proposed,
    EV_SPEC_APPROVAL_REQUESTED: _on_spec_approval_requested,
    EV_SPEC_APPROVED: _on_spec_approved,
    EV_SPEC_DRIFT: _on_spec_drift,
    EV_WORKSPACE_CHANGED: _on_workspace_changed,
    EV_ENV_CHANGED: _on_env_changed,
    EV_DIVERSITY_ARCHIVE: _on_diversity_archive,
    EV_COVERAGE_SNAPSHOT: _on_coverage_snapshot,
    EV_CONCEPT_COVERAGE_SNAPSHOT: _on_concept_coverage_snapshot,
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
    EV_NOVELTY_GRADED: _on_novelty_graded,
    EV_NODE_VERIFIED: _on_node_verified,
    EV_HYPOTHESIS_MERGED: _on_hypothesis_merged,
    EV_HYPOTHESIS_ADDED: _on_hypothesis_added,
    EV_HYPOTHESIS_UPDATED: _on_hypothesis_updated,
    EV_PROXY_SCORED: _on_proxy_scored,
    EV_BEST_CONFIRMED: _on_best_confirmed,
    EV_RUN_FINISHED: _on_run_finished,
    EV_FINALIZATION_FINISHED: _on_finalization_finished,
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
    for index, e in enumerate(events):
        ctx.event_index = index
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
    # R1/SearchFitness: the eligibility predicate, the ranked-scalar keys and the direction chooser are
    # OWNED by core.fitness.SearchFitness — one spelling shared with rank_by_metric / holdout_topk, so a
    # later scored tie-break (R1-c) composes in exactly one place. Byte-identical to the inlined logic.
    fit = SearchFitness(st.direction, verifier_tiebreak=st.select_verifier_tiebreak)
    evaluated = [n for n in st.evaluated_nodes() if fit.eligible(n, flagged, st.aborted_nodes)]
    if evaluated:
        # If any node has been confirmed (multi-seed), the final answer must be the
        # robust winner: rank confirmed nodes by confirmed_mean. With no confirmations
        # this is identical to ranking all evaluated nodes by their single metric.
        # R1-c: promotion_key adds a calibrated-verifier tie-break slot when select_verifier_tiebreak is
        # on — it resolves metric-EQUAL contests only, never overriding a strictly-better robust_metric.
        confirmed = [n for n in evaluated if n.confirmed_mean is not None]
        pool = confirmed if confirmed else evaluated
        st.best_node_id = fit.best(pool, key=fit.promotion_key).id

    # The variance-gated confirmation decision (I10) overrides the mean-based pick — but never
    # past the feasibility gate (#5): a constraint-violating node must not become best even if
    # the confirm phase ran on it (the mean-based pick above already excluded infeasibles).
    # The confirm certificate is the confirm phase's OWN authoritative winner (robust_selection over the
    # multi-seed means + a significance test), so it overrides the mean pick — including its verifier
    # tie-break. Scope boundary (by design): among confirmed nodes that tie on confirmed_mean the winner
    # is the confirm phase's choice, not the verifier's; the verifier tie-break applies to the mean pick
    # and the holdout pick (both rankings HERE), not to which node the confirm phase certified.
    if (best_confirmed is not None and best_confirmed in st.nodes
            and st.nodes[best_confirmed].status is NodeStatus.evaluated
            and not st.nodes[best_confirmed].tombstoned
            and st.nodes[best_confirmed].robust_metric is not None
            and st.nodes[best_confirmed].feasible
            and best_confirmed not in flagged and best_confirmed not in st.aborted_nodes):
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
            # holdout_key carries the SAME verifier tie-break slot (when select_verifier is on): a tie on
            # the unseen-signal holdout metric is broken by soundness too, so the stronger holdout signal
            # decides first and the verifier only resolves a holdout tie (never overrides it).
            st.best_node_id = fit.best(hpool, key=fit.holdout_key).id

    # An explicit human approval of a real non-best node is a selection decision, not a global latch
    # that authorizes publication of some OTHER algorithmic best. Honor it last; if the chosen node is
    # no longer eligible, invalidate the grant so the engine asks again instead of finalizing another.
    if st.approved and st.approved_node_id is not None:
        chosen = st.nodes.get(st.approved_node_id)
        if (chosen is not None and chosen.status is NodeStatus.evaluated and not chosen.tombstoned
                and chosen.feasible
                and chosen.robust_metric is not None and chosen.id not in flagged
                and chosen.id not in st.aborted_nodes):
            st.best_node_id = chosen.id
        else:
            st.approved = False
            st.approved_node_id = None

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
