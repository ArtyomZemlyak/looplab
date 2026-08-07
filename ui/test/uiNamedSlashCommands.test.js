import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { assistantDirectIntent } from '../src/assistantCommand.js'

// Every slash command a screen PRINTS must be one a human can type. `panels.jsx` told the operator to
// "Trigger one with /deep-research in the chat" and to "add one from the chat (/experiment)"; neither
// has ever been a command. The browser's slash vocabulary is exactly two tables — the run-control
// intents in `assistantCommand.js::INTENTS` and AssistantBar's new-run draft regex — and nothing
// routes free text to the `deep_research` / `inject_node` control events, so both sentences were dead
// ends printed at the one moment the reader has nothing else to go on (an empty panel).
//
// Drive both tables rather than pinning literals: `assistantDirectIntent` IS the run-control
// vocabulary (it throws on anything else), and the draft regex is read out of AssistantBar's own
// source and executed, so renaming either goes red here in the same change. Coding-assistant skills
// (`/init`, `/review`, … from `GET /api/assistant/commands`) are a DIFFERENT plane and deliberately
// not accepted: they do not steer a run, and a screen that offers one as a run action is the same
// defect over again.
const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')
const CODE_SPAN = /<code[^>]*>([\s\S]*?)<\/code>/g
const SLASH_TOKEN = /^\/[a-z][a-z0-9-]*$/

const walk = dir => readdirSync(dir).flatMap(name => {
  const full = join(dir, name)
  return statSync(full).isDirectory() ? walk(full) : [full]
})

function newRunDraftPattern() {
  const source = readFileSync(join(SRC, 'AssistantBar.jsx'), 'utf8')
  const match = /const NEW_RUN_DRAFT_RE = \/(.+?)\/([a-z]*)\n/.exec(source)
  assert.ok(match, 'AssistantBar no longer declares NEW_RUN_DRAFT_RE; this scan cannot classify /new')
  return new RegExp(match[1], match[2])
}

function namedSlashCommands() {
  const found = []
  for (const file of walk(SRC).filter(name => name.endsWith('.js') || name.endsWith('.jsx'))) {
    const text = readFileSync(file, 'utf8')
    for (const match of text.matchAll(CODE_SPAN)) {
      const tokens = match[1].split(/[\s·,]+/).filter(token => SLASH_TOKEN.test(token))
      if (!tokens.length) continue
      const line = text.slice(0, match.index).split('\n').length
      for (const token of tokens) found.push({ file: file.split('/').pop(), line, token })
    }
  }
  return found
}

const accepted = (token, draftRe) => {
  if (draftRe.test(token)) return true
  try {
    // 0/0 satisfy /approve's id + generation guards; every other intent ignores them.
    assistantDirectIntent(token.slice(1), 0, 0)
    return true
  } catch { return false }
}

test('every slash command the UI prints is one the chat accepts', () => {
  const draftRe = newRunDraftPattern()
  const unknown = namedSlashCommands()
    .filter(entry => !accepted(entry.token, draftRe))
    .map(entry => `${entry.file}:${entry.line} prints ${entry.token}`)
  assert.deepEqual(unknown, [],
    `the UI prints a slash command the chat cannot run:\n  ${unknown.join('\n  ')}`)
})

test('the scan reaches the real screens (it must not pass by finding nothing)', () => {
  const found = namedSlashCommands()
  assert.ok(found.length >= 4, `expected the printed slash hints to be visible; found ${found.length}`)
  const tokens = new Set(found.map(entry => entry.token))
  for (const expected of ['/stop', '/finalize', '/resume', '/approve', '/new']) {
    assert.ok(tokens.has(expected), `Dock/RunList should still print ${expected}; the span regex drifted`)
  }
})

test('the two dead-end commands stay gone', () => {
  // Negative pin: what must not come back is the TEXT (CLAUDE.md's rule for negative source pins).
  const panels = readFileSync(join(SRC, 'panels.jsx'), 'utf8')
  assert.ok(!panels.includes('<code>/deep-research</code>'),
    'panels.jsx must not offer /deep-research: no intent, no assistant tool, no CONTROL caller')
  assert.ok(!panels.includes('<code>/experiment</code>'),
    'panels.jsx must not offer /experiment: injects come from the graph node menu')
})
