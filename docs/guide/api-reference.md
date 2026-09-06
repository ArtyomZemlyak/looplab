# HTTP API reference

**Generated** from the server's own OpenAPI schema by `python -m looplab.serve.api_reference` and
pinned by `tests/test_api_reference.py` (doc 52 row 25): a route that lands without a regenerated
page is a red test, so the surface cannot grow undocumented. Every row is `(method, path,
deprecated)` plus the handler's docstring first line and its declared response model — edit the
handler, not this page. The live schema is served at `/openapi.json`; the interactive form at
`/docs`. Refusal codes are `serve/http.py::REFUSALS` (`docs/guide/ui.md`), and the control
vocabulary a client may append is `serve/protocol.py::CONTROL_EVENTS`.

<!-- generated: api routes -->

137 routes on 123 paths; 2 deprecated; 25 with a declared response model.

### `/`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/` | *Index Placeholder* (no docstring) | — |  |

### `/api`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/{kind}` | *List Author* (no docstring) | — |  |
| `PUT` | `/api/{kind}/{name}` | *Write Author* (no docstring) | — |  |

### `/api/assistant`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/assistant/commands` | *Assistant Commands* (no docstring) | — |  |
| `GET` | `/api/assistant/permissions` | *Assistant Permissions* (no docstring) | — |  |
| `POST` | `/api/assistant/permissions/{req_id}` | *Assistant Resolve* (no docstring) | — |  |
| `GET` | `/api/assistant/progress` | *Assistant Progress* (no docstring) | — |  |
| `POST` | `/api/assistant/revert` | Undo one exact assistant change while its applied post-image is still current. | — |  |
| `GET` | `/api/assistant/sessions` | *Assistant Sessions* (no docstring) | — |  |
| `POST` | `/api/assistant/sessions` | *Assistant Create* (no docstring) | — |  |
| `GET` | `/api/assistant/sessions/{sid}` | *Assistant Get* (no docstring) | — |  |
| `DELETE` | `/api/assistant/sessions/{sid}` | *Assistant Delete* (no docstring) | — |  |
| `POST` | `/api/assistant/sessions/{sid}/cancel` | Stop an in-flight turn. Sets the session's cancel flag; the tool loop checks it at the next | — |  |
| `POST` | `/api/assistant/sessions/{sid}/fork` | *Assistant Fork* (no docstring) | — |  |
| `GET` | `/api/assistant/sessions/{sid}/fork/{action_id}` | *Assistant Fork Status* (no docstring) | — |  |
| `POST` | `/api/assistant/sessions/{sid}/message` | One assistant turn. Persists the user turn, drives the read-only tool loop as a BACKGROUND | — |  |
| `POST` | `/api/assistant/sessions/{sid}/message_stream` | Streaming variant: SSE of `token` (final-answer tokens), `step`, `todos`, then `done` (the | — |  |
| `POST` | `/api/assistant/sessions/{sid}/share` | Mint a read-only share link. | — |  |
| `DELETE` | `/api/assistant/sessions/{sid}/share` | Revoke every link for this chat. Revocation exists so taking a share back does not mean | — |  |
| `GET` | `/api/assistant/sessions/{sid}/shares` | The owner's view of this chat's live links — never the tokens, only their terms. | — |  |
| `GET` | `/api/assistant/shared` | Header-carried public capability. | — |  |
| `GET` | `/api/assistant/watches` | *List Watches* (no docstring) | — |  |
| `POST` | `/api/assistant/watches` | Arm standing status/schedule/work from the UI (the agent uses the corresponding watch | — |  |
| `DELETE` | `/api/assistant/watches/{watch_id}` | *Stop Watch* (no docstring) | — |  |

### `/api/attention`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/attention` | *Attention* (no docstring) | — |  |

### `/api/auth`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/auth/status` | *Auth Status* (no docstring) | — |  |
| `POST` | `/api/auth/verify` | *Auth Verify* (no docstring) | — |  |

### `/api/cross-run`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/cross-run/atlas` | Live structured Research Atlas projection, bounded per section. | `CrossRunAtlasResponse` |  |
| `GET` | `/api/cross-run/claim-curation-log` | *Claim Curation Log* (no docstring) | `CurationLogResponse` |  |
| `POST` | `/api/cross-run/claim-decide` | Ratify/reject/pin/clear exactly the claim ID and revision the operator observed. | `ClaimDecisionResponse` |  |
| `POST` | `/api/cross-run/claim-steward` | Run a proposal-only claim review; typed operator actions apply selected proposals. | `StewardProposalResponse` |  |
| `GET` | `/api/cross-run/claims` | Scope/polarity-safe claims with stable IDs and bounded offset pagination. | `CrossRunClaimsResponse` |  |
| `POST` | `/api/cross-run/concept-alias-clear` | Undo the current alias/purge policy without deleting its audit history. | `ConceptAliasResponse` |  |
| `POST` | `/api/cross-run/concept-merge` | Merge one non-empty concept into another; purge is a separate confirmed action. | `ConceptAliasResponse` |  |
| `GET` | `/api/cross-run/concept-policy` | The alias/split registry as a lookup table a browser can APPLY, plus who is in memory. | `CrossRunConceptPolicyResponse` |  |
| `POST` | `/api/cross-run/concept-purge` | Explicitly tombstone one concept after a typed confirmation. | `ConceptAliasResponse` |  |
| `POST` | `/api/cross-run/concept-split` | Record one bounded deterministic split rule set. | `ConceptSplitResponse` |  |
| `POST` | `/api/cross-run/concept-split-clear` | Undo the active split while preserving the append-only history. | `ConceptSplitResponse` |  |
| `POST` | `/api/cross-run/concept-steward` | Run a proposal-only taxonomy review; typed operator actions apply selected proposals. | `StewardProposalResponse` |  |
| `GET` | `/api/cross-run/curation-log` | *Curation Log* (no docstring) | `CurationLogResponse` |  |

### `/api/genesis`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `POST` | `/api/genesis` | Pre-run BOSS: turn a one-line goal into an editable run spec (name + task + key settings). | — |  |
| `GET` | `/api/genesis/{job_id}` | Poll a pending genesis plan (the agentic loop runs in the background so a slow model doesn't | — |  |

### `/api/gpu`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/gpu` | *Gpu* (no docstring) | — |  |

### `/api/health`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/health` | P1-3 zero-model liveness: the ONE /api/ route that stays open without a UI token, so a | — |  |

### `/api/jobs`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/jobs/{job_id}` | Poll a generic background job (see _run_as_job): `running` until done, then the result dict | — |  |

### `/api/llm`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `POST` | `/api/llm/health` | One revision-fenced, idempotent and output-capped provider reachability mutation. | — |  |

### `/api/memory`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/memory` | *Memory* (no docstring) | — |  |

### `/api/operations`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/{kind}/{name}/operations/{operation_id}` | *Get Author Operation* (no docstring) | `AuthoringOperationResponse` |  |
| `PUT` | `/api/{kind}/{name}/operations/{operation_id}` | *Write Author Operation* (no docstring) | `AuthoringOperationResponse` |  |

### `/api/projects`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/projects` | *List Projects* (no docstring) | — |  |
| `POST` | `/api/projects` | *Create Project* (no docstring) | — |  |
| `DELETE` | `/api/projects/{pid}` | *Delete Project* (no docstring) | — |  |
| `PATCH` | `/api/projects/{pid}` | *Patch Project* (no docstring) | — |  |

### `/api/research`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `POST` | `/api/research` | DEPRECATED. Best-effort LLM brief for a research topic, to prime a run. Optionally saved | — | yes |

### `/api/review`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/review` | Resolve the credential carried by the tokenless review SPA. | — |  |
| `GET` | `/api/review/comments` | Current, redacted comments only; review capabilities never expose prior revisions. | — |  |
| `GET` | `/api/review/config` | *Review Config* (no docstring) | — |  |
| `GET` | `/api/review/cost` | *Review Cost* (no docstring) | — |  |
| `GET` | `/api/review/nodes/{nid}` | Opt-in evidence projection: source/results, redacted, never live trace sidecars. | — |  |
| `GET` | `/api/review/nodes/{nid}/metrics` | *Review Node Metrics* (no docstring) | — |  |
| `GET` | `/api/review/state` | Return the review-safe state with the same bounded Cards fragment as owner/SSE state. | — |  |

### `/api/runs`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/runs` | *List Runs* (no docstring) | — |  |
| `DELETE` | `/api/runs/{run_id}` | Never let a bodyless request delete an uninspected replacement generation. | — |  |
| `PATCH` | `/api/runs/{run_id}` | Set/clear a run's UI display label. Non-destructive: the run dir id is unchanged. | — |  |
| `GET` | `/api/runs/{run_id}/agents_md` | DEPRECATED. Serve a run's AGENTS.md. | — | yes |
| `GET` | `/api/runs/{run_id}/artifact` | Serve ONE artifact's content for inline viewing. `root` must be one of the ids returned by | — |  |
| `GET` | `/api/runs/{run_id}/artifacts` | List files currently visible to the run, grouped by root. | — |  |
| `GET` | `/api/runs/{run_id}/cards/{card_id}/trace` | One CARD's whole story: the research that proposed it, then every node it produced. | — |  |
| `POST` | `/api/runs/{run_id}/chat` | Advisory chat grounded on a run (and optionally one experiment node). Read-only — it | — |  |
| `POST` | `/api/runs/{run_id}/chat-compact` | Summarize a stretch of older chat turns into ONE tight recap, so the boss's working memory | — |  |
| `GET` | `/api/runs/{run_id}/chat-log` | The saved chat turns for this run, in order ({role:'user'\|'assistant'\|'action', …}). | — |  |
| `POST` | `/api/runs/{run_id}/chat-log` | Append ONE chat turn (the verbatim feed entry: role/content/trace or role/action/status) | — |  |
| `POST` | `/api/runs/{run_id}/command` | Action-router (Workstream C): turn a free-text instruction into EITHER a concrete control | — |  |
| `POST` | `/api/runs/{run_id}/commands` | *Submit Command* (no docstring) | `RunCommandRecord` |  |
| `GET` | `/api/runs/{run_id}/commands/{command_id}` | *Get Command* (no docstring) | `RunCommandRecord` |  |
| `POST` | `/api/runs/{run_id}/commands/{command_id}/retry` | *Retry Command* (no docstring) | `RunCommandRecord` |  |
| `GET` | `/api/runs/{run_id}/comments` | *List Comments* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/comments/{comment_id}/history` | *Comment History* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/concepts` | Return one versioned, bounded, generation-bound ConceptFrame. | — |  |
| `POST` | `/api/runs/{run_id}/concepts/lens` | Create one generation-bound derived lens behind a durable paid-work claim. | — |  |
| `POST` | `/api/runs/{run_id}/concepts/lens/abandon` | Explicitly terminalize an orphaned/uncertain paid claim without provider retry. | — |  |
| `GET` | `/api/runs/{run_id}/concepts/lens/recovery` | Discover current-generation paid work after the browser loses its private receipt. | — |  |
| `POST` | `/api/runs/{run_id}/concepts/lens/recovery/abandon` | Resolve one exactly identified orphan without possessing or replaying its paid key. | — |  |
| `GET` | `/api/runs/{run_id}/config` | *Run Config* (no docstring) | `RunConfigResponse` |  |
| `PUT` | `/api/runs/{run_id}/config` | Per-run settings edit: rewrite THIS run's config.snapshot.json so a later RESUME re-enters | `RunConfigUpdateResponse` |  |
| `POST` | `/api/runs/{run_id}/control` | *Control* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/cost` | *Run Cost* (no docstring) | — |  |
| `POST` | `/api/runs/{run_id}/deletions` | Delete one exact run generation through an operation-bound durable transaction. | — |  |
| `GET` | `/api/runs/{run_id}/deletions/{operation_id}` | *Observe Run Deletion* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/events` | Stream canonical public state frames, including the Cards completeness receipt. | — |  |
| `GET` | `/api/runs/{run_id}/lifecycle` | Bounded identity/liveness probe used after a terminal SSE stream closes. | — |  |
| `GET` | `/api/runs/{run_id}/log` | Raw event envelopes (for the activity feed + event/span explorer). `since` = exclusive | — |  |
| `GET` | `/api/runs/{run_id}/log-page` | Bounded timeline transport. Cursors survive append and fail closed across run reset. | — |  |
| `GET` | `/api/runs/{run_id}/memory-attribution` | What a cascading delete WOULD remove from cross-run memory, and what it would keep. | — |  |
| `POST` | `/api/runs/{run_id}/memory-purge` | Finish a cascade whose store was locked at the moment the run was deleted. | — |  |
| `GET` | `/api/runs/{run_id}/nodes/{nid}` | *Node Detail* (no docstring) | — |  |
| `POST` | `/api/runs/{run_id}/nodes/{nid}/clear_trace` | Erase ONE node's spans from spans.jsonl — the "clear this node's trace" button. spans.jsonl | — |  |
| `GET` | `/api/runs/{run_id}/nodes/{nid}/conversation` | The node's trace as a LINEAR, de-duplicated conversation: the system+user request shown | — |  |
| `GET` | `/api/runs/{run_id}/nodes/{nid}/episodes` | THE MAP of one node's trace: every episode (band) it recorded, with none of their contents. | — |  |
| `GET` | `/api/runs/{run_id}/nodes/{nid}/logs` | Live training/eval logs for a node — the streamed stdout/stderr of its eval + setup | — |  |
| `GET` | `/api/runs/{run_id}/nodes/{nid}/metrics` | Online metric SERIES a node's training logged — every scalar (loss, each recall@k, grad | — |  |
| `GET` | `/api/runs/{run_id}/nodes/{nid}/trace` | The LIGHT trace tree for ONE node — the hot path for expanding a node's trace card. Reads | — |  |
| `POST` | `/api/runs/{run_id}/project` | *Assign Run* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/prov` | W3C-PROV-style provenance of the search DAG: each node's solution is an entity | — |  |
| `POST` | `/api/runs/{run_id}/report_refresh` | Force a high-quality regeneration of the agent-authored run report NOW. Appends a | — |  |
| `POST` | `/api/runs/{run_id}/reset` | round-7 "Replay": reset a run IN PLACE — archive its event log + spans + node workspaces and | — |  |
| `POST` | `/api/runs/{run_id}/resolve-activity-claims` | Guarded operator recovery for an ownership claim that cannot be proven dead. | — |  |
| `POST` | `/api/runs/{run_id}/resume` | *Resume Run* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/reviews` | *List Reviews* (no docstring) | — |  |
| `POST` | `/api/runs/{run_id}/reviews` | *Create Review* (no docstring) | — |  |
| `DELETE` | `/api/runs/{run_id}/reviews/{link_id}` | *Revoke Review* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/spans/{sid}` | Bounded, redacted I/O projection for one observation; raw diagnostics stay in spans.jsonl. | — |  |
| `GET` | `/api/runs/{run_id}/state` | Return the bounded public run state. | `PublicRunStateResponse` |  |
| `POST` | `/api/runs/{run_id}/suggest` | Turn the chat discussion (or a free-form instruction) into a CONCRETE experiment idea | — |  |
| `POST` | `/api/runs/{run_id}/supertask` | *Assign Supertask* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/trace` | *Trace* (no docstring) | — |  |
| `GET` | `/api/runs/{run_id}/trace/by_trace/{trace_id}` | Spans of ONE operation's trace (by trace_id) as a tree, WITH capped I/O — powers the | — |  |
| `GET` | `/api/runs/{run_id}/trace/by_trace/{trace_id}/conversation` | ONE operation's trace as the LINEAR conversation, the twin of `/nodes/{nid}/conversation`. | — |  |
| `GET` | `/api/runs/{run_id}/trace/tail` | LIVE 'what is the agent doing right now' feed: the most recent generation (LLM thinking/ | — |  |

### `/api/scope-report`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/scope-report/{scope_type}/{scope_id}` | *Get Scope Report* (no docstring) | — |  |
| `POST` | `/api/scope-report/{scope_type}/{scope_id}/generate` | Generate (or regenerate) the cross-run report for a scope. On-demand only — the agent reads | — |  |

### `/api/scope-report-actions`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/scope-report-actions/{action_id}` | *Get Scope Report Action* (no docstring) | — |  |
| `POST` | `/api/scope-report-actions/{action_id}/abandon` | Explicitly release an indeterminate paid-action fence without erasing its identity. | — |  |

### `/api/settings`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/settings` | *Get Settings* (no docstring) | `SettingsSnapshotResponse` |  |
| `PUT` | `/api/settings` | *Put Settings* (no docstring) | `SettingsUpdateResponse` |  |
| `GET` | `/api/settings/schema/{version}` | Revalidated display metadata for the versioned Settings/Config form contract. | `SettingsUISchemaResponse` |  |
| `PUT` | `/api/settings/secret` | Store (or clear) a secret credential securely. The value is written owner-only to | `SecretUpdateResponse` |  |

### `/api/start`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `POST` | `/api/start` | *Start Run* (no docstring) | — |  |
| `POST` | `/api/start/preflight` | Validate and resolve a launch without writing, reserving a name, or starting an engine. | — |  |
| `POST` | `/api/start/{run_id}/resolve-claim` | Operator recovery for a crash-window claim whose child identity cannot be proven. | — |  |
| `GET` | `/api/start/{run_id}/status` | Observe one exact durable startup. GET never launches or resumes an engine. | — |  |

### `/api/supertasks`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/supertasks` | *List Supertasks* (no docstring) | — |  |
| `POST` | `/api/supertasks` | *Create Supertask* (no docstring) | — |  |
| `DELETE` | `/api/supertasks/{sid}` | *Delete Supertask* (no docstring) | — |  |
| `PATCH` | `/api/supertasks/{sid}` | *Patch Supertask* (no docstring) | — |  |

### `/api/tasks`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `GET` | `/api/tasks` | Discover runnable task JSON files (the `examples/` catalogue by default, plus any in the | — |  |

### `/api/validate`

| method | path | summary | response model | deprecated |
|---|---|---|---|---|
| `POST` | `/api/validate` | Is this launch proposal launchable, and if not, why — the same `preflight_start` funnel | — |  |

<!-- /generated -->
