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

// CODEX AGENT: A retained concept row is authoritative only when replay emitted no
// materialization receipt for it. The trust boundary is receipt presence (never the reason text);
// malformed envelopes fail unavailable while future partial reasons remain safely display-only.
const UNAVAILABLE_REASON = /^(?:concept_mode_|delta_dependency_|invalid_consolidation)/
const receiptStoreCache = new WeakMap()

function receiptStatus(receipt) {
  const reasons = receipt?.reasons
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)
      || Object.keys(receipt).length !== 2 || !Object.hasOwn(receipt, 'status')
      || !Object.hasOwn(receipt, 'reasons') || !Array.isArray(reasons) || !reasons.length
      || !reasons.every(reason => typeof reason === 'string' && reason)) return 'invalid'
  const unavailable = reasons.some(reason => UNAVAILABLE_REASON.test(reason))
  if (receipt.status === 'partial') return unavailable ? 'invalid' : 'partial'
  return receipt.status === 'unavailable' && unavailable ? 'unavailable' : 'invalid'
}

function receiptStoreValid(receipts, nodes) {
  const cached = receiptStoreCache.get(receipts)
  if (cached?.nodes === nodes) return cached.valid
  const valid = Object.entries(receipts).every(([key, receipt]) => {
    const id = Number(key)
    return Number.isSafeInteger(id) && id >= 0 && String(id) === key
      && nodes && typeof nodes === 'object' && !Array.isArray(nodes)
      && Object.hasOwn(nodes, key) && receiptStatus(receipt) !== 'invalid'
  })
  receiptStoreCache.set(receipts, { nodes, valid })
  return valid
}

// Omit nodeId for current-view aggregate truth; pass it for one node's theme/tag projection.
export function conceptMaterializationStatus(state = null, nodeId = undefined) {
  let aggregate = 'complete'
  if (state?.run_base_concept_receipt != null) {
    const status = receiptStatus(state.run_base_concept_receipt)
    if (status === 'invalid' || status === 'unavailable') return 'unavailable'
    aggregate = status
  }
  const receipts = state?.node_concept_materialization_receipts
  if (receipts == null) return aggregate
  if (typeof receipts !== 'object' || Array.isArray(receipts)) return 'unavailable'
  const nodes = state?.nodes
  // # CODEX AGENT: ConceptFrame rejects an orphan or malformed receipt globally. Validate the whole
  // immutable snapshot once before serving any node-specific theme, including inactive receipt rows.
  if (!receiptStoreValid(receipts, nodes)) return 'unavailable'
  if (nodeId !== undefined) {
    const key = String(nodeId)
    if (!Object.hasOwn(receipts, key)) return aggregate
    const status = receiptStatus(receipts[key])
    return status === 'unavailable' ? status : 'partial'
  }
  const aborted = new Set((state?.aborted_nodes || []).map(Number))
  for (const [key, receipt] of Object.entries(receipts)) {
    if (!nodeIsActive(nodes[key], state, aborted)) continue
    const status = receiptStatus(receipt)
    if (status === 'unavailable') return status
    aggregate = status
  }
  return aggregate
}
