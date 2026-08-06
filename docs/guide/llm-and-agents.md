# LLM & coding agents

LoopLab's Researcher and Developer roles are **pluggable backends**. They can be the offline `toy`
optimizer, a live LLM over any OpenAI-compatible endpoint, or — for the Developer — a full external
coding agent. Swapping a backend is a config change; the engine, sandbox, policy, and event log are
unchanged.

## Backends at a glance

| | Offline | Live LLM | External agent |
|---|---|---|---|
| **Set** | `--backend toy` (explicit) | `--backend llm` (**default** since 2026-08-04) | `--developer-backend opencode` |
| **Researcher** | deterministic optimizer | the model | the model |
| **Developer** | templated | the model writes code | the agent edits a worktree |
| **Network** | none | LLM endpoint only | LLM endpoint only |

## Using a live LLM

Point LoopLab at any OpenAI-compatible `/v1` endpoint:

```bash
export LOOPLAB_BACKEND=llm
export LOOPLAB_LLM_BASE_URL=http://localhost:11434/v1     # Ollama
export LOOPLAB_LLM_MODEL=qwen3:8b
# export LOOPLAB_LLM_API_KEY=sk-...                       # hosted endpoints only
```

Verify before a real run:

```bash
looplab smoke        # sends a text + a structured (tool-call) request and reports each
```

Then run with `--backend llm` (or the env var above):

```bash
looplab run examples/code_regression_task.json --backend llm --max-nodes 6
```

### Endpoint preflight (before a run starts)

**A run whose endpoint will not serve it is refused before it starts** — no event log, no `run_finished`
event, nothing to resume. (The run *directory* is created a moment earlier, to take `engine.lock`; the
refusal leaves it holding nothing but that empty lock file, and `resume` rejects it with
`no run found … (no events.jsonl)`.) You get one message on stderr and exit code `2` — the
[refusal code](cli-reference.md#exit-codes-a-refusal-is-not-a-crash), not a traceback:

```
Refused: LLM endpoint preflight failed: [unreachable] researcher (qwen3:8b at http://localhost:11434/v1):
LLM request to http://localhost:11434/v1 failed: Connection error.
  Nothing answered at that address (refused connection, DNS failure, TLS error or timeout) — start
  the endpoint, or point LOOPLAB_LLM_BASE_URL / --model at one that is running.
  Run offline with `--backend toy` (or -s backend=toy), or check the same endpoints without
  launching anything with `looplab smoke`. Refusing to start: the roles would degrade to empty
  fallback proposals and the run would report success on a flat, meaningless result.
```

Why it refuses instead of trying: every LLM role degrades on purpose so one flaky answer cannot kill a
run, and those degradations stack into a lie when the endpoint will not serve. An unparseable answer
becomes an empty `Idea`, which becomes a `fallback (agent parse failed)` proposal. A measured
`looplab run examples/toy_task.json --max-nodes 3` against a dead endpoint produced three **identical**
`x=0,y=0` nodes, a metric flat at 10.0 and `finished=True` — a confident-looking completed run with no
error anywhere — while `--backend toy` optimized the same task to 8.05.

What the check actually does (`looplab/agents/preflight.py`):

| | |
|---|---|
| **When** | In the engine constructor, immediately after the credential check and **before any role is built** — so `run`, `resume`, `finalize` and the UI's spawned engines all go through it. It **refuses** wherever the command can still start work, and **warns** on a wrap-up-only entry point (below) |
| **Cost** | One four-token completion per **distinct** target, not one per role. The ordinary single-model run pays exactly one; roles that differ only in credential are still probed separately |
| **Bounds** | A 60 s wall guard **per attempt** (headers + body), 2 retries through the client's own retry policy, and at most 15 s of waiting **between** attempts — so the whole gate is bounded, not just each request. A refused connection, bad DNS, 401, or a hard 403/404 is not retried at all, so the common failure is instant; the retries forgive a transient 429/5xx, and each wait is announced (below) |
| **Shape** | The same probe (no stream, no cache, no reasoning) that the Web UI's `/api/llm/health` card issues, so a green card and a startable run mean the same thing |
| **Skipped for** | `--backend toy` — bypassed **entirely**, no probe of any kind. Also the Developer roles when `--developer-backend` is external (those authenticate from the coding tool's own credential store), and any client supplied through the `make_llm_client` seam that has no `probe` method (a test double or a custom transport is not evidence of a failed endpoint) |

A failure lists **every** failing role/target, not just the first, so one restart can fix a
multi-provider setup. Run `looplab smoke` first if you want the same answer without launching anything.

#### It names the cause, because the causes need opposite fixes

`[unreachable]` above is one of six. Until 2026-08-05 it was the *only* one: every failure got the
sentence "start the endpoint, or point `LOOPLAB_LLM_BASE_URL` / `--model` at one", which is
unfollowable advice when the endpoint is running and correctly configured. It blocked two measured
launches against an endpoint that was merely rate-limiting (`HTTP 429 … Current limit: 50`).

The refusal now classifies the probe failure (`core/llm_transient.py::classify_llm_failure`, read off
the HTTP status the endpoint returned rather than off the message text) and prints one remedy per
cause present:

| Tag | What the endpoint did | What actually fixes it |
|---|---|---|
| `[throttled]` | Answered **429**, or a **403** whose body reads as a burst/rate limit | Wait for the window to reset; lower concurrency (`-s eval_parallel=1 -s llm_parallel=1 -s speculation_depth=0`); move the role to a different model on the **same** endpoint; raise the limit. **Not** a URL change |
| `[overloaded]` | Answered **5xx / 408** — up, failing on its own side | Wait and re-run, or check the server's logs. Nothing in your configuration is wrong |
| `[unreachable]` | Nothing answered: refused connection, DNS failure, TLS error, timeout | Start the endpoint, or repoint `LOOPLAB_LLM_BASE_URL` |
| `[credential]` | Answered **401** | Fix `LOOPLAB_LLM_API_KEY` or the profile's `api_key_env`. The endpoint and model are fine |
| `[model]` | Answered **400 / 403 / 404**, refusing the request — its own words are quoted, and normally name the model | Fix the model id. Use the bare name the endpoint advertises: a tier suffix like `:max` or `:high` is a *different* id and is refused as unknown |
| `[protocol]` | No readable HTTP status — an empty/non-JSON body, or a transport LoopLab did not build | Check the base URL really ends at an OpenAI-compatible `/v1` root; `looplab smoke` prints the full error. This gate does **not** guess which of the above it is |

**A throttled endpoint is still refused.** It is tempting to wave a 429 through as transient, but the
roles cannot tell one from a dead port either — both arrive as `LLMError` — so a run started into a
live limit degrades into the same flat `finished=True` lie the gate exists to prevent. What separates
"transient" from "sustained" is the retries: the probe re-asks after the endpoint's own `Retry-After`,
so a blip clears and only a limit still refusing on the last attempt reaches the refusal.

**And the wait is bounded and audible.** Each backoff is announced at `WARNING` (endpoint, status,
seconds, `attempt 2 of 3`), so a pausing launch is not mistaken for a hang. A server `Retry-After`
*longer* than the preflight's 15 s per-wait budget ends the retries instead of being served: the
refusal quotes the number the server asked for, rather than sleeping it. That bound is the fix for a
measured 121.4 s of complete silence — two 60 s directives slept in a preflight advertising 60 s —
followed by advice that could not help. Ordinary run-time requests are unaffected and still ride out a
directive up to `RETRY_AFTER_CAP_S` (120 s).

**Wrapping up a run that is already over is never refused.** Every sentence of the reasoning above is
about a *proposal*, and a run past its terminal boundary makes none — it can only turn work already paid
for into the report, the lessons, the cost roll-up and `tree.html`. Refusing there would cost the
operator every artifact that needs no model at all and leave the run `finalization_pending` forever,
with its spend stranded in `.llm-usage-outbox`. So `finalize` — and a `run`/`resume` that lands on a
wrap-up boundary — runs the same probe through `wrap_up_endpoint_warning` instead: it proceeds and names
what the missing model degrades (`(report unavailable)` for the report, nothing model-authored in
cross-run memory), **before** the wrap-up starts, because those steps are marked complete once attempted
and a later `finalize` will not redo them. Its header reads `⚠ LLM endpoint unusable while wrapping
up`, with the same per-target `[cause]` tags as the refusal — it shares the probe, so it must not
assert "unreachable" about an endpoint that answered. See [`finalize`](cli-reference.md#finalize) for
the full artifact-by-artifact list.

**The credential check is softened the same way, and it has to be.** The gate one step earlier
(`validate_bound_profiles`, below) refuses a run whose key/endpoint pair is unusable, and on a wrap-up
entry that refusal is the identical dead end: exit 1, no artifacts, `finalization_pending` forever. It
therefore warns on the same boundaries, with the same list of what a missing model costs — plus one
extra line, because the two gates fail at different moments. An unreachable endpoint still *builds* its
clients and fails at request time; an unusable credential fails inside `make_llm_client`, so warning
alone would only move the same error into role construction a few lines later. The wrap-up therefore
runs with **no credential at all** rather than with one bound somewhere else: whatever calls it does
attempt are refused by the provider instead of carrying a key to a host it was not issued for. Your
configuration is untouched — only that one wrap-up's transport is degraded.

**Which boundary a command is on is decided under the run's singleton lock**, not before it. `resume`
is the only entry point that is wrap-up-only *sometimes*: it LIFTS a stopped or finished run back into
the loop (new work — a dead endpoint must refuse) but may only complete an existing wrap-up on a
`pending_finalize` / `finalization_pending` boundary. When it has to wait for a previous owner to
release `engine.lock`, that wait exists precisely to let the owner *finish* its wrap-up — so a decision
made before the wait can be stale by the time the run is lifted. Both the probe and the lift now read
one fold taken after the lock is held, and every command re-checks the promise once more immediately
before entering the loop.

### Moving a run to a different endpoint

The key and its endpoint binding are **one atomic credential**. Point a run somewhere new and the
credential has to move with it — LoopLab will not carry a key issued for one host into an
`Authorization` header aimed at another, and will not quietly complete a half-override from a
different source. Change all three together:

```bash
export LOOPLAB_LLM_BASE_URL=https://new-endpoint/v1
export LOOPLAB_LLM_API_KEY=sk-…                              # the key for THAT endpoint
export LOOPLAB_LLM_API_KEY_BASE_URL=https://new-endpoint/v1  # must equal the line above
```

Two rules explain every refusal you can hit here:

- **The pair is selected from ONE source** — the process environment if either name appears there,
  otherwise `.env`. Exporting only `LOOPLAB_LLM_API_KEY` in your shell does *not* inherit
  `LOOPLAB_LLM_API_KEY_BASE_URL` from `.env`; it replaces the whole pair with half of one. (The
  refusal says so explicitly, naming the `.env` half it dropped.)
- **The binding is checked against the endpoint the request would actually go to.** Overriding
  `llm_base_url` alone — via `LOOPLAB_LLM_BASE_URL`, `-s llm_base_url=…`, or a config file — leaves
  the key bound to the old host, and the run refuses before any transport is built.

`--model` is safe on its own: it changes the model id only and never moves an endpoint, so it cannot
strand a credential.

If the new endpoint needs no key at all (a local Ollama/vLLM), unset **both** names rather than one.

A run refused for either reason names the mistake, both endpoints, and the variables to set together
— **once**, with the roles it affects listed underneath. Seven roles share one shared credential, so
one wrong variable is one problem, not seven.

### Endpoint options

| Endpoint | `LOOPLAB_LLM_BASE_URL` | Notes |
|---|---|---|
| **Ollama** | `http://localhost:11434/v1` | Native Windows; easiest local start (`ollama pull qwen3:8b`) |
| **vLLM** | `http://host:8000/v1` | Supports constrained decoding (`llm_guided_json`) |
| **SGLang** | `http://host:30000/v1` | Use `--tool-call-parser qwen` for Qwen tool-calls |
| **OpenAI / compatible** | the vendor's `/v1` | Set `LOOPLAB_LLM_API_KEY` |

The client (`OpenAICompatibleClient`) runs on the **openai SDK over an httpx transport** (migrated from the old stdlib-urllib transport for reliable timeouts + a streaming idle-guard); `openai`/`httpx` are declared deps but import-guarded so offline/replay still imports. A LiteLLM client is also available. Structured
output uses tool-calling with an automatic text-parse fallback, so weaker models still work.

## Reasoning / thinking

`llm_reasoning` controls the chain-of-thought sent in the request (defaults to `high` — the agent
reasons before proposing/repairing). The model's thinking is captured either way (a
`reasoning_content` field or inline `<think>`), and the UI can show it per node.

| `LOOPLAB_LLM_REASONING` | Effect |
|---|---|
| `""` | Send nothing (server default) |
| `off` | Actively disable thinking |
| `on` | Enable at default depth |
| `low` / `medium` / `high` | Enable at that effort (default `high`) |

`llm_reasoning_style` shapes how the request param is built (`auto` / `qwen` / `effort` / `none`);
`llm_reasoning_extra` is a raw escape hatch merged into the body. To get a *separate*
`reasoning_content` field from SGLang/vLLM, the server also needs `--reasoning-parser qwen3`.

## Constrained decoding

`LOOPLAB_LLM_GUIDED_JSON=1` drives structured calls with the endpoint's `guided_json` /
`response_format` (vLLM/SGLang) so a weak model can't emit invalid JSON. Off by default (and for
Ollama). Turn it on only if a model struggles to produce valid structured output.

## Per-role and per-stage models

Run the Researcher and Developer on different models or endpoints — e.g. a strong coder model for
the Developer and a fast model for breadth:

```bash
export LOOPLAB_RESEARCHER_MODEL=qwen3:8b
export LOOPLAB_DEVELOPER_MODEL=qwen3-coder:30b
export LOOPLAB_DEVELOPER_BASE_URL=http://coder-host:8000/v1
```

Blank values fall back to the shared `llm_model` / `llm_base_url`. With the **unified agent** (one
identity across stages, on by default), use `agent_stage_models` / `agent_stage_base_urls` to
override per stage — recognized keys are `propose`, `implement`, `repair`, `strategy`, `pilot`:

```bash
export LOOPLAB_AGENT_STAGE_MODELS='{"implement":"qwen3-coder:30b","repair":"qwen3-coder:30b"}'
```

Each property resolves on its own, stage map first, then the per-role field, then the shared
default — so a stage that overrides only the endpoint keeps its role's model and its role's
temperature. `implement` and `repair` are genuinely independent stages: give them different models
and the repair stage runs on its own Developer instead of sharing the implement one.

The Strategist has `strategist_model` / `strategist_base_url` alongside its temperature; before they
existed it always ran on the shared `llm_model` however the other roles were pointed.

## Several providers at once

Per-role *models* alone cannot express per-role *credentials* — two roles on the same provider may
need different keys or budgets — so a role can instead point at a named **connection profile**
carrying a model, an endpoint, a temperature and the NAME of the environment variable holding its
key. The key travels only with the endpoint the profile named — overriding a role's endpoint
drops it rather than sending one provider's secret to another host. Skip this entirely if you run one model; see
[Configuration → Connection profiles](configuration.md#connection-profiles-only-needed-with-more-than-one-provider)
for the fields, the full precedence table and the safety rules.

```bash
export LOOPLAB_LLM_PROFILES='{"coder": {"base_url": "https://api.provider.tld/v1",
  "model": "big-coder", "api_key_env": "LOOPLAB_LLM_API_KEY_CODER"}}'
export LOOPLAB_ROLE_PROFILES='{"implement": "coder", "repair": "coder"}'
export LOOPLAB_LLM_API_KEY_CODER=sk-...
```

Roles you can bind: `propose`, `implement`, `repair`, `strategy`, `pilot`, `researcher`,
`developer`, `strategist`, `compressor`, `embed`. An unknown role name, a missing profile, or a
literal key inside a profile is refused at startup rather than silently ignored, and a bound profile
whose variable is unset stops the run **before its first paid call** — except on a wrap-up-only entry
point, where it warns and the wrap-up runs credential-free (see
[Endpoint preflight](#endpoint-preflight-before-a-run-starts)).

## External coding agents

The Developer role can be delegated to an external terminal coding agent. LoopLab runs it headless
in a git worktree, points it at your local LLM endpoint, and reads the edited solution back.

```bash
looplab run examples/code_regression_task.json \
    --backend llm --developer-backend opencode --model qwen3:8b
```

Supported presets: **`opencode`**, **`aider`**, **`goose`**, **`continue`**. Three guardrails make
this robust (all on by default):

- **Self-contained & headless.** A config (e.g. `opencode.json` with a local Ollama provider and an
  explicit `--model`) is dropped into the agent workdir, so the agent never fetches an external
  model registry on startup.
- **Output validation** (`validate_agent`, `agent_max_retries`). Every agent output is checked
  (launched / not-timed-out / produced / modified-seed / parses / in-surface). On failure it
  re-prompts the agent with the reason, then falls back to the in-house LLM Developer. Each node
  logs an `agent_validated` event.
- **Patch-gated, multi-file** (`agent_patch_gate`, `agent_surface`). The agent runs in a git
  worktree; its diff is gated by an edit-surface allow-list (default `*.py`, reject-not-strip).
  Accepted files become `Node.files` (files-as-truth, resumable) and are materialized into the eval
  workdir.

| Setting | Default | Purpose |
|---|---|---|
| `developer_backend` | `default` | `default` / `opencode` / `aider` / `goose` / `continue` |
| `agent_cmd` | — | Override the launcher/path |
| `validate_agent` | `true` | Audit + retry + fall back |
| `agent_max_retries` | `1` | Re-prompts on an invalid result |
| `agent_patch_gate` | `true` | Worktree + surface-gated diff |
| `agent_surface` | `["*.py"]` | Edit-surface globs |

The built-in LLM Developer (writes code via your endpoint, no external fetch) remains the
zero-dependency default coding path.

## The Developer: three phases (stages → plan → implement)

On a fresh (non-repair) repo node the Developer runs **three separately-traced phases**, so the context
stays focused and the trace reads cleanly:

1. **STAGES** (mandatory, first — unless the operator pre-empts it) — a **read-only** phase whose only
   exit is `declare_stages`. The Developer studies the repo + the operator's `cmd` and declares the
   ordered eval pipeline (e.g. `data_prep → train → …`) that runs BEFORE the operator's protected
   `score` step, baking this node's hyperparameters into the train command. It writes
   `looplab_stages.json`. The **Developer** owns the stages (it knows the repo), not the
   planner/Genesis. Two operator knobs skip the phase: a valid `cmd.stages` pipeline (the engine runs
   it verbatim; the Developer implements the code those stages run) or a protected
   `looplab_stages.json` (disables Developer pipelines — no point burning an LLM loop whose manifest
   would be dropped). If `cmd` is present it's shown as immutable (declare only the preceding stages);
   if absent, declare the full pipeline including a final evaluating stage. The name `score` is
   **reserved either way** (it always denotes the engine-appended operator step). Good practice:
   separate **prep / train / test** stages.
2. **PLAN** — decomposes the code changes into ordered atomic steps (still read-only).
3. **IMPLEMENT** — writes the code those stages run, one bounded session per step. The prompt states
   the node's **actual** declared pipeline (or its absence), and the session carries `declare_stages`
   only to **fix** a broken manifest (e.g. a repair whose root cause is a bad stage command/timeout) —
   authoring stays in the STAGES phase.

A **repair** skips stages+plan and is one focused session. The first **two** phases are read-only: they
get the repo scouts plus the env inspector (`read_file`, `grep`, `find_files`, `list_dir`, `pkg_info`,
`py_api`, `read_installed`, `grep_installed`, `gpu_info`) — but **no write tools** — so the Developer
reads the real eval/entry script and
the files it will change *before* deciding what to do, instead of planning blind off a truncated
preview. Two tools make this practical:

- **`read_file` paginates.** It takes `start_line` + `lines` to window a large file (like an editor's
  "go to line N, show M lines"); each reply is one page of at most ~3,600 characters of content that —
  when more of the file remains — ends with a `… (more below — continue with start_line=N)` resume
  marker, so the planner reads a file *once*, page by page from exactly where it left off, and a long
  file is never silently truncated mid-read.
- **`gpu_info`** reports the visible GPUs (count / names / memory via `torch.cuda`) — the `nvidia-smi`
  equivalent for an agent that has no shell, so the plan can size a model/batch to the real hardware.

## Phase-handoff summaries

Each LLM phase in a node build re-explores the same repo — the stages phase maps it, then plan reads
it again, then implement reads it again. **`phase_handoff_summary`** (on by default) cuts that with
**handoff briefs**, one `handoff_scope` the engine opens around each build: when an *exploration*
phase emits, ONE extra LLM call distills its whole transcript — the repo structure it mapped, the
files/data/APIs it confirmed, the decisions it made — into a tight brief injected into the **next**
phase's prompt, which is told to *trust it and not re-read*. The brief flows across the whole build
and **across the role boundary**: `Researcher·propose → Developer·stages → plan → implement`. Only
the exploration phases contribute (the ledger stays ≤3 briefs — no K-step bloat); terminal phases (a
single-session implement, each implement step, a repair) **consume** the briefs but don't summarize,
so there's no wasted call on the tail. The summary is best-effort (any error → the next phase just
runs without it), skipped for a phase that barely read anything, and produced by the Researcher's
propose phase only when the in-house repo Developer follows it (a single-shot developer never reads
the ledger, so the call would be wasted).

There is deliberately **no read cache**: every read tool call executes and returns fresh content
(a result that exceeds the ~4000-char tool-result cap is truncated with an explicit
`…[truncated by the tool-result cap …]` marker so the agent knows to re-request a narrower range);
the `StuckDetector` remains the safety net against true repeat loops. A parallel node build gets its own
scope; every phase runs through the shared `run_phase` wrapper, so with the setting off it's
byte-identical to a plain `drive_tool_loop`.

## Agentic auxiliary steps

Every remaining single-shot LLM step is now a **tool-using agent** (via the shared `agentic_text` /
`agentic_struct` helpers). Lessons distillation, the research + reward-hack / leakage **verify** pass,
the end-of-run **report**, and **Genesis** (goal → task plan) each *read* the real experiments / code
/ data before emitting. The **Strategist** likewise defaults to the agentic backend
(`strategist_backend=agent`). Novelty/dedup is decided the same way — the embedding / param search
only *suggests* near-duplicates and the LLM adjudicates; `novelty_gate` (and semantic novelty) stay
**off by default**.

## LLM-outage resilience

The LLM client is hardened against a flaky or throttling endpoint. A rate-limit-shaped **403** (a
proxy/WAF burst-throttle, not a real auth failure) is treated as retryable and backed off, and the
client makes up to **8 retries** (429 / 5xx / throttle-403) before surfacing an error. If the model
is genuinely unreachable, a Developer session crashes (`developer_crash`); the engine then **pauses
the whole run** on the *first* such crash (an `EV_PAUSE`) rather than rapid-firing dozens of dead
nodes — resume once the endpoint is back.

The same circuit-breaker covers a provider that stops working **mid-run**, during an *inline repair*
rather than a build. A failed repair *call* is not a repair, in any of the four ways the call can
fail to produce one: the Developer returns the in-band `(developer error: …)` sentinel, the call
**raises** (an LLM client that throws on a 401/402 — normalized into the same sentinel), it answers
with something that is **not a program at all** (a comment-only or docstring-only reply, which would
otherwise exit 0 and be reported to you as "printed no metric"), or it answers with something that
is **not Python** several times over. In each case the engine records **no** `node_repaired` (the
attempt isn't spent and nothing is written to the workdir), terminalizes that node with
`reason="developer_crash"` naming the provider failure, and appends the run-level pause.

**The crash-triage judge runs on that same endpoint**, and it is what decides whether to keep
repairing — so "the judge did not answer" must never mean "keep repairing". But *how* it failed to
answer decides how much it stops, because only one of the two ways is evidence about your provider:

| What happened | Verdict | What stops |
|---|---|---|
| **Nobody answered** — the call raised, the endpoint was unreachable, a 401/402, the loop never emitted | `unanswerable` | The node (`developer_crash`) **and the run**: one run-level pause naming the provider, `resume` once it is back |
| **The model answered something unreadable** — an action outside `repair`/`abandon`/`reject_idea`, an empty or missing one, or the literal word `unanswerable` arriving from the wire | `unreadable` | **Only the node**, terminalized like an `abandon` with the eval's own failure reason, so a node reset re-opens it. No pause — the endpoint just answered |

Either way the engine **re-asks once** before acting: one non-answer is not a diagnosis, and a single
flapped socket used to end a node with zero repair calls where the second ask would have healed it.
And a `repair`/`abandon`/`reject_idea` never triggers a re-ask, so a healthy run still costs exactly
one triage call per attempt.

Collapsing those two rows was a real defect and it cost more than it saved: one healthy model
emitting a single out-of-enum verdict raised a run-level pause carrying no `node_id` (so a node reset
could not clear it) telling the operator to check credits, key and base URL — using the *model's own*
rationale as the evidence — and under `eval_parallel > 1` it took every healthy in-flight sibling
down with it. Excluding `unanswerable` from the schema enum could not prevent that on its own,
because the *fail-closed default* for an unreadable verdict was `unanswerable`.

Without any of this, a provider error string was committed as the node's code and re-evaluated:
one real run turned an out-of-credits `402` into **2345 `node_repaired` events on a single node over
3.5 hours**. Use [`looplab timings RUN_DIR`](cli-reference.md#timings) to see where a run's
wall-clock actually went (LLM vs eval vs repair vs tools, per node **and** run-level, reconciled
against the run's real duration with the untraced remainder named).

### …and the same breaker on the **proposal** path

The Researcher degrades too, and until 2026-08-05 nothing noticed. When its provider is unreachable
the role returns a **degraded fallback Idea** — no params, no hypothesis, the transport error where
the rationale should be (`fallback (agent parse failed: …)`). That is not a weak experiment, it is the
*absence* of one, but the engine used to build it: a run whose endpoint died after node 0 produced
three more nodes with byte-identical bounds-midpoint params, spliced the error string into the
hypothesis board, the node rationale and the research memo, wrote the "winner" into **cross-run
memory**, and printed `finished=True … BEST node 3` with `run_finished` carrying no reason at all.

The Researcher's fallback now carries the same kind of in-band sentinel the Developer's crash does
(`agents/roles.py::RESEARCHER_FALLBACK_PREFIX`), and the engine refuses it at every lane that turns a
proposal into work — before any `card_added`, before any node id. It then appends a **run-level pause**
naming the provider and what to fix, exactly like `developer_crash`:

```
auto-paused: the Researcher's LLM provider failed, so it returned a degraded FALLBACK instead of a
proposal — agent parse failed: LLM request to http://… failed: Connection error. Nothing was
proposed, so no node was built. Fix the endpoint/credentials and `looplab resume`; the run keeps
every experiment it already has.
```

Because the run **freezes rather than finishes**, a run whose proposals were all fallbacks cannot
report a champion, write a report, or add a case to cross-run memory — there is nothing to report on.
Experiments that already completed are untouched, and `looplab resume` continues from them.
This is the *second* line of defence; the first is the endpoint preflight above, which refuses to
start a run against an endpoint that is already dead.

## Signal delivery (agent synergy)

The engine computes rich, expensive signals — and each is only useful if it reaches the agent (or
human) that can act on it. The recurring failure mode is *"the signal is folded into the event log
but nothing injects it into a prompt"* — the same class the hint registry
(`roles.RESEARCHER_HINT_ATTRS`) already turned into a test-enforced invariant. LoopLab now routes
**eight** such signals, each through exactly one documented injection site:

| Signal | Folded into | Reaches | How |
|---|---|---|---|
| **Trust flags** (reward-hack / leakage) | `RunState.reward_hacks` | Researcher | a trust-reflection line in the proposal hint (`digest.trust_reflection`) — "a recent solution was flagged for X; avoid it if unintended" |
| **Watchdog signals** (train-monitor / ASHA rank) | *not folded* — DIAGNOSTIC events read from `store.read_all()` | Researcher | a watchdog-reflection line in the proposal hint (`digest.watchdog_reflection`), naming the eval PHASE the verdict was about (`log_role`/`stage` on the alert row) rather than calling every verdict "training" |
| **Crash-triage verdict** | `Node.triage_rationale` | Researcher | the failure line in the experiments digest + the failure-reflection hint carry the LLM's *why*, not just the error kind |
| **Foresight calibration** | `RunState.foresight_selected` | the world model | a track-record line in `_memory_brief` — "of your last N predict-before-execute picks, K beat the parent" (closes the predict→outcome loop) |
| **Deep-research memo** | `RunState.research` | Researcher | a one-line takeaway in the state brief **plus** a `read_research_memo` tool to pull the full findings/claims on demand |
| **Operator yields** | derived from the DAG | Strategist | a per-operator gain-per-second line in the strategist brief, so it tunes the operator mix from evidence, not priors |
| **Operator directives** | `RunState.pending_hints` | Researcher, Strategist, pilot, crash-triage, **Developer** | one `render_hint_directives` helper — the engine also folds directives into the idea handed to `implement`, so a directive steers the **code**, not only the proposal |
| **Run states** (paused / awaiting-approval / trust-flag / stuck-build) | `RunState` | boss / assistant | an "ATTENTION" block in the boss context, surfacing the states where human intervention is most valuable |

**The invariant.** `engine/signal_delivery.py` is a registry of these routes (signal → folded field
→ injection site → consumer), and `tests/test_signal_delivery.py` asserts each injection symbol
resolves *and* that a synthetic input's content actually reaches the rendered output. A signal added
to the registry without a delivery probe fails the suite — so *"the signal silently stopped being
delivered"* is a red test, not the next review's finding. Three of the routes (trust flags, watchdog
signals, operator directives) are **push** (the engine injects them), one (deep-research memo) is
**pull** (a tool the
agent may call for depth), and the rest ride the always-on folded-state briefs. The full rationale is
in `docs/14-agent-framework-mega-review-2026-07-10.md` §1.

## Knowledge, skills & prompts

Give the agentic Researcher extra context and tools:

| Setting | What it adds |
|---|---|
| `knowledge_dir` | A notes directory; the Researcher gets `grep` / `kb_search` / `list_notes` / `read_note` tools and chooses when to use them |
| `skills_dir` | A directory whose recursive `*/SKILL.md` packages and root-level `*.md` skills the Researcher can list and load. The shipped flat example works with `-s skills_dir=examples/skills` |
| `prompt_dir` | Editable, hot-reloaded role-prompt `.md` files (override the built-in prompts) |
| `researcher_tools` | (on) Read its own experiments + the task data mid-loop |
| `cross_run_tools` | (on) Read-only tools over sibling runs (same task id, same run-root). Fails **closed**: with no authoritative task id — an unbound provider, or a legacy log whose `run_started` carried none — it lists and serves nothing, rather than widening to every task |
| `all_runs_tools` | (on) Read-only tools over every run **under this run-root**, across ALL tasks — read any experiment's code + result to reuse it. Bound to the configured run-root, not the machine, so absence here is not machine-wide absence |
| `literature_search` | An arXiv search tool (network-optional) |
| `web_search` | Web search/fetch for the Deep-Research stage (network-optional) |

### Prompt override keys (`prompt_dir`)

Every built-in system prompt below can be replaced by dropping a `<key>.md` file into `prompt_dir`.
Files are **hot-reloaded** — re-read on every use, so you can tune a prompt mid-run without a
restart — and rendered with `string.Template` **`$var`** substitution (leading YAML frontmatter is
stripped). A missing file falls back to the built-in default.

| Key | Who uses it |
|---|---|
| `researcher_system` | The plain (non-tool) LLM Researcher — the CORE persona only: the sweep/`eval_timeout` capability suffix and the operator note are appended by code after the render (the sweep offer is gated on the active **Developer** declaring `honors_idea_space`, so a templated Developer is never promised a grid it cannot run), so an override supplies just the core body |
| `developer_system` | The LLM Developer (both `implement` and `repair`) — the from-scratch script Developer |
| `developer_repair_prefix` | Short prefix prepended to `developer_system` on repair calls |
| `repo_developer_system_intro` | The in-house repo-editing Developer (`LLMRepoDeveloper`): the intro of its system prompt |
| `repo_developer_system_body` | The in-house repo-editing Developer (`LLMRepoDeveloper`): the body of its system prompt |
| `tool_researcher_system` | The tool-using Researcher — the default agentic Researcher |
| `strategist_system` | The plain LLM Strategist (meta-control decisions) |
| `tool_strategist_system` | The agent (tool-using) Strategist |
| `pilot_system` | The unified agent's action pilot (chooses the next macro action) |
| `triage_system` | The unified agent's crash triage (retry / repair / abandon) |
| `foresight_system` | The foresight ranker (predict-before-execute idea/hypothesis prioritization) |
| `bestofn_judge_system` | The best-of-N judge (picks the best of N candidate implementations) |
| `merge_system` | The hybrid-merge adjudicator (lesson & hypothesis-board consolidation); `$kind` and `$detail` vars |
| `deep_research_system` | The Deep-Research stage agent |

Migration note: a `researcher_system.md` override copied from the old full default should drop the
trailing sweep/`eval_timeout` capability text and the operator note — the code now appends those
fragments itself, so keeping them in the override would duplicate the prose.

```bash
looplab run examples/regression_task.json --backend llm \
    --knowledge-dir examples/knowledge --max-nodes 6
```

With `--knowledge-dir`, the Researcher becomes a tool-using agent: in a bounded multi-turn loop it
may call the knowledge tools, then `emit` its structured idea. The orchestrator is unchanged — the
tool-using Researcher drops in behind the same protocol.

See [Configuration](configuration.md) for every related setting and [Concepts](concepts.md) for how
the roles fit into the loop.
<!-- CODEX AGENT: the separate Part IV/V `CrossRunTools` provider has a second scope ambiguity:
unbound is intentionally portfolio-wide for CLI/human inspection, but the same constructor is used for
model-facing roles, so a forgotten/failed `bind_state` silently inherits that broad audience. Require an
explicit audience at construction and make an unbound agent audience return no rows. Its bounded retrieval
also still computes an O(all evidence) corpus digest outside `max_corpus`. -->
