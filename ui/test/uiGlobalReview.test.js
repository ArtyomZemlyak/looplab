import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { initialDagOverviewDecision, shouldAutoFitDag, shouldRefitDag } from '../src/dagViewport.js'
import { safeExternalHref, safeMarkdownHref } from '../src/urlSafety.js'
import {
  CONTROL, runApiPath, runNodeApiPath, patchProject, deleteProject, renameSupertask, deleteSupertask,
} from '../src/api.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('untrusted research source URLs allow only credential-free HTTP(S)', () => {
  assert.equal(safeExternalHref('https://example.com/paper?q=1'), 'https://example.com/paper?q=1')
  for (const href of ['javascript:alert(1)', 'data:text/html,boom', '//attacker.invalid/x',
    '/relative', 'https://user:secret@example.com/private', 'https://example.com/\nheader']) {
    assert.equal(safeExternalHref(href), null, href)
  }
})

test('Markdown links share the bounded URL policy and reject active or credential-bearing URLs', async () => {
  for (const href of ['https://example.com/docs?q=1', 'mailto:owner@example.com', '#evidence',
    '/runs/one', './local', '../parent', 'plain-relative']) {
    assert.equal(safeMarkdownHref(href), href)
  }
  for (const href of ['javascript:alert(1)', 'data:text/html,boom', 'vbscript:boom',
    '//attacker.invalid/x', '\\\\attacker.invalid\\x', '/\\attacker.invalid/x',
    'https://user:secret@example.com/private', 'https://user@example.com/private',
    'https://example.com/\nheader', 'mailto://attacker.invalid', 'mailto:x@y.test?body=%0aBcc:z@y.test',
    `https://example.com/${'x'.repeat(4096)}`]) {
    assert.equal(safeMarkdownHref(href), null, href)
  }
  const markdown = await source('markdown.jsx')
  assert.match(markdown, /import \{ safeMarkdownHref \} from '\.\/urlSafety\.js'/)
  assert.match(markdown, /export const safeHref = safeMarkdownHref/)
  assert.match(markdown, /const href = safeMarkdownHref\(mm\[2\]\)/)
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
  const [runView, concepts, css] = await Promise.all([
    source('RunView.jsx'), source('ConceptChipBar.jsx'), source('styles.css'),
  ])
  assert.match(runView, /copy-view-btn[\s\S]*?aria-label=\{reviewMode/)
  assert.match(concepts, /role="group" aria-label="Concept filter"/)
  assert.match(concepts, /className="cb-chip-main" aria-pressed=\{on\}/)
  assert.match(concepts, /aria-label=\{`Open \$\{chip\.id\}`\}/)
  assert.match(css, /\.cb-chips \{ flex-wrap: nowrap; max-height: none; overflow-x: auto/)
  assert.match(css, /\.cb-chip \{ flex: 0 0 auto; max-width: min\(78vw, 320px\); min-height: 44px/)
  assert.match(css, /\.cb-chip-main, \.cb-drill \{ min-height: 44px; \}/)
})

test('collapsed group drill-down preserves the active direction projection', async () => {
  const [runView, inspector, dag, groupnodes] = await Promise.all([
    source('RunView.jsx'), source('Inspector.jsx'), source('Dag.jsx'), source('groupnodes.jsx'),
  ])
  assert.match(runView, /<GroupSummary[\s\S]*?themeFilter=\{themeFilter\}/)
  assert.match(inspector, /themeFilteredGroupAggregate\(memberIds \|\| \[\], state\.nodes, dir, themeFilter\)/)
  assert.match(inspector, /aggregate\.matchedIds\.map/)
  assert.match(inspector, /No experiments in this group match direction/)
  assert.match(inspector, /<KV k="directions" v=\{themes\.join\(', '\)\}/,
    'Group Summary must present the legacy theme field as coarse directions')
  assert.doesNotMatch(inspector, /<KV k="themes"/)
  assert.match(groupnodes, /className="grp-super-select"[\s\S]*?aria-pressed=\{!!selected\}/)
  assert.match(dag, /<button type="button" className="grp-chev btn-chev"/)
})

test('stale async resources are cancelled and run ids are encoded at every transport boundary', async () => {
  const [shared, deadline, hooks, api, dock, inspector, panels] = await Promise.all([
    source('SharedAssistant.jsx'), source('requestDeadline.js'), source('hooks.js'),
    source('api.js'), source('Dock.jsx'), source('Inspector.jsx'), source('panels.jsx'),
  ])
  assert.match(deadline, /const controller = new AbortController\(\)/)
  assert.match(deadline, /setTimeout\([\s\S]*controller\.abort/)
  assert.match(shared, /deadlineRequest\([\s\S]*SHARED_REQUEST_TIMEOUT_MS/)
  assert.match(shared, /requestRef\.current !== timed/)
  assert.match(shared, /requestRef\.current\?\.controller\.abort\(\)/)
  assert.doesNotMatch(shared, /sharedLoadError = error =>[\s\S]{0,400}error\?*\.message/)
  assert.match(shared, /shared\/\$\{encodeURIComponent\(sid\)\}/)
  assert.match(hooks, /fetchEventStream\(runApiPath\(runId, '\/events'\), \{/)
  assert.match(hooks, /const controller = new AbortController\(\)/)
  assert.match(hooks, /lastEventId: lastStreamEventId/)
  assert.match(hooks, /streamRef\.current\?\.abort\(\)/)
  assert.doesNotMatch(hooks, /new EventSource\(/)
  for (const [name, body] of Object.entries({ api, dock, inspector, panels })) {
    assert.doesNotMatch(body, /`\/api\/runs\/\$\{(?!encodeURIComponent\()/,
      `${name} must not interpolate a raw run identity into a URL`)
  }
  for (const body of [api, dock, inspector, panels]) assert.match(body, /run(?:Node)?ApiPath\(/)
})

test('dynamic API paths preserve fragment syntax and a literal encoded slash as one identity segment', async () => {
  assert.equal(runApiPath('trial#1', '/state'), '/api/runs/trial%231/state')
  assert.equal(runApiPath('literal%2Fname', '/state'), '/api/runs/literal%252Fname/state')
  assert.equal(runNodeApiPath('trial#1', 'node%2F4', '/metrics'),
    '/api/runs/trial%231/nodes/node%252F4/metrics')
  const previous = {
    fetch: globalThis.fetch, location: globalThis.location, sessionStorage: globalThis.sessionStorage,
  }
  const calls = []
  globalThis.location = { pathname: '/proxy/app/', hash: '' }
  globalThis.sessionStorage = { getItem: () => '' }
  globalThis.fetch = async url => {
    calls.push(String(url))
    return { ok: true, json: async () => ({ ok: true, generation: 'a'.repeat(64), seq: 1 }) }
  }
  try {
    await CONTROL.refreshReport('trial#1', {
      expectedGeneration: 'a'.repeat(64), idempotencyKey: 'report-trial-1',
    })
    await CONTROL.refreshReport('literal%2Fname', {
      expectedGeneration: 'a'.repeat(64), idempotencyKey: 'report-literal-name',
    })
    await patchProject('project#1', { name: 'renamed' })
    await deleteProject('literal%2Fproject')
    await renameSupertask('task#1', 'renamed')
    await deleteSupertask('literal%2Ftask')
    assert.match(calls[0], /\/api\/runs\/trial%231\/report_refresh$/)
    assert.match(calls[1], /\/api\/runs\/literal%252Fname\/report_refresh$/)
    assert.match(calls[2], /\/api\/projects\/project%231$/)
    assert.match(calls[3], /\/api\/projects\/literal%252Fproject$/)
    assert.match(calls[4], /\/api\/supertasks\/task%231$/)
    assert.match(calls[5], /\/api\/supertasks\/literal%252Ftask$/)
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
})

test('fresh workspaces are canvas-first and dense settings tabs stay on one scrollable row', async () => {
  const [runView, css] = await Promise.all([source('RunView.jsx'), source('styles.css')])
  assert.match(runView, /storageGet\('ll\.dockC', '1'\) === '1'/)
  assert.match(css, /\.sf-tabs \{ flex-wrap: nowrap; scrollbar-width: thin; \}/)
  assert.match(css, /\.sf-tabs \.tab \{ flex: 0 0 auto; white-space: nowrap; \}/)
})
