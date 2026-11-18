import test from 'node:test'
import assert from 'node:assert/strict'
import { mountHarness } from './_mount.js'

const base = { state: 'current', authority: 'authored_interpretation', verdict_changed: false,
  premise_status: [{ premise_id: 'card-1', state: 'current' }],
  undercut: [{ premise_id: 'card-1', review_seq: 12, reason: 'One author interpretation <script>bad()</script>' }] }

test('premise challenge explains authored scope and escapes text without work or approval controls', async () => {
  const h = await mountHarness()
  try {
    const { default: View } = await h.load('/src/ClaimPremiseNotice.jsx')
    const html = h.render(View, { review: base })
    assert.match(html, /Logical premise challenged/)
    assert.match(html, /Child measurements and research verdict stay unchanged/)
    assert.match(html, /card-1/)
    assert.match(html, /&lt;script&gt;/)
    assert.doesNotMatch(html, /<script>|<button|<form|proven false/)
    assert.equal(h.fetch.calls.length, 0)
  } finally { await h.close() }
})

test('stale dependency is unavailable advice, never a current refutation', async () => {
  const h = await mountHarness()
  try {
    const { default: View } = await h.load('/src/ClaimPremiseNotice.jsx')
    const html = h.render(View, { review: { ...base, undercut: [],
      premise_status: [{ premise_id: 'card-1', state: 'stale' }] } })
    assert.match(html, /stale or unavailable/)
    assert.doesNotMatch(html, /Logical premise challenged/)
    assert.equal(h.fetch.calls.length, 0)
  } finally { await h.close() }
})

test('legacy card and unsupported authority cannot render a scientific warning', async () => {
  const h = await mountHarness()
  try {
    const { default: View } = await h.load('/src/ClaimPremiseNotice.jsx')
    for (const review of [undefined, { ...base, authority: 'engine_proven' }, { ...base, verdict_changed: true }]) {
      assert.equal(h.render(View, { review }), '')
    }
    assert.equal(h.fetch.calls.length, 0)
  } finally { await h.close() }
})

test('actual Card board keeps the measured child result and independent verdict beside the notice', async () => {
  const h = await mountHarness()
  try {
    const { CardWorkspace } = await h.load('/src/CardBoard.jsx')
    const html = h.render(CardWorkspace, { runId: 'r', readOnly: true, selectedCardId: 'card-2', onSelect() {}, state: {
      cards: { 'card-2': { id: 'card-2', statement: 'Independent child', status: 'evaluated',
        verdict: 'supported', evidence: [1], premise_review: base } },
      nodes: { '1': { id: 1, status: 'evaluated', metric: .8, attempt: 0 } }, hypotheses: {},
    } })
    assert.match(html, /Logical premise challenged/)
    assert.match(html, /supported/)
    assert.match(html, /0\.8/)
    assert.doesNotMatch(html, /verdict.*refuted/)
    assert.equal(h.fetch.calls.length, 0)
  } finally { await h.close() }
})
