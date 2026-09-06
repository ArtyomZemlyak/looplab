"""SearchPolicy (I6/I7/I11, ADR-18). `GreedyTree`: seed K drafts, then repeatedly
improve the current best, periodically merging the top-2 (multi-parent DAG step),
until the node budget is spent. (It no longer debugs failed leaves: the Debug node
was deleted on 2026-08-13 — see the block above `operator_yields`.)

The policy is *pure*: it reads a RunState and returns the next actions; the
orchestrator executes them. This is our moat (the loop), not a framework graph.

Action kinds:
    {"kind": "draft"}
    {"kind": "improve", "parent_id": int}
    {"kind": "debug",   "parent_id": int}   # HISTORICAL — no producer since 2026-08-13 (F5)
    {"kind": "merge",   "parent_ids": [int, int]}
    {"kind": "evaluate","node_id": int}
    {"kind": "ablate",  "parent_id": int}

Actions may additionally carry underscore-prefixed meta keys (annotations for the
engine's event log, not part of the action proper): `_scores` (per-candidate
comparison surfaced as a `policy_decision` event), `_chosen` (the candidate the
policy picked), `_reason` (one-line why), and — ASHA only — `_rung` / `_promoted`
(promotion bookkeeping logged as a `rung_promoted` event).
"""
from __future__ import annotations

import math
from typing import Callable, Optional, Protocol

from looplab.core.errors import ConfigRefusal
from looplab.core.models import NodeStatus, RunState

# Action kinds (the vocabulary of the action schema above; values are load-bearing in
# the event log, so they never change).
KIND_DRAFT = "draft"
KIND_IMPROVE = "improve"
# HISTORICAL ONLY since F5 (2026-08-13): the Debug node was deleted and NOTHING mints this kind any
# more. The spelling stays because these values are load-bearing in the event log — every preserved
# run with a `debug` node has to keep folding, rendering and replaying exactly as it did. A new
# producer is the defect, not a missing constant; `tests/test_debug_node_removed.py` is what refuses
# one. See the block above `operator_yields` for the decision and why it needed F8 beside it.
KIND_DEBUG = "debug"
KIND_MERGE = "merge"
KIND_EVALUATE = "evaluate"
KIND_ABLATE = "ablate"
# PART IV D7 (§21.8, §21.13): capability-EXPANSION — an improve proposal made under action-space LOCK-IN,
# where the directive is "build a capability the run never had" rather than tweak the saturated lever.
# Stamped as its OWN operator (vs a plain `improve`) so `operator_yields` MEASURES whether expanding pays
# off (SCORED, SearchFitness-competing as its own lineage) — the meta-learning plateau-escape needs. Emitted
# only when `capability_expansion` is on; a plain improve otherwise. See engine/proposal_cues.py.
# NOTE: like `refine_block`, `expand` appears as its own bucket in `operator_yields` but is NOT itself a
# `_bandit_pick` candidate, so (only when operator_bandit AND capability_expansion are BOTH on) it shifts the
# UCB normalization reference — the intended "scored distinctly" behavior, consistent with the existing
# non-candidate operators. `expand` still counts as improve-FAMILY in the generation/cadence bookkeeping.
KIND_EXPAND = "expand"
# Engine-facing action meta keys (see the module docstring).
META_SCORES = "_scores"
META_CHOSEN = "_chosen"
META_REASON = "_reason"
META_RUNG = "_rung"
META_PROMOTED = "_promoted"
# THE MODEL ARM (doc 52 row 19): which model the bandit routed a build to. `_model` rides only on
# an action the bandit branch produced with arms declared; the engine builds under that arm's model
# (`core/llm.py::model_override`) and records the arm on `node_created`, which `model_arm_yields`
# folds so the pick learns per arm. The configured Developer model is the implicit `default` arm.
META_MODEL = "_model"
DEFAULT_MODEL_ARM = "default"


def parse_model_arms(raw) -> dict[str, tuple[str, float]]:
    """`Settings.model_arms` — `{arm: "model-id[@relative-cost]"}` — as `{arm: (model, cost)}`.

    The cost is the arm's price RELATIVE to the configured model (1.0), declared by the operator
    because it is a fact about the box's endpoints the run cannot measure; the pick divides an
    arm's gain by it, which is the iso-budget lever the four 2026 results measured (LEVI, DEI,
    cross-tier routing, ShinkaEvolve's bandit). Junk rows, a non-positive cost and the reserved
    `default` name are skipped rather than guessed."""
    out: dict[str, tuple[str, float]] = {}
    if not isinstance(raw, dict):
        return out
    for name, spec in raw.items():
        if not isinstance(name, str) or not name.strip() or name == DEFAULT_MODEL_ARM:
            continue
        if not isinstance(spec, str) or not spec.strip():
            continue
        model, _, cost_text = spec.partition("@")
        model = model.strip()
        if not model:
            continue
        cost = 1.0
        if cost_text.strip():
            try:
                cost = float(cost_text)
            except ValueError:
                continue
            if not (cost > 0.0) or cost != cost:
                continue
        out[name.strip()[:64]] = (model[:200], cost)
    return out


def model_arm_costs(arms: dict[str, tuple[str, float]]) -> dict[str, float]:
    """The per-arm relative cost the policy is handed, the default arm at 1.0 first."""
    return {DEFAULT_MODEL_ARM: 1.0, **{name: cost for name, (_model, cost) in (arms or {}).items()}}


def model_arm_yields(state: RunState) -> dict[str, dict]:
    """`operator_yields`' rule keyed by the model ARM a node was built with (`Node.model_arm`; an
    unrouted node counts for the default arm), so the pick can learn which MODEL's builds pay."""
    out: dict[str, dict] = {}
    for n in state.nodes.values():
        if not n.parent_ids or n.status is not NodeStatus.evaluated or n.metric is None:
            continue
        if (n.tombstoned or not n.feasible
                or n.id in state.aborted_nodes or n.id in state.breed_excluded):
            continue
        pm = [state.nodes[p].metric for p in n.parent_ids
              if p in state.nodes and state.nodes[p].metric is not None]
        if not pm:
            continue
        base = max(pm) if state.direction == "max" else min(pm)
        delta = (n.metric - base) if state.direction == "max" else (base - n.metric)
        gain = max(0.0, delta) / max(0.1, (n.eval_seconds or 0.1))
        arm = getattr(n, "model_arm", "") or DEFAULT_MODEL_ARM
        d = out.setdefault(arm, {"n": 0, "gain": 0.0})
        d["gain"] = (d["gain"] * d["n"] + gain) / (d["n"] + 1)
        d["n"] += 1
    return out


def _model_arm_pick(yields: dict[str, dict], arms: list[str], costs: dict[str, float],
                    c: float = 0.8) -> str:
    """`_bandit_pick` over model arms with the gain COST-NORMALIZED: an untried arm is tried first
    (in arm order — the caller lists the default first), then mean gain / relative cost plus the
    same exploration bonus. Deterministic; ties break by arm order."""
    for k in arms:
        if yields.get(k, {"n": 0})["n"] == 0:
            return k
    total = sum(yields[k]["n"] for k in arms) or 1
    scored = {k: yields[k]["gain"] / max(1e-9, float(costs.get(k, 1.0))) for k in arms}
    gmax = max(scored.values(), default=0.0) or 1.0
    best_k, best_s = arms[0], None
    for k in arms:
        score = (scored[k] / gmax) + c * math.sqrt(math.log(total + 1) / (yields[k]["n"] + 1))
        if best_s is None or score > best_s + 1e-12:
            best_k, best_s = k, score
    return best_k

# ASHA: how many FAILED promotion children a survivor may accrue before it is retired (no longer
# re-promoted). One transient crash shouldn't abandon a good lineage, but a lineage that crashes
# deterministically must not be re-promoted forever, starving its siblings and burning the node budget.
_ASHA_MAX_FAILED_PROMOTIONS = 2


class SearchPolicy(Protocol):
    def next_actions(self, state: RunState) -> list[dict]: ...


def _metric_scores(nodes) -> dict[int, float]:
    """Candidate comparison surfaced as a `policy_decision` event ("why this node"): map each
    node's id to its observed metric. Lets the UI show the alternatives the policy weighed
    against the one it chose — even for policies (GreedyTree) that pick by raw metric."""
    return {n.id: round(n.metric, 4) for n in nodes if n.metric is not None}


def rank_by_metric(state: RunState, nodes) -> list:
    """Rank `nodes` best-first by observed metric with an id tie-break — descending when
    the run maximizes, ascending when it minimizes. The one ranking idiom every policy
    (and the diversity archive) shares; nodes must carry a non-None `metric` (the
    feasible/evaluated pools policies rank always do). Delegates to the SearchFitness owner
    (core/fitness.py) so search-side ranking has ONE spelling shared with the fold's promotion
    pick (R1/SearchFitness)."""
    from looplab.core.fitness import SearchFitness
    return SearchFitness(state.direction).rank(nodes)


# --------------------------------------------------------------------------- #
# THE DEBUG NODE IS GONE (F5, decided 2026-08-13). `debug_action` and `_debug_lineage`
# lived here and every policy called them: "the inline-repair limit was exceeded, so
# open a NEW node and start fixing again". The operator's ruling — *"дебаг ноду нафиг
# убираем. У нас репейринг есть. Им вот и должно всё решаться."* — is that a failure is
# fixed INSIDE the one node, for as long as it takes.
#
# What made that safe to remove is F8 landing in the same change: the inline-repair
# bound stopped being a count. Deleting the Debug node while the bound was still
# `attempt < 12` would have turned "give up and open a new node" into plain "give up",
# which is strictly worse than what it replaced. The stop is now a judgment — the triage
# judge, the Developer's own `(developer stuck: …)` and the repair critic
# (`engine/repair_judgment.py`) — over floors that are about time and money.
#
# AND THE HALF THAT MATTERS MORE, because it is how the removal would otherwise be
# evaded: no `draft`/`improve` node may be minted that is a Debug node under another
# name. `improve` already cannot reach a failed node — `breedable_nodes()` is
# evaluated-and-feasible only, and every policy below ranks that set — and
# `tests/test_debug_node_removed.py` drives that rather than trusting the reading.
# `KIND_DEBUG` survives as an event-log spelling (old runs contain `debug` nodes and must
# still fold and render), with no producer anywhere.
# --------------------------------------------------------------------------- #


def operator_yields(state: RunState) -> dict[str, dict]:
    """P4: per-operator empirical yield, folded purely from the DAG — {op: {"n": tried,
    "gain": mean positive Δmetric-over-best-parent per eval-second}}. The data for a
    deterministic UCB over operators (the cheap, principled 'adaptive operator mix' — the
    Strategist's rule table becomes priors, not hard-coded cadences). Draft nodes have no
    parent, so 'draft' yield is not defined here (drafts are the exploration baseline)."""
    out: dict[str, dict] = {}
    # CREDIT ONLY BREEDABLE NODES — the same pool `breedable_nodes()` defines, not `state.nodes` raw.
    # A tombstoned node is §6.3 logically deleted and `evaluated_nodes()` gates it out of every other
    # selection path; an aborted one never finished on its own terms; and a `breed_excluded` node is
    # one the trust gate hard-flagged as cheating/leaking, whose whole point (§2.2) is that "the
    # search never sinks budget improving a cheating lineage". Crediting its inflated Δmetric to its
    # OPERATOR did exactly that one level up: the P4 bandit then picked that operator more often.
    # (NaN metrics are NOT a concern here: replay's `_finite_metric` nulls non-finite values at fold
    # time, so folded state never carries a NaN metric.)
    for n in state.nodes.values():
        if not n.parent_ids or n.status is not NodeStatus.evaluated or n.metric is None:
            continue
        if (n.tombstoned or not n.feasible
                or n.id in state.aborted_nodes or n.id in state.breed_excluded):
            continue
        pm = [state.nodes[p].metric for p in n.parent_ids
              if p in state.nodes and state.nodes[p].metric is not None]
        if not pm:
            continue
        # direction-aware improvement over the best parent (clamped at 0 — a regression yields
        # no positive credit), amortized per eval-second so cheap wins rank above slow ones.
        base = max(pm) if state.direction == "max" else min(pm)
        delta = (n.metric - base) if state.direction == "max" else (base - n.metric)
        gain = max(0.0, delta) / max(0.1, (n.eval_seconds or 0.1))
        d = out.setdefault(n.operator, {"n": 0, "gain": 0.0})
        d["gain"] = (d["gain"] * d["n"] + gain) / (d["n"] + 1)
        d["n"] += 1
    return out


def _bandit_pick(yields: dict[str, dict], candidates: list[str], c: float = 0.8) -> str:
    """Deterministic UCB1 over operator kinds: an UNTRIED operator is optimistically tried first
    (classic UCB1 infinite priority, in candidate order); otherwise mean normalized gain + an
    exploration bonus for rarely-tried operators. Ties break by candidate order (caller lists
    its default first)."""
    for k in candidates:
        if yields.get(k, {"n": 0})["n"] == 0:
            return k                      # optimism under uncertainty: try every operator once
    total = sum(d["n"] for d in yields.values()) or 1
    gmax = max((d["gain"] for d in yields.values()), default=0.0) or 1.0
    best_k, best_s = candidates[0], None
    for k in candidates:
        d = yields[k]
        score = (d["gain"] / gmax) + c * math.sqrt(math.log(total + 1) / (d["n"] + 1))
        if best_s is None or score > best_s + 1e-12:
            best_k, best_s = k, score
    return best_k


def weighted_parent(state: RunState, feasible=None) -> Optional[int]:
    """ShinkaEvolve-shaped parent selection, derandomized for replay safety: prefer parents
    with a high fitness RANK that are still UNDER-EXPANDED — weight = 1/rank / (1 + children).
    Expanding a node lowers its weight, so selection rotates through good stepping stones
    instead of hammering the single global best (weighted parent sampling beat both
    hill-climbing and random selection in ShinkaEvolve's ablation). Deterministic: pure
    argmax with id tie-break."""
    pool = feasible if feasible is not None else state.breedable_nodes()
    if not pool:
        return None
    ranked = rank_by_metric(state, pool)
    # The under-expansion counter skips children the lifecycle has retired. Counting over RAW
    # state.nodes meant a §6.3-tombstoned, aborted or gate-flagged (breed_excluded) child still
    # lowered its honest parent's weight through `1/(1 + children)` — a logically-deleted child is
    # supposed to be invisible to selection, yet it de-prioritized its own ancestor for `improve`,
    # so deleting or flagging a child changed where the search goes. Same lifecycle gap the MCTS
    # value filter and `operator_yields` already close. A FAILED child still counts: that expansion
    # really was spent, and not counting it would re-hammer the same parent.
    kids: dict[int, int] = {}
    for n in state.nodes.values():
        if n.tombstoned or n.id in state.aborted_nodes or n.id in state.breed_excluded:
            continue
        for p in n.parent_ids:
            kids[p] = kids.get(p, 0) + 1
    best_id, best_w = None, -1.0
    for rank, n in enumerate(ranked, start=1):
        w = (1.0 / rank) / (1.0 + kids.get(n.id, 0))
        if w > best_w + 1e-12:
            best_id, best_w = n.id, w
    return best_id


class GreedyTree:
    """The default SearchPolicy (see the module docstring for the action schema and meta
    keys): seed `n_seeds` drafts, then repeatedly `improve` the best feasible node,
    periodically `merge` the top-2 (every `merge_every` improves, at most `max_merges`)
    and — when `ablate_every` > 0 — `ablate` the best to refine its highest-impact
    parameter, until `max_nodes` is spent. It no longer repairs failed leaves by opening a
    node: `debug_depth` is accepted and inert (F5 — see the block above `operator_yields`),
    kept only so Settings/snapshot/env compatibility and the calibrated speculation
    envelope, which both pin the name, are undisturbed. Pure over the folded RunState (reads state, returns actions;
    the orchestrator executes them), so it is deterministic and replay-safe;
    `operator_bandit` (P4) swaps the fixed merge/ablate cadences for a deterministic UCB
    over observed per-operator yields."""

    def __init__(
        self,
        n_seeds: int = 3,
        max_nodes: int = 8,
        debug_depth: int = 1,
        enable_merge: bool = True,
        merge_every: int = 3,
        max_merges: int = 2,
        ablate_every: int = 0,
        operator_bandit: bool = False,
        model_arms: Optional[dict] = None,
    ):
        self.n_seeds = n_seeds
        self.max_nodes = max_nodes
        self.debug_depth = debug_depth
        self.enable_merge = enable_merge
        self.merge_every = max(1, merge_every)   # 0 would ZeroDivision in `n_improve // merge_every`
        self.max_merges = max_merges
        self.ablate_every = ablate_every  # 0 = off (I7 ablation-driven refinement)
        # P4: replace the FIXED merge/ablate cadences with a deterministic UCB over operator
        # yields folded from the run itself (Δmetric per eval-second). Off by default: the
        # cadence defaults are well-tested and the bandit has no direct published ablation —
        # `thorough` turns it on.
        self.operator_bandit = operator_bandit
        # doc 52 row 19: `{arm: relative cost}` — the model arms the bandit branch may route a build
        # to beside the implicit default. Inert without `operator_bandit`.
        self.model_arms = {str(k): float(v) for k, v in (model_arms or {}).items()
                           if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}

    def next_actions(self, state: RunState) -> list[dict]:
        # 1. Evaluate anything created-but-not-evaluated (crash-resume re-entry point).
        pending = state.pending_nodes()
        if pending:
            return [{"kind": KIND_EVALUATE, "node_id": n.id} for n in pending]

        total = len(state.nodes)


        if total >= self.max_nodes:
            return []  # budget spent -> finish

        # 3. Seed phase.
        if total < self.n_seeds:
            k = min(self.n_seeds - total, self.max_nodes - total)
            return [{"kind": KIND_DRAFT} for _ in range(k)]

        best = state.best()
        if best is None:
            return [{"kind": KIND_DRAFT}]

        evaluated = state.breedable_nodes()   # never breed from constraint-violating nodes (#5)
        # D7: `expand` is an improve-VARIANT (an improve proposal under lock-in) -> count it as improve for
        # the cadence, so the capability-expansion lever doesn't undercount refinement effort. No expand
        # nodes exist unless the flag is on, so this is byte-identical on a default run.
        n_improve = sum(1 for n in state.nodes.values() if n.operator in ("improve", KIND_EXPAND))
        n_merge = sum(1 for n in state.nodes.values() if n.operator == "merge")
        n_refine = sum(1 for n in state.nodes.values() if n.operator == "refine_block")

        # P4 operator bandit: pick the next operator by observed yield (deterministic UCB over
        # Δmetric per eval-second folded from the run), instead of the fixed cadences below.
        # Only chooses among currently-LEGAL operators; falls through to the cadence logic when
        # off or when it picks the default (improve).
        if self.operator_bandit:
            cands = ["improve"]
            if self.enable_merge and len(evaluated) >= 2 and n_merge < self.max_merges:
                cands.append("merge")
            if (self.ablate_every > 0 and len(best.idea.params) >= 2
                    and getattr(self, "ablation_capable", True)):
                cands.append("refine_block")
            pick = _bandit_pick(operator_yields(state), cands)
            if pick == "merge":
                top2 = rank_by_metric(state, evaluated)[:2]
                action = {"kind": KIND_MERGE, "parent_ids": [top2[0].id, top2[1].id],
                          META_SCORES: _metric_scores(top2), META_CHOSEN: top2[0].id,
                          META_REASON: "bandit: merge top-2"}
            elif pick == "refine_block":
                action = {"kind": KIND_ABLATE, "parent_id": best.id,
                          META_SCORES: _metric_scores(evaluated), META_CHOSEN: best.id,
                          META_REASON: "bandit: ablate highest-impact param"}
            else:
                action = {"kind": KIND_IMPROVE, "parent_id": best.id,
                          META_SCORES: _metric_scores(evaluated), META_CHOSEN: best.id,
                          META_REASON: "bandit: exploit best"}
            if self.model_arms:
                # The model ARM (doc 52 row 19), chosen by the same UCB over per-arm yield with the
                # gain divided by the arm's relative cost — the default first, then each declared
                # arm once, then whichever pays most per unit of spend.
                arms = [DEFAULT_MODEL_ARM] + [a for a in self.model_arms if a != DEFAULT_MODEL_ARM]
                action[META_MODEL] = _model_arm_pick(
                    model_arm_yields(state), arms, {DEFAULT_MODEL_ARM: 1.0, **self.model_arms})
            return [action]

        # 4. Periodic merge of the top-2 evaluated nodes (multi-parent DAG step).
        # One merge per `merge_every` improves (not back-to-back): gate on the merge DEFICIT
        # vs the milestone count, since n_improve is unchanged between consecutive merges.
        if (self.enable_merge and len(evaluated) >= 2 and n_merge < self.max_merges
                and n_improve >= self.merge_every and n_merge < n_improve // self.merge_every):
            top2 = rank_by_metric(state, evaluated)[:2]
            return [{"kind": KIND_MERGE, "parent_ids": [top2[0].id, top2[1].id],
                     META_SCORES: _metric_scores(top2), META_CHOSEN: top2[0].id,
                     META_REASON: "merge top-2"}]

        # 5. Ablation-driven refinement (I7): periodically ablate the best to find the
        #    highest-impact parameter, then refine just that one. `ablation_capable` is stamped
        #    False by the engine on repo/eval-spec runs, where ablation probes cannot run (the
        #    solution.py sandbox path is wrong) — proposing it there would spin forever, since the
        #    skip creates no refine_block node and the cadence never clears (see engine/ablation.py).
        if (self.ablate_every > 0 and len(best.idea.params) >= 2
                and getattr(self, "ablation_capable", True)
                and n_improve >= (n_refine + 1) * self.ablate_every):
            return [{"kind": KIND_ABLATE, "parent_id": best.id,
                     META_SCORES: _metric_scores(evaluated), META_CHOSEN: best.id,
                     META_REASON: "ablate highest-impact param"}]

        # 6. Exploit: improve the current best (over all feasible candidates).
        return [{"kind": KIND_IMPROVE, "parent_id": best.id,
                 META_SCORES: _metric_scores(evaluated), META_CHOSEN: best.id,
                 META_REASON: "exploit best"}]


class EvolutionaryPolicy:
    """Opt-in alternative SearchPolicy (I22, ADR-2). Maintains a population; each
    generation either crossovers two elites (merge) or mutates a *rotating* elite
    (improve) — so it explores more broadly than GreedyTree's always-exploit-the-best.
    Plugs into the unchanged orchestrator (same action vocabulary), proving the
    SearchPolicy/algorithm seam.
    """

    def __init__(self, pop: int = 4, max_nodes: int = 12, elite: int = 2,
                 debug_depth: int = 1):
        self.pop = pop
        self.max_nodes = max_nodes
        self.elite = max(1, elite)  # guard against /0 in gen % len(elites)
        self.debug_depth = debug_depth

    def next_actions(self, state: RunState) -> list[dict]:
        pending = state.pending_nodes()
        if pending:
            return [{"kind": KIND_EVALUATE, "node_id": n.id} for n in pending]

        total = len(state.nodes)
        if total >= self.max_nodes:
            return []

        # Fill the initial population with drafts.
        if total < self.pop:
            k = min(self.pop - total, self.max_nodes - total)
            return [{"kind": KIND_DRAFT} for _ in range(k)]

        evaluated = rank_by_metric(state, state.breedable_nodes())   # elites must be feasible (#5)
        if not evaluated:
            return [{"kind": KIND_DRAFT}]
        elites = evaluated[: self.elite]
        # Offspring index = how many generation-producing operators (improve/merge) already
        # exist — NOT total node count, so inserted debug/failed nodes can't perturb the
        # crossover/mutate parity or the elite rotation (deterministic w.r.t. eval failures).
        gen = sum(1 for n in state.nodes.values() if n.operator in ("improve", "merge", KIND_EXPAND))

        # Even generations crossover two elites; odd generations mutate a WEIGHTED parent:
        # fitness-rank × under-expansion over the WHOLE feasible archive (not just elites) —
        # ShinkaEvolve's #1-ranked lever (weighted parent sampling beat both hill-climbing and
        # random), derandomized for replay safety. Good stepping stones outside the elite set
        # stay reachable; expanding a node lowers its weight, so selection rotates naturally.
        if gen % 2 == 0 and len(elites) >= 2:
            i = (gen // 2) % len(elites)
            j = (i + 1) % len(elites)
            return [{"kind": KIND_MERGE, "parent_ids": [elites[i].id, elites[j].id]}]
        pid = weighted_parent(state, evaluated)
        if pid is None:
            pid = elites[gen % len(elites)].id
        return [{"kind": KIND_IMPROVE, "parent_id": pid,
                 META_SCORES: _metric_scores(evaluated), META_CHOSEN: pid,
                 META_REASON: "weighted parent (fitness-rank × under-expansion)"}]


def _mcts_reward(value: float, direction: str) -> float:
    """Map a subtree's best metric to a BOUNDED, direction-correct UCB1 reward so the exploration
    term (c≈1.4, calibrated for a ~(0,1] reward) is never swamped by a large-magnitude metric.

    Both directions are continuous at 0 (=1.0), strictly monotone (min: decreasing; max: increasing),
    and bounded in (0, 2):
      * min (lower is better): value>=0 -> (0,1] via 1/(1+value); value<0 -> (1,2) via 2 - 1/(1-value).
        A bare `abs(value)` inverted signed metrics; a plain `1-value` made reward unbounded for
        negatives (log-likelihood ≈ -400 -> reward ≈ 400 -> pure greedy).
      * max (higher is better): the SYMMETRIC reflection — value>=0 -> [1,2) via 2 - 1/(1+value);
        value<0 -> (0,1) via 1/(1-value). A bare `reward = value` left the same UCB-degeneracy the
        min branch fixes unaddressed for unbounded max metrics (Sharpe, throughput, negative-MSE) —
        architecture-review M2. Bounded max metrics (accuracy/AUC/F1 in [0,1]) keep a sensible order.
    """
    if direction == "min":
        return (1.0 / (1.0 + value)) if value >= 0 else (2.0 - 1.0 / (1.0 - value))
    return (2.0 - 1.0 / (1.0 + value)) if value >= 0 else (1.0 / (1.0 - value))


class MCTSPolicy:
    """Opt-in UCB1 tree search (I22, ADR-2). Selects which node to expand by
    UCB1 = reward + c·sqrt(ln N / visits), balancing exploiting good subtrees against
    exploring under-visited ones — distinct from greedy (always the best) and
    evolutionary (rotating elites). Pure: visits/values are derived from the folded DAG.
    """

    def __init__(self, n_seeds: int = 3, max_nodes: int = 12, c: float = 1.4,
                 debug_depth: int = 1):
        self.n_seeds = n_seeds
        self.max_nodes = max_nodes
        self.c = max(0.0, float(c))   # >= 0: a negative c flips UCB exploration into a penalty
        self.debug_depth = debug_depth

    def next_actions(self, state: RunState) -> list[dict]:
        pending = state.pending_nodes()
        if pending:
            return [{"kind": KIND_EVALUATE, "node_id": n.id} for n in pending]
        total = len(state.nodes)
        if total >= self.max_nodes:
            return []
        if total < self.n_seeds:
            k = min(self.n_seeds - total, self.max_nodes - total)
            return [{"kind": KIND_DRAFT} for _ in range(k)]
        evaluated = state.breedable_nodes()   # improve only feasible candidates (#5)
        if not evaluated:
            return [{"kind": KIND_DRAFT}]

        children: dict[int, list[int]] = {}
        for n in state.nodes.values():
            for p in n.parent_ids:
                children.setdefault(p, []).append(n.id)

        def subtree(nid: int) -> set[int]:
            seen: set[int] = set()
            stack = [nid]
            while stack:
                x = stack.pop()
                if x in seen:
                    continue
                seen.add(x)
                stack.extend(children.get(x, []))
            return seen

        n_total = len(evaluated)
        best_of = min if state.direction == "min" else max
        chosen, best_ucb = None, None
        scores: dict[int, float] = {}   # per-candidate UCB1 (surfaced as a `policy_decision` event)
        for node in sorted(evaluated, key=lambda n: n.id):
            tree = subtree(node.id)
            # Value the subtree by its feasible descendants' metrics, EXCLUDING gate-flagged (cheating)
            # nodes: their inflated metric must not make an ancestor's subtree look good and pull MCTS
            # exploration toward a cheating lineage (§2.2, matching breedable_nodes for direct targets).
            # Tombstoned/aborted descendants are excluded too: a §6.3 logically-deleted descendant
            # is invisible to the candidate pool, so letting its metric keep steering UCB toward its
            # ancestor's subtree meant deleting a node changed what the operator could pick but not
            # where the search wanted to go. (NaN metrics are not reachable: replay's
            # `_finite_metric` nulls non-finite values at fold time, so the `metric is not None`
            # check already excludes them.)
            metrics = [state.nodes[i].metric for i in tree
                       if state.nodes[i].metric is not None and state.nodes[i].feasible
                       and not state.nodes[i].tombstoned and i not in state.aborted_nodes
                       and i not in state.breed_excluded]  # #5
            if not metrics:
                continue
            value = best_of(metrics)
            # Bounded, direction-correct reward so a large-magnitude metric can't swamp the c≈1.4
            # exploration term and collapse UCB1 to greedy (see _mcts_reward for the full rationale;
            # the max branch was unbounded `reward = value` before architecture-review M2).
            reward = _mcts_reward(value, state.direction)
            # Visits = real (feasible, evaluated) trials in the subtree, not failed/infeasible
            # nodes, so the UCB exploration term reflects actual exploration (#76). It shares the
            # value filter's lifecycle gates: a tombstoned / aborted / gate-flagged descendant is
            # still `status is evaluated` and `feasible` (§6.3 delete and the gate posture both keep
            # those flags), so filtering only on status/feasible inflated the exploration
            # DENOMINATOR with descendants that contribute nothing to `value` — and by this
            # policy's own §6.3 argument above, deleting or flagging a node would then change UCB1
            # for its ancestor, i.e. change where the search goes.
            visits = sum(1 for i in tree if state.nodes[i].status is NodeStatus.evaluated
                         and state.nodes[i].feasible and not state.nodes[i].tombstoned
                         and i not in state.aborted_nodes
                         and i not in state.breed_excluded) or 1
            ucb = reward + self.c * math.sqrt(math.log(n_total + 1) / visits)
            scores[node.id] = round(ucb, 4)
            if best_ucb is None or ucb > best_ucb:
                best_ucb, chosen = ucb, node.id
        if chosen is None:
            b = state.best()
            chosen = b.id if b is not None else sorted(evaluated, key=lambda n: n.id)[0].id
        return [{"kind": KIND_IMPROVE, "parent_id": chosen, META_SCORES: scores, META_CHOSEN: chosen}]


def asha_expansion(state) -> tuple[set[int], set[int]]:
    """ASHA's view of which survivors are already expanded and which are retired.

    Returns ``(has_live_child, retired)``:

    * ``has_live_child`` — "expanded" means a child that is still LIVE (evaluated or pending). A
      FAILED child leaves the survivor re-promotable so ONE transient crash doesn't abandon a
      (possibly best) lineage.
    * ``retired`` — but CAP the retries: after `_ASHA_MAX_FAILED_PROMOTIONS` failed children and no
      live child the survivor is retired, else a lineage that crashes deterministically would be
      re-promoted every iteration (chosen = lowest-id unexpanded survivor), starving its siblings and
      burning the whole node budget on it.

    `ASHAPolicy.next_actions` is the authority for ASHA promotion and `card_selection._asha_lane` must
    agree with it, or the Card lane silently promotes a lineage the policy has retired — a divergence
    with no runtime guard, because the 15-case scorer-fidelity matrix covers only GreedyTree (doc 25
    SE-03). Both now read this one function; the constant was already shared, the algorithm was not.
    """
    has_live_child: set[int] = set()
    failed_children: dict[int, int] = {}
    for node in state.nodes.values():
        if node.status is NodeStatus.failed:
            for parent_id in node.parent_ids:
                failed_children[parent_id] = failed_children.get(parent_id, 0) + 1
        else:
            has_live_child.update(node.parent_ids)
    retired = {parent_id for parent_id, count in failed_children.items()
               if count >= _ASHA_MAX_FAILED_PROMOTIONS and parent_id not in has_live_child}
    return has_live_child, retired


class ASHAPolicy:
    """A1 · Asynchronous Successive Halving (ASHA / Hyperband, ADR-2). Allocates compute by
    *racing*: seed a wide rung-0 of cheap drafts, then promote only the top 1/eta survivors to the
    next rung (an `improve` that gets more attention), recursively — instead of full-expanding
    every lineage. Adapted to LoopLab's tree substrate: a "rung" is a generation (draft=rung 0,
    improve-of-survivor=rung 1, …); promotion = spending the next node on a survivor's lineage. The
    fidelity (smoke at low rungs, full near the top) is driven by the Strategist/eval-profile seam.

    Pure: rungs/survivors are derived from the folded DAG, so it's deterministic and replay-safe.
    Emits `_rung`/`_promoted` meta on its action so the engine can log a `rung_promoted` event."""

    def __init__(self, n_seeds: int = 4, max_nodes: int = 12, eta: int = 3, debug_depth: int = 1,
                 rung_nodes: int = 0):
        self.n_seeds = max(2, n_seeds)
        # rung-0 width: an explicit rung_nodes (>0) overrides n_seeds, else default to n_seeds.
        self.rung0 = max(2, rung_nodes) if rung_nodes else self.n_seeds
        self.max_nodes = max_nodes
        self.eta = max(2, eta)                 # keep top 1/eta per rung
        self.debug_depth = debug_depth

    def _generation(self, state: RunState) -> dict[int, int]:
        """Generation (rung) of each node: a draft is rung 0; an improve/merge child is parent+1.
        Computed by a monotone pass over ids (parents always precede children)."""
        gen: dict[int, int] = {}
        for n in sorted(state.nodes.values(), key=lambda n: n.id):
            if not n.parent_ids:
                gen[n.id] = 0
            else:
                gen[n.id] = 1 + max((gen.get(p, 0) for p in n.parent_ids), default=0)
        return gen

    def next_actions(self, state: RunState) -> list[dict]:
        pending = state.pending_nodes()
        if pending:
            return [{"kind": KIND_EVALUATE, "node_id": n.id} for n in pending]
        total = len(state.nodes)
        if total >= self.max_nodes:
            return []

        # Rung 0: fill to rung0 cheap drafts (the wide base of the bracket).
        drafts = [n for n in state.nodes.values() if not n.parent_ids]
        if len(drafts) < self.rung0:
            k = min(self.rung0 - len(drafts), self.max_nodes - total)
            return [{"kind": KIND_DRAFT} for _ in range(k)]

        gen = self._generation(state)
        feasible = {n.id for n in state.breedable_nodes()}
        if not feasible:
            return [{"kind": KIND_DRAFT}]
        has_child, retired = asha_expansion(state)

        # Promote from the LOWEST rung that still has an unexpanded survivor (asynchronous: don't
        # wait for a whole rung to finish before promoting from a lower one).
        by_rung: dict[int, list[int]] = {}
        for nid in feasible:
            by_rung.setdefault(gen.get(nid, 0), []).append(nid)
        for r in sorted(by_rung):
            members = by_rung[r]
            # successive-halving survivor count: keep the top ⌈n/η⌉ (round UP so a rung wider than η
            # always promotes ≥2 — `floor` would collapse e.g. n=4,η=3 to 1 survivor and never halve).
            keep = max(1, math.ceil(len(members) / self.eta))
            survivors = sorted(
                members, key=lambda i: (state.nodes[i].metric, i),
                reverse=(state.direction == "max"))[:keep]
            unexpanded = [i for i in survivors if i not in has_child and i not in retired]
            if len(members) <= 1:
                continue            # a 1-member rung genuinely has nothing to halve. A 2-member rung
                # keeping 1 survivor (ceil(2/η)=1) IS a real halving decision, so it still promotes.
            if unexpanded:
                chosen = sorted(unexpanded)[0]
                scores = {i: round(state.nodes[i].metric, 4) for i in members
                          if state.nodes[i].metric is not None}
                # SCOPE, stated plainly: this allocates ATTEMPTS, not compute. The rung travels as
                # `_rung` only to stamp the `rung_promoted` audit receipt (orchestrator) and fold into
                # `RunState.rungs`; NOTHING reads it back to raise a node's eval fidelity or resources.
                # Promotion also spends the survivor on a MUTATED child (KIND_IMPROVE), where textbook
                # successive halving re-runs the SAME configuration with a larger budget. So this is a
                # halving SELECTION schedule — real, and paired with `asha_live`'s early stop — but not
                # multi-fidelity racing. Making it so needs an immutable-survivor re-run bound to an
                # explicit higher eval profile: that is BACKLOG A1 ("multi-fidelity racing
                # ASHA/Hyperband ... over existing `eval_profile` smoke/full"), still open, and it would
                # CHANGE what gets evaluated — a feature, not a repair. The operator-facing help text
                # (`serve/settings_ui_schema.json`) states this limit rather than promising fidelity
                # scaling the code does not do.
                return [{"kind": KIND_IMPROVE, "parent_id": chosen,
                         META_SCORES: scores, META_CHOSEN: chosen,
                         META_REASON: f"promote rung {r + 1}",
                         META_RUNG: r + 1, META_PROMOTED: survivors}]

        # All rungs collapsed/expanded -> exploit the global best with remaining budget.
        b = state.best()
        if b is None:
            return [{"kind": KIND_DRAFT}]
        return [{"kind": KIND_IMPROVE, "parent_id": b.id,
                 META_SCORES: _metric_scores(state.breedable_nodes()), META_CHOSEN: b.id,
                 META_REASON: "exploit best (rungs collapsed)"}]


def legal_actions(state: RunState, policy: SearchPolicy, *, max_nodes: int) -> list[dict]:
    """Pure legal-action gate for the self-driving unified agent (replaces the policy as the
    *master* of action selection without surrendering pipeline discipline). Returns the set of
    macro actions the agent may choose from given the folded state, derived from the SAME
    invariants every `next_actions` enforces — so whatever the agent picks, the pipeline stays
    correct. Forced phases return a single non-negotiable set (the agent has no discretion):

      * pending nodes        -> only `evaluate` (crash-resume re-entry invariant)
      * node budget spent    -> `[]` (finish)
      * seed phase           -> only `draft`

    Otherwise the explore/exploit envelope is built from REAL nodes (draft / improve any feasible /
    debug a failed leaf within depth / merge the top-2 / ablate the best), so a chosen parent can
    never be illegal. Deterministic and side-effect-free — safe to call on every loop turn."""
    pending = state.pending_nodes()
    if pending:
        return [{"kind": KIND_EVALUATE, "node_id": n.id} for n in pending]
    total = len(state.nodes)
    if total >= max_nodes:
        return []
    n_seeds = getattr(policy, "rung0", None) or getattr(policy, "n_seeds", getattr(policy, "pop", 3))
    if total < n_seeds:
        return [{"kind": KIND_DRAFT}]
    actions: list[dict] = [{"kind": KIND_DRAFT}]
    feasible = rank_by_metric(state, state.breedable_nodes())
    actions.extend({"kind": KIND_IMPROVE, "parent_id": n.id} for n in feasible)
    # NO DEBUG ACTION HERE ANY MORE (F5). This gate is the envelope the self-driving agent picks
    # from, so leaving `debug` in it would let the agent mint the very node the policies no longer
    # can — the "Debug node under another name" the decision names as the way the removal gets
    # evaded. A failed node is fixed in place; `feasible` above is `breedable_nodes()`, which is
    # evaluated-and-feasible only, so no `improve` here can anchor on one either.
    if len(feasible) >= 2:
        actions.append({"kind": KIND_MERGE, "parent_ids": [feasible[0].id, feasible[1].id]})
    best = state.best()
    if (best is not None and len(best.idea.params) >= 2
            and getattr(policy, "ablation_capable", True)):
        actions.append({"kind": KIND_ABLATE, "parent_id": best.id})
    return actions


# Per-policy factories for the registry below. Uniform signature: the explicit make_policy
# kwargs plus the resolved `depth` and the raw `params` overrides.
def _make_greedy(*, n_seeds: int, max_nodes: int, ablate_every: int, depth: int,
                 params: dict) -> SearchPolicy:
    return GreedyTree(n_seeds=n_seeds, max_nodes=max_nodes, ablate_every=ablate_every,
                      debug_depth=depth,
                      operator_bandit=bool(params.get("operator_bandit", False)),
                      model_arms=params.get("model_arms") or None)


def _make_evolutionary(*, n_seeds: int, max_nodes: int, ablate_every: int, depth: int,
                       params: dict) -> SearchPolicy:
    return EvolutionaryPolicy(pop=n_seeds, max_nodes=max_nodes, debug_depth=depth)


def _make_mcts(*, n_seeds: int, max_nodes: int, ablate_every: int, depth: int,
               params: dict) -> SearchPolicy:
    # Clamp the exploration constant >= 0: a Strategist-supplied negative `c` (validate_strategy
    # accepts any scalar) would flip the UCB exploration term into a PENALTY on under-visited
    # subtrees — a silently degenerate hyper-greedy policy recorded as a legitimate strategy.
    c = max(0.0, float(params.get("c", 1.4)))
    return MCTSPolicy(n_seeds=n_seeds, max_nodes=max_nodes, c=c, debug_depth=depth)


def _make_asha(*, n_seeds: int, max_nodes: int, ablate_every: int, depth: int,
               params: dict) -> SearchPolicy:
    eta = int(params.get("eta", 3))
    return ASHAPolicy(n_seeds=n_seeds, max_nodes=max_nodes, eta=eta, debug_depth=depth,
                      rung_nodes=int(params.get("rung_nodes", 0) or 0))


# Policy registry (ADR-2). The Strategist (A7) may only pick from these names; new policies
# auto-register here and become selectable without engine changes. Insertion order is the
# order `available_policies()` reports.
_REGISTRY: dict[str, Callable[..., SearchPolicy]] = {
    "greedy": _make_greedy,
    "evolutionary": _make_evolutionary,
    "mcts": _make_mcts,
    "asha": _make_asha,
    # A3 BOHB = Hyperband racing (ASHA) × surrogate-guided proposal (A2). The racing schedule is
    # the ASHA policy; the surrogate is wired as the Researcher (cli enables it for `bohb`), so
    # the policy object is the same — the fusion is the racing schedule + a surrogate proposer.
    # Hence "bohb" is an alias for the ASHA factory (kept exactly for compatibility).
    "bohb": _make_asha,
}


# WHICH POLICIES CANNOT KEEP A WIDE RUN BUSY, and it is a property of the SCHEDULE rather than of
# any one board. An ASHA-family policy races arms in rungs: once the seed target is met it can only
# PROMOTE, and a promotion needs the rung's survivors, so while a single rung-0 arm is unresolved it
# can neither seed nor promote and answers nothing. A run with two eval slots therefore executes one
# node at a time until that arm finishes.
#
# MEASURED, and the measurement is already in the tree — `search/card_selection.py::
# _asha_mask_is_unsound` records 8.03 starved hours across five runs, 5.94 of them this shape, and
# **0.00 in every GreedyTree and EvolutionaryPolicy run**. The lanes always answered there.
#
# THIS NAMES A COST, NEVER A REFUSAL. A racing schedule on a wide box can be exactly the right
# choice — `runs/e5small-dr-unified-v4`'s Strategist picked it with the sound argument that
# five-hour evaluations make a numeric sweep cheaper than many separate ones. What went unsaid is
# that the same decision ALSO set `eval_parallel: 2`, and nothing anywhere connected the two fields.
# The operator's own instruction after the fact: the engine may not silently half-use its box.
SERIALISING_POLICIES: frozenset[str] = frozenset({"asha", "bohb"})


def policy_fills_width(policy_name, width) -> bool:
    """Can `policy_name` keep `width` evaluation slots busy in steady state? Pure; total.

    False ONLY for a racing schedule asked to fill more than one slot. Unknown names answer True —
    a policy this table has not heard of is not accused of starving anything, the same fail-quiet
    rule `per_experiment_gpu_budget` follows for an unprobed pool.
    """
    try:
        w = int(width)
    except (TypeError, ValueError, OverflowError):
        return True
    if w <= 1:
        return True
    return str(policy_name or "").strip().lower() not in SERIALISING_POLICIES


def available_policies() -> list[str]:
    return list(_REGISTRY)


def make_policy(name: str = "greedy", *, n_seeds: int, max_nodes: int,
                ablate_every: int = 0, **params) -> SearchPolicy:
    """Select a search policy by name (ADR-2 pluggable algorithm). `params` carries
    policy-specific overrides the Strategist may pass (e.g. mcts `c`, asha `eta`) plus the
    run-wide `debug_depth` / `operator_bandit` knobs (Settings)."""
    depth = int(params.get("debug_depth", 1) or 1)
    factory = _REGISTRY.get(name)
    if factory is None:
        # `Settings.policy` is a free-form string (the registry, not pydantic, is the vocabulary),
        # so a typo reaches here from `-s policy=…` — an operator input error, not a bug. Name the
        # accepted values: without them the refusal states the problem and not the fix.
        raise ConfigRefusal(f"unknown policy: {name!r}; choose one of: "
                            + ", ".join(available_policies()))
    return factory(n_seeds=n_seeds, max_nodes=max_nodes, ablate_every=ablate_every,
                   depth=depth, params=params)
