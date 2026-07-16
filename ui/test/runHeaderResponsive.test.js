import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('compact run header keeps run truth visible and historical identity exact', async () => {
  const view = await source('RunView.jsx')
  assert.match(view, /const gen = generation\?\.slice\(0, 8\) \|\| 'unknown'/)
  assert.match(view, /className="muted"[^>]*>[\s\S]*?<b>\{state\.label \|\| state\.run_id \|\| runId\} · \{displayedPhase\} · gen \{gen\}<\/b>\{state\.goal \|\| state\.task_id\}/)
  assert.equal(view.match(/Historical snapshot · gen \{gen\} · seq/g)?.length, 2,
    'loading and resolved history states must both identify generation and sequence')
})

test('compact header and stale-generation detail reflow without clipped truth', async () => {
  const css = await source('styles.css')
  assert.match(css, /\.route-generation-detail \{[^}]*flex-wrap: wrap;[^}]*max-width: 100%;/)
  assert.match(css, /\.route-generation-detail code \{[^}]*max-width: 100%;[^}]*overflow-wrap: anywhere;/)
  assert.match(css, /@media \(max-width: 900px\) \{[\s\S]*?\.topbar\.run-head \{[^}]*flex-wrap: wrap;/)
  assert.match(css, /\.run-head > \.muted \{[^}]*display: block;[^}]*order: 2;[^}]*flex: 1 1 calc\(100% - 110px\);[^}]*max-width: none;[^}]*overflow: visible;[^}]*white-space: normal;[^}]*overflow-wrap: anywhere;/)
  assert.match(css, /\.run-head > \.live \{[^}]*order: 2;[^}]*max-width: 100%;[^}]*white-space: normal;/)
  assert.match(css, /\.run-head button, \.history-banner \.btn, \.run-resource-state \.btn \{ min-height: 44px; \}/)
  assert.match(css, /\.copy-view-btn \{ min-width: 44px;/)
})
