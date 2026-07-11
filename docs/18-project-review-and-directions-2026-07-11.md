# LoopLab — Project Review & Development Directions (2026-07-11)

**Companion docs:** [01-product-design.md](01-product-design.md) · [02-architecture.md](02-architecture.md) · [03-decisions.md](03-decisions.md) (ADR-6) · [16-architecture-code-review-2026-07-11.md](16-architecture-code-review-2026-07-11.md) (the tactical audit this complements) · [17-napmem-sciresearcher-exploration-kb-2026-07-11.md](17-napmem-sciresearcher-exploration-kb-2026-07-11.md) · [ROADMAP.md](ROADMAP.md) · [BACKLOG.md](BACKLOG.md)

**What this is.** A **strategic** review across three axes the maintainer asked for — **functionality**,
**code**, **architecture** — plus a worked-out set of **development directions**. It is the *altitude*
companion to [doc 16](16-architecture-code-review-2026-07-11.md), which is the *line-level* audit (66
agents, 70 verified findings, fixes already applied to this branch). This doc does **not** re-run that bug
hunt; it assesses capability completeness, structural health, and where to invest next.

**Method.** Three parallel code-grounded deep-dives (functionality/capability audit; test-coverage & code
health; a deduplicated directions synthesis over the whole `docs/` planning corpus), each anchored to
`file:line` and cross-verified against `looplab/`, then synthesized here against [doc 16](16-architecture-code-review-2026-07-11.md)
and the product goals ([doc 01 §3](01-product-design.md)).

> **Caveat.** `pytest` is not installed in this environment, so — like doc 16 — this is a **static** review
> (test code was read, not executed). Doc 16 reports the full offline suite green at **1718 passed, 23
> skipped**; that figure is quoted, not re-run here. All fixes from doc 16 are present on this branch
> (spot-verified: `policy.ablation_capable`, `_mcts_reward`).

---

## 1. Executive summary — where LoopLab stands

**LoopLab is a mature, unusually well-engineered system whose hard parts are built and whose remaining
frontier is small and concentrated.** ~39.7k LoC of engine across 12 packages, ~27k LoC of tests
(~1,600 test functions, ~1 test per 25 source LoC), an event-sourced spine whose replay/fold invariants
hold under an end-to-end crash-resume test, and a self-audit ([doc 16](16-architecture-code-review-2026-07-11.md))
that surfaced **zero critical and zero reproducible data-corruption/replay-divergence defects**. The
multi-document roadmap reads mostly as a *record of completed work*, not a wish-list: held-out-gated
promotion, comparative live-shared lessons, the SWE-reliability stack, structured-output reliability, the
trust ladder, memory hygiene, the decoupled verifier, and the entire UI layer are **shipped and verified**.

**The verdict on each axis:**

| Axis | Verdict |
|---|---|
| **Architecture** | **Sound.** Event-log-as-truth + single-writer + layered packages hold; `events/replay.py` is exemplary. Risks are *maintainability* (a god-object Engine paginated across 12 mixins; a 147-field flat config), not correctness. |
| **Code & tests** | **Healthy and rigorously tested.** Two-way registry source-scans, splice-neutrality tests, a real subprocess crash-resume test. Thinnest layer is `serve/` UI relative to size. |
| **Functionality** | **Broad and largely complete**, with two strategic soft spots: the **trust/rigor differentiator ships OFF by default**, and the **temporal-CV "moat" has no live caller**. |
| **Directions** | **The genuinely-open frontier is ~8 items**, most of them cheap refinements of already-built machinery (timing/gating, two feature flags, a dormant class to activate) — plus one L-effort external proof point. |

**The two findings the review most wants surfaced** (both defensible-by-design, both a risk):

1. **The trust layer is comprehensively built but shipped dormant.** Confirmation, reward-hack, code-leakage,
   critic, redaction, and *enforcement* (`trust_gate`) are all OFF/advisory by default; only `--profile
   thorough` turns them on and flips `trust_gate` to `gate` (`core/config.py:46-63`). This is a *deliberate*
   cheap-toy-default design (the config comment says so), but it means the default `looplab run` performs
   none of the rigor the product — and the UI's own "Trust & rigor — the point of LoopLab" panel
   (`ui/src/panels.jsx:118`) — advertises. **Reconcile the default with the positioning.**
2. **The temporal/target-leakage differentiator (ADR-6's stated edge) has no live caller.** `trust/cv.py`'s
   own docstring says `purged_walk_forward` / `consistent_cv` / the `Evaluator` Protocol are "complete and
   tested, but not yet consumed by a shipped adapter"; the `timeseries` adapter runs its own embedded
   backtest instead. **The claimed moat needs one adapter that exercises it end-to-end.**

Neither is a bug; both are *positioning/wiring* gaps between what's built and what's on the default path.

---

## 2. Current-state snapshot

| Package | Files | LoC | Role |
|---|---|---|---|
| `engine/` | 27 | 7,125 | orchestrator + 12 mixins + cross-run memory |
| `serve/` | 30 | 6,723 | FastAPI server, routers, TUI, assistant |
| `tools/` | 24 | 4,914 | agent-facing tools, memora/vectorstore, env_inspect |
| `adapters/` | 15 | 4,592 | 9 task kinds + composable front-end |
| `core/` | 19 | 4,562 | domain models, Settings, LLM client, parse/trace |
| `agents/` | 10 | 3,271 | roles, tool-loop, cli-agent, unified agent, strategist |
| `events/` | 9 | 2,483 | event store, replay/fold, projections, exporters |
| `search/` | 9 | 1,874 | policies, operators, foresight, hybrid-merge, coverage |
| `runtime/` | 8 | 1,662 | sandbox tiers, command-eval, deps |
| `cli/` | 6 | 1,171 | run/export/inspect/ui command groups |
| `trust/` | 10 | 876 | leakage, reward-hack, CV, redaction, confirm, harden |
| **Total** | **~156** | **~39.7k** | + ~27k LoC / ~1,600 tests |

Hotspots (LoC): `engine/orchestrator.py` 1486 · `adapters/repo_developer.py` 962 · `events/replay.py` 961 ·
`core/llm.py` 865 · `core/config.py` 768 · `agents/roles.py` 727.

*(Branch note: this review reflects `claude/framework-arch-design-fzxwwn`, which has diverged from `master`
— it carries doc-16's fixes and recent composable-schema work. Code claims are anchored to this branch.)*

---

## 3. Functionality review

### Capability matrix (✅ solid · 🟡 partial · ⬜ thin/missing)

| Area | Status | Notes (code-anchored) |
|---|---|---|
| **Task adapters** (9 kinds) | 🟡 | The "nine adapters" claim is accurate (`adapters/tasks.py::_KINDS`), but only **`repo`, `mlebench_real`, `dataset`** do real work; the other six (`quadratic`, `regression`, `classification`, `timeseries`, `mlebench`, `code_regression`) are **synthetic demos / trust-harness validators**. `mlebench_real`'s offline baselines cover only **3 competitions**. |
| **Composable task front-end** | ✅ | `normalize_task` infers the adapter from capability fields, keeps legacy spellings, and **raises on ambiguity** rather than silently defaulting — mature, contract-tested. |
| **Roles & backends (ADR-7)** | ✅ | Toy / `LLMResearcher` / `ToolUsingResearcher` / `LLMRepoDeveloper` / `UnifiedAgent` all wired; per-role model/temp/backend routing shipped. External coding-agent Developer (opencode/aider/goose/continue) is **wired but the least-reliable path** (biases to full-file rewrites on small models; needs the binary installed). The robust repo path is the **in-house** `LLMRepoDeveloper` (STAGES→PLAN→IMPLEMENT). |
| **"Evaluator" role** | ⬜ | There is **no first-class Evaluator**; verification is a distributed subsystem (sandbox + host grader + trust gates + critic + memo-verifier), and `trust/cv.py`'s `Evaluator` Protocol is **unused**. A naming/architecture gap. |
| **Search policies** | 🟡 | `greedy` is the only default; `evolutionary`/`mcts`/`asha`/`bohb` are **opt-in and under-exercised** (their own docstrings say "opt-in", "no published ablation") — yet they're in the Strategist's default decision space. |
| **Operators** | ✅/🟡 | draft/improve/`debug`(depth-bounded)/merge(ensemble for code kinds) default; `ablate→refine_block` **opt-in** (`ablate_every=0`) and correctly disabled on repo/eval-spec runs. Mean-merge silently no-ops on non-numeric params. |
| **Trust layer** | 🟡 (built, **shipped OFF**) | Every gate exists and is unit-tested, but defaults are advisory/disabled: `confirm_top_k=0`, `reward_hack_detect=False`, `code_leakage_detect=False`, `critic_check=False`, `redact_output=False`, `trust_gate=advisory`. `--profile thorough` flips them. **Held-out-gated promotion is the exception — ON by default** (`holdout_select=True`). Temporal-CV **unwired** (see §1). |
| **Sandbox tiers** | 🟡 | `trusted_local` (subprocess, default) / `untrusted` (docker `--network none`) / `hostile` (gVisor) implemented — but the **real adapters aren't validated on the isolated tiers** (`dataset` reads absolute host paths; `mlebench_real` needs pandas/sklearn in-image). |
| **Memory / knowledge** | ✅ | All seven tiers complete and wired (cases/lessons/meta-notes/skills/KB/hypotheses/deep-research memo); fingerprint transfer; Memora harmonic index. (Retrieval is flat top-k — the NapMem upgrade in §6.) |
| **Serve / UI** | ✅ | ~20 React panels, full router set, token-auth/CSRF hardening, a dependency-light TUI. **Assistant write/shell/git is phased (P0 read-only)** — the "fix LoopLab itself" story is largely aspirational in the default. |
| **CLI** | ✅ | run/resume/stop/finalize/approve/init + export/bench/harden/smoke + inspect/replay/timings + ui/tui. Comprehensive, friendly errors, exclusive `engine.lock`. |
| **Genesis / Strategist / deep-research** | ✅ | Genesis authors the task from a goal (CLI + web); the **Strategist is the standout** (rule/LLM/tool-using backends, a governance matrix, replay-safe); deep-research memo with evidence ledger + verifier — but **introspective by default** (`literature_search`/`web_search` off). |

### The functionality gaps worth prioritizing

1. **Trust/rigor ships dormant** (§1.1) — reconcile the default with the "trust is the point" positioning.
2. **Temporal-CV moat has no caller** (§1.2) — ship one adapter that runs the purged/embargoed splitter live.
3. **Real-task breadth is thin** — 6 of 9 adapters are synthetic; `dataset` uses a **self-reported metric**
   (reward-hackable, no private grader). Add real tabular/text adapters with held-out grading.
4. **External coding-agent Developer is the least-reliable "hero" path** — invest in the in-house repo
   developer / one validated external agent rather than four half-proven presets.
5. **Isolated sandbox tiers aren't validated for the real adapters** — a data-mount + image contract that
   makes at least one real adapter runnable under Docker.
6. **Assistant write/shell/git is incomplete** — confirm what P1 actually landed before claiming a
   self-editing agent.
7. **No first-class Evaluator** — either unify verification behind one Evaluator or drop the four-role framing.
8. **Alt search policies are opt-in/under-exercised** yet in the Strategist's default reach — promote+benchmark
   them, or narrow what the agentic Strategist may switch to.

---

## 4. Code & test health

**Healthy and rigorously tested — the concentrated risks are maintainability, not correctness.**

### Test coverage shape

| Tier | Subsystems | Character |
|---|---|---|
| **Very strong** | `events/`, `engine/` | The crown jewel. Fold determinism, torn-tail healing, idempotent terminals, seq monotonicity; a **real subprocess `kill -9` + resume** test (`test_end_to_end.py`) asserting the log is un-rewritten and all nodes replay. |
| **Strong** | `core/`, `search/`, `trust/` | Dense; config↔docs sync guarded; `test_openai_client` (792 LoC). |
| **Good** | `tools/`, `adapters/`, `agents/` | Contract-tested seams; a few thin modules (`edit_match`, `kaggle_dl`, `mlebench_prep`). |
| **Moderate — thinnest per LoC** | `serve/`, `cli/`, `runtime/` | ~10 `serve/` modules unreferenced by name (`tui_format`, `scope_report`, `routers/misc`, `routers/reports`, `settings_store`) — and this is exactly where doc-16's real bugs slipped (token-auth raw-file gap, projection metric disagreement). |

**Invariant discipline is a standout.** Every load-bearing seam has a *two-way source-scan* test (producer
**and** consumer checked against one registry): task hooks, role-output attrs, prompt keys, signals
(`test_signal_delivery`), hints. Splice-neutrality is proven by folding the same log with a background event
at *every* position (`test_background_appendable`). This is unusually strong for a system this size.

### Complexity hotspots & structural debt

- **The Engine is a god-object *paginated* across 12 mixins, not decomposed.** All mixins share `self` as the
  Engine; each carries **no state of its own** and reads ~106 attributes it never declares. `__init__` alone
  is **375 lines / 106 `self.<attr> =` assignments**. The split gives file-level readability with **zero
  coupling reduction** — and no interface declares which attributes a mixin may touch, so a rename in
  `__init__` can silently orphan a mixin the type checker can't see. (The `test_background_appendable`
  source-scan exists precisely because the seam offers no static guarantee.)
- **`core/config.py` is a 147-field flat `Settings` with 2 validators.** The flatness is a hard invariant
  (env `LOOPLAB_<FIELD>` 1:1), so it's *permanent* debt — the mitigation is validators, not nesting. Enum-like
  fields historically weren't config-time-validated (a typo silently disables a gate).
- **The `_LAYOUT` meta-path shim** (`__init__.py`, 143 hand-maintained entries) aliases every pre-split flat
  module to its subpackage via a custom finder — permanent back-compat surface that every new module must
  extend.
- **Defensive-default divergence** — scattered `getattr(s, "x", <literal>)` fallbacks disagree with schema
  defaults (`auto_install_deps` False vs True; `foresight_panel` 1 vs 2; `strategist_backend` 'off' vs
  'agent'). Latent today (full `Settings` always passed), but two sources of truth per default.
- **`events/replay.py` is the model the rest should follow** — ~65 uniform `_on_*(st,e,d,ctx)` handlers
  dispatched via one dict; additive, unknown-tolerant. The cleanest big file in the tree.

**Error-handling posture is a genuine strength**: a clean transient/deterministic/fatal classification
(`core/errors.py` + `core/llm_transient.py`), fold tolerance of malformed rows, and resume-by-replay tested
end-to-end.

### Code-health directions (from the audit)

1. **Dissolve the Engine god-object for real, or stop pretending** — either extract genuine collaborators with
   declared interfaces (finish the `LessonMemory`-as-held-object job; delete the property-forwarders), or
   group `__init__`'s 70 knobs into cohesive frozen sub-configs.
2. **Config-time validation for every enum-like `Settings` field** — the single highest-leverage fix for a
   147-field flat config (a typo currently disables a gate silently).
3. **Kill the defensive-default divergence class** — one helper that reads the field default; never define a
   default in two places.
4. **Raise the `serve/` test floor** — even import+shape tests on the ~10 unreferenced modules would catch the
   doc-16-class regressions.
5. **Close the fork/inject crash-window** (doc-16 §13 deferred) — deterministic request-indexed node-ids so
   effect-before-gate becomes idempotent on resume; the one place the flagship invariant still leaks.
6. **Make the mixin/registry seams statically checkable** — `typing.Protocol` for what each Engine mixin
   requires of `self`, so a type checker (not just a red test) catches an orphaning rename.
7. **Plan the `_LAYOUT` shim's retirement** — migrate call sites to canonical paths, shrink the map, keep
   `test_package_layout` honest until the finder can be deleted.

---

## 5. Architecture review

**The event-sourced design does what it claims, and the load-bearing invariants hold** (verified afresh in
[doc 16 §3](16-architecture-code-review-2026-07-11.md); re-summarized, not re-derived, here):

- **Files-as-truth + single-writer + CQRS.** The engine is the sole writer of domain events (every
  `store.append` site classified; the only non-engine writers are the allow-listed, fold-safe control events).
  The UI reads projections and appends command intents — no UI↔engine write race.
- **Layering is clean.** `core` imports nothing above itself; `events` imports only `core`; the engine has
  **no** dependency on `serve`. The back-compat shim resolves both names to the same module object.
- **The fold is deterministic, order-tolerant, unknown-type-tolerant, additive-only.** First-terminal-wins
  idempotency and the `<x>_requests`/`<x>s_done` counter pairs are correct; resume = replay.
- **Six registry-guarded duck-typed seams are in sync**, each with a two-way source-scan test.

**Architectural evolution pressure points** (where the design will strain as it grows):

1. **The mixin-Engine's implicit coupling** (§4) is the biggest structural risk — it scales file count, not
   comprehensibility. As the Engine accretes knobs, the 106-attribute shared `self` becomes the limiting
   factor on safe change. *This is the #1 architectural refactor to schedule.*
2. **Concurrency is single-node, in-process, and off by default.** `max_parallel=1` (a deliberate local-first
   cost choice); the `anyio` `CapacityLimiter` fan-out seam exists but there is **no remote worker pool or
   fleet launcher**. Scaling to throughput-based test-time compute (the AIRA lever) needs this seam extended
   outward — a real architectural project, not a flag.
3. **Verification is distributed, not a component.** No Evaluator object owns "is this result real"; it's
   spread across sandbox/grader/gates/critic/verifier. Fine today, but it's why `trust/cv.py`'s Protocol
   drifted into being unused — there's no single home to wire it into.
4. **The flat-config invariant** trades nesting for env-mapping simplicity; the cost is a 147-field surface
   that only validators keep honest.

**Net architectural verdict: sound and defensible.** The event-log spine is the genuine, still-defensible
edge (per [ADR-6](03-decisions.md)); the risks are maintainability and a few structural debts the team is
visibly managing, not correctness or replay-safety.

---

## 6. Development directions

The planning corpus (docs 10–13, 17, ROADMAP, BACKLOG, the D1–D14 / T1–T10 / P1–P4 / M1–M6 / Themes A–I
schemes) has heavy **ID sprawl** — the same direction appears under 3–5 labels — and is **mostly a record of
completed work.** Deduplicated and verified against code, it splits into six themes. The critical honesty
move: **most of it is already shipped.**

### 6.1 Already shipped — do NOT re-propose (verified against code)

Held-out-gated promotion (`holdout_select=True`, ON by default — the single most-cited "gap" in the corpus,
and its "parked per user" note is **stale**); the trust ladder; memory hygiene; **comparative live-shared
lessons** (MARS 2+5 = "M6"); operator-scoped memory + insight backprop; auto-distilled skills;
fingerprint transfer; real embeddings + fold read-cache; the decoupled **verifier + evidence ledger** (D8);
the **hacker-fixer-solver** hardening core (`trust/harden.py`); the hypothesis ledger + board; **richer
operators** (ablation, ensemble-merge, depth-bounded debug); ASHA/BOHB; proxy scoring; list-wise Best-of-N;
endgame reserve; the SWE-reliability stack (localize→best-of-N→repair→critic); structured-output reliability
(Theme H); feature-eng + tabular adapters (Theme I); and the **entire U1–U7 UI layer**. Plus doc-16's H/M
bug set (44 files, suite green).

### 6.2 The genuinely-open frontier (the small, concentrated part)

| # | Direction | Theme | Status (verified) | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | **Deep-research literature/web grounding** — flip `literature_search`/`web_search` to ON + widen the tool budget | Research breadth | ⬜ both `False` by default → memo is introspective-only | **S** (2 flags) | strong (doc-17 rec#1: highest-ROI cheap flip in the corpus) |
| 2 | **Proactive coverage trigger + distance-from-seed** — fire the already-computed concentration signal *before* collapse; mode-gate to open-ended | Research breadth | 🟡 `coverage_signal` computed + cites arXiv:2605.27905, drives a *reactive* proposal hint only, never selection | S–M | medium (open-ended only) |
| 3 | **Frameworks/Libs curated KB + note frontmatter** | Memory depth | ⬜ `KnowledgeWriteTools` writes plain markdown + `_tags` only — no frontmatter | S–M | medium (doc-17 item 0) |
| 4 | **NapMem navigable pyramid** — activate the **dormant `CaseLibrary`** (`engine/memory.py:514`) into provenance-linked drill-down; **drop the RL** | Memory depth | ⬜ retrieval is flat top-k everywhere; the store is 80% built and unwired | M | speculative paper / strong internal fit (ADR-10 revision) |
| 5 | **Cost/budget-aware reward in search** (MARS #1) | Search intelligence | ⬜ `budget_aware` is a prompt cue only; `operator_yields` computes Δ/sec but feeds only the off-by-default bandit | S | strong (MARS) |
| 6 | **Cumulative parent-diff + modular decompose** (MARS #4) — pass `parent.code` into `improve` and patch in place | Developer reliability | 🟡 repo kind has multi-file/diff/refine_block/fault-loc; `improve` re-seeds from pristine baseline (`parent.code` not passed) | S (first step) / M | strong (MARS; doc-13 §7 *corrected* the original over-scope) |
| 7 | **Wire the temporal-CV moat** — one adapter that runs `purged_walk_forward`/`consistent_cv` on the live path | Trust integrity | ⬜ `trust/cv.py` Protocol complete-but-unconsumed | M | strong (ADR-6's stated edge, currently un-exercised) |
| 8 | **Reconcile trust-default with positioning** — auto-enable a rigor tier on real-work adapters, or make `thorough` the non-toy default, or reposition the claim | Trust integrity | 🟡 all gates OFF by default (deliberate cheap-default) | S–M | strong (§1.1) |
| 9 | **Scale**: remote worker pool + fleet launcher; LLM response cache | Scale & proof | 🟡 in-process fan-out seam exists; remote/fleet/cache open | L / S | strong (throughput is the AIRA lever) |
| 10 | **External proof: real MLE-bench publish** with the now-shipped holdout discipline; **AgentDS adapter** (probes framing/pivot) | Scale & proof | ⬜ infra/data-gated | L / S | strong (the standing validation of everything above; targets MARS+ 62.67% / Arbor 86.4% Lite) |
| 11 | **Code-health refactors** (§4): dissolve the mixin-Engine, config-time enum validation, kill defensive-default divergence, raise `serve/` test floor, close the fork/inject crash-window | Maintainability | mixed | mixed | strong (structural debt the team is already tracking) |

### 6.3 Tensions the review must not paper over

1. **ADR-6 demotes diversity/novelty vs doc-17 re-justifies it.** Not a reversal — a **scoping**: diversity is
   dead weight in **fixed-metric mode** (MLE-bench — local elaboration *is* the win) and load-bearing in
   **open-ended mode**. *Resolution: mode-gate all breadth machinery on task kind / open-ended flag — never
   global.*
2. **"Default off" ≠ "unreachable."** The exploration levers called "off by default" are **static-config-off
   but Strategist-mutable at runtime** (`strategist_backend='agent'`), flipping **reactively, post-collapse**.
   So the open work is *timing and gating* (make triggers proactive), not new subsystems.
3. **MLflow: shipped-then-demoted.** `events/mlflow_export.py` exists, but ADR-6 demotes it from core to an
   optional exporter off the hot path. Don't re-propose MLflow-as-core.
4. **B6 held-out selection: "parked per user" (stale) vs shipped-on-by-default (real).** Treat as **done**.
5. **MARS #4 was over-scoped and corrected** (doc-13 §7) — repo kind already has modular/diff/refine machinery;
   the real gap narrowed to cumulative parent-diff. A review repeating the uncorrected framing over-scopes it.
6. **Report/paper stage is a product non-goal** ([doc 01:40](01-product-design.md)) — adopting it is a scope
   decision, not a backlog item.
7. **"More attempts ≫ more compute" vs `max_parallel=1`** — the throughput lever is real, but the default is a
   deliberate local-first cost choice; the seam exists, flipping it is a cost-policy call.
8. **SciResearcher-8B backend / self-distillation** (doc-17 rec 6/7) **breaks backend-agnosticism** (ADR-7) and
   rests on unverified paper claims — external/opt-in only.

---

## 7. Recommendation — the next bets, in three horizons

**Horizon 1 — cheap, high-ROI, on existing seams (do first).**
- **Flip deep-research `literature_search`/`web_search` ON + widen its budget** (dir 1) — two flags; the
  plumbing to turn `recommended_directions` into hypotheses already exists. The single highest-ROI open item.
- **Cost-aware reward in search** (dir 5) + **pass `parent.code` into `improve`** (dir 6 first step) — both
  small, both on live seams.
- **Config-time enum validation + kill defensive-default divergence** (dir 11, code-health #2/#3) — retires a
  whole class of latent config bugs cheaply.

**Horizon 2 — close the credibility gaps (the "built but not on the default path" problems).**
- **Reconcile the trust default with the positioning** (dir 8) — e.g. auto-enable a rigor tier on
  `repo`/`mlebench_real`/`dataset`, since a real-work run doing zero rigor undercuts the product's core claim.
- **Wire the temporal-CV moat** (dir 7) — one adapter that runs the purged/embargoed splitter live, so ADR-6's
  differentiator has a caller.
- **Add a real dataset adapter with a private grader** (functionality gap 3) — closes the `dataset`
  self-reported-metric hole.
- **Proactive, mode-gated coverage trigger + distance-from-seed** (dir 2) — make the reactive breadth machinery
  fire before collapse.
- **Raise the `serve/` test floor + close the fork/inject crash-window** (dir 11, code-health #4/#5).

**Horizon 3 — strategic depth & external proof.**
- **Memory navigation**: Frameworks/Libs KB (dir 3) → activate the dormant `CaseLibrary` into a NapMem pyramid
  (dir 4) — the ADR-10 retrieval upgrade.
- **Dissolve the mixin-Engine god-object** (dir 11, code-health #1) — the #1 maintainability investment before
  the Engine accretes further.
- **Scale outward**: remote worker pool + fleet launcher + response cache (dir 9).
- **The external proof point**: a real MLE-bench run published with the shipped holdout discipline, plus the
  AgentDS adapter (dir 10) — the L-effort item that would validate the whole stack against MARS+/Arbor.

**One-line strategy read.** LoopLab has already built the hard, differentiated core — an event-sourced,
crash-resumable, trust-gated research loop with deep memory. The highest-value near-term work is not new
subsystems but **turning on and wiring up what's already built** (literature grounding, the trust default, the
temporal-CV moat, the dormant memory pyramid) and **paying down the one structural debt that will otherwise
throttle everything** (the god-object Engine). The single strategic bet that would convert all of this into
external credibility is a **published real-MLE-bench result** under the holdout discipline that already ships.

---

## Sources & method

- **This review's inputs:** three parallel code-grounded deep-dives (functionality/capability audit;
  test-coverage & code health; directions synthesis over `docs/`), each anchored to `file:line` and verified
  against `looplab/` on this branch.
- **Complements:** [16-architecture-code-review-2026-07-11.md](16-architecture-code-review-2026-07-11.md)
  (tactical line-level audit + applied fixes), [17-…-kb-2026-07-11.md](17-napmem-sciresearcher-exploration-kb-2026-07-11.md)
  (memory/exploration directions), [13-external-works-analysis-2026-07.md](13-external-works-analysis-2026-07.md)
  (MARS/AgentDS/AgentRxiv), [ROADMAP.md](ROADMAP.md) / [BACKLOG.md](BACKLOG.md) (the shipped record).
- **Framing:** [01-product-design.md](01-product-design.md) §3 (goals/non-goals), [ADR-6](03-decisions.md)
  (the 2026 positioning: differentiate on operators / evaluation rigor / ensembling / leakage-safety).
- **Caveat:** static review (pytest unavailable); suite status (1718 passed) quoted from doc 16, not re-run.
</content>
