# LoopLab — working-loop implementation

A runnable slice of the [LoopLab](00-INDEX.md) design: the **autonomous search loop**
plus the result-moving levers and the knowledge/trust seams. It drafts → runs candidates
in a sandbox → evaluates → improves the best → **merges** top candidates → repeats, with
the **event log as the source of truth** and **crash-resume by replay**. Runs fully
offline (no API keys, no Docker) on a toy optimization task.

**Iterations implemented (of [06-implementation-plan.md](06-implementation-plan.md)):**
I0–I6 (working loop) · I7 (debug + improve operators) · I8 (consistent-CV harness +
purged walk-forward) · I9 (leakage detectors) · I10 (variance gate) · I11 (ensemble/merge
+ multi-parent DAG) · I12 (multi-seed top-k confirmation) · I14 (custom JSONL span
exporter) · I16 (data profiler) · I17 (pluggable vector store + agentic retrieval) · I19
(cross-run memory) · **I2 (live LLM Researcher** via any OpenAI-compatible endpoint;
tool-calling + text fallback) · I21 (secret-leak gate + trust-mode sandbox tiering) ·
I22 (opt-in `EvolutionaryPolicy`).

## Live LLM (real model driving the loop)

The Researcher can be a live model via any OpenAI-compatible endpoint (Ollama / SGLang /
vLLM / OpenAI) — a base_url change, not code. Tested with **Qwen3-8B on Ollama** (native
Windows, works on the RTX 5090; SGLang has no native-Windows path — it needs WSL2/Docker).

```bash
ollama pull qwen3:8b
python -m LoopLab.cli smoke                                   # verify endpoint + tool-calling
python -m LoopLab.cli run examples/toy_task.json --backend llm --max-nodes 6
```

With `--backend llm`, Qwen3-8B reasons about the objective and refines params
gradient-descent-style toward the optimum — the engine, sandbox, policy, and event log
are all unchanged (only the role backend swaps, per ADR-7). Config: `LOOPLAB_BACKEND=llm`,
`LOOPLAB_LLM_MODEL`, `LOOPLAB_LLM_BASE_URL`. For SGLang instead, point `llm_base_url` at
its `/v1` (run it in WSL2/Docker).

### LLM writes the code (full coding loop)

```bash
python -m LoopLab.cli run examples/code_regression_task.json --backend llm --max-nodes 6
```

Here the LLM is also the **Developer**: it writes a complete numpy script that reads the
dataset from a `data.json` asset (materialized into each node's sandbox workdir), fits a
polynomial ridge model, runs 5-fold CV, and prints the metric. When a generated script
crashes, the **error-feedback debug operator** hands the failing code + stderr back to the
model to repair — observed live: a failed node repaired into a working solution, best
landing at the true degree near the noise floor. This is the real invent → implement →
run → self-repair loop.

### Agentic retrieval (Researcher consults a knowledge base)

```bash
python -m LoopLab.cli run examples/regression_task.json --backend llm \
    --knowledge-dir examples/knowledge --max-nodes 6
```

With `--knowledge-dir`, the Researcher becomes a **tool-using agent** (ADR-16): in a
bounded multi-turn loop it may call `kb_search` (semantic), `grep` (lexical), `list_notes`,
or `read_note` over the markdown notes, then `emit` its structured Idea — the model
chooses which tools to use. Observed live with Qwen3-8B: guided by a note on degree
selection, it proposed the true degree (2) and converged at the noise floor. The
orchestrator is unchanged — the tool-using Researcher drops in behind the same Protocol.

## External coding-agent Developer (optional, ADR-7)

The Developer role can be delegated to an external terminal coding agent via the
tool-agnostic `cli_agent.CliAgentDeveloper` — it runs the agent head-less in a git
worktree, points it at the local Ollama endpoint, and reads the edited solution back. The
tool is a preset (`opencode` / `aider` / `goose` / `continue`):

```bash
python -m LoopLab.cli run examples/code_regression_task.json \
  --backend llm --developer-backend opencode --model qwen3:8b
```

Verified live end-to-end with **OpenCode + local Ollama**. Three things make it robust:

- **Self-contained, headless.** `cli_agent.opencode_config()` drops an `opencode.json`
  (local Ollama provider, explicit `--model`) into the agent workdir, so OpenCode never
  fetches the external model registry — the startup fetch that hangs behind a corporate
  TLS proxy. (`_resolve_launcher` maps the bare `opencode` to the real `.exe`; output is
  captured as UTF-8 — both Windows-specific fixes.)
- **Output validation** (`validate.py` + `roles.ValidatingDeveloper`, on by default).
  Every agent output is checked (launched / not-timed-out / produced / modified-seed /
  parses / in-surface). On failure it re-prompts the agent with the reason, then falls
  back to the in-house LLM Developer. Each node logs an `agent_validated` event surfaced
  in the HTML tree + SQLite read-model. Toggle with `--validate-agent` /
  `--agent-max-retries`.
- **Patch-gated, multi-file** (ADR-7 Rule 3, on by default). The agent runs in a git
  worktree; its `git diff` is gated by an edit-surface allow-list (`--agent-surface`,
  default `*.py`, reject-not-strip). Accepted files (solution + helper modules) become
  `Node.files` (files-as-truth, resumable) and are materialized into the eval workdir.
  Toggle with `--agent-patch-gate`.

LoopLab's built-in LLM Developer (writes code via Ollama, no external fetch) remains the
zero-dependency default coding path.

### MLEBench-style held-out benchmark (`kind="mlebench"`, I20)

A competition-shaped TaskAdapter with **leaderboard grading**: the solution gets
`train.json` (X + labels) and `test.json` (X only — labels withheld) and must call a
private `grader.score(preds)`, so the loop optimizes the *true held-out* accuracy, not a
self-reported metric. The agent cannot overwrite the grader (asset-name protection); on
`trusted_local` it could still read the key — an accepted caveat (see `mlebench.py`).

```bash
python -m LoopLab.cli run examples/mlebench_task.json   # offline templated k-NN
```

### Real MLE-bench competitions (`kind="mlebench_real"`, D1)

Run **actual Kaggle competitions** from OpenAI's [MLE-bench](https://github.com/openai/mle-bench):
the engine gets the official `public/` split, writes `submission.csv`, and the **host** scores it
with mle-bench's *real* grader against held-out answers — producing the genuine MLE-bench metric
plus the official medal / above-median report. The candidate sandbox stays zero-dep (numpy+stdlib,
CPU-only); only the host grader uses pandas/sklearn. Auth uses the modern Kaggle `KGAT_` Bearer
token directly (`looplab/kaggle_dl.py`), bypassing the kaggle client that can't use it.

```bash
python -m looplab.mlebench_prep --selected               # download+prepare the CPU-lite comps
python -m looplab.cli run examples/mlebench_real_spooky.json --out runs/spooky --backend llm
```

Full runbook (token, per-competition rule acceptance, untrusted tier): **[docs/MLEBENCH.md](docs/MLEBENCH.md)**.

### Work inside an existing repo (`kind="repo"`, ADR-7)

Point the R&D agent at an **existing experiment repo**: it edits code within an
allow-listed surface, and success is the **operator's own eval command + metric** — never
a metric the agent authored. The eval = *"run a command in the workspace, read a metric
from a declared source"* (`stdout_json` / `stdout_regex` / `file_json` / `file_regex`),
which generalizes the `solution.py`-prints-metric and mlebench-grader models.

```jsonc
// examples/repo_task.json
{
  "kind": "repo", "direction": "max",
  "editable_path": "examples/repo_example",   // the agent edits this repo (worktree copy)
  "edit_surface": ["*.json"],                  // … only files matching these globs
  "protect": ["ttrain.py"],                    // … never the eval entrypoint
  "eval": { "command": ["python", "ttrain.py"],
            "metric": {"kind": "stdout_json", "key": "metric"} }
}
```
```bash
python -m LoopLab.cli run examples/repo_task.json --backend llm --developer-backend opencode
```

The editable repo is mounted into each eval workdir, the agent's edits applied on top
(surface-gated), and the eval command run; **eval/protected files can't be overwritten**
by the agent (enforced by construction). Offline / on agent failure a baseline (no-op)
developer leaves the repo unmodified.

**Framework mode — tune an existing framework with no code edits** (`params_style:
cli_overrides`): declare a hyperparameter space and the Researcher's proposals become
`key=value` CLI overrides on the eval command (Hydra-style); add **eval profiles**
(cheap `smoke` during search, `full` on the confirm phase):

```jsonc
{ "kind": "repo", "direction": "max", "editable_path": "examples/repo_example",
  "protect": ["ttrain_cli.py"], "params": {"x": [-5.0, 5.0]},
  "eval": { "command": ["python", "ttrain_cli.py"], "params_style": "cli_overrides",
            "metric": {"kind": "stdout_json", "key": "metric"},
            "profiles": {"smoke": {"overrides": ["steps=10"], "timeout": 60},
                         "full":  {"overrides": ["steps=200"], "timeout": 120}} } }
```
```bash
python -m LoopLab.cli run examples/repo_framework_task.json   # offline hyperparameter search
```

**Onboarding — let the agent figure out the eval** (`"onboard": true`): you give the
framework's command; the agent **writes a metric adapter** for whatever tracker the repo
uses (TensorBoard / MLflow / ClearML / a metrics file / stdout). It proposes the eval, a
human **ratifies it once** (`LoopLab approve`), then it's **frozen + protected** and the
optimization loop trusts it. The trust policy is `eval_trust_mode`: `ratify_freeze`
(default — pause for approval), `autonomous` (no gate), or `ratify_freeze_drift`.

```bash
python -m LoopLab.cli run examples/repo_onboard_task.json --backend llm \
  --developer-backend opencode --model qwen3:8b
# run pauses with a proposed eval+adapter; review it, then:
python -m LoopLab.cli approve runs/run_local
python -m LoopLab.cli resume runs/run_local --task-file examples/repo_onboard_task.json
```

Writing the adapter code is the **Developer's** job; the **Researcher** proposes the spec
— onboarding reuses the same two roles, no bespoke agent. Still ahead: reference repos at
runtime, multiple editable repos, the untrusted Docker tier — see `plans/`.

## Docker is not required (trust-mode tiering, ADR-13)

The sandbox tier is chosen by **trust mode**, not environment:
- **`trusted_local`** (default, the CLI): `SubprocessSandbox` — process isolation +
  timeout + tree-kill + output caps. You run your own research on your own box, so the
  code is in your trust domain. **No Docker, no daemon** — the whole engine + all 34 tests
  run here.
- **`untrusted`** (hosted / web-UI / multi-tenant): `DockerSandbox` (`--network none` →
  gVisor) — a real boundary, required only when executing code on infra that must protect
  other users. Set `LOOPLAB_TRUST_MODE=untrusted`.

So **Docker becomes necessary only for the hosted/UI scenario that serves untrusted
code — never for the local CLI.**

## Deploy the full stack with Docker Compose (optional)

For the hosted scenario you can bring up **everything — the LLM, the UI, and the engine —
with one command**. This is purely a deployment convenience; the local CLI above needs none
of it. Requires Docker with the NVIDIA GPU runtime (Docker Desktop + WSL2 is fine).

The stack (`docker-compose.yml`):
- **`sglang`** — serves a 4-bit MoE on the GPU via SGLang, OpenAI-compatible at `:30000`. Default
  **`Qwen3-Coder-30B-A3B`** (works on Blackwell today). *Qwen3.6-35B-A3B* — the hybrid Mamba+MoE — currently
  hits a known SGLang `causal_conv1d` kernel bug on Blackwell ([sglang#24364](https://github.com/sgl-project/sglang/issues/24364));
  set `SGLANG_MODEL` back to it once that's fixed.
- **`ui`** — the live React UI + control-plane (`LoopLab ui`) on `:8765`, pointed at the in-network model.
- **`run`** — a one-shot engine runner (compose profile `tasks`), started on demand.

```bash
cp .env.example .env                       # model id, ports, context length, etc.
docker compose up -d sglang ui             # start the model + UI (first run downloads ~18 GB weights)
docker compose logs -f sglang              # watch the one-time model load (minutes)
# open http://localhost:8765

# run an autonomous experiment against the containerized model:
docker compose run --rm run \
  LoopLab run examples/regression_task.json --backend llm --max-nodes 14
```

LoopLab is wired to the model purely by env (`LOOPLAB_BACKEND=llm`, `LOOPLAB_LLM_BASE_URL=http://sglang:30000/v1`,
`LOOPLAB_LLM_MODEL`). Structured output uses Qwen's native tool-call parser (`--tool-call-parser qwen`);
`LOOPLAB_LLM_GUIDED_JSON` is **off** by default because SGLang's guided_json/xgrammar path produced
empty `{}` for Qwen3-Coder-30B — set it to `1` in `.env` only if a weaker model needs constrained
decoding. The model, ports, VRAM fraction, context length and SGLang flags are all tunable in `.env`.
Run artifacts land in `./runs` (shared with the host and the UI).

> **Exposure:** both ports publish to `127.0.0.1` only by default — the UI control-plane is
> unauthenticated unless `LOOPLAB_UI_TOKEN` is set, so it is not put on the LAN implicitly. To
> serve it beyond localhost, set a token and `UI_BIND=0.0.0.0` in `.env`.

## Quick start

```bash
pip install -e .                       # or: pip install pydantic pydantic-settings orjson anyio typer
python -m LoopLab.cli run examples/toy_task.json --out runs/demo --max-nodes 14
python -m LoopLab.cli inspect runs/demo          # config snapshot + best result
python -m LoopLab.cli replay  runs/demo          # pure fold of the event log -> state

# A real ML task: polynomial degree + ridge model selection via 5-fold CV.
# The loop discovers the right model complexity (true degree 2) from a profiled dataset.
python -m LoopLab.cli run examples/regression_task.json --out runs/reg --max-nodes 14
```

Open `runs/demo/tree.html` for the static lineage view.

### Crash & resume (the keystone)

```bash
python -m LoopLab.cli run examples/toy_task.json --out runs/c --max-nodes 12 --crash-after 3
#   -> hard-exits (code 137) mid-run, like kill -9
python -m LoopLab.cli resume runs/c --task-file examples/toy_task.json --max-nodes 12
#   -> replays the log, continues from the exact frontier, finishes; no duplicate/lost work
```

## Tests

```bash
python -m pytest -q        # 105 pass + 1 skipped (live-LLM auto-skips w/o Ollama; live OpenCode is opt-in)
```

- `test_events_replay.py` — **replay determinism** + torn-final-line durability + seq monotonicity (the I1 keystone risk).
- `test_sandbox_gate.py` — sandbox metric capture / failure / nonzero-exit / timeout-kill / relative-workdir; the >1-SE variance gate.
- `test_end_to_end.py` — full autonomous run optimizes the toy task; resume of a finished run is idempotent; **crash (subprocess `kill -9`) → resume → completion**.
- `test_parse_llm.py` — structured-output parse, **auto-fallback tool_call→baml**, cost accountant warn/stop, LLM role seam (mock client).
- `test_operators_policy.py` — `merge_idea`, debug-leaf policy transition, merge cadence, **end-to-end merge node with 2 parents**.
- `test_trust_knowledge.py` — leakage detectors (contamination/target/temporal), data profiler, vector store + grep/glob retrieval, cross-run case library retain-on-improvement.
- `test_security.py` — **trust-mode sandbox selection** + **secret-leak scan** (no secret value reaches any on-disk artifact).
- `test_cv_confirm.py` — K-fold partition + purged walk-forward (no look-ahead) + consistent CV; **multi-seed confirmation demotes a seed-lucky leader**.
- `test_tracing_altpolicy.py` — JSONL span exporter; **`EvolutionaryPolicy` runs end-to-end through the unchanged engine** (pluggable-algorithm seam).
- `test_confirm_integration.py` — the **orchestrator-wired confirmation phase**: on a noisy task it re-runs the top-k under multiple seeds, emits `node_confirmed` events, picks the robust best, and survives replay.
- `test_regression_task.py` — a **real ML task** (polynomial + ridge model selection via CV) runs end-to-end: the generated solution's CV prefers the true degree, the loop finds a sensible-complexity model, the grounding pre-phase profiles the dataset, and it survives replay.
- `test_code_loop.py` — **LLM-as-Developer mechanics** (offline, fakes): code extraction from fenced/`<think>` replies, dataset **assets materialized** into the sandbox workdir, and the **error-feedback debug operator** repairing a deliberately broken solution.
- `test_agentic_retrieval.py` — **agentic retrieval** (offline, fakes): `KnowledgeTools` (grep/kb_search/list/read, path-restricted) and the **tool-using Researcher** loop calling a tool then emitting a bounds-clamped Idea.
- `test_partials_wired.py` — the trust/ops/infra levers **wired into the loop**: span tracing (`spans.jsonl`), wall-clock **budget** abort, **variance-gated** confirmation promotion, **leakage-first** abort, and **cross-run memory** (persist + retain-on-improvement + recall as searchable knowledge).

## What maps to what

| Module | Plan ref | Role |
|---|---|---|
| `models.py` | I0 | domain models + event envelope (JSON-Schema source) |
| `config.py` | I0, ADR-11 | layered settings + secret-masked snapshot + trust mode |
| `eventstore.py` / `replay.py` / `readmodel.py` | I1, ADR-1/12/17 | append-only log, pure fold, rebuildable SQLite |
| `sandbox.py` | I3/I21, ADR-13 | `Sandbox` seam + `SubprocessSandbox` (trusted_local) + `DockerSandbox` seam + `make_sandbox` |
| `roles.py` | I5/I2, ADR-7/14 | `Researcher`/`Developer` Protocols + toy backends + LLM backends |
| `parse.py` / `llm.py` | I2/I13, ADR-14/11 | structured-output + auto-fallback (`<think>`-aware); `OpenAICompatibleClient` (stdlib) + LiteLLM client + cost accountant |
| `operators.py` | I7/I11, ADR-6 | `merge_idea` (ensemble/merge) |
| `orchestrator.py` | I6, ADR-12/18 | anyio control loop + crash-resume |
| `gate.py` / `confirm.py` | I10/I12, ADR-15 | >1-SE variance gate; multi-seed top-k confirmation |
| `cv.py` | I8, ADR-15 | consistent-eval harness, K-fold, purged walk-forward |
| `leakage.py` / `profile.py` | I9/I16, ADR-15 | leakage detectors + data profiler |
| `vectorstore.py` / `retrieval.py` | I17, ADR-16 | `VectorStore` seam + in-memory default + grep/glob retrieval |
| `agent.py` / `knowledge_tools.py` | I17, ADR-16 | tool-using Researcher (multi-turn) + grep/kb_search/read toolset over a notes dir |
| `memory.py` | I19, ADR-10 | cross-run case library (retain-on-improvement) |
| `tracing.py` | I14, ADR-17 | custom JSONL span exporter (diagnostics, files-as-truth) |
| `policy.py` | I6/I7/I11/I22, ADR-2/18 | `GreedyTree` + opt-in `EvolutionaryPolicy` + `make_policy` |
| `htmlview.py` | I6, ADR-1 | static-HTML lineage tree |
| `tasks.py` / `toytask.py` / `regression.py` | I6/I16, ADR-2 | `TaskAdapter` seam + loader; toy quadratic; CV model-selection (`regression`) + LLM-writes-code (`code_regression`) |

## Remaining seams (infra-bound — stub behind the Protocol, swap = no loop change)

These genuinely need external infra/services, so they ship as seams, not running code:

- **DockerSandbox body** (docker-py, `--network none`, gVisor) — needs a Docker daemon;
  only for the `untrusted` trust tier. → I21
- **LanceDB/FastMCP** backends behind the `VectorStore`/retrieval seams (in-memory
  default works offline). → I17
- **MLEBench/Kaggle adapters**, **OTel/MLflow export**, **Textual TUI / React web UI**. → I14/I15/I20/I22
