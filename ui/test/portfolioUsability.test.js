import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

import {
  bestComparableRun, configDifferences, decodePortfolioViews, DEFAULT_COMPARE_COLUMNS,
  normalizeCompareColumns, portfolioViewSignature, upsertPortfolioView,
} from '../src/portfolioModel.js'
import { parseRunRouteState } from '../src/runRouteState.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const state = {
  project: 'project-a',
  query: 'baseline',
  task: 'task-a',
  status: 'running',
  supertask: 'super-a',
  sort: 'metric',
  direction: 'asc',
  view: 'compare',
  compare: ['a', 'b'],
  columns: ['best', 'cost'],
}

test('saved portfolio views are bounded, sanitized, replaceable, and exact-state comparable', () => {
  assert.deepEqual(decodePortfolioViews('{broken'), [])
  assert.deepEqual(decodePortfolioViews([{ name: 'bad\u0000name' }]), [])

  const saved = upsertPortfolioView([], 'Baselines', state)
  assert.equal(saved.length, 1)
  assert.deepEqual(saved[0], { name: 'Baselines', ...state })
  assert.equal(portfolioViewSignature(saved[0]), portfolioViewSignature(state))

  const replaced = upsertPortfolioView(saved, 'Baselines', { ...state, query: 'candidate' })
  assert.equal(replaced.length, 1)
  assert.equal(replaced[0].query, 'candidate')

  const many = Array.from({ length: 20 }, (_, index) => ({
    ...state, name: `view-${index}`, compare: Array.from({ length: 20 }, (__, id) => `run-${id}`),
  }))
  const decoded = decodePortfolioViews(JSON.stringify(many))
  assert.equal(decoded.length, 12)
  assert.equal(decoded[0].compare.length, 8)
})

test('compare columns accept only the known persistent layout vocabulary', () => {
  assert.deepEqual(normalizeCompareColumns('not-json'), [...DEFAULT_COMPARE_COLUMNS])
  assert.deepEqual(normalizeCompareColumns(JSON.stringify(['cost', 'cost', 'unknown', 'best'])),
    ['cost', 'best'])
  assert.deepEqual(normalizeCompareColumns([]), [])
})

test('cross-run ranking never compares different tasks or objectives', () => {
  const min = [
    { run_id: 'a', task_id: 't', direction: 'min', best_metric: 0.4 },
    { run_id: 'b', task_id: 't', direction: 'min', best_confirmed: 0.2 },
  ]
  assert.equal(bestComparableRun(min).run_id, 'b')
  assert.equal(bestComparableRun(min.map(run => ({ ...run, direction: 'max' }))).run_id, 'a')
  assert.equal(bestComparableRun([{ ...min[0], task_id: 'x' }, min[1]]), null)
  assert.equal(bestComparableRun([{ ...min[0], direction: 'max' }, min[1]]), null)
  assert.equal(bestComparableRun([{ ...min[0], task_id: null }, min[1]]), null)
  assert.equal(bestComparableRun(min.map(run => ({ ...run, direction: null }))), null)
})

test('configuration differences are bounded and preserve unavailable evidence', () => {
  const result = configDifferences([
    { config: { policy: 'mcts', max_nodes: 10, same: true } },
    { config: { policy: 'greedy', max_nodes: 10, same: true } },
    { config: null },
  ], 1)
  assert.ok(result.total >= 1)
  assert.equal(result.rows.length, 1)
  assert.equal(result.rows[0].values[2], 'unavailable')
})

test('compare detail is deadline-bounded and champion links keep their generation fence', async () => {
  let vite
  const originalFetch = globalThis.fetch
  try {
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const { championRunHref, loadDetail } = await vite.ssrLoadModule('/src/RunCompare.jsx')
    globalThis.fetch = () => new Promise(() => {})
    const started = Date.now()
    const detail = await loadDetail({ run_id: 'hung-run' }, new AbortController().signal, 15)
    assert.equal(detail.partial, true)
    assert.equal(detail.state, null)
    assert.ok(Date.now() - started < 500, 'a transport that ignores AbortSignal must still settle')

    const generation = 'a'.repeat(64)
    const href = championRunHref({ run_id: 'run/one' }, {
      generation, state: { best_node_id: 7 },
    })
    assert.equal(href, `#/run/run%2Fone?gen=${generation}&node=7`)
    const parsed = parseRunRouteState(href)
    assert.equal(parsed.state.generation, generation)
    assert.equal(parsed.state.nodeId, 7)
    assert.deepEqual(parsed.issues, [])
  } finally {
    globalThis.fetch = originalFetch
    await vite?.close()
  }
})

test('portfolio UI exposes saved views, bounded selection, fenced detail, and comfortable density', async () => {
  const [list, compare, density, app, css] = await Promise.all([
    readFile(new URL('../src/RunList.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/RunCompare.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/DensityToggle.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/App.jsx', import.meta.url), 'utf8'),
    readFile(new URL('../src/styles.css', import.meta.url), 'utf8'),
  ])
  assert.match(list, /compareIds\.size > 0/)
  assert.match(list, /next\.size >= 8/)
  assert.match(list, /Save current view/)
  assert.match(compare, /snapshot\?\.generation === expectedGeneration/)
  assert.match(compare, /probe\?\.generation === expectedGeneration/)
  assert.match(compare, /deadlineRequest/)
  assert.match(compare, /hashWithRunRouteState/)
  assert.match(compare, /role="region" aria-label="Selected run comparison" tabIndex=\{0\}/)
  assert.match(compare, /do not share one task and objective/)
  assert.match(density, /data-comfortable/)
  assert.match(app, /initDensity\(\)/)
  assert.match(css, /data-comfortable[\s\S]*font-size: 12px/)
})
