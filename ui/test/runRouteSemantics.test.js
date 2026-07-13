import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('App routes on the fragment path while diagnostic state uses Back and Forward safely', async () => {
  const app = await source('App.jsx')
  assert.match(app, /const h = routeHashPath\(location\.hash\)/)
  assert.match(app, /window\.addEventListener\('hashchange', on\)/)
  assert.match(app, /window\.addEventListener\('popstate', on\)/)
  assert.match(app, /<ReviewRoute key=\{route\.token \|\| 'invalid-review'\}/)
  assert.match(app, /key=\{`\$\{resource\.data\.id \|\| token\}:\$\{resource\.data\.run_id\}`\}/)
})

test('RunView URL state is authoritative and stale generations lock every mutation surface', async () => {
  const [runView, assistant, dock] = await Promise.all([
    source('RunView.jsx'), source('AssistantBar.jsx'), source('Dock.jsx'),
  ])
  assert.match(runView, /useRunRouteState\(\{ generation, reviewMode \}\)/)
  assert.match(runView, /const viewSeq = routeState\.sequence/)
  assert.match(runView, /const selectedId = routeState\.nodeId/)
  assert.match(runView, /useLayoutEffect\(\(\) => \{[\s\S]*?setRunAccess\(runId,/)
  assert.match(runView, /mode: reviewMode \? 'review' : routeFenceBlocked \? 'stale-link'/)
  assert.match(runView, /enabled: !reviewMode && !routeFenceBlocked/)
  assert.match(runView, /timelineFilter: value \}\), \{ mode: 'replace' \}\)/)
  assert.match(runView, /kindFilters=\{routeState\.timelineKinds\}/)
  assert.match(runView, /This diagnostic link targets an earlier run generation/)
  assert.match(assistant, /staleDiagnostic/)
  assert.match(assistant, /open the current generation/)
  assert.match(dock, /URL-owned diagnostic filters/)
  assert.doesNotMatch(dock, /const \[filter, setFilter\]/)
})

test('generation-only exact links remain fenced during route hydration', async () => {
  const routeHook = await source('useRunRouteState.js')
  assert.match(routeHook, /forceGeneration: !!parsed\.state\.generation/)
})

test('historical Inspector and Report detail reads carry the exact run generation', async () => {
  const [runView, inspector, report] = await Promise.all([
    source('RunView.jsx'), source('Inspector.jsx'), source('Report.jsx'),
  ])
  assert.match(runView, /expectedGeneration=\{routeState\.generation\}/g)
  assert.match(inspector, /expected_generation=\$\{encodeURIComponent\(expectedGeneration \|\| ''\)\}/)
  assert.match(report, /expected_generation=\$\{encodeURIComponent\(expectedGeneration \|\| ''\)\}/)
})

test('minted review links carry only scope-safe canonical context after the bearer', async () => {
  const [panels, api] = await Promise.all([source('panels.jsx'), source('api.js')])
  assert.match(panels, /reviewRouteStateForScope\(\{ \.\.\.\(reviewRouteState \|\| \{\}\),/)
  assert.match(panels, /generation: result\.generation/)
  assert.match(panels, /hashWithRunRouteState\(target\.hash, scopedState,/)
  assert.match(api, /splitRouteHash\(loc\.hash \|\| ''\)\.path/)
})
