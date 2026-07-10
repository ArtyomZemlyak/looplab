# LoopLab — Agent-Framework Mega-Review (2026-07-10)

**Scope.** A deep, agent-focused review of the whole engine, organized around the three axes the
review was commissioned for: **synergy** between the agents, **corner-case coverage**, and
**agentic best practices**. Six parallel reviewers each read one subsystem end to end —
`agents/` (roles, tool-loop, unified facade, strategist, deep-research, stuck) + `core/llm.py` +
`core/parse.py`; `engine/` (orchestrator 3277 lines + lessons/memory/genesis/confirm/finalize/
triage/ablation/holdout/localize/workspace/options); `search/` (policy/operators/foresight/
surrogate/hybrid_merge/best_of_n/archive/coverage); `tools/` (all providers + `_base.py`
contract); `trust/` + `runtime/` (sandbox tiers, command_eval, deps, gates); and `events/` +
`adapters/` + `serve/` (fold/replay/eventstore, TaskAdapter contract, repo_developer, FastAPI
server + assistant/TUI). Every finding cites `file:line`.

**Method.** Findings were traced from code by each reviewer, then the **highest-impact
cross-cutting claims were independently re-verified** by executing the real helpers (trust
detectors, `extract_code`, the fold's `budget_extend` handler) with synthetic inputs and by
re-reading the wiring in `config.py`/`replay.py`/`orchestrator.py`. Re-verified findings are
marked **✔ reproduced**. This review deliberately does **not** re-propose work already tracked in
[BACKLOG.md](BACKLOG.md), [ROADMAP.md](ROADMAP.md), [PROMPT_REVIEW.md](PROMPT_REVIEW.md), or the
research docs (§11/§13); §7 maps overlaps.

---

## 0. Verdict in one paragraph

The agent core is in **better shape than most production agent stacks**: `drive_tool_loop` is
genuine defense-in-depth (malformed-JSON coercion, forced structured emit on prose stall, budget
salvage, per-invocation StuckDetector, honest truncation markers with resume pointers), the fold
is idempotent and corrupt-log-tolerant with incident-cited guards, and the hint-delivery contract
is a *test-enforced registry* rather than hope. But the review surfaced **one systemic weakness and
three sharp correctness bugs that fire under the recommended configuration**. The systemic weakness
is a **generalization of the delivery-layer problem PROMPT_REVIEW.md already named for hints**: the
framework computes rich, expensive signals — trust flags, LLM triage rationale, foresight
predictions, deep-research memos, per-operator yields — and then **drops most of them before they
reach the agent that could act on them.** The three configuration-live bugs are: (1) two
trust-gate **false positives that demote honest champions** under the `thorough` profile (an
ordinary `_y` variable and a standard `eval_set=` early-stopping call are flagged as cheating/
leakage — ✔ reproduced); (2) a **trust/policy split-brain** where flagged nodes are barred from
winning but still bred from by every search policy (✔ reproduced); (3) **two unbudgeted loops**
(inline-repair and confirm) that can overrun the eval budget by large multiples under default
settings. Plus a scattering of never-raise-contract violations, an auth-bypass on assistant
transcripts, and a poison control-event that bricks a run on every resume (✔ reproduced).

---

## 1. The central synergy thesis — signals are computed, then dropped before the agent sees them

PROMPT_REVIEW.md (§C) concluded that LoopLab's *"systemic weakness is the delivery layer, not the
prompt text"*, and fixed it for the hint registry (P2). This review finds the **same shape recurs
for every other signal class** the engine produces. Each item below is a signal the framework pays
real compute to generate, and then fails to route to the agent that would change its behavior:

| Signal produced | Where it dies | Who never sees it |
|---|---|---|
| **Trust flags** (reward-hack / leakage / critic) | Emitted as `reward_hack_suspected` audit only, *after* the terminal event; no branch in `_repair_error_context` (`orchestrator.py:1908-1952`) | The Developer/Researcher — a hacking/leaking node is neither failed nor corrected; the search keeps regenerating flagged variants (trust G1) |
| **LLM triage rationale** ("the *idea* is wrong because X") | Recorded in `node_failed.data` but the fold reads only error/reason/eval_seconds (`replay.py:137-151`) | The Researcher drafting the replacement — the most expensive judgment in the failure path is invisible to the digest and failure-reflection hint (engine synergy #1) |
| **Foresight predictions** (`foresight_selected`/`hypothesis_ranked`, with confidence) | Written to events + UI; never scored against realized outcomes; `confidence` is never read by any caller (`best_of_n.py:156-161`) | The world model itself — a 0.05-confidence ranking overrides the static pick exactly like a 0.95 one; the learning loop is **open** (search synergy #1) |
| **Deep-research memo** (summary, findings, cited claims) | Only `recommended_directions[:5]` become hints/hypotheses (`orchestrator.py:1329-1341`); summary/findings/claims recorded but never injected, and no `read_research_memo` tool exists | The Researcher — the "go think hard" output mostly informs the UI, not the search (agents synergy #3) |
| **Per-operator yields** (`operator_yields`, `policy.py:84-106`) | Consumed only by the off-by-default bandit | The Strategist that tunes `ablate_every`/`merge_mode` from priors instead of this evidence (search synergy #3) |
| **Operator directives** (human "use only sklearn") | `render_hint_directives` has 3 consumers: plain + tool Researcher, Strategist | The Developer, pilot, and triage prompts — a directive steers *proposals* but not the *code written* nor the *next action chosen* (agents synergy #1; `hints.py:6-8` docstring overclaims) |
| **Run state** (paused / awaiting-approval / trust-flagged / stuck-build) | Folded into `RunState`, surfaced in the UI | The boss/assistant context (`llm_context.py:98-119`) covers only finished/live/stalled — precisely the states where human intervention is most valuable are absent (events synergy) |

**The pattern:** the engine's *event log* is rich and honest, but the *prompt-assembly layer*
reads a narrow slice of it. The single highest-leverage architectural investment is a **"signal
router"** discipline — the same one the hint registry already models — that makes "every folded
signal has exactly one documented injection site" a reviewable invariant. Four of the seven rows
above are 3–6-line fixes (fold one field, add one context block); two (foresight scoreboard,
trust→repair feedback) are small features.

---

## 2. Configuration-live correctness bugs (fix first)

These fire under shipped defaults or the **recommended** `thorough` profile — not hypothetical.

### 2.1 ✔ Trust gate demotes honest champions (two independent false positives)

Under `config.py` profile `thorough` (`reward_hack_detect=True`, `code_leakage_detect=True`,
`trust_gate="gate"` — verified at `config.py:54-57`), two extremely common, entirely honest code
shapes produce **hard** gating signals (signals not prefixed `critic:`/`perfect_metric` are hard —
`replay.py:37-40`), which exclude the node from best-selection:

- **`X, _y = load_data()`** → `grader_access`. The pattern `r"\b_Y\b"` (`reward_hack.py:17`) is
  matched with `re.IGNORECASE` (`reward_hack.py:58`), so the ubiquitous throwaway `_y` variable
  matches the answer-key tell. **✔ reproduced:** `detect_reward_hacks("X, _y = load()...")` →
  `[{'signal': 'grader_access', ...'_y'...}]`. Same for `df.to_csv("solution.csv")` via
  `r"solutions?\.csv"` (`reward_hack.py:18`) — a plausible *output* name flagged as key access.
- **`model.fit(X_train, y_train, eval_set=[(X_val, y_val)])`** → `data_leakage:fit_on_test`.
  `leakage.py:68` tests `"val" in arg`, and `"eval_set"` contains the substring `val`.
  **✔ reproduced:** the standard LightGBM/XGBoost early-stopping call (zero leakage) is flagged;
  `orchestrator.py:3177` prefixes it `data_leakage:`, making it a hard signal.

**Impact:** the recommended real-task profile silently drops the true best solution on any run that
uses `_y`, writes `solution.csv`, or uses early stopping — the false-negative corrupts the run's
answer. *Fix:* drop `\b_Y\b` (or make it case-sensitive + require a `grader`/`answer` qualifier);
strip `eval_set=`/`validation_data=` kwarg spans (or match `\bval\b` as a word) before the
substring test. One line each. (`reward_hack.py:17`, `leakage.py:68`.)

### 2.2 ✔ Trust/policy split-brain — flagged nodes are barred from winning but still bred from

`flagged_node_ids` excludes flagged nodes **only** in `_select_best`/holdout
(`replay.py:28-40, 534-536`). `RunState.feasible_nodes()` (`core/models.py:424`) filters on
`n.feasible and n.metric is not None` — **not** trust flags — and every policy selects parents from
it: GreedyTree merge top-2 (`policy.py:228,260-265`), Evolutionary elites (`:314-317`), MCTS pool
(`:368`), ASHA rungs (`:475`), `legal_actions` (`:540-546`), `weighted_parent` (`:135`), and the
surrogate's training history (`surrogate.py:61-70`). **✔ verified:** `feasible_nodes` reads no
trust state. Under `gate`, a node with a hard cheating signal posting metric 0.99 can never be
champion but *is* top-1 feasible — GreedyTree merges it, MCTS exploits its subtree, the surrogate
fits on its inflated point. The search spends its remaining budget descending from a disqualified
ancestor. *Fix (2 files, ~6 lines, fold-derived so replay-deterministic):* stamp
`Node.trust_flagged` during fold near `replay.py:534`; filter it in `feasible_nodes()` when
`trust_gate in ("gate","block")`. This is the single most valuable synergy wire-up — it makes the
trust and search subsystems agree on which nodes are real.

### 2.3 Two unbudgeted loops overrun the eval budget

- **Inline-repair loop** (`orchestrator.py:2922-3107`): a `while True` re-runs full evals with no
  `max_eval_seconds`/`max_seconds` check between attempts (`max_es` isn't passed into `_evaluate`
  at `:2863`). Defaults make it live: `inline_repair=True`, `inline_repair_attempts=0` (unlimited —
  ✔ `options.py:93-94`). The anti-stuck guard (`:2994-3000`) trips only on a *repeating normalized
  signature*; an LLM whose repairs vary the stderr keeps the loop alive, overshooting the budget by
  multiples within one node (the loop-top and per-eval budget checks only see `total_eval_seconds`
  from *terminal* events, and no terminal is emitted mid-repair). *Fix:* break with
  `("abandon","eval budget exhausted")` when `total_eval_seconds + total_eval >= max_es`.
- **Confirm phase** (`confirm_phase.py:101-107`): runs `confirm_top_k × confirm_seeds` full-profile
  evals with no budget check between seeds; the loop-top check runs only after the whole phase
  returns. Per-seed memoization already makes a mid-phase break resumable for free — add a per-seed
  `max_es` check.

### 2.4 ✔ `budget_extend` poison event bricks a run on every resume

`/control` validates only `node_reset`/inject payloads (`control.py:81-133`); the fold copies
`max_seconds`/`max_eval_seconds` **verbatim** into `budget_overrides` (`replay.py:432-434`) while
`add_nodes` gets `int()`-coerced. The engine then compares `state.total_eval_seconds >= max_es`
(`orchestrator.py:600-601`). A string `"600"` (trivial from a UI form or the TUI's JSON) raises
`TypeError` in the main loop — and because the event replays, **every resume re-crashes**: a
permanent poison event. **✔ verified:** the fold path copies without coercion; `timeout`/
`max_parallel` are guarded engine-side (`:829-834`) but the two budget keys are not. *Fix:* wrap
`float(...)` in try/except in the fold (mirror `add_nodes`) — kills the class even for existing bad
logs — and type-check `budget_extend` at `/control`.

### 2.5 ✔ Auth bypass — assistant transcripts leak `raw` past the token gate

The `LOOPLAB_UI_TOKEN` middleware gates mutating requests + only artifact GETs, on the rationale
"every other GET returns folded projections, never raw files" (`server.py:119-133`). But
`GET /api/assistant/sessions/{sid}` returns full transcripts **including `raw`** — the full
model-facing instruction with attached file contents and UI-context preamble persisted by
`_begin_turn` (`assistant.py` router `:208-216, 304-311`). Only `/shared/{sid}` strips `raw`
(`:249-260`). On a token-gated deployment an unauthenticated GET can read anything the user ever
attached (which can include `$HOME` file contents). *Fix:* add `/api/assistant/` GETs to
`sensitive_get`, or strip `raw` from the non-shared GET.

---

## 3. Corner-case gaps (per subsystem, CONFIRMED via traced paths)

### agents/ + core/llm.py + core/parse.py
- ✔ **`extract_code` returns raw text incl. the ` ```python ` header on an unclosed fence**
  (`parse.py:24-25,43-49`). A Developer reply truncated at max tokens (`finish_reason="length"`
  passthrough, `llm.py:1040-1053`) yields a guaranteed `SyntaxError` node + wasted eval + repair
  cycle; nothing checks `finish_reason`. **✔ reproduced.** *Fix:* salvage
  `re.match(r"```(?:python|py)?\s*\n(.*)\Z", ...)` (mirror `llm.py`'s `_CODE_SPAN_RE`).
- ✔ **`agentic_text`/`agentic_struct` swallow `BudgetExceeded`** (`agent.py:507-508,533-534` catch
  bare `Exception`; `BudgetExceeded` **✔ is** an `Exception` subclass) → a hard budget stop degrades
  to *another* LLM call. Every sibling call site re-raises first. **Aggravating:** no config sets
  `CostAccountant(limit=...)` — `make_llm_client` builds a bare `CostAccountant()` (`tasks.py:327`,
  ✔ verified) and there's no `llm_cost_budget` Setting, so the whole BudgetExceeded discipline is
  **unreachable in production today**.
- **Deep-Research loop dropped the soft-convergence guards** (`deep_research.py:146-159` omits
  `emit_after=300`/`emit_force=500`); with shipped `agent_max_turns=0`/`agent_time_budget_s=0`, a
  model issuing ever-different searches never trips the StuckDetector and runs unbounded — the exact
  "one idea, then ~200 more reads" pathology the G-guard exists for. *Fix:* 2 lines in
  `make_deep_researcher`.
- **`emit_force` retries a doomed force forever** instead of falling through to the working
  `fallback` text path (`agent.py:451-454`); **forced emits bypass the emit validator**
  (`:331-333,452-454,468-470,482-484`) so a refusal/empty force re-creates the empty no-op node the
  validator was built to prevent.
- **Context-overflow 400 discards the whole investigation** — `LLMError` propagates, `_fallback`
  re-sends the *same oversized* messages → fails again → no-op idea (`agent.py:805-814`); nothing
  detects overflow-shaped 400s to trigger emergency compaction. `context_budget_chars` defaults to
  1,000,000 chars (~250k tokens, `config.py:507`), larger than most local windows, so proactive
  compaction may never fire first.
- **Plain `LLMResearcher` lacks the non-numeric-param sanitizer** its agentic twin got
  (`agent.py:762-779` vs `roles.py:399-406`): a `{"params":{"new_metric":"linear"}}` proposal →
  `ParseError` → retry with *byte-identical* messages → empty fallback.

### engine/
- **Stale idea-embedding cache after `node_reset`** — `_idea_vecs` keyed by `node_id` only
  (`orchestrator.py:1619-1624`); a `node_reset from_stage="propose"` re-creates the same id with a
  new idea but never invalidates the cache, so the semantic novelty gate compares future proposals
  against the *old* vector. *Fix:* key by `(node_id, hash(text))`.
- **Role telemetry mis-attributed** — `_rerun_node` (`:2197`), the ablation refine-child
  (`ablation.py:67,73-79`), and `_create_injected_node` (`:2270`) invoke `propose`/`implement` but
  never consume `last_hyp_priority`/`last_foresight`, so the pick set leaks onto the *next*
  `_create_node`'s id — the mis-attribution `_emit_role_telemetry` exists to prevent.
- **Crash windows duplicate/lose work:** fork/inject between `_create_node` and its
  `*_DONE` gate (`:846-863`) → resume creates a *second* child; ablate between `EV_ABLATE` and the
  refine child re-pays all probes (policy path) or silently loses the child (forced path,
  `ablation.py:62-79`).
- **`JsonlCaseLibrary` has no interprocess lock** (`memory.py:451-472`) unlike the lessons store —
  two runs sharing `memory_dir` (the live-share scenario) clobber each other's cases.
- **`novelty_rejected` mis-labels node id on gapped logs** (`len(state.nodes)` vs the real
  `max(...)+1`, `:1699-1795`) — also seeds the deterministic nudge RNG at the wrong slot.
- **Modulo cadences skip on node-count jumps** — `_should_consult` (strategist/coverage) and the
  deep-research cadence still use `n % every == 0` (`:1064,1264`) while `_cadence_due`
  (`:1605-1610`) exists precisely to avoid a batch jump stepping over the only multiple.

### search/
- **`cluster_near_duplicates` has no similarity floor** (`hybrid_merge.py:107-131`) — `hash_embed`
  gives cosine > 0 for essentially every pair, so **✔ 8 unrelated lessons collapse into one
  cluster**; the production caller `_agentic_merge_lessons` (`memory.py:154-166`) hands the *entire*
  per-task lesson store to `agent_merge` as one ~60KB adjudication prompt. Precision is fully
  delegated to the agent; retrieval does no recall/precision work. *Fix:* edge i~j only when j is in
  ≥2 of 3 signal rankings for i, and/or cap component size.
- **`GreedyTree(merge_every=0)` → ZeroDivisionError** (`policy.py:261`); **MCTS `c=-5`** flips
  exploration into a penalty, accepted by `validate_strategy` (`strategist.py:170-174`), recorded as
  a legit strategy; **`DiversityArchive.build` admits infeasible nodes** as niche elites
  (`archive.py:24`, missing `n.feasible` filter); **D10 listwise tie-break runs on byte-identical
  candidates** (`best_of_n.py:165` checks only `len(top)>1`) — a full LLM call spent choosing among
  identical strings at temperature 0.
- **ASHA retirement bugs** (`policy.py:478-497`): a survivor whose only promotion child *failed*
  counts as expanded and is never re-promoted; a 2-member rung never promotes and decays to
  exploit-best.
- **Ablate actions lose their `policy_decision` audit** (`orchestrator.py:658-662` handles ablates
  before the create bucket that emits it; `ablation.py` never emits it).

### tools/
- **`MemoryTools.execute`/`SkillTools.execute` violate the never-raise contract**
  (`memory_tools.py:72-101`, `skills.py:68-75`): `int(args.get("limit"))` on `limit:"ten"` raises;
  `drive_tool_loop` doesn't guard `tools.execute` (`agent.py:417`), so it propagates and
  `ToolUsingResearcher.propose` discards the **entire investigation** for the fallback Idea. Contract
  `_base.py:46-48` says "it never raises."
- **Hand-written skills silently shadowed** — `tasks.py:438-446` registers two `SkillTools`; both
  claim `list_skills`/`use_skill` and `CompositeTools._route` is last-wins (`agent.py:52-55`), so the
  hand-written library is unreachable and duplicate function names go to the endpoint (some backends
  400).
- **Overlay suffix-match returns the wrong file's content** — `reposcout.py:221-225`
  `norm.endswith("/"+kk)` maps any path whose tail matches a staged key; **✔** with staged `test.py`,
  `read_file("src/test.py")` returns the staged root file. The Developer edits a file it never read.
- **CRLF splice** — the whitespace-tolerant edit fallback rebuilds the span with `\n`
  (`edit_match.py:44-53`), **✔** producing mixed endings in a CRLF file → later exact-match edits and
  `git apply` mis-match.
- **No `kill_background` tool, no background timeout, no log pruning** (`shell_tools.py:136-170`;
  `bg_tasks.kill` never surfaced) — a runaway background training loop can't be stopped by the agent.
- **Dimension-mismatched vector search returns arbitrary hits** — `cosine` returns 0.0 on mismatch
  but `search` still sorts and returns top-k (`vectorstore.py:148-179`); after an embedding-endpoint
  death mid-run, `kb_search` ranks noise and presents it as relevant.

### trust/ + runtime/
- **Hardcoded / self-reported metric wins under every built-in gate** — the only metric-value tell
  is `perfect_metric` (fires only at the exact floor/ceiling, advisory); the critic's
  `hardcoded_metric` is tagged `critic:` (also advisory). **✔** `print(json.dumps({"metric":0.0001}))`
  wins under `gate`/`block` on any self-report task. *Fix:* promote `critic:hardcoded_metric` to a
  hard signal (it already requires a literal with no computed assignment).
- **DockerSandbox.run has no memory/CPU cap** (`sandbox.py:437-440`: `--pids-limit` only, no
  `--memory`/`--cpus`/`--cap-drop`/`no-new-privileges`/`--user`) — the untrusted tier whose entire
  purpose is tenant isolation runs as root with unbounded RAM/CPU; `make_docker_wrap` already models
  the caps.
- **Default sandbox buffers unbounded stdout in host memory** — the solution.py path never sets
  `log_path`, so it takes `proc.communicate()` and the bounded `_tee_drain` reader is bypassed
  (`sandbox.py:257-278,287-344`); an adversarial fast printer accumulates its whole output in host
  RAM for up to `timeout` seconds (compounds the missing cap above).
- **`file_json`/`file_regex` metric readers are not workdir-confined** (`command_eval.py:68-73`, no
  `_is_within` guard unlike the adjacent `host_score`) — latent while operator-owned, a direct
  answer-key read the moment reader paths become agent-authorable.
- **Static gate bypasses:** `open(f, "w")` kwarg / `Path().write_text()` / `os.replace` evade
  `_WRITE_RE`; `import grader as g` evades the literal grader patterns; file-split leakage
  (`pd.concat([train,test])` from separate CSVs) is entirely missed by `code_leakage_scan`.

### events/ + adapters/ + serve/
- ✔ **"First terminal wins" is only true for eval-seconds** — the `node_evaluated`/`node_failed`
  handlers set `metric`/`status`/`error` **unconditionally**; only `total_eval_seconds` is
  `first_terminal`-guarded (`replay.py:110-151`). Conflicting terminals (evaluated→failed from a
  double-appended log) are last-wins, contradicting CLAUDE.md invariant #2. `node_repaired` shows the
  correct pattern. *Fix:* gate the whole field-mutation block on `first_terminal`.
- **O(n²) serve read path** — `AppState.events()` fully re-reads+re-parses `events.jsonl` and
  re-folds it, called every 0.4s per SSE client (`appstate.py:61-65`, `runs.py:167-189`); repo runs
  whose `node_created` embeds full file sets push MB of orjson+pydantic per tick. The incremental
  `EventStore` cache exists but serve never uses it.
- **Mid-file corrupt line permanently splits readers from the writer** — readers stop there forever
  (`eventstore.py:74-79`) while `append` keeps minting seqs from the tail; reachable exactly where
  the docstrings admit flock degrades to no-op (FUSE/S3/NFS). The engine's own `fold(read_all())`
  then sees a frozen prefix, UI shows a frozen run, no one is told.
- **`normalize_task` silently drops `cmd` when both `cmd` and `eval` present** (`tasks.py:165`) —
  the composable spelling loses to a stale legacy `eval` with no signal (the exact failure the
  `data`/`dataset` clash check was added for). *Fix:* raise, mirroring `:135-143`.
- **`assets() -> list[str]` contract doc is wrong** — every implementation returns `dict[str,str]`
  and the engine types it as dict (`tasks.py:42` vs `orchestrator.py:458`).
- **`MLEBenchRealTask.assets()` slurps every top-level CSV as a str** (`mlebench_real.py:113-125`)
  and materializes the dict into every node workdir — real Kaggle files are multi-GB → RAM spike +
  n_nodes disk copies.

---

## 4. Agentic best-practice gaps (concrete, HERE — not generic)

1. **Per-role temperature is missing.** One `llm_temperature=0.6` (`config.py:444`) drives every
   role and every call type via `make_llm_client` (`tasks.py:327`): the Researcher (wants
   diversity), the Developer (wants precision), the Strategist/pilot/triage, and **all structured
   `complete_tool` emits** sample at 0.6. H3 gives per-role *models* but not per-role *sampling*.
   Bonus: the T7 response cache only activates at temperature 0 (`llm.py:930`), so deterministic
   stages (triage, forced emits) at temp 0 would also make `llm_cache` usable. **Net-new; high
   leverage; cheap.**
2. **Malformed tool-call args are silently coerced to `{}` and executed** (`agent.py:363-369`) — the
   model gets a confusing result and no signal its JSON was broken. Returning "(your arguments were
   not valid JSON: …)" as the observation is strictly more informative and cheaper than executing
   blind.
3. **No few-shot examples for any structured emit** (Idea/Strategy/memo). The codebase's whole
   coercion machinery exists *because* of weak local models; one worked `emit` example in the
   Researcher system prompt would cut the bounce/parse-fallback rate more cheaply than the repair
   ladder.
4. **Selective vs. always-on reflection** — the docs (§11 stream 2) already cite that reflecting
   only when the agent is doing poorly beats reflecting every step. The reflection cadence is a flat
   modulo; keying it on stagnation (which the Strategist already detects) is aligned with the
   evidence and free of a new signal.
5. **Trust gates as a corrective loop, not a scoreboard** — see §1; the inter-stage `check_fn`
   (`command_eval.py:504-514`) already proves the "fail with an agent-authored concern string that
   becomes repair feedback" pattern is achievable; the trust scans just don't reuse the seam.
6. **Foresight prediction calibration** — the world model never sees its own track record (§1,
   search synergy #1); a fold-derived scoreboard ("of the last N picks, how many beat parent, at
   what mean confidence") appended to `_memory_brief` closes the loop the module docstring claims.

---

## 5. Genuine strengths (calibration — keep these)

1. **`drive_tool_loop` defense-in-depth** — malformed-JSON→`{}`, valid-but-non-object emit
   coercion, forced structured emit on prose stall (not nudge-and-hope), budget-exhaust salvage,
   per-invocation StuckDetector + identical-result repeat note for 3+-cycle round-robins, honest
   truncation markers with resume pointers under a single shared `RESULT_CAP`. Best-in-class LLM
   tool ergonomics; all offline-tested.
2. **Fold idempotence + corrupt-log tolerance** — first-terminal cost guard, per-(node,seed) confirm
   dedup, pending-only `node_repaired` guard, deliberate re-raise of `MemoryError`/`RecursionError`
   with the 184MB-runaway incident cited inline; torn-tail healing + seq-from-max in the event store;
   incremental read cache provably byte-identical to a full scan. All test-locked.
3. **The hint-delivery registry** (`RESEARCHER_HINT_ATTRS` + `forward_hints` + a test that *scans the
   engine's setattr sites*) — the exact fix for the "wrapper silently drops an attribute" class, and
   the template §1 recommends generalizing to every signal.
4. **LLM client resilience** — reasoning-toggle 400 auto-detect-and-drop, stream-stall watchdog with
   non-stream degrade ladder, keepalive-vs-truncated distinction, leaked native-tool-call recovery
   gated on tag anchors so quoted examples never execute.
5. **Trust primitives done right where they are done** — non-finite metric rejection routed through
   every reader; timeout invalidates all self-reported outputs across all three tiers; whole-tree
   process kill (`psutil`→`taskkill /T`→`killpg`) backed by `start_new_session` + in-container
   `timeout -k`; host-side scoring restricts the candidate's prediction payload to one canonical key;
   gating readers refuse `kind=="adapter"` so the gated thing can't supply its own gate.
6. **The repo Developer's context engineering** — explicit-base seeding beats stale `last_files` for
   improve/repair (with the wrong-node-files bug documented), the artifact-chain/never-self-skip
   training contract carries its incident history, operator-stage refusal so a repair can't "fix" a
   manifest nobody reads. These read like an agent file should.

---

## 6. Prioritized recommendations (ordered by leverage)

**P0 — correctness under shipped/recommended config (all small):**
1. Trust false positives (§2.1): drop `\b_Y\b`; strip `eval_set=`/`validation_data=` before the
   `val` substring test. (`reward_hack.py:17`, `leakage.py:68`.)
2. Trust/policy split-brain (§2.2): `Node.trust_flagged` in fold + filter in `feasible_nodes()`
   under gate/block. (2 files, ~6 lines.)
3. Budget checks inside the inline-repair loop and per confirm seed (§2.3).
4. `budget_extend` poison: `float()` coercion in the fold + endpoint type-check (§2.4).
5. `extract_code` unclosed-fence salvage (§3 agents); re-raise `BudgetExceeded` before the broad
   excepts in `agentic_text`/`agentic_struct`; restore `emit_after`/`emit_force` to Deep-Research.
6. Never-raise wrappers for `MemoryTools`/`SkillTools`; assert duplicate names in
   `CompositeTools.__init__` (turns two silent-shadowing bugs loud). (§3 tools.)
7. Token-gate `/api/assistant/` GETs or strip `raw` (§2.5).

**P1 — synergy wire-ups (the §1 thesis) + the sharpest corner cases:**
8. Route hard trust signals into an agent-facing repair/next-proposal directive (§1 row 1; reuse the
   `check_fn` seam).
9. Fold `triage_rationale` onto `Node` and feed it to failure-reflection + digest (§1 row 2).
10. Close the foresight loop: confidence-gate the D10 fall-through + a prediction scoreboard in
    `_memory_brief` (§1 row 3).
11. Inject operator directives into the Developer/pilot/triage prompts (§1 row 6); add
    paused/awaiting-approval/trust-flag lines to `_boss_context` (§1 row 7).
12. Call `_set_complexity_hint` in the debug-propose branch so cross-run priors + fault localization
    reach the agent *when it is fixing a failure* (engine synergy #2).
13. `first_terminal`-guard the field mutation in the fold (§3 events); cached per-run `EventStore` in
    serve (§3 events).
14. Overlay suffix-match tightening + CRLF-preserving edit fallback (§3 tools); DockerSandbox
    resource caps + bounded reader (§3 trust).
15. **Per-role temperature** (§4.1) — a new `researcher_temperature`/`developer_temperature` etc.
    (or a per-role override map), defaulting to today's single value.

**P2 — robustness + hygiene:**
16. `cluster_near_duplicates` similarity floor (§3 search); MCTS `c` clamp + `merge_every` guard +
    archive feasibility filter; ASHA failed-promotion fix.
17. `_idea_vecs` invalidation on reset; `JsonlCaseLibrary` interprocess lock; modulo→`_cadence_due`
    for strategist/deep-research (§3 engine).
18. Promote `critic:hardcoded_metric` to a hard signal (§3 trust); workdir-confine `file_json`/
    `file_regex`; bound `web`/`literature` reads.
19. `kill_background` tool + background timeout + log pruning (§3 tools); `mcp_tools` 8000→`RESULT_CAP`.
20. Wire a `CostAccountant.limit` setting so the BudgetExceeded discipline is actually reachable.

**Refactor seams (verbatim-move, no rewrite — same style as the confirm/ablation/lessons extracts):**
`engine/stage_reuse.py` (`orchestrator.py:2448-2634`, ~190 pure-static lines), an `EvaluateMixin`
(`:2666-3235`, ~570 lines — start with `_trust_scan`), `engine/novelty.py` (`:1612-1807`, natural
home for the `_idea_vecs` fix), `engine/create.py` (`:2010-2295`, where the telemetry-consume family
lives), `engine/cadences.py` (`:998-1360`, co-locates the two modulo cadences with `_cadence_due`).

---

## 7. Overlap with the existing backlog (so nothing is re-proposed)

- **Already tracked, this review adds concrete file:line + reproduction:** memory-as-lifecycle /
  misevolution (docs §11 stream 2; the append-only case/lesson stores + the trust→agent gap here are
  the operational face of it); held-out gate B6 (BACKLOG §2 Theme B — the trust false positives in
  §2.1 are a *precondition*: the gate must be correct before it can be trusted); adversarially
  co-evolved evaluators (docs §11 — the static-regex bypasses in §3 trust are the current cost);
  tool-reader-family consolidation (BACKLOG §6); the `_shutdown_pool_sockets` blast radius
  (BACKLOG §5).
- **Net-new (not in any current doc):** the two trust false positives (§2.1) and the trust/policy
  split-brain (§2.2); the `budget_extend` poison event (§2.4); the assistant-`raw` auth bypass
  (§2.5); the unbudgeted inline-repair/confirm loops (§2.3); the never-raise-contract violations
  (§3 tools); `extract_code` unclosed-fence + `BudgetExceeded` swallow + unreachable cost budget
  (§3 agents); per-role temperature (§4.1); the foresight open-loop scoreboard (§1 row 3);
  triage-rationale drop (§1 row 2).

---

## 8. Implementation — P1 signal-delivery (shipped 2026-07-10)

The §1 delivery system was implemented in one change (full suite green — **1577 passed / 23
skipped** — offline smoke + replay reproduce; the infographic + configuration table were updated in
the same change). What shipped:

- **The seven routes of §1**, each with exactly one injection site, registered in the new
  `engine/signal_delivery.py` and enforced by `tests/test_signal_delivery.py` (every route's inject
  symbol must resolve *and* a synthetic input's content must reach the rendered output — a
  folded-but-uninjected signal is now a red test, generalizing the hint-registry pattern):
  1. **Trust flags → Researcher** — `digest.trust_reflection` renders a recently hard-flagged node
     into the proposal hint (`_set_complexity_hint`); fires even under `audit`.
  2. **Triage rationale → Researcher** — folded onto `Node.triage_rationale` (additive) and surfaced
     in the experiments digest (`_node_line`) + the failure-reflection hint (was dropped by the fold).
  3. **Foresight calibration → world model** — `foresight_selected` folded into
     `RunState.foresight_selected`; `foresight.foresight_scoreboard` primes `_memory_brief` with the
     predictor's own hit rate (L4, closing the predict→outcome loop).
  4. **Deep-research memo → Researcher** — a `read_research_memo` tool (`RunTools`) for the full
     findings/claims + a one-line takeaway in `_state_brief`.
  5. **Operator yields → Strategist** — `StrategyContext.operator_yields` rendered in the strategist
     brief.
  6. **Operator directives → Developer / pilot / triage** — `render_hint_directives` folded into the
     idea handed to the Developer (`_directed_idea`, recorded idea untouched) and appended to the
     pilot + crash-triage briefs.
  7. **Run states → boss/assistant** — `llm_context._attention_states` surfaces paused /
     awaiting-approval / trust-flag / stuck-build.
- **Preconditions for a sound trust channel (§2.1 P0):** the two trust false positives were fixed so
  the flags are trustworthy before they steer the agent — `\b_Y\b` is now case-sensitive
  (`\b(?-i:_Y)\b`; the uppercase answer-key access still fires, the `_y` variable no longer does),
  `solutions?\.csv` is anchored to a READ call, and `leakage.code_leakage_scan` strips
  `eval_set=`/`validation_data=` kwargs before the substring test (a standard early-stopping call is
  no longer flagged as fit-on-test). Verified: false positives gone, true positives preserved.
- **Foresight confidence gate (§6 item 10):** config-first `foresight_min_confidence` (default `0.0`
  = off, byte-identical to prior behavior); above it a low-confidence pick abstains (panel → first
  proposal, best-of-N → D10) rather than committing.
- **Also shipped:** the debug-propose branch now calls `_set_complexity_hint` so cross-run priors +
  fault localization + failure reflection reach the agent when it is FIXING a failure (engine
  synergy #2).

**Deliberately NOT in this change (own follow-ups):** the trust/policy split-brain (§2.2 — changing
`feasible_nodes` gate/block semantics deserves a dedicated change + tests); the budget/loop and
auth/fold P0s (§2.3–2.5); the corner-case fixes in §3; per-role temperature (§4.1).

**Adversarial mega-review of this change (2026-07-10, 18 agents, 5 dimensions × review→verify).**
13 findings, 2 refuted, 11 confirmed (1 major, the rest minor/nit) — all addressed on this branch:
- **Foresight scoreboard double-count** (minor) — with `foresight_panel>1` *and* `best_of_n>1` a node
  folds TWO `foresight_selected` entries (idea-pick + solution-pick), so the calibration line
  double-weighted dual-pick nodes and halved the lookback. Fixed: `foresight_scoreboard` de-dups by
  `node_id` (last pick per node) before scoring.
- **Trust-fix recall regressions** (minor) — stripping the whole `eval_set=` kwarg also masked a
  genuine `eval_set=[(X_test,y_test)]` monitor-on-test leak, and the `solutions.csv` read-anchor still
  flagged `open('solution.csv','w')` while missing a variable-path read. Fixed: leakage now waives only
  `val` inside the monitor but still flags `test`/`x_test` there; `solutions.csv` is flagged unless it
  is *solely* a write (`to_csv`/`savetxt`/`write_text`/`open(...,'w'|'a')`). Verified: false positives
  gone, both genuine leaks and answer-key reads still caught.
- **Test rigor** (major) — the per-signal probes exercised each render function in isolation, so a
  deleted *call site* would not have turned the suite red (the exact §1 failure the registry claims to
  prevent). Fixed: each route now carries `call_sites` (the real wiring points) that a source-scan test
  asserts present (mirroring `test_hint_forwarding`), the memo probe routes through `specs()`/`execute()`
  (not the private method), and the memo's dual channel (tool + `_state_brief`) is registered. Also
  reworded the `_state_brief` takeaway to be channel-neutral (the toolless plain researcher is no longer
  told to call a tool it lacks) and moved `EV_FORESIGHT_SELECTED` out from under the "NOT folded" divider.

Suite after the fix pass: **1584 passed / 23 skipped**.

## 9. Bug-fix pass — the remaining findings, resolved (2026-07-10)

§8 shipped P1 (signal-delivery) plus only the trust P0s that made the signal channel trustworthy;
this pass closes the rest of §2–§3 (and the corner cases surfaced by the code-review of the P1
branch itself). Fixes landed subsystem-by-subsystem so each batch is independently bisectable and
green. The suite ends at **1600 passed / 23 skipped / 1 docker deselected**; the offline smoke +
`looplab replay` reproduce byte-identically (no fold divergence introduced).

| Batch | Subsystem | Class of defect fixed |
|---|---|---|
| 1 | `events/` | fold/replay P0: `budget_extend` string coercion (§2.4 poison event — now killed even for existing bad logs); first-terminal-wins extended to gate **every** field mutation in `node_evaluated`/`node_failed` (a corrupt second terminal can't flip metric/status/feasibility); `novelty_rejected` stamps the gap-safe prospective id; `normalize_task` rejects `cmd`+`eval` both set. |
| 2 | `trust/` | gate precision: `perfect_metric` flags the exact floor (`metric == 0.0`), not every signed objective; `critic:hardcoded_metric` promoted to a hard gate (closes the hardcode-and-win bypass); metric readers confined to the workdir (`_is_within`); `_regex_metric` ReDoS cap; dep-install latch resets per run; surrogate never trains on a flagged/cheated metric (preserves the tested gate-vs-block breeding contract). |
| 3 | `engine/` | unbudgeted loops: inline-repair honors the eval ceiling (`_evaluate(max_es=…)`); confirm phase checks budget once per node; `_idea_vec` keys the cache on text (a `node_reset` no longer compares against a stale vector); role telemetry consumed on rerun/inject/ablate so a pick can't leak onto the next id; `JsonlCaseLibrary.add` re-reads under the interprocess lock; deep-research cadence uses the since-last gate. |
| 4 | `agents/` + `core/parse.py` | `extract_code` salvages an unclosed fence (was a guaranteed SyntaxError node); `agentic_text`/`agentic_struct` re-raise `BudgetExceeded`; deep-research loop restored `emit_after`/`emit_force` (was unbounded under the shipped `max_turns=0`); forced emits validated (`_accept_forced`); plain researcher folds the parse error into the retry prompt. |
| 5 | `search/` | policy/archive corner cases: `merge_every`/MCTS `c` clamped; `DiversityArchive` + best-of-N tie-break skip infeasible / identical candidates; `HybridRetriever` `min_signals` floor (8 unrelated texts → 8 clusters, was 1); ASHA counts only live children as expanded; ablate emits its `policy_decision`. |
| 6 | `tools/` | ToolProvider never-raise contract (a junk arg reads as a tool error, not a killed phase); hand-written + auto skills share one library (no shadowing); `CompositeTools` de-dups by name; reposcout overlay suffix-match is absolute-only; `edit_match` preserves CRLF; vector search drops non-positive scores. |
| 7 | `runtime/` | untrusted-tier hardening the isolation path lacked: `--memory`/`--cpus`/`--cap-drop ALL`/`no-new-privileges`; `_run_argv` always drains through the memory-bounded reader (no unbounded host-RAM buffering); reward-hack write-detection broadened past bare `open(name,'w')`. |
| 8 | `serve/` + `adapters/` | assistant session-transcript GET is token-gated (`raw` leaked attached-file contents past the auth gate — §2.5); `state_payload` caches the fold+dump by `(size, mtime, upto_seq)` (SSE hot path was O(n²)); `LLMOnboarder` bounds the rglob walk + guards every stat/read with `OSError`; `MLEBenchRealTask.assets` fails loud on a >512MB public file instead of OOMing. |

**Deliberately deferred (enhancements, not bugs — noted in the commits):**
- **Trust/policy split-brain full fix (§2.2).** Only the unambiguous surrogate-training exclusion was
  applied. Changing `feasible_nodes` so a *gated* node is also barred from **breeding** (not just
  winning) would collapse the gate-vs-block distinction that `tests/test_profile_trust_gate.py` pins
  as a contract (`gate` = exclude-from-win, `block` = also-infeasible). That deserves its own change
  with its own tests, not a rider on a bug-fix batch.
- **`kill_background` tool + background timeout / log-pruning** (batch 6) — a capability gap, not a
  correctness bug.
- **Mid-file corrupt-log divergence surfacing** (batch 8) — a rare FUSE/NFS-only detection
  enhancement; the fold already ignores unparseable lines safely.
- **Full MLEBench asset→mount refactor** (batch 8) — only the >512MB OOM guard was added; large
  competitions still need a real data mount, which is a task-adapter feature.

---

*Companion docs: [PROMPT_REVIEW.md](PROMPT_REVIEW.md) (the 2026-07-09 prompt/delivery review this
extends from hints to all signal classes), [BACKLOG.md](BACKLOG.md) §5–6 (deferred follow-ups),
[11-agent-systems-research.md](11-agent-systems-research.md) + [13-external-works-analysis-2026-07.md](13-external-works-analysis-2026-07.md)
(frontier evidence).*
