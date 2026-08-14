import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('AssistantChat presents bounded, semantic tool activity without raw payloads', async t => {
  const vite = await createServer({
    root: UI_ROOT,
    configFile: false,
    appType: 'custom',
    logLevel: 'silent',
    server: { middlewareMode: true },
  })

  try {
    const { Turn } = await vite.ssrLoadModule('/src/AssistantChat.jsx')
    const renderTurn = message => {
      const markup = renderToStaticMarkup(React.createElement(Turn, {
        m: message,
        audience: 'owner',
        readOnly: true,
      }))
      const dom = new JSDOM(`<!doctype html><html lang="en"><body>${markup}</body></html>`)
      return { dom, document: dom.window.document, markup }
    }

    await t.test('live groups and persisted legacy steps share the native disclosure', () => {
      const labels = ['Inspect task', 'Read files', 'Search tests', 'Compare evidence', 'Run checks']
      const live = renderTurn({
        role: 'assistant', content: '', streaming: true,
        activity: [{ type: 'tools', labels }],
      })
      const legacy = renderTurn({
        role: 'assistant', content: '', streaming: false,
        steps: labels.map((label, index) => ({
          label,
          tool: `internal_tool_${index}`,
          arg: `RAW_ARGUMENT_${index}`,
          result: `RAW_RESULT_${index}`,
        })),
      })

      try {
        for (const fixture of [live, legacy]) {
          const disclosure = fixture.document.querySelector('details.asst-tool-disclosure')
          assert.ok(disclosure, 'groups longer than three labels use a details disclosure')
          assert.equal(disclosure.open, false, 'tool details are collapsed by default')
          const summary = disclosure.querySelector(':scope > summary.asst-tool-toggle')
          assert.ok(summary, 'the disclosure has a native keyboard-operable summary')
          assert.equal(summary.querySelector('.asst-tool-label').textContent,
            '5 tool steps · Run checks')
          assert.equal(summary.querySelector('.asst-tool-icon').getAttribute('aria-hidden'), 'true')
          assert.deepEqual([...disclosure.querySelectorAll('.asst-tool-list > li')]
            .map(item => item.textContent), labels)
        }
        assert.equal(live.document.querySelector('.asst-tool-label').getAttribute('aria-live'), 'polite',
          'streaming summary changes are announced without exposing the hidden list')
        assert.equal(legacy.document.querySelector('.asst-tool-disclosure').className,
          live.document.querySelector('.asst-tool-disclosure').className)
        assert.doesNotMatch(legacy.markup, /RAW_ARGUMENT|RAW_RESULT|internal_tool/,
          'raw tool fields and the fallback tool id stay hidden when a display label exists')
      } finally {
        live.dom.window.close()
        legacy.dom.window.close()
      }
    })

    await t.test('short groups stay readable without unnecessary disclosure chrome', () => {
      const fixture = renderTurn({
        role: 'assistant', content: '', streaming: false,
        activity: [{ type: 'tools', labels: ['Read brief', 'Inspect UI', 'Check contrast'] }],
      })
      try {
        assert.equal(fixture.document.querySelector('.asst-tool-disclosure'), null)
        assert.equal(fixture.document.querySelector('.asst-tool-line .asst-tool-label').textContent,
          'Read brief · Inspect UI · Check contrast')
        assert.equal(fixture.document.querySelector('.asst-tool-line summary'), null)
      } finally {
        fixture.dom.window.close()
      }
    })

    await t.test('large provider groups retain only the newest forty list rows', () => {
      const labels = Array.from({ length: 45 }, (_, index) => `Tool step ${index + 1}`)
      labels[44] = 'Z'.repeat(1_000)
      const fixture = renderTurn({
        role: 'assistant', content: '', streaming: false,
        activity: [{ type: 'tools', labels }],
      })
      try {
        const list = fixture.document.querySelector('.asst-tool-list')
        const rows = [...list.children]
        assert.equal(rows.length, 40)
        // An explicit `value` per row, not a computed `start` on the list. `start` was derived as
        // `total - shown + 1`, which is the right first ordinal only when EVERY windowed item
        // produced a label — one unlabelled payload shifted all forty. See
        // `assistantToolActivity.test.js` for that topology; here the two spellings agree.
        assert.equal(list.getAttribute('start'), null)
        assert.equal(rows[0].getAttribute('value'), '6')
        assert.equal(rows.at(-1).getAttribute('value'), '45')
        assert.equal(rows[0].textContent, 'Tool step 6')
        assert.equal(rows.at(-1).textContent, 'Z'.repeat(240))
        assert.equal(fixture.document.querySelector('.asst-tool-limit').textContent,
          'Showing the latest 40 of 45 steps.')
        assert.equal(fixture.document.querySelector('.asst-tool-toggle .asst-tool-label').textContent,
          `45 tool steps · ${'Z'.repeat(240)}`)
        assert.doesNotMatch(fixture.markup, /Z{241}/, 'each provider label has a fixed text budget')
      } finally {
        fixture.dom.window.close()
      }
    })
  } finally {
    await vite.close()
  }
})
