// Missing or invalid evidence never means zero contribution.
export function validConceptEffect(e) {
  if (!e || e.v !== 1 || e.method !== 'matched_concept_contrast'
    || !['matched', 'insufficient', 'unavailable'].includes(e.status)) return false
  if (e.has_advisory_warnings !== undefined && typeof e.has_advisory_warnings !== 'boolean') return false
  const counts = ['n_pairs', 'n_contexts', 'n_with', 'n_without', 'n_unknown',
    'positive', 'negative', 'neutral', 'pairs_omitted']
  if (counts.some(k => !Number.isSafeInteger(e[k]) || e[k] < 0)) return false
  if (!Array.isArray(e.pairs) || e.pairs.length > 8
    || e.pairs.length + e.pairs_omitted !== e.n_pairs
    || e.positive + e.negative + e.neutral !== e.n_pairs
    || e.n_pairs > Math.min(e.n_with, e.n_without) || e.n_contexts > e.n_pairs) return false
  if (e.pairs.some(p => !p || ['with_node', 'without_node', 'with_attempt', 'without_attempt']
    .some(k => !Number.isSafeInteger(p[k]) || p[k] < 0)
    || p.with_node === p.without_node || !Number.isFinite(p.delta)
    || !['parent', 'matched'].includes(p.kind) || !['search', 'confirmed'].includes(p.phase))) return false
  const ids = e.pairs.flatMap(p => [p.with_node, p.without_node])
  if (new Set(ids).size !== ids.length) return false
  const values = ['estimate', 'mean', 'low', 'high']
  return e.status === 'matched'
    ? e.n_pairs > 0 && e.n_contexts > 0
      && values.every(k => typeof e[k] === 'number' && Number.isFinite(e[k]))
      && e.low <= e.estimate && e.estimate <= e.high
    : e.n_pairs === 0 && values.every(k => e[k] === null)
}
export function conceptEffectValue(e) {
  return validConceptEffect(e) && e.status === 'matched' ? e.estimate : null
}
