import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import {
  ATLAS_RENDER_LIMITS,
  boundedAtlasText,
  buildResearchAtlasView,
  mergeCurationLogs,
  mergeResearchAtlasPayload,
} from '../src/researchAtlasModel.js'
import {
  getCrossRunAtlas, getCrossRunClaims, getCrossRunCurationLog, getCrossRunClaimCurationLog,
} from '../src/api.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

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
  assert.equal(view.claims[0].nSupport, 1)
  assert.equal(view.claims[0].nUnverified, 1)
  assert.deepEqual(view.claims[0].unverified, ['run-0:node-2'])
  assert.equal(view.curation[0].merges, 1)
  assert.equal(view.curation[0].splits, 2)
  assert.equal(boundedAtlasText({ unsafe: 'shape' }), '')
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

test('Atlas has a discoverable owner-only route and complete resource states', async () => {
  const [app, runList, atlas, api, settings] = await Promise.all([
    source('App.jsx'), source('RunList.jsx'), source('ResearchAtlas.jsx'), source('api.js'), source('Settings.jsx'),
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
  assert.match(atlas, /Promise\.allSettled/)
  for (const state of [/Loading Research Atlas preview/, /Research Atlas preview unavailable/,
    /No cross-run memory yet/, /Degraded view\./]) assert.match(atlas, state)
  assert.match(atlas, /Research Atlas preview[\s\S]*Experimental · bounded · read-only/)
  assert.match(atlas, /role="region" tabIndex=\{0\} aria-label="Bounded explored concepts"/)
  assert.match(atlas, /Some portfolio records were ignored\./)
  assert.match(atlas, /Partial refresh; preserving last-loaded data for failed sources\./)
  assert.match(atlas, /data-route-main tabIndex=\{-1\}/)
  assert.match(api, /crossRunRead[\s\S]*cache: 'no-store'/)
  assert.match(api, /cross-run\/claims\?limit=\$\{args\.limit\}&offset=/)
  assert.match(settings, /settingsSavePayload\(form, agentControl\)/)
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
