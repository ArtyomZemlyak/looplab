import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import React, { act, useEffect } from 'react'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('App routes on the fragment path while diagnostic state uses Back and Forward safely', async () => {
  const app = await source('App.jsx')
  assert.match(app, /const h = routeHashPath\(location\.hash\)/)
  assert.match(app, /window\.addEventListener\('hashchange', on\)/)
  assert.match(app, /window\.addEventListener\('popstate', on\)/)
  assert.match(app, /<ReviewRoute key=\{route\.token \|\| 'invalid-review'\}/)
  assert.match(app, /const reviewKey = `\$\{resource\.data\.id \|\| token\}:\$\{resource\.data\.run_id\}`/)
  assert.match(app, /<RunView key=\{reviewKey\}/)
})

test('RunView URL state is authoritative and stale generations lock every mutation surface', async () => {
  const [runView, assistant, dock] = await Promise.all([
    source('RunView.jsx'), source('AssistantBar.jsx'), source('Dock.jsx'),
  ])
  assert.match(runView, /useRunRouteState\(\{ generation, reviewMode \}\)/)
  assert.match(runView, /const viewSeq = routeState\.sequence/)
  assert.match(runView, /const selectedId = routeState\.nodeId/)
  assert.match(runView, /useLayoutEffect\(\(\) => \{[\s\S]*?setRunAccess\(runId,/)
  assert.match(runView, /mode: reviewMode \? 'review' : startOverMutationBlocked \? 'start-over'\n\s*: routeFenceBlocked \? 'stale-link'/)
  assert.match(runView, /enabled: !reviewMode && !routeFenceBlocked/)
  assert.match(runView, /timelineFilter: value \}\), \{ mode: 'replace' \}\)/)
  assert.match(runView, /kindFilters=\{routeState\.timelineKinds\}/)
  assert.match(runView, /This diagnostic link targets an earlier run generation/)
  assert.match(assistant, /staleDiagnostic/)
  assert.match(assistant, /open the current generation/)
  assert.match(dock, /URL-owned diagnostic filters/)
  assert.doesNotMatch(dock, /const \[filter, setFilter\]/)
})

test('run workspace navigation announces deep links without stealing focus on local view clicks', async () => {
  const [runView, conceptView] = await Promise.all([source('RunView.jsx'), source('ConceptView.jsx')])
  assert.match(runView, /const routeMainRef = useRef\(null\)/)
  assert.match(runView, /routeMainRef\.current \|\| document\.querySelector\('\[data-route-main\]'\)/)
  // `overlayPanelOpen` joined the dep list so the announcement also re-runs when an overlay panel
  // opens or closes over the workspace — otherwise the outer main is announced while a panel above
  // it owns the screen. Pinning the exact two-element array failed on the addition rather than on
  // anything missing, so require the two that carry the meaning and allow the effect to depend on
  // more.
  assert.match(runView, /\}, \[routeFocusPhase, route\.navigationRevision(?:, [^\]]+)?\]\)/,
    'Back/Forward navigation must re-announce the named outer main')
  assert.doesNotMatch(runView, /\[routeFocusPhase, route\.navigationRevision, view\]/,
    'ordinary workspace view clicks must keep focus on the invoking tab')
  assert.match(runView, /ref=\{routeMainRef\}[\s\S]*?aria-label=\{workspaceRouteLabel\}/)
  // Three more resource phases (start-over recovery, stale-route, history) gained their own
  // route-level main since this count was written. The count is a proxy; what has to hold is that
  // EVERY one of them is focusable and carries an accessible name, so a phase added without either
  // fails here rather than silently announcing an unnamed region.
  const routeMains = runView.match(/<main\b[\s\S]*?data-route-main[\s\S]*?>/g) || []
  assert.equal(routeMains.length, 7, 'every run resource phase has one route-level main')
  for (const main of routeMains) {
    assert.match(main, /tabIndex=\{-1\}/,
      `a route-level main must be programmatically focusable: ${main.slice(0, 80)}`)
    assert.match(main, /aria-label(?:ledby)?=/,
      `a route-level main must be named for the announcement: ${main.slice(0, 80)}`)
  }
  for (const main of routeMains) {
    assert.match(main, /aria-(?:label|labelledby)=/,
      'loading, failure, fence, history, and ready route mains all need an accessible name')
  }
  assert.doesNotMatch(conceptView, /data-route-main/,
    'Concept projection regions must not compete with the one route-level main landmark')
  assert.equal((conceptView.match(/role="region"/g) || []).length, 2,
    'both Concept resource roots remain named regions in ready and state-card modes')
})

test('run route revision changes only for URL hydration, not local workspace updates', async () => {
  const generation = 'a'.repeat(64)
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: `https://looplab.test/#/run/demo?gen=${generation}&view=concepts`,
    pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, location: dom.window.location,
    history: dom.window.history, PopStateEvent: dom.window.PopStateEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root, vite, latest
  const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, routeModule] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/useRunRouteState.js'),
    ])
    function Harness() {
      const route = routeModule.useRunRouteState({ generation })
      useEffect(() => { latest = route }, [route])
      return null
    }
    root = createRoot(document.getElementById('root'))
    await act(async () => { root.render(React.createElement(Harness)); await settle() })
    assert.equal(latest.state.view, 'concepts')
    assert.equal(latest.navigationRevision, 1)

    await act(async () => {
      latest.update(current => ({ ...current, view: 'dag' }))
      await settle()
    })
    assert.equal(latest.state.view, 'dag')
    assert.equal(latest.navigationRevision, 1,
      'a local workspace click must not trigger route-level focus')

    await act(async () => {
      history.replaceState(history.state, '', `#/run/demo?gen=${generation}&view=concepts`)
      window.dispatchEvent(new PopStateEvent('popstate'))
      await settle()
    })
    assert.equal(latest.state.view, 'concepts')
    assert.equal(latest.navigationRevision, 2,
      'Back/Forward hydration creates a new route announcement boundary')
  } finally {
    if (root) await act(async () => { root.unmount(); await settle() })
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})

test('generation-only exact links remain fenced during route hydration', async () => {
  const routeHook = await source('useRunRouteState.js')
  assert.match(routeHook, /forceGeneration: !!parsed\.state\.generation/)
})

test('historical Inspector and Report detail reads carry the exact run generation', async () => {
  const [runView, inspector, report] = await Promise.all([
    source('RunView.jsx'), source('Inspector.jsx'), source('Report.jsx'),
  ])
  assert.match(runView, /expectedGeneration=\{generation\}/g,
    'every historical detail read must carry the run generation the route resolved')
  assert.match(inspector, /query\.push\(`expected_generation=\$\{encodeURIComponent\(expectedGeneration\)\}`\)/)
  assert.match(report, /params\.set\('expected_generation', solutionGeneration\)/)
})

test('minted review links carry only scope-safe canonical context after the bearer', async () => {
  const [panels, api] = await Promise.all([source('CollabPanel.jsx'), source('api.js')])
  assert.match(panels, /reviewRouteStateForScope\(\{ \.\.\.\(reviewRouteState \|\| \{\}\),/)
  assert.match(panels, /generation: expectedGeneration \}, \{ evidence: includeEvidence \}\)/,
    'a minted link must carry the generation the OWNER validated, not one echoed back by the mint')
  assert.match(panels, /const routeQuery = encodeRunRouteState\(scopedState,\n\s*\{ reviewMode: true, forceGeneration: true \}\)/,
    'a review link must encode the SCOPED state, in review mode, with the generation pinned')
  assert.match(api, /splitRouteHash\(loc\.hash \|\| ''\)\.path/)
})
