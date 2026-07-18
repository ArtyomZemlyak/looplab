import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import React, { act } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { JSDOM } from 'jsdom'
import { createServer } from 'vite'
import {
  ATLAS_RENDER_LIMITS,
  boundedAtlasText,
  buildResearchAtlasView,
  isValidAtlasSourceEnvelope,
  mergeCurationLogs,
  mergeResearchAtlasPayload,
  reconcileAtlasSourceStatuses,
} from '../src/researchAtlasModel.js'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
} from '../src/api.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')
const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const claim = (index = 0) => ({
  claim_uid: `claim-${index}`,
  statement: `claim ${index}`,
  epistemic: index % 2 ? 'mixed' : 'supported',
  maturity: 'machine-proposed',
  n_support: 0,
  support: [`run-${index}:node-1`],
  oppose: [],
  unverified: [`run-${index}:node-2`],
  scopes: ['task-a'],
  runs: [`run-${index}`],
})

test('Atlas projection reconciles concurrent totals without trusting malformed text or counts', () => {
  const view = buildResearchAtlasView({
    n_runs: 0,
    n_concepts: 0,
    n_contested: 0,
    explored: [{ concept: '\n  robust\tsearch ', n_runs: 0, runs: [
      { run_id: 'run/one\n', direction: 'max', metric: 0.75 },
    ] }],
    thin_coverage: ['robust\nsearch'],
    contradictions: [{ ...claim(1), n_oppose: -10, oppose: ['run-1:node-3'] }],
  }, { n: 0, claims: [claim(0)] }, {
    n: 0,
    entries: [{ run_id: 'run-0', proposals: { merges: [{}], splits: 2 },
      receipt: { applied: [{}], skipped: [] } }],
  })

  assert.deepEqual(view.totals, { runs: 3, concepts: 1, claims: 1, contested: 1, curation: 1 })
  assert.equal(view.empty, false)
  assert.equal(view.concepts[0].concept, 'robust search')
  assert.equal(view.concepts[0].runs[0].runId, 'run/one')
  assert.equal(view.concepts[0].runs[0].metric, null,
    'a legacy raw metric without task/scope context must fail closed before rendering')
  assert.equal(view.concepts[0].runs[0].metricSuppressed, true)
  assert.equal(view.claims[0].nSupport, 1)
  assert.equal(view.claims[0].nUnverified, 1)
  assert.deepEqual(view.claims[0].unverified, ['run-0:node-2'])
  assert.equal(view.curation[0].merges, 1)
  assert.equal(view.curation[0].splits, 2)
  assert.equal(boundedAtlasText({ unsafe: 'shape' }), '')
  assert.equal(boundedAtlasText('task\u202e-name\u2066 scope\u2069'), 'task -name scope',
    'bidi formatting controls cannot visually reorder model-authored comparison context')
})

test('Atlas suppresses context-free raw metrics and never renders a bare optimization direction', async () => {
  const view = buildResearchAtlasView({ explored: [
    { concept: 'legacy row', runs: [
      { run_id: 'legacy-run', direction: 'max', metric: 0.8 },
    ] },
    { concept: 'scoped row', runs: [
      {
        run_id: 'scoped-run', task_id: ' task-a\n', scope: ' holdout\tset ',
        direction: 'max', metric: 0.8,
      },
    ] },
  ] }, {}, {})
  const legacy = view.concepts.find(row => row.concept === 'legacy row').runs[0]
  const scoped = view.concepts.find(row => row.concept === 'scoped row').runs[0]

  assert.deepEqual(
    [legacy.metric, legacy.optimizationOrientation, legacy.metricSuppressed],
    [null, '', true],
  )
  assert.deepEqual(
    [scoped.task, scoped.scope, scoped.metric, scoped.optimizationOrientation,
      scoped.metricSuppressed],
    ['task-a', 'holdout set', 0.8, 'maximize', false],
  )

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { AtlasRunReference } = await vite.ssrLoadModule('/src/ResearchAtlas.jsx')
    const legacyMarkup = renderToStaticMarkup(
      React.createElement(AtlasRunReference, { run: legacy }),
    )
    const scopedMarkup = renderToStaticMarkup(
      React.createElement(AtlasRunReference, { run: scoped }),
    )
    const legacyDom = new JSDOM(legacyMarkup)
    const scopedDom = new JSDOM(scopedMarkup)
    const legacyText = legacyDom.window.document.body.textContent
    const scopedText = scopedDom.window.document.body.textContent

    assert.match(legacyText,
      /legacy-run.*Metric hidden.*task\/scope comparison context or optimization orientation was not recorded/is,
      'a sighted user can see why an available raw value was not rendered')
    assert.doesNotMatch(legacyMarkup, /0\.8|\bmax\b/i,
      'the raw value and shorthand direction must not leak into a context-free run reference')
    assert.match(legacyDom.window.document.querySelector('a').getAttribute('aria-label'),
      /Metric hidden.*task\/scope comparison context/is)
    assert.match(scopedText,
      /task: task-a.*scope: holdout set.*Not cross-run comparable.*Primary objective metric.*unnamed.*unit not recorded.*optimization orientation: maximize.*value 0[.,]8/is)
    assert.ok(scopedText.indexOf('Not cross-run comparable') < scopedText.indexOf('value'),
      'the visible comparability warning precedes the numeric value')
    assert.doesNotMatch(scopedText, /\bmax\s+0[.,]8\b/i,
      'optimization orientation must never be presented as a bare comparable metric')
    assert.doesNotMatch(scopedText, /\bDirection\b/i,
      'optimization orientation is distinct from the narrative Direction concept')
    const atlasCss = await source('research-atlas.css')
    const runReferenceRule = atlasCss.match(/\.atlas-runrefs a \{[^}]+\}/s)?.[0] || ''
    assert.match(runReferenceRule, /white-space:\s*normal/)
    assert.doesNotMatch(runReferenceRule, /text-overflow:\s*ellipsis|overflow:\s*hidden/,
      'desktop Atlas references must not clip the warning while leaving its value visible')
  } finally {
    await vite.close()
  }
})

test('Atlas derives evidence balance from sanitized totals instead of payload labels', () => {
  const contradictory = [
    { ...claim(0), epistemic: 'refuted', n_support: 2, support: [], n_oppose: 0, oppose: [] },
    { ...claim(1), epistemic: 'supported', n_support: 0, support: [], n_oppose: 3, oppose: [] },
    { ...claim(2), epistemic: 'inconclusive', n_support: 1, support: [], n_oppose: 1, oppose: [] },
    { ...claim(3), epistemic: 'mixed', n_support: -4, support: [], n_oppose: 'bad', oppose: [] },
  ]
  const view = buildResearchAtlasView({}, { claims: contradictory }, {})

  assert.deepEqual(view.claims.map(row => [row.nSupport, row.nOppose, row.epistemic]), [
    [2, 0, 'supported'],
    [0, 3, 'refuted'],
    [1, 1, 'mixed'],
    [0, 0, 'inconclusive'],
  ])
})

test('Atlas preserves bounded structured contradictions and cannot render them support-only', () => {
  const contradicts = Array.from(
    { length: ATLAS_RENDER_LIMITS.evidence + 3 },
    (_, index) => `opposite claim ${index}\n${'x'.repeat(500)}`,
  )
  const view = buildResearchAtlasView({}, { claims: [
    {
      ...claim(8), epistemic: 'mixed', n_support: 1, support: ['run-8:node-1'],
      n_oppose: 0, oppose: [], contradicts,
    },
    {
      ...claim(9), epistemic: 'mixed', n_support: 1, support: ['run-9:node-1'],
      n_oppose: 0, oppose: [], contradicts: [],
    },
  ] }, {})

  assert.equal(view.claims[0].epistemic, 'mixed')
  assert.equal(view.claims[0].nContradicts, contradicts.length)
  assert.equal(view.claims[0].contradicts.length, ATLAS_RENDER_LIMITS.evidence)
  assert.ok(view.claims[0].contradicts.every(value => value.length <= 300 && !value.includes('\n')))
  assert.equal(view.claims[1].epistemic, 'mixed', 'explicit structured mixed state must survive')
})

test('Atlas requires support before structured contradictions become mixed evidence', () => {
  const view = buildResearchAtlasView({}, { claims: [{
    ...claim(10), epistemic: 'mixed', n_support: 0, support: [],
    n_oppose: 0, oppose: [], contradicts: ['opposite assertion'],
  }] }, {})

  assert.equal(view.claims[0].nSupport, 0)
  assert.equal(view.claims[0].nContradicts, 1)
  assert.equal(view.claims[0].epistemic, 'inconclusive')
})

test('Atlas preserves decision freshness and warns only on stale or unknown governed decisions', async () => {
  const rows = [
    { ...claim(20), maturity: 'operator-ratified', decision_fresh: false },
    { ...claim(21), maturity: 'operator-pinned' },
    { ...claim(22), maturity: 'operator-rejected', decision_fresh: true },
    { ...claim(23), maturity: 'machine-proposed' },
  ]
  const view = buildResearchAtlasView({}, { claims: rows }, {})
  assert.deepEqual(view.claims.map(row => row.decisionFresh), [false, null, true, null])

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { ClaimCard } = await vite.ssrLoadModule('/src/ResearchAtlas.jsx')
    const markup = view.claims.map(row => renderToStaticMarkup(
      React.createElement(ClaimCard, { claim: row }),
    ))
    assert.match(markup[0], /operator ratified · ⚠ stale/)
    assert.match(markup[1], /operator pinned · ⚠ freshness unknown/)
    assert.doesNotMatch(markup[2], /⚠/)
    assert.doesNotMatch(markup[3], /⚠/)
  } finally {
    await vite.close()
  }
})

test('Atlas projection applies hard caps before React receives portfolio collections', () => {
  const concepts = Array.from({ length: ATLAS_RENDER_LIMITS.concepts + 3 }, (_, index) => ({
    concept: `concept-${index}`,
    n_runs: 1,
    runs: Array.from({ length: ATLAS_RENDER_LIMITS.conceptRuns + 2 }, (__, run) => ({
      run_id: `run-${index}-${run}`, direction: 'min', metric: run,
    })),
  }))
  const claims = Array.from({ length: ATLAS_RENDER_LIMITS.claims + 4 }, (_, index) => ({
    ...claim(index),
    support: Array.from({ length: ATLAS_RENDER_LIMITS.evidence + 5 }, (__, ref) => `run-${index}:node-${ref}`),
  }))
  const contradictions = Array.from(
    { length: ATLAS_RENDER_LIMITS.contradictions + 2 }, (_, index) => claim(index))
  const entries = Array.from({ length: ATLAS_RENDER_LIMITS.curation + 7 }, (_, index) => ({
    run_id: `run-${index}`, proposals: {}, receipt: {},
  }))
  const view = buildResearchAtlasView({ explored: concepts, contradictions,
    thin_coverage: concepts.map(row => row.concept) }, { claims }, { entries })

  assert.equal(view.concepts.length, ATLAS_RENDER_LIMITS.concepts)
  assert.equal(view.concepts[0].runs.length, ATLAS_RENDER_LIMITS.conceptRuns)
  assert.equal(view.claims.length, ATLAS_RENDER_LIMITS.claims)
  assert.equal(view.claims[0].support.length, ATLAS_RENDER_LIMITS.evidence)
  assert.equal(view.contradictions.length, ATLAS_RENDER_LIMITS.contradictions)
  assert.equal(view.curation.length, ATLAS_RENDER_LIMITS.curation)
  assert.equal(view.hiddenConcepts, 3)
  assert.equal(view.hiddenContradictions, 2)
  assert.equal(view.hiddenClaims, 4)
  assert.equal(view.hiddenCuration, 7)
})

test('Atlas source-revision touches only the bounded visible slice of a huge raw ledger', () => {
  let indexedReads = 0
  const entries = new Proxy(new Array(200000), {
    get(target, property, receiver) {
      if (/^\d+$/u.test(String(property))) {
        indexedReads += 1
        return { revision: 200000 - Number(property) }
      }
      return Reflect.get(target, property, receiver)
    },
  })
  const huge = { conceptCuration: { entries } }
  const statuses = reconcileAtlasSourceStatuses({}, huge, 'now')
  assert.equal(statuses.conceptCuration.state, 'current')
  assert.equal(statuses.conceptCuration.revision, '200000')
  assert.ok(indexedReads <= ATLAS_RENDER_LIMITS.curation)
})

test('concept and claim steward logs merge deterministically by time, revision, then kind', () => {
  const merged = mergeCurationLogs({ n: 3, entries: [
    { run_id: 'concept-new', at: '2026-07-16T01:00:00Z', revision: 1, outcome: 'proposed',
      proposals: { merges: [{}] } },
    { run_id: 'concept-rev', revision: 4, outcome: 'empty', proposals: {} },
  ] }, { n: 2, entries: [
    { run_id: 'claim-newest', at: '2026-07-16T02:00:00Z', revision: 1, outcome: 'proposed',
      proposals: { decisions: [{}] } },
    { run_id: 'claim-rev', revision: 4, outcome: 'unavailable', auto_requested: true, proposals: {} },
  ] })

  assert.equal(merged.n, 5)
  assert.deepEqual(merged.entries.map(entry => [entry.kind, entry.run_id]), [
    ['claim', 'claim-newest'],
    ['concept', 'concept-new'],
    ['claim', 'claim-rev'],
    ['concept', 'concept-rev'],
  ])
  const view = buildResearchAtlasView({}, {}, merged)
  assert.equal(view.curation[0].kind, 'claim')
  assert.equal(view.curation[2].autoRequested, true)
  assert.equal(view.curation[2].outcome, 'unavailable')
})

test('a partial refresh replaces successful slices and preserves last-good failed slices', () => {
  const previous = {
    atlas: { n_runs: 2, explored: [{ concept: 'kept coverage' }] },
    claims: { claims: [claim(1)] },
    conceptCuration: { entries: [{ run_id: 'kept-concept-log' }] },
    claimCuration: { entries: [{ run_id: 'kept-claim-log' }] },
  }
  const freshClaims = { claims: [claim(2)] }
  const merged = mergeResearchAtlasPayload(previous, { claims: freshClaims })
  assert.equal(merged.atlas, previous.atlas)
  assert.equal(merged.claims, freshClaims)
  assert.equal(merged.conceptCuration, previous.conceptCuration)
  assert.equal(merged.claimCuration, previous.claimCuration)
})

test('malformed fulfilled Atlas envelopes are rejected and retain last-good source data', () => {
  assert.equal(isValidAtlasSourceEnvelope('claims', {}), false)
  assert.equal(isValidAtlasSourceEnvelope('claims', { claims: [] }), true)
  assert.equal(isValidAtlasSourceEnvelope('atlas', {
    explored: [], thin_coverage: [], contradictions: [],
  }), true)
  assert.equal(isValidAtlasSourceEnvelope('conceptCuration', { entries: [] }), true)

  const previousPayload = { claims: { revision: 7, claims: [claim(7)] } }
  const previousStates = reconcileAtlasSourceStatuses({}, previousPayload, 'before')
  const successful = isValidAtlasSourceEnvelope('claims', {}) ? { claims: {} } : {}
  const merged = mergeResearchAtlasPayload(previousPayload, successful)
  const states = reconcileAtlasSourceStatuses(previousStates, successful, 'after')
  assert.equal(merged.claims, previousPayload.claims)
  assert.equal(states.claims.state, 'retained-stale')
  assert.equal(states.claims.loadedAt, 'before')
})

test('Atlas source provenance distinguishes current, retained-stale, and failed slices', () => {
  const first = reconcileAtlasSourceStatuses({}, {
    claims: { revision: 7, claims: [claim(1)] },
  }, '2026-07-16T03:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(first).map(([key, value]) => [key, value.state])), {
    atlas: 'failed', claims: 'current', conceptCuration: 'failed', claimCuration: 'failed',
  })
  assert.equal(first.claims.loadedAt, '2026-07-16T03:00:00Z')
  assert.equal(first.claims.revision, '7')

  const second = reconcileAtlasSourceStatuses(first, {
    atlas: { revisions: { concept_governance: 4, claims: 9 } },
  }, '2026-07-16T04:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(second).map(([key, value]) => [key, value.state])), {
    atlas: 'current', claims: 'retained-stale', conceptCuration: 'failed', claimCuration: 'failed',
  })
  assert.equal(second.claims.loadedAt, first.claims.loadedAt)
  assert.equal(second.claims.revision, first.claims.revision)
  assert.equal(second.atlas.revision, 'concept 4 · claims 9')

  const failedRefresh = reconcileAtlasSourceStatuses(second, {}, '2026-07-16T05:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(failedRefresh).map(([key, value]) => [key, value.state])), {
    atlas: 'retained-stale', claims: 'retained-stale', conceptCuration: 'failed', claimCuration: 'failed',
  })

  const allCurrent = reconcileAtlasSourceStatuses(failedRefresh, {
    atlas: { revisions: { concept_governance: 5, claims: 10 } },
    claims: { revision: 8 },
    conceptCuration: { entries: [{ revision: 11 }, { revision: 12 }] },
    claimCuration: { entries: [{ revision: 13 }] },
  }, '2026-07-16T06:00:00Z')
  assert.deepEqual(Object.fromEntries(Object.entries(allCurrent).map(([key, value]) => [key, value.state])), {
    atlas: 'current', claims: 'current', conceptCuration: 'current', claimCuration: 'current',
  })
  assert.equal(allCurrent.conceptCuration.revision, '12')
  assert.equal(allCurrent.claimCuration.revision, '13')

  const claimsOnly = reconcileAtlasSourceStatuses(allCurrent, {
    claims: { revision: 9 },
  }, '2026-07-16T07:00:00Z')
  assert.equal(claimsOnly.claims.state, 'current')
  assert.equal(claimsOnly.claims.loadedAt, '2026-07-16T07:00:00Z')
  for (const key of ['atlas', 'conceptCuration', 'claimCuration']) {
    assert.equal(claimsOnly[key].state, 'retained-stale')
    assert.equal(claimsOnly[key].loadedAt, '2026-07-16T06:00:00Z')
  }
})

test('a source-local retry changes only the attempted Atlas slice', () => {
  const before = reconcileAtlasSourceStatuses({}, {
    atlas: { explored: [], thin_coverage: [], contradictions: [] },
    claims: { claims: [] },
    conceptCuration: { entries: [] },
    claimCuration: { entries: [{ revision: 2 }] },
  }, 'before')
  const failed = reconcileAtlasSourceStatuses(before, {}, 'failed', ['claimCuration'])

  assert.equal(failed.claimCuration.state, 'retained-stale')
  assert.equal(failed.claimCuration.loadedAt, 'before')
  for (const key of ['atlas', 'claims', 'conceptCuration']) {
    assert.deepEqual(failed[key], before[key], `${key} was not attempted and must remain current`)
  }

  const recovered = reconcileAtlasSourceStatuses(failed, {
    claimCuration: { entries: [{ revision: 3 }] },
  }, 'after', ['claimCuration'])
  assert.equal(recovered.claimCuration.state, 'current')
  assert.equal(recovered.claimCuration.loadedAt, 'after')
  assert.equal(recovered.claimCuration.revision, '3')
})

test('partial Atlas UI never presents an unavailable source as an empty current fact', async () => {
  const [atlas, deadline] = await Promise.all([
    source('ResearchAtlas.jsx'), source('requestDeadline.js'),
  ])

  assert.match(atlas, /const id = \+\+requestId\.current[\s\S]*requestedSources\.forEach[\s\S]*settle\(source/)
  assert.match(atlas, /!active \|\| id !== requestId\.current/,
    'late results must be fenced to the mounted request')
  assert.match(atlas, /deadlineRequest\(source\.read, SOURCE_TIMEOUT_MS\)/)
  assert.match(deadline, /setTimeout\([\s\S]*controller\.abort/,
    'each source needs a bounded liveness escape')
  assert.match(atlas, /loaded \? value : 'not loaded'/,
    'summary values from a never-loaded slice need an explicit unavailable state')
  assert.match(atlas, /view\.empty && <AtlasEmptyState/,
    'empty and partial-empty projections need compact source-level readiness')
  assert.match(atlas, /!view\.empty && <div className="atlas-grid">/,
    'empty projections must not render four oversized empty panels')
  assert.match(atlas, /atlasLoaded && <>[\s\S]*No concepts returned\./)
  assert.match(atlas, /claimsLoaded && \(view\.claims\.length > 0[\s\S]*No claims returned\./)
  assert.match(atlas, /curationCurrent[\s\S]*incomplete merge/)
  assert.doesNotMatch(atlas, /shown · incomplete merge/,
    'an incomplete two-ledger merge must not expose a false combined count')
  assert.doesNotMatch(atlas, /errorText|result\.reason\?\.message/,
    'transport failures must use client-owned copy instead of reflecting internal error text')
  assert.match(atlas, /errors\.every\(error => error\.status === 400\)/)
  assert.match(atlas, /state !== 'current'[\s\S]*disabled=\{busy\}[\s\S]*retry\(sourceKey\)/,
    'failed and retained-stale watermarks both need their own retry action')
  for (const key of ['atlas', 'claims']) {
    assert.match(atlas, new RegExp(`sourceKey="${key}"[\\s\\S]*?retry=\\{retry\\}`))
  }
  assert.match(atlas, /\[\['conceptCuration', 'Concept'\], \['claimCuration', 'Claim'\]\][\s\S]*sourceKey=\{sourceKey\}[\s\S]*retry=\{retry\}/)
})

test('Atlas empty state distinguishes evidence runs and each independent source', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  const current = { state: 'current', loadedAt: '2026-07-16T10:00:00Z', revision: '1' }
  const failed = { state: 'failed', loadedAt: '', revision: '' }
  const stale = { state: 'retained-stale', loadedAt: '2026-07-16T09:00:00Z', revision: '1' }
  try {
    const { AtlasEmptyState } = await vite.ssrLoadModule('/src/ResearchAtlas.jsx')
    const allCurrent = renderToStaticMarkup(React.createElement(AtlasEmptyState, {
      sourceStates: {
        atlas: current, claims: current, conceptCuration: current, claimCuration: current,
      }, pending: [], retry() {}, busy: false, onBack() {},
    }))
    const currentDom = new JSDOM(allCurrent)
    assert.match(currentDom.window.document.querySelector('h2').textContent, /No cross-run evidence/)
    assert.match(currentDom.window.document.body.textContent,
      /does not mean the project has no runs/)
    assert.equal(currentDom.window.document.querySelectorAll('.atlas-empty-source').length, 4)
    assert.deepEqual([...currentDom.window.document.querySelectorAll('.atlas-readiness-state')]
      .map(node => node.textContent), ['Empty', 'Empty', 'Empty', 'Empty'])
    assert.equal(currentDom.window.document.querySelectorAll('.atlas-empty-source .btn').length, 0)
    assert.equal(currentDom.window.document.querySelector('a[href="#/settings"]')?.textContent,
      'Memory settings')
    assert.equal(currentDom.window.document.querySelector('.atlas-empty-actions button')?.textContent,
      'Back to runs')

    const partial = renderToStaticMarkup(React.createElement(AtlasEmptyState, {
      sourceStates: {
        atlas: current, claims: failed, conceptCuration: failed, claimCuration: stale,
      }, pending: ['conceptCuration'], retry() {}, busy: false, onBack() {},
    }))
    const partialDom = new JSDOM(partial)
    assert.match(partialDom.window.document.querySelector('h2').textContent,
      /No current Atlas records/)
    assert.deepEqual([...partialDom.window.document.querySelectorAll('.atlas-readiness-state')]
      .map(node => node.textContent), ['Empty', 'Unavailable', 'Loading', 'Stale'])
    assert.deepEqual([...partialDom.window.document.querySelectorAll('.atlas-empty-source .btn')]
      .map(node => node.getAttribute('aria-label')),
    ['Retry Claim records', 'Retry Claim steward log'])
    assert.equal(partialDom.window.document.querySelector('.atlas-empty-source-loading .btn'), null)
  } finally {
    await vite.close()
  }
})

test('mounted Atlas settles sources progressively and fences timed-out or superseded reads', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'https://looplab.test/', pretendToBeVisual: true,
  })
  const realSetTimeout = globalThis.setTimeout
  const realClearTimeout = globalThis.clearTimeout
  const requests = []
  const sourceTimers = []
  const installed = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    location: dom.window.location, sessionStorage: dom.window.sessionStorage,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
    requestAnimationFrame: callback => realSetTimeout(callback, 0),
    cancelAnimationFrame: handle => realClearTimeout(handle),
    IS_REACT_ACT_ENVIRONMENT: true,
    fetch: (url, options = {}) => new Promise(resolve => requests.push({
      url: String(url), options, resolve,
    })),
  }
  const previous = Object.fromEntries(Object.keys(installed)
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  const previousTimers = Object.fromEntries(['setTimeout', 'clearTimeout']
    .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]))
  let root
  let vite
  const response = (payload, status = 200) => ({
    ok: status < 400, status, headers: { get: () => null }, json: async () => payload,
  })
  const reply = (request, payload, status = 200) => act(async () => {
    request.resolve(response(payload, status))
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  })
  const click = button => act(async () => {
    button.click()
    await Promise.resolve(); await Promise.resolve()
  })
  const atlasEnvelope = concept => ({
    explored: [{ concept, n_runs: 1, runs: [] }], thin_coverage: [], contradictions: [],
  })
  const claimsEnvelope = statement => ({ claims: [{ ...claim(70), statement }] })
  const requestFor = (batch, path) => batch.find(item => item.url.includes(path))
  const sourceNote = label => [...document.querySelectorAll('.atlas-source-note')]
    .find(node => node.textContent.includes(label))
  try {
    for (const [key, value] of Object.entries(installed)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value })
    }
    vite = await createServer({
      root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
      server: { middlewareMode: true },
    })
    const [{ createRoot }, { default: ResearchAtlas }] = await Promise.all([
      import('react-dom/client'), vite.ssrLoadModule('/src/ResearchAtlas.jsx'),
    ])
    globalThis.setTimeout = (callback, delay, ...args) => {
      if (delay !== 15_000) return realSetTimeout(callback, delay, ...args)
      const handle = { cleared: false, fire: () => callback(...args) }
      sourceTimers.push(handle)
      return handle
    }
    globalThis.clearTimeout = handle => {
      if (handle && typeof handle === 'object' && 'cleared' in handle) handle.cleared = true
      else realClearTimeout(handle)
    }
    root = createRoot(document.getElementById('root'))
    await act(async () => {
      root.render(React.createElement(ResearchAtlas, { onBack() {} }))
      await Promise.resolve()
    })
    const initial = requests.slice()
    assert.equal(initial.length, 4)
    await reply(requestFor(initial, '/atlas?'), atlasEnvelope('progressive concept'))
    await reply(requestFor(initial, '/claims?'), claimsEnvelope('progressive claim'))
    await reply(requestFor(initial, '/curation-log?'), { entries: [] })

    assert.match(document.body.textContent, /progressive concept.*progressive claim/s,
      'settled slices render while the fourth request remains unresolved')
    assert.equal(document.querySelector('main').getAttribute('aria-busy'), 'true')
    assert.match(sourceNote('Claim steward invocation log').textContent, /loading/)
    let retryButton = sourceNote('Claim steward invocation log').querySelector('button')
    assert.equal(retryButton.disabled, true)
    assert.match(retryButton.getAttribute('aria-label'), /unavailable while refresh is active/)

    const hangingTimer = sourceTimers.find(timer => !timer.cleared)
    await act(async () => { hangingTimer.fire(); await Promise.resolve(); await Promise.resolve() })
    assert.equal(document.querySelector('main').getAttribute('aria-busy'), 'false')
    assert.match(sourceNote('Claim steward invocation log').textContent, /unavailable/)
    assert.equal(sourceNote('Claim steward invocation log').querySelector('button').disabled, false,
      'a timed-out slice must release its exact retry')

    const refreshStart = requests.length
    await click(document.querySelector('[aria-label="Refresh all Research Atlas sources"]'))
    const refreshBatch = requests.slice(refreshStart)
    assert.equal(refreshBatch.length, 4)
    await reply(requestFor(refreshBatch, '/atlas?'), { detail: 'offline' }, 503)
    await reply(requestFor(refreshBatch, '/claims?'), claimsEnvelope('fresh claim'))
    await reply(requestFor(refreshBatch, '/api/cross-run/curation-log?'), { entries: [] })
    await reply(requestFor(refreshBatch, '/claim-curation-log?'), { detail: 'offline' }, 503)
    assert.match(document.body.textContent,
      /Refresh incomplete; showing stale last-good data; some sources unavailable\./)
    assert.match(document.body.textContent, /progressive concept/)
    assert.match(sourceNote('Atlas concept/evidence projection').textContent, /stale/)

    const localStart = requests.length
    retryButton = sourceNote('Claim steward invocation log').querySelector('button')
    await click(retryButton)
    const localBatch = requests.slice(localStart)
    assert.equal(localBatch.length, 1)
    assert.match(localBatch[0].url, /\/api\/cross-run\/claim-curation-log\?limit=20$/)
    retryButton = sourceNote('Claim steward invocation log').querySelector('button')
    assert.equal(retryButton.disabled, true)
    assert.match(retryButton.getAttribute('aria-label'), /unavailable while refresh is active/)
    assert.equal(sourceNote('Atlas concept/evidence projection').querySelector('button').disabled, true,
      'all retries are disabled while any source request is active')
    await reply(localBatch[0], { entries: [{ run_id: 'claim-current', outcome: 'empty' }] })
    assert.match(document.body.textContent, /claim-current/)

    await reply(requestFor(initial, '/claim-curation-log?'), {
      entries: [{ run_id: 'late-timeout', outcome: 'empty' }],
    })
    assert.doesNotMatch(document.body.textContent, /late-timeout/,
      'a response arriving after its timeout cannot overwrite the successful retry')

    const supersededStart = requests.length
    await click(sourceNote('Atlas concept/evidence projection').querySelector('button'))
    const superseded = requests[supersededStart]
    assert.match(superseded.url, /\/api\/cross-run\/atlas\?limit=24$/)
    const replacementStart = requests.length
    await act(async () => {
      root.render(React.createElement(ResearchAtlas, { key: 'replacement', onBack() {} }))
      await Promise.resolve(); await Promise.resolve()
    })
    assert.equal(superseded.options.signal.aborted, true)
    const replacement = requests.slice(replacementStart)
    assert.equal(replacement.length, 4)
    await reply(requestFor(replacement, '/atlas?'), atlasEnvelope('replacement concept'))
    await reply(requestFor(replacement, '/claims?'), claimsEnvelope('replacement claim'))
    await reply(requestFor(replacement, '/api/cross-run/curation-log?'), { entries: [] })
    await reply(requestFor(replacement, '/claim-curation-log?'), { entries: [] })
    await reply(superseded, atlasEnvelope('superseded concept'))
    assert.match(document.body.textContent, /replacement concept/)
    assert.doesNotMatch(document.body.textContent, /superseded concept/,
      'an aborted late response from a replaced request cannot commit')
  } finally {
    if (root) await act(async () => root.unmount())
    for (const [key, descriptor] of Object.entries(previousTimers)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    if (vite) await vite.close()
    for (const [key, descriptor] of Object.entries(previous)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
    dom.window.close()
  }
})

test('Atlas has a discoverable owner-only route and complete resource states', async () => {
  const [app, runList, atlas, api, css] = await Promise.all([
    source('App.jsx'), source('RunList.jsx'), source('ResearchAtlas.jsx'), source('api.js'),
    source('research-atlas.css'),
  ])

  assert.match(app, /lazy\(\(\) => import\('\.\/ResearchAtlas\.jsx'\)\)/)
  assert.match(app, /h === '#\/research-atlas'[\s\S]*canonicalHash: '#\/atlas'/)
  assert.match(app, /const researchAtlas = \(\) => \{ location\.hash = '#\/atlas' \}/)
  assert.match(app, /history\.replaceState\(history\.state, '', route\.canonicalHash\)/)
  assert.match(app, /route\.view === 'research-atlas'/)
  assert.match(app, /onResearchAtlas=\{researchAtlas\}/)
  assert.ok(app.lastIndexOf("route.view === 'research-atlas'") < app.indexOf('<OwnerAuth label={routeLabel}>'),
    'Atlas content must be wrapped by the owner authentication gate')
  assert.match(runList, /aria-label="Open Research Atlas preview"/)
  assert.match(atlas, /requestedSources\.forEach/)
  for (const state of [/Loading Research Atlas preview/, /Research Atlas preview unavailable/,
    /No cross-run evidence/, /Some sources unavailable\./]) assert.match(atlas, state)
  assert.match(atlas, /Research Atlas preview[\s\S]*Experimental · bounded · read-only/)
  assert.match(atlas, /Evidence runs[\s\S]*not the total run count/)
  assert.match(atlas, /Atlas source readiness/)
  // Keep the implicit list role: role="region" would erase list semantics for assistive technology.
  assert.match(atlas, /<ul className="atlas-concepts" tabIndex=\{0\} aria-label="Bounded explored concepts">/)
  assert.doesNotMatch(atlas, /<ul[^>]*role="region"/)
  assert.match(atlas, /aria-label="Bounded mixed-evidence claim records"/)
  assert.doesNotMatch(atlas, /aria-label="[^"]*[Cc]ontradictory claims"/)
  assert.match(atlas, /Some portfolio records were ignored\./)
  assert.match(atlas, /Refresh incomplete; showing stale last-good data/)
  assert.match(css, /\.atlas-source-retained-stale \{[^}]*color: var\(--working-text\)/)
  assert.match(css, /\.atlas-source-loading \{[^}]*color: var\(--working-text\)/)
  assert.match(css, /\.atlas-source-failed \{[^}]*color: var\(--fail-text\)/)
  assert.match(atlas, /Concept observations[\s\S]*Concepts seen across runs/)
  assert.match(atlas, /Observed in one run/)
  assert.doesNotMatch(atlas, /<p className="atlas-eyebrow">Coverage<\/p>|<h3>Thin coverage/)
  assert.match(atlas, /support-only evidence/)
  assert.match(atlas, /not a proposition verdict or an applicability decision/)
  assert.match(atlas, /claim grouping ·/)
  assert.match(atlas, /supporting attempt refs/)
  assert.doesNotMatch(atlas, /scope ·|>support <b>|>oppose <b>/)
  assert.match(atlas, /Steward invocation log[\s\S]*Recent proposals \+ outcomes/)
  assert.doesNotMatch(atlas, /<p className="atlas-eyebrow">Audit trail<\/p>|Concept \+ claim governance/)
  assert.equal((atlas.match(/<SourceWatermark (?:key=\{sourceKey\} )?sourceKey=/g) || []).length, 4,
    'every panel must disclose its source; the mapped curation watermark renders twice')
  assert.match(atlas, /not a\s+CoverageFrame, frozen snapshot, or completeness estimate/)
  assert.match(atlas, /logged proposals and outcomes; not a current governance snapshot/)
  assert.match(atlas, /data-route-main tabIndex=\{-1\}/)
  assert.match(api, /crossRunRead[\s\S]*cache: 'no-store'/)
  assert.match(api, /cross-run\/claims\?limit=\$\{args\.limit\}&offset=/)
})

test('Atlas reads are no-store, abortable, and clamp every paging input', async () => {
  const previous = {
    fetch: globalThis.fetch,
    location: globalThis.location,
    sessionStorage: globalThis.sessionStorage,
  }
  const calls = []
  globalThis.location = { pathname: '/', hash: '' }
  globalThis.sessionStorage = { getItem: () => null }
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options })
    return { ok: true, json: async () => ({}) }
  }
  const controller = new AbortController()
  try {
    await getCrossRunAtlas(Number.POSITIVE_INFINITY, { signal: controller.signal })
    await getCrossRunClaims(500, -7, { signal: controller.signal })
    await getCrossRunCurationLog(0, { signal: controller.signal })
    await getCrossRunClaimCurationLog(999, { signal: controller.signal })
    assert.match(calls[0].url, /\/api\/cross-run\/atlas\?limit=24$/)
    assert.match(calls[1].url, /\/api\/cross-run\/claims\?limit=200&offset=0$/)
    assert.match(calls[2].url, /\/api\/cross-run\/curation-log\?limit=1$/)
    assert.match(calls[3].url, /\/api\/cross-run\/claim-curation-log\?limit=50$/)
    for (const call of calls) {
      assert.equal(call.options.cache, 'no-store')
      assert.equal(call.options.signal, controller.signal)
    }
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name]
      else globalThis[name] = value
    }
  }
})

test('empty and malformed envelopes degrade to a stable empty projection', () => {
  assert.doesNotThrow(() => buildResearchAtlasView(null, { claims: 'not-an-array' }, []))
  const view = buildResearchAtlasView({
    explored: [null], thin_coverage: [{}], contradictions: [{}],
  }, { claims: [{ statement: { unsafe: true } }] }, { entries: [{}] })
  assert.deepEqual(view.concepts, [])
  assert.equal(view.invalidRows.concepts, 1)
  assert.deepEqual(view.thin, [])
  assert.deepEqual(view.contradictions, [])
  assert.deepEqual(view.claims, [])
  assert.deepEqual(view.curation, [])
  assert.equal(view.invalidRows.total, 5)
  assert.equal(view.empty, true)
  assert.deepEqual(buildResearchAtlasView(null, null, null).totals,
    { runs: 0, concepts: 0, claims: 0, contested: 0, curation: 0 })
  assert.equal(buildResearchAtlasView(null, null, null).empty, true)
})

test('thin observations and contested/contradiction records prevent a false empty Atlas', () => {
  assert.equal(buildResearchAtlasView({ thin_coverage: ['one-run-only'] }, {}, {}).empty, false)
  assert.equal(buildResearchAtlasView({ n_contested: 2 }, {}, {}).empty, false)
  assert.equal(buildResearchAtlasView({ contradictions: [claim(1)] }, {}, {}).empty, false)
})

test('curation metadata keeps long run identity and time readable at 360px', async () => {
  const [atlas, css] = await Promise.all([source('ResearchAtlas.jsx'), source('research-atlas.css')])
  assert.match(atlas, /className="atlas-curation-meta"/)
  assert.match(atlas, /className="atlas-curation-run"/)
  assert.match(atlas, /title=\{entry\.runId\}/)
  assert.match(atlas, /className="atlas-curation-time"[\s\S]*?title=\{timeLabel\}/)
  assert.match(css, /\.atlas-curation-run \{ flex: 1 1 auto; min-width: 0; \}/)
  assert.match(css, /\.atlas-curation-run a \{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;/)
  assert.match(css, /@media \(max-width: 600px\) \{[\s\S]*?\.atlas-curation-meta \{ flex-wrap: wrap;/)
  assert.match(css, /\.atlas-curation-time \{ max-width: 100%; text-align: left; \}/)
})
