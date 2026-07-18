// One current-state visibility predicate for every experiment projection. Tombstones and the
// run-level aborted id set remain in the fold for replay/audit, but they are no longer live
// experiments and must not affect DAG geometry, concept counts, charts, or report aggregates.
export function nodeIsActive(node, state = null, aborted = null) {
  if (!node || node.tombstoned) return false
  const excluded = aborted || new Set((state?.aborted_nodes || []).map(Number))
  return !excluded.has(Number(node.id))
}

export function activeNodeMap(nodes = {}, state = null) {
  const out = {}
  const aborted = new Set((state?.aborted_nodes || []).map(Number))
  for (const [key, node] of Object.entries(nodes || {})) {
    if (nodeIsActive(node, state, aborted)) out[key] = node
  }
  return out
}

export function activeNodeConcepts(state = null) {
  const memberships = state?.node_concepts || {}
  const nodes = state?.nodes || {}
  const aborted = new Set((state?.aborted_nodes || []).map(Number))
  const out = {}
  for (const [key, concepts] of Object.entries(memberships)) {
    if (nodeIsActive(nodes[key], state, aborted)) out[key] = concepts
  }
  return out
}
