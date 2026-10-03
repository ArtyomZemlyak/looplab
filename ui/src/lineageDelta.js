import { parentScoreDifference } from './scoreComparison.js'

export function delta(node, state) {
  const parent = state?.nodes?.[node?.parent_ids?.[0]]
  // An arrow claims improvement. Base-score receipts do not certify confirmation
  // means, and a new shared runner changes the conditions of the experiment.
  if (!node || !parent || node.confirmed_mean != null || parent.confirmed_mean != null) return null
  const d = parentScoreDifference(node, state.nodes, state)
  if (d == null) return null
  return { d, improved: state.direction === 'min' ? d < 0 : d > 0 }
}
