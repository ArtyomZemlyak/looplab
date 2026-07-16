export const DAG_AUTO_FIT_LIMIT = 48
export const DAG_READABLE_VIEWPORT = Object.freeze({ x: 24, y: 24, zoom: 0.78 })
export const DAG_REFIT_LIMIT = 24
export const LARGE_GRAPH_OVERVIEW_THRESHOLD = 80

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
