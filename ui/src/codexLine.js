// The copy-paste lines that hand LoopLab to Codex (doc 82 П1/П4). Dependency-free on purpose: the
// launch card and the first-run model panel import it without pulling the handoff's instruction
// text and its Russian dictionary into their chunks (`npm run check:bundle`).

export function harnessServerUrl(href) {
  const url = new URL(href)
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('HTTP(S) UI URL required')
  url.username = ''; url.password = ''; url.search = ''; url.hash = ''
  url.pathname = url.pathname.replace(/\/index\.html$/, '').replace(/\/+$/, '')
  return url.href.replace(/\/$/, '')
}

export const HARNESS_DEFAULT_URL = 'http://127.0.0.1:8765'

// POSIX single-quote: the one quoting a copied shell line uses, so no URL character is code.
export const shellQuote = value => /^[A-Za-z0-9_./:=@%+-]+$/.test(value) ? value
  : `'${String(value).replaceAll("'", `'\\''`)}'`

// The one-time registration on the anonymous path: `codex mcp add` writes Codex's own config.
// The URL is passed only when the UI is not on harness-mcp's default address.
export function harnessCodexAddCommand(serverUrl) {
  const url = harnessServerUrl(serverUrl)
  const env = url === HARNESS_DEFAULT_URL ? '' : ` --env ${shellQuote(`LOOPLAB_HARNESS_URL=${url}`)}`
  return `codex mcp add looplab${env} -- looplab harness-mcp`
}

// The ONE line a user copies to hand a run (or a goal) to Codex without editing any config (doc 82
// П4): MCP registration through `-c` overrides plus the prompt, every user-derived piece quoted
// with `shellQuote` so a run name or a goal is data, never shell. POSIX shells; on Windows
// PowerShell use the one-time `codex mcp add` line and then `codex "<prompt>"`.
export function harnessCodexOneLiner(serverUrl, prompt) {
  const url = harnessServerUrl(serverUrl)
  const parts = ['codex',
    '-c', shellQuote('mcp_servers.looplab.command="looplab"'),
    '-c', shellQuote('mcp_servers.looplab.args=["harness-mcp"]')]
  if (url !== HARNESS_DEFAULT_URL) {
    parts.push('-c', shellQuote(`mcp_servers.looplab.env={LOOPLAB_HARNESS_URL=${JSON.stringify(url)}}`))
  }
  parts.push(shellQuote(String(prompt)))
  return parts.join(' ')
}

// What the copied line asks Codex to do with an EXISTING run (the launch card's external run).
// Statement returns, like `harnessAgentInstruction`: an agent prompt is not UI copy. English, with
// a reply-language request, because a copied prompt is read by the agent, not by the page.
const replyIn = language => (language === 'ru' ? ' Отвечай по-русски.' : '')

export function harnessRunPrompt(runId, language = 'en') {
  return `Drive LoopLab run ${JSON.stringify(String(runId))} via the looplab MCP tools: connection_check, run_progress, then submit_experiment and wait_for_next until done; LoopLab measures. Pause or finish explicitly.${replyIn(language)}`
}

// What the copied line asks Codex to do with a GOAL (no LoopLab model: path B from the Assistant).
export function harnessGoalPrompt(goal, language = 'en') {
  return `Via the looplab MCP tools: start_run a LoopLab run for this goal, give me its ui_url, then drive it (run_progress, submit_experiment, wait_for_next).${replyIn(language)} Goal: ${String(goal || '').trim()}`
}
