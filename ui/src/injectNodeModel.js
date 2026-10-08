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

import { questionFilingOptions } from './questionLattice.js'

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
 * THE QUESTION AN INJECT ANSWERS (`idea.parent_card_id`). Every operator-injected env on
 * `minionerec-backbones-v11` named none — 39 of 39 — because no inject surface offered the field, so
 * the Research view could only guess where they belonged. The open questions on the board, labelled
 * by statement (`questionLattice.js::questionFilingOptions`); a merged-away row is not offered, the
 * server refuses it (`inject_question_invalid`).
 */
export function injectQuestions(state) {
  const cards = state?.cards && typeof state.cards === 'object' && !Array.isArray(state.cards)
    ? Object.entries(state.cards)
      .filter(([id, card]) => id && card && typeof card === 'object' && !card.merged_into)
      .map(([id, card]) => ({ ...card, id }))
    : []
  return questionFilingOptions(cards)
}

/**
 * The DETERMINISTIC suggestion: a node built on parent P continues P's line of work, so it is offered
 * the question P's card is filed under — nothing else is guessed (a concept match needs concepts the
 * new node does not have yet). Null when there is no parent, the parent's card is unfiled, or that
 * question is not on the board.
 */
export function suggestedInjectQuestion(state, parentId) {
  if (parentId == null) return null
  const node = state?.nodes?.[parentId] ?? state?.nodes?.[String(parentId)]
  const cardId = typeof node?.idea?.card_id === 'string' ? node.idea.card_id : null
  const card = cardId && state?.cards && typeof state.cards === 'object' ? state.cards[cardId] : null
  const question = typeof card?.parent_card_id === 'string' ? card.parent_card_id : null
  return question && injectQuestions(state).some(q => q.id === question) ? question : null
}

// A message the panel shows is a CATALOGUE KEY plus its values, never an English sentence with the
// value already spliced in: `uiMessage(key, values)` finds the Russian entry only for the key, and an
// interpolated string matched nothing (critic 2026-10-08). `error` keeps the English rendering for
// the callers and tests that read it.
const message = (key, values = []) => ({
  key, values, text: key.replace(/\{(\d+)\}/g, (_, i) => String(values[Number(i)] ?? '')),
})

/**
 * Parse the optional parameters box: empty is "no params" (absent), else a JSON OBJECT of numbers —
 * `Idea.params` is `dict[str, float]`, so a string value would be refused by the server after the
 * operator had been told the form was fine. A refusal carries `message` ({key, values}) beside its
 * English `error`.
 */
export function parseInjectParams(text) {
  if (!String(text ?? '').trim()) return { ok: true, value: undefined }
  const refuse = (key, values) => {
    const m = message(key, values)
    return { ok: false, error: m.text, message: m }
  }
  let parsed
  try { parsed = JSON.parse(text) } catch (error) {
    return refuse('Parameters must be valid JSON: {0}', [error.message])
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return refuse('Parameters must be a JSON object, for example {"lr": 0.001}.')
  }
  const bad = Object.entries(parsed).find(([, v]) => typeof v !== 'number' || !Number.isFinite(v))
  if (bad) return refuse('Parameter "{0}" must be a number.', [bad[0]])
  return { ok: true, value: parsed }
}

// Each refusal's catalogue KEY; `injectBlockedMessage` supplies the values the two numeric ones name.
export const INJECT_BLOCKED_REASONS = Object.freeze({
  no_rationale: 'Describe the experiment: the Developer builds it from this text.',
  too_long: 'The description is longer than {0} characters.',
  bad_kind: 'Choose an experiment or an artifact node.',
  unknown_parent: 'That parent is no longer an evaluated experiment of this run.',
  unknown_artifact: 'One of the artifacts it uses is no longer produced.',
  unknown_question: 'That research question is no longer on the board.',
  too_many_uses: 'An experiment may use at most {0} artifacts.',
  bad_params: 'Fix the parameters first.',
  submitting: 'Already submitting.',
})

/**
 * The refusal `code` as `{key, values, text}` for `uiMessage(key, values)`. `bad_params` is the
 * parser's own message when the draft carries one. `locale` formats the description cap
 * (20,000 / 20 000); null for an unknown code.
 */
export function injectBlockedMessage(code, { params, locale = 'en-US' } = {}) {
  if (code === 'bad_params' && params?.message) return params.message
  const key = INJECT_BLOCKED_REASONS[code]
  if (!key) return null
  if (code === 'too_long') return message(key, [INJECT_RATIONALE_MAX.toLocaleString(locale)])
  if (code === 'too_many_uses') return message(key, [INJECT_USES_MAX])
  return message(key)
}

/**
 * What the draft still NAMES that the run no longer offers — the ticked artifacts that stopped being
 * produced (reset, failed, deleted) and a parent that is no longer an evaluated experiment. The panel
 * shows each one, still selected, with the control that clears it: the form used to keep refusing
 * `unknown_artifact` over a box that had disappeared from the list, with no way to untick it
 * (critic 2026-10-08). Nothing is cleared FOR the operator — what they chose stays visible until they
 * change it.
 */
export function staleInjectSelections(state, draft) {
  const { parents, artifacts } = injectCandidates(state)
  const uses = Array.isArray(draft?.uses) ? draft.uses : []
  const parentId = draft?.parentId
  return {
    uses: uses.filter(id => !artifacts.some(a => a.id === id)),
    parentId: parentId != null && !parents.some(p => p.id === parentId) ? parentId : null,
  }
}

/**
 * Whether the draft may be sent, and if not the FIRST reason's `code`. `draft` is
 * `{rationale, kind, uses, parentId, params, questionId}` where `params` is `parseInjectParams`'s
 * answer and `questionId` the research question it answers (null: none).
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
  if (draft?.questionId != null && !injectQuestions(state).some(q => q.id === draft.questionId)) {
    return { ok: false, code: 'unknown_question' }
  }
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
  if (draft.questionId != null) idea.parent_card_id = draft.questionId
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
