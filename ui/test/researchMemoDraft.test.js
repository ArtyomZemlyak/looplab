import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, mountLive, until, click } from './_mount.js'

test('reviews technical work and publishes a separate attempt-bound comment', async () => {
  const h = await mountLive()
  try {
    const { default: Draft } = await h.load('/src/ResearchMemoDraft.jsx')
    const gen = 'a'.repeat(64), sent = []
    globalThis.fetch = fetchStub({
      'POST /api/runs/r/research-memo-review': ({ init }) => {
        const body = JSON.parse(init.body); assert.equal(body.purpose, 'technical')
        assert.equal(body.memo.summary, 'Recovered the existing attempt.')
        return { issues: [] }
      },
      'POST /api/runs/r/commands': ({ init }) => {
        sent.push({ body: JSON.parse(init.body), key: init.headers['Idempotency-Key'] })
        return { id: 'cmd_' + 'a'.repeat(32), status: 'succeeded' }
      },
    })
    const view = await h.mount(Draft, { runId: 'r', generation: gen })
    try {
      const select = view.container.querySelector('select'), text = view.container.querySelector('textarea')
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set.call(select, 'technical')
        select.dispatchEvent(new window.Event('change', { bubbles: true }))
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(text, 'Recovered the existing attempt.')
        text.dispatchEvent(new window.Event('input', { bubbles: true }))
      })
      const fields = [...view.container.querySelectorAll('input')]
      await React.act(async () => {
        for (const [element, value] of [[fields[0], '7'], [fields[1], '2']]) {
          Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(element, value)
          element.dispatchEvent(new window.Event('input', { bubbles: true }))
        }
      })
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Review draft'))
      await until(() => globalThis.fetch.calls.some(c => c.path.endsWith('research-memo-review')), 'draft review')
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Publish technical comment'))
      await until(() => view.container.textContent.includes('succeeded'), 'durable receipt')
      assert.equal(sent.length, 1)
      assert.equal(sent[0].body.expected_generation, gen)
      assert.equal(sent[0].body.type, 'comment_created')
      assert.deepEqual(sent[0].body.data, { node_id: 7, node_generation: 2, text: 'Recovered the existing attempt.' })
      assert.ok(sent[0].key)
      assert.equal(view.container.querySelector('fieldset').disabled, true)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('recovers a command key after reload without storing the memo or repeating a POST', async () => {
  const h = await mountLive()
  try {
    const { default: Draft } = await h.load('/src/ResearchMemoDraft.jsx')
    const gen = 'b'.repeat(64), posts = []
    globalThis.fetch = fetchStub({
      'POST /api/runs/recover/commands': ({ init }) => {
        posts.push(init.headers['Idempotency-Key'])
        throw new Error('connection lost')
      },
      'GET /api/runs/recover/command-receipt': ({ init }) => {
        assert.equal(init.headers['Idempotency-Key'], posts[0])
        return { terminal: true, command: { id: 'cmd_' + 'c'.repeat(32), status: 'succeeded' }, intake_notices: [{ kind: 'questions_not_on_board', count: 1 }] }
      },
    })
    const first = await h.mount(Draft, { runId: 'recover', generation: gen })
    const text = first.container.querySelector('textarea')
    await React.act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(text, 'Private draft text')
      text.dispatchEvent(new window.Event('input', { bubbles: true }))
    })
    await click([...first.container.querySelectorAll('button')].find(b => b.textContent === 'Publish memo'))
    await until(() => first.container.textContent.includes('Submission outcome is unknown'), 'unknown result')
    assert.ok(!JSON.stringify(window.sessionStorage).includes('Private draft text'))
    const postCount = posts.length
    assert.ok(posts.every(key => key === posts[0]))
    await first.unmount()
    const second = await h.mount(Draft, { runId: 'recover', generation: gen })
    try {
      assert.ok(second.container.textContent.includes('draft text was not stored'))
      await click([...second.container.querySelectorAll('button')].find(b => b.textContent === 'Check command receipt'))
      await until(() => second.container.textContent.includes('succeeded'), 'recovered receipt')
      assert.equal(posts.length, postCount)
      assert.ok(second.container.textContent.includes('File relevant open questions on the question board.'))
    } finally { await second.unmount() }
  } finally { await h.close() }
})

test('invalid node references block publication instead of being silently dropped', async () => {
  const h = await mountLive()
  try {
    const { default: Draft } = await h.load('/src/ResearchMemoDraft.jsx')
    const view = await h.mount(Draft, { runId: 'refs', generation: 'd'.repeat(64) })
    try {
      const text = view.container.querySelector('textarea')
      const input = [...view.container.querySelectorAll('label')].find(x => x.textContent.includes('Evidence node numbers')).querySelector('input')
      await React.act(async () => {
        Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(text, 'A claim')
        text.dispatchEvent(new window.Event('input', { bubbles: true }))
        Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, '2, x')
        input.dispatchEvent(new window.Event('input', { bubbles: true }))
      })
      assert.ok(view.container.textContent.includes('Use non-negative'))
      assert.equal([...view.container.querySelectorAll('button')].find(x => x.textContent === 'Publish memo').disabled, true)
    } finally { await view.unmount() }
  } finally { await h.close() }
})

test('publishes a run-local lesson with author status, limitation and evidence', async () => {
  const h = await mountLive()
  try {
    const { default: Draft } = await h.load('/src/ResearchMemoDraft.jsx')
    let sent
    globalThis.fetch = fetchStub({ 'POST /api/runs/lesson/commands': ({ init }) => {
      sent = JSON.parse(init.body); return { id: 'cmd_' + 'e'.repeat(32), status: 'succeeded' }
    } })
    const view = await h.mount(Draft, { runId: 'lesson', generation: 'e'.repeat(64) })
    try {
      const setText = (label, value) => {
        const el = [...view.container.querySelectorAll('label')].find(x => x.textContent.includes(label)).querySelector('textarea, input')
        const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value)
        el.dispatchEvent(new window.Event('input', { bubbles: true }))
      }
      await React.act(async () => {
        setText('Conclusion', 'Measured outcome')
        setText('Evidence node numbers', '2, 3')
        setText('Run-local lesson', 'Use this only with this policy')
        setText('Lesson limitation', 'One cohort')
      })
      await click([...view.container.querySelectorAll('button')].find(b => b.textContent === 'Publish memo'))
      await until(() => !!sent, 'lesson sent')
      assert.deepEqual(sent.data.memo.local_lessons, [{ statement: 'Use this only with this policy',
        limitation: 'One cohort', status: 'open', node_ids: [2, 3] }])
    } finally { await view.unmount() }
  } finally { await h.close() }
})
