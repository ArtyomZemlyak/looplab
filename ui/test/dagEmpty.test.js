import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('zero-node canvas renders the lifecycle model and hides meaningless graph controls', async () => {
  const [runView, css] = await Promise.all([source('RunView.jsx'), source('styles.css')])
  assert.match(runView, /dagEmptyPresentation\(\{[\s\S]*?historyActive, reviewMode/)
  assert.match(runView, /<DagEmptyOverlay presentation=\{emptyPresentation\}/)
  assert.match(runView, /role=\{presentation\.liveRegion === 'assertive' \? 'alert' : 'status'\}/)
  assert.match(runView, /\^\[0-9a-f\]\{64\}\$\/\.test\(transport\?\.expectedGeneration/)
  assert.match(css, /\.dag-empty \.react-flow__controls[\s\S]*?display: none;/)
  assert.match(css, /\.dag-empty-actions \.btn \{ min-height: 40px; \}/)
  assert.match(css, /@media \(max-width: 900px\)[\s\S]*?\.dag-empty-actions \.btn \{ min-height: 44px; \}/)
})

test('canvas recovery reuses a committed exact-generation Dock controller', async () => {
  const [runView, dock] = await Promise.all([source('RunView.jsx'), source('Dock.jsx')])
  assert.match(dock, /useLayoutEffect\(\(\) => \{[\s\S]*?Object\.freeze\(\{[\s\S]*?runId, expectedGeneration/)
  assert.match(dock, /publishTransport\(current => current === controller \? null : current\)/)
  assert.match(dock, /pendingAction: transportPending\?\.action \|\| externalTransportPending\?\.action/)
  assert.match(runView, /exactController\.runId !== runId[\s\S]*?exactController\.expectedGeneration !== generation/)
  assert.match(runView, /if \(exactController\.failure\) \{ revealEvents\(\); return \}/)
})

test('approval empty state only prefills Assistant and never auto-submits', async () => {
  const [runView, assistant] = await Promise.all([source('RunView.jsx'), source('AssistantBar.jsx')])
  assert.match(runView, /new CustomEvent\('ll:focus-assistant', \{ detail: \{ text: approvalCommand \} \}\)/)
  assert.match(runView, /Approval target is missing\.[\s\S]*?no command has been guessed/)
  assert.match(assistant, /window\.addEventListener\('ll:focus-assistant', onFocusAssistant\)/)
  assert.match(assistant, /if \(text && \(!draft \|\| draft === text\)\) setInput\(text\)/)
  assert.match(assistant, /Draft preserved — clear it before inserting/)
  assert.doesNotMatch(assistant, /onFocusAssistant[\s\S]{0,400}\bsend\(\)/)
})
