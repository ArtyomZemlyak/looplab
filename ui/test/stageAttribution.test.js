// The eval pipeline's stage strip, attributed — see `src/stageAttribution.js` for the measurement.
//
// The FIXTURES ARE THE REAL SHAPES: `v9Node5` / `v9Node6` are the two nodes the operator reported
// on `runs/rubertlite-dr-unified-v9` at 2026-08-17 12:48 UTC, folded — pending, training, and
// showing a red ✗ from a repair cycle that had been superseded 177 and 56 minutes earlier. The
// negative controls are equally real: v9 node 4 (never repaired) and the shape of v8 node 3, whose
// `mine ok` row is two epochs old because every later attempt REUSED it and is not stale at all.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  STAGE_SUPERSEDED_ICON, stageIcon, stagePipelineView, stageRowSuperseded, stageRowTitle,
  stageRowView, stageSupersededLabel, stageTone, stageSupersessionNotice,
} from '../src/stageAttribution.js'

// runs/rubertlite-dr-unified-v9 node 5, exactly as the fold reports it.
const v9Node5 = {
  id: 5, status: 'pending', failed_stage: null, repairs: 3,
  stages: [
    { name: 'mine', status: 'expect_failed', exit_code: 0, seconds: 153.352, repairs: 2 },
    { name: 'train', status: 'fail', exit_code: 1, seconds: 228.845, repairs: 1 },
  ],
}
const v9Node6 = {
  id: 6, status: 'pending', failed_stage: null, repairs: 1,
  stages: [
    { name: 'mine', status: 'ok', exit_code: 0, seconds: 2216.976, repairs: 0 },
    { name: 'train', status: 'fail', exit_code: 1, seconds: 297.349, repairs: 0 },
  ],
}
// node 4 of the same run: three stages, no repair, evaluated. Nothing about it may move.
const v9Node4 = {
  id: 4, status: 'evaluated', failed_stage: null, repairs: 0,
  stages: [
    { name: 'mine', status: 'ok', exit_code: 0, seconds: 3642.915, repairs: 0 },
    { name: 'train', status: 'ok', exit_code: 0, seconds: 11053.199, repairs: 0 },
    { name: 'score', status: 'ok', exit_code: 0, seconds: 3107.442, repairs: 0 },
  ],
}

// ------------------------------------------------------------------ the property

test('a node whose last stage row is a failure but which has a later repair does not read as failed', () => {
  const { rows, notice } = stagePipelineView(v9Node5)
  assert.deepEqual(rows.map(r => r.superseded), [true, true])
  // The glyph is REPLACED, not kept. It used to stay '✗' while the tone said "stale", and the glyph
  // is what an operator reads — see the e5small node 9 case below, which is why this pin moved.
  assert.deepEqual(rows.map(r => r.icon), ['⋯', '⋯'])
  assert.equal(rows.every(r => r.icon !== stageIcon(r.row)), true)
  assert.deepEqual(rows.map(r => r.tone), ['var(--fg-mut)', 'var(--fg-mut)'])
  assert.notEqual(notice, null)
  assert.equal(notice.superseded, 2)
  assert.equal(notice.failed, 2)
  assert.match(notice.text, /2 of 2 stage results are from an earlier attempt/)
  assert.match(notice.text, /repair 3 was applied after them/)
  assert.match(notice.text, /has not been scored since/)
  assert.match(notice.failureText, /2 of them are failures/)
})

test('node 6: one repair supersedes the whole strip, and only the failure is counted as one', () => {
  const { rows, notice } = stagePipelineView(v9Node6)
  assert.deepEqual(rows.map(r => r.superseded), [true, true])
  assert.equal(notice.superseded, 2)
  assert.equal(notice.failed, 1)                 // `mine ok` is stale but is not a red chip
  assert.match(notice.failureText, /1 of them is a failure/)
})

test('the row title names both epochs, so the claim is checkable', () => {
  const { rows } = stagePipelineView(v9Node5)
  assert.equal(rows[1].title,
    'train: fail · 228.845s · exit 1 — recorded under repair 1 of 3; '
    + 'no later attempt has reported on this stage')
})

test('a settled node says its outcome came from a later attempt, not that it is unscored', () => {
  const { notice } = stagePipelineView({ ...v9Node5, status: 'evaluated' })
  assert.match(notice.text, /settled by a later attempt these rows do not describe/)
  assert.doesNotMatch(notice.text, /not been scored/)
})

// ------------------------------------------- the glyph, on the run that produced the complaint

// runs/e5small-dr-unified-v2 node 9 at 2026-08-19, exactly as the fold reports it: ONE inline
// repair applied, both stage rows recorded at repair 0, node still pending. The `train` crash at
// 223.871 s was repaired and the retry trained all 2,109 steps and went to evaluation — nothing in
// the log says either row is the node's current state, and the strip drew `✓ mine → ✗ train`.
const e5Node9 = {
  id: 9, status: 'pending', failed_stage: null, repairs: 1,
  stages: [
    { name: 'mine', status: 'ok', exit_code: 0, seconds: 3688.692, repairs: 0 },
    { name: 'train', status: 'fail', exit_code: 1, seconds: 223.871, repairs: 0 },
  ],
}

test('a superseded FAILURE does not keep the glyph that says the stage failed', () => {
  const { rows } = stagePipelineView(e5Node9)
  const train = rows[1]
  assert.equal(train.superseded, true)
  assert.equal(train.icon, STAGE_SUPERSEDED_ICON)
  assert.notEqual(train.icon, '✗')               // the operator read '✗ train' as "training failed"
  // The tone was already right; the row must not go on making two opposite claims.
  assert.equal(train.tone, 'var(--fg-mut)')
  // The glyph agrees with the sentence the title already writes.
  assert.match(train.title, /recorded under repair 0 of 1; no later attempt has reported/)
})

test('a superseded SUCCESS does not keep its ✓ either, and is never promoted to a result', () => {
  const { rows } = stagePipelineView(e5Node9)
  const mine = rows[0]
  assert.equal(mine.superseded, true)
  // Both rows of this node are superseded, so one glyph has to serve `ok` and `fail` alike: it may
  // not assert that this attempt's success stands, and it may not assert the retry succeeded.
  assert.equal(mine.icon, STAGE_SUPERSEDED_ICON)
  assert.notEqual(mine.icon, '✓')
  assert.equal(rows[0].icon, rows[1].icon)
  // ... and it is not the `reused` glyph, which WOULD be a claim: a reuse is a later attempt saying
  // the result stands, and `stageRowSuperseded` is exactly the case where no later attempt spoke.
  assert.notEqual(mine.icon, stageIcon({ status: 'reused' }))
})

test('the replaced glyph keeps its own status reachable as text', () => {
  const { rows } = stagePipelineView(e5Node9)
  assert.equal(rows[1].iconLabel, stageSupersededLabel(e5Node9.stages[1]))
  assert.match(rows[1].iconLabel, /^fail under an earlier attempt; superseded/)
  assert.match(rows[0].iconLabel, /^ok under an earlier attempt; superseded/)
})

// ------------------------------------------------------------------ negative controls

test('a node whose last stage row IS its current state renders exactly as before', () => {
  const { rows, notice } = stagePipelineView(v9Node4)
  assert.equal(notice, null)                     // no banner at all
  assert.deepEqual(rows.map(r => r.superseded), [false, false, false])
  // Byte-for-byte against the tables the component used to inline.
  for (const [i, s] of v9Node4.stages.entries()) {
    assert.equal(rows[i].tone, stageTone(s))
    assert.equal(rows[i].icon, stageIcon(s))
    assert.equal(rows[i].iconLabel, null)        // no sr-only text is added to an unchanged glyph
    assert.equal(rows[i].title,
      `${s.name}: ${s.status}${s.seconds != null ? ` · ${s.seconds}s` : ''}`
      + `${s.exit_code != null ? ` · exit ${s.exit_code}` : ''}`)
  }
})

test('a failing strip that is CURRENT keeps its red and gets no banner', () => {
  const current = { status: 'failed', failed_stage: 'train', repairs: 2, stages: [
    { name: 'mine', status: 'ok', exit_code: 0, seconds: 10, repairs: 2 },
    { name: 'train', status: 'fail', exit_code: 1, seconds: 5, repairs: 2 }] }
  const { rows, notice } = stagePipelineView(current)
  assert.equal(notice, null)
  assert.deepEqual(rows.map(r => r.tone), ['var(--ok)', 'var(--fail)'])
})

test('a projection with no repairs key makes no claim in either direction', () => {
  // A reviewer scope, an older server, or the `/nodes/{id}` building branch: absent is not zero.
  const legacy = { status: 'pending', stages: [
    { name: 'train', status: 'fail', exit_code: 1, seconds: 5 }] }
  const { rows, notice } = stagePipelineView(legacy)
  assert.equal(notice, null)
  assert.equal(rows[0].superseded, false)
  assert.equal(rows[0].tone, 'var(--fail)')
})

test('a reused row two epochs old is NOT superseded — the fold carried its epoch forward', () => {
  // rubertlite-dr-unified-v8 node 3: `mine` ran once and every later attempt said `reused`, which
  // is that attempt's own statement that the result stands. Convicting it would be worse than the
  // defect, so the fold advances the row's epoch and the model sees no staleness here.
  const v8Node3 = { status: 'evaluated', repairs: 2, stages: [
    { name: 'mine', status: 'ok', exit_code: 0, seconds: 2304, repairs: 2 },
    { name: 'train', status: 'ok', exit_code: 0, seconds: 19915.75, repairs: 2 },
    { name: 'score', status: 'ok', exit_code: 0, seconds: 3130.3, repairs: 2 }] }
  assert.equal(stagePipelineView(v8Node3).notice, null)
})

// ------------------------------------------------------------------ the rule's truth table

test('stageRowSuperseded mirrors core/models.py::stage_row_superseded', () => {
  assert.equal(stageRowSuperseded({ repairs: 1 }, 2), true)
  assert.equal(stageRowSuperseded({ repairs: 2 }, 2), false)   // EQUAL is the current attempt
  assert.equal(stageRowSuperseded({ repairs: 0 }, 0), false)   // …the unrepaired node
  assert.equal(stageRowSuperseded({ repairs: 3 }, 1), false)   // a reset re-stamped
  assert.equal(stageRowSuperseded({}, 2), false)               // absent -> no claim
  assert.equal(stageRowSuperseded({ repairs: 1 }, null), false)
  assert.equal(stageRowSuperseded({ repairs: 1 }, undefined), false)
  assert.equal(stageRowSuperseded({ repairs: true }, 2), false)
  assert.equal(stageRowSuperseded({ repairs: '1' }, 2), false)
  assert.equal(stageRowSuperseded({ repairs: 1.5 }, 2), false)
  assert.equal(stageRowSuperseded(null, 2), false)
})

test('the notice is absent when nothing is superseded and never invents a subject', () => {
  assert.equal(stageSupersessionNotice([], { repairs: 3 }), null)
  assert.equal(stageSupersessionNotice([stageRowView(v9Node4.stages[0], 0)], { repairs: 0 }), null)
  const one = stageSupersessionNotice(
    [stageRowView({ name: 'train', status: 'fail', repairs: 0 }, 1)], { repairs: 1, status: 'pending' })
  assert.match(one.text, /1 of 1 stage result is from an earlier attempt/)
  assert.match(one.text, /repair 1 was applied after it/)
})

test('stageRowTitle adds nothing when the row is current', () => {
  const row = { name: 'train', status: 'ok', seconds: 3, exit_code: 0, repairs: 0 }
  assert.equal(stageRowTitle(row), 'train: ok · 3s · exit 0')
  assert.equal(stageRowTitle(row, { superseded: false, repairs: 0 }), 'train: ok · 3s · exit 0')
})

test('an empty or absent stages list yields no rows and no notice', () => {
  assert.deepEqual(stagePipelineView({ repairs: 3 }), { rows: [], notice: null, failedStage: null })
  assert.deepEqual(stagePipelineView(null), { rows: [], notice: null, failedStage: null })
})
