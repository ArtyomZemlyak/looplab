// A PROVENANCE LABEL BESIDE AN EMPTY CELL — the invented caveat.
//
// `extraKeys` is the UNION of every extras key any node in the run reported, so this node
// legitimately holds no value for most of it. `extraMetricChannel(n, k)` answers `unknown` for a
// key `n` never reported exactly as it does for one recorded before the channel was written down,
// so the table printed a warn "provenance unknown" next to an EMPTY cell — a caveat about a value
// that does not exist, which is the shape `objectiveMetricSource` forbids two lines above it. It
// also fed `anyUnverified`, so a phantom row could summon the whole self-reported footnote.
//
// The second half: the `best #N` column is a DIFFERENT node's number and carried no channel read
// of its own — only the ★ row consulted `champObjective` — so a self-reported champion extra sat
// unlabelled beside this node's labelled one, the by-contrast misread the ★ cell's comment warns
// about.
//
// This renders the real `Metrics` component, because the defect is a GATE and no model test can
// see one.
import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { fileURLToPath } from 'node:url'

import React, { act } from 'react'
import { createServer } from 'vite'
import { JSDOM } from 'jsdom'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
let dom = null, root = null, vite = null
const previous = {}

const node = (id, extras, provenance, metric) => ({
  id, metric, status: 'evaluated', attempt: 0,
  extra_metrics: extras, extra_metrics_provenance: provenance,
  metric_provenance: {},
})

async function html(nodes, bestId) {
  if (!vite) {
    dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',
      { url: 'https://looplab.test/', pretendToBeVisual: true })
    const installed = {
      window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
      location: dom.window.location, sessionStorage: dom.window.sessionStorage,
      MutationObserver: dom.window.MutationObserver, HTMLElement: dom.window.HTMLElement,
      requestAnimationFrame: cb => setTimeout(cb, 0), cancelAnimationFrame: h => clearTimeout(h),
      IS_REACT_ACT_ENVIRONMENT: true, fetch: () => new Promise(() => {}),
    }
    for (const [k, v] of Object.entries(installed)) {
      previous[k] = Object.getOwnPropertyDescriptor(globalThis, k)
      Object.defineProperty(globalThis, k, { configurable: true, writable: true, value: v })
    }
    vite = await createServer({ root: UI_ROOT, configFile: false, appType: 'custom',
                                logLevel: 'silent', server: { middlewareMode: true } })
    const { createRoot } = await import('react-dom/client')
    root = createRoot(document.getElementById('root'))
  }
  const mod = await vite.ssrLoadModule('/src/Inspector.jsx')
  const state = { nodes: Object.fromEntries(nodes.map(n => [n.id, n])), best_node_id: bestId }
  await act(async () => root.render(React.createElement(mod.Metrics, {
    n: nodes[0], detail: {}, state, runId: 'r',
  })))
  return document.getElementById('root').innerHTML
}

after(async () => {
  if (root) await act(async () => root.unmount())
  if (vite) await vite.close()
  for (const [k, d] of Object.entries(previous)) {
    if (d) Object.defineProperty(globalThis, k, d); else delete globalThis[k]
  }
})

// This node reports NOTHING; the champion reports `train_auc`. The union puts `train_auc` on the
// table, and this node's cell for it is empty.

// THE ROW, not the page. An earlier cut of this file asserted `!html.includes('provenance unknown')`
// and failed on correct code: the self-reported FOOTNOTE explains that very phrase, so a page-wide
// search matches the explanation as well as the label. The defect is a CELL rendering a word about
// nothing, so the assertion has to be scoped to the cell.
const rowFor = (html, key) => {
  const rows = html.split('<tr')
  return rows.find(r => r.includes('>' + key + '<') || r.includes(key + '</td>')) || ''
}
const sourceCell = row => {
  const tds = row.split('<td')
  return tds.length > 2 ? tds[2] : ''      // 0 = pre-<td>, 1 = metric name, 2 = source
}

const THIS_NODE = () => node(0, {}, {}, 0.70)
const CHAMP = () => node(1, { train_auc: 0.9 }, { train_auc: 'auto' }, 0.80)

test('a key THIS node never reported gets NO provenance label', async () => {
  const out = await html([THIS_NODE(), CHAMP()], 1)
  assert.ok(out.includes('train_auc'), 'precondition: the union puts the key on the table')
  const cell = sourceCell(rowFor(out, 'train_auc'))
  assert.ok(!cell.includes('provenance unknown'),
    'a caveat beside an empty cell is a claim about a value that does not exist — the '
    + `invented-caveat shape \`objectiveMetricSource\` forbids. source cell was: ${cell.slice(0, 120)}`)
})

test('the self-reported FOOTNOTE is not summoned by a phantom row', async () => {
  const out = await html([THIS_NODE(), CHAMP()], 1)
  // The footnote DOES fire here, and correctly: the champion's `train_auc` is self-reported and is
  // on screen in the `best #1` column, so there is a labelled cell for it to explain. What must not
  // happen is the footnote firing for THIS node's phantom row alone — driven by the next test.
  assert.ok(out.includes('self-reported'), 'the champion cell is labelled, so the footnote belongs')
})

test('a key this node DOES report is still labelled', async () => {
  const out = await html([node(0, { train_auc: 0.5 }, { train_auc: 'auto' }, 0.70), CHAMP()], 1)
  assert.ok(out.includes('self-reported'),
    'the whole rung must survive: an auto-captured value this node really reported is caveated')
})

test('the CHAMPION column carries the CHAMPION\'s own channel', async () => {
  const out = await html([THIS_NODE(), CHAMP()], 1)
  assert.ok(/0\.9.{0,40}self-reported/s.test(out),
    'the best column is a different node\'s number and must be labelled from ITS record — '
    + 'unlabelled beside a labelled cell is the by-contrast misread')
})

test('a DECLARED champion value gets no warn label', async () => {
  const out = await html([THIS_NODE(), node(1, { train_auc: 0.9 }, { train_auc: 'declared' }, 0.80)], 1)
  assert.ok(!/0\.9.{0,40}self-reported/s.test(out),
    'declared is the one channel that is not a caveat; labelling it warn would invert the rung')
})

test('a phantom row ALONE summons no footnote', async () => {
  // No champion at all: the union still carries `train_auc` from a THIRD node this view is not
  // showing a column for, so THIS node's row is empty on both sides and nothing is labelled.
  const other = node(2, { train_auc: 0.4 }, { train_auc: 'auto' }, 0.60)
  const out = await html([THIS_NODE(), other], 0)
  const cell = sourceCell(rowFor(out, 'train_auc'))
  assert.ok(!cell.includes('provenance unknown'), `source cell was: ${cell.slice(0, 120)}`)
  assert.ok(!out.includes('are audit-only and never drive selection'),
    'with no labelled cell on screen the self-reported footnote has nothing to explain and must '
    + 'not print — a phantom row used to summon it')
})
