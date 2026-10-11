import assert from 'node:assert/strict'
import test from 'node:test'

import { JSDOM } from 'jsdom'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { sharedVite } from './_mount.js'

// Measured 2026-10-11 on the offline demo (backend=toy, finished): Progress → Research said "No
// research memos yet" and "Deep research runs on the configured cadence or when the Strategist
// requests it" — on a run that had no model to run research and would never run again.

const run = phase => ({ run_id: 'r', goal: 'g', direction: 'min', phase, nodes: {}, research: [] })

const renderEmpty = async (phase, runBackend) => {
  const vite = await sharedVite()
  const { ResearchPanel } = await vite.ssrLoadModule('/src/panels.jsx')
  const markup = renderToStaticMarkup(React.createElement(ResearchPanel, {
    state: run(phase), runId: 'r', runBackend, onSelect() {}, onSelectEvidence() {},
  }))
  const document = new JSDOM(markup).window.document
  const empty = document.querySelector('.research-empty-state')
  return {
    badge: document.querySelector('.panel-sub').textContent,
    heading: empty.querySelector('strong').textContent,
    reason: empty.querySelector(':scope > p').textContent,
    why: empty.querySelector('details') != null,
  }
}

test('the empty-state reason follows the run: no model, finished, or still searching', async () => {
  const vite = await sharedVite()
  const { researchEmptyReason } = await vite.ssrLoadModule('/src/panels.jsx')
  assert.equal(researchEmptyReason('toy', true),
    'Deep research needs a model. This run used none (backend=toy), so it records no research memos.')
  assert.equal(researchEmptyReason('toy', false), researchEmptyReason('toy', true),
    'an offline run never gets a memo, finished or not')
  assert.equal(researchEmptyReason('llm', true), 'No research memo was recorded before this run finished.')
  assert.equal(researchEmptyReason(null, true), 'No research memo was recorded before this run finished.')
  assert.equal(researchEmptyReason('llm', false),
    'Deep research runs on the configured cadence or when the Strategist requests it.')
  assert.equal(researchEmptyReason(null, false), researchEmptyReason('llm', false),
    'an unread config keeps the old sentence, never the offline one')
})

test('the offline demo’s Research panel says why it is empty and promises no cadence', async () => {
  const offline = await renderEmpty('finished', 'toy')
  assert.equal(offline.heading, 'No research memos')
  assert.equal(offline.badge, 'none', 'the header badge said "none yet", and untranslated (it skipped uiText)')
  assert.match(offline.reason, /needs a model.*backend=toy/)
  assert.equal(offline.why, false, 'the generic "Why can this be empty?" guesses are replaced by the known cause')

  const finished = await renderEmpty('finished', 'llm')
  assert.equal(finished.heading, 'No research memos')
  assert.equal(finished.badge, 'none')
  assert.equal(finished.reason, 'No research memo was recorded before this run finished.')
  assert.equal(finished.why, true)

  const live = await renderEmpty('running', 'llm')
  assert.equal(live.heading, 'No research memos yet')
  assert.equal(live.badge, 'none yet')
  assert.match(live.reason, /configured cadence/)
  assert.equal(live.why, true)
})
