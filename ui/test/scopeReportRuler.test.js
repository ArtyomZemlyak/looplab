import assert from 'node:assert/strict'
import test from 'node:test'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { sharedVite } from './_mount.js'
import { observationRuler } from '../src/scopeReportModel.js'

// Doc 68 68.2 (critic 2026-09-27, second pass): "Unranked metrics" printed a retargeted run's value
// bare, though the server stamps the objective it was ranked by on the observation.
test('an observation names the objective its run was ranked by, and only then', () => {
  assert.equal(observationRuler({ objective_key: 'filtered' }),
               ' · ranked by filtered (an operator retarget)')
  for (const item of [{}, { objective_key: '' }, { objective_key: null }, { objective_key: 3 }, null]) {
    assert.equal(observationRuler(item), '')
  }
})

test('the scope report draws the ruler beside the number it qualifies', async () => {
  const vite = await sharedVite()
  try {
    const { MetricRun } = await vite.ssrLoadModule('/src/ScopeReport.jsx')
    const draw = item => renderToStaticMarkup(React.createElement(MetricRun, { item }))
    assert.match(draw({ run_id: 'r1', metric: 0.7, objective_key: 'filtered' }),
                 /0\.7.*r1.*ranked by filtered \(an operator retarget\)/s)
    assert.doesNotMatch(draw({ run_id: 'r1', metric: 0.7 }), /ranked by/)
  } finally {
    await vite.close()
  }
})
