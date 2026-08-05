import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

// ConfigPanel.jsx and CardBoard.jsx split out of panels.jsx (doc 25 UI-04). They are listed by name
// because this contract is per-module: dropping them would silently stop covering the tables and the
// keyboard-native row/header rules that moved with them.
const files = ['Dock.jsx', 'Inspector.jsx', 'panels.jsx', 'ConfigPanel.jsx', 'CardBoard.jsx', 'Report.jsx']
const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

test('every user-visible legacy table uses a uniquely named DataTable region', async () => {
  const sources = await Promise.all(files.map(source))
  const captions = []
  let tableCount = 0

  for (const [index, text] of sources.entries()) {
    const tables = text.match(/<table className="tbl"/g) || []
    const wrappers = [...text.matchAll(/<DataTable caption="([^"]+)" card=\{false\}><table className="tbl"/g)]
    tableCount += tables.length
    captions.push(...wrappers.map(match => match[1]))

    // Only a module that actually renders a table owes the shared region an import; CardBoard.jsx is
    // listed here for the wrapper/keyboard rules and renders none, and asserting an unused import on
    // it would be a rule about nothing.
    if (tables.length) {
      assert.match(text, /import \{[^}]*\bDataTable\b[^}]*\} from '\.\/accessibility\.jsx'/,
        `${files[index]} must import the shared table region`)
    }
    assert.equal(wrappers.length, tables.length,
      `${files[index]} has a table outside a non-card DataTable region`)
  }

  assert.ok(tableCount > 0, 'the migration contract must cover real tables')
  assert.equal(new Set(captions).size, captions.length, 'table captions must be unique')
  assert.ok(captions.every(caption => caption.trim().length >= 8), 'captions must be meaningful')
})

test('migrated table actions remain keyboard-native and expose sorting semantics', async () => {
  const [inspector, panels, config, cards] = await Promise.all(
    [source('Inspector.jsx'), source('panels.jsx'), source('ConfigPanel.jsx'), source('CardBoard.jsx')])
  for (const [name, text] of [['Inspector.jsx', inspector], ['panels.jsx', panels],
                              ['ConfigPanel.jsx', config], ['CardBoard.jsx', cards]]) {
    assert.doesNotMatch(text, /<tr[^>]*\bonClick=/, `${name} still contains a mouse-only clickable row`)
    assert.doesNotMatch(text, /<th[^>]*\bonClick=/, `${name} still contains a mouse-only clickable header`)
  }
  assert.match(inspector, /<th aria-sort=\{sortKey === k[\s\S]*?<button type="button" className="table-sort"/)
  // The raw run-configuration table moved to ConfigPanel.jsx with the panel that renders it.
  assert.match(config, /<th scope="row" className="muted">\{k\}<\/th>/)
})
