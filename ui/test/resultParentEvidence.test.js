import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import React from 'react'
import { resultNoticeText, validResultNotices } from '../src/resultNoticeModel.js'
import { parseRunRouteState } from '../src/runRouteState.js'
import { generation, node, payload } from './_resultNoticesFixtures.js'
import { click, fetchStub, mountLive, until } from './_mount.js'

const cases = JSON.parse(await readFile(new URL('../../tests/fixtures/parent_result_cases_v1.json', import.meta.url), 'utf8')).cases
const rowFor = item => ({ ...node, id: 'node:1:0', node_id: 1, attempt: 0, direction: 'min',
  parents: item.parents, score_comparison: { version: 1, parent_count: item.name === 'root' ? 0 : item.name.includes('merge') ? 2 : 1, status: item.status } })

test('comparison reasons distinguish a root, unavailable attempt and parent without a metric', () => {
  const reasons = { no_parent: [/has no parent/, /нет родителя/], parent_unavailable: [/recorded parent attempt is unavailable/, /попытка родителя недоступна/],
    ineligible: [/both experiments/, /обоих экспериментов/], base_different: [/code bases differ/, /Базы кода.*отличаются/],
    different: [/conditions differ/, /Условия оценки отличаются/], multiple_parents: [/Multiple parents/, /Несколько исходных/] }
  for (const item of cases) {
    const row = rowFor(item)
    assert.equal(validResultNotices(payload([row]), generation), true, item.name)
    for (const [language, index] of [['en', 0], ['ru', 1]]) {
      const brief = resultNoticeText(row, language)
      if (item.status === 'same') assert.match(brief.comparison, language === 'ru' ? /лучше, чем.*#0 · попытка 0/ : /improves on parent #0 · attempt 0/)
      else {
        assert.match(brief.comparison, reasons[item.status][index], item.name)
        assert.doesNotMatch(brief.comparison, /improves on|лучше, чем/)
      }
    }
  }
})

test('page validation and the prose comparison guard both refuse self and duplicate parents', () => {
  const row = rowFor(cases.find(item => item.name === 'same'))
  for (const parents of [[{ ...row.parents[0], node_id: row.node_id }], [row.parents[0], row.parents[0]]]) {
    const invalid = { ...row, parents }
    assert.equal(validResultNotices(payload([invalid]), generation), false)
    for (const language of ['en', 'ru']) assert.doesNotMatch(resultNoticeText(invalid, language).comparison, /improves on|лучше, чем/)
  }
})

test('chat opens only recorded parent attempts and never substitutes a reset parent or drives work', async () => {
  const h = await mountLive()
  try {
    const { default: Feed } = await h.load('/src/AssistantResults.jsx')
    const backend = fetchStub(Object.fromEntries(cases.map(item => [`GET /api/runs/${item.name}/result-notices`, payload([rowFor(item)])])))
    globalThis.fetch = backend
    for (const language of ['en', 'ru']) {
      localStorage.clear(); localStorage.setItem('looplab.language', language)
      for (const item of cases) {
        const opened = [], asked = []
        const view = await h.mount(Feed, { runId: item.name, generation,
          onOpen: (event, href) => { event.preventDefault(); opened.push(href) }, onAsk: question => asked.push(question) })
        try {
          await until(() => view.container.querySelector('article'), item.name)
          const links = [...view.container.querySelectorAll('article a')]
          assert.equal(links.length, 1 + item.parents.length)
          for (const [index, parent] of item.parents.entries()) {
            const link = links[index + 1]
            assert.match(link.textContent, new RegExp(`#${parent.node_id} · ${language === 'ru' ? 'попытка' : 'attempt'} ${parent.attempt}`))
            const target = parseRunRouteState(link.getAttribute('href')).state
            assert.equal(target.generation, generation)
            assert.equal(target.nodeId, parent.node_id)
            assert.equal(target.nodeGeneration, parent.attempt)
            assert.equal(target.inspectTab, 'Metrics')
            await click(link)
            assert.equal(opened.at(-1), link.getAttribute('href'))
          }
          await click(view.container.querySelector('article button'))
          assert.match(asked[0], language === 'ru' ? /попытку 0.*Не запускай/ : /attempt 0.*Do not start/)
          assert.equal(backend.calls.filter(call => call.method !== 'GET').length, 0)
        } finally { await view.unmount() }
      }
    }
  } finally { await h.close() }
})

test('refresh withdraws parent links after reset and all links when the page becomes invalid', async t => {
  const h = await mountLive()
  try {
    const { default: Feed } = await h.load('/src/AssistantResults.jsx')
    let row = rowFor(cases.find(item => item.name === 'same'))
    const backend = fetchStub({ 'GET /api/runs/demo/result-notices': () => payload([row]) })
    globalThis.fetch = backend
    localStorage.clear()
    t.mock.timers.enable({ apis: ['setInterval'] })
    const view = await h.mount(Feed, { runId: 'demo', generation })
    try {
      await until(() => view.container.querySelectorAll('article a').length === 2, 'parent link')
      row = rowFor(cases.find(item => item.name === 'reset_parent'))
      await React.act(async () => { t.mock.timers.tick(5000) })
      await until(() => view.container.querySelectorAll('article a').length === 1, 'parent link withdrawn')
      assert.match(view.container.textContent, /recorded parent attempt is unavailable/)
      const remaining = parseRunRouteState(view.container.querySelector('article a').getAttribute('href')).state
      assert.equal(remaining.nodeId, 1)
      row = { ...rowFor(cases.find(item => item.name === 'same')), parents: [{ ...node.parents[0], node_id: 1 }] }
      await React.act(async () => { t.mock.timers.tick(5000) })
      await until(() => view.container.textContent.includes('Could not refresh results'), 'invalid page refused')
      assert.equal(view.container.querySelectorAll('article').length, 0)
      row = rowFor(cases.find(item => item.name === 'reset_parent'))
      await click([...view.container.querySelectorAll('button')].find(button => button.textContent === 'Retry'))
      await until(() => view.container.querySelectorAll('article a').length === 1, 'explicit recovery')
      assert.ok(backend.calls.every(call => call.method === 'GET'))
    } finally { await view.unmount() }
  } finally { await h.close() }
})
