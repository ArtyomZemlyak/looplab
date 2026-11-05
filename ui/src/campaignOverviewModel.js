const integer = value => Number.isSafeInteger(value) && value >= 0

export function campaignOverviewView(overview, { visibleNodeCount, mapEventSeq, snapshotSeq } = {}) {
  if (!overview || typeof overview !== 'object') return { status: 'missing', reasons: [] }
  if (integer(mapEventSeq) && integer(snapshotSeq) && mapEventSeq > snapshotSeq) {
    return { status: 'inconsistent', reasons: [] }
  }
  const reasons = (Array.isArray(overview.freshness?.reasons) ? overview.freshness.reasons : [])
    .filter(row => row && typeof row.code === 'string').map(row => ({ ...row }))
  const atNode = integer(overview.at_node) ? overview.at_node : null
  if (atNode !== null && integer(visibleNodeCount) && atNode < visibleNodeCount
      && !reasons.some(row => row.code === 'newer_nodes')) {
    reasons.push({ code: 'newer_nodes', at_node: atNode, current_nodes: visibleNodeCount })
  }
  return {
    status: reasons.length ? 'stale' : (atNode === null ? 'unknown' : 'current'),
    reasons, atNode,
    publishedSeq: integer(overview.published_seq) ? overview.published_seq : null,
  }
}
