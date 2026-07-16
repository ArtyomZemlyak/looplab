import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import {
  normalizeResearchMemo,
  normalizeResearchMemos,
  RESEARCH_MEMO_LIMITS,
} from '../src/researchMemoModel.js'
import { normalizeRunReport, REPORT_LIMITS, REPORT_LIST_FIELDS } from '../src/reportModel.js'
import { toMarkdown } from '../src/report.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('deep-research memo normalization bounds huge and malformed provider shapes', () => {
  const huge = 'x'.repeat(250_000)
  const tenThousandTexts = Array.from({ length: 10_000 }, () => huge)
  const tenThousandIds = Array.from({ length: 10_000 }, (_, index) => index)
  const memo = normalizeResearchMemo({
    summary: huge,
    reasoning: huge,
    findings: tenThousandTexts,
    recommended_directions: tenThousandIds,
    sources: Array.from({ length: 10_000 }, () => ({ title: huge, url: huge, snippet: huge })),
    verification: {
      unsupported: 1_000_000,
      verdicts: Array.from({ length: 10_000 }, () => ({ verdict: 'unsupported', statement: huge, note: huge })),
    },
    at_node: Number.MAX_SAFE_INTEGER + 1,
    trigger: { unrenderable: true },
  })

  assert.ok(memo.summary.length <= RESEARCH_MEMO_LIMITS.summaryChars)
  assert.ok(memo.reasoning.length <= RESEARCH_MEMO_LIMITS.reasoningChars)
  assert.ok(memo.findings.length <= RESEARCH_MEMO_LIMITS.findings)
  assert.ok(memo.recommended_directions.length <= RESEARCH_MEMO_LIMITS.directions)
  assert.ok(memo.sources.length <= RESEARCH_MEMO_LIMITS.sources)
  assert.ok(memo.verification?.verdicts?.length > 0,
    'verifier verdicts must survive before optional memo collections consume the budget')
  assert.ok(memo.verification.unsupported > 0)
  assert.equal(memo.at_node, null)
  assert.equal(memo.trigger, '')
  assert.ok(JSON.stringify(memo).length < RESEARCH_MEMO_LIMITS.memoChars + 8_000,
    'the retained object stays near the shared text budget, regardless of input size')

  const malformed = normalizeResearchMemo({
    summary: { not: 'text' }, findings: 'not-a-list', recommended_directions: null,
    sources: [null, 'bad', { title: { nested: true }, url: 42, snippet: false }],
    verification: { verdicts: 'not-a-list' },
  })
  assert.equal(malformed.summary, '')
  assert.deepEqual(malformed.findings, [])
  assert.deepEqual(malformed.recommended_directions, [])
  assert.deepEqual(malformed.sources, [{ title: '', url: '42', snippet: 'false' }])
  assert.equal(malformed.verification, null)
})

test('memo collection scans and renders only a bounded newest tail', () => {
  const raw = Array.from({ length: 10_000 }, (_, index) => ({ summary: `memo ${index}` }))
  const projection = normalizeResearchMemos(raw)
  assert.equal(projection.total, 10_000)
  assert.equal(projection.memos.length, RESEARCH_MEMO_LIMITS.memos)
  assert.equal(projection.omitted, 10_000 - RESEARCH_MEMO_LIMITS.memos)
  assert.equal(projection.memos[0].sourceIndex, 10_000 - RESEARCH_MEMO_LIMITS.memos)
  assert.equal(projection.memos.at(-1).summary, 'memo 9999')
})

test('memo verifier truncation is explicit even when unsupported verdicts are in the omitted tail', () => {
  const verdicts = Array.from({ length: RESEARCH_MEMO_LIMITS.verificationVerdicts }, (_, index) => ({
    verdict: 'supported', statement: `supported ${index}`,
  }))
  verdicts.push({ verdict: 'unsupported', statement: 'hidden unsupported tail' })
  const memo = normalizeResearchMemo({ verification: { method: 'llm', verdicts } })
  assert.equal(memo.verification.totalVerdicts, verdicts.length)
  assert.equal(memo.verification.verdicts.length, RESEARCH_MEMO_LIMITS.verificationVerdicts)
  assert.equal(memo.verification.unsupported, 0)
  assert.equal(memo.verification.omittedVerdicts, 1)
})

test('memo collection enforces per-memo and aggregate text budgets newest-first', () => {
  const huge = 'x'.repeat(250_000)
  const hugeMemo = {
    summary: huge,
    reasoning: huge,
    trigger: huge,
    findings: Array.from({ length: RESEARCH_MEMO_LIMITS.findings }, () => huge),
    recommended_directions: Array.from({ length: RESEARCH_MEMO_LIMITS.directions }, () => huge),
  }
  const retainedTextChars = value => {
    if (typeof value === 'string') return value.length
    if (Array.isArray(value)) return value.reduce((sum, item) => sum + retainedTextChars(item), 0)
    if (value && typeof value === 'object') {
      return Object.values(value).reduce((sum, item) => sum + retainedTextChars(item), 0)
    }
    return 0
  }

  const single = normalizeResearchMemos([hugeMemo])
  assert.equal(retainedTextChars(single.memos[0]), RESEARCH_MEMO_LIMITS.memoChars)

  const projection = normalizeResearchMemos([hugeMemo, hugeMemo, { summary: 'latest' }])
  const retained = projection.memos.map(retainedTextChars)
  assert.deepEqual(projection.memos.map(memo => memo.sourceIndex), [0, 1, 2])
  assert.equal(projection.memos.at(-1).summary, 'latest')
  assert.ok(retained.every(length => length <= RESEARCH_MEMO_LIMITS.memoChars))
  assert.equal(retained.reduce((sum, length) => sum + length, 0),
    RESEARCH_MEMO_LIMITS.collectionChars)
  assert.equal(retained[1], RESEARCH_MEMO_LIMITS.memoChars)
  assert.equal(retained[0], RESEARCH_MEMO_LIMITS.memoChars - 'latest'.length)
})

test('report normalization preserves legacy string lists as one bounded item', () => {
  const raw = Object.fromEntries(REPORT_LIST_FIELDS.map(key => [key, `${key}\nlegacy`]))
  raw.headline = 'h'.repeat(10_000)
  raw.verdict = { not: 'renderable' }
  raw.at_node = -1
  const report = normalizeRunReport(raw)

  assert.equal(report.headline.length, REPORT_LIMITS.headlineChars)
  assert.equal(report.verdict, '')
  assert.equal(report.at_node, null)
  for (const key of REPORT_LIST_FIELDS) {
    assert.equal(report[key].length, 1, `${key} must not become one item per character`)
    assert.ok(report[key][0].length <= REPORT_LIMITS.listItemChars)
    assert.doesNotMatch(report[key][0], /[\r\n]/)
  }

  const oversized = normalizeRunReport({
    what_worked: Array.from({ length: 10_000 }, () => 'x'.repeat(10_000)),
  })
  assert.equal(oversized.what_worked.length, REPORT_LIMITS.listItems)
  assert.ok(oversized.what_worked.every(value => value.length <= REPORT_LIMITS.listItemChars))
  assert.equal(normalizeRunReport('legacy prose'), null)

  const caveatPriority = normalizeRunReport({
    what_worked: Array.from({ length: REPORT_LIMITS.listItems }, () => 'w'.repeat(REPORT_LIMITS.listItemChars)),
    learnings: Array.from({ length: REPORT_LIMITS.listItems }, () => 'l'.repeat(REPORT_LIMITS.listItemChars)),
    caveats: ['critical advisory caveat'],
  })
  assert.deepEqual(caveatPriority.caveats, ['critical advisory caveat'],
    'ordinary positive narrative must not exhaust the shared budget before caveats')
})

test('Markdown export also consumes the normalized legacy report shape', () => {
  const state = {
    run_id: 'run', task_id: 'task', goal: 'goal', direction: 'min', nodes: {},
    report: {
      headline: 'legacy', learnings: 'one legacy lesson', next_directions: 'one next step',
      caveats: ['agent caveat must remain advisory'],
    },
  }
  const markdown = toMarkdown(state, null)
  assert.match(markdown, /- one legacy lesson/)
  assert.match(markdown, /- one next step/)
  assert.match(markdown, /Agent-authored caveats \(narrative only; not deterministic trust checks\)/)
  assert.match(markdown, /- agent caveat must remain advisory/)
  assert.ok(markdown.length < 20_000)
})

test('untrusted report, memo, and research-panel prose has narrow-layout containment', async () => {
  const [reportCss, coreCss] = await Promise.all([
    readFile(new URL('../src/report-trust-polish.css', import.meta.url), 'utf8'),
    readFile(new URL('../src/styles.css', import.meta.url), 'utf8'),
  ])
  assert.match(reportCss, /\.report-view \.agent-report-caveats li,[\s\S]{0,300}?overflow-wrap:\s*anywhere/)
  assert.match(reportCss, /\.report-view \.memo-body li,[\s\S]{0,200}?overflow-wrap:\s*anywhere/)
  assert.match(reportCss, /\.report-view \.memo-head[\s\S]{0,120}?min-width:\s*0/)
  assert.match(coreCss, /\.rsch-h > \*,[\s\S]{0,220}?min-width:\s*0;[\s\S]{0,60}?overflow-wrap:\s*anywhere/)
})

test('MemoCard and Report SSR stay bounded for 10k-entry and malformed payloads', async () => {
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const [{ default: MemoCard }, { default: ReportView }, { ResearchPanel }] = await Promise.all([
      vite.ssrLoadModule('/src/MemoCard.jsx'),
      vite.ssrLoadModule('/src/Report.jsx'),
      vite.ssrLoadModule('/src/panels.jsx'),
    ])

    const memoMarkup = renderToStaticMarkup(React.createElement(MemoCard, {
      idx: 3,
      open: true,
      onToggle() {},
      memo: {
        summary: { malformed: true },
        findings: Array.from({ length: 10_000 }, (_, index) => index ? { malformed: true } : 'kept'),
        recommended_directions: 'not-a-list',
        sources: Array.from({ length: 10_000 }, () => ({
          title: 'source'.repeat(1_000), url: 'javascript:alert(1)', snippet: 's'.repeat(10_000),
        })),
        verification: {
          method: 'source-check',
          unsupported: 999,
          verdicts: [
            { verdict: 'unsupported', statement: 'unsupported claim', note: 'evidence misses claim' },
            { verdict: 'supported', statement: 'supported claim' },
          ],
        },
        reasoning: 'reason '.repeat(100_000),
      },
    }))
    assert.match(memoMarkup, /memo #4/)
    assert.match(memoMarkup, />kept</)
    assert.match(memoMarkup, /Verification/)
    assert.match(memoMarkup, /1 unsupported/)
    assert.match(memoMarkup, /unsupported claim/)
    assert.match(memoMarkup, /evidence misses claim/)
    assert.doesNotMatch(memoMarkup, /href="javascript:/)
    assert.ok((memoMarkup.match(/<li/g) || []).length <= RESEARCH_MEMO_LIMITS.findings + RESEARCH_MEMO_LIMITS.sources)
    assert.ok(memoMarkup.length < 100_000)

    const truncatedMarkup = renderToStaticMarkup(React.createElement(MemoCard, {
      idx: 0, open: true, onToggle() {},
      memo: { verification: { method: 'llm', verdicts: Array.from({ length: 65 }, (_, index) => ({
        verdict: index === 64 ? 'unsupported' : 'supported', statement: `claim ${index}`,
      })) } },
    }))
    assert.match(truncatedMarkup, /verification incomplete/)
    assert.match(truncatedMarkup, /Showing 64 of 65 verifier verdicts/)

    const state = {
      run_id: 'run', task_id: 'task', goal: 'bounded report', direction: 'min', phase: 'running',
      nodes: {}, best_node_id: null, reward_hacks: [], drifts: [],
      report: {
        headline: { malformed: true },
        summary: 'legacy narrative',
        what_worked: 'legacy string list',
        learnings: Array.from({ length: 10_000 }, () => 'bounded lesson'),
        caveats: ['agent caveat must not change deterministic trust'],
      },
      research: Array.from({ length: 10_000 }, (_, index) => ({ summary: `memo ${index}` })),
    }
    const reportMarkup = renderToStaticMarkup(React.createElement(ReportView, {
      state, runId: 'run', readOnly: true,
    }))
    assert.match(reportMarkup, /legacy narrative/)
    assert.match(reportMarkup, /legacy string list/)
    assert.match(reportMarkup, /Agent-authored caveats/)
    assert.match(reportMarkup, /agent caveat must not change deterministic trust/)
    assert.match(reportMarkup, /not fully verified/)
    assert.match(reportMarkup, /Showing the latest 32 of 10000 research memos/)
    assert.equal((reportMarkup.match(/class="memo-card"/g) || []).length, RESEARCH_MEMO_LIMITS.memos)
    assert.ok(reportMarkup.length < 250_000)

    const panelMarkup = renderToStaticMarkup(React.createElement(ResearchPanel, {
      state: {
        research: Array.from({ length: 10_000 }, (_, index) => ({
          summary: `panel memo ${index}`,
          sources: [{ title: 'unsafe', url: 'javascript:alert(1)' }],
        })),
      },
      runId: 'run',
    }))
    assert.match(panelMarkup, /Showing 32 of 10000 newest valid memos/)
    assert.match(panelMarkup, /panel memo 9999/)
    assert.doesNotMatch(panelMarkup, /panel memo 0/)
    assert.doesNotMatch(panelMarkup, /href="javascript:/)
    assert.equal((panelMarkup.match(/class="rsch-memo"/g) || []).length, RESEARCH_MEMO_LIMITS.memos)
    assert.ok(panelMarkup.length < 250_000)
  } finally {
    await vite.close()
  }
})
