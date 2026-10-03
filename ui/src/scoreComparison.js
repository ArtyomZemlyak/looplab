import { nodeComparabilityStatus, sourceIncomplete } from './runIndex.js'
import { nodeIsActive } from './nodeProjection.js'
import { objectiveMetricSource, objectiveSourceCaveated } from './trustSemantics.js'
import { nodeBase } from './baseRevision.js'

// A score comparison never compares confirmation means or certifies statistical significance.
export function scoreDifference(node, other, state) {
  if (!state || sourceIncomplete(state) || state.objective_key
      || !['min', 'max'].includes(state.direction)
      || ![node, other].every(n => nodeIsActive(n, state) && n.status === 'evaluated'
        && n.feasible === true && Number.isFinite(n.metric)
        && !(state.breed_excluded || []).some(id => Number(id) === Number(n.id))
        && !objectiveSourceCaveated(objectiveMetricSource(n)))
      || nodeComparabilityStatus(node, other) !== 'same') return null
  const base = nodeBase(node), otherBase = nodeBase(other)
  if ((state.upstream_enabled || node.metric_provenance?.base_revision || other.metric_provenance?.base_revision)
      && (!base || !otherBase || base.digest !== otherBase.digest)) return null
  const difference = node.metric - other.metric
  return Number.isFinite(difference) ? difference : null
}

export function parentScoreDifference(node, nodes, state) {
  if (!Array.isArray(node?.parent_ids) || node.parent_ids.length !== 1) return null
  const parent = nodes?.[node.parent_ids[0]]
  const reference = node.parent_comparison
  if (!parent || !Number.isSafeInteger(parent.attempt) || parent.attempt < 0
      || reference?.version !== 1 || reference.node_id !== parent.id
      || reference.attempt !== parent.attempt) return null
  return scoreDifference(node, parent, state)
}
