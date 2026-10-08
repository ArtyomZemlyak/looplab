// Add an experiment (or an ARTIFACT node) to a live run by hand — the RULES of the inject form
// (doc 73 §1.4). The React half is `InjectNodePanel.jsx`; this is the pure one, the split
// `forkFromSeqModel.js` / `ForkFromSeqPanel.jsx` already uses, for the same reason: what must never
// be got wrong is WHAT GETS SENT.
//
// THE COMMAND IS `inject_node`, the one the Assistant's `inject_experiment` tool and the API already
// speak (`CONTROL.inject`). Until this form existed the UI had no way to reach it: the graph's node
// menu branches (`fork`) or merges, and neither can make a node with no parent, an artifact node, or
// a consumer that reads one.
//
// WHAT THE SERVER ALREADY REFUSES, and therefore what this module only mirrors so the button can
// explain itself before a round trip (`looplab/serve/control_validation.py`):
//   * `node_kind` is "artifact" or absent;
//   * `uses` names artifact nodes that are ALREADY PRODUCED (evaluated in their current lifecycle) —
//     `inject_uses_not_produced` / `inject_uses_not_artifact` otherwise;
//   * a parent is fenced by its generation (`parent_generations`), so an inject composed against
//     attempt 2 is refused if the parent has been re-run since.
// The server is the authority; a refusal it returns is printed as it came.

export const INJECT_KINDS = Object.freeze(['experiment', 'artifact'])
export const INJECT_USES_MAX = 32
export const INJECT_RATIONALE_MAX = 20_000

const status = node => String(node?.status?.value ?? node?.status ?? '')
const label = node => {
  const text = String(node?.idea?.rationale || node?.idea?.operator || '').replace(/\s+/g, ' ').trim()
  return text.length > 90 ? `${text.slice(0, 89)}…` : text
}

/**
 * What the form may offer, from the run's folded state: the PARENTS (every evaluated, undeleted
 * node — a branch continues from a settled experiment) and the PRODUCED ARTIFACTS a consumer may
 * name. Sorted by id so a poll never reorders a select under the operator's cursor.
 */
export function injectCandidates(state) {
  const nodes = Object.values(state?.nodes || {})
    .filter(node => node && Number.isInteger(node.id) && !node.tombstoned)
    .sort((a, b) => a.id - b.id)
  const evaluated = nodes.filter(node => status(node) === 'evaluated')
  return {
    parents: evaluated.filter(node => node.kind !== 'artifact')
      .map(node => ({ id: node.id, attempt: Number(node.attempt ?? 0), label: label(node) })),
    artifacts: evaluated.filter(node => node.kind === 'artifact')
      .map(node => ({ id: node.id, label: label(node) })),
  }
}

/**
 * Parse the optional parameters box: empty is "no params" (absent), else a JSON OBJECT of numbers —
 * `Idea.params` is `dict[str, float]`, so a string value would be refused by the server after the
 * operator had been told the form was fine.
 */
export function parseInjectParams(text) {
  if (!String(text ?? '').trim()) return { ok: true, value: undefined }
  let parsed
  try { parsed = JSON.parse(text) } catch (error) {
    return { ok: false, error: `Parameters must be valid JSON: ${error.message}` }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: 'Parameters must be a JSON object, for example {"lr": 0.001}.' }
  }
  const bad = Object.entries(parsed).find(([, v]) => typeof v !== 'number' || !Number.isFinite(v))
  if (bad) return { ok: false, error: `Parameter "${bad[0]}" must be a number.` }
  return { ok: true, value: parsed }
}

export const INJECT_BLOCKED_REASONS = Object.freeze({
  no_rationale: 'Describe the experiment: the Developer builds it from this text.',
  too_long: `The description is longer than ${INJECT_RATIONALE_MAX.toLocaleString('en-US')} characters.`,
  bad_kind: 'Choose an experiment or an artifact node.',
  unknown_parent: 'That parent is no longer an evaluated experiment of this run.',
  unknown_artifact: 'One of the artifacts it uses is no longer produced.',
  too_many_uses: `An experiment may use at most ${INJECT_USES_MAX} artifacts.`,
  bad_params: 'Fix the parameters first.',
  submitting: 'Already submitting.',
})

/**
 * Whether the draft may be sent, and if not the FIRST reason's `code`. `draft` is
 * `{rationale, kind, uses, parentId, params}` where `params` is `parseInjectParams`'s answer.
 */
export function injectSubmitDecision({ state, draft, submitting = false }) {
  if (submitting) return { ok: false, code: 'submitting' }
  const rationale = String(draft?.rationale ?? '').trim()
  if (!rationale) return { ok: false, code: 'no_rationale' }
  if (rationale.length > INJECT_RATIONALE_MAX) return { ok: false, code: 'too_long' }
  if (!INJECT_KINDS.includes(draft?.kind)) return { ok: false, code: 'bad_kind' }
  if (draft?.params && !draft.params.ok) return { ok: false, code: 'bad_params' }
  const { parents, artifacts } = injectCandidates(state)
  if (draft?.parentId != null && !parents.some(p => p.id === draft.parentId)) {
    return { ok: false, code: 'unknown_parent' }
  }
  const uses = Array.isArray(draft?.uses) ? draft.uses : []
  if (uses.length > INJECT_USES_MAX) return { ok: false, code: 'too_many_uses' }
  if (uses.some(id => !artifacts.some(a => a.id === id))) return { ok: false, code: 'unknown_artifact' }
  return { ok: true, code: null }
}

/**
 * The `inject_node` body, or null when `injectSubmitDecision` refuses. Optional keys are LEFT OUT,
 * never null-filled: the server stamps `node_kind`/`uses` onto `node_created` only when present, so
 * an ordinary experiment's payload keeps its historical shape. The parent's generation is the one
 * the operator SAW (from the same `state`), never re-read at send time.
 */
export function buildInjectPayload({ state, draft }) {
  if (!injectSubmitDecision({ state, draft }).ok) return null
  const idea = { operator: 'inject', rationale: String(draft.rationale).trim() }
  if (draft.params?.ok && draft.params.value !== undefined) idea.params = draft.params.value
  const payload = { idea }
  if (draft.parentId != null) {
    const parent = injectCandidates(state).parents.find(p => p.id === draft.parentId)
    payload.parent_id = parent.id
    payload.parent_generations = { [parent.id]: parent.attempt }
  }
  if (draft.kind === 'artifact') payload.node_kind = 'artifact'
  const uses = [...new Set(Array.isArray(draft.uses) ? draft.uses : [])].sort((a, b) => a - b)
  if (uses.length) payload.uses = uses
  return payload
}
