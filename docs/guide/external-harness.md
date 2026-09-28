# External agent harness

LoopLab has two ways to work with Codex, Claude Code and other coding agents:

| Mode | Who chooses the next experiment? | Who edits code? | Who evaluates and records the score? |
|---|---|---|---|
| External harness (`external_harness=true`) | External agent through MCP/control API | External agent submits a ready-made candidate | LoopLab |
| Delegated Developer (`developer_backend=codex` or `claude`) | LoopLab's existing Researcher and search loop | Installed coding CLI in a gated candidate workspace | LoopLab |

The external mode leaves proposal, stage design, planning, implementation, repair and
search decisions to the connected agent. It also leaves novelty judgments, lesson
distillation and taxonomy curation to that agent. It can skip a stage or plan when unnecessary.
LoopLab still owns admission, lineage, protected files, execution, measured results,
budgets, pause/finalize, event history and replay. The agent must stay connected (or
reconnect later) to choose further work: an idle external run waits for commands.

## External harness setup

Install the optional UI and MCP packages. Start the UI against the run root you want
to control; set your own owner token so the MCP process can authenticate.

```sh
pip install 'looplab[ui,harness]'
export LOOPLAB_UI_TOKEN='choose-a-private-token'
looplab ui --run-root runs --host 127.0.0.1 --port 8765
```

Launch a run with LoopLab's offline roles; they are never asked to propose or repair
in external mode. The task's declared eval command still runs in LoopLab's sandbox.
Choose the mode at launch; per-run configuration edits cannot switch an existing
run between internal search and external control.

```sh
looplab run task.json --out runs/my-run --backend toy -s external_harness=true
```

Keep that run process open while the coding agent sends commands from another terminal.

Configure your coding agent's MCP client to launch `looplab harness-mcp` over stdio,
with `LOOPLAB_UI_TOKEN` and, if the server is elsewhere,
`LOOPLAB_HARNESS_URL=http://127.0.0.1:8765`. The process offers eight tools:
`capabilities`, `phases`, `phase_info`, `settings_keys`, `setting_info`, `operations`,
`operation_schema` and `api_request`. The latter
forwards to the same authenticated HTTP API as the UI. It never writes directly to
the event log. Use the live `operations` catalog to discover read, settings,
task, evidence, artifact and control routes, and `operation_schema` for a route's
OpenAPI definition. `settings_keys` and `setting_info` expose all Settings, including
advanced fields omitted from the UI form. `looplab harness --settings` returns the full Settings schema
and curated field help without a server.

`phases` is the workflow index for both modes. Each entry names the entity, the
built-in owner, the evidence to read, the external actions that write the same
domain state, and any PromptStore keys. `phase_info("research")`, for example,
includes the `ResearchMemo` schema and the accepted/server-derived fields of
`command:research_completed`. A `command:TYPE` action
means a durable `POST /api/runs/{run_id}/commands` with `type: TYPE`, `data`,
`expected_generation` from `/state`, and a fresh `Idempotency-Key`. Search
`operations` for each HTTP path and read `operation_schema` before submitting.
The catalog covers Genesis, onboarding, research, hypotheses and Cards, proposal,
novelty, ranking, strategy, stages, implementation, repair, live monitoring,
evaluation, concepts, claims, lessons, reports, and the pilot's next action.

For Codex, add this to your project `.codex/config.toml` (or the user config):

```toml
[mcp_servers.looplab]
command = "looplab"
args = ["harness-mcp"]
env_vars = ["LOOPLAB_UI_TOKEN", "LOOPLAB_HARNESS_URL"]
```

For Claude Code, a project `.mcp.json` can pass those environment variables
without putting the token value in the file:

```json
{
  "mcpServers": {
    "looplab": {
      "type": "stdio",
      "command": "looplab",
      "args": ["harness-mcp"],
      "env": {
        "LOOPLAB_UI_TOKEN": "${LOOPLAB_UI_TOKEN}",
        "LOOPLAB_HARNESS_URL": "${LOOPLAB_HARNESS_URL:-http://127.0.0.1:8765}"
      }
    }
  }
}
```

The normal control cycle is:

1. Read `/api/runs/{run_id}/state`, task, config and relevant events/artifacts.
   Search `operations` for the exact read routes. Review the goal, evaluation
   command, allowed edit paths, parent generations, budget and prior results.
2. Decide whether a proposal, stage split and plan help. The operator's declared
   `cmd.stages` wins. For a single command, an agent may put preceding stages in
   `looplab_stages.json` only if `edit_surface` allows that JSON path; LoopLab appends
   the protected score command.
3. Submit `inject_node` using `POST /api/runs/{run_id}/commands`: include an `idea`
   and ready-made `code` or `files` (and optionally `deleted`). Use `parent_id` or
   `parent_ids` to branch from measured candidates. Read the command's schema for
   its exact envelope, including `expected_generation`, and send a fresh
   `Idempotency-Key`; reuse the key only when retrying the identical request.
4. Poll the returned command record and run state. Inspect measured metrics,
   stage logs and failures. Submit another ready-made candidate if a repair or a
   different idea is useful. The metric is measured by LoopLab's evaluator; never
   submit a claimed score as evidence.
5. Publish a research memo, hypothesis, lesson or report when the evidence warrants it.
   These are separate durable decisions; no particular number of stages is required.
6. Pause or finalize the run through the same command API. Resume from the durable
   state after a client restart. A run does not finish merely because the external
   agent has no immediate action.

## Novelty and cross-run knowledge

A deep-research memo can be authored with `type: research_completed` and
`data: {"memo": {"summary": "...", "findings": [...], "open_questions": [...],
"next_experiments": [...]}}`. It enters the regular `RunState.research` projection,
so subsequent readers see it as they see a built-in research memo. The server
stamps the node count and external trigger, sanitizes the memo, computes its
identity, and runs deterministic verification over its cited claims. An
external agent cannot claim a model-verifier verdict or mark a manual internal
research request served. Use `hypothesis_added` and `hypothesis_updated` commands
for standalone board decisions, and the Card controls for prioritization.

An authored run report uses `type: report_generated` and
`data: {"content": {"headline": "...", "verdict": "..."}}`; the same
projection and publication sequence as an internal report serve it. These
commands are valid in normal runs too, so a human or external collaborator can
add evidence while built-in roles continue their work.

The agent can call `POST /api/runs/{run_id}/novelty-preview` with
`expected_generation` and an `idea` before submitting a candidate. It returns
LoopLab's deterministic graded novelty result and nearby node, without admitting
the idea or calling an internal model. Concept tags in this preview are authored
claims, not independent classifier evidence. The agent makes the final novelty
decision after reviewing node history and relevant cross-run claims.

Read accumulated lessons through `GET /api/memory` (optionally `?run_id=...`)
and claims through `GET /api/cross-run/claims`. When a result teaches something
generalizable, call `POST /api/runs/{run_id}/lessons` with the current
`expected_generation`, a stable `action_id`, `statement`, `outcome`
(`supported`, `tested`, `abandoned`, `failed`, `refuted` or `noted`), optional
`role` (`researcher`, `developer` or `shared`) and `evidence` (terminal node IDs).
LoopLab validates the cited nodes and records their current outcome signatures,
task fingerprint, direction and run identity in the same `lessons.jsonl` store
its cross-run readers use. Repeating an identical `action_id` returns the stored
lesson; reusing it for different content fails. An agent can publish before or
after finalizing the run. In external mode, finalization does not ask LoopLab's
internal reflector to create additional lessons or auto-promote skills.

Concept authoring and deduplication use the existing durable controls. Supply
`idea.concepts`, or `concept_mode` with `concepts_added` and `concepts_removed`,
in an `inject_node` command. `concept_tag_edited` updates an observed node's tags;
`run_concepts` sets the run base. Inspect `/api/runs/{run_id}/concepts`,
`/api/cross-run/atlas` and `/api/cross-run/concept-policy` before a cross-run
decision. The governed `concept-merge`, `concept-alias-clear`, `concept-split`,
`concept-split-clear` and `concept-purge` routes record reversible, revision-fenced
taxonomy changes. `claim-decide` handles claim governance. The engine still
produces deterministic case and concept-capsule projections from measured,
authored evidence at run end; it does not call its internal concept or claim
stewards or automatically ratify concept merges in external mode. Use
`operations` and `operation_schema` to get each route's live request shape.

For cross-run task facets, read `GET /api/cross-run/task-facets` for the current
portfolio identity and ledger revision, then send `POST` to the same path with
`task_id`, a non-empty `facets` object, `expected_portfolio_id`,
`expected_revision` and a stable `action_id`. The writer uses the same strict
append-only ledger as `looplab task-facets-set`. An identical retry returns the
first record even after its revision advanced; a stale or conflicting write is
refused.

The API rejects `fork`, `force_ablate`, `deep_research`, code-less `inject_node`
and `node_reset` from `propose` or `implement` in external mode: those commands
would otherwise start internal agent work. Submit a ready-made child candidate
instead. `node_reset` from `eval` can remeasure unchanged code. In external mode,
failed evaluations become terminal evidence for the external agent; inline repair,
training-log judges, ASHA judges and inter-stage model checks do not run. The
operator's declared artifact checks and score reader continue to apply.

## Delegating only code editing

For a normal autonomous LoopLab run, install and sign in to the coding CLI on
the LoopLab machine:

```sh
looplab run task.json --out runs/try --backend llm --developer-backend codex
# or: --developer-backend claude
```

Codex uses `codex exec` with its `workspace-write` sandbox. Claude Code uses
print mode with `acceptEdits`. Their CLI account and model settings apply;
LoopLab does not give them its LLM key. The candidate is edited in an isolated
git workspace, checked against `edit_surface`, validated where configured,
then executed by LoopLab. A failed validation may fall back to the task's
in-process Developer. The Researcher and other decision roles remain LoopLab's
in this mode. `param_search` tasks do not use the external editing Developer.

## Scope and provenance

The MCP adapter forwards JSON API requests and limits a response to 256 KiB and
a request body to 1 MiB. Query narrow routes for larger outputs; binary uploads
need their dedicated API or CLI flow. All MCP clients holding the owner token
have the same authority as the UI owner. Agent reasoning and model token cost
happen outside LoopLab's ledger; the event log records the submitted candidate,
measured execution and command receipts, not external provider billing.

HyperResearch's discoverable workflow and OpenResearch's experiment workspace
informed the interface. LoopLab uses its own event log, candidate snapshots and
metric provenance as the durable center.
