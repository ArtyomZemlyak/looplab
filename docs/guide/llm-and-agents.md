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
| **Skipped for** | `--backend toy` — bypassed **entirely**, no probe of any kind. An external coding CLI authenticates from its own store and is never probed with a LoopLab key; however, when `validate_agent=true` and that task's validation fallback is an in-process LLM Developer, the fallback's exact `developer` or `implement`/`repair` targets **are** credential-checked and probed. Repo-baseline and deterministic-template fallbacks build no Developer client and add no probe. A merely potential custom/historical Strategy switch from the external CLI to the in-process Developer is not a startup consumer; if requested, its target is credential-checked and probed immediately before the replacement is built. A client supplied through the `make_llm_client` seam with no `probe` method is also skipped (a test double or custom transport is not evidence of a failed endpoint) |

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

Blank values fall back to the shared `llm_model` / `llm_base_url`. With the **unified control
facade** (one engine-facing object over stage-specific clients and local contexts, on by default),
use `agent_stage_models` / `agent_stage_base_urls` to override per stage — recognized keys are
`propose`, `implement`, `repair`, `strategy`, `pilot`. This is not one shared cross-stage
conversation identity:

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
  re-prompts the agent with the reason, then falls back to the task's original in-process Developer:
  the LLM code writer for script-generating tasks, the unchanged baseline for repo tasks, or the
  deterministic template for closed synthetic tasks. Only the reachable LLM fallback receives its
  LoopLab-managed role target/key; the nested external CLI still gets a secret-scrubbed environment.
  Each node logs an `agent_validated` event.
- **Patch-gated, multi-file** (`agent_patch_gate`, `agent_surface`). The agent runs in a git
  worktree; its diff is gated by an edit-surface allow-list (default `*.py`, reject-not-strip).
  Accepted files become `Node.files` (files-as-truth, resumable) and are materialized into the eval
  workdir.

A dedicated `developer` profile may therefore describe the external tool's model/remote endpoint,
but an external-only role must omit `api_key_env`: promising a LoopLab-managed key is rejected because
the secret-scrubbed CLI can never receive it. A trusted in-process validation fallback or active Repo
onboarder may use that key. The shipped LLM Strategist schema and operator `set_strategy` surface do
not expose Developer switching. A custom or historical split-mode Strategy can still request `llm`;
LoopLab then validates and probes that in-process target lazily. If either check fails, the engine
keeps the current external Developer. Unified mode never performs this live facade-breaking swap.

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
get the repo scouts, the env inspector, the probe, and any operator-pinned preflight commands
(`read_file`, `grep`, `find_files`, `list_dir`, `pkg_info`, `py_api`, `read_installed`,
`grep_installed`, `gpu_info`, `run_probe`, and—only when declared—`run_dev_command`) — but **no write
tools** — so the Developer
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

### `run_probe` — checking instead of guessing

`developer_probe` (on by default) gives the Developer one more tool: **`run_probe(code)`** runs a short
**Python program** against the real environment, and returns its exit code plus tailed stdout/stderr.
It exists because a Developer that cannot check something works around it instead — the shape the
operator saw was, verbatim, *"Since I have no shell/install ability, the cleanest repair is a small
loguru shim module"*: a **fake library**, written rather than spending one line finding out the real
one imports. `env_inspect` answers *static* questions (a version, a signature, an Enum's members);
`run_probe` answers "does this actually work **here**" — does this import, does this CSV parse, does
the code I just staged get this API right.

**It is not a free-form shell, and that is the design, not a limitation.** The [source-tree read
fence](tasks.md) is a CPython audit hook: it covers `open` inside an interpreter and nothing else. A
tool that could run `cat`, `cp` or `bash` would be an execution surface the fence does not reach — one
`cp <source>/final/model.safetensors ./ckpt` away from laundering somebody else's result into a node's
workspace, which is exactly the defect the fence was built for. So the probe surface **is** the
interpreter, which is a *boundary* rather than an allow-list of commands that would need maintaining.

When an operator really does want compilation, a focused test, or a Bash validator, the separate
[`developer_commands`](tasks.md#operator-pinned-developer-commands) contract records the **complete**
argv in the task snapshot and exposes only `run_dev_command(name)`. That does not widen `run_probe`:
the command runs in a disposable candidate workspace, and the model cannot add arguments or retain
candidate-tree changes (declared mounts retain their task/trust-tier policy). This is an in-house
repo Developer contract; external coding-agent presets keep their own native process/tool boundary
and do not receive `run_dev_command`.

Four rules, none of them a list:

| the probe cannot… | enforced by | what it closes |
|---|---|---|
| read the operator's editable source tree | a fence rendered from the SAME `read_fence.fence_inputs`/`render` the engine installs, always at `deny` — turning `read_fence` off must not open a second door | v6 node 4: a good model trained, then a human's checkpoint scored and **recorded** |
| write a file, anywhere | THREE rungs, each stated for exactly what it covers: an audit hook (the actionable, non-`OSError` message, for what CPython audits) **plus** a **Landlock** ruleset that handles every filesystem-mutating access right and grants **nothing** (existence, in the kernel, for every caller — a native writer, `ctypes` into libc, a syscall CPython does not audit) **plus** `RLIMIT_FSIZE 0` (content). The kernel rung landed 2026-08-15: `os.mknod` and `os.mkfifo` raise **no** audit event and the rlimit bounds bytes and not existence, so both created a file outside the replica — and `os.mknod` creates a REGULAR ZERO-BYTE file, which passes a `needs`/`expect` presence check and shadows a real module as an empty `.py`. The two names are not the class: `pyarrow.parquet.write_table` raises **zero** audit events, which is why the boundary is the kernel and not a list | the mid-run `pip install` that corrupted a **running** node's site-packages and cost a repair generation — closed without ever naming pip |
| start another program | `subprocess`/`os.exec*`/`os.system`/`posix_spawn` refused. A **fork** is not: a fork inherits the hook, an exec replaces it — the rule is "no new program" | this is what makes the read fence total here rather than stopping at the first `subprocess.run(["cat", …])` |
| see a GPU | `CUDA_VISIBLE_DEVICES=""` | a probe allocating on a device a **sibling node's** training holds for hours behind the host GPU-pool lease |

It runs in a **disposable replica** of the files the Developer has staged so far (the same paths
`write_file` uses), in a temp directory deleted when the call returns. The replica flows one way:
authoring → probe, never back.

**Why it produces no run event.** Those four rules are exactly the statement that a probe has no side
effect, so [engine invariant #3](architecture.md) — every side effect gated on a domain event — has
nothing to gate. The probe is recorded the way every other Developer tool call is: a `tool` span in
`spans.jsonl`, visible in the node's trace and conversation views, not folded into `RunState`. The two
decisions are one decision — a probe that could write its own workspace would make
`node_created.files` stop being the whole record of what the Developer built, and would need an event.
It is also why there is no probe *count* budget: the bound is the per-probe `developer_probe_timeout_s`
(default 60 s, hard max 300) inside the Developer session's own wall-clock ceiling.

Anything that must **produce** a file is not a probe: it belongs in the node's files and its declared
eval stages, the surface that has a metric contract, a live log and a repair loop attached.

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

**A stream the gateway CUTS mid-answer is kept, not re-asked.** A proxy whose own upstream dies
half-way through a generation reports it *in band* — a `data: {"error": …}` frame inside a response
that already returned HTTP 200 and has been streaming for minutes — so the client is holding a real,
truncated answer, not a failed request. It keeps it: the reassembled body comes back with
`finish_reason` `truncated`, the call reaches the accountant, and it is deliberately **never
cached**, so a later identical temperature-0 ask re-issues instead of being served an amputated
answer. **The stream is read to its END, not to its error frame** — the openai SDK treats the first
error frame as terminal and closes the response, so a gateway that reports the failure and *then*
reports what it billed for the tokens it already forwarded would have its price thrown away
permanently. The client holds that frame back, reads the rest, and lets the SDK raise it last, so
such a call is priced normally; only a cut with genuinely nothing behind it is recorded as
**unpriced**, which is not the same as free. Only a cut that produced *nothing at all* is retried —
and that retry drops SSE for the next attempt, exactly as a stalled stream does. The split is what
keeps the retries affordable: re-asking happens only where re-asking is free. Measured on a 20-run
AlgoTune campaign, 26 cut streams burned **13.15 hours** — 18.7–94.6 % of each affected run's
lifetime — and $1.66 that reached no ledger; one task spent 94.6 % of its run inside six of them
and produced zero nodes. Each cut is announced at **WARNING** naming what was kept and what it
cost, because a shorter answer and a missing price are both invisible from the call site — and
"what was kept" counts reasoning and tool-call arguments, not just `content`: a reasoning model cut
mid-think has spent everything on its chain of thought and has not begun its answer, which is the
normal shape of a cut here rather than a corner.

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
| **Nobody answered** — the request never completed: the call raised, the endpoint was unreachable, a 401/402, a transport error surviving the client's own retry ladder | `unanswerable` | The node (`developer_crash`) **and the run**: one run-level pause naming the provider, `resume` once it is back |
| **The model answered something unreadable** — an action outside `repair`/`abandon`/`reject_idea`, an empty or missing one, the literal word `unanswerable` arriving from the wire, **or no emit at all** (prose replies your endpoint would not force into a tool call, the stuck detector, the turn/wall-clock budget) | `unreadable` | **Only the node**, terminalized like an `abandon` with the eval's own failure reason, so a node reset re-opens it. No pause — the endpoint just answered |

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

The **no-emit** row moved from the first line to the second on 2026-08-06, for the same reason: it
was reachable from a demonstrably healthy endpoint. A local vLLM/SGLang/llama.cpp deployment that
ignores `tool_choice` and a model that prefers to reason in prose together end the tool loop with no
emit — every HTTP request having completed — and that used to raise the run-level pause and tell you
to check your credits. The rule now is mechanical rather than descriptive: the tool loop **raising**
is a transport failure; the tool loop **returning without an emit** is an answer nobody could read.

Without any of this, a provider error string was committed as the node's code and re-evaluated:
one real run turned an out-of-credits `402` into **2345 `node_repaired` events on a single node over
3.5 hours**. Use [`looplab timings RUN_DIR`](cli-reference.md#timings) to see where a run's
wall-clock actually went (LLM vs eval vs repair vs tools, per node **and** run-level, reconciled
against the run's real duration with the untraced remainder named).

**A phase that spends must open a span, or `timings` cannot see it.** A `phase_progress` beacon
alone appends an event and opens nothing, and an LLM call made outside every span is written to
`events.jsonl` with `trace_id: null` and lands in no span at all. Measured over a 20-run AlgoTune
campaign on 2026-08-20, **1,579 of 6,002 paid calls (26 %) were untraced**, $1.77 of them the
novelty gate — a beacon-only phase running a 12-turn agentic loop plus a whole second Researcher
proposal on each rejection, 11 % of the budget and 6.6 of 60.8 run-hours with nothing saying so.
`SharedEngineMixin._paid_progress` is the beacon and the span together; the novelty phase uses it
since 2026-08-20, so its cost now appears under `phase=novelty` like every other phase. Turn the
gate off with `novelty_mode=off` (**not** `novelty_gate=false`, which is a legacy alias that forces
nothing) — see [configuration](configuration.md).

### What the triage judge is allowed to look at

Until 2026-08-15 the answer was: `res.stderr[-500:]`. Five hundred characters — the tail of one
stream, prefixed with the failing stage's name. That is the *whole* evidence base for the role whose
job is "why did this stage die, and what should change", and on a long run it is not a summary of the
failure, it is **the last frames of whatever happened to be rendering when the process died**.

The measurement that ended it is `runs/rubertlite-dr-unified-v8` node 3. Its `train` stage declared a
22,000 s ceiling and was killed at 22,003 s, and its log holds two progress bars on different totals:
the training bar reached `10590/10590 [5:29:35]` and printed `{'train_runtime': 19775.3, …, 'epoch':
14.98}` — all fifteen epochs, done — after which a retrieval phase started its own bar and was killed
at `223/361 [31:29<19:50]`, about twenty minutes from a result. The 522-character `error_in` on that
`node_repaired` row contains the last two renders of the *second* bar and nothing else. The verdict
read that bar's elapsed field as training progress ("node 3 is still in epoch 1 at 31:20"; `31:20` is
verbatim the `222/361` render) and prescribed halving the batch **and** cutting `n_epochs` 15 → 8.
Six GPU-hours went in the bin. That epochs cut **never landed**: `repair_verify` stamped
`unmet: ['grad_accum', 'n_epochs']` on the same row, no repaired file sets it and the node's
`config.yaml` still reads `n_epochs: 15`.

**The projection that stood here — 22,096 s into the same 22,000 s ceiling, extrapolated live from
`1928/10590 [57:46]` at 1.798 s/step — has since been FALSIFIED by the run, and it is corrected here
rather than left standing**. Five sibling sites carried the same figure and were retracted in the
same session (`docs/BACKLOG.md`, `docs/guide/configuration.md`'s `repair_log_tools` row, the process
diagram's `e_ir` block, `core/config.py` and `engine/train_monitor.py`); a SEVENTH, `CLAUDE.md`'s
`looplab/tools/` row, was outside that enumeration and was retracted on 2026-08-15 once a review
found it still asserting the projection as current. The retry took
**19,915.75 s** and PASSED. What saved it was not the epoch cut but a second edit in the same repair,
which deleted the in-`train` `test_model()` call on the note that the full-index retrieval "is run
independently by the protected `score` stage" — and `score` then ran 3,130.3 s of its own. The point
survives the correction and is sharper for it: a diagnosis this wrong prescribed a compute cut the
node did not need, and the change that actually fixed it rode along in the same session. The node
recorded **0.762048** and is the run's champion. It carries one further mark from that chain — on the
record, not on the metric: attempt 4's OOM fix set `batch_size 4096` / `gradient_accumulation_steps
4` imperatively in `train.py` while `idea.params` AND `config.yaml` both still declare 8192 / 2. See
`engine/repair_verify.py::declared_param_overrides` and the `params_overridden` champion caveat.

With `repair_log_tools` on (the default) the judge gets the same `read_log` / `metric_series` pair
the two live-eval watchdogs already have, over the same map: the stage logs this eval's own resolved
plan names, chosen by NAME and never by path, each read a bounded seek whose answer states the bytes
it covered. One `metric_series(metric="step", whole_run=true)` on that log answers the question the
tail could not: the counter reached 10,590 of 10,590, and the `223` belongs to a lane whose total is
361. On a node that really *was* still training (v6 node 5's timeout, killed at `4614/7060`) the same
call says so, which is the point — the tools are not a thumb on the scale toward "it finished".

**`LogSource.floor` matters more here than anywhere else.** A repair runs when an attempt has just
died, on a log every earlier attempt of that node also appended to; the floor is
`attempt_byte_floor` over the snapshot taken *before this attempt started*, so a repairer diagnosing
attempt N cannot read attempt N-1's curve as its own. That snapshot is why the log plan is resolved
at the top of every attempt rather than lazily at the failure — by then there is no "before" left to
take.

### Why the kill bar is a bar and not a ladder

`train_monitor_kill_confidence` is 0.8, and a `broken` verdict under it does nothing. That looks
wasteful on a node the watchdog has already doubted several times, and on `runs/e5small-dr-unified-v8`
node 2 it demonstrably was: three sub-bar `broken` verdicts — **0.70 at 08:31:58, 0.65 at 08:44:38,
0.70 at 08:56:39** — and the kill only came with the 0.90 at 09:07:14. **36 minutes of GPU ran under
a watchdog that already believed the node broken three times.**

The obvious answer is to kill on *K consecutive* sub-bar `broken` verdicts. It was measured against
every alert this box has recorded — 259 `train_monitor_alert` rows over 35 node-generations, 114 of
them `broken` — and it is **refused**. Only four node-generations ever reach two or more consecutive
sub-0.8 `broken` verdicts, and a K=3 ladder fires on three of them:

| run | node | the sub-bar streak | what the node did |
|---|---|---|---|
| `rubertlite-dr-unified-v6` | 1 | 0.62, 0.62, 0.75 | **recorded 0.715142** |
| `e5small-dr-unified-v4` | 3 | 0.75, 0.70, 0.75 | **recorded 0.790898** |
| `e5small-dr-unified-v8` | 2 | 0.70, 0.65, 0.70 | failed `not_learning` |
| `e5small-dr-unified-v4` | 12 | 0.62, 0.70 | `idea_rejected` — never trained |

**Two good nodes destroyed per node saved**, and one of the two carries the strongest number in its
neighbourhood. The fourth row is not evidence either way: that node never trained. So the 36 minutes
are real, and they are the *price* of that ratio rather than an argument against it — a low-confidence
`broken` verdict on this box is far more often a slow start than a dead run.

What would change the answer is a signal that separates those rows from each other. The trajectory
veto is the existing attempt and it is consulted on every one of them. Until something does separate
them, the bar stays a bar: the `broken-verdict-ladder` decline in `engine/train_monitor.py` carries
the number, and lowering `train_monitor_kill_confidence` is the same trade bought at a worse price, since
it discards the confidence signal entirely instead of counting it.

### And since 2026-08-27 the look has a wall

The turn grant that came with those tools is additive **over a finite budget**, and the shipped
configuration on this box is `agent_max_turns = 0`. What was left bounding the triage loop was
`agent_emit_after`/`agent_emit_force` — 300/500 **turns**, sized for the pilot's self-driving loop —
and the `StuckDetector`, which by its own docstring catches 1-cycles and 2-cycles and leaves "exotic
longer cycles" to those backstops.

`runs/e5small-dr-unified-v8` node 2 fell straight through all three. Its `train` stage timed out at
09:07:19 and the engine did not return to work until 11:07:32: **88.3 min of triage (206 provider
calls, 19,156,560 tokens), then 31.9 min of the already-bounded repair**, with both H200s at 0% the
whole time. What the 88 minutes bought was one 663-line file — `training/loss.py`, read 78 times
through 34 distinct windows, the identical twelve-window sweep repeating verbatim **six times**, 664
of its 667 lines served five times or more — while the transcript grew 14,548 → 160,671 prompt
tokens and all 206 calls re-sent it.

That round-robin shape was **already known and already answered with a nudge**: `tool_loop`'s
`_REPEAT_NOTE` appends *"this exact call has now run k× this phase with an IDENTICAL result"*, on the
stated reasoning that "we only TELL the model it is repeating itself so it can stop on its own". It
fired **57 times inside this one triage** and the model did not stop. `Settings.triage_time_budget_s`
is that rung escalated from informing to bounding, on evidence that informing was tried first — not
instead of it.

It is not a one-off either. The **same node's second triage**, measured while this was being
written, ran **48.2 min** (then 38.4 min of repair) before node 2 was abandoned — 4.2× the worst
decision in the entire prior corpus, on an independent draw. Two decisions, 136.5 minutes of triage
on one node; the ceiling makes it 40.

The number is 1200 s and it is measured: across the **124 triage decisions** in the eight runs that
carry `spans.jsonl`, the worst prior triage wall is **11.6 min** and the worst prior call count is
**91**. A 20-minute ceiling therefore fires on none of them, and would have cut this one at 20 min
instead of 88. It is deliberately the same number as `developer_session_time_budget_s`: triage and
the repair it hands to run one after the other on the same eval-blocking thread.

Two things it deliberately is not. It is **not a `min()`** with a configured budget — an operator who
sets a finite `agent_time_budget_s` keeps their number, for the same reason `0 + n` may not turn
their "no turn cap" into a cap. And it is **not a lost verdict**: `drive_tool_loop`'s time exit
announces the budget through its `on_budget` observer — so the operator is told the investigation was
cut short, and `node_repaired.budget_exhausted` records which bound ended it — and then forces the
emit from everything gathered. The triage still answers; it stops browsing.

A session-scoped *"this exact call+result has been served m times"* rung was measured and **refused**,
recorded here so it is not re-proposed: over 2,472 tool-using sessions the max-serve distribution is
`{1: 1782, 2: 285, 3: 181, 4: 100, 5: 48, 6: 35, ≥7: 41}`, and this pathological session's own max is
**5** — lower than 41 healthy sessions. At `m=3` it fires on 259 of the 586 sessions with ≥40 calls.
The signal does not separate them; wall clock does.

**It widens what the judge can SEE and nothing it can decide.** The verdict vocabulary is still
`repair` / `abandon` / `reject_idea`, both fail-closed degradations above are unchanged, and the
terminal below the triage call still carries the eval's own authenticated failure `reason`. Nothing
a model reads through these tools can reach a metric, a champion, selectability or a violation — it
is text the candidate's own script wrote, which is exactly the line `docs/36` draws.

Turn it off and the paid call you have always made is reproduced byte for byte, prompt included; a
run resumed from a snapshot written before the field existed gains nothing.

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
| **Deep-research memo** | `RunState.research` | Researcher, crash-triage, repair-critic | a one-line takeaway in the state brief — **prefixed with the memo's own verifier tally** and with the fact that the verifier checks a memo's CLAIMS and never its summary (`Settings.memo_verdict_cue`, ON; `false` restores the historical line byte for byte) — **plus** a `read_research_memo` tool to pull the memo on demand ONE SECTION at a time (`section=` `overview` (default) / `directions` / `findings` / `claims` / `summary`), **each claim carrying the verdict this run's own verifier gave it** (`supported` / `unsupported` / `unclear` / `cited`, or `unverified` when the check was bounded away or its rows do not align with the claims) — the groundless ones LEAD every section, and the default overview carries the verifier block, the memo's **recommended directions in full** and a clipped summary, then names what it left out beside the call that returns it; **and since 2026-08-29 every rendered direction is MARKED with which of the memo's own two lists it came from** — `[question]` (a family of experiments) or `[experiment]` (one concrete change) — recovered by exact MEMBERSHIP rather than by which field the render read, because the prompt asks the model to also fill `recommended_directions` with the union of both UNCHANGED and it complies: on `e5small-dr-unified-v11`'s first memo `recommended_directions == next_experiments + open_questions` exactly, in order, 6/6 and 4/4 verbatim. That union is what made the flat list unreadable, and it cost a real misreading: v11's card-0 matched `next_experiments[1]` and matched `recommended_directions[1]` byte-for-byte, so no observation of the card could name the carrier. An entry in BOTH lists or in NEITHER is left unlabelled — a refusal that records itself, never a guess; the memo itself is produced from a lifecycle-aware coverage sample with an explicit omission receipt |
| **Operator yields** | derived from the DAG | Strategist | a per-operator gain-per-second line in the strategist brief, so it tunes the operator mix from evidence, not priors |
| **Operator directives** | `RunState.pending_hints` | Researcher, Strategist, pilot, crash-triage, **Developer** | one `render_hint_directives` helper — the engine also folds directives into the idea handed to `implement`, so a directive steers the **code**, not only the proposal |
| **Run states** (paused / awaiting-approval / trust-flag / stuck-build) | `RunState` | boss / assistant | an "ATTENTION" block in the boss context, surfacing the states where human intervention is most valuable |

**A delivered signal can still be silently empty, and this one was.** `read_research_memo`'s
renderer keyed on `verification["summary"]` — a field neither writer has ever produced — from the
commit that introduced it (2026-07-10) until 2026-08-16. Measured over every `research_completed`
row in `runs/`: **98 memos carry a verification block, 0 carry a `summary` key**, and 16 have every
verdict `unsupported`, so **not one verifier verdict ever reached a role through that tool**. The
signal-delivery probe could not see it: the memo WAS delivered, in full, minus the one part that
said whether to believe it. `rubertlite-dr-unified-v8` paid for that — its `at_node: 0` memo records
`total_verdicts: 8, unsupported: 8`, the first of them refusing a `recall@100=0.8776` claim with
`cited experiments do not exist: [9]`, and the number became the run's stated anchor anyway. **That sentence covered TWO facts with opposite remedies until 2026-08-29.** `trust/memo_verify.py::_evidence_snapshot` admits a cited node only at a TERMINAL lifecycle (`evaluated`/`failed`, non-tombstoned, non-aborted) — correct, since a claim cannot be evidenced by an experiment that produced no number — but when no cited id cleared that bar the note said *do not exist* whether the id was invented or merely **still running**. Measured over every event log preserved on this box: **259 such notes carrying 397 cited ids, of which 169 (42.6 %) named a node that existed and was mid-eval at that memo's own timestamp**, spread across all eleven runs that produced a memo. Live on `e5small-dr-unified-v11`: memo0 cited `[1, 13]` when the run had zero nodes, and memo2 cited `[0, 1]` — created 15:23:37 and 16:47:07, both real, both pending, and the memo's own summary says "both still pending drafts". Absent means the model invented an id and must stop; pending means the citation was ACCURATE and merely premature. The note now partitions them (`cited experiments have no result yet: [...]`); the VERDICT is unchanged, `_evidence_snapshot` is untouched, and every durable claim gate reads the verdict and never this string. The
reader now lives beside the writer (`core/advisory_payloads.py::memo_verification_view`) and
`tests/test_research_memo_verdicts.py` re-derives BOTH key sets from source, so a reader keyed on a
field nothing writes is a red test rather than a quiet one. What that does NOT change: the tool
returns a string, so nothing here reaches a metric, a champion, a selectability decision or a
violation (docs/36 — a wider CONTEXT must not widen the trusted set), and folding all 46 event logs
in `runs/` gives a byte-identical corpus digest before and after.

**A signal can also be delivered and then cut in the last 30 characters of the path.** Until
2026-08-19 `read_research_memo` returned ONE string — verifier lead, summary, findings, claims,
recommended directions — and the agent loop bounds every tool result at `RESULT_CAP = 4,000` chars
**head-keep** (`agents/tool_loop.py::_cap_tool_result`). Replayed over all 90 memos in `runs/`: the
render is a median **9,083** chars, **89 of 90 exceed the cap**, a median **5,180** chars are
discarded, and the cut is not where the padding is — `Summary` and `Findings` survived every time,
`Claims` began past the cut in **80 of 89** and **`Recommended directions` in 89 of 89**. In the real
traces: **375** recorded `read_research_memo` calls, **362** over the cap, and of the 212 whose
recorded render still shows a directions section, **194** have it starting past the cut. The run
paid for a think-hard review and then discarded its conclusions on delivery.

**The cap was not raised, and that is the point.** It is the tool loop's shared bounded-output
contract, and a memo that merely fits is still the "портянка" the operator asked to be rid of. What
changed is WHAT is kept. The memo is now ADDRESSABLE: an omitted `section` gives the OVERVIEW — the
verifier block, the recommended directions **in full**, and a summary clipped to
`_MEMO_OVERVIEW_SUMMARY` (600 chars; it is the one field of the memo nothing checks, and
`_state_brief` already pushes its first 300 chars into every proposal prompt unasked) — and the
overview ends by naming every section it left out **beside the exact call that returns it**, each of
which gets the whole cap to itself. That is this repo's existing bounded-answer rule
(`tools/log_tools.py` rule 3: say what you did not cover and name a remedy the caller has not already
spent). Measured after the change over the same 90 memos: **0 of 90** answers are cut by the agent
layer in any section, and the directions arrive complete in **86 of 89** default answers (the other
3 name `read_research_memo(section="directions")`, which delivers all of them for **89 of 89**).

**And a delivered signal has two channels, which can disagree about the same payload.** The row
above is one signal with a PULL half (the tool) and a PUSH half (`roles.py::_state_brief`), and the
push half needed its own fix on the same day. `trust/memo_verify.py::verify_memo` verifies a memo's
`claims` and has never, at any commit, looked at its `summary` — so the field the state brief splices
into a prompt is the one field of the memo that no verifier has ever checked, and it reaches THREE
phases, not one: measured over `rubertlite-dr-unified-v8`'s `spans.jsonl`, **293 real prompts**
(`propose` 269, `triage` 20, `repair_critic` 4), and **none of those 293 whole prompts contains the
word `Verifier` or the word `unsupported`**, while the memo behind 52 of them records
`total_verdicts: 8, unsupported: 8` and opens *"climb from the known ~0.88 plateau"* — a rounded
number from `rubert-dr-0807`, which `engine/eval_contract.py` reports as a different evaluation
contract. (The literal `0.8776` is in 11 of that run's 15 full memo summaries and in **0** of the
300-char windows the brief actually pushes; the carrier was the rounded form.) Corpus-wide: of the
100 pushable memos in `runs/`, 81 push a decimal number, 76 push one from a memo carrying a
non-`supported` verdict, and 13 push a ≥3-decimal number that is a node metric of a provably
different-contract run and of no node of their own — across five runs, so this is a mechanism rather
than one run's accident. The cue ANNOTATES and withholds nothing: replayed over all 103 memos, 100
lines change, **0 lose a byte** of the text they carried before, and the added clause is 87-120 chars
(median 102 — 0.64 % of v8's median 15,930-char user turn, against a `context_budget_chars` of
1,000,000, so it displaces nothing). Suppressing an unsupported memo's summary was weighed and
refused for the reason the pull half refused it: 26 of those 100 memos have no supported verdict at
all and 45 of the 45 verdicts the deterministic pass emits are `unsupported` about the CITATION, so
suppression drops real findings over bad footnotes. And it states no opinion about which number is
foreign — a summary is prose with no per-number provenance, and deciding that from the model's own
text is what docs/36 forbids.

**The invariant.** `engine/signal_delivery.py` is a registry of these routes (signal → folded field
→ injection site → consumer), and `tests/test_signal_delivery.py` asserts each injection symbol
resolves *and* that a synthetic input's content actually reaches the rendered output. A signal added
to the registry without a delivery probe fails the suite — so *"the signal silently stopped being
delivered"* is a red test, not the next review's finding. Three of the routes (trust flags, watchdog
signals, operator directives) are **push** (the engine injects them), two (deep-research memo,
and the scored eval's own stderr via `read_logs`) are **pull** (a tool the
agent may call for depth), and the rest ride the always-on folded-state briefs.
The scored-eval route (`Node.stderr_tail`, written by `engine/evaluate.py::_scored_output_evidence`)
is deliberately pull rather than context: the `triage_rationale` route carries a ~100-char verdict
about a node that FAILED on the always-on digest, while this one is up to 4,000 characters of the
eval's own text on EVERY scored node, and the always-on working set is under a hard char cap. Before
it existed a node that exited 0 and scored badly kept nothing but its metric and a 500-char stdout
tail — so the loop could see *what* a node scored and never *why*. The full rationale is
in `docs/14-agent-framework-mega-review-2026-07-10.md` §1.

Deep Research uses the shared `agent_self_plan` setting. With the shipped default enabled, the
researcher is instructed to create a typed 2–4 item `update_plan` before investigating and can revise
it as evidence gaps close. Its first prompt is deliberately bounded: the current champion, early
seeds, eligible top metrics, representative genuine failure classes and recent active experiments are
preferred. Tombstoned/aborted rows and durable pre-dispatch discards are excluded from experimental
evidence and counted separately; constraint/trust-ineligible rows reached through another coverage
bucket are explicitly labelled. The prompt also states how many active experimental rows were omitted.
External, repository, memory, prior-run, and free-form current-run text (including
rationales/errors/logs) is always covered by an immutable untrusted-data boundary, even when an
operator hot-overrides the rest of the Deep Research system prompt.

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
| `hide_empty_tools` | (off) Stop ADVERTISING a tool whose provider reports it holds nothing right now. Only the OFFER is withheld — a hidden tool still dispatches if called — and the check is re-made once per agent PHASE. Only a definite `0` hides; a store that could not be counted stays offered. The prompt publishes the same counts either way. |
| `literature_search` | An arXiv search tool (network-optional) |
| `web_search` | Web search/fetch for the Deep-Research stage (network-optional) |

When `memory_dir` is configured, the same skill tool also reads auto-distilled Markdown under
`<memory_dir>/skills`. A new one-run `status: candidate` remains on disk for later cross-task
promotion but is excluded from the production agent surface. Only `status: promoted` auto-skills
are listed/loaded, and their bodies carry an `UNTRUSTED_MEMORY_AUTO_SKILL` provenance label. The
library constructor's explicit `include_auto_candidates=True` seam is for review and tests; it is
not a runtime setting. Hand-written and legacy skills keep their previous visibility and body.

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
| `repo_onboarder_system` | The run-start, pre-search repo onboarding stage that authors a ratifiable `read_metric(workdir)` adapter |
| `tool_researcher_system` | The tool-using Researcher — the default agentic Researcher |
| `strategist_system` | The plain LLM Strategist (meta-control decisions) |
| `tool_strategist_system` | The agent (tool-using) Strategist |
| `pilot_system` | The unified agent's action pilot (chooses the next macro action) |
| `triage_system` | The unified agent's crash triage (retry / repair / abandon) |
| `triage_look_invitation` | The sentence that tells crash triage its 500-char stderr tail may be about a DIFFERENT PHASE than the one it is diagnosing. Spliced only when `repair_log_tools` actually wired the log tools, so it is a separate key: with the tools off the historical ask is reproduced byte for byte |
| `triage_findings_invitation` | The other half of that look: the ask that turns what crash triage READ into what the run RECORDS — a self-sufficient `summary` (the causal statement with its numbers inline) plus `findings`, the trail of `{source, locator, quote, means}` behind it. Spliced under the same condition as `triage_look_invitation` and for the same byte-for-byte reason; a separate key so an operator can reshape the record without touching the diagnosis prompt |
| `foresight_system` | The foresight ranker (predict-before-execute idea/hypothesis prioritization) |
| `bestofn_judge_system` | The best-of-N judge (picks the best of N candidate implementations) |
| `merge_system` | The hybrid-merge adjudicator (lesson & hypothesis-board consolidation); `$kind` and `$detail` vars |
| `concept_consolidate_system` | The live concept-map and `concept-coverage` vocabulary consolidator (merges synonymous `axis/slug` ids without collapsing distinct techniques) |
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

`CrossRunTools` declares its audience explicitly at construction. A model-facing `audience="run"`
provider returns no rows until it is bound to a run; it never falls back to portfolio-wide access after
a missing or failed bind. The unbound `audience="portfolio"` mode is reserved for intentional owner/CLI
inspection (and pre-task Genesis). Applicability scoping is still not a multi-user authorization layer,
and the exact tool result is not yet durably joined to the consuming model turn.
