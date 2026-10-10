// The visible line of each Why-strip entry, built from STRUCTURED fields (doc 75 UX-14).
//
// The strip printed the strategist's free-text `rationale` and the policy's `reason` verbatim:
// "exploring breadth: race candidates with ASHA (smoke rung -> promote survivors to full)",
// "endgame (inside the plan's reserve, 100% of node budget spent): …", "exploit best (rungs
// collapsed) -> #3" — on the first screen of a first run. Those texts are the decision's own record
// (and a model-written rationale is a prompt's output, never rewritten here), so they stay, whole,
// in the entry's title and its disclosure. What is SHOWN is said from the fields the decision
// carries — `policy`, `fidelity`, `operators` — in words that need no glossary.
// `finished`: the run wrote its end, so the endgame is no longer "nearly" anything — the finished
// demo's strip said "Budget nearly spent" beside "Finished".
export function strategyPlainLine(strategy, finished = false) {
  if (!strategy || typeof strategy !== 'object') return ''
  const operators = strategy.operators && typeof strategy.operators === 'object' ? strategy.operators : {}
  if (operators.endgame_sweep || operators.merge_mode === 'ensemble')
    return finished ? 'Budget spent: combined the best results'
      : 'Budget nearly spent: combining the best results'
  const policy = typeof strategy.policy === 'string' ? strategy.policy : ''
  if (policy === 'asha' || policy === 'bohb') return 'Trying several directions quickly, then refining the best'
  if (policy === 'evolutionary') return 'Combining and varying the best results'
  if (policy === 'mcts') return 'Exploring branches of the most promising results'
  if (policy === 'greedy') return 'Refining the best result so far'
  return policy ? 'Changing how the search proceeds' : ''
}

// `[message, args]` for `uiMessage`, so the number stays out of the translated text.
export function policyPlainLine(chosen) {
  return Number.isSafeInteger(chosen) && chosen >= 0 ? ['building on experiment #{0}', [chosen]]
    : ['choosing the next experiment', []]
}
