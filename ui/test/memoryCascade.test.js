// "If I delete runs, do the lessons and knowledge go too?"
//
// No — and the answer is now a checkbox rather than a paragraph. The rule the operator set is
// narrow: delete only what belongs to the deleted run ALONE; anything merged (their example was
// concepts) stays. The server decides what qualifies; this file pins how that answer is STATED,
// because a cascade the operator cannot see the extent of is a cascade nobody consented to.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  cascadeKeptNotice, cascadeLabel, cascadeOutcome, cascadeStores,
} from '../src/memoryCascadeModel.js'

const report = (extra = {}) => ({
  available: true,
  deletable: 4,
  kept: 2,
  stores: [
    { store: 'lessons', file: 'lessons.jsonl', deletable: 1, kept: 2, reasons: [
      { reason: 'consolidated: it carries evidence from other runs', rows: 2 }] },
    { store: 'notes', file: 'meta_notes.jsonl', deletable: 1, kept: 0, reasons: [] },
    { store: 'claims', file: 'research_claims.jsonl', deletable: 2, kept: 0, reasons: [] },
    { store: 'skills', file: 'skills', deletable: 0, kept: 0, reasons: [] },
  ],
  ...extra,
})

test('the label counts what would go, so the checkbox is never an unknown quantity', () => {
  assert.match(cascadeLabel(report()), /4 rows/)
  assert.match(cascadeLabel(report({ deletable: 1 })), /1 row\)/, 'singular')
})

test('the kept rows and their reason are stated, not left to be inferred from a smaller number', () => {
  // Showing "4 rows" alone would invite the reader to conclude the run's whole footprint is going.
  const notice = cascadeKeptNotice(report())
  assert.match(notice, /2 rows stay/)
  assert.match(notice, /carries evidence from other runs/)
})

test('several distinct reasons collapse to one honest sentence rather than a list of jargon', () => {
  const mixed = report({ stores: [
    { store: 'lessons', deletable: 0, kept: 1, reasons: [{ reason: 'consolidated: x', rows: 1 }] },
    { store: 'capsules', deletable: 0, kept: 1, reasons: [{ reason: 'concepts merged', rows: 1 }] },
  ] })
  assert.match(cascadeKeptNotice(mixed), /shared with runs that still exist/)
})

test('nothing held back means no reassurance at all', () => {
  // Noise beside a destructive control is worse than noise anywhere else.
  assert.equal(cascadeKeptNotice(report({ kept: 0, stores: [] })), '')
  assert.equal(cascadeKeptNotice(null), '')
})

test('a run that contributed nothing says so instead of offering an empty deletion', () => {
  assert.match(cascadeLabel(report({ deletable: 0, kept: 0, stores: [] })), /contributed none/)
})

test('an unconfigured memory store is named, never silently shown as "nothing to delete"', () => {
  // The two look identical to a reader and mean opposite things: one run wrote no memory, the other
  // means this deployment has no cross-run memory at all.
  assert.match(cascadeLabel({ available: false, deletable: 0, kept: 0 }), /no memory store/)
})

test('an unread survey reads as unread, not as zero', () => {
  assert.equal(cascadeLabel(null), 'Also delete this run’s own cross-run memory')
})

test('only stores with rows are listed, and the counts survive junk', () => {
  const stores = cascadeStores(report())
  assert.deepEqual(stores.map(s => s.store), ['lessons', 'notes', 'claims'])
  assert.deepEqual(cascadeStores(null), [])
  assert.deepEqual(cascadeStores({ stores: [{ deletable: 'two' }] }), [])
})

test('the outcome distinguishes "purged", "had nothing" and "partly failed"', () => {
  assert.match(cascadeOutcome({ ok: true, deleted: 3 }).text, /3 cross-run memory rows only it owned/)
  assert.match(cascadeOutcome({ ok: true, deleted: 0 }).text, /contributed no cross-run memory/)
  assert.equal(cascadeOutcome(null), null, 'no cascade ran: the caller keeps its own wording')
})

test('a partly-failed purge carries a retry handle, because the run card is already gone', () => {
  // This is the whole reason the outcome is a record. The run is deleted, its card has left the
  // list, and this notice is the last surface that can offer to finish the job.
  const outcome = cascadeOutcome(
    { ok: false, deleted: 1, failures: [{ store: 'lessons' }] }, 'doomed')
  assert.equal(outcome.kind, 'error')
  assert.equal(outcome.retryRunId, 'doomed')
  assert.match(outcome.text, /only partly removed/)
  assert.match(outcome.text, /lessons/)
  assert.ok(!/1 cross-run memory row/.test(outcome.text),
    'never report a count as if the purge completed')
  assert.equal(cascadeOutcome({ ok: true, deleted: 2 }).retryRunId, '',
    'a successful purge offers no retry')
})

test('the dialog reads the survey through the deadline HANDLE, never by awaiting it', () => {
  // `deadlineRequest` returns `{controller, promise, timedOut}`. Calling `.then` on the handle
  // throws inside the effect, and the section would render a permanent "Checking…" — the exact
  // defect the card-trace fetches shipped with, which no test caught because nothing mounts them.
  const source = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  // The CALL SITE, not the import at the top of the file.
  const at = source.indexOf('getRunMemoryAttribution(deleteDialogRunId')
  assert.ok(at > 0, 'the cascade preview fetch is gone')
  const block = source.slice(at - 300, at + 700)
  assert.match(block, /request\.promise/)
  assert.match(block, /request\.controller\.abort\(\)/)
})

test('the cascade is opt-in at every layer that could default it on', () => {
  const source = readFileSync(new URL('../src/RunList.jsx', import.meta.url), 'utf8')
  assert.match(source, /useState\(false\)[\s\S]{0,80}memoryRetryBusy|setDeleteCascade\(false\)/)
  assert.ok(source.includes('setDeleteCascade(false); setDeleteMemoryReport(null)'),
    'each open must reset the checkbox — consent does not carry from one run to another')
  const api = readFileSync(new URL('../src/api.js', import.meta.url), 'utf8')
  assert.ok(api.includes('if (options.deleteMemory === true) body.delete_memory = true'),
    'the flag must be sent only when asked for: the server body is a strict key set')
})
