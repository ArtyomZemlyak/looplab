import test from 'node:test'
import assert from 'node:assert/strict'
import { mountLive, fetchStub, click, until, settle } from './_mount.js'

const generation = 'a'.repeat(64)
const input = { version: 1, generation, run_uid: 'b'.repeat(32), report_seq: 5,
  report_digest: 'c'.repeat(64), input_digest: 'd'.repeat(64) }
const body = { expected_generation: generation, report_seq: 5,
  report_digest: input.report_digest, input_digest: input.input_digest }
const content = { version: 1, ...input, summary: 'One observed gain.',
  reviewer: { mutation_tools: false, verdict_authority: 'advisory_only' },
  comments: [{ report_field: 'verdict', quote: 'Mechanism proven', comment: 'Needs a matched comparison.',
    nomination: 'comparison', rationale: 'Compare only under the permitted scientific scope.',
    nomination_id: 'e'.repeat(64), target_node_id: 1, node_ids: [1],
    outcomes: [{ node_id: 1, attempt: 0, metric: .4 }] }] }
const receipt = { ok: true, generation, seq: 9, content }
const empty = { generation, input, latest: null, unresolved: false, conflicted: false }
const complete = { ...empty, latest: { seq: 9, content, is_current: true, unavailable: false } }

test('mounted review starts no work; explicit paid click preserves request and nomination only drafts Assistant', async () => {
  const h = await mountLive()
  try {
    const { default: View } = await h.load('/src/ReportReview.jsx')
    let paid = false
    globalThis.fetch = fetchStub({
      'GET /api/runs/r/report-review': () => paid ? complete : empty,
      'POST /api/runs/r/report-review': async ({ init }) => {
        assert.deepEqual(JSON.parse(init.body), body)
        const key = new Headers(init.headers).get('Idempotency-Key')
        const stored = JSON.parse(window.sessionStorage.getItem(`ll.report-review.r.${generation}`))
        assert.equal(stored.key, key) // Saved before the request leaves.
        paid = true
        return receipt
      },
    })
    const view = await h.mount(View, { runId: 'r', generation })
    try {
      assert.equal(globalThis.fetch.calls.length, 0)
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Read report review'))
      await until(() => view.container.textContent.includes('Review with model'), 'read source')
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent.includes('Review with model')))
      await until(() => view.container.textContent.includes('One observed gain.'), 'review receipt')
      await until(() => view.container.textContent.includes('Review of current evidence'), 'fresh evidence')
      assert.equal(window.sessionStorage.getItem(`ll.report-review.r.${generation}`), null)
      const events = []
      const listener = event => events.push(event.detail)
      window.addEventListener('ll:focus-assistant', listener)
      try { await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Discuss nomination with Assistant')) }
      finally { window.removeEventListener('ll:focus-assistant', listener) }
      assert.equal(events.length, 1)
      assert.match(events[0].text, /nominate_report_review/)
      assert.match(events[0].text, /must not launch/)
      assert.equal(globalThis.fetch.calls.filter(row => row.method === 'POST').length, 1)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('unknown paid response retains exact identity across remount and resumes without a new key', async () => {
  const h = await mountLive()
  try {
    const { default: View } = await h.load('/src/ReportReview.jsx')
    const keys = []
    globalThis.fetch = fetchStub({
      'GET /api/runs/r/report-review': () => empty,
      'POST /api/runs/r/report-review': ({ init }) => {
        keys.push(new Headers(init.headers).get('Idempotency-Key'))
        return { ok: false, generation, code: 'report_review_uncertain', ambiguous: true }
      },
    })
    let view = await h.mount(View, { runId: 'r', generation })
    await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Read report review'))
    await until(() => view.container.textContent.includes('Review with model'), 'source')
    await click([...view.container.querySelectorAll('button')].find(b => b.textContent.includes('Review with model')))
    await until(() => view.container.textContent.includes('Review outcome is unknown'), 'unknown outcome')
    await view.unmount()
    view = await h.mount(View, { runId: 'r', generation, modelUnavailable: 'model now offline' })
    try {
      await until(() => view.container.textContent.includes('Resume saved review request'), 'saved identity restored')
      const resume = [...view.container.querySelectorAll('button')].find(b => b.textContent === 'Resume saved review request')
      assert.equal(resume.disabled, false)
      await click(resume)
      await until(() => keys.length === 2, 'same-key resume')
      assert.equal(keys[0], keys[1])
      assert.ok(window.sessionStorage.getItem(`ll.report-review.r.${generation}`))
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('historical and read-only surfaces cannot nominate or start paid review', async () => {
  const h = await mountLive()
  try {
    const { default: View } = await h.load('/src/ReportReview.jsx')
    globalThis.fetch = fetchStub({ 'GET /api/runs/r/report-review': () => ({ ...complete,
      latest: { ...complete.latest, is_current: false } }) })
    let view = await h.mount(View, { runId: 'r', generation })
    await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Read report review'))
    await until(() => view.container.textContent.includes('Historical review'), 'historical receipt')
    assert.equal([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Discuss nomination with Assistant').disabled, true)
    await view.unmount()
    globalThis.fetch.calls.length = 0
    view = await h.mount(View, { runId: 'r', generation, readOnly: true })
    try {
      assert.equal(view.container.textContent, '')
      assert.equal(globalThis.fetch.calls.length, 0)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('client rejects malformed receipt identities and never overwrites damaged saved intents', async () => {
  const h = await mountLive()
  try {
    const api = await h.load('/src/reportReviewApi.js')
    const saved = api.saveReportReviewIntent('r', generation, input)
    assert.deepEqual(api.validateReportReviewResult(receipt, saved), receipt)
    for (const change of [r => { r.content.run_uid = 123 }, r => { r.content.comments[0].target_node_id = true },
      r => { r.content.comments[0].outcomes[0].attempt = true }, r => { r.content.report_seq = true },
      r => { r.content.input_digest = 'f'.repeat(64) }, r => { r.content.version = 2 }]) {
      const damaged = structuredClone(receipt); change(damaged)
      assert.throws(() => api.validateReportReviewResult(damaged, saved), /receipt is invalid/)
      assert.deepEqual(api.loadReportReviewIntent('r', generation), saved)
    }
    const key = `ll.report-review.r.${generation}`
    window.sessionStorage.setItem(key, 'damaged')
    assert.throws(() => api.saveReportReviewIntent('r', generation, input), /damaged/)
    assert.equal(window.sessionStorage.getItem(key), 'damaged')
  } finally { await h.close() }
})

test('switching runs releases the busy UI and ignores a late old-scope receipt', async () => {
  const h = await mountLive()
  try {
    const { default: View } = await h.load('/src/ReportReview.jsx')
    let release
    globalThis.fetch = fetchStub({
      'GET /api/runs/r/report-review': () => new Promise(resolve => { release = resolve }),
      'GET /api/runs/next/report-review': () => empty,
    })
    const view = await h.mount(View, { runId: 'r', generation })
    try {
      await click(view.container.querySelector('button'))
      assert.equal(view.container.querySelector('button').disabled, true)
      await view.rerender({ runId: 'next', generation })
      assert.equal(view.container.querySelector('button').disabled, false)
      await click(view.container.querySelector('button'))
      await until(() => view.container.textContent.includes('Review with model'), 'new run source')
      release(complete)
      await settle()
      assert.equal(view.container.textContent.includes('One observed gain.'), false)
      assert.equal(globalThis.fetch.calls.filter(row => row.method === 'POST').length, 0)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('a same-generation historical receipt from another UID is not accepted', async () => {
  const h = await mountLive()
  try {
    const api = await h.load('/src/reportReviewApi.js')
    globalThis.fetch = fetchStub({ 'GET /api/runs/r/report-review': () => ({ ...complete,
      latest: { ...complete.latest, is_current: false,
        content: { ...content, run_uid: 'f'.repeat(32) } } }) })
    await assert.rejects(api.getReportReview('r'), /receipt is invalid/)
  } finally { await h.close() }
})
