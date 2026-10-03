// Cached detail remains useful for code and trace. Result evidence comes from one
// complete record of the same lifecycle/status, never from fields spliced across attempts.
export function currentResultNode(node, state) {
  const current = state?.nodes?.[node.id]
  return current?.id === node.id && Number.isSafeInteger(node.attempt) && node.attempt >= 0
    && current.attempt === node.attempt && current.status === node.status ? current : node
}

export function confirmationSeedResults(node, detail, state) {
  if (state?.nodes?.[node.id] === node && Object.hasOwn(state, 'confirm_seed_results'))
    return state.confirm_seed_results?.[node.id] || {}
  return ['id', 'attempt', 'status', 'metric', 'confirmed_mean', 'confirmed_std', 'confirmed_seeds']
    .every(key => detail?.[key] === node[key]) ? detail?.confirm_seeds_detail || {} : {}
}
