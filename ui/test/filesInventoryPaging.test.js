// UI review 2026-10-14 section 7.6: Files lists one experiment's workdir on request, and pages past a
// root's first listing instead of stopping at it ("Listing stopped at 1500 files").
import assert from 'node:assert/strict'
import test from 'node:test'

import React from 'react'

import { previewKind } from '../src/artifactPreviewModel.js'
import { fetchStub, mountLive, settle } from './_mount.js'

const GENERATION = 'd'.repeat(64)
const root = (files, extra = {}) => ({
  id: 'run', label: 'run directory', path: '/runs/demo', is_run_dir: true, visibility: 'run_workspace',
  n_files: files.length, files, truncated: false, next_offset: null, offset: 0, ...extra,
})
const file = path => ({ path, size: 1, is_text: true })

let harness
let ArtifactsPanel
test.before(async () => {
  harness = await mountLive({ visible: true })
  ;({ ArtifactsPanel } = await harness.load('/src/panels.jsx'))
})
test.after(async () => { await harness?.close() })

test('a PDF is a pdf preview whatever its body held; SVG stays metadata', () => {
  assert.equal(previewKind('report.pdf', { is_text: false }), 'pdf')
  assert.equal(previewKind('report.pdf', { is_text: true, content: '%PDF-1.4' }), 'pdf')
  assert.equal(previewKind('plot.svg', { is_text: false }), 'meta')
})

test('the experiment filter and Load more ask the server for exactly that page', async () => {
  const stub = fetchStub({
    '/api/runs/demo/artifacts': ({ url }) => {
      const nodeId = url.searchParams.get('node_id'), offset = url.searchParams.get('offset')
      if (nodeId === '3') return { run_id: 'demo', run_generation: GENERATION,
        roots: [root([file('nodes/node_3/out.txt')], { node_id: 3 })] }
      if (offset === '2') return { run_id: 'demo', run_generation: GENERATION,
        roots: [root([file('c.txt')], { offset: 2 })] }
      return { run_id: 'demo', run_generation: GENERATION,
        roots: [root([file('a.txt'), file('b.txt')], { truncated: true, next_offset: 2 })] }
    },
  })
  globalThis.fetch = stub
  const view = await harness.mount(ArtifactsPanel, { runId: 'demo', expectedGeneration: GENERATION, nodeIds: [0, 3] })
  try {
    await settle()
    const rows = () => [...view.container.querySelectorAll('.art-file .art-name')].map(node => node.textContent)
    assert.deepEqual(rows(), ['a.txt', 'b.txt'])
    const more = [...view.container.querySelectorAll('button')].find(b => b.textContent === 'Load more files')
    assert.ok(more, 'a truncated root offers the next page')
    await React.act(async () => { more.click() })
    await settle()
    assert.deepEqual(rows(), ['a.txt', 'b.txt', 'c.txt'])
    assert.ok(![...view.container.querySelectorAll('button')].some(b => b.textContent === 'Load more files'))
    const select = view.container.querySelector('.art-node-filter select')
    const setValue = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(select), 'value').set
    await React.act(async () => {
      setValue.call(select, '3')
      select.dispatchEvent(new window.Event('change', { bubbles: true }))
    })
    await settle()
    assert.deepEqual(rows(), ['nodes/node_3/out.txt'])
    const queries = stub.calls.filter(call => call.path === '/api/runs/demo/artifacts').length
    assert.ok(queries >= 3)
  } finally { view.unmount?.() }
})
