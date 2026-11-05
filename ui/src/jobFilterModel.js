export const JOB_ROLES = Object.freeze(['prepare', 'profile', 'fit', 'score', 'recovery'])

export function jobLabel(node) {
  return node?.job_role || (node?.kind === 'artifact' ? 'artifact' : 'experiment')
}

export function jobChoices(nodes) {
  const roles = new Set(Object.values(nodes || {}).map(jobLabel))
  return [...roles].sort((a, b) => a.localeCompare(b))
}

export function jobMatches(node, role) {
  return !role || jobLabel(node) === role
}

export function filterJobNodes(nodes, role) {
  return Object.values(nodes || {}).filter(node => jobMatches(node, role)).map(node => node.id)
}
