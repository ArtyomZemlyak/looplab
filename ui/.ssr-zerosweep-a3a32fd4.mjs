// task a3a32fd4: render the state-driven panels against every REAL run's folded state and report
// every place a bare `0` reaches the screen, with its label, so each one can be triaged as a
// measured zero or an unknown printed as a number. Throwaway probe, not a test.
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
import { fileURLToPath } from 'node:url'

const BASE = process.env.LL_BASE || 'http://127.0.0.1:8873'
const store = new Map()
const noopStorage = { getItem: k => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k), clear: () => store.clear() }
globalThis.window = globalThis.window || {
  addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  localStorage: noopStorage, sessionStorage: noopStorage, location: { hash: '', href: BASE },
  requestAnimationFrame: fn => setTimeout(fn, 0), cancelAnimationFrame: clearTimeout,
  devicePixelRatio: 1, innerWidth: 1600, innerHeight: 900,
}
globalThis.localStorage = noopStorage
globalThis.sessionStorage = noopStorage
globalThis.document = globalThis.document || {
  documentElement: { style: { setProperty() {} }, classList: { add() {}, remove() {}, toggle() {}, contains: () => false }, dataset: {}, setAttribute() {}, removeAttribute() {}, getAttribute: () => null },
  body: { classList: { add() {}, remove() {}, toggle() {}, contains: () => false } },
  querySelector: () => null, querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
}
globalThis.requestAnimationFrame = globalThis.requestAnimationFrame || (fn => setTimeout(fn, 0))
globalThis.cancelAnimationFrame = globalThis.cancelAnimationFrame || clearTimeout
globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }
globalThis.IntersectionObserver = globalThis.IntersectionObserver || class { observe() {} unobserve() {} disconnect() {} }
globalThis.EventSource = globalThis.EventSource || class { constructor() {} close() {} addEventListener() {} }

const getJSON = async path => {
  const r = await fetch(BASE + path)
  if (!r.ok) throw new Error(`${path} -> ${r.status}`)
  return r.json()
}

const vite = await createServer({
  root: fileURLToPath(new URL('.', import.meta.url)),
  configFile: false, appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
})

// Every text node in the markup, in order, so a bare "0" can be reported with what precedes it.
function bareZeroContexts(markup) {
  const parts = markup.split(/<[^>]*>/g).map(s => s.replace(/&#x27;/g, "'").replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>'))
  const out = []
  for (let i = 0; i < parts.length; i++) {
    if (parts[i].trim() !== '0') continue
    const before = parts.slice(Math.max(0, i - 6), i).map(s => s.trim()).filter(Boolean).join(' ¦ ')
    const after = parts.slice(i + 1, i + 5).map(s => s.trim()).filter(Boolean).join(' ¦ ')
    out.push(`«${before}» 0 «${after}»`)
  }
  return out
}

const panels = await vite.ssrLoadModule('/src/panels.jsx')
const report = await vite.ssrLoadModule('/src/Report.jsx')
const board = await vite.ssrLoadModule('/src/CardBoard.jsx')
const chips = await vite.ssrLoadModule('/src/ConceptChipBar.jsx')

const noop = () => {}
const CASES = [
  ['OverviewPanel', s => React.createElement(panels.OverviewPanel, { state: s, maxEval: null, onClose: noop })],
  ['TrustPanel', s => React.createElement(panels.TrustPanel, { state: s, runId: 'r', onClose: noop, onSelect: noop, onToast: noop, readOnly: true })],
  ['SensitivityPanel', s => React.createElement(panels.SensitivityPanel, { state: s, onClose: noop, onSelect: noop })],
  ['FailuresPanel', s => React.createElement(panels.FailuresPanel, { state: s, onClose: noop, onSelect: noop })],
  ['ParetoPanel', s => React.createElement(panels.ParetoPanel, { state: s, onClose: noop, onSelect: noop })],
  ['DataQualityPanel', s => React.createElement(panels.DataQualityPanel, { state: s, onClose: noop })],
  ['RegistryPanel', s => React.createElement(panels.RegistryPanel, { state: s, onClose: noop })],
  ['HyperImportancePanel', s => React.createElement(panels.HyperImportancePanel, { state: s, onClose: noop })],
  ['CrossRunPanel', s => React.createElement(panels.CrossRunPanel, { state: s, onClose: noop })],
  ['QueuePanel', s => React.createElement(panels.QueuePanel, { state: s, runId: 'r', onSelect: noop, onClose: noop, onToast: noop })],
  ['ResearchPanel', s => React.createElement(panels.ResearchPanel, { state: s, runId: 'r', onToast: noop, onClose: noop })],
  ['ReportView', s => React.createElement(report.default, { state: s, runId: 'r', onOpenPanel: noop, canOpenPanel: () => false, onToast: noop, onPickNode: noop, readOnly: true })],
  ['HypothesisBoard', s => React.createElement(board.HypothesisBoard, { state: s, runId: 'r', runGeneration: null, onSelect: noop, onClose: noop, onToast: noop })],
  ['ConceptChipBar', s => React.createElement(chips.default, { state: s, onHighlight: noop })],
]

const runs = (await getJSON('/api/runs')).map(r => r.run_id)
const tally = new Map()
for (const runId of runs) {
  let state
  try { state = (await getJSON(`/api/runs/${encodeURIComponent(runId)}/state`)).state } catch { continue }
  for (const [name, make] of CASES) {
    let markup
    try { markup = renderToStaticMarkup(make(state)) } catch (e) {
      const key = `${name}  !! THREW: ${String(e.message).slice(0, 90)}`
      tally.set(key, (tally.get(key) || new Set()).add(runId))
      continue
    }
    for (const ctx of bareZeroContexts(markup)) {
      const key = `${name}  ${ctx}`
      if (!tally.has(key)) tally.set(key, new Set())
      tally.get(key).add(runId)
    }
  }
}
for (const [key, runIds] of [...tally.entries()].sort()) {
  console.log(`[${runIds.size}] ${key}`)
  console.log(`      runs: ${[...runIds].slice(0, 4).join(', ')}`)
}
await vite.close()
