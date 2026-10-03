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

## First external run

Use a **separate run root and UI server for external runs**. A harness token can control
the launched runs on its server; it is not a token limited to a single run. The
operator starts the server and run, while the coding agent proposes candidates.

1. From a source checkout, install `pip install -e ".[ui,harness]"`. Start the UI with
   an owner credential and a distinct `LOOPLAB_HARNESS_TOKEN`, using the commands in
   [External harness setup](#external-harness-setup). Keep the owner credential out
   of the coding agent's environment. Open the UI to watch the same run root.
2. Start a small task in a second terminal with
   `looplab run task.json --out runs/my-run --backend toy -s external_harness=true`.
   Keep this process running. The external agent, not LoopLab's built-in Researcher,
   chooses the next candidate; the task's evaluator still runs inside LoopLab.
3. In **Progress → Agent cycle → Connect external agent**, inspect the server/root
   and copy the instruction for this run. Configure the coding client's stdio MCP
   server as `looplab harness-mcp`, passing only `LOOPLAB_HARNESS_TOKEN` and the UI
   URL. Supply the credential separately; client-specific examples are below.
   If the MCP client runs on another machine, install `pip install -e ".[harness]"`
   there from a matching LoopLab checkout; the client does not need `[ui]` or FastAPI.
   In Claude, review the project MCP approval prompt; **Pending approval** means
   the process has not connected. In Codex, use a trusted project and inspect `/mcp`.
   **Connected** only proves the stdio process, even when the UI/API is unreachable.
   Ask the agent to call MCP `connection_check` for this run before continuing.
   Without the UI handoff, give the agent this instruction, replacing the run ID:

   > Start with `looplab harness` and MCP capabilities. Call `connection_check`
   > for `my-run` to check live API reads, not only stdio. Read the current
   > `/state?observe_only=true`, task, config, harness-contract and harness-progress.
   > Use `phases` and `phase_info` for the next required decision. Submit a
   > ready-made candidate through a durable command, inspect its measured
   > result and checkpoints, then decide what to do next. Explicitly pause or
   > finalize when done. Do not claim a score before LoopLab evaluates it.

4. Watch the run in the UI. Before its first candidate, **Progress → Agent cycle**
   shows what the external agent owes. A live server or engine does not prove
   that the agent is connected. If the agent stops, reconnect it to the **same**
   run, read current generation and receipts, and continue; do not blindly
   resubmit the last candidate. See [Reconnect and recovery](#reconnect-and-recovery).

For a quick offline evaluator, `examples/toy_task.json` can replace `task.json`.
The coding agent's own model calls can still cost money; they are outside
LoopLab's provider-cost ledger. The scoped token cannot launch a run or change
global settings. The full `harness` contract and the live progress endpoint
remain authoritative if enabled obligations require more steps than this sketch.

### Brief results in Assistant chat

The Assistant transcript for the attached run shows a free completion brief
after each terminal evaluation (including failure/abort), and after the run has
finished finalization and released its engine. It uses recorded metrics, names
confirmation and constraints, and links to the exact node attempt or Report.
A trainer exit or an unanswered monitor checkpoint is not completion. The composer
language selector (Auto / English / Русский) controls new Assistant replies and
these briefs; it does not translate existing model/agent prose. The browser choice
does not change the external agent's contract: publish interpretations in the
user's language.
Briefs separate comparison, reliability and next steps. Evaluation scores and
confirmation means are distinct; unknown/different comparison conditions explain
why improvement is not established. Stopped attempts link to Trace rather than
Metrics. Recovered metrics are explicitly labelled and caveats are explained.

External agents should add a short interpretation after each completion:

1. Call MCP `result_notices(run_id, expected_generation)` with the current run
   generation from `/state`. Check `status`, `code` and `outcome` before using `body`.
   This reads `GET /api/runs/{run_id}/result-notices?expected_generation=TOKEN`;
   direct HTTP/MCP `api_request` is also available.
   The typed tool checks the version-1 envelope (`total`, `items`, `has_more`,
   `next_cursor`), the requested page size, distinct terminal receipt identities,
   evidence tokens and numeric score/confirmation fields. Boolean/string/nonfinite
   scores and failed/aborted receipts claiming a score are refused. An incomplete
   HTTP 200 returns `response_incomplete`, `outcome=unavailable`,
   `reason=invalid_result_page`, without its body. This is not an empty result list.
   Read again explicitly after checking the server; the tool does not repair,
   retry or follow pages. Extra fields are retained for compatible server additions.
   Each node also carries required `score_comparison` v1: `parent_count` is the
   declared lineage size, even when the bounded `parents` list omits unavailable
   or reset parents. `status=same` permits comparing primary evaluation scores
   only for one recorded, current parent attempt, eligible measurements on both
   sides, matching evaluation conditions and matching recorded code bases when
   a base is stamped or upstream is enabled. It does not establish statistical
   significance. Other statuses explain the boundary: `different`/`unknown`
   evaluation conditions, `base_different`/`base_unknown`, `ineligible`,
   `retargeted`, `no_parent`, `multiple_parents` or `parent_unavailable`.
   Retargeted metrics are displayed but do not authorize an improvement claim.
   Missing or inconsistent comparison metadata makes a typed read unavailable;
   update an older server and read again explicitly. Parent eligibility/base
   changes invalidate the evidence token and withdraw attached commentary.
   Required booleans distinguish Trust meanings: `trust_flagged` means exclusion
   by the run's Trust policy; `trust_advisory` means this attempt has a named
   warning without that exclusion. `parent_trust_advisory` warns about an attempt
   in the returned comparison parents. Advisory signals, including soft signals
   under `gate`/`block`, do not change scores or selection eligibility. Explain
   the warning separately from numeric improvement; repeated seeds do not clear
   it. These fields are not detector-coverage proofs. Missing, nonboolean or
   contradictory fields make the typed page unavailable. New signal evidence
   on either side changes the token and withdraws current commentary even when
   the score and warning booleans are unchanged; exact publication retries still
   acknowledge the original receipt. An old-attempt signal does not warn about
   the replacement attempt.
   A finalized `run` receipt also requires `trust_advisory`: it describes the
   selected attempt, including soft signals, rather than every warning in the
   run. It is false when no result is selected. The selected identity, attempt,
   score and caveats must be consistent; incomplete run receipts are unavailable
   through typed MCP/UI reads. `caveats=trust_flagged` retains its existing
   high-precision meaning and requires the selected advisory warning; soft
   warnings do not become hard caveats or change the winner. The cached
   `/api/runs` summary carries the same boolean on `result_summary.selected`
   for the Assistant result card. Older summary rows retain the existing
   unknown-coverage notice; absence of metadata does not prove clean results.
2. Copy the item's `id` and `evidence_token`. Submit through MCP `api_request`:

   ```json
   {
     "expected_generation": "TOKEN",
     "action_id": "stable-unique-summary-id",
     "receipt_id": "node:0:0",
     "evidence_token": "TOKEN_FROM_RECEIPT",
     "summary": "What changed, what the evidence supports, caveats, and the next decision."
   }
   ```

   POST to `/api/runs/{run_id}/result-notices`; for a finalized run use its `run`
   receipt. Write in the user's language, at most 700 characters. Scores come
   from LoopLab; metric, role and action fields are rejected. An interpretation
   may be uncertain and is visibly separate from the recorded measurement.
3. Retry a lost response with the **same action_id and exact body**. One comment
   is allowed per receipt version. Reconnect/reload restores the same briefs;
   changing the node attempt, measurement, provenance or comparison evidence
   withdraws old commentary. This is a current evidence view, not a copied
   Assistant-session message or a historical/public transcript.

This path accepts the scoped harness token on external runs without giving it
owner chat-log/model-workflow access. It stores bounded commentary beside the
run, never executes actions, and does not fulfill reports or checkpoints. It
adds **no admission/finalization gate or hidden engine wait**. If the agent
disconnects, automatic measured briefs remain readable. The chat initially
shows the latest three briefs; earlier loaded results can be expanded. **Earlier**,
**Newer** and **Latest results** navigate current receipts and interpretations in
50-item pages, including runs with more than 200 receipts. Older pages refresh
their evidence; an invalidated cursor requires returning to the latest results.
Generation changes reset navigation. API/MCP reads allow up to 200 items per page.
Events holds the domain event history, not the separate commentary sidecar.
Incomplete event/commentary sources produce an explicit error.

#### Recover earlier results after reconnect

Start with the latest page. Its `items` are chronological within the page;
`next_cursor` reads the next **older** page with the same `expected_generation`
and chosen `limit`. Pass the cursor unchanged until `next_cursor` is null
(`has_more=false`). Even more than 200 receipts can be drained in bounded pages.
MCP `result_notices(run_id, expected_generation, limit=50, cursor=null)` builds this
query and verifies the returned generation. Pass the previous `body.next_cursor`
as `cursor`, using the same generation; omit cursor for the latest page. Each call
does one GET and never follows another page or retries automatically. HTTP errors,
including cursor refresh advice, pass through. A malformed, over-cap or different
generation HTTP-200 response is unavailable evidence with no usable body, not an
empty result list. The tool checks the generation envelope, not the complete
receipt domain schema; server evidence tokens and POST validation still apply.
If an MCP reply exceeds its byte cap, start again with a smaller page limit.
For each current receipt with `commentary=null`, publish an evidence-bound summary
with a stable action ID for that evidence version. Keep the exact body for retries;
existing commentary does not need to be copied or submitted again.

The cursor binds this server run directory, generation, anchor receipt and evidence
token. New completions and commentary do not shift older pages; restart UI preserves
the cursor at the same path. A reset, changed anchor confirmation/provenance or
different run returns HTTP 409 `result_notice_cursor_changed` with refresh advice.
Malformed cursors return HTTP 400 `result_notice_cursor_invalid`; they never silently
select the latest page. Read current state/generation and start from the latest page
after a cursor refusal. These are current-evidence pages, not a frozen snapshot:
older receipts may change, and POST rechecks every evidence token. After draining,
refresh the latest page for completions that arrived meanwhile. Paging starts no
work and creates no new admission/finalization requirement. The browser still loads
its bounded latest-results view; this API recovery procedure is for the agent.

### Connect from the UI

For an already launched external run, open **Progress → Agent cycle → Connect
external agent**. Opening this block performs an authenticated read of
`GET /api/runs/{run_id}/harness-handoff?expected_generation=TOKEN`. The server
checks the run generation, external mode, event history and saved task/config.
The block displays its actual run root and directory, a last-read engine probe,
and a copyable MCP configuration. Choose **Codex**, **Claude Code**, or
**Other MCP client**. Codex uses TOML with `env_vars`; Claude uses project
`.mcp.json` with environment expansion. The other-client descriptor is:

```json
{
  "command": "looplab",
  "args": ["harness-mcp"],
  "env": {"LOOPLAB_HARNESS_URL": "http://127.0.0.1:8765"}
}
```

Use **Copy MCP configuration**, or select the displayed text when clipboard access
is unavailable. Place it in your client's configuration file. LoopLab
must be installed on that client's machine; if its executable is not on PATH,
use its installed executable path. The UI uses its current HTTP(S) origin and
proxy prefix, removing URL credentials, query and fragment. Check that this URL
is reachable from the client: `127.0.0.1` on a remote client is that client's host.
The run and source paths belong to the server host and may not exist locally.

Supply a distinct `LOOPLAB_HARNESS_TOKEN` through the client's protected credential
mechanism. Remove `LOOPLAB_UI_TOKEN` from the MCP process environment, including
inherited variables. The descriptor and copied instruction contain no credential.
Restart the coding client after setting its environment. `harness-mcp` refuses
to start without a nonempty scoped credential or when it matches the inherited
owner token; it never falls back to `LOOPLAB_UI_TOKEN`. An unexpanded token
placeholder also fails locally. Claude's empty default makes an unset variable
fail at startup instead of sending placeholder text to the API.
The token must contain printable ASCII without control characters; invalid header
values fail before transport with a fixed message that omits the secret.
The block reports only whether a scoped credential was configured when this
server started; it cannot tell whether your client has the right secret or is
connected. If none is configured, the operator must configure one and restart the
server before connecting. Its scope covers the server's launched external runs
and limited controls on internal runs, rather than only the displayed run.

**Copy agent instruction** prepares the run identity, generation, workspace edit
constraints and recovery reads. Constraint lists are bounded to 40 entries with
explicit totals; the agent must still read the full task, config and contract.
Known secrets in paths/names are redacted, so those paths need not be usable as
filesystem commands. If clipboard access fails, select the instruction in
**Preview instruction and workspace permissions**. A failed refresh or a newer
observed event withdraws the copy action until fresh context arrives.

Copying does not launch/resume a run or change a client configuration. After
connecting, read `/state?observe_only=true` and compare generation/run UID before acting;
reconnect to the same run using current command receipts and checkpoints.
Engine liveness does not measure agent liveness. The handoff has been exercised
with Python MCP SDK 2.2.0 and Codex 0.159.2's real MCP client: discovery, live reads,
ready-made candidates, measured evaluations, receipts, retries and result commentary.
Claude Code 2.1.286's project configuration was checked for pending approval,
approved stdio connection and missing-token failure. Its real MCP client also completed
candidate evaluation, reconnect/retry and result commentary with a local scripted
Messages provider. The provider selected tool calls; LoopLab measured the scores.
Paid model judgment and interactive tool approvals remain untested. See doc 71
sections 31–33 for the tested boundary.

### Check the live connection

MCP `connection_check` is an explicit read. It takes the literal `run_id` and an
optional `expected_generation` from the copied handoff. It reads `/state?observe_only=true`, then
generation-fenced handoff and compact progress. If the supplied generation differs,
it stops before reading the replacement run's context. Without it, the tool discovers
the current generation; use that only when you intend to inspect the current run.
Generation is a hexadecimal digest: `connection_check` accepts either hex case and
uses its canonical lowercase spelling for subsequent fenced reads. This does not
accept a different digest or reinterpret a run ID, key or action ID.
The returned handoff must match the run ID, generation and external mode, with
configured credential and valid server paths/engine observation. A malformed or
mismatched handoff stops this check before progress is read.
Both `run_progress` and `connection_check` validate critical compact progress fields:
evidence stamps, explicit read completeness for events/decisions/reviews/checkpoints,
the aggregate `complete`, lifecycle and engine observations, expansion/finish gates,
and pending node/question counts with their bounded lists and truncation flags.
Missing or inconsistent fields cannot mean empty obligations. `run_progress` returns
HTTP 200 `response_incomplete`, `outcome=unavailable`, `reason=invalid_progress`
without body; `connection_check` returns `ok=false`/`invalid_response` without
exporting that context. Refresh explicitly before deciding; no automatic retry occurs.
Structurally valid `complete=false` reads retain incomplete-source diagnostics;
connection success then has `evidence_complete=false` and grants no permission to act.
Extra fields/sources and future advice codes remain compatible. This checks the
observation structure, not the correctness of policy advice or source contents.
Recorded questions may retain `claim_seq=-1` when no invocation is recorded;
this is observation, not verdict authority. Read the full current checkpoint before answering.
Observation bypasses the ordinary state route's reconciliation of a pending
operator reset. It can see an unfinished reset, so the subsequent generation fences
remain necessary; the diagnostic never completes that reset itself.

The result distinguishes unreachable UI/API, refused credentials/access, a missing
run, changed context, missing server harness credential and incomplete responses.
For `api_request`, `run_progress`, `result_notices` and `command_receipt`, an HTTP transport failure
returns `status: null` with fixed diagnostics. A GET has `code: api_unreachable`,
`outcome: unavailable`; this is unavailable evidence, not proof of a missing receipt.
A mutation has `code: request_outcome_unknown`, `outcome: unknown`: the server may
already have accepted or applied it. No automatic retry is made. Read saved command
receipts using the original Idempotency-Key/current generation, then state and
checkpoints, before choosing an exact retry. For checkpoint or result commentary
writes preserve the original `action_id` and exact body. Do not recover by inventing
a new key. HTTP responses, including 4xx/5xx, retain their original status/body;
`status: null` never means an applied command. MCP delivering this diagnostic is
successful tool transport, so inspect the returned fields even when `isError` is false.
The same unknown write outcome is explicit for received **5xx**, oversized **2xx**
or invalid JSON **2xx** acknowledgements. The original HTTP status survives; inspect
`code` and `outcome` even with HTTP 200. `reason` names `transport_error`, `server_error`,
`response_too_large` or `invalid_json`. A bounded 5xx body remains available; oversized
and malformed success bodies are not presented as receipts. A read over the 256 KiB
cap returns `response_incomplete`/`unavailable` with narrower-query advice; the cap
does not suggest resubmitting a write. Typed `run_progress`/`result_notices`/`command_receipt` also
refuse HTTP-200 non-object bodies. This is envelope validation, not a substitute for
domain schemas or source health. These typed reads also verify that the returned
generation matches the requested digest (either hex case). `command_receipt` checks
the returned command ID against the requested ID or the original key's durable ID,
using the server's identity derivation. A valid but different generation/command
returns `response_context_mismatch`, `outcome: unavailable`, with reason
`generation_mismatch` or `command_mismatch`. Missing/malformed identity fields return
`response_incomplete`, reason `invalid_response`. Both retain HTTP 200 but omit the
unbound body. Refresh state and original receipts before acting; there is no extra
request, retry or worker restart. This verifies identity, not full response schemas
or terminal evaluation. Generic text GETs and empty
204/205 mutation responses remain supported; validation 4xx responses keep their
original status/body. Never infer whether a server error occurred before or after
acceptance without reading durable evidence.

Typed `command_receipt` additionally checks its version-1 receipt fields: known
status/control event, strict boolean `terminal` consistent with that status,
nonnegative integer/null `event_seq`, string `error_code` (up to 256 characters)
and strict boolean `retryable`. A missing or inconsistent field returns HTTP 200
with `response_incomplete`, `outcome=unavailable`, `reason=invalid_command_receipt`,
without body. Extra fields are retained. The status/control sets and error-code
cap come from the same UI-free protocol constants as the server; a remote client
still needs no FastAPI/Uvicorn imports. Repeat the observation explicitly after
checking the source, rather than treating an incomplete reply as a command verdict.

The server also refuses malformed saved `event_seq`/`error` field types with 503,
instead of replacing a bad sequence with null or a bad error with empty diagnostics.
Older records with absent optional fields remain readable and are normalized into
the same public receipt. Observation never repairs a record, reconciles a command,
starts a worker or takes the exclusive write sequencer. `terminal=true` includes
rejected/failed/timed-out commands; inspect status/error before deciding. Even a
valid `succeeded` inject receipt proves admission, not completed training.
Connection-check errors omit raw HTTP bodies, exception details and URLs. On success it returns server
paths, last-read engine status, source health, `evidence_complete` and the next step.
`ok: true` means these reads succeeded; incomplete journals remain explicit, and
admission/checkpoint/report obligations still apply. It does not prove credential
scope or external-agent liveness and never submits, resumes or calls a model.

For Claude, open the project interactively and review the MCP approval prompt before
expecting a project server to connect. `/mcp` shows its status. This is client approval,
not a LoopLab checkpoint. Neither **Pending approval** nor **Connected** proves that
LoopLab has received a candidate. Resolve connection diagnostics before decisions.

**Tool approval is a separate step.** A loaded server can still wait for permission
to call its tools. Review the client's tool prompt. In Claude `--print`, a refused
tool can return `is_error: true` and appear in the final JSON `permission_denials`
while the CLI exits **0** with `is_error: false`. Inspect each MCP tool result, then
the API status and durable receipt; do not count a client exit as a submitted candidate.
If permission was refused, review it and reconnect to the same run. Read the original
command receipt before retrying a potentially submitted command. Approving a project
server does not grant permission for every call, resume the engine or answer a checkpoint.
`api_request` can write through the scoped server-wide credential; review its scope
before allowing unattended use. Client policy remains authoritative.

### Reproduce the Claude transport check

From a source checkout with `[ui,harness]` installed and an installed Claude Code,
run this opt-in, local acceptance check:

```sh
python -m benchmarks.claude_harness_smoke --claude /absolute/path/to/claude --out .tmp/claude-smoke
```

The output directory must be new. It starts a disposable loopback server/root,
protected CPU training scorer and local scripted Messages provider. Three fresh
Claude invocations check a denied tool, submit a candidate, then reconnect/retry,
measure two successful configurations and one failed configuration, publish Russian
interpretations and explicitly finish. Inspect/replay and protected file bytes are checked.
The client gets only a synthetic API key and scoped test token, isolated settings,
default permission mode and an explicit allowlist of LoopLab tools for the authorized
cases. It uses no permission bypass, shell tools, saved chat or model account.
`acceptance.json` stores client version, call counts and measured outcomes. Claude's
cost estimate reflects fixture token counts; it is not a provider charge. This checks
transport and permission handling, not an LLM's research choices or enabled optional
obligations (research, concepts, reports and monitors are disabled for this small task).

## External harness setup

Install the optional UI and MCP packages. Start the UI against the run root you want
to control. Set a distinct harness token for the coding agent; keep the owner token
in the UI server/operator session.

```sh
pip install -e ".[ui,harness]"
export LOOPLAB_UI_TOKEN='choose-a-private-token'
export LOOPLAB_HARNESS_TOKEN='choose-a-different-agent-token'
looplab ui --run-root runs --host 127.0.0.1 --port 8765
```

The server needs a distinct resolved owner credential whenever a harness token is
configured. On a local private origin, set both variables above; setting only
`LOOPLAB_HARNESS_TOKEN` refuses startup with instructions to set `LOOPLAB_UI_TOKEN`
and restart. The existing shared-origin policy may resolve a minted owner token.
Anonymous opt-out cannot be combined with a scoped harness credential. A local UI
without either token keeps its usual anonymous mode. Supply only the harness token
to the external MCP process; use the owner token to unlock the browser.

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
`LOOPLAB_HARNESS_URL=http://127.0.0.1:8765`. The process offers twelve tools:
`capabilities`, `connection_check`, `phases`, `phase_info`, `settings_keys`, `setting_info`, `operations`,
`operation_schema`, `run_progress`, `result_notices`, `command_receipt` and `api_request`. The latter
forwards to the same authenticated HTTP API as the UI. It never writes directly to
the event log. Use the live `operations` catalog to discover read, settings,
task, evidence, artifact and control routes, and `operation_schema` for a route's
OpenAPI definition. `settings_keys` and `setting_info` expose all Settings, including
advanced fields omitted from the UI form. `looplab harness --settings` returns the full Settings schema
and curated field help without a server.

`operations` and `operation_schema` each read the authenticated **live** OpenAPI
catalog once, then select matching routes or a route's referenced schemas. They
make no automatic retry and use no cached fallback. Inspect `code`/`outcome`:
transport loss returns `status: null`, `code: api_unreachable`, `outcome: unavailable`,
`at: openapi`. A non-200 response keeps its status with `code: api_read_failed`;
invalid JSON or malformed paths/components returns HTTP-200 `response_incomplete`,
`reason: invalid_response`. Error bodies, exception text and URLs are omitted.
Unavailable discovery supplies neither a matches list nor route schemas; it does
not establish absence of capabilities. Repeat discovery explicitly after recovery.
A valid empty matches list remains distinct from failure. The full catalog is
read before local selection, so the individual API-reply 256 KiB cap does not apply
to it. This checks catalog structure, not every OpenAPI/domain schema constraint.

For a remote client, `[harness]` is sufficient for all local metadata tools,
including every `phase_info`, entity schemas and curated `setting_info`.
Command request/server-derived fields live in the UI-free protocol; server
validation imports and re-exports the same objects. Metadata discovery does not
load the server validator or start a local UI. The MCP SDK may install Uvicorn
transitively; stdio does not import/run it. Keep client/server LoopLab versions
aligned because local phases/settings come from the client's package. Read live
`operation_schema` and this run's contract before writing; metadata grants no
authority and server validation remains definitive.

Read the launched task snapshot through `GET /api/runs/{run_id}/artifact` with
`root=run`, `path=task.snapshot.json`, and `expected_generation=TOKEN` from
`/state`. `GET /api/runs/{run_id}/artifacts` lists that file and its root;
`GET /api/runs/{run_id}/config` returns the run settings. All three routes are
available through MCP `api_request` with the scoped harness token.

`phases` is the workflow index for both modes. Each entry names the entity, the
built-in owner, the evidence to read, the external actions that write the same
domain state, and any PromptStore keys. `write_access` distinguishes operator
setup actions (global settings, launch, and the prompts, skills and knowledge stores)
from scoped agent actions.
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
review and checkpoint receipts. `finish_pending_nodes` lists candidates that
must settle or be explicitly aborted before finalization. Its `policy_preview`
computes suggested actions and parent IDs from a recorded strategy, or from the config snapshot before a
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

### A short next step

After reading the current `/state` generation and the run contract, call MCP
`run_progress(run_id, expected_generation)`. It performs one read of
`harness-progress?expected_generation=TOKEN&brief=true`; HTTP failures retain
their original status, with no hidden retry or resume. The UI **Agent cycle**
shows the same server-authored `next_step`. The owner run workspace also shows
a compact status above its views; expand its title for the explanation or open
**Agent cycle** for the full requirements. History and review omit live advice.

The summary prioritizes incomplete sources, then unanswered evaluation questions.
It names the responsible external agent, detail reads, response route and MCP phase.
`next_step.phase_id` resolves through `phase_info`: `evaluation` for a stage check,
`monitor` for train/ASHA questions, and `deadline_grace` for a deadline extension.
The question's own `phase_id` remains its runtime kind (`stage_check`,
`train_monitor`, `asha_live` or `deadline_grace`) and governs allowed verdicts.
When the engine is stopped, a listed question is recorded state, not a live
training wait. Inspect state and saved command receipts before choosing recovery;
resume may re-evaluate the interrupted attempt and replace its evaluator/question.
Refresh progress and checkpoints after resume before submitting a verdict.
Read the full checkpoint and `phase_info` before answering: the summary grants
no extra verdict or early-stop authority. Recorded pause/finish/stop requests
direct the agent to live state and command receipts; they do not prove that an
engine or external agent process is alive. Submitted experiments remain visible
as unsettled even when their evaluator has exited.
Even when every node is terminal and no pause was recorded, an absent engine owner
directs the agent to `recovery` before another candidate. An inconclusive lock probe
has its own unknown-status explanation and does not prove engine death. These hints
offer no automatic `resume`: inspect original receipts, then explicitly choose
recovery or finalization. When a live engine is merely idle, `choose_direction`
remains available. Neither branch claims that the external agent is connected.
The next-step explanation lists the same verdict vocabulary the server validates:
stage checks use `proceed`, `inconclusive`, `fail`; monitors use `continue`, `watch`
and `abort` only when that question grants kill authority; deadline review uses
`extend`, `stop` with a runtime-capped extension. A kill-enabled run setting alone
does not grant abort on its first advisory monitor question.

Both full and compact progress include `execution`: a last-read `engine_running`
lock probe (`true`, `false`, or `null`), `agent_connection: "not_measured"`, and
`recorded_node_counts` for unsettled `building`, `queued`, `evaluating`, and
legacy/untracked `pending` nodes. Counts use the same event prefix and public
activity rules as the node inspector: a pause-withheld evaluation is queued,
and a replacement engine owner must admit it again. A recorded start on a
stopped engine is interrupted work, not a claim that training continues.
The engine probe is independent of the journal prefix; refresh even when no
new event arrives. The UI polls this read every ten seconds and displays the
observation in its next step. A failed refresh withdraws that advice.

Progress also has `agent_activity`, an informational observation independent of
the journals and engine probe. Only a successful `harness-progress` read using
the scoped harness credential updates it; this includes MCP `run_progress` and
the progress read inside `connection_check`. Owner/browser polling, capabilities,
other API calls, a merely running MCP process and invalid requests do not count.
`recent_request` means a progress request was observed within **120 seconds**;
`quiet` means no such request was observed for at least that long. It does not
prove agent death, thinking, connection or delivery of the response to the client.
`not_observed` means this UI process has no observation for this run generation.
`last_seen_at` is server UTC; `age_seconds` uses a monotonic clock. The cache has
at most **256** run/generation entries and stores no credential or request body.
Restarting the UI, eviction or a new generation loses the observation. Nothing
is added to events or sidecar journals. It never blocks evaluation/finalization,
answers a checkpoint, pauses/resumes compute or invokes an internal replacement.
The compact workspace and Agent cycle show the signal separately, with Russian
labels when Russian is selected. Failed/stale reads withdraw it; malformed optional
activity is unavailable without hiding the authoritative obligations. Inspect
your client, current checkpoints and original receipts when the signal is quiet.

Checkpoint headings distinguish a completed-stage review, training monitor,
and deadline-extension decision. Questions and incomplete sources retain their
priority even if the engine stops. Follow the full question's allowed verdicts;
answering a checkpoint does not itself restart an engine. Suggested state reads
use `observe_only=true`, and original command receipts are read without worker
reconciliation. These reads create no admission or finish requirement.

Once these immediate issues are clear, choose **continue** or **finish**.
`candidate_blockers_if_expanding`, per-Idea reviews and concept/hypothesis fields
apply to continuation; they do not force a finalizing agent to propose again.
Blocker `phase_id` names the MCP decision phase: the enabled concept base is
`concept_tags` with `command:run_concepts`; its setting remains `concept_run_base`.
`finish_pending_node_count`, `finish_report_due` and `finish_reviews_due` describe
finalization. No current expansion gate is a guarantee of admission: candidate
validation, task edit surface and budgets still apply. `policy_preview` is advice;
the compact view carries its source and count, with full actions in the detail read.

The compact read preserves all four source-health receipts and each history's
total, offset, limit and `has_more`. It omits receipt bodies and observations;
question/node lists are limited to 20 with explicit total/truncation fields.
Use `details.history` to page the full read and `details.questions` for full
questions, substituting the current run ID and generation. Refresh after events
or responses; sidecar-only writes also become visible through the UI's polling.
The UI withdraws next-step advice after a failed refresh or a newer observed event.

For Codex, add this to your project `.codex/config.toml` (or the user config):

```toml
[mcp_servers.looplab]
command = "looplab"
args = ["harness-mcp"]
env_vars = ["LOOPLAB_HARNESS_TOKEN"]
env = { LOOPLAB_HARNESS_URL = "http://127.0.0.1:8765" }
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
        "LOOPLAB_HARNESS_TOKEN": "${LOOPLAB_HARNESS_TOKEN:-}",
        "LOOPLAB_HARNESS_URL": "http://127.0.0.1:8765"
      }
    }
  }
}
```

The credential is read from the coding client's environment. Replace the URL
with your reachable UI/API address and `command` with the installed executable
path when necessary. Formats follow the official
[Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
and [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
On MCP initialization, LoopLab supplies workflow instructions for generation
checks, phase discovery, reconnect receipts, explicit finish and result commentary;
the launched run's contract still defines its obligations.

The normal control cycle is:

1. Read `/api/runs/{run_id}/state`, task, config, `harness-contract`,
   `harness-progress` and relevant events/artifacts.
   Search `operations` for the exact read routes. Review the goal, evaluation
   command, allowed edit paths, parent generations, budget and prior results.
   Public state nodes and node detail responses include `parent_comparison`: for a single recorded parent,
   `{"version": 1, "node_id": ID, "attempt": ATTEMPT}`, otherwise `null`.
   This is the parent's attempt recorded when the child was created, not its current
   attempt. Check it against the current parent's `attempt` before interpreting a
   parent-score difference; reset does not refresh the child's reference. The full
   internal `parent_generations` map remains private. A matching reference alone does
   not establish matching evaluation conditions or grant permission to submit a candidate.
   The evidence-scoped reviewer node route uses the same DTO; summary-only access still
   refuses node detail. Building nodes have no comparison receipt. For live Inspector reads,
   details can lag or lead state: comparison requires coherent current result evidence,
   not merely the presence of a parent score or matching input keys.
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
   Preserve the entire value request, including `expected_evidence_revision`,
   estimates and `action_id`. An exact retry acknowledges the original batch,
   even after a policy switch, reset, new outcome or tombstone; it never writes
   fresh estimates. Changing the evidence revision under that action ID is a
   conflicting request and returns 409, even with identical estimates. LoopLab
   reconstructs the original reviewed evidence from the event prefix before the
   batch's first `node_value_estimated` event, so existing events need no migration.
   After reset, read the current complete candidate batch and publish a justified
   new review with a new action ID. Old ACKs do not close the new value obligation.
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
   `watch` requests another look; it does not itself authorize stopping. The
   training monitor's deterministic loss veto reads only the current attempt's
   bytes, including after reset or repair. An improving, non-anomalous curve
   removes abort authority. Arrival of the declared training artifacts also
   spends that authority, even if checkpoint validation still logs in the same
   command. Read the new question's `kill_enabled` instead of carrying permission
   from an earlier question. Flat loss alone is not proof that training is broken.
   ASHA compares the declared resource on canonical geometric rungs (the largest
   power of two at or below it), using persisted sibling start-of-rung measurements.
   Final endpoints alone never grant stop authority. After `watch`, at least three
   consecutive underperforming checks with enough same-rung peers are required in
   the current stage; a new stage starts a fresh grace window. Missing resource key
   or peer rung leaves ASHA advisory. Objective retarget disables task-curve abort,
   including an already open question: its current `kill_enabled` becomes false,
   with `recorded_kill_enabled` and `stop_refusal` explaining the prior grant and veto.
   The journal stays unchanged. An exact retry remains a receipt of prior acceptance;
   if retarget occurs before an accepted abort is consumed, the engine vetoes that
   stop, records `asha_rank.stop_refusal`, and can open a fresh advisory question.
   An opened observation holds the node terminal until answered. Inspect measured
   metrics, stage logs and failures, then submit a corrected candidate if useful.
   The metric is measured by LoopLab's evaluator; never submit a claimed score.
   If a command stage reaches its deadline and `eval_deadline_grace_s` is enabled,
   the agent receives a `deadline_grace` checkpoint. Answer `extend` or `stop`;
   the runtime limits an extension to the operator's configured allowance and
   records the granted seconds with the stage. The external run does not invoke
   LoopLab's internal deadline judge. While the question is unanswered, its
   watchdog waits for the agent; the command is not suspended and may keep
   running or finish. This wait has no automatic timeout. The cap limits one
   extension starting when the runtime consumes `extend`; it does **not** bound
   the unanswered wait or the command's total wall time. A disconnected agent
   therefore leaves an explicit pending question, even if the command finishes.
   Reconnect, inspect the current question and logs, then answer explicitly.
   `stop` records a deadline failure without a completed score. Setting
   `eval_deadline_grace_s=0` disables this question and keeps the ordinary deadline
   timeout. No automatic answer or agent-death detection is provided.
   The train observer checks at the configured adaptive cadence during a command
   evaluation. If an evaluation finishes before its first tick, LoopLab checks
   its final attributed training log before committing the node result and waits
   for the external agent's answer. ASHA asks when an intermediate metric and
   enough eligible completed siblings exist; same-rung comparability is additionally
   required for stop authority. Evaluators without an
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
`set_strategy` is durably accepted when its command succeeds, then the live engine
applies it at its next decision boundary and records `strategy_decision`. Before
acting on the new policy, refresh `harness-progress.policy_preview` and wait for
its `policy` and `policy_source=recorded_strategy` to reflect the switch; command
success alone does not confirm application. A paused engine needs a resume to
reach that boundary: on a paused run a `set_strategy`, `inject_node`,
`force_confirm` or `budget_extend` command is recorded and settles `succeeded`
with `deferred_until_resume` without starting the engine, and the explicit
resume serves the queued intents together (doc 69 69.30; `fork`,
`force_ablate` and `deep_research` are refused on an external run). A
`budget_extend` on the engine's own obligations pause (below) still resumes
the run. Editing the run's `policy` config takes effect on the next
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

The default lesson role is `shared`; explicit `researcher`/`developer` roles are
stored so the corresponding cross-run channel can use them. Exact retries compare
the original authored payload before checking eligibility for a fresh publication.
After reset, tombstone or abort they acknowledge the stored action without updating
its outcome signatures. A reconciled/retired lesson is returned as it is currently
stored; retry does not restore support. Changed content under the same action ID
is a conflict. Healthy generation and run identity are still required.

Lesson and skill publications require a healthy event prefix. Original lesson
lookups and new writes also require complete JSONL in `lessons.jsonl`; skill ACK
lookups require `skill_candidate_actions.jsonl`, and fresh skill drafts additionally
check the current lesson source. Malformed JSON, non-object records and invalid
UTF-8 refuse with 503, a named source and available health diagnostics, rather than
skipping a possible original/conflicting action. Existing 64 MiB store bounds remain.
Compatible legacy object rows are retained; this does not replace the claim schema.
Fresh `completed` lesson/skill reviews also require a complete referenced store.
The shared sources are named in the refusal; progress's four run-local source
receipts do not include these memory stores. Ask the operator to recover known
bytes, then explicitly resolve the same request. No API repairs or resumes work.

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
An existing skill action may replay after its source lesson has retired or vanished:
that ACK proves its earlier publication, not current eligibility or promotion.

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

#### Recover a decision or review acknowledgement

Keep the exact body, `action_id` and target generation for `/harness-decisions`
and `/harness-reviews`. An identical retry returns the **original** saved receipt,
including its original `at_node` and `evidence_revision`, even after a new terminal
result, reset or tombstone. The two server-assigned context fields are not new
request content; normalized authored content, run identity and action identity
still must match. Conflicting content or a different generation is refused.
Existing journals use the same rule without a migration or a new digest field.

A decision/review source must be complete before either an acknowledgement or a
fresh write can be trusted. Malformed JSON, invalid UTF-8, non-object records or
invalid receipt fields return HTTP 503 with `harness_history_incomplete` and the
source filename. Candidate admission cannot use decisions from that damaged
source; finish and configured review windows cannot use reviews from it.
`harness-progress` remains readable with the accepted history, `complete=false`,
`source_health` and `next_step.code=inspect_sources`. Its displayed due lists are
diagnostics on accepted rows, not permission to continue while a source is incomplete.

The harness never deletes damaged lines or manufactures a replacement receipt.
Ask the operator to recover the journal from known evidence, then refresh progress
and resolve the saved request using its exact body/action ID. Missing journals are
ordinary empty sources; unavailable/over-bound sources refuse reads and writes.
Explicit HTTP/CLI finish refuses damaged reviews. A live engine reaching its
existing budget finish gate pauses with `due.source_error`, preserving a resumable
run instead of routing source damage through fatal-error finalization. Recovery
does not itself resume evaluation or satisfy current report/review obligations.

These semantic harness operations also require a healthy authoritative event
prefix: GET hypothesis board/selection and POST decisions, reviews, hypothesis
reviews, verifier samples or MCTS values. A complete corrupted event record returns
503 with `harness_history_incomplete`, `source=events.jsonl` and `source_health`,
before a successful ACK or a fresh publication can be claimed. Unreadable sources
use `harness_history_unavailable` with health diagnostics; absent/empty sources
cannot prove identity and use the same unavailable code. Generation mismatches on
a healthy source still return 409.

The source check runs before and after reading, catching damage introduced during
the read. Normal valid concurrent appends remain allowed; event publications keep
their existing CAS fence. Progress remains the diagnostic read for an incomplete
prefix. EventStore's normal torn-tail rule is unchanged: an unterminated crash tail
is separate from a complete corrupted record. The harness neither rewrites bytes
nor supplies missing events. After operator recovery, refresh progress and resolve
the original body/action; an old ACK still does not approve current evidence.

`replayed=true` acknowledges prior publication; it does **not** refresh its review
window or grant admission/finalization. Read current `harness-progress` and the
history item's `validity`. If it is `superseded`, inspect the new evidence and
publish a fresh justified decision/review with a new action ID. A fresh review
still validates its current evidence and recorded domain action. Result commentary
never discharges these obligations or the report requirement.

For durable commands, HTTP 200 alone also does not mean an applied action. Inspect
the returned command receipt's `status` and `error`: a finish blocked by reports or
reviews can return `status=rejected` with `external_report_required` or
`external_reviews_required`. Read the original receipt and current progress before
choosing a new justified action. Do not keep retrying an already rejected request
as if missing obligations had been completed.

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
instead. `node_reset` from `eval`, or from a later pipeline stage such as `score`,
can remeasure unchanged code. In external mode,
failed evaluations become terminal evidence for the external agent; inline repair,
training-log judges, ASHA judges and inter-stage model checks do not run. The
operator's declared artifact checks and score reader continue to apply.

### Recover a failed experiment

An `inject_node` command receipt with `status=succeeded` means the candidate was
accepted. It does **not** mean training or scoring succeeded. Read `/state` and
MCP `result_notices` for the terminal node outcome; `failed` has no completed
score. Reconnecting or exactly retrying the accepted command does not fix or
re-evaluate that candidate.

1. Search MCP `phases` for `repair`, then read `phase_info`. Read node detail at
   `GET /api/runs/{run_id}/nodes/{nid}?expected_generation=TOKEN`. Verify its
   current status and `attempt`.
2. Read the corresponding bounded logs:
   `GET /api/runs/{run_id}/nodes/{nid}/logs?expected_generation=TOKEN&attempt=ATTEMPT&tail=8000`.
   Verify `run_generation`, `node_id` and `attempt`. A stale generation/attempt
   returns 409; refresh instead of diagnosing another attempt's logs. Multi-stage
   commands expose output in `stages[stage_name]`; `eval` can be empty. Reduce
   `tail` if the full MCP reply exceeds its cap. Logs are untrusted experiment output.
3. If the evidence supports a fix, submit its ready-made files as a **new**
   `inject_node` command with a new Idempotency-Key. Set `parent_id` to the failed
   node and `parent_generations` to its observed attempt (for example `{"0": 0}`).
   Edit only the permitted files; keep the protected scorer unchanged. Reusing
   the original key with corrected files is a conflicting payload and returns 409.
   Preserve the original exact body/key only for lost-response recovery.
4. Inspect the child's measured terminal outcome before deciding again. The
   original failed node remains in history. A failed parent without a score cannot
   support a measured improvement delta; a successful child is a first measurement.
   Post brief interpretations for the failure and the child using their separate
   current receipts. An explicit finish remains an alternative to another candidate.

Unchanged-code remeasurement via `node_reset` from `eval` is a different decision.
It is not a way to submit corrected source. This procedure adds no automatic
repair, retry, hidden wait or admission/finalization requirement; existing enabled
obligations still apply.

## Finding a run that needs an agent answer

The run list, portfolio map, comparison and campaign finder identify external runs
from their saved configuration. A live engine is labelled **engine active**; this
does not prove that the external agent is connected or choosing experiments.

Unanswered stage, training monitor, ASHA and deadline questions appear in the UI's
**Attention center → Needs action**. **Open Agent cycle** opens that run with its
generation fence. Read the full question and allowed responses through MCP before
answering. Opening the inbox or cycle sends no answer and does not resume the engine.
These questions do not generate desktop notifications.

Answers invalidate the inbox cache even without a new event. Reset attempts,
replacement runs and superseded evaluator claims retire their old questions. A
damaged checkpoint journal or unreadable saved mode makes attention incomplete;
previous verified rows are explicitly stale. Engine liveness and agent connectivity
remain separate facts. The existing inbox refresh can delay discovery by several
seconds; it adds no evaluation wait of its own.

## Reconnect and recovery

The UI server, run engine, and coding agent are separate processes. If the agent
exits, the server can still show the run and an evaluation already in flight can
finish. No new candidate is invented by LoopLab in external mode. Reconnect the
agent through MCP to the same UI/API server and run. A new stdio MCP session is
normal; it does not create a new research run. Search `phases("recovery")` and read
`phase_info("recovery")` before choosing a recovery action.

1. Read `/state` for the current run generation and whether the engine is live,
   paused, or finished. If the engine stopped, an authorized operator or external
   agent must explicitly choose resume after checking the outstanding work;
   reconnecting MCP by itself does not restart search.
2. Read `harness-progress` using that generation. Inspect `source_health` before
   interpreting a missing decision or receipt. Check pending checkpoints and
   candidate/finish requirements. **Agent cycle** in the UI shows the same
   obligations and links to the event timeline.
   Check the engine probe even if there are no pending nodes: a process can die
   between experiments without recording pause. Stopped or unknown idle engines
   point to `phase_info("recovery")`; reconnection and repeated reads start no work.
3. For a lost command response, call MCP `command_receipt(run_id,
   expected_generation, idempotency_key=ORIGINAL_KEY)`, or supply `command_id`
   instead of the key if known. It performs one GET of `/command-receipt`; the key
   travels in the `Idempotency-Key` header, not in the URL. This reads the saved
   status without reconciliation, worker restart, record healing or an exclusive
   sequencer lock. **GET `/commands/{command_id}` has different semantics:** it can
   restart a nonterminal worker. Choose that recovery deliberately after the read.
   Inspect Events and node state: the last request may already have applied, and
   `succeeded` describes the control command, not a completed experiment.
   Wait for that command's terminal receipt before another state-changing command;
   seeing its effect or a live replacement engine can precede the worker's finish.
4. Continue the outstanding decision, or explicitly pause/finalize. A checkpoint
   may remain open after the evaluator command has finished; answer it before
   treating the node as terminal. Finalization can owe a report or reviews.

If checkpoint reads or answers return **503**, inspect `source_health`. Damaged,
duplicate, orphaned or invalid checkpoint records cannot supply a current question
or an answer to the engine. A missing, empty or incomplete event history also
refuses checkpoint reads, publication and answer consumption. The engine keeps the existing question
unresolved; reconnecting does not repair journals or approve the stage. An operator
must recover the source explicitly, then the agent must refresh state and progress.
Question kinds must belong to the implemented checkpoint vocabulary and
`kill_enabled` must be a boolean. A string such as `"false"`, an integer or an
unknown kind makes the source incomplete; truthiness never supplies stop authority.
Keep a lost checkpoint answer's exact body and `action_id`: after a healthy source
is available, an identical retry returns `replayed`, even after the node is terminal.
That receipt proves prior acceptance. The engine consumes a verdict only while its
run generation/UID, node attempt and latest evaluator claim are still current and
the node is pending. Reset or evaluator reclaim requires a fresh question and answer.
Pause itself does not supersede an in-flight question: an evaluation draining after
pause can still require its answer. Progress names that remaining checkpoint;
answering it does not resume search. An explicit `resume` while the engine is
still waiting retains that engine and question. Reconnecting to a stopped,
paused run leaves it paused until an explicit durable `resume` succeeds.
If the engine died before the evaluation settled, explicit resume can claim that
same node attempt with a new evaluator and run the protected command again.
Its old unanswered checkpoint becomes superseded; an answer to it is refused
with 409. Answer the freshly observed question. This differs from reconnecting
to a live waiting engine, where the original question remains current. A score
command's completion alone does not make its interrupted attempt a terminal result.

In the UI, open **Agent cycle → Connect external agent → Reconnect or recover a
lost response**. Read by original key or command ID. Typing submits nothing;
**Read saved receipt** sends a GET. The form keeps its identity only in memory,
and a failed refresh hides the previous status. No resume, retry or candidate
submission is automatic. The ordinary Events view can help locate a `command_id`.

Save the exact request payload, original key and target run generation in your
agent's private recovery record **before** submission. If the response was lost,
an exact resubmission uses that same payload and key; a new key can create a second
candidate. For a recorded retryable failure, inspect current evidence before
explicitly using `/commands/{command_id}/retry`. Changed payloads are new decisions.
Never automatically retry after 404/409/503: a missing receipt does not prove
that nothing applied; a generation mismatch requires a fresh identity check;
an unreadable record needs operator recovery. The saved snapshot may lag events.
Reads are bounded to a 2 MiB record and omit payloads, hashes and error prose.

The UI does not currently prove whether a remote agent process is connected.
It reports the known engine and run obligations; a quiet event log alone is not
evidence that the agent has died.

### Reproduce recovery locally

From a source checkout with `[ui,harness]` installed, use a **new** output directory:

```sh
python -m benchmarks.external_session_smoke --out .tmp/new-session-proof
# Observe a paused open checkpoint for two minutes; use another new directory.
python -m benchmarks.external_session_smoke --out .tmp/held-proof --checkpoint-hold-seconds 120
# Kill only the fixture's engine at an open checkpoint and explicitly recover it.
python -m benchmarks.external_session_smoke --out .tmp/engine-loss-proof --engine-loss
# Enable research, concept base, report and lesson/skill obligations across reconnect.
python -m benchmarks.external_session_smoke --out .tmp/obligations-proof --obligations
# Reconnect while a fast command's first advisory training review is unanswered.
python -m benchmarks.external_session_smoke --out .tmp/monitor-proof --monitor --obligations
# Reconnect at deadline: extend, stop, exhausted cap; also test disabled grace.
python -m benchmarks.external_deadline_smoke --out .tmp/deadline-proof
# Inspect actual live loss: protection, authorized stop recovery, checkpoint validation.
python -m benchmarks.external_live_monitor_smoke --out .tmp/live-monitor-proof
# Compare measured ASHA rungs, recover a stop question, and test objective retarget.
python -m benchmarks.external_asha_smoke --out .tmp/asha-proof
# Recover between experiments: MCP loss with a live engine or an explicitly resumed engine.
python -m benchmarks.external_idle_recovery_smoke --out .tmp/idle-proof
# Record copied-base provenance while the operator source changes during training.
python -m benchmarks.external_idle_recovery_smoke --out .tmp/seed-base-proof --case all --seed-base-recovery
# Keep a selected archived base across recovery after removing the owned source.
python -m benchmarks.external_idle_recovery_smoke --out .tmp/pinned-base-proof --case all --seed-base-recovery --pinned-seed
# Observe actual request silence after MCP death, without owner polling refreshing it.
python -m benchmarks.external_idle_recovery_smoke --out .tmp/activity-proof --case agent_loss --quiet-hold-seconds 121
```

`--seed-base-recovery` covers agent loss and explicit engine recovery on a private
git fixture. It changes the source after the first seed and before its terminal,
checks the old/new copied-base receipts through public MCP, joins each to its
seed event and generation, and requires exactly two protected training executions.
It verifies seed provenance and the old/new run-owned archives, including their
protected scorer bytes. It makes no claim of upstream advancement or complete
archived-base replay (data and environment remain outside the snapshot).
After the two evaluations finish, this option removes its disposable source and
node workdirs, exports both bases via the CLI and verifies the crate. One separate
export-validation SGD execution reapplies the recorded second recipe and compares
its result. The proof reports it separately from the two engine evaluations;
export/validation leave the event log unchanged.
This variant declares `cmd.scorer_boundary.files: [score.py]`, with manual `protect`
empty and entrypoint inference disabled. It verifies the declared scorer's hash
and complete boundary receipt through MCP after each terminal and reconnect.
The boundary covers the listed files; it does not certify dependency closure.

`--pinned-seed` first records a private seed archive, then selects that exact
`workspace_seeded` sequence and digest in the operator's task. After the first
terminal it removes its owned source, reconnects or explicitly resumes the engine,
and submits the second recipe. Both terminal/MCP receipts must retain the same
base digest and origin selection. Each case still runs exactly two engine SGD
evaluations and one separately counted export-validation execution. Export verifies
one deduplicated base after source/workdir loss; `inspect`/`replay` and protected
scorer checks remain required. The selected origin is retained as a required input;
origin loss refuses future seeds/resume. This tests immutable **initial** selection,
not live advancement or model-authored equivalence judgments. See
[task setup](tasks.md#start-a-new-task-from-a-recorded-base) for the operator field;
candidate injection cannot set or change it.

The offline scenario runs three real CPU training configurations with a protected
scorer. It kills its own MCP process, restarts its private UI server, withholds two
committed acknowledgements as HTTP 503, and damages a disposable checkpoint tail.
It checks original receipt recovery without duplicate candidates, read-only history,
source refusal, answer replay after terminal, result summaries, and `inspect`/`replay`.
It pauses with an open stage checkpoint and reconnects without answering or
resuming; phase discovery follows the server's actual next step. The optional
hold samples the same question, pending node and unchanged journals over real
time. It settles the pause's recorded engine acknowledgement before comparing
journal bytes, then explicitly resumes that same engine without repeating score.
It later pauses, reconnects in a third MCP session, resumes through the production
command spawner and resets a measured node. The old verdict remains replayable;
the new attempt stays pending until its own checkpoint is answered.
With `--engine-loss`, the fixture terminates its owned engine after score has
completed but before the checkpoint is answered. Read-only reconnect leaves it
stopped; explicit resume spawns a replacement, re-evaluates the same node attempt
and opens a question bound to its fresh claim. The old answer receives 409 without
changing the journal. The history retains that question as superseded. The proof
counts both score executions and distinguishes them from the single terminal result.
With `--obligations`, the operator enables research/report every node, a run concept
base, reflection priors and per-node comparative lesson/skill reviews. The agent
uses MCP discovery, publishes a research memo, tags candidates, seeds `run_concepts`,
and writes reports over actual measured state. It records justified no-action
reviews because this single deterministic fixture supports no reusable ML finding.
Reports/reviews survive reconnect and become due again after reset, even when the
node count is unchanged. Finalization closes finish obligations without satisfying
an expansion-only research gate. This flag can be combined with `--engine-loss`.
With `--monitor`, the operator declares one protected `train_eval` command with a
training role. It performs training and scoring once. Monitoring is enabled with
kill configured and a 600-second cadence; the fast command opens its first question
in the final observation before node terminal. The fixture kills a second owned
MCP process at that monitor, restarts its UI again, and verifies the same question
and live engine on reconnect. Advisory `abort` is refused, explicit `continue`
settles the question, and exact replay creates no duplicate answer. Reset gets a
fresh monitor; the failed configuration keeps no metric after its own review.
This tests completed-command advisory recovery, not live-curve stop judgment.
Only this fixture restores its known valid bytes; that is not a production repair
procedure. `acceptance.json`, engine logs and inspection output are saved under the
output directory. This checks protocol recovery, not model judgments or interactive
tool approval in an installed Codex/Claude client. Remote agent liveness is unmeasured.

The separate deadline probe launches four isolated runs with an unchanged protected
SGD scorer and an operator-declared timeout. A deliberate timer before SGD makes
deadline decisions reproducible. For enabled grace it observes continued command
output during an unanswered checkpoint, kills its own MCP, restarts its UI and
reads the same question with the same live engine and no terminal metric. It rejects
`abort`, explicitly answers `extend` or `stop`, and retries the exact answer both
before and after terminal. The completion case measures held-out MSE; stop and
exhausted grace retain no metric. Stage events record the granted cap, including
the timed-out stage. Each command executes once, and a cap allows no second question.
With grace disabled there is no checkpoint. All runs publish result interpretations
in Russian and pass `inspect`/`replay`. Use `--case completed_extend`, `--case stop`,
`--case capped_extend` or `--case disabled` for one scenario in a new output directory.
This checks protocol timing with fault injection, not long-running ML or model judgment.

The live monitor probe runs three protected CPU SGD configurations with a separate
protected scorer. The improving curve keeps abort unavailable after `watch`.
A zero-learning-rate control grants an explicit second-question abort; killing the
fixture's MCP and restarting its UI preserves that question and live engine. The
agent deliberately stops this acceptance control, which yields no completed metric
or training artifact and never runs score. A third case saves its checkpoint before
validation in the same command: subsequent questions remain advisory after `watch`.
Each question is answered through MCP, exact retries create no duplicate answers,
protected sources stay unchanged, and terminal/run interpretations appear in Russian.
All three runs pass `inspect`/`replay`. Use `--case improving`, `--case frozen_stop`
or `--case checkpoint_validation` to select one case. Delays pace real optimizer
steps for observation; this does not test model judgment or a multi-hour session.

The ASHA probe measures two baseline SGD curves before each new control candidate.
Its five cases cover same-resource stop recovery, no declared resource, an absent
peer rung, operator retarget before evaluation, and retarget while a stop question
is open and MCP is disconnected. The last case reconnects to the same question
with current abort authority removed and unchanged journal bytes. Only the fixture's
operator performs retarget, with a private credential never supplied to MCP.
An opaque protected command trains and scores once per node, computing actual
held-out MSE at each step. Stop produces no completed metric or checkpoint; advisory
cases finish normally. Every terminal node and finalized run gets a Russian result
interpretation, protected scorer bytes stay unchanged, and `inspect`/`replay` pass.
Use `--case same_resource_stop`, `missing_resource`, `unmatched_rung`, `retarget`
or `retarget_open` to select one case in a new output directory. This is protocol
acceptance with real short SGD, not a claim that ASHA or the agent makes good ML decisions.

The idle recovery probe finishes a protected real SGD evaluation before killing
its own MCP and restarting its private UI. With only MCP loss, the same engine
waits for another explicit candidate. With engine loss too, `run_progress` directs
the new session to recovery despite having no pending nodes or recorded pause.
Three repeated reads leave the event log unchanged; a durable explicit `resume`
creates one replacement engine, and an exact retry creates none. Both cases
measure a second candidate, publish result interpretations, finalize explicitly
and pass `inspect`/`replay`. Select `--case agent_loss` or `--case engine_loss`.
This short protocol test observes engine ownership, not remote agent availability.
Its optional `--quiet-hold-seconds 121` holds the real engine idle after MCP death
for more than the activity threshold. Operator reads then see `quiet`; the measured
result and event log remain unchanged. UI restart shows `not_observed`, and a new
MCP progress read establishes a new observation. A quiet/forgotten observation
never resumes the run or becomes a verdict that the agent died.

For real lost responses after server acceptance, use a new disposable output:

```sh
python -m benchmarks.external_idle_recovery_smoke --out .tmp/new-transport-proof --case agent_loss --drop-command-response
```

An owned loopback TCP proxy forwards the first candidate and node commentary,
receives the UI's successful responses, then closes each connection without sending
the reply to MCP. The agent observes unknown write outcomes, reads the saved receipts
and retries the exact bodies/identifiers. A progress GET reply is also lost: unavailable
evidence changes no work. Recovery, UI restart and a second measured candidate still
produce exactly two score executions and three commentary rows (two nodes and one
explicitly finalized run). This exercises real transport/protocol recovery with short
protected SGD, not model decisions or interactive client approval.
Add `--response-fault invalid_json`, `oversized` or `server_error` to replace the
two accepted write replies with malformed HTTP 200, over-cap HTTP 200 or HTTP 503.
The proxy first receives upstream 200 in every case. The GET reply still disconnects.
Each independent case must preserve the same receipts, two actual score executions
and three commentary rows; fault payloads never supply a metric.
Add `--read-fault stale_generation` or `wrong_receipt` to replace the GET reply
with a JSON object carrying another generation or command ID. The latter targets
the original command receipt; the former targets progress. MCP must return
`response_context_mismatch` despite HTTP 200, omit the body and leave work unchanged.
Explicit fresh reads then recover the correct evidence. Each case needs a new out
directory; write faults and read faults are independent fixture options.
Add `--discovery-fault disconnect`, `invalid_json` or `invalid_catalog` to fault
both live discovery tools before the first candidate. The proxy receives upstream
200, then disconnects or substitutes HTML/JSON with malformed `paths`. Each tool
must return unavailable evidence without claiming an empty catalog; its next
explicit read must recover the real command route/schema. Discovery changes no
events or work. The remaining protected SGD/reconnect checks still apply.
For the minimal remote-client installation, create a separate environment with
`pip install -e ".[harness]"` and pass its Python executable using `--mcp-python`.
The fixture requires FastAPI absent and blocks both FastAPI and Uvicorn imports
inside its stdio process. It reads every phase and the setting metadata before
and after reconnect, while its server, engine and protected scorer use the normal
interpreter. Uvicorn may be present as an MCP dependency; it must not be imported
by the stdio path. Combine this option with the discovery/response faults above.
Use `--result-backlog --case all` in a fresh output directory to defer the two
node interpretations across MCP death/UI restart, then recover them using one-item
pages through typed MCP `result_notices`. Each summary is posted and exactly retried while its next cursor stays valid;
finalized run/node receipts are paged too. This covers both agent loss and engine
loss with an explicit resume. It can use `--mcp-python`; response-loss proxy options
are separate probes. The fixture waits for the engine's existing post-evaluation
`trust_scan` before asserting that commentary/page reads append no events.
Add `--failed-first` to this backlog probe to fail the first protected scorer on
an invalid learning-rate type, read generation/attempt-fenced logs after reconnect,
reject stale log reads and changed-payload key reuse, then submit a corrected child.
An exact retry of the admitted failed candidate must not execute its scorer again.
The final result contains one failure without a score and one measured SGD result,
with separate interpretations and preserved lineage. Both agent-loss and engine-loss
cases are supported; the fault changes only editable configuration.
Add `--obligations` to enable research, authored concepts, novelty decisions,
reports and lesson/skill reviews in this recovery probe. After the corrected child
settles, it repeats the old journal requests and verifies that original receipts
are returned without new rows while progress still marks them superseded.
Commentary alone must not permit finish; a fresh report alone must not discharge
fresh lesson/skill reviews. The agent supplies justified no-action reviews for
this deterministic fixture, then explicitly finishes. Expansion-only research and
concept-base gates remain separate from finalization. These are scripted protocol
decisions, not model judgment parity tests.
Add `--damaged-journals` with `--obligations` to damage each private decision/review
journal after the two terminal outcomes. Progress must expose incomplete sources;
both exact retries and new action IDs must refuse without changing journal/event
bytes. A damaged review source also refuses explicit finish. Only the fixture
operator restores its own known-good backup; the server performs no repair.
After restoration the old receipts replay, stay superseded, and require current
finish evidence as above. The byte attribution check waits for the specific
existing report command's engine acknowledgement, which may follow its intake receipt.
Add `--value-recovery` with `--result-backlog` to exercise explicit greedy → MCTS →
greedy switches on two successful measured SGD nodes. The fixture operator sets
`mcts_value_weight=0.4` at launch; the scoped agent switches only the policy.
It submits the complete batch with clearly labelled scripted headroom beliefs,
replays its original request, refuses changed evidence revision under that action,
then replays again after returning to greedy. Exactly two value events and two
applied strategy decisions are recorded, without extra scoring. This option excludes
`--failed-first`: MCTS branch review needs two eligible measured branches.
Combine it with `--obligations` and `--mcp-python` for report/review and minimal
remote-client coverage; use a fresh output directory for each probe.
Add `--damaged-events` with `--obligations` to damage a complete record in the
private event log after terminal results. Progress must expose incomplete event
health; hypothesis/selection reads and exact/fresh decision/review requests must
return source refusals without modifying event or sidecar bytes. With
`--value-recovery`, the accepted value batch is checked too. Only the fixture
operator restores its known-good backup, then recovers the old ACKs and explicitly
satisfies current finish obligations. This probe preserves normal EventStore
torn-tail handling and makes no production repair request.
`--case agent_loss --drop-command-response --read-fault incomplete_result_page`
removes `items` from a real successful result reply while preserving generation.
The typed read must return `invalid_result_page`, without body or automatic retry.
The next explicit read recovers the measured receipt without changing event bytes.
Run this response-loss probe separately from backlog/engine-loss probes.
`--read-fault incomplete_receipt` in the same agent-loss response-loss probe
submits an invalid hint, observes its real `rejected` record, and removes `terminal`
from one successful receipt reply while preserving generation/ID. The typed read
must refuse it; the next explicit read recovers `rejected`/`invalid_command` with
no new event, automatic retry or worker restart. The protected SGD evaluations remain unchanged.
`--read-fault incomplete_progress` removes the candidate decision requirements from
two real successful progress replies, keeping generation and the other fields.
Both typed `run_progress` and `connection_check` must refuse that context. Explicit
subsequent reads recover the gates without event changes, work or automatic retries.
Add `--knowledge-recovery` with `--result-backlog --obligations` to publish a
protected-evaluation protocol lesson and candidate skill after two measured nodes.
An owned proxy drops both accepted responses; exact retries must recover one lesson
and one skill receipt. The fixture damages its own event/knowledge sources, proves
refusal for writes and fresh completed reviews without changing domain bytes, and
restores only its own backups. Run it separately from `--failed-first` and
`--drop-command-response`. The authored technique checks recovery protocol; it is
not evidence of general ML quality. Finish report and completed reviews stay explicit.
`--case agent_loss --drop-command-response --read-fault stale_result_generation`
instead replaces a successful result page's generation at the owned proxy. The
typed tool must return unavailable context without those receipts; the next explicit
read recovers. This fault is synthetic transport evidence, never an ML result.

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

### Reusable capabilities and verified base advancement

When the operator launches a pinned repo task with `upstream`, search
`phases(query="upstream")` and read `phase_info("upstream")`. Use
`upstream_status(run_id, expected_generation, offset, limit)` for the current base,
Maintainer instructions, per-hunk nominations and paged gate history. A nomination
is advice; a completed source must have its current primary score and seed receipt.
Candidate pages are independent of history pages: use `candidate_limit` (1–200),
follow `candidates.next_offset` with `candidate_offset`, or filter `source_node_id`.
History keeps its separate `offset`/`limit`. Assistant exposes the same controls.
Narrow both limits if a response exceeds the transport/context budget. A full first
page does not mean later sources have no capabilities. Refresh after state changes;
nomination is current advice, and proposal admission rechecks the selected source
and exact hunk hashes independently of the displayed page. MCP rejects a page that
does not bind the requested filter/offset/limit or has inconsistent pagination.
Nominations use the same source eligibility as proposal admission: salvaged or
missing scores, reset lifecycles and invalid seed identities supply no hunks.
A retargeted extra metric cannot substitute for a missing primary task score.
Conversely, a measured primary source remains eligible when it is unranked on
the new objective because that extra metric is absent. The gate repeats the
declared primary evaluator; ranking and the current leader remain unchanged.
The receipt must bind integer node/generation/seed sequence identities and agree
with a complete stored seed event, including archive identity, counts and bytes.
That seed event must precede the current primary evaluation terminal.
Malformed source receipts remain diagnostic reads with no nomination; fresh
writes refuse them before work starts. Saved exact ACKs remain historical reads.

1. Pause through the ordinary durable command, then wait for engine exit. Every
   fresh upstream write requires a stopped engine; readings start no work.
2. Author `upstream_propose` with `{run_id, body}`. Retain the exact body:
   `expected_generation`, stable `action_id`, `source_node_id`,
   `expected_base_revision`, selected `hunk_hashes`, `files`/`deleted`, separate
   `recipe_files`/`recipe_deleted`, `summary`, `flag:{name,default,enabled}`,
   `documentation_path`, and `critic:{verdict:"pass",reason,reviewer}`.
   The flag preserves the prior default. Scoring boundaries remain protected.
   The whole saved candidate must match the original archive plus this exact
   approved patch. Do not amend a retained worktree or snapshot behind its request;
   unexpected additions, rewrites or deletions refuse publication before any gate.
   Inspect the failed claim and submit a fresh corrected body under a new action ID.
   New non-config helpers belong to the shared implementation too, even when they
   did not exist in the nominated source. Probes and source recipes cannot replace
   shared non-config patch files; calling a helper `documentation_path` grants no
   exception. Such masking refuses admission before a claim or worktree is created.
   The comparison rejects case aliases across overlays too (`RUNNER.py` versus
   `runner.py`). Fresh checks and advancement revalidate saved proposals, including
   ones admitted before an admission fix. Exact retries still return the original
   ACK; that historical receipt does not grant permission for a fresh action.
3. Explicitly call `upstream_check` with `expected_generation`, new `action_id` and
   `proposal_id`. This buys actual operator tests, old-recipe artifact regressions,
   paired full source repetitions and any required original repair trigger.
   Read `status`, `result`, execution costs and `evidence_token`; model prose never
   certifies a gate. No node score is rewritten by these separate executions.
4. Call `upstream_advance` only with a current passing gate, exact `evidence_token`
   and `expected_base_revision`. Its CAS changes future seeds. Resume explicitly;
   conflicting pending overlays retain their entire old verified base with a reason.
5. Continue checking ordinary harness progress/checkpoints/result notices. The new
   base does not waive enabled research/report/knowledge obligations. Reuse the
   shared runner and express scientific changes as recipes.

For repeated evidence, discover `phases(query="confirmation")` and
`phase_info("confirmation")`; `command:force_confirm` takes the terminal `node_id`
and its current `generation`. Confirmation retains that experiment's primary
measured base and scientific files, even after later advancements. A missing
historical archive refuses before workspace cleanup. It records full-profile seeds
without electing a robust winner. A new candidate or reset is a new lifecycle and
uses current-base migration. Seeding records the actual Node ID/generation; late
old-generation seeds and terminal repetitions cannot redefine a pending overlay's
basis. Legacy seed events remain readable without invented generation evidence.
The authoring basis comes from the creation event actually accepted by replay.
Ignored late or malformed creation responses cannot move it; reset and repair
retain it, while an accepted in-place recreation establishes a new basis.
Materialization checks the current pending lifecycle, manifest and available
authoring identity before workspace cleanup. A superseded, settled, aborted or
tombstoned snapshot refuses without replacing that workspace; reread the current
node before another attempt. Terminal confirmations still use measured provenance.
Capability absorption applies only to advancements after the overlay's recorded
base. An explicit reversal authored on an already advanced base remains an
experiment; an older promotion cannot erase it or inject its source recipe.
Three-way merging checks text compatibility, not scientific validity: inspect
the resulting evaluation, including failures requiring repair.

Exact body/action retries recover the original ACK without re-execution or fresh
evidence, including after resume. A started claim with no verdict is unresolved;
inspect its logs before asking the operator to abandon it through
`POST /api/runs/{run_id}/upstream/recover`. Recovery grants no pass or resume. A new
check requires a new action ID. Damaged event sources refuse even old ACK reads.
The initial private Git repository is built in a proposal-specific
`upstream/.git-init-PROPOSAL_ID` directory and published only after its base commit
and ref are ready. After process loss, inspect the retained staging directory and
resolve the claim explicitly; a fresh proposal uses a separate staging directory.
Recovery never deletes that evidence or adopts an unfinished repository. Existing
repositories damaged before this publication fix still require operator repair.
After operator abandonment, an exact retry of an unfinished proposal/check returns
`upstream_claim_abandoned`: inspect its history and use a new `action_id` for new
work. It does not ask for another abandonment or engine wait, and starts no work.
Changed bodies still conflict. Exact recovery ACKs and subsequently retained
terminal results remain historical reads, including while an engine is running;
they never restore an abandoned check's advancement authority. New actions still
require engine exit.
Typed MCP validates complete page/receipt identities, every passing gate leg,
paired full samples, their means/SEM/delta and corresponding executions and costs.
Statistical representation closeness never enlarges the declared scientific
tolerance: both passing and failed equivalence verdicts must agree with the
actual paired samples, including a zero tolerance. A contradictory rounded
verdict is unavailable evidence even when its receipt hash matches.
Nonfinite decoded numbers and overflowed total execution costs also supply no
verdict. A malformed check ACK returns unknown without its body; malformed history
is unavailable. Read the original receipt explicitly before recovery; validation
does not automatically retry the check. Finite extra result fields remain readable
and covered by the receipt hash.
Typed upstream pages also recompute the active CAS revision from the returned
generation, selector and advancement sequence. Visible advancements must agree
with that current base or precede it; older pages need not contain its current
advancement. A continuation requires a full history page, and a bounded candidate
list must reach its declared limit. Contradictory HTTP 200 is unavailable without
body; a valid empty terminal page remains readable. Reads perform no automatic
paging, retry or advancement.
Passing regressions require actual nonempty artifact receipts on both sides.
Before a fresh CAS the server also validates every launched probe and repetition,
the complete declared artifact sets, the operator's tolerance and source-score
reproduction, and exact agreement with the gate's recorded execution charges.
Those executions must lie between one matching current-generation gate start
and one completion. The start must bind the exact check body and result context.
An operator-abandoned claim cannot regain authority from a delayed completion;
inspect the named `events.jsonl` source and buy an explicit fresh check/action ID.
Shared evidence validation applies to direct HTTP and Assistant writes too;
a stored passing flag and matching hash cannot substitute for these checks.
An ACK must match the requested proposal and CAS evidence. A matching digest alone
does not certify complete evidence. Incomplete HTTP 200 is unavailable/unknown,
without a verdict body; read explicitly before recovery. A complete failed gate,
including partial measurements, remains readable and grants no advancement.

Full gate evaluations use the node pipeline's live `budget_extend{eval_timeout}`,
stall/divergence settings and `metric_subject` policy. Changing the effective
timeout after a gate requires a fresh check/action ID before CAS; exact retries
still recover the original evidence. Probe-specific timeouts remain operator
declared. These bounded gate executions do not open node monitor questions.

The selected interpreter's distribution identity is read under the same
run/task env merge and each validated operator-stage env override. A changed
distribution exposed through declared `PYTHONPATH` invalidates advancement even
when the server's own environment is unchanged. Identical environments share an
observation within one read; gate and CAS always read again, with a shared
30-second observation bound. Wrappers and undeclared dependency closures retain
the scope limits in doc 72. A `valid` execution cannot contain a timed-out or
failed stage; contradictory MCP evidence supplies no verdict. Genuine failed
executions remain readable diagnostics.

The scoped token may write this lane on external runs; internal runs require the
owner credential. Task declarations and interrupted-claim recovery are owner-only.
The UI Assistant uses the same lane with its ordinary approval policy. No hidden
Maintainer model job is launched. Support and bounds are documented in
[doc 72 §18](../72-node-to-framework-upstream-2026-10-01.md#18-upstream-lane).
The code-base panel distinguishes a recorded completed check, an unfinished
claim, operator abandonment and unavailable claim evidence. A late completion
cannot restore an abandoned check, and an older pass does not replace a newer
unfinished claim. Missing costs remain unavailable. Read current upstream
evidence before advancement; the panel's recorded result is not CAS approval.

The MCP adapter forwards JSON API requests and limits a response to 256 KiB and
a request body to 1 MiB. Query narrow routes for larger outputs; binary uploads
need their dedicated API or CLI flow. A dedicated harness token can read and
control launched runs but cannot change operator defaults (settings, prompts, skills,
knowledge), launch new runs, drive the owner assistant or the paid concept lens,
reset, purge or delete runs, or write the chat log the owner's TUI replays (reading
it stays open). Nor can it clean up the owner's work: clear a node's trace, resolve a
stuck command's activity claim (the fix a command refusal names for a record no
server can read — ask the operator), abandon a concept lens or a scope-report action,
revoke a share link, or delete a project or a super-task. Commands
that start internal agent work (fork, forced ablation,
deep research, a node reset from `propose` or `implement`, a code-less inject — judged
on what an import resolves to) are refused on a run launched with `external_harness`
for every credential. On an INTERNAL run served by the same UI the harness token may
only pause the run, annotate a node or add a comment, and is refused
(`agent_token_refused`, doc 70 item 70.8) everything else: a hint (every role reads
the run's hints as the operator's directives, the newest first), a resume, restart or
reopen, any inject or node reset, a node abort, a budget extension, a strategy, an
approval, an abort, a metric retarget, a promotion, a research memo or report, a
hypothesis or Card change, an edit or resolution of a comment, a retry of any of those
and an edit of the run's configuration. What it may submit is marked
`submitted_by: agent_token` on the command record. The operator,
with the owner's token, keeps everything. A run whose snapshot cannot be read refuses
the harness token those
commands. The HTTP API's `LOOPLAB_UI_TOKEN` grants full owner authority;
`harness-mcp` no longer uses it as a fallback. Keep it out of the agent environment.
Agent reasoning and model token cost
happen outside LoopLab's ledger; the event log records the submitted candidate,
measured execution and command receipts, not external provider billing.

HyperResearch's discoverable workflow and OpenResearch's experiment workspace
informed the interface. LoopLab uses its own event log, candidate snapshots and
metric provenance as the durable center.
