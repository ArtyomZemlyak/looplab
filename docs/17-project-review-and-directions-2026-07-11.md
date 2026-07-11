# LoopLab — Project Review, Architecture & Development Directions (2026-07-11)

**Companion docs:** [01-product-design.md](01-product-design.md) · [02-architecture.md](02-architecture.md) · [03-decisions.md](03-decisions.md) (ADR-6, ADR-9, ADR-10) · [16-architecture-code-review-2026-07-11.md](16-architecture-code-review-2026-07-11.md) (the tactical audit this complements) · [13-external-works-analysis-2026-07.md](13-external-works-analysis-2026-07.md) · [ROADMAP.md](ROADMAP.md) · [BACKLOG.md](BACKLOG.md) · [guide/memory.md](guide/memory.md)

**Current-state baseline.** Strategic content is reconciled through repository baseline `32dc6c0`;
the production, test, and UI trees remain executable snapshot `2ce82fd`.

**What this is.** One structural document in three parts. **Part I** is a *strategic* review across the
three axes the maintainer asked for — **functionality**, **code**, **architecture** — the altitude
companion to [doc 16](16-architecture-code-review-2026-07-11.md), the revalidated line-level audit and
priority ledger: 7 P0, 12 P1, plus P2/P3; most original H/M findings are fixed, while H2, M4, and M8
are partial/reopened. **Part II** works out the **development directions** (themes, an honest
shipped-vs-open split, tensions, a four-horizon plan). **Part III** is a
code-level **deep-dive on four specific candidate directions** — a curated Frameworks/Libs knowledge base,
and three 2026 papers (NapMem, SciResearcher, "AI Research Agents Narrow Scientific Exploration") — each
assessed for how it integrates in code, the complications, and the synergy, then re-verified by an 8-agent
adversarial pass (§13).

**Method.** Parallel code-grounded deep-dives (functionality/capability audit; test-coverage & code health;
a deduplicated directions synthesis over the whole `docs/` corpus; and the four-direction integration study),
each anchored to `file:line` and cross-verified against `looplab/` on this branch, then synthesized against
[doc 16](16-architecture-code-review-2026-07-11.md) and the product goals ([doc 01 §3](01-product-design.md)).

> **Caveat.** This strategic pass itself was static because `pytest` was unavailable in its environment;
> doc 16 was not static-only. It reports separate Linux 3.11 and 3.12 runs of **1,711 passed, 33 skipped,
> 1 failed** each, plus focused Windows, platform, UI, and static-analysis evidence. Both full runs had
> the same stale keepalive-test failure. **Part III's paper claims** (quantitative figures + the arXiv IDs)
> were arXiv-proxy-blocked and are snippet-sourced — **unverified this session** (marked inline; see §13).

**Contents.** Part I — §1 executive summary · §2 snapshot · §3 functionality · §4 code & tests · §5
architecture. Part II — §6 development directions · §7 the next bets. Part III — §8 Frameworks/Libs KB · §9
NapMem · §10 SciResearcher · §11 Narrow-Exploration · §12 how they compose + deep-dive recommendation · §13
verification pass.

---

# PART I — PROJECT & ARCHITECTURE REVIEW

## 1. Executive summary — where LoopLab stands

**LoopLab is a mature, unusually well-engineered system with a strong experimental foundation, but its
remaining frontier is not only small feature work or maintainability.** ~39.7k LoC of engine across 12
packages and ~27k LoC of tests (~1,600 test functions, ~1 test per 25 source LoC) support an event-sourced
spine that is deterministic for a fixed ordered log. The revalidated tactical audit in
[doc 16](16-architecture-code-review-2026-07-11.md) also found **7 P0 and 12 P1** issues around attempt/epoch
identity, durability, permissions, trust, budgets, process control, and supported-platform behavior. The
multi-document roadmap still records substantial shipped work — held-out promotion, comparative shared
lessons, the SWE-reliability stack, structured-output reliability, memory hygiene, the verifier, and UI —
but release confidence now requires stabilization before more broad feature work.

**The verdict on each axis:**

| Axis | Verdict |
|---|---|
| **Architecture** | **Strong foundation, conditional invariants.** The lower dependency direction and pure fold are worth keeping, but reset/reopen/resume lack attempt, epoch, request/subject, and manifest identity; durability and policy enforcement still have release-blocking gaps. |
| **Code & tests** | **Broadly tested, not all-green.** The suite and registry tests are substantial, but the fold-handler partition guard is stale, one full-suite test fails on both Linux interpreters, and important server/platform/concurrency paths remain thin. |
| **Functionality** | **Broad and largely complete**, with two strategic soft spots: the **trust/rigor differentiator ships OFF by default**, and the **temporal-CV "moat" has no live caller**. |
| **Directions** | **Stabilization first, then the strategic frontier.** Most product directions are refinements of already-built machinery, but they follow doc 16's Phase 0–3 identity, durability, boundary, budget, and process work. |

**After the P0/P1 stabilization ledger, the two strategic findings this review most wants surfaced**
(both defensible-by-design, both a risk):

1. **The trust layer is comprehensively built but shipped dormant.** Confirmation, reward-hack, code-leakage,
   critic, redaction, and *enforcement* (`trust_gate`) are all OFF/audit-only by default. `--profile
   thorough` enables confirmation/detectors and flips `trust_gate` to `gate`, but it does **not** enable
   `redact_output` (`core/config.py:43-63,336-354`). This is a *deliberate*
   cheap-toy-default design (the config comment says so), but it means the default `looplab run` performs
   none of the rigor the product — and the UI's own "Trust & rigor — the point of LoopLab" panel
   (`ui/src/panels.jsx:118`) — advertises. **Reconcile the default with the positioning.**
2. **The temporal/target-leakage differentiator (ADR-6's stated edge) has no live caller.** `trust/cv.py`'s
   own docstring says `purged_walk_forward` / `consistent_cv` / the `Evaluator` Protocol are "complete and
   tested, but not yet consumed by a shipped adapter"; the `timeseries` adapter runs its own embedded
   backtest instead. **The claimed moat needs one adapter that exercises it end-to-end.**

Neither is a bug; both are *positioning/wiring* gaps between what's built and what's on the default path.

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
| **Trust layer** | 🟡 (built, **shipped OFF**) | Defaults are disabled/audit-only: `confirm_top_k=0`, `reward_hack_detect=False`, `code_leakage_detect=False`, `critic_check=False`, `redact_output=False`, `trust_gate='audit'`. `--profile thorough` enables confirmation/detectors and gating, but not redaction. **Held-out-gated promotion is ON by default** (`holdout_select=True`), while reopen/epoch semantics remain a doc-16 P0. Temporal-CV is **unwired** (see §1). |
| **Sandbox tiers** | 🟡 | `trusted_local` (subprocess, default) / `untrusted` (Docker `--network none`) / `hostile` (gVisor) are implemented, but real adapters lack a complete isolated-tier input/image contract; Windows extra mounts are currently malformed (doc 16 P1-8). |
| **Memory / knowledge** | ✅ | All seven tiers complete and wired (cases/lessons/meta-notes/skills/KB/hypotheses/deep-research memo); fingerprint transfer; Memora harmonic index. (Retrieval is flat top-k — the NapMem upgrade in §9.) |
| **Serve / UI** | 🟡 | ~20 React panels, a full router set, token/CSRF hardening, and a dependency-light TUI. Sensitive-GET auth is still default-open for missed routes and client control registries diverge (doc 16 P1-3/P1-10). **Assistant write/shell/git is phased (product P0 read-only)** — the "fix LoopLab itself" story is largely aspirational in the default. |
| **CLI** | ✅ | run/resume/stop/finalize/approve/init + export/bench/harden/smoke + inspect/replay/timings + ui/tui. Comprehensive, friendly errors, exclusive `engine.lock`. |
| **Genesis / Strategist / deep-research** | 🟡 | Genesis authors the task from a goal (CLI + web); the **Strategist is a standout** (rule/LLM/tool-using backends and an event-recorded governance matrix), though the latest policy/parameter authorization has a P1-11 bypass. Deep-research memo has an evidence ledger + verifier but is **introspective by default** (`literature_search`/`web_search` off — see §10). |

### The functionality gaps worth prioritizing

These are strategic/product gaps. They come **after** doc 16's P0 and P1 stabilization work; none
supersedes the attempt/epoch, durability, permission, auth, trust, or process-control blockers.

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

## 4. Code & test health

**Broadly and seriously tested — but the concentrated risks now include correctness and durability,
not only maintainability.**

### Test coverage shape

| Tier | Subsystems | Character |
|---|---|---|
| **Very strong foundation** | `events/`, `engine/` | Fold is deterministic for a fixed ordered log; torn **final-line** healing and first-terminal idempotency **within one node attempt** are well tested. A real subprocess `kill -9` + resume test exists. Mid-file corruption, cross-attempt effects, epoch transitions, and fail-open-lock sequence numbers remain outside those guarantees. |
| **Strong** | `core/`, `search/`, `trust/` | Dense; config↔docs sync guarded; `test_openai_client` (792 LoC). |
| **Good** | `tools/`, `adapters/`, `agents/` | Contract-tested seams; a few thin modules (`edit_match`, `kaggle_dl`, `mlebench_prep`). |
| **Moderate — thinnest per LoC** | `serve/`, `cli/`, `runtime/` | ~10 `serve/` modules are unreferenced by name (`tui_format`, `scope_report`, `routers/misc`, `routers/reports`, `settings_store`) — and this is where doc 16's current auth, permission, process, platform, and client-control bugs cluster. |

**Invariant discipline is a real strength, with one important qualification.** Two-way source-scan tests
protect task hooks, role-output attrs, prompt keys, signals (`test_signal_delivery`), and control membership.
The fold-handler coverage test still scans the pre-dispatch implementation and no longer proves the
handled/diagnostic partition. Background splice tests preserve the already-folded champion, but a hint
racing an operator `replace` can still change steering text seen by later proposals.

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
(`core/errors.py` + `core/llm_transient.py`) and tolerant handling of a torn final JSONL row. Resume is a
hybrid of events, snapshots, mutable workdirs, current source/data, and process side channels; mid-file
corruption and setup crash boundaries are not safely recovered by replay alone.

### Code-health directions (from the audit)

First execute doc 16's Phase 0–3 stabilization program: fail-closed containment, event/state identity,
durability/reproducibility, and shared execution infrastructure. The maintainability items below are
second-order unless they directly support that work.

1. **Dissolve the Engine god-object for real, or stop pretending** — either extract genuine collaborators with
   declared interfaces (finish the `LessonMemory`-as-held-object job; delete the property-forwarders), or
   group `__init__`'s 70 knobs into cohesive frozen sub-configs.
2. **Config-time validation for every enum-like `Settings` field** — the single highest-leverage fix for a
   147-field flat config (a typo currently disables a gate silently).
3. **Kill the defensive-default divergence class** — one helper that reads the field default; never define a
   default in two places.
4. **Raise the `serve/` test floor** — even import+shape tests on the ~10 unreferenced modules would catch the
   doc-16-class regressions.
5. **Close the fork/inject crash-window** (doc 16 P3) — deterministic request-indexed node IDs so
   effect-before-gate becomes idempotent on resume. It is one residual edge, not the only lifecycle gap;
   attempt, epoch, request/subject, setup, and manifest identities are higher-priority work.
6. **Make the mixin/registry seams statically checkable** — `typing.Protocol` for what each Engine mixin
   requires of `self`, so a type checker (not just a red test) catches an orphaning rename.
7. **Plan the `_LAYOUT` shim's retirement** — migrate call sites to canonical paths, shrink the map, keep
   `test_package_layout` honest until the finder can be deleted.

## 5. Architecture review

**The event-sourced foundation is valuable, but its load-bearing guarantees are conditional** (see the
revalidated [doc 16 §§2–5](16-architecture-code-review-2026-07-11.md)):

- **Fold is deterministic for a fixed ordered log, not commutative or attempt-aware.** Unknown events are
  tolerated and first-terminal-wins is correct inside one attempt; reset reuses the node ID, so late work
  can mutate the replacement attempt. Reopen likewise lacks a search/finalization epoch and subject-bound
  approval/promotion records.
- **EventStore handles a torn final line, not arbitrary corruption.** A malformed middle line creates an
  invisible valid tail that future appends continue to extend. Sequence monotonicity also depends on a
  working filesystem lock; the documented fail-open path can duplicate sequence numbers.
- **The lower dependency direction is clean.** `core <- events <- engine <- cli/serve` holds narrowly and
  engine has no `serve` dependency. The middle layer still contains a lazy-import SCC among adapters,
  agents, search, and tools.
- **Resume is not replay alone.** It also depends on task/config snapshots, mutable node workdirs, current
  source/data bytes, role side channels, background processes, and memory files. A task hash does not pin
  the complete executable environment or workspace manifest.
- **Single-writer/CQRS is a useful intent, not a complete concurrency proof.** Control and allow-listed
  background writers append outside the main loop; state-sensitive commands lack expected-revision/CAS,
  and client registries disagree about which controls require a live Engine.

**Architectural evolution pressure points, in priority order:**

1. **Add state identity and fail-closed durability first.** Node attempts, search/finalization epochs,
   request/subject revisions, setup completion, and immutable run/workspace manifests are the release
   blockers; they fit as additive event-schema evolution rather than a rewrite.
2. **Centralize policy-bearing effects.** Auth, permission, transition validation, budgets, path policy,
   and process supervision are currently enforced by duplicated conventions that have already drifted.
3. **Then dissolve the mixin-Engine's implicit coupling.** The shared `self` improves file navigation but
   not ownership. Extract typed collaborators behind the existing facade as the stabilization services
   become real.
4. **Concurrency remains local and in-process.** `max_parallel=1` is a deliberate default; scaling to a
   worker fleet is a separate architecture project and should follow correct atomic budgets/process control.
5. **Verification has no single owner.** Sandbox, grader, gates, critic, and verifier are distributed, while
   `trust/cv.py`'s Evaluator Protocol has no live caller.
6. **The flat-config invariant** trades nesting for env-mapping simplicity; the cost is a 147-field surface
   that validators and generated representations must keep consistent.

**Net architectural verdict: strong foundation, not yet release-safe under reset/reopen/resume and
concurrency.** Keep the event log, fold, lower layer direction, and public projections. Stabilize identities,
transitions, durability, and effect ownership before treating correctness/replay safety as closed.

---

# PART II — DEVELOPMENT DIRECTIONS

## 6. Development directions

The planning corpus (docs 10–13, Part III below, ROADMAP, BACKLOG, the D1–D14 / T1–T10 / P1–P4 / M1–M6 /
Themes A–I schemes) has heavy **ID sprawl** — the same direction appears under 3–5 labels — and is **mostly a
record of completed work.** Deduplicated and verified against code, it splits into six themes. The critical
honesty move: **most of it is already shipped.**

This feature/direction inventory does not supersede the tactical priority ledger. Doc 16 Phase 0–3
stabilization — containment, state identity, durability, budgets, process control, and supported-platform
boundaries — is the prerequisite for the product bets below.

### 6.1 Already shipped — do NOT re-propose (verified against code)

Held-out-gated promotion (`holdout_select=True`, ON by default — the single most-cited "gap" in the corpus,
and its "parked per user" note is **stale**); the trust ladder; memory hygiene; **comparative live-shared
lessons** (MARS 2+5 = "M6"); operator-scoped memory + insight backprop; auto-distilled skills;
fingerprint transfer; real embeddings + fold read-cache; the decoupled **verifier + evidence ledger** (D8);
the **hacker-fixer-solver** hardening core (`trust/harden.py`); the hypothesis ledger + board; **richer
operators** (ablation, ensemble-merge, depth-bounded debug); ASHA/BOHB; proxy scoring; list-wise Best-of-N;
endgame reserve; the SWE-reliability stack (localize→best-of-N→repair→critic); structured-output reliability
(Theme H); feature-eng + tabular adapters (Theme I); and the **entire U1–U7 UI layer**. Most original doc-16
H/M defects are fixed; H2, M4, and M8 are partial/reopened, and the current full-suite evidence includes one
stale keepalive-test failure per Linux interpreter rather than an all-green run.

### 6.2 The genuinely-open frontier (the small, concentrated part)

**Prerequisite, not counted as a feature direction:** complete doc 16's 7 P0 and immediate P1 stabilization
ledger. The table below is the strategic frontier after that gate.

| # | Direction | Theme | Status (verified) | Effort | Evidence |
|---|---|---|---|---|---|
| 1 | **Deep-research literature/web grounding** — flip `literature_search`/`web_search` to ON + widen the tool budget | Research breadth | ⬜ both `False` by default → memo is introspective-only | **S** (2 flags) | strong (§10-B: highest-ROI cheap flip in the corpus) |
| 2 | **Proactive coverage trigger + distance-from-seed** — fire the already-computed concentration signal *before* collapse; mode-gate to open-ended | Research breadth | 🟡 `coverage_signal` computed + cites arXiv:2605.27905, drives a *reactive* proposal hint only, never selection | S–M | medium (open-ended only; §11) |
| 3 | **Frameworks/Libs curated KB + note frontmatter** | Memory depth | ⬜ `KnowledgeWriteTools` writes plain markdown + `_tags` only — no frontmatter | S–M | medium (§8) |
| 4 | **NapMem navigable pyramid** — activate the **dormant `CaseLibrary`** (`engine/memory.py:514`) into provenance-linked drill-down; **drop the RL** | Memory depth | ⬜ retrieval is flat top-k everywhere; the store is 80% built and unwired | M | speculative paper / strong internal fit (ADR-10 revision; §9) |
| 5 | **Cost/budget-aware reward in search** (MARS #1) | Search intelligence | ⬜ `budget_aware` is a prompt cue only; `operator_yields` computes Δ/sec but feeds only the off-by-default bandit | S | strong (MARS) |
| 6 | **Cumulative parent-diff + modular decompose** (MARS #4) — pass `parent.code` into `improve` and patch in place | Developer reliability | 🟡 repo kind has multi-file/diff/refine_block/fault-loc; `improve` re-seeds from pristine baseline (`parent.code` not passed) | S (first step) / M | strong (MARS; doc-13 §7 *corrected* the original over-scope) |
| 7 | **Wire the temporal-CV moat** — one adapter that runs `purged_walk_forward`/`consistent_cv` on the live path | Trust integrity | ⬜ `trust/cv.py` Protocol complete-but-unconsumed | M | strong (ADR-6's stated edge, currently un-exercised) |
| 8 | **Reconcile trust-default with positioning** — auto-enable a rigor tier on real-work adapters, or make `thorough` the non-toy default, or reposition the claim | Trust integrity | 🟡 all gates OFF by default (deliberate cheap-default) | S–M | strong (§1.1) |
| 9 | **Scale**: remote worker pool + fleet launcher; LLM response cache | Scale & proof | 🟡 in-process fan-out seam exists; remote/fleet/cache open | L / S | strong (throughput is the AIRA lever) |
| 10 | **External proof: real MLE-bench publish** with the now-shipped holdout discipline; **AgentDS adapter** (probes framing/pivot) | Scale & proof | ⬜ infra/data-gated | L / S | strong (the standing validation of everything above; targets MARS+ 62.67% / Arbor 86.4% Lite) |
| 11 | **Code-health refactors** (§4): dissolve the mixin-Engine, config-time enum validation, kill defensive-default divergence, raise `serve/` test floor, close the fork/inject crash-window | Maintainability | mixed | mixed | strong (structural debt the team is already tracking) |

### 6.3 Tensions the review must not paper over

1. **ADR-6 demotes diversity/novelty vs §11 (Narrow-Exploration) re-justifies it.** Not a reversal — a
   **scoping**: diversity is dead weight in **fixed-metric mode** (MLE-bench — local elaboration *is* the win)
   and load-bearing in **open-ended mode**. *Resolution: mode-gate all breadth machinery on task kind /
   open-ended flag — never global.*
2. **"Default off" ≠ "unreachable"** (§13-A). The exploration levers called "off by default" are
   **static-config-off but Strategist-mutable at runtime** (`strategist_backend='agent'`), flipping
   **reactively, post-collapse**. So the open work is *timing and gating* (make triggers proactive), not new
   subsystems.
3. **MLflow: shipped-then-demoted.** `events/mlflow_export.py` exists, but ADR-6 demotes it from core to an
   optional exporter off the hot path. Don't re-propose MLflow-as-core.
4. **B6 held-out selection: "parked per user" (stale) vs shipped-on-by-default (real).** Treat as **done**.
5. **MARS #4 was over-scoped and corrected** (doc-13 §7) — repo kind already has modular/diff/refine machinery;
   the real gap narrowed to cumulative parent-diff. A review repeating the uncorrected framing over-scopes it.
6. **Report/paper stage is a product non-goal** ([doc 01:40](01-product-design.md)) — adopting it is a scope
   decision, not a backlog item.
7. **"More attempts ≫ more compute" vs `max_parallel=1`** — the throughput lever is real, but the default is a
   deliberate local-first cost choice; the seam exists, flipping it is a cost-policy call.
8. **SciResearcher-8B backend / self-distillation** (§12, rec 6/7) **breaks backend-agnosticism** (ADR-7) and
   rests on unverified paper claims — external/opt-in only.

## 7. Recommendation — the next bets, in four horizons

**Horizon 0 — stabilization (before product bets).**

- Ship fail-closed containment for auth/permissions/path policy/log corruption and the resume/finalization
  wakeup race.
- Add node-attempt, search-epoch, request/subject, setup, and manifest identity; then centralize budgets and
  process supervision. Use doc 16's Phase 0–3 gates and event-sequence/platform matrix as exit criteria.

**Horizon 1 — cheap, high-ROI, on existing seams (after stabilization).**

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
  (dir 4) — the ADR-10 retrieval upgrade (detailed in §8–§9).
- **Dissolve the mixin-Engine god-object** (dir 11, code-health #1) — the #1 maintainability investment before
  the Engine accretes further.
- **Scale outward**: remote worker pool + fleet launcher + response cache (dir 9).
- **The external proof point**: a real MLE-bench run published with the shipped holdout discipline, plus the
  AgentDS adapter (dir 10) — the L-effort item that would validate the whole stack against MARS+/Arbor.

**One-line strategy read.** LoopLab has built a differentiated event-sourced research foundation with deep
memory, but its highest-value near-term work is **stabilizing lifecycle identity, durability, and centralized
effects**. After that, turn on and wire what already exists (literature grounding, the trust default,
temporal CV, the dormant memory pyramid) and reduce the god-object Engine as those services take ownership.
The strategic proof point remains a **published real-MLE-bench result**, but only after the promotion,
holdout, workspace, and trust boundaries in doc 16 are reliable.

---

# PART III — DEEP-DIVE: FOUR CANDIDATE DIRECTIONS

> A code-level integration study of four specific directions summarized in §6.2 (dirs 1–4): a curated
> Frameworks/Libs KB (общая БЗ) and three 2026 papers. For each — how it integrates in code, the
> complications, the synergy. **arXiv full texts were proxy-blocked**; paper claims (figures + arXiv IDs) are
> snippet-sourced and **unverified this session** (marked inline). Every *code* claim was re-verified by an
> 8-agent adversarial pass — see **§13** for the corner cases and corrections folded in.

**TL;DR verdict.**

| Item | Verdict | Synergy | Effort | Mode |
|---|---|---|---|---|
| **§8 · Frameworks/Libs KB** | **Build it** — the missing *curated semantic corpus*; the store + retrieval tools exist, but note frontmatter/versioning is *not* implemented yet, so it's a small real build, not "drop files in." | **High** — substrate the other three read from. | **S–M** | all |
| **§9 · NapMem** | **Adopt the structure, drop the RL** — retrieval today is **flat top-k everywhere**; NapMem is the navigable-pyramid upgrade of ADR-10. A **dormant `CaseLibrary`** is a ready-made seam. | **High** — closest fit; an ADR-10 revision. | **M** | large-corpus / open-ended |
| **§10 · SciResearcher** | **Backend option + deep-research patterns, not a framework** — it's a *training + data-construction* paradigm yielding an 8B bio/chem model; we're inference-time, backend-agnostic, ML-engineering. | **Modest** — the concrete win is turning on literature/web in `deep_research`. | **S (backend/flags) / L (training)** | opt-in |
| **§11 · Narrow-Exploration** | **Partly already built** — `coverage.py` cites this exact paper and computes a concentration signal, but it's *context-only, reactive*. Make it proactive + add distance-from-seed + wire it to selection. | **High** (open-ended) / N/A (fixed-metric) | **M** | open-ended only |

Nothing here replaces the core. Three of four are upgrades to machinery already in the tree; the KB is a small
build on the existing store; the only genuinely new capability class is training our own model (SciResearcher
angle C), which is the one thing that breaks backend-agnosticism — so it stays opt-in and external.

## 8. Frameworks/Libs knowledge base (общая БЗ)

### What we have vs what's missing (code-verified)

The **storage + retrieval** exist; the **curated corpus** and the **rich note schema** do not.

- **`knowledge/*.md`** — free-form notes, canonical on disk (`~/.looplab/knowledge`, `LOOPLAB_KNOWLEDGE_DIR`).
  Read via `kb_search`/`grep`/`list_notes`/`read_note` (`KnowledgeTools`, `knowledge_tools.py:192-319`);
  written by the assistant's `remember` tool (`KnowledgeWriteTools`, `knowledge_tools.py:137-189`). Sample
  notes are ML *concepts* (`examples/knowledge/polynomial_model_selection.md`).
- **Skills** — the **procedural** tier (`SkillTools`, `skills.py:56-89`; `examples/skills/cross_validation.md`):
  a recipe + code, read via `list_skills`/`use_skill`. **Skills already carry YAML frontmatter**
  (`name/description/status/provenance/source_task/fingerprints`, written by `write_auto_skill`,
  `memory.py:411-446`). Note the reader is minimal: `_parse_skill` (`skills.py:22-35`) parses the block but
  extracts only `name`+`description`; the other fields are consumed by `write_auto_skill`'s own regex
  (`memory.py:423,429`), not by `_parse_skill` — so the frontmatter *machinery* to copy exists, even if the
  reader would need extending.
- **`tools/env_inspect.py`** — the repo Developer's **live, read-only** introspector: an installed package's
  *version/source*, a class/function *signature*, an Enum's valid members, grep over installed source. Built
  to kill the #1 repo-experiment failure — the Developer **guessing** an API and being wrong (`precision='16-mixed'`
  vs `'16'`, a nonexistent `--gradient_clip_val`, an import that moved between versions; `env_inspect.py:1-9`).

**Two gaps, both real:**

1. **No curated framework/library corpus.** The idioms, gotchas, version-sensitive APIs, and "reach for X
   when Y" wisdom for the ML stack the agents actually write against (PyTorch/Lightning, JAX/Flax,
   scikit-learn, XGBoost/LightGBM/CatBoost, HF Transformers/`timm`, Optuna, pandas/Polars, …) live only in the
   base model's weights (stale, version-blind) plus whatever `env_inspect` reads off the *installed* package.
   `env_inspect` gives **live truth but no wisdom** ("what the API is" — not "this optimizer diverges without
   LR-warmup", "this splitter leaks on grouped data", "on this GPU prefer bf16").
2. **The rich note schema is docs-only.** ADR-10/ADR-16 describe notes as `{content, frontmatter(provenance,
   type, task_fingerprint, confidence, status), embedding, tags, [[links]]}` (`02-architecture.md:213`,
   `03-decisions.md:280-283`) — but **no code produces it.** `KnowledgeWriteTools.execute`
   (`knowledge_tools.py:163-189`) writes plain markdown + a trailing `_tags:_` line: **no frontmatter, no
   provenance/type/confidence/status, no on-disk embedding** (embeddings are ephemeral, rebuilt in-memory by
   `KnowledgeTools._build_index`, `knowledge_tools.py:233-265`), **no `[[links]]`** (wikilink-graph is
   unimplemented — see §9). The structured fields (`fingerprint/confidence/outcome`) live in the
   **JSONL** stores (`lessons.jsonl`/`cases.jsonl`) — and `fingerprints` additionally in auto-skill
   frontmatter, `outcome` in the event log — but **not** in the plain-markdown knowledge notes written by
   `remember`. *(The `_tags:` line is emitted only when tags are non-empty.)*

### Design — the KB is the ADR-10 *semantic* tier, split by facet

```
knowledge/
  frameworks/<name>.md   # pytorch-lightning.md, jax.md, xgboost.md — capabilities, idioms, when-to-use
  libs/<name>.md         # optuna.md, polars.md, timm.md          — version-sensitive APIs, gotchas, pins
  seed/<topic>.md        # existing ML-concept notes (unchanged)
  index/                 # DERIVED (vector), rebuildable          (ADR-10)
```

Two facets on purpose:
- **Frameworks** (torch/lightning/jax/sklearn/xgboost/hf/…): capability map, idiomatic usage, failure modes,
  and a `[[link]]` (once links exist) to the matching **Skill** where one exists.
- **Libs** (optuna/polars/timm/`accelerate`/…): the version-sensitive surface — API shapes that changed
  across versions, dtype/device gotchas, pins that matter. This is exactly what pairs with `env_inspect`:
  the **note says what to watch for; `env_inspect` confirms what's installed.**

### Code seams (corrected — this is a small build, not zero-code)

| Concern | Seam | Change |
|---|---|---|
| Storage/format | `knowledge/{frameworks,libs}/*.md` | New dirs — no code |
| **Note frontmatter** (version_range, type, confidence) | `KnowledgeWriteTools`/`KnowledgeTools` (`knowledge_tools.py:163-189, 220-265`) | **Real, small change**: add YAML-frontmatter parse/emit — **borrow the Skills machinery** (`_parse_skill`, `write_auto_skill`) which already does exactly this |
| Indexing/retrieval | `KnowledgeTools._build_index`/`_records` (`knowledge_tools.py:220-265`) | Reuse; add a `type: framework\|lib` tag so the router can prefer it on Developer queries; optionally a per-facet index (ADR-10 "separate indices") |
| Live-truth companion | `tools/env_inspect.py` | Unchanged; **document the pairing** in the Developer prompt (curated note ↔ live introspection) |
| Agent access | `kb_search`/`read_note` (+ `use_skill`) | Already exposed (`kb_search` = flat top-k=3 + 1 anchor hop, `knowledge_tools.py:282-300`); add a Developer hint to consult `frameworks/`/`libs/` before writing framework code |
| Persistence | `InMemoryVectorStore` (`vectorstore.py:165-201`) | Today the KB re-embeds every run (no persistent store ships; LanceDB is a design-only seam, `vectorstore.py:1-9`). Fine at small corpus; note it if the KB grows |

### Complications

- **Staleness & version-sensitivity.** A note about torch 2.3 is wrong on 2.7. Add a `version_range`
  frontmatter field; prefer `env_inspect`'s *live* version and down-weight out-of-range notes. **Never let a
  curated note override live introspection** — `env_inspect` is truth.
- **Context-rot / distractors.** ADR-10 point 2: *do not merge curated knowledge with distractor-rich
  ingested RAG in one index.* Today everything flattens into one "kb" index (`knowledge_tools.py:233-265`) —
  keep `frameworks/`/`libs/` curated-tagged and separable.
- **Curation cost & poisoning.** A wrong framework note *confidently* misleads the Developer — worse than
  none. Reuse ADR-10 gating (confidence, `candidate→trusted`, mark-invalid ledger) and the poisoning filter.
  Skills' `status: candidate|promoted` frontmatter is the pattern to copy.
- **Scope creep.** This is *curated guidance*, not a docs mirror. Keep notes short (guidance lives in the
  note, code in the Skill, API in `env_inspect`); past ~200k tokens the ADR-3 "skip RAG, load in-context"
  heuristic flips.

### Synergy

**The substrate the other three read from** — the ADR-10 semantic tier finally populated for the *tools of
the trade*. It pairs with `env_inspect` (curated wisdom × live truth), with Skills (guidance × runnable
recipe), and it is exactly the corpus NapMem (§9) navigates and that a broadened Researcher (§11) needs to
propose *cross-framework* ideas instead of staying in one library's rut.

## 9. NapMem — navigable memory pyramid (arXiv 2607.05794, Jul 2026)

**What it is** *(snippet + secondary-review sourced)*. "From Passive Retrieval to Active Memory Navigation:
Learning to Use Memory as a Structured Action Space." It reframes long-term memory from **flat top-k
retrieval** into a **linked multi-granularity pyramid**: **raw conversations** (evidence) → **typed memory
records** (compact facts/preferences) → **topic tracks** (cross-session aggregation) → **user profiles**
(stable summaries), connected by **provenance relations** (each level links *down* to the evidence it was
distilled from). Each level is a **granularity-specific tool**; the agent is **RL-trained (GRPO)** to choose
which tool given the query + evidence so far — start broad, **drill down**, **stop when enough** — rewarded
for *accurate answer + valid format + appropriate memory use under a tool-call budget*. RL **reduces
unnecessary calls** and *calibrates* use (not "retrieve more"). Competitive on **PersonaMem-v2, LongMemEval,
LoCoMo**.

### Relation to LoopLab — retrieval is flat top-k almost everywhere (code-verified)

The subagent map is unambiguous: **there is no tiering, no coarse→fine navigation, no pyramid today.** The
only two non-flat behaviors are (a) Memora's *single lateral anchor hop* and (b) Skills' manifest→body
disclosure:

- **`kb_search`** — flat **top-k=3** vector hits, **plus** one anchor-expansion hop *only when a harmonic
  abstractor is wired* (`self.abstract` set); on a legacy/no-abstract index — e.g. the deep-research path
  (`deep_research.py:227`) — it is **pure flat top-k, no hop** (`knowledge_tools.py:282-300`). `k` defaults to
  3 at both production call sites (tests set `k=1`). *(The conditional hop actually **strengthens** "flat
  top-k almost everywhere" on the harmonic-off path.)*
- **`search_lessons` / `recall_notes`** — **pure token-overlap set intersection, no embeddings at all**,
  top-`limit` (`memory_tools.py:68-100`).
- **Memora** (`tools/memora.py`) — indexes an `Abstraction` = `primary` (essence) + `anchors` (cue tags);
  `expand_by_anchors` (`memora.py:241-269`) is **one extra retrieval hop** to "different-primary, shared-cue"
  entries — *lateral cross-links, not a hierarchy* (`Abstraction` has no level field).
- **`retrieve_lessons_harmonic`** (`memory.py:229-278`) builds a *fresh flat index per call*, top-k + one
  anchor hop; `_render_role_prior` then Jaccard-gates (`lessons_priors.py:137`), splices in harmonic recall,
  applies D2 hygiene/ranking, and picks **top-5** (`lessons_priors.py:160-173`).
- **`VectorStore`**: only `InMemoryVectorStore` ships (brute-force cosine, `vectorstore.py:165-201`); **no
  BM25/hybrid here** (RRF lives in `hybrid_merge.py`, and is a *write-path hygiene* tool, not read retrieval).
- **`[[wikilinks]]→graph`: confirmed NOT implemented** — no `[[`-parsing, no `networkx` in the memory code
  (`networkx` appears only as a dep string in `runtime/deps.py:67`). GraphRAG is a deferred ADR-16 seam.

But the *tiers* NapMem wants **already exist as separate stores** — they're just not linked or navigable:
**cases** (winning config, verbatim = evidence) → **meta-notes** (*why* it won, per task) → **lessons**
(generalizable claims) → **skills** (promoted recipe). That is raw-evidence → typed-record → topic-track →
profile, in our vocabulary (`guide/memory.md`). And **ADR-10 point 4 already mandates progressive
disclosure** (manifest → note → detail) — NapMem just makes it an *agent-driven, provenance-linked* action.

### The ready-made seam — a dormant `CaseLibrary`

There is a **`CaseLibrary`** class (`memory.py:514-609`) — VectorStore-backed, with anchor-expanding
`retrieve` (`:578-585`), build-time `_consolidate` of near-duplicates (`:545-576`), and `retain_if_improved`
(`:587-609`) — **defined but never instantiated in production** (the wired one is `JsonlCaseLibrary`, a flat
keyword top-k, `:449-511`). This dormant class is already 80% of a pyramid *tier*: activate/generalize it into
level-aware tiers rather than writing a new store.

### Integration seams

| NapMem piece | LoopLab seam | Change |
|---|---|---|
| Multi-granularity pyramid | the 4 stores (cases/meta-notes/lessons/skills, `engine/memory.py` + `lessons*.py`) | Add **typed provenance edges** case→meta-note→lesson→skill (they exist implicitly at distillation; make them explicit `[[links]]`/anchors) |
| Provenance-linked navigation | `tools/memora.py` `expand_by_anchors` (`:241-269`) | Generalize the 2-level anchor hop into an N-level typed pyramid; anchors → typed provenance edges |
| Level-aware store | dormant `CaseLibrary` (`memory.py:514-609`) + `VectorStore` protocol (`vectorstore.py:37-41`) | Activate/extend it; a summary/manifest index per tier |
| Granularity-specific tools | `KnowledgeTools`/`MemoryTools` tool set | Add drill-down tools: `summary_of(topic)` → `open_note(id)` → `evidence_for(id)`; the tool-using Researcher already picks tools (`agent.py:213-267`) |
| "Appropriate use under a budget" | tool-loop context/cost budget (`context_budget.py`, per-role caps) | Map NapMem's tool-call budget onto our existing budget — **no RL** |

### The RL question — skip it

NapMem's headline mechanism (GRPO-train the model to navigate) **does not fit LoopLab** (inference-time,
backend-agnostic; a PersonaMem-trained policy wouldn't transfer to ML-research memory). **Adopt the
navigable-structure half** — pyramid + provenance tools + drill-down — and let the existing tool-using
Researcher navigate by prompt-guided function-calling. Keep NapMem's *insight* ("memory use is an explicit,
budgeted decision; stop when you have enough") as prompt guidance + the existing budget guard, not a trained
policy. (If we ever fine-tune a local role — §10 angle C — the navigation trajectories become natural
training data, but that's opt-in and external.)

### Complications

- **No RL** = structure without *learned* calibration; navigation quality rides on the base model's tool-use
  judgment. Degrades to today's behavior, only better-structured.
- **Provenance-graph construction cost.** Building typed edges at run-end distillation adds work to
  `engine/memory.py`'s reflection; edges must be derived/rebuildable (like the vector index) and replay-safe.
- **More tool-calls = more latency/cost.** Drill-down is several round-trips vs one top-k — pays only when
  the corpus is large enough that flat top-k pulls distractors (the context-rot case). Gate it; keep flat
  top-k as the cheap default.
- **No persistent *vector* store yet.** Only the **vector index** is ephemeral: with `InMemoryVectorStore` a
  large pyramid re-embeds each run — the LanceDB seam (`vectorstore.py:1-9`) becomes worth building *before* a
  big pyramid, not after. *(To be precise: the **JSONL stores + cross-run reflection priors are ON by
  default** — `memory_dir=~/.looplab/memory` (`config.py:411`), `reflection_priors=True` (`config.py:318`),
  `comparative_lessons=True`; clear `LOOPLAB_MEMORY_DIR` to disable. So the pyramid's *content* persists; only
  its *index* is rebuilt in-memory.)*

### Synergy — highest of the three

NapMem is **almost an ADR-10 revision**: the *navigable* upgrade of the exact tiering + progressive
disclosure + Memora anchors we already committed to, and it directly attacks the **context-rot** failure
ADR-10 exists to avoid. It reads the §8 KB as one more tier. **Recommend: fold NapMem into ADR-10 as the
retrieval-interface upgrade; activate the dormant `CaseLibrary` and ship the pyramid tools first; defer any
RL indefinitely.**

## 10. SciResearcher — scaling deep-research agents (arXiv 2605.01489, May 2026)

**What it is** *(snippet + Moonlight-review sourced)*. Zheng, Wang, Li, Song, Fang. A **fully automated
agentic *data-construction* framework** for frontier science: synthesizes diverse **conceptual + computational**
tasks grounded in academic evidence, eliciting *information acquisition, tool-integrated reasoning, and
long-horizon* capabilities. It trains **SciResearcher-8B** via **cold-start SFT** on agent trajectories from a
**Claude-Sonnet-4.5 teacher** with **rejection sampling**, then **RL**. Results: **19.46% on HLE-Bio/Chem-Gold**
(SOTA at 8B, beating larger proprietary agents), **+13–15%** on SuperGPQA-Hard-Biology and TRQA-Literature.
The contribution is a **paradigm for automated training-data construction** — not a runnable framework.

### Relation to LoopLab — a model + a data pipeline, not a framework

Three reasons it's **not** a drop-in: it yields a **trained 8B model + a data-synthesis pipeline**, not an
orchestrator (we're backend-agnostic, ADR-7 — we don't ship/train a model); its **domain is bio/chem
reasoning**, not ML-engineering on a metric harness; and its "scaling" is **training-data scaling**, not
test-time search scaling (which for us is ADR-6's throughput lever).

### Integration angles, cheapest first — and the concrete win

- **(A) Backend/model option — S, config-only.** *If* SciResearcher-8B ships under a usable license, wire it
  as a cheap **local** backend for the deep-research pass via LiteLLM (`roles.*.model`, ADR-7). Caveat:
  bio/chem tuning may not transfer to ML ideation — validate before trusting; likely a *deep-research/grounding*
  backend, not the MLE Researcher.
- **(B) Turn deep research from introspective to literature-expanding — S, the real win.** The subagent map
  found the decisive gap: `deep_research` **defaults `web_search=False` and `literature_search=False`**
  (`config.py:691,695`); `LiteratureTools`/`WebTools` are only wired when on (`deep_research.py:214-256`), and
  the foresight ranker's tools never include web at all (`cli/__init__.py:169-178`). So **by default the
  research memo is grounded in the run's own experiments + local knowledge — not external literature.** That is
  the *opposite* of SciResearcher's "information acquisition" pillar. The single highest-ROI SciResearcher-
  flavored change is: **default literature/web on for the deep-research pass** and give it a **broader
  tool-integrated reasoning budget** (`max_turns`/`emit_after`/`emit_force`, `deep_research.py:118-121`). The
  plumbing to *act* on the output already exists — when `track_hypotheses` is on (default True) the first 5
  non-empty `recommended_directions` become OPEN hypotheses (`research_cadence.py:130-136`), and all top-5
  also surface as a standing operator hint (`research_cadence.py:122-126`). Seam: `make_deep_researcher`
  (`deep_research.py:214`) + the two default flags.
- **(C) Self-distillation from our own `events.jsonl` — L, opt-in, out of core scope.** The teacher→
  trajectory→rejection-sampling→SFT recipe *could* fine-tune a cheap local role from **LoopLab's own** event
  log — the event log records proposals/patches/verdicts, *plausibly* a trajectory corpus with gated rewards
  from the trust layer (exactly the signal rejection-sampling needs). **This premise is unverified — it needs
  an events.jsonl schema audit** (are proposal + patch + verdict + reward co-located per node with the linkage
  SFT needs?) before treating the data as in-hand. Genuinely synergistic *if* it holds, but a **training
  project**, not an inference-engine feature; it also risks the "recursively train on un-curated self-output"
  trap ADR-10 point 3 warns against (mitigated by our *gated* verdicts, still a hazard). Park as a research
  direction.

### Complications

- **Availability/licensing unknown** (angle A). **Domain transfer** may hurt, not help — A/B on our tasks.
- **Training is out of scope** (angle C): needs a training stack, GPU budget, curation/reward pipeline.
- **Don't delegate the loop** (ADR-7 rule 1): even a strong SciResearcher-8B backs a *step* (the deep-research
  pass), never the research loop.

### Synergy — modest, concentrated at the deep-research stage

Bounded but real: (A)+(B) plug into `agents/deep_research.py` + the ADR-7 backend seam, and (B) is a
genuinely cheap, high-value change (flip two defaults + widen a budget) that makes the literature-grounded
half of LoopLab behave like SciResearcher's information-acquisition pillar. (C) is the tantalizing long-shot —
*we already own the trajectory+reward data such a pipeline needs* — but it's a separate endeavor.
**Recommend: (i) turn on literature/web + widen the deep-research budget now; (ii) benchmark SciResearcher-8B
as a deep-research backend if released; (iii) shelve self-distillation as a research direction.**

## 11. AI Research Agents Narrow Scientific Exploration (arXiv 2605.27905, May 2026)

**What it is** *(HF-page + snippet sourced)*. Tang & Yang. An **empirical diagnostic**: 4 AI research-agent
frameworks × 6 LLMs generate **37,802 ideas** from shared seed literature across citation-defined AI/ML areas,
vs human papers from the same areas. **Four consistent patterns:** (1) AI ideas are **substantially more
concentrated** than human papers; (2) they stay **much closer to the seed literature** than human follow-on
work; (3) papers most similar to AI ideas get **lower subsequent citations**; (4) when AI ideas differ, the
difference is mostly **recombining existing methods**, not **new research questions**. **Conclusion: current
agents are better at *local elaboration* than *broadening exploration*.** (Diagnostic — supplies *metrics*,
not a fix; concurrent related work: *Heuresis* 2606.25198 on quality/diversity/novelty search.)

### LoopLab already has this pathology — and already cites this paper

The subagent map found the smoking gun in two places:

- **The narrowing is (partly) baked into prompts.** `ToolUsingResearcher`'s system prompt literally says
  *"Work FOCUSED, not scattered: pick the most promising direction... and RESEARCH THAT"* (`agent.py:103-117`),
  and `_state_brief` opens with the goal + optimize-direction, then **foregrounds the current best + parent
  *when they exist*** (`roles.py:307-311`; both are absent in the first-seed phase, so it doesn't *always*
  lead with them). It leans toward the leader — finding #4 as a design choice — but it *does* also carry an
  always-on **sibling-diversity digest** (`roles.py:320`), so the narrowing is a tendency, not absolute.
- **`search/coverage.py` already cites arXiv 2605.27905** in its docstring (`coverage.py:16-19`) and computes
  a concentration signal — `themes`, `niches`, `theme_entropy`, `dominant_theme_frac`, `recent_dominant_frac`
  (`coverage_signal`, `coverage.py:50-100`). **So the metric this paper implies is already implemented.** It
  never drives **node selection** (no policy reads it — confirmed) — but it is *not* inert: the recorded
  snapshot is read by `proposal_cues.py:104-111` to inject an EXPLORE "broaden the space" directive into the
  researcher's **proposal prompt**, *once the Strategist stance has flipped to `explore`*. So it already
  shapes proposal content — **reactively, post-collapse** (see below), which is exactly the gap: the fix is to
  make that trigger *proactive*, not to add a first consumer.

### The honest tension with ADR-6 — and its resolution

ADR-6 **demoted** the diversity archive + fancy policies as *"unproven on MLE-bench; greedy + good operators
wins."* This paper says agents *systematically narrow*. **Not a conflict — different objectives:**

- **Fixed-metric mode (MLE-bench).** The metric *is* the goal; local elaboration *is* the win. ADR-6 correct.
- **Open-ended mode (Genesis, `deep_research.recommended_directions`, open dataset tasks, cross-run research).**
  No fixed metric; value = genuinely novel directions. **Here the Narrow-Exploration finding bites**, and the
  parked diversity/novelty machinery becomes load-bearing again.

So the paper **re-validates the demoted machinery, scoped to open-ended mode** — a *scoping* of ADR-6, not a
reversal.

### What's built vs missing (code-verified)

| Lever | Status today | Gap |
|---|---|---|
| Concentration metric | **Built** — `coverage_signal` (`coverage.py:50-100`), folded every cadence | Within-run *theme* concentration only (not distance-from-seed-*literature*); drives **proposal content** only reactively (via `proposal_cues.py:104-111` once stance=`explore`), never **node selection** |
| Novelty gate | `_llm_novelty_gate` default (`novelty.py:70-131`) — **within-run dedup** ("already tried in THIS run"), prefers NOVEL only vs repeats; doesn't hard-reject (worst case keeps the original) | No notion of "too close to the seed literature"; `"algo"` semantic gate off by default (`config.py:298`) — but *fires* whenever the Strategist flips stance to `explore` |
| Diversity archive | `DiversityArchive` (`archive.py:12-46`) — **audit-only** ("never affects selection", `models.py:323`); build() feeds only the `niches` count into `coverage_signal` | No MAP-Elites "expand an empty niche" operator |
| Selection diversity | Only `GreedyTree`'s **IMPROVE** arm targets `state.best()` (`policy.py:286`); parent-selection diversity lives in `weighted_parent` (`policy.py:133`, used by `EvolutionaryPolicy`), `MCTSPolicy` (`policy.py:369`), ASHA/BOHB (`policy.py:450`) — **all off by default** (`policy=greedy`) | Default is exploitation on the IMPROVE arm; **the agentic Strategist *can* switch policy at runtime** (`agent_control`) — reactively |
| Broaden lever | Strategist `novelty_stance=explore\|balanced\|exploit` (`strategist.py:50-68`) — **the main dial**; the default `strategist_backend='agent'` (`config.py:377`) governs it live | **Reactive**: `_rule_novelty_stance` flips to `explore` only *after* concentration ≥0.6–0.75 (`strategist.py:145-160`), i.e. after collapse; stall logic keys on metric stagnation, blind to coverage collapse (`strategist.py:305-324`) |
| Diverse seeding | Genesis authors *what to solve*, not an idea portfolio (`engine/genesis.py:161-238`); seeds = 3 blind drafts (`search/policy.py:225-227`) | No "generate N orthogonal seed directions" step |

### Integration seams

| Finding → lever | Seam | Change |
|---|---|---|
| Concentration is measurable | `coverage_signal` (`coverage.py:50-100`) — already computed | **Surface it as a KPI** (dashboard) and **make the Strategist trigger proactive**, not post-collapse |
| Distance-from-seed | `engine/novelty.py` + the seed embeddings from Genesis/ingestion | Add a **distance-from-seed** term (reuse the `_embedder`/`HybridRetriever` plumbing already present); degrade to distance-from-archive on `--no-genesis` |
| Question-novelty vs method-recombination | `engine/novelty.py`, `idea.hypothesis` field | Embed the idea's *question/hypothesis* separately from its *method*; stop scoring pure recombination as "novel" |
| Diversity in selection | `GreedyTree.next_actions` (`policy.py:172-288`) + `DiversityArchive.build()` (`archive.py:20`) / `weighted_parent` (`policy.py:133`); ASHA/BOHB rung-racing (`policy.py:450`) already exist as non-greedy parent selection (off by default) | Reserve a **breadth quota** (every Nth node a forced-divergent draft) or a niche-expansion action — **open-ended mode only** |
| Ranker breadth (already partial) | `search/foresight.py:213` (`_novelty_rank_directive`) | The foresight panel (default **on**, `foresight_panel=2`) *already* breaks near-ties toward the **more divergent** candidate — but **only under `explore` stance**. Make that divergence directive always-on / raise the panel in open-ended mode |
| Proactive, not reactive | `_rule_novelty_stance` / Strategist prompt (`strategist.py:145,353`) | Lower/invert the collapse thresholds; add a coverage-collapse stall trigger (today it's metric-only) |
| Broaden at entry | `engine/genesis.py` + the board (`hypothesis_added` + `_prioritize_board`) | A seed-phase "portfolio generation": emit N deliberately-orthogonal seed hypotheses (the board machinery already carries them) |
| Broaden at ideation | `agents/deep_research.py` `recommended_directions` (auto-become hypotheses, `research_cadence.py:130-136`) | Diversify the directions set; compounds with §10-B's literature-on change |

### Complications

- **Mode-gating is mandatory.** Diversity pressure **off** in fixed-metric mode, or it trades MLE-bench score
  for breadth — the exact regression ADR-6 warned about. Gate on task kind / open-ended flag.
- **"Novel" ≠ "good."** Naively maximizing distance-from-seed surfaces low-quality far-out ideas. Pair with the
  foresight quality estimate / trust layer — **quality-diversity** (why MAP-Elites, not random jitter), not
  diversity-at-any-cost. (Finding #3 is about AI ideas being *derivative*, not novelty causing low quality.)
- **Question-vs-method novelty** needs a representation we only *partly* have; the `idea.hypothesis`/method
  split is a feasible first cut, imperfect.
- **Seed anchor availability.** Distance-from-seed-literature needs a seed embedding set (present after
  Genesis/ingestion, weak on bare `--no-genesis`); degrade to distance-from-archive.

### Synergy — high in open-ended mode; and it hands us a KPI already half-built

The most *conceptually* aligned of the three with LoopLab's stated ambition (an autonomous *researcher*, not
just an MLE-bench climber). It (a) **re-justifies** the parked machinery for open-ended mode, (b) hands us a
**concentration KPI that is already computed** — the work is to *act* on it (proactive Strategist trigger +
dashboard) and to add *distance-from-seed*, and (c) composes with §8 (a broad KB → cross-framework
directions) and §9 (a navigable pyramid surfaces far-from-seed precedents). **Recommend: make the existing
`coverage_signal` proactive (KPI + collapse trigger) + add distance-from-seed first (cheap, immediately
useful), then re-activate quality-diversity selection behind the open-ended-mode gate.**

## 12. How the four compose + deep-dive recommendation

They stack — substrate → navigation → objective + reasoner:

```
  ┌────────────────────────────────────────────────────────────────────┐
  │  §8 · Frameworks/Libs KB  →  the curated SEMANTIC SUBSTRATE         │
  │       (what the tools of the trade can do; paired with env_inspect) │
  └───────────────┬────────────────────────────────────────────────────┘
                  │ read by
  ┌───────────────▼────────────────────────────────────────────────────┐
  │  §9 · NapMem pyramid      →  HOW you navigate that substrate        │
  │       (agent drills summary→note→evidence across provenance tiers)  │
  └───────────────┬────────────────────────────────────────────────────┘
                  │ feeds ideation
  ┌───────────────▼──────────────────────┐   ┌─────────────────────────┐
  │  §11 · Narrow-Exploration             │   │  §10 · SciResearcher    │
  │  the OBJECTIVE: use memory to BROADEN, │   │  a reasoner/loop over it│
  │  not narrow (coverage KPI, already     │   │  (turn literature ON;   │
  │  built → make proactive; quality-      │   │  optional 8B backend;   │
  │  diversity, open-ended mode)           │   │  patterns not framework)│
  └────────────────────────────────────────┘   └─────────────────────────┘
```

- **§8 → §9:** the KB is one more tier NapMem navigates; a bigger, better-organized memory is what makes
  navigation (vs flat top-k) pay off.
- **§9 → §11:** navigating a provenance pyramid surfaces *far-from-seed precedents* the Researcher misses under
  flat top-k — mechanically counteracting concentration.
- **§10 → §8/§11:** turning on literature in deep research (§10-B) *fills* the KB with grounded cited claims
  **and** supplies the far-from-seed material a broadened Researcher (§11) needs — the two changes reinforce.
- **§11 governs the budget:** the coverage KPI tells the Strategist *when* to spend navigation/deep-research
  budget on distant regions vs exploit the leader.

### Consolidated deep-dive recommendation

Priority-ordered, each mapped to an existing seam; none replaces the engine. *(These "rec N" items detail dirs
1–4 from the master §6.2 list; "Rec #N" references in §13 point here.)*

| # | Item | Source | Seam | Effort | Mode | Why now |
|---|---|---|---|---|---|---|
| 1 | **Turn deep research literature-expanding** (default `literature_search`/`web_search` on + wider tool budget) | SciResearcher (§10-B) | `deep_research.py:214`, `config.py:691,695` | S | opt-in default | Flip two flags; plumbing to act on directions already exists; fills the KB + broadens ideation |
| 2 | **Make the coverage trigger proactive** (dashboard KPI + pre-collapse trigger; add distance-from-seed) | Narrow-Exploration (§11) | `coverage.py:50`, `strategist.py:145`, `proposal_cues.py:104`, `novelty.py` | S–M | KPI all / action open-ended | The metric is *already computed* and already drives a *reactive* explore-hint (`proposal_cues.py:104-111`); the gap is firing it **before** collapse, not after |
| 3 | **Frameworks/Libs KB** (`knowledge/{frameworks,libs}/` + note frontmatter) | общая БЗ (§8) | `knowledge_tools.py:163-265` (borrow Skills frontmatter) | S–M | all | Substrate for 1/4/5; small build (frontmatter parse), not zero-code |
| 4 | **NapMem pyramid tools** (activate dormant `CaseLibrary`; provenance drill-down; no RL) | NapMem (§9) | `memory.py:514-609`, `memora.py:241-269`, kb tools | M | large-corpus / open-ended | ADR-10 retrieval upgrade; attacks context-rot; a store is already 80% built |
| 5 | **Quality-diversity selection re-activation** (breadth quota / niche-expansion; make the `foresight.py:213` divergence tie-break always-on) | Narrow-Exploration (§11) | `policy.py:172-288` (+ ASHA/BOHB `450`), `archive.py:20`, `foresight.py:213` | M | open-ended only (gated) | Re-justifies ADR-6-parked machinery; foresight already nudges divergent *under `explore`* — just make it proactive |
| 6 | **SciResearcher-8B as a deep-research backend** (if released) | SciResearcher (§10-A) | `roles.*.model` (ADR-7) | S | opt-in | Config-only; benchmark before trusting (domain mismatch); *paper claims unverified* |
| 7 | **Self-distillation from `events.jsonl`** (fine-tune a local role) | SciResearcher (§10-C) | *external training project* | L | research direction | Event log *plausibly* holds proposal+patch+verdict trajectories with gated rewards — **needs a schema audit** before treating as in-hand; training breaks backend-agnosticism → external only |

## 13. Verification pass & corner cases (deep-dive, 2026-07-11)

Every code claim in Part III was re-checked by an **8-agent adversarial pass** (7 claim-cluster verifiers + a
config-profile/env sweep, then a completeness critic) — each opened the source, tested the `file:line`
anchors, and hunted exceptions to "always/never/only/everywhere" and to "default off/on." **The load-bearing
claims held (18 of 30 CONFIRMED verbatim);** the rest were tightened and the corrections folded into the body
above. The corners worth knowing:

**A. The single biggest corner — "default off" ≠ "unreachable": the agentic Strategist is a live override
surface.** `strategist_backend='agent'` by default (`config.py:377`), and the governance matrix
(`DEFAULT_AGENT_CONTROL`, `config.py:70-85`) grants it live control of `policy`, `novelty_stance`,
`ablate_every`, `merge_mode`, `fidelity`, … So the exploration machinery the doc calls "off by default" is
**static-config off but Strategist-mutable at runtime** — and it flips **reactively, post-collapse**
(`novelty_stance`→`explore` only at concentration ≥0.6–0.75, which in turn switches on the `novelty_semantic`
gate *and* the `proposal_cues` broaden-hint). This *reinforces* the Narrow-Exploration thesis (the levers
exist; they fire too late) and sharpens Rec #2/#5 from "add machinery" to "make the existing trigger proactive."

**B. Config profiles do not flip any "default" the doc relies on.** `PROFILES` (`config.py:43-64`):
`default`/`fast` are empty `{}`; `thorough` turns on quality/trust machinery (`confirm_*`, `reward_hack_detect`,
`code_leakage_detect`, `critic_check`, `trust_gate=gate`, `ablate_every=3`, `operator_bandit`, `complexity_cue`,
`budget_aware`) but **deliberately leaves OFF** the novelty algo-gate (comment: it "mis-fired" — nudged a fresh
idea onto a twice-dead lineage), `novelty_semantic`, the `explore` stance, a diversity `policy`, and
`web_search`/`literature_search`. So **`web/literature off` (Rec #1), `novelty_semantic off`, `policy=greedy`
hold under every shipped profile** — env vars can flip them, no *profile* does. *(Two `thorough` keys —
`failure_reflection`, `reflection_priors` — are redundant no-ops, already default True.)*

**C. Corrections folded into the body:**

| Claim | Was | Now (corrected) |
|---|---|---|
| `kb_search` k / hop (§9) | "k=3, never overridden; + one anchor hop" | k=3 at the two *production* sites (tests set k=1); the anchor hop fires **only** with a harmonic abstractor — the deep-research path (`deep_research.py:227`) is pure flat top-k (this *strengthens* the thesis) |
| coverage (§11, Rec #2) | "context, never a decision; fed to Strategist, nothing more" | never drives *selection* — but already drives the *proposal* explore-hint via `proposal_cues.py:104-111`, **reactively**; the gap is making it *proactive*, not adding a first consumer |
| `_state_brief` (§11) | "always leads with best+parent" | best/parent are conditional (absent in the first-seed phase); the brief also carries a sibling-diversity digest — a tendency, not absolute |
| `GreedyTree` (§11) | "always improves `best()`; selection diversity only in Evo/MCTS" | only the IMPROVE arm targets `best()`; it also drafts/debugs/ablates/merges-top-2; ASHA/BOHB are further non-greedy paths (all off by default) |
| `recommended_directions` (§10) | "auto-become hypotheses" | gated on `track_hypotheses` (default True), first 5 non-empty only; also surfaced as a standing hint |
| Skills frontmatter (§8) | "parsed by `_parse_skill`" | `_parse_skill` reads only name+description; the other fields are parsed by `write_auto_skill`'s own regex |
| Cross-run memory (§9) | (persistence framing implied ephemeral) | only the *vector index* is ephemeral; the JSONL stores + reflection priors are **on by default** |

Anchor drift corrected in two places: the lessons Jaccard gate is `lessons_priors.py:137` (not 160-173); the
deep-research budget knobs are `deep_research.py:118-121`.

**D. Two claim classes the code-only verifiers structurally COULD NOT check — read these as contingent:**
1. **Paper content.** All three papers' quantitative figures (NapMem GRPO/benchmarks; SciResearcher-8B 19.46%
   HLE-Bio/Chem-Gold; the 37,802-idea study) and the **arXiv IDs themselves** are snippet-sourced and
   unresolved this session — the *only* independently corroborated paper fact is that `coverage.py` cites
   2605.27905. **Rec #6/#7 are contingent on the paper claims holding.**
2. **`events.jsonl` as a training corpus** (Rec #7 premise) carries no `file:line` anchor and needs a schema
   audit before "we already own the data" is safe.

**E. A default the doc had understated:** the **foresight ranker is a default-ON stage** (`foresight=True`,
`foresight_panel=2`), and its `foresight.py:213` tie-break *already* nudges toward the more-divergent
candidate — but only under `explore` stance. Rec #5 now points at it as an existing, partial breadth lever to
make *proactive*, not a fresh build.

**Net:** no recommendation was invalidated. Rec #2 and #5 were **sharpened** (the levers exist and fire
reactively — make them proactive), Rec #7 was **hedged** (needs a data audit), and the "flat top-k almost
everywhere" thesis was **strengthened** (the one hop the doc counted is conditional). The honest through-line:
LoopLab's exploration-breadth machinery is *present but reactive/post-collapse*, which is exactly the
Narrow-Exploration pathology — so the fixes are mostly about *timing and gating*, not new subsystems.

---

## Sources & method

- **This review's inputs:** parallel code-grounded deep-dives (functionality/capability audit; test-coverage &
  code health; a directions synthesis over `docs/`; and the four-direction integration study), each anchored
  to `file:line` and verified against `looplab/` on this branch, plus an 8-agent adversarial re-verification
  pass over Part III (§13).
- **Complements:** [16-architecture-code-review-2026-07-11.md](16-architecture-code-review-2026-07-11.md)
  (revalidated tactical audit, current P0–P3 priority ledger, old-finding disposition, and validation
  evidence), [13-external-works-analysis-2026-07.md](13-external-works-analysis-2026-07.md)
  (MARS/AgentDS/AgentRxiv), [ROADMAP.md](ROADMAP.md) / [BACKLOG.md](BACKLOG.md) (the shipped record).
- **Framing:** [01-product-design.md](01-product-design.md) §3 (goals/non-goals), [ADR-6](03-decisions.md)
  (2026 positioning: differentiate on operators / evaluation rigor / ensembling / leakage-safety),
  [ADR-9](03-decisions.md) (MCP + Skills), [ADR-10](03-decisions.md) (unified knowledge/memory),
  [guide/memory.md](guide/memory.md).
- **Papers (Part III — snippet-sourced, arXiv-proxy-blocked, unverified this session):** NapMem *From Passive
  Retrieval to Active Memory Navigation* — [arXiv:2607.05794](https://arxiv.org/abs/2607.05794); SciResearcher
  *Scaling Deep Research Agents for Frontier Scientific Reasoning* — [arXiv:2605.01489](https://arxiv.org/abs/2605.01489)
  ([themoonlight.io review](https://www.themoonlight.io/en/review/sciresearcher-scaling-deep-research-agents-for-frontier-scientific-reasoning));
  *AI Research Agents Narrow Scientific Exploration* — [arXiv:2605.27905](https://arxiv.org/abs/2605.27905)
  ([HF page](https://huggingface.co/papers/2605.27905)); related *Heuresis* — [arXiv:2606.25198](https://arxiv.org/abs/2606.25198).
- **Caveat:** this strategic pass was static because pytest was unavailable in its environment. Doc 16's
  separately executed evidence is 1,711 passed / 33 skipped / 1 failed on each Linux interpreter, plus
  focused Windows, broader platform, UI, and static-analysis results; see its §9 for exact scopes.
