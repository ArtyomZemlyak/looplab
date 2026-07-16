import assert from 'node:assert/strict'
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
})

test('Markdown export also consumes the normalized legacy report shape', () => {
  const state = {
    run_id: 'run', task_id: 'task', goal: 'goal', direction: 'min', nodes: {},
    report: { headline: 'legacy', learnings: 'one legacy lesson', next_directions: 'one next step' },
  }
  const markdown = toMarkdown(state, null)
  assert.match(markdown, /- one legacy lesson/)
  assert.match(markdown, /- one next step/)
  assert.ok(markdown.length < 20_000)
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
        reasoning: 'reason '.repeat(100_000),
      },
    }))
    assert.match(memoMarkup, /memo #4/)
    assert.match(memoMarkup, />kept</)
    assert.doesNotMatch(memoMarkup, /href="javascript:/)
    assert.ok((memoMarkup.match(/<li/g) || []).length <= RESEARCH_MEMO_LIMITS.findings + RESEARCH_MEMO_LIMITS.sources)
    assert.ok(memoMarkup.length < 100_000)

    const state = {
      run_id: 'run', task_id: 'task', goal: 'bounded report', direction: 'min', phase: 'running',
      nodes: {}, best_node_id: null, reward_hacks: [], drifts: [],
      report: {
        headline: { malformed: true },
        summary: 'legacy narrative',
        what_worked: 'legacy string list',
        learnings: Array.from({ length: 10_000 }, () => 'bounded lesson'),
      },
      research: Array.from({ length: 10_000 }, (_, index) => ({ summary: `memo ${index}` })),
    }
    const reportMarkup = renderToStaticMarkup(React.createElement(ReportView, {
      state, runId: 'run', readOnly: true,
    }))
    assert.match(reportMarkup, /legacy narrative/)
    assert.match(reportMarkup, /legacy string list/)
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
