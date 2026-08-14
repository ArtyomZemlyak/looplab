// The browser half of the incomplete-record receipt (server side: `eventstore.py::log_integrity`,
// `tests/test_seq_gap_visibility.py`). The rule that matters is the DEFAULT: an absent receipt must
// read as "nothing claimed", never as "corrupt" — a legacy server, or any payload this field has not
// reached yet, must not paint every run with a warning nobody can act on.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { sourceIncomplete, sourceIntegrityNotice } from '../src/runIndex.js'

// The real corpus row, from the run this whole change exists for.
const RUBERT = { run_id: 'rubertlite-dense-retrieval', nodes: 2, best_metric: 0.8077,
  source_integrity: { complete: false, good_records: 20, corrupt_line: 21, dropped_lines: 1603 } }

test('an absent or complete receipt is silent', () => {
  assert.equal(sourceIncomplete({}), false)                       // legacy server: nothing claimed
  assert.equal(sourceIncomplete({ source_integrity: null }), false)
  assert.equal(sourceIncomplete({ source_integrity: { complete: true } }), false)
  // The wire shape carries the detail keys as null on a healthy log; still silent.
  assert.equal(sourceIncomplete({ source_integrity: {
    complete: true, good_records: null, corrupt_line: null, dropped_lines: null } }), false)
  assert.equal(sourceIntegrityNotice({ source_integrity: { complete: true } }), '')
})

test('an incomplete receipt names the boundary, the fraction, and what is behind it', () => {
  assert.equal(sourceIncomplete(RUBERT), true)
  const note = sourceIntegrityNotice(RUBERT)
  assert.match(note, /line 21/)
  assert.match(note, /20 of 1624 records/)      // good + the boundary row + dropped == the file
  assert.match(note, /1603 durable record\(s\)/)
  // It must not let the reader convert a truncated view into an absence claim — the row it decorates
  // says `nodes: 2` about a run whose own budget event says 81.
  assert.match(note, /not evidence that the rest did not happen/)
})

test('an unreadable log is incomplete and says only that', () => {
  const run = { source_integrity: { complete: false, unreadable: true } }
  assert.equal(sourceIncomplete(run), true)
  assert.match(sourceIntegrityNotice(run), /could not be read at all/)
})

test('a receipt missing its numbers still refuses to claim completeness', () => {
  // Fail toward "we cannot show you this run": `complete !== true` is the whole test, and a
  // half-formed receipt degrades to a shorter sentence rather than to silence.
  const run = { source_integrity: { complete: false } }
  assert.equal(sourceIncomplete(run), true)
  assert.match(sourceIntegrityNotice(run), /Part of the log is not shown/)
})

test('both surfaces render the receipt, and the run banner is an alert', () => {
  // A source scan is the weak tier (CLAUDE.md), used here for what only the JSX text can show: that
  // the two render sites exist and which ARIA role each carries. The decision itself is driven above.
  const list = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  const view = readFileSync(new URL('../src/RunView.jsx', import.meta.url), 'utf8')
  assert.ok(list.includes('sourceIncomplete(r) && <div className="pill warn" role="status"'))
  // `alert`, not `status`: this one says the run on screen is not the run on disk.
  assert.ok(view.includes('{sourceIncomplete(live) && <div className="review-banner" role="alert"'))
  assert.ok(view.includes('data-run-source-incomplete'))
})
