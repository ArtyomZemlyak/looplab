import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'

// The event-feed narration MODEL (src/narration.js) used to be ~300 lines of data inside the Dock
// component, split across two parallel registries keyed by the same event types (NARR + NARR_VALID)
// plus GROUPS/TYPE2GROUP/GROUP_GLYPH/TYPE_GLYPH. Nothing forced the halves to agree, and the file's
// own comments record past drift. These tests hold the merged shape to its promises: one entry per
// type, both halves reachable, and the kind tables covering exactly the narrated types. doc 25 UI-08.

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const source = name => readFile(new URL(`../src/${name}`, import.meta.url), 'utf8')

const withNarration = async (body) => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try { await body(vite, await vite.ssrLoadModule('/src/narration.js')) } finally { await vite.close() }
}

const GENERIC = type => `${type} — details could not be summarized`

test('one narration entry per event type carries both halves', async () => {
  await withNarration(async (vite, { NARR, eventNarration }) => {
    const types = Object.keys(NARR)
    assert.ok(types.length > 90, `expected the full narration table, saw ${types.length} entries`)
    for (const type of types) {
      const entry = NARR[type]
      assert.equal(typeof entry, 'object', `${type} must be a {validate?, render} entry`)
      assert.equal(typeof entry.render, 'function', `${type} must carry a callable render`)
      if (entry.validate !== undefined) {
        assert.equal(typeof entry.validate, 'function', `${type}: validate must be callable`)
      }
      assert.deepEqual(Object.keys(entry).sort().filter(k => k !== 'render' && k !== 'validate'), [],
        `${type} entry carries an unknown key — the entry shape is {validate?, render}`)
    }
    // WHY every entry must carry a render, driven rather than asserted: a validator whose type has no
    // renderer is DEAD — eventNarration consults `validate` only once a `render` exists. That is
    // exactly the drift the two parallel registries permitted, and the merged shape only makes it
    // visible; this test is what makes it impossible.
    NARR.zz_shape_probe = { validate: () => false }
    try {
      assert.equal(eventNarration({ type: 'zz_shape_probe', data: { a: 1 } }), '{"a":1}',
        'a validator with no renderer is silently ignored — hence every entry needs a render')
    } finally { delete NARR.zz_shape_probe }
  })
})

test('every declared validator is consulted before its renderer interpolates a claim', async () => {
  await withNarration(async (vite, { NARR, eventNarration }) => {
    const validated = Object.keys(NARR).filter(type => NARR[type].validate)
    assert.ok(validated.length > 60, `expected the validated majority, saw ${validated.length}`)
    for (const type of validated) {
      // The anti-"#undefined" rule itself: a field that DEFINES the claim can never be satisfied by an
      // empty payload, so `{}` is a rejection every validator must agree on. This is what keeps the
      // fallback assertion below from going vacuous if a future validator turns permissive.
      assert.equal(NARR[type].validate({}), false,
        `${type}: an empty payload must not satisfy a claim-defining guard`)
      assert.equal(eventNarration({ type, data: {} }), GENERIC(type),
        `${type}: a payload its own validator rejects must degrade to the generic line`)
    }
    // Positive control: a complete payload still renders. Without this the assertion above would also
    // pass if eventNarration had simply stopped rendering anything at all.
    assert.equal(eventNarration({ type: 'node_evaluated', data: { node_id: 3, metric: 0.5 } }),
      'node #3 → 0.5')
    assert.equal(eventNarration({ type: 'card_dropped', data: { id: 'card-1', reason: 'stale' } }),
      'Card card-1 dropped — stale')
  })
})

test('no renderer is reached with a payload that is not a plain object', async () => {
  await withNarration(async (vite, { NARR, eventNarration }) => {
    for (const type of Object.keys(NARR)) {
      for (const data of [null, undefined, 'text', 7, true, []]) {
        assert.equal(eventNarration({ type, data }), GENERIC(type),
          `${type}: a non-object payload must not reach the renderer`)
      }
    }
  })
})

test('the kind tables cover exactly the narrated types', async () => {
  await withNarration(async (vite, { NARR, GROUPS, TYPE2GROUP, GROUP_GLYPH, TYPE_GLYPH, kindOf }) => {
    const narrated = new Set(Object.keys(NARR))
    const grouped = Object.keys(TYPE2GROUP)
    // Both directions. A narrated type with no group silently files under 'lifecycle' (wrong chip,
    // wrong icon, and the filter chip that should show it never does); a grouped type with no
    // narration is a chip promise the allow-listed feed can never keep.
    assert.deepEqual(grouped.filter(type => !narrated.has(type)), [],
      'a kind group names an event type that has no narration entry')
    assert.deepEqual([...narrated].filter(type => !Object.hasOwn(TYPE2GROUP, type)), [],
      'a narrated event type has no kind group')
    assert.equal(grouped.length, narrated.size)
    // A type may not be claimed by two groups — Object.fromEntries would silently keep the last.
    const declared = GROUPS.flatMap(([, , types]) => types.split(' '))
    assert.equal(declared.length, new Set(declared).size, 'an event type is claimed by two kind groups')
    for (const [group, label] of GROUPS) {
      assert.ok(GROUP_GLYPH[group], `group ${group} (${label}) has no glyph`)
    }
    for (const type of Object.keys(TYPE_GLYPH)) {
      assert.ok(narrated.has(type), `per-type glyph override ${type} is not a narrated type`)
    }
    // Forward-compat + prototype-key hostility: an unknown type files under lifecycle/flag, and a type
    // spelled like an Object.prototype key must never resolve to an inherited function.
    assert.deepEqual(kindOf('future_event'), { group: 'lifecycle', glyph: 'flag' })
    for (const hostile of ['constructor', 'toString', 'valueOf', '__proto__']) {
      const { group, glyph } = kindOf(hostile)
      assert.equal(typeof group, 'string')
      assert.equal(typeof glyph, 'string')
      assert.deepEqual({ group, glyph }, { group: 'lifecycle', glyph: 'flag' })
    }
  })
})

test('the curated allow-list is the narration table itself', async () => {
  await withNarration(async (vite, { NARR, isCuratedType }) => {
    for (const type of Object.keys(NARR)) assert.equal(isCuratedType(type), true, type)
    for (const type of ['node_concepts', 'llm_usage', 'finalize_step', 'finalization_finished',
      'concept_edge', 'future_event']) {
      assert.equal(isCuratedType(type), false, `${type} must stay out of the curated feed`)
    }
    for (const hostile of ['constructor', 'toString', 'valueOf', 'hasOwnProperty']) {
      assert.equal(isCuratedType(hostile), false, `${hostile} must not resolve through the prototype`)
    }
  })
})

test('Dock consumes the narration model instead of owning it', async () => {
  const vite = await createServer({
    root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    await vite.ssrLoadModule('/src/Dock.jsx')
    // The real resolved import edge, read from vite's module graph — a commented-out import creates
    // no edge, so this cannot be satisfied by text.
    const graph = vite.environments?.ssr?.moduleGraph ?? vite.moduleGraph
    const [dockModule] = [...graph.getModulesByFile(fileURLToPath(new URL('../src/Dock.jsx', import.meta.url)))]
    const imported = [...(dockModule.importedModules ?? dockModule.ssrImportedModules)].map(m => m.url)
    assert.ok(imported.includes('/src/narration.js'),
      `Dock must import the narration model; its imports are ${imported.join(', ')}`)
  } finally { await vite.close() }
  const dock = await source('Dock.jsx')
  // NEGATIVE pins (substrings on purpose, per the repo's guard-test rule): what must not come back is
  // the TEXT of a second copy of the model inside the component — a commented-out copy drifts just as
  // fast as a live one.
  for (const gone of ['const NARR', 'NARR_VALID', 'const GROUPS', 'const TYPE2GROUP',
    'const GROUP_GLYPH', 'const TYPE_GLYPH', 'function kindOf', 'const isCuratedType',
    'export function eventNarration']) {
    assert.ok(!dock.includes(gone), `Dock.jsx must not re-declare ${gone}`)
  }
  const narration = await source('narration.js')
  // The two parallel registries are what this change removed; a reintroduced NARR_VALID is the exact
  // regression, whether it lands back in Dock or beside the merged table.
  assert.ok(!narration.includes('NARR_VALID'), 'the parallel validator registry must not come back')
})
