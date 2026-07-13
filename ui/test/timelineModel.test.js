import test from 'node:test'
import assert from 'node:assert/strict'

import {
  createTimelineState, mergeTimelinePage, normalizeTimelinePage, noticeTimelineFrontier,
  parkTimeline, setTimelineFollowing, timelineContainsSeq, timelinePagePath, timelineRequestFailed,
  timelineBehindLive, timelineConflictMatchesGeneration, timelineRequestStarted, TIMELINE_RETAIN_LIMIT,
  TIMELINE_CATCHUP_DELAYS,
} from '../src/timelineModel.js'

const generation = char => char.repeat(64)
const page = (events, options = {}) => ({
  events,
  generation: options.generation ?? generation('a'),
  cursors: { older: options.older ?? null, newer: options.newer ?? null },
  has_more: { older: options.hasOlder ?? false, newer: options.hasNewer ?? false },
  torn_tail: options.tornTail ?? false,
  total_events: options.totalEvents,
})
const events = (from, to) => Array.from({ length: to - from + 1 }, (_, offset) => ({
  seq: from + offset, type: 'tick', data: { value: from + offset },
}))

test('catch-up visibility retries use bounded exponential backoff', () => {
  assert.deepEqual([...TIMELINE_CATCHUP_DELAYS], [100, 200, 400])
})

test('same-generation stale cursor conflicts recover in place', () => {
  const shown = generation('a')
  assert.equal(timelineConflictMatchesGeneration({ status: 409, detail: { actual_generation: shown } }, shown), true)
  assert.equal(timelineConflictMatchesGeneration({ status: 409, detail: { actual_generation: generation('b') } }, shown), false)
})

test('timeline page normalization supports nested and transitional cursor spellings', () => {
  assert.deepEqual(normalizeTimelinePage(page(events(2, 3), { older: 'o', newer: 'n', hasOlder: true })), {
    rows: events(2, 3), generation: generation('a'), cursors: { older: 'o', newer: 'n' },
    hasMore: { older: true, newer: false }, tornTail: false, sourceTailLimited: false,
    totalEvents: null, range: null, anchorSeq: null, matchedSeq: null,
  })
  const transitional = normalizeTimelinePage({ rows: [], generation: null, older_cursor: 'left',
    newer_cursor: 'right', has_older: true, has_newer: true, tornTail: true })
  assert.deepEqual(transitional.cursors, { older: 'left', newer: 'right' })
  assert.deepEqual(transitional.hasMore, { older: true, newer: true })
  assert.equal(transitional.tornTail, true)
})

test('tail, older, and newer pages dedupe by seq and retain chronological order', () => {
  let state = createTimelineState({ retainLimit: 100 })
  state = mergeTimelinePage(state, page(events(10, 12), { older: 'before', hasOlder: true }), { direction: 'tail' })
  assert.deepEqual(state.rows.map(row => row.seq), [10, 11, 12])
  state = mergeTimelinePage(state, page(events(7, 10), { newer: 'after', hasNewer: true }), { direction: 'older' })
  assert.deepEqual(state.rows.map(row => row.seq), [7, 8, 9, 10, 11, 12])
  assert.equal(state.cursors.newer, null, 'an older page cannot move the retained live frontier')
  assert.equal(state.hasMore.newer, false, 'the retained tail remains known-live after a prepend')
  state = mergeTimelinePage(state, page(events(12, 15)), { direction: 'newer' })
  assert.deepEqual(state.rows.map(row => row.seq), [7, 8, 9, 10, 11, 12, 13, 14, 15])
  assert.equal(state.windowAtTail, true)
})

test('directional merges preserve the opposite cursor until retention evicts that frontier', () => {
  let state = createTimelineState({ retainLimit: 100 })
  state = mergeTimelinePage(state, page(events(10, 12), { older: 'tail-old', newer: 'tail-new', hasOlder: true }), { direction: 'tail' })
  state = mergeTimelinePage(state, page(events(7, 9), { older: 'older-old', newer: 'older-new', hasOlder: true, hasNewer: true }), { direction: 'older' })
  assert.deepEqual(state.cursors, { older: 'older-old', newer: 'tail-new' })
  assert.deepEqual(state.hasMore, { older: true, newer: false })
  state = mergeTimelinePage(state, page(events(13, 14), { older: 'newer-old', newer: 'newer-new', hasOlder: true }), { direction: 'newer' })
  assert.deepEqual(state.cursors, { older: 'older-old', newer: 'newer-new' })
  assert.deepEqual(state.hasMore, { older: true, newer: false })
})

test('a changed generation replaces rather than mixes rows', () => {
  let state = mergeTimelinePage(createTimelineState(), page(events(40, 42)), { direction: 'tail' })
  state = parkTimeline(state, 42, 3)
  state = mergeTimelinePage(state, page(events(0, 1), { generation: generation('b') }), { direction: 'newer' })
  assert.deepEqual(state.rows, [])
  assert.equal(state.generation, null)
  assert.equal(state.status, 'loading')
  assert.equal(state.generationChanged, true)
  assert.equal(state.parkedEventCount, null)
  assert.equal(state.liveEventCount, null)
})

test('retention evicts whole page segments and keeps cursors aligned to retained boundaries', () => {
  let state = createTimelineState({ retainLimit: 600 })
  state = mergeTimelinePage(state, page(events(600, 799), { older: 'o600', newer: 'n799', hasOlder: true }), { direction: 'tail' })
  state = mergeTimelinePage(state, page(events(400, 599), { older: 'o400', newer: 'n599', hasOlder: true, hasNewer: true }), { direction: 'older' })
  state = mergeTimelinePage(state, page(events(200, 399), { older: 'o200', newer: 'n399', hasOlder: true, hasNewer: true }), { direction: 'older' })
  state = mergeTimelinePage(state, page(events(0, 199), { older: null, newer: 'n199', hasNewer: true }), { direction: 'older' })
  assert.deepEqual([state.rows[0].seq, state.rows.at(-1).seq], [0, 599])
  assert.deepEqual(state.cursors, { older: null, newer: 'n599' })
  assert.equal(state.segments.length, 3)
  state = mergeTimelinePage(state, page(events(600, 799), { older: 'o600', newer: 'n799', hasOlder: true }), { direction: 'newer' })
  assert.deepEqual([state.rows[0].seq, state.rows.at(-1).seq], [200, 799])
  assert.deepEqual(state.cursors, { older: 'o200', newer: 'n799' })
})

test('history around replaces the retained window and URL binds the generation fence', () => {
  let state = mergeTimelinePage(createTimelineState(), page(events(90, 100)), { direction: 'tail' })
  state = mergeTimelinePage(state, page(events(40, 60), { older: 'left', newer: 'right', hasOlder: true, hasNewer: true }), { direction: 'around' })
  assert.deepEqual(state.rows.map(row => row.seq), events(40, 60).map(row => row.seq))
  assert.equal(timelineContainsSeq(state, 51), true)
  assert.equal(timelineContainsSeq(state, 99), false)
  const path = timelinePagePath('a/b', { direction: 'around', anchorSeq: 51, generation: generation('a'), limit: 400 })
  assert.match(path, /^\/api\/runs\/a%2Fb\/log-page\?/)
  assert.match(path, /direction=around/)
  assert.match(path, /anchor_seq=51/)
  assert.match(path, /generation=a{64}/)
})

test('around remembers the server-matched anchor until a replacing tail clears it', () => {
  const around = { ...page(events(50, 60), { hasOlder: true, hasNewer: true }),
    anchor_seq: 55, matched_seq: 54, total_events: 1_000 }
  let state = mergeTimelinePage(createTimelineState(), around, { direction: 'around' })
  assert.equal(state.requestedAnchor, 55)
  assert.equal(state.matchedSeq, 54)
  assert.equal(state.totalEvents, 1_000)
  state = mergeTimelinePage(state, page(events(900, 999)), { direction: 'tail' })
  assert.equal(state.requestedAnchor, null)
  assert.equal(state.matchedSeq, null)
})

test('followingTail is distinct from atLive and unread is cleared only on explicit follow', () => {
  let state = mergeTimelinePage(createTimelineState(), page(events(1, 3)), { direction: 'tail' })
  assert.equal(state.windowAtTail, true)
  state = parkTimeline(state, 3)
  assert.equal(state.windowAtTail, true)
  assert.equal(state.followingTail, false)
  state = mergeTimelinePage(state, page(events(3, 5)), { direction: 'newer' })
  assert.equal(state.unread, 2)
  state = noticeTimelineFrontier(state, 9)
  assert.equal(state.unread, 2)
  assert.equal(state.unreadUnknown, true)
  state = mergeTimelinePage(state, page(events(5, 9)), { direction: 'newer' })
  assert.equal(state.unread, 6, 'observing the same arrivals must not double-count them')
  state = setTimelineFollowing(state, true)
  assert.equal(state.unread, 0)
})

test('a historical around window counts only arrivals since parking', () => {
  let state = mergeTimelinePage(createTimelineState(), page(events(9_900, 10_000)), { direction: 'tail' })
  state = parkTimeline(state, 10_000)
  state = mergeTimelinePage(state, page(events(90, 110), { hasOlder: true, hasNewer: true }), { direction: 'around' })
  state = noticeTimelineFrontier(state, 10_003)
  assert.equal(state.unread, 0)
  assert.equal(state.unreadUnknown, true)
})

test('total_events turns parked activity into an exact count even when seq has gaps', () => {
  const initial = { ...page([{ seq: 0, type: 'tick', data: {} }]), total_events: 1 }
  let state = mergeTimelinePage(createTimelineState(), initial, { direction: 'tail' })
  state = parkTimeline(state, 0)
  state = noticeTimelineFrontier(state, 10)
  assert.equal(state.unreadUnknown, true)
  const delta = { ...page([{ seq: 10, type: 'tick', data: {} }]), total_events: 2 }
  state = mergeTimelinePage(state, delta, { direction: 'newer' })
  assert.equal(state.unread, 1)
  assert.equal(state.unreadUnknown, false)
})

test('folded live event_count makes parked unread exact across seq gaps', () => {
  let state = mergeTimelinePage(createTimelineState(), page(events(10, 11), { totalEvents: 2 }), { direction: 'tail' })
  state = parkTimeline(state, 11, 2)
  state = noticeTimelineFrontier(state, 90, 5)
  assert.equal(state.unread, 3)
  assert.equal(state.unreadUnknown, false)

  state = mergeTimelinePage(state, page(events(12, 14), { totalEvents: 5 }), { direction: 'newer' })
  assert.equal(state.unread, 3, 'page dedupe cannot replace the generation-scoped parked delta')
  assert.equal(state.unreadUnknown, false)
})

test('live lag prefers event_count and falls back to seq for a legacy backend', () => {
  const state = mergeTimelinePage(createTimelineState(), page(events(10, 11), { totalEvents: 2 }), { direction: 'tail' })
  assert.equal(timelineBehindLive(state, 99, 2), false, 'seq gaps are not unread events')
  assert.equal(timelineBehindLive(state, 11, 3), true, 'a same-seq count anomaly still wakes catch-up')
  assert.equal(timelineBehindLive(state, 12, null), true, 'legacy payloads retain the seq fallback')
})

test('a pager-stopped nonmonotonic tail stays an unknown-activity fallback', () => {
  let state = mergeTimelinePage(createTimelineState(), page(events(10, 11), {
    totalEvents: 2, tornTail: true,
  }), { direction: 'tail' })
  state = parkTimeline(state, 11, 2)
  state = noticeTimelineFrontier(state, 11, 3)
  assert.equal(state.unread, 0)
  assert.equal(state.unreadUnknown, true)
})

test('same-seq or decreasing count growth is unknown before the pager reveals a torn tail', () => {
  for (const liveSeq of [11, 10]) {
    let state = mergeTimelinePage(createTimelineState(), page(events(10, 11), {
      totalEvents: 2, tornTail: false,
    }), { direction: 'tail' })
    state = parkTimeline(state, 11, 2)
    state = noticeTimelineFrontier(state, liveSeq, 3)
    assert.equal(state.unread, 0)
    assert.equal(state.unreadUnknown, true)
    assert.equal(state.tornTail, true)
    state = noticeTimelineFrontier(state, 12, 4)
    assert.equal(state.unreadUnknown, true, 'the inferred noncanonical boundary remains sticky until a page verifies it')
  }
})

test('malformed generations and unstable event keys fail closed', () => {
  assert.throws(() => normalizeTimelinePage({ events: [], generation: 'short' }), /64 lowercase hex/)
  assert.throws(() => normalizeTimelinePage({ events: [{ type: 'missing-seq' }], generation: generation('a') }), /non-negative integer seq/)
  assert.throws(() => normalizeTimelinePage({ events: [{ seq: 2 }, { seq: 2 }], generation: generation('a') }), /strictly increasing/)
})

test('request errors are directional and keep already loaded rows visible', () => {
  let state = mergeTimelinePage(createTimelineState(), page(events(1, 2)), { direction: 'tail' })
  state = timelineRequestStarted(state, 'older')
  state = timelineRequestFailed(state, 'older', new Error('offline'))
  assert.equal(state.status, 'ready')
  assert.equal(state.errors.older, 'offline')
  assert.equal(state.rows.length, 2)
  state = mergeTimelinePage(state, page(events(10, 11)), { direction: 'tail' })
  assert.deepEqual(state.errors, { tail: null, older: null, newer: null, around: null })
})

test('serialized request handoff clears the aborted direction loading flag', () => {
  let state = timelineRequestStarted(createTimelineState(), 'newer')
  assert.equal(state.loading.newer, true)
  state = timelineRequestStarted(state, 'older')
  assert.deepEqual(state.loading, { tail: false, older: true, newer: false, around: false })
})

test('generated 50k-row page is bounded and remains a small chronological tail window', () => {
  const huge = events(0, 49_999)
  const started = performance.now()
  const state = mergeTimelinePage(createTimelineState(), page(huge), { direction: 'tail' })
  const elapsed = performance.now() - started
  assert.equal(state.rows.length, TIMELINE_RETAIN_LIMIT)
  assert.equal(state.rows[0].seq, 45_000)
  assert.equal(state.rows.at(-1).seq, 49_999)
  // A generous guard catches accidental quadratic merging without making normal CI timing brittle.
  assert.ok(elapsed < 5_000, `50k merge took ${Math.round(elapsed)}ms`)
})

test('a one-event live append to a full 5k segmented window stays within the live-update budget', () => {
  let state = createTimelineState()
  for (let start = 0; start < 5_000; start += 500) {
    state = mergeTimelinePage(state, page(events(start, start + 499), {
      older: start === 0 ? null : `o${start}`,
      newer: `n${start + 499}`,
      hasOlder: start > 0,
      hasNewer: start < 4_500,
    }), { direction: start === 0 ? 'tail' : 'newer' })
  }
  assert.equal(state.rows.length, 5_000)
  const started = performance.now()
  state = mergeTimelinePage(state, page(events(5_000, 5_000), {
    older: 'o5000', newer: 'n5000', hasOlder: true,
  }), { direction: 'newer' })
  const elapsed = performance.now() - started
  assert.equal(state.rows.at(-1).seq, 5_000)
  assert.ok(state.rows.length <= 5_000)
  assert.ok(elapsed < 32, `single-event merge took ${elapsed.toFixed(1)}ms`)
})
