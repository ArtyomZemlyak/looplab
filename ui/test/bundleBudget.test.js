import assert from 'node:assert/strict'
import { resolve } from 'node:path'
import test from 'node:test'

import {
  collectAssetFiles,
  collectStaticClosure,
  evaluateBundle,
  findStaticPath,
  formatBundleReport,
  measureAssetBuffer,
  safeAssetPath,
  selectChunks,
  validateManifestGraph,
} from '../scripts/check-bundle.mjs'

const manifest = () => ({
  'index.html': {
    file: 'assets/entry.js', src: 'index.html', name: 'index', isEntry: true,
    imports: ['_shared.js'], dynamicImports: ['src/RunList.jsx'], css: ['assets/base.css'],
  },
  '_shared.js': { file: 'assets/shared.js', name: 'shared' },
  'src/RunList.jsx': {
    file: 'assets/list.js', src: 'src/RunList.jsx', name: 'RunList', isDynamicEntry: true,
    imports: ['_shared.js'], css: ['assets/base.css'],
  },
  'vendor-flow': { file: 'assets/vendor-flow.js', name: 'vendor-flow' },
  'src/panels.jsx': {
    file: 'assets/panels.js', src: 'src/panels.jsx', name: 'panels', isDynamicEntry: true,
  },
})

const stats = () => new Map([
  ['assets/entry.js', { file: 'assets/entry.js', kind: 'js', raw: 100, gzip: 40 }],
  ['assets/shared.js', { file: 'assets/shared.js', kind: 'js', raw: 80, gzip: 30 }],
  ['assets/list.js', { file: 'assets/list.js', kind: 'js', raw: 70, gzip: 25 }],
  ['assets/vendor-flow.js', { file: 'assets/vendor-flow.js', kind: 'js', raw: 90, gzip: 35 }],
  ['assets/panels.js', { file: 'assets/panels.js', kind: 'js', raw: 60, gzip: 20 }],
  ['assets/base.css', { file: 'assets/base.css', kind: 'css', raw: 50, gzip: 15 }],
])

const generous = () => ({
  total: { js: { raw: 1_000, gzip: 1_000 }, css: { raw: 1_000, gzip: 1_000 } },
  individual: { js: { raw: 1_000, gzip: 1_000 }, css: { raw: 1_000, gzip: 1_000 } },
  closures: [{
    name: 'List', roots: [{ entry: true }, { src: 'src/RunList.jsx' }],
    limits: { js: { raw: 1_000, gzip: 1_000 }, css: { gzip: 1_000 } },
  }],
  forbidden: [{
    name: 'List defers Flow', roots: [{ entry: true }, { src: 'src/RunList.jsx' }],
    targets: [{ name: 'vendor-flow' }], requireTargets: true,
  }],
})

test('static closure excludes dynamic imports and deduplicates shared chunks and CSS', () => {
  const graph = manifest()
  const initial = collectStaticClosure(graph, ['index.html'])
  assert.deepEqual(initial.keys, ['_shared.js', 'index.html'])

  const list = collectStaticClosure(graph, ['index.html', 'src/RunList.jsx'])
  assert.deepEqual(list.keys, ['_shared.js', 'index.html', 'src/RunList.jsx'])
  assert.deepEqual(collectAssetFiles(graph, list.keys), [
    'assets/base.css', 'assets/entry.js', 'assets/list.js', 'assets/shared.js',
  ])
})

test('selectors and diagnostics identify an exact forbidden static import chain', () => {
  const graph = manifest()
  graph['src/RunList.jsx'].imports.push('vendor-flow')
  assert.deepEqual(selectChunks(graph, { src: 'src/RunList.jsx' }), ['src/RunList.jsx'])
  assert.deepEqual(selectChunks(graph, { contains: 'VENDOR-FLOW' }), ['vendor-flow'])
  assert.deepEqual(
    findStaticPath(graph, ['index.html', 'src/RunList.jsx'], ['vendor-flow']),
    ['src/RunList.jsx', 'vendor-flow'],
  )
})

test('a split graph passes and closure totals count shared assets once', () => {
  const result = evaluateBundle({ manifest: manifest(), assetStats: stats(), budgets: generous() })
  assert.deepEqual(result.violations, [])
  assert.equal(result.totals.js.raw, 400)
  assert.equal(result.totals.js.gzip, 150)
  assert.equal(result.totals.css.gzip, 15)
  const list = result.closures[0]
  assert.equal(list.summary.js.raw, 250)
  assert.equal(list.summary.js.gzip, 95)
  assert.equal(list.summary.css.gzip, 15)
})

test('an interaction increment excludes only assets proven present in its baseline closure', () => {
  const budgets = generous()
  budgets.closures[0].baselineRoots = [{ entry: true }]
  const result = evaluateBundle({ manifest: manifest(), assetStats: stats(), budgets })
  assert.deepEqual(result.violations, [])
  const list = result.closures[0]
  assert.deepEqual(list.files, ['assets/list.js'])
  assert.equal(list.summary.js.raw, 70)
  assert.equal(list.summary.js.gzip, 25)
  assert.deepEqual(list.baselineRoots, ['index.html'])
})

test('an incremental budget fails closed when its baseline selector disappears', () => {
  const budgets = generous()
  budgets.closures[0].baselineRoots = [{ name: 'missing-route' }]
  const result = evaluateBundle({ manifest: manifest(), assetStats: stats(), budgets })
  assert.ok(result.violations.some(item => item.code === 'missing_closure_baseline'))
})

test('the default policy is satisfiable by a fully split route and interaction graph', () => {
  const sources = [
    'RunList.jsx', 'AssistantBar.jsx', 'AttentionCenter.jsx', 'RunView.jsx', 'Dag.jsx',
    'Dock.jsx', 'Inspector.jsx', 'ConceptChipBar.jsx', 'ConceptView.jsx', 'panels.jsx',
    'CollabPanel.jsx', 'SharedAssistant.jsx', 'Report.jsx', 'ResearchAtlas.jsx',
  ]
  const graph = {
    'index.html': {
      file: 'assets/entry.js', src: 'index.html', name: 'index', isEntry: true,
      dynamicImports: [...sources.map(file => `src/${file}`), '_settings-support.js', '_vendor-flow.js'],
      css: ['assets/base.css'],
    },
    '_settings-support.js': {
      file: 'assets/settings-support.js', name: 'settings-support', isDynamicEntry: true,
    },
    '_vendor-flow.js': { file: 'assets/vendor-flow.js', name: 'vendor-flow' },
  }
  for (const file of sources) {
    graph[`src/${file}`] = {
      file: `assets/${file.replace(/\.jsx$/, '').toLowerCase()}.js`,
      src: `src/${file}`,
      name: file.replace(/\.jsx$/, ''),
      isDynamicEntry: true,
    }
  }
  const measured = new Map(collectAssetFiles(graph, Object.keys(graph)).map(file => [file, {
    file,
    kind: file.endsWith('.css') ? 'css' : 'js',
    raw: 1_024,
    gzip: 512,
  }]))

  const result = evaluateBundle({ manifest: graph, assetStats: measured })
  assert.deepEqual(result.violations, [])
  assert.equal(result.reachability.length, 7)
  assert.ok(result.reachability.every(item => item.paths.length === 0))

  for (const [root, target] of [
    ['src/Inspector.jsx', 'src/Dock.jsx'],
    ['src/Report.jsx', 'src/panels.jsx'],
  ]) {
    const poisoned = structuredClone(graph)
    poisoned[root].imports = [target]
    const blocked = evaluateBundle({ manifest: poisoned, assetStats: measured })
    assert.ok(blocked.violations.some(item => item.code === 'forbidden_reachability'
      && item.message.includes('valid review and comments exclude owner-only surfaces')),
    `${root} must not pull ${target} into a review route`)
  }
})

test('all budget and reachability failures are actionable', () => {
  const graph = manifest()
  graph['src/RunList.jsx'].imports.push('vendor-flow')
  const budgets = generous()
  budgets.total.js.gzip = 100
  budgets.individual.js.raw = 85
  budgets.closures[0].limits.js.gzip = 80
  budgets.forbidden.push({
    name: 'List defers missing panel alias',
    roots: [{ entry: true }, { src: 'src/RunList.jsx' }],
    targets: [{ name: 'renamed-panel-chunk' }],
    requireTargets: true,
  })
  const result = evaluateBundle({ manifest: graph, assetStats: stats(), budgets })
  const codes = new Set(result.violations.map(item => item.code))
  for (const code of ['total_budget', 'individual_budget', 'closure_budget',
    'missing_forbidden_target', 'forbidden_reachability']) assert.ok(codes.has(code), code)
  assert.match(result.violations.find(item => item.code === 'forbidden_reachability').message,
    /src\/RunList\.jsx -> vendor-flow/)
  const output = formatBundleReport(result)
  assert.match(output, /raw .*gzip/)
  assert.match(output, /FAIL:/)
  assert.match(output, /dynamic import/)
})

test('missing roots, imports, and asset measurements fail closed', () => {
  const graph = manifest()
  graph['index.html'].imports.push('_missing.js')
  graph['orphan.js'] = {
    file: 'assets/orphan.js',
    dynamicImports: ['src/MissingDynamic.jsx'],
  }
  const budgets = generous()
  budgets.closures.push({ name: 'Missing route', roots: [{ src: 'src/Missing.jsx' }], limits: {} })
  const measured = stats()
  measured.delete('assets/list.js')
  const result = evaluateBundle({ manifest: graph, assetStats: measured, budgets })
  const codes = new Set(result.violations.map(item => item.code))
  for (const code of ['missing_closure_root', 'manifest_integrity', 'missing_asset_measurement']) {
    assert.ok(codes.has(code), code)
  }
  assert.ok(result.violations.some(item => item.message.includes('src/MissingDynamic.jsx')),
    'the complete manifest graph, not just selected closures, must be checked')
})

test('malformed manifest records fail closed without crashing the pure analyzer', () => {
  const graph = manifest()
  graph['broken.js'] = null
  graph['src/RunList.jsx'].imports = 'vendor-flow'
  const issues = validateManifestGraph(graph)
  assert.equal(issues.filter(item => item.code === 'manifest_integrity').length, 2)
  const result = evaluateBundle({ manifest: graph, assetStats: stats(), budgets: generous() })
  assert.ok(result.violations.some(item => item.message.includes('broken.js')))
  assert.ok(result.violations.some(item => item.message.includes('non-array imports')))
})

test('static manifest cycles fail closed with an exact path', () => {
  const graph = manifest()
  graph['_shared.js'].imports = ['src/RunList.jsx']
  const issues = validateManifestGraph(graph)
  const cycle = issues.find(item => item.code === 'manifest_cycle')
  assert.ok(cycle)
  assert.match(cycle.message, /_shared\.js -> src\/RunList\.jsx -> _shared\.js/)

  const result = evaluateBundle({ manifest: graph, assetStats: stats(), budgets: generous() })
  assert.ok(result.violations.some(item => item.code === 'manifest_cycle'))
})

test('asset paths are confined to dist on every host platform', () => {
  const dist = resolve('synthetic-dist')
  assert.equal(safeAssetPath(dist, 'assets/app.js'), resolve(dist, 'assets/app.js'))
  assert.throws(() => safeAssetPath(dist, '../outside.js'), /escapes dist/)
  assert.throws(() => safeAssetPath(dist, resolve(dist, '..', 'outside.js')), /escapes dist/)
  assert.throws(() => safeAssetPath(dist, ''), /invalid path/)
})

test('real gzip measurement preserves raw byte length', () => {
  const source = 'const value = 1;\n'.repeat(20)
  const measured = measureAssetBuffer('assets/example.js', Buffer.from(source))
  assert.equal(measured.kind, 'js')
  assert.equal(measured.raw, Buffer.byteLength(source))
  assert.ok(measured.gzip > 0 && measured.gzip < measured.raw)
})
