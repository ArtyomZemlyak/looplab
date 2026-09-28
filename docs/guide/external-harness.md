# External agent harness

LoopLab has two ways to work with Codex, Claude Code and other coding agents:

| Mode | Who chooses the next experiment? | Who edits code? | Who evaluates and records the score? |
|---|---|---|---|
| External harness (`external_harness=true`) | External agent through MCP/control API | External agent submits a ready-made candidate | LoopLab |
| Delegated Developer (`developer_backend=codex` or `claude`) | LoopLab's existing Researcher and search loop | Installed coding CLI in a gated candidate workspace | LoopLab |

The external mode leaves proposal, stage design, planning, implementation, repair and
search decisions to the connected agent. It also leaves novelty judgments, lesson
distillation and taxonomy curation to that agent. Enabled operator settings remain
requirements for the external agent; it can skip an optional stage or plan when unnecessary.
LoopLab still owns admission, lineage, protected files, execution, measured results,
budgets, pause/finalize, event history and replay. The agent must stay connected (or
reconnect later) to choose further work: an idle external run waits for commands.

## External harness setup

Install the optional UI and MCP packages. Start the UI against the run root you want
to control. Set a distinct harness token for the coding agent; keep the owner token
in the UI server/operator session.

```sh
pip install 'looplab[ui,harness]'
export LOOPLAB_UI_TOKEN='choose-a-private-token'
export LOOPLAB_HARNESS_TOKEN='choose-a-different-agent-token'
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
with **only** `LOOPLAB_HARNESS_TOKEN` and, if the server is elsewhere,
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
domain state, and any PromptStore keys. `write_access` distinguishes operator
setup actions (global settings and launch) from scoped agent actions.
The scoped token also refuses old owner routes that invoke LoopLab's model
(legacy chat/suggestion/report refresh, taxonomy stewards, provider probe and
scope-report generation). The agent writes run reports with `report_generated`
and knowledge decisions through the guarded domain APIs.
`phase_info("research")`, for example,
includes the `ResearchMemo` schema and the accepted/server-derived fields of
`command:research_completed`. A `command:TYPE` action
means a durable `POST /api/runs/{run_id}/commands` with `type: TYPE`, `data`,
`expected_generation` from `/state`, and a fresh `Idempotency-Key`. Search
`operations` for each HTTP path and read `operation_schema` before submitting.
The catalog lists capabilities. Read `GET /api/runs/{run_id}/harness-contract`
for the effective obligations of this run; an enabled feature is not silently
turned into an optional suggestion. The catalog covers Genesis, onboarding, research, hypotheses and Cards, proposal,
novelty, ranking, strategy, stages, implementation, repair, live monitoring,
evaluation, concepts, claims, lessons, reports, and the pilot's next action.

Read `GET /api/runs/{run_id}/harness-progress?expected_generation=TOKEN` for
the current expansion and finish gates, required concept/hypothesis fields,
pending evaluation questions and paginated histories of decision, knowledge
review and checkpoint receipts. Its `policy_preview` computes suggested actions
and parent IDs from a recorded strategy, or from the config snapshot before a
strategy decision is recorded. It names that source; a config edit takes effect
on engine restart, and admission may apply further gates. This is advice:
the external agent implements or rejects the suggestion, and no internal
Researcher is invoked. The token comes from `/state`. Its `event_seq`
identifies the measured event prefix; refresh after any event or response. The
three histories live in independent JSONL journals; the event log is a fourth
source. `source_health` reports damaged rows in all four, and `complete=false`
means an absent receipt cannot be taken as
proof that no action occurred. `file_present=false` is normal for a fresh run;
this view cannot detect deletion of an entire journal that had already been
written. Older receipts stay visible as `superseded` when
node count or measured evidence changes. A decision marked
`current_evidence_for_idea` remains valid only for its original Idea (and for
candidate ranking, its exact implementation); this view cannot preapprove a
different candidate. The event timeline is the separate source for commands,
node lifecycle, research, concepts, reports, selection and measured outcomes.
Unsubmitted private agent planning cannot be reconstructed; publish material
choices through the appropriate domain action. In the UI, open **Progress →
Agent cycle** to inspect this view and its source health.

For Codex, add this to your project `.codex/config.toml` (or the user config):

```toml
[mcp_servers.looplab]
command = "looplab"
args = ["harness-mcp"]
env_vars = ["LOOPLAB_HARNESS_TOKEN", "LOOPLAB_HARNESS_URL"]
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
        "LOOPLAB_HARNESS_TOKEN": "${LOOPLAB_HARNESS_TOKEN}",
        "LOOPLAB_HARNESS_URL": "${LOOPLAB_HARNESS_URL:-http://127.0.0.1:8765}"
      }
    }
  }
}
```

The normal control cycle is:

1. Read `/api/runs/{run_id}/state`, task, config, `harness-contract`,
   `harness-progress` and relevant events/artifacts.
   Search `operations` for the exact read routes. Review the goal, evaluation
   command, allowed edit paths, parent generations, budget and prior results.
2. Decide whether a proposal, stage split and plan help. The operator's declared
   `cmd.stages` wins. For a single command, an agent may put preceding stages in
   `looplab_stages.json` only if `edit_surface` allows that JSON path; LoopLab appends
   the protected score command.
   Enabled `deep_research_every` requires a current `research_completed` memo;
   enabled `track_hypotheses` requires a nonempty candidate hypothesis statement;
   injection creates a fresh Card and does not attach a supplied `card_id`.
   When four or more pure belief Cards are open, review the current board with
   `GET/POST /api/runs/{run_id}/harness-hypotheses`: merge genuine aliases or
   record `no_merge` with a reason. Echo the GET response's `board_sha256` as
   `expected_board_sha256`; a changed board or measured outcome rejects a stale
   review. Admission requires a fresh board review.
   With `concept_run_base`, publish `run_concepts` after the first scored node
   with authored tags and before the next candidate.
   Enabled coverage snapshots remain live: LoopLab computes breadth and
   concept lock-in at their configured cadences from measured outcomes and
   the agent's recorded concept tags, without an internal classifier.
   If `select_verifier` exposes a live selector tie, GET
   `/api/runs/{run_id}/harness-selection` and POST a complete group to
   `/harness-selection/verify`. Judge the realized result for each member
   `select_verifier_samples` times and send boolean `samples` with the returned
   node generation and evidence digest. LoopLab derives scores and refuses a
   partial or stale group. If the active MCTS policy has positive
   `mcts_value_weight`, use that GET's `value_candidates` and
   `evidence_revision` to POST one headroom estimate (0–1 and rationale) per
   candidate to `/harness-selection/values`. Both judgments are required before
   the next candidate when their respective conditions arise. The external agent
   can switch the live policy with `set_strategy` (including `greedy`, `mcts`,
   `asha` and `bohb`); MCTS value judgments are due only while MCTS is active
   with a positive value weight. A reset or new
   measurement requires a fresh judgment. These are the external agent's
   assessments, not independent model verification.
   With `reflection_priors` and `lessons_every` enabled, review skill candidates
   at each configured node interval; review lessons there when
   `comparative_lessons` is also enabled. Both reviews are due at finalization.
   Publish evidence-linked lessons and skill candidates if
   warranted, then POST `harness-reviews` for each due phase with its recorded
   action reference, or `no_applicable_action` and a reason. A changed measured
   outcome invalidates the review of the current window.
   Record enabled novelty, foresight, ranking and strategy choices through
   `POST /api/runs/{run_id}/harness-decisions` before admitting that Idea.
3. Submit `inject_node` using `POST /api/runs/{run_id}/commands`: include an `idea`
   and ready-made `code` or `files` (and optionally `deleted`). Use `parent_id` or
   `parent_ids` to branch from measured candidates. Read the command's schema for
   its exact envelope, including `expected_generation`, and send a fresh
   `Idempotency-Key`; reuse the key only when retrying the identical request.
4. Poll the returned command record and run state. While a command eval runs,
   poll `GET /api/runs/{run_id}/harness-checkpoints?expected_generation=TOKEN`.
   Answer each pending question with `POST` to the same path, including the
   checkpoint ID, generation, unique `action_id`, verdict and reason. An operator
   stage with `check: true` or `expect.assert` waits for `proceed`, `inconclusive`
   or `fail` (with a named physical failure kind); LoopLab still applies its
   measured trajectory and declared-condition vetoes. When train monitoring or
   ASHA is enabled and live evidence exists, answer `continue` or `watch`;
   a follow-up question may grant `abort` after a prior `watch` and the engine's
   attribution/comparability checks. Disabled kill settings grant no `abort`.
   An opened observation holds the node terminal until answered. Inspect measured
   metrics, stage logs and failures, then submit a corrected candidate if useful.
   The metric is measured by LoopLab's evaluator; never submit a claimed score.
   If a command stage reaches its deadline and `eval_deadline_grace_s` is enabled,
   the agent receives a `deadline_grace` checkpoint. Answer `extend` or `stop`;
   the runtime limits an extension to the operator's configured allowance and
   records the granted seconds with the stage. The external run does not invoke
   LoopLab's internal deadline judge, and a missing answer never grants time.
   The train observer checks at the configured adaptive cadence during a command
   evaluation. If an evaluation finishes before its first tick, LoopLab checks
   its final attributed training log before committing the node result and waits
   for the external agent's answer. ASHA only asks when comparable intermediate
   measurements and enough completed siblings exist. Evaluators without an
   attributed training log cannot produce a training-monitor question.
5. Review each enabled cross-run knowledge phase and record its action reference,
   or why no action applies, through `POST /api/runs/{run_id}/harness-reviews`.
   Publish `report_generated` at each enabled `report_every` node interval
   before the next candidate, and cover the latest candidate before finishing.
   No particular number of implementation stages is required.
6. Pause or finalize the run through the same command API. Resume from the durable
   state after a client restart. A run does not finish merely because the external
   agent has no immediate action.

The obligation settings of an external run are fixed at launch (including concept,
novelty, monitoring, review and report switches). Per-run config rejects changes to these
fields, including changes made through the MCP bridge's harness token. The
harness token cannot change global settings, launch through Genesis or `/api/start`,
drive the owner assistant, or reset/delete a run. Start a
new run to change those obligations. The search `policy` is a tactical choice:
`set_strategy` applies immediately to the live engine and records a durable
`strategy_decision`; editing the run's `policy` config takes effect on the next
restart if no durable `set_strategy` pin overrides it. In external mode LoopLab
uses the active policy only to schedule
evaluations of agent-submitted nodes; the external agent still chooses and
submits every new candidate. Selecting `greedy` does not generate an internal
`improve` step: the agent can follow that heuristic by choosing the best
measured parent itself, or choose a different experiment. Operational tuning
fields such as `timeout` remain editable and take effect in the engine on its
next restart. Budget or
leakage stops with outstanding external finish obligations pause the run; publish
the due report and reviews, then explicitly finalize it. CLI `finalize` and HTTP
`run_abort` apply the same preflight, including pending evaluations.

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
When novelty is enabled, the agent records that choice against the exact Idea,
run generation and node count through `harness-decisions`. The same route
records configured foresight and best-of-N reviews. It takes a list of
different alternative Ideas for foresight. Best of N takes complete
`implementations` with code/files, plus `selected_index`; the selected Idea and
artifact must match the subsequently admitted candidate. The server counts
distinct reviewed options and refuses a review narrower than the configured panel. A strategy review is due
on its configured cadence. Candidate admission rejects a missing review.

These receipts enforce that the agent performed and recorded a decision; they do
not recreate every internal model judgment. `novelty-preview` is advice: the agent
can still submit a near duplicate after recording a novelty decision. Foresight
requires the configured panel size, but `foresight_min_confidence`,
`foresight_verify`, verifier sample count and `foresight_agentic` do not run an
independent check of the agent's choice. Best of N requires distinct full
implementations and an exact selected artifact, but the built-in static floor,
confidence abstention and listwise tie break are delegated to the agent. The
live `harness-contract.delegated_semantics` field states these limits alongside
the enforceable gates.

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

Concept authoring and deduplication use the existing durable controls. If
`concept_pivot`, `concept_run_base`, or `cross_run_concepts` is enabled, an
external `inject_node` must carry nonempty effective concepts. The admission
gate checks full tags or materialized base/parent plus delta and rejects an
empty result. When all three are off, candidate concept tags are optional. Supply
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

An external agent may draft a reusable skill from a supported lesson with
`POST /api/runs/{run_id}/skill-candidates`. Supply its current generation,
unique action ID, published `lesson_action_id` and a procedural Markdown body.
The server checks that the lesson still cites measured, reliable node outcomes,
then applies the existing portability prefilter and cross-task fingerprint
promotion rule. The agent cannot set `status: promoted` itself.

Enabled `cross_run_curation`, `task_facets_finalize`, `concept_tidy` and
`reflection_priors` require a final review of the applicable concept, claim,
facet, lesson and skill decisions when the run contains candidates. Use
`no_applicable_action` with a reason when evidence does not justify a write;
`completed` includes the domain action reference and current run node evidence. These reviews attest the
agent's judgment; LoopLab verifies the referenced domain action exists, but a
shared concept/claim ledger reference alone does not prove it was created for
this run. The
actual write must still pass its own guarded API.
Finalization rejects missing reviews or reviews from an earlier node count.

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
need their dedicated API or CLI flow. A dedicated harness token can read and
control launched runs but cannot change operator defaults, launch new runs,
drive the owner assistant, or reset/delete runs. Legacy configurations passing `LOOPLAB_UI_TOKEN` still give
the agent full UI owner authority; keep that owner credential out of its environment
when operator policy must remain separate. Agent reasoning and model token cost
happen outside LoopLab's ledger; the event log records the submitted candidate,
measured execution and command receipts, not external provider billing.

HyperResearch's discoverable workflow and OpenResearch's experiment workspace
informed the interface. LoopLab uses its own event log, candidate snapshots and
metric provenance as the durable center.
