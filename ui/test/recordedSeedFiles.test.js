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
    assert.equal(valid({ ...page, file: { ...row, text_status: 'too_large', text: null } }, identity, 0, 'train.py'), false)
    assert.equal(valid({ ...page, file: { ...row, path: 'unknown.py', text_status: 'utf8', text } }, identity, 0, 'unknown.py'), false)
    const nul = { ...row, bytes: 1, sha256: createHash('sha256').update('\0').digest('hex') }
    assert.equal(valid({ ...page, files: [nul], file: { ...nul, text_status: 'utf8', text: '\0' } }, identity, 0, 'train.py'), false)
  } finally { await h.close() }
})

test('real Inspector reads inherited files only on request, verifies text hash and drops the preview after node reset', async () => {
  const bodies = { 'train.py': text, 'recipe.env': 'MOMENTUM=0.2\n', 'old.py': 'old base file\n' }
  const archiveRows = Object.entries(bodies).sort(([a], [b]) => a.localeCompare(b)).map(([path, text]) => ({
    path, bytes: Buffer.byteLength(text), executable: false, sha256: createHash('sha256').update(text).digest('hex'),
  }))
  const archivePage = { ...page, total: archiveRows.length, files: archiveRows }
  const base = { version: 1, complete: true, digest: baseDigest, node_id: 1, generation: 0,
    seed_event_seq: 1, file_count: archiveRows.length, bytes: archiveRows.reduce((sum, file) => sum + file.bytes, 0),
    scope: 'seeded_editables_before_mounts_and_overlay',
    archive: { version: 1, status: 'stored', path: `base_snapshots/${baseDigest}` } }
  const node = { id: 1, attempt: 0, status: 'evaluated', feasible: true, metric: 0.5,
    parent_ids: [], operator: 'draft', idea: { params: {}, rationale: '' }, code: '',
    files: { 'recipe.env': 'MOMENTUM=0.3\n' }, deleted: ['old.py'], metric_provenance: { base_revision: base } }
  let corrupt = false
  const readUrls = []
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1': { ...node, run_generation: generation, annotations: [],
      confirm_seeds_detail: {}, trace: { nodes: [] }, trace_revision: null },
    '/api/runs/r/nodes/1/seed-files': ({ url }) => {
      readUrls.push(url)
      const path = url.searchParams.get('path')
      return { ...archivePage, file: path ? { ...archiveRows.find(file => file.path === path), text_status: 'utf8',
        text: corrupt ? bodies[path].replace('runner', 'broken') : bodies[path] } : null }
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
    await until(() => mounted.container.querySelector('[aria-label="База: train.py"]'), 'inherited text')
    assert.match(mounted.container.querySelector('[aria-label="База: train.py"]').textContent, /inherited runner/)
    assert.equal(document.activeElement.textContent, 'train.py', 'the selected preview receives focus')
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
    await until(() => mounted.container.querySelector('[aria-label="База: train.py"]'), 'preview recovered')
    const archive = () => mounted.container.querySelector('[aria-label="Записанные файлы базы"]')
    const archiveButton = name => [...archive().querySelectorAll('button')].find(button => button.textContent === name)
    await click(archiveButton('recipe.env'))
    await until(() => archive().querySelector('[aria-label="База: recipe.env"]'), 'base recipe')
    assert.match(archive().querySelector('[aria-label="База: recipe.env"]').textContent, /MOMENTUM=0.2/)
    assert.match(archive().textContent, /не включает её/)
    const beforeSwitch = h.fetch.calls.length
    await click(archiveButton('Правка опыта'))
    assert.match(archive().querySelector('[aria-label="Правка #1: recipe.env"]').textContent, /MOMENTUM=0.3/)
    assert.equal(archive().querySelector('[aria-label="База: recipe.env"]'), null)
    await click(archiveButton('Версия базы'))
    assert.match(archive().querySelector('[aria-label="База: recipe.env"]').textContent, /MOMENTUM=0.2/)
    assert.equal(h.fetch.calls.length, beforeSwitch, 'switching already read file versions issues no request')
    await click(archiveButton('old.py'))
    await until(() => archive().querySelector('[aria-label="База: old.py"]'), 'deleted base file remains an explicitly labelled archive')
    assert.match(archive().textContent, /прежний файл из архива базы, а не восстановленный файл опыта/)
    assert.equal(archiveButton('Правка опыта'), undefined)
    await mounted.rerender(props({ ...initial, nodes: { 1: { ...node, attempt: 1, status: 'pending' } } }))
    assert.equal(open(), undefined)
    assert.equal(mounted.container.querySelector('[aria-label="База: old.py"]'), null)
    const review = await h.mount(Inspector, { ...props(initial), readOnly: true })
    await until(() => /recipe.env/.test(review.container.textContent), 'read-only overlay remains readable')
    assert.doesNotMatch(review.container.textContent, /Открыть файлы базы/)
    assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
  } finally { localStorage.removeItem('looplab.language'); await h.close() }
})

test('archive text refuses encoder replacement even when its replacement bytes match the recorded hash', async () => {
  // JSON permits lone UTF-16 surrogates. TextEncoder replaces them with U+FFFD, so hashing
  // alone would authenticate different text. The server's strict UTF-8 decoder cannot emit them.
  let source = '\ufffd'
  let wire = source
  const response = path => {
    const file = { ...row, bytes: Buffer.byteLength(source),
      sha256: createHash('sha256').update(source).digest('hex') }
    return { ...page, files: [file], file: path ? { ...file, text_status: 'utf8', text: wire } : null }
  }
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1/seed-files': ({ url }) => response(url.searchParams.get('path')),
  } })
  const previousClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  const copied = []
  Object.defineProperty(navigator, 'clipboard', { configurable: true,
    value: { writeText: async value => copied.push(value) } })
  try {
    const { default: RecordedSeedFiles, validSeedFilesPage: valid } = await h.load('/src/RecordedSeedFiles.jsx')
    for (const malformed of ['\ud800', '\udfff', 'a\ud800b', '\ud800\ud800', '\udfff\udfff']) {
      wire = JSON.parse(JSON.stringify(malformed))
      source = new TextDecoder().decode(new TextEncoder().encode(wire))
      assert.equal(valid(response('train.py'), identity, 0, 'train.py'), false,
        'a matching hash of replacement bytes cannot validate malformed Unicode')
    }
    source = '\ufffd'; wire = '\ud800'
    const mounted = await h.mount(RecordedSeedFiles, { runId: 'r', node: { id: 1, attempt: 0 },
      generation, baseDigest, language: 'ru' })
    const button = name => [...mounted.container.querySelectorAll('button')].find(item => item.textContent === name)
    await click(button('Открыть файлы базы'))
    await until(() => button('train.py'), 'inventory')
    await click(button('train.py'))
    await until(() => /Архив или его квитанция недоступны/.test(mounted.container.textContent), 'malformed Unicode refused')
    assert.equal(mounted.container.querySelector('[aria-label="База: train.py"]'), null)
    for (const genuine of ['\ufffd', 'кириллица \ud83d\ude80\n', '\ufeffBOM\r\n', '']) {
      source = genuine; wire = genuine
      assert.equal(valid(response('train.py'), identity, 0, 'train.py'), true)
      await click(button('Открыть файлы базы'))
      await until(() => button('train.py'), 'explicit inventory recovery')
      await click(button('train.py'))
      await until(() => mounted.container.querySelector('[aria-label="База: train.py"]'), 'exact UTF-8 preview')
      // CodeViewer renders separate lines. Copy must preserve BOM, CRLF and an empty file exactly.
      await click(button('Копировать') || button('Скопировано'))
      assert.equal(copied.at(-1), genuine)
    }
    assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
  } finally {
    if (previousClipboard) Object.defineProperty(navigator, 'clipboard', previousClipboard)
    else delete navigator.clipboard
    await h.close()
  }
})

test('a delayed archive read does not take focus from another input; missing edits remain explicitly unknown', async () => {
  let finish
  const h = await mountLive({ visible: true, routes: {
    '/api/runs/r/nodes/1/seed-files': ({ url }) => url.searchParams.has('path')
      ? new Promise(resolve => { finish = () => resolve({ ...page, file: { ...row, text_status: 'utf8', text } }) })
      : page,
  } })
  try {
    const { default: RecordedSeedFiles } = await h.load('/src/RecordedSeedFiles.jsx')
    const mounted = await h.mount(RecordedSeedFiles, { runId: 'r', node: { id: 1, attempt: 0 },
      generation, baseDigest, language: 'en' })
    const button = name => [...mounted.container.querySelectorAll('button')].find(button => button.textContent === name)
    await click(button('Open base files'))
    await until(() => button('train.py'), 'inventory')
    assert.match(mounted.container.textContent, /Edits unavailable/)
    button('train.py').focus()
    await click(button('train.py'))
    const input = document.createElement('input')
    mounted.container.appendChild(input)
    input.focus()
    finish()
    await until(() => mounted.container.querySelector('[aria-label="Base: train.py"]'), 'delayed preview')
    assert.equal(document.activeElement, input)
    assert.match(mounted.container.textContent, /base version cannot establish the final experiment file/)
    input.remove()
  } finally { await h.close() }
})
