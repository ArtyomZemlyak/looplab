export const DEFAULT_TIMELINE_ROW_HEIGHT = 34
export const DEFAULT_TIMELINE_OVERSCAN = 8

export function buildVirtualLayout(rows, measurements, getKey, estimate = DEFAULT_TIMELINE_ROW_HEIGHT) {
  const count = rows.length
  const offsets = new Float64Array(count + 1)
  const keys = new Array(count)
  const fallback = Math.max(1, Number(estimate) || DEFAULT_TIMELINE_ROW_HEIGHT)
  for (let index = 0; index < count; index += 1) {
    const key = String(getKey(rows[index], index))
    keys[index] = key
    const measured = Number(measurements?.get?.(key))
    offsets[index + 1] = offsets[index] + (Number.isFinite(measured) && measured > 0 ? measured : fallback)
  }
  return { offsets, keys, totalHeight: offsets[count], count }
}

export function virtualIndexAt(layout, offset) {
  if (!layout.count) return 0
  const target = Math.max(0, Math.min(Number(offset) || 0, layout.totalHeight))
  let low = 0, high = layout.count
  while (low < high) {
    const middle = (low + high) >>> 1
    if (layout.offsets[middle + 1] <= target) low = middle + 1
    else high = middle
  }
  return Math.min(layout.count - 1, low)
}

export function virtualRange(layout, scrollTop, viewportHeight, overscan = DEFAULT_TIMELINE_OVERSCAN) {
  if (!layout.count) return { start: 0, end: 0 }
  const pad = Math.max(0, Number(overscan) || 0)
  const first = virtualIndexAt(layout, scrollTop)
  const last = virtualIndexAt(layout, (Number(scrollTop) || 0) + Math.max(0, Number(viewportHeight) || 0))
  return { start: Math.max(0, first - pad), end: Math.min(layout.count, last + pad + 1) }
}

export function anchoredScrollTop(layout, key, offset = 0) {
  const index = layout.keys.indexOf(String(key))
  return index < 0 ? null : Math.max(0, layout.offsets[index] + Number(offset || 0))
}

export const tailScrollTop = (totalHeight, viewportHeight) =>
  Math.max(0, (Number(totalHeight) || 0) - Math.max(0, Number(viewportHeight) || 0))

export function timelineBottomGap(scrollHeight, scrollTop, viewportHeight) {
  return Math.max(0, (Number(scrollHeight) || 0) - Math.max(0, Number(scrollTop) || 0)
    - Math.max(0, Number(viewportHeight) || 0))
}

export function timelineViewportAtTail(scrollHeight, scrollTop, viewportHeight, tolerance = 2) {
  return timelineBottomGap(scrollHeight, scrollTop, viewportHeight)
    <= Math.max(0, Number(tolerance) || 0)
}
