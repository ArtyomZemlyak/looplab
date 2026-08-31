// "It hangs for a very long time with no logs (or they are not visible)."
//
// The logs existed; the CLOCK did not. The live status strip named the phase ("Writing experiment
// #3…") and never how long it had been in it, so a build that had been silent for forty minutes read
// exactly like one that started two seconds ago — there was nothing on screen to distinguish work
// from a stall. Build ages reuse `_on_node_building`'s marker; evaluation ages now use the
// generation-scoped activity receipt because their one-shot event may leave the retained log window.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createServer } from 'vite'

// `narration.js` reaches markdown.jsx, which node cannot load directly — the same vite SSR harness
// every other narration test uses.
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
const { STATUS_NOISE, liveStatusAgeLabel, liveStatusStartedAt } =
  await vite.ssrLoadModule('/src/narration.js')
test.after(() => vite.close())

test('the clock follows the OLDEST in-flight build, not the newest', () => {
  // With several Developers writing at once, the number that matters is how long the SLOWEST has
  // been going — that is the one stalling the batch. Keying on the newest would hide it.
  const live = { buildings: { 3: { node_id: 3, started: 1000 }, 4: { node_id: 4, started: 1900 } } }
  assert.equal(liveStatusStartedAt(live, []), 1000)
  assert.equal(liveStatusAgeLabel(live, [], 2200), '20m')
})

test('a serial-build run falls back to the singular marker', () => {
  assert.equal(liveStatusStartedAt({ building: { node_id: 1, started: 500 } }, []), 500)
})

test('a long evaluation keeps its activity timestamp after the start event leaves the timeline', () => {
  const live = { engine_running: true, nodes: {
    2: { id: 2, attempt: 1, status: 'pending',
      activity: { status: 'evaluating', generation: 1, started_at: 700 } },
  } }
  assert.equal(liveStatusStartedAt(live, [{ type: 'llm_usage', ts: 1900 }]), 700)
  assert.equal(liveStatusAgeLabel(live, [], 1900), '20m')
})

test('interrupted or historical activity never keeps a live clock ticking', () => {
  const node = { id: 2, attempt: 0, status: 'pending',
    activity: { status: 'evaluating', generation: 0, started_at: 700 } }
  assert.equal(liveStatusStartedAt({ engine_running: false, nodes: { 2: node } }, []), null)
  assert.equal(liveStatusStartedAt({ engine_running: null, nodes: { 2: node } }, []), null)
})

test('between experiments the clock starts at the last MEANINGFUL event', () => {
  // Same noise filter the label uses, so the two can never describe different moments.
  const log = [
    { type: 'node_evaluated', ts: 100 },
    { type: 'llm_cost', ts: 900 },
    { type: 'coverage_snapshot', ts: 950 },
  ]
  assert.ok(STATUS_NOISE.has('llm_cost') && STATUS_NOISE.has('coverage_snapshot'))
  assert.equal(liveStatusStartedAt({}, log), 100, 'bookkeeping must not reset the clock')
  assert.equal(liveStatusAgeLabel({}, log, 400), '5m')
})

test('a young phase shows nothing — a ticking number under 20s is churn, not information', () => {
  const live = { building: { node_id: 1, started: 1000 } }
  assert.equal(liveStatusAgeLabel(live, [], 1005), '')
  assert.equal(liveStatusAgeLabel(live, [], 1019), '')
  assert.equal(liveStatusAgeLabel(live, [], 1020), '20s')
})

test('the scale stays readable from seconds to hours', () => {
  // Base at a real instant, not 0: epoch 0 is rejected as junk on purpose (the same "absent is not
  // zero" rule the rest of this UI is guarded by), which the skew test below pins.
  const BASE = 1_700_000_000
  const at = seconds => liveStatusAgeLabel({ building: { started: BASE } }, [], BASE + seconds)
  assert.equal(at(45), '45s')
  assert.equal(at(89), '89s')
  assert.equal(at(90), '2m')
  assert.equal(at(3600), '60m')
  assert.equal(at(5400), '1h 30m')
  assert.equal(at(7200), '2h')
})

test('clock skew and junk timestamps produce no label rather than a wrong one', () => {
  // A negative age is skew between the engine host and the browser, not a fact about the run.
  assert.equal(liveStatusAgeLabel({ building: { started: 5000 } }, [], 1000), '')
  assert.equal(liveStatusStartedAt({ building: { started: 'soon' } }, []), null)
  assert.equal(liveStatusStartedAt({ building: { started: 0 } }, []), null)
  assert.equal(liveStatusStartedAt({}, []), null)
  assert.equal(liveStatusAgeLabel({}, [], 1000), '')
  assert.equal(liveStatusStartedAt({}, [{ type: 'node_created', ts: NaN }]), null)
})

test('Dock renders the age beside the phase label', () => {
  const src = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'Dock.jsx'), 'utf8')
  assert.ok(src.includes('liveStatusAgeLabel(live, log)'))
  assert.ok(src.includes('className="muted as-age"'))
  // One definition of the noise filter, shared by the label and the clock.
  assert.ok(!src.includes('const STATUS_NOISE = new Set('), 'the second copy came back')
})


test('the trace reach control is VISIBLE, not sr-only until focused', () => {
  // It was `position:absolute; width:1px; clip:rect(0 0 0 0)` until it took focus, on the reasoning
  // that a pointer user reaches earlier steps by scrolling. The operator reported "the button to
  // load the whole trace is gone again" twice — because to a pointer user, a control that only
  // exists once you tab to it does not exist. Scrolling still works; this stops the keyboard path
  // from being the only discoverable one.
  const css = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', 'src', 'styles.css'), 'utf8')
  const rule = css.slice(css.indexOf('.trace-reach {'), css.indexOf('.trace-reach:hover'))
  assert.ok(rule.includes('display: block'), 'the reach control must render')
  assert.ok(!/clip:\s*rect\(0 0 0 0\)/.test(rule), 'the sr-only clip came back')
  assert.ok(!/width:\s*1px/.test(rule), 'the sr-only size came back')
})
