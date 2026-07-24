// Gap #4 from the current-master review follow-up: the Inspector concept-editor state was not keyed by
// run/node/attempt (an editor opened on A could submit A's draft to a later-selected B), and `canEdit`
// gated on concept-PROJECTION completeness rather than node LIFECYCLE (a still-building / reset-
// rebuilding node — folded status `pending` — could still expose Edit). Structural regressions, matching
// the repo's read-the-source UI test idiom. Run with `node --test test/` from ui/.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const inspector = () => readFile(new URL('../src/Inspector.jsx', import.meta.url), 'utf8')

test('ConceptTags is keyed on run/node/attempt so its draft resets on selection change', async () => {
  const src = await inspector()
  assert.ok(src.includes('<ConceptTags key={`${runId}:${n.id}:${n.attempt}`}'),
    'the concept re-tag editor must be remounted per run/node/attempt identity')
})

test('concept re-tag Edit is gated on a settled (terminal) node lifecycle, not projection alone', async () => {
  const src = await inspector()
  assert.ok(src.includes("&& (n.status === 'evaluated' || n.status === 'failed')"),
    'canEdit must require a terminal node status so a still-building / reset-rebuilding node stays display-only')
})
