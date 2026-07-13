import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('ephemeral command results use polite atomic live regions', async () => {
  const [runView, assistant] = await Promise.all([source('RunView.jsx'), source('AssistantBar.jsx')])
  assert.match(runView, /className="toast" role="status" aria-live="polite" aria-atomic="true"/)
  assert.equal((assistant.match(/className="cmdbar-toast(?: side)?" role="status" aria-live="polite" aria-atomic="true"/g) || []).length, 3)
  assert.match(runView, /setTimeout\(\(\) => setToast\(null\), 5000\)/)
  assert.match(assistant, /setTimeout\(\(\) => mountedRef\.current && setToast\(null\), 5000\)/)
})

test('permission cards expose an accessible pending decision and safe default focus', async () => {
  const [chat, bar] = await Promise.all([source('AssistantChat.jsx'), source('AssistantBar.jsx')])
  assert.match(chat, /role="alertdialog" aria-modal="false" aria-labelledby=\{titleId\} aria-describedby=\{detailsId\}/)
  assert.match(chat, /rejectRef\.current\?\.focus\(\{ preventScroll: true \}\)/)
  assert.match(chat, /permission\.canAlways && <button/)
  assert.match(chat, /Approve once/)
  assert.match(bar, /className="asst-perm-region" role="region"/)
  assert.match(bar, /aria-live="assertive" aria-atomic="false"/)
  assert.match(bar, /pending\.length > 0 && view === 'bar'[\s\S]*?setView\('side'\)/)
  const resolve = bar.slice(bar.indexOf('const resolvePerm = async'), bar.indexOf('const onRevert = async'))
  assert.ok(resolve.indexOf('await assistantResolve') < resolve.indexOf('setPending(current =>'),
    'the card must remain visible until the resolve POST succeeds')
})

test('timeline reveal exposes its state and a mobile touch-sized target', async () => {
  const [dock, css] = await Promise.all([source('Dock.jsx'), source('styles.css')])
  assert.match(dock, /aria-label=\{collapsed \? 'Expand events and timeline' : 'Collapse events and timeline'\}/)
  assert.match(dock, /aria-expanded=\{!collapsed\} aria-controls="run-events-timeline"/)
  assert.match(dock, /id="run-events-timeline" className="dock-body chat-body"/)
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*?\.dock-tabs \.dock-collapse \{ min-width: 44px; min-height: 44px;/)
})

test('reduced-motion disables every live CTRL activity indicator', async () => {
  const css = await source('styles.css')
  const reduced = css.slice(css.indexOf('@media (prefers-reduced-motion: reduce)'))
  for (const selector of ['.cmdbar-pip', '.agent-status .as-dot', '.trace-live-status .tls-dot', '.live.on .led']) {
    assert.ok(reduced.includes(selector), `${selector} must be static under reduced motion`)
  }
})

test('Dock exposes accepted command records immediately instead of waiting in submitting copy', async () => {
  const dock = await source('Dock.jsx')
  assert.match(dock, /onRecord: next => \{[\s\S]*?persistTransport\(visible\)[\s\S]*?setTransportPending/)
  assert.match(dock, /idempotencyKey, expectedGeneration: generation, waitMs: 0/)
})

test('displayed run generation participates in state dedupe and is published only after commit', async () => {
  const hooks = await source('hooks.js')
  assert.match(hooks, /p\.seq === lastSeq && alive === lastAlive && nextGeneration === lastGeneration/)
  assert.match(hooks, /useLayoutEffect\(\(\) => \{ observeRunGeneration\(runId, generation\) \}/)
})

test('Config restart tracks both accepted/executing and terminates authoritative read failures', async () => {
  const panels = await source('panels.jsx')
  assert.match(panels, /!\['accepted', 'executing'\]\.includes\(pending\.record\.status\)/)
  assert.match(panels, /\[401, 403, 404\]\.includes\(error\?\.status\)[\s\S]*?setRestartPending\(null\)/)
})

test('both command surfaces fail before POST when durable intent persistence fails', async () => {
  const [assistant, dock] = await Promise.all([source('AssistantBar.jsx'), source('Dock.jsx')])
  assert.match(assistant, /if \(!persistDirect\(submitting\)\) \{ localStorageFailure\(bound\); return \}[\s\S]*?submitAssistantDirect/)
  assert.match(dock, /if \(!persistTransport\(start\)\) \{ storageTransportFailure\(action, idempotencyKey, generation\); return \}[\s\S]*?runCommand/)
})

test('Assistant renders durable Retry-same-command and actionable Dismiss states', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /retryRunCommand\(failure\.runId, failure\.record\.id/)
  assert.match(assistant, />Retry same command<\/button>/)
  assert.match(assistant, /dismissDirectFailure/)
  assert.match(assistant, /role=\{directNeedsAlert \? 'alert' : 'status'\}/)
})

test('collapsed recovery actions are not clipped and retain a 44px touch target', async () => {
  const css = await source('assistant-polish.css')
  assert.match(css, /\.cmdbar-status\.recovery \{[\s\S]*?overflow: visible;[\s\S]*?white-space: normal;/)
  assert.match(css, /\.cmdbar-status\.recovery \.btn \{ min-height: 44px;/)
})

test('Assistant unmount cleanup clears its toast timer and every command poll clears its timer', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /if \(flashTimerRef\.current\) clearTimeout\(flashTimerRef\.current\)/)
  assert.match(assistant, /return \(\) => \{ active = false; clearTimeout\(timer\) \}/)
})

test('direct command presentation is guarded by currentRunId while durable cleanup remains unconditional', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /currentRunIdRef\.current = runId/)
  assert.match(assistant, /presentAssistantCommandResult\([\s\S]*?currentRunIdRef\.current, entry\.runId/)
  assert.match(assistant, /clearAssistantRunTransport\(entry\.runId[\s\S]*?setCurrentFailure\(entry/)
})

test('Assistant clears stale run-scoped toast immediately when the route run changes', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /assistantRunChanged\(toastRunIdRef\.current, runId\)[\s\S]*?clearTimeout\(flashTimerRef\.current\)[\s\S]*?setToast\(null\)[\s\S]*?toastRunIdRef\.current = runId/)
  assert.match(assistant, /const visibleToast = assistantRunChanged\(toastRunIdRef\.current, runId\) \? null : toast/)
  assert.equal((assistant.match(/\{visibleToast && <div className="cmdbar-toast(?: side)?"/g) || []).length, 3)
})

test('moving pending or failed command UI to side/full re-arms status focus', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /const openCommandView = \(next\) => \{[\s\S]*?if \(commandBusy \|\| directFailure\) commandFocusRequestedRef\.current = true[\s\S]*?setView\(next\)/)
  assert.match(assistant, /const openSide = \(\) => openCommandView\('side'\)/)
  assert.match(assistant, /const openFull = \(\) => openCommandView\('full'\)/)
  assert.match(assistant, /const collapseToBar = \(\) => \{[\s\S]*?if \(commandBusy \|\| directFailure\) commandFocusRequestedRef\.current = true[\s\S]*?setView\('bar'\)/)
  assert.match(assistant, /\[directPending\?\.record\?\.id,[\s\S]*?busy, view\]/)
})

test('an unsent Assistant storage quarantine keeps failure and Dismiss visible in the same tab', async () => {
  const assistant = await source('AssistantBar.jsx')
  assert.match(assistant, /const ownStorageFailureLock = assistantStorageFailureOwnsLock\(directFailure, runCommandLock\)/)
  assert.match(assistant, /const commandBusy = directPending != null \|\| \(runCommandLock != null && !ownStorageFailureLock\)/)
  assert.match(assistant, /const clearUnsentDirectRecovery = entry => \{[\s\S]*?clearAssistantRunTransport\(entry\.runId[\s\S]*?clearRunCommandLock\(entry\.runId, expected\)[\s\S]*?remainingTransport[\s\S]*?remainingLock/)
  assert.match(assistant, /const localStorageFailure = entry => \{[\s\S]*?clearUnsentDirectRecovery\(failure\)[\s\S]*?setCurrentFailure\(entry, failure\)/)
  assert.match(assistant, /failure\.record\?\.error\?\.code === 'command_storage_unavailable'[\s\S]*?!clearUnsentDirectRecovery\(failure\)[\s\S]*?remains quarantined/)
})
