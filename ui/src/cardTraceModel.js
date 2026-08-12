// A CARD's whole story, as the operator reads it — the pure half of the card Trace section.
//
// A Card is one hypothesis: the Researcher proposes it, the Developer builds one or more experiments
// (nodes) under it. Those two halves used to live on different screens, so following one line of
// reasoning meant hunting around the UI. Worse, until `orchestrator.stamp_proposal_span` there was no
// join between them AT ALL — no span carried a card id and no card event carried a trace id — so the
// hunt could not have succeeded even in principle.
//
// The rule this model exists to hold: a section is shown because something DURABLE says it belongs
// to this card, never because it happened at about the right time. An empty research section is an
// honest answer; another hypothesis's reasoning under this card is not.

// How a research row was matched, in the operator's words. Shown on the row so nobody has to guess
// which rule applied — and so a `shared_trace` match reads as the inference it is.
export const RESEARCH_LINK_LABEL = Object.freeze({
  card_id: 'proposed this card',
  shared_trace: 're-proposed in this experiment’s build',
})

export const researchLinkLabel = link => RESEARCH_LINK_LABEL[link] || 'linked to this card'

/**
 * The ordered sections: research first, then one per node.
 *
 * Deliberately NOT interleaved by timestamp. The story an operator wants is "what was the idea, then
 * what did each experiment do with it", and a strict time sort scatters a re-proposal into the middle
 * of the node list where it reads as a fourth experiment.
 */
export function cardTraceSections(payload) {
  const research = Array.isArray(payload?.research) ? payload.research : []
  const nodes = Array.isArray(payload?.nodes) ? payload.nodes : []
  const sections = []
  if (research.length) {
    sections.push({
      kind: 'research',
      key: 'research',
      title: research.length === 1 ? 'Research' : `Research · ${research.length} proposals`,
      rows: research.map(row => ({
        ...row,
        label: researchLinkLabel(row.link),
        // A proposal with no sub-spans has no trace worth opening — say so before the click.
        openable: Number(row.spans || 0) > 1 && !!row.trace_id,
      })),
    })
  }
  for (const node of nodes) {
    sections.push({
      kind: 'node',
      key: `node-${node.node_id}`,
      title: `Experiment #${node.node_id}`,
      node,
      // Deliberately NOT `&& node.trace_id`. `trace_id` is the trace the node was AUTHORED in
      // (`node_created.trace_id`) — two spans, `Author node` → `materialize_node` — and the section
      // used to open exactly that, which is how the Developer's build, repairs and evaluation
      // vanished from this surface. The Developer's trace is the NODE's, read per node, and
      // `spans` is the count of it: 61 for rubertlite-dr-unified-v3 node 0 against that trace's 2.
      openable: Number(node.spans || 0) > 0,
    })
  }
  return sections
}

/**
 * What the reader is NOT being shown, in words. Silent when nothing is missing — a receipt that
 * always prints teaches the reader to ignore it, and this one has to be believed the day it matters.
 */
export function cardTraceNotice(payload) {
  const projection = payload?.projection || {}
  if (projection.unavailable === true) return 'Trace unavailable for this work item.'
  const research = Array.isArray(payload?.research) ? payload.research.length : 0
  const nodes = Array.isArray(payload?.nodes) ? payload.nodes.length : 0
  if (!research && !nodes) return 'No trace recorded for this work item yet.'
  if (!research) {
    // The common case on runs recorded before the engine stamped the link, and the one place where
    // saying nothing would read as "the Researcher did no work".
    return 'No research is linked to this work item — runs recorded before the card link shipped '
      + 'carry no such link, and it is never inferred from timing.'
  }
  return ''
}
