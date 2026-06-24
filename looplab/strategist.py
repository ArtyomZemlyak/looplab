"""A7 · Strategist — optional adaptive meta-control (ADR-2, user-requested).

The Strategist is an OPTIONAL meta-controller that, at a bounded cadence, reads the folded
`RunState` and decides *which search machinery to use next*: the search policy/allocator, the
operator mix, the eval fidelity, and (when a Developer factory is wired) the Developer backend.
It never selects a node itself and never writes a domain event — it emits an audit-only
`strategy_decision` that swaps the active policy/operators. Every field it can decide is also a
direct `Settings` knob, so the Strategist is a convenience layer over the same config, fully
hand-overridable, and `backend="off"` (the default) is byte-identical to today's behavior.

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

from .models import NodeStatus, RunState

# A fully-serializable description of the active search machinery. Every field maps to an existing
# config knob, so a Strategy is just "a settings delta the engine applies live".
Strategy = TypedDict(
    "Strategy",
    {
        "policy": str,          # "greedy"|"evolutionary"|"mcts"|"asha" (whatever make_policy knows)
        "policy_params": dict,  # {"c":1.4} | {"eta":3} | {"n_seeds":4} ...
        "developer": str,       # "default"|"llm"|"opencode"|... (whatever the dev factory knows)
        "operators": dict,      # {"ablate_every":int, "merge_mode":str, "complexity_cue":bool, ...}
        "fidelity": str,        # "smoke"|"full"|"adaptive"
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
        if clean:
            out["operators"] = clean
    fid = strat.get("fidelity")
    if fid in ("smoke", "full", "adaptive"):
        out["fidelity"] = fid
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

        # High failure rate -> stop spending breadth on broken code; deepen repair, narrow search.
        if ctx.failure_rate > 0.4:
            strat: Strategy = {"policy": "greedy", "fidelity": "adaptive",
                               "rationale": f"high failure rate ({ctx.failure_rate:.0%}): "
                                            "narrow to greedy + deeper repair",
                               "source": "rule"}
            if "agentless" in ctx.available_developers:
                strat["developer"] = "agentless"   # only when C5 has landed
            return strat

        # Stall: the leader hasn't been dethroned for a while. Per the verified "operators > search"
        # finding, first probe operators (bump ablation); if MCTS is available, switch to explore.
        if ctx.improves_since_best >= self.stall_window:
            if "mcts" in avail:
                return {"policy": "mcts", "policy_params": {"c": 1.4}, "fidelity": "adaptive",
                        "rationale": f"stalled for {ctx.improves_since_best} improves: "
                                     "switch greedy->mcts to explore under-visited subtrees",
                        "source": "rule"}
            return {"policy": "greedy", "operators": {"ablate_every": 2}, "fidelity": "adaptive",
                    "rationale": f"stalled for {ctx.improves_since_best} improves: probe operators "
                                 "(ablate the leader) — the verified higher-leverage move than search",
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
    rationale: str = ""


class LLMStrategist:
    """Structured-output meta-controller. Falls back to the rule baseline (and ultimately None) on
    any parse/transport failure, so a flaky local model never crashes the run."""

    def __init__(self, client, n_seeds: int = 3, parser: str = "tool_call"):
        self.client = client
        self.parser = parser
        self._rule = RuleStrategist(n_seeds=n_seeds)

    def decide(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
        from .parse import ParseError, parse_structured
        brief = (
            f"phase={ctx.phase} nodes={ctx.node_count} failure_rate={ctx.failure_rate:.2f} "
            f"improves_since_best={ctx.improves_since_best} numeric_space={ctx.is_numeric_space} "
            f"eval_budget_remaining={ctx.eval_budget_remaining}\n"
            f"available_policies={ctx.available_policies}\n"
            "Choose the next strategy (policy from the available list; fidelity smoke|full|adaptive; "
            "optional ablate_every, merge_mode mean|ensemble, complexity_cue)."
        )
        messages = [
            {"role": "system", "content": _STRATEGIST_SYSTEM},
            {"role": "user", "content": brief},
        ]
        try:
            out = parse_structured(self.client, messages, _StrategyOut, self.parser)
        except (ParseError, Exception):  # noqa: BLE001 — never crash the run on a strategy call
            return self._rule.decide(state, ctx)   # graceful fallback to deterministic heuristics
        strat: Strategy = {"source": "llm", "rationale": out.rationale or "llm-chosen strategy"}
        if out.policy:
            strat["policy"] = out.policy
        if out.fidelity:
            strat["fidelity"] = out.fidelity
        ops: dict = {}
        if out.ablate_every is not None:
            ops["ablate_every"] = out.ablate_every
        if out.merge_mode:
            ops["merge_mode"] = out.merge_mode
        if out.complexity_cue is not None:
            ops["complexity_cue"] = out.complexity_cue
        if ops:
            strat["operators"] = ops
        return strat


def make_strategist(settings, *, client=None, n_seeds: int = 3) -> Optional[Strategist]:
    """Select the Strategist backend from config (config-first). `off` (default) -> None (engine
    uses the static config policy, == today). `rule` -> deterministic. `llm` -> structured output,
    needs an LLM client (falls back to the rule baseline when none is available)."""
    backend = getattr(settings, "strategist_backend", "off")
    if backend == "off":
        return None
    if backend == "rule":
        return RuleStrategist(n_seeds=n_seeds)
    if backend == "llm":
        if client is None:
            return RuleStrategist(n_seeds=n_seeds)   # no model wired -> deterministic fallback
        return LLMStrategist(client, n_seeds=n_seeds, parser=getattr(settings, "llm_parser", "tool_call"))
    raise ValueError(f"unknown strategist_backend: {backend!r}")
