import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { mountLive, click } from './_mount.js'
import { createInspectorDraftStore } from '../src/inspectorDraftStore.js'

test('code search highlights original source positions after Unicode characters, without creating matches', async () => {
  const h = await mountLive({ visible: true })
  const previous = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  const copied = []
  Object.defineProperty(navigator, 'clipboard', { configurable: true,
    value: { writeText: async value => copied.push(value) } })
  try {
    const { default: CodeViewer } = await h.load('/src/CodeViewer.jsx')
    const drafts = createInspectorDraftStore()
    const scope = 'source:unicode'
    const code = 'İx\nX\nİ\nliteral .* [x]'
    const props = { code, label: 'Source', language: 'ru', draftStore: drafts, draftScope: scope }
    const mounted = await h.mount(CodeViewer, props)
    const query = value => React.act(async () => {
      drafts.updateField(scope, 'query', value, '', { disposable: true })
    })
    const marks = () => [...mounted.container.querySelectorAll('mark')].map(mark => mark.textContent)
    await query('x')
    assert.deepEqual(marks(), ['x', 'X', 'x'], 'lowercase expansion cannot shift the source highlight')
    assert.match(mounted.container.textContent, /Строк с совпадением: 3/)
    await query('\u0307')
    assert.deepEqual(marks(), [], 'a dot created by lowercasing İ is not a character in the source')
    assert.match(mounted.container.textContent, /Строк с совпадением: 0/)
    await query('.*')
    assert.deepEqual(marks(), ['.*'], 'search text is literal, not an arbitrary regex')
    await mounted.rerender({ ...props, language: 'en' })
    assert.match(mounted.container.textContent, /Matching lines: 1/)
    assert.equal([...mounted.container.querySelectorAll('.code-line code')].map(item => item.textContent).join('\n'), code)
    await click([...mounted.container.querySelectorAll('button')].find(item => item.textContent === 'Wrap'))
    assert.deepEqual(marks(), ['.*'])
    assert.equal(copied.length, 0, 'search, language and wrap do not copy or execute anything')
    await click([...mounted.container.querySelectorAll('button')].find(item => item.textContent === 'Copy'))
    assert.deepEqual(copied, [code], 'Copy preserves source text without search marks')
    assert.equal(h.fetch.calls.length, 0)
  } finally {
    if (previous) Object.defineProperty(navigator, 'clipboard', previous)
    else delete navigator.clipboard
    await h.close()
  }
})

test('diff search counts matching rows rather than occurrences and preserves source line identities', async () => {
  const h = await mountLive({ visible: true })
  try {
    const { default: CodeViewer } = await h.load('/src/CodeViewer.jsx')
    const drafts = createInspectorDraftStore()
    const scope = 'source:diff'
    const diff = [
      { l: 'İx', kind: 'del', cls: 'diff-del', oldNo: 7, newNo: null },
      { line: 'x x', kind: 'add', cls: 'diff-add', oldNo: null, newNo: 8 },
    ]
    const mounted = await h.mount(CodeViewer, { diff, draftStore: drafts, draftScope: scope })
    await React.act(async () => { drafts.updateField(scope, 'query', 'x', '', { disposable: true }) })
    assert.match(mounted.container.textContent, /Matching lines: 2/)
    assert.deepEqual([...mounted.container.querySelectorAll('mark')].map(item => item.textContent), ['x', 'x', 'x'])
    assert.equal(mounted.container.querySelector('.diff-del .code-old-no').textContent, '7')
    assert.equal(mounted.container.querySelector('.diff-add .code-new-no').textContent, '8')
    await React.act(async () => { drafts.updateField(scope, 'query', '', '', { disposable: true }) })
    assert.equal(mounted.container.querySelector('mark'), null)
    assert.doesNotMatch(mounted.container.textContent, /Matching lines:/)
    assert.deepEqual([...mounted.container.querySelectorAll('.code-line code')].map(item => item.textContent), ['İx', 'x x'])
  } finally { await h.close() }
})
