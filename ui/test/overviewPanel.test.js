import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))

test('Overview prioritizes measured state and keeps long hints available in a disclosure', async () => {
  const vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom',
    logLevel: 'silent', server: { middlewareMode: true } })
  try {
    const { OverviewPanel } = await vite.ssrLoadModule('/src/panels.jsx')
    const state = {
      task_id: 'toy', goal: 'Minimize loss', direction: 'min', phase: 'finished',
      nodes: { 1: { id: 1, metric: 0.5 }, 2: { id: 2, status: 'failed' }, 3: { id: 3, metric: 0.4 } },
      best_node_id: 3, total_eval_seconds: 90, llm_cost: { total_tokens: 1200 },
      reward_hacks: [{}], pending_hints: [{ text: 'First **idea**' }, { text: 'Latest **idea**' }],
      active_strategy: { policy: 'greedy', rationale: 'Measured result supports it.' },
    }
    const dom = new JSDOM(renderToStaticMarkup(React.createElement(OverviewPanel,
      { state, maxEval: 120, phase: 'finished' })))
    try {
      const doc = dom.window.document
      assert.equal(doc.querySelector('.ov-best strong').textContent, '0.4')
      assert.match(doc.querySelector('.ov-run-facts').textContent, /2 evaluated.*3 nodes.*1/s)
      assert.equal(doc.querySelector('.ov-latest p').textContent, 'Latest idea')
      assert.equal(doc.querySelector('.ov-hints').children.length, 2)
      assert.equal(doc.querySelector('.ov-hints').closest('details').open, false)
      assert.match(doc.querySelector('.ov-signal-alert').textContent, /Open Trust/)
      const bar = doc.querySelector('[role="progressbar"]')
      assert.equal(bar.getAttribute('aria-valuemax'), '120')
      assert.equal(bar.getAttribute('aria-valuenow'), '90')
      assert.equal(bar.querySelector('span').style.width, '75%')
    } finally { dom.window.close() }

    const noLimit = new JSDOM(renderToStaticMarkup(React.createElement(OverviewPanel,
      { state: { nodes: {}, total_eval_seconds: 0 }, maxEval: null })))
    try {
      assert.equal(noLimit.window.document.querySelector('[role="progressbar"]'), null)
      assert.match(noLimit.window.document.body.textContent, /No measured result yet/)
      assert.match(noLimit.window.document.body.textContent, /Evaluation-time limit unavailable/)
    } finally { noLimit.window.close() }

    const zeroLimit = new JSDOM(renderToStaticMarkup(React.createElement(OverviewPanel,
      { state: { nodes: {}, total_eval_seconds: 0 }, maxEval: 0 })))
    try {
      assert.match(zeroLimit.window.document.querySelector('.ov-budget').textContent, /0s \/ 0s/)
      assert.equal(zeroLimit.window.document.querySelector('[role="progressbar"]'), null)
    } finally { zeroLimit.window.close() }

    const overLimit = new JSDOM(renderToStaticMarkup(React.createElement(OverviewPanel,
      { state, maxEval: 60, phase: 'paused' })))
    try {
      const doc = overLimit.window.document
      assert.equal(doc.querySelector('[role="progressbar"]').getAttribute('aria-valuenow'), '60')
      assert.match(doc.querySelector('.ov-budget-note').textContent, /Over limit by 30s/)
      assert.match(doc.querySelector('.ov-run-facts').textContent, /paused/)
    } finally { overLimit.window.close() }
  } finally { await vite.close() }
})
