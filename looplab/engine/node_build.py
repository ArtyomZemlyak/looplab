"""Node-building helpers (idea -> code -> `node_created` payload) — extracted from
orchestrator.py as a MIXIN: `class Engine(NodeBuildMixin, …)` inherits these methods unchanged,
so there is ZERO call-site churn and `self` here IS the engine. Verbatim moves; several are
exercised on bare `Engine.__new__(Engine)` instances by tests, which a mixin preserves.

DELIBERATELY NOT MOVED: `_create_node` / `_rerun_node` / `_create_injected_node` /
`_activate_spec` stay in orchestrator.py — they call the module-global `fold`, which two tests
monkeypatch THROUGH the orchestrator module (`monkeypatch.setattr(orch, "fold", …)`); moving
them would silently detach that seam. This split keeps the fold-callers with the spine and
extracts only the stateless build sub-helpers they call.

Agent-facing deps (`legal_actions`, `_state_brief`, `render_hint_directives`) stay lazy,
method-local imports so monkeypatching through their source modules keeps working."""
from __future__ import annotations

from types import MappingProxyType
from typing import Optional

from looplab.agents.roles import DeveloperResult, developer_call_lock
from looplab.core.llm_broker import in_llm_lane
from looplab.core.models import (Idea, NodeStatus, RunState, normalize_researcher_footprint,
                                 is_developer_error, is_developer_stuck)
from looplab.engine.costs import find_cost_accountants
from looplab.events.types import EV_AGENT_DECISION, EV_NODE_CREATED, EV_NODE_FAILED, EV_PAUSE
from looplab.search.operators import merge_idea

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
#   * the two serial orchestrator sites append sequentially on the main task.
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
        that method runs in an `anyio.to_thread` WORKER, and a `BudgetExceeded` raised there is
        swallowed by `_create_node_guarded` into one node's terminal instead of ending the run. Nor
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
            brief = _state_brief(state, None, for_proposal=False,
                                 memo_verdicts=getattr(self, "_memo_verdict_cue", False))
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
        bind_state = getattr(developer, "bind_state", None)
        if callable(bind_state):
            bind_state(state)
        impl_from = getattr(developer, "implement_from", None)
        if parent is not None and callable(impl_from):
            if co_parents and accepts_co_parents(impl_from):
                return self._run_developer(developer, impl_from, idea, parent,
                                           co_parents=tuple(co_parents))
            return self._run_developer(developer, impl_from, idea, parent)
        return self._run_developer(developer, developer.implement, idea)

    def _run_developer(self, developer, fn, *args, **kwargs) -> DeveloperResult:
        """ONE Developer call and the capture of its outputs, as one atomic step under the
        instance's lock (`developer_call_lock`). The lock is what makes two offloaded calls on a
        SHARED instance safe: they queue here, in a worker, instead of on the event loop."""
        with developer_call_lock(developer):
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
            last_edit_calls=edit_calls,
        )

    @staticmethod
    def _reset_developer_footprint(developer) -> None:
        """Clear per-call resource output through a wrapper tree before invoking Developer.

        Parallel builds use isolated role pairs, but serial reruns/repairs reuse one object.  Clearing
        every reachable wrapper/inner/fallback prevents a backend that omits the optional output from
        inheriting another node's finalization.
        """
        pending = [developer]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            if hasattr(current, "last_footprint"):
                try:
                    current.last_footprint = None
                except Exception:  # noqa: BLE001 - optional audit output must never block a build
                    pass
            for attr in ("inner", "developer", "fallback"):
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
                              self._dev_prior_note_text.strip()) if b]
        if not blocks:
            return idea
        di = idea.model_copy(deep=True)
        di.rationale = ((di.rationale or "") + "\n" + "\n".join(blocks)).strip()
        return di

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
        bind_state = getattr(developer, "bind_state", None)
        if callable(bind_state):
            bind_state(state)
        rf = getattr(developer, "repair_from", None)
        if callable(rf):
            return self._run_developer(developer, rf, idea, node, err)
        return self._run_developer(developer, developer.repair, idea, node.code, err)

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
