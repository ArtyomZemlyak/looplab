// THE EPISODE MAP's truth table — how a bounded trace window is POINTED, as opposed to grown.
//
// Every rule here is one the mounted control depends on and cannot state: what a failed map means
// (never "no history"), which rows may be offered (only ones that can actually be sought to), how a
// map that stopped short must number its rows, and where the window currently is.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer, parseAst } from 'vite'

// A depth-first walk over an ESTree tree. Small on purpose: the alternative is a substring pin, and
// `tests/_source_scan.py`'s rule holds in this language too — a comment is not an AST node.
const walk = (node, visit) => {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) { for (const child of node) walk(child, visit); return }
  if (typeof node.type === 'string') visit(node)
  for (const key of Object.keys(node)) {
    if (key !== 'type' && key !== 'loc') walk(node[key], visit)
  }
}

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const vite = await createServer({
  root: UI_ROOT, configFile: false, appType: 'custom', logLevel: 'silent',
  server: { middlewareMode: true },
})
const model = await vite.ssrLoadModule('/src/traceEpisodeModel.js')
test.after(() => vite.close())

const {
  EPISODE_MAP_EMPTY, EPISODE_MAP_UNAVAILABLE, buildEpisodeMap, clampEpisodeIndex, episodeAnchor,
  episodeAt, episodeDurationLabel, episodeKindOptions, episodeLabel, episodeMapNotice,
  episodeOrdinal, episodePosition, episodeSummary, mergeEpisodePagePayload,
} = model

const episode = (over = {}) => ({
  band: 'b', anchor: 'a', trace_id: 't', label: 'inline_repair', start: 1, seconds: 12,
  status: 'OK', spans: 27, ordinal: null, reason: null, generations: 13, tools: 14, ...over,
})

const payload = (episodes, projection = {}) => ({
  schema: 2, node_id: '1', episodes, projection: { omitted_episodes: 0, ...projection },
})

test('a map that could not be read is never an absent history', () => {
  // The whole point of the four-way status. An empty picker on a failed read tells the operator
  // their repaired node has no earlier steps, which is the exact lie every other trace receipt in
  // this codebase is written to avoid.
  for (const bad of [null, undefined, 'nope', payload([], { unavailable: true })]) {
    assert.equal(buildEpisodeMap(bad).status, 'unavailable', JSON.stringify(bad))
  }
  assert.notEqual(EPISODE_MAP_UNAVAILABLE, EPISODE_MAP_EMPTY)
  // A node whose whole trace fits in one window read FINE and has nowhere to seek to. Distinct.
  assert.equal(buildEpisodeMap(payload([])).status, 'empty')
})

test('exclusive episode pages prepend into one reachable history', () => {
  const pagePayload = (episodes, over = {}) => ({
    schema: 2, node_id: '1', attempt: 0, run_generation: 'generation-a', episodes,
    projection: { total_episodes: 6, visible_episodes: episodes.length,
      omitted_episodes: 6 - episodes.length, truncated: true },
    ...over,
  })
  const newest = pagePayload([
    episode({ anchor: 'r5', ordinal: 5 }), episode({ anchor: 'r6', ordinal: 6 }),
  ], { page: { snapshot: 'r6', before: null, next_before: 'r5', has_older: true,
    older_episodes: 4, newer_episodes: 0 } })
  const middle = pagePayload([
    episode({ anchor: 'r3', ordinal: 3 }), episode({ anchor: 'r4', ordinal: 4 }),
  ], { page: { snapshot: 'r6', before: 'r5', next_before: 'r3', has_older: true,
    older_episodes: 2, newer_episodes: 2 } })
  const oldest = pagePayload([
    episode({ anchor: 'r1', ordinal: 1 }), episode({ anchor: 'r2', ordinal: 2 }),
  ], { page: { snapshot: 'r6', before: 'r3', next_before: null, has_older: false,
    older_episodes: 0, newer_episodes: 4 } })

  const four = mergeEpisodePagePayload(newest, middle)
  assert.deepEqual(four.episodes.map(row => row.anchor), ['r3', 'r4', 'r5', 'r6'])
  assert.equal(four.projection.omitted_episodes, 2)
  assert.equal(four.page.next_before, 'r3')
  assert.equal(buildEpisodeMap(four).hasOlder, true)
  assert.match(episodeMapNotice(buildEpisodeMap(four)), /most recent 4 of 6/)

  const all = mergeEpisodePagePayload(four, oldest)
  assert.deepEqual(all.episodes.map(row => row.anchor), ['r1', 'r2', 'r3', 'r4', 'r5', 'r6'])
  assert.equal(all.projection.visible_episodes, 6)
  assert.equal(all.projection.omitted_episodes, 0)
  assert.equal(all.projection.truncated, false)
  assert.equal(buildEpisodeMap(all).hasOlder, false)
  assert.equal(episodeMapNotice(buildEpisodeMap(all)), '')

  // Cursor and lifecycle are part of the representation. A late or foreign page is rejected rather
  // than merged into a plausible-looking list whose seek anchors point somewhere else.
  assert.equal(mergeEpisodePagePayload(newest, { ...middle, node_id: '2' }), null)
  assert.equal(mergeEpisodePagePayload(newest, { ...middle, attempt: 1 }), null)
  assert.equal(mergeEpisodePagePayload(newest, { ...middle, run_generation: 'generation-b' }), null)
  assert.equal(mergeEpisodePagePayload(newest,
    { ...middle, page: { ...middle.page, before: 'wrong' } }), null)
  assert.equal(mergeEpisodePagePayload(newest,
    { ...middle, page: { ...middle.page, snapshot: 'another-tail' } }), null)
  assert.equal(mergeEpisodePagePayload(newest,
    { ...middle, projection: { ...middle.projection, total_episodes: 7 } }), null)
})

test('only an episode that can be sought to is offered', () => {
  // The anchor IS the seek. A row without one is a choice that cannot be made, so it is dropped
  // rather than rendered as a disabled option — a dead control is what this whole change removes.
  const map = buildEpisodeMap(payload([
    episode({ anchor: 'r1', ordinal: 1 }),
    episode({ anchor: null, ordinal: 2 }),
    episode({ anchor: '', ordinal: 3 }),
    episode({ anchor: 'r4', ordinal: 4 }),
  ]))
  assert.equal(map.status, 'ready')
  assert.equal(map.total, 2)
  assert.deepEqual(map.kinds[0].episodes.map(e => e.anchor), ['r1', 'r4'])
  assert.equal(map.dropped, 2, 'the rows this fold refused are counted, not discarded')
  // And a map of nothing BUT unanchored rows is UNSEEKABLE, not empty and not ready-with-no-choices:
  // the node has those steps, this fold simply cannot point at any of them.
  const none = buildEpisodeMap(payload([episode({ anchor: null })]))
  assert.equal(none.status, 'unseekable')
  assert.equal(none.dropped, 1)
  assert.equal(episodeMapNotice(none), 'This experiment has 1 earlier step and it cannot be jumped to.')
})

test('kinds read in the order the node lived them, with their counts', () => {
  // Alphabetical would put `implement` before `propose` and `train` last — an ordering that says
  // something false about how the experiment went. First occurrence is the honest one.
  const map = buildEpisodeMap(payload([
    episode({ label: 'propose', anchor: 'p' }),
    episode({ label: 'implement', anchor: 'i' }),
    episode({ label: 'train', anchor: 't1' }),
    episode({ label: 'inline_repair', anchor: 'r1' }),
    episode({ label: 'train', anchor: 't2' }),
    episode({ label: 'inline_repair', anchor: 'r2' }),
  ]))
  assert.deepEqual(episodeKindOptions(map), [
    { label: 'propose', name: 'propose', count: 1 },
    { label: 'implement', name: 'implement', count: 1 },
    { label: 'train', name: 'train', count: 2 },
    { label: 'inline_repair', name: 'repair', count: 2 },
  ])
  // A rename, never an invention: an unrecorded label keeps its own spelling verbatim.
  assert.equal(episodeLabel('inline_repair'), 'repair')
  assert.equal(episodeLabel('some_new_phase'), 'some_new_phase')
  assert.equal(episodeLabel(null), 'step')
})

test('a typed position can never issue a request that does not exist', () => {
  const map = buildEpisodeMap(payload([
    episode({ anchor: 'r1' }), episode({ anchor: 'r2' }), episode({ anchor: 'r3' }),
  ]))
  assert.equal(clampEpisodeIndex(map, 'inline_repair', -5), 0)
  assert.equal(clampEpisodeIndex(map, 'inline_repair', 99), 2)
  assert.equal(clampEpisodeIndex(map, 'inline_repair', 1.5), 2)   // a non-integer is not a position
  assert.equal(clampEpisodeIndex(map, 'nope', 0), 0)
  assert.equal(episodeAt(map, 'inline_repair', 99).anchor, 'r3')
  assert.equal(episodeAt(map, 'nope', 0), null)
  // `null` from `episodeAnchor` is a real answer — "read the newest" — and not a failure.
  assert.equal(episodeAnchor(null), null)
  assert.equal(episodeAnchor(episode({ anchor: 'r1' })), 'r1')
})

test('where the window is, from the anchor on screen', () => {
  const map = buildEpisodeMap(payload([
    episode({ label: 'train', anchor: 't1' }),
    episode({ anchor: 'r1', ordinal: 1 }),
    episode({ anchor: 'r2', ordinal: 2 }),
  ]))
  const here = episodePosition(map, 'r2')
  assert.equal(here.label, 'inline_repair')
  assert.equal(here.name, 'repair')
  assert.equal(here.index, 1)
  assert.equal(here.ordinal, 2)          // 1-based where the operator reads it
  assert.equal(here.count, 2)
  // No anchor is the TAIL, which is a position the control must not caption as an episode.
  assert.equal(episodePosition(map, null), null)
  assert.equal(episodePosition(map, ''), null)
  // An anchor this map does not contain (a stale seek, a map re-read after a repair landed) reports
  // nothing rather than the nearest row — inventing a position is how a caption starts lying.
  assert.equal(episodePosition(map, 'r9'), null)
})

test('a bounded map keeps the engine’s own numbering and says it stopped short', () => {
  // The map has a ceiling, and its tail is the NEWEST rows. If it renumbered from 1, its oldest row
  // would read as the node's first repair — a map asserting a beginning it does not have.
  const map = buildEpisodeMap(payload(
    [episode({ anchor: 'r1', ordinal: 2001 }), episode({ anchor: 'r2', ordinal: 2002 })],
    { omitted_episodes: 2000 }))
  assert.equal(map.partial, true)
  assert.equal(map.omitted, 2000)
  assert.equal(episodeOrdinal(map.kinds[0].episodes[0], 0), 2001)
  assert.match(episodeMapNotice(map), /most recent 2 of 2002/)
  // With no stamped ordinal the position stands in, and an unbounded map says nothing at all.
  assert.equal(episodeOrdinal(episode({ ordinal: null }), 3), 4)
  assert.equal(episodeOrdinal(episode({ ordinal: 0 }), 0), 1)   // 0 is not a repair number
  assert.equal(episodeMapNotice(buildEpisodeMap(payload([episode()]))), '')
})

test('an episode summarises only what it recorded', () => {
  assert.equal(episodeDurationLabel(episode({ seconds: 12 })), '12s')
  assert.equal(episodeDurationLabel(episode({ seconds: 600 })), '10m')
  // `2h`, not the `2.0h` this module used to print on its own: the tiering is now
  // `format.js::durationLabel`, shared with the live-status age strip an operator reads on the same
  // screen. See test/durationLabel.test.js.
  assert.equal(episodeDurationLabel(episode({ seconds: 7200 })), '2h')
  assert.equal(episodeDurationLabel(episode({ seconds: null })), '')
  assert.equal(episodeDurationLabel(episode({ seconds: 'soon' })), '')
  const full = episodeSummary(episode({ ordinal: 17, reason: 'crash' }), 0)
  assert.equal(full, '#17 · 12s · 27 spans · 13 gen · crash')
  // Absent facts stay OUT — a summary reading "· 0 gen ·" invents a measurement.
  const bare = episodeSummary(
    { anchor: 'a', label: 'train', seconds: null, spans: 0, generations: 0 }, 0)
  assert.equal(bare, '#1')
})

test('a map that can point at nothing never claims the trace fits in one window', () => {
  // TWO FACTS, ONE STATUS — the defect this split fixes. `empty` used to be reached both by a node
  // with genuinely no earlier steps and by a payload that REPORTED earlier steps the map could not
  // offer, and `Inspector.jsx::TraceEpisodes` prints EPISODE_MAP_EMPTY for that status. So a node
  // whose own `/episodes` answer said "900 more" was told its whole trace fits in one window, with
  // the count it was carrying (`omitted`) rendered nowhere: the notice was only reachable from the
  // `ready` branch. That is the "a partial read must never read as an absent history" rule this
  // vocabulary exists for, arriving through the one branch with no way to say what it knew.
  const genuinelyEmpty = buildEpisodeMap(payload([]))
  assert.equal(genuinelyEmpty.status, 'empty')
  assert.equal(genuinelyEmpty.partial, false)
  assert.equal(episodeMapNotice(genuinelyEmpty), '', 'nothing omitted, nothing to say')

  // The server's map stopped short and every row it did return was unanchored.
  const bounded = buildEpisodeMap(payload([episode({ anchor: null })], { omitted_episodes: 900 }))
  assert.equal(bounded.status, 'unseekable')
  assert.notEqual(bounded.status, 'empty', 'the sentence "fits in one window" must be unreachable')
  assert.equal(bounded.partial, true)
  assert.equal(bounded.omitted, 900)
  assert.equal(bounded.dropped, 1)
  const notice = episodeMapNotice(bounded)
  assert.match(notice, /901 earlier steps/, 'omitted and dropped are both steps out of reach')
  assert.match(notice, /none of them can be jumped to/)
  assert.doesNotMatch(notice, /most recent 0/, 'a zero-count caption reads as a bug, not a fact')

  // Omitted alone, with nothing returned at all, is the same status for the same reason.
  const withheld = buildEpisodeMap(payload([], { omitted_episodes: 12 }))
  assert.equal(withheld.status, 'unseekable')
  assert.match(episodeMapNotice(withheld), /12 earlier steps/)

  // A READY map that also dropped rows says so: `total` is what can be sought to, and the two
  // unreachable populations are named apart because only one of them has a remedy.
  const partial = buildEpisodeMap(payload(
    [episode({ anchor: 'r1' }), episode({ anchor: null }), episode({ anchor: null })],
    { omitted_episodes: 5 }))
  assert.equal(partial.status, 'ready')
  assert.equal(partial.total, 1)
  assert.equal(partial.dropped, 2)
  assert.match(episodeMapNotice(partial), /most recent 1 of 8 steps\. 2 cannot be jumped to\./)
})

test('the control renders four map outcomes, not three', async () => {
  // The model's four outcomes are only worth having if the surface renders four: `TraceEpisodes`
  // needed the branch, and without it the new status falls through every condition and the operator
  // gets an open picker with nothing in it — quieter than the lie, still not the count.
  //
  // Read as AST over the COMPILED module rather than as substrings of the source: a commented-out
  // branch is not an AST node, and esbuild rewrites `'unseekable'` to `"unseekable"`, so a text pin
  // here would be quote-sensitive on top of being satisfiable by a comment.
  const compiled = await vite.transformRequest('/src/Inspector.jsx', { ssr: true })
  const statuses = new Set()
  walk(parseAst(compiled.code), node => {
    if (node.type !== 'BinaryExpression' || node.operator !== '===') return
    if (node.right?.type !== 'Literal' || typeof node.right.value !== 'string') return
    // `map?.status === '<x>'` — the optional chain is a ChainExpression around the member read.
    const left = node.left?.type === 'ChainExpression' ? node.left.expression : node.left
    if (left?.type === 'MemberExpression' && left.property?.name === 'status'
        && left.object?.name === 'map') statuses.add(node.right.value)
  })
  for (const status of ['unavailable', 'empty', 'unseekable', 'ready']) {
    assert.ok(statuses.has(status), `TraceEpisodes must render the ${status} map`)
  }
})
