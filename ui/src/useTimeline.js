import { useCallback, useEffect, useRef, useState } from 'react'
import { get } from './api.js'
import {
  createTimelineState, mergeTimelinePage, noticeTimelineFrontier, parkTimeline, setTimelineFollowing,
  timelineBehindLive,
  timelineConflictMatchesGeneration, timelineContainsSeq, timelinePagePath, timelineRequestFailed, timelineRequestStarted,
  TIMELINE_CATCHUP_DELAYS, TIMELINE_PAGE_SIZE, TIMELINE_RETAIN_LIMIT,
} from './timelineModel.js'

const generationChanged = error => error?.status === 409 && error?.code === 'run_generation_changed'

// One controller owns the cursor state for both Dock and EventExplorer. Requests are serialized so
// an older-page reply cannot race a newer/tail reply and publish cursors for a window that no longer
// exists. A reset is recovered by an unfenced tail read; a cross-generation delta is never merged.
export function useTimeline(runId, {
  liveSeq = null,
  liveEventCount = null,
  expectedGeneration = null,
  enabled = true,
  pageSize = TIMELINE_PAGE_SIZE,
  retainLimit = TIMELINE_RETAIN_LIMIT,
} = {}) {
  const fresh = useCallback(() => createTimelineState({ retainLimit }), [retainLimit])
  const [state, setState] = useState(fresh)
  const stateRef = useRef(state)
  const requestRef = useRef({ token: 0, controller: null, generation: null })
  const latestLiveSeqRef = useRef(liveSeq)
  const latestLiveEventCountRef = useRef(liveEventCount)
  const expectedGenerationRef = useRef(expectedGeneration)
  const runRef = useRef(runId)
  const loadRef = useRef(null)
  const lastAnchorRef = useRef(null)
  const catchupTimerRef = useRef(null)
  const catchupResolveRef = useRef(null)
  const mountedRef = useRef(true)
  expectedGenerationRef.current = expectedGeneration

  const commit = useCallback(next => {
    stateRef.current = next
    setState(next)
    return next
  }, [])

  const cancelCatchup = useCallback(() => {
    clearTimeout(catchupTimerRef.current)
    catchupTimerRef.current = null
    const resolve = catchupResolveRef.current
    catchupResolveRef.current = null
    resolve?.(false)
  }, [])
  const waitCatchup = useCallback(delay => new Promise(resolve => {
    cancelCatchup()
    catchupResolveRef.current = resolve
    catchupTimerRef.current = setTimeout(() => {
      catchupTimerRef.current = null
      catchupResolveRef.current = null
      resolve(true)
    }, delay)
  }), [cancelCatchup])

  const load = useCallback(async (direction = 'tail', {
    anchorSeq = null, unfenced = false, catchupAttempt = 0,
  } = {}) => {
    if (!mountedRef.current || !enabled || !runId) return null
    cancelCatchup()
    const snapshot = stateRef.current
    const cursor = direction === 'older' || direction === 'newer' ? snapshot.cursors[direction] : null
    if ((direction === 'older' || direction === 'newer') && !cursor) return null
    requestRef.current.controller?.abort()
    const controller = new AbortController()
    const token = requestRef.current.token + 1
    const generation = unfenced ? null : (snapshot.generation || expectedGenerationRef.current || null)
    requestRef.current = { token, controller, generation }
    commit(timelineRequestStarted(snapshot, direction))
    try {
      const payload = await get(timelinePagePath(runId, {
        direction, cursor, generation, anchorSeq, limit: pageSize,
      }), { signal: controller.signal, cache: 'no-store' })
      if (requestRef.current.token !== token || runRef.current !== runId) return null
      const latestExpected = expectedGenerationRef.current
      if (latestExpected && payload?.generation && payload.generation !== latestExpected) {
        const reset = { ...fresh(), status: 'loading', generationChanged: true,
          followingTail: stateRef.current.followingTail }
        commit(reset)
        queueMicrotask(() => loadRef.current?.('tail'))
        return reset
      }
      const merged = mergeTimelinePage(stateRef.current, payload, { direction })
      const observesLiveEdge = (direction === 'tail' || direction === 'newer') && merged.followingTail
      const lagging = observesLiveEdge && timelineBehindLive(
        merged, latestLiveSeqRef.current, latestLiveEventCountRef.current)
      // has_more can be false for a page snapshot taken just before the state/SSE snapshot. Publish
      // the authoritative lag immediately so headers cannot transiently claim that the view is live.
      const observed = lagging && merged.windowAtTail ? { ...merged, windowAtTail: false } : merged
      commit(observed)
      if (observed.generationChanged) queueMicrotask(() => loadRef.current?.('tail', {
        unfenced: !expectedGenerationRef.current,
      }))
      else if (observesLiveEdge) {
        if (observed.hasMore.newer || (lagging && catchupAttempt < 3)) {
          const nextOptions = { catchupAttempt: observed.hasMore.newer ? 0 : catchupAttempt + 1 }
          const delay = observed.hasMore.newer ? 0 : TIMELINE_CATCHUP_DELAYS[catchupAttempt]
          catchupTimerRef.current = setTimeout(() => {
            if (!stateRef.current.followingTail) return
            if (observed.cursors.newer) loadRef.current?.('newer', nextOptions)
            else loadRef.current?.('tail', nextOptions)
          }, delay)
        } else if (lagging) {
          const parked = parkTimeline(observed, latestLiveSeqRef.current, latestLiveEventCountRef.current)
          commit({ ...parked, unreadUnknown: true,
            errors: { ...parked.errors, newer: 'Live events are still behind the displayed run state.' } })
        }
      }
      return observed
    } catch (error) {
      if (error?.name === 'AbortError' || requestRef.current.token !== token || runRef.current !== runId) return null
      if (generationChanged(error)) {
        const displayedGeneration = expectedGenerationRef.current
        const sameGenerationBoundary = timelineConflictMatchesGeneration(error, displayedGeneration)
        const reset = { ...fresh(), status: displayedGeneration && !sameGenerationBoundary ? 'error' : 'loading', generationChanged: true,
          followingTail: stateRef.current.followingTail }
        if (displayedGeneration && !sameGenerationBoundary) reset.errors = { ...reset.errors, [direction]:
          'The run was replaced; waiting for the displayed generation to refresh.' }
        commit(reset)
        // Never alternate stale displayed generation A with an unfenced discovery of B. Hold the
        // empty fenced view until SSE publishes B; only discover when no displayed generation exists.
        if (sameGenerationBoundary) queueMicrotask(() => loadRef.current?.('tail'))
        else if (!displayedGeneration) queueMicrotask(() => loadRef.current?.('tail', { unfenced: true }))
        return reset
      }
      commit(timelineRequestFailed(stateRef.current, direction, error))
      return null
    }
  }, [cancelCatchup, commit, enabled, fresh, pageSize, runId])
  loadRef.current = load

  useEffect(() => {
    mountedRef.current = true
    runRef.current = runId
    requestRef.current.controller?.abort()
    cancelCatchup()
    requestRef.current = { token: requestRef.current.token + 1, controller: null, generation: null }
    const reset = fresh()
    commit(reset)
    if (enabled && runId) loadRef.current?.('tail', { unfenced: !expectedGenerationRef.current })
    return () => {
      mountedRef.current = false
      requestRef.current.controller?.abort(); cancelCatchup()
      requestRef.current = { token: requestRef.current.token + 1, controller: null, generation: null }
    }
    // expectedGeneration has its own fence effect below; including it here would double-fetch and
    // let one generation-change microtask abort the other.
  }, [runId, enabled, fresh, commit, cancelCatchup])

  // The displayed SSE generation is an observation fence. When it changes, discard every opaque
  // cursor from the old file and request the new tail against the exact displayed generation.
  useEffect(() => {
    if (!enabled || !expectedGeneration || expectedGeneration === stateRef.current.generation) return
    if (!stateRef.current.generation && requestRef.current.generation === expectedGeneration) return
    // If an unfenced startup read is still in flight, abort it. Otherwise its old-generation payload
    // could publish after the SSE generation became known and the effect would not run again.
    requestRef.current.controller?.abort()
    cancelCatchup()
    const reset = { ...fresh(), status: 'loading', generationChanged: true }
    commit(reset)
    loadRef.current?.('tail')
  }, [enabled, expectedGeneration, fresh, commit, cancelCatchup])

  // liveSeq is only a wake-up signal. The opaque newer cursor remains authoritative, including at
  // the live edge where has_more.newer=false but cursors.newer can poll future appends.
  useEffect(() => {
    latestLiveSeqRef.current = liveSeq
    latestLiveEventCountRef.current = liveEventCount
    if (!enabled || (liveSeq == null && liveEventCount == null) || stateRef.current.status !== 'ready') return
    if (!stateRef.current.followingTail) {
      commit(noticeTimelineFrontier(stateRef.current, liveSeq, liveEventCount))
      return
    }
    if (stateRef.current.loading.newer || stateRef.current.loading.tail) return
    if (stateRef.current.cursors.newer) loadRef.current?.('newer')
    else loadRef.current?.('tail')
  }, [enabled, liveSeq, liveEventCount, commit])

  const setFollowingTail = useCallback(value => {
    if (!value) cancelCatchup()
    return commit(value ? setTimelineFollowing(stateRef.current, true)
      : parkTimeline(stateRef.current, latestLiveSeqRef.current, latestLiveEventCountRef.current))
  }, [cancelCatchup, commit])
  const loadOlder = useCallback(() => {
    cancelCatchup()
    commit(parkTimeline(stateRef.current, latestLiveSeqRef.current, latestLiveEventCountRef.current))
    return stateRef.current.hasMore.older ? loadRef.current?.('older') : Promise.resolve(null)
  }, [cancelCatchup, commit])
  const loadNewer = useCallback(() => stateRef.current.cursors.newer
    ? loadRef.current?.('newer') : Promise.resolve(null), [])
  const loadAround = useCallback(seq => {
    cancelCatchup()
    lastAnchorRef.current = seq
    commit(parkTimeline(stateRef.current, latestLiveSeqRef.current, latestLiveEventCountRef.current))
    return loadRef.current?.('around', { anchorSeq: seq })
  }, [cancelCatchup, commit])
  const ensureSeq = useCallback(seq => {
    commit(parkTimeline(stateRef.current, latestLiveSeqRef.current, latestLiveEventCountRef.current))
    const anchorSatisfied = stateRef.current.requestedAnchor === Number(seq)
      && ((stateRef.current.matchedSeq != null && timelineContainsSeq(stateRef.current, stateRef.current.matchedSeq))
        || stateRef.current.totalEvents === 0)
    return timelineContainsSeq(stateRef.current, seq) || anchorSatisfied
      ? Promise.resolve(stateRef.current) : loadAround(seq)
  }, [commit, loadAround])
  const jumpToLive = useCallback(async () => {
    // Refresh before clearing the unread evidence. windowAtTail describes the last response, not the
    // server at click time, and may be stale after the reader parked.
    let refreshed = null
    for (let attempt = 0; attempt < 4; attempt += 1) {
      refreshed = await loadRef.current?.('tail')
      if (!refreshed) return refreshed
      const lagging = timelineBehindLive(
        refreshed, latestLiveSeqRef.current, latestLiveEventCountRef.current)
      if (refreshed.windowAtTail && !lagging) {
        return commit(setTimelineFollowing(stateRef.current, true))
      }
      if (attempt < TIMELINE_CATCHUP_DELAYS.length) {
        const continued = await waitCatchup(TIMELINE_CATCHUP_DELAYS[attempt])
        if (!continued) return refreshed
      }
    }
    const parked = parkTimeline({ ...stateRef.current, windowAtTail: false },
      latestLiveSeqRef.current, latestLiveEventCountRef.current)
    return commit({ ...parked, unreadUnknown: true,
      errors: { ...parked.errors, newer: 'Live events are still behind the displayed run state.' } })
  }, [commit, waitCatchup])
  const retry = useCallback(direction => direction === 'around'
    ? (lastAnchorRef.current == null ? Promise.resolve(null) : loadAround(lastAnchorRef.current))
    : loadRef.current?.(direction), [loadAround])

  return {
    ...state,
    loadOlder, loadNewer, loadAround, ensureSeq, jumpToLive, setFollowingTail, retry,
  }
}
