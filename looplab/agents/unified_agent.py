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
                    state=None, bind_state: bool = True, transport_fallback=None,
                    extra_tools=None, extra_turns: int = 0):
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

        `extra_tools` is a PER-CALL provider merged over the standing pilot toolset for this emit
        only, and `extra_turns` the turn grant that comes with it. Both default to the identity: with
        no extra provider the loop is handed `self._pilot_tools` itself (not a one-element composite,
        which would be a different object with a different `specs()` ordering) and `self._loop_opts`
        unchanged, so every existing caller's request is byte-identical. The pilot toolset is FIRST in
        the composite because `CompositeTools` dedups first-provider-wins: a per-call provider must
        never be able to shadow a standing tool by reusing its name.

        The grant is ADDITIVE and only over a FINITE budget. `max_turns=0` means unlimited, and
        `0 + n` would silently turn an operator's "no turn cap" into a cap of n — the loop is already
        bounded there by `agent_emit_after`/`agent_emit_force` and the stuck detector, which is what
        that configuration chose.
        """
        if bind_state and self._pilot_tools is not None and hasattr(self._pilot_tools, "bind_state"):
            self._pilot_tools.bind_state(state, None)
        tools = self._pilot_tools
        loop_opts = self._loop_opts
        if extra_tools is not None:
            from looplab.agents.agent import CompositeTools
            tools = (CompositeTools([self._pilot_tools, extra_tools])
                     if self._pilot_tools is not None else extra_tools)
            _configured = int(getattr(loop_opts, "max_turns", 0) or 0)
            if _configured > 0 and extra_turns > 0:
                loop_opts = loop_opts.replace(max_turns=_configured + int(extra_turns))
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
            lambda: drive_tool_loop(self._pilot_client, tools, messages, emit_spec,
                                    finalize=finalize, fallback=fallback, **loop_opts),
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
            # The third emit cap in this file, and like the critic's it stays at 300 deliberately:
            # the pilot's rationale is prose whose only sink is the audit-only `agent_decision` row
            # (`engine/node_build.py`), nothing extracts a claim from it, and the DECISION travels in
            # `index` rather than in the text. See the triage finalizer for the shape that forced a
            # widening — a downstream rung reading the string, not the string being long.
            return {"index": idx, "rationale": str((args or {}).get("rationale", ""))[:300]}

        def _fallback(_messages) -> dict:
            return {"index": default_idx, "rationale": "fallback: policy recommendation"}

        # On any transport failure the pilot degrades to the policy recommendation, still within
        # `legal` — see `_pilot_emit` for the budget-vs-transport rule it applies.
        return self._pilot_emit(messages, emit_spec, _finalize, _fallback, state=state)

    # --------------------------------------------------- Crash triage (in-node repair)
    _TRIAGE_SYSTEM = (
        "You are debugging an autonomous ML research loop. One experiment node just FAILED at "
        "runtime (the error is tagged with its kind: crash, timeout, oom, diverged, stalled, or "
        "needs_failed). "
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
        "    'needs_failed' is a DECLARATION mismatch, not a runtime error: the stage said it "
        "reads a path (`needs`) that was not there when it was about to start, so nothing ran and "
        "there is no traceback. Either an earlier stage wrote the artifact somewhere else or this "
        "stage names a path it does not really read — fix whichever is wrong, and never by "
        "deleting the declaration.\n"
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
        "YOU ARE ALSO THE DIAGNOSTICIAN: you say WHAT THE FAILURE WAS (`failure_kind`), and the "
        "tagged kind above is NOT an answer — it is what the engine could see from the outside. It "
        "is one of four, and each is tagged for a reason it cannot see past:\n"
        "  - 'crash': the process exited non-zero. That is ALL it means. Nothing observed why.\n"
        "  - 'no_metric': it exited zero and no reader found the number. Also just that.\n"
        "  - 'oom': the engine already suspects memory, but it is still an inference.\n"
        "  - 'check_failed': a stage's declared condition was judged not to hold — BY ANOTHER MODEL "
        "reading the stage's output. That verdict names the symptom it saw; it is evidence for you, "
        "not a diagnosis. A frozen loss, a run that could not fit its budget, and a config that "
        "silently ran one epoch instead of fifty all arrive tagged exactly this way.\n"
        "Answer with the kind that is TRUE, from these five:\n"
        "  - 'oom': it ran out of memory, device or host, however it died. This is the expensive "
        "one to miss, because it costs the memory-reduction directive and sends the repair hunting "
        "a bug that is not there. Watch for an allocator whose spelling is unusual (a host "
        "`MemoryError`, `DefaultCPUAllocator: can't allocate memory`, an OOM re-raised inside "
        "another library's exception) and — far more common here — a launcher (torchrun/accelerate) "
        "that SWALLOWS the child exception and shows only a 'Root Cause ... exitcode: 1' block, in "
        "which case the tail below names nothing at all.\n"
        "  - 'not_learning': the training ran but the objective never descended — a loss pinned at "
        "its initialization value, a config that trained a fraction of what it declared, a "
        "reduction or normalization that cannot learn as written. Say this when the log shows it, "
        "even when the tagged kind says 'check_failed' or 'no_metric'.\n"
        "  - 'crash': it failed for some other reason — a bug, a bad argument, a missing file, an "
        "assertion the script itself raised.\n"
        "  - 'no_metric': it completed and simply never produced the number.\n"
        "  - 'check_failed': the declared condition really did not hold and that IS the whole "
        "story.\n"
        "Choose from those FIVE only. The other kinds — timeout, diverged, stalled, drift, setup "
        "and the two filesystem stage contracts (needs/expect) — are facts the ENGINE recorded out "
        "of band about what IT did or stat'ed, they are never in question here, and you will not be "
        "asked about one. In particular do NOT answer 'timeout' for a run that ran out of budget: "
        "the engine's clock is the only thing that says that, it did not fire, and you would be "
        "asserting a mechanism you cannot observe — say what you saw and put the budget evidence in "
        "`evidence_quote`. `failure_kind` is not a licence to relabel a failure into whatever your "
        "fix is aimed at.\n"
        "SAY WHERE YOU LOOKED. `evidence_source` / `evidence_locator` / `evidence_quote` are the "
        "three fields that make your answer checkable by someone reading the record months later, "
        "and the engine re-resolves the locator against the workdir. Cite the thing that actually "
        "decided it: 'code' with a `path` or `path:line` inside the workdir, 'log' with the stage "
        "log name, or 'error' for the tail spliced below. Quote the one line that settles it. An "
        "answer with no citation is still recorded — this is not a hoop — but a diagnosis nobody "
        "can re-derive is worth much less than one they can.\n"
        "Consult the run if useful (read the code, find analogous experiments, read the stage logs, "
        "and READ THE PROGRAM THIS EVAL ACTUALLY RAN — `list_dir`/`find_files`/`grep`/`read_file` "
        "are rooted at the node's own workdir), then call `triage_crash` exactly once with your "
        "`action`, your `failure_kind`, your evidence and a one-sentence `rationale`."
    )

    # The sentence that tells the triage judge the stderr tail is not all it may have. Spliced ONLY
    # when log tools are actually wired (`engine/train_monitor.py::repair_log_tools`), at the SAME
    # position pattern as `budget` and `depth` — the two other additive lines above the evidence
    # block — so `repair_log_tools=false` reproduces the historical message byte for byte.
    #
    # It NAMES the failure it exists to end, for `train_monitor._LOOK_INVITATION`'s reason: a model
    # handed both a tail and a tool still reasons from the tail. On `rubertlite-dr-unified-v8` node 3
    # the whole 522-character tail was two renders of a RETRIEVAL progress bar that started AFTER
    # training had finished all 15 epochs, and the verdict read its elapsed field as training
    # progress — "node 3 is still in epoch 1 at 31:20" — about a run that was twenty minutes from a
    # result. Telling it the window is small is not the same as telling it the window may be about a
    # DIFFERENT PHASE than the one it is diagnosing, so this says the second thing.
    _REPAIR_LOOK_INVITATION = (
        "YOU CAN LOOK AT THE LOGS. The error below is a SHORT tail — a few hundred characters — of "
        "one stream, and on a long run it is usually the last frames of whatever was rendering when "
        "the process died. THAT IS NOT NECESSARILY THE PHASE THAT FAILED: a training stage often "
        "finishes its epochs and then encodes/evaluates/retrieves under a SECOND progress bar with a "
        "different total, and the elapsed time in that bar is NOT training time. Before you say how "
        "far the run got, or that it never started, or that it was too slow, USE YOUR TOOLS: "
        "`metric_series` (metric='step' or 'epoch', whole_run=true) for what the run actually "
        "completed, and `read_log` to search for the traceback, the final summary line, or the "
        "run's start. If the tools disagree with the tail, the tools have more evidence.\n"
        "This matters most for a TIMEOUT: 'cut the epochs' is the wrong fix for a run that already "
        "finished its epochs, and it silently changes the experiment the node was built to measure.")

    # THE OTHER HALF OF THE LOOK: write down what you found, so the looking survives the call.
    #
    # Spliced under exactly the same condition as `_REPAIR_LOOK_INVITATION` and in the same shape
    # (empty string when absent), so `repair_log_tools=false` still reproduces the historical prompt
    # byte for byte. That condition is the whole argument for asking here rather than in the base
    # directive: without tools this role has read nothing the prompt did not already splice into it,
    # so it would be summarizing the tail back to the engine that spliced it. With tools it has just
    # read the stage logs, the config and the program the eval ran, and ALL of that is discarded the
    # moment the call returns — the engine keeps one `{source, locator, quote}` and the model's own
    # tool transcript is not durable state.
    #
    # THE SUMMARY LEADS AND THE CITATIONS FOLLOW, and that order IS the design. The bytes are not
    # lost — 787 MB of stage logs across the eight preserved runs, `<workdir>/<stage>.log` written
    # by `sandbox._tee_drain`, nothing deletes them — so what a record owes a reader is the CAUSAL
    # STATEMENT, with its numbers, that they can act on without opening any of it. A link may die;
    # that is not a reason to build machinery around dead links, it is the reason the summary must
    # stand alone. The findings are then exactly what they are: the trail, for whoever wants to dig.
    #
    # It is also the cheapest possible fix for what the corpus measures. On the 122-row
    # `failure_triage.v1` corpus, widening the evidence from the durable 500-character stderr tail
    # to this role's own log reads moves 16 rows — and those 16 are rows whose answer is in a stage
    # log, which no amount of preserving more STDERR reaches. This role has already read those logs.
    _REPAIR_FINDINGS_INVITATION = (
        "NOW WRITE THE RECORD. `summary` is the one thing here a person will actually read: say what "
        "failed and BECAUSE OF WHAT, with the numbers and names INLINE — the allocation size, the "
        "parameter and the value it had, which stage, which epoch, the exception type. Write it for "
        "someone a week from now who has this row and nothing else: no workdir, no logs, no tools. "
        "\"See train.log:41233\" is a failed summary even if that line is exactly right.\n"
        "THEN the trail, in `findings`, most decisive first — one entry per thing you actually "
        "opened, `{source, locator, quote, means}`, where `quote` is the line verbatim and `means` "
        "is what it tells you about this failure. That is a convenience for whoever wants to check "
        "you or dig further; it is not where the facts live, and a fact that is only in a `quote` "
        "is a fact you did not record. Use the `path:line` or `path:startbyte-endbyte` your tools "
        "gave you rather than a remembered one — the engine re-resolves every locator against the "
        "workdir and marks the ones that did not resolve. Do NOT pad the list: an entry you did not "
        "read is worse than a short list, because it is the part a reader cannot check.")

    # The turn grant that comes with those tools, and it is deliberately NOT the watchdog's
    # `_MONITOR_LOOK_TURNS=6`, which is that judge's WHOLE budget for a call with nothing else to
    # spend it on. This is ADDITIVE over a budget the operator already sized for the pilot tools
    # (`read_code`/`find_analogous`), so it should cover only the new shape and nothing else: the
    # whole-run series that answers "what phase is this", one `read_log` that finds the traceback or
    # the final summary line, and one narrower follow-up. That is three, plus one spare for a
    # refusal-and-retry (a mistyped `log` name costs a turn and returns the list of real ones). The
    # emit turn and the orientation turns are not new and are already paid for.
    #
    # Four rather than "whatever the loop allows" because triage is on the EVAL-BLOCKING thread and
    # fires at the worst moment — a timeout repair happens after a multi-hour node has already died,
    # with the GPU idle behind it. It applies only when `agent_max_turns` is finite; see `_pilot_emit`
    # for why an unlimited budget stays unlimited.
    # SEVEN since 2026-08-20, not four, and the extra three are `failure_diagnosis.
    # DIAGNOSIS_CODE_LOOK_TURNS` — the same grant and the same argument `train_monitor.
    # _MONITOR_LOOK_TURNS` made when it moved 6 -> 9 on 2026-08-18 for the identical toolset. The
    # judge now also gets `RepoScoutTools` over the node's workdir, and attributing a cause to the
    # implementation costs a `grep` to locate the file that sets a parameter, a `read_file` to read
    # it, and possibly a second page — without giving up the log evidence the verdict is primarily
    # about. A budget that forces a choice between looking at the failure and looking at the code
    # produces exactly the guess this whole seam exists to replace.
    #
    # It is imported rather than re-spelled so the two halves of one decision cannot drift, and it
    # is still ADDITIVE over a FINITE budget only (see `_pilot_emit`: `max_turns=0` means unlimited
    # and `0 + n` would silently turn an operator's "no cap" into a cap of n).
    # SEVEN since 2026-08-20 (4 + 3), and the extra three are the CODE scouts' grant — the same
    # number and the same argument `train_monitor._MONITOR_LOOK_TURNS` made when it moved 6 -> 9 on
    # 2026-08-18 for the identical toolset. The judge now also gets `RepoScoutTools` over the node's
    # workdir, and attributing a cause to the implementation costs a `grep` to locate the file that
    # sets a parameter, a `read_file` to read it, and possibly a second page — WITHOUT giving up the
    # log evidence the verdict is primarily about. A budget that forces a choice between looking at
    # the failure and looking at the code produces exactly the guess this seam exists to replace.
    #
    # IT IS A LITERAL AND NOT AN IMPORT, deliberately: `agents` sits BELOW the engine and may reach
    # it only through a function-local import (the same rule `triage_crash`'s deferred import of the
    # verdict registry states), and a class-body default is evaluated at import time. So the sum is
    # spelled here and `tests/test_failure_ownership_split.py` asserts it equals
    # `failure_diagnosis.DIAGNOSIS_CODE_LOOK_TURNS + 4` — a red test rather than a silent drift, in
    # the one place where the layering forbids the single-definition fix.
    _REPAIR_LOOK_TURNS = 7

    def triage_crash(self, node, error: str, attempt: int, *, state: Optional[RunState] = None,
                     brief: str = "", history: str = "", stages_passed: Optional[int] = None,
                     attempts_left: Optional[int] = None, tools=None,
                     engine_facts: str = "") -> Optional[dict]:
        """Decide what to do with a just-crashed node: returns ``{"action", "failure_kind",
        "rationale"}`` where
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

        `tools` (from `engine/train_monitor.py::repair_log_tools`) is the fourth such argument and the
        one that stops the ENGINE choosing for this judge what it is allowed to see. The three above
        are all still fixed slices; this one lets it ASK the dead eval's own stage logs — what step
        the run reached, whether a second phase had already started, where the traceback is. The
        stderr tail still arrives spliced, so a model that ignores the tools answers exactly as it did
        before, and `_REPAIR_LOOK_INVITATION` is what tells it they are there. Additive by
        construction: `_TRIAGE_SYSTEM` and every evidence header are unchanged, and `tools=None`
        reproduces the historical message byte for byte.

        It widens what this judge can SEE and nothing else. The verdict vocabulary, the coercion, both
        fail-closed degradations and the caller's terminal are untouched, so nothing a model reads
        here can reach a metric, a champion, selectability or a violation — the text these tools return
        is what the candidate's own script wrote, which is `engine/metric_salvage.py`'s rule two
        packages over.

        `failure_kind` + the three `evidence_*` fields (2026-08-20) make this call the FAILURE
        DIAGNOSTICIAN as well as the stop rule — a second question, not a widening of the first:
        what the failure WAS, over `engine/failure_diagnosis.py::DIAGNOSED_FAILURE_REASONS`. It
        rides on this emit rather than on a call of its own because the diagnosis is already being
        paid for: this judge is consulted once per failed attempt, is handed exactly the evidence
        the question needs, and measurably already spends 8.82 provider calls doing it (335 calls /
        38 failures over v8+v9+v3), so a separate agent would roughly double the failure-path
        agentic cost and could contradict this one — the repair directive is BUILT from the kind.

        WHAT IT MAY NOT SAY is the point. The ENGINE-FINAL kinds — timeout, diverged, stalled,
        not_learning, drift, setup, and the two FILESYSTEM stage contracts (needs/expect) — are the
        engine's own record of what it caused, ran or measured; it never asks about one and
        `diagnosed_failure_reason` would not read an answer about one if it arrived. That is what
        keeps this from re-creating the v6 node 5 incident from the other direction, and it is what
        makes the field safe for the RECORD: the vocabulary is disjoint from
        `metric_salvage.NEVER_SALVAGED_REASONS`, so no metric can move on it — and salvage is
        decided branches EARLIER on the engine's own answer anyway.

        NOTE WHAT AN ABSENT `failure_kind` NOW MEANS, because it changed: a wired seam that returns
        no readable kind is a diagnostician that was ASKED and could not answer, and the engine
        records `unclassified` rather than quietly keeping its own residual. A run with no pilot
        model at all is a different branch (`triage._rule_triage`, which stamps the unforgeable
        `DIAGNOSIS_UNAVAILABLE_KEY`) and is unchanged byte for byte.

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
                                           DIAGNOSED_FAILURE_REASONS, DIAGNOSIS_SUMMARY_CAP,
                                           EVIDENCE_LOCATOR_CAP,
                                           EVIDENCE_QUOTE_CAP, EVIDENCE_SOURCES,
                                           FINDINGS_CAP, FINDING_MEANS_CAP,
                                           TRIAGE_RATIONALE_CAP,
                                           TRIAGE_TRANSPORT_FAILURE_KEY,
                                           UNANSWERABLE_TRIAGE_ACTION)
        code_tail = (getattr(node, "code", "") or "")[-1500:]
        budget = ("" if attempts_left is None else
                  f"Attempts left before the hard cap stops this node anyway: {attempts_left}.\n")
        depth = ("" if stages_passed is None else
                 f"Pipeline stages that passed before this failure: {stages_passed}.\n")
        # Same shape as the two lines above it (empty string when the thing it describes is absent),
        # which is what makes `tools=None` byte-identical to the historical prompt.
        look = ("" if tools is None else
                render(self.prompts, "triage_look_invitation", self._REPAIR_LOOK_INVITATION) + "\n"
                + render(self.prompts, "triage_findings_invitation",
                         self._REPAIR_FINDINGS_INVITATION) + "\n")
        # WHAT THE ENGINE ITSELF OBSERVED, spliced in the same shape as `budget`/`depth`/`look` —
        # empty string when absent, so an older caller reproduces the historical prompt byte for
        # byte. It carries the exit status and whether the process wrote anything at all, which
        # `_eval_failure_text` surfaces ONLY in its blank-stderr fallback: a cgroup OOM-kill that
        # leaves a "Killed" line hands this judge that one word and nothing else. See
        # `engine/failure_diagnosis.py::engine_observed_facts` for why stating the fact and NOT the
        # conclusion is the whole point, and for why the inference it enables is stronger than the
        # deleted rule that used to make it.
        facts = (str(engine_facts) + "\n") if str(engine_facts or "").strip() else ""
        messages = [
            {"role": "system", "content": render(self.prompts, "triage_system", self._TRIAGE_SYSTEM)},
            {"role": "user", "content": (
                (brief + "\n" if brief else "") +
                f"Crashed node {getattr(node, 'id', '?')} (repair attempt {attempt}).\n"
                + budget + depth + look + facts +
                f"--- ERROR (stderr tail) ---\n{error}\n"
                + (f"{history}\n" if history else "") +
                f"--- CODE (tail) ---\n{code_tail}\n"
                "Choose an action (repair, reject_idea, abandon) AND, if the kind tagged above is "
                "crash/oom/no_metric, the failure_kind you believe it really was.").strip()},
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
                # WHAT THE FAILURE ACTUALLY WAS, over `engine/failure_diagnosis.py::
                # DIAGNOSED_FAILURE_REASONS`. The enum is read from that registry and never
                # re-spelled here, for the same reason `action`'s is:
                # `Settings.inline_repair_reasons` selects on these exact strings, so an invented
                # one would silently make a failure class unrepairable. Every ENGINE-FINAL kind is
                # absent by construction — the engine never asks about one and
                # `diagnosed_failure_reason` would not read an answer about one — so a model cannot
                # relabel a watchdog kill, a deadline, a drift rejection, a setup failure or a
                # FILESYSTEM stage contract into something the memory playbook answers, which is
                # precisely the incident `tests/test_watchdog_kill_is_not_an_oom.py` exists to
                # prevent. `not_learning` is the one member that is on BOTH lists and it is a
                # registered exception with its argument at `DIAGNOSED_ENGINE_FINAL_OVERLAP`: the
                # engine produces it from a watchdog KILL, the diagnostician answers it about a run
                # nothing killed, and the asymmetry (never asked when the engine already said it) is
                # what keeps the two apart.
                "failure_kind": {"type": "string", "enum": list(DIAGNOSED_FAILURE_REASONS),
                                 "description": "What this failure really was. The tagged kind is "
                                                "what the engine saw from outside the process; "
                                                "repeat it when it is right and correct it when "
                                                "the log or the code says otherwise."},
                # WHERE THE DIAGNOSIS STANDS, in three fields rather than folded into `rationale`.
                # Separate because the ENGINE re-resolves the locator against the workdir and
                # records whether it resolved (`failure_diagnosis.evidence_citation_resolves`),
                # which it cannot do to a sentence. That check is the closest thing available to
                # `runtime/deps.py::is_present`: it cannot verify the CONCLUSION — no out-of-band
                # probe of a failure KIND exists — but it can verify the diagnostician looked at a
                # thing that is really there, which is what makes a wrong answer auditable.
                "evidence_source": {"type": "string", "enum": list(EVIDENCE_SOURCES),
                                    "description": "Where the decisive evidence came from: 'code' "
                                                   "(a file in this eval's workdir), 'log' (a "
                                                   "stage log), 'error' (the tail spliced into "
                                                   "this prompt), or 'none'."},
                "evidence_locator": {"type": "string",
                                     "description": "The file path (optionally `path:line`) or log "
                                                    "name the evidence is at. Workdir-relative."},
                "evidence_quote": {"type": "string",
                                   "description": "The one line that settles it, quoted."},
                # THE ACCOUNT A HUMAN READS, and the one field here that must work with nothing else
                # in front of it. Its description carries the BAR rather than a word count, because
                # the failure it is written against is a summary that points instead of stating:
                # a reader a week later has no workdir, and the citations below may not resolve.
                # `failure_diagnosis.coerce_diagnosis_summary` owns the cap and the redaction.
                "summary": {"type": "string",
                            "description": "What failed and BECAUSE OF WHAT, in prose a reader with "
                                           "no access to this run can act on. Put the numbers and "
                                           "names INLINE — the allocation size, the parameter and "
                                           "its value, the stage, the epoch, the exception type. "
                                           "Not a pointer to where they can be found."},
                # THE TRAIL BEHIND THAT ACCOUNT — a convenience for whoever wants to check or dig,
                # never a substitute for saying it in `summary`. By the time this call answers it has
                # read the stage logs, the config and the code the eval ran; the three singular
                # fields above record ONE of those looks and drop the rest.
                # `failure_diagnosis.coerce_findings` bounds this at `FINDINGS_CAP` and re-resolves
                # every locator, so a long or invented list costs a dropped tail entry rather than an
                # unbounded durable row.
                "findings": {
                    "type": "array",
                    "description": "Everything you actually looked at that bears on this, most "
                                   "decisive first. Not a summary of your reasoning — one entry "
                                   "per thing you READ.",
                    "items": {"type": "object", "properties": {
                        "source": {"type": "string", "enum": list(EVIDENCE_SOURCES)},
                        "locator": {"type": "string",
                                    "description": "Workdir-relative path, optionally `path:line` "
                                                   "or `path:startbyte-endbyte`."},
                        "quote": {"type": "string",
                                  "description": "The line you read there, verbatim."},
                        "means": {"type": "string",
                                  "description": "What that line tells you about this failure."}}}},
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
            #
            # THE RATIONALE WEARS THE ENGINE'S INTAKE BOUND, not a 300 of its own, and that is the
            # 2026-08-14 correction to the 2026-08-13 fix. This finalizer is the FIRST cap the text
            # meets: it runs one layer below `engine/crash_repair.py::_ask_triage`, which caps what
            # this seam RETURNS. `UnifiedAgent` is the only implementation of the seam in the tree and
            # is the shipped default, so raising the intake alone changed nothing at all — a bound
            # over a string that is already 300 chars long is not a bound. The reader that needs the
            # rest is `engine/repair_verify.py::verify_repair`, which asks whether a repair did what
            # its rationale said and is fed by exactly this string; a crash rationale is written
            # diagnosis-first and "Fix:"-last, so 300 kept the citations and dropped the claims.
            # ONE constant, read from the registry module this method already imports, because two
            # caps that must not disagree may not be two literals. Every durable SINK still clips
            # independently at its own 300 (`node_repaired.rationale`, `node_failed.triage_rationale`)
            # — no durable bytes move, only what the engine reads before it writes them.
            # `failure_kind` rides back RAW and lower-cased, never coerced here: the refusal has
            # to be spelled where the engine's own deterministic answer is in hand, and this layer
            # does not have it. `engine/failure_diagnosis.py::diagnosed_failure_reason` holds both
            # halves. An absent key travels as "" — but since 2026-08-20 "" no longer falls back to
            # the engine's residual on a DIAGNOSABLE reason: a diagnostician that was asked and said
            # nothing readable answers `unclassified`, so that a failed diagnosis and a confirmed
            # one cannot write the same row. A model that ignores the field is therefore NOT
            # unchanged, and that is the intended change.
            #
            # The three evidence fields ride back beside it, bounded but uninterpreted, for the same
            # reason: the engine re-resolves the locator against the workdir it owns, and this frame
            # holds no workdir. The caps are `failure_diagnosis`' durable-row caps, imported rather
            # than re-spelled.
            #
            # `summary` and `findings` ride in the SAME bag and under the same rule. Both are bounded
            # HERE too, not only in `failure_diagnosis`, because this frame is where an unbounded
            # emit first becomes an in-process object: a model that answers with ten thousand
            # findings must not be able to make the engine hold them while it decides to drop them.
            # Intake bounds shape only — every semantic decision (what counts as a citation, the
            # dedup, the REDACTION, the workdir resolution) is the engine's, one layer up. Note the
            # summary is NOT redacted here and must not be: masking before a cap is the rule
            # (`_screened`), and this layer holds no redactor, so capping it here and masking it
            # there would put the cut on the wrong side of the screen.
            findings = (args or {}).get("findings")
            findings = findings if isinstance(findings, (list, tuple)) else ()
            return {"action": action,
                    "summary": str((args or {}).get("summary", ""))[:DIAGNOSIS_SUMMARY_CAP],
                    "failure_kind": str((args or {}).get("failure_kind", "")).strip().lower()[:40],
                    "evidence_source": str((args or {}).get("evidence_source", "")).strip().lower()[:16],
                    "evidence_locator": str((args or {}).get("evidence_locator", ""))[:300],
                    "evidence_quote": str((args or {}).get("evidence_quote", ""))[:300],
                    "findings": [
                        {"source": str((f or {}).get("source", "")).strip().lower()[:16],
                         "locator": str((f or {}).get("locator", ""))[:EVIDENCE_LOCATOR_CAP],
                         "quote": str((f or {}).get("quote", ""))[:EVIDENCE_QUOTE_CAP],
                         "means": str((f or {}).get("means", ""))[:FINDING_MEANS_CAP]}
                        for f in findings[:FINDINGS_CAP] if isinstance(f, dict)],
                    "rationale": str((args or {}).get("rationale", ""))[:TRIAGE_RATIONALE_CAP],
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
                                transport_fallback=_transport_failed,
                                extra_tools=tools, extra_turns=self._REPAIR_LOOK_TURNS)

    # --------------------------------------------------- Repair critic (F8: the stop, not the fix)
    _REPAIR_CRITIC_SYSTEM = (
        "You are reviewing an autonomous ML research loop's REPAIR TRAJECTORY on a single "
        "experiment node. You are not fixing anything and you are not deciding what the failure "
        "was. You answer exactly one question: are the successive attempts addressing DIFFERENT "
        "causes, or are they circling one?\n"
        "  - 'continue': the chain is going somewhere. The cause changes, or the pipeline reaches a "
        "later stage, or each fix touches different code and the failure moves with it. A long "
        "chain is not by itself a circling one — a repo with stale dependencies legitimately needs "
        "a run of mechanical fixes before the real experiment can start, and stopping that early "
        "throws away the whole node.\n"
        "  - 'stop': the chain is re-covering the same ground. The same cause after fixes that "
        "claimed to address it; the same files rewritten again and again; a sequence of fixes that "
        "are all variations of one guess (three rounds of halving a batch size is ONE idea tried "
        "three times, not three ideas). Note especially that the same underlying wall can wear a "
        "DIFFERENT error text every attempt — a broken library that renames the symbol it fails on "
        "each time is still one wall. Judge the causes and the fixes, not the wording.\n"
        "Prefer 'continue' when you are unsure: something else is still bounding this loop, and a "
        "wrong 'stop' discards work that was about to succeed. Say 'stop' when you can state, in "
        "one sentence, what the repeated thing IS.\n"
        "Call `repair_critic` exactly once with your `action` and a one-sentence `rationale` naming "
        "the pattern you saw."
    )

    def repair_critic(self, node, *, state: Optional[RunState] = None, brief: str = "",
                      trajectory: str = "", attempt: Optional[int] = None) -> Optional[dict]:
        """Should this node's repair loop keep going? ``{"action": "continue"|"stop", "rationale"}``,
        or ``None`` when no pilot model is wired (the engine then simply has no critic).

        THE SECOND OF F8's TWO SIGNALS, and deliberately a different call from `triage_crash` rather
        than three more sentences in its prompt. The triage judge is answering "given this failure,
        do I know what to change?" — a question about the NEXT step, which a model answers well and
        optimistically. "Has this chain stopped making progress?" is a question about the SHAPE of
        everything already tried, and asking one model both at once is what the deleted anti-stuck
        counter was doing badly on the same evidence: the judge that just proposed a fix is the
        worst-placed participant to rule that the fixes are all the same idea.

        IT CAN ONLY STOP. There is no verdict here that makes the loop repair MORE, sets `reason`,
        selects a salvage, or touches the node's metric — the caller turns `stop` into the same
        terminal an `abandon` produces, carrying the eval's own authenticated failure reason. That is
        doc 36's line: this decides whether to keep going, never what the result was.

        AND ITS EVIDENCE IS AUTHENTICATED. `trajectory` is rendered by
        `engine/repair_judgment.py::format_repair_trajectory`, whose per-attempt `cause` is the
        engine's own `_failure_reason` (read from the sandbox's out-of-band signal channel) and
        whose stderr tail is LABELLED as candidate-controlled. A critic that read the kind of a
        failure off a banner the failing script printed would hand the candidate the stop decision —
        see `c862045c`, which took that exact route away from the failure classifier.

        Degrades like nothing else here: no emit, an out-of-enum action and a dead endpoint all
        become `continue`. See `engine/repair_judgment.py::DEFAULT_CRITIC_ACTION` for why this
        judge's fail-closed direction is the opposite of triage's."""
        if self._pilot_client is None:
            return None                       # no critic model -> the engine simply has no critic
        # Deferred import of the verdict registry, for the reason `triage_crash` states above: the
        # engine's stop decision keys on these exact strings and `agents` sits below the engine.
        # `engine/repair_judgment.py` is pure (stdlib-only at module scope), so this cannot cycle.
        from looplab.engine.repair_judgment import AGENT_CRITIC_ACTIONS, DEFAULT_CRITIC_ACTION
        if not str(trajectory or "").strip():
            # NOTHING TO JUDGE. The critic's whole question is about a chain, and asking it about an
            # empty one buys a paid call whose only honest answer is `continue`. Refused here rather
            # than at the caller so a duck-typed replacement inherits the property.
            return {"action": DEFAULT_CRITIC_ACTION,
                    "rationale": "no repair trajectory to judge yet"}
        messages = [
            {"role": "system", "content": render(self.prompts, "repair_critic_system",
                                                 self._REPAIR_CRITIC_SYSTEM)},
            {"role": "user", "content": (
                (brief + "\n" if brief else "") +
                f"Node {getattr(node, 'id', '?')}"
                + ("" if attempt is None else f", about to spend repair attempt {attempt}") + ".\n"
                + trajectory + "\n"
                "Is this chain addressing different causes, or circling one? "
                "Choose: continue or stop.").strip()},
        ]
        emit_spec = {"type": "function", "function": {
            "name": "repair_critic",
            "description": "Decide whether this node's repair loop is still making progress.",
            "parameters": {"type": "object", "properties": {
                # Read from the registry, never re-spelled — `engine/repair_judgment.py` owns the
                # vocabulary and the engine's stop keys on it. Unlike triage there is no
                # engine-minted member to exclude here: a critic that cannot be reached contributes
                # `continue`, which is a value the model may legitimately emit too.
                "action": {"type": "string", "enum": list(AGENT_CRITIC_ACTIONS),
                           "description": "continue (the chain is addressing different causes) | "
                                          "stop (the chain is circling one)."},
                "rationale": {"type": "string"}},
                "required": ["action"]}}}

        def _finalize(args: dict) -> dict:
            action = str((args or {}).get("action", "")).strip().lower()
            if action not in AGENT_CRITIC_ACTIONS:
                action = DEFAULT_CRITIC_ACTION
            # THE SIBLING CAP, AND IT STAYS AT 300 — decided, not inherited by symmetry with the
            # triage finalizer above. What made that one wrong was a downstream READER of the text:
            # `repair_verify` extracts CLAIMS from a triage rationale and a cut on the "Fix:" seam
            # silently changed a verdict on a durable column. Nothing extracts anything from a
            # critic rationale. Its only consumers are prose — `crash_repair.py::_repair_critic`'s
            # own 300-char normalization on the way back and the abandon reason an operator reads —
            # so widening it would move no verdict and only put more model text into a durable row.
            # If a rung is ever written that READS this string, it moves to the shared intake bound
            # for the same reason its sibling did.
            return {"action": action, "rationale": str((args or {}).get("rationale", ""))[:300]}

        def _no_verdict(_messages) -> dict:
            # BOTH degradations collapse here ON PURPOSE, which is the opposite of `triage_crash`'s
            # rule and for the opposite reason. There the two ways to fail carry different POWERS (a
            # per-node stop vs a run-level pause), so conflating them cost a run per bad emit. Here
            # neither may stop anything: a critic that did not answer has no opinion, the triage
            # judge and the floors are untouched, and the loop ends exactly where it would have
            # without a critic wired at all. So there is nothing for a second callable to say.
            return {"action": DEFAULT_CRITIC_ACTION,
                    "rationale": "the repair critic returned no verdict — no opinion recorded"}

        return self._pilot_emit(messages, emit_spec, _finalize, _no_verdict,
                                state=state, bind_state=state is not None)
