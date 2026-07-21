"""A7 · Strategist — optional adaptive meta-control (ADR-2, user-requested).

The Strategist is an OPTIONAL meta-controller that, at a bounded cadence, reads the folded
`RunState` and decides *which search machinery to use next*: the search policy/allocator, the
operator mix, the eval fidelity, and (when a Developer factory is wired) the Developer backend.
It never selects a node itself and never writes a domain event — it emits an audit-only
`strategy_decision` that swaps the active policy/operators. Every field it can decide is also a
direct `Settings` knob, so the Strategist is a convenience layer over the same config, fully
hand-overridable, and `backend="off"` is byte-identical to today's legacy static-config
behavior (the shipped default is `"agent"` — the tool-using agentic meta-controller consulted at
cadence; `"llm"`/`"rule"` are the lighter single-shot backends).

Replay-safe by construction: the chosen `Strategy` is recorded in the event log and reconstructed
by `replay.fold`; the (possibly non-deterministic) LLM backend is NEVER re-invoked during replay —
exactly how an LLM `Idea` is recorded in `node_created` and replayed without a model call.

Two backends:
- `RuleStrategist` — deterministic heuristics over pure folded state (zero-dep, the LLM fallback).
- `LLMStrategist`  — structured output via the existing `llm`/`parse` stack; degrades to None
  (keep current strategy) on any parse/transport failure, never crashing the run.
"""
from __future__ import annotations

import math
from typing import Literal, Optional, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from looplab.agents.roles import _attention_points
from looplab.core.config import PARALLELISM_ALIASES, canonicalize_parallelism_source
from looplab.core.llm import BudgetExceeded
from looplab.core.llm_broker import LLM_LANES
from looplab.core.models import NodeStatus, RunState
from looplab.core.prompts import PromptStore, render

# The novelty-stance vocabulary (the Strategist-owned dial). Centralized so the write side
# (validate_strategy) and the apply side (Engine._apply_strategy) share ONE source of truth — a typo
# can't silently accept an unknown stance, and a new stance is added in exactly one place.
# "balanced" == today's behavior. (The `== "explore"` READ-side checks in the proposer / foresight /
# novelty gate stay inline literals — each is exercised by tests, so a typo there fails loudly.)
NOVELTY_STANCES: tuple[str, ...] = ("explore", "balanced", "exploit")
CARD_SCORING_STANCES: tuple[str, ...] = ("explore", "balanced", "exploit")

_CARD_SCORING_FIELDS = frozenset({"stance", "novelty_weight", "coverage_weight"})


def validate_card_scoring(value: object) -> Optional[dict]:
    """Validate one atomic ``Strategy.card_scoring`` treatment.

    The scorer is selection-affecting, so partial, extended, boolean-as-number and non-finite maps
    fail closed instead of inheriting implicit values. Weights are independent bounded coefficients;
    they need not sum to one because the pure scorer normalizes their relative contribution.
    """
    if not isinstance(value, dict) or set(value) != _CARD_SCORING_FIELDS:
        return None
    stance = value.get("stance")
    if stance not in CARD_SCORING_STANCES:
        return None
    clean: dict = {"stance": stance}
    for name in ("novelty_weight", "coverage_weight"):
        raw = value.get(name)
        if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                or not math.isfinite(float(raw)) or not 0.0 <= float(raw) <= 1.0):
            return None
        clean[name] = float(raw)
    return clean

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
        "card_scoring": dict,   # atomic {stance, novelty_weight, coverage_weight} Card treatment
        "novelty_stance": str,  # "explore"|"balanced"|"exploit" — how much novelty pressure to apply
                                # downstream (researcher proposal + foresight rank + novelty gate).
                                # "balanced" == today's behavior; the Strategist owns this dial.
        "timeout": float,       # per-eval wall-clock budget (s) — applied only if the matrix allows it
        # Layer-2 canonical parallelism names (docs/23) — prefer these; the two legacy names below stay
        # accepted for back-compat. Applied only if the agent-control matrix allows it.
        "eval_parallel": int,   # live eval width (GPU consumer); 0 settles to safe serial width 1
        "llm_parallel": int,    # live provider-call total + build width; 0 settles to safe serial 1
        "llm_lane_limits": dict,  # per-lane LLM allotments; each live 0 settles to 1
        "max_parallel": int,    # legacy alias of eval_parallel — applied only if the matrix allows it
        "parallel_build": int,  # legacy alias of llm_parallel (live 0 -> serial 1) — if allowed
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
    eval_parallel: int = 1                     # current settled eval width (never startup AUTO/0)
    # `_llm_parallel` is also the settled build fan-out used by legacy-compatible producer code.
    # It is *not* proof that the shared broker has a finite total: canonical-unset startup keeps
    # the broker total unbounded while resolving this build width from eval concurrency.
    llm_parallel: int = 1
    llm_total: Optional[int] = None             # live shared-broker total; None = unbounded
    llm_lane_limits: dict[str, int | None] = Field(default_factory=dict)
    card_driven_selection: bool = False
    # Current live Card treatment. Distinct from policy and novelty_stance: it only ranks already
    # eligible Cards and is inert while the run-start-pinned Card selector is off.
    card_scoring: dict = Field(default_factory=lambda: {
        "stance": "balanced", "novelty_weight": 0.5, "coverage_weight": 0.5,
    })
    available_policies: list[str] = Field(default_factory=list)
    available_developers: list[str] = Field(default_factory=list)
    defaults: dict = Field(default_factory=dict)   # the static config Strategy (fallback/start)
    # Breadth read-model (search/coverage.py): themes/niches/theme_entropy/dominant_theme_frac.
    # CONTEXT the Strategist reads to judge how much novelty pressure to apply — it is informative,
    # not a decision (the LLM decides). Empty when coverage_context is off.
    coverage: dict = Field(default_factory=dict)
    # Per-operator empirical yield (search/policy.py::operator_yields) {op: {n, gain}} — how much
    # each operator has actually moved the metric per eval-second so far. Signal-delivery (§1): the
    # Strategist tunes `ablate_every`/`merge_mode` but previously judged from priors only; this lets
    # it set cadences from the run's OWN evidence. Empty on an early/degenerate run.
    operator_yields: dict = Field(default_factory=dict)
    # PART V §22 — a bounded live CROSS-RUN observation note, populated by the engine when
    # `cross_run_advisory` is on. It has no frozen corpus or coverage denominator; advisory prose, empty when off.
    cross_run_note: str = ""
    # Immutable evidence receipt for the scoped snapshot rendered into ``cross_run_note``. It is persisted
    # with strategy_decision but omitted from the prose brief; no raw memory text is duplicated here.
    cross_run_receipt: dict = Field(default_factory=dict)


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
               if n.id > best_id and n.operator in ("improve", "refine_block", "merge", "expand"))


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


def _rule_novelty_stance(ctx: StrategyContext) -> Optional[str]:
    """Deterministic novelty stance from the coverage read-model (the RuleStrategist's counterpart to
    the LLM's own choice). Returns None (leave unset -> balanced) unless there's a clear signal, so a
    bare StrategyContext with no coverage never perturbs today's behavior:
      - endgame / nearly-spent budget -> `exploit` (converge on the leader, don't open new breadth);
      - the run is NARROWING (the recent window or the whole run concentrates on one theme) with
        enough nodes to trust the signal -> `explore`;
      - otherwise None."""
    cov = ctx.coverage or {}
    if cov.get("nodes", 0) < 3:                       # too little signal to steer novelty
        return None
    if ctx.node_budget_frac >= 0.8 or ctx.defaults.get("_budget_frac", 1.0) < 0.2:
        return "exploit"
    if cov.get("recent_dominant_frac", 0.0) >= 0.75 or cov.get("dominant_theme_frac", 0.0) >= 0.6:
        return "explore"
    return None


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
    ns = strat.get("novelty_stance")
    if ns in NOVELTY_STANCES:
        out["novelty_stance"] = ns
    card_scoring = validate_card_scoring(strat.get("card_scoring"))
    if ctx.card_driven_selection and card_scoring is not None:
        out["card_scoring"] = card_scoring
    # Resource budgets (bounds match config: timeout>0, eval parallelism >=0). Whitelisted here for shape;
    # the engine's _apply_strategy applies them ONLY if the governance matrix grants the strategist.
    tmo = strat.get("timeout")
    if (isinstance(tmo, (int, float)) and not isinstance(tmo, bool)
            and math.isfinite(tmo) and tmo > 0):
        out["timeout"] = float(tmo)
    # Layer-2 canonical parallelism names (docs/23) + their legacy aliases. Bounds match config
    # (eval_parallel 0..1024, llm_parallel 0..64). Live 0 settles to serial width 1 in
    # _apply_strategy; only startup Settings resolve AUTO from hardware/the settled eval width.
    ep = strat.get("eval_parallel")
    if isinstance(ep, int) and not isinstance(ep, bool) and 0 <= ep <= 1024:
        out["eval_parallel"] = ep
    mp = strat.get("max_parallel")
    if isinstance(mp, int) and not isinstance(mp, bool) and 0 <= mp <= 1024:
        out["max_parallel"] = mp   # legacy alias of eval_parallel, resolved in _apply_strategy
    lp = strat.get("llm_parallel")
    if isinstance(lp, int) and not isinstance(lp, bool) and 0 <= lp <= 64:
        out["llm_parallel"] = lp
    lane_limits = strat.get("llm_lane_limits")
    if isinstance(lane_limits, dict) and lane_limits:
        # One allocation is atomic. Reject the whole mapping on an unknown lane or malformed value
        # rather than silently applying a surprising partial paid-call budget. Values stay RAW in the
        # durable Strategy; the live apply boundary settles 0 to one worker, just like the totals.
        clean_lanes: dict[str, int] = {}
        for lane, value in lane_limits.items():
            if (lane not in LLM_LANES or isinstance(value, bool)
                    or not isinstance(value, int) or not 0 <= value <= 64):
                clean_lanes = {}
                break
            clean_lanes[lane] = value
        if clean_lanes:
            out["llm_lane_limits"] = clean_lanes
    pb = strat.get("parallel_build")
    if isinstance(pb, int) and not isinstance(pb, bool) and 0 <= pb <= 64:
        out["parallel_build"] = pb   # legacy alias of llm_parallel, resolved in _apply_strategy
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
        """Pick the search machinery, then overlay a coverage-driven `novelty_stance` (deterministic,
        pure over ctx). The stance is the offline/fallback counterpart to the LLM Strategist's own
        stance choice: `explore` when the coverage read-model shows the run narrowing onto one theme,
        `exploit` in the endgame/low-budget, else left unset (== balanced, today's behavior). Empty
        coverage (e.g. a bare StrategyContext) leaves the stance unset, so nothing changes."""
        strat = self._decide_machinery(state, ctx)
        ns = _rule_novelty_stance(ctx)
        if ns:
            strat = dict(strat or {})
            strat.setdefault("source", "rule")
            strat.setdefault("rationale", f"novelty_stance={ns} (coverage-driven)")
            strat["novelty_stance"] = ns
        if ctx.card_driven_selection:
            strat = dict(strat or {})
            strat.setdefault("source", "rule")
            strat.setdefault("rationale", "card_scoring=balanced (neutral coverage signal)")
            # Always author the complete treatment in Card mode. Strategy decisions merge onto the
            # active record, so omitting this field when coverage returns to neutral would retain a
            # stale explore/exploit treatment from an earlier cadence.
            if ns == "explore":
                strat["card_scoring"] = {
                    "stance": "explore", "novelty_weight": 0.55, "coverage_weight": 0.75,
                }
            elif ns == "exploit":
                strat["card_scoring"] = {
                    "stance": "exploit", "novelty_weight": 0.25, "coverage_weight": 0.25,
                }
            else:
                strat["card_scoring"] = {
                    "stance": "balanced", "novelty_weight": 0.5, "coverage_weight": 0.5,
                }
        return strat or None

    def _decide_machinery(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
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
    "richer operators over fancier search (operators are the verified bottleneck). You also own "
    "`novelty_stance` (explore|balanced|exploit): how hard the proposer, the foresight ranker and "
    "the novelty gate should push for NEW directions vs refining the leader. READ the coverage "
    "signal (theme spread / dominant-theme concentration) — choose `explore` when the search is "
    "NARROWING onto one theme (high dominant-theme fraction / low theme entropy, especially in the "
    "recent window), `exploit` in the endgame or when a fresh lead is compounding, else `balanced` "
    "(== today's behavior). You may retune the two INDEPENDENT canonical concurrency axes: "
    "`eval_parallel` (0..1024, concurrent evaluations) and `llm_parallel` (0..64, concurrent LLM "
    "provider calls). You may also allocate that LLM budget with `llm_lane_limits` over the closed "
    "lanes build, deep_research, novelty_dedup, enrichment, and engine (each 0..64). Emit ONLY "
    "canonical names, never the legacy max_parallel/parallel_build aliases. "
    "A live value of 0 safely serializes that axis to 1; startup config uses 0 as hardware AUTO. "
    "Respond ONLY with the requested structured fields; pick `policy` from the provided available list."
)

# This is appended *after* PromptStore rendering.  It is a runtime/durable semantics contract, not
# tunable strategy advice: a custom operator prompt must not accidentally turn a replacement map
# into an apparent patch map and silently remove existing background-lane caps.
_LLM_LANE_ALLOCATION_CONTRACT = (
    "`llm_lane_limits` is an ATOMIC replacement map: when emitted, it replaces the previous lane "
    "allocation. Omitted lanes are unbounded within the shared `llm_parallel` total; include every "
    "lane whose cap must remain. Omitting `llm_lane_limits` entirely retains the current allocation."
)


def canonicalize_strategy_parallelism(strat: Optional[dict]) -> dict:
    """Return one spelling per parallelism axis for durable/live Strategy deltas.

    A partial legacy delta must first promote to canonical and then discard both legacy spellings.
    Otherwise merging it onto an active Strategy that already contains a canonical value leaves the
    stale canonical value to win at apply time, silently dropping the newer delta.
    """
    out = canonicalize_parallelism_source(strat or {})
    for legacy, canonical in PARALLELISM_ALIASES.items():
        if canonical in out:
            out.pop(legacy, None)
    return out


class _LLMLaneLimitsOut(BaseModel):
    """Closed structured-output vocabulary for an optional per-lane allocation."""
    model_config = ConfigDict(extra="forbid")

    build: Optional[int] = Field(default=None, ge=0, le=64)
    deep_research: Optional[int] = Field(default=None, ge=0, le=64)
    novelty_dedup: Optional[int] = Field(default=None, ge=0, le=64)
    enrichment: Optional[int] = Field(default=None, ge=0, le=64)
    engine: Optional[int] = Field(default=None, ge=0, le=64)

    @field_validator(*LLM_LANES, mode="before")
    @classmethod
    def _lane_width_is_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("LLM lane width must not be boolean")
        return value


class _CardScoringOut(BaseModel):
    """Closed, atomic structured-output vocabulary for Card queue treatment."""
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    stance: Literal["explore", "balanced", "exploit"]
    novelty_weight: float = Field(ge=0.0, le=1.0)
    coverage_weight: float = Field(ge=0.0, le=1.0)

    @field_validator("novelty_weight", "coverage_weight", mode="before")
    @classmethod
    def _weight_is_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("Card scoring weight must not be boolean")
        return value


class _StrategyOut(BaseModel):
    """Structured shape the LLM fills (a subset of Strategy; validated again by validate_strategy)."""
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    policy: Optional[str] = None
    fidelity: Optional[str] = None
    novelty_stance: Optional[str] = None    # explore|balanced|exploit — novelty pressure downstream
    ablate_every: Optional[int] = None
    merge_mode: Optional[str] = None
    complexity_cue: Optional[bool] = None
    prefer_sweep: Optional[bool] = None
    request_research: Optional[bool] = None
    timeout: Optional[float] = Field(default=None, gt=0)
    eval_parallel: Optional[int] = Field(default=None, ge=0, le=1024)
    llm_parallel: Optional[int] = Field(default=None, ge=0, le=64)
    llm_lane_limits: Optional[_LLMLaneLimitsOut] = None
    rationale: str = ""

    @field_validator("timeout", "eval_parallel", "llm_parallel", mode="before")
    @classmethod
    def _resource_scalars_are_not_booleans(cls, value):
        # CODEX AGENT: JSON booleans are numeric subclasses in Python; accepting true as width/timeout
        # 1 makes a malformed tool result look valid and diverges from validate_strategy's contract.
        if isinstance(value, bool):
            raise ValueError("resource scalar must not be boolean")
        return value


class _CardStrategyOut(_StrategyOut):
    """Flag-on extension; the legacy schema remains byte-identical while Card selection is off."""
    card_scoring: Optional[_CardScoringOut] = None


def _strategy_output_model(ctx: StrategyContext):
    return _CardStrategyOut if ctx.card_driven_selection else _StrategyOut


def _fmt_operator_yields(yields: dict) -> str:
    """Render per-operator empirical yield as one compact line for the Strategist prompt (evidence
    for the operator mix — the model may raise/lower ablate_every or switch merge_mode when the data
    shows an operator paying off or not). `unavailable` on an early run with no attributed gains."""
    if not yields:
        return "unavailable"
    return "; ".join(
        f"{op}: gain={d.get('gain', 0.0):.4g}/s over {d.get('n', 0)}"
        for op, d in sorted(yields.items(), key=lambda kv: -(kv[1].get('gain') or 0.0)))


def _fmt_coverage(cov: dict) -> str:
    """Render the breadth read-model as one compact line for the Strategist prompt (the narrowing
    signal is CONTEXT: it informs the novelty_stance the model chooses, it does not decide it)."""
    if not cov:
        return "unavailable"
    return (f"themes={cov.get('themes', 0)} niches={cov.get('niches', 0)} "
            f"operators={cov.get('operators', 0)} "
            f"theme_entropy={cov.get('theme_entropy', 0.0)} "
            f"dominant_theme_frac={cov.get('dominant_theme_frac', 0.0)} "
            f"recent_dominant_frac={cov.get('recent_dominant_frac', 0.0)} "
            f"top_themes={cov.get('top_themes', [])}")


def _strategist_brief(state: RunState, ctx: StrategyContext) -> str:
    """The compact decision brief shared by the structured-output and tool-using Strategists."""
    brief = (
        f"phase={ctx.phase} nodes={ctx.node_count} failure_rate={ctx.failure_rate:.2f} "
        f"improves_since_best={ctx.improves_since_best} numeric_space={ctx.is_numeric_space} "
        f"eval_budget_remaining={ctx.eval_budget_remaining}\n"
        f"available_policies={ctx.available_policies} avg_eval_seconds={ctx.avg_eval_seconds}\n"
        f"current runtime concurrency: eval_parallel={ctx.eval_parallel}; "
        f"LLM broker total={ctx.llm_total if ctx.llm_total is not None else 'unbounded'} "
        f"(the value to change with canonical llm_parallel); "
        f"current build fan-out={ctx.llm_parallel}; LLM lanes={ctx.llm_lane_limits}\n"
        f"coverage (narrowing signal): {_fmt_coverage(ctx.coverage)}\n"
        + (f"bounded cross-run observations (not coverage): {ctx.cross_run_note}\n"
           if ctx.cross_run_note else "")
        + f"operator yields (evidence for the operator mix — mean metric gain per eval-second, n tried): "
        f"{_fmt_operator_yields(ctx.operator_yields)}\n"
        "Choose the next strategy (policy from the available list; fidelity smoke|full|adaptive; "
        "novelty_stance explore|balanced|exploit — pick explore when coverage shows the search "
        "narrowing onto one theme (high dominant_theme_frac / low theme_entropy), exploit in the "
        "endgame or on a compounding lead, else balanced; "
        "optional ablate_every, merge_mode mean|ensemble, complexity_cue, prefer_sweep — set "
        "prefer_sweep=true to bias the researcher toward an in-process hyperparameter sweep when "
        "evals are costly and the space is numeric; set request_research=true when the run is "
        "stalled or confused and would benefit from a deep-research step over all results + the "
        "web before continuing; optional timeout (>0), eval_parallel (0..1024), and llm_parallel "
        "(0..64 total provider calls), plus llm_lane_limits over build/deep_research/novelty_dedup/"
        "enrichment/engine (each 0..64). Use only those canonical parallel names. These are live deltas: "
        "0 means serial 1 for a total or lane; "
        "startup settings use 0 for hardware AUTO)."
    )
    if ctx.card_driven_selection:
        brief += (
            "\nCard-driven selection is enabled. Current Card scoring treatment="
            f"{ctx.card_scoring}. You may independently return card_scoring as the COMPLETE ATOMIC "
            "object {stance: explore|balanced|exploit, novelty_weight: 0..1, "
            "coverage_weight: 0..1}; it ranks already-eligible Cards and is distinct from policy."
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
    if out.novelty_stance:
        strat["novelty_stance"] = out.novelty_stance
    if out.request_research:
        strat["request_research"] = True
    if out.timeout is not None:
        strat["timeout"] = out.timeout
    if out.eval_parallel is not None:
        strat["eval_parallel"] = out.eval_parallel
    if out.llm_parallel is not None:
        strat["llm_parallel"] = out.llm_parallel
    if out.llm_lane_limits is not None:
        lanes = out.llm_lane_limits.model_dump(exclude_none=True)
        if lanes:
            strat["llm_lane_limits"] = lanes
    card_scoring = getattr(out, "card_scoring", None)
    if card_scoring is not None:
        strat["card_scoring"] = card_scoring.model_dump()
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
        output_model = _strategy_output_model(ctx)
        messages = [
            # P8: the Strategist decides timeouts/parallelism/fidelity, so the hardware attention
            # points reach it too — appended after the render(), like every other planning role.
            {"role": "system", "content": render(self.prompts, "strategist_system", _STRATEGIST_SYSTEM)
                               + "\n\n" + _LLM_LANE_ALLOCATION_CONTRACT
                               + "\n\n" + _attention_points()},
            {"role": "user", "content": _strategist_brief(state, ctx)},
        ]
        try:
            out = parse_structured(self.client, messages, output_model, self.parser)
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
                 time_budget_s: float = 0.0, context_budget_chars: int | None = None, prompts=None):
        self.client = client
        self.tools = tools          # CompositeTools of read-only providers (None = emit-only, like LLM)
        self.parser = parser
        self.prompts = prompts      # hot-reloadable PromptStore (I18, ADR-8); None = inline default
        self._rule = RuleStrategist(n_seeds=n_seeds)
        self.max_turns = max_turns
        self.time_budget_s = time_budget_s
        self.context_budget_chars = context_budget_chars
        # Collapse the two sources of context_budget_chars once, here (see ToolUsingResearcher.__init__):
        # loop_opts_from_settings injects it AND it arrives as a ctor kwarg — passing both to
        # drive_tool_loop would raise TypeError, caught below as a "can't drive tools" degrade to the RULE
        # baseline in the default config. Merging in __init__ makes the collision impossible per call.
        self.loop_opts = dict(loop_opts or {})
        self.loop_opts.setdefault("context_budget_chars", context_budget_chars)

    def _emit_spec(self, ctx: StrategyContext) -> dict:
        return {"type": "function", "function": {
            "name": "emit", "description": "Emit the chosen search strategy.",
            "parameters": _strategy_output_model(ctx).model_json_schema()}}

    def decide(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
        from looplab.agents.agent import drive_tool_loop
        output_model = _strategy_output_model(ctx)
        if self.tools is not None and hasattr(self.tools, "bind_state"):
            self.tools.bind_state(state)        # let the run-aware tools read the current search
        messages = [
            # P8: hardware attention points, after the render() like the plain LLMStrategist above.
            {"role": "system",
             "content": render(self.prompts, "tool_strategist_system", _TOOL_STRATEGIST_SYSTEM)
                        + "\n\n" + _LLM_LANE_ALLOCATION_CONTRACT
                        + "\n\n" + _attention_points()},
            {"role": "user", "content": _strategist_brief(state, ctx)
                + "\nInvestigate with the tools if useful, then emit the strategy."},
        ]

        def _finalize(args: dict) -> Optional[Strategy]:
            try:
                return _assemble_strategy(output_model.model_validate(args), source="agent")
            except Exception:  # noqa: BLE001 — a junk emit must not crash the run
                return self._rule.decide(state, ctx)

        def _fallback(_messages) -> Optional[Strategy]:
            return self._rule.decide(state, ctx)   # no emit -> deterministic baseline

        try:
            # context_budget_chars is folded into self.loop_opts once in __init__ (see there) — pass the
            # merged opts straight through, no per-call re-merge, no double-keyword collision.
            return drive_tool_loop(
                self.client, self.tools, messages, self._emit_spec(ctx),
                max_turns=self.max_turns, time_budget_s=self.time_budget_s,
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
    backend = getattr(settings, "strategist_backend", "agent")   # fallback MATCHES the Settings default (P3)
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
            context_budget_chars=getattr(settings, "context_budget_chars", None))
    raise ValueError(f"unknown strategist_backend: {backend!r}")
