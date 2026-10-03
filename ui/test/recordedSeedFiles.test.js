import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mountLive, until, click } from './_mount.js'

const generation = 'a'.repeat(64), baseDigest = 'b'.repeat(64)
const text = "print('inherited runner')\n"
const row = { path: 'train.py', bytes: Buffer.byteLength(text), executable: false,
  sha256: createHash('sha256').update(text).digest('hex') }
const page = { version: 1, scope: 'recorded_seed_before_mounts_and_overlay', run_generation: generation,
  node_id: 1, attempt: 0, base_digest: baseDigest, offset: 0, limit: 100, total: 1,
  files: [row], next_offset: null, text_limit: 256 * 1024, file: null }
const identity = { generation, nodeId: 1, attempt: 0, baseDigest }

test('strict archived page read rejects incomplete identity/pagination/text rather than accepting an empty result', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { validSeedFilesPage: valid } = await h.load('/src/RecordedSeedFiles.jsx')
    assert.equal(valid(page, identity, 0, null), true)
    for (const bad of [{ ...page, files: undefined }, { ...page, next_offset: 1 },
      { ...page, attempt: 1 }, { ...page, base_digest: 'c'.repeat(64) },
      { ...page, file: undefined }, { ...page, version: 2 }, { ...page, limit: 200 },
      { ...page, total: 200, next_offset: 1 }]) {
      assert.equal(valid(bad, identity, 0, null), false)
    }
    assert.equal(valid({ ...page, file: { ...row, text_status: 'utf8', text: text.slice(1) } }, identity, 0, 'train.py'), false)
    assert.equal(Boolean(valid({ ...page, file: { ...row, sha256: 'c'.repeat(64), text_status: 'utf8', text } }, identity, 0, 'train.py')), false)
  } finally { await h.close() }
})

test('real Inspector reads inherited files only on request, verifies text hash and drops the preview after node reset', async () => {
  const base = { version: 1, complete: true, digest: baseDigest, node_id: 1, generation: 0,
    seed_event_seq: 1, file_count: 1, bytes: row.bytes,
    scope: 'seeded_editables_before_mounts_and_overlay',
    archive: { version: 1, status: 'stored', path: `base_snapshots/${baseDigest}` } }
  const node = { id: 1, attempt: 0, status: 'evaluated', feasible: true, metric: 0.5,
    parent_ids: [], operator: 'draft', idea: { params: {}, rationale: '' }, code: '',
    files: { 'recipe.env': 'MOMENTUM=0.3\n' }, deleted: [], metric_provenance: { base_revision: base } }
  let corrupt = false
  const readUrls = []
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1': { ...node, run_generation: generation, annotations: [],
      confirm_seeds_detail: {}, trace: { nodes: [] }, trace_revision: null },
    '/api/runs/r/nodes/1/seed-files': ({ url }) => {
      readUrls.push(url)
      return { ...page, file: url.searchParams.has('path') ? { ...row, text_status: 'utf8',
        text: corrupt ? text.replace('runner', 'broken') : text } : null }
    },
  } })
  localStorage.setItem('looplab.language', 'ru')
  try {
    const { default: Inspector } = await h.load('/src/Inspector.jsx')
    const initial = { run_id: 'r', direction: 'min', nodes: { 1: node } }
    const props = state => ({ runId: 'r', nodeId: 1, state, live: null, tab: 'Code',
      setTab() {}, onToast() {}, expectedGeneration: generation })
    const mounted = await h.mount(Inspector, props(initial))
    const open = () => [...mounted.container.querySelectorAll('button')].find(button => button.textContent === 'Открыть файлы базы')
    await until(open, 'archive read control')
    assert.equal(h.fetch.calls.some(call => call.path.endsWith('/seed-files')), false)
    await click(open())
    const fileButton = () => [...mounted.container.querySelectorAll('button')].find(button => button.textContent === 'train.py')
    await until(fileButton, 'verified inventory')
    await click(fileButton())
    await until(() => mounted.container.querySelector('[aria-label="train.py"]'), 'inherited text')
    assert.match(mounted.container.querySelector('[aria-label="train.py"]').textContent, /inherited runner/)
    const calls = h.fetch.calls.filter(call => call.path.endsWith('/seed-files'))
    assert.equal(calls.length, 2)
    assert.ok(readUrls.every(url => url.searchParams.get('expected_generation') === generation
      && url.searchParams.get('attempt') === '0'))
    corrupt = true
    await click(fileButton())
    await until(() => /Архив или его квитанция недоступны/.test(mounted.container.textContent), 'hash mismatch refused')
    assert.doesNotMatch(mounted.container.textContent, /inherited broken|inherited runner/)
    corrupt = false
    await click(open())
    await until(fileButton, 'inventory recovered by an explicit read')
    await click(fileButton())
    await until(() => mounted.container.querySelector('[aria-label="train.py"]'), 'preview recovered')
    await mounted.rerender(props({ ...initial, nodes: { 1: { ...node, attempt: 1, status: 'pending' } } }))
    assert.equal(open(), undefined)
    assert.equal(mounted.container.querySelector('[aria-label="train.py"]'), null)
    const review = await h.mount(Inspector, { ...props(initial), readOnly: true })
    await until(() => /recipe.env/.test(review.container.textContent), 'read-only overlay remains readable')
    assert.doesNotMatch(review.container.textContent, /Открыть файлы базы/)
    assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
  } finally { localStorage.removeItem('looplab.language'); await h.close() }
})
