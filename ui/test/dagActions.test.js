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
  assert.match(runView, /<Dag key=\{`experiment-graph:\$\{runId\}:\$\{generation \|\| 'pending'\}`\}/,
    'a semantic generation boundary remounts the DAG and closes any same-id stale action menu')
  assert.match(runView, /state=\{state\} selectedId=\{selectedId\} onSelect=\{onCanvasSelect\}/,
    'keyboard node selection must use the same merge-arm aware handler as pointer selection')
  assert.match(runView, /<ConceptChipBar key=\{`concept-filter:\$\{runId\}:\$\{generation \|\| 'pending'\}`\}/,
    'concept selection cannot leak across semantic generations')
  assert.match(dag, /focusable: false,/, 'React Flow must not add a second anonymous tab stop around the card')
  assert.match(dag, /onOpenActions: onNodeAction \? openActions : null/)
  // The gate was renamed AND widened: `mutationReadOnlyMode` is `readOnlyMode` plus lost run
  // authority plus an in-progress start-over. Pinning the old name meant this assertion stopped
  // running exactly when the gate grew the conditions most worth checking.
  //
  // [2026-08-14] It grew a CARVE-OUT, and this pin had to move with it rather than be made green:
  // a HISTORICAL snapshot now opens the menu for exactly one action, branching from the snapshot
  // (`forkFromSeqModel.js` says why that one and no other — its whole intent is in the payload).
  // What this assertion still holds is the half that did not change: review, a stale-generation
  // link, an unresolved start-over and an unloaded run all still get `null`, because `forkAccess.ok`
  // is false in every one of them. That is a truth table now, driven in `forkFromSeqPanel.test.js`;
  // here the property is that the component asks it instead of exposing the menu unconditionally.
  assert.match(runView, /onNodeAction=\{mutationReadOnlyMode && !forkAccess\.ok \? null : onNodeAction\}/,
    'review, stale-link and start-over must not expose node actions at all')
  assert.match(runView, /nodeMenuActions=\{forkAccess\.ok \? FORK_ONLY_NODE_MENU : null\}/,
    'and a read-only view that CAN branch must be offered that one item, not the whole menu')
  assert.match(runView, /const mutationReadOnlyMode = readOnlyMode \|\| runAuthorityBlocked \|\| startOverMutationBlocked/,
    'a run whose authority was lost, or that is mid start-over, must not offer node mutations either')
  assert.match(runView, /const readOnlyMode = reviewMode \|\| historyActive \|\| routeFenceBlocked/)
  assert.match(groups, /<button type="button" className="grp-pill"/)
  assert.match(groups, /aria-label=\{`Collapse group \$\{label\}; \$\{countDescription\}`\}/)
  assert.match(dag, /totalCount: groups\.get\(cell\.key\)\?\.length \?\? cell\.ids\.length/)
  assert.match(groups, /\$\{count\}\/\$\{totalCount\} · split/,
    'a group repeated in topology bands must disclose cell subtotal versus whole-group total')
  assert.match(groups, /experiments in this topology band/)
})

test('DAG action popup follows the ARIA menu keyboard pattern and restores focus', async () => {
  const dag = await source('Dag.jsx')

  assert.match(dag, /className="node-menu" role="menu" aria-label=\{`Actions for experiment #\$\{menu\.nodeId\}`\}/)
  // [2026-08-14] This counted nine hand-written `<button role="menuitem">`s. The menu became a
  // TABLE when a historical snapshot needed to offer one item and not nine, so there is now a single
  // button template — which is a stronger form of the same property, not a weaker one: every row is
  // a real menuitem with the roving tab stop BY CONSTRUCTION, and a tenth item cannot ship without
  // them. The count moved to the table, and `forkFromSeqPanel.test.js` drives which rows each view
  // actually gets.
  assert.equal((dag.match(/<button type="button" key=\{entry\.id\} role="menuitem" tabIndex=\{-1\}/g) || []).length, 1,
    'one template, so no row can be built without menu semantics')
  assert.equal((dag.match(/^ {2}\{ id: /gm) || []).length, 10,
    'ten actions in the table: the nine the live menu has always offered, plus the snapshot branch')
  assert.match(dag, /querySelector\('\[role="menuitem"\]'\)\?\.focus\(\{ preventScroll: true \}\)/)
  for (const key of ['Tab', 'ArrowDown', 'ArrowUp', 'Home', 'End', 'Escape']) {
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
