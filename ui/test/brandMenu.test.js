import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

// The complaint this file guards: the run header rendered the word LoopLab TWICE, side by side —
// once as an inert wordmark and once as a button labelled "LoopLab ▾" that opened the installation
// menu. The owner asked for one thing: the mark itself, carrying the arrow, opening the menu.
//
// These tests RENDER the components rather than reading their source. That is deliberate: the whole
// property here is what an operator's browser receives (is the mark a button? is it inside the
// button? is the arrow outside the element the stylesheet hides?), and every one of those questions
// is invisible to a substring pin — a `className="brand"` pin passes just as happily on a mark that
// sits beside the trigger as on one that IS the trigger, which is the exact defect being fixed.
// SSR is enough because the closed trigger is the whole contract that changed; nothing here needs a
// DOM, which also keeps this file out of the way of the `jsdom` import that kills a whole test file
// in this environment.

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = new URL('../src/', import.meta.url)
const source = name => readFile(new URL(name, SRC), 'utf8')

// One vite server for the file: `createServer` costs 50-80s here, and every test wants the same
// module graph.
let server = null
const ui = () => {
  server ||= createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  return server
}
after(async () => { if (server) await (await server).close() })

const menuModule = async () => (await ui()).ssrLoadModule('/src/GlobalMenu.jsx')

// The mark's exact markup, as the six topbars have always rendered it. Pinned as a literal because
// `runScreenShell.test.js` pins the same bytes inside a whole rendered run screen: the two must
// agree, and this is the definition both of them are reading.
const MARK = '<span class="brand"><span class="dot">◉</span> LoopLab</span>'

const buttons = html => [...html.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)]

test('the LoopLab mark IS the menu trigger — one control, not a wordmark beside a button', async () => {
  const { default: GlobalMenu } = await menuModule()
  const html = renderToStaticMarkup(React.createElement(GlobalMenu))

  const found = buttons(html)
  assert.equal(found.length, 1, 'a closed LoopLab menu is exactly one control')
  const [trigger] = found
  // The mark is INSIDE the trigger, and there is no second copy of it anywhere in the render. This
  // is the redundancy the owner reported: two elements carrying the same word, one inert.
  assert.equal((html.match(/class="brand"/g) || []).length, 1)
  assert.ok(trigger[1].includes(MARK), 'the trigger must contain the mark itself, not a re-spelling')
  assert.equal(html.indexOf(MARK), trigger.index + trigger[0].indexOf(MARK),
    'the only mark in the header must be the one inside the trigger')

  // A menu button, by the ARIA contract: it announces that it opens a menu, whether it is open, and
  // which element it owns. `aria-expanded` must be present and false while closed — an absent one
  // reads as "not expandable" and is how a disclosure silently becomes an ordinary button.
  assert.match(trigger[0], /aria-haspopup="menu"/)
  assert.match(trigger[0], /aria-expanded="false"/)
  assert.match(trigger[0], /aria-controls="looplab-global-menu"/)
  assert.match(trigger[0], /type="button"/)

  // The arrow follows the word and sits OUTSIDE `.brand`. Both halves matter: styles.css hides
  // `.run-head .brand` at ≤900px, so an arrow inside the mark would leave a zero-width invisible
  // button on every phone-width run view, and the installation menu would be unreachable there.
  const mark = trigger[1].slice(trigger[1].indexOf(MARK), trigger[1].indexOf(MARK) + MARK.length)
  assert.ok(!mark.includes('▾'), 'the arrow must survive a hidden mark')
  assert.ok(trigger[1].indexOf('▾') > trigger[1].indexOf(MARK), 'the arrow follows the word')
  assert.match(trigger[1], /<span class="global-menu-caret" aria-hidden="true">▾<\/span>/,
    'the arrow is decoration; the accessible name must not depend on a glyph')

  // …and because the visible label can be hidden, the name is on the button. It must still START
  // with the visible word or speech input cannot address the control (WCAG 2.5.3 Label in Name).
  const label = /aria-label="([^"]*)"/.exec(trigger[0])
  assert.ok(label, 'a control whose visible label can be display:none needs its own name')
  assert.ok(label[1].startsWith('LoopLab'), label[1])
})

test('the mark discloses a menu; it does not also navigate', async () => {
  // `Runs (#/)` is the first item of the menu the mark opens, so a mark that ALSO navigated to `#/`
  // would give one click two meanings — and `aria-haspopup="menu"` promises activation opens the
  // menu. The destination the wordmark used to imply is the first thing the menu offers instead.
  const { default: GlobalMenu } = await menuModule()
  const html = renderToStaticMarkup(React.createElement(GlobalMenu))
  assert.ok(!html.includes('<a '), 'the closed trigger must not be, or contain, a link')
  assert.ok(!html.includes('href='))
  const { GLOBAL_DESTINATIONS } = await import('../src/globalNav.js')
  assert.equal(GLOBAL_DESTINATIONS[0].hash, '#/',
    'the mark stops being a link only because the menu leads with that destination')
})

test('a public review capability gets an inert mark — not a dead button, not an empty menu', async () => {
  const { BrandMark } = await menuModule()
  const html = renderToStaticMarkup(React.createElement(BrandMark))
  assert.equal(html, MARK)
  // Stated as properties too, because the literal above is what must not drift while these are what
  // must not appear: a disabled trigger or one opening an empty menu still ADVERTISES that
  // installation-wide owner surfaces exist, on a route that bypasses OwnerAuth.
  for (const forbidden of ['<button', 'aria-haspopup', 'aria-expanded', 'role=', 'href=', 'disabled']) {
    assert.ok(!html.includes(forbidden), `an inert mark must not render ${forbidden}`)
  }
})

test('the review route renders the inert arm and the owner routes render the menu', async () => {
  const runView = await source('RunView.jsx')
  // The branch is at the call site because that is the only place `reviewMode` is known (App.jsx
  // passes it to RunView alone). Counted, not merely present: a second copy of the ternary would be
  // a second header, and a commented-out one pushes this to 2.
  assert.equal((runView.match(/\{reviewMode \? <BrandMark \/> : <GlobalMenu \/>\}/g) || []).length, 1)
  assert.equal((runView.match(/<GlobalMenu\b/g) || []).length, 1,
    'the workspace header may mount the menu once, and only through the reviewMode branch')

  // Every owner topbar reaches the menu through the same component; no screen composes its own.
  const owners = { 'RunList.jsx': 'list', 'Settings.jsx': 'settings', 'ClaimsCuration.jsx': 'claims' }
  for (const [file, current] of Object.entries(owners)) {
    assert.match(await source(file), new RegExp(`<GlobalMenu current="${current}"`),
      `${file} must mark its own destination as the open one`)
  }
  assert.match(await source('InstallationView.jsx'), /<GlobalMenu current=\{view\} \/>/)
  // RunScreen is the shell for six screens, three of which are passed no `reviewMode` at all, so it
  // cannot tell an owner from a reviewer and must render the inert mark.
  const runScreen = await source('RunScreen.jsx')
  assert.match(runScreen, /<BrandMark \/>/)
  assert.ok(!runScreen.includes('<GlobalMenu'),
    'a shell that cannot see reviewMode must never mount the owner menu')
})

test('the mark has exactly one definition in the tree', async () => {
  // What stops the six topbars drifting back apart is that there is nowhere else to write a mark.
  // A negative pin over the whole source tree, on purpose: a commented-out copy is as much of a
  // drift risk as a live one, and the next screen to grow a topbar has to import this component.
  const files = (await readdir(fileURLToPath(SRC))).filter(name => /\.jsx?$/.test(name))
  assert.ok(files.length > 40, 'the sweep must be reading the real source tree')
  for (const name of files) {
    if (name === 'GlobalMenu.jsx') continue
    const text = await source(name)
    assert.ok(!text.includes('className="brand"'), `${name} must render the mark, not re-spell it`)
    assert.ok(!text.includes('LoopLab ▾'), `${name} must not carry a second copy of the trigger label`)
  }
})
