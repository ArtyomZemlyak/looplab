// Two render sites printed catalogue strings without translating them, so the Russian UI showed the
// lineage's group-by options ("primary concept axis", "operator", "metric", "niche", "none") and the
// link-state notice ("Diagnostic state without a generation fence was ignored.") in English. Found by
// opening the offline demo in Russian in a real browser (2026-10-10).
//
// Both halves are needed: the string must have a catalogue entry, AND the site must pass it through
// `uiText` — the entries for four of the five options already existed and were never used.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { GROUP_MODES } from '../src/grouping.js'

const catalogue = JSON.parse(readFileSync(new URL('../src/locales/ru.json', import.meta.url), 'utf8')).messages
const source = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')     // code only, never a comment

test('every group-by option has a Russian label and the select translates it', () => {
  for (const [, label] of GROUP_MODES) assert.ok(catalogue[label] && catalogue[label] !== label, label)
  assert.match(source('Dag.jsx'), /GROUP_MODES\.map\(\(\[v, l\]\) => <option key=\{v\} value=\{v\}>\{uiText\(l\)\}<\/option>\)/)
})

test('every fixed link-state notice has a Russian text and the notice translates it', () => {
  const fixed = [...source('runRouteState.js').matchAll(/issues\.push\('([^']+)'\)/g)].map(match => match[1])
  assert.ok(fixed.includes('Diagnostic state without a generation fence was ignored.'), fixed)
  for (const issue of fixed) assert.ok(catalogue[issue], issue)
  assert.match(source('RunView.jsx'), /\.\.\.route\.issues\.map\(issue => uiText\(issue\)\)/)
})

test('the run page label is translated as a template, not as a finished sentence', () => {
  // `uiText("Run demo")` matched no entry, so every run page was titled and announced in English.
  assert.ok(catalogue['Run {0}'])
  assert.match(source('App.jsx'), /const routeLabel = route\.view === 'run' \? uiMessage\('Run \{0\}', \[route\.id\]\)/)
})
