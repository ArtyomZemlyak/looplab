// THE MINIFIER IS PART OF THE UI, and until this file nothing tested it.
//
// The operator reported a bare `0` after every popover trigger in the topbars — `Theme 0`,
// `Energy 0`, `LoopLab ▾ 0`. Four passes over ui/src found nothing, correctly: every one of those
// controls holds `useState(false)` and guards `{open && <>…}`, which is right. The defect was in
// `vite.config.js`, where `terserOptions.compress.booleans_as_integers` rewrote `false` to `0`. That
// is value-preserving for every JS operator and NOT value-preserving for React, which renders
// numbers: the menu still stayed closed, and the guard painted the `0` itself.
//
// Nothing in the suite could see it, because dev, SSR and `node --test` all run the UNMINIFIED
// source. So this file does the one thing that reaches the property: it takes the REAL terser options
// out of the real vite config, minifies a module shaped exactly like what JSX compiles to, EXECUTES
// the minified output, and renders it with the real React server renderer. Rewrite the config and
// this goes red; rewrite the components and it still holds, because it never reads them.
//
// Every assertion is paired with a control arm that forces the option back on. A green test whose
// control does not go red is not evidence, and this one is cheap enough to carry its own proof.
//
// The synthetic probe below is not a guess at the shape. The four REAL topbar controls were built
// through the REAL production pipeline (Rolldown + Terser, the real config) in SSR mode and rendered,
// both ways, on 2026-08-06. Rewritten, they reproduce the operator's screen dump character for
// character — including the fact that DensityToggle, the one control with no `&&` guard, has no zero:
//
//   flag on : DensityToggle ["Aa · Compact"]   ThemeSwitcher ["Theme","0"]
//             EnergyToggle  ["Energy","0"]     GlobalMenu    ["LoopLab ▾","0"]
//   flag off: DensityToggle ["Aa · Compact"]   ThemeSwitcher ["Theme"]
//             EnergyToggle  ["Energy"]         GlobalMenu    ["LoopLab ▾"]
//
// That build takes ~45 s per arm, so it is not repeated here: the property it demonstrates is held
// cheaply by the arms below, and held over the REAL emitted bytes — whichever minifier produced them —
// by `scripts/check-bundle.mjs::findIntegerBooleanChunks`, which CI already runs after every build.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
import { minify } from 'terser'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { findIntegerBooleanChunks } from '../scripts/check-bundle.mjs'

const UI_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

// What `<div className="th-switch"><button aria-expanded={open}/>{open && <div/>}</div>` compiles
// to, written in plain calls so the test needs no JSX transform of its own. The shape is the point:
// a false-initialised state that is BOTH an ARIA value and the left side of a rendering guard.
const POPOVER = `
export function Popover(React) {
  const [open, setOpen] = React.useState(false)
  return React.createElement('div', { className: 'th-switch' },
    React.createElement('button', { type: 'button', 'aria-expanded': open,
      onClick: () => setOpen(!open) }, 'Theme'),
    open && React.createElement('div', { className: 'th-menu' }, 'Dark'))
}
`

const terserOptionsFromViteConfig = async () => {
  const config = (await import(pathToFileURL(join(UI_ROOT, 'vite.config.js')).href)).default
  const options = config?.build?.terserOptions
  assert.ok(options?.compress, 'vite.config.js must still configure terser compression')
  return options
}

// Minify with the given options, import the result, and render it. Executing the minified module is
// what makes this a property rather than a source pin: the assertion is about what reaches a screen.
const renderMinified = async (source, options) => {
  const { code } = await minify(source, options)
  const mod = await import('data:text/javascript;base64,' + Buffer.from(code, 'utf8').toString('base64'))
  return { code, markup: renderToStaticMarkup(React.createElement(() => mod.Popover(React))) }
}

// Text nodes only — an attribute value that happens to contain a zero is not a stray `0` on screen.
const textNodes = markup => markup.split(/<[^>]*>/g).map(part => part.trim()).filter(Boolean)

test('the shipped minifier settings leave a closed popover rendering nothing at all', async () => {
  const options = await terserOptionsFromViteConfig()
  const { markup } = await renderMinified(POPOVER, options)

  assert.ok(!textNodes(markup).includes('0'),
    `a closed popover rendered a bare 0 — the minifier turned its \`false\` into \`0\`: ${markup}`)
  assert.equal(markup, '<div class="th-switch"><button type="button" aria-expanded="false">Theme</button></div>')
})

test('a collapsed trigger reports a real ARIA boolean, not the number 0', async () => {
  const options = await terserOptionsFromViteConfig()
  const { markup } = await renderMinified(POPOVER, options)

  // `aria-expanded="0"` is not a valid ARIA boolean. The menus were not merely painting a stray
  // character; they were reporting their collapsed state in a vocabulary no assistive tech reads.
  assert.match(markup, /aria-expanded="false"/)
  assert.ok(!/aria-expanded="[01]"/.test(markup), markup)
})

test('CONTROL: with booleans_as_integers back on, both defects return — so the two tests above can see them', async () => {
  const options = await terserOptionsFromViteConfig()
  const forced = { ...options, compress: { ...options.compress, booleans_as_integers: true } }
  const { code, markup } = await renderMinified(POPOVER, forced)

  assert.match(code, /useState\(0\)/, 'the control arm must actually reproduce the rewrite')
  assert.ok(textNodes(markup).includes('0'), `the control arm must paint the bare 0: ${markup}`)
  assert.match(markup, /aria-expanded="0"/)
})

// The two arms above cover the minifier the config names today. This one covers the emitted bytes,
// whichever minifier produced them — the build runs Rolldown/Oxc before Terser, and a future option
// on either one would land the same way.
test('the emitted-chunk check accepts a build that kept its booleans and refuses one that did not', () => {
  const kept = new Map([['assets/a.js', 'let o=!1;o&&x();'], ['assets/b.css', '.a{color:red}']])
  assert.deepEqual(findIntegerBooleanChunks(kept), [])

  const rewritten = new Map([['assets/a.js', 'let o=0;o&&x();'], ['assets/b.css', '.a{color:red}']])
  const violations = findIntegerBooleanChunks(rewritten)
  assert.equal(violations.length, 1)
  assert.equal(violations[0].code, 'integer_booleans')
  assert.match(violations[0].message, /assets\/a\.js/)

  // CSS is never scanned: a stylesheet has no boolean literals and must not be reported as one.
  assert.deepEqual(findIntegerBooleanChunks(new Map([['assets/only.css', '.a{color:red}']])), [])
  // The bare WORD `false` is not accepted as evidence — it survives the rewrite inside strings, and
  // taking it would have falsely rescued 23 of the 34 chunks of the build that actually shipped the
  // defect. This chunk was rewritten and must still be caught despite carrying the word.
  assert.equal(findIntegerBooleanChunks(
    new Map([['assets/a.js', 'let o=0;o&&x(\'aria-hidden\',`true`);']])).length, 1)
  // `!0` inside an identifier or a longer numeric literal is not a boolean and must not rescue it.
  assert.equal(findIntegerBooleanChunks(new Map([['assets/a.js', 'let o=0;if(a!==!05)x();']])).length, 1)
})
