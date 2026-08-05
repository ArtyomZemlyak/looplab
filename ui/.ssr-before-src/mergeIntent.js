const RUN_GENERATION = /^[0-9a-f]{64}$/

const safeNodeId = value => Number.isSafeInteger(value) && value >= 0
const safeAttempt = value => Number.isSafeInteger(value) && value >= 0
const nodeAttempt = (nodes, nodeId) => {
  const attempt = nodes?.[nodeId]?.attempt
  return safeAttempt(attempt) ? attempt : null
}

export function captureMergeIntent({ runId, runGeneration, nodes, sourceId, targetId = null }) {
  if (typeof runId !== 'string' || !runId || !RUN_GENERATION.test(runGeneration || '')
      || !safeNodeId(sourceId) || (targetId != null && (!safeNodeId(targetId) || targetId === sourceId))) {
    return null
  }
  const sourceAttempt = nodeAttempt(nodes, sourceId)
  const targetAttempt = targetId == null ? null : nodeAttempt(nodes, targetId)
  if (sourceAttempt == null || (targetId != null && targetAttempt == null)) return null
  return { runId, runGeneration, sourceId, sourceAttempt, targetId, targetAttempt }
}

export function mergeIntentMatches(intent, { runId, runGeneration, nodes }, requireTarget = false) {
  if (!intent || intent.runId !== runId || intent.runGeneration !== runGeneration
      || nodeAttempt(nodes, intent.sourceId) !== intent.sourceAttempt) return false
  if (intent.targetId == null) return !requireTarget
  return nodeAttempt(nodes, intent.targetId) === intent.targetAttempt
}

export function selectMergeTarget(intent, context, targetId) {
  if (!mergeIntentMatches(intent, context) || !safeNodeId(targetId) || targetId === intent.sourceId) {
    return null
  }
  const targetAttempt = nodeAttempt(context.nodes, targetId)
  return targetAttempt == null ? null : { ...intent, targetId, targetAttempt }
}

export function mergeIntentCommand(intent) {
  if (!intent || intent.targetId == null) return null
  return {
    runId: intent.runId,
    ids: [intent.sourceId, intent.targetId],
    parentGenerations: {
      [intent.sourceId]: intent.sourceAttempt,
      [intent.targetId]: intent.targetAttempt,
    },
    expectedGeneration: intent.runGeneration,
  }
}
