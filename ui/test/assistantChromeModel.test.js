import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import { foldControl, newChatGate } from '../src/assistantChromeModel.js'

const assistantSource = () => readFile(new URL('../src/AssistantBar.jsx', import.meta.url), 'utf8')

test('a new-chat control is never disabled without saying why, and never says why while enabled', () => {
  // The whole reason this is one function: `disabled` and `title` come from the SAME branch, so the
  // two cannot drift into a control that refuses silently or explains a refusal that is not happening.
  const facts = ['turnStarting', 'retryChecking', 'directConfirm']
  for (const blocked of [{}, ...facts.map(f => ({ [f]: true }))]) {
    const gate = newChatGate(blocked, 'new chat')
    const anyBlocked = facts.some(f => blocked[f])
    assert.equal(gate.disabled, anyBlocked, JSON.stringify(blocked))
    assert.equal(gate.title !== 'new chat', anyBlocked,
      'a disabled control must name its cause; an enabled one must not')
  }
})

test('the new-chat gate names the innermost cause', () => {
  const all = { turnStarting: true, retryChecking: true, directConfirm: {} }
  assert.equal(newChatGate(all).title, 'Wait for the new Assistant chat to finish starting')
  assert.equal(newChatGate({ ...all, turnStarting: false }).title,
    'Wait for the saved turn check to finish')
  assert.equal(newChatGate({ ...all, turnStarting: false, retryChecking: false }).title,
    'Resolve the run-command confirmation first')
})

test('the idle title is per-view and the blocked titles are not', () => {
  // "new chat" in the side rail and no tooltip at all in the full view are two true sentences about
  // two positions on screen. Every arm ABOVE idle is the shared decision.
  assert.equal(newChatGate({}, 'new chat').title, 'new chat')
  assert.equal(newChatGate({}, undefined).title, undefined)
  assert.equal(newChatGate({}).title, undefined)
  assert.equal(newChatGate({ turnStarting: true }, 'new chat').title,
    newChatGate({ turnStarting: true }, undefined).title)
  assert.equal(newChatGate().disabled, false, 'no known blocker means the control is live')
  assert.equal(newChatGate({ directConfirm: null }).disabled, false)
  assert.equal(newChatGate({ directConfirm: {} }).disabled, true,
    'a confirmation RECORD blocks; the flag is its truthiness, not its identity')
})

test('a pending run-command confirmation repurposes the fold button in all three fields at once', () => {
  // The same pixel stops folding the view and starts cancelling the command. If the label, the
  // tooltip and the action could disagree, the button would say "Cancel command" and collapse.
  const idle = foldControl(null, 'fold to the bar')
  assert.deepEqual(idle, { action: 'collapse', label: '▾ bar', title: 'fold to the bar' })
  for (const confirmation of [{}, { command: 'stop' }, 'pending', 1]) {
    const busy = foldControl(confirmation, 'fold to the bar')
    assert.equal(busy.action, 'cancel')
    assert.equal(busy.label, 'Cancel command')
    assert.equal(busy.title, 'Cancel the run-command confirmation and keep the draft')
  }
  for (const empty of [null, undefined, false, 0, '']) {
    assert.equal(foldControl(empty, 'collapse to the bar').action, 'collapse', String(empty))
  }
})

test('each of the three fold positions keeps its own sentence and none keeps its own gate', () => {
  for (const [title, expected] of [
    ['collapse to the bar', 'collapse to the bar'],
    ['fold back to the bar', 'fold back to the bar'],
    ['fold to the bar', 'fold to the bar'],
  ]) {
    assert.equal(foldControl(null, title).title, expected)
    assert.equal(foldControl({}, title).title, 'Cancel the run-command confirmation and keep the draft',
      'the confirmation sentence is shared; only the idle one is positional')
  }
})

test('the shared chrome is written once and reached from every view', async () => {
  const source = await assistantSource()
  // Doc 25 UI-05, measured before the change: the new-chat gate appeared twice byte for byte and the
  // fold control three times, each with its own copy of the confirmation branch. What must hold is
  // that no view re-derives either — a hand-written fourth copy is how the ladder drifts again.
  assert.equal([...source.matchAll(/\{newChatButton\(/g)].length, 2)
  assert.equal([...source.matchAll(/\{foldToBarButton\(/g)].length, 3)
  assert.equal([...source.matchAll(/onClick=\{newChat\}/g)].length, 1)
  assert.equal([...source.matchAll(/cancelDirectConfirmation : collapseToBar/g)].length, 1)
  assert.doesNotMatch(source, /directConfirm \? 'Cancel command' : '▾ bar'/,
    'a re-derived fold label is a second opinion about what the button is currently for')
  assert.doesNotMatch(source, /disabled=\{turnStarting \|\| retryChecking \|\| !!directConfirm\}/,
    'a re-derived new-chat gate can disagree with the tooltip beside it')
  // The action must be selected by the SAME verdict that chose the label, not by re-reading the fact.
  assert.match(source, /const fold = foldControl\(directConfirm, idleTitle\)\s*\n\s*return <button className=\{cls\} title=\{fold\.title\}\s*\n\s*onClick=\{fold\.action === 'cancel' \? cancelDirectConfirmation : collapseToBar\}>\s*\n\s*\{fold\.label\}<\/button>/,
    'label, tooltip and action must all be read off one foldControl verdict')
  // Both idle sentences that distinguish the three positions must survive the sharing.
  for (const sentence of ['collapse to the bar', 'fold back to the bar', 'fold to the bar', 'new chat']) {
    assert.ok(source.includes(`'${sentence}'`), sentence)
  }
})
