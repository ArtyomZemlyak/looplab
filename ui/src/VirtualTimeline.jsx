import React, { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import {
  anchoredScrollTop, buildVirtualLayout, DEFAULT_TIMELINE_OVERSCAN, DEFAULT_TIMELINE_ROW_HEIGHT,
  tailScrollTop, timelineViewportAtTail, virtualIndexAt, virtualRange,
} from './timelineWindow.js'

const defaultKey = row => row.seq

function MeasuredRow({ itemKey, top, position, setSize, onMeasure, children }) {
  const ref = useRef(null)
  useLayoutEffect(() => {
    const element = ref.current
    if (!element) return undefined
    const measure = () => onMeasure(itemKey, Math.max(1, element.getBoundingClientRect().height + 2))
    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    return () => observer.disconnect()
  }, [itemKey, onMeasure])
  return <div ref={ref} className="timeline-virtual-row" data-event-row role="listitem"
    aria-posinset={position} aria-setsize={setSize}
    style={{ transform: `translateY(${top}px)` }}>{children}</div>
}

// Dependency-free variable-height windowing. The visible anchor is captured under the PREVIOUS
// layout and restored before the new layout is observed. Only a user scroll can relinquish follow;
// ResizeObserver and programmatic anchor/bottom corrections never change that intent.
export default function VirtualTimeline({
  rows,
  getKey = defaultKey,
  renderRow,
  identity = 'timeline',
  className = '',
  followingTail = false,
  windowAtTail = false,
  unread = 0,
  unreadUnknown = false,
  busy = false,
  onFollowingTailChange,
  onJumpToLive,
  estimateSize = DEFAULT_TIMELINE_ROW_HEIGHT,
  overscan = DEFAULT_TIMELINE_OVERSCAN,
  ariaLabel = 'Event timeline',
}) {
  const scrollRef = useRef(null)
  const measurements = useRef(new Map())
  const pendingMeasurements = useRef(new Map())
  const measureScheduled = useRef(false)
  const anchorRef = useRef(null)
  const followingRef = useRef(followingTail)
  const suppressScrollRef = useRef(followingTail)
  const scrollWriteTokenRef = useRef(0)
  const identityRef = useRef(identity)
  // Mounting a replacement is itself an identity reset: the previous timeline is conditionally
  // removed while its fenced tail loads, so a new component cannot infer this from identityRef.
  const identityResetRef = useRef(followingTail)
  const tailPinTokenRef = useRef(0)
  const mountedRef = useRef(true)
  const [measureRevision, setMeasureRevision] = useState(0)
  const [viewport, setViewport] = useState({ top: 0, height: 1 })
  followingRef.current = followingTail

  const layout = useMemo(() => buildVirtualLayout(rows, measurements.current,
    (row, index) => `${identity}:${getKey(row, index)}`, estimateSize),
    [rows, getKey, identity, estimateSize, measureRevision])
  const range = virtualRange(layout, viewport.top, viewport.height, overscan)

  const readViewport = useCallback((updateAnchor = false) => {
    const element = scrollRef.current
    if (!element) return
    const top = element.scrollTop
    const height = element.clientHeight
    setViewport(previous => previous.top === top && previous.height === height ? previous : { top, height })
    if (updateAnchor && layout.count) {
      const index = virtualIndexAt(layout, top)
      anchorRef.current = { key: layout.keys[index], offset: top - layout.offsets[index] }
    }
  }, [layout])

  const setProgrammaticScroll = useCallback((top) => {
    const element = scrollRef.current
    if (!element) return
    const writeToken = scrollWriteTokenRef.current + 1
    scrollWriteTokenRef.current = writeToken
    suppressScrollRef.current = true
    element.scrollTop = Math.max(0, top)
    readViewport(true)
    // Several sync/microtask/rAF pin attempts may overlap. An early attempt must never release the
    // suppression owned by a later write and misclassify its native scroll event as a user park.
    const release = () => {
      if (scrollWriteTokenRef.current === writeToken) suppressScrollRef.current = false
    }
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(release)
    else queueMicrotask(release)
  }, [readViewport])

  // Replacing the log generation can momentarily mount this scroller before its flex height and
  // virtual-space scrollHeight have settled. A single layout-effect assignment is then clamped to
  // zero and no later row measurement is guaranteed to repair it. Pin once synchronously and again
  // after layout; if a verified live-edge window still cannot be pinned, relinquish follow before
  // its header can keep claiming "live" while the first rows are visible.
  const pinFollowingTail = useCallback(() => {
    const token = tailPinTokenRef.current + 1
    tailPinTokenRef.current = token
    identityResetRef.current = true
    let attempts = 0
    const pin = () => {
      const element = scrollRef.current
      if (!element || token !== tailPinTokenRef.current || !followingRef.current) return false
      setProgrammaticScroll(tailScrollTop(element.scrollHeight, element.clientHeight))
      const expectsOverflow = layout.totalHeight > element.clientHeight + 2
      const geometryReady = !expectsOverflow || element.scrollHeight > element.clientHeight + 2
      return geometryReady
        && timelineViewportAtTail(element.scrollHeight, element.scrollTop, element.clientHeight)
    }
    pin()
    const verify = (stableFrames = 0) => {
      const element = scrollRef.current
      if (!element || token !== tailPinTokenRef.current || !followingRef.current) return
      const expectsOverflow = layout.totalHeight > element.clientHeight + 2
      const geometryReady = !expectsOverflow || element.scrollHeight > element.clientHeight + 2
      const aligned = geometryReady
        && timelineViewportAtTail(element.scrollHeight, element.scrollTop, element.clientHeight)
      if (aligned && stableFrames >= 1) {
        identityResetRef.current = false
        return
      }
      if (attempts < 8) {
        attempts += 1
        if (!aligned) pin()
        requestAnimationFrame(() => verify(aligned ? stableFrames + 1 : 0))
        return
      }
      identityResetRef.current = false
      if (windowAtTail && element.clientHeight > 0) {
        followingRef.current = false
        onFollowingTailChange?.(false)
      }
    }
    queueMicrotask(pin)
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => verify(0))
    } else queueMicrotask(() => {
      const aligned = pin()
      identityResetRef.current = false
      if (!aligned && windowAtTail) {
        followingRef.current = false
        onFollowingTailChange?.(false)
      }
    })
  }, [layout.totalHeight, onFollowingTailChange, setProgrammaticScroll, windowAtTail])

  const onScroll = useCallback(() => {
    readViewport(true)
    // Replacing the virtual space can enqueue a native scroll-to-zero event after React's layout
    // effects. Until the new geometry is stably pinned, that event belongs to the reset, not to the
    // reader. The fence is bounded above; a later real user scroll still relinquishes follow.
    if (suppressScrollRef.current || identityResetRef.current) return
    const element = scrollRef.current
    if (!element) return
    const nearBottom = timelineViewportAtTail(
      element.scrollHeight, element.scrollTop, element.clientHeight, 64)
    const nextFollowing = windowAtTail && nearBottom
    if (nextFollowing !== followingRef.current) {
      followingRef.current = nextFollowing
      onFollowingTailChange?.(nextFollowing)
    }
  }, [layout.totalHeight, onFollowingTailChange, readViewport, windowAtTail])

  // Measurements and anchors are generation-scoped. A reset may reuse seq=0 with a completely
  // different expanded row height; carrying the old cache across identity would visibly jump.
  useLayoutEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      tailPinTokenRef.current += 1
      scrollWriteTokenRef.current += 1
      pendingMeasurements.current.clear()
    }
  }, [])

  useLayoutEffect(() => {
    if (identityRef.current === identity) return
    identityRef.current = identity
    identityResetRef.current = followingRef.current
    // Invalidate a release queued by the old generation. A true follower owns all replacement-
    // caused scroll events until the new virtual space is stable; a parked reader keeps control.
    scrollWriteTokenRef.current += 1
    suppressScrollRef.current = followingRef.current
    measurements.current.clear()
    pendingMeasurements.current.clear()
    anchorRef.current = null
    setMeasureRevision(revision => revision + 1)
  }, [identity])

  useLayoutEffect(() => {
    const retained = new Set(layout.keys)
    for (const key of measurements.current.keys()) {
      if (!retained.has(key)) measurements.current.delete(key)
    }
    for (const key of pendingMeasurements.current.keys()) {
      if (!retained.has(key)) pendingMeasurements.current.delete(key)
    }
  }, [layout.keys])

  // Restore the OLD anchor before any observer records the NEW layout. For true followers the bottom
  // pin wins. On identity reset there is no meaningful old anchor to preserve.
  useLayoutEffect(() => {
    const element = scrollRef.current
    if (!element) return
    if (followingTail) {
      pinFollowingTail()
    } else if (!identityResetRef.current && anchorRef.current) {
      const target = anchoredScrollTop(layout, anchorRef.current.key, anchorRef.current.offset)
      if (target != null && Math.abs(element.scrollTop - target) > 0.5) setProgrammaticScroll(target)
      else readViewport(true)
    } else readViewport(false)
  }, [followingTail, identity, layout, pinFollowingTail, readViewport, rows, setProgrammaticScroll, windowAtTail])

  useLayoutEffect(() => {
    if (!followingTail) {
      tailPinTokenRef.current += 1
      scrollWriteTokenRef.current += 1
      identityResetRef.current = false
      suppressScrollRef.current = false
    }
  }, [followingTail])

  useLayoutEffect(() => {
    const element = scrollRef.current
    if (!element || typeof ResizeObserver === 'undefined') return undefined
    let previousHeight = element.clientHeight
    const observer = new ResizeObserver(() => {
      const nextHeight = element.clientHeight
      if (nextHeight !== previousHeight && followingRef.current) {
        previousHeight = nextHeight
        pinFollowingTail()
      } else {
        previousHeight = nextHeight
        readViewport(false)
      }
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [layout.totalHeight, pinFollowingTail, readViewport])

  // Every visible row measures during one layout pass. Batch those callbacks into one revision so a
  // 40-row window performs one O(retained rows) layout, not 40 consecutive layouts.
  const measure = useCallback((key, height) => {
    const previous = measurements.current.get(key)
    if (previous != null && Math.abs(previous - height) < 0.5) return
    pendingMeasurements.current.set(key, height)
    if (measureScheduled.current) return
    measureScheduled.current = true
    queueMicrotask(() => {
      measureScheduled.current = false
      if (!mountedRef.current || !pendingMeasurements.current.size) return
      for (const [pendingKey, pendingHeight] of pendingMeasurements.current) {
        measurements.current.set(pendingKey, pendingHeight)
      }
      pendingMeasurements.current.clear()
      setMeasureRevision(revision => revision + 1)
    })
  }, [])

  const visible = []
  for (let index = range.start; index < range.end; index += 1) {
    const row = rows[index]
    const key = layout.keys[index]
    visible.push(<MeasuredRow key={key} itemKey={key} top={layout.offsets[index]}
      position={index + 1} setSize={rows.length} onMeasure={measure}>
      {renderRow(row, index)}
    </MeasuredRow>)
  }
  return <div className="timeline-virtual-wrap">
    <div ref={scrollRef} className={`timeline-virtual ${className}`.trim()} onScroll={onScroll}
         role="list" aria-label={ariaLabel} aria-busy={busy} tabIndex={0}>
      <div className="timeline-virtual-space" style={{ height: layout.totalHeight }}>{visible}</div>
    </div>
    {!followingTail && (unreadUnknown || unread > 0) && <div className="timeline-unread-status" role="status"
      aria-live="polite" aria-atomic="true">
      <button type="button" className="timeline-unread" onClick={onJumpToLive}
        aria-label={unreadUnknown ? 'New activity; jump to live'
          : `${unread} new event${unread === 1 ? '' : 's'}; jump to live`}>
        {unreadUnknown ? 'new activity · jump to live' : `${unread} new · jump to live`}
      </button>
    </div>}
  </div>
}
