"""Unified self-driving agent (NEW): ONE object that plays Researcher + Developer
(+ Strategist + action-pilot) across pipeline stages, choosing its own model/toolset per
stage. It implements BOTH the `Researcher` and `Developer` Protocols (and the `Strategist`
Protocol via `decide`), so the orchestrator wires the SAME object as `researcher`,
`developer`, and `strategist` — the engine interface is unchanged.

Design: the merge is a *facade* over the already-tested split-role backends. The agentic
loop, structured-output parsing, sweep contract, validation/best-of-N wrappers, and H3
per-role models are reused verbatim by composing the normal `make_roles` output (built with
`unified_agent=False`) and rebinding a per-stage LLM client. The genuinely-new unified
behavior — the action `pilot` (self-driving the next macro action within a pure legal-action
gate) and the absorbed `strategy` stage — lives here on top of that reused core.

Replay-safety is preserved exactly as for the split roles: every decision the agent makes is
recorded as an event (`node_created` / `strategy_decision` / `agent_decision`) and replayed
from the log, never re-invoked.
"""
from __future__ import annotations

from typing import Optional

from looplab.agents.roles import WrapsDeveloper, forward_hints
# `BudgetExceeded` is no longer named here — the propagate-vs-degrade rule moved into
# `tool_loop.resilient`, which owns it (doc 25 AG-06). Re-exported for any importer that
# reached it through this module.
from looplab.core.llm import BudgetExceeded  # noqa: F401
from looplab.core.models import Idea, Node, RunState
from looplab.core.prompts import render


class UnifiedAgent(WrapsDeveloper):
    """Facade composing per-stage role backends behind one engine-facing control object.

    `researcher` drives `propose` (an Idea), `developer` drives `implement` (and `repair`, unless
    `repair_developer` is given), `strategist` drives `decide` (a Strategy at meta-cadence).
    `pilot_client`/`pilot_tools` drive `choose_action` (the next macro action). Each backend is
    already bound to its own per-stage client (H3), so `propose` and `implement` can run on
    different models — and `repair` gets its own Developer object rather than sharing (and
    overwriting) the implement one when the two stages are pointed at different models. Their local
    model contexts remain separate; the facade is not a shared cross-stage conversation identity.

    Developer-facing forwarding (brief/is_code_generating/client/last_report/audit_extra and
    per-call files/deletions/footprint)
    comes from `WrapsDeveloper`, delegating to `self.developer` via `_wrapped` below.
    """

    # `prompts` is LOCAL (a plain handle threaded at construction, read by the pilot/triage
    # prompt renders below) — shadow the mixin's forwarding property so `self.prompts = prompts`
    # binds on the instance instead of pushing into the internal developer.
    prompts = None

    # Delegation target for the WrapsDeveloper forwarders: the (possibly wrapped) internal developer
    # the most recent code-producing call actually ran on — while `inner` (set in __init__) exposes
    # the fully-unwrapped probe developer. It is `self.developer` throughout unless the operator gave
    # implement and repair DIFFERENT stage models, in which case repair runs on its own object and
    # every forwarder (last_files via _sync_audit, last_report, audit_extra, client) has to describe
    # the call that happened — otherwise the orchestrator writes the other stage's files to the node.
    @property
    def _wrapped(self):
        return self._active_developer

    def __init__(self, *, researcher, developer, strategist=None,
                 pilot_client=None, pilot_tools=None, stage_clients=None, prompts=None,
                 agent_max_turns: int = 0, agent_time_budget_s: float = 0.0,
                 loop_opts: Optional[dict] = None, repair_developer=None):
        # Internal per-stage backends. Named `researcher`/`developer`/`strategist` (not _-prefixed)
        # so the engine's cost roll-up walk (_emit_llm_cost) descends into them and finds every
        # per-stage CostAccountant.
        self.researcher = researcher
        self.developer = developer
        # Same rule for the repair stage's own Developer: PUBLIC so the roll-up walk finds its
        # accountant (`costs._CHILD_ATTRS` names it). None = repair shares `developer`, the default.
        self.repair_developer = repair_developer
        self._active_developer = developer
        self.strategist = strategist
        self._pilot_client = pilot_client
        self._pilot_tools = pilot_tools
        # Tool-loop limits for the pilot's self-driving + crash-triage calls (0/0 = unlimited;
        # config-driven via Settings.agent_max_turns / agent_time_budget_s — never hardcoded).
        # Kept as the recorded configuration; what `_pilot_emit` actually spreads is `_loop_opts`
        # below, into which these two are folded once (doc 25 AG-01).
        self._agent_max_turns = agent_max_turns
        self._agent_time_budget_s = agent_time_budget_s
        # B1 stuck + C1 self-plan + C2 summary (config-driven), folded ONCE into the typed bundle
        # together with the two limits above so `_pilot_emit` spreads it with no option keyword
        # beside it — the duplicate-keyword shape doc 25 AG-01 closes. `with_defaults`: a bundle
        # that already carries a limit is the operator's configured value and wins over the ctor's.
        from looplab.agents.loop_options import LoopOptions
        self._loop_opts = LoopOptions.coerce(loop_opts).with_defaults(
            max_turns=agent_max_turns, time_budget_s=agent_time_budget_s)
        # Per-stage clients NOT reachable via researcher/developer (strategy, pilot) — surfaced so
        # the engine's cost roll-up can find their CostAccountants. Deduped by identity downstream.
        self.stage_clients = list(stage_clients or [])
        self.prompts = prompts
        # Raw probe developer (bypasses any ValidatingDeveloper) for ablation probes: the engine
        # reads `getattr(self.developer, "inner", self.developer)`, so expose the unwrapped inner.
        self.inner = getattr(developer, "inner", developer)
        # Forwarded so make_roles-style introspection keeps working.
        self.bounds = getattr(researcher, "bounds", None)
        self.space_hint = getattr(researcher, "space_hint", "")
        # Developer-protocol audit attributes the orchestrator reads off `self.developer`
        # (`last_report` is forwarded live by the WrapsDeveloper mixin).
        self.last_files: dict = {}
        self.last_deleted: list = []
        self.last_footprint: dict | None = None

    # ----------------------------------------------------------- Researcher
    @property
    def parser(self):
        # Read-through to the internal researcher's configured structured-output parser (mirroring
        # the `prompts` wiring above): chain-walkers like engine/lessons.py::_merge_prompt_opts
        # getattr `parser` off the ACTIVE researcher — this facade in unified mode — and a missing
        # attr here silently fell back to the "tool_call" default, shadowing the run's llm_parser.
        return getattr(self.researcher, "parser", None)

    def propose(self, state: RunState, parent: Optional[Node]) -> Idea:
        # The engine sets ephemeral hints via `setattr(self.researcher, ...)` where self.researcher
        # is THIS agent; forward them (P2 — roles.forward_hints owns the rule) to the internal
        # researcher that actually reads them.
        forward_hints(self, self.researcher)
        return self.researcher.propose(state, parent)

    # ----------------------------------------------------------- Developer
    def _for_stage(self, stage: str):
        """The Developer object a code stage runs on, and the delegate the forwarders describe until
        the next call. `repair_developer` is None in the default posture, so both stages resolve to
        the same object and this is exactly the historical single-developer path."""
        dev = (self.repair_developer or self.developer) if stage == "repair" else self.developer
        self._active_developer = dev
        return dev

    def implement(self, idea: Idea) -> str:
        code = self._for_stage("implement").implement(idea)
        self._sync_audit()
        return code

    def implement_from(self, idea: Idea, parent) -> str:
        """Parent-aware implement: delegate when the inner developer supports it (repo tasks), so an
        improve patches the parent's solution instead of regenerating from the baseline."""
        dev = self._for_stage("implement")
        impl = getattr(dev, "implement_from", None)
        code = impl(idea, parent) if callable(impl) else dev.implement(idea)
        self._sync_audit()
        return code

    def repair(self, idea: Idea, code: str, error: str) -> str:
        dev = self._for_stage("repair")
        rep = getattr(dev, "repair", None)
        out = rep(idea, code, error) if callable(rep) else dev.implement(idea)
        self._sync_audit()
        return out

    def repair_from(self, idea: Idea, node, error: str) -> str:
        """Node-aware repair: delegate to the inner developer's repair_from (seeds the failing node's
        OWN files) when available, else the plain repair(idea, node.code, error)."""
        dev = self._for_stage("repair")
        rf = getattr(dev, "repair_from", None)
        out = (rf(idea, node, error) if callable(rf)
               else self.repair(idea, getattr(node, "code", ""), error))
        self._sync_audit()
        return out

    # ----------------------------------------------------------- Strategist
    def decide(self, state: RunState, ctx):
        """Strategist Protocol: delegate to the strategy-stage backend (None => keep current)."""
        if self.strategist is None:
            return None
        return self.strategist.decide(state, ctx)

    # ----------------------------------------------------------- Pilot (self-driving)
    _PILOT_SYSTEM = (
        "You are the pilot of an autonomous ML research loop. Exactly one macro action runs "
        "next. You are given the LEGAL actions (you may pick ONLY from these — the pipeline "
        "forbids anything else) and the search policy's recommended action. Consult the run if "
        "useful, then call `choose_action` exactly once with the `index` of your chosen action "
        "from the legal list and a one-sentence `rationale`."
    )

    def _pilot_emit(self, messages: list, emit_spec: dict, finalize, fallback, *,
                    state=None, bind_state: bool = True, transport_fallback=None):
        """Drive the pilot tool loop for one emit, containing everything but a budget stop.

        `choose_action` and `triage_crash` differ only in prompt, schema and coercion; this owns what
        they shared — the optional `bind_state`, the call-time seam import, the loop kwargs, and the
        containment boundary. Each caller's `fallback` still carries its own meaning (the policy
        recommendation; the safe "attempt repair" action), so what degrades and why stays at the site
        that knows.

        TWO DEGRADATIONS, NOT ONE — and only a caller that can tell them apart passes the second.
        `drive_tool_loop` RETURNING its `fallback` means the endpoint answered and the model never
        emitted (prose replies the client could not force into a tool call, the stuck detector, the
        `emit_force` ceiling, turn/wall-clock exhaustion, an operator cancel). `drive_tool_loop`
        RAISING means the transport failed — a transport error propagates out of the loop by design
        and `resilient` is what contains it. Collapsing the two was a measured defect: with ONE
        `fallback` carrying `triage_crash`'s transport marker, a demonstrably alive endpoint that
        answers in prose (a local vLLM/SGLang that ignores `tool_choice`) produced
        `node_failed reason='developer_crash'` plus a RUN-level pause telling the operator to check
        credits, key and base URL — the exact incident the 2026-08-06 `unanswerable`/`unreadable`
        split exists to prevent, reached through the other door. `transport_fallback` defaults to
        `fallback`, so a caller whose two degradations really are the same value (the pilot's policy
        recommendation) is unchanged.

        `bind_state` is a flag rather than an inference from `state is not None` because the two
        callers genuinely differ: the pilot binds unconditionally, while triage binds only when it
        was handed a run state — collapsing that would silently change which tools are reachable.
        """
        if bind_state and self._pilot_tools is not None and hasattr(self._pilot_tools, "bind_state"):
            self._pilot_tools.bind_state(state, None)
        # Resolve through `agent.py`'s module global at CALL time, not at import time: a
        # module-level `from ... import drive_tool_loop` early-binds the function object, so a
        # monkeypatch on the documented seam `looplab.agents.agent.drive_tool_loop` (CLAUDE.md;
        # `agent.py` states the contract) never reached this call and an offline test silently
        # drove the REAL loop against the real client. `strategist.py` already imports it here.
        from looplab.agents.agent import drive_tool_loop
        from looplab.agents.tool_loop import resilient

        # A hard budget stop propagates and ends the run; a transport failure degrades to the
        # caller's TRANSPORT fallback rather than crashing it. `resilient` is that rule written down
        # once (doc 25 AG-06), and this is the new call site it was meant to be adopted at. The loop
        # keeps its own no-emit `fallback`: only the exception path is evidence about the transport.
        _transport = transport_fallback if transport_fallback is not None else fallback
        return resilient(
            lambda: drive_tool_loop(self._pilot_client, self._pilot_tools, messages, emit_spec,
                                    finalize=finalize, fallback=fallback, **self._loop_opts),
            lambda: _transport(messages))

    def choose_action(self, state: RunState, legal: list[dict], recommended: Optional[dict] = None,
                      *, brief: str = "") -> dict:
        """Pick the next macro action from `legal` (the pure legal-action gate). Returns a dict
        ``{"index": int, "rationale": str}``. Structurally cannot escape `legal`: the emit schema
        constrains `index` to the legal range, and any malformed/out-of-range emit falls back to
        the policy's `recommended` (or the first legal action). The CALLER turns the index into the
        concrete action and records the `agent_decision` event — this method has no side effects."""
        n = len(legal)
        if n == 0:
            return {"index": -1, "rationale": "no legal actions"}

        def _parents(a: dict) -> tuple:
            # P30 (docs/PROMPT_REVIEW.md): merge actions carry `parent_ids` (a list, policy.py);
            # everything else `parent_id`. One accessor for both the menu and the matcher.
            pids = a.get("parent_ids")
            if pids:
                return tuple(pids)
            return (a["parent_id"],) if a.get("parent_id") is not None else ()

        default_idx = 0
        if recommended is not None:
            for i, a in enumerate(legal):
                # Match merges by their parent SET (a merge is symmetric in its parents) — kind +
                # parent_id alone matched the FIRST merge in the list regardless of which pair the policy
                # recommended, and an ordered-tuple compare missed a non-greedy policy's recommended pair
                # whenever its order differed from the menu's top-2 order (the fallback then silently
                # defaulted to legal[0]/draft instead of the recommendation). `frozenset` is order-neutral
                # for merges and a no-op for the single-parent / draft kinds.
                if (a.get("kind") == recommended.get("kind")
                        and frozenset(_parents(a)) == frozenset(_parents(recommended))):
                    default_idx = i
                    break
        if self._pilot_client is None:        # pilot model not wired -> take the policy recommendation
            return {"index": default_idx, "rationale": "policy recommendation (no pilot model)"}
        # Single-parent actions render exactly as before (" parent=N"); merges now show what they
        # merge (" parents=N,M") instead of a bare "[i] merge" the pilot had to choose blind.
        menu = "\n".join(
            f"  [{i}] {a.get('kind')}"
            + (f" parent={_parents(a)[0]}" if len(_parents(a)) == 1 else "")
            + (" parents=" + ",".join(str(p) for p in _parents(a)) if len(_parents(a)) > 1 else "")
            for i, a in enumerate(legal))
        rec = ("\nPolicy recommends index "
               f"{default_idx}: {legal[default_idx].get('kind')}.") if recommended is not None else ""
        messages = [
            {"role": "system", "content": render(self.prompts, "pilot_system", self._PILOT_SYSTEM)},
            {"role": "user", "content": (brief + "\nLegal actions:\n" + menu + rec +
                                         "\nChoose the next action.").strip()},
        ]
        emit_spec = {"type": "function", "function": {
            "name": "choose_action",
            "description": "Choose the next macro action by its index in the legal list.",
            "parameters": {"type": "object", "properties": {
                "index": {"type": "integer", "minimum": 0, "maximum": n - 1,
                          "description": "Index of the chosen action in the legal list."},
                "rationale": {"type": "string"}},
                "required": ["index"]}}}

        def _finalize(args: dict) -> dict:
            try:
                idx = int((args or {}).get("index"))
            except (TypeError, ValueError):
                idx = default_idx
            if not (0 <= idx < n):           # out-of-range -> safe fallback, never escapes `legal`
                idx = default_idx
            return {"index": idx, "rationale": str((args or {}).get("rationale", ""))[:300]}

        def _fallback(_messages) -> dict:
            return {"index": default_idx, "rationale": "fallback: policy recommendation"}

        # On any transport failure the pilot degrades to the policy recommendation, still within
        # `legal` — see `_pilot_emit` for the budget-vs-transport rule it applies.
        return self._pilot_emit(messages, emit_spec, _finalize, _fallback, state=state)

    # --------------------------------------------------- Crash triage (in-node repair)
    _TRIAGE_SYSTEM = (
        "You are debugging an autonomous ML research loop. One experiment node just FAILED at "
        "runtime (the error is tagged with its kind: crash, timeout, oom, diverged, or stalled). "
        "Decide what to do BEFORE "
        "spending another eval:\n"
        "  - 'repair': the SAME idea is sound — fix the code and re-run in place. Choose this for a "
        "mechanical crash (bad import, removed/renamed API, typo, wrong arg), for a 'timeout' (the "
        "code was just too slow — reduce compute: fewer estimators/epochs/folds/seeds, early stopping, "
        "a lighter model), AND for an 'oom' (the code was killed for using too much memory — reduce "
        "memory: smaller batch, lighter/smaller model, fewer features or a subsample, lower precision). "
        "A timeout or oom is NOT evidence the idea is wrong (and an oom usually has no traceback).\n"
        "    Two kinds are the ENGINE's own watchdogs stopping the stage, and both look like an oom "
        "(SIGKILL, no traceback) while needing the OPPOSITE fix — say so explicitly in your "
        "rationale so the repair does not reach for the memory playbook: 'diverged' means the live "
        "log reported a non-finite loss/grad_norm repeatedly (stabilise the objective — LR, warmup, "
        "gradient clipping, epsilons in log/sqrt/division, float32 loss, the weight of a new "
        "auxiliary term), and 'stalled' means the stage stayed alive and produced no output at all "
        "(remove the hang, or emit a heartbeat so the next run shows where it stopped). Neither is "
        "evidence the idea is wrong, and neither is fixed by a smaller batch.\n"
        "  - 'reject_idea': the idea itself is fundamentally flawed (e.g. the approach can't work, or "
        "nearby configs crash the same way) — abandon this lineage so the loop tries a different idea.\n"
        "  - 'abandon': stop here without judging the idea (e.g. not worth another attempt).\n"
        "NOTE: a missing KNOWN library (ModuleNotFoundError) is auto-installed by the engine and the "
        "node re-run BEFORE you are consulted, so you should rarely see one. If a ModuleNotFoundError "
        "still reaches you, the install failed (offline / not on PyPI / a typo'd or local module) — "
        "prefer 'repair' (switch to an available library or fix the import) over 'reject_idea' unless "
        "the approach itself is unsound.\n"
        "YOU ARE THE STOPPING RULE. There is no heuristic behind you that will notice a repair loop "
        "going in circles — only a hard attempt cap that exists so a wrong call is not unbounded. If "
        "you are shown a repair history, read it as a TRAJECTORY and answer the question it poses: "
        "given everything already tried on this node, do you still know what to change next?\n"
        "  - Say 'repair' when you can name the next change. A failure that MOVES (a different error, "
        "a later pipeline stage reached, different files touched each time) is progress even if it "
        "has taken many attempts — a repo with stale dependencies legitimately needs a run of "
        "mechanical fixes before the real experiment can start, and stopping that early wastes the "
        "whole node.\n"
        "  - Say 'abandon' when the history shows the same ground being re-covered: the same failure "
        "after fixes that claimed to address it, edits cycling over the same files, or a fix you "
        "cannot describe beyond retrying. 'I do not know how to fix this any more' is a correct and "
        "useful answer — say it rather than proposing a guess.\n"
        "If the crash is caused by a library that is simply NOT INSTALLED — including one that "
        "degraded into a NameError or AttributeError because the library guards it behind an "
        "availability check — put ONLY that distribution's name in `missing_dependency` (e.g. "
        "\"accelerate\"); the engine installs it and re-runs. Leave it empty for anything you would "
        "fix by editing code.\n"
        "Consult the run if useful (read the code, find analogous experiments), then call "
        "`triage_crash` exactly once with your `action` and a one-sentence `rationale`."
    )

    def triage_crash(self, node, error: str, attempt: int, *, state: Optional[RunState] = None,
                     brief: str = "", history: str = "", stages_passed: Optional[int] = None,
                     attempts_left: Optional[int] = None) -> Optional[dict]:
        """Decide what to do with a just-crashed node: returns ``{"action", "rationale"}`` where
        action ∈ {repair, abandon, reject_idea} — or one of the engine's two fail-closed verdicts
        when this call could not produce one of those (see the bottom of this docstring) — or
        ``None`` when no pilot model is wired (the engine
        then falls back to its deterministic rule). The agent may use its run-introspection tools
        (read_code / find_analogous) to judge whether the IDEA is wrong vs just the code. No side
        effects: the CALLER performs the repair and records the events.

        `history` is this node's in-node repair trajectory, already rendered by the engine
        (`engine/crash_repair.py::_format_repair_log`) and, since the ledger became durable, it spans
        RESUMES — a node with eight repairs behind it shows eight rows in a freshly started process.
        `attempts_left` is the remaining hard cap. Both exist because this call IS the loop's
        stopping rule — see `_TRIAGE_SYSTEM`. All three are keyword-only with empty/None defaults, so
        an older caller (and every test double) still gets the historical single-traceback prompt.

        NEITHER degradation path answers "repair", and they answer DIFFERENT things: `_finalize`'s
        out-of-enum branch says `unreadable` (the model is alive, this node stops) and `_fallback`
        says `unanswerable` with the engine-side transport marker (the endpoint is gone, the run
        pauses). See `engine/triage.py`'s verdict contract for why collapsing them was a defect."""
        if self._pilot_client is None:
            return None                       # no triage model -> engine uses the rule-based fallback
        # DEFERRED (function-local) import of the verdict registry: `agents` sits BELOW the engine,
        # and re-spelling the vocabulary here is exactly the silent-typo failure the registry exists
        # to prevent — the engine's stop decision keys on these strings. `engine/triage.py` is pure
        # (stdlib-only at module scope), so this cannot cycle; keeping it call-local mirrors the
        # `agents` -> `search` rule and adds no import-time edge upward.
        from looplab.engine.triage import (AGENT_TRIAGE_ACTIONS, DEFAULT_TRIAGE_ACTION,
                                           TRIAGE_TRANSPORT_FAILURE_KEY,
                                           UNANSWERABLE_TRIAGE_ACTION)
        code_tail = (getattr(node, "code", "") or "")[-1500:]
        budget = ("" if attempts_left is None else
                  f"Attempts left before the hard cap stops this node anyway: {attempts_left}.\n")
        depth = ("" if stages_passed is None else
                 f"Pipeline stages that passed before this failure: {stages_passed}.\n")
        messages = [
            {"role": "system", "content": render(self.prompts, "triage_system", self._TRIAGE_SYSTEM)},
            {"role": "user", "content": (
                (brief + "\n" if brief else "") +
                f"Crashed node {getattr(node, 'id', '?')} (repair attempt {attempt}).\n"
                + budget + depth +
                f"--- ERROR (stderr tail) ---\n{error}\n"
                + (f"{history}\n" if history else "") +
                f"--- CODE (tail) ---\n{code_tail}\n"
                "Choose: repair, reject_idea, or abandon.").strip()},
        ]
        emit_spec = {"type": "function", "function": {
            "name": "triage_crash",
            "description": "Decide how to handle the crashed node.",
            "parameters": {"type": "object", "properties": {
                # The verdict contract (engine/triage.py::AGENT_TRIAGE_ACTIONS) — the enum is
                # read from the registry, never re-spelled here, because the engine's STOP decision
                # keys on these exact strings. Both engine verdicts are deliberately absent:
                # `unanswerable` says the transport failed and a live model must not be able to claim
                # its own unreachability and trip the RUN-level circuit breaker; `unreadable` says
                # the engine could not read the answer, which is not the answerer's to assert. The
                # absence is documentation, not enforcement — `_finalize` below and
                # `engine/triage.py::coerce_triage_action` are what actually refuse them, because a
                # schema enum only constrains a well-behaved emit.
                "action": {"type": "string", "enum": list(AGENT_TRIAGE_ACTIONS),
                           "description": "repair in place | abandon this node (you no longer know "
                                          "what to change) | reject the whole idea."},
                "missing_dependency": {"type": "string",
                                       "description": "Distribution name to install, ONLY when the "
                                                      "crash is caused by a library that is not "
                                                      "installed. Empty otherwise."},
                "rationale": {"type": "string"}},
                "required": ["action"]}}}

        def _finalize(args: dict) -> dict:
            action = str((args or {}).get("action", "")).strip().lower()
            if action not in AGENT_TRIAGE_ACTIONS:
                # FAIL CLOSED. This used to default to "repair" — "the cheap, safe action" — which is
                # only cheap if a repair is cheap: each one is a full re-eval plus two LLM calls, and
                # a provider stuck emitting garbage would drive them forever. An emit nobody can read
                # is not a verdict, so it becomes `DEFAULT_TRIAGE_ACTION`.
                #
                # That default is `unreadable`, and it must not be `unanswerable`: THIS branch is the
                # one place we KNOW the provider is alive — the request completed and the model
                # emitted something, it just was not one of the three words. Answering the engine's
                # provider-outage verdict here made one out-of-enum emit stop the node with
                # `developer_crash` AND pause the whole run with a "check your credits" reason
                # derived from the model's own rationale. The caller stops this node and lets the run
                # continue; the literal string "unanswerable" arriving from the wire lands here too.
                action = DEFAULT_TRIAGE_ACTION
            # `missing_dependency` is a NAME, not a command — the engine still requires its own
            # allowlist + traceback corroboration + an absence check before any install
            # (runtime/deps.py::triage_install_candidates), so a hallucinated value here can at worst
            # be ignored.
            return {"action": action, "rationale": str((args or {}).get("rationale", ""))[:300],
                    "missing_dependency": str((args or {}).get("missing_dependency", ""))[:100]}

        def _no_emit(_messages) -> dict:
            # THE LOOP ENDED WITHOUT AN EMIT, AND THE ENDPOINT ANSWERED THROUGHOUT. This is what
            # `drive_tool_loop` RETURNS its `fallback` for: two prose replies the client could not
            # force into a tool call, the stuck detector, the `emit_force` ceiling, turn/wall-clock
            # exhaustion, an operator cancel. Every one of those completed its requests and produced
            # bytes, so by the verdict contract this is `unreadable` — a per-NODE stop — and it
            # carries NO transport marker.
            #
            # It used to be the same callable as `_transport_failed` below, and that collapse was the
            # 2026-08-06 split's own defect seen from the other side: driven end-to-end, a live
            # endpoint that ignores `tool_choice` and answers in prose (an ordinary local
            # vLLM/SGLang deployment) produced `node_failed reason='developer_crash'` plus a RUN-level
            # pause carrying `node_id=None` and the "check your credits, key and base URL" banner —
            # on four successful HTTP requests, with zero repairs attempted. `_finalize` being
            # unforgeable is not enough on its own: the model does not have to put the marker in its
            # ANSWER when refusing to answer at all reaches the marked branch.
            return {"action": DEFAULT_TRIAGE_ACTION,
                    "rationale": "the crash-triage model answered but never returned a verdict "
                                 "(no emit within the loop's turn/prose budget)",
                    "missing_dependency": ""}

        def _transport_failed(_messages) -> dict:
            # A TRANSPORT FAILURE IS NOT A VERDICT. `resilient` calls this when the pilot loop RAISED
            # — an unreachable endpoint, a 401/402, a transport error surviving the client's own
            # retry ladder. It answered "attempt repair", which is precisely the reading that let a
            # dead provider keep a repair loop at full speed with no model in it. It says
            # `unanswerable` instead, and its caller stops the node and pauses the RUN naming the
            # provider — recoverable with `resume` once the endpoint is back.
            #
            # THIS is the only path in the package that may say that word, and the ONLY reason the
            # engine believes it is `TRIAGE_TRANSPORT_FAILURE_KEY` below: the marker is what
            # `engine/triage.py::is_transport_failure_verdict` requires. It is unforgeable from the
            # wire because `_finalize` above rebuilds its dict from the three schema properties alone
            # AND because the only way to reach here is an exception escaping the loop — which no
            # emit, and no refusal to emit, can produce. Without the marker this dict would be read
            # as an ordinary unreadable answer — the correct fail-closed direction, since the wrong
            # reading of THIS branch only costs a node while the wrong reading of it costs the run.
            return {"action": UNANSWERABLE_TRIAGE_ACTION,
                    TRIAGE_TRANSPORT_FAILURE_KEY: True,
                    "rationale": "the crash-triage model could not be reached "
                                 "(transport failure)",
                    "missing_dependency": ""}

        # Binding only with a run state is what enables read_code / find_analogous on it; a loop that
        # ends without an emit degrades to `unreadable`, and only a raised transport failure to the
        # run-halting `unanswerable`.
        return self._pilot_emit(messages, emit_spec, _finalize, _no_emit,
                                state=state, bind_state=state is not None,
                                transport_fallback=_transport_failed)
