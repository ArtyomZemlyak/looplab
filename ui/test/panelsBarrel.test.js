// panels.jsx is a BARREL: ConfigPanel and the Card board live in their own modules (doc 25 UI-04)
// and are re-exported from the hub. Two things silently break that arrangement, and neither shows up
// as a red test anywhere else:
//
//   1. A re-export line is dropped or misspelled while the panel moves. RunView reaches every panel
//      through `lazyNamed(loadPanels, '<Name>')` — a STRING, not an import binding — so a missing
//      export is not a build error. `module[name]` is `undefined`, React.lazy resolves
//      `{ default: undefined }`, and the failure surfaces as a render-time crash the first time an
//      operator opens that panel.
//   2. Some module OUTSIDE the declared importer set reaches ./ConfigPanel.jsx or ./CardBoard.jsx
//      directly. That gives Rolldown an unbudgeted reachability root for panel code, which is what
//      RunView's single `loadPanels` promise exists to prevent: measured after the split,
//      `src/panels.jsx` is still the only manifest entry for ConfigPanel and the panel-hub increment
//      is one 11-chunk closure. CardBoard is the one deliberate exception — see EXTRACTED below.
//
// So the expected export list is DERIVED from the real call sites rather than hand-written here, and
// the check is behavioural: load the barrel and look at what it actually gives back.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const SRC = new URL('../src/', import.meta.url)
const TEST = new URL('./', import.meta.url)

// The panel modules the hub re-exports, each mapped to the COMPLETE set of modules allowed to
// import it. Adding another split module means adding a row here.
//
// `CardBoard.jsx` has two importers on purpose since the board became the fourth workspace view:
// `panels.jsx` keeps the legacy `HypothesisBoard` re-export, and `RunView.jsx` lazy-loads
// `CardWorkspace` as a view like Dag/Report/Concepts. That is a second importer, not a second copy
// — re-measured on the shipped build: `grep -rl 'card-kanban-card card-lane-card' dist/assets/*.js`
// matches exactly ONE of the 35 chunks, because Rollup hoists a module both entry points reach.
// The chunk-SIZE half of this concern is owned by `scripts/check-bundle.mjs` and its closure
// budgets, which measure the real build; this test owns only the importer set.
const EXTRACTED = { 'ConfigPanel.jsx': ['panels.jsx'], 'CardBoard.jsx': ['RunView.jsx', 'panels.jsx'] }

const readDirSource = async base => {
  const names = (await readdir(base)).filter(name => /\.(js|jsx)$/.test(name))
  const entries = await Promise.all(
    names.map(async name => [name, await readFile(new URL(name, base), 'utf8')]))
  return new Map(entries)
}

const matchAll = (text, re) => [...text.matchAll(re)].map(match => match[1])

test('every name a consumer takes from the panel hub is really exported by it', async () => {
  const [src, tests] = await Promise.all([readDirSource(SRC), readDirSource(TEST)])
  const wanted = new Set()
  const sources = new Set()

  // Production: RunView names each lazy panel as a string literal.
  for (const [file, text] of src) {
    for (const name of matchAll(text, /lazyNamed\(loadPanels,\s*'([A-Za-z_$][\w$]*)'\)/g)) {
      if (name !== 'default') wanted.add(name)
      sources.add(file)
    }
    // Any future static/dynamic named import of the hub counts too.
    for (const clause of matchAll(text, /import\s*\{([^}]*)\}\s*from\s*'\.\/panels\.jsx'/g)) {
      for (const part of clause.split(',')) {
        const name = part.trim().split(/\s+as\s+/)[0].trim()
        if (name) { wanted.add(name); sources.add(file) }
      }
    }
  }

  // Tests: the hub is loaded through vite and destructured, or held as a namespace and read.
  for (const [file, text] of tests) {
    if (file === 'panelsBarrel.test.js' || !text.includes("ssrLoadModule('/src/panels.jsx')")) continue
    for (const clause of matchAll(text, /\{([^{}]*)\}\s*=\s*await\s+vite\.ssrLoadModule\('\/src\/panels\.jsx'\)/g)) {
      for (const part of clause.split(',')) {
        const name = part.trim().split(':')[0].trim()
        if (name) { wanted.add(name); sources.add(file) }
      }
    }
    for (const name of matchAll(text, /\bpanels\.([A-Z][\w$]*)/g)) {
      wanted.add(name)
      sources.add(file)
    }
  }

  // Anti-vacuity: a derivation that silently stops finding call sites would make every assertion
  // below pass over an empty set. ConfigPanel and HypothesisBoard are the two names this split moved,
  // so they are the ones a dropped re-export would take out; they must come from real call sites in
  // more than one file (RunView's lazyNamed table and a test's ssrLoadModule).
  assert.ok(sources.size >= 2, `the scan found consumers in only ${sources.size} file(s)`)
  assert.ok(wanted.size >= 20, `the scan derived only ${wanted.size} hub exports`)
  for (const canary of ['ConfigPanel', 'HypothesisBoard']) {
    assert.ok(wanted.has(canary), `${canary} is no longer derived from any call site`)
  }

  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const hub = await vite.ssrLoadModule('/src/panels.jsx')
    const missing = [...wanted].filter(name => hub[name] === undefined).sort()
    assert.deepEqual(missing, [],
      `panels.jsx no longer exports ${missing.join(', ')} — a consumer resolves undefined`)
    for (const name of wanted) {
      assert.equal(typeof hub[name], 'function', `${name} must be callable through the barrel`)
    }
  } finally {
    await vite.close()
  }
})

test('only the hub imports its extracted panel modules, so they stay in one lazy chunk', async () => {
  const src = await readDirSource(SRC)
  for (const [extracted, allowed] of Object.entries(EXTRACTED)) {
    const specifier = `./${extracted}`
    const importers = [...src]
      .filter(([, text]) => text.includes(`'${specifier}'`))
      .map(([file]) => file)
      .sort()
    assert.deepEqual(importers, [...allowed].sort(),
      `${extracted} must be reachable only through ${allowed.join(' / ')}; an unlisted importer `
      + 'splits the panel hub into more than one lazy chunk')
  }
})

test('the extracted panel modules never import back from the barrel', async () => {
  const src = await readDirSource(SRC)
  // A shared helper goes DOWN into panelPrimitives.js, never back up into panels.jsx. Importing the
  // barrel from a module the barrel imports is a cycle, and under native ESM ordering a cycle is a
  // load-time TDZ crash rather than a build error.
  for (const leaf of [...Object.keys(EXTRACTED), 'panelPrimitives.js']) {
    const text = src.get(leaf)
    assert.ok(text, `${leaf} is missing`)
    assert.doesNotMatch(text, /from\s*'\.\/panels\.jsx'/, `${leaf} imports the barrel it is part of`)
  }
  assert.doesNotMatch(src.get('panelPrimitives.js'), /from\s*'\.\/(ConfigPanel|CardBoard)\.jsx'/,
    'the shared leaf must not depend on the panels that share it')
})

test('the shared panel primitives are defined once, not re-derived per module', async () => {
  const src = await readDirSource(SRC)
  const primitives = src.get('panelPrimitives.js')
  for (const name of ['invalidPanelPayload', 'isRecord', 'nullableText', 'nullableNumber',
                      'PANEL_REQUEST_TIMEOUT_MS', 'RUN_GENERATION_RE']) {
    assert.match(primitives, new RegExp(`^export const ${name}\\b`, 'm'),
      `panelPrimitives.js must own ${name}`)
    // Negative pins stay substrings on purpose: what must not come back is a second DEFINITION, and
    // splitting a module is exactly the moment someone reaches for a local copy instead of an import.
    for (const consumer of ['panels.jsx', ...Object.keys(EXTRACTED)]) {
      assert.doesNotMatch(src.get(consumer), new RegExp(`^const ${name}\\b`, 'm'),
        `${consumer} re-derives ${name} instead of importing the shared one`)
    }
  }
})
