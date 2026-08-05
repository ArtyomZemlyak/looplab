// Server-render the REAL ConceptChipBar against REAL run state fetched over HTTP from the live server.
// usage: node .ssr-render.mjs <harness-dir> [runId ...]
// jsdom cannot load on node 20.8.1 either (it require()s an ESM dep), so the markup is matched directly.
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const HARNESS = process.argv[2]
const RUNS = process.argv.slice(3)
const BASE = process.env.LL_BASE || 'http://127.0.0.1:8792'

const { default: ConceptChipBar } = await import(`${HARNESS}/ConceptChipBar.mjs`)
const { conceptMaterializationStatus } = await import(`${HARNESS}/nodeProjection.mjs`)

async function getJSON(pathname) {
  const response = await fetch(BASE + pathname)
  if (!response.ok) throw new Error(`${pathname} -> ${response.status}`)
  return response.json()
}

const strip = html => html.replace(/<[^>]*>/g, '').replace(/&#x27;/g, "'").replace(/&amp;/g, '&').trim()
const all = (html, re) => [...html.matchAll(re)].map(match => match[1])

const runs = RUNS.length ? RUNS : (await getJSON('/api/runs')).map(run => run.run_id)
for (const runId of runs) {
  const { state } = await getJSON(`/api/runs/${encodeURIComponent(runId)}/state`)
  const markup = renderToStaticMarkup(React.createElement(ConceptChipBar, { state, onHighlight() {} }))
  const names = all(markup, /class="cb-name">([^<]*)</g)
  const counts = all(markup, /class="cb-count">([^<]*)</g)
  const chips = names.map((name, i) => `${name}(${counts[i]})`)
  const banner = strip((markup.match(/class="muted">(.*?)<\/span>/s) || [])[1] || '')
  const warn = all(markup, /class="chip xs warn"[^>]*>(.*?)<\/span>/gs).map(strip).join(' | ')
  const role = (markup.match(/class="concept-bar" role="([^"]+)"/) || [])[1] || '-'
  const tagged = Object.values(state.node_concepts || {}).filter(v => v && v.length).length
  const total = Object.values(state.node_concepts || {}).reduce((n, v) => n + (v?.length || 0), 0)
  if (!markup && !tagged) continue                       // genuinely untagged run: bar absent, correct
  console.log(`${runId}`)
  console.log(`   state: ${tagged} tagged experiment(s), ${total} membership(s), `
    + `receipts=${JSON.stringify(state.node_concept_materialization_receipts || {})}, `
    + `base_receipt=${JSON.stringify(state.run_base_concept_receipt)}`)
  console.log(`   aggregate status: ${conceptMaterializationStatus(state)}`)
  console.log(`   RENDER: ${markup ? `role=${role}` : '<nothing rendered>'}`
    + (warn ? ` warn="${warn}"` : '') + (banner ? ` banner="${banner}"` : ''))
  console.log(`   chips (${chips.length}): ${chips.join(' ') || '-'}`)
}
