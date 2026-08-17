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

// The ONE tiered duration rendering (`format.js` imports nothing, so a pure model may reach it).
import { durationLabel } from './format.js'

// The kinds a node's own trace records, in the words the operator uses. Only a RENAME lives here: a
// label with no row keeps its recorded spelling verbatim, because inventing a friendly name for a
// band nobody has seen is how a map starts describing something the trace does not contain.
const EPISODE_LABELS = Object.freeze({
  inline_repair: 'repair',
  card_build: 'build',
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
const pageState = payload => {
  const page = payload?.page
  const snapshot = typeof page?.snapshot === 'string' && page.snapshot ? page.snapshot : null
  const nextBefore = typeof page?.next_before === 'string' && page.next_before
    ? page.next_before : null
  return {
    snapshot,
    nextBefore,
    hasOlder: page?.has_older === true && nextBefore !== null,
    older: safeCount(page?.older_episodes),
    newer: safeCount(page?.newer_episodes),
  }
}

const episodeIdentity = row => {
  if (anchored(row)) return `anchor:${row.anchor}`
  if (typeof row?.band === 'string' && row.band) return `band:${row.band}`
  return null
}

const episodeTotal = payload => {
  const rows = Array.isArray(payload?.episodes) ? payload.episodes.length : 0
  return Math.max(rows + safeCount(payload?.projection?.omitted_episodes),
    safeCount(payload?.projection?.total_episodes))
}

/**
 * Prepend one exclusive-cursor page to an aggregate that started at the newest tail.
 *
 * Returns null on any identity/cursor disagreement. A late response from another lifecycle is not
 * an older page of the map on screen, and quietly merging it would create seek controls whose labels
 * and anchors describe different traces. Rows are de-duplicated defensively even though the server's
 * exclusive cursor already guarantees no overlap.
 */
export const mergeEpisodePagePayload = (current, older) => {
  if (!current || typeof current !== 'object' || !older || typeof older !== 'object') return null
  if (String(current.node_id) !== String(older.node_id)
      || current.schema !== older.schema
      || current.attempt !== older.attempt
      || current.run_generation !== older.run_generation
      || current.projection?.unavailable === true
      || older.projection?.unavailable === true) return null
  const cursor = current.page?.next_before
  const snapshot = current.page?.snapshot
  if (typeof cursor !== 'string' || !cursor || older.page?.before !== cursor
      || typeof snapshot !== 'string' || !snapshot || older.page?.snapshot !== snapshot
      || episodeTotal(current) !== episodeTotal(older)) return null

  const currentRows = Array.isArray(current.episodes) ? current.episodes : []
  const olderRows = Array.isArray(older.episodes) ? older.episodes : []
  const seen = new Set()
  const episodes = []
  for (const row of [...olderRows, ...currentRows]) {
    const identity = episodeIdentity(row)
    if (identity && seen.has(identity)) continue
    if (identity) seen.add(identity)
    episodes.push(row)
  }
  const total = Math.max(episodes.length, episodeTotal(current))
  const omitted = Math.max(0, total - episodes.length)
  const nextBefore = typeof older.page?.next_before === 'string' && older.page.next_before
    ? older.page.next_before : null
  const hasOlder = older.page?.has_older === true && nextBefore !== null
  const projection = {
    ...(current.projection || {}),
    total_episodes: total,
    visible_episodes: episodes.length,
    omitted_episodes: omitted,
    truncated: omitted > 0 || safeCount(current.projection?.omitted_spans) > 0
      || safeCount(current.projection?.truncated_spans) > 0,
  }
  return {
    ...current,
    episodes,
    page: {
      ...(current.page || {}),
      next_before: nextBefore,
      has_older: hasOlder,
      older_episodes: safeCount(older.page?.older_episodes),
      // The aggregate still ends at the same newest edge as its first page. This is normally zero,
      // but carrying it makes the helper honest for a caller that deliberately began mid-history.
      newer_episodes: safeCount(current.page?.newer_episodes),
    },
    projection,
  }
}

/**
 * Fold one `/nodes/{n}/episodes` payload into what a picker can offer.
 *
 * FOUR outcomes, and they are deliberately four rather than "a list that may be empty":
 *   unavailable — the map could not be read. NEVER the same as "this node has no episodes": a
 *                 failed read that renders as an empty picker tells the operator their node has no
 *                 history, which is the lie the whole projection vocabulary exists to prevent.
 *   empty       — read fine, and there is genuinely nothing to seek to: the node's whole trace fits
 *                 in one window. NOTHING was omitted and nothing was dropped — that is what makes
 *                 the sentence this status prints ("fits in one window") true.
 *   unseekable  — read fine, the node HAS earlier steps, and not one of them can be pointed at:
 *                 either the server's own map stopped short (`omitted_episodes > 0`) or every row it
 *                 returned lacked an anchor. Split out of `empty` on 2026-08-15, because the two
 *                 were one status carrying `omitted` forward while forcing `total: 0` — so a node
 *                 whose payload said "there are 900 more episodes" was told its whole trace fits in
 *                 one window. That is exactly the "a partial read must never read as an absent
 *                 history" lie the other three outcomes were written to prevent, arriving through
 *                 the one branch that had no way to say what it knew.
 *   ready       — kinds to choose from.
 * `partial` rides alongside all four: one server page has a ceiling, and an aggregate that has not
 * loaded its older cursor pages yet must say so rather than let its oldest row read as the node's
 * beginning.
 *
 * An episode with no usable `anchor` is dropped, not disabled: the anchor IS the seek, so a row
 * without one is a choice that cannot be made, and a picker offering it would be a dead control.
 * `dropped` is that count, carried rather than discarded for the same reason `omitted` is — the two
 * are different facts (one the server withheld, one this fold refused) and both are steps the
 * operator cannot reach, so `episodeMapNotice` states them together and never as an absence.
 */
export const buildEpisodeMap = payload => {
  if (!payload || typeof payload !== 'object' || payload.projection?.unavailable === true) {
    return { status: 'unavailable', kinds: [], total: 0, omitted: 0, dropped: 0, partial: true,
      snapshot: null, nextBefore: null, hasOlder: false, older: 0, newer: 0 }
  }
  const rows = Array.isArray(payload.episodes) ? payload.episodes : []
  const omitted = safeCount(payload.projection?.omitted_episodes)
  const page = pageState(payload)
  let dropped = 0
  const kinds = []
  const byLabel = new Map()
  for (const row of rows) {
    if (!anchored(row)) { dropped += 1; continue }
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
  // Read fine, nothing to OFFER — and the two reasons for that are not one fact. A node whose whole
  // trace fits in one window has no earlier steps at all; a map that stopped short, or whose every
  // row lacked an anchor, has them and cannot point at them. The operator's move differs too: the
  // first is finished reading, the second is looking at a control that will not take them where the
  // steps are. Neither is a failed read, which is the distinction the `unavailable` branch keeps.
  if (!kinds.length) {
    const status = omitted > 0 || dropped > 0 ? 'unseekable' : 'empty'
    return { status, kinds: [], total: 0, omitted, dropped, partial: omitted > 0 || dropped > 0,
      ...page }
  }
  return {
    status: 'ready',
    kinds,
    total: kinds.reduce((sum, kind) => sum + kind.episodes.length, 0),
    omitted,
    dropped,
    partial: omitted > 0 || dropped > 0,
    ...page,
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

// How long an episode took, in the run's ONE duration rendering (`format.js::durationLabel`, shared
// with the live-status age and the standing-watch strip). This copy printed the top tier as `3.4h`
// against the status strip's `3h 25m` for the same interval — and those two surfaces are read side
// by side, the picker being inside the run screen the strip captions. `durationLabel` keeps this
// module's own "absent is not zero" rule: only a real number is a duration, so a missing `seconds`
// stays out rather than rendering as `0s`.
export const episodeDurationLabel = episode => durationLabel(episode?.seconds)

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
//
// TOTAL over every status, because it is the ONLY thing the `unseekable` branch has to say and the
// branch exists precisely because that branch used to say nothing. Two shapes, and the split is on
// whether anything can be reached at all rather than on the status name: with rows to offer it
// captions them, and with none it states the size of what is out of reach — "the most recent 0 of
// 900" is arithmetically true and reads as a bug, which is not the sentence to hand somebody who
// has just been told their trace fits in one window.
export const episodeMapNotice = map => {
  const total = safeCount(map?.total)
  const omitted = safeCount(map?.omitted)
  const dropped = safeCount(map?.dropped)
  if (!omitted && !dropped) return ''
  const known = total + omitted + dropped
  // `dropped` is named separately wherever it is non-zero: an omitted episode can be reached by
  // asking the server for more, one this fold refused for a missing anchor cannot be reached at all,
  // and rolling them into one number would promise a remedy for half of them.
  const unreachable = dropped ? ` ${dropped} cannot be jumped to.` : ''
  if (!total) {
    return known === 1
      ? 'This experiment has 1 earlier step and it cannot be jumped to.'
      : `This experiment has ${known} earlier steps and none of them can be jumped to.`
  }
  return `Showing the most recent ${total} of ${known} steps.${unreachable}`
}

// What the control says when the map could not be read. A failed map is not an absent history, and
// the remedy is to try again — never "this node has no earlier steps".
export const EPISODE_MAP_UNAVAILABLE = 'Could not read this experiment’s steps.'
// And the one status that may claim there is nothing earlier to see. It is now reachable ONLY when
// the payload proves it: no omitted episodes, no dropped rows. See `buildEpisodeMap`.
export const EPISODE_MAP_EMPTY = 'This experiment’s whole trace fits in one window.'
