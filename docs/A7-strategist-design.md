# A7 · Strategist role — implemented design record

**Status:** implemented / historical design record · **Date:** 2026-06-24 · **Roadmap:** [ROADMAP.md](ROADMAP.md) A7,
[BACKLOG.md](BACKLOG.md) Theme A · **Decision:** config-first, strategist-optional (default OFF).

> The Strategist is an **optional meta-controller** that, at a bounded cadence, reads the folded run
> state and decides *which search machinery to use next* — search policy/allocator, Developer backend
> (agentless vs agentic vs in-house LLM), operator mix, and fidelity. It never selects a node itself
> and does not select a winner itself. It writes a folded `strategy_decision` control/config event that
> changes live behavior and is reapplied on resume without re-calling the LLM; the event is not
> audit-only even though folding it has no external side effect. **Everything it can decide is also a direct config knob** — the Strategist
> is a convenience layer over the same settings, fully hand-overridable, and `off` ⇒ today's behavior.

---

## 1. Why this shape (fit with LoopLab)

- **Reuses the role-swap seam.** `Engine` already takes an injected `policy: SearchPolicy`
  ([orchestrator.py:113](../looplab/engine/orchestrator.py)) and calls `self.policy.next_actions(state)` in
  one place ([orchestrator.py:381](../looplab/engine/orchestrator.py)). Policies are **pure functions of
  `RunState` sharing one action vocabulary** (`draft/improve/debug/merge/ablate/evaluate`), so they
  are hot-swappable between loop iterations with zero state migration.
- **Reuses the event-sourced control plane.** It mirrors the existing `policy_decision` "why-this-node"
  event ([orchestrator.py:415](../looplab/engine/orchestrator.py)) — the Strategist gets a `strategy_decision`
  event and a "why this strategy" panel, exactly parallel.
- **Replay-safe by construction.** Like an LLM `Idea` (recorded in `node_created`, never re-called on
  replay), the Strategist's decision is **recorded in the log** and reconstructed by `fold`; the LLM is
  never re-invoked during replay. Determinism of the loop is preserved.
- **Makes the §0.5① research finding actionable.** "Operators > search algorithm" means *don't hardcode
  MCTS-everywhere* — let an informed controller pick per-situation, but keep it overridable.

---

## 2. Core types

New module **`looplab/agents/strategist.py`** (keeps `roles.py`/`policy.py` focused; depends only on
`models`, `config`, `llm`, `parse`).

```python
# A fully-serializable description of the active search machinery. Every field maps to an
# existing config knob, so a Strategy is just "a settings delta the engine applies live".
Strategy = TypedDict("Strategy", {
    "policy":        str,          # "greedy"|"evolutionary"|"mcts"|"asha"|"bo"|"bohb" (whatever make_policy knows)
    "policy_params": dict,         # {"c":1.4} | {"eta":3,"rungs":[...]} | {"n_seeds":4} ...
    "developer":     str,          # "llm"|"agentless"|"opencode"|... (whatever make_roles knows)
    "operators":     dict,         # {"enable_merge":bool,"ablate_every":int,"feature_eng":bool,...}
    "fidelity":      str,          # "smoke"|"full"|"adaptive"
    "rationale":     str,          # human-readable "why" (panel)
    "source":        str,          # "rule"|"llm"|"operator"|"config"  (provenance, audit)
}, total=False)

class StrategyContext(BaseModel):           # read-only inputs handed to the Strategist
    node_count: int
    phase: str                              # "seed"|"explore"|"exploit"|"confirm"
    eval_budget_remaining: float | None     # max_eval_seconds - total_eval_seconds (None = unbounded)
    wall_remaining: float | None
    available_policies:  list[str]          # make_policy registry — the Strategist may only pick from here
    available_developers: list[str]
    defaults: dict                          # the static config Strategy (the fallback / starting point)

class Strategist(Protocol):
    def decide(self, state: RunState, ctx: StrategyContext) -> Optional[Strategy]:
        """Return a NEW strategy to switch to, or None to keep the current one.
        MUST be deterministic for the `rule` backend; the `llm` backend may be non-deterministic
        because its output is recorded in the log (replay reads the record, never re-calls)."""
```

`make_strategist(settings) -> Optional[Strategist]`:
- `strategist_backend == "off"` → `None` (engine uses the static config policy — **default, == today**).
- `"rule"` → `RuleStrategist(settings)` — deterministic, zero-dep (also the LLM-path fallback).
- `"llm"` → `LLMStrategist(client, settings)` — structured output via the existing `llm`/`parse` stack.

---

## 3. Event + fold (replay-safe)

**New event `strategy_decision`** (audit-only, like `policy_decision` — never changes node selection):
```jsonc
{"type":"strategy_decision","data":{
   "strategy": { /* the Strategy dict above */ },
   "at_node": 7,            // node_count when decided
   "ctx": {"phase":"explore","eval_budget_remaining": 412.0}  // snapshot for the panel
}}
```

**`models.RunState`** — add two fields (audit-only; never read by best-selection):
```python
active_strategy: Optional[dict] = None          # the latest applied Strategy
strategy_history: list[dict] = Field(default_factory=list)   # [{strategy, at_node}, ...] for the panel
```

**`replay.fold`** — one new branch (mirrors how `policy_decision` is folded):
```python
elif e.type == "strategy_decision":
    st.active_strategy = e.data["strategy"]
    st.strategy_history.append({"strategy": e.data["strategy"], "at_node": e.data.get("at_node")})
```
On resume, `fold(read_all())` rebuilds `active_strategy` → the engine re-applies it once at startup
(§4), so a crash-resumed run continues with the last-decided strategy **without re-calling the LLM**.

---

## 4. Engine integration (orchestrator)

**Constructor:** add `strategist: Optional[Strategist] = None`, `strategist_every: int = 3`.

**Apply helper** (rebuilds the live machinery from a Strategy; pure wiring, no events):
```python
def _apply_strategy(self, strat: dict) -> None:
    self.policy = make_policy(strat["policy"], n_seeds=self.n_seeds, max_nodes=self.max_nodes,
                              ablate_every=strat.get("operators",{}).get("ablate_every",0),
                              **strat.get("policy_params", {}))
    dev = strat.get("developer")
    if dev and dev != self._developer_name:        # swap Developer backend if changed
        self.developer = make_developer(self.task, self.settings, backend=dev)
        self._developer_name = dev
    self._fidelity = strat.get("fidelity", "adaptive")   # consumed by _run_eval profile choice
```
*Safety:* policies share the action vocabulary and are pure → swapping between iterations is safe.
Developer is swapped only **between** sequential `_create_node` calls (creation is awaited
sequentially, [orchestrator.py:417](../looplab/engine/orchestrator.py)), so no in-flight node sees two
Developers.

**Resume:** right after the initial fold in `run()`, `if state.active_strategy: self._apply_strategy(state.active_strategy)`.

**Consult point** — at the top of the loop, *before* `self.policy.next_actions(state)`
([orchestrator.py:381](../looplab/engine/orchestrator.py)), guarded so it can't thrash:
```python
if self.strategist and self._should_consult(state):
    ctx = self._strategy_ctx(state)
    strat = self.strategist.decide(state, ctx)          # live call (rule: pure; llm: recorded)
    strat = validate_strategy(strat, ctx)               # whitelist policies/devs/operators; None if invalid/unchanged
    if strat and strat != state.active_strategy:
        self.store.append("strategy_decision",
                          {"strategy": strat, "at_node": len(state.nodes),
                           "ctx": ctx.model_dump(include={"phase","eval_budget_remaining"})})
        self._apply_strategy(strat)
        state = fold(self.store.read_all())             # re-fold so active_strategy is current
```

**`_should_consult(state)`** — bounded cadence (deterministic), consult on any of:
- every `strategist_every` created nodes (`len(nodes) % strategist_every == 0`), **and**
- phase transitions (seed→explore→exploit→confirm), **and**
- eval-budget thresholds crossed (50% / 80% of `max_eval_seconds`).
Never more than once per loop iteration; never during the seed phase's first node.

**Operator override (HITL parity):** add a `set_strategy` CONTROL event (UI/human) folded with
`source:"operator"`; `_should_consult` yields to a pending operator strategy first → the human always
wins over the Strategist, just like `pause`/`hint` today.

---

## 5. The `rule` baseline (ship first — zero-dep, deterministic)

`RuleStrategist.decide` reads only folded state, so it's pure and needs no recording for correctness
(we record anyway for audit + parity with the LLM path). Concrete heuristics (all overridable/tunable):

| Situation (derived from `RunState`) | Decision |
|---|---|
| `node_count < n_seeds` | `policy=greedy`, `fidelity=smoke`, broad drafts (cheap breadth first) |
| ≥ `n_seeds` feasible **and** best improved in last `M` nodes | keep current (`return None`) |
| **Stall**: best metric unchanged over last `M` improves | `greedy→mcts` (explore) *if mcts available*, else bump `ablate_every` (probe operators — the §0.5① lever) |
| **High failure rate** (`failed/total > 0.4`) | `developer→agentless` + deeper repair; narrow breadth |
| **Numeric param space** + ≥ `N` evals + `bo` available | `policy=bo` (surrogate refinement, A2) |
| **Eval budget < 20% remaining** | exploit-only: `policy=greedy` on best, `fidelity=full` for confirm |
| **Many cheap candidates queued** + `asha` available | `policy=asha` (race rungs, A1) |

Helper signals (all pure, deterministic from the folded DAG): `failure_rate`, `improves_since_best`
(scan nodes by id, track when `best_node_id` last changed), `is_numeric_space`
(`all(isinstance(v,(int,float)) for v in best.idea.params.values())`), `budget_frac`.

*Note:* the Strategist may only pick **policies/Developers that exist** (`ctx.available_policies`). As
A1 (`asha`), A2 (`bo`), A3 (`bohb`) land in `make_policy`, they auto-register and become selectable —
so A7 **composes with** the Theme-A search additions rather than blocking on them. The rule baseline is
useful from day one with just `greedy|evolutionary|mcts`.

---

## 6. The `llm` backend

`LLMStrategist.decide` builds a compact JSON prompt from `state` + `ctx` (progress, per-operator yield,
failure mix, budget, the menu of `available_policies/developers/operators`) and asks for a `Strategy`
via the existing structured-output path (`llm.complete_tool` / `parse.parse_structured`, with the same
`tool_call|baml` fallback the Researcher uses). Robustness, mirroring the Researcher:
- parse/HTTP failure → return `None` (keep current strategy) + emit `strategy_rejected` (audit). Never
  crashes the run (the §H2 schema-aligned parser makes this reliable on weak local models).
- `validate_strategy` rejects any field outside the whitelist before it's applied.
- Bounded cadence (`strategist_every`) keeps token cost low — it's consulted ~once per few nodes, not
  per action.

---

## 7. Config (`config.Settings`)

```python
strategist_backend: str = "off"      # "off"(default) | "rule" | "llm"   — config-first
strategist_every:   int = Field(default=3, ge=1)   # consult cadence (created nodes)
# existing knobs (policy, ablate_every, developer_backend, n_seeds, max_nodes, …) become the
# *defaults* the Strategist may override within; with backend="off" they're authoritative (== today).
```
Exposed in the Settings UI (`settingsSchema.js`) under "Search & policy" as a preset
(`off / rule / llm`) + cadence. No new required deps (`rule` is stdlib; `llm` reuses the LLM stack).

---

## 8. UI ("why this strategy")

- Server: `strategy_decision` already flows through the event log → expose `active_strategy` +
  `strategy_history` in the state payload (they're folded fields). Mirror the existing
  `policy_decision` surfacing.
- A small **Strategy panel** (parallel to the policy "why-this-node" panel added 2026-06-24): current
  policy/Developer/fidelity + the `rationale`, and a timeline of switches (`strategy_history`) over the
  run. A `set_strategy` control lets the operator pin/override live.

---

## 9. Determinism, safety, invariants

- **Audit-only.** `strategy_decision` never changes node selection or metrics; `fold` only updates
  `active_strategy`/`strategy_history`. Removing the Strategist (replay an old log under `off`) yields
  the same nodes — the decisions are recorded config, not hidden selection.
- **Replay never re-calls the LLM.** The applied Strategy lives in the log; resume re-applies it.
- **Whitelist + fallback.** `validate_strategy` constrains every field; invalid → keep current.
- **No thrash.** Bounded cadence + act-only-on-change + operator-override-wins.
- **`off` ⇒ no behavior change.** The default path is byte-identical to today (no consult, no event).

---

## 10. Phasing & tests

**Step 1 (M) — plumbing + rule baseline:** `strategist.py` (Protocol, `RuleStrategist`,
`make_strategist`, `validate_strategy`), `RunState` fields + `fold` branch, Engine consult/apply/resume,
config + CLI wiring. Tests:
- `fold` reconstructs `active_strategy` from `strategy_decision`; **resume re-applies** without re-call.
- A stall scenario flips `greedy→mcts` deterministically; a high-failure scenario flips
  `developer→agentless`.
- `off` produces **zero** `strategy_decision` events and an unchanged node sequence (golden test).
- `validate_strategy` rejects an unknown policy/Developer → keeps current.
- Replay determinism: a log with `strategy_decision` events folds identically twice (extends the
  existing fold-determinism test).

**Step 2 (M) — LLM backend:** `LLMStrategist` + structured prompt + the reject/fallback path; live
(Ollama-gated) test that a junk/unparseable strategy emit keeps the current strategy and doesn't crash.

**Step 3 (S) — UI:** strategy panel + `set_strategy` operator override.

---

## 11. Dependencies & composition

- **Composes with** A1/A2/A3 (each new policy auto-registers in `make_policy` → becomes selectable),
  C5 (agentless Developer becomes a `developer` option), A6 (proxy signals can feed the rule heuristics
  / LLM prompt), E4 (cross-run meta-priors can seed the Strategist's *opening* strategy), A5 (budget is
  already in `StrategyContext`).
- **Blocks nothing** — ships useful with today's `greedy|evolutionary|mcts` + `llm|agentless` and grows
  as the menu grows.
- **Touched files:** new `strategist.py`; edit `models.py` (2 fields), `replay.py` (1 branch),
  `orchestrator.py` (ctor + consult/apply/resume + `set_strategy` control), `config.py` (2 settings),
  `tasks.py`/`cli.py` (wire `make_strategist`), `server.py` (expose fields + `set_strategy`),
  `ui/src` (panel + Settings preset). Net: ~1 new module + small edits to 6 files.
