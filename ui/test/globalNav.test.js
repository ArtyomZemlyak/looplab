import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

import {
  GLOBAL_DESTINATIONS, INSTALLATION_ROUTE_PANEL, INSTALLATION_SCOPED_RUN_PANELS,
  globalHashForRunPanel, installationRouteLabel, isInstallationRouteView,
} from '../src/globalNav.js'
import { RUN_ROUTE_PANELS, parseRunRouteState } from '../src/runRouteState.js'

const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

// The complaint this file guards: the run's menu and LoopLab's menu were one menu. Three panels that
// take no run id (cross-run memory, authored prompts/skills/knowledge, the host's GPUs) were only
// reachable from INSIDE a run, and the run's own config editor was labelled "Settings" one screen
// away from the engine-defaults "Settings". These tests pin the SPLIT, not the styling.

test('every LoopLab destination is a distinct, labelled, canonical hash', () => {
  const hashes = GLOBAL_DESTINATIONS.map(entry => entry.hash)
  assert.equal(new Set(hashes).size, hashes.length, 'two destinations must not share a hash')
  const keys = GLOBAL_DESTINATIONS.map(entry => entry.key)
  assert.equal(new Set(keys).size, keys.length)
  for (const entry of GLOBAL_DESTINATIONS) {
    assert.match(entry.hash, /^#\/[a-z-]*$/, `${entry.key} must be a plain owner hash route`)
    assert.ok(entry.label && entry.title, `${entry.key} needs a label and an explanation`)
  }
  // The legacy aliases are canonicalized by App; the menu must only ever emit the canonical form.
  // `#/atlas` joined `#/research-atlas` on that list when doc 29 F7 renamed the surface to
  // Claims & Curation, so BOTH old spellings have to stay out of the menu.
  assert.ok(!hashes.includes('#/research-atlas'))
  assert.ok(!hashes.includes('#/atlas'))
})

test('the installation route views round-trip to their panels and back', () => {
  for (const [view, panel] of Object.entries(INSTALLATION_ROUTE_PANEL)) {
    assert.ok(isInstallationRouteView(view))
    assert.ok(INSTALLATION_SCOPED_RUN_PANELS.has(panel))
    const destination = GLOBAL_DESTINATIONS.find(entry => entry.key === view)
    assert.ok(destination, `${view} needs a LoopLab menu entry`)
    assert.equal(globalHashForRunPanel(panel), destination.hash)
    assert.equal(installationRouteLabel(view), destination.label)
  }
  assert.equal(globalHashForRunPanel('queue'), null, 'a run-scoped panel has no LoopLab home')
  assert.equal(globalHashForRunPanel('trust'), null)
  assert.ok(!isInstallationRouteView('run'))
  assert.ok(!isInstallationRouteView('list'))
})

test('moved panels keep working as deep links inside a run', () => {
  // The whole point of leaving them mounted: an operator's bookmark and every URL already written
  // into the docs must still resolve to the same panel. Losing this is what makes an IA change cost
  // the operator their bookmarks.
  for (const panel of INSTALLATION_SCOPED_RUN_PANELS) {
    assert.ok(RUN_ROUTE_PANELS.includes(panel),
      `${panel} must stay a parseable run panel or its deep links 404`)
    const gen = 'a'.repeat(64)
    const { state, issues } = parseRunRouteState(`#/run/demo?gen=${gen}&panel=${panel}`)
    assert.equal(state.panel, panel)
    assert.deepEqual(issues, [])
  }
})

test('the run menu offers only run-scoped panels', async () => {
  const runView = await source('RunView.jsx')
  const hubs = runView.slice(runView.indexOf('const HUBS = ['),
    runView.indexOf('const HUB_OF ='))
  assert.ok(hubs.includes("['queue', 'Queue']"), 'the hub block must be the one being read')
  for (const panel of INSTALLATION_SCOPED_RUN_PANELS) {
    assert.ok(!hubs.includes(`'${panel}'`),
      `${panel} is installation-wide and must not be offered by a run's menu`)
  }
  // Still mounted below the bar — that is what keeps the deep links above alive.
  for (const panel of INSTALLATION_SCOPED_RUN_PANELS) {
    assert.ok(runView.includes(`panel === '${panel}' && panelAllowed('${panel}')`),
      `${panel} must still render for an existing ?panel= link`)
  }
})

test('the two Settings are named apart and the LoopLab menu reaches both scopes', async () => {
  const [runView, runList] = await Promise.all([source('RunView.jsx'), source('RunList.jsx')])
  // A run's config editor and the engine defaults were both plain "Settings", one screen apart.
  assert.ok(runView.includes('>Run settings</button>'),
    "the run's own config button must say which scope it edits")
  assert.ok(GLOBAL_DESTINATIONS.some(entry => entry.hash === '#/settings' && entry.label === 'Settings'))
  // Both planes mount the same menu, so "what about LoopLab" is answerable without leaving a run.
  // The menu is now the LoopLab MARK itself rather than a second "LoopLab ▾" button beside it —
  // `brandMenu.test.js` renders that trigger and owns its shape; what stays here is that each plane
  // reaches the menu at all, and that the run list still names its own destination as the open one.
  assert.ok(runView.includes('<GlobalMenu />'))
  assert.ok(runList.includes('<GlobalMenu current="list"'))
})

test('a public review link never advertises installation surfaces', async () => {
  const runView = await source('RunView.jsx')
  // The guard used to be `{!reviewMode && <GlobalMenu />}` — render the menu or render nothing.
  // Now that the mark IS the trigger, rendering nothing would delete the wordmark from the public
  // review header, so the review arm is the inert mark instead: same refusal, still spelled at the
  // one call site that knows `reviewMode`. `brandMenu.test.js` proves that arm renders no button,
  // no `aria-haspopup` and no destination — the properties that make it safe on a route which
  // bypasses OwnerAuth.
  assert.ok(runView.includes('{reviewMode ? <BrandMark /> : <GlobalMenu />}'),
    'the review route bypasses OwnerAuth, so it must not render the owner LoopLab menu')
  assert.ok(!runView.includes('reviewMode && <GlobalMenu'),
    'the owner menu must never be reachable from the review arm')
})

test('an installation destination renders as a page, not a modal over an empty shell', async () => {
  const [shell, installation] = await Promise.all([
    source('PanelShell.jsx'), source('InstallationView.jsx'),
  ])
  assert.ok(installation.includes('<PanelPresentationContext.Provider value="page">'))
  assert.ok(shell.includes("useDialogFocus(dialogRef, onClose, !page)"),
    'page presentation must not install a modal focus trap')
  // aria-modal on a destination would make the LoopLab menu in its own header inert.
  const pageBranch = shell.slice(shell.indexOf('if (page) return'), shell.indexOf('return <div className="overlay"'))
  assert.ok(!pageBranch.includes('aria-modal'))
})
