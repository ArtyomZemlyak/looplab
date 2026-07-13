import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('every mutable DAG experiment exposes a named native menu trigger', async () => {
  const [dag, runView, groups] = await Promise.all([source('Dag.jsx'), source('RunView.jsx'), source('groupnodes.jsx')])

  assert.match(dag, /<button type="button" className="node-action-trigger nodrag nopan"/)
  assert.match(dag, /aria-label=\{`Open actions for experiment #\$\{nodeId\}`\}/)
  assert.match(dag, /aria-haspopup="menu" aria-expanded=\{expanded\}/)
  assert.equal((dag.match(/<NodeActionTrigger nodeId=\{node\.id\}/g) || []).length, 2,
    'both full cards and overview glyphs expose the action trigger')
  assert.match(dag, /<button type="button" className="node-select-trigger"/)
  assert.equal((dag.match(/<NodeSelectionTrigger nodeId=\{node\.id\}/g) || []).length, 2,
    'full cards and overview glyphs expose a native keyboard selection control')
  assert.match(dag, /onClick=\{\(\) => onSelect\(nodeId\)\}/)
  assert.doesNotMatch(dag, /className=\{cardCls[^\n]*role="button"/,
    'the card content must not flatten its nested provenance link under button semantics')
  assert.match(runView, /<Dag state=\{state\} selectedId=\{selectedId\} onSelect=\{onCanvasSelect\}/,
    'keyboard node selection must use the same merge-arm aware handler as pointer selection')
  assert.match(dag, /focusable: false,/, 'React Flow must not add a second anonymous tab stop around the card')
  assert.match(dag, /onOpenActions: onNodeAction \? openActions : null/)
  assert.match(runView, /onNodeAction=\{readOnlyMode \? null : onNodeAction\}/,
    'history and review modes must not expose mutating node actions')
  assert.match(groups, /<button type="button" className="grp-pill"/)
  assert.match(groups, /aria-label=\{`Collapse group \$\{label\}`\}/)
})

test('DAG action popup follows the ARIA menu keyboard pattern and restores focus', async () => {
  const dag = await source('Dag.jsx')

  assert.match(dag, /className="node-menu" role="menu" aria-label=\{`Actions for experiment #\$\{menu\.nodeId\}`\}/)
  assert.equal((dag.match(/<button role="menuitem"/g) || []).length, 9)
  assert.match(dag, /querySelector\('\[role="menuitem"\]'\)\?\.focus\(\{ preventScroll: true \}\)/)
  for (const key of ['ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape']) {
    assert.ok(dag.includes(`event.key === '${key}'`), `${key} must be handled by the menu`)
  }
  assert.match(dag, /returnFocus\?\.isConnected \? returnFocus : fallback \|\| dagRef\.current/,
    'focus falls back to the node selection surface, then the graph, if the trigger unmounts')
  assert.match(dag, /target\?\.focus\(\{ preventScroll: true \}\)/)
  assert.match(dag, /focusToken: \+\+menuOpenSequence\.current/)
  assert.match(dag, /\[menu\?\.focusToken\]/)
  assert.match(dag, /onBlur=\{event => \{ if \(!event\.currentTarget\.contains\(event\.relatedTarget\)\) closeMenu\(false\) \}\}/,
    'Tab/focus leaving the popup must close it instead of leaving an inert backdrop')
  assert.match(dag, /getBoundingClientRect\(\)[\s\S]*?viewport\.right - rect\.width - 8[\s\S]*?viewport\.bottom - rect\.height - 8/,
    'the rendered menu is clamped to the actual viewport and popup dimensions')
  assert.match(dag, /const viewport = window\.visualViewport/)
  assert.match(dag, /viewport\?\.addEventListener\('resize', reclamp\)/)
  assert.match(dag, /onNodeContextMenu[\s\S]*?openMenu\(id, e\.clientX, e\.clientY, trigger\)/,
    'right-click must continue to open the shared accessible menu')
})

test('DAG node action trigger and menu focus are visibly styled', async () => {
  const css = await source('styles.css')

  assert.match(css, /\.node-action-trigger \{[\s\S]*?width: 25px;[\s\S]*?height: 25px;/)
  assert.match(css, /\.node-select-trigger:focus-visible \{ outline: 2px solid var\(--accent\)/)
  assert.match(css, /\.node-select-trigger \{[\s\S]*?pointer-events: none;/,
    'the keyboard control must not cover provenance links, hover details, or drag-to-merge pointer input')
  assert.match(css, /\.node-action-trigger:focus-visible \{ outline: 2px solid var\(--accent\)/)
  assert.match(css, /@media \(max-width: 900px\) \{[\s\S]*?\.node-action-trigger::before \{ content: ''; position: absolute; inset: -10px;/)
  assert.match(css, /\.grp-pill:focus-visible \{ outline: 2px solid var\(--accent\)/)
  assert.match(css, /\.nm-item:focus-visible \{ outline: 2px solid var\(--accent\)/)
  assert.match(css, /\.node-menu \{[\s\S]*?max-height: calc\(100vh - 16px\); overflow-y: auto;/)
})
