import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const source = () => readFile(new URL('../src/RunList.jsx', import.meta.url), 'utf8')

test('RunList resources preserve last-good data and fence every settlement', async () => {
  const text = await source()
  const hook = text.slice(text.indexOf('function useResource'), text.indexOf('function ResourceNotice'))

  assert.match(hook, /const \[state, setState\] = useState\('loading'\)/)
  assert.match(hook, /const version = useRef\(0\)/)
  assert.match(hook, /useEffect\(\(\) => \(\) => \{ version\.current \+= 1 \}, \[\]\)/,
    'unmount must revoke every in-flight owner')
  assert.match(hook, /const owner = \+\+version\.current/)
  assert.match(hook, /\.then\(value => \{[\s\S]*version\.current !== owner[\s\S]*setData\(value\); setState\('ready'\)/)
  assert.match(hook, /\.catch\(\(\) => \{[\s\S]*version\.current !== owner[\s\S]*\['ready', 'stale'\]/)
  assert.doesNotMatch(hook.slice(hook.indexOf('.catch')), /setData/,
    'a failed initial load or refresh must not overwrite the last successful payload')
  assert.match(text, /useResource\(listProjects, \{ projects: \[\], assignments: \{\} \}\)/)
  assert.match(text, /useResource\(listSupertasks, \{ supertasks: \[\], assignments: \{\} \}\)/)
  assert.doesNotMatch(text, /listSupertasks\(\)\.then\(setSuperdata\)\.catch\(\(\) => \{\}\)/,
    'super-task failures must never be swallowed')
})

test('RunList exposes safe loading, error, stale, and retry semantics', async () => {
  const text = await source()
  const notice = text.slice(text.indexOf('function ResourceNotice'), text.indexOf('// Module-scope'))

  assert.match(notice, /state === 'loading'[\s\S]*role="status">\{label\} loading/)
  assert.match(notice, /state === 'stale'/)
  assert.match(notice, /role=\{stale \? 'status' : 'alert'\}/)
  assert.match(notice, /\{label\}: \{stale \? 'Last loaded data; refresh failed\.' : 'Unavailable\.'/)
  assert.match(notice, /<button className="btn sm" onClick=\{retry\}>Retry<\/button>/)
  assert.doesNotMatch(notice, /error\?\.message|String\(error\)/,
    'resource notices use bounded copy instead of reflecting provider text')
})

test('project and super-task empties require a successful current response', async () => {
  const text = await source()

  assert.match(text, /projectsState === 'ready' && !proj\.projects\.length[\s\S]*No projects yet\./)
  assert.match(text, /state === 'ready' && !supertasks\.length[\s\S]*No super-tasks yet\./)
  assert.match(text, /\['ready', 'stale'\]\.includes\(projectsState\)[\s\S]*<MapView/,
    'the map may use current or explicitly stale last-good project metadata')
  assert.doesNotMatch(text, /projectsState === 'error'[\s\S]*<MapView/,
    'the map must stay closed when no successful project payload exists')
  assert.match(text, /!stModal && <ResourceNotice state=\{superState\}/)
  assert.match(text, /<SuperTaskModal supertasks=\{superdata\.supertasks\} state=\{superState\} onRetry=\{loadSupers\}/,
    'the active modal must own the super-task error and retry surface')
})

test('run empties and filtered recovery preserve current resource truth', async () => {
  const text = await source()

  assert.match(text, /runsState === 'ready' && runs && !runsOf\(sel\)\.length[\s\S]*No runs here\./,
    'a stale empty snapshot must not be presented as a current empty workspace')
  assert.match(text, /runsState === 'stale' \? 'No runs in the last loaded data match the filters\.'/)
  assert.match(text, /hasActiveFilters && <button[^>]*onClick=\{clearFilters\}>Clear filters<\/button>/,
    'a zero-result combination must have one-step recovery')
  assert.match(text, /aria-label=\{`Sort \$\{sortKey === 'metric'/,
    'the direction control needs a semantic name, not an arrow glyph alone')
})

test('RunList resources ignore late successes, late failures, and unmounted settlements', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, HTMLElement: dom.window.HTMLElement,
    requestAnimationFrame: callback => setTimeout(callback, 0),
    cancelAnimationFrame: handle => clearTimeout(handle), IS_REACT_ACT_ENVIRONMENT: true,
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const pending = []
  const read = () => new Promise((resolve, reject) => pending.push({ resolve, reject }))
  let load
  let renders = 0
  let root
  let mounted = false
  let vite
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { useResource }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/RunList.jsx'),
    ])
    root = createRoot(document.getElementById('root'))
    mounted = true

    function Harness() {
      const [data, state, next] = useResource(read, 'initial')
      load = next
      renders += 1
      return React.createElement('output', { 'data-state': state }, data)
    }
    await act(async () => root.render(React.createElement(Harness)))

    let older
    let newer
    await act(async () => {
      older = load(); newer = load(); await Promise.resolve()
    })
    assert.equal(pending.length, 2)
    await act(async () => { pending[1].resolve('new'); await newer })
    assert.equal(document.querySelector('output').textContent, 'new')
    assert.equal(document.querySelector('output').dataset.state, 'ready')
    await act(async () => { pending[0].resolve('old'); await older })
    assert.equal(document.querySelector('output').textContent, 'new',
      'an older poll cannot resurrect its snapshot')

    let staleFailure
    let freshSuccess
    await act(async () => {
      staleFailure = load(); freshSuccess = load(); await Promise.resolve()
    })
    await act(async () => { pending[3].resolve('freshest'); await freshSuccess })
    await act(async () => { pending[2].reject(new Error('stale')); await staleFailure })
    assert.equal(document.querySelector('output').textContent, 'freshest')
    assert.equal(document.querySelector('output').dataset.state, 'ready',
      'an older failure cannot downgrade the latest ready snapshot')

    let late
    await act(async () => { late = load(); await Promise.resolve() })
    await act(async () => root.unmount())
    mounted = false
    const rendersAtUnmount = renders
    await act(async () => { pending[4].resolve('after-unmount'); await late })
    assert.equal(renders, rendersAtUnmount)
  } finally {
    if (root && mounted) await act(async () => root.unmount())
    if (vite) await vite.close()
    dom.window.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  }
})
