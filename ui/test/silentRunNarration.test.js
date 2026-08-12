// "This nonsense has been hanging for half an hour since the run was created."
//
// It had not been hanging. Measured on `rubertlite-dr-unified-v5`: setup finished at t=6.6 s, the
// Researcher then spent 813 s on one proposal and the Developer 23 more minutes on the build —
// 452 spans, 163 provider calls. The run was working the whole time. Three independent surfaces
// failed to say so, and two of them are in this file.
//
//   1. The card lifecycle events were IN the log and dropped by the feed, because the feed is an
//      allow-list and none of them had a NARR entry.
//   2. The one clock in the product never rendered, because its "last meaningful event" fallback
//      counted the per-call accounting rows as meaningful.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

// `narration.js` reaches markdown.jsx, which node cannot load directly — the same vite SSR harness
// every other narration test uses.
const vite = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  configFile: false, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
})
const {
  STATUS_NOISE, eventNarration, isCuratedType, liveStatusAgeLabel, liveStatusStartedAt,
} = await vite.ssrLoadModule('/src/narration.js')
test.after(() => vite.close())

// The exact payloads the engine wrote during that run, read back from its event log.
const V5 = [
  { type: 'card_added', data: { id: 'card-0', source: 'researcher', at_node: 0,
    statement: 'Reproducing the human-proven qwen3 cross-batch InfoNCE recipe reaches recall@100 ≈ 0.95' } },
  { type: 'card_build_requested', data: { card_id: 'card-0', generation: 0 } },
  { type: 'card_build_attempted', data: { card_id: 'card-0', generation: 0, index: 0 } },
  { type: 'card_build_done', data: { card_id: 'card-0', generation: 0, node_id: 0, speculative: true } },
]

test('the card lifecycle reaches the feed at all', () => {
  for (const event of V5) {
    assert.ok(isCuratedType(event.type), `${event.type} is still dropped by the allow-list`)
  }
})

test('each card row says what actually happened, not that an id changed', () => {
  const [added, requested, attempted, done] = V5.map(eventNarration)
  assert.match(added, /card-0/)
  assert.match(added, /InfoNCE/, 'the statement is the only part worth reading')
  assert.match(requested, /the Developer is starting/,
    'this row is the one that explains a 23-minute silence — it must say so')
  assert.match(attempted, /card-0/)
  assert.match(done, /node #0/)
  assert.match(done, /prefetched/, 'a speculative build is a different fact from a serial one')
})

test('a malformed card payload degrades instead of throwing into the feed', () => {
  for (const type of ['card_added', 'card_build_requested', 'card_build_done', 'card_ranked']) {
    const text = eventNarration({ type, data: {} })
    assert.equal(typeof text, 'string')
    assert.ok(text.length > 0)
  }
  assert.equal(typeof eventNarration({ type: 'card_added', data: null }), 'string')
})

test('deep research announces its START, not only its result', () => {
  // A multi-minute web + LLM investigation that reports only on completion is indistinguishable
  // from a stall while it runs.
  assert.ok(isCuratedType('research_attempted'))
  assert.match(eventNarration({ type: 'research_attempted', data: { trigger: 'cadence' } }),
    /deep research started \(cadence\)/)
})

test('per-call accounting no longer restarts the clock', () => {
  // THE defect: one `llm_usage` per provider call, so a long agentic stage reset the clock every
  // few seconds and it never crossed its own 20-second floor.
  assert.ok(STATUS_NOISE.has('llm_usage'))
  assert.ok(STATUS_NOISE.has('llm_cost'), 'the aggregate sibling was already excluded')
})

test('a long silent proposal reports its real age, not the age of the last token bill', () => {
  const started = 1_000_000
  const log = [
    { type: 'setup_finished', ts: started },
    // What the run actually wrote for the next 13 minutes.
    ...Array.from({ length: 163 }, (_, i) => ({ type: 'llm_usage', ts: started + 5 + i * 5 })),
  ]
  const now = started + 813
  assert.equal(liveStatusStartedAt({}, log), started,
    'the clock must anchor on the last thing that MEANT something')
  assert.equal(liveStatusAgeLabel({}, log, now), '14m')
})

test('a real event still wins over an older one, so the fix does not freeze the clock', () => {
  const log = [
    { type: 'setup_finished', ts: 1_000_000 },
    { type: 'llm_usage', ts: 1_000_400 },
    { type: 'card_added', ts: 1_000_500 },
  ]
  assert.equal(liveStatusStartedAt({}, log), 1_000_500)
  assert.equal(liveStatusAgeLabel({}, log, 1_000_560), '60s')
})

test('an in-flight build still outranks the event log', () => {
  // Unchanged behaviour, pinned because the fix above touches the same function.
  const log = [{ type: 'card_added', ts: 2_000_000 }]
  assert.equal(liveStatusStartedAt({ buildings: { 0: { started: 1_999_000 } } }, log), 1_999_000)
})
