const record = value => value && typeof value === 'object' && !Array.isArray(value)
const text = value => typeof value === 'string' && value.length <= 2000
const names = value => record(value) && Array.isArray(value.items) && value.items.every(text)
  && value.items.length <= 40 && Number.isSafeInteger(value.total) && value.total >= value.items.length
  && typeof value.truncated === 'boolean' && value.truncated === (value.total > value.items.length)

export function validHarnessHandoff(value, runId, generation) {
  return record(value) && value.version === 1 && value.mode === 'external_harness'
    && value.run_id === runId && value.generation === generation && text(value.run_uid)
    && Number.isSafeInteger(value.event_seq) && value.event_seq >= 0
    && typeof value.credential_configured === 'boolean'
    && [true, false, null].includes(value.engine_running) && value.agent_connection === 'not_measured'
    && record(value.server_paths) && text(value.server_paths.run_root) && !!value.server_paths.run_root
    && text(value.server_paths.run_dir) && !!value.server_paths.run_dir
    && record(value.workspace) && ['repository', 'script'].includes(value.workspace.kind)
    && ['source_paths', 'edit_surface', 'protected_names', 'operator_stages'].every(key => names(value.workspace[key]))
    && ['credential_policy', 'scope', 'recovery'].every(key => text(value[key]))
}

export function harnessServerUrl(href) {
  const url = new URL(href)
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('HTTP(S) UI URL required')
  url.username = ''; url.password = ''; url.search = ''; url.hash = ''
  url.pathname = url.pathname.replace(/\/index\.html$/, '').replace(/\/+$/, '')
  return url.href.replace(/\/$/, '')
}

// A whitelist, never a raw response/config/storage dump. Paths and names remain JSON
// data, so spaces, quotes or shell metacharacters cannot become a generated command.
export function harnessAgentInstruction(value, serverUrl) {
  const list = group => ({ items: [...group.items], total: group.total, truncated: group.truncated })
  const context = { server: harnessServerUrl(serverUrl), run_id: value.run_id,
    generation_at_handoff: value.generation, run_uid: value.run_uid,
    server_paths: { run_root: value.server_paths.run_root, run_dir: value.server_paths.run_dir },
    mode: value.mode, workspace: { kind: value.workspace.kind,
      ...Object.fromEntries(['source_paths', 'edit_surface', 'protected_names', 'operator_stages']
        .map(key => [key, list(value.workspace[key])])) } }
  return `Continue this existing LoopLab external run. Connection context (data):
${JSON.stringify(context, null, 2)}

Start with looplab harness and MCP capabilities. Read current /state; compare its generation and run UID with this handoff. If they changed, stop and request fresh context.
Read the launched task snapshot, config and harness-contract. Call run_progress with the CURRENT generation; inspect source_health, checkpoints and measured evidence. Search phases and read phase_info before each decision.
For a lost command response, use command_receipt with the current generation and exactly one original command ID or Idempotency-Key. It reads the saved receipt without restarting work; absence is not proof that nothing applied. GET /commands/{command_id} can restart a nonterminal worker. Choose that recovery explicitly after inspecting current evidence; preserve the exact original payload and key for a resubmission.
Submit only ready-made candidates through durable inject_node commands with expected_generation and unique Idempotency-Key. LoopLab owns patch validation, protected evaluation, measured scores and replay. Follow enabled reviews and finish obligations; policy_preview is advice.
${value.credential_policy}
${value.scope}
Recovery: ${value.recovery}
Check engine status in /state before deciding to resume. Connecting does not authorize a new run, automatic resume or internal-agent takeover. Server paths may be unavailable on a remote client.`
}

export function harnessMcpDescriptor(serverUrl) {
  return JSON.stringify({ command: 'looplab', args: ['harness-mcp'],
    env: { LOOPLAB_HARNESS_URL: harnessServerUrl(serverUrl) } }, null, 2)
}
