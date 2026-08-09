# LoopLab — Implementation ToDo & Status

**Version:** 0.2 (status tracker) · **Updated:** 2026-06-22
**Companion docs:** [00-INDEX.md](00-INDEX.md) · [01-product-design.md](01-product-design.md) · [02-architecture.md](02-architecture.md) · [03-decisions.md](03-decisions.md) · [04-file-layout.md](04-file-layout.md) · [05-build-decisions.md](05-build-decisions.md) · code: [README.md](https://github.com/ArtyomZemlyak/looplab/blob/master/README.md)

> **Historical iteration snapshot.** Checkbox states and counts below describe 2026-06-22 and are kept
> for implementation traceability; they are not a living status board. Current source/tests, README and
> user guide decide shipped behavior.

### Current reconciliation (2026-08-08)

| Originally listed as missing | Current status |
|---|---|
| Test suite | **8,936 tests collected** by current `pytest --collect-only` |
| React web UI | **Shipped:** FastAPI serves the Vite/React control plane and `uibuild.py` validates/atomically publishes builds |
| `DockerSandbox` | **Shipped** for the `untrusted` network-disabled tier; the `hostile` tier still requires an installed gVisor-compatible runtime |
| OpenTelemetry / MLflow | **Shipped as optional exporters:** `[otel]` provides OTLP; `looplab export-mlflow` requires the separately installed `mlflow` package |
| Real MLE-bench/Kaggle | **Shipped as `mlebench_real`**, with the optional `mlebench` package, prepared competition data and Kaggle access still external prerequisites |
| LanceDB / FastMCP server bus | **Not shipped:** in-memory/vector HTTP embeddings ship; MCP is an optional outbound owner-Assistant client, not a server bus |
| Co-evolving evaluator | **Not shipped** |
| Per-node git lineage | External-agent worktrees and patch gates ship, but canonical solution lineage remains `events.jsonl`/`parent_ids`, not one git commit per node |

**Legend:** ✅ done · 🟡 partial (note says what's missing) · ⬜ todo · 🧱 infra-seam (Protocol/stub exists; body needs external infra — daemon/lib/service)

---

## Historical snapshot (2026-06-22)

- **Tests:** 105 passing + 1 skipped (offline + 4 live-LLM that auto-skip without Ollama; the live OpenCode test is opt-in). Python 3.14, Windows, no Docker.
- **Reviewed 2026-06-22** ([07-architecture-review.md](07-architecture-review.md)): design↔code consistent; 7 bugs found & fixed; I10 gate semantics clarified. **Post-review:** policy-agnostic self-repair; **I7 ablation**, **I18 skills/prompts/AGENTS.md**, **I22 diversity archive**, **I21 HITL**, **I4 patch surface-gate** shipped; **I15 TUI cut**.
- **The planned offline-buildable scope was complete at this checkpoint.** The then-missing list below is superseded by the current reconciliation above.
- **The loop is real and live:** invent (with optional knowledge-base + cross-run-memory tools) → write code → run in sandbox → self-repair on error → cross-validate + multi-seed confirm (variance-gated) → pick best. Leakage-first guard + wall-clock budget + span tracing. Driven offline by a toy optimizer or live by **Qwen3-8B (Ollama)**.
- **At this checkpoint:** core loop + result-moving levers + trust gates + live LLM (Researcher, Developer, agentic retrieval, cross-run memory) were **done and wired**. The contemporaneous remaining list is historical.

| Phase | Status |
|---|---|
| P0 — working loop | ✅ done (one deviation: git/patch path, see I4) |
| P1 — result levers (operators, CV, leakage, gate) | ✅ done (ablation operator still todo); leakage + >1-SE gate now wired |
| P2 — ensemble + multi-seed rigor | ✅ done |
| P3 — scale, obs, TUI | 🟡 concurrency + budget + span tracing wired; **TUI shipped** (`looplab tui`); OTel/MLflow todo |
| P3.5/.6/.7 — grounding, knowledge, memory | ✅ profiling + agentic retrieval + cross-run memory + skills + prompt store |
| P4 — breadth, hardening, web UI | 🟡 3 policies + diversity archive + secret/trust/leakage + **HITL** + **terminal TUI** done; adapters + git/patch + (Docker/gVisor seam) todo |
| **Beyond plan** — real ML task, LLM-writes-code, live serving | ✅ done |

---

## P0 — Working loop ✅

- [x] **I0 — Skeleton, config, domain models, JSON Schemas** ✅
  - [x] Package + Typer CLI (`run`/`resume`/`inspect`/`replay`/`smoke`) — `cli.py`
  - [x] `pydantic-settings` config + secret-masked snapshot — `config.py`
  - [x] Domain models + event envelope (JSON-Schema source) — `models.py`
- [x] **I1 — Event store, replay, read-model** ✅
  - [x] Append-only orjson JSONL, single-writer, fsync, torn-line-tolerant — `eventstore.py`
  - [x] Pure `fold(events) → RunState` (resume = replay) — `replay.py`
  - [x] Rebuildable SQLite projection — `readmodel.py`
  - [x] Replay-determinism + durability tests — `test_events_replay.py`
- [x] **I2 — LLM layer + structured outputs** ✅ (also live, see below)
  - [x] tool_call default → BAML/text fallback, `<think>`-aware — `parse.py`
  - [x] `OpenAICompatibleClient` (OpenAI SDK/httpx) + optional `LiteLLMClient` + `CostAccountant` — `llm.py`; shipped Settings select the OpenAI-compatible client
  - [x] Auto-fallback + cost tests; mock-HTTP client test — `test_parse_llm.py`, `test_openai_client.py`
- [x] **I3 — Sandbox (trust-mode tiered)** ✅
  - [x] `SubprocessSandbox` (timeout, tree-kill, output caps, cwd jail) — `sandbox.py`
  - [x] `make_sandbox(trust_mode)`; `trusted_local` default — `sandbox.py`
  - [x] `DockerSandbox` (`docker run`, `--network none`, resource bounds); gVisor remains an operator-supplied runtime for `hostile`
- [x] **I4 — Unified-diff patch path + surface gate** ✅ (lineage-via-git still a deliberate deviation)
  - [x] `patch.py` — `changed_paths` parse, **out-of-surface gate (reject, not strip)**: allow-list globs + escape rules (`..`, leading `/`, drive letter), then `git apply --check` → apply — tested `test_patch.py` (incl. path-traversal/absolute rejection)
  - Note: solutions are still **whole-file** by default (no diff-emitting backend yet); lineage stays in the event log (`parent_ids`), not git commits — a deliberate deviation that works. The gate is ready for a diff-emitting external coding-agent backend.
- [x] **I5 — Role backends (ADR-7 seam)** ✅
  - [x] `Researcher`/`Developer` Protocols; toy + LLM backends — `roles.py`
- [x] **I6 — GreedyTree + orchestrator + crash-resume + HTML** ✅
  - [x] `GreedyTree` pure policy — `policy.py`
  - [x] anyio loop + `CapacityLimiter` + crash-resume — `orchestrator.py`
  - [x] static-HTML lineage tree — `htmlview.py`
  - [x] full run + kill-9/resume tests — `test_end_to_end.py`

---

## P1 — Result-moving levers 🟡

- [x] **I7 — Rich operators** ✅ (complete)
  - [x] draft, improve, depth-bounded debug, **error-feedback debug (code repair)**, ensemble/merge — `operators.py`, `orchestrator.py`, `policy.py`
  - [x] **policy-agnostic self-repair**: failed-leaf→debug via shared `policy.debug_action`, used by all three policies — tested `test_operators_policy.py`
  - [x] **MLE-STAR ablation operator**: probe each param's impact → `ablate` event → `refine_block` child on the highest-impact param; `ablate_every`/`--ablate-every` — `orchestrator._ablate`, `policy.py`; tested `test_ablation.py`
- [x] **I8 — Consistent CV harness** ✅
  - [x] K-fold + purged/embargoed walk-forward + consistent-eval — `cv.py`; used by the regression task’s evaluation
- [x] **I9 — Leakage detectors** ✅ *(now a leakage-first gate)*
  - [x] train/test contamination, temporal, target leakage + tests — `leakage.py`, `test_trust_knowledge.py`
  - [x] **wired into the loop**: grounding scans `task.leakage_inputs()`; a detected leak **aborts the run** (`run_finished` reason=`leakage`) — `orchestrator._leakage_blocks`; tested `test_partials_wired.py`
- [x] **I10 — Variance gate** ✅ *(flags significance of the confirmed demotion)*
  - [x] `>1-SE`-of-the-difference rule (sample std) implemented + tested — `gate.py`, `cv.py`
  - [x] **wired**: confirmation selects the robust-mean winner (demotes seed-lucky leaders) and the gate records whether that demotion is **statistically significant** on the `best_confirmed` event — `confirm.py`, `orchestrator._confirm_phase`, `replay.py`; tested `test_partials_wired.py`. *(Selection-vetoing the leader was rejected in review — it defeats the demote-lucky goal; see [07](07-architecture-review.md) §3.)*

---

## P2 — Ensemble + frontier rigor ✅

- [x] **I11 — Ensemble/merge + multi-parent DAG** ✅ — `operators.merge_idea`, policy merge, `parent_ids`; tested `test_operators_policy.py`
- [x] **I12 — Multi-seed top-k confirmation** ✅ *(wired into the loop)*
  - [x] `confirm.confirm_top_k` (demotes seed-lucky leaders) — `confirm.py`
  - [x] orchestrator confirmation phase (`node_confirmed` events, robust best, seeded-noise path) — `orchestrator.py`; tested `test_cv_confirm.py`, `test_confirm_integration.py`

---

## P3 — Scale & reproducibility 🟡

- [x] **I13 — Parallel throughput + budget** ✅ (cost-abort for API backends = seam)
  - [x] concurrent evaluation via `CapacityLimiter` — `orchestrator.py`
  - [x] `CostAccountant` (warn 80% / stop 100% / `BudgetExceeded`) — `llm.py`
  - [x] **wall-clock budget** aborts the run (`run_finished` reason=`time_budget`) + `budget` summary event — `orchestrator.py`; tested `test_partials_wired.py`
  - [ ] 🧱 cost-budget abort (local model cost is 0; meaningful only for paid API backends)
- [x] **I14 — Observability** ✅ (custom JSONL spans wired; OTel/MLflow = seam)
  - [x] custom JSONL span exporter + `span()` + **wired into the orchestrator** (per-evaluation spans → `spans.jsonl`) — `tracing.py`, `orchestrator.py`; tested `test_partials_wired.py`
  - [ ] 🧱 OTel-SDK wrapping + MLflow optional exporter
- [x] **I15 — Terminal control plane (`looplab tui`)** — the Textual TUI was originally **CUT** (2026-06-22) to avoid a heavy widget dependency. Shipped instead as a **dependency-free, chat-first TUI** (`looplab/serve/tui.py`): a thin HTTP client of the existing UI server (ADR-18), built on stdlib `urllib` + `rich` (already shipped with Typer) — no Textual, no curses. It is deliberately the *control* slice, not a graph explorer: a run dashboard (status · nodes · best · age), the genesis flow (describe a goal → the boss proposes a spec → the operator explicitly types `launch`), and a per-run boss chat that proposes actions for operator confirmation before the control service applies the chosen subset (the same action-router the web Dock uses). The dashboard and run view **auto-refresh live** (poll-on-idle via `select`, redraw only on change), action plans + destructive controls ask for **confirmation** (apply all / pick a subset / cancel), and **bare `looplab`** opens it. Auto-launches an API-only server when none is found. Tested `test_tui.py` (pure render/gate helpers, the live-refresh loop over a real pipe, + the client contract via TestClient).

---

## P3.5 / .6 / .7 — Grounding, knowledge, memory 🟡

- [x] **I16 — Data profiler + grounding pre-phase** ✅ — `profile.py`; orchestrator emits `data_profiled`; tested `test_regression_task.py`
- [x] **I17 — Vector store + agentic retrieval** ✅ / 🧱 production backends
  - [x] `VectorStore` Protocol + in-memory default (cosine) — `vectorstore.py`
  - [x] grep/glob/read tools — `retrieval.py`
  - [x] **tool-using Researcher** (multi-turn) + `KnowledgeTools` (grep/kb_search/list/read) — `agent.py`, `knowledge_tools.py`; tested `test_agentic_retrieval.py` + live
  - [ ] 🧱 LanceDB / Qdrant backends; FastMCP server bus
- [x] **I18 — Skills + prompt store + AGENTS.md** ✅
  - [x] `PromptStore` — hot-reloaded role prompts ($var templates, frontmatter-stripped, default fallback) wired into LLM roles via `prompt_dir` — `prompts.py`; tested `test_skills_prompts.py`
  - [x] `SkillLibrary`/`SkillTools` — progressive-disclosure SKILL.md (`list_skills`/`use_skill`), composed with knowledge/memory tools via `CompositeTools`; `skills_dir` — `skills.py`, `agent.py`
  - [x] `generate_agents_md` — task/contract context written to the run dir at start — `agents_md.py`, `orchestrator.py`
- [x] **I19 — Cross-run memory** ✅ *(persisted + wired both ends)*
  - [x] `CaseLibrary` + persistent `JsonlCaseLibrary` (retain-on-improvement) — `memory.py`
  - [x] **run end**: engine stores the best result as a case (`memory_dir`) — `orchestrator._store_case`
  - [x] **run start / live**: past cases are indexed by `KnowledgeTools` and recalled via `kb_search` by the tool-using Researcher — `knowledge_tools.py`, `tasks.make_roles`; tested `test_partials_wired.py`

---

## P4 — Breadth, hardening, opt-in machinery 🟡

- [x] **I20 — MLEBench / Kaggle adapters** — the synthetic held-out `mlebench` task and real
  `mlebench_real` competition adapter both ship. Real runs still require the optional `mlebench`
  dependency, prepared competition data and Kaggle credentials/access. The synthetic adapter's setup
  leakage check is exact-row matching, not near-duplicate/temporal/target coverage.
- [🟡] **I21 — Hardening** 🟡
  - [x] secret-leak scan gate + secret-masked config — `test_security.py`, `config.py`
  - [x] trust-mode sandbox tiering — `sandbox.py`
  - [x] **HITL-as-events**: `require_approval` pauses at the final-best gate; `approval_requested`/`approval_granted` flow through the event log; `LoopLab approve` CLI; resume finishes — `orchestrator.py`, `cli.py`; tested `test_archive_hitl.py`
  - [ ] 🧱 gVisor/Sysbox escalation; [ ] ⬜ short-lived gateway tokens
- [🟡] **I22 — Opt-in machinery + web UI** 🟡
  - [x] `EvolutionaryPolicy` + **`MCTSPolicy` (UCB1)** + `make_policy` (config-selectable: greedy/evolutionary/mcts) — `policy.py`; tested `test_tracing_altpolicy.py`
  - [x] **diversity archive** (quality-diversity niching, best-per-niche, summary recorded at run end) — `archive.py`, `orchestrator.py`; tested `test_archive_hitl.py`
  - [ ] ⬜ co-evolving evaluator
  - [x] React web UI (FastAPI + Vite; auto-build/validated atomic publication through `uibuild.py`)

---

## Beyond the original plan ✅

- [x] **Real ML task** — polynomial+ridge model selection via CV (`regression` kind) — `regression.py`; tested `test_regression_task.py`
- [x] **LLM writes the solution code** (`code_regression` kind) — dataset as a `data.json` asset materialized into the sandbox; LLM-Developer + error-feedback repair — `regression.py`, `roles.py`, `orchestrator.py`; tested `test_code_loop.py` + live
- [x] **Live serving** — Ollama + Qwen3-8B on the RTX 5090; `smoke` self-test; live tests `test_live_llm.py` (see [README](https://github.com/ArtyomZemlyak/looplab/blob/master/README.md))

---

## Next functions (the ⬜ todo list)

All partials are now done. **In progress:** `MCTSPolicy` ✅ (third search algorithm, UCB1 — `policy.py`, `make_policy("mcts")`; tested `test_tracing_altpolicy.py`).

Remaining offline-buildable features, roughly by value:

- [x] ~~Policy-agnostic self-repair (review rec #1)~~ ✅ done — `policy.debug_action`
- [x] ~~Ablation operator (I7)~~ ✅ done — `orchestrator._ablate`

- [x] ~~Skills + prompt store + AGENTS.md (I18)~~ ✅ done
- [x] ~~Diversity archive (I22)~~ ✅ done — `archive.py`
- [x] ~~HITL-as-events (I21)~~ ✅ done — `orchestrator.py` + `LoopLab approve`
- [x] Terminal control plane (I15) — shipped as a dependency-free chat-first TUI (`looplab tui`), not Textual

- [x] ~~Git/patch path (I4)~~ ✅ done — `patch.py` (surface gate; ready for a diff-emitting backend)

- [x] ~~External coding-agent backend~~ ✅ **built + LIVE-WORKING** — tool-agnostic `cli_agent.CliAgentDeveloper` + presets (opencode/aider/goose/continue), `developer_backend`/`agent_cmd` config, reuses the task brief. Tested offline with a stub agent **and live end-to-end with OpenCode 1.17.9 + local Ollama** (`python -m LoopLab.cli run … --developer-backend opencode`): all nodes written by the agent, evaluated, best selected.
  - **Headless, self-contained:** `cli_agent.opencode_config()` drops an `opencode.json` (local Ollama provider, explicit `--model`) into the agent workdir, so OpenCode never fetches the external model registry — the startup fetch that the corporate proxy used to stall (the earlier "live blocked on this box" conclusion is **superseded**; it only ever hung on the registry/TUI/interactive-provider paths, all now avoided). Live test opt-in via `LOOPLAB_TEST_OPENCODE=1`.
  - **Windows fixes:** `_resolve_launcher` maps the bare `opencode` name to the real `node_modules\…\opencode.exe` (a `subprocess`-without-shell run of the npm `.cmd` shim fails with WinError 2 — this had been silently forcing the fallback on every node); subprocess capture forced to UTF-8/`errors=replace` (cp1252 reader-thread crash on agent glyphs).
  - **Output validation (NEW):** `validate.py` + `roles.ValidatingDeveloper` audit each agent output (launched / not-timed-out / produced / modified-seed / parses / emits-metric), retry the agent with the failure as feedback (`agent_max_retries`), then fall back to the task's original Developer — which may be an in-house LLM writer, a deterministic/template implementation, or the repo baseline. Per-node `agent_validated` event → `node.agent_report` → surfaced in the SQLite read-model (`agent_ok`/`agent_fell_back`) and the HTML node table. Audit only; never affects selection. Config `validate_agent` (default on).
  - **Edit-surface gate (ADR-7 Rule 3) — ✅ DONE (multi-file, patch-gated):** with `agent_patch_gate` (default on) the agent runs in a **git worktree** (seed committed); afterward `git diff --cached <seed>` is gated by `patch.gate(agent_surface)` (default `["*.py"]`, reject-not-strip out-of-surface/`..`/absolute). Accepted in-surface files (solution.py + helper modules) are captured into `Node.files` (files-as-truth → resumable) and materialized into the eval/confirm workdirs by `Engine._write_node_files`; the validator adds an `edit_in_surface` check. Robust to agents that self-commit (diffs vs the seed SHA) and degrades to whole-file readback if git is missing. Verified live with OpenCode (clean in-surface single + multi-file via stub). Config `agent_patch_gate` / `agent_surface`.

Current remaining seams from this original list: **LanceDB**, an MCP server bus, a co-evolving
evaluator, and short-lived gateway tokens. Real MLE-bench data/access, an OTLP collector, MLflow and
gVisor remain optional external infrastructure for already-shipped adapters.
