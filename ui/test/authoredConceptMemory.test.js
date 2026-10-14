// doc 80 §9 item 7: the authored concept tier is offered in the portfolio Concepts view, labelled as
// coverage and read only on demand (a static render fetches nothing).
import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { sharedVite } from './_mount.js'

test('the Concepts view offers the authored tier as unmeasured coverage, closed by default', async () => {
  const vite = await sharedVite()
  try {
    const { default: PortfolioConcepts } = await vite.ssrLoadModule('/src/PortfolioConcepts.jsx')
    const markup = renderToStaticMarkup(React.createElement(PortfolioConcepts, { runs: [] }))
    assert.match(markup, /<details class="pc-authored">/)
    assert.match(markup, /Authored concept memory \(not measured\)/)
    assert.match(markup, /not evidence that it worked/)
    assert.doesNotMatch(markup, /pc-authored-list/, 'nothing is listed before the operator opens it')
  } finally { await vite.close() }
})
