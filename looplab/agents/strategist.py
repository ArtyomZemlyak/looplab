"""A7 · Strategist — optional adaptive meta-control (ADR-2, user-requested).

The Strategist is an OPTIONAL meta-controller that, at a bounded cadence, reads the folded
`RunState` and decides *which search machinery to use next*: the search policy/allocator, the
operator mix, the eval fidelity, and (when a Developer factory is wired) the Developer backend.
It never selects a node itself and never writes a domain event — it emits an audit-only
`strategy_decision` that swaps the active policy/operators. Every field it can decide is also a
direct `Settings` knob, so the Strategist is a convenience layer over the same config, fully
hand-overridable, and `backend="off"` is byte-identical to today's legacy static-config
behavior (the shipped default is `"llm"` — an adaptive meta-controller consulted at cadence).

Replay-safe by construction: the chosen `Strategy` is recorded in the event log and reconstructed
by `replay.fold`; the (possibly non-deterministic) LLM backend is NEVER re-invoked during replay —
exactly how an LLM `Idea` is recorded in `node_created` and replayed without a model call.

Two backends:
- `RuleStrategist` — deterministic heuristics over pure folded state (zero-dep, the LLM fallback).
- `LLMStrategist`  — structured output via the existing `llm`/`parse` stack; degrades to None
  (keep current strategy) on any parse/transport failure, never crashing the run.
"""
from __future__ import annotations

from typing import Optional, Protocol, TypedDict

from pydantic import BaseModel, Field

from looplab.core.llm import BudgetExceeded
from looplab.core.models import NodeStatus, RunState
from looplab.core.prompts import PromptStore, render

# A fully-serializable description of the active search machinery. Every field maps to an existing
# config knob, so a Strategy is just "a settings delta the engine applies live".
#
# ADDING A FIELD touches four more sites in this file/module chain — keep them in sync:
#   1. `_StrategyOut` (the LLM output schema, below)      — so the model can propose it,
#   2. `_assemble_strategy` (below)                        — so the proposal is copied over,
#   3. `validate_strategy` (below)                         — the paranoid whitelist that lets it through,
#   4. `Engine._apply_strategy` (engine/orchestrator.py)   — so the engine actually applies it,
# plus the brief text in `_STRATEGIST_SYSTEM` if the model should know the knob exists.
# (`prefer_sweep`'s history shows the full chain.)
Strategy = TypedDict(
    "Strategy",
    {
        "policy": str,          # "greedy"|"evolutionary"|"mcts"|"asha" (whatever make_policy knows)
        "policy_params": dict,  # {"c":1.4} | {"eta":3} | {"n_seeds":4} ...
        "developer": str,       # "default"|"llm"|"opencode"|... (whatever the dev factory knows)
        "operators": dict,      # {"ablate_every":int, "merge_mode":str, "complexity_cue":bool, ...}
        "fidelity": str,        # "smoke"|"full"|"adaptive"
        "timeout": float,       # per-eval wall-clock budget (s) — applied only if the matrix allows it
        "max_parallel": int,    # concurrent evals — applied only if the matrix allows it
        "request_research": bool,  # ask the engine to run the Deep-Research stage before continuing
        "rationale": str,       # human-readable "why" (the UI panel)
        "source": str,          # "rule"|"llm"|"operator"|"config" (provenance, audit)
    },
    total=False,
)


class StrategyContext(BaseModel):
    """Read-only inputs handed to the Strategist (a compact, serializable view of the run)."""
    node_count: int = 0
    phase: str = "seed"                       # "seed"|"explore"|"exploit"|"confirm"
    eval_budget_remaining: Optional[float] = None
    wall_remaining: Optional[float] = None
    failure_rate: float = 0.0
    improves_since_best: int = 0
    is_numeric_space: bool = False
    avg_eval_seconds: Optional[float] = None   # mean per-node eval cost so far (sweep cost signal)
    node_budget_frac: float = 0.0              # fraction of the node budget spent (P2 endgame reserve)
    current_policy: str = "greedy"             # the ACTIVE policy (for switch-back rules, D3)
    available_policies: list[str] = Field(default_factory=list)
    available_developers: list[str] = Field(default_factory=list)
    defaults: dict = Field(default_factory=dict)   # the static config Strategy (fallback/start)


class Strategist(Protocol):
    def decide(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
        """Return a NEW strategy to switch to, or None to keep the current one. MUST be
        deterministic for `rule`; `llm` may be non-deterministic (its output is recorded)."""
        ...


# --------------------------------------------------------------------------- #
# Pure signals derived from the folded DAG (deterministic, no I/O)
# --------------------------------------------------------------------------- #

def failure_rate(state: RunState) -> float:
    total = sum(1 for n in state.nodes.values()
                if n.status in (NodeStatus.evaluated, NodeStatus.failed))
    if not total:
        return 0.0
    failed = sum(1 for n in state.nodes.values() if n.status is NodeStatus.failed)
    return failed / total


def improves_since_best(state: RunState) -> int:
    """How many improve/refine nodes were created AFTER the current best node — i.e. how long the
    search has been pushing without dethroning the leader (a stall signal). Deterministic: ids are
    monotonic, so 'after' is just a higher id than best."""
    best_id = state.best_node_id
    if best_id is None:
        return 0
    return sum(1 for n in state.nodes.values()
               if n.id > best_id and n.operator in ("improve", "refine_block", "merge"))


def is_numeric_space(state: RunState) -> bool:
    best = state.best()
    if best is None or not best.idea.params:
        return False
    return all(isinstance(v, (int, float)) for v in best.idea.params.values())


def run_phase(state: RunState, n_seeds: int) -> str:
    if state.confirmed_done:
        return "confirm"
    n = len(state.nodes)
    if n < n_seeds:
        return "seed"
    feasible = len(state.feasible_nodes())
    return "exploit" if feasible >= max(1, n_seeds) and state.best_node_id is not None else "explore"


# --------------------------------------------------------------------------- #
# Validation — whitelist every field before a Strategy is applied
# --------------------------------------------------------------------------- #

def validate_strategy(strat: Optional[Strategy], ctx: StrategyContext) -> Optional[Strategy]:
    """Constrain a proposed Strategy to known/safe values. Returns a cleaned copy, or None if the
    proposal is empty/invalid (engine then keeps the current strategy). Never trusts the LLM blindly."""
    if not strat or not isinstance(strat, dict):
        return None
    out: Strategy = {}
    pol = strat.get("policy")
    if isinstance(pol, str) and pol in ctx.available_policies:
        out["policy"] = pol
    pp = strat.get("policy_params")
    if isinstance(pp, dict):
        # keep only scalar numeric/bool params (defense against arbitrary payloads)
        out["policy_params"] = {str(k): v for k, v in pp.items()
                                if isinstance(v, (int, float, bool))}
    dev = strat.get("developer")
    if isinstance(dev, str) and dev in ctx.available_developers:
        out["developer"] = dev
    ops = strat.get("operators")
    if isinstance(ops, dict):
        clean: dict = {}
        if isinstance(ops.get("ablate_every"), int) and ops["ablate_every"] >= 0:
            clean["ablate_every"] = ops["ablate_every"]
        if ops.get("merge_mode") in ("mean", "ensemble"):
            clean["merge_mode"] = ops["merge_mode"]
        if isinstance(ops.get("complexity_cue"), bool):
            clean["complexity_cue"] = ops["complexity_cue"]
        if isinstance(ops.get("ablate_code_blocks"), bool):
            clean["ablate_code_blocks"] = ops["ablate_code_blocks"]
        # Intra-node sweep bias: a hint that nudges the Researcher toward a sweep. The Strategist
        # only sets the flag — it never creates a sweep itself (the Researcher decides whether/how
        # to build the grid), preserving the "Researcher is the decision-maker" division.
        if isinstance(ops.get("prefer_sweep"), bool):
            clean["prefer_sweep"] = ops["prefer_sweep"]
        if clean:
            out["operators"] = clean
    fid = strat.get("fidelity")
    if fid in ("smoke", "full", "adaptive"):
        out["fidelity"] = fid
    # Resource budgets (bounds match config: timeout>0, max_parallel>=1). Whitelisted here for shape;
    # the engine's _apply_strategy applies them ONLY if the governance matrix grants the strategist.
    tmo = strat.get("timeout")
    if isinstance(tmo, (int, float)) and not isinstance(tmo, bool) and tmo > 0:
        out["timeout"] = float(tmo)
    mp = strat.get("max_parallel")
    if isinstance(mp, int) and not isinstance(mp, bool) and mp >= 1:
        out["max_parallel"] = mp
    if isinstance(strat.get("request_research"), bool) and strat["request_research"]:
        out["request_research"] = True   # ask the engine to run the Deep-Research stage
    if not out:
        return None
    out["rationale"] = str(strat.get("rationale", ""))[:500]
    out["source"] = strat.get("source", "rule")
    return out


# --------------------------------------------------------------------------- #
# Rule baseline (ship first — zero-dep, deterministic, also the LLM fallback)
# --------------------------------------------------------------------------- #

class RuleStrategist:
    """Deterministic heuristics over the folded state. Pure (no recording needed for correctness;
    the engine records anyway for audit + parity with the LLM path). Knobs are taken from the
    static config defaults, so the operator can tune every threshold."""

    def __init__(self, n_seeds: int = 3, stall_window: int = 3):
        self.n_seeds = n_seeds
        self.stall_window = max(1, stall_window)

    def decide(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
        avail = ctx.available_policies
        # Seed phase: cheap broad drafts at smoke fidelity (greedy is fine; nothing to exploit yet).
        if ctx.phase == "seed":
            return {"policy": "greedy", "fidelity": "smoke",
                    "rationale": "seed phase: broad cheap drafts before any exploitation",
                    "source": "rule"}

        # Eval budget almost gone -> stop exploring, exploit the leader at full fidelity.
        if ctx.eval_budget_remaining is not None and ctx.defaults.get("_budget_frac", 1.0) < 0.2:
            return {"policy": "greedy", "fidelity": "full",
                    "operators": {"ablate_every": 0},
                    "rationale": "eval budget <20% left: exploit the current leader, no new breadth",
                    "source": "rule"}

        # P2/D13 endgame reserve: in the FINAL fraction of the node budget, stop opening new breadth
        # and spend the reserve on a final ENSEMBLE of the strongest solutions at full fidelity —
        # top MLE-bench systems reserve an explicit final-ensemble/confirm window rather than
        # exploring until the budget dies. (The confirm phase then runs at finish as usual.)
        if ctx.node_budget_frac >= 0.8 and ctx.phase in ("explore", "exploit"):
            return {"policy": "greedy", "fidelity": "full",
                    "operators": {"merge_mode": "ensemble", "ablate_every": 0},
                    "rationale": f"endgame ({ctx.node_budget_frac:.0%} of node budget spent): "
                                 "reserve for a final ensemble of the top solutions, no new breadth",
                    "source": "rule"}

        # High failure rate -> stop spending breadth on broken code; deepen repair, narrow search.
        if ctx.failure_rate > 0.4:
            strat: Strategy = {"policy": "greedy", "fidelity": "adaptive",
                               "rationale": f"high failure rate ({ctx.failure_rate:.0%}): "
                                            "narrow to greedy + deeper repair",
                               "source": "rule"}
            if "agentless" in ctx.available_developers:
                strat["developer"] = "agentless"   # only when C5 has landed
            return strat

        # D3 (FML-bench): the adaptive greedy⇄broad cycle beats every FIXED strategy. The stall
        # rule below broadens the search when the leader stops moving; THIS rule closes the loop —
        # once a broadened search produced a fresh leader (no current stall), return to greedy
        # exploitation instead of paying breadth forever. Greedy wins when improvement
        # opportunities are dense; breadth wins when they're sparse — the signal is stagnation.
        if (ctx.current_policy not in ("", "greedy")
                and ctx.improves_since_best < self.stall_window
                and ctx.phase == "exploit"):
            return {"policy": "greedy", "fidelity": "adaptive",
                    "rationale": f"fresh leader under {ctx.current_policy} "
                                 "(no stall): switch back to greedy exploitation "
                                 "(adaptive greedy⇄broad beats fixed strategies)",
                    "source": "rule"}

        # Stall: the leader hasn't been dethroned for a while. Per the verified "operators > search"
        # finding, first probe operators (bump ablation); if MCTS is available, switch to explore.
        if ctx.improves_since_best >= self.stall_window:
            # A hard stall is exactly when stepping back to "think hard" pays off: ask the engine to
            # run the Deep-Research stage (read across all results + the literature/web) alongside the
            # machinery switch, so the next batch is informed by more than local hill-climbing.
            deep = ctx.improves_since_best >= 2 * self.stall_window
            if "mcts" in avail:
                strat: Strategy = {"policy": "mcts", "policy_params": {"c": 1.4},
                                   "fidelity": "adaptive",
                                   "rationale": f"stalled for {ctx.improves_since_best} improves: "
                                                "switch greedy->mcts to explore under-visited subtrees",
                                   "source": "rule"}
            else:
                strat = {"policy": "greedy", "operators": {"ablate_every": 2}, "fidelity": "adaptive",
                         "rationale": f"stalled for {ctx.improves_since_best} improves: probe operators "
                                      "(ablate the leader) — the verified higher-leverage move than search",
                         "source": "rule"}
            if deep:
                strat["request_research"] = True
                strat["rationale"] += " + deep-research the problem (hard stall)"
            return strat

        # Exploring a numeric space where each eval is expensive: bias the Researcher toward an
        # intra-node sweep. Running several grid points in ONE process amortizes the data load /
        # imports / GPU warm-up that dominate a costly single eval — strictly cheaper than the same
        # points as separate nodes. We only set the FLAG; the Researcher chooses the grid.
        if (ctx.phase == "explore" and ctx.is_numeric_space
                and (ctx.avg_eval_seconds or 0.0) >= 5.0):
            return {"policy": "greedy", "operators": {"prefer_sweep": True}, "fidelity": "adaptive",
                    "rationale": f"explore on a numeric space with costly evals "
                                 f"(~{ctx.avg_eval_seconds:.0f}s each): bias toward an in-process "
                                 "sweep to amortize data load / warm-up across grid points",
                    "source": "rule"}

        # Many cheap candidates to race + ASHA available -> successive-halving over fidelities.
        if "asha" in avail and ctx.phase == "explore":
            return {"policy": "asha", "policy_params": {"eta": 3}, "fidelity": "adaptive",
                    "rationale": "exploring breadth: race candidates with ASHA "
                                 "(smoke rung -> promote survivors to full)",
                    "source": "rule"}

        # Healthy exploit on a numeric space: keep greedy, refine the leader.
        return None   # nothing to change


# --------------------------------------------------------------------------- #
# LLM backend (optional; structured output, robust fallback)
# --------------------------------------------------------------------------- #

_STRATEGIST_SYSTEM = (
    "You are the search Strategist for an autonomous ML research engine. Given the current run "
    "state and a menu of available search policies, operators and fidelities, decide the BEST "
    "machinery to use next. You never pick a specific experiment — only the strategy. Prefer "
    "richer operators over fancier search (operators are the verified bottleneck). Respond ONLY "
    "with the requested structured fields; pick `policy` from the provided available list."
)


class _StrategyOut(BaseModel):
    """Structured shape the LLM fills (a subset of Strategy; validated again by validate_strategy)."""
    policy: Optional[str] = None
    fidelity: Optional[str] = None
    ablate_every: Optional[int] = None
    merge_mode: Optional[str] = None
    complexity_cue: Optional[bool] = None
    prefer_sweep: Optional[bool] = None
    request_research: Optional[bool] = None
    rationale: str = ""


def _strategist_brief(state: RunState, ctx: StrategyContext) -> str:
    """The compact decision brief shared by the structured-output and tool-using Strategists."""
    brief = (
        f"phase={ctx.phase} nodes={ctx.node_count} failure_rate={ctx.failure_rate:.2f} "
        f"improves_since_best={ctx.improves_since_best} numeric_space={ctx.is_numeric_space} "
        f"eval_budget_remaining={ctx.eval_budget_remaining}\n"
        f"available_policies={ctx.available_policies} avg_eval_seconds={ctx.avg_eval_seconds}\n"
        "Choose the next strategy (policy from the available list; fidelity smoke|full|adaptive; "
        "optional ablate_every, merge_mode mean|ensemble, complexity_cue, prefer_sweep — set "
        "prefer_sweep=true to bias the researcher toward an in-process hyperparameter sweep when "
        "evals are costly and the space is numeric; set request_research=true when the run is "
        "stalled or confused and would benefit from a deep-research step over all results + the "
        "web before continuing)."
    )
    # Active operator/boss directives (the same `pending_hints` the Researcher already follows,
    # rendered the same way so recency/precedence read identically): the Strategist owns the
    # policy/fidelity, so it MUST weigh standing directives or it will fight them — e.g. answer a
    # "try 10 different neural nets" request with a pure-exploit greedy switch that just refines
    # the current champion. Advisory; the Strategist still decides.
    from looplab.agents.hints import render_hint_directives
    directives = render_hint_directives(state.pending_hints)
    if directives:
        brief += (directives + "\n(When a directive calls for EXPLORATION or trying several "
                  "distinct approaches, prefer an exploratory policy such as evolutionary/asha "
                  "and do NOT switch to pure-exploit greedy.)")
    return brief


def _assemble_strategy(out: "_StrategyOut", *, source: str = "llm") -> Strategy:
    """Build the validated Strategy dict from the model's structured fields (shared by both LLM
    Strategist variants). `validate_strategy` still clamps this against the governance whitelist."""
    strat: Strategy = {"source": source, "rationale": out.rationale or f"{source}-chosen strategy"}
    if out.policy:
        strat["policy"] = out.policy
    if out.fidelity:
        strat["fidelity"] = out.fidelity
    if out.request_research:
        strat["request_research"] = True
    ops: dict = {}
    if out.ablate_every is not None:
        ops["ablate_every"] = out.ablate_every
    if out.merge_mode:
        ops["merge_mode"] = out.merge_mode
    if out.complexity_cue is not None:
        ops["complexity_cue"] = out.complexity_cue
    if out.prefer_sweep is not None:
        ops["prefer_sweep"] = out.prefer_sweep
    if ops:
        strat["operators"] = ops
    return strat


class LLMStrategist:
    """Structured-output meta-controller. Falls back to the rule baseline (and ultimately None) on
    any parse/transport failure, so a flaky local model never crashes the run."""

    def __init__(self, client, n_seeds: int = 3, parser: str = "tool_call", prompts=None):
        self.client = client
        self.parser = parser
        self.prompts = prompts   # hot-reloadable PromptStore (I18, ADR-8); None = inline default
        self._rule = RuleStrategist(n_seeds=n_seeds)

    def decide(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
        from looplab.core.parse import ParseError, parse_structured
        messages = [
            {"role": "system", "content": render(self.prompts, "strategist_system", _STRATEGIST_SYSTEM)},
            {"role": "user", "content": _strategist_brief(state, ctx)},
        ]
        try:
            out = parse_structured(self.client, messages, _StrategyOut, self.parser)
        except BudgetExceeded:      # a hard budget stop must end the run, not degrade to the rule
            raise
        except (ParseError, Exception):  # noqa: BLE001 — never crash the run on a strategy call
            return self._rule.decide(state, ctx)   # graceful fallback to deterministic heuristics
        return _assemble_strategy(out)


_TOOL_STRATEGIST_SYSTEM = (
    _STRATEGIST_SYSTEM + " You MAY first investigate before deciding: call the read-only tools to "
    "read this run's experiments, code and themes, the task data/schema, SIBLING runs of the same "
    "task, the knowledge base and memory of past cases, and (if available) skills/literature/web. "
    "Ground your strategy in what actually happened — then call `emit` exactly once with the chosen "
    "strategy."
)


class ToolUsingStrategist:
    """Agentic Strategist (same `Strategist` protocol): a `drive_tool_loop` agent that can READ the
    run, the data, sibling runs, the knowledge base + memory (and skills/literature/web when wired)
    before emitting one Strategy — so meta-decisions are evidence-based, not stats-only. Inherits the
    shared loop's B1 stuck guard + C1 self-plan + C2 auto-summary. Falls back to the deterministic
    RuleStrategist on any parse/transport failure, so a flaky model never crashes the run."""

    def __init__(self, client, tools=None, n_seeds: int = 3, parser: str = "tool_call",
                 loop_opts: Optional[dict] = None, max_turns: int = 0,
                 time_budget_s: float = 0.0, context_budget_chars: int = 0, prompts=None):
        self.client = client
        self.tools = tools          # CompositeTools of read-only providers (None = emit-only, like LLM)
        self.parser = parser
        self.prompts = prompts      # hot-reloadable PromptStore (I18, ADR-8); None = inline default
        self._rule = RuleStrategist(n_seeds=n_seeds)
        self.loop_opts = loop_opts or {}
        self.max_turns = max_turns
        self.time_budget_s = time_budget_s
        self.context_budget_chars = context_budget_chars

    def _emit_spec(self) -> dict:
        return {"type": "function", "function": {
            "name": "emit", "description": "Emit the chosen search strategy.",
            "parameters": _StrategyOut.model_json_schema()}}

    def decide(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
        from looplab.agents.agent import drive_tool_loop
        if self.tools is not None and hasattr(self.tools, "bind_state"):
            self.tools.bind_state(state)        # let the run-aware tools read the current search
        messages = [
            {"role": "system",
             "content": render(self.prompts, "tool_strategist_system", _TOOL_STRATEGIST_SYSTEM)},
            {"role": "user", "content": _strategist_brief(state, ctx)
                + "\nInvestigate with the tools if useful, then emit the strategy."},
        ]

        def _finalize(args: dict) -> Optional[Strategy]:
            try:
                return _assemble_strategy(_StrategyOut.model_validate(args), source="agent")
            except Exception:  # noqa: BLE001 — a junk emit must not crash the run
                return self._rule.decide(state, ctx)

        def _fallback(_messages) -> Optional[Strategy]:
            return self._rule.decide(state, ctx)   # no emit -> deterministic baseline

        try:
            return drive_tool_loop(
                self.client, self.tools, messages, self._emit_spec(),
                max_turns=self.max_turns, time_budget_s=self.time_budget_s,
                context_budget_chars=self.context_budget_chars,
                finalize=_finalize, fallback=_fallback, **self.loop_opts)
        except BudgetExceeded:      # a hard budget stop must end the run, not degrade to the rule
            raise
        except Exception:  # noqa: BLE001 — the model/endpoint can't drive tools at all -> rule baseline
            return self._rule.decide(state, ctx)


def make_strategist(settings, *, client=None, n_seeds: int = 3, tools=None) -> Optional[Strategist]:
    """Select the Strategist backend from config (config-first). `off` -> None (engine uses the static
    config policy). `rule` -> deterministic. `llm` -> single structured-output call. `agent` -> a
    tool-using agent that reads the run/data/siblings/KB/memory before deciding (`tools` is the
    read-only toolset; None falls back to emit-only). `llm`/`agent` need an LLM client (else the rule
    baseline)."""
    backend = getattr(settings, "strategist_backend", "off")
    if backend == "off":
        return None
    if backend == "rule":
        return RuleStrategist(n_seeds=n_seeds)
    parser = getattr(settings, "llm_parser", "tool_call")
    # Hot-reloadable prompt store (I18, ADR-8): lets `strategist_system.md` /
    # `tool_strategist_system.md` override the built-in system prompts; no prompt_dir (or no
    # file) keeps the inline defaults byte-identical.
    prompts = (PromptStore(settings.prompt_dir)
               if getattr(settings, "prompt_dir", None) else None)
    if backend == "llm":
        if client is None:
            return RuleStrategist(n_seeds=n_seeds)   # no model wired -> deterministic fallback
        return LLMStrategist(client, n_seeds=n_seeds, parser=parser, prompts=prompts)
    if backend == "agent":
        if client is None:
            return RuleStrategist(n_seeds=n_seeds)
        from looplab.agents.agent import loop_opts_from_settings
        return ToolUsingStrategist(
            client, tools=tools, n_seeds=n_seeds, parser=parser, prompts=prompts,
            loop_opts=loop_opts_from_settings(settings),
            max_turns=getattr(settings, "agent_max_turns", 0),
            time_budget_s=getattr(settings, "agent_time_budget_s", 0.0),
            context_budget_chars=getattr(settings, "context_budget_chars", 0))
    raise ValueError(f"unknown strategist_backend: {backend!r}")
