// ONE INTERVAL, ONE RENDERING.
//
// Three surfaces printed durations and each had re-derived the same two tier boundaries (90 s and
// 5,400 s) with the numbers unnamed in all three. The top tier had already drifted: for one and the
// same interval the run's live-status age said `3h 25m`, a node's episode picker `3.4h` and the
// standing-watch strip `3h`. The first two are read side by side — the episode picker sits inside
// the run screen whose header the status strip captions — so an operator comparing "how long has
// this been going" with "how long did this step take" was reading two vocabularies for one clock.
//
// Nothing anywhere stated a reason for the three to differ, so this drives the opposite property:
// the SAME seconds produce the SAME digits everywhere, and each call site keeps only its own wording
// around them (`in 3h 26m`) and its own rule about when to print at all (a noise floor, a `now`).
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

import { DURATION_MINUTE_TIER_MAX_S, DURATION_SECOND_TIER_MAX_S,
  durationLabel } from '../src/format.js'
import { episodeDurationLabel } from '../src/traceEpisodeModel.js'
import { nextCheckText } from '../src/assistantWatchModel.js'

// `narration.js` reaches markdown.jsx, which node cannot load directly — the same vite SSR harness
// every other narration test uses. It is also the reason the shared formatter lives in `format.js`
// (which imports nothing) rather than in narration.js: the two models above are driven by plain
// `node --test`, and importing narration.js into either would have taken that away.
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
const { liveStatusAgeLabel } = await vite.ssrLoadModule('/src/narration.js')
test.after(() => vite.close())

// The live-status age of a build that started `seconds` ago — the third surface, reached through its
// own public function so this test cannot pass on a formatter nothing calls.
const BASE = 1_700_000_000
const ageAt = seconds => liveStatusAgeLabel({ building: { started: BASE } }, [], BASE + seconds)

test('the three tiers and their two named boundaries', () => {
  assert.equal(durationLabel(0), '0s')
  assert.equal(durationLabel(45), '45s')
  assert.equal(durationLabel(DURATION_SECOND_TIER_MAX_S - 1), '89s')
  assert.equal(durationLabel(DURATION_SECOND_TIER_MAX_S), '2m', 'the boundary belongs to minutes')
  assert.equal(durationLabel(600), '10m')
  assert.equal(durationLabel(3600), '60m', 'minutes run to 90, so an hour still reads as 60m')
  assert.equal(durationLabel(DURATION_MINUTE_TIER_MAX_S - 1), '90m')
  assert.equal(durationLabel(DURATION_MINUTE_TIER_MAX_S), '1h 30m', 'the boundary belongs to hours')
  assert.equal(durationLabel(7200), '2h', 'a whole hour drops the empty minute half')
  assert.equal(durationLabel(12_345), '3h 26m')
})

test('the minute remainder carries into the hour instead of printing 60m', () => {
  // 7,199 s is 1 h 59.98 m. Rounding the remainder on its own printed `1h 60m` — a label an operator
  // has to translate. The narration copy this consolidates had the bug and no test could see it,
  // which is the second half of why three copies is worse than one.
  assert.equal(durationLabel(7199), '2h')
  assert.equal(durationLabel(3600 * 4 - 1), '4h')
  assert.equal(durationLabel(3600 * 4 - 31), '3h 59m', 'and it only carries when it must')
})

test('absent is not zero: only a real number is a duration', () => {
  // `Number(null)` and `Number('')` are 0, so a coercing guard would render an ABSENT duration as
  // `0s` — the same "absent is not zero" rule the rest of this UI is guarded by.
  for (const junk of [null, undefined, '', 'soon', NaN, Infinity, -1, {}, [], true]) {
    assert.equal(durationLabel(junk), '', JSON.stringify(junk) ?? String(junk))
  }
})

test('one interval reads the same on all three surfaces', () => {
  // The drift, driven directly. Each surface is asked about the SAME number of seconds through its
  // own public entry point, and the digits must agree; only the wording around them is local.
  for (const seconds of [45, 89, 90, 600, 3600, 5400, 7200, 12_345, 40_000]) {
    const shared = durationLabel(seconds)
    assert.equal(ageAt(seconds), shared, `live status age at ${seconds}s`)
    assert.equal(episodeDurationLabel({ seconds }), shared, `episode duration at ${seconds}s`)
    assert.equal(nextCheckText({ status: 'armed', next_due: seconds }, 0), `in ${shared}`,
      `watch countdown at ${seconds}s`)
  }
  // The specific interval the three used to disagree about — 3 h 25.75 m.
  assert.equal(durationLabel(12_345), '3h 26m')
})

test('each surface keeps its own rule about when to say nothing at all', () => {
  // Consolidating the DIGITS must not consolidate the decisions above them. The status strip has a
  // noise floor (a blinking sub-20s number reads as churn), the countdown has a `now`, and the
  // episode picker has "absent stays out" — none of which is a formatting rule.
  assert.equal(ageAt(19), '', 'below the status-age noise floor')
  assert.equal(ageAt(20), '20s')
  assert.equal(nextCheckText({ status: 'armed', next_due: 0.5 }, 0), 'now')
  assert.equal(nextCheckText({ status: 'done', next_due: 600 }, 0), '', 'a terminal watch is not due')
  assert.equal(nextCheckText({ status: 'armed', next_due: null }, 0), '',
    'an absent schedule must not read as "about to fire"')
  assert.equal(episodeDurationLabel({ seconds: null }), '')
  assert.equal(episodeDurationLabel(null), '')
})
