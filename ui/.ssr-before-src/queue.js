// Pure projection for the Queue panel. Node ids are reusable after reset, so operator requests
// and their completion gates must be compared as immutable (node_id, generation) pairs.

const validPair = (value, idKey = 'node_id') => {
  const nodeId = value?.[idKey]
  const generation = value?.generation
  return Number.isInteger(nodeId) && Number.isInteger(generation)
    ? { node_id: nodeId, generation }
    : null
}

const pairKey = ({ node_id, generation }) => `${node_id}:${generation}`

const uniquePairs = (values, idKey = 'node_id') => {
  const out = []
  const seen = new Set()
  for (const value of values || []) {
    const pair = validPair(value, idKey)
    if (!pair) continue
    const key = pairKey(pair)
    if (!seen.has(key)) { seen.add(key); out.push(pair) }
  }
  return out
}

const uniqueLegacy = (ids, twinIds, completedIds) => {
  const out = []
  const seen = new Set()
  for (const id of ids || []) {
    if (!Number.isInteger(id) || twinIds.has(id) || completedIds.has(id) || seen.has(id)) continue
    seen.add(id)
    out.push({ node_id: id, generation: null })
  }
  return out
}

export function queuedGenerationControls(state = {}) {
  const confirmRequests = uniquePairs(state.confirm_request_generations)
  const confirmDone = new Set(uniquePairs(state.confirmed_forced_generations).map(pairKey))
  const confirmTwinIds = new Set(confirmRequests.map(r => r.node_id))
  const confirmLegacyDone = new Set(state.confirmed_forced || [])
  const confirms = confirmRequests.filter(r => !confirmDone.has(pairKey(r))).concat(
    uniqueLegacy(state.confirm_requests, confirmTwinIds, confirmLegacyDone),
  )

  const ablateRequests = uniquePairs(state.ablate_request_generations)
  const ablateDone = new Set(uniquePairs(state.ablations, 'parent_id').map(pairKey))
  const ablateTwinIds = new Set(ablateRequests.map(r => r.node_id))
  const ablateLegacyDone = new Set(
    (state.ablations || []).map(a => a?.parent_id).filter(Number.isInteger),
  )
  const ablates = ablateRequests.filter(r => !ablateDone.has(pairKey(r))).concat(
    uniqueLegacy(state.ablate_requests, ablateTwinIds, ablateLegacyDone),
  )

  return { confirms, ablates }
}
