export const generation = 'a'.repeat(64)
export const node = { id: 'node:2:1', kind: 'node', node_id: 2, attempt: 1, status: 'evaluated',
  score: 0.5, confirmed_mean: null, confirmed_seeds: null, feasible: true, trust_flagged: false,
  salvaged: false, violations: 0, objective: 'accuracy', direction: 'max', failure: '', commentary: null,
  evidence_token: 'b'.repeat(64), parents: [{ node_id: 0, attempt: 0, score: 0.4, comparability: 'unknown' }] }
export const payload = items => ({ version: 1, generation, total: items.length, has_more: false, next_cursor: null, items })
