// doc 77 §3: a recorded base file is offered in the same shaped view the Files panel gives it — a JSON
// pretty-printed, a CSV as a bounded table — beside its source. Only on request; the source stays default.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { mountLive, until, click } from './_mount.js'

const generation = 'a'.repeat(64), baseDigest = 'b'.repeat(64)
const bodies = { 'metrics.json': '{"recall":0.25,"ndcg":0.31}', 'train.py': 'print(1)\n' }
const rows = Object.entries(bodies).sort(([a], [b]) => a.localeCompare(b)).map(([path, text]) => ({
  path, bytes: Buffer.byteLength(text), executable: false, sha256: createHash('sha256').update(text).digest('hex'),
}))
const page = { version: 1, scope: 'recorded_seed_before_mounts_and_overlay', run_generation: generation,
  node_id: 1, attempt: 0, base_digest: baseDigest, offset: 0, limit: 100, total: rows.length,
  files: rows, next_offset: null, text_limit: 256 * 1024 }

test('a recorded JSON base file can be read formatted; a .py file offers no such switch', async () => {
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1/seed-files': ({ url }) => {
      const path = url.searchParams.get('path')
      return { ...page, file: path ? { ...rows.find(row => row.path === path), text_status: 'utf8', text: bodies[path] } : null }
    },
  } })
  localStorage.setItem('looplab.language', 'en')
  try {
    const { default: RecordedSeedFiles } = await h.load('/src/RecordedSeedFiles.jsx')
    const node = { id: 1, attempt: 0, code: '', files: {}, deleted: [] }
    const mounted = await h.mount(RecordedSeedFiles, { runId: 'r', node, generation, baseDigest, language: 'en' })
    const button = label => [...mounted.container.querySelectorAll('button')].find(b => b.textContent === label)
    await until(() => button('Open base files'), 'read control')
    await click(button('Open base files'))
    await until(() => button('metrics.json'), 'inventory')
    await click(button('metrics.json'))
    await until(() => button('Formatted'), 'format switch')
    assert.equal(button('Source').getAttribute('aria-pressed'), 'true', 'the source stays the default view')
    await click(button('Formatted'))
    // The shaped view is a lazy chunk: it arrives after the click, never with the Code tab.
    await until(() => mounted.container.querySelector('.art-pre'), 'the shaped view is drawn')
    const pre = mounted.container.querySelector('.art-pre')
    assert.match(pre.textContent, /"recall": 0.25/)
    await click(button('train.py'))
    await until(() => mounted.container.querySelector('[aria-label="Base: train.py"]'), 'python source')
    assert.equal(button('Formatted'), undefined, 'no formatted view for source code')
  } finally {
    await h.close()
  }
})
