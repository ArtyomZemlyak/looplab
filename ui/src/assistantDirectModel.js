// The assistant's DIRECT run-control grammar (review 2026-09-22, UI-06): which typed commands and
// which natural-language phrases fire a run command with NO model call, and what each one says when
// it lands. Moved verbatim out of `AssistantBar.jsx`, where these rules were module-private and so
// reachable only by mounting the whole component; here `node --test` drives them directly
// (`test/assistantDirectModel.test.js`). Pure: no React, no DOM, no storage.

// Run-control commands safe to fire directly (no model). `arg:true` needs a node id (e.g. /approve #12).
const FREEZE = { success: '⏸ run stopped (not finalized)',
  noop: '⏸ run already stopped', executing: 'Stop requested — waiting for freeze' }
const FINALIZE = { success: '⏹ run finalized',
  noop: '⏹ run already finalized', executing: 'Finalize requested — waiting for wrap-up' }
export const DIRECT = {
  stop: FREEZE, pause: FREEZE, finalize: FINALIZE, abort: FINALIZE,
  resume:   { success: '▶ run resumed',
    noop: '▶ run already running', executing: 'Resume requested — waiting for engine' },
  ratify:   { success: '✓ eval spec ratified',
    noop: '✓ eval spec already ratified', executing: 'Ratification requested — awaiting confirmation' },
  approve:  { arg: true, success: (id) => `✓ approved #${id}`,
    noop: (id) => `✓ #${id} already approved`, executing: (id) => `Approval #${id} requested — awaiting confirmation` },
}
export const directSpec = name => typeof name === 'string' && Object.hasOwn(DIRECT, name)
  ? DIRECT[name] : null
export const UNKNOWN_DIRECT_SPEC = {
  success: 'Run command completed', noop: 'Run command was already satisfied',
  executing: 'Run command is pending',
}
export const directCopy = (value, arg) => typeof value === 'function' ? value(arg) : value
// Unambiguous = a lone /name optionally + a single #id token, and NOTHING else. Trailing prose → LLM.
export function parseDirect(t) {
  const m = /^\/([a-z_]+)(?:\s+#?(\d+))?\s*$/i.exec(t)
  if (!m) return null
  const name = m[1].toLowerCase()
  const spec = directSpec(name)
  if (!spec) return null
  const arg = m[2] ? Number(m[2]) : null
  if (spec.arg && arg == null) return null
  // A node token changes the meaning of a run-wide lifecycle command. Treat that input as
  // invalid instead of silently discarding the node and stopping the whole run (or asking an LLM
  // to reinterpret a control-shaped typo). Preserve the draft so the user can correct it in place.
  if (!spec.arg && arg != null) return {
    invalid: true,
    message: `/${name} controls the whole run and does not accept #${arg}. Remove the node id to continue.`,
  }
  return { name, spec, arg }
}

// U5 · cheap pre-router: catch a few natural-language control phrases WITHOUT paying for an LLM
// round-trip. Fires ONLY when the phrase names the run ("stop the run", "finalize run") — a bare
// "stop" or "continue" is everyday chat directed at the assistant. `stop` is now a reversible FREEZE
// (safe); the terminal wrap-up is `finalize` (maps everyday "abort/halt/wrap up" onto it).
const _NL_CONTROL = { stop: 'stop', freeze: 'stop', pause: 'stop',
  finalize: 'finalize', abort: 'finalize', halt: 'finalize', wrapup: 'finalize',
  resume: 'resume', continue: 'resume', unpause: 'resume' }
export function preRoute(t) {
  const cleaned = t.toLowerCase().replace(/^(please\s+|can you\s+)/, '').replace(/[.!]+$/, '').trim()
  if (!/\brun\b/.test(cleaned)) return null
  const norm = cleaned.replace(/\b(the\s+|this\s+|current\s+)?run\b/g, '').trim()
  const name = Object.hasOwn(_NL_CONTROL, norm) ? _NL_CONTROL[norm] : null
  const spec = directSpec(name)
  return name && spec ? { name, spec, arg: null } : null
}
