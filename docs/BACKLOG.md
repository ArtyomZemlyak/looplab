# LoopLab — Consolidated Development Backlog

**Date:** 2026-06-24 · One flat, status-tagged checklist that merges (a) the remaining
[code-review](CODE_REVIEW.md) hardening and (b) the [strategic roadmap](ROADMAP.md) Themes A–I, with
*verified current status in the code*. This is the actionable tracker; ROADMAP.md is the narrative/why.

**Status legend:** ✅ done · 🟡 partial (some sub-items done) · ⬜ todo
**Priority:** P0 (do next) · P1 · P2 · **Effort:** S / M / L

---

## ★ Shipped 2026-06-24 (this session) — ~43 roadmap items, config-first, all in the UI

Branch `feat/adaptive-search-intelligence`, ~30 commits. All **config-first** (every knob in
`config.Settings` + the Settings UI), **replay-safe**, surfaced in the UI (new Strategist /
Importance / Cross-run / Collab panels, Pareto/Trust additions, chips, activity narratives, Model-card
+ Notebook exports). Full suite **413 passed, 5 skipped**; live-verified (toy, ASHA, BOHB, surrogate,
proxy, time-series, classification, a live `qwen3:30b-a3b` run, UI preview of every new panel).

- ✅ **Theme A — search intelligence (complete):** A7 Strategist · A1 ASHA · A2 surrogate · A3 BOHB ·
  A4 failure-reflection · A0a code-block ablation · A0b ensemble merge · A0d complexity cue ·
  A5 budget-aware · A6 proxy scoring.
- ✅ **Theme B — trust:** B1 host-side scoring (`host_score`) · B3 output redaction · B4+ gVisor
  hostile tier · B5 reward-hack detector. *(B6 parked per user.)*
- ✅ **Theme C — Developer:** C1 fault localization · C2 best-of-N · C3 deep repair · C4 critic.
  *(C6 ACI: largely covered by the patch-gate / whole-file-write.)*
- ✅ **Theme D:** D2 `looplab bench` · D3 classification adapter · D4 data provenance.
- ✅ **Theme E:** E1 novelty gate · E2 researcher panel · E3 literature grounding · E4 reflection priors.
- ✅ **Theme F:** F1 importance · F2 cross-run sweep · F3 model-card · F4 collab · F6 fork-to-branch.
- ✅ **Theme G:** G1 server auth token · G3 parallel-eval budget guard · G5 MLflow export.
- ✅ **Theme H (complete):** H1 guided_json · H2 schema-aligned parser · H3 per-role models · H4 ctx budget.
- ✅ **Theme I:** I1 feature-engineering · I2 time-series adapter · I3 code-leakage · I4 notebook export ·
  I5 Pareto selector.

**Still open** (external-infra-gated): **D1 real MLE-bench** (needs Kaggle creds + dataset download) +
the **out-of-process grader** (a careful eval-loop refactor — B1 `host_score` is the scoring primitive
it builds on). B6 parked per user decision.

---

## 0. What concurrent sessions already shipped (verified in code, commit range `f98b1fb…42d5fc5`)

**Code-review fixes landed:**
- ✅ **C1 (partial)** — `RepoTask._eval_protected` now protects *every* file-based reader (primary
  metric + `metrics` + `constraints` + drift `cross_check`); protected-name normalization (`_normp`);
  Docker `--pids-limit 1024` on both untrusted paths. *(commit 9722226)*
- ✅ **C2 (partial)** — child process no longer inherits host secrets: `sandbox._run_argv` filters
  `_SECRET_ENV`-matching vars out of the child env ([sandbox.py:112](../looplab/runtime/sandbox.py)).
- ✅ **C3 (partial)** — CORS narrowed from `*` to a localhost allow-list (`LOOPLAB_UI_CORS` override);
  SPA fallback `GET /{path:path}` now resolve-guards against traversal ([server.py:739](../looplab/serve/server.py)).
- ✅ **C4 (partial)** — `replay.fold` is idempotent for terminal node events (duplicate
  `node_evaluated/node_failed` can't inflate `total_eval_seconds`).
- ✅ **G4 (partial)** — `llm._post` now catches `URLError/HTTPError/TimeoutError/OSError` + JSON decode
  errors instead of aborting the run ([llm.py:72](../looplab/core/llm.py)).
- ✅ **F5 (partial)** — Dock `'Reasoning'`→`'Trace'` tab regression fixed (both call sites).

**Roadmap expanded** (commits b0d7628 → 42d5fc5): ROADMAP.md + RESEARCH_NOTES.md added; **9 research
passes** (AI-ML-engineering frontier re-run verified 3-0); plan reprioritized **operators-first (A0)**,
new **B6 held-out/generalization-gap**, **A6 proxy scoring**, **Theme H** (local-LLM serving on the
5090), **Theme I** (net-new: feature-eng, adapters, data-centric, integrations). **UI parity panels**
added: live GPU monitor, policy "why-this-node" (MCTS UCB1), pending-hint feedback chip *(commit 42d5fc5)*.

---

## 1. Foundation — remaining hardening (finish before scaling)

*These gate credible benchmarks + any non-local deployment. Ordered by priority.*

- ⬜ **P0 · B1 host-side scoring + read-only eval mount (S–M).** Mount inputs `-v root:/work:ro` +
  separate writable `out/`; candidate writes `predictions.json`, host scores it. *Closes the rest of
  C1 — self-reported metric is still trusted on the default path.* → `command_eval.py`, `sandbox.py`.
- ⬜ **P0 · mlebench out-of-process grader (M).** Grader/`_Y` answer key still runs *in the candidate's
  interpreter/workdir* ([mlebench.py:102](../looplab/adapters/mlebench.py)) — `import grader; grader._Y` leaks
  labels. Grade in a separate process; labels never on the candidate FS. *(self-admitted caveat → close it).*
- ⬜ **P0 · C2 output redaction (S–M).** Env is filtered, but `stdout_tail = res.stdout[-500:]` is still
  persisted **verbatim** ([orchestrator.py:808](../looplab/engine/orchestrator.py)) — a `print(secret)` or
  traceback still leaks into the event log/UI. Add a redaction pass (regex + entropy) before write.
- ⬜ **P0 · C3 auth token on mutating `/api/*` + `task_file` allow-list (S).** CORS+SPA are fixed, but
  endpoints are still **unauthenticated** and `task_file` from the request body is executed without an
  allow-list ([server.py](../looplab/serve/server.py)). Add a shared-secret token + path validation.
- 🟡 **P1 · C4 finish (M).** Idempotent fold ✅; still TODO: **read/enforce `Event.v`** (a v2 log read
  by v1 silently mis-folds) + **fail-loud append lock** (still `except OSError: pass` →
  [eventstore.py:38](../looplab/events/eventstore.py)) + a real multi-process append-race test.
- ⬜ **P1 · C5 read-model integrity (M).** SQLite rebuilt only at exit, non-atomically, no seq
  watermark, never refreshed for post-run control events → can diverge undetectably. Rebuild to temp +
  `os.replace`; stamp max `seq`; refresh on append. → `readmodel.py`.
- ⬜ **P1 · G4 finish (S–M).** LLM `_post` ✅; still TODO: reuse `_kill_tree`/process-group in
  `cli_agent` (timeout orphans grandchildren) + guard `choices[0]` envelope.
- 🟡 **P1 · B4 sandbox hardening (M).** `--pids-limit` ✅; add `--read-only`+tmpfs, `--memory`/`--cpus`,
  `--cap-drop ALL`, `--user`, `no-new-privileges`; Windows Job Object for atomic tree-kill; bounded
  in-flight output (kill-on-exceed).
- ⬜ **P2 · B4+ true-isolation tier (L).** gVisor/Kata/Firecracker microVM (`hostile` tier) — verified
  (3-0): shared-kernel hardening is *not* an isolation boundary for untrusted LLM code.
- ⬜ **P2 · F5 remaining UX debt (S).** `delete_run` still `ignore_errors=True` (silent partial-delete →
  [server.py:245](../looplab/serve/server.py)); `layoutWithGroups` cycle guard; SSE/Dock O(n²) full-log
  refetch per tick; SSE `JSON.parse`/listener-leak guards; `RegistryPanel` min/max sort by direction.

---

## 2. Capability roadmap — flat checklist (Themes A–I)

### Theme A · Operators & search intelligence  *(do A0 first — operators are the verified bottleneck)*
> **Principle (user decision): config-first, strategist-optional.** Every operator/policy/allocator
> below is **individually configurable** (enable/disable + params); manual control is the default.
> The optional **A7 Strategist** adapts those choices at runtime but never hides a knob.
- ⬜ **P0 · A0a code-block ablation → targeted refinement (M).** Extend `_ablate` from *params* to
  *pipeline code blocks* (MLE-STAR, 64% MLE-bench-Lite). *LoopLab is one extension away.* + config knobs.
- ⬜ **P0 · A0b real merge/ensembling (M).** Replace mean-param `merge_idea` with code-recombination +
  agent-proposed iterative ensembler (verified: no-ensemble 37.9%→43.9%; removing merge −9pp).
- ⬜ **P1 · A0c operator-scoped memory (S–M).** sibling-recall for draft/improve, ancestral debug-chain
  for debug (port aira-dojo `MEM_OPS` shape).
- ⬜ **P1 · A0d complexity cue by node breadth (S, quick win).** Prompt hint keyed on child count.
- ⬜ **P1 · A0e multi-turn ReAct debug (M).** Replace one-shot `repair` with bounded act/observe loop
  (+5.5 percentile pts). *Ties C3/C5.*
- ⬜ **P2 · A0f web-retrieval-grounded init (M, network-optional).** *Ties E3.*
- ⬜ **P0 · A6 proxy/predictive scoring (M–L).** Early-signal scoring to kill doomed runs (KompeteAI
  6.9× faster eval = current Lite leader 51.5%). Pairs with A1 + C2.
- ⬜ **P1 · A1 multi-fidelity racing ASHA/Hyperband (M).** Successive-halving scheduler over existing
  `eval_profile` smoke/full; emit `rung_promoted`. → `policy.py`.
- ⬜ **P1 · A2 surrogate-guided proposal TPE/RF (M–L).** Fit `(params→metric)`; EI/UCB acquisition.
- ⬜ **P2 · A3 BOHB/DEHB fusion (M).** Capstone once A1+A2 land.
- ⬜ **P2 · A4 LATS-style MCTS (M).** LLM value est + reflection + novelty/dedup.
- ⬜ **P1 · A5 budget-aware proposal (S).** Surface remaining eval budget into the prompt/policy.
- ⬜ **P0 · A7 Strategist role — adaptive meta-control (M rule + M llm) (NEW, user-requested).** Optional
  LLM role that reads run state and **picks the search policy/allocator + Developer mode (agentless vs
  agentic) + operator mix** per situation; every choice is also a direct config knob (config-first).
  Emits `strategy_decision` (audit) + a "why this strategy" panel. **Ship the rule-based baseline
  first** (zero-dep, deterministic), then the LLM variant. Default OFF. → `roles.Strategist`,
  `make_strategist`, config `strategist_backend=off|rule|llm`. *Pairs:* A5/A6/E4.

### Theme B · Trust & eval integrity
- 🅿️ **P2 · B6 held-out test + generalization-gap guard (M) — PARKED IN BACKLOG (user decision).** Still
  the #1 verified *unsolved* problem and the recommended gate before a published D1 MLE-bench number,
  but **deliberately not P0/Phase-1**. A final split the search never sees; fold
  `generalization_gap = val − test`; flag/penalize high-gap winners. → eval contract + confirm panel.
- ⬜ **P1 · B5 reward-hacking detector (M).** Flag suspicious wins (grader import, runtime writes to
  protected paths, val≠host-recompute) → `reward_hack_suspected` event in Trust panel.
- *(B1/B2/B3/B4 tracked in §1 Foundation.)*

### Theme C · Reliable coding Developer (SWE-bench stack)
- ⬜ **P1 · C5 agentless mode = default repo Developer, but agentic stays configurable (M).**
  localize→generate-N→validate; more reliable/stable/cheaper than agent loop (Agentless 32% Lite @
  $0.70). Subsumes C1+C2+C4. **Keep the external coding-agent (agentic) backend as a first-class
  option** — `developer_backend = llm | agentless | <agent>`, selectable by config **or by the A7
  Strategist** per phase/node. Agentic is never removed.
- ⬜ **P1 · C2 best-of-N + selection (M).** N attempts, keep best (SWE-RM best-of-k +10pts). *Depends A1/A6.*
- ⬜ **P1 · C1 fault localization (M).** grep/embedding localization sub-phase (reuse `RepoTools`).
- ⬜ **P2 · C3 deep test-driven repair (M).** Feed failing-test output + minimal repro; cap depth.
- ⬜ **P2 · C4 independent critic (S–M).** Self-consistency/critic before accept. *Ties B5.*
- ⬜ **P2 · C6 better ACI / write-over-edit (M).** Tuned edit/navigate/test interface (SWE-agent finding).

### Theme D · Benchmarks & real tasks
- ⬜ **P1 · D1 wire real MLE-bench (L).** Kaggle download + real grader. *Needs B1+B6.* Highest proof point.
- ⬜ **P2 · D2 self-benchmark harness (M).** N held-out tasks per release; capability regression test.
- ⬜ **P2 · D3 more task adapters (M each).** *(overlaps I2.)*
- ⬜ **P1 · D4 dataset/data-version provenance (S).** Pin data hashes into the run. *(overlaps I3.)*

### Theme E · Idea generation & multi-agent ideation
- ⬜ **P1 · E1 novelty/dedup gate (S–M).** Embedding-similarity reject near-duplicate ideas (reuse vector store).
- ⬜ **P2 · E2 researcher panel + *empirical* ranking (M).** Small panel (≤3); rank by cheap eval/surrogate,
  **not** LLM-judge (verified: LLM-judge ≈random at ranking). Elo-tournament only as a *prior*.
- ⬜ **P2 · E3 literature-grounded ideation (M, network-optional).**
- ⬜ **P1 · E4 reflection-memory → priors (M).** Meta-review note distilled into next run's prompt
  (gradient-free cross-run meta-learning). *Pairs A0c.*

### Theme F · Observability & researcher UX
- ⬜ **P1 · F1 global hyperparameter-importance view (S–M).**
- ⬜ **P1 · F2 cross-run sweep aggregation (M).** Overlay runs of the same task → lab dashboard.
- ⬜ **P1 · F3 lineage/provenance export + model-card (S).**
- ⬜ **P2 · F4 collaboration/sharing (M).** Read-only run links, annotation threads, export-to-report.
- ⬜ **P1 · F6 fork-to-branch from any checkpoint (M).** Fuse time-travel + `inject_node` + reopen into
  one "branch from this seq with edited idea" gesture (top verified steering UX). *Partially in progress.*
- *(F5 UX debt tracked in §1.)*

### Theme G · Scale, ops, hardening
- 🟡 **P1 · G2 replay/durability (M).** *(= C4/C5 in §1.)*
- ⬜ **P2 · G3 distributed/parallel eval (L).** Worker pool + parallel-path budget guard; enables A1 at scale.
- ⬜ **P2 · G5 MLflow/OTLP consumer bridges (M).** *(overlaps I4.)*
- *(G1 server auth, G4 client robustness tracked in §1.)*

### Theme H · Local-LLM serving & structured-output reliability (RTX 5090)
- ⬜ **P0 · H2 schema-aligned (BAML-style) parser as default (S).** Native FC collapses on small models
  (~20% vs SAP ~92–94%). Make the `baml` path a real error-correcting parser. → `parse.py`. *Cheapest
  whole-system lift; gates Themes C/E.*
- ⬜ **P1 · H1 vLLM/SGLang recipe + `guided_json` constrained decoding (S–M).** Drive structured calls
  from the Pydantic schema. → `llm.py`, `parse.py`, docs.
- ⬜ **P1 · H3 per-role model presets (S–M).** Developer=Qwen3-Coder-30B-A3B, fast model for breadth /
  strong for depth; per-role `model`+`base_url`. → `config.py`, `tasks.make_roles`, Settings UI.
- ⬜ **P2 · H4 context budgeting for long traces (S).** Truncate/scoped-memory; paged-KV. *Pairs A0c.*

### Theme I · Net-new capabilities (expand functional surface)
- ⬜ **P1 · I1 LLM feature-engineering operator, CV-gated (M).** CAAFE-style (0.798→0.822); **CV gate
  mandatory** (FE is non-universal). Highest net-new value for tabular. *Composes A0a.*
- ⬜ **P1 · I2 new TaskAdapters (M each).** Time-series (AutoGluon-TS/Darts backtesting), tabular AutoML,
  multimodal. *(overlaps D3.)*
- ⬜ **P2 · I3 data-centric (M).** Static-dataflow leakage detection (beyond exact-match), drift, provenance.
- ⬜ **P2 · I4 integrations (S–M).** Champion→Jupyter notebook export, MLflow autolog, data connectors.
- ⬜ **P2 · I5 true Pareto / cost-aware (M).** Non-dominated-set selector over `extra_metrics` (panel exists).

---

## 3. Top-of-backlog — the ordered "do next" list

**Phase 1 — finish "trust the numbers" (foundation):**
`B1 host-side scoring` → `mlebench out-of-process grader` → `C2 output redaction` → `C3 auth token` →
`C4 finish (Event.v + fail-loud lock)` → `C5 read-model` → `H2 schema-aligned parser`.

**Phase 2 — "better moves, then better search" (differentiation):**
`A0a code-block ablation` → `A0b merge/ensembling` (each config-gated) → `A7 Strategist (rule baseline
→ LLM)` → `A6 proxy scoring` → `A1 ASHA` → `C5 agentless Developer (agentic kept as option)` +
`C2 best-of-N` → `A0c/d/e operator memory+cues+ReAct repair`.

**Phase 3 — "prove it & scale" (validation + reach):**
`B6 held-out + gap guard (gate for D1)` → `D1 real MLE-bench` → `I1 feature-eng operator` +
`I2 time-series adapter` → `A2/A3 surrogate+BOHB` → `E4 meta-priors` → `F2/F6 cross-run UX +
fork-branch` → `G3 distributed eval` → `B4+ microVM tier`.

**If you do only three things (user decision 2026-06-24):** **A0** (code-block ablation + real merge,
each configurable — the verified #1 lever) · **A6/A1** (proxy + ASHA — what separates the MLE-bench
leaders) · **A7 Strategist** (optional LLM meta-controller picking search algo + Developer mode,
config-overridable). *(B6 parked in backlog — high value, not top-3 now.)*

---

*Companion docs: [ROADMAP.md](ROADMAP.md) (strategy/why), [RESEARCH_NOTES.md](RESEARCH_NOTES.md)
(sourced evidence), [CODE_REVIEW.md](CODE_REVIEW.md) (foundation findings).*

---

## 4. Maintainability backlog (architecture review, 2026-07-04)

A six-subsystem architecture audit (engine / core+events / agents+search / serve /
adapters+runtime+trust+tools / repo-DX) landed the low-risk subset on branch
`claude/agent-architecture-review-t6iqkn`: the `events/types.py` registry + typo-guard test,
`core/errors.py` cycle break, `tools/_base.py` ToolProvider contract, `_shared_providers`,
hint-attr registry, policy constants/registry, orchestrator extractions (triage/lessons/finalize),
serve protocol/prompt hoists, DeepResearcher→`drive_tool_loop` merge, root `CLAUDE.md`, tests CI.

**Waves 3–5 (commits `d024f94`, `79cd990`, `2bd16b5`) then landed almost the entire remainder**,
each behavior-preserving (differential tests / route-list diffs / behavior matrices, full suite
green — 1165 passed / 22 skipped):

- ✅ **`Engine.__init__` 79-param collapse.** `engine/options.py::EngineOptions` (64 config knobs,
  `from_settings`); `__init__` keeps every kwarg (now `_UNSET`-defaulted, explicit > options >
  default); `cli.py::_engine` passthrough collapsed (net −59 lines). Differential test locks
  old-kwarg vs options equivalence.
- ✅ **`serve/server.py` router split.** 2,968 → 245 lines: `AppState` + `serve/routers/`
  (runs/org/control/genesis/assistant/boss/reports/misc) + engine_proc/jobs/settings_store/
  llm_context/artifacts modules. Route list byte-identical (75 routes). Genesis jobs unified onto
  the shared `JobRegistry`; assistant turn endpoints share `_begin_turn`/`_finish_turn`. Zero test
  lines changed (seams preserved via late binding + re-exports).
- ✅ **orchestrator step 2.** `HoldoutGrader`, `WorkspaceSeeder` (with `materialize()`),
  `ConfirmPhaseMixin`, `AblationMixin`; `_emit_node_created` unifies the 4 payload sites
  (historical key sets kept, incl. the ablate `deleted`-omission quirk, now flagged). Orchestrator
  2863 → 2358 lines. *(Not done: decomposing `run()` (~420 lines) into guarded phase methods —
  still open.)*
- ✅ **llm.py shared SSE generator.** `_sse_chunks` (+`_SSETail`); both callers keep their divergent
  merge semantics; +1 regression test.
- ✅ **trust dedup.** `trust/confirm.py::robust_selection` shared by `confirm_top_k` and the engine
  confirm tail. *(Not done: marking `cv.py`'s unwired splitters as library code — trivial, open.)*
- ✅ **SurfacePolicy** in `tools/patch.py` (reason codes; each site keeps its wording; provable
  semantic differences parameterized). RepoTask read-side normalizer left separate by design.
- ✅ **`repo_task.py` split** (946 → 395; `adapters/repo_developer.py`, re-exports kept, mega-prompt
  hoisted) + **`tools/edit_match.py`** (tolerant matcher extracted).
- ✅ **RunStateCache** (`tools/_runcache.py`) dedupes the fold-cache + traversal guard.
- ✅ **CLI polish.** `_run_engine_guarded` dedupes the run/resume error funnel; `ui_preview.py`/
  `ui_with_env.py` → `tools/`; `task_mlebench_100.json` was a byte-identical duplicate → removed.
  Pytest `live`/`docker` markers registered. *(Not done: freezing the 24-flag `run()` surface.)*
- ✅ **wrapper-forwarding mixin** (`WrapsDeveloper` in roles.py) + **prompt-store routing** (7 more
  prompts through `render(prompts, key, default)`; byte-identical defaults). *(Not done: moving the
  two researcher instruction-prose duplicates onto shared fragments — open.)*
- 🟡 **test-suite reorganization.** Codemod of all legacy flat-import test files to canonical paths
  ✅; `live`/`docker` markers ✅. *(Not done: moving the 21 accretion-named `test_review_fixes*`/
  `test_*_fixes` files into per-subject homes — higher-churn/lower-value, deferred.)*
- 🟡 **known one-off flags from the audit.** `Strategy`'s four-site cross-reference comment added
  ✅. *(Open decisions, unchanged behavior: `ToolUsingResearcher` `_sweep_hint` omission — bug or
  intent?; `command_eval` docker rc 137 vs 124; `parse._ORDER` `"outlines"` alias;
  `_PRELOAD_PRIORITY`/`_recipes` hardcoded filenames.)*

**Still open (carried forward):** `run()` phase-method decomposition; `cv.py` splitter docstrings;
`run()` flag-surface freeze; researcher-prose shared fragments; the review-round test-file reorg;
the four one-off behavior decisions above.
