import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import { analyze, buildModelCard, failureBreakdown, toMarkdown, verdict } from '../src/report.js'
import { normalizeRunReport, reportNarrativeCoverage } from '../src/reportModel.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

const node = (id, metric, operator, parentIds = [], theme = '') => ({
  id, metric, operator, parent_ids: parentIds, feasible: true, status: 'evaluated',
  idea: { params: {}, theme },
})

const state = (direction, first, final = null) => {
  const nodes = { 0: node(0, first, 'draft', [], 'starter') }
  if (final != null) nodes[1] = node(1, final, 'manual', [0], 'manual-probe')
  return {
    run_id: `report-${direction}`, task_id: 'task', goal: 'Report semantics', direction,
    phase: 'finished', nodes, best_node_id: final == null ? 0 : 1,
    reward_hacks: [], drifts: [], research: [],
  }
}

test('total improvement is positive for both objective directions and Markdown names a lone baseline truthfully', () => {
  assert.equal(analyze(state('min', 10, 7)).totalGain, 3)
  assert.equal(analyze(state('max', 10, 13)).totalGain, 3)
  assert.match(toMarkdown(state('min', 10, 7)), /Total improvement: \*\*3\*\*/)

  const baseline = toMarkdown(state('min', 10))
  assert.match(baseline, /^## Metric baseline$/m)
  assert.doesNotMatch(baseline, /What worked — key improvements/)
})

test('failure breakdown treats prototype names as ordinary model-authored reasons', () => {
  const protoFailure = { id: 1, status: 'failed', error_reason: '__proto__' }
  const constructorFailure = { id: 2, status: 'failed', error_reason: 'constructor' }
  const breakdown = failureBreakdown({ 1: protoFailure, 2: constructorFailure })

  assert.equal(Object.getPrototypeOf(breakdown), null)
  assert.deepEqual(breakdown.__proto__, [protoFailure])
  assert.deepEqual(breakdown.constructor, [constructorFailure])
  assert.deepEqual(Object.keys(breakdown).sort(), ['__proto__', 'constructor'])
})

test('Report uses semantic section headings and exposes an unambiguous operator/theme identity', async () => {
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ReportView } = await vite.ssrLoadModule('/src/Report.jsx')
    const markup = renderToStaticMarkup(React.createElement(ReportView, {
      state: state('min', 10, 7), runId: 'report-min', readOnly: true,
    }))
    const dom = new JSDOM(markup)
    try {
      const sections = [...dom.window.document.querySelectorAll('.report-view .section-h')]
      assert.ok(sections.length >= 4)
      assert.ok(sections.every(heading => heading.tagName === 'H2'))
      assert.equal(sections[1].textContent, 'How the metric got better')

      const identities = [...dom.window.document.querySelectorAll('.report-step-kind')]
      assert.ok(identities.every(identity => identity.getAttribute('aria-hidden') === 'true'))
      assert.equal(identities[1].nextElementSibling?.textContent, 'manual · manual-probe')
      assert.equal(identities[1].nextElementSibling?.className, 'sr-only')
      assert.equal(identities[1].hasAttribute('aria-label'), false)
    } finally {
      dom.window.close()
    }

    const baselineMarkup = renderToStaticMarkup(React.createElement(ReportView, {
      state: state('min', 10), runId: 'report-min', readOnly: true,
    }))
    assert.match(baselineMarkup, /<h2 class="section-h">Metric baseline<\/h2>/)
    assert.doesNotMatch(baselineMarkup, /How the metric got better/)
    assert.match(baselineMarkup, /First feasible metric; no improvement is recorded yet/)
  } finally {
    await vite.close()
  }
})

test('print CSS removes the screen-only code viewport limit', async () => {
  const css = await readFile(new URL('../src/report-trust-polish.css', import.meta.url), 'utf8')
  assert.match(css, /@media print[\s\S]*?\.report-view pre\.code\s*\{[^}]*max-height:\s*none\s*!important;[^}]*overflow:\s*visible\s*!important;/)
  assert.match(css, /@media print[\s\S]*?\.report-view \.report-provenance\s*\{[^}]*break-inside:\s*avoid;/)
})

test('report provenance normalization and node coverage fail closed in every state', () => {
  const report = normalizeRunReport({
    headline: 'bounded', at_node: 2, trigger: 'manual',
    published_seq: 17, published_at: 1_700_000_000.25,
  })
  assert.equal(report.published_seq, 17)
  assert.equal(report.published_at, 1_700_000_000.25)
  assert.equal(reportNarrativeCoverage(null, 2).status, 'absent')
  assert.equal(reportNarrativeCoverage({ at_node: null }, 2).status, 'unknown')
  assert.deepEqual(reportNarrativeCoverage(report, 4), {
    status: 'stale', atNode: 2, currentNodeCount: 4, staleBy: 2,
  })
  assert.equal(reportNarrativeCoverage(report, 2).status, 'node_count_matched')
  assert.equal(reportNarrativeCoverage(report, 1).status, 'inconsistent')

  const invalid = normalizeRunReport({
    published_seq: Number.MAX_SAFE_INTEGER + 1, published_at: 253_402_300_800,
  })
  assert.equal(invalid.published_seq, null)
  assert.equal(invalid.published_at, null)
})

test('deterministic verdict stays authoritative across UI, Markdown, and model-card v2', async () => {
  const run = state('min', 10, 7)
  const agentHeadline = 'Everything is perfect — trust the agent'
  const championNote = 'The old champion narrative must not attach to the current card.'
  run.report = {
    headline: agentHeadline, verdict: 'Provider-authored verdict body.\r## Forged deterministic section',
    champion_summary: championNote, what_worked: ['Agent observation'],
    learnings: ['Agent learning'], what_didnt: ['Agent miss'], next_directions: ['Agent next step'],
    caveats: ['Agent caveat'], at_node: 1, trigger: 'manual',
    published_seq: 7, published_at: 1_700_000_000,
  }
  const best = run.nodes[1]
  const deterministic = verdict(run, analyze(run)).headline
  const generation = 'a'.repeat(64)
  const context = { generation, snapshotSeq: 11 }

  const markdown = toMarkdown(run, best, context)
  assert.match(markdown, new RegExp(`## Verdict\\n\\n\\*\\*${deterministic.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\*\\*`))
  assert.ok(markdown.indexOf(agentHeadline) > markdown.indexOf('## Agent narrative (advisory)'))
  assert.doesNotMatch(markdown, /^## Forged deterministic section$/m)
  assert.match(markdown, /^> ## Forged deterministic section$/m)
  assert.match(markdown, /Node coverage:.*stale by 1 node/)
  assert.match(markdown, /Published:.*event #7.*2023-11-14T22:13:20\.000Z.*trigger manual/)
  assert.ok(markdown.includes(`- **Run generation:** ${generation}`))
  assert.ok(markdown.includes('- **Snapshot event:** #11'))

  const card = buildModelCard(run, best, context)
  assert.equal(card.schema_id, 'looplab.model-card')
  assert.equal(card.schema_version, 2)
  assert.equal(card.verdict, deterministic)
  assert.equal(card.verdict_source, 'deterministic')
  assert.equal(card.agent_narrative.headline, agentHeadline)
  assert.equal(card.agent_narrative.advisory, true)
  assert.deepEqual(card.agent_narrative.coverage, {
    status: 'stale', at_node: 1, current_node_count: 2, stale_by: 1,
    basis: 'node_count', full_state_freshness: 'unknown',
  })
  assert.deepEqual(card.agent_narrative.provenance, {
    published_event_seq: 7, published_at: 1_700_000_000,
    published_at_unit: 'unix_seconds', trigger: 'manual', node_count_at_publication: 1,
  })
  assert.equal(card.provenance.run_generation, generation)
  assert.equal(card.provenance.snapshot_seq, 11)
  assert.equal(typeof card.task, 'string')
  assert.ok(Array.isArray(card.agent_report_caveats), 'v1 list-shaped field remains compatible')

  const forgedChampion = node(99, -100, 'caller-forged')
  assert.equal(buildModelCard(run, forgedChampion, context).champion.node_id, 1,
    'model-card champion must come from folded state, not the caller argument')
  const mismatchedMarkdown = toMarkdown(run, forgedChampion, context)
  assert.match(mismatchedMarkdown, /^- \*\*Best:\*\* node #1 /m)
  assert.doesNotMatch(mismatchedMarkdown, /^- \*\*Best:\*\* node #99 /m)

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { default: ReportView } = await vite.ssrLoadModule('/src/Report.jsx')
    const markup = renderToStaticMarkup(React.createElement(ReportView, {
      state: run, runId: run.run_id, readOnly: true, expectedGeneration: generation, observedSeq: 11,
    }))
    const dom = new JSDOM(markup)
    try {
      const doc = dom.window.document
      assert.equal(doc.querySelector('.verdict-headline').textContent, deterministic)
      assert.equal(doc.querySelector('.agent-report-headline').textContent, agentHeadline)
      const narrative = doc.querySelector('.agent-report.stale[role="note"]')
      assert.ok(narrative)
      assert.match(narrative.querySelector('.report-coverage').textContent, /stale by 1 node/)
      assert.equal(narrative.querySelector('time').getAttribute('datetime'), '2023-11-14T22:13:20.000Z')
      assert.match(narrative.querySelector('.report-provenance').textContent, /published event #7/)
      assert.ok(narrative.textContent.includes(championNote))
      assert.equal(doc.querySelector('.champion-card').textContent.includes(championNote), false)
      assert.ok(doc.querySelector('.champion-card').compareDocumentPosition(narrative)
        & dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
      'advisory prose must follow deterministic champion evidence')
      assert.equal(doc.querySelector('.report-provenance').closest('.report-toolbar'), null,
        'publication provenance must remain printable when the action toolbar is hidden')
    } finally {
      dom.window.close()
    }
  } finally {
    await vite.close()
  }
})
