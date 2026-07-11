# LoopLab — Architecture & Code Review (2026-07-11)

**Goal of this review.** A whole-codebase audit across four axes the maintainer asked for:
(1) **architecture** — layering, the event-sourced engine invariants, coupling, duck-typed
seams; (2) **functional consistency** — that no two parts of the system contradict each other;
(3) **general code quality/correctness** — real bugs, races, security, robustness; and
(4) **documentation, schemas and diagrams** — that the docs, the settings table, and the
process diagram match the code. **No fixes are applied in this document** — every item carries a
`file:line` anchor, the concrete failure/contradiction, and a suggested fix so the work can be
sequenced and test-gated.

**Method.** The review fanned out over `looplab/` (~39k LoC, 160 test files) as **33 independent
scopes** — 23 per-subsystem code reviews + 5 cross-cutting architecture/consistency scopes +
5 documentation/diagram scopes — run as parallel agents. **Every finding was then adversarially
re-verified** by a second agent that re-read each cited `file:line`, confirmed *both* sides of
each contradiction, and refuted false positives. 66 agents, ~5.7M tokens. Only findings that
survived verification appear below; each is marked **CONFIRMED** (failure fully traced) or
**PLAUSIBLE** (real code defect, runtime trigger conditional). Severities are the *verified*
severities — several reviewer over-statements were downgraded during verification, and where a
reviewer's suggested fix was itself wrong (e.g. a discontinuous MCTS reward map) the corrected
fix is used.

> **Environment note.** `pytest` is not installed in the review environment, so this is a
> **static** review: test code was read, not executed. Claims that required running code
> (e.g. reproducing the `X_trainval` leakage false-positive, the PEP-604 coercion miss) were
> reproduced by the agents in a Python REPL where noted; the full suite was not run.

**Non-negotiable invariants checked against** (from `CLAUDE.md`): the engine is the sole writer
of domain events; exactly one terminal event per node; every side effect gated on a domain event
(resume-by-replay idempotent); state observed only via `fold(store.read_all())`; `fold` stays
deterministic, order-tolerant, unknown-type-tolerant, additive-only with reader-side defaults;
`Settings` fields flat and never renamed/nested; event-type names are constants; prompt strings
are behavioural contracts.

---

## 1. Executive summary

**The architecture is sound and the load-bearing invariants hold.** The event-sourced design
does what it claims: the layering rules are respected (no upward or `engine → serve` imports),
the fold is deterministic, the six registry-guarded seams are in sync, and the "engine is the
sole writer" invariant survives an end-to-end trace of every `store.append` site. **No critical
defects and no reproducible data-corruption / replay-divergence bugs were found.** The two
`high`-severity items are a search-liveness bug and a trust-gate false-positive, both gated
behind non-default configuration; everything else is `medium` or below, and a large share is
documentation/comment drift rather than runtime behaviour.

| Severity | Count | Character |
|---|---|---|
| **Critical** | 0 | — |
| **High** | 3 | a no-progress engine spin (repo/eval-spec + ablation cadence); a leakage-gate false-positive that can silently bar an honest winner; an untrusted-container hardening gap |
| **Medium** | 12 | objective-inversion prompts, an MCTS UCB degeneracy for unbounded `max` metrics, a lessons-file race, a token-auth gap, a dropped-env confirm-gate bug, several genuine doc/config contradictions |
| **Low** | 49 | mostly latent (hand-edited-log robustness, crash-window idempotency), conservative defensive-default divergences, and doc/comment drift |
| **Nit** | 6 | cosmetic / undefined-name annotations / advertised-vs-actual budget rounding |
| *Refuted* | 4 | false positives dropped in verification (documented as intended behaviour) |

*(70 verified findings across 33 scopes; 4 refuted.)*

**Where to spend the first day** (details in §4): the three `high` items — `engine/ablation.py`
liveness (H1), `trust/leakage.py` substring match (H2), and the untrusted command-eval container
hardening (H3) — then the objective-inversion prompt (`adapters/dataset_task.py`, M1), the
strategy-resume divergence (`engine/strategy.py`, M3), and the `agent_control` governance
contradiction (`core/config.py`, M4) — because the last is a **documented guarantee the code does
not provide**, which is exactly the "no contradictions" concern.

**Functional contradictions** (the specific ask "что нет противоречий"): the review surfaced a
focused set of real ones — see §6. The most consequential are behavioural (governance map is
advisory for all but two knobs; strategy deltas silently revert on resume; a loss metric is
unconditionally negated for `direction="min"`); the rest are doc/comment-vs-code contradictions,
which `CLAUDE.md` explicitly classes as bugs.

---

## 2. Coverage map (33 scopes)

| Dimension | Scopes |
|---|---|
| **Code — core** | core-llm · core-config-models · core-foundation |
| **Code — events** | events-replay-fold · events-projections |
| **Code — runtime/tools** | runtime-sandbox · tools-exec · tools-retrieval-memory |
| **Code — agents/search** | agents-core · agents-aux · search-policy · search-foresight-merge |
| **Code — trust/engine** | trust-gates · engine-orchestrator · engine-eval · engine-memory-lessons · engine-strategy-cadence |
| **Code — adapters/serve/cli** | adapters-tasks · adapters-repo · adapters-mlebench · serve-routers · serve-assistant-tui · cli |
| **Architecture** | arch-layering · arch-invariants · arch-registries · arch-contradictions · arch-eventlog-schema |
| **Docs & diagram** | docs-config-table · docs-diagram-infographic · docs-architecture-concepts · docs-guides · docs-readme-adr |

Scopes that came back **clean** (0 findings after verification): **arch-layering**
(layering rules hold; back-compat `_LAYOUT` shim resolves), **search-foresight-merge** (the
`__getattr__` proxy hint-forwarding is correct), **docs-architecture-concepts** (the
architecture spec / concepts / package map match the code — including, on inspection, the
"thirteen files" Engine claim: exactly 13 files host the `Engine` class + mixins; the other
engine modules are helpers, so the claim is accurate), and **docs-diagram-infographic** — see §11.

> Two scopes needed a second pass: the diagram finder first exceeded the structured-output cap
> and was re-run at high effort (result: clean, §11), and the runtime-sandbox verifier misfired
> and was re-verified separately (§12).

---

## 3. Architecture assessment

**Layering & coupling — clean.** `core` imports nothing above itself, `events` imports only
`core`, and the engine has **no** dependency on `serve`. The back-compat meta-path shim in
`looplab/__init__.py` correctly aliases every pre-split flat module path to the same module
object.

**Event log / fold invariants — hold, with two latent robustness gaps.** The fold is pure and
deterministic; unknown event types are ignored; the first-terminal-wins idempotency and the
`<x>_requests`/`<x>s_done` counter pairs are correctly implemented. The two gaps are both
*conditional*, not live:

- `_on_run_started` reads `d["run_id"]`/`d["task_id"]` with **bare subscripts** while every
  other fold read (and the twin `_on_node_created`) defends against a hand-edited/torn log — so
  a malformed `run_started` raises `KeyError` out of the *entire* fold instead of degrading
  (§5, `events-replay-fold`). Unreachable under the sole-writer invariant, but it contradicts
  the file's own repeatedly-documented "tolerate a BYO/hand-edited log" posture.
- The operator **fork/inject** side effect is appended *before* its gate counter, so a hard
  crash in the sub-millisecond window between the two `append`s duplicates the node on resume
  (§5, `arch-invariants` / `engine-orchestrator`). Worst case: one wasted experiment.

**Sole-writer invariant — holds.** Every `store.append` site was classified. The only
non-engine writers are the allow-listed control/ratification events (`spec_approved`,
`trust_gate_changed`, `report_generated`), which are documented exceptions in
`events/types.py` and are fold-safe (last-write-wins/audit-only). The reviewer's claim that
`trust_gate_changed` is an *undocumented* exception was **refuted** — it is documented.

**Registry-guarded seams — in sync.** `TASK_OPTIONAL_HOOKS`, `DEVELOPER_OUTPUT_ATTRS`,
`RESEARCHER_ACTION_ATTRS`, `RESEARCHER_HINT_ATTRS`, `PROMPT_KEYS`, `SIGNALS`, and
`BACKGROUND_APPENDABLE` all match their usage. One **dormant** gap: the `PROMPT_KEYS`
source-scan test regex omits digits, so a digit-bearing prompt key would escape the "every
render key is registered" guard — no current key triggers it (§5, `arch-registries`).

---

## 4. High-severity findings

### H1 · Ablation no-op on repo/eval-spec runs → unbounded no-progress spin
**`looplab/engine/ablation.py:41` · correctness · CONFIRMED**

On a `RepoTask`/command-eval run, `_ablate` appends an empty `ablate` event and returns
**without creating a `refine_block` node**. That empty event closes the *forced*-ablate gate but
not the **policy-cadence / operator-bandit** ablate path: `GreedyTree.next_actions` re-proposes
`KIND_ABLATE` when `n_improve >= (n_refine+1)*ablate_every`, and because the skip creates no
node, `n_refine`/`n_improve`/`total` never change — so the *identical* ablate action is returned
every iteration. No safety net fires: the runaway trip counts only creates, `total >= max_nodes`
never trips (total frozen), and the eval-compute budget never trips (no eval runs). Absent a
wall-clock `max_seconds` the engine **loops forever** appending empty `ablate` events; with
`max_seconds` set it burns the whole budget. Reachable whenever `ablate_every > 0` (the
`thorough` preset sets 3; the strategist sets 2 without an `is_numeric_space` guard) on a
repo/eval-spec run whose leader has ≥2 numeric params.

**Fix.** On a repo/eval-spec run, advance the search instead of a bare return (create a
`refine_block` child via the normal `_implement(...)` path so `n_refine` increments), **or**
guard the policy so it stops proposing `ablate` on ablation-incapable runs (an
"ablation-capable" flag set when `_eval_spec`/`_repo_spec` is present).

### H2 · `code_leakage_scan` matches `val`/`test` as bare substrings → bars honest winners
**`looplab/trust/leakage.py:86` · correctness · CONFIRMED**

The fit-on-test detector tests `"test" in head or "val" in head` on lowercased fit arguments —
a raw substring test. The agents reproduced at runtime that `model.fit(X_trainval, y_trainval)`
(the standard non-leaking refit on train+validation after CV), `pipe.fit(X_interval, ...)`, and
`clf.fit(X_latest, ...)` (`latest` contains `test`) are **all** flagged as `fit_on_test`. That
flag is a hard signal: `is_hard_signal('data_leakage:fit_on_test')` is `True`, so the node is
stamped into `breed_excluded` and excluded from `_select_best`. Because the shipped `thorough`
profile enables `code_leakage_detect=True` **and** `trust_gate='gate'` together, an honest
solution that refits on `X_trainval` is **silently barred from ever being selected best and from
being bred/confirmed**. The maintainers already treat this class as a bug — the `eval_set`
carve-out was added for exactly the benign-`val`-substring case.

**Fix.** Token-anchor the match, e.g. `re.compile(r"(?<![a-z])(?:val|test)")`, which still fires
on `x_val`/`x_test`/`_val` while rejecting `trainval`, `eval`, `retrieval`, `interval`,
`latest`. Add regression cases for `X_trainval`/`X_latest` to `tests/test_code_leakage.py`.

> Note: `H2` is coupled to a documentation contradiction — `leakage.py`'s docstring calls these
> flags "not a hard gate", but the wiring makes them hard-gate under `gate`/`block` (§6, D-leak).
> Fixing precision (H2) and reconciling the docstring should land together.

### H3 · Untrusted command-eval container is under-hardened (and contradicts two docs)
**`looplab/runtime/command_eval.py:379` · security · CONFIRMED**

`make_docker_wrap` builds the **RepoTask command-eval** (and shell-tool) container as
`docker run --rm --network none [--runtime] --pids-limit 1024 -v root:/work [--memory mem]` — with
**no** `--cap-drop ALL`, **no** `--security-opt no-new-privileges`, **no** `--cpus`, and
`eval_dispatch.py:125` calls it with **no** `mem`, so `--memory` is omitted too. The *other* Docker
tier, `DockerSandbox.run` (the `solution.py` path, `sandbox.py:414-423`), drops all caps, forbids
privilege escalation, sets `--memory` (default `4g`), and supports `--cpus`. Both tiers serve the
same untrusted/hostile trust modes. Verified that `sandbox_memory`/`sandbox_cpus` reach *only* the
`DockerSandbox` path (`cli/__init__.py:273-274`), never `make_docker_wrap`. So an untrusted/hostile
RepoTask candidate **keeps all Linux capabilities, can privilege-escalate via a setuid binary, and
can exhaust host RAM/CPU** (gVisor on the hostile tier blocks kernel escape but not resource
exhaustion — `DockerSandbox`'s own comment says so). It is also a two-sided doc contradiction:
`generating-code.md:463` documents this tier's command literally *with* `--cap-drop ALL
--security-opt no-new-privileges --memory 4g`, and `configuration.md:239-241` scope
`sandbox_memory`/`sandbox_cpus` to the "untrusted command-eval tier" — the exact path that ignores
them. **Fix:** add `--cap-drop ALL --security-opt no-new-privileges` to the `make_docker_wrap`
argv, add a `cpus` param, and plumb `settings.sandbox_memory`/`sandbox_cpus` through the engine so
`eval_dispatch` passes them. *(Correction from verification: `DockerSandbox`'s `cpus` default is
`""` (off), not on — only `mem=4g` is a live default — but the caps/no-new-privileges/memory
asymmetry fully holds.)*

Precondition: this is the **opt-in Docker tier for untrusted/hostile RepoTasks**; the default
subprocess tier and trusted runs are unaffected. It is `high` because it is precisely the tier
whose entire purpose is tenant isolation, and the docs promise hardening the code does not apply.

---

## 5. Medium-severity findings

### M1 · Objective inverted: `DatasetTask._brief` unconditionally negates a loss metric
**`looplab/adapters/dataset_task.py:319` · correctness · CONFIRMED**

`higher = self.direction == "max"` drives `sense`/`objective` correctly, but *both* metric-line
branches hard-code "report its NEGATIVE" for a loss. That is correct only for `direction="max"`.
For the supported `direction="min"`, a natural loss (RMSE) already matches the orientation and
must be reported as-is; negating it makes the loop `minimize(-loss) = maximize(loss)` — silently
**selecting the worst model**. The named-metric branch even self-contradicts (says "SAME
orientation as the loop (LOWER is better)" then tells the agent to negate). Prompt strings are
contracts, so a provably-inverted instruction is a real defect. **Fix:** make the negate
instruction conditional on `higher`; add a min-direction test.

### M2 · MCTS reward unbounded for `max` runs → UCB1 degenerates to greedy
**`looplab/search/policy.py:419` · correctness · CONFIRMED**

The `min` branch maps reward through a bounded monotone transform, with a load-bearing comment
explaining *why* (keep reward O(1) so the `c·√(ln N / visits)` exploration term isn't swamped).
The `max` branch is bare `reward = value` with no normalization. For a `max` metric of large
magnitude/spread (log-likelihood ≈ −400, Sharpe, throughput, negative-MSE), reward differences
dwarf the exploration bonus and UCB1 collapses to `argmax(reward)` — silently greedy search
recorded as legitimate MCTS. Accuracy/AUC/F1 (∈[0,1]) are safe, so it hides until an unbounded
`max` metric is used. **Fix (corrected during verification):** mirror the `min` branch with a
*continuous, monotone-increasing* map — `reward = (2 - 1/(1+value)) if value>=0 else 1/(1-value)`
(the reviewer's first proposal was discontinuous at 0). Opt-in policy, no replay impact → medium.

### M3 · Strategy deltas silently revert the applied machinery on resume
**`looplab/engine/strategy.py:274` · invariant-violation · CONFIRMED**

The autonomous Strategist consult records the decision **un-merged** with the active strategy.
Strategist decisions are *partial* dicts (a novelty-stance-only decision carries no `policy`).
`fold` then *replaces* `active_strategy` wholesale, and on resume `_reentry_repin` applies only
the last `active_strategy`. Because `_apply_strategy` uses `if pol:` guards that never reset
omitted fields, the **live** engine accumulates knobs across decisions while the folded/resumed
engine holds only the last decision's fields. Scenario: node k1 switches `policy greedy→mcts`
(live=mcts); node k2's decision is `{novelty_stance:'explore'}` with no policy, so the folded
`active_strategy` loses the policy, and **a resumed run selects nodes with the config-default
policy, not mcts** — a silent divergence of the search machinery from pre-crash state. The
operator-pin path deliberately *merges* onto `active_strategy` to avoid exactly this; the
autonomous path lacks the merge. **Fix:** record the decision merged onto `active_strategy`
(mirror the pin path), or replay `strategy_history` in order on resume.

### M4 · `agent_control` governance contract is unenforced for all but two knobs
**`looplab/core/config.py:208` · contradiction · CONFIRMED**

`config.py:208` documents "A setting ABSENT from this map is LOCKED — no agent may change it"
(restated in `configuration.md:216`). But the enforcement gate `_agent_may` is only ever consulted
for **`timeout`** and **`max_parallel`**. Every other knob the strategist applies
(`policy`, `novelty_stance`, `ablate_every`, `merge_mode`, `complexity_cue`,
`ablate_code_blocks`, `prefer_sweep`, `developer`) and the boss's `max_eval_seconds` are applied
**ungated**. So an operator who removes `policy` from the map to lock the search policy still has
the strategist switch it on the next consult — and the UI renders per-setting R/S/B pills
implying the lock takes effect. The map is decorative for every knob other than
`timeout`/`max_parallel`. **Fix:** either gate the strategist/boss knob applications behind
`_agent_may`, or narrow the docstring + doc to say only `timeout`/`max_parallel` are
runtime-gated. *(Related dead-seam, §7: the default map lists `"fidelity"`, which is not a
Settings field at all — the grant is inert.)*

### M5 · `reconcile_lessons` reads the shared lessons file outside the interprocess lock
**`looplab/engine/lessons_reconcile.py:209` · race · CONFIRMED**

`reconcile_lessons` does a read-modify-write of the **shared** cross-run `lessons.jsonl` but only
the *write* is inside the interprocess lock: `rows` is read unlocked, seconds of LLM
re-derivation follow, then the whole file is rewritten (`os.replace`) from that pre-lock
snapshot. A concurrent run sharing `memory_dir` (the supported AgentRxiv live-share) can
`O_APPEND` a lesson under the lock during the LLM window; that row isn't in `rows`, so the
rewrite **silently drops it**. `JsonlCaseLibrary.add` and `append_lessons` avoid this by reading
*inside* the lock; `reconcile` is the sole path reading outside. **Fix:** move the read inside
the lock and reconcile by identity, or drop-stale + `O_APPEND` the fresh derivations under the
lock. Best-effort store → medium.

### M6 · Run-end reflection lessons capped at 3, not the documented 8
**`looplab/engine/lessons_distill.py:225` · correctness · CONFIRMED**

`reflect_lessons` calls `parse_credit_lessons(out, 0)[:8]`, but `parse_credit_lessons`'s internal
cap is `if len(out) >= max(3, n_pairs): break` → `max(3,0)=3`, so it stops after 3 and the `[:8]`
slice is **dead**. A run with >3 distinct themes silently loses themes 4–8 from the cross-run
lessons store, despite the adjacent comment intending 8 ("bound at 8 as a runaway guard, not a
target"). **Fix:** give `parse_credit_lessons` an explicit count cap and pass 8 from
`reflect_lessons`, decoupled from the per-pair `max(3, n_pairs)` index-clamp floor.

### M7 · Improve path mislabels a carried-over parent pipeline on a degraded stages phase
**`looplab/adapters/repo_developer.py:777` · correctness · CONFIRMED**

On improve/refine, `_run` preloads `write.files = dict(base)` including the parent's
`looplab_stages.json`. If the STAGES phase *degrades* (exception / no-emit / invalid manifest) it
returns `[]` and **never clears** the stale parent entry, but `stage_note` is computed from the
phase *return* — so the implement session is told "NO pipeline stages are declared … train a
FRESH model", while the eval actually runs the parent's prep→train stages **plus** the operator's
score cmd. Net: the model is trained twice and the reported metric reflects the entrypoint's own
training, not the declared pipeline. The **repair** path does this correctly (reads
`write.files['looplab_stages.json']`); the fresh-improve path is the inconsistent one. **Fix:**
recompute `stage_note` from `write.files` after the stages phase, or clear the pre-seeded
manifest when the phase yields `[]`.

### M8 · Token-auth middleware comment falsely guarantees "never raw files"
**`looplab/serve/server.py:124` · security · CONFIRMED**

When `LOOPLAB_UI_TOKEN` is set, the middleware only gates GETs ending in `/artifact(s)` or
containing `/assistant/sessions`, justified by "Every OTHER GET only returns folded projections …
never raw files." That is **false**: `GET /api/runs/{id}/agents_md` returns `AGENTS.md` text,
`GET /api/{kind}` returns operator-authored prompt/skill/knowledge markdown, `GET
/api/runs/{id}/log` returns the **raw event envelopes** (source-of-truth `events.jsonl`, which
embeds solution code + captured stdout/stderr), and `GET /api/assistant/permissions` returns an
action whose `preview` is a snippet of the file about to be written. On a shared-origin /
multi-principal deployment (the scenario the token exists for) an untokened caller reads all of
this. **Fix:** extend the sensitive-GET predicate to gate these raw-content routes (or invert to
gate all `/api` GETs and allow-list the public ones). Conditional on token + shared origin →
medium.

### M9 · `repo_read` caps files at 200 KB but reports truncation as end-of-file
**`looplab/tools/knowledge_tools.py:120` · correctness · CONFIRMED**

`repo_read` paginates `read_file(...)`, whose default `max_bytes=200_000` truncates before the
paginator sees the content; `_paginate` then computes line count from the already-truncated
string and **omits the "… (more below)" resume marker** at the end of the slice. The tool spec
says a reply *without* that marker **is** the end of the file — so for any repo file >200 KB the
Researcher/Developer is told it read the whole file while everything past 200 KB is missing, then
proposes changes to code it never saw. The sibling `RepoScoutTools._read_file` reads the full
file first (the intended pattern). **Fix:** read the full file before paginating (or pass
`max_bytes=size+1`).

### M10 · `context_budget_chars=0` — doc says "120 k fallback", code says "compaction OFF"
**`docs/guide/configuration.md:131` · doc-drift · CONFIRMED**

The `agent_auto_summary` row says the compactor "falls back to a ~120k-char high-water mark only
if `context_budget_chars` is `0`". The code does the opposite: the 120 k fallback applies only
when the budget is **`None`** (unset); `0` is falsy so compaction is **disabled entirely**. This
contradicts the same doc's `context_budget_chars` row ("0 = off") **and** the load-bearing
`tool_loop.py:304-306` comment recording that the old "turn 0 into 120 k" behaviour was the exact
bug that was fixed. An operator setting `0` expecting a 120 k cap gets compaction fully off.
**Fix:** reword the row — the 120 k fallback applies only when the value is *unset*; `0` means
OFF.

### M11 · Prompt-override table omits two registered `repo_developer_*` keys
**`docs/guide/llm-and-agents.md:256` · doc-drift · CONFIRMED**

The page claims "Every built-in system prompt below can be replaced …" then lists 12 keys, but
omits `repo_developer_system_intro` and `repo_developer_system_body` — both in the enforced
`PROMPT_KEYS` registry and both live `render(...)` call sites driving the in-house repo-editing
Developer that the whole repo-edit flow depends on. **Fix:** add the two rows.

### M12 · Container eval drops engine env → confirm variance gate collapses
**`looplab/runtime/command_eval.py:384` · correctness · CONFIRMED**

On the Docker command-eval path, `run_argv` merges the engine-provided `env` only into the **host**
`docker run` client process; `make_docker_wrap` emits **no `-e` flags**, and Docker does not
forward host env into the container. So `LOOPLAB_EVAL_SEED` never reaches the eval. The confirm
pass runs multi-seed via `_run_eval(nd, workdir, {"LOOPLAB_EVAL_SEED": str(s)}, "full")`
(`confirm_phase.py:53-55`); inside the container each seed reads
`os.environ.get("LOOPLAB_EVAL_SEED","0")` → **`"0"` for every seed**, so all confirm seeds produce
identical results — collapsing the confirm/variance gate that is supposed to demote seed-lucky
leaders, and also defeating the seed-holdout disjointness (search-seed-0 and every confirm seed
both resolve to `"0"`). `DockerSandbox.run` forwards env via `-e` correctly; only the command-eval
path is broken, and only for untrusted/hostile RepoTasks (the trusted subprocess tier passes env
directly). **Fix:** thread the per-call `env` into `make_docker_wrap`'s inner `wrap` and emit
`-e {k}={v}` per key (redacting secret-named keys per `run_argv`'s `SECRET_ENV` policy); note the
seed varies per call, so it cannot be baked in at wrap-construction time.

---

## 6. Functional contradictions (focused list)

The maintainer specifically asked to confirm there are no contradictions. There are a handful of
real ones; the behavioural ones are the priority, the doc/comment ones are `CLAUDE.md`-class bugs.

**Behavioural contradictions (system disagrees with itself at runtime):**

- **Governance map advisory for all but two knobs** — M4. The documented lock is real for
  `timeout`/`max_parallel` and decorative for everything else.
- **Strategy machinery reverts on resume** — M3. Live engine state and folded/resumed state
  diverge because partial deltas aren't merged.
- **Loss metric negated under `min`** — M1. The brief's own "LOWER is better" framing contradicts
  its "report the negative" instruction.
- **MCTS bounded for `min`, unbounded for `max`** — M2. The same UCB-swamping the `min` branch
  documents-and-fixes recurs, unaddressed, on the `max` side.
- **Defensive `getattr` fallbacks disagree with the real schema defaults** (PLAUSIBLE, latent):
  `auto_install_deps` fallback `False` vs default `True` (`adapters/tasks.py:563`);
  `foresight_panel` fallback `1` vs default `2` (`cli/__init__.py:200,210`); `strategist_backend`
  fallback `'off'` vs default `'agent'` (`strategist.py:571`). No live misbehaviour (full
  `Settings` is always passed) and all fallbacks are the conservative choice — but they are a
  two-place inconsistency waiting for an incremental-construction refactor.

**Doc / load-bearing-comment contradictions:**

- **D-leak** · `trust/leakage.py:66` — docstring "not a hard gate" vs the wiring that makes
  `data_leakage:*` hard-gate under `gate`/`block`. Reconcile with H2.
- **`trust_gate` gate-vs-block** · `config.py:318` — the schema comment says `gate` keeps a node
  *breedable* and only `block` stops breeding; the code excludes from breeding under **both**
  (only `feasible=False` is block-exclusive). The `models.py` comments and `configuration.md:247`
  are correct; the schema comment is the stale outlier.
- **`novelty_semantic` "needs `novelty_gate` on"** · `configuration.md:173` / `config.py:263` —
  it also activates under `novelty_mode=algo` and the explore stance.
- **`declare_stages` "NOT in RepoWriteTools"** · `repo_developer.py:14` — it *is* in the toolset
  (deliberately, per the write-tool's own D1 comment); the two load-bearing comments contradict.
- **`misc.py` router-order docstring is inverted** · `misc.py:8` — says `/api/memory` is kept
  *after* `/api/{kind}`; the code (correctly) and the route's own comment say **before** (else the
  Memory panel 404s). Following the docstring would reintroduce the bug.
- **`_dep_failed` cache** · `triage.py:131`, `evaluate.py:164` — both comments name a cache that
  doesn't exist; the attribute is `_dep_attempted`.
- **Strategist default backend** · `strategist.py:10` (and `config.py:343`) — docstring says the
  default is `"llm"`; the actual default is `"agent"`.
- **generating-code.md undercounts from-scratch kinds** · `:49` — says "three" (code_regression,
  mlebench, mlebench_real); `dataset` also writes the whole solution (the same page and tasks.md
  say so) → four.
- **memory.md Cases schema** · `:18` — lists `operator`/`run_id`/`evidence` (never persisted) and
  omits `direction` (persisted and load-bearing for retain-on-improvement).
- **`--selected` "3 CPU-lite comps"** · `mlebench_prep.py:18` + `MLEBENCH.md:71,34` — the set has
  only 2 (insults was removed).
- **Untrusted-container hardening claimed but absent** · H3 — `generating-code.md:463` and
  `configuration.md:239-241` document `--cap-drop ALL` / `--security-opt no-new-privileges` /
  `sandbox_memory` / `sandbox_cpus` for the untrusted command-eval tier, but `make_docker_wrap`
  provides none of them. This is the most security-relevant contradiction in the set.

---

## 7. Low-severity findings (grouped)

**Robustness / hand-edited-log tolerance (latent under sole-writer):**
- `events/replay.py:91` — `_on_run_started` bare-subscripts `run_id`/`task_id`; a malformed
  `run_started` bricks the whole fold instead of degrading. (CONFIRMED)
- `events/replay.py:316` *(nit)* — `_on_confirm_eval` eval-cost accounting is only idempotent when
  the event carries both `node_id` and `seed`; an un-keyed duplicate double-counts. Unreachable
  today (sole emitter always writes both). (CONFIRMED)
- `engine/orchestrator.py:798` — fork/inject effect appended before its gate counter; crash-window
  duplicate on resume. (PLAUSIBLE)
- `core/parse.py:126` — `_coerce_value` misses PEP-604 `X | None` unions (`get_origin` returns
  `types.UnionType`, not `typing.Union`), so schema-aligned coercion is skipped for such fields.
  No currently-parsed model uses `| None`, so zero live impact; a trap for the next one.
  (CONFIRMED, reproduced)
- `core/hardware.py:33` — `detect_gpus` mis-columns memory for a GPU **name containing a comma**;
  the sibling `detect_gpu` handles it. Feeds a degraded hardware line into agent prompts.
  (PLAUSIBLE)
- `core/_pathsafe.py:82` *(nit)* — raw-string root `.resolve()` lacks the `OSError` guard its two
  siblings have; unreachable today (callers pass resolved `Path`s). (PLAUSIBLE)

**LLM client / context budget:**
- `core/llm.py:444` — the reasoning-reject retry branch omits the `attempt < _max_retries` guard
  its five siblings have; a reasoning-reject on the final attempt raises a misleading "no response
  after retries" instead of retrying without reasoning. (CONFIRMED)
- `core/context_budget.py:55` — `truncate_history` counts `tool_calls[].arguments` in the budget
  trigger but only ever trims `content`, so the deterministic-compaction fallback is a no-op for
  argument-heavy histories. Default path (`compact_history`) is immune; the fallbacks
  (summarizer-fails / degenerate-middle / `auto_summary=False`) are not. (CONFIRMED)

**Search / researcher prompt fidelity:**
- `agents/roles.py:295` — `_clamp_fill` injects swept dimensions as spurious fixed midpoint params,
  so the Developer prompt says a swept dim is *both* "sweep over [1,2,3]" and "fixed at 3.0".
  Bypasses the `_clamp_params_to_space` validator via direct mutation. (CONFIRMED)
- `agents/agent.py:182` — `_validate_emit` treats a sweep-only Idea (populated `space`, empty
  params/rationale/hypothesis) as EMPTY and bounces it with a wrong message; narrow trigger.
  (PLAUSIBLE; the finding's secondary "`_sanitize` drops space" claim was refuted)

**Engine liveness / cadence:**
- `engine/orchestrator.py:615` — the Developer-crash circuit-breaker pauses on the first
  `developer_crash` but the batch loop finishes the *rest* of the current create batch first
  (checks `paused` only at the next while-top), contradicting its "PAUSE on the FIRST" comment.
  Bounded blast radius (one batch). (CONFIRMED)
- `engine/research_cadence.py:48` — deep-research cadence first-fires one node early (`default=-1`)
  vs the report/lessons cadences (`default=0`). Audit-only sidecar. (CONFIRMED)
- `engine/strategy.py:40` — `_strategy_core` omits `timeout`/`max_parallel`, so a strategy
  differing *only* in those would be dropped by the change-detector. Fully latent (no producer
  emits them). (PLAUSIBLE)
- `engine/lessons_reconcile.py:268` — the `except` handler doesn't reset the change-gate hash on a
  re-derivation failure (unlike the client-None branch), suppressing a same-signature retry.
  (PLAUSIBLE)

**Memory / retrieval:**
- `engine/memory.py:572` — harmonic retrieval embeds the **raw** query against an
  **abstraction-keyed** index in `kb_search`/`CaseLibrary.retrieve`, diverging from
  `retrieve_lessons_harmonic` (which abstracts both sides and documents why). `CaseLibrary` is
  dead/test-only; `kb_search` is the live default path → retrieval-quality dampening. (PLAUSIBLE)

**Serve / tools:**
- `serve/routers/runs.py:540` — `put_run_config` hardcodes `{"llm_api_key"}` instead of the shared
  `_SECRET_FIELDS`, breaking the single-source contract; latent plaintext-leak if a second
  `SecretStr` field is ever added. (CONFIRMED)
- `serve/routers/assistant.py:432` — streaming endpoint leaves a dangling user turn on offline
  soft-fail (non-stream endpoint appends an error bubble; stream doesn't). (CONFIRMED)
- `serve/routers/assistant.py:491` — the SSE generator holds a shared anyio threadpool thread via
  blocking `q.get(timeout=10)`; the sibling run-events stream avoids this. (PLAUSIBLE)
- `serve/tui_format.py:104` — the TUI proposed-run panel omits goal/task/repo for composable
  (kind-less) genesis tasks. Display-only. (CONFIRMED)
- `tools/machine_runs_tools.py:682` — `delete_node` rewrites the source-of-truth `events.jsonl`
  **non-atomically** (a `.bak` is copied first, so recovery is manual not automatic); the same
  module uses `atomic_write_text` for the far-less-critical snapshot. (CONFIRMED)
- `tools/machine_runs_tools.py:686` — `delete_node` parses `spans.jsonl` *after* rewriting events,
  with no guard, so a torn spans line leaves a half-applied deletion reported as failure.
  (CONFIRMED)
- `tools/perm_modes.py:59` — `DEFAULT_PROTECT` grader globs `**/*grade*.py` over-match `upgrade.py`
  / `downgrade.py`, silently locking legitimate files with no override. (CONFIRMED)
- `tools/_mcp_transport.py:29` — `_ServerHandle` leaks its thread/loop/subprocess when boot exceeds
  the 30 s wait but later succeeds (the "can't leak" comment reasons only about the failure path).
  (CONFIRMED)
- `runtime/sandbox.py:178` — `run_argv` docstring describes a `communicate()` fast path (keyed on
  `cancel`/`log_path` being None) that no longer exists — the impl always drains via `_tee_drain`;
  a load-bearing comment right below even explains *why* it was removed (host-memory DoS). Stale
  docstring only, no behaviour change. (CONFIRMED)

**Trust panel noise (advisory-only, no gating):**
- `trust/reward_hack.py:128` — `perfect_metric` false-positives on any `max` metric ≥ 1.0; the
  `min` sibling was narrowed to the exact floor, the `max` side wasn't. Advisory-only. (CONFIRMED)

**Projections (derived, non-source-of-truth):**
- `events/readmodel.py:42` & `events/htmlview.py:67` — store/show **raw** `n.metric` while `is_best`
  / the "Best:" line follow the **robust** (confirmed-mean/holdout) selection, so an external
  `ORDER BY metric` (or the same HTML page) can disagree about which node is best. The sibling
  `digest.py` uses `robust_metric` consistently. (CONFIRMED)

**CLI:**
- `cli/run_cmds.py:297` — `run` on a *paused* run dir silently does nothing and prints the stale
  best (only `finished` is handled; `resume` handles paused). (CONFIRMED)
- `cli/inspect_cmds.py:58` — `timings` passes `dicts_only=False`, so a valid-JSON-non-dict corrupt
  span line crashes the command instead of being skipped, contradicting its guarding comment.
  (CONFIRMED)

**Config hardening:**
- `core/config.py:259` — enum-like fields (`novelty_mode`, `seed_mode`, `eval_trust_mode`,
  `strategist_backend`) aren't validated at config time the way `trust_gate`/`merge_mode` are, so a
  typo (`NOVELTY_MODE=LLM`) silently disables the gate instead of failing loud. (CONFIRMED)

**Dead seam:**
- `core/config.py:225` — `agent_control` default lists `"fidelity"`, which is not a Settings field;
  the grant is inert and no UI pill can bind to it. (CONFIRMED)

---

## 8. Nits

- `engine/orchestrator.py:1238` — `_rerun_node(self, node: Node, ...)` annotates with an
  **unimported** `Node`; harmless at runtime (`from __future__ import annotations`) but
  `get_type_hints` raises `NameError`. (CONFIRMED)
- `tools/shell_tools.py:90` — `run_command` spec advertises a stderr floor (~1900) that
  `_stream_tails` can't guarantee (true worst case 1800). (CONFIRMED)
- `tools/mcp_tools.py:87` — `McpTools.execute` truncates at 8000 chars, 2× the loop's
  `RESULT_CAP` (4000), against the "derive budgets from RESULT_CAP" convention. (CONFIRMED)
- `adapters/regression.py:124` *(offline toy baseline only)* — multiplicative lambda perturbation
  is absorbing at zero, so a `lam=0.0`-rooted subtree can never introduce ridge. (PLAUSIBLE)
- `adapters/mlebench_real.py:194` — brief/schema hard-code `train.csv`/`test.csv` while `assets()`
  accepts any `train*.csv`; latent coupling, no current trigger. (PLAUSIBLE)
- *(docs)* `README.md:6` — Tests badge (1147) and "~1150 tests" prose are stale; actual ≈1706.
  `mkdocs.yml:99` — `docs/13-external-works-analysis-2026-07.md` is orphaned from both `nav` and
  `not_in_nav` (every sibling review doc is in `not_in_nav`). (CONFIRMED)

---

## 9. Refuted (checked and cleared)

These were reported by a finder and **dropped** in verification — recorded so the same ground
isn't re-litigated:

- **`kill_background` bypasses the approver** — the comment was misread; SIGTERM-ing a process
  group is correctly treated as a side effect and denied only in read-only plan mode.
- **`validate_strategy` whitelists unreachable `timeout`/`max_parallel`** — factually accurate that
  no shipped emitter produces them, but it's a deliberate forward-compat whitelist, not a bug (this
  is the same latent seam noted non-critically at `strategy.py:40`).
- **`_ablate` repo-skip appends a domain event without `_write_lock`** — benign; `_ablate` runs
  only in the sequential main loop, so there is no concurrent writer to race.
- **`trust_gate_changed` is an undocumented sole-writer exception** — it **is** documented in the
  `events/types.py` registry as an allow-listed, fold-safe control event.

---

## 10. Suggested priority order

1. **H1** `engine/ablation.py:41` — engine can hang / burn the whole budget on repo+ablation runs.
2. **H2** `trust/leakage.py:86` + **D-leak** docstring — stop barring honest winners; reconcile the
   "not a hard gate" doc in the same change.
3. **H3** `runtime/command_eval.py:379` — harden the untrusted command-eval container to match the
   `solution.py` tier and the two docs (critical if you run untrusted RepoTasks multi-tenant).
4. **M1** `adapters/dataset_task.py:319` — objective inversion selects the worst model under `min`.
5. **M3** `engine/strategy.py:274` — resume no longer reconstructs the search machinery faithfully.
6. **M12 / M4 / M8 / M5** — dropped-env confirm-gate collapse, governance-contract contradiction,
   token-auth raw-file gap, lessons-file race.
7. **M2 / M6 / M7 / M9 / M10 / M11** — search quality, lessons cap, stage mislabel, pagination
   EOF, and the two doc contradictions.
8. Sweep the doc/comment contradictions in §6 and §7 (they are `CLAUDE.md`-class bugs) and the
   config-time enum validation (`config.py:259`).

---

## 11. Diagram & schema accuracy — clean

`docs/infographic/agent-architecture.html` is **data-driven** (a `B` block map + `E` edge list in
its inline `<script>`); `CLAUDE.md` mandates its numbers/cadences/thresholds be verified against
`looplab/`. A high-effort audit enumerated every concrete claim — config defaults, engine cadence
math, event-type names, the signal-delivery registry, digest composition counts, and
stage-to-stage edges — and found **no drift**. Spot-verified as correct against the code:

- `novelty_mode=llm` default with the `algo` gate opt-in; `novelty_semantic_threshold=0.92`,
  `novelty_epsilon=0.05`; the nudge `seed=id*1009+7`, `scale=max(|p|,1)*0.1` (`novelty.py:216,219`).
- `n_seeds=3`, `holdout=0.25`, `holdout_top_k=3`; confirm OFF by default, `confirm_seed_base=1`.
- `deep_research_every=3`, `strategist_every=3`, `strategist_backend="agent"`.

**The process diagram is well-maintained and matches the code** — a credit to the "keep the docs
and the process diagram in sync in the SAME change" discipline. (The settings-table accuracy is a
separate axis: see M10, D-`trust_gate`, D-`novelty_semantic`, and the memory.md Cases-schema
drift for the table/guide mismatches that *were* found.)

## 12. Runtime & sandbox hardening

The runtime-sandbox scope surfaced the most consequential *security* finding in the review (H3),
a related correctness bug (M12), and one doc-drift (the `run_argv` docstring, §7). The through-line
is that the **RepoTask command-eval Docker path** (`make_docker_wrap`) is a second, weaker Docker
tier than the `solution.py` path (`DockerSandbox.run`): the former omits the container hardening
and per-call env forwarding that the latter has, even though both serve the *same*
untrusted/hostile trust modes. H3 and M12 are written up in §4 and §5; the docstring drift is in §7.

- **H3** `command_eval.py:379` — untrusted command-eval container missing `--cap-drop ALL` /
  `--security-opt no-new-privileges` / `--cpus` / `--memory`; contradicts two docs. (CONFIRMED, high)
- **M12** `command_eval.py:384` — engine env (incl. `LOOPLAB_EVAL_SEED`) dropped inside the
  container, collapsing the confirm variance gate. (CONFIRMED, medium)
- `sandbox.py:178` — `run_argv` docstring describes a `communicate()` fast path that no longer
  exists (always drains via `_tee_drain`). (CONFIRMED, low — §7)
