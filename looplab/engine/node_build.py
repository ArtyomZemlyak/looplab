"""Node-building helpers (idea -> code -> `node_created` payload) — extracted from
orchestrator.py as a MIXIN: `class Engine(NodeBuildMixin, …)` inherits these methods unchanged,
so there is ZERO call-site churn and `self` here IS the engine. Verbatim moves; several are
exercised on bare `Engine.__new__(Engine)` instances by tests, which a mixin preserves.

THE BUILD SPINE JOINED THEM in ENG1-04 step 4c (review 2026-09-22): `_create_node` ->
`_create_node_scoped` -> `_commit_built_node`, the pooled and guarded fan-out builds, the offload to a
worker (`_offload_build`, `_reserve_on_main_task`), the batch-proposal consumption, the node-reset
rebuild (`_rerun_node`) and the operator inject (`_create_injected_node`), with the module-level names
only they read (`parent_generations_current`, `stamp_proposal_span`, `_OFFLOADED_BUILD` and the two
plan records). They had stayed in orchestrator.py because they call the module-global `fold`, which
tests monkeypatch THROUGH the orchestrator module (`monkeypatch.setattr(orch, "fold", …)`), so moving
them would have silently detached that seam; since ENG1-04 step 0 every engine file folds through
`engine/shared.py::engine_fold`, which resolves `orchestrator.fold` at call time, and that reason no
longer binds. `_activate_spec` (the Phase-3 spec gate, run every loop turn) stays there.

Agent-facing deps (`legal_actions`, `_state_brief`, `render_hint_directives`) stay lazy,
method-local imports so monkeypatching through their source modules keeps working."""
from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import NamedTuple, Optional

import anyio

from looplab.agents.role_wrappers import audit_extra_of
from looplab.agents.roles import (DeveloperResult, developer_call_lock,
                                  researcher_budget_exhausted)
from looplab.core.containment import contain
from looplab.core.errors import budget_stop_leaf
from looplab.core.llm import BudgetExceeded, model_override
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import (Idea, Node, NodeStatus, RunState, durable_idea_payload,
                                 normalize_researcher_footprint, is_developer_error,
                                 is_developer_stuck)
from looplab.engine.audit import AuditMixin
from looplab.engine.card_reservation import (_BuildReservation, discarded_proposal_receipt,
                                             scored_anchor)
from looplab.engine.costs import bind_cost_accountants, find_cost_accountants, seed_prior_spend
from looplab.engine.lessons_priors import LESSON_ROLE_DEVELOPER
from looplab.engine.plan import META_SWEEP
from looplab.engine.proposal_cues import normalize_steering_context
# Through the ENGINE's fold seam, not `replay.fold` directly — see `shared.py::engine_fold`. The
# build spine's re-folds are what `monkeypatch.setattr(orch, "fold", …)` is written to intercept.
from looplab.engine.shared import engine_fold as fold
from looplab.events.eventstore import retry_tail_cas
from looplab.events.types import (EV_AGENT_DECISION, EV_CARD_ADDED, EV_NODE_BUILDING,
                                  EV_NODE_CREATED, EV_NODE_FAILED, EV_NOVELTY_REJECTED, EV_PAUSE,
                                  PROGRESS_STAGE_BUILD)
from looplab.search.operators import merge_idea
from looplab.search.policy import DEFAULT_MODEL_ARM, KIND_EXPAND, META_MODEL

_LOG = logging.getLogger(__name__)

# --- the Developer-crash transaction (doc 25 EC-03) -------------------------------------------
#
# A Developer that cannot finish returns its error IN BAND as the node's code. Left pending, the
# eval then runs the PARENT's carried-over entrypoint and inherits the PARENT's metric — a false
# success that pollutes the search (the 401-window nodes 50-54 each faked the parent's 0.81 this
# way). So the node is FAILED now, and the run is PAUSED: a Developer that could not finish even
# after the LLM client's own within-call retries (429 / 5xx / throttle-403 all back off and retry)
# has hit a problem a NEW node cannot fix, so rapid-firing more dead nodes is the wrong response —
# the 403 blowout spun 67 of them.
#
# The pair was spelled out at five sites, and the reason strings had already drifted apart. What is
# shared is the RECORDS: the two event types, their order (terminal first — a pause naming a node
# that has no terminal reads as an operator freeze), and every field name and default. What is NOT
# shared, deliberately, is how each site APPENDS them, because those genuinely differ and the
# difference is load-bearing:
#
#   * the speculation sites append both under one tail CAS, so a concurrent operator control lands
#     wholly before or wholly after the pair;
#   * `_create_node`'s fan-out runs in a WORKER thread, where EV_PAUSE is a run-GLOBAL folded event
#     outside invariant #1's worker seam — it queues the pause via `_request_create_pause` and the
#     MAIN task appends it after the join, because a worker's byte position relative to a
#     concurrent EV_RESUME is nondeterministic;
#   * the two serial sites of the build spine (`_rerun_node`, `_create_injected_node` — both in this
#     module since ENG1-04 step 4c) append sequentially on the main task.
#
# Unifying those onto one CAS discipline would be an improvement rather than preservation, and doc
# 25 marks it as a separate decision item.


def developer_crash_records(node_id: int, generation: int, code: str, pause_reason: str,
                            *, terminal: bool = True) -> list:
    """The `(node_failed, pause)` pair for a Developer that returned the crash sentinel.

    `pause_reason` is the caller's — the sites describe genuinely different situations (a fresh
    build, a rebuild before GPU dispatch, an operator inject, a recovery sweep) and an operator
    reading the pause needs to know which. Everything else is fixed here.

    `terminal=False` omits the `node_failed` and returns the pause alone. That is the recovery
    branch in `_close_developer_sentinel_once`: a legacy writer (or a crash in the old two-append
    path) can leave the sentinel already terminal with only its pause lost, and re-appending a
    second terminal would violate the one-terminal-per-node invariant.
    """
    records = []
    if terminal:
        records.append((EV_NODE_FAILED, {
            "node_id": node_id, "generation": generation,
            "error": code, "reason": "developer_crash", "eval_seconds": 0.0,
        }))
    records.append((EV_PAUSE, {
        "node_id": node_id, "generation": generation, "reason": pause_reason,
    }))
    return records


def developer_crash_rank(state: RunState, node_id: int) -> int:
    """This crash's 1-based position among the run's Developer-crash terminals, in log order.

    THE COUNT IS THE LOG'S, NOT A PROCESS COUNTER'S. `Settings.developer_crash_pause_after` bounds
    what a RUN has spent on dead Developer sessions, and a run outlives its process: a counter on
    the engine restarts at zero on every `resume`, and — worse — the recovery sweep
    (`speculation.py::_close_developer_sentinel_once`) runs every turn and reads the log to find a
    crash terminal that owns no pause. Under any threshold above one, a below-threshold crash is
    EXACTLY that shape by design, so the sweep and the live decision have to agree on which crashes
    owe a pause, and the only record both can read is the terminal's own position in the log.

    Rank of a node that already carries its `developer_crash` terminal is `1 + the number of such
    terminals before it`; for a node whose terminal is NOT in `state` yet (the callers that decide
    BEFORE appending — the speculation lane's tail-CAS plan and the recovery sweep's pending branch)
    it is the rank the terminal WOULD take if appended now. A node reset out of its crash (its
    current lifecycle is no longer a `developer_crash` terminal) no longer counts, which is the
    right reading: the operator already dealt with that one.
    """
    crashes = [
        node for node in state.nodes.values()
        if node.status is NodeStatus.failed
        and node.error_reason == "developer_crash"
        and type(node.terminal_event_seq) is int
    ]
    own = next((node for node in crashes if node.id == node_id), None)
    if own is None:
        return len(crashes) + 1
    return 1 + sum(1 for node in crashes if node.terminal_event_seq < own.terminal_event_seq)


# Sentinel for `_emit_node_created`'s optional payload keys (moved with its only user):
# distinguishes "key not passed" (the key is OMITTED from the event, matching each call site's
# historical payload shape) from a REAL value, including None (e.g. `research_origin=None`
# must still be emitted).
_OMIT = object()
# The spelling the build spine read it by in `orchestrator.py` (an import alias there), kept so
# the moved bodies stay byte-identical: one sentinel, two names.
_OMIT_ARM = _OMIT


def accepts_co_parents(fn) -> bool:
    """Whether a Developer's `implement_from` takes the `co_parents` keyword (doc 52 row 18) — a
    named parameter or `**kwargs`. The probe is what lets the engine hand an ensemble's other
    lineages to a Developer that can read them and call every other one exactly as before."""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "co_parents" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class _RerunCardCommit(NamedTuple):
    """What a node-reset re-proposal's main-task Card commit decided (`Engine._commit_rerun_card`).

    `outcome` is "reserved" (the swap and the claim landed as one batch), "stale" (the node's own
    lifecycle or a parent moved while the Researcher worked — nothing written), "rejected" (the
    proposal cannot form a native Card — the reservation was closed `proposal_rejected`) or "lost"
    (the tail CAS never settled — nothing written, the reset stays pending for the next turn)."""

    outcome: str
    state: Optional[RunState]
    plan: object


class _InjectedNodePlan(NamedTuple):
    """Pure, bounded preparation result for one operator-authored Node request."""

    idea: Idea
    parent_ids: list[int]
    parent_generations: dict[str, int]
    code: Optional[str]
    implementation_ref: Optional[str]


def parent_generations_current(state, parent_generations) -> bool:
    """Is every parent this build was reserved against still the exact lifecycle it named?

    The same four clauses were spelled at three creation sites (doc 25 ES-02) — twice as an
    affirmative `all(...)` and once as a NEGATED `any(...)` — so the three copies could drift on
    what "the parents are still there" means with every test staying green. Each clause is
    load-bearing: the parent must still exist, still be on the generation the reservation named (a
    reset while we built makes this build a child of a lifecycle that no longer exists), and be
    neither tombstoned nor aborted. An empty mapping is vacuously current, which is what a seed
    build wants.
    """
    return all(
        pid in state.nodes
        and state.nodes[pid].attempt == generation
        and not state.nodes[pid].tombstoned
        and pid not in state.aborted_nodes
        for pid, generation in ((int(pid), gen) for pid, gen in parent_generations.items())
    )


def stamp_proposal_span(span, idea, *, node_id=None) -> None:
    """Bind a Researcher `propose` operation to the CARD it produced.

    THE LINK DID NOT EXIST. The product model is that the Researcher works per CARD (a hypothesis)
    while the Developer works per NODE, and one card can carry several nodes. Nothing recorded that:
    measured on `runs/rubert-dr-0807` (2026-08-11), all 15 `propose` spans carry an EMPTY attribute
    map, no span anywhere in the run carries a card id, and every `card_added` / `card_build_*` /
    `card_enriched` event has `trace_id: None`. So "show me the research behind this card" had no
    join to answer it — not through the spans, not through the events — and the only reason it was
    not noticed is that no surface had ever tried to ask.

    `_link` has resolved the writer-owned Card id onto the Idea by the time this runs, so the id is
    simply there to be written down.

    `idea` may be None, and the re-proposal path passes None ON PURPOSE. Its `node.idea.card_id` is
    the card this very path is about to DROP (`_drop_card_once(..., reason="reproposed")`, and it is
    handed to `_plan_native_card` as `superseded_card_id`), so stamping it would file the research
    that REPLACED a card as the research that produced it — the one mis-attribution a card trace must
    never make. The replacement is minted afterwards under `_id_lock`; the link survives through the
    `node_created` event that shares this span's trace.

    `proposed_for_node` is deliberately NOT spelled `node_id`. `node_id` is the attribution key
    `traceview.effective_node_id` projects the WHOLE trace by, so using it here would move every
    Researcher trace into one node's per-node view — and a card's research belongs to all of its
    nodes, not to whichever one happened to be prepared first. The node this proposal was prepared
    for is still worth recording; it is context, not ownership.
    """
    if span is None:
        return
    card_id = getattr(idea, "card_id", None) if idea is not None else None
    if isinstance(card_id, str) and card_id.strip():
        span.set("card_id", card_id.strip())
    if isinstance(node_id, int) and not isinstance(node_id, bool) and node_id >= 0:
        span.set("proposed_for_node", node_id)
    operator = getattr(idea, "operator", None) if idea is not None else None
    if isinstance(operator, str) and operator.strip():
        span.set("operator", operator.strip())


# Set INSIDE an `_offload_build` worker, read by `_reserve_on_main_task`. A thread-local rather than
# a ContextVar: the worker needs to know it is a worker, and a ContextVar copied into the thread
# would say the same thing on the main task.
_OFFLOADED_BUILD = threading.local()


class NodeBuildMixin:
    """The engine's node-building helper cluster. See the module docstring for the mixin
    convention (`self` is the Engine)."""

    def _developer_crash_pause_due(self, state: RunState, node_id: int) -> bool:
        """Does THIS Developer crash trip the run-level circuit breaker?

        `developer_crash_pause_after` (default 1 = the historical "pause on the FIRST crash", byte
        for byte) against `developer_crash_rank`. The node's `node_failed` terminal is owed either
        way and is not this method's business; only the `pause` beside it is conditional, and HOW
        that pause is appended stays each site's own (queued from a worker, tail-CAS'd on the
        speculation lane, sequential on the serial paths — see `developer_crash_records`).

        `getattr` with the historical default because ~170 tests build the engine through `__new__`
        without `__init__`; a junk or sub-1 value settles to 1 rather than to "never pause".
        """
        raw = getattr(self, "developer_crash_pause_after", 1)
        try:
            threshold = int(raw)
        except (TypeError, ValueError):
            threshold = 1
        if isinstance(raw, bool) or threshold < 1:
            threshold = 1
        return developer_crash_rank(state, node_id) >= threshold

    def _refuse_node_open_below_floor(self, what: str) -> None:
        """The node-OPEN gate under the spend ceiling: `CostAccountant.require_headroom` for every
        accountant this engine's roles meter on, with `node_open_budget_floor_usd`.

        MAIN TASK ONLY, at the decision to open a unit of paid work — the serial create loop, the
        head of a parallel-build chunk, the Card lane's staging pass and `_request_card_build`'s
        election — and deliberately NOT inside `_create_node`: under the `llm_parallel` fan-out
        that method runs in an `anyio.to_thread` WORKER, where a `BudgetExceeded` is HELD until the
        whole fan-out joins (`_create_node_guarded` lets it propagate into the caller's
        `_DeferredBudgetStop` since review 2026-09-22, ENG1-01 — before that it was swallowed into
        one node's terminal and the run went on spending), so a floor asked there would refuse only
        AFTER the node it exists to keep closed had been opened and paid for. Nor
        in an eval child, where the same raise cancels sibling terminals (the `_DeferredBudgetStop`
        docstring's open item). Raised on the main task it is exactly the ceiling's own path:
        `Engine.run` drains the in-flight evaluation and re-raises, the CLI records
        `budget_exhausted`.

        Every reachable accountant rather than "the" accountant: `run_cost_accountant` makes them
        one per `Settings`, but a test double or a hand-built role may carry its own, and a floor
        asked of a subset is the split ceiling `run_cost_accountant`'s docstring measured. Inert
        when the floor is 0/junk (the library default) or no accountant has a limit.
        """
        raw = getattr(self, "node_open_budget_floor_usd", 0.0)
        try:
            floor = float(raw)
        except (TypeError, ValueError):
            return
        if isinstance(raw, bool) or not floor > 0.0:
            return
        for accountant in find_cost_accountants(self):
            check = getattr(accountant, "require_headroom", None)
            if callable(check):
                check(floor, what)

    def _ensemble_idea(self, parents) -> Idea:
        """A0b: an ensembling/recombination merge — instruct the Developer to combine the parents'
        solutions (stack/average predictions) rather than mean-averaging params. Carries the mean
        params as a safe payload so a Toy/baseline Developer degrades to the legacy mean-merge."""
        base = merge_idea(parents)
        descr = "; ".join(
            f"node {p.id} (metric={p.metric}, params={p.idea.params})"
            + (f": {p.idea.rationale[:120]}" if p.idea.rationale else "")
            for p in parents)
        base.rationale = ("Ensemble/recombine the top solutions into one stronger pipeline "
                          "(e.g. average or stack their predictions, or merge their best components). "
                          f"Parents — {descr}.")
        return base

    @in_llm_lane("build")
    def _agent_next_actions(self, state: RunState) -> list[dict]:
        """Self-driving action selection (Step 5). The unified agent picks the next macro action
        from the pure legal-action gate; forced phases (evaluate-pending / budget / seed) give it
        no discretion. Records an audit-only `agent_decision` (never read by best-selection); the
        chosen action then flows through the SAME bucket logic as the policy path. Falls back to the
        policy's own recommendation on any malformed/abstaining choice — the agent can never escape
        `legal`, so 'follow the right pipeline' is a structural invariant, not prompt obedience."""
        from looplab.search.policy import legal_actions
        # Honor a live node-budget extension (set on self.policy.max_nodes in the run loop) so the
        # agent path and the pure-policy path agree on when the search is allowed to keep going.
        legal = legal_actions(state, self.policy, max_nodes=self.policy.max_nodes)
        if len(legal) <= 1:
            return legal                       # finish ([]), forced evaluate/seed, or single option
        if {a["kind"] for a in legal} == {"evaluate"}:
            return legal                       # forced: evaluate all pending, no discretion
        recommended = next(iter(self.policy.next_actions(state)), None)
        chooser = getattr(self.researcher, "choose_action", None)
        if not callable(chooser):              # defensive: agent_drives_actions implies unified
            return self.policy.next_actions(state)
        from looplab.agents.roles import _state_brief
        from looplab.agents.hints import render_hint_directives
        try:
            # NOT a proposal: this asks for an INDEX into `legal`, and the reply has no `card_id`
            # field at all. `for_proposal=False` keeps the board's content (which is real context for
            # choosing a macro action) and drops the two claim contracts that only a proposer can
            # honour — see `_state_brief`.
            # The cue's default is the settled True, for `crash_repair._ask_triage`'s reason.
            brief = _state_brief(state, None, for_proposal=False,
                                 memo_verdicts=getattr(self, "_memo_verdict_cue", True))
        except Exception:  # noqa: BLE001 - a brief is advisory; never block on it
            brief = ""
        # Signal-delivery (§1): the pilot picks the next macro action, so a standing operator
        # directive must reach it too — else it can choose an action that fights the directive.
        brief += render_hint_directives(state.pending_hints)
        choice = chooser(state, legal, recommended, brief=brief)
        idx = choice.get("index", -1) if isinstance(choice, dict) else -1
        chosen = legal[idx] if isinstance(idx, int) and 0 <= idx < len(legal) else \
            (recommended if recommended is not None else legal[0])

        def _summ(a: Optional[dict]) -> Optional[dict]:
            if not a:
                return None
            return {"kind": a.get("kind"), "parent_id": a.get("parent_id"),
                    "parent_ids": a.get("parent_ids"), "node_id": a.get("node_id")}

        self.store.append(EV_AGENT_DECISION, {
            "at_node": len(state.nodes),
            "chosen": _summ(chosen),
            "legal": [_summ(a) for a in legal],
            "recommended": _summ(recommended),
            "rationale": (choice.get("rationale", "") if isinstance(choice, dict) else "")[:500],
        })
        return [chosen]

    @in_llm_lane("build")
    def _implement(self, idea, parent=None, *, developer=None,
                   state: Optional[RunState] = None) -> str:
        """Route an implement through `implement_from(idea, parent)` when the Developer supports it
        and a parent exists — so an IMPROVE/REFINE starts from the parent's actual solution (its
        code/files) and patches it, instead of regenerating everything from the pristine baseline
        (which loses the parent's accumulated edits and burns tokens re-deriving them). Falls back
        to the plain `implement(idea)` for developers that don't take a parent (draft, offline)."""
        return self._implement_result(idea, parent, developer=developer, state=state).code

    def _implement_result(self, idea, parent=None, *, developer=None, state=None,
                          co_parents=()) -> DeveloperResult:
        """`_implement`, returning the whole `DeveloperResult` envelope (doc 52 row 12).

        The `str`-returning `_implement` above is kept for its callers and the suite; every site
        that then READ a side channel off the instance (`last_files`, the footprint, the rollback
        ask) reads this envelope instead, which is what lets the paid call leave the loop thread —
        see `agents/roles.py::DeveloperResult` for why the freeze was the only thing that made the
        instance reads safe.

        `co_parents` (doc 52 row 18) are the OTHER parents of an ensemble merge: the Developer is
        seeded with `parent`'s files as its working set and, when its `implement_from` accepts the
        keyword, is shown the co-parents' code and traces too — a recombination that sees one
        lineage is an improve with a longer rationale. A Developer without the keyword is called
        exactly as before."""
        developer = developer or self.developer
        impl_from = getattr(developer, "implement_from", None)
        if parent is not None and callable(impl_from):
            if co_parents and accepts_co_parents(impl_from):
                return self._run_developer(developer, impl_from, idea, parent,
                                           bind_to=state, co_parents=tuple(co_parents))
            return self._run_developer(developer, impl_from, idea, parent, bind_to=state)
        return self._run_developer(developer, developer.implement, idea, bind_to=state)

    def _run_developer(self, developer, fn, *args, bind_to=_OMIT, **kwargs) -> DeveloperResult:
        """ONE Developer call — BIND, CLEAR, call, capture — as one atomic step under the instance's lock
        (`developer_call_lock`). The lock is what makes two offloaded calls on a SHARED instance
        safe: they queue here, in a worker, instead of on the event loop.

        THE CLEAR IS THE THIRD MEMBER OF THAT STEP and used to sit outside it. Five build sites each
        called `_reset_developer_footprint(developer)` themselves, before the call — an UNLOCKED
        write to the shared instance — and once the serial build, the fork's build and the node-reset
        rebuild moved off the loop thread (2026-09-06, `_offload_build`), two of those writes could
        land inside another caller's locked window. Driven, both directions on one shared instance:
        a reset landing between a call's `fn` and its capture makes the envelope report
        `last_footprint=None`, so `_finalize_developer_footprint` falls back to the Researcher's
        proposal and a build that RAISED its own estimate is scheduled at the old one; and a site
        that clears before it blocks on the lock inherits whatever the intervening call left behind
        — the cross-node leak the clear exists to prevent, delivered by the clear itself. That is
        the exact guarantee `node_build.py::_offload_build`'s docstring claims the envelope gives,
        so the clear had to move rather than the docstring.

        Reach: this also clears before a REPAIR, which no site did. Nothing reads a repair's
        footprint — all five `_finalize_developer_footprint` sites are build sites and each was one
        of the five that cleared — and per-call is what the clear means, so a Developer that omits
        the optional output on a repair now reads as "no estimate" instead of inheriting the last
        build's. A call site may no longer clear on its own: an unlocked write is the defect, and a
        LOCKED one at the site would not fix it either, since the gap between that lock and this one
        is all an intervening call needs (`tests/test_developer_result.py`).

        AND THE BIND IS THE FOURTH MEMBER, for the same reason and after the same miss. When the
        clear moved in, its unlocked sibling did not: `_implement_result` and `_repair_result` each
        called `bind_state(state)` on the shared Developer ABOVE this lock. `bind_state` is a plain
        write (`repo_developer` stores `self._memory_state = state`), so with two offloaded calls on
        one instance worker A could bind its fold, block here, and run its build against the fold
        worker B bound while A was waiting — the Developer's memory and cross-run providers
        answering about a different lifecycle than the node being built. `bind_to` defaults to
        `_OMIT` rather than None because `_repair_result` legitimately binds None (no state given),
        and a call that asks for no bind at all must be distinguishable from that."""
        with developer_call_lock(developer):
            if bind_to is not _OMIT:
                bind_state = getattr(developer, "bind_state", None)
                if callable(bind_state):
                    bind_state(bind_to)
            self._reset_developer_footprint(developer)
            code = fn(*args, **kwargs)
            return self._capture_developer_result(developer, code)

    @staticmethod
    def _capture_developer_result(developer, code) -> DeveloperResult:
        """Read every registered side channel off the instance INTO the envelope, totally.

        Literal `getattr`s, one per `DEVELOPER_OUTPUT_ATTRS` member, on purpose: the registry's
        two-way contract test (`tests/test_role_output_contract.py`) needs each consumer read to be
        greppable, and a loop over the tuple would hide them all behind one line. TOTAL over junk —
        a stub that sets a string where a dict is expected must read as "nothing", never raise out
        of a build or a repair — which is the coercion the old inline reads did piecemeal."""
        files = getattr(developer, "last_files", {}) or {}
        deleted = getattr(developer, "last_deleted", []) or []
        footprint = getattr(developer, "last_footprint", None)
        edit_calls = getattr(developer, "last_edit_calls", 0) or 0
        try:
            edit_calls = int(edit_calls)
        except (TypeError, ValueError):
            edit_calls = 0
        return DeveloperResult(
            code=code,
            last_files=MappingProxyType(dict(files) if isinstance(files, dict) else {}),
            last_deleted=tuple(str(d) for d in deleted) if isinstance(deleted, (list, tuple)) else (),
            last_footprint=dict(footprint) if isinstance(footprint, dict) else footprint,
            last_report=getattr(developer, "last_report", None),
            last_seed=getattr(developer, "last_seed", None),
            last_run=getattr(developer, "last_run", None),
            last_patch=getattr(developer, "last_patch", None),
            last_rollback_stage=str(getattr(developer, "last_rollback_stage", "") or "").strip(),
            last_budget_exhausted=str(
                getattr(developer, "last_budget_exhausted", "") or "").strip()[:32],
            # REGISTERED AND NOT CAPTURED until 2026-09-07: the field was added to
            # `DEVELOPER_OUTPUT_ATTRS` and to the envelope, and this reader was not extended, so a
            # session cut off by its money ceiling wrote `{"kind": …, "seconds": …, "detail": …}`
            # onto the instance and the envelope reported None. `tests/test_developer_result.py`
            # stayed green because it pins the FIELD SET, not that each field is read — and every
            # engine site is being migrated off instance reads ONTO this envelope, so the first
            # consumer to move records "the session was not cut off". Coerced like its siblings: a
            # stub setting a string where a dict belongs must read as nothing, never raise.
            last_budget_facts=(dict(getattr(developer, "last_budget_facts", None))
                               if isinstance(getattr(developer, "last_budget_facts", None), dict)
                               else None),
            last_edit_calls=edit_calls,
            # NOT A REGISTRY MEMBER but the same call's output, so it is captured under the same
            # lock — see `DeveloperResult.audit_extra` for the race this closes. That is also why
            # it is not one of the literal per-member `getattr`s above: those mirror the registry,
            # and this is a method the registry cannot hold.
            audit_extra=audit_extra_of(developer),
            # …and the predictive pick, for the reason `DeveloperResult.last_foresight_pick`
            # records: a repair on the shared developer clears it between this call and the emit.
            last_foresight_pick=(dict(getattr(developer, "last_foresight_pick", None))
                                 if isinstance(getattr(developer, "last_foresight_pick", None),
                                               dict) else None),
        )

    @staticmethod
    def _reset_developer_footprint(developer) -> None:
        """Clear per-call resource output through a wrapper tree before invoking Developer.

        Parallel builds use isolated role pairs, but serial reruns/repairs reuse one object.  Clearing
        every reachable wrapper/inner/fallback prevents a backend that omits the optional output from
        inheriting another node's finalization.

        ONE CALLER, `_run_developer`, which holds the instance's lock around this walk, the call and
        the capture together — see its docstring for what an unlocked clear cost. Call it from a
        build site and the walk is an unlocked write to a possibly-shared instance again.
        """
        pending = [developer]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            # ONLY ON THE OBJECT THAT OWNS THE ATTRIBUTE. `search/foresight.py::
            # ForesightPanelResearcher` is a READ-ONLY `__getattr__` proxy with no `__setattr__`,
            # and under the shipped defaults (`unified_agent`, `foresight`, `foresight_panel=2`)
            # it IS the engine's `developer`. `hasattr` there resolves THROUGH to the wrapped
            # agent, so this clear used to land in the PROXY's own `__dict__` and shadow the inner
            # agent's real value for the rest of the run: `_capture_developer_result` read None off
            # the proxy on every build from the second one on, `_finalize_developer_footprint` fell
            # back to the Researcher's proposal every time, and a build that RAISED its own resource
            # estimate was scheduled at the old one — exactly what the envelope prevents. Walking to
            # `base` (below) reaches the real slot; declining to CREATE the attribute is what stops
            # the proxy from shadowing it. An object that has never set it is already "cleared".
            if "last_footprint" in getattr(current, "__dict__", {}):
                try:
                    current.last_footprint = None
                except Exception:  # noqa: BLE001 - optional audit output must never block a build
                    pass
            # `base` is the `__getattr__` proxy's delegate; the other three are the wrapper chain.
            for attr in ("inner", "developer", "fallback", "base"):
                try:
                    child = getattr(current, attr, None)
                except Exception:  # noqa: BLE001 - a plugin property may be defensive/remote
                    child = None
                if child is not None and child is not current:
                    pending.append(child)

    def _finalize_developer_footprint(self, idea: Idea, developer, code: str,
                                      footprint=_OMIT) -> tuple[Idea, bool]:
        """Merge the Developer's per-call resource estimate onto a durable Idea.

        A missing optional output means the Developer accepted the Researcher's proposal.  A concrete
        output may scale it up or down, then the detected pool clamps the effective quantities.  An
        unspecified proposal stays unspecified so legacy scheduling remains byte-for-byte compatible.

        `footprint` is the envelope's `last_footprint` (doc 52 row 12): every build site that has a
        `DeveloperResult` passes it, so the estimate read is the one captured under the call's own
        lock and never a sibling's landing on the shared instance afterwards. Omitted, the instance
        is read as before — the shape the suite's direct callers and older wrappers still use.
        """
        proposed = normalize_researcher_footprint(getattr(idea, "footprint", None))
        # BOTH SENTINELS. A footprint finalized from a build that produced no code is a claim about
        # resources nothing will use. `empty_build_refusal` moved to the `stuck` spelling on
        # 2026-08-28 and this reader kept testing only the crash one -- found by the cross-module
        # invariant in `tests/test_empty_build_is_stuck_not_a_crash.py`, not by hand, after the same
        # omission in `engine/speculation.py` had already cost dsFix1 a node.
        if proposed is None or is_developer_error(code) or is_developer_stuck(code):
            return idea, False
        finalized = normalize_researcher_footprint(
            getattr(developer, "last_footprint", None) if footprint is _OMIT else footprint
        ) or proposed
        clamp = getattr(self, "_clamp_resource_footprint", None)
        if callable(clamp):
            finalized = clamp(finalized) or proposed
        return idea.model_copy(deep=True, update={"footprint": finalized}), True

    def _directed_idea(self, idea, state: RunState):
        """Signal-delivery (§1): fold the active operator directives into the idea HANDED TO THE
        DEVELOPER so a standing directive ("use only sklearn") steers the CODE that gets written,
        not only the proposal (the Researcher already renders directives; the Developer never saw
        them). Returns a COPY with the rendered directive block appended to `rationale` — the field
        every Developer backend renders — so it reaches the innermost developer through ANY wrapper
        chain (the copy rides the data, not a forwarded attribute that a wrapper could drop). The
        ORIGINAL idea, recorded in `node_created`, is untouched, so the audit rationale stays the
        Researcher's own. Nothing to add -> the idea is returned unchanged (identity).

        Also carries the DEVELOPER's own cross-run code-fix lessons (§role-split): the Developer only
        ever sees ITS lessons ("a node failing with X was fixed by …" on similar tasks) — never the
        Researcher's R&D lessons, which ride the proposal prompt instead. Most useful on the repair
        path (`_repair` routes through here), where "what fixed this crash class" is exactly relevant."""
        from looplab.agents.hints import render_hint_directives
        blocks = [b for b in (render_hint_directives(state.pending_hints),
                              self._developer_prior_text(idea).strip()) if b]
        if not blocks:
            return idea
        di = idea.model_copy(deep=True)
        di.rationale = ((di.rationale or "") + "\n" + "\n".join(blocks)).strip()
        return di

    def _developer_prior_text(self, idea) -> str:
        """The Developer's cross-run prior for THIS build — operator-scoped when the operator about
        to fire is known and the operator scoping is on, otherwise the run-wide text verbatim.

        This is the one place in the loop that holds both halves: the retrieved cross-run lessons and
        the `Idea` whose `operator` the node will carry. Cross-run LESSONS were retrieved by task
        fingerprint and role and by nothing about the action (doc 52 §4.3), so a merge, a repair and
        an improve all read the same five rows.

        `operator_scoped_prior` returns None with the flag off (the default), with no operator on the
        idea, or before any prior has been loaded — and then this is the historical expression, byte
        for byte. It never falls back to the unscoped text while claiming a scoped receipt: the
        receipt is written by the scoped render itself or not at all."""
        scoped = self.lessons.operator_scoped_prior(
            LESSON_ROLE_DEVELOPER, str(getattr(idea, "operator", "") or ""), phase="build")
        return self._dev_prior_note_text if scoped is None else scoped

    @in_llm_lane("build")
    def _repair(self, node, err: str, state: Optional[RunState] = None, *, developer=None) -> str:
        """Route a repair through `repair_from(idea, node, error)` when the Developer supports it, so
        the fix is seeded from the FAILING NODE's OWN files — not the shared developer's `last_files`,
        which holds whatever node it built last (a batch builds every node before any eval, so
        `last_files` is almost never the node being repaired). Falls back to `repair(idea, code, err)`.

        §1: when `state` is given, standing operator directives are folded into the idea so the REPAIRED
        code honors them too (consistency with the four build sites); without it the raw idea is used."""
        return self._repair_result(node, err, state, developer=developer).code

    def _repair_result(self, node, err: str, state: Optional[RunState] = None, *,
                       developer=None) -> DeveloperResult:
        """`_repair`, returning the whole `DeveloperResult` envelope — see `_implement_result`."""
        idea = self._directed_idea(node.idea, state) if state is not None else node.idea
        developer = developer or self.developer
        rf = getattr(developer, "repair_from", None)
        if callable(rf):
            return self._run_developer(developer, rf, idea, node, err, bind_to=state)
        return self._run_developer(developer, developer.repair, idea, node.code, err, bind_to=state)

    def _emit_node_created(self, *, node_id: int, parent_ids: list, operator: str, idea: dict,
                           code: str, files: dict, deleted=_OMIT, research_origin=_OMIT,
                           source=_OMIT, origin=_OMIT, forked_from=_OMIT, generation=_OMIT,
                           parent_generations=_OMIT, cross_run_receipt=_OMIT,
                           footprint_finalized=_OMIT, speculative=_OMIT,
                           card_build_generation=_OMIT, eval_start_boundary=_OMIT,
                           materialize_aborted_intent=_OMIT, model_arm=_OMIT,
                           expected_last_seq=_OMIT) -> None:
        """The single `node_created` emitter for all four creation sites (`_create_node`,
        `_create_injected_node`, `_ablate`, `_ablate_code`). Optional keys default to the
        `_OMIT` sentinel and are LEFT OUT of the payload when not passed — never None-filled —
        so omitted compatibility fields retain their historical shape (key set AND key order).
        All current creation sites intentionally opt into the additive ``eval_start_boundary``
        contract; old logs remain reader-defaulted. Known quirk kept for replay compatibility: the
        two ablate sites emit NO `deleted` key at all (`_create_node` always emits it,
        `_create_injected_node`
        emits `deleted` + `source` + `origin` but no `research_origin`) — the fold reads every
        optional key with a default, so do not "normalize" the shapes here."""
        data = {"node_id": node_id, "parent_ids": parent_ids, "operator": operator,
                "idea": idea, "code": code, "files": files}
        for k, v in (("deleted", deleted), ("research_origin", research_origin),
                     ("model_arm", model_arm),
                     ("source", source), ("origin", origin), ("forked_from", forked_from),
                     ("generation", generation),
                     ("parent_generations", parent_generations),
                     ("cross_run_receipt", cross_run_receipt),
                     ("footprint_finalized", footprint_finalized),
                     ("speculative", speculative),
                     ("card_build_generation", card_build_generation),
                     ("eval_start_boundary", eval_start_boundary),
                     ("materialize_aborted_intent", materialize_aborted_intent)):
            if v is not _OMIT:
                data[k] = v
        append_kwargs = (
            {} if expected_last_seq is _OMIT
            else {"expected_last_seq": expected_last_seq}
        )
        self.store.append(EV_NODE_CREATED, data, **append_kwargs)

    def _consume_batch_proposal(self, state, width: int):
        """Run one batched proposal and READ its result. Returns ``(ideas, telemetry, dropped)``.

        `_propose_batch` (novelty.py) signalled its results through three instance attributes rather
        than a return value — `_pending_batch_telemetry`, `_pending_batch_dropped` and
        `_pending_batch_novelty_gated` — until review 2026-09-22 (ENG1-12) made them the RETURNED
        `novelty.py::BatchProposal`. Two call sites — `_handle_create_actions`' concurrent-build
        chunk and `card_reservation.py::_stage_card_creates` — used to read that protocol by hand,
        including the padding rule and the snapshot-before-reset ordering (doc 25 ES-08), which is
        what this funnel replaced. With no shared attribute there is nothing left to reset: each
        call's lists are its own, so no batch can read another's telemetry, drops or gate capability
        whatever order its caller records, reserves or raises in. The gate capability
        (`BatchProposal.gated`) is not returned from here at all — both callers reserve or stage
        every Idea, and neither crosses `_prepare_node_idea`, its only consumer.

        NEITHER CALLS THIS DIRECTLY ANY MORE (2026-08-30): both `await _await_batch_proposal`, which
        runs this on a worker thread under `_capture_proposal_events` and publishes the buffered
        folded rows from the main task. A reader hunting callers of THIS name finds the wrapper, not
        the two methods; the chunk also left `run` for `_handle_create_actions` (doc 25 ES-05), so
        the old name sent that reader somewhere the call had never been.

        The padding is load-bearing: telemetry must align 1:1 with `ideas` so each build emits ITS
        OWN hypothesis_ranked/foresight_selected. A short list silently shifts every later idea's
        telemetry onto the wrong node.

        Both `dropped` and `telemetry` are SNAPSHOTTED (copied) here. That once let each caller reset
        the attributes at whatever point its own durability ordering required without losing what it
        was about to record (`_handle_create_actions` cleared after the reservations were durable,
        `_stage_card_creates` in a `finally`); with the attributes gone the copy still keeps what a
        caller records independent of a producer that reuses its buffers.
        """
        # The BATCH proposal's progress beacon, and the one that matters most: on the shipped default
        # width this — not `_prepare_node_idea` from `_create_node_scoped` — is the path a run
        # actually takes, and it is the single longest wholly invisible stretch in the loop. It runs
        # before any node id exists, so no `node_building` marker has been appended and the UI has
        # literally nothing to draw; the strip falls through to "Planning next experiment…" for the
        # entire Researcher call. This is the funnel BOTH batch call sites go through (doc 25 ES-08),
        # which is why the beacon belongs here rather than duplicated at each.
        #
        # No `node_id`: a batch proposes `width` ideas at once and none of them has an id yet.
        # Emitting a prospective one would name a node that most of these ideas will not become.
        # `count` is the honest shape, and a beacon without a node_id is the run-level phase it is.
        # `_paid_progress`, NOT `_progress`: a batch proposal is `width` paid Researcher calls, and
        # `_progress` opens no span — `core/tracing.py::generation` then yields the NULL handle and
        # every one of those calls is written with `trace_id=null`, attributable to nothing and
        # invisible to `looplab timings`, the trace view and every per-phase cost question. This
        # comment's own note above already called this "the single longest wholly invisible stretch
        # in the loop", and on the shipped default width it is the path a run actually takes: the
        # SERIAL propose one method over is inside `_create_node`'s `create_node` span, so the
        # asymmetry hid — the lane that pays most was the one with no span.
        with self._paid_progress(PROGRESS_STAGE_BUILD, "propose", count=int(width)):
            proposal = self._propose_batch(state, width)
        ideas = list(proposal.ideas)
        telemetry = list(proposal.telemetry or ())
        if len(telemetry) < len(ideas):
            telemetry.extend([None] * (len(ideas) - len(telemetry)))
        dropped = list(proposal.dropped or ())
        return ideas, telemetry, dropped

    async def _await_batch_proposal(self, state, width: int):
        """`_consume_batch_proposal`, OFF the event-loop thread, with its folded audit rows published
        by the MAIN TASK. Returns exactly what that funnel returns.

        THE PER-ACTION LANE WAS MOVED OFF THE LOOP AND THIS SIBLING WAS NOT. `_propose_batch` is the
        same minutes-long paid provider wait with no `await` in it, so as one event-loop callback it
        stopped everything: no eval terminal, no watchdog tick, no timer could land. Measured on the
        live engine with py-spy for the per-action twin — asyncio's `_run_once` sat below a
        `threading.join` with no coroutine frame between, and a node whose training had already died
        waited 62 MINUTES for its terminal while both H200s idled. On the shipped default width the
        BATCH path is the one a run actually takes, so this was the larger half of that defect.

        ONE HELPER, BOTH CALL SITES, for the reason `_consume_batch_proposal` itself is one funnel:
        `_handle_create_actions`' concurrent-build chunk and `_stage_card_creates`' multi-draft branch
        are both on the main task and both blocked identically. Two hand-written offloads would be two
        chances to forget the sink.

        THE SINK IS NOT OPTIONAL AND IS THE WHOLE REASON THIS IS NOT A ONE-LINE `to_thread`.
        `_propose_batch` reaches `_append_proposal_event` (novelty.py), which falls through to
        `self.store.append` whenever no sink is installed — writing EV_NOVELTY_REJECTED and friends.
        Those are FOLDED and named by NONE of `events/types.py`'s three thread-append registries, so
        appending them from a worker breaches invariant #1's sole-writer rule; worse, they are
        AUTHORITY-BEARING for `speculation.py::_proposal_authority_seq`, the fence that discards a
        paid proposal when a non-diagnostic row lands inside its equality window. Buffer, then
        publish from here. This is Layer 5's own discipline, not a second one.

        PUBLISHED WHETHER OR NOT ANY IDEA FORMED, exactly as the per-action lane publishes: a refused
        proposal is when the receipt matters most, and gating the publish on a non-empty result would
        restore the silence `bd182357` exists to end.

        The `_progress` beacon INSIDE `_consume_batch_proposal` deliberately travels to the worker
        with it. It is a direct `store.append` of a DIAGNOSTIC type, which invariant #1 permits a
        concurrent task by name — and a beacon that stayed behind would announce the phase from a
        thread that is no longer in it.
        """
        # RIDES THE PROPOSAL POOL, not anyio's shared 40-token default, since 2026-08-31. An
        # in-flight `_run_eval` holds a default token for its whole multi-hour duration and
        # `eval_parallel` is admitted to 1024, so at a raised width the paid proposal queued behind
        # the evals before it even started. See `novelty.proposal_limiter` for the derivation of the
        # size and for the two sibling lanes that share it.
        return await self._offload_under_proposal_sink(
            functools.partial(self._consume_batch_proposal, state, width))

    def _fail_reserved_build(self, *, node_id: int, card_id: Optional[str], generation: int,
                             reason: str, error: str, drop_card: bool = True,
                             never_evaluated: bool = False) -> None:
        """Close a pre-node reservation and, when bare, its immutable Card work item.

        A terminal on a bare ``node_building`` clears the transient marker but creates no Node evidence.
        Without the paired card_auto_dropped receipt that Card would resurrect as a fresh proposed item after
        replay.  Existing-node reruns pass ``drop_card=False`` because they reuse the original lifecycle.

        ``never_evaluated`` stamps the durable pre-dispatch receipt (``Node.never_evaluated``) that the
        L5 node-budget refund is proven from.  It stays OPT-IN and defaults False: only a caller that
        can show, from the state it just folded, that no evaluation was ever dispatched for this exact
        lifecycle may claim it — an ordinary failed/aborted build keeps counting exactly as before.

        ``drop_card`` is a caller's INTENT and is no longer sufficient on its own. Since the `attach`
        disposition (2026-08-12) a reservation's ``card_id`` is not always one that reservation
        minted, and every close path across this module/`orchestrator`/`speculation`/`ablation`
        reaches here — one of them `_recover_interrupted_builds`, which for an interrupted build has
        no Node to read and so cannot see the difference at all. `_reservation_minted_card` is the
        ownership half, and it is checked HERE rather than at each site precisely because the sites
        do not know: the same intent that is right for a bare first build ("close the work item
        nobody will ever own") deleted the PARENT's card from the board after one interrupted repair.
        """
        # Fail closed first. If the process dies between these two appends, the still-live build marker
        # makes recovery retry the terminal, while the Card is already non-selectable. Skip an existing
        # drop receipt so that prefix recovery remains idempotent.
        if card_id and drop_card and not self._reservation_minted_card(
                self.store.read_all(), node_id, card_id):
            # An attached repair, or a card another node's durable idea already names. The
            # reservation still gets its terminal below; the work item it JOINED stays on the board.
            drop_card = False
        if card_id and drop_card:
            self._drop_card_once(card_id, reason=reason)
        payload = {
            "node_id": node_id,
            "generation": generation,
            "error": error,
            "reason": reason,
            "eval_seconds": 0.0,
        }
        if card_id:
            payload["card_id"] = card_id
        if never_evaluated:
            payload["never_evaluated"] = True
        self.store.append(EV_NODE_FAILED, payload)

    def _build_role_pairs(self, n: int) -> list:
        """Up to `n` (researcher, developer) pairs for a parallel build batch: the primary (self's roles)
        plus fresh WIRED pairs from `role_factory`, cached in `self._role_pool` and reused across batches
        (each pair's per-build state — developer.last_files, researcher hints — is captured at node_created
        before the next batch reuses it, so reuse is safe). `role_factory` None or `n<=1` -> just the
        primary pair, and the caller stays serial. Fresh pairs are what isolate per-build role state so
        concurrent drafts don't clobber each other. A pair that cannot be BUILT (the factory raises or
        returns no pair, a pooled Developer backend raises) caps the fan-out at the pairs already
        built — logged with its exception and counted by `contain`, never raised unless it is the
        run's spend ceiling — and the next call tries the factory again."""
        if n <= 1 or self.role_factory is None:
            return [(self.researcher, self.developer)]
        if self._role_pool is None:
            self._role_pool = []
        # Function-local: the stack module pulls the three wrapper modules in, which nothing else on
        # the engine's import path needs until a pool is actually minted.
        from looplab.search.researcher_stack import pooled_researcher
        while len(self._role_pool) < n - 1:
            try:
                pair = self.role_factory()
            except Exception as exc:  # noqa: BLE001 — a factory failure just caps fan-out, never crashes the run
                # SAID, AND COUNTED (review 2026-09-22, ENG1-14 — doc 50 ES1-04). This `break` was
                # silent, so a transient failure while BUILDING a pooled pair left only its
                # consequence behind: the Layer-5 producer found no pair and closed its head
                # `producer_failed` — the word for "the producer RAN and gave up" — which bars the
                # Card from speculative election for the rest of the run, with no error text and no
                # log line anywhere (driven: 1 pair, producer pair None, 0 log records). Capping the
                # fan-out is still the containment; the failure is now on the span (`contain`, which
                # also re-raises a spend ceiling) and in the log with its traceback, and
                # `speculation.py::_start_head_producer` RETRIES a head whose pair could not be built
                # instead of closing it.
                contain("pooled role pair could not be built", exc)
                _LOG.warning(
                    "role_factory raised building pooled role pair %d; fan-out is capped at %d "
                    "pair(s) for this call and the next one retries the factory: %s: %s",
                    len(self._role_pool) + 1, len(self._role_pool) + 1, type(exc).__name__, exc,
                    exc_info=True)
                break
            if not (isinstance(pair, tuple) and len(pair) == 2):
                _LOG.warning(
                    "role_factory returned %s, not a (researcher, developer) pair; fan-out is "
                    "capped at %d pair(s)", type(pair).__name__, len(self._role_pool) + 1)
                break
            # THE PRIMARY'S FREE LAYERS, NONE OF ITS PAID ONES (review 2026-09-22, SCJ-02). The
            # factory's pair is bare, and the Layer-5 producer PROPOSES on it: under
            # `surrogate_proposer`/`policy=bohb` the primary proposed through the surrogate while
            # this lane ignored the setting. Decided off the primary's LIVE chain, before the
            # developer override below so the unified-facade test sees the factory's own pair, and
            # on a draw stream of its own (`seed`) so two lanes never re-propose one point.
            # `search/researcher_stack.py` says why the two panels stay off a pooled pair.
            pair = (pooled_researcher(self.researcher, pair[0], pair[1],
                                      explore=self._surrogate_explore,
                                      seed=len(self._role_pool) + 1), pair[1])
            if self._pool_developer_override is not None and self.developer_factory is not None:
                try:
                    pair = (pair[0], self.developer_factory(self._pool_developer_override))
                except Exception as exc:  # noqa: BLE001 - cap fan-out if the selected backend cannot be built
                    # The same silent cap as the factory's above, one step later — said the same way.
                    contain("pooled developer backend could not be built", exc)
                    _LOG.warning(
                        "developer_factory(%r) raised building pooled role pair %d; fan-out is "
                        "capped at %d pair(s) for this call: %s: %s",
                        self._pool_developer_override, len(self._role_pool) + 1,
                        len(self._role_pool) + 1, type(exc).__name__, exc, exc_info=True)
                    break
            self._role_pool.append(pair)
        # workers are constructed lazily, after Engine.__init__ bound the primary role
        # graph. Attach every newly reachable accountant before the first concurrent paid request.
        #
        # SEED FIRST, THEN BIND, the order `Engine.__init__` uses and `seed_prior_spend`'s docstring
        # requires: the tracker takes each accountant's baseline at bind time, so seeding after it
        # would re-record the prior spend as new usage. `Engine.__init__` ran that pass ONCE, before
        # this pool existed, so on a RESUME every accountant a pooled `role_factory()` mints here
        # was reachable only afterwards and started at `spent = 0.0` — a second full
        # `llm_budget_usd` on the widest-spending lanes, exactly the §213 overshoot the seeding
        # exists to end. It was benign only where the factory happened to hand every pooled client
        # the SAME accountant object; that is a property of a factory, not of this seam. Idempotent
        # per accountant (`_PRIOR_SEEDED_ATTR`), so the repeat costs one log read.
        seed_prior_spend(self)
        bind_cost_accountants(self)
        return [(self.researcher, self.developer)] + self._role_pool[: n - 1]

    def _prepare_node_idea(self, action: dict, state: RunState, *, researcher,
                           prospective_node_id: int, source: str,
                           proposal_events=None, preproposed=None,
                           already_gated: bool = False,
                           drop_repeated_duplicate: bool = False) -> Optional[Idea]:
        """Finish the concrete Idea before Card/node reservation, without implementing code.

        A native ownership receipt binds the final operator/params/space/profile/footprint, so the
        old reserve-before-propose ordering cannot produce an honest Card.  This helper is the moved
        proposal half of ``_create_node``; every Developer call remains after durable reservation.

        ``already_gated`` is the caller's statement that ``preproposed`` is an object the batch pass
        itself put through the vs-history novelty gate — `BatchProposal.crossed_gate(idea)`, an
        IDENTITY test (review 2026-09-22, ENG1-12). It was an engine list this method consumed.
        """
        kind = action["kind"]
        events = list(proposal_events) if proposal_events is not None else self.store.read_all()
        try:
            setattr(researcher, "_steering_context", [])
        except Exception:  # noqa: BLE001 - wrappers may expose a read-only compatibility surface
            pass
        parent_snapshot = self._build_parent_snapshot(state, action)
        if parent_snapshot is None:
            return None
        _kind, parents, parent_generations = parent_snapshot

        def _link(candidate, *, proposed: bool = True, receipt_from=None) -> Optional[Idea]:
            if candidate is None:
                return None
            # The proposal path's provider circuit breaker, at the ONE funnel every proposal
            # (draft/improve/debug and a preproposed batch idea) passes through before a Card or a
            # node id exists. A degraded FALLBACK is the ABSENCE of a proposal, so nothing downstream
            # — the Card statement, the hypothesis board, the node rationale, the cross-run case —
            # may be minted from it. See `_refuse_degraded_proposal`.
            #
            # `proposed=False` for the two MECHANICAL ideas, which no Researcher authored: the merge
            # operator's mean/ensemble Idea, and the repair path's copy of the failing parent's Idea.
            # The copy is why this flag exists rather than an unconditional check — it inherits the
            # PARENT's rationale, so replaying or resuming a log written before this change (one whose
            # nodes already carry `fallback (…)` rationales, e.g. `/tmp/ll-s4b/run`) would debug such a
            # node and raise a provider pause naming a failure that is not happening now.
            if proposed and self._refuse_degraded_proposal(candidate, main_task=False):
                return None
            # WHICH BOUND ENDED THE PAID PROPOSE, if any. `roles.RESEARCHER_OUTPUT_ATTRS` carries
            # the rule; the Researcher sets it per call and "" means the model emitted on its own
            # terms — and in unified mode `UnifiedAgent.propose` mirrors it onto the facade this
            # handle is, exactly as `_sync_audit` does for the Developer's, or this read reports
            # the DEVELOPER's last cutoff instead. Surfaced HERE for the per-action lanes
            # (draft/improve/debug and a preproposed idea crossing `_prepare_node_idea`); the
            # BATCH lane hands its Ideas straight to the stager without crossing `_link`
            # (card_reservation.py's staging loop says so), so `novelty._propose_batch` makes the
            # same check per roll at the propose site. Warning-only on purpose: with
            # `agent_max_turns`/`agent_time_budget_s` both shipping at 0 this can only fire for an
            # operator who set a cap, and the value of saying so is telling a TRUNCATED proposal
            # from a converged one — the distinction a cap destroys if nobody records it.
            # `receipt_from`: the handle whose propose produced THIS candidate when that is not
            # `researcher` — the endgame sweep's own surrogate (see the improve path below).
            if proposed:
                _bound = researcher_budget_exhausted(
                    researcher if receipt_from is None else receipt_from)
                if _bound:
                    _LOG.warning(
                        "the proposal for node %s was cut short by its %s budget — it did not "
                        "emit on its own terms, so treat it as TRUNCATED rather than converged",
                        prospective_node_id, _bound)
            linked = (candidate if isinstance(candidate, Idea)
                      else Idea.model_validate(candidate)).model_copy(deep=True)
            linked.card_id = None  # a Researcher/plugin can never claim writer namespace authority
            if self._speculation_gate_calibration:
                # Mechanical merge/debug Ideas do not pass through ToyResearcher, but they are still
                # members of the calibrated workload. Keep every physical node inside the same
                # one-GPU resource/provenance envelope without changing ordinary Idea bytes.
                linked.footprint = {"gpus": 1}
                linked.concept_mode = "full"
                linked.concepts = [
                    f"operator/{_kind}", "objective/quadratic", "space/two-dimensional"]
                linked.concepts_added = []
                linked.concepts_removed = []
            # Bind the Card and the durable Node to the action that execution will actually honor.
            # Keeping the model-requested value here would make a 3600s request with a 90s ceiling
            # appear as 3600s in both receipts even though eval_dispatch runs only 90s.
            linked.eval_timeout = self._effective_researcher_eval_timeout(linked)
            steering_context = normalize_steering_context(
                getattr(researcher, "_steering_context", []))
            if steering_context is None:
                return None
            plan = self._plan_native_card(
                events, state, linked, parents=parents, parent_generations=parent_generations,
                scored_against=state.best_node_id, source=source, at_node=prospective_node_id,
                steering_context=steering_context,
                # The proposal half of the build spine, matching `_create_node_scoped`'s
                # `_reserve_node_build(retry_attach=True)`. This pass runs OUTSIDE `_id_lock`, so the
                # two can genuinely disagree when a `card_dropped`/`card_merged`/terminal lands
                # between them; the commit pass is the authority and now RESOLVES that race instead
                # of returning None and losing the turn in silence (see the fence there).
                retry_attach=True,
            )
            if plan.disposition == "invalid":
                self._append_proposal_event(EV_NOVELTY_REJECTED, {
                    "node_id": prospective_node_id, "generation": 0,
                    "kind": "card_contract",
                    "reason": "proposal cannot form a bounded native Card action",
                    "action": "dropped",
                })
            elif plan.disposition not in {"mint", "reuse", "attach"}:
                # A DISCARDED PROPOSAL IS RECEIPTED HERE, and the placement is the decision. This
                # branch runs immediately after the proposal call, so it is a pass that can know a
                # PAID proposal was refused. (It said "THE ONLY PLACE ... and nowhere else" until
                # 2026-09-02, and that overstated its coverage: the batch draft lane in
                # `novelty.py::_link_card` and the Layer-5 speculative producer each run a paid
                # propose and refuse one too, and both lost it in silence. All three now emit
                # `card_reservation.py::discarded_proposal_receipt`, which is why the payload moved
                # out of this branch — three hand-written copies of one row is how they came to
                # disagree about whether the row exists at all.) It returned None
                # on a non-accepting disposition and the caller (`_create_node_scoped`) then unwound
                # through `_discard_node_build_telemetry`, which despite its name appends nothing —
                # its body only nulls the per-role prediction attributes so a later build cannot
                # emit an abandoned build's ranking. Correct as far as it goes, and exactly why the
                # loss was invisible. Measured on `runs/e5small-dr-unified-v8`: a propose of 24.1
                # min / 81 provider calls / 4,270,000 tokens emitted a well-formed one-knob delta
                # (train.max_seq_length 128 -> 256, citing four file:line locations) and left no
                # `card_added`, no `card_enriched`, no `hypothesis_added` and no `card_dropped`.
                #
                # `card_reservation._reserve_node_build` is DELIBERATELY silent on the same
                # dispositions: it is also the batch pre-reservation entry point, reached with a
                # ready-made Idea and no propose behind it, so calling it twice with one idea is the
                # idempotent retry of a single action and
                # `test_card_writer_lifecycle::test_batch_prereservations_mint_on_main_thread_and_
                # dedupe_exact_active_work` pins that it appends nothing. A row there would count a
                # phantom loss on every exact twin.
                #
                # REFUSING THE MINT IS UNCHANGED — a card whose owner is in flight must not be
                # minted twice. What is fixed is refusing in SILENCE.
                self._append_proposal_event(EV_NOVELTY_REJECTED, discarded_proposal_receipt(
                    plan.disposition, prospective_node_id, linked, lane="planner"))
            return plan.idea if plan.disposition in {"mint", "reuse", "attach"} else None

        if preproposed is not None:
            # The capability arrives as `already_gated`, stated by the caller that holds the
            # `BatchProposal` (review 2026-09-22, ENG1-12) — it was an engine list this branch
            # consumed by identity, and which every batch lane had to remember to reset. Equality is
            # intentionally insufficient: a direct plugin/caller proposal that happens to match a
            # batch result has not itself crossed the proposal-bound gate
            # (`BatchProposal.crossed_gate` is the identity test).
            candidate = (self._canonicalize_draft_idea(preproposed)
                         if kind == "draft" else preproposed)
            linked = _link(candidate)
            if linked is None or kind in {"merge", "debug"}:
                return linked
            if already_gated:
                return linked
            # Direct callers may supply a concrete proposal without a batch reservation. Resolve its
            # final writer-owned Card id first, then run the same proposal-bound novelty sidecar as the
            # ordinary draft/improve path. Reserved parallel batches bypass this helper entirely: their
            # shared proposal pass has already applied the gate.
            with self._paid_progress(PROGRESS_STAGE_BUILD, "novelty",
                                     node_id=prospective_node_id, prospective=True, operator=kind):
                final = self._apply_novelty_gate(
                    state, linked, researcher=researcher,
                    prospective_node_id=prospective_node_id,
                )
            return _link(final)

        if kind == "draft":
            self._set_complexity_hint(state, None, researcher=researcher)
            with self.tracer.span("propose") as _span:
                idea = _link(self._canonicalize_draft_idea(researcher.propose(state, None)))
                stamp_proposal_span(_span, idea, node_id=prospective_node_id)
            if idea is None:
                return None
            with self._paid_progress(PROGRESS_STAGE_BUILD, "novelty",
                                     node_id=prospective_node_id, prospective=True, operator=kind):
                final = self._apply_novelty_gate(
                    state, idea,
                    repropose=lambda: _link(self._canonicalize_draft_idea(
                        researcher.propose(state, None))),
                    researcher=researcher, prospective_node_id=prospective_node_id,
                    drop_repeated_duplicate=drop_repeated_duplicate)
            return _link(final)

        if kind == "merge":
            parents = list(action["parent_ids"])
            pnodes = [state.nodes[node_id] for node_id in parents]
            return _link(self._ensemble_idea(pnodes) if self._merge_mode == "ensemble"
                         else merge_idea(pnodes), proposed=False)

        parent = state.nodes[action["parent_id"]]
        if kind == "debug":
            # REFUSED (F5). This branch used to copy the failed parent's own Idea onto a NEW node
            # and hand it back to the Developer — the Debug node, i.e. another attempt at the
            # experiment that just failed, paid for out of the node budget. `None` is the answer
            # this function already gives for "no idea could be prepared", so the caller declines
            # the build without a new failure mode; the loop-level filter above is what normally
            # stops such an action reaching here at all, and this is the second door.
            return None

        # improve / capability-expand
        self._set_complexity_hint(state, parent, researcher=researcher)
        authoritative_operator = "improve"
        if (getattr(self, "_capability_expansion", False)
                and getattr(self, "_novelty_stance", None) == "explore"):
            from looplab.engine.proposal_cues import _LOCK_IN_STREAK
            from looplab.search.lock_in import capability_expansion_due
            if capability_expansion_due(state, streak_threshold=_LOCK_IN_STREAK)[0]:
                authoritative_operator = KIND_EXPAND
        # doc 52 row 18: an endgame champion sweep proposes through the k-NN surrogate (the LLM
        # Researcher below warm-up); every other improve proposes exactly as before.
        proposer = (self._sweep_researcher(researcher)
                    if action.get(META_SWEEP) and self._endgame_reserve_frac > 0.0 else researcher)
        # WHOSE PROPOSE RECEIPT `_link` READS (review 2026-09-22, W5-5 follow-up): the handle that
        # produced the candidate. For a sweep that is the sweep's OWN surrogate, whose per-call
        # receipt is "" for a numeric point; reading `researcher` there reported the Researcher's
        # LAST cut-short proposal against a point that made no call — driven: two "cut short"
        # warnings for a sweep node whose fallback was never called. The gate's re-proposal goes
        # through `researcher`, so it hands the read back. Every other improve: `researcher`, as
        # before.
        answered_by = [proposer]

        def _repropose(p=parent):
            answered_by[0] = researcher
            return _link(self._canonicalize_idea_operator(
                researcher.propose(state, p), authoritative_operator))

        with self.tracer.span("propose") as _span:
            idea = _link(self._canonicalize_idea_operator(
                proposer.propose(state, parent), authoritative_operator), receipt_from=proposer)
            stamp_proposal_span(_span, idea, node_id=prospective_node_id)
        if idea is None:
            return None
        with self._paid_progress(PROGRESS_STAGE_BUILD, "novelty",
                                 node_id=prospective_node_id, prospective=True, operator=kind):
            final = self._apply_novelty_gate(
                state, idea, repropose=_repropose,
                researcher=researcher, prospective_node_id=prospective_node_id,
                drop_repeated_duplicate=drop_repeated_duplicate)
        return _link(final, receipt_from=answered_by[0])

    @in_llm_lane("build")
    def _create_node(self, action: dict, roles=None, reserved=None, preproposed=None,
                     pretelemetry=None, precoded=None,
                     precoded_max_eval_seconds: Optional[float] = None, *,
                     already_gated: bool = False) -> None:
        """Run proposal, reservation and implementation in one node-scoped handoff context.

        ``already_gated`` travels with an UNRESERVED ``preproposed`` Idea to `_prepare_node_idea`:
        `BatchProposal.crossed_gate(idea)` from the batch that proposed it (review 2026-09-22,
        ENG1-12). A reserved build never consults it — its reservation carries the Idea."""
        from looplab.agents.agent import handoff_scope

        folded = None
        if reserved is not None:
            trace_node_id = reserved.node_id
        else:
            trace_events = self.store.read_all()
            trace_state = fold(trace_events)
            trace_node_id = self._node_id_ceiling(trace_events, trace_state)
            # ONE fold per unreserved build, not two (review 2026-09-22, EVT-04). The trace label is
            # this fold's only use here, and `_create_node_scoped`, which only this method calls,
            # folded the identical prefix again a few statements later for its proposal — measured,
            # every one of those (44-60 per 60-node toy run) was a repeat. Handed down rather than
            # cached: it is this call's own object, and nothing reads or writes it in between.
            folded = (trace_events, trace_state)
        with self.tracer.span(
                "create_node", new_trace=True, node_id=trace_node_id,
                generation=0, operator=action.get("kind")), \
                handoff_scope(enabled=self._phase_handoff_summary):
            if precoded is not None:
                # Layer 5: the isolated producer already completed every slow role call.  Keep the
                # ordinary path below literally unchanged; this main-task branch only commits the
                # exact buffered result and its durable speculative marker.
                return self._create_precoded_node(
                    action,
                    reserved,
                    precoded,
                    max_eval_seconds=precoded_max_eval_seconds,
                )
            # doc 52 row 19: a routed action builds under its arm's model — a ContextVar the
            # client reads per call, scoped to this build (a worker thread's copied context).
            with model_override(self._model_for_arm(action)):
                return self._create_node_scoped(
                    action, roles, reserved, preproposed=preproposed,
                    pretelemetry=pretelemetry, folded=folded, already_gated=already_gated)

    def _model_for_arm(self, action: dict) -> Optional[str]:
        """The model id an action's `_model` arm names, or None for the default arm / no arm /
        an arm this engine was not configured with (the build then runs on the configured model)."""
        arm = action.get(META_MODEL) if isinstance(action, dict) else None
        if not isinstance(arm, str) or arm == DEFAULT_MODEL_ARM:
            return None
        entry = self._model_arms.get(arm)
        return entry[0] if entry else None

    def _create_node_scoped(self, action: dict, roles=None, reserved=None, preproposed=None,
                            pretelemetry=None, folded=None, already_gated: bool = False) -> None:
        # Variant-1 parallel build: `roles` is a per-build (researcher, developer) pair from the pool
        # (isolated per-build state so concurrent drafts don't clobber each other's hints/last_files);
        # `reserved` is a pre-reserved (state, id, kind, parents, parent_generations) tuple (the parallel
        # path reserves ids up front, serially, then fans out). `preproposed` (Phase 2) is a draft Idea
        # the shared researcher already proposed + novelty-gated in the batch pass (`_propose_batch`), so
        # the fan-out only IMPLEMENTS it. All default to the serial behaviour.
        researcher, developer = roles if roles is not None else (self.researcher, self.developer)
        if reserved is None:
            # `folded` is `_create_node`'s own `(events, state)` for this build (EVT-04): the same
            # prefix this line used to re-read and re-fold. The gap it widens is a few statements
            # with no I/O — nothing beside the paid propose this snapshot already outlives, which
            # is what the reservation fences below exist for.
            if folded is not None:
                proposal_events, proposal_state = folded
            else:
                proposal_events = self.store.read_all()
                proposal_state = fold(proposal_events)
            # Both halves of the score fence, from THIS fold — the paid propose below runs between
            # here and the reservation. See `card_reservation.scored_anchor`.
            _proposal_anchor_id, _proposal_anchor_attempt = scored_anchor(proposal_state)
            if self._build_parent_snapshot(proposal_state, action) is None:
                return
            prospective_node_id = self._node_id_ceiling(proposal_events, proposal_state)
            source = "engine" if action.get("kind") == "merge" else "researcher"
            # The PROSPECTIVE id, not a real one: this whole phase runs before `node_building` is
            # appended, which is the reason it is the invisible one — the UI has no node to draw yet,
            # so it falls through to "Planning next experiment…" for however long the Researcher
            # takes. `prospective: True` marks the id as the ceiling's guess so a reader never treats
            # it as a committed node; the `reserve` beacon below carries the id that was actually
            # taken, and the two agree except when a concurrent build wins the id first.
            with self._progress(PROGRESS_STAGE_BUILD, "propose", node_id=prospective_node_id,
                                prospective=True, operator=action.get("kind")):
                idea = self._prepare_node_idea(
                    action, proposal_state, researcher=researcher,
                    prospective_node_id=prospective_node_id,
                    source=source, proposal_events=proposal_events, preproposed=preproposed,
                    already_gated=already_gated, drop_repeated_duplicate=True)
            if idea is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            steering_context = normalize_steering_context(
                getattr(researcher, "_steering_context", []))
            if steering_context is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
            with self._progress(PROGRESS_STAGE_BUILD, "reserve", node_id=prospective_node_id,
                                prospective=True, operator=action.get("kind")):
                reserved = self._reserve_on_main_task(
                    action, idea, scored_against=_proposal_anchor_id,
                    scored_against_attempt=_proposal_anchor_attempt,
                    source=source, steering_context=steering_context,
                    # The ordinary build spine, and the ONE site that commits an attach. `_link` above
                    # planned with the same flag, so a `debug` re-attempt of a question card-N already
                    # asks becomes another node under card-N instead of a byte-identical twin. Spelled
                    # here rather than defaulted inside the reservation: four other callers reach that
                    # method and none of them may attach (see `_plan_native_card`).
                    retry_attach=True)
        if reserved is None:
            # A RECEIPT, because this branch spends money and used to leave nothing behind.
            # `_reserve_node_build` returns None when a control/research/lifecycle row won its CAS,
            # and returning to the selection boundary is the CORRECT answer there — minting a
            # replacement for a just-dropped orphan would defeat an operator's stop intent. What was
            # wrong is the silence: the proposal above is already PAID FOR, and this returned with no
            # node, no card and no row, so the loss was invisible in the log and unmeasurable after
            # the fact. `offloaded-serial-build-reserves-off-the-main-task` is the loss itself;
            # this only makes it countable.
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
            # `_progress` is a CONTEXT MANAGER: a bare call builds a generator and emits nothing.
            # The first cut of this receipt was exactly that no-op, and it was caught by driving it
            # rather than reading it — which is the same lesson this file's own guard rules state.
            with self._progress(PROGRESS_STAGE_BUILD, "discarded",
                                operator=str(action.get("kind") or ""),
                                reason="reservation_lost_the_cas"):
                pass
            return
        state = reserved.state
        node_id = reserved.node_id
        kind = reserved.kind
        parent_generations = reserved.parent_generations
        idea = reserved.idea.model_copy(deep=True) if reserved.idea is not None else None
        if idea is None:
            # Legacy direct reservation: retain the historical behavior for internal callers, but do
            # not pretend it produced a native Card. Production paths always prepare before reserve.
            idea = self._prepare_node_idea(
                action, state, researcher=researcher,
                prospective_node_id=node_id,
                source="engine" if action.get("kind") == "merge" else "researcher",
                proposal_events=self.store.read_all(), preproposed=preproposed,
                already_gated=already_gated)
            if idea is None:
                self._discard_node_build_telemetry(researcher=researcher, developer=developer)
                return
        # Phase-handoff ledger for THIS node build: propose → stages → plan → implement each distill
        # their transcript into a brief the next phase reads (see agents.agent.run_phase), so later
        # phases trust what earlier ones explored instead of re-reading the repo. Node-scoped (fresh
        # per build), and a no-op when the setting is off.
        with self.tracer.span("materialize_node", node_id=node_id, operator=kind):
            # node_building was appended inside _reserve_node_build (under _id_lock) — the id is committed
            # to the log atomically, so a PARALLEL build (parallel_build>1) can never pick the same id.
            # Restore THIS pre-proposed idea's own FOREAGENT telemetry after main-task reservation and
            # before the worker's audit emitters consume it.
            if pretelemetry:
                for _attr, _val in pretelemetry.items():
                    if _val is not None:
                        try:
                            setattr(researcher, _attr, _val)
                        except Exception:  # noqa: BLE001
                            pass
            # Per-call output: never let a reused wrapper/backend leak another node's resource
            # finalization into this build. The clear is `_run_developer`'s, under the instance's
            # own lock, because at THIS site it would be an unlocked write to a shared Developer.
            if kind == "draft":
                parents: list[int] = []        # not whatever label the LLM returns
                # The progress beacon rides ON the existing tracer span rather than nesting inside
                # it: same boundary, same body, no reindentation of code whose comments are
                # load-bearing. The span feeds `spans.jsonl` (a per-node trace an operator opens
                # after the fact); the beacon feeds the live strip, which is the surface that was
                # blank while this call ran.
                with self.tracer.span("implement"), self._progress(
                        PROGRESS_STAGE_BUILD, "implement", node_id=node_id, operator=kind):
                    built = self._implement_result(
                        self._directed_idea(idea.model_copy(deep=True), state),
                        developer=developer, state=state)
            elif kind == "merge":
                parents = list(action["parent_ids"])
                # A0b: real ensembling (code recombination) when configured/Strategist-selected;
                # else the legacy mean-param merge. Toy/baseline developers degrade to mean.
                pnodes = [state.nodes[i] for i in parents]
                with self.tracer.span("implement"), self._progress(
                        PROGRESS_STAGE_BUILD, "implement", node_id=node_id, operator=kind):
                    # A code-ensemble merge must SEED from the primary parent's solution (like improve),
                    # not implement() from scratch: from-scratch gave the Developer no base, so the
                    # ensemble node shipped without the agent-authored eval entrypoint and crash-failed
                    # ("can't open file test_looplab.py" — live node 63, 3 repairs couldn't recover). Now
                    # parent[0]'s working code + entrypoint carry over and the idea directs blending in
                    # the other parent. Mean-param merges (numeric tasks, no files) stay from-scratch.
                    _didea = self._directed_idea(
                        idea.model_copy(deep=True), state)   # §1: directives steer the merge code too
                    built = self._implement_result(
                        _didea,
                        pnodes[0] if self._merge_mode == "ensemble" and pnodes else None,
                        developer=developer, state=state,
                        # doc 52 row 18: the other lineages, code and traces, not a 120-char digest
                        co_parents=pnodes[1:] if self._merge_mode == "ensemble" else ())
            # The `debug` build branch is GONE (F5). It called `developer.repair` on a FRESH node
            # seeded from the failed parent's files — inline repair with a node-budget slot attached
            # to it. Its whole justification was that the in-node loop had a fixed count and had to
            # hand off somewhere when the count ran out; F8 removed the count, so the hand-off has
            # nowhere to go and no reason to exist. `_prepare_node_idea` refuses the kind before a
            # build is ever reached, so this branch was unreachable as well as unwanted.
            else:  # improve
                parent = state.nodes[action["parent_id"]]
                parents = [parent.id]
                with self.tracer.span("implement"), self._progress(
                        PROGRESS_STAGE_BUILD, "implement", node_id=node_id, operator=kind):
                    built = self._implement_result(
                        self._directed_idea(idea.model_copy(deep=True), state), parent,
                        developer=developer, state=state)
            # THE ENVELOPE (doc 52 row 12): everything this build recorded about itself is read off
            # the `DeveloperResult` the call returned, captured under the instance's lock in the
            # same breath as the call — never off the instance afterwards, which is what makes the
            # serial lane safe to run off the loop thread beside a repair on the shared Developer.
            code = built.code
            idea, footprint_finalized = self._finalize_developer_footprint(
                idea, developer, code, footprint=built.last_footprint)
            # 💡 deep-research provenance: tag the first couple of nodes created right after a research
            # memo (its directions are the active steering) so the UI can show WHERE research landed in
            # the tree. Audit/UI only — never affects search. Coarse-but-honest (temporal proximity).
            research_origin = None
            if state.research:
                _m = state.research[-1]
                _ra = _m.get("at_node")
                if _ra is not None and _ra <= node_id < _ra + 2:
                    from looplab.core.advisory_payloads import valid_advisory_ref
                    _memo_id = _m.get("memo_id")
                    research_origin = {
                        "at_node": _ra,
                        "trigger": _m.get("trigger"),
                        **({"memo_id": _memo_id}
                           if valid_advisory_ref(_memo_id, "memo") else {}),
                    }
            # Read off the pre-build snapshot, exactly as before, and hoisted above the commit only
            # because the emit inside it needs the answer: an abort already recorded when this slot
            # was reserved. A pure read of `state`, so its new position cannot change it.
            materialize_abort = node_id in state.aborted_nodes
            if not self._commit_built_node(
                    node_id=node_id, generation=0, card_id=reserved.card_id,
                    parents=parents, parent_generations=parent_generations,
                    idea=idea, code=code,
                    files=dict(built.last_files),                # the envelope's, never the instance's
                    deleted=list(built.last_deleted),
                    footprint_finalized=footprint_finalized,
                    stale_error="parent lifecycle changed while building",
                    rejected_error="node creation was rejected during replay",
                    researcher=researcher, developer=developer,
                    research_origin=research_origin,
                    # doc 52 row 19: the arm this build was routed to (omitted when none was)
                    model_arm=(action.get(META_MODEL) if isinstance(action.get(META_MODEL), str)
                               else _OMIT_ARM),
                    # Variant-1: read the receipt THIS build stamped on its own researcher (set under
                    # `_advisory_lock` in `_set_complexity_hint`), so a concurrent sibling draft's advisory
                    # write to `self._cross_run_advisory_receipt` can't mis-stamp this node. Falls back to
                    # the shared attr only when a path never refreshed it (attr genuinely absent).
                    cross_run_receipt=(_rcpt if (_rcpt := getattr(researcher, "_cross_run_advisory_receipt", None))
                                       is not None else getattr(self, "_cross_run_advisory_receipt", {})),
                    # A legacy generation-less abort may intentionally reserve a not-yet-created slot.
                    # Mark only an intent already present in the reservation snapshot. An abort that lands
                    # after node_building is a losing-worker race and deliberately gets no escape hatch.
                    **({"materialize_aborted_intent": True}
                       if materialize_abort else {}),
            ):
                return
            if materialize_abort:
                # Preserve the already-recorded operator intent as the first terminal for this newly
                # materialized lifecycle. This also keeps a Developer-error sentinel from stealing the
                # terminal with an unrelated crash/pause after the operator had already cancelled it.
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": "aborted by operator",
                    "reason": "aborted", "eval_seconds": 0.0,
                })
            # The Developer session CRASHED when its code is the "(developer error: …)" sentinel (an
            # exception in _run — e.g. an LLM 401/timeout). FAIL the node now: without this it stays
            # pending, and the eval runs the PARENT's carried-over entrypoint and inherits the PARENT's
            # metric — a false success that pollutes the search (the 401-window nodes 50-54 each faked
            # the parent's 0.81 this way). node_created → node_failed keeps the one-terminal invariant.
            # THE MODEL RAN OUT OF MOVES, WHICH IS NOT THE PROVIDER DYING. `empty_build_refusal`
            # convicts a fresh build that wrote no candidate, and until 2026-08-28 it spelled that
            # with the CRASH sentinel — so a session that probed 24 times and never called
            # `write_file` paused the whole run through the provider circuit breaker.
            # `core/models.py::DEVELOPER_STUCK_PREFIX` already states the rule this violates: "(developer error: …)
            # routes to the provider circuit breaker and pauses the RUN, which is exactly the wrong
            # answer for a healthy model that has simply run out of ideas about one node."
            #
            # Measured on the probe corpus: 2 of 106 nodes ended this way (dsNew2 node 2, qwen38f
            # node 0) and BOTH ended their run. dsNew2 stopped at 2 evaluated nodes of 3 on a
            # gateway that answered every call, which at 2-4 nodes per $1 run is a third of it.
            #
            # This branch must come FIRST and must not fall through: `is_developer_error` does not
            # match the stuck spelling, and the fresh-build path knew only that one, so a bare
            # respelling in the refusal would drop the sentinel into "this is solution code" — the
            # false-success path the comment below was written for.
            elif is_developer_stuck(code):
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": code,
                    # NOT `developer_crash`: that reason is what the circuit breaker reads. This node
                    # is finished and the run is not — the next proposal is free to try something else.
                    "reason": "developer_stuck", "eval_seconds": 0.0,
                })
            elif is_developer_error(code):
                # Terminal only — see `_request_create_pause` below for why the pause is queued.
                crash_terminal, _crash_pause = developer_crash_records(
                    node_id, 0, code,
                    "auto-paused: a Developer session crashed (LLM unreachable or a hard error, "
                    "unresolved within the node) — resume once it's fixed")
                self.store.append(crash_terminal[0], crash_terminal[1])
                # Circuit-breaker — PAUSE on the FIRST developer_crash. A developer_crash means the
                # Developer couldn't finish THIS node even after the LLM client's own within-call retries
                # (429 / 5xx / throttle-403 all back off + retry): a problem that a NEW node can't fix
                # (LLM unreachable, or a hard error), NOT a bad experiment. One node = one experiment; if
                # it can't be resolved within the node, stop the whole run rather than rapid-fire more
                # dead nodes (the 403 blowout spun 67 of them). Freeze (not finish) so a plain `resume`
                # continues once the cause is resolved — no premature report/lessons.
                # REQUESTED, not appended here. On the parallel-build fan-out (`_pb_pairs`) this
                # method runs in an `anyio.to_thread` WORKER thread, and EV_PAUSE is a FOLDED,
                # run-GLOBAL, selection-affecting event — outside invariant #1's documented worker
                # seam (a worker may append only its OWN node's node_created / node_failed /
                # per-node audit) and not in BACKGROUND_APPENDABLE or DIAGNOSTIC_EVENTS. It is
                # splice-neutral for the `paused` flag alone, but NOT against a concurrent EV_RESUME
                # (which folds `paused=False`): a worker's byte position relative to an external
                # control is nondeterministic. `_request_create_pause` records the intent; the MAIN
                # task appends it where it already observes `_create_paused`, after the join.
                # "FIRST" is `developer_crash_pause_after`'s default; above it, this crash requests
                # the pause only when its position in the log reaches the run's threshold, and a
                # crash below it keeps exactly the terminal appended above (`developer_crash_rank`).
                if self._developer_crash_pause_due(fold(self.store.read_all()), node_id):
                    self._request_create_pause(
                        node_id,
                        "auto-paused: a Developer session crashed (LLM unreachable or a hard "
                        "error, unresolved within the node) — resume once it's fixed")
        self._consume_node_build_telemetry(
            node_id, 0, researcher=researcher, developer=developer, report=built.last_report,
            audit_extra=built.audit_extra, foresight_pick=built.last_foresight_pick)

    def _consume_node_build_telemetry(self, node_id: int, generation: int,
                                      *, researcher=None, developer=None,
                                      report=AuditMixin._REPORT_OMITTED,
                                      audit_extra=AuditMixin._REPORT_OMITTED,
                                      foresight_pick=AuditMixin._REPORT_OMITTED) -> None:
        """Attribute this build's role telemetry to the node it belongs to, then clear it.

        All three creation paths end with this triple, and it is the CONSUMING half of the pairing
        `_discard_node_build_telemetry` performs on every failure path. Skipping it is not inert: a
        "propose" reset re-runs the researcher (setting last_hyp_priority/last_foresight), and the
        pick set left behind then leaks onto the NEXT created node's id — the exact mis-attribution
        `_emit_role_telemetry` exists to prevent. Because it is three separate emits with no shared
        name, a path could quietly keep two of them and lose the third; here they move together.

        Variant-1: pass THIS build's pooled roles so concurrent draft builds cannot cross-wire each
        other's telemetry (last_report / last_hyp_priority / last_foresight). For the serial paths
        `researcher`/`developer` ARE `self.researcher`/`self.developer`, so omitting them is
        byte-identical to passing them.
        """
        # `report=` is THIS build's envelope copy when the caller has one — see
        # `_emit_agent_report` for the unlocked window the instance read still sits in.
        self._emit_agent_report(node_id, report=report, audit_extra=audit_extra,
                                **({"developer": developer} if developer is not None else {}))
        self._emit_hypothesis_ranked(
            node_id, generation, **({"researcher": researcher} if researcher is not None else {}))
        self._emit_foresight_selected(
            node_id, generation, foresight_pick=foresight_pick,
            **({"researcher": researcher} if researcher is not None else {}),
            **({"developer": developer} if developer is not None else {}))

    def _commit_built_node(self, *, node_id: int, generation: int, card_id: Optional[str],
                           parents: list, parent_generations: Mapping, idea, code: str,
                           files: dict, deleted: list, footprint_finalized: bool,
                           stale_error: str, rejected_error: str,
                           check_node_lifecycle: bool = False, strict_landing: bool = False,
                           stamp_generation: bool = False, drop_card: bool = True,
                           append_failure_error: Optional[str] = None,
                           researcher=None, developer=None, **emit_extra) -> bool:
        """Commit ONE finished build as its `node_created`, or close the reservation (doc 25 ES-02).

        The three creation paths — `_create_node_scoped`, `_rerun_node`, `_create_injected_node` —
        each hand-coded the same three-stage epilogue: re-fold and refuse a build whose parents (and,
        on a rerun, whose own lifecycle) moved while the Developer worked; emit the `node_created`;
        re-fold and refuse a node the fold did not accept. That triplication is what forced the
        false-success sentinel guard to be retrofitted into all three copies SEPARATELY, and it had
        already let the copies drift mechanically. The sequence now exists once; the callers keep
        only what genuinely differs — how they obtained the idea/code, and everything AFTER the node
        has landed (materialize-abort, the two Developer sentinels, telemetry consumption).

        Every keyword below is a MEASURED divergence between the three copies, not a knob:

        * `stale_error` / `rejected_error` — the two refusal sentences. They differ per path, they
          are durable operator-facing text, so they stay the callers' words.
        * `check_node_lifecycle` — a rerun re-enters an EXISTING lifecycle and must also fence its
          own node (reset again / tombstoned / aborted mid-rebuild), on the SAME fold as the parent
          check. A first creation has no prior lifecycle to lose.
        * `strict_landing` — a rerun must see THIS generation land carrying THIS build's code and no
          pending `rerun_from`; for a first landing, existing IS landing.
        * `stamp_generation` — only the rerun writes a `generation` key into the payload. The other
          two omit it (never None-fill it), which is the historical shape `_emit_node_created`'s
          docstring pins.
        * `drop_card` — a rerun keeps the original work item unless it minted a replacement card;
          `_fail_reserved_build` owns the ownership half of that rule.
        * `append_failure_error` — only the injected path recovers from an append that RAISES (the
          operator's request must not leave a bare `node_building` behind). `None` re-raises
          untouched, which is exactly what the two agent paths did: the parallel one is caught by
          `_create_node_guarded`, the serial one deliberately crashes so bugs surface in tests.
        * `researcher` / `developer` — the pooled roles of THIS build, so a concurrent draft's
          telemetry is not what gets discarded here. Omitted = the shared instance attrs.
        * `**emit_extra` — the per-path `node_created` keys (research_origin / model_arm /
          cross_run_receipt; source / origin / forked_from). A typo cannot silently enter a payload:
          `_emit_node_created` has an explicit keyword signature and raises `TypeError`.

        Returns True when the node is committed and the caller may run its post-landing epilogue;
        False when the reservation has already been closed and this build's telemetry discarded —
        the caller must return without consuming telemetry.

        Lives beside `_emit_node_created` since ENG1-04 step 4c; it had stayed in orchestrator.py
        for the reason the module-global `fold` seam comment gives — both re-folds below belong to the
        three creation paths that `monkeypatch.setattr(orch, "fold", ...)` is written to intercept.
        They still are: this module's `fold` is `shared.engine_fold`, which resolves
        `orchestrator.fold` at CALL time (`tests/test_node_commit_epilogue.py` drives that).
        """
        latest = fold(self.store.read_all())
        stale = not parent_generations_current(latest, parent_generations)
        if check_node_lifecycle and not stale:
            current = latest.nodes.get(node_id)
            stale = (current is None or current.attempt != generation
                     or current.tombstoned or node_id in latest.aborted_nodes)
        if stale:
            # Clear both the transient node owner and its immutable, now-unbuildable Card.
            self._fail_reserved_build(
                node_id=node_id, card_id=card_id, generation=generation,
                error=stale_error, reason="superseded", drop_card=drop_card)
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
            return False
        try:
            self._emit_node_created(
                node_id=node_id,
                parent_ids=parents,
                operator=idea.operator,
                idea=durable_idea_payload(idea),
                code=code,
                files=files,
                deleted=deleted,
                # Every engine-created lifecycle promises the same generation-scoped admission
                # receipt. Besides crash-safe speculative accounting, this is what lets the public
                # activity projection prove "waiting for a slot" versus "evaluating".
                eval_start_boundary=True,
                **({"generation": generation} if stamp_generation else {}),
                **({"parent_generations": parent_generations} if parent_generations else {}),
                **({"footprint_finalized": True} if footprint_finalized else {}),
                **emit_extra,
            )
        except Exception:
            if append_failure_error is None:
                raise                       # the caller's historical behaviour — see the docstring
            try:
                landed = node_id in fold(self.store.read_all()).nodes
            except Exception:  # noqa: BLE001 — a failed landing probe reads as not landed; the branch below decides
                landed = False
            if not landed:
                self._fail_reserved_build(
                    node_id=node_id, card_id=card_id, generation=generation,
                    error=append_failure_error, reason="build_crash", drop_card=drop_card)
            raise
        landed = fold(self.store.read_all()).nodes.get(node_id)
        rejected = landed is None
        if strict_landing and not rejected:
            rejected = (landed.attempt != generation or landed.rerun_from is not None
                        or landed.code != code)
        if rejected:
            self._fail_reserved_build(
                node_id=node_id, card_id=card_id, generation=generation,
                error=rejected_error, reason="superseded", drop_card=drop_card)
            self._discard_node_build_telemetry(researcher=researcher, developer=developer)
            return False
        return True

    def _create_node_guarded(self, action: dict, roles=None, reserved=None, preproposed=None,
                             pretelemetry=None) -> None:
        """Variant-1 parallel build: run one pooled build, converting an UNEXPECTED exception into a
        `node_failed` terminal for its already-reserved id (its `node_building` was appended up front
        under `_id_lock`) instead of letting the exception propagate through the task group and tear
        down — and kill — the whole run. Keeps the one-terminal-per-node invariant (the reserved id
        gets exactly one terminal) and lets the rest of the concurrent batch finish. Used ONLY on the
        parallel path; the serial path keeps its historical crash-on-raise so bugs surface in tests.

        THE SPEND CEILING IS NOT ONE BUILD'S CRASH (review 2026-09-22, ENG1-01). `BudgetExceeded`
        is an `Exception`, so the blind handler below used to record it as this reservation's
        `build_crash` and let the run go on proposing — every proposal a paid call against an
        accountant already over its limit — where the serial path ends the run on the same raise.
        It now PROPAGATES, whether raised bare or wrapped (`core/errors.py::budget_stop_leaf`),
        and both callers run this under `_DeferredBudgetStop`, which holds it until the fan-out
        joins and re-raises it on the MAIN task: the siblings already paid for land their nodes,
        and the run then ends `budget_exhausted` exactly as a serial build's raise does. The
        reservation that raised is left OPEN, as the serial path leaves it, for
        `_recover_interrupted_builds` to close on the next entry."""
        try:
            self._create_node(action, roles, reserved, preproposed=preproposed,
                              pretelemetry=pretelemetry)
        except BudgetExceeded:
            raise                    # the run's stop — deferred to the join by the caller, above
        except Exception as exc:  # noqa: BLE001 — one build's crash must not abort the concurrent batch
            stop = budget_stop_leaf(exc)
            if stop is not None:
                # The same stop, wrapped (a task group, or a `raise … from` inside a role). Hand the
                # deferral the LEAF: `_DeferredBudgetStop` captures by class, and the CLI derives
                # `budget_exhausted` from the leaf either way.
                raise stop
            node_id = reserved[1] if reserved else None
            if node_id is None:
                return
            latest = fold(self.store.read_all())
            node = latest.nodes.get(node_id)
            # Synthesise a terminal ONLY when this id has no node_created yet (a bare node_building whose
            # build raised before landing). A node that ALREADY has node_created carries real generated
            # code and is `pending` — the exception then came from the post-creation audit emitters
            # (audit-only); leave it for the evaluator. Marking a built, code-carrying node `failed` here
            # would silently discard a good build (review finding #2). If _create_node already wrote a
            # terminal (developer-crash sentinel, or node_evaluated), likewise nothing to do.
            if node is None:
                try:
                    self._fail_reserved_build(
                        node_id=node_id,
                        card_id=getattr(reserved, "card_id", None),
                        generation=0,
                        error=f"(build error: {exc})",
                        reason="build_crash",
                    )
                    # An EXCEPTION out of a build (not the graceful "(developer error: …)" sentinel) is a
                    # HARD fault — an LLM client that RAISES on a 401/outage, or a real bug in implement().
                    # The serial path crashes the run on such a raise; under concurrency we can't crash
                    # (it would kill sibling builds), so mirror the developer_crash circuit-breaker: PAUSE
                    # so the batch loop stops after this chunk instead of burning the node budget on
                    # repeated build_crash nodes (review finding #3). A plain resume continues once fixed.
                    # Same worker-seam reason as the developer_crash branch above: request the
                    # global pause, let the main task append it after the join.
                    # NODE-LESS (`None`), because this id never reached `node_created`: a pause
                    # naming it is dropped by `replay._on_pause` (review 2026-09-22, ENG1-01 — see
                    # `_request_create_pause`), which is how this breaker never engaged at all.
                    self._request_create_pause(
                        None,
                        "auto-paused: a node build raised (LLM unreachable or a hard error, "
                        "unresolved within the build) — resume once it's fixed")
                except Exception:  # noqa: BLE001 — best-effort terminal; never re-raise into the group
                    pass

    async def _offload_build(self, fn) -> None:
        """Run ONE paid build off the event-loop thread, on the proposal pool.

        THE SERIAL LANE HELD THE LOOP (doc 52 row 12; the marker that stood above
        `_handle_create_actions`). The 2026-08-29/31 offloads moved the two propose lanes and left
        the Developer call of the serial build, the fork's build and the node-reset rebuild on the
        loop thread — driven, a fork served with an adopted eval and a width-2 Card claim with node
        0 in flight each ran the paid call with ZERO ticks, and the critic's re-read of the marker's
        own file found the harm: a dead node waited 62 minutes for its terminal while both H200s
        idled, because the loop was inside a build. `_occupancy_paced_creates` delivers work here
        precisely when an evaluation is burning, which is when the loop must keep turning.

        ONE helper for the four sites, on `proposal_limiter()` (anyio's shared 40-token default is
        held by every in-flight `_run_eval` for its whole multi-hour duration, so a build queued on
        it could wait behind the evaluations it exists to feed). A bare `to_thread` and not the
        proposal SINK: the build's appends are its OWN node's — `node_building`, `node_created`,
        `node_failed`, the per-node audit — which is exactly the worker seam invariant #1 licenses
        for the concurrent fan-out, and the run-global pause it may need goes through
        `_request_create_pause` and is drained by the caller on the main task, as the fan-out's
        already is. The build's outputs come back through the `DeveloperResult` envelope, so a
        repair on the shared Developer running in another worker cannot clobber what this build
        read (`agents/roles.py::DeveloperResult`). A raise propagates to the caller unchanged: the
        serial path keeps its historical crash-on-raise so bugs surface in tests.
        """
        from looplab.engine.novelty import proposal_limiter
        # UNDER THE PROPOSAL SINK, not a bare `to_thread`. The paragraph above argued the bare form
        # on the ground that "the build's appends are its OWN node's — `node_building`,
        # `node_created`, `node_failed`, the per-node audit — which is exactly the worker seam
        # invariant #1 licenses". True of those four and false of the path: `_create_node` reaches
        # `_prepare_node_idea` -> `_apply_novelty_gate` -> `_append_proposal_event`, whose sink is
        # unset in this worker, so `novelty_rejected` / `novelty_graded` / `cross_run_prior` fell
        # through to `store.append` from a thread. None is in `BACKGROUND_APPENDABLE`,
        # `SETUP_THREAD_APPENDABLE` or `ASSISTANT_APPENDABLE`, and they are exactly the
        # authority-bearing rows `speculation._proposal_authority_seq` keys on — a row landing
        # inside a concurrent reservation's window discards a proposal the run already paid for.
        # This is the SAME breach the 2026-08-29 card-lane and 2026-08-30 batch-lane fixes each
        # closed, arriving a third time through the lane that offloaded last.
        #
        # The helper buffers those intents and publishes them from the MAIN task on the way out; the
        # node's own four appends are untouched and stay exactly as licensed.
        # MARKED, so `_reserve_node_build` inside this worker knows to marshal its CAS back onto
        # the loop — see `_reserve_on_main_task`.
        def _marked_build():
            _OFFLOADED_BUILD.value = True
            try:
                return fn()
            finally:
                _OFFLOADED_BUILD.value = False

        await self._offload_under_proposal_sink(_marked_build, limiter=proposal_limiter())

    def _reserve_on_main_task(self, *args, **kwargs):
        """`_reserve_node_build`, always on the MAIN task, whichever thread asks.

        THE RESERVATION IS NOT THE WORKER'S TO MAKE. It appends `card_added` and `node_building` —
        both FOLDED, both in none of the five writer registries, and `events/types.py` says of the
        Card ledger in so many words: "Main-task-written; NONE are BACKGROUND_APPENDABLE (a
        monotonic card_id cannot be background-minted)". When `_offload_build` moved the whole of
        `_create_node` into a worker (2026-09-06) it took the reservation with it, and both those
        appends started coming off the main task against that statement.

        The money half is worse and was driven: `_proposal_authority_seq` fences the CAS retries on
        seq EQUALITY, and its docstring justifies that by the window being "microseconds long"
        with "nothing paid at risk in it" — true while `_create_node` froze the loop, false once it
        ran in a worker with the loop still turning. Isolated 2x2, 40 trials a cell, real Engine and
        real store, the racer appending the BACKGROUND_APPENDABLE `research_completed`:

            racer=off offload=off  0/40 | racer=off offload=on  0/40
            racer=on  offload=off  0/40 | racer=on  offload=on  38/40 PAID PROPOSALS LOST

        and 8 % of nodes in a real wide CLI run — silently: no node, no card, no row.

        Marshalling back is the fix that restores BOTH halves at once, and it is why widening the
        fence's exclusion list was refused: that would have bought the money half and left
        `card_added` still minted in a worker. It also restores the fence's own premise rather than
        working around it — on the main task the window really is microseconds again.

        The paid work stays off the loop. Only the CAS comes back, which is what the loop was free
        for."""
        call = functools.partial(self._reserve_node_build, *args, **kwargs)
        if getattr(_OFFLOADED_BUILD, "value", False):
            return anyio.from_thread.run_sync(call)
        return call()

    def _commit_rerun_card_on_main_task(self, **kwargs) -> _RerunCardCommit:
        """`_commit_rerun_card`, always on the MAIN task, whichever thread asks.

        The re-proposal's twin of `_reserve_on_main_task`, and for the same reason (review
        2026-09-22, ENG1-05): `_rerun_node` has run in an `_offload_build` worker since 2026-09-06,
        so its Card swap — `card_auto_dropped` + `card_added` + `node_building` — came off the
        worker as three separate appends with no tail CAS, against the Card ledger's
        "main-task-written" contract (`events/types.py`). It cannot reuse `_reserve_node_build`
        itself: that mints a NEW node id, and a reset rebuilds an EXISTING one at its new
        generation. The paid Developer call stays in the worker; only the commit comes back."""
        call = functools.partial(self._commit_rerun_card, **kwargs)
        if getattr(_OFFLOADED_BUILD, "value", False):
            return anyio.from_thread.run_sync(call)
        return call()

    def _commit_rerun_card(self, *, node_id: int, generation: int, operator: str,
                           parents: list, parent_generations: Mapping, idea,
                           steering_context) -> _RerunCardCommit:
        """Replace a re-proposed node's Card and claim its rebuild: ONE `append_many`, one tail CAS.

        The plan and the commit are the ones `_rerun_node` made inline under `_id_lock` — the
        lifecycle fence, `_plan_native_card` with the superseded card named, the ownership check
        before the drop — moved, not changed, with two differences that are the fix. The rows land
        as one batch, so another writer lands before or after the swap and never between the
        drop and its replacement (a torn tail exposes none of them). And the batch is CAS'd on the
        tail the plan read: a row that lands meanwhile makes the commit RE-PLAN against it rather
        than be written over, which the three bare appends could not do.

        The parent half of the fence is `parent_generations_current`, the one spelling the other
        creation sites share, instead of this path's own inline copy (review 2026-09-22, ENG1-11).
        """
        with self._id_lock:
            def _plan(events, tail) -> _RerunCardCommit:
                latest = fold(events)
                current = latest.nodes.get(node_id)
                if (current is None or current.attempt != generation
                        or current.rerun_from != "propose" or current.tombstoned
                        or node_id in latest.aborted_nodes
                        or not parent_generations_current(latest, parent_generations)):
                    return _RerunCardCommit("stale", latest, None)
                plan = self._plan_native_card(
                    events, latest, idea, parents=parents,
                    parent_generations=parent_generations,
                    scored_against=latest.best_node_id, source="researcher", at_node=node_id,
                    steering_context=steering_context,
                    superseded_card_id=current.idea.card_id,
                )
                if plan.disposition not in {"mint", "reuse"}:
                    return _RerunCardCommit("rejected", latest, plan)
                rows = []
                # THE SAME OWNERSHIP CHECK the refusal path four lines up routes through (via
                # `_fail_reserved_build`). `_drop_card_once` has none of its own, so dropping
                # unconditionally destroyed a card that a DIFFERENT node had attached to — a
                # debug re-attempt landing on the same work item — and a dropped card is
                # unrecoverable, because `_retry_attach_card` refuses `dropped` forever. The
                # later repair then minted a byte-identical twin: exactly the duplicate work
                # item the attach disposition exists to prevent. Reproduced end-to-end (mint on
                # node 0, attach on node 1, `node_reset from_stage=propose` on node 0).
                # Fail-closed here means the superseded card survives as proposed inventory,
                # which is the cost `_reservation_minted_card`'s own docstring prices against
                # deleting somebody else's finished work item.
                # (The drop row is `_card_auto_drop_row`'s — `_drop_card_once`'s own idempotence
                # rule without its append — so it can ride this batch.)
                if self._reservation_minted_card(events, node_id, current.idea.card_id):
                    drop = self._card_auto_drop_row(events, current.idea.card_id,
                                                    reason="reproposed")
                    if drop is not None:
                        rows.append(drop)
                if plan.disposition == "mint":
                    rows.append((EV_CARD_ADDED, plan.payload))
                rows.append((EV_NODE_BUILDING, {
                    "node_id": node_id, "generation": generation,
                    "operator": operator, "parent_ids": parents,
                    "card_id": plan.card_id,
                }))
                self.store.append_many(rows, expected_last_seq=tail)
                return _RerunCardCommit("reserved", latest, plan)

            committed = retry_tail_cas(
                self.store, _plan, on_exhaust=lambda: _RerunCardCommit("lost", None, None))
            if committed.outcome == "rejected":
                # Closed OUTSIDE the CAS loop (it appends through its own) but still under
                # `_id_lock` and on this task, exactly where the inline block closed it.
                current = committed.state.nodes[node_id]
                self._fail_reserved_build(
                    node_id=node_id, card_id=current.idea.card_id, generation=generation,
                    error="replacement proposal was duplicate or outside the Card contract",
                    reason="proposal_rejected", drop_card=bool(current.idea.card_id))
            return committed

    async def _offload_node_build(self, action: dict, **kwargs) -> None:
        """`_create_node`, off the loop — see `_offload_build`."""
        await self._offload_build(functools.partial(self._create_node, action, **kwargs))

    @in_llm_lane("build")
    def _rerun_node(self, node: Node, state: RunState) -> None:
        """node_reset "propose"/"implement": re-run this EXISTING node id IN PLACE (never mints a new
        id — the whole point is to FIX a node, not proliferate). "implement" keeps the Researcher's idea
        (only the Developer re-runs — the "researcher ok, developer crashed" case); "propose" re-proposes
        a fresh idea too. Emits node_building + node_created for the SAME id — the fold applies it over the
        reset (clearing the rerun marker), the node goes pending-with-code, and the eval loop scores it
        next. Same developer-crash circuit-breaker as a first build. (An "eval" reset never reaches here —
        the fold left it pending-with-code and the eval dispatch re-scores it directly.)"""
        if (node.id in state.aborted_nodes or node.tombstoned
                or node.status is not NodeStatus.pending):
            return
        stage = node.rerun_from
        parents = list(node.parent_ids)
        parent = state.nodes.get(parents[0]) if parents else None
        generation = node.attempt
        parent_generations = {str(pid): state.nodes[pid].attempt for pid in parents
                              if pid in state.nodes}
        if len(parent_generations) != len(parents) or any(
                pid in state.aborted_nodes or state.nodes[pid].tombstoned for pid in parents):
            self.store.append(EV_NODE_FAILED, {
                "node_id": node.id, "generation": generation,
                "error": "parent is missing or aborted", "reason": "parent_unavailable",
                "eval_seconds": 0.0})
            return
        replacement_card = stage == "propose" and node.operator != "merge"
        with self.tracer.span(
                "create_node", new_trace=True, node_id=node.id, generation=generation,
                operator=node.operator):
            if replacement_card:
                # Re-proposal changes immutable work-item meaning. Finish the Idea first, then replace
                # the old Card with one exact native receipt while keeping the operator-requested node id.
                self._set_complexity_hint(state, parent)
                # RE-PROPOSAL: the card id genuinely is not knowable while this span is open —
                # the point of a re-proposal is that the old Card is DROPPED and a replacement is
                # minted afterwards, under `_id_lock`, from `_plan_native_card`. So this site stamps
                # the node context only. The card link is still derivable and still durable: this
                # span is nested in this node's `create_node` trace, and the `node_created` event
                # that trace ends with carries both that `trace_id` and the replacement `card_id`.
                with self.tracer.span("propose") as _span:
                    proposed = self.researcher.propose(state, parent)
                    # `None`, not `node.idea`: that idea still carries the card this path is
                    # about to drop, and stamping it would file this re-proposal under the card it
                    # REPLACED. See `stamp_proposal_span`.
                    stamp_proposal_span(_span, None, node_id=node.id)
                # THE PROPOSAL PATH'S PROVIDER CIRCUIT BREAKER, which every other proposal lane
                # crosses and this one did not (review 2026-09-22, ENG1-02): it called
                # `researcher.propose` directly, so a dead provider's degraded FALLBACK dropped this
                # node's live Card for good, minted a replacement whose STATEMENT was the provider's
                # error text (a credential it quoted back landed verbatim, three times, in
                # `events.jsonl`) and built a node from the non-proposal — and the run never paused.
                # Refused BEFORE `_id_lock`, so nothing is dropped, minted or reserved: the node keeps
                # its Card and its `rerun_from`, and a `resume` once the endpoint is back retries the
                # reset. QUEUED (`main_task=False`): this runs in an `_offload_build` worker, and the
                # loop drains the queue right after the offload returns.
                if self._refuse_degraded_proposal(proposed, main_task=False):
                    self._discard_node_build_telemetry()
                    return
                idea = self._canonicalize_idea_operator(proposed, node.operator)
                if idea is None:
                    self._fail_reserved_build(
                        node_id=node.id, card_id=node.idea.card_id, generation=generation,
                        error="researcher returned no replacement proposal",
                        reason="proposal_rejected", drop_card=bool(node.idea.card_id))
                    return
                idea = idea.model_copy(deep=True, update={
                    "card_id": None,
                    # Re-proposal is the same Researcher-owned action boundary as a fresh proposal.
                    # Persist the governed value so rerun receipts cannot diverge from execution.
                    "eval_timeout": self._effective_researcher_eval_timeout(idea),
                })
                # THE CARD SWAP AND THE CLAIM ARE THE MAIN TASK'S, as ONE CAS'd batch (review
                # 2026-09-22, ENG1-05): this method runs in an `_offload_build` worker, and the
                # inline `_id_lock` block that stood here appended `card_auto_dropped`,
                # `card_added` and `node_building` from it, separately and with no tail CAS. The
                # plan, its fences and its ownership check (with its reproduction) moved unchanged
                # into `_commit_rerun_card`. `_steering_context` is read HERE, off the same
                # Researcher the proposal above just ran on, before the hop.
                committed = self._commit_rerun_card_on_main_task(
                    node_id=node.id, generation=generation, operator=node.operator,
                    parents=parents, parent_generations=parent_generations, idea=idea,
                    steering_context=getattr(self.researcher, "_steering_context", []))
                if committed.outcome != "reserved":
                    self._discard_node_build_telemetry()
                    return
                state = committed.state
                idea = committed.plan.idea
                active_card_id = committed.plan.card_id
            else:
                # An implement reset keeps immutable Idea/Card identity and only re-runs Developer.
                idea = node.idea.model_copy(deep=True)
                active_card_id = idea.card_id
                building_payload = {
                    "node_id": node.id, "generation": node.attempt,
                    "operator": node.operator, "parent_ids": parents,
                }
                if active_card_id:
                    building_payload["card_id"] = active_card_id
                self.store.append(EV_NODE_BUILDING, building_payload)
            with self.tracer.span("implement"):
                # §1: a reset RE-BUILDS the node from scratch, so standing operator directives must
                # steer its code too — same as the four _create_node build sites.
                built = self._implement_result(
                    self._directed_idea(idea.model_copy(deep=True), state), parent, state=state)
            code = built.code
            idea, footprint_finalized = self._finalize_developer_footprint(
                idea, self.developer, code, footprint=built.last_footprint)
            if not self._commit_built_node(
                    node_id=node.id, generation=generation, card_id=active_card_id,
                    parents=parents, parent_generations=parent_generations,
                    idea=idea, code=code,
                    files=dict(built.last_files),
                    deleted=list(built.last_deleted),
                    footprint_finalized=footprint_finalized,
                    stale_error="node lifecycle changed while rebuilding",
                    rejected_error="rebuilt node creation was rejected during replay",
                    # A reset re-enters an EXISTING lifecycle, so the fence is wider than the two
                    # first-creation paths': the node itself may have been reset again, tombstoned or
                    # aborted while the Developer worked, and the landing must be THIS generation
                    # carrying THIS build's code — a bare "the id exists" would accept the previous
                    # attempt's node as proof that the rebuild landed.
                    check_node_lifecycle=True, strict_landing=True, stamp_generation=True,
                    # The original work item survives a rerun; only a re-proposal that MINTED a
                    # replacement card may close the one it superseded (`replacement_card`).
                    drop_card=replacement_card,
            ):
                return
            if is_developer_stuck(code):
                # SAME DISTINCTION AS THE FRESH-BUILD PATH ABOVE. The model ran out of moves on this
                # node; the provider is fine. Terminalize the node and let the run continue -- the
                # crash branch below would route it to the circuit breaker and pause everything.
                # Found by the cross-module invariant in
                # `tests/test_empty_build_is_stuck_not_a_crash.py`, which counted three readers of
                # the crash sentinel here against one of the stuck one -- my own c11251a1 taught
                # only the first of the three.
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node.id, "generation": generation,
                    "error": code, "reason": "developer_stuck", "eval_seconds": 0.0,
                })
                self._discard_node_build_telemetry()
                return
            if is_developer_error(code):
                crash_terminal, crash_pause = developer_crash_records(
                    node.id, generation, code,
                    "auto-paused: a Developer session crashed (LLM unreachable or a hard "
                    "error, unresolved within the node) — resume once it's fixed")
                # Terminal first and unconditionally; the pause beside it only once this crash
                # reaches `developer_crash_pause_after` (1 = always, the historical pair).
                self.store.append(*crash_terminal)
                if self._developer_crash_pause_due(fold(self.store.read_all()), node.id):
                    # QUEUED for the main task, not appended (review 2026-09-22, ENG1-05): this
                    # method runs in an `_offload_build` worker, and EV_PAUSE is FOLDED and
                    # run-GLOBAL — the same reason `_create_node_scoped`'s crash branch queues it.
                    # The run loop drains it right after the offload. The GENERATION is this
                    # rebuild's own: `_on_pause` binds the pause to it, and a rebuild is never 0.
                    self._request_create_pause(node.id, crash_pause[1]["reason"],
                                               generation=generation)
        self._consume_node_build_telemetry(node.id, generation, report=built.last_report,
                                           audit_extra=built.audit_extra,
                                           foresight_pick=built.last_foresight_pick)

    def _prepare_injected_node(
        self,
        state: RunState,
        req: Mapping,
    ) -> _InjectedNodePlan:
        """Purely validate and normalize an inject request before any slot/LLM wait.

        Control/API writers already enforce a stricter schema. This boundary also handles legacy or
        hand-authored event rows and deliberately mirrors the tolerant materializer semantics; it has
        no provider, Developer, filesystem, or event-log side effect.
        """

        if not isinstance(req, Mapping):
            raise ValueError("injected request must be an object")
        idea_d = dict(req.get("idea") or {})
        idea_d.setdefault("operator", "manual")
        # Coerce params to floats defensively (a manual form may send strings); drop unparseable.
        raw_params = idea_d.get("params") or {}
        if not isinstance(raw_params, dict):
            raw_params = {}
        params: dict[str, float] = {}
        for key, value in raw_params.items():
            try:
                params[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        idea_d["params"] = params

        raw_parents = req.get("parent_ids")
        if isinstance(raw_parents, list):
            parents = [parent_id for parent_id in raw_parents if parent_id is not None]
        else:
            parent_id = req.get("parent_id")
            parents = [parent_id] if parent_id is not None else []
        # REJECT an unknown id instead of dropping it. Dropping left the operator with a ROOTLESS
        # node and no error — lineage silently lost to a typo — while the comparable stale and
        # tombstoned/aborted cases both fail the request loudly. (`parent_generations` caught the
        # drop only when the caller happened to supply a snapshot.) An explicitly parentless inject
        # still works: it passes no parent_id / an empty list, which never reaches this check.
        missing = [parent_id for parent_id in parents if parent_id not in state.nodes]
        if missing:
            raise ValueError(f"no such parent node(s): {missing}")
        unavailable = [
            parent_id for parent_id in parents
            if state.nodes[parent_id].tombstoned or parent_id in state.aborted_nodes
        ]
        if unavailable:
            raise ValueError(f"parent node(s) unavailable: {unavailable}")
        parent_generations = {
            str(parent_id): state.nodes[parent_id].attempt for parent_id in parents
        }
        expected_parent_generations = req.get("parent_generations")
        if expected_parent_generations is not None:
            if not isinstance(expected_parent_generations, dict):
                raise ValueError("parent_generations must be an object")
            if len(expected_parent_generations) != len(parent_generations):
                raise ValueError("parent generation snapshot does not match parents")
            for parent_id, generation in parent_generations.items():
                if expected_parent_generations.get(parent_id) != generation:
                    raise ValueError(f"stale parent generation for node #{parent_id}")

        # AN OPERATOR MAY NOT INJECT A DEBUG NODE EITHER (F5). This surface offered
        # draft/improve/debug/merge and was the last producer left once the policies and the Card
        # lane lost theirs — and it is the one that most looks like an exception worth making, since
        # a human asked for it. It is not: what the operator gets instead is strictly better, because
        # the node they would have opened is now repaired in place with no budget slot spent and no
        # second lineage to reconcile. Raised as a `ValueError` like every other refusal here, so the
        # control path answers the operator rather than the run dying (`_drain_injects`).
        if str(idea_d.get("operator") or "").strip().lower() == "debug":
            raise ValueError(
                "debug nodes were removed on 2026-08-13: a failure is repaired inside the node that "
                "failed, for as long as the repair judgment allows. Reset the failed node to repair "
                "it again, or inject a draft/improve if this is genuinely a different experiment.")

        code = req.get("code")
        # U3 real merge: this combines Idea metadata only. Developer work remains after reservation.
        if not code and idea_d.get("operator") == "merge" and len(parents) >= 2:
            parent_nodes = [state.nodes[parent_id] for parent_id in parents]
            idea = (self._ensemble_idea(parent_nodes) if self._merge_mode == "ensemble"
                    else merge_idea(parent_nodes))
        else:
            idea = Idea(**idea_d)
        idea = idea.model_copy(deep=True, update={"card_id": None})
        # THE EXACT ACTION THE RESERVATION IS HANDED, checked here too (review 2026-09-22,
        # ENG1-07). A reservation refused on its parent snapshot now leaves the request QUEUED for
        # the next turn, which is only safe because the next turn's re-decision is THIS validator:
        # a parent set no build action can carry (a repeated id, a bool, an operator with no kind)
        # passed every check above and failed the snapshot on EVERY turn — a spin, where the old
        # receipt-first order merely burned the request. Refused before the budget wait instead.
        if self._build_parent_snapshot(state, {
                "kind": idea.operator, "parent_ids": parents,
                "parent_generations": parent_generations}) is None:
            raise ValueError("the injected operator and parents cannot form one build action")
        implementation_ref = self._implementation_ref(
            code=code,
            files=req.get("files"),
            deleted=req.get("deleted"),
        )
        return _InjectedNodePlan(
            idea,
            parents,
            parent_generations,
            code,
            implementation_ref,
        )

    def _reserve_injected_node(self, state: RunState, prepared: _InjectedNodePlan, *,
                               refusal: Optional[list] = None) -> Optional[_BuildReservation]:
        """The FREE half of an operator inject: its native Card and `node_building`, nothing paid.

        Split out of `_create_injected_node` (review 2026-09-22, ENG1-07) so `_serve_forced_requests`
        can RESERVE before it spends the request's `inject_done` receipt: a reservation that lost a
        race used to burn a request nothing had been paid for. `state` is the fold `prepared` was
        validated against and its champion is the score anchor; `_reserve_node_build._plan` re-folds
        under the CAS, so a parent or an anchor that moved since is refused THERE and named in
        ``refusal`` (see `card_reservation.py::RESERVATION_REFUSALS`), never reserved stale.
        """
        _op_anchor_id, _op_anchor_attempt = scored_anchor(state)
        idea = prepared.idea
        # `_reserve_on_main_task`, not `_reserve_node_build` itself: `_serve_forced_requests` runs
        # `_create_injected_node` in an `_offload_build` worker since review 2026-09-22 (ENG1-07),
        # and the `card_added` + `node_building` CAS is the main task's (see
        # `_reserve_on_main_task`). A direct call on the loop thread reserves in place, exactly as
        # before. The serving branch now calls THIS on the main task, before the offload, so there it
        # is the in-place case; a direct `_create_injected_node(req)` from a worker still marshals.
        return self._reserve_on_main_task(
            {
                "kind": idea.operator,
                "parent_ids": prepared.parent_ids,
                "parent_generations": prepared.parent_generations,
            },
            idea,
            scored_against=_op_anchor_id,
            scored_against_attempt=_op_anchor_attempt,
            source="operator",
            implementation_ref=prepared.implementation_ref,
            refusal=refusal,
            # NO ATTACH HERE, deliberately (`retry_attach` defaults off and this site keeps it off).
            # An operator `debug` injection against a failed node whose card is live would otherwise
            # attach — and an attach mints no `card_added`, so BOTH of the two receipts that make
            # this an operator-authored experiment are silently discarded: `source="operator"` (the
            # board would credit the Researcher's card) and `implementation_ref`, whose stated
            # purpose is that folding two injections with ready-made code "would lose executable
            # work". The human IS the researcher here; their work item is their own.
        )

    @in_llm_lane("build")
    def _create_injected_node(self, req: dict, *,
                              reservation: Optional[_BuildReservation] = None) -> None:
        """Materialize an operator-authored experiment (`inject_node` control event) into a real
        pending node. The operator supplies an idea (operator label, params, rationale, optional
        theme) and optionally a parent and ready-made code. If no code is given, the Developer
        implements the idea — so a human can describe an experiment and let the agent build it.
        The new node enters the search as `pending`; the policy evaluates it next.

        Manual injection deliberately bypasses the policy's proposal step — the human IS the
        researcher here — but everything downstream (eval, confirmation, best-selection, lineage)
        is identical to an agent-authored node, so a hand-added winner can be selected as best.

        ``reservation`` is the FREE half already done: `_serve_forced_requests` reserves on the main
        task BEFORE it spends the request's receipt and hands the reservation to this, the PAID half
        (review 2026-09-22, ENG1-07). A direct call without one prepares and reserves in place,
        exactly as before, and raises when the reservation is refused."""
        if reservation is None:
            state = fold(self.store.read_all())
            reservation = self._reserve_injected_node(
                state, self._prepare_injected_node(state, req))
            if reservation is None:
                raise ValueError("injected idea could not reserve one exact native Card")
        # What `_prepare_injected_node` would say again, read off the COMMITTED reservation: its
        # parents are the prepared list the snapshot kept, and the ready-made code is the request's
        # own field. Re-preparing here from a later fold could disagree with what was reserved.
        parents = list(reservation.parent_ids)
        code = req.get("code")
        state = reservation.state
        node_id = reservation.node_id
        parent_generations = reservation.parent_generations
        idea = reservation.idea.model_copy(deep=True)
        with self.tracer.span("create_node", new_trace=True, node_id=node_id,
                              generation=0, operator=idea.operator, source="manual"):
            # READY-MADE MEANS EITHER HALF OF THE ARTEFACT (2026-09-24). `code` is the script-solution
            # field; a REPO task's candidate is its file overlay, which the operator supplies as
            # `files` / `deleted`. Keyed on `code` alone, an injected repo node carrying a complete
            # overlay was built AGAIN from its rationale — `minionerec-backbones-v8` spent 60+ min of
            # Developer·stages/plan/implement on an inject whose only file was the finished
            # `experiment.env`, while four GPUs idled. The overlay is committed exactly as supplied
            # below (`files=req.get("files")`), so skipping the session changes nothing it would
            # have kept.
            developer_called = not (code or req.get("files") or req.get("deleted"))
            if not developer_called and code is None:
                code = ""
            footprint_finalized = False
            _inj = None                     # the envelope, when the Developer was called (doc 52 row 12)
            if developer_called:
                try:
                    with self.tracer.span("implement"):
                        # An injected experiment usually BUILDS ON its parent (a human picked it as the
                        # base) — hand the parent's solution to a parent-aware developer. Preserve the
                        # receipt-bound Idea by handing the plugin a deep working copy.
                        _pnode = state.nodes.get(parents[0]) if parents else None
                        _inj = self._implement_result(idea.model_copy(deep=True), _pnode, state=state)
                        code = _inj.code
                except Exception:
                    self._fail_reserved_build(
                        node_id=node_id, card_id=reservation.card_id, generation=0,
                        error="injected Developer raised before node creation", reason="build_crash")
                    self._discard_node_build_telemetry()
                    raise
                idea, footprint_finalized = self._finalize_developer_footprint(
                    idea, self.developer, code,
                    footprint=(_inj.last_footprint if _inj is not None else None))
            if not self._commit_built_node(
                    node_id=node_id, generation=0, card_id=reservation.card_id,
                    parents=parents, parent_generations=parent_generations,
                    idea=idea, code=code,
                    # Honour explicit files/deleted on the request (a cross-run `import` ships the
                    # sibling's full multi-file solution); else use the Developer's last build, and
                    # only when the Developer actually implemented (no ready-made code was supplied).
                    files=(req.get("files")
                           or ({} if req.get("code") or _inj is None else dict(_inj.last_files))) or {},
                    deleted=req.get("deleted") or [],
                    footprint_finalized=footprint_finalized,
                    stale_error="parent lifecycle changed while building",
                    rejected_error="injected node creation was rejected during replay",
                    # The operator's request must not leave a bare `node_building` behind when the
                    # append itself RAISES; the two agent paths have callers that already handle it.
                    append_failure_error="injected node append failed",
                    source="manual",
                    # Cross-run provenance: a DICT when this inject seeded from a sibling run's
                    # experiment (an `import` action), else None. Coerce defensively — a non-dict
                    # origin (a hand-authored/API inject that passed a label string) would make the
                    # folded Node fail validation and silently vanish, so the inject gate would keep
                    # re-creating the SAME node id forever.
                    origin=req.get("origin") if isinstance(req.get("origin"), dict) else None,
                    # The operator's fork-from-a-snapshot receipt, validated and stamped by
                    # `serve/control_validation.py::_normalize_fork_receipt` before it ever reached
                    # the log. OMITTED (not None-filled) when absent, so every historical inject
                    # emits its exact previous payload shape — see `_emit_node_created`'s docstring.
                    # Coerced defensively for the same reason `origin` is: a hand-authored durable
                    # row carrying a non-dict here must not make the folded Node fail validation and
                    # leave the inject gate re-creating the SAME id forever.
                    **({"forked_from": req["forked_from"]}
                       if isinstance(req.get("forked_from"), dict) else {}),
            ):
                return
            # Mirror _create_node / _rerun_node: a Developer session that CRASHED returns the
            # "(developer error: …)" sentinel as its code (an LLM 401/timeout/hard error). Without
            # this guard the injected node stays pending and its eval runs the PARENT's carried-over
            # entrypoint/files and inherits the PARENT's metric — a false success (the exact bug the
            # two sibling create paths already fix). FAIL it now (node_created → node_failed keeps the
            # one-terminal invariant) and trip the SAME developer-crash circuit-breaker, so an operator
            # inject during an LLM outage can't silently slip a garbage-code node past it.
            if is_developer_stuck(code):
                # SAME DISTINCTION AS THE FRESH-BUILD PATH ABOVE. The model ran out of moves on this
                # node; the provider is fine. Terminalize the node and let the run continue -- the
                # crash branch below would route it to the circuit breaker and pause everything.
                # Found by the cross-module invariant in
                # `tests/test_empty_build_is_stuck_not_a_crash.py`, which counted three readers of
                # the crash sentinel here against one of the stuck one -- my own c11251a1 taught
                # only the first of the three.
                self.store.append(EV_NODE_FAILED, {
                    "node_id": node_id, "generation": 0,
                    "error": code, "reason": "developer_stuck", "eval_seconds": 0.0,
                })
                self._discard_node_build_telemetry()
                return
            if is_developer_error(code):
                crash_terminal, crash_pause = developer_crash_records(
                    node_id, 0, code,
                    "auto-paused: a Developer session crashed while building an injected node "
                    "(LLM unreachable or a hard error, unresolved within the node) — resume "
                    "once it's fixed")
                # Same split as `_rerun_node`: the terminal always, the pause at the threshold.
                self.store.append(*crash_terminal)
                if self._developer_crash_pause_due(fold(self.store.read_all()), node_id):
                    # QUEUED, as `_rerun_node`'s is (review 2026-09-22, ENG1-07): offloaded, this is
                    # a worker, and EV_PAUSE is FOLDED and run-GLOBAL; `_serve_forced_requests`
                    # drains it after the await. A direct call on the loop thread (no offload) is
                    # already the main task, so it drains in place and keeps its historical order.
                    self._request_create_pause(node_id, crash_pause[1]["reason"])
                    if not getattr(_OFFLOADED_BUILD, "value", False):
                        self._drain_create_pause()
        if developer_called:
            # `_inj` is bound by the same `if developer_called` above; a build that never called the
            # Developer does not reach this line at all.
            self._consume_node_build_telemetry(node_id, 0, report=_inj.last_report,
                                               audit_extra=_inj.audit_extra,
                                               foresight_pick=_inj.last_foresight_pick)
