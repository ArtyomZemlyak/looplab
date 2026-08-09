import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

import { normalizeResearchMemo, RESEARCH_MEMO_LIMITS } from '../src/researchMemoModel.js'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const SOURCE_IDENTITY = `http-sha256:${'a'.repeat(64)}`
const OTHER_SOURCE_IDENTITY = `http-sha256:${'b'.repeat(64)}`

const richMemo = (summary, verdict = 'supported') => ({
  summary,
  trigger: 'cadence',
  at_node: 7,
  findings: ['The focused sweep improved the robust metric.'],
  claims: [{
    claim_id: `claim:${summary}`,
    statement: 'Experiment 7 improved the robust metric.',
    node_ids: [7],
    urls: ['https://example.com/evidence'],
    url_identities: [SOURCE_IDENTITY],
    evidence_receipt: {
      v: 1,
      node_refs_total: 1, node_refs_retained: 1, node_refs_omitted: 0,
      url_refs_total: 1, url_refs_retained: 1, url_refs_omitted: 0,
      complete: true,
    },
  }],
  claims_receipt: { v: 1, total: 1, retained: 1, omitted: 0, complete: true },
  verification: {
    method: 'llm',
    verdicts: [{
      verdict, statement: 'Experiment 7 improved the robust metric.', note: 'Checked.',
      evidence: {
        v: 1,
        node_refs: [{ node_id: 7, generation: 0 }],
        url_identities: [SOURCE_IDENTITY],
        complete: true,
      },
    }],
    total_verdicts: 1,
    omitted_verdicts: 0,
  },
  sources: [{ title: 'fetch(example.com)', url: 'https://example.com/evidence', snippet: 'Exact evidence.' }],
  recommended_directions: ['Confirm across three seeds.'],
  reasoning: 'Debug-only reasoning.',
})

const cappedMemo = summary => ({
  summary,
  claims: [{
    claim_id: 'claim:retained',
    statement: 'One retained claim row.',
    node_ids: [7],
    urls: [],
    evidence_receipt: {
      v: 1,
      node_refs_total: 2, node_refs_retained: 1, node_refs_omitted: 1,
      url_refs_total: 0, url_refs_retained: 0, url_refs_omitted: 0,
      complete: false,
    },
  }],
  claims_receipt: { v: 1, total: 2, retained: 1, omitted: 1, complete: false },
  verification: {
    method: 'llm',
    verdicts: [{ verdict: 'supported', statement: 'One retained claim row.' }],
    total_verdicts: 2,
    omitted_verdicts: 1,
  },
})

const legacyMemo = summary => ({
  summary,
  claims: [{ statement: 'Legacy claim.', node_ids: [7], urls: [] }],
  verification: {
    method: 'llm',
    verdicts: [{
      verdict: 'supported', statement: 'Legacy claim.',
      evidence: {
        v: 1, node_refs: [{ node_id: 7, generation: 0 }], url_identities: [], complete: true,
      },
    }],
  },
})

const incompleteVerifierMemo = summary => {
  const memo = richMemo(summary)
  return {
    ...memo,
    verification: {
      ...memo.verification,
      verdicts: memo.verification.verdicts.map(row => ({
        ...row, evidence: { ...row.evidence, complete: false },
      })),
    },
  }
}

const alignmentRegressionMemos = () => {
  const cardinality = richMemo('Two claims, one verdict')
  cardinality.claims.push({
    ...cardinality.claims[0],
    claim_id: 'claim:second',
    statement: 'A second claim was never checked.',
    node_ids: [8],
  })
  cardinality.claims_receipt = { v: 1, total: 2, retained: 2, omitted: 0, complete: true }

  const orphanVerdict = richMemo('Verdict without a claim')
  orphanVerdict.claims = []
  orphanVerdict.claims_receipt = { v: 1, total: 0, retained: 0, omitted: 0, complete: true }

  const statement = richMemo('Mismatched verifier statement')
  statement.verification.verdicts[0].statement = 'A different statement was checked.'

  const identities = richMemo('Mismatched verifier identities')
  identities.verification.verdicts[0].evidence = {
    ...identities.verification.verdicts[0].evidence,
    node_refs: [{ node_id: 8, generation: 0 }],
    url_identities: [OTHER_SOURCE_IDENTITY],
  }

  const emptyEvidence = {
    summary: 'Supported without evidence',
    claims: [{
      statement: 'A claim with no cited evidence.', node_ids: [], urls: [], url_identities: [],
      evidence_receipt: {
        v: 1,
        node_refs_total: 0, node_refs_retained: 0, node_refs_omitted: 0,
        url_refs_total: 0, url_refs_retained: 0, url_refs_omitted: 0,
        complete: true,
      },
    }],
    claims_receipt: { v: 1, total: 1, retained: 1, omitted: 0, complete: true },
    verification: {
      method: 'llm',
      verdicts: [{
        verdict: 'supported', statement: 'A claim with no cited evidence.',
        evidence: { v: 1, node_refs: [], url_identities: [], complete: true },
      }],
      total_verdicts: 1, omitted_verdicts: 0,
    },
  }
  return [
    { key: 'cardinality', memo: cardinality },
    { key: 'orphan-verdict', memo: orphanVerdict },
    { key: 'statement', memo: statement },
    { key: 'identities', memo: identities },
    { key: 'empty-evidence', memo: emptyEvidence },
  ]
}

test('claim projection preserves bounded evidence identities and omission receipts', () => {
  const claims = Array.from({ length: RESEARCH_MEMO_LIMITS.claims + 1 }, (_, index) => ({
    claim_id: `claim:${index}`,
    statement: `claim ${index}`,
    node_ids: Array.from({ length: 20 }, (_value, nodeId) => nodeId),
    urls: Array.from({ length: 20 }, (_value, urlId) => `https://example.com/${urlId}`),
  }))
  const value = normalizeResearchMemo({ claims })

  assert.equal(value.claims.length, RESEARCH_MEMO_LIMITS.claims)
  assert.equal(value.claimsTotal, claims.length)
  assert.equal(value.claimsOmitted, 1)
  assert.equal(value.claimsComplete, false)
  assert.equal(value.claims[0].node_ids.length, RESEARCH_MEMO_LIMITS.claimNodeRefs)
  assert.equal(value.claims[0].urls.length, RESEARCH_MEMO_LIMITS.claimUrlRefs)
  assert.equal(value.claims[0].evidence.complete, false)
  assert.ok(value.claims[0].claim_id)
})

test('trust receipts fail closed for legacy claims and incomplete verifier evidence', () => {
  const legacy = normalizeResearchMemo(legacyMemo('Legacy authority'))
  assert.equal(legacy.claimsComplete, false)
  assert.equal(legacy.claims[0].evidence.complete, false)
  assert.equal(legacy.verification.verdicts[0].evidence.complete, true,
    'a valid verifier identity remains visible even when legacy claim authority is unknown')

  const incomplete = normalizeResearchMemo(incompleteVerifierMemo('Incomplete verifier'))
  assert.equal(incomplete.claimsComplete, true)
  assert.equal(incomplete.claims[0].evidence.complete, true)
  assert.deepEqual(incomplete.verification.verdicts[0].evidence.node_refs,
    [{ node_id: 7, generation: 0 }])
  assert.deepEqual(incomplete.verification.verdicts[0].evidence.url_identities, [SOURCE_IDENTITY])
  assert.equal(incomplete.verification.verdicts[0].evidence.complete, false)
})

test('claim-to-verifier alignment fails closed on cardinality, statement, and evidence identity gaps', () => {
  const aligned = normalizeResearchMemo(richMemo('Aligned contract'))
  assert.equal(aligned.verification.alignment.complete, true)

  const cases = alignmentRegressionMemos()
    .map(({ key, memo }) => ({ key, value: normalizeResearchMemo(memo) }))
  for (const { key, value } of cases) {
    assert.equal(value.verification.alignment.complete, false, `${key} must not be trusted`)
  }
  assert.equal(cases.find(row => row.key === 'cardinality').value.verification.alignment.totalsMatch, false)
  assert.equal(cases.find(row => row.key === 'orphan-verdict').value.verification.alignment.totalsMatch, false)
  assert.equal(cases.find(row => row.key === 'statement')
    .value.verification.verdicts[0].alignment.statementMatches, false)
  const identityAlignment = cases.find(row => row.key === 'identities')
    .value.verification.verdicts[0].alignment
  assert.equal(identityAlignment.nodeIdsMatch, false)
  assert.equal(identityAlignment.sourceIdentitiesMatch, false)
  assert.equal(cases.find(row => row.key === 'empty-evidence')
    .value.verification.verdicts[0].alignment.supportedEvidencePresent, false)
})

test('shared memo presentation keeps takeaway and trust visible while detail stays layered', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const [{ default: ResearchMemoCard, ResearchMemoBody }, { ResearchPanel }, { ResearchDetail }, { default: ReportView }] =
      await Promise.all([
        vite.ssrLoadModule('/src/ResearchMemoCard.jsx'),
        vite.ssrLoadModule('/src/panels.jsx'),
        vite.ssrLoadModule('/src/Dock.jsx'),
        vite.ssrLoadModule('/src/Report.jsx'),
      ])

    const unsupportedMarkup = renderToStaticMarkup(React.createElement(ResearchMemoCard, {
      memo: richMemo('Risky synthesis', 'unsupported'), memoNumber: 3, open: true,
      onToggle() {}, variant: 'panel', latest: true,
    }))
    const unsupported = new JSDOM(unsupportedMarkup).window.document
    const toggle = unsupported.querySelector('.research-memo-toggle')
    const region = unsupported.querySelector('.research-memo-region')
    assert.equal(toggle?.getAttribute('aria-controls'), region?.id)
    assert.equal(region?.getAttribute('aria-labelledby'), unsupported.querySelector('.research-memo-heading')?.id)
    assert.match(toggle?.textContent || '', /Risky synthesis/)
    assert.match(toggle?.textContent || '', /1 unsupported/)
    assert.match(toggle?.textContent || '', /1 research step/)
    assert.doesNotMatch(toggle?.textContent || '', /1 source/)
    assert.equal(unsupported.querySelector('.research-memo-disclosure.evidence')?.hasAttribute('open'), true,
      'warnings must reveal their evidence automatically')
    assert.equal(unsupported.querySelector('.research-memo-disclosure.activity')?.hasAttribute('open'), false)
    assert.equal(unsupported.querySelector('.research-memo-disclosure.reasoning')?.hasAttribute('open'), false)
    assert.equal(unsupported.querySelector('a.research-evidence-chip')?.getAttribute('href'),
      'https://example.com/evidence')

    const advisoryMarkup = renderToStaticMarkup(React.createElement(ResearchMemoCard, {
      memo: { summary: 'Legacy evidence-free advisory' }, memoNumber: 1, open: true,
      onToggle() {}, variant: 'panel',
    }))
    const advisory = new JSDOM(advisoryMarkup).window.document
    assert.match(advisory.querySelector('.research-memo-toggle')?.textContent || '', /Not verified/)
    assert.doesNotMatch(advisory.querySelector('.research-memo-toggle')?.textContent || '', /Check incomplete/)
    assert.equal(advisory.querySelector('.research-memo-disclosure.evidence'), null)

    const panelMarkup = renderToStaticMarkup(React.createElement(ResearchPanel, {
      state: { research: [richMemo('Older conclusion'), richMemo('Newest conclusion')] },
      runId: 'run', onToast() {}, onClose() {}, onSelect() {},
    }))
    const panel = new JSDOM(panelMarkup).window.document
    const panelCards = [...panel.querySelectorAll('.research-memo-card')]
    assert.equal(panelCards.length, 2)
    assert.match(panelCards[0].textContent, /Newest conclusion/)
    assert.match(panelCards[1].textContent, /Older conclusion/)
    assert.deepEqual(panelCards.map(card => card.querySelector('button')?.getAttribute('aria-expanded')),
      ['true', 'false'])
    assert.equal(panelCards[1].querySelector('.research-memo-region'), null,
      'collapsed history should not mount its heavy body in the drawer')

    const dockMarkup = renderToStaticMarkup(React.createElement(ResearchDetail, {
      d: { memo: { summary: 'Timeline conclusion', findings: 'malformed', sources: [null] } },
      MemoBody: ResearchMemoBody,
    }))
    assert.match(dockMarkup, /Timeline conclusion/)
    assert.doesNotMatch(dockMarkup, /malformed/)

    const reportState = {
      run_id: 'run', task_id: 'task', goal: 'Research report', direction: 'min', phase: 'running',
      nodes: {}, best_node_id: null, reward_hacks: [], drifts: [],
      research: [richMemo('Old report memo'), richMemo('New report memo')],
    }
    const reportMarkup = renderToStaticMarkup(React.createElement(ReportView, {
      state: reportState, runId: 'run', readOnly: true,
    }))
    assert.ok(reportMarkup.indexOf('New report memo') < reportMarkup.indexOf('Old report memo'),
      'the report should put the open newest memo before history')

    // A reset may reuse a node id for a new attempt. Verifier evidence must continue to identify
    // the exact historical attempt rather than offering a bare-id jump into the current attempt.
    const resetState = {
      ...reportState,
      nodes: {
        7: {
          id: 7, attempt: 1, metric: 1, operator: 'retry', parent_ids: [], feasible: true,
          status: 'evaluated', idea: { params: {}, theme: 'reset' },
        },
      },
      best_node_id: 7,
      research: [richMemo('Evidence from before reset')],
    }
    const exactEvidenceSurfaces = [
      {
        name: 'panel',
        markup: renderToStaticMarkup(React.createElement(ResearchPanel, {
          state: resetState, runId: 'run', onSelect() {}, onSelectEvidence() {},
        })),
        interactive: true,
      },
      {
        name: 'report',
        markup: renderToStaticMarkup(React.createElement(ReportView, {
          state: resetState, runId: 'run', readOnly: true,
          onPickNode() {}, onPickEvidence() {},
        })),
        interactive: true,
      },
      {
        name: 'dock',
        markup: renderToStaticMarkup(React.createElement(ResearchDetail, {
          d: { memo: richMemo('Evidence from before reset') }, MemoBody: ResearchMemoBody,
        })),
        interactive: false,
      },
    ]
    for (const { name, markup, interactive } of exactEvidenceSurfaces) {
      const dom = new JSDOM(markup)
      try {
        const exact = dom.window.document.querySelector(
          '.research-evidence-chip.node.exact[data-node-generation="0"]')
        assert.ok(exact, `${name} must retain the verifier attempt`)
        assert.match(exact.textContent, /#7 · attempt 0/)
        assert.match(exact.getAttribute('aria-label') || '', /attempt 0/i)
        assert.equal(exact.tagName, interactive ? 'BUTTON' : 'SPAN',
          `${name} must ${interactive ? 'use generation-aware navigation' : 'keep exact history inert'}`)
        assert.doesNotMatch(exact.textContent, /attempt 1|current/,
          `${name} must not relabel historical evidence as the reset attempt`)
      } finally {
        dom.window.close()
      }
    }

    const claimOnlyMarkup = renderToStaticMarkup(React.createElement(ResearchMemoCard, {
      memo: cappedMemo('Claim-only reference'), memoNumber: 1, open: true,
      onToggle() {}, onSelectNode() {}, variant: 'panel',
    }))
    const claimOnly = new JSDOM(claimOnlyMarkup).window.document
      .querySelector('.research-evidence-chip.node.unverified')
    assert.equal(claimOnly?.tagName, 'BUTTON')
    assert.match(claimOnly?.textContent || '', /#7 · current · unverified/)
    assert.match(claimOnly?.getAttribute('aria-label') || '', /current experiment 7.*unverified/i)

    const alignmentCases = alignmentRegressionMemos()
    for (const { key, memo } of alignmentCases) {
      const directMarkup = renderToStaticMarkup(React.createElement(ResearchMemoCard, {
        memo, memoNumber: 1, open: true, onToggle() {}, variant: 'panel',
      }))
      const direct = new JSDOM(directMarkup).window.document
      assert.match(direct.querySelector('.research-memo-toggle').textContent, /Check incomplete/, key)
      assert.doesNotMatch(direct.querySelector('.research-memo-toggle').textContent, /Checked/, key)
      assert.equal(direct.querySelector('.research-memo-disclosure.evidence')?.hasAttribute('open'), true)
      assert.match(direct.body.textContent, /Claim-to-verifier alignment is incomplete/)
      if (key === 'statement') {
        assert.match(direct.body.textContent, /Verifier checked a different statement: A different statement was checked/)
      }
      if (key === 'empty-evidence') {
        assert.match(direct.body.textContent, /Supported verdict has no verifiable evidence identity/)
      }

      const dockAlignmentMarkup = renderToStaticMarkup(React.createElement(ResearchDetail, {
        d: { memo }, MemoBody: ResearchMemoBody,
      }))
      assert.match(dockAlignmentMarkup, /Claim-to-verifier alignment is incomplete/, key)
    }

    for (const markup of [
      renderToStaticMarkup(React.createElement(ResearchPanel, {
        state: { research: alignmentCases.map(row => row.memo) }, runId: 'run',
      })),
      renderToStaticMarkup(React.createElement(ReportView, {
        state: { ...reportState, research: alignmentCases.map(row => row.memo) },
        runId: 'run', readOnly: true,
      })),
    ]) {
      const surface = new JSDOM(markup).window.document
      const cards = [...surface.querySelectorAll('.research-memo-card')]
      assert.equal(cards.length, alignmentCases.length)
      for (const card of cards) {
        assert.match(card.querySelector('.research-memo-toggle').textContent, /Check incomplete/)
        assert.doesNotMatch(card.querySelector('.research-memo-toggle').textContent, /Checked/)
      }
    }

    for (const markup of [
      renderToStaticMarkup(React.createElement(ResearchPanel, {
        state: { research: [cappedMemo('Panel retained receipt')] }, runId: 'run',
      })),
      renderToStaticMarkup(React.createElement(ReportView, {
        state: { ...reportState, research: [cappedMemo('Report retained receipt')] },
        runId: 'run', readOnly: true,
      })),
    ]) {
      const dom = new JSDOM(markup)
      try {
        const card = dom.window.document.querySelector('.research-memo-card')
        assert.match(card.querySelector('.research-memo-toggle').textContent, /Check incomplete/)
        assert.match(card.querySelector('.research-memo-toggle').textContent, /2 claims/)
        assert.equal(card.querySelector('.research-memo-disclosure.evidence')?.hasAttribute('open'), true)
        assert.match(card.textContent, /Verification incomplete: showing 1 of 2 verifier verdicts/)
        assert.match(card.textContent, /Evidence references are incomplete/)
      } finally {
        dom.window.close()
      }
    }

    const trustRegressions = [legacyMemo('Legacy receipt gap'), incompleteVerifierMemo('Verifier receipt gap')]
    for (const memo of trustRegressions) {
      const sharedMarkups = [
        renderToStaticMarkup(React.createElement(ResearchMemoCard, {
          memo, memoNumber: 1, open: true, onToggle() {}, variant: 'panel',
        })),
        renderToStaticMarkup(React.createElement(ResearchPanel, {
          state: { research: [memo] }, runId: 'run',
        })),
        renderToStaticMarkup(React.createElement(ReportView, {
          state: { ...reportState, research: [memo] }, runId: 'run', readOnly: true,
        })),
      ]
      for (const markup of sharedMarkups) {
        const dom = new JSDOM(markup)
        try {
          const card = dom.window.document.querySelector('.research-memo-card')
          assert.match(card.querySelector('.research-memo-toggle').textContent, /Check incomplete/)
          assert.doesNotMatch(card.querySelector('.research-memo-toggle').textContent, /Checked/)
        } finally {
          dom.window.close()
        }
      }

      const dockMarkup = renderToStaticMarkup(React.createElement(ResearchDetail, {
        d: { memo }, MemoBody: ResearchMemoBody,
      }))
      assert.match(dockMarkup, memo === trustRegressions[0]
        ? /Claim completeness could not be verified/
        : /Verification evidence identity is incomplete/)
    }
  } finally {
    await vite.close()
  }
})
