// WHERE TO POINT A BOUNDED TRACE WINDOW — the pure half of the node Trace tab's episode control
// (`Inspector.jsx::TraceEpisodes`), beside `traceScrollModel.js`, which owns how that window GROWS.
// No React, no I/O: `node --test` drives every rule here directly.
//
// WHY this exists. A node's trace surfaces read a bounded window over the TAIL of one
// `(node_id, generation)`. Widening it is the only control there has ever been, and widening cannot
// reach the beginning of a long node — it is the same tail, and its ceiling is real (the server pays
// 3.4 ms per span, on the request thread). Measured 2026-08-13 on `runs/rubert-dr-0804` node 1:
// 14,507 spans across 2,345 inline repairs over 3 h 50 m, of which the 512-span default window shows
// the last 7.6 minutes and the 4,096-span ceiling the last 59.3. The operator's report — "you cannot
// see traces from earlier versions of a node, bugs happened and repair kicked in" — is exactly that:
// 74 % of the node, including every early repair where the bug first showed, was unreachable.
//
// The ATTEMPT picker beside this one does NOT cover it, and that is the trap worth writing down: an
// attempt is a lifecycle GENERATION, bumped only by `node_reset`. Inline repair does not bump it. So
// all 14,507 of that node's spans are attempt 0, `nodeAttemptOptions` returns one option, and the
// picker does not render at all on the very node the complaint was about. The two controls answer
// different questions and neither substitutes for the other:
//   attempt  — WHICH lifecycle of this node (what a reset abandoned)
//   episode  — WHERE inside one lifecycle (what a repair loop did, 2,345 times)
//
// So the server offers a MAP (`/nodes/{n}/episodes`, every band with no band's contents) and an
// ANCHOR (`?before=<span_id>`, the same window ending at a chosen step instead of at the newest
// one). This module turns that map into the choices a picker offers and the position it reports.

// The kinds a node's own trace records, in the words the operator uses. Only a RENAME lives here: a
// label with no row keeps its recorded spelling verbatim, because inventing a friendly name for a
// band nobody has seen is how a map starts describing something the trace does not contain.
const EPISODE_LABELS = Object.freeze({
  inline_repair: 'repair',
  salvage_cause_repair: 'salvage repair',
  create_node: 'author node',
  handoff_summary: 'handoff',
  'handoff-summary': 'handoff',
  foresight_rank: 'foresight',
})

export const episodeLabel = label => (label == null || label === ''
  ? 'step'
  : (EPISODE_LABELS[label] || String(label)))

const safeCount = value => (Number.isSafeInteger(value) && value >= 0 ? value : 0)
const anchored = episode => typeof episode?.anchor === 'string' && episode.anchor !== ''

/**
 * Fold one `/nodes/{n}/episodes` payload into what a picker can offer.
 *
 * Four outcomes, and they are deliberately four rather than "a list that may be empty":
 *   unavailable — the map could not be read. NEVER the same as "this node has no episodes": a
 *                 failed read that renders as an empty picker tells the operator their node has no
 *                 history, which is the lie the whole projection vocabulary exists to prevent.
 *   empty       — read fine, nothing to seek to (a node whose whole trace fits in one window).
 *   ready       — kinds to choose from.
 * `partial` rides alongside: the server's map has its own ceiling, and a map that stops short must
 * say so rather than let its oldest row read as the node's beginning.
 *
 * An episode with no usable `anchor` is dropped, not disabled: the anchor IS the seek, so a row
 * without one is a choice that cannot be made, and a picker offering it would be a dead control.
 */
export const buildEpisodeMap = payload => {
  if (!payload || typeof payload !== 'object' || payload.projection?.unavailable === true) {
    return { status: 'unavailable', kinds: [], total: 0, omitted: 0, partial: true }
  }
  const rows = Array.isArray(payload.episodes) ? payload.episodes : []
  const omitted = safeCount(payload.projection?.omitted_episodes)
  const kinds = []
  const byLabel = new Map()
  for (const row of rows) {
    if (!anchored(row)) continue
    const label = row.label == null ? '' : String(row.label)
    let kind = byLabel.get(label)
    if (!kind) {
      // First occurrence orders the kinds, so they read in the order the node lived them
      // (propose → implement → train → triage → repair) rather than alphabetically.
      kind = { label, name: episodeLabel(label), episodes: [] }
      byLabel.set(label, kind)
      kinds.push(kind)
    }
    kind.episodes.push(row)
  }
  // Read fine, nothing to seek to. Reached both by a node whose whole trace fits in one window and
  // by a map whose every row lacked an anchor — the operator's move is the same in both, and neither
  // is a failed read, which is the distinction the `unavailable` branch above exists to keep.
  if (!kinds.length) {
    return { status: 'empty', kinds: [], total: 0, omitted, partial: omitted > 0 }
  }
  return {
    status: 'ready',
    kinds,
    total: kinds.reduce((sum, kind) => sum + kind.episodes.length, 0),
    omitted,
    partial: omitted > 0,
  }
}

/** The kind rows a `<select>` renders: its recorded label, its operator-facing name, its count. */
export const episodeKindOptions = map => (map?.kinds || []).map(kind => ({
  label: kind.label,
  name: kind.name,
  count: kind.episodes.length,
}))

const kindOf = (map, label) => (map?.kinds || []).find(kind => kind.label === label) || null

/**
 * Clamp a requested position to one that exists.
 *
 * A picker's number field is typed into, and a trace surface may not answer a typo with an empty
 * panel or an out-of-range request the server then refuses. `index` is 0-based internally and 1-based
 * where the operator sees it (`ordinal` below), because "repair 1" is the first repair.
 */
export const clampEpisodeIndex = (map, label, index) => {
  const kind = kindOf(map, label)
  if (!kind || !kind.episodes.length) return 0
  if (!Number.isSafeInteger(index)) return kind.episodes.length - 1
  return Math.max(0, Math.min(index, kind.episodes.length - 1))
}

/** The episode at a position, or null. */
export const episodeAt = (map, label, index) => {
  const kind = kindOf(map, label)
  if (!kind || !kind.episodes.length) return null
  return kind.episodes[clampEpisodeIndex(map, label, index)] || null
}

/**
 * The `?before=` value for a selection — or `null`, which means "no anchor: read the newest".
 *
 * Null is a real answer and not a failure: it is what "back to the latest steps" sends, and it is
 * what an unresolvable selection must fall back to rather than sending an anchor nobody chose.
 */
export const episodeAnchor = episode => (anchored(episode) ? episode.anchor : null)

/**
 * WHERE THE WINDOW IS, from the anchor the server echoed — never from what was last clicked.
 *
 * The distinction is the same one `traceProjection.js::traceForAttempt` makes for a historical
 * attempt: the picker's state says what was ASKED for, and only the response says what is on
 * screen. Reporting the request would caption the newest window with the episode a refused or
 * still-in-flight seek named.
 */
export const episodePosition = (map, anchor) => {
  if (anchor == null || anchor === '') return null
  for (const kind of map?.kinds || []) {
    const index = kind.episodes.findIndex(episode => episode.anchor === anchor)
    if (index >= 0) {
      return { label: kind.label, name: kind.name, index, ordinal: index + 1,
        count: kind.episodes.length, episode: kind.episodes[index] }
    }
  }
  return null
}

/**
 * How one episode reads in a list: its number within its kind, when it ran, and how long it took.
 *
 * `ordinal` is the engine's OWN inline-repair counter when the span carries one (`attempt` on a
 * triage/inline_repair span, 1..2,345 on the measured node) and the position in the map otherwise.
 * Preferring the recorded number matters: a map that stopped at its ceiling would otherwise renumber
 * the node's repairs from 1 and quietly assert that the oldest row it holds is the first one.
 */
export const episodeOrdinal = (episode, index) => {
  const stamped = episode?.ordinal
  if (Number.isSafeInteger(stamped) && stamped > 0) return stamped
  return Number.isSafeInteger(index) && index >= 0 ? index + 1 : null
}

const seconds = value => (typeof value === 'number' && Number.isFinite(value) && value >= 0
  ? value : null)

export const episodeDurationLabel = episode => {
  const value = seconds(episode?.seconds)
  if (value == null) return ''
  if (value < 90) return `${Math.round(value)}s`
  if (value < 5400) return `${Math.round(value / 60)}m`
  return `${(value / 3600).toFixed(1)}h`
}

/** The one-line summary a chosen episode shows beside the picker. Never invented: absent stays out. */
export const episodeSummary = (episode, index) => {
  const parts = []
  const ordinal = episodeOrdinal(episode, index)
  if (ordinal != null) parts.push(`#${ordinal}`)
  const duration = episodeDurationLabel(episode)
  if (duration) parts.push(duration)
  const spans = episode?.spans
  if (Number.isSafeInteger(spans) && spans > 0) parts.push(`${spans} spans`)
  const generations = episode?.generations
  if (Number.isSafeInteger(generations) && generations > 0) {
    parts.push(`${generations} gen`)
  }
  if (typeof episode?.reason === 'string' && episode.reason) parts.push(episode.reason)
  return parts.join(' · ')
}

// The notice the control prints when the map itself is bounded. Stated as a count, never as an
// adjective — the same rule `traceWindowNotice` follows, and for the same reason: an operator whose
// map stops short needs to know it stops short, or they will read its oldest row as the beginning.
export const episodeMapNotice = map => (map?.omitted > 0
  ? `Showing the most recent ${map.total} of ${map.total + map.omitted} steps.`
  : '')

// What the control says when the map could not be read. A failed map is not an absent history, and
// the remedy is to try again — never "this node has no earlier steps".
export const EPISODE_MAP_UNAVAILABLE = 'Could not read this experiment’s steps.'
export const EPISODE_MAP_EMPTY = 'This experiment’s whole trace fits in one window.'
