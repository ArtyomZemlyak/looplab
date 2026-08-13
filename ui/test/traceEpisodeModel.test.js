// THE EPISODE MAP's truth table — how a bounded trace window is POINTED, as opposed to grown.
//
// Every rule here is one the mounted control depends on and cannot state: what a failed map means
// (never "no history"), which rows may be offered (only ones that can actually be sought to), how a
// map that stopped short must number its rows, and where the window currently is.
import assert from 'node:assert/strict'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { createServer } from 'vite'

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
  episodeOrdinal, episodePosition, episodeSummary,
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
  // And a map of nothing BUT unanchored rows is empty, not ready-with-no-choices.
  assert.equal(buildEpisodeMap(payload([episode({ anchor: null })])).status, 'empty')
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
  assert.equal(episodeDurationLabel(episode({ seconds: 7200 })), '2.0h')
  assert.equal(episodeDurationLabel(episode({ seconds: null })), '')
  assert.equal(episodeDurationLabel(episode({ seconds: 'soon' })), '')
  const full = episodeSummary(episode({ ordinal: 17, reason: 'crash' }), 0)
  assert.equal(full, '#17 · 12s · 27 spans · 13 gen · crash')
  // Absent facts stay OUT — a summary reading "· 0 gen ·" invents a measurement.
  const bare = episodeSummary(
    { anchor: 'a', label: 'train', seconds: null, spans: 0, generations: 0 }, 0)
  assert.equal(bare, '#1')
})
