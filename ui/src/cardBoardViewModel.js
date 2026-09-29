// Decisions used only by the lazily-loaded Card workspace. Keeping them out of cardBoardModel.js
// prevents the DAG route from paying for the Kanban's 1-Card -> N-Node index and coverage receipts.
import { isRecord } from './panelPrimitives.js'
import { cardNodes, nodeCardId } from './cardBoardModel.js'

export function cardAttemptIndex(state, cards) {
  const rows = Array.isArray(cards) ? cards : []
  const nodes = isRecord(state?.nodes) ? state.nodes : {}
  const byCard = new Map(rows.map(card => [card.id, new Map()]))
  const touch = (cardId, nodeId) => {
    const bucket = byCard.get(cardId)
    if (!bucket) return null
    if (!bucket.has(nodeId)) {
      const node = isRecord(nodes[nodeId]) ? nodes[nodeId] : null
      bucket.set(nodeId, { nodeId, evidence: false, owned: false, substituted: false, present: !!node, node })
    }
    return bucket.get(nodeId)
  }
  for (const card of rows) {
    for (const nodeId of cardNodes(card.evidence)) touch(card.id, nodeId).evidence = true
    // The SAME join as `cardBoardModel.js::cardAttempts`, which the Inspector reads while the board
    // reads this one: a substituted build must be flagged in both, or the board calls it evidence
    // that "fed the verdict" (or a pending reservation) while the Inspector says it was not a test.
    for (const nodeId of cardNodes(card.substituted_nodes)) touch(card.id, nodeId).substituted = true
  }
  for (const [key, node] of Object.entries(nodes)) {
    const cardId = nodeCardId(node)
    const nodeId = Number(key)
    if (!byCard.has(cardId) || !Number.isSafeInteger(nodeId) || nodeId < 0) continue
    touch(cardId, nodeId).owned = true
  }
  return new Map([...byCard].map(([cardId, bucket]) => [
    cardId, [...bucket.values()].sort((a, b) => a.nodeId - b.nodeId),
  ]))
}

export function cardAttemptCoverage(attempts, receipt) {
  const visible = Array.isArray(attempts) ? attempts.length : 0
  const omission = isRecord(receipt?.omissions?.evidence) ? receipt.omissions.evidence : null
  if (!omission) return { exact: true, lowerBound: visible, label: String(visible) }
  const trustedTotal = omission.unit === 'node_ids' && Number.isSafeInteger(omission.total)
    && omission.total >= 0 ? omission.total : null
  const lowerBound = Math.max(visible, trustedTotal ?? visible)
  return { exact: false, lowerBound, label: `≥${lowerBound}` }
}

// A Card's verdict is a research judgment, while this is a measured node result. Keep the two
// separate and only surface a terminal, present evidence node that actually tested the Card.
export function cardLatestMeasuredEvidence(attempts) {
  if (!Array.isArray(attempts)) return null
  let latest = null
  for (const entry of attempts) {
    if (!entry?.evidence || entry.substituted || !entry.present
      || entry.node?.status !== 'evaluated' || entry.node?.feasible === false
      || typeof entry.node?.metric !== 'number' || !Number.isFinite(entry.node.metric)) continue
    if (!latest || entry.nodeId > latest.nodeId) {
      latest = { nodeId: entry.nodeId, metric: entry.node.metric }
    }
  }
  return latest
}
