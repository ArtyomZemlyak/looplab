export const DAG_AUTO_FIT_LIMIT = 48
export const DAG_READABLE_VIEWPORT = Object.freeze({ x: 24, y: 24, zoom: 0.78 })
export const DAG_REFIT_LIMIT = 24
export const LARGE_GRAPH_OVERVIEW_THRESHOLD = 80

// ResizeObserver fires in the same delivery turn as React Flow's own size observer. Two animation
// frames let the library commit its new width/height before fitView reads them. Kept pure so the
// expand/collapse runtime contract is testable without a browser-specific observer shim.
export function createDagCanvasRefitScheduler({ fit, cameraTouched, requestFrame, cancelFrame }) {
  let frame = 0
  let lastSize = ''
  const cancel = () => {
    if (frame) cancelFrame(frame)
    frame = 0
  }
  return {
    resize(width, height) {
      if (!Number.isFinite(width) || !Number.isFinite(height) || width < 320 || height < 180) return false
      const size = `${Math.round(width)}x${Math.round(height)}`
      if (size === lastSize) return false
      lastSize = size
      cancel()
      frame = requestFrame(() => {
        frame = requestFrame(() => {
          frame = 0
          if (!cameraTouched()) fit()
        })
      })
      return true
    },
    cancel,
  }
}

export const shouldAutoFitDag = count => Number.isSafeInteger(count) && count >= 0
  && count <= DAG_AUTO_FIT_LIMIT

export const shouldRefitDag = (previous, current) => !!previous && !!current
  && previous.signature !== current.signature
  && Number.isSafeInteger(current.count) && current.count >= 0 && current.count <= DAG_REFIT_LIMIT
  && (current.count < previous.count
    || (current.mode !== previous.mode && current.count === previous.count))

export function initialDagOverviewDecision({ ready, nodeCount, explicitContext = false }) {
  if (!ready || !Number.isSafeInteger(nodeCount) || nodeCount <= 0) return 'wait'
  if (explicitContext || nodeCount < LARGE_GRAPH_OVERVIEW_THRESHOLD) return 'preserve'
  return 'collapse'
}
