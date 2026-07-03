# LoopLab — Architecture Review (2026-06-22)

**Scope:** full audit of the `LoopLab/` implementation (~22 modules, 76 tests) against the design (ADR-1…18 in [03](03-decisions.md)/[05](05-build-decisions.md)). Method: two parallel adversarial code-review passes (concurrency/replay/policy cluster; trust/LLM cluster) + manual design-consistency check. Companion: [README.md](../README.md), status board [06](06-implementation-plan.md).

**Verdict:** the implementation is **consistent with the architecture**; the moat (files-as-truth event loop, pluggable roles/policies/sandbox, trust layer) is intact. Seven real bugs were found and **fixed**; documented deviations are deliberate. No design change required — only the I10 gate *semantics* were clarified (below).

---

## 1. Design ↔ code consistency (ADR alignment)

| ADR | Claim | Status in code |
|---|---|---|
| ADR-1 | files-as-truth, engine = sole writer, UI reads | ✅ `events.jsonl` only written by the engine, all appends under `_write_lock`, creates sequential; `spans.jsonl` (diagnostics) and `readmodel.sqlite` (derived) are separate sinks |
| ADR-7 | pluggable role backends | ✅ `Researcher`/`Developer` Protocols; toy / LLM / tool-using swap with **zero** orchestrator change (3 task kinds, 3 policies prove it) |
| ADR-12 | anyio + resume-by-replay | ✅ `fold()` is pure and **order-independent** (best by `(value,id)`); resume = replay; verified across event permutations |
| ADR-13 | trust-mode sandbox tiers | ✅ `SubprocessSandbox` default, `DockerSandbox` seam, `make_sandbox(trust_mode)` |
| ADR-14 | tool-call default + BAML/text fallback | ✅ + `<think>`-aware + resilient (retry → safe default, never crashes a run) |
| ADR-15 | leakage-first trust layer | ✅ leakage gate, consistent CV, variance gate, multi-seed confirmation (gate semantics clarified, §3) |
| ADR-16 | agentic retrieval, pluggable vector store | ✅ `ToolUsingResearcher` + `KnowledgeTools`; `VectorStore` Protocol + in-memory default |
| ADR-18 | our loop, no agent framework | ✅ hand-rolled `SearchPolicy` + orchestrator over the folded DAG |

---

## 2. Deviations from the plan (intentional, documented)

1. **I4 git/patch path not built.** Solutions are whole-file scripts in per-node workdirs; lineage is `parent_ids` in the event log, not git commits. Fine until an external **diff-emitting** coding agent is wired — at which point the ADR-14 unidiff allow-list gate must be built before trusting its edits.
2. **Flat module layout** vs the planned subpackage tree (documented in `LoopLab/__init__.py`).
3. **Leakage gate is inert for the current tasks** — it activates only when a task exposes `leakage_inputs()` (toy/regression have no train/test split). The wiring is real; the trigger awaits a split-bearing task.
4. **Wall-clock budget is per-invocation**, not cumulative across resumes (documented in code).

---

## 3. Bugs found & fixed in this review

| # | Sev | Bug | Fix |
|---|---|---|---|
| 1 | HIGH | Confirmation phase **looped forever** if every confirm-seed run failed (no `best_confirmed` emitted, `next_actions` stays empty) | `_confirm_phase` now **always** emits a `best_confirmed` completion marker (falls back to the single-eval leader) |
| 2 | HIGH | **Resume after a partial confirm** skipped the phase and silently produced a different best (gate decision lost) | completion gated on a `confirmed_done` flag (set by the `best_confirmed` event); resume **reuses** already-confirmed nodes idempotently |
| 3 | HIGH | `confirm_top_k([])` crashed (`min([])`) | early-return guard on empty input |
| 4 | MED | `cv_summary` used **population** std → SE too small → variance gate too lenient | **sample** std (Bessel, `n−1`) |
| 5 | MED | `one_se_better` used a single estimate's SE, not the **SE of the difference** of two noisy means | pooled `sqrt(SE_a²+SE_b²)`; optional incumbent std/n |
| 6 | MED | `LiteLLMClient.complete_tool` raised `TypeError` on null `tool_calls`, **bypassing** the parse fallback | guard → `KeyError` (mirrors `OpenAICompatibleClient`) so fallback triggers |
| 7 | LOW | `_extract_json` grabbed a trailing brace span; `extract_code` preferred any first fence | decode the **first complete** JSON object; prefer the **python-tagged** fence |

Plus latent guards: `EvolutionaryPolicy` `elite=0` ÷0, `MCTSPolicy` `state.best()` None-deref. Each fix has a regression test (`test_partials_wired.py`, `test_parse_llm.py`).

**I10 semantics clarified (not a bug, a design correction):** my earlier "keep the single-eval leader unless a challenger is >1 SE better" made confirmation *selection* conservative — but a seed-lucky leader has high variance, so its mean is uncertain and the difference is usually within 1 SE, which **defeats the demote-lucky-leader goal**. Corrected to: **selection = the robust-mean winner** (which correctly demotes lucky leaders), and the variance gate now **records whether that demotion is statistically significant** (`significant` on the `best_confirmed` event) rather than vetoing it.

---

## 4. Residual risks / recommendations (no action taken yet)

1. ~~**Self-repair is GreedyTree-only.**~~ ✅ **RESOLVED (post-review):** lifted to a shared `policy.debug_action` helper used by all three policies; regression-tested under Evolutionary + MCTS (`test_operators_policy.py`).
2. **Cost-budget abort is inert for local models** (0-cost). Real only for paid API backends; the wall-clock budget covers local runs.
3. **Read-during-append** relies on torn-line tolerance, so `read_all` can transiently miss the last record mid-write. The engine never depends on this for correctness (a node is always created before it is evaluated), but any future reader that needs the absolute latest event should read under the write lock.
4. **No surface gate on file writes.** Current tasks write a single `solution.py`; once an agent can edit arbitrary files, add the ADR-14 path allow-list before applying its changes.

---

## 5. Plan/status updates from this review

- [06](06-implementation-plan.md): I10 description updated to the corrected semantics; bug-fix pass recorded; recommendation #1 (policy-agnostic debug) added to the next-funcs list.
- Test count: 76 (73 offline + 3 live).
