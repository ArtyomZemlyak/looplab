// The visible line of each Why-strip entry, built from STRUCTURED fields (doc 75 UX-14).
//
// The strip printed the strategist's free-text `rationale` and the policy's `reason` verbatim:
// "exploring breadth: race candidates with ASHA (smoke rung -> promote survivors to full)",
// "endgame (inside the plan's reserve, 100% of node budget spent): …", "exploit best (rungs
// collapsed) -> #3" — on the first screen of a first run. Those texts are the decision's own record
// (and a model-written rationale is a prompt's output, never rewritten here), so they stay, whole,
// in the entry's title and its disclosure. What is SHOWN is said from the fields the decision
// carries — `policy`, `fidelity`, `operators` — in words that need no glossary.

// Where the run stands against its PLAN (the folded `state.plan` row, `engine/plan.py`), as
// `{ inEndgame, budgetSpent }`. The endgame operators alone say nothing about the budget: the
// Strategist may switch `endgame_sweep` on at any consult, and the strip then read "Budget nearly
// spent" from node 2 of 20; a run stopped by the operator, a plateau or an error read "Budget spent"
// (code review). `inEndgame` is `in_endgame`'s rule, `budgetSpent` the id allocator at the plan's
// `max_nodes` (which a `budget_extend` re-cut already includes).
export function planStanding(state) {
  const plan = state?.plan
  const ids = Object.keys(state?.nodes || {}).map(Number).filter(Number.isSafeInteger)
  const used = ids.length ? Math.max(...ids) + 1 : 0
  if (!plan || typeof plan !== 'object') return { inEndgame: false, budgetSpent: false }
  const start = Number(plan.endgame_start)
  const end = plan.endgame_end == null ? null : Number(plan.endgame_end)
  const max = Number(plan.max_nodes)
  return {
    inEndgame: Number.isFinite(start) && ids.length >= start && (end == null || ids.length < end),
    budgetSpent: Number.isFinite(max) && max > 0 && used >= max,
  }
}

// `standing`: `planStanding(state)` plus `finished`. The budget line is said only where the plan
// says it is true; elsewhere the policy's own line stands.
export function strategyPlainLine(strategy, standing = {}) {
  if (!strategy || typeof strategy !== 'object') return ''
  const operators = strategy.operators && typeof strategy.operators === 'object' ? strategy.operators : {}
  const endgame = operators.endgame_sweep || operators.merge_mode === 'ensemble'
  if (endgame && standing.finished && standing.budgetSpent) return 'Budget spent: combined the best results'
  if (endgame && !standing.finished && standing.inEndgame) return 'Budget nearly spent: combining the best results'
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
