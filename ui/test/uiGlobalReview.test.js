import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { initialDagOverviewDecision, shouldAutoFitDag, shouldRefitDag } from '../src/dagViewport.js'
import { safeExternalHref } from '../src/urlSafety.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('untrusted research source URLs allow only credential-free HTTP(S)', () => {
  assert.equal(safeExternalHref('https://example.com/paper?q=1'), 'https://example.com/paper?q=1')
  for (const href of ['javascript:alert(1)', 'data:text/html,boom', '//attacker.invalid/x',
    '/relative', 'https://user:secret@example.com/private', 'https://example.com/\nheader']) {
    assert.equal(safeExternalHref(href), null, href)
  }
})

test('large graph starts bounded and preserves exact deep links', async () => {
  const [runView, dag] = await Promise.all([source('RunView.jsx'), source('Dag.jsx')])
  assert.match(runView, /initialDagOverviewDecision\(\{/)
  assert.match(runView, /explicitContext: selectedId != null \|\| selectedGroup != null \|\| historyActive/)
  assert.match(runView, /largeOverviewAppliedRef\.current = true[\s\S]*?computeGroups\(live\.nodes, 'operator'\)/)
  assert.match(runView, /setCollapsed\(new Set\(groups\.keys\(\)\)\)/)
  assert.match(dag, /fitView=\{autoFit\}[\s\S]*?defaultViewport=\{DAG_READABLE_VIEWPORT\}/)
  assert.match(dag, /useNodesInitialized\(\)/)
  assert.match(dag, /onAutoCollapse\(true\)/)
  assert.equal(shouldAutoFitDag(48), true)
  assert.equal(shouldAutoFitDag(49), false)
  const initial = { signature: 'theme:a|b', count: 131, mode: 'theme' }
  assert.equal(shouldRefitDag(initial, { signature: 'operator:g1|g2', count: 5, mode: 'operator' }), true)
  assert.equal(shouldRefitDag(initial, { signature: 'operator:many', count: 25, mode: 'operator' }), false)
  assert.equal(shouldRefitDag(
    { signature: 'theme:a|b', count: 8, mode: 'theme' },
    { signature: 'operator:a|b', count: 8, mode: 'operator' },
  ), true)
  assert.equal(shouldRefitDag(
    { signature: 'operator:g1', count: 1, mode: 'operator' },
    { signature: 'operator:a|b', count: 8, mode: 'operator' },
  ), false, 'expanding detail preserves the camera')
  assert.equal(initialDagOverviewDecision({ ready: false, nodeCount: 131 }), 'wait')
  assert.equal(initialDagOverviewDecision({ ready: true, nodeCount: 79 }), 'preserve')
  assert.equal(initialDagOverviewDecision({ ready: true, nodeCount: 80 }), 'collapse')
  assert.equal(initialDagOverviewDecision({ ready: true, nodeCount: 131, explicitContext: true }), 'preserve')
})

test('compact graph controls remain named and vertically bounded', async () => {
  const [runView, directions, css] = await Promise.all([
    source('RunView.jsx'), source('DirectionsOverview.jsx'), source('styles.css'),
  ])
  assert.match(runView, /copy-view-btn[\s\S]*?aria-label=\{reviewMode/)
  assert.match(directions, /role="group" aria-label="Research directions"/)
  assert.match(css, /\.do-chips \{ flex-wrap: nowrap; max-height: none; overflow-x: auto/)
  assert.match(css, /\.do-chip \{ flex: 0 0 auto; max-width: min\(78vw, 320px\); min-height: 44px/)
})

test('stale async resources are cancelled and run ids are encoded at every transport boundary', async () => {
  const [shared, hooks] = await Promise.all([source('SharedAssistant.jsx'), source('hooks.js')])
  assert.match(shared, /const controller = new AbortController\(\)/)
  assert.match(shared, /e\?\.name !== 'AbortError'/)
  assert.match(shared, /return \(\) => \{ active = false; controller\.abort\(\) \}/)
  assert.match(shared, /shared\/\$\{encodeURIComponent\(sid\)\}/)
  assert.match(hooks, /new EventSource\(apiUrl\(`\/api\/runs\/\$\{encodeURIComponent\(runId\)\}\/events`\)\)/)
})

test('fresh workspaces are canvas-first and dense settings tabs stay on one scrollable row', async () => {
  const [runView, css] = await Promise.all([source('RunView.jsx'), source('styles.css')])
  assert.match(runView, /storageGet\('ll\.dockC', '1'\) === '1'/)
  assert.match(css, /\.sf-tabs \{ flex-wrap: nowrap; scrollbar-width: thin; \}/)
  assert.match(css, /\.sf-tabs \.tab \{ flex: 0 0 auto; white-space: nowrap; \}/)
})
