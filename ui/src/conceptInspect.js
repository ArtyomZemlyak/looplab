// Pure decisions for inspecting an experiment FROM the concept view — no React, no I/O, so
// `node --test` can drive them directly (the `ui/` house pattern: a pure model beside its React half).
//
// The concept view used to be a place you navigated AWAY from: clicking an Experiment row threw you
// to the Lineage graph with that node selected. That is a strange thing for a *reading* surface to
// do — you were reading a concept's evidence, and the answer to "what was that experiment?" removed
// the question. So the row now selects in place and the view grows a detail pane, exactly as the Card
// board does. The two rules below are what make that honest rather than merely possible.
//
// RULE 1 — a concept frame is GENERATION-BOUND; the run state is not.
// `ConceptView` renders `experiment_refs` out of a validated `ConceptFrame` captured at some sequence,
// and each ref carries its own `node_generation` (the node ATTEMPT the membership was recorded
// against). The displayed `state.nodes[id].attempt` can have moved on since — a `node_reset` re-runs
// the same numeric id as a new attempt. Rendering the Inspector for the current attempt under a row
// describing an older one would silently answer a different question than the one clicked. The row
// button already refused that case; the pane has to refuse it the same way, from the same rule,
// which is why the rule is stated once here instead of twice inline.
//
// RULE 2 — a selected node id can outlive the frame it was picked from.
// `nodeId` is route state (`#…?view=concepts&node=12`), so it arrives from deep links, Back/Forward
// and a lens change that re-fetches a different frame. The pane must therefore describe what it has
// rather than assume: "nothing picked", "picked but not in this snapshot", or "inspect it".

const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)

/**
 * May this frame ref be opened in the Inspector against the displayed run state?
 *
 * True only when the ref's node exists in the snapshot AND its `attempt` equals the ref's
 * `node_generation`. Everything else — a ref for a node the snapshot does not contain, a ref whose
 * lifecycle has been superseded — is refused, because the Inspector reads the CURRENT attempt and
 * would present it under the older attempt's row.
 */
export function conceptRefInspectable(ref, state) {
  if (!record(ref) || !Number.isSafeInteger(ref.node_id)) return false
  const node = record(state?.nodes) ? state.nodes[ref.node_id] : null
  return !!node && Number.isSafeInteger(node.attempt) && node.attempt === ref.node_generation
}

/**
 * What the concept view's detail pane should show, given the route's selected node id.
 *
 * Three kinds, and the middle one is the reason this is a function rather than a boolean:
 *   • `empty`   — nothing is selected. The pane explains what picking a row will do.
 *   • `absent`  — an id is selected but the displayed snapshot has no such node. This is a real
 *                 state (a historical fold does not contain nodes created after its sequence, and a
 *                 deep link can name any id), and it must READ as "not in this snapshot" rather than
 *                 as an empty pane, which would look like nothing was clicked.
 *   • `node`    — inspect it. `attempt` travels so the pane can label which lifecycle it is showing.
 *
 * Deliberately NOT a fourth "stale" kind: once a node id is selected the Inspector shows that node's
 * current attempt, which is the truth about the node. The attempt fence belongs to the ROW (rule 1),
 * where the mismatch is between what you clicked and what you would get.
 */
export function conceptPaneTarget(state, nodeId) {
  if (!Number.isSafeInteger(nodeId) || nodeId < 0) return { kind: 'empty', nodeId: null, attempt: null }
  const node = record(state?.nodes) ? state.nodes[nodeId] : null
  if (!record(node)) return { kind: 'absent', nodeId, attempt: null }
  return {
    kind: 'node',
    nodeId,
    attempt: Number.isSafeInteger(node.attempt) ? node.attempt : null,
  }
}
