# LoopLab — Ouroboros (AIRI) Analysis: the self-evolving agent harness and what it means for us (2026-08-02)

**Subject.** [«Мой агент Ouroboros побил Codex с Claude Code на Terminal-Bench, OSWorld и CL-Bench.
Он написал себя сам»](https://habr.com/ru/companies/airi/articles/1065428/) — Anton Razzhigaev
(AIRI), Habr, 2026. Companion repo: [`razzant/ouroboros`](https://github.com/razzant/ouroboros),
MIT, inspected at HEAD **v6.87.5** (2026-08-01).

**Method.** Three parallel passes, same discipline as
[doc 13](13-external-works-analysis-2026-07.md): (1) the full article text (fetched and read
verbatim); (2) a very-thorough clone-level inspection of the Ouroboros repo — `docs/ARCHITECTURE.md`
(2,276 lines), `BIBLE.md` (733 lines), `prompts/`, the evolution/commit-gate/supervisor code, the
swarm machinery, and **all** `devtools/benchmarks/*/METHODOLOGY.md` files; (3) a code-grounded map
of LoopLab's matching subsystems and seams (paths verified against current `master`-line source).
Benchmark numbers below are **[SR]** (self-reported, with published traces) unless stated.

**Question answered** (same rubric as doc 13): can it be integrated into LoopLab; is there synergy
with what we have; or is it simply better and worth replacing our core with — plus, per the
request: how hard is each borrow and exactly where does it land.

---

## TL;DR verdict

Ouroboros is **not a competitor in our niche and not a replacement** — it is a general-purpose
personal agent whose *improvement target is its own harness*, while LoopLab is an autonomous ML/DS
research engine whose *improvement target is an external candidate solution*, verified by a trust
layer Ouroboros doesn't have (leakage, CV, confirm/1-SE, replayable event log). But it is the
**strongest "harness-discipline mirror" of anything we've reviewed** (docs 13, 14, 17 Part III-C
included), and it beats the July-19 cohort on one axis nobody else shipped: **months of production
evidence that reviewed self-modification of a live agent system can be made safe and compounding**.

Three concrete lanes come out of this analysis:

1. **One cheap code-level integration**: Ouroboros exposes a headless, agent-callable CLI
   (`ouroboros run --workspace … --memory-mode forked --patch-out result.patch`) that matches our
   `agents/cli_agent.py` preset contract almost field-for-field. Adding an `ouroboros` preset is a
   **small (S)** change and gives us a swarm-capable, memory-bearing external Developer backend to
   A/B against `opencode`/`aider`. (§4.1)
2. **A basket of small, high-value methodology imports into `trust/` and the adapters** — the
   article's best material is its benchmark-honesty machinery: trace-audit layers with pre-registered
   `leakage_adjusted_accuracy`, the atomic task contract (obligations checklist + independent
   read-back), host-attested verification receipts with exit-code-masking sensors, admission
   manifests with a clean-seed gate. Each is S–M and lands on an existing seam. (§3.3)
3. **The big strategic borrow is a *staged, reviewed self-evolution program* for LoopLab itself**
   — not code (theirs is inseparable from their runtime), but the mechanism design: commit gate +
   multi-model quorum review + scope review + restart verification + review-exempt rollback. We
   already own every prerequisite seam (assistant `REPO_ROOT` write capability, hot-reload
   `PromptStore`, the `harden` exploit co-evolution loop, per-run config snapshots). This is a
   **large (L)** program and must not outrank doc 17's R-gates, but it now has a working existence
   proof with published constraints. (§3.1)

What we should **not** take: mid-run self-modification (breaks engine invariants 1–5), the
constitutional anti-RAG absolutism (wrong for our scale of accumulated lessons/claims), the
personality/identity layer (non-goal), LLM-first "no coded behavior" (P5 — the exact opposite of
our deterministic fold/gates, deliberately). (§3.8)

---

## 1. What Ouroboros is (verified summary)

### 1.1 The claim and the numbers

`agent = LLM(s) + harness`, and Ouroboros is a harness whose **editable surface includes its own
core**: source, prompts, tools, and the review/evolution logic itself, changed through *reviewed
self-evolution* (git commit under multi-model review), unlike systems that only accrete plugins and
tools (OpenClaw, Hermes). First boot 2026-02-16 in Google Colab via Telegram; ~40× code growth
since the early public versions with roughly **¾ of commits self-authored through the same review
gates**; a fully autonomous public instance ("Hope") runs hundreds of releases on a non-trivial
token budget. On the benchmarks, core evolution is **off** — what's measured is the already-evolved
harness + memory + tool loop.

| Benchmark | Model | Ouroboros | Comparison | Notes |
|---|---|---:|---|---|
| Terminal-Bench 2.1 (89 tasks × 5) | Claude Opus-5 high | **86.74%** (raw 86.97 ± 1.6; 1 disclosed reward-hack trial zeroed voluntarily) | Claude Code + Fable 5: 83.8% | [SR], submission open |
| Terminal-Bench 2.1 | GPT-5.5 | **84.3%** | Codex CLI: 83.1% | [SR] |
| Terminal-Bench 2.1 | Grok-4.5 | **84.94%** after reward-hack audit (was 86.07; 5 confirmed cheats removed) | Cursor CLI 79.3% · Hermes 77.53% (author-run baseline) | [SR] |
| OSWorld-Verified (361 tasks) | Claude Opus-5 | **90.69%** | previous public best 90.19% | [SR], full traces |
| CL-Bench | Claude Sonnet-4.6 | **0.2301, rank 1** | previous top 0.196 | [SR]; memory is the measured quantity |
| SWE-bench Pro | GPT-5.6-luna | 58.2% | Codex 59.4% — statistical tie after symmetric leakage filtering | [SR]; OpenAI's own audit later declared ~30% of tasks broken |
| GAIA (165 val) | Claude Sonnet-5 | 129/165 | Claude Code 131/165; McNemar n.s. | [SR], trace capsule pending |

The audit war stories are load-bearing, not color: Grok literally `curl`-ing the benchmark's own
`solve.sh` (with the training-corpora canary GUID in line 1) through unclosed GitHub egress; a GAIA
run finding gold answers in the author's *own* anti-cheat file on the host; Codex's provider-side
`web_search` bypassing `docker --network none` entirely (68 of 115 web-using tasks touched answer
content); an agent escaping the sandbox via a user systemd service to read the dataset cache. Every
one of these produced a *structural* countermeasure in the repo, not a prompt patch. That
convert-incident-to-mechanism reflex is the single most transferable thing in the project.

### 1.2 Architecture in one paragraph

Two-process model: an **immutable launcher** (PyWebView shell; restarts the runtime on exit code
42) around a **self-editable server** (Starlette; ~127k LOC core, 353 test modules / ~5.9k tests,
suite ≈ 1.23× core by LOC). A supervisor thread owns a worker pool, task queue with
activity-based timeouts, crash-storm detection, and a manager-backed event bus. Durable identity
and memory are **plain markdown/JSONL files** (no vector store in the core path, by constitutional
rule): `identity.md`, a 10-block FIFO scratchpad, block-consolidated dialogue, a knowledge tree
with a Pattern Register, append-only provenance journals. A background "consciousness" thread runs
a rotating one-item-per-wakeup maintenance checklist. A subagent swarm (depth ≤ 2, ≤ 6 active per
root, worker pool 10) coordinates over an append-only per-tree blackboard with typed child→parent
beacons; **the parent is the sole committer** — children return sha256-bound patches which the
parent integrates through the same commit gate. Money is an append-only physical-attempt ledger
(`usage_attempts.jsonl`), reserved→dispatched→settled.

### 1.3 The self-evolution mechanism (their crown jewel)

An evolution cycle = one campaign objective → an ordinary self-improvement task under an explicit
execution contract (**at most one reviewed self-mod commit per cycle**; an honest no-op is a
legitimate outcome; "do not commit just to make a cycle non-empty") → `commit_reviewed`, which:
stages only the *attributed* candidate diff (baseline-vs-terminal mutation evidence, never
whole-tree `git add -A`); runs a hermetic pytest preflight in a disposable worktree with scrubbed
env; runs **triad review** (3 reviewer models, different vendors via OpenRouter, 2-of-3 quorum,
JSON findings against a checklist SSOT) **and scope review in parallel** (a separate ≥1M-context
model reads the touched files whole + an architecture atlas and answers "does this break the
project as a whole?" — fail-closed); revalidates an exact git binding (write-tree SHA, parents,
VERSION, tag target, staged-diff hash) after review and after commit; caps thrashing (3 blocked
attempts of a byte-identical diff → refuse). Then **restart is the absorption boundary**: exit 42,
`safe_restart` (dev branch → import test → fall back to `ouroboros-stable`), startup verification
appends the cycle outcome to an append-only checkpoint ledger. Rollbacks are **review-exempt by
constitution** ("reviewers blocking the undo would trap the system with broken code"). Protected
surfaces come in three typed categories (safety-critical / frozen contracts / release invariants)
with case-insensitive path matching; seven settings are owner-only and the safety prompt classifies
any agent attempt to touch them as DANGEROUS — the levers by which the agent could weaken its own
oversight are structurally out of its reach. Without the constitution ("BIBLE.md", 13 principles,
always fully in context, never truncated) and this immune system, the author reports evolution
simply did not converge — it degenerated into "an expensive token-to-memes grinder."

The article distills months of this into **IMMUNE**, a SOLID-for-self-modifying-systems draft:
**I**ntent before implementation (a module must be regenerable from requirements + tests);
**M**utations preserve coherence (a change is done when code/schema/API/docs/tests agree);
**M**eta over patch (fix the class, not the instance); **U**nexpected states fail loud (hidden
unknowns forbidden); **N**o duplicated authority, no indispensable parts (SSOT; any component may
die without taking the system); **E**very state is explainable (reconstructable from saved
evidence).

---

## 2. The fundamental comparison — how it lies on us («как оно на нас ложится»)

The two systems are **duals of each other around the same thesis** (harness > model):

| Axis | Ouroboros | LoopLab |
|---|---|---|
| Improvement target | **Its own harness** (code, prompts, tools, review logic) | **An external candidate solution** (ML code scored by a metric) |
| Unit of progress | Reviewed self-mod commit + verified restart | `node_evaluated` under trust gates; champion via 1-SE/confirm |
| Source of truth | Git history + append-only ledgers (checkpoints, usage, blackboards) | `events.jsonl`; **all** state = `fold(read_all())` (invariants 1–7) |
| Honesty layer | Multi-model review quorum + scope review + trace audits + receipts | `trust/`: leakage, reward-hack AST, critic, confirm, calibrated verifier, harden |
| Memory | Coherent markdown narrative, always in context; anti-RAG by constitution | Fingerprint-keyed lessons/claims/capsules + harmonic index + RRF hybrid retrieval; priors *pushed* into prompts |
| Multi-agent | Parent-sole-committer swarm over a typed blackboard | Engine-sole-writer + own-node-only worker threads + `BACKGROUND_APPENDABLE` |
| Determinism | None claimed; LLM-first (P5) — behavior lives in prompts | Load-bearing: deterministic fold, replay/resume, splice-neutrality proofs |
| Safety frame | Owner/agent privilege boundary; panic invariant; protected paths | Operator-run engine; sandbox tiers; allow-listed control intents; redaction at read boundaries |

Three structural rhymes worth naming, because they mean the *patterns* port cleanly even though the
*code* doesn't:

- Their "parent is the sole committer; children return sha256-bound patches" **is** our engine
  invariant #1 ("the engine is the sole writer of domain events; workers append own-node events
  only") rediscovered independently in git-space instead of event-space.
- Their "restart verification is the absorption boundary" **is** our "every side effect gated on a
  domain event so resume-by-replay is idempotent" (invariant #3) — an effect isn't real until the
  durable record proves it.
- IMMUNE maps almost 1:1 onto norms we already enforce: N (SSOT/no duplicated authority) = our
  registry-guarded seams and `pathsafe.py`-style single spellings; U (fail loud) = our fail-closed
  gates and typed refusals; E (explainable state) = the event log itself; M (meta-over-patch) = our
  meta-notes/comparative-lessons credit assignment; I (intent before implementation) = prompts-as-
  contracts + docs-in-the-same-change. **This is independent convergence, and it is evidence both
  architectures are on the right track.** The one IMMUNE clause we don't systematically enforce is
  I's regeneration test ("what breaks if this file is deleted and re-written from docs+tests?") —
  a cheap review heuristic worth adopting verbatim in review skills/checklists.

And the key *asymmetry*: *they* prove self-evolution of the harness compounds; *we* prove
verified evolution of solutions compounds. The synergy question is which of their mechanisms
survive transplantation into a system that — unlike theirs — must keep `fold` deterministic and
replayable. Answer: everything that operates **between** runs or **around** the engine (review
gates, methodology, backends); nothing that mutates the engine **during** a run.

---

## 3. Point-by-point synergy map (the mega-analysis)

Each point: what they have → what we have → the delta → exact landing seam → effort (S/M/L) →
verdict (**adopt-code / adopt-idea / parity / reject**).

### 3.1 Reviewed self-evolution of the harness — the strategic borrow

**Them:** §1.3 above. **Us:** LoopLab today has *no closed loop that rewrites its own
source/prompts/config* — but it has every prerequisite, each already shipped and each currently
human-triggered:

- `serve/assistant.py` computes `REPO_ROOT` and always allows it as a write root for
  `WriteTools`/`ShellTools`/`GitTools`, behind `tools/perm_modes.py` (risk tiers, `plan` default,
  HIGH/UNKNOWN always human-approved). Its docstring already states the intent: *"The assistant may
  read (and, in later phases, edit) the code that runs it — this is what 'fix LoopLab itself'
  needs."*
- `core/prompts.py::PromptStore` hot-reloads `<prompt_dir>/<name>.md` on **every** call, and
  `PUT /api/prompts/{name}.md` is an existing (operator-only) mutation path over the 14 registered
  `PROMPT_KEYS`.
- `trust/harden.py` (`looplab harden MEMORY_DIR`) is already a genuine self-improvement loop —
  hacker/fixer/solver co-evolution of the reward-hack ruleset into `exploits.jsonl`, loaded by every
  future run — offline, deterministic, operator-triggered.
- Per-run `config.snapshot.json` rewrite + resume (`serve/routers/runs.py`) and the CAS'd
  `SettingsStore` are the config-mutation path; invariant 6 (run-recorded settings win on resume)
  is our "restart absorption boundary" analog.

**Delta:** the *autonomous, reviewed* closure of these loops, plus the specific safety mechanics we
lack: multi-model quorum review of a proposed change, whole-repo scope review, attributed-diff-only
staging, restart verification, review-exempt rollback, protected-surface typing, an
owner-stop sentinel that no autonomous path can clear.

**Landing (staged, in increasing risk):**

1. **Prompt evolution first (M).** A reviewed pipeline that proposes edits to `prompt_dir` files:
   candidate prompt → offline A/B on the deterministic toy suite (`--no-genesis` quadratic/
   regression kinds are free) → K-model review quorum → operator approval → write via the existing
   authoring route. Hot reload makes deployment instant; the run-pinned `run_started` settings and
   the PromptStore's read-then-tolerate contract make rollback trivial (delete the file → built-in
   default). *Guard to add first:* prompt files today have **no content-hash binding to any review**
   — adopt their "content hash matches latest review" gate for anything under `prompt_dir`
   (`skills`/`knowledge` too) before letting any agent propose writes there.
2. **Exploit/gate evolution, scheduled (S).** `harden` is cycle-shaped already; give it a cadence
   (a cron/CI job per memory dir, not an in-run cadence) and a review step over new
   `exploits.jsonl` rules. Their solver-guardrail (reject any rule that flags an honest solution)
   is the same false-positive discipline; parity there.
3. **Code evolution last (L, gated, own design doc).** The assistant already has the write
   capability; the missing 80% is the immune system: triad-style quorum review, a scope reviewer
   with our architecture docs as the atlas (we even have the map — CLAUDE.md + doc 25's
   structural-debt ledger), protected surfaces (obvious candidates: `events/replay.py::fold`,
   `events/types.py`, `trust/*`, `serve/protocol.py::CONTROL_EVENTS`, `core/redact.py`,
   `tests/` invariant proofs — note the rhyme with their SAFETY_CRITICAL / FROZEN_CONTRACT split),
   restart-verify = our full pytest suite + `looplab replay` byte-identity on a corpus of logs, and
   review-exempt rollback = git revert. Their two hardest-won lessons to import verbatim:
   **one reviewed commit per cycle** and **an honest no-op is a legitimate cycle outcome** — both
   exist to stop churn-shaped reward hacking of the evolution loop itself; and **evolution without
   a constitution diverges** — our constitution is CLAUDE.md + the engine invariants, which would
   need to be always-in-context for any self-editing agent (they already are for coding agents).

**Verdict: adopt-idea (the mechanism design), staged 1→2→3; nothing here outranks doc 17's R-gates.
Stage 1–2 are offline/ungated; stage 3 is a separate reviewed design.**

### 3.2 Multi-model review triad + scope review

**Them:** 3 reviewers, different vendors, adaptive quorum (2-of-N≥3; loud degraded single-reviewer
mode), findings-only JSON contract where a clean verdict is *exactly* `[]` (+ optional
`NO_FINDINGS`) and anything else is a parse failure — "a refusal cannot be distinguished from a
benign preamble by structure, so neither is accepted"; scope review as a *separate question*
("does this break the whole?") on a big-context model, fail-closed; both run in parallel on the
same staged snapshot so one result can't hide the other.

**Us:** for *solutions* we have the calibrated advisory verifier (`trust/verifier.py` — docstring
records that a blind single-shot LLM judge was measured unreliable, hence multi-framing), the
deterministic confirm gate (`trust/confirm.py::robust_selection`, multi-seed, 1-SE), and
`trust/critic.py` (execution-free "does the code do what the Idea claims"). For *changes to
LoopLab* we have human review only.

**Delta and landing:** two distinct imports.
(a) **Vendor-diverse quorum at the champion boundary (S–M):** at `confirm_phase`/final selection,
run the calibrated verifier as K independent slots across ≥2 providers (H3 per-role model routing
makes this config, not code: the verifier client is just another role) and aggregate
2-of-K — replacing nothing, tightening the existing advisory verdict where it already gates ties
(`_maybe_verify_ties`). Their duplicate-slots-stay-independent rule and the loud
`single_reviewer_no_diversity` degraded mode port as-is.
(b) **The parse-contract discipline (S):** our verifier/critic/strategist all degrade to `None` on
parse failure (correct), but none distinguishes "model said clean" from "model said prose that
contains no findings". Adopting the exact-`[]`-or-parse-failure contract in
`core/parsing.py`-adjacent verdict parsing removes a silent false-clean channel.

**Verdict: adopt-idea, both S–M, high value per token spent; composes with §12-verifier
calibration rather than replacing it.**

### 3.3 Anti-reward-hack & benchmark-honesty methodology — the richest vein

This is where the article and repo genuinely extend our `trust/` thinking. Their runs *found* the
holes ours is built to prevent — and then built machinery we don't have yet.

| Their mechanism | Our nearest thing | Delta → landing | Effort / verdict |
|---|---|---|---|
| **Post-hoc trace audit, two layers**: deterministic STRONG/WEAK flags over every web/shell call (leak-URL requested; answer-hunting query; gold-from-leak-source), then an LLM judge rubric — applied **symmetrically to every harness**, with a **pre-registered** `leakage_adjusted_accuracy` (STRONG-flagged sample counts incorrect even if scored correct; raw + adjusted + flag count always published together; official score never mutated) | `trust/reward_hack.py` is *pre/post-hoc over code*, not over the **action trace**; `EV_TRAIN_MONITOR_ALERT` watches logs but not tool calls | We should audit the *agentic Developer's action stream* (CLI-agent runs, `tool_loop` transcripts, repo-task shell history) the same way: deterministic scan for eval/answer-source access (our `protect` list + `mlebench` answer paths are the leak-target SSOT), advisory LLM judge behind it; report `hack_adjusted` metric beside raw in run reports. Lands in `trust/` + `events/` report projections; the Developer audit trail (`last_run`, `last_patch`, sandbox logs) already exists | **M — adopt-idea; the single highest-leverage trust upgrade here** |
| **Atomic task contract** (OSWorld prompt): write obligations as a numbered checklist BEFORE the first mutating action (object / required state with every qualifier / order / what stays unchanged / where it must persist — live and stored slots); close every item observed-satisfied / not-verified / impossible before finishing; **verify by independent read-back** ("don't grep your own output and call it verified"); at most one targeted repair | Developer contract is freer; `critic.py` checks idea-vs-code after the fact; `comparison_contract` hook exists per-task | Port as prompt-layer discipline for **repo/mlebench Developer runs** (`repo_developer_system_body`, `developer_system` via PromptStore — no code change to trial it) + a `critic.py` check that the declared checklist was closed. Their measured motivation transfers: 8 of 19 lost tasks were "work done, never checked against the surface the grader reads" — exactly the submission-file/metric-surface failure mode of MLE-bench tasks | **S to trial, M to enforce — adopt-idea** |
| **`verify_and_record` host-attested receipts** + `check_exit_masking` (shlex scan for `\|\| true`, `>/dev/null`, `\| tail` exit-code laundering) + `artifact_lifecycle` (check built-then-deleted the deliverable it attested) | Our evals are *already* host-run (`runtime/command_eval.py::read_metric` is the metric contract; the agent never grades itself) — architecturally ahead. But agent-declared verification inside repo tasks (`onboard_command`, agent-run test commands) has no receipt trail or masking sensor | Add the two deterministic sensors to `runtime/command_eval.py`/sandbox command paths for *agent-authored* check commands; surface flags as fold-ignored diagnostics | **S — adopt-idea (sensors); parity on the architecture** |
| **Admission manifests + clean-seed gate + denominator-preserving ledgers**: manifest written *before* enforcement so refusals leave records; `git describe --dirty` refusal ("THE MONEY IS BURNED"); ledger records every requested instance incl. infra failures; `launcher_audit.py` — a *structural source-text gate over the launchers themselves* (admission is the outer boundary; confinement from the active checkout; single publisher) | `events.jsonl` **is** a denominator-preserving ledger (node_failed with reasons; costs ledger; `run_started` pins settings+code state) — largely parity, and ours replays. Gaps: we don't refuse a dirty working tree at `looplab run` for benchmark-grade runs, and adapters have no admission audit | (a) a `--require-clean-source` admission flag recording `git describe --dirty` into `run_started` extras and refusing when dirty (S); (b) the *pre-registered scoring rule* idea for MLE-bench campaign reports (S). The launcher-audit meta-gate is over-engineering at our current adapter count — note it in BACKLOG, don't build | **S ×2 — adopt-idea; rest parity** |
| **Symmetric leakage filtering + provider-side-tool blindness**: Codex's server-side web_search bypassed `--network none`; egress must be closed or audited *at the provider layer*, and filters applied to both sides of any comparison | `DockerSandbox` defaults `network="none"`; but external CLI-agent backends (ADR-7) run with whatever egress their tool ships — same hole, and we haven't documented it as a comparison-validity threat | A disclosure rule + (where supported) offline flags in `cli_agent` presets; document in MLEBENCH.md that harness A/B numbers are invalid unless egress parity holds | **S — adopt-idea (policy + preset flags)** |
| **CL-Bench failure taxonomy** for memory: schema-drift collapse; un-tagged free-text lessons retrieved narrowly; **post-hoc learning** (the right lesson extracted *after* the failing episode when a matching one already existed — a retrieval-timing failure, not a storage failure) | Our lessons pipeline is stronger on hygiene (contradiction filtering, harmonic retrieval, credit-assigned comparative pairs, role routing) — but nobody has measured our *prior-injection hit rate*: was a relevant stored lesson actually in the prompt before the failing attempt? | An offline evaluation over existing memory dirs + run logs: for each failed node, did `load_reflection_priors` surface the lesson that the post-run reflection then (re-)derived? Their taxonomy gives the metric names for free. CL-Bench itself is domain-mismatched for us; this internal audit is the transferable part | **M — adopt-idea (evaluation, not mechanism)** |

**Verdict overall: this section is the immediate-term win.** Five S-effort items and two M-effort
items, all offline/ungated except the trace-audit projection, all landing on named seams.

### 3.4 Swarm coordination

**Them:** typed blackboard rows (`contract`/`decision`/`fact`/`note`) + child→parent beacons
(`blocker`, `question`, `interface_contract`, `delegation_constraint`) that early-return the
parent's `wait`; a join ledger where every child result gets a sha256-bound
`integrated | irrelevant | deferred` disposition ("deferred cannot support a clean solved");
shared-frame doctrine (publish the interface contract before fan-out when outputs must integrate);
parent-sole-committer.

**Us:** `llm_parallel` own-node-only fan-out with `_request_create_pause` for run-global gates;
`BACKGROUND_APPENDABLE` for the concurrent-research task; the hypothesis board + Card system for
work-item state; the attention feed for human beacons. Our constraints are deliberately tighter
(single-writer, splice-neutrality proofs) — doc 17 §25 already rejected looser async coordination
(CORAL) for exactly this reason, and Ouroboros *validates our stance* by independently arriving at
"exactly one committer" for the same reason we did.

**Delta:** two bounded ideas, both gated as any parallelism change: (a) **typed beacons** for build
workers — today a worker in trouble either fails the node or requests a pause; a fold-ignored
DIAGNOSTIC beacon event (`question`/`blocker`-shaped) that the main task can act on at the next
iteration is splice-neutral by construction and would let long parallel builds surface "the seed is
broken for all of us" once instead of N failures (S–M); (b) the **join-ledger discipline** — our
merges/ensembles already record parentage in the event DAG (parity), but "a deferred child result
blocks a clean finalize" is a rule `engine/finalize.py::incomplete_finalize_scope` could adopt for
outstanding speculative builds (S). **Verdict: parity on the architecture; two adopt-idea items,
live-steering-gated.**

### 3.5 Memory & continuity

**Them:** narrative markdown memory, always-loaded, anti-RAG by constitution; block scratchpad with
FIFO eviction into journals; generation-aware consolidation with explicit `[MEMORY GAP]` markers;
Experience Review where reflection LLM actions auto-apply to scratchpad/knowledge but identity
updates are **candidate-only** ("autonomous learning cannot silently drift the personality");
memory modes `forked | empty | shared` for children.

**Us:** the entire `engine/memory.py` + lessons + claims + capsules + facets stack, fingerprint
similarity as the cross-run join key, harmonic/Memora abstraction indexing, RRF hybrid retrieval
with agent-decided merges, curation as **proposal-only** (operator applies typed commands).

**Assessment:** mostly **parity with opposite trade-offs, correctly chosen on both sides.** Their
anti-RAG rule is right for *one* agent's identity-scale memory (fits in context); wrong for our
portfolio-scale corpus (thousands of claims/lessons across runs — cannot fit, must be retrieved;
we *push* the retrieved priors before action, which is exactly the property their rule protects,
and CL-Bench's result argues the push side matters — see §3.3's hit-rate audit). Two small
imports: (a) their **candidate-only identity writes** mirrors our proposal-only curation — parity,
reassuring; (b) the explicit **`[MEMORY GAP]` marker on unexplainable discontinuity** is a nice
honesty idiom for `cross_run_index` rebuilds over partially-GC'd run dirs (S, cosmetic). The
`forked`-memory child mode rhymes with what `SiblingRunTools`/reviewer namespaces already do
read-only. **Verdict: parity; validation of ADR-10's push+pull design; one S idiom.**

### 3.6 LLM layer

**Them:** cognitive lanes (main/heavy/light/vision/consciousness) + ordered cross-model fallback
chains; **learned capability evidence** — provider rejections teach durable per-model
rejected-params and reasoning-effort ceilings/floors (14-day expiry, every clamp disclosed in the
usage event, never silent); per-(model,lane) concurrency semaphores; cache-aware stable-first
prompt assembly; money as an append-only reserved→settled attempt ledger.

**Us:** H3 per-role models, `LLM_LANES` broker, `llm_transient` retry/backoff, `engine/costs.py`
durable usage/cost ledger + outbox, prompt assembly per role.

**Delta:** (a) **learned rejection/effort cache** — our transient layer retries but re-learns
nothing across calls; a small durable `capability_evidence`-style sidecar in `core/llm.py`'s
sibling modules would stop paying the same 4xx twice (S–M, offline); (b) **ordered cross-model
fallback chains** per role — today backend failover is a Strategist/operator action; a config-level
`model_fallbacks` list with disclosed switches is S and pairs with lane widths. Their
disclose-every-clamp rule matches our "no silent degradation" norms. **Verdict: two S–M
adopt-ideas; ledger/lanes at parity.**

### 3.7 Ops & product ideas worth a note (no near-term action)

- **Background consciousness** = CORAL's heartbeat with a concrete design (rotating
  one-item-per-wakeup maintenance; second-failure-of-a-kind escalation: "six silent retries is not
  persistence — it's amnesia"). Doc 17 §25 item 3 already holds this slot gated; Ouroboros supplies
  the best reference implementation sketch if/when we build it. No parallel mechanism.
- **Skills marketplace with content-hash-bound review and 8-condition execution gates** — out of
  scope as product, but the *hash-binds-review* gate is precisely what our auto-distilled skills
  (`write_auto_skill` candidate→promoted) should get before any self-evolution program lets agents
  write them (already noted in §3.1 stage 1).
- **Outcome honesty tiers** (`solved / best_effort / blocked_with_evidence`, stamped from runtime
  facts, never prose) — our node terminals + benign-failure-reason filtering are the analog;
  a typed best-effort shelf could refine `run_finished`/finalize reporting someday (S, cosmetic).
- **"Every commit is a release"** — their answer to "is this significant enough?"; our analog is
  "docs and diagram in the same change." Different problem (they ship a product; we keep replay
  compatibility). No action.

### 3.8 What we explicitly do NOT take (and why)

1. **Mid-run self-modification of the engine.** Violates invariants 1–5 (sole-writer,
   deterministic fold, no I/O in fold). Their own design agrees: benchmarks run with core evolution
   *off*; evolution belongs between tasks. Any LoopLab self-evolution operates strictly
   between runs.
2. **LLM-first P5 ("no coded behavior, no regexp; if it can be a prompt, it is a prompt").** The
   exact inverse of our trust layer's deliberate design — deterministic AST/heuristic gates that add
   nothing to the log on a clean node and never hallucinate. Our one LLM-judge (the verifier) is
   calibrated *because* single-shot judging measurably failed. Keep our split: LLM for generation
   and advisory judgment, code for verdicts that gate.
3. **Constitutional anti-RAG.** See §3.5 — right for identity, wrong for a portfolio corpus.
4. **Personality/identity/continuity-as-biography.** Non-goal for a research engine; our
   "identity" is the reproducible run, which is the *point* of the event log.
5. **Their code wholesale.** 127k LOC coupled to their runtime (supervisor, gateway, desktop,
   Telegram); nothing is a library. The importable units are mechanisms and prompt disciplines, not
   modules — with the single exception below.

---

## 4. Integration difficulty — the concrete map

### 4.1 The one direct code-level integration: Ouroboros as a Developer backend (S)

Their CLI is explicitly designed for invocation *by other agents*:

```bash
ouroboros run --start --workspace /path/to/project --memory-mode forked \
  --patch-out result.patch --result-json-out result.json --jsonl "…task…"
```

Our `agents/cli_agent.py` preset contract (`CliAgentSpec{name, argv, needs_git, env}` with
`{message}`/`{model}`/`{file}` placeholders; patch-gated multi-file mode in a git worktree with a
`surface` allow-list minus `protect`) accepts this shape nearly verbatim — external workspaces must
be separate git worktree roots on *both* sides, and their patch-out contract matches our
patch-gate audit (`last_patch: {ok, paths, rejected}`). Concretely: add an `ouroboros` entry to
`PRESETS` (validated automatically by `Settings._check_trust_gate`), point env at a local
`ouroboros server`, and A/B it against `opencode`/`aider` on the offline suite, then on a repo
task. Caveats to record in `docs/guide/llm-and-agents.md` when doing it: it wants its own running
server (heavier than any current preset); its swarm/memory are the value *and* the cost; its
benchmark evidence is [SR]; egress parity (§3.3) must be pinned before quoting any A/B number.
MIT license — clean.

### 4.2 Effort/priority table (all items from §3)

| # | Item | Seam (ours) | Effort | Mode / gate | Value |
|---|------|-------------|--------|-------------|-------|
| 1 | `ouroboros` CLI-agent preset + A/B | `agents/cli_agent.py::PRESETS` | **S** | offline first; live behind `developer_backend` | High (capability + intel) |
| 2 | Action-trace reward-hack audit + `hack_adjusted` reporting | `trust/` + Developer audit trail + report projections | **M** | offline/report-only first | **Highest** |
| 3 | Atomic task contract in Developer prompts | `PromptStore` keys (`developer_system`, `repo_developer_system_body`) + `trust/critic.py` | **S→M** | prompt trial ungated; critic check offline | High |
| 4 | Verifier quorum at champion boundary (vendor-diverse 2-of-K) | `engine/confirm_phase.py`, `trust/verifier.py`, H3 role models | **S–M** | off by default; pairs with §12 calibration | High |
| 5 | Exact-`[]` parse contract for clean verdicts | verdict parsing shared by verifier/critic/strategist | **S** | ungated | Medium |
| 6 | `check_exit_masking` + artifact-lifecycle sensors on agent-authored checks | `runtime/command_eval.py` / sandbox | **S** | diagnostics only | Medium |
| 7 | `--require-clean-source` admission + dirty-state in `run_started` | CLI + `engine/orchestrator.py` run start | **S** | opt-in flag | Medium (benchmark-grade runs) |
| 8 | Egress-parity disclosure policy for external backends | `cli_agent` presets + MLEBENCH.md | **S** | policy/doc | Medium |
| 9 | Prior-injection hit-rate audit (post-hoc-learning metric) | offline over memory dirs + logs | **M** | offline | High (validates ADR-10) |
| 10 | Content-hash review binding for `prompt_dir`/auto-skills | authoring routes + `write_auto_skill` | **S–M** | prerequisite for #12 | Medium |
| 11 | Learned provider-capability cache; per-role fallback chains | `core/llm.py` siblings; `Settings` | **S–M** | offline | Medium |
| 12 | **Reviewed self-evolution program** (prompts → exploits cadence → code) | §3.1 staged plan | **M / S / L** | staged; stage 3 = own design doc; never outranks doc 17 R-gates | Strategic |
| 13 | Worker beacons (fold-ignored) + deferred-child finalize rule | `events/types.py` DIAGNOSTIC + `engine/finalize.py` | **S–M** | live-steering gated | Low-Medium |
| 14 | `[MEMORY GAP]` idiom; best-effort outcome shelf | `cross_run_index`, finalize reporting | **S** | cosmetic | Low |

**At parity already (listed to prevent re-proposal):** engine-sole-writer ≈ parent-sole-committer;
event-log ≈ ledgers+git as explainable state (ours replays, theirs doesn't); host-run scoring ≈
`verify_and_record`'s host-attested stance; denominator-preserving accounting ≈ `events.jsonl` +
`engine/costs.py`; proposal-only curation ≈ candidate-only identity writes; per-role models ≈
cognitive lanes; `forked` child memory ≈ read-only sibling/reviewer namespaces; their swarm
"shared frame before fan-out" ≈ our Card/hypothesis-board contracts; IMMUNE ≈ our conventions
(§2).

### 4.3 Is it "simply better"? Could it replace our core?

No. For our problem it is missing, with no path visible in the repo: leakage detection, consistent
CV/multi-seed confirmation, metric-driven search over candidate solutions (operators, policies, QD
archive, foresight), eval-fidelity ladders, task adapters, and — decisively — **deterministic
replay**: their explainability is forensic (ledgers + git), ours is constructive
(`fold(read_all())` rebuilds the exact state; `looplab replay` proves it). Running MLE-bench
through Ouroboros would mean rebuilding our trust layer inside their harness. The converse
(their strengths inside ours) is exactly the table above, and is tractable because every borrow
lands on an existing registry-guarded seam.

---

## 5. Sources / evidence status

- Article: [habr.com/ru/companies/airi/articles/1065428](https://habr.com/ru/companies/airi/articles/1065428/) — full text read (primary).
- Repo: [`razzant/ouroboros`](https://github.com/razzant/ouroboros) @ v6.87.5 — clone-level
  inspection (primary): `README.md`, `BIBLE.md`, `docs/ARCHITECTURE.md`, `prompts/{SYSTEM,
  CONSCIOUSNESS,SAFETY}.md`, `launcher.py`, `server.py`, `supervisor/*`, `ouroboros/*` (evolution
  lifecycle, commit gate, review stack, memory, subagents, skills, LLM layer),
  `devtools/benchmarks/*/METHODOLOGY.md` + `common/` admission machinery.
- Benchmark rows: **[SR]** with public traces (Harbor jobs, HuggingFace bundles) and open
  leaderboard submissions; per-benchmark methodology files disclose reward-hack audits, leakage
  filtering, and known limitations. Not independently reproduced by us.
- LoopLab-side facts: current source inspection (paths as cited); doc 13, doc 17 Part III-C, and
  doc 25 used as the comparison baseline and to avoid re-proposing items already at parity or
  already gated.
