// Missing or partial recorded provenance is unknown, never the current base.
export function nodeBase(node) {
  const base = node?.metric_provenance?.base_revision
  if (base?.version !== 1 || base.complete !== true || !/^[0-9a-f]{64}$/.test(base.digest || '')
      || base.scope !== 'seeded_editables_before_mounts_and_overlay'
      || !Number.isSafeInteger(base.file_count) || base.file_count < 0
      || !Number.isSafeInteger(base.bytes) || base.bytes < 0
      || !Number.isSafeInteger(base.seed_event_seq) || base.seed_event_seq < 0
      || base.node_id !== node.id || base.generation !== node.attempt
      || base.archive?.version !== 1 || base.archive?.status !== 'stored'
      || base.archive.path !== `base_snapshots/${base.digest}`) return null
  return base
}
export function baseChoices(nodes) {
  const counts = new Map()
  for (const node of Object.values(nodes || {})) {
    if (node.tombstoned) continue
    const key = nodeBase(node)?.digest || 'unknown'
    counts.set(key, (counts.get(key) || 0) + 1)
  }
  return [...counts].sort(([a], [b]) => a.localeCompare(b)).map(([digest, count]) => ({ digest, count }))
}
export function baseMatches(node, selected) {
  return !selected || (nodeBase(node)?.digest || 'unknown') === selected
}
export function capabilityOrigin(state, digest) {
  const history = Array.isArray(state?.upstream_history) ? state.upstream_history : []
  return history.find(row => row?.type === 'base_advanced' && row.selector?.digest === digest)
    || (state?.upstream_base?.selector?.digest === digest ? state.upstream_base : null)
    || null
}

// Whether ANY experiment of the run recorded a code base — the condition under which a missing one
// is news (doc 75 UX-17).
export const runRecordsBases = state => Object.values(state?.nodes || {}).some(node => nodeBase(node) != null)
