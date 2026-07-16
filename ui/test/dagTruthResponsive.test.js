import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('DAG accessibility reports feasibility as an honest three-way state', async () => {
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { dagFeasibilityLabel } = await vite.ssrLoadModule('/src/Dag.jsx')
    assert.equal(dagFeasibilityLabel(false), 'infeasible')
    assert.equal(dagFeasibilityLabel(true), 'feasible')
    assert.equal(dagFeasibilityLabel(null), 'constraint status not reported')
    assert.equal(dagFeasibilityLabel(undefined), 'constraint status not reported')
    assert.match(await source('Dag.jsx'), /dagFeasibilityLabel\(node\.feasible\)/,
      'the three-way label must feed the native node selection control')
  } finally {
    await vite.close()
  }
})

test('collapsed DAG aggregates cannot enter the experiment drag-to-merge gesture', async () => {
  const dag = await source('Dag.jsx')
  assert.match(dag, /id: superId\(key\), type: 'groupSuper',[^\n]*draggable: false,/)
  assert.match(dag, /nodesDraggable=\{!!onNodeAction\}/,
    'experiment drag-to-merge remains available while the per-super-node override wins')
})

test('DAG chrome follows semantic theme colors instead of a dark-only palette', async () => {
  const dag = await source('Dag.jsx')
  assert.match(dag, /<Background color="var\(--line\)" gap=\{22\} \/>/)
  for (const token of ['bg-3', 'best', 'fail', 'ok', 'pending', 'bg-1']) {
    assert.ok(dag.includes(`var(--${token})`), `DAG chrome must use --${token}`)
  }
  assert.doesNotMatch(dag, /#20252f|#3a4250|#ffd54a|#ef4444|#2ecc71|#6b7686|#12151c/)
})

test('DAG grouping controls wrap within a narrow viewport with touch-size targets', async () => {
  const css = await source('styles.css')
  assert.match(css, /@media \(max-width: 600px\) \{[\s\S]*?\.grp-control \{[^}]*max-width: calc\(100dvw - 30px\);[^}]*flex-wrap: wrap;/)
  assert.match(css, /\.grp-control \.text \{[^}]*flex: 1 1 140px;[^}]*min-width: 0;[^}]*max-width: 100%;[^}]*min-height: 44px;/)
  assert.match(css, /\.grp-control \.btn \{[^}]*flex: 1 1 auto;[^}]*min-height: 44px;/)
})
