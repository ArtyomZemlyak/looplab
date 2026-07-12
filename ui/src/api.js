// The UI's server API: the fetch client (get/post/send wrappers + auth/prefix plumbing), the generic
// background-job await, every /api/* endpoint function, and the CONTROL action map. Split out of
// util.js (mega-refactor P5.2 — bodies verbatim); util.js re-exports everything, so importers are
// unchanged.

export const CONTROL = {
  // Three operator controls (see docs/guide/concepts.md → "Stopping a run"):
  //   stop     — freeze the run, NO finalization (event: pause). Resumable; finalize later if wanted.
  //   finalize — stop AND wrap up (report / cross-run lessons+case / cost roll-up). event: run_abort.
  //   resume   — continue from ANY stopped state (pause / finalize / natural finish). event: resume.
  stop: (rid) => post(`/api/runs/${rid}/control`, { type: 'pause', data: {} }),
  finalize: (rid) => post(`/api/runs/${rid}/control`, { type: 'run_abort', data: { reason: 'finalized' } }),
  resume: (rid) => post(`/api/runs/${rid}/control`, { type: 'resume', data: {} }),
  // back-compat aliases (older callers / NL control): pause≡stop, abort≡finalize, reopen≡resume.
  pause: (rid) => post(`/api/runs/${rid}/control`, { type: 'pause', data: {} }),
  abort: (rid) => post(`/api/runs/${rid}/control`, { type: 'run_abort', data: { reason: 'finalized' } }),
  nodeAbort: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'node_abort', data: { node_id: id, reason: 'ui' } }),
  // Re-run an existing node IN PLACE from a stage (no new node): eval=re-score (keep code),
  // implement=re-run the Developer (keep the idea), propose=full redo. Resume the run to apply.
  resetNode: (rid, id, stage) => post(`/api/runs/${rid}/control`, { type: 'node_reset', data: { node_id: id, from_stage: stage } }),
  approve: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'approval_granted', data: { node_id: id } }),
  ratify: (rid) => post(`/api/runs/${rid}/control`, { type: 'spec_approved', data: {} }),
  hint: (rid, text) => post(`/api/runs/${rid}/control`, { type: 'hint', data: { text } }),
  budget: (rid, sec) => post(`/api/runs/${rid}/control`, { type: 'budget_extend', data: { max_eval_seconds: sec } }),
  forceConfirm: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'force_confirm', data: { node_id: id } }),
  forceAblate: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'force_ablate', data: { node_id: id } }),
  fork: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'fork', data: { from_node_id: id } }),
  annotate: (rid, id, text) => post(`/api/runs/${rid}/control`, { type: 'annotation', data: { node_id: id, text } }),
  promote: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'promote', data: { node_id: id, alias: 'champion' } }),
  // Operator-authored experiment: hand-add a node to the search tree. `idea` = {operator, params,
  // rationale, theme?}; optional parent_id (branch from a node) and code (ship ready-made code).
  inject: (rid, { idea, parent_id = null, code = null }) =>
    post(`/api/runs/${rid}/control`, { type: 'inject_node', data: { idea, parent_id, code } }),
  reopen: (rid) => post(`/api/runs/${rid}/control`, { type: 'run_reopened', data: {} }),
  // U3: merge two nodes — inject a multi-parent `merge` node; the engine recombines the parents'
  // solutions via its real merge/ensemble operator (not a blank manual node).
  merge: (rid, ids) => post(`/api/runs/${rid}/control`, { type: 'inject_node', data: {
    idea: { operator: 'merge', rationale: `merge ${ids.map(i => '#' + i).join(' + ')}` }, parent_ids: ids } }),
  // A7: pin/override the Strategist's choice live (HITL parity). `strategy` = a Strategy dict
  // {policy?, policy_params?, developer?, operators?, fidelity?, rationale?}.
  setStrategy: (rid, strategy) => post(`/api/runs/${rid}/control`, { type: 'set_strategy', data: { strategy } }),
  // P2: ask the engine to run the Deep-Research stage now (read all results + the web, write a memo).
  deepResearch: (rid) => post(`/api/runs/${rid}/control`, { type: 'deep_research', data: {} }),
  // P1: register an open hypothesis on the board (a question the search should resolve), or drop one.
  addHypothesis: (rid, statement) => post(`/api/runs/${rid}/control`, { type: 'hypothesis_added', data: { statement, source: 'human' } }),
  abandonHypothesis: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'hypothesis_updated', data: { id, status: 'abandoned' } }),
  deleteHypothesis: (rid, id) => post(`/api/runs/${rid}/control`, { type: 'hypothesis_updated', data: { id, status: 'deleted' } }),
  // Workstream A: force a high-quality regeneration of the agent-authored run report now. Dedicated
  // endpoint (not /control) — appends a `report_generated` event. Runs as a background job, so we
  // jobAwait the response (a slow/large regen can't 504 behind a proxy; a fast one returns inline).
  // Contract preserved: resolves to {ok, seq, content} (or {ok:false} offline), never a job_id.
  refreshReport: async (rid) => jobAwait(await post(`/api/runs/${rid}/report_refresh`, {})),
  // Workstream C: a generic control append by {type, data} — the single execution path every chat
  // action funnels through (slash commands and the LLM action-router both produce {type, data}).
  raw: (rid, type, data = {}) => post(`/api/runs/${rid}/control`, { type, data }),
}

// Workstream C: chat actions on a FINISHED run must reopen + re-enter the loop so the engine actually
// processes them (mirrors InjectModal's reopen→inject→resume). These verbs need the loop running.
// `budget_extend` is here too: raising the node budget on a finished run is pointless unless the run
// reopens and keeps going (the agentic boss pairs it with inject/hint steps).
// Actions whose effect only takes hold once the engine is (re)spawned on a stopped/finished run.
// run_abort = FINALIZE: the wrap-up (report/lessons/cost) needs the engine to fold stop_requested
// into run_finished; resume needs it to keep going. (Twin of tui.py _NEEDS_RESUME.)
// arch-review §4 P1-10: approval_granted/spec_approved/node_reset/run_reopened also only take hold once
// the engine re-enters a finished/zombie run — keep this in step with tui_format.py::_NEEDS_RESUME.
const NEEDS_RESUME = new Set(['fork', 'inject_node', 'force_confirm', 'force_ablate', 'deep_research', 'set_strategy', 'budget_extend', 'resume', 'run_abort', 'approval_granted', 'spec_approved', 'node_reset', 'run_reopened'])

// Does applying this action on a FINISHED run require reopening + resuming the engine? (Used to batch a
// multi-action plan: append every step's intent, then reopen+resume ONCE if any step needs the loop.)
export const actionNeedsEngine = (action) => NEEDS_RESUME.has(action?.type)

// Append ONE action's control intent WITHOUT reopening/resuming — the building block for applying an
// agentic plan as a batch (append every step, then resume once at the end). `__refresh_report__` is
// the report-refresh special case (its own endpoint, never the engine loop).
export async function appendAction(runId, action) {
  if (action.type === '__refresh_report__') return CONTROL.refreshReport(runId)
  return CONTROL.raw(runId, action.type, action.data || {})
}

// Re-enter the engine loop on an existing run dir (used to continue a finished run after an inject).
export const resumeRun = (rid) => post(`/api/runs/${encodeURIComponent(rid)}/resume`, {})

// round-7 "Replay": reset a run IN PLACE — the server archives its event log + spans and re-spawns a
// fresh run on the same run-id. Only offered on a FINISHED run (no live engine), so it's race-free.
export const resetRun = (rid) => post(`/api/runs/${encodeURIComponent(rid)}/reset`, {})

// Clear ONE node's trace: erase its spans from spans.jsonl so a reset+rebuild's fresh bands don't
// stack on top of the old attempt's. Server refuses (409) while the engine is live (sole writer).
export const clearNodeTrace = (rid, id) =>
  post(`/api/runs/${encodeURIComponent(rid)}/nodes/${id}/clear_trace`, {})

export const llmHealth = () => get('/api/llm/health')

// G1 server auth: when the server runs with LOOPLAB_UI_TOKEN it injects the token into the served
// page as <meta name="ll-token">. A *cross-origin* page can't read it (that's per-origin SOP), but a
// SAME-origin page on a different path CAN — so the token only isolates users when each has its own
// origin (default 127.0.0.1 bind, or a per-user subdomain), NOT on a shared jupyter-server-proxy
// origin where it's a per-deployment secret (server injects it only on top-level navigations + sets
// X-Frame-Options/no-store; see looplab/server.py and docs/guide/deployment.md). Send it on every
// mutating request. No token (default local) -> header omitted, behaviour unchanged.
const _authHeaders = (base) => {
  const t = (typeof document !== 'undefined' && document.querySelector('meta[name="ll-token"]')?.content) || ''
  return t ? { ...base, 'X-LoopLab-Token': t } : { ...base }
}
// Surface the server's error DETAIL (FastAPI puts the human-readable reason in `detail`) instead of a
// bare status code — so e.g. a 422 from a per-run config save reads "invalid settings — n_seeds: …"
// in the toast rather than just "422". Falls back to status when there's no JSON body.
async function _throw(r, path) {
  let detail = ''
  try { const j = await r.json(); detail = (j && (j.detail || j.error)) || '' } catch { /* no body */ }
  const err = new Error(detail ? String(detail) : `${path}: ${r.status}`)
  err.status = r.status   // callers branch on the code (e.g. 409 = run live / name taken), not a regex on the message
  throw err
}

// Path-mounting-proxy support. The UI may be served under a prefix (JupyterHub
// `/user/<name>/proxy/8765/`, a reverse-proxy subpath, …) rather than at the domain root, so an
// absolute `/api/…` would hit the proxy host's root and miss the backend. We route every request
// through apiUrl(), which prepends the prefix the page itself was served from. Routing is hash-based
// (`#/run/…`), so location.pathname is exactly that prefix; the proxy strips it before forwarding,
// so the backend still sees `/api/…`. At the root (local `looplab ui`) the prefix is '' — unchanged.
export function apiPrefix() {
  if (typeof location === 'undefined') return ''
  return location.pathname.replace(/\/index\.html$/, '').replace(/\/+$/, '')
}
export const apiUrl = (path) => apiPrefix() + path

export async function get(path) {
  // Carry the UI token on reads too: most GETs don't need it, but the artifact routes (raw file
  // content) are token-gated server-side. _authHeaders is a no-op when no token is set (local), so
  // ordinary local use is unchanged.
  const r = await fetch(apiUrl(path), { headers: _authHeaders({}) })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
export async function post(path, body) {
  const r = await fetch(apiUrl(path), { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify(body) })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
export async function putText(path, text) {
  const r = await fetch(apiUrl(path), { method: 'PUT', headers: _authHeaders({ 'Content-Type': 'text/plain' }), body: text })
  if (!r.ok) await _throw(r, path)
  return r.json()
}
async function send(path, method, body) {
  // Only attach a JSON body for methods that carry one (PATCH/PUT/POST). A DELETE with a request
  // body + Content-Type is unusual and some reverse proxies (e.g. jupyter-server-proxy) mishandle it
  // — which surfaced as a 500 on "delete chat"/"delete run". DELETE goes bodyless.
  const hasBody = method !== 'DELETE' && method !== 'GET'
  const opts = { method, headers: _authHeaders(hasBody ? { 'Content-Type': 'application/json' } : {}) }
  if (hasBody) opts.body = JSON.stringify(body || {})
  const r = await fetch(apiUrl(path), opts)
  if (!r.ok) await _throw(r, path)
  return r.json()
}

// ---- ClearML-style project API ----
export const listProjects = () => get('/api/projects')
export const createProject = (name, parent_id = null) => post('/api/projects', { name, parent_id })
export const patchProject = (id, body) => send(`/api/projects/${id}`, 'PATCH', body)
export const deleteProject = (id) => send(`/api/projects/${id}`, 'DELETE')
export const assignRun = (runId, project_id) => post(`/api/runs/${encodeURIComponent(runId)}/project`, { project_id })
export const renameRun = (runId, label) => send(`/api/runs/${encodeURIComponent(runId)}`, 'PATCH', { label })
export const deleteRun = (runId) => send(`/api/runs/${encodeURIComponent(runId)}`, 'DELETE')

// super-tasks: a user-managed, flat grouping of runs by the global task they attack (parallel axis
// to projects). create / rename / delete the bucket, then assign any run (existing or new) to it.
export const listSupertasks = () => get('/api/supertasks')
export const createSupertask = (name, task_id = null) => post('/api/supertasks', { name, task_id })
export const renameSupertask = (id, name) => send(`/api/supertasks/${id}`, 'PATCH', { name })
export const deleteSupertask = (id) => send(`/api/supertasks/${id}`, 'DELETE')
export const assignSupertask = (runId, supertask_id) => post(`/api/runs/${encodeURIComponent(runId)}/supertask`, { supertask_id })

export const gpuStat = () => get('/api/gpu')

// ---- settings + run launch ----
export const getSettings = () => get('/api/settings')
export const saveSettings = (settings) => send('/api/settings', 'PUT', { settings })
// Store (or clear, value='') a secret credential. Goes to the dedicated owner-only secret store,
// NOT ui_settings.json — the value is never echoed back (the GET reports it only as masked "***").
export const saveSecret = (key, value) => send('/api/settings/secret', 'PUT', { key, value })
// Per-run settings: edit a specific run's config.snapshot.json so the next RESUME picks up the
// change (only changed fields are sent). Blocked server-side while the run's engine is live.
export const saveRunConfig = (rid, settings) => send(`/api/runs/${encodeURIComponent(rid)}/config`, 'PUT', { settings })
export const startRun = (body) => post('/api/start', body)

// cross-run aggregate reports over a scope (project | task | supertask). GET returns the stored report
// + staleness ({exists, content, generated_at, run_ids, stale, added, current_run_count}); generate
// (re)synthesizes on demand via an agent with access to every run in the scope.
const _scopeUrl = (type, id) => `/api/scope-report/${encodeURIComponent(type)}/${encodeURIComponent(id)}`
export const getScopeReport = (type, id) => get(_scopeUrl(type, id))
// Generic background-job poll: the server hands back {status:'running', job_id}
// for slow work so it can't 504 behind a proxy. Returns the final result dict; tolerates transient
// poll errors. `resp` that's already a result (fast inline path) is returned unchanged.
const _job = (jobId) => get(`/api/jobs/${encodeURIComponent(jobId)}`)
export async function jobAwait(resp, { intervalMs = 1500, timeoutMs = 600000 } = {}) {
  if (!resp || resp.status !== 'running' || !resp.job_id) return resp
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, intervalMs))
    let j
    try { j = await _job(resp.job_id) } catch { continue }   // transient — keep polling
    if (j.status === 'done') return j
    if (j.status === 'unknown') return { ok: false, error: 'the job expired — try again' }
  }
  return { ok: false, error: 'timed out' }
}
// Cross-run synthesis can read many runs + drive an agent, so it runs as a background job; await it to
// completion and surface a hard failure as a throw (the panel's catch shows it), preserving the old
// "returns the final record" contract for callers.
export async function genScopeReport(type, id) {
  const r = await jobAwait(await post(`${_scopeUrl(type, id)}/generate`, {}))
  if (r && r.ok === false && r.error) throw new Error(r.error)
  return r
}

// ---- assistant (general chat agent — the evolution of Genesis) ----
export const assistantSessions = () => get('/api/assistant/sessions')
export const assistantCreate = (title = '', mode = 'plan') => post('/api/assistant/sessions', { title, mode })
export const assistantGet = (sid) => get(`/api/assistant/sessions/${encodeURIComponent(sid)}`)
export const assistantDelete = (sid) => send(`/api/assistant/sessions/${encodeURIComponent(sid)}`, 'DELETE')
export const assistantFork = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/fork`, {})
// Streaming turn: POST and read the SSE stream, invoking callbacks for token/step/todos/done/error.
// Real token streaming of the final answer (Claude-Desktop feel). Returns the final result dict.
export async function assistantMessageStream(sid, instruction, mode, cbs = {}, signal, display = null) {
  const r = await fetch(apiUrl(`/api/assistant/sessions/${encodeURIComponent(sid)}/message_stream`),
    { method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(display && display !== instruction ? { instruction, mode, display } : { instruction, mode }), signal })
  if (!r.ok || !r.body) { await _throw(r, 'message_stream'); return null }
  const reader = r.body.getReader(); const dec = new TextDecoder()
  let buf = ''; let result = null
  for (;;) {
    let chunk
    try { chunk = await reader.read() } catch { break }   // aborted (unmount) — stop cleanly
    const { done, value } = chunk
    if (done) break
    buf += dec.decode(value, { stream: true })
    let i
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, i); buf = buf.slice(i + 2)
      let ev = 'message'; let data = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      let parsed; try { parsed = JSON.parse(data) } catch { parsed = data }
      if (ev === 'token') cbs.onToken && cbs.onToken(parsed)
      else if (ev === 'text') cbs.onText && cbs.onText(parsed)
      else if (ev === 'step') cbs.onStep && cbs.onStep(parsed)
      else if (ev === 'todos') cbs.onTodos && cbs.onTodos(parsed)
      else if (ev === 'error') { cbs.onError && cbs.onError(parsed); result = { ok: false, error: parsed } }
      else if (ev === 'done') { result = parsed; cbs.onDone && cbs.onDone(parsed) }
    }
  }
  return result
}
// Full (uncapped) I/O for one trace observation — fetched lazily when the user expands a
// generation/tool in the trace tree (the tree itself is served light, without prompts/outputs).
export const spanDetail = (runId, spanId) =>
  get(`/api/runs/${encodeURIComponent(runId)}/spans/${encodeURIComponent(spanId)}`)

// Linear, de-duplicated conversation view of a node's trace (request once per sub-loop, then each
// generation's delta interleaved with tool calls) — the readable alternative to the raw span tree.
export const nodeConversation = (runId, nid) =>
  get(`/api/runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nid)}/conversation`)

// Stop an in-flight assistant turn server-side (survives a page reload, unlike aborting the local
// stream). Also used to poll whether a turn is still running (reattach after switch/reload).
export const assistantCancel = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/cancel`, {})
export const assistantProgress = (sid) => get(`/api/assistant/progress?session=${encodeURIComponent(sid)}`)

export const assistantCommands = () => get('/api/assistant/commands')
export const assistantRevert = (path) => post('/api/assistant/revert', { path })
export const assistantShare = (sid) => post(`/api/assistant/sessions/${encodeURIComponent(sid)}/share`, {})
// Pending human-in-the-loop confirm requests for a session, and resolving one.
export const assistantPermissions = (sid) => get(`/api/assistant/permissions?session=${encodeURIComponent(sid)}`)
export const assistantResolve = (reqId, decision) =>
  post(`/api/assistant/permissions/${encodeURIComponent(reqId)}`, { decision })
