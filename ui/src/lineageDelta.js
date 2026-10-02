import { nodeComparabilityStatus, sourceIncomplete } from './runIndex.js'
import { nodeIsActive } from './nodeProjection.js'
import { objectiveMetricSource, objectiveSourceCaveated } from './trustSemantics.js'
import { nodeBase } from './baseRevision.js'

export function delta(node, state) {
  const parent = state?.nodes?.[node?.parent_ids?.[0]]
  // An arrow claims improvement. Base-score receipts do not certify confirmation
  // means, and a new shared runner changes the conditions of the experiment.
  if (!parent || sourceIncomplete(state) || state.objective_key
      || !['min', 'max'].includes(state.direction)
      || ![node, parent].every(n => nodeIsActive(n, state) && n.status === 'evaluated'
        && n.feasible === true && Number.isFinite(n.metric) && n.confirmed_mean == null
        && !objectiveSourceCaveated(objectiveMetricSource(n)))
      || nodeComparabilityStatus(node, parent) !== 'same') return null
  const base = nodeBase(node), parentBase = nodeBase(parent)
  if ((base && parentBase && base.digest !== parentBase.digest)
      || (state.upstream_enabled && (!base || !parentBase))) return null
  const d = node.metric - parent.metric
  return { d, improved: state.direction === 'min' ? d < 0 : d > 0 }
}
