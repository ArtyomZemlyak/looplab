import test from 'node:test'
import assert from 'node:assert/strict'
import { nodeCodeModel, recordedFileOverlay } from '../src/nodeCodeModel.js'
import { mountLive, until, click } from './_mount.js'

const generation = 'a'.repeat(64)
const base = (id, digest) => ({ version: 1, complete: true, digest,
  node_id: id, generation: 0, seed_event_seq: 1, file_count: 1, bytes: 10,
  scope: 'seeded_editables_before_mounts_and_overlay',
  archive: { version: 1, status: 'stored', path: `base_snapshots/${digest}` } })
const parent = { id: 0, attempt: 0, status: 'evaluated' }
const child = { id: 1, attempt: 0, status: 'evaluated', feasible: true, metric: 0.5,
  operator: 'improve', parent_ids: [0], idea: { params: {}, rationale: '' }, code: '',
  files: { 'recipe.env': 'MOMENTUM=0.3\n', 'new.py': "print('new')\n" }, deleted: ['old.py'],
  metric_provenance: { base_revision: base(1, 'b'.repeat(64)) },
  parent_edit: { version: 1, scope: 'node_edit_overlay', node_id: 0, attempt: 0, code: '',
    files: { 'recipe.env': 'MOMENTUM=0.2\n', 'old.py': "print('old')\n", 'inherited.py': 'override\n' },
    deleted: ['disabled.py'], base_revision: base(0, 'a'.repeat(64)) } }
const state = { run_id: 'r', direction: 'min', nodes: { 0: parent, 1: child } }

test('archived file context distinguishes empty edits, deletion and unavailable overlay evidence', () => {
  assert.deepEqual(recordedFileOverlay(child, 'recipe.env'), { kind: 'override', text: 'MOMENTUM=0.3\n' })
  assert.deepEqual(recordedFileOverlay({ ...child, files: { 'recipe.env': '' } }, 'recipe.env'), { kind: 'override', text: '' })
  assert.deepEqual(recordedFileOverlay({ ...child, files: { 'old.py': 'conflicting edit' } }, 'old.py'), { kind: 'deleted', text: null })
  assert.equal(recordedFileOverlay(child, 'train.py').kind, 'inherited')
  assert.equal(recordedFileOverlay({ ...child, files: undefined }, 'train.py').kind, 'unknown')
  assert.equal(recordedFileOverlay({ ...child, deleted: undefined }, 'train.py').kind, 'unknown')
})

test('archived solution.py identifies separately saved main code rather than claiming no edit', () => {
  const node = { ...child, code: 'print(2)\n', files: {}, deleted: [] }
  assert.deepEqual(recordedFileOverlay(node, 'solution.py'), { kind: 'main_code', text: node.code })
  assert.deepEqual(recordedFileOverlay({ ...node, files: { 'solution.py': 'ignored helper edit' },
    deleted: ['solution.py'] }, 'solution.py'), { kind: 'main_code', text: node.code })
  assert.equal(recordedFileOverlay(node, 'pkg/solution.py').kind, 'inherited')
  assert.equal(recordedFileOverlay({ ...node, code: '' }, 'solution.py').kind, 'inherited')
  assert.equal(recordedFileOverlay({ ...node, code: null }, 'solution.py').kind, 'inherited')
  assert.equal(recordedFileOverlay({ ...node, files: undefined }, 'solution.py').kind, 'unknown')
})

test('file comparison distinguishes removed overrides from explicit deletion and includes recipe-only edits', () => {
  const model = nodeCodeModel(child, state)
  assert.equal(model.available, true)
  assert.equal(model.mainChanged, false)
  const row = path => model.rows.find(row => row.path === path)
  assert.equal(row('recipe.env').changed, true)
  assert.equal(row('recipe.env').oldText, 'MOMENTUM=0.2\n')
  assert.equal(row('recipe.env').newText, 'MOMENTUM=0.3\n')
  assert.equal(row('old.py').newKind, 'deleted')
  assert.equal(row('inherited.py').newKind, 'inherited')
  assert.equal(row('disabled.py').oldKind, 'deleted')
  assert.equal(row('disabled.py').newKind, 'inherited')
  assert.equal(row('new.py').oldKind, 'inherited')
  assert.notEqual(model.base.digest, model.parentBase.digest)
})

test('incomplete source, reset, missing parent and wrong identity cannot become an empty successful diff', () => {
  for (const parent_edit of [null, { ...child.parent_edit, files: undefined },
    { ...child.parent_edit, deleted: undefined }, { ...child.parent_edit, node_id: 2 },
    { ...child.parent_edit, version: 2 }]) {
    assert.equal(nodeCodeModel({ ...child, parent_edit }, state).available, false)
  }
  assert.equal(nodeCodeModel(child, { nodes: {} }).available, false)
  assert.equal(nodeCodeModel(child, { nodes: { 0: { ...parent, attempt: 1 } } }).available, false)
  assert.equal(nodeCodeModel({ ...child, files: undefined }, state).available, false)
  assert.equal(nodeCodeModel(child, { nodes: { 0: { ...parent, tombstoned: true } } }).available, false)
})

test('real Inspector renders RU repo files, changed recipe and deletion states; a parent reset removes stale diff', async () => {
  const detail = { ...child, run_generation: generation, annotations: [],
    confirm_seeds_detail: {}, trace: { nodes: [] }, trace_revision: null }
  const h = await mountLive({ visible: true, routes: { '/api/runs/r/nodes/1': detail } })
  localStorage.setItem('looplab.language', 'ru')
  try {
    const { default: Inspector } = await h.load('/src/Inspector.jsx')
    const props = snapshot => ({ runId: 'r', nodeId: 1, state: snapshot, live: null,
      tab: 'Code', setTab() {}, onToast() {}, expectedGeneration: generation })
    const mounted = await h.mount(Inspector, props(state))
    await until(() => mounted.container.querySelector('[aria-label="Код эксперимента"]'), 'code detail')
    const code = () => mounted.container.querySelector('[aria-label="Код эксперимента"]')
    assert.match(code().textContent, /Файлы эксперимента/)
    assert.doesNotMatch(code().textContent, /no solution.py|Helper files|Основной код/)
    await click(code().querySelector('.code-toolbar button'))
    const recipe = code().querySelector('[aria-label="recipe.env"]')
    assert.match(recipe.querySelector('.diff-del').textContent, /MOMENTUM=0.2/)
    assert.match(recipe.querySelector('.diff-add').textContent, /MOMENTUM=0.3/)
    assert.match(code().textContent, /Базы различаются/)
    assert.match(code().textContent, /inherited.py · Возврат к файлу базы/)
    assert.match(code().textContent, /old.py · Удаление файла/)
    assert.match(code().textContent, /disabled.py · Возврат к файлу базы/)
    assert.match(code().textContent, /new.py · Добавлена правка/)
    const restored = code().querySelector('[aria-label="inherited.py"]').closest('.code-viewer')
    assert.equal([...restored.querySelectorAll('button')].some(button => button.textContent === 'Копировать'), false,
      'a removed override cannot offer an empty replacement as copied code')
    assert.match(code().textContent, /Перенос строк/)
    assert.equal(h.fetch.calls.filter(call => call.path === '/api/runs/r/nodes/1').length, 1)

    await mounted.rerender(props({ ...state, nodes: { ...state.nodes, 0: { ...parent, attempt: 1 } } }))
    assert.match(code().textContent, /Сравнение недоступно/)
    assert.equal(code().querySelector('.code-toolbar button').disabled, true)
    assert.equal(code().querySelector('[aria-label="recipe.env"] .diff-del'), null)
    assert.doesNotMatch(code().textContent, /MOMENTUM=0.2/)
    assert.ok(h.fetch.calls.every(call => call.method === 'GET'))
  } finally { localStorage.removeItem('looplab.language'); await h.close() }
})

test('script edits retain a main-code comparison and unchanged overlays have no invented changes', () => {
  const script = { ...child, code: 'print(2)\n', files: {}, deleted: [],
    parent_edit: { ...child.parent_edit, code: 'print(1)\n', files: {}, deleted: [] } }
  assert.equal(nodeCodeModel(script, state).mainChanged, true)
  assert.equal(nodeCodeModel(script, state).oldCode, 'print(1)\n')
  assert.equal(nodeCodeModel(script, state).code, 'print(2)\n')
  const unchanged = nodeCodeModel({ ...script, code: 'print(1)\n' }, state)
  assert.equal(unchanged.mainChanged, false)
  assert.equal(unchanged.rows.some(row => row.changed), false)
})
