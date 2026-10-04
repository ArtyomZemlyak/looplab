import { isRecord } from './panelPrimitives.js'

const count = value => Number.isSafeInteger(value) && value >= 0
const text = value => typeof value === 'string' && !!value
const strings = value => Array.isArray(value) && value.every(text)
const optionalText = value => value == null || typeof value === 'string'
const question = value => isRecord(value) && text(value.checkpoint_id) && text(value.phase_id)
  && count(value.node_id) && count(value.node_generation)
  && ['stage', 'expectation', 'observation'].every(key => optionalText(value[key]))

// Validate the full read envelope, not admission policy. Missing obligations and
// inconsistent pagination are unavailable; a valid damaged source stays readable.
export function validHarnessProgress(value, offset = 0) {
  if (!isRecord(value) || !count(value.event_seq) || !count(value.at_node)
      || typeof value.complete !== 'boolean' || !isRecord(value.source_health)
      || !['events', 'decisions', 'reviews', 'checkpoints'].every(key => isRecord(value.source_health[key]))
      || !Object.values(value.source_health).every(row => isRecord(row)
        && typeof row.read_complete === 'boolean')
      || !['decisions', 'reviews', 'checkpoints'].every(key => count(value.source_health[key].accepted_rows))
      || value.complete !== Object.values(value.source_health).every(row => row.read_complete)
      || !isRecord(value.candidate_requirements)
      || !['effective_concepts', 'hypothesis_statement'].every(key => typeof value.candidate_requirements[key] === 'boolean')
      || !Array.isArray(value.candidate_blockers_if_expanding)
      || !value.candidate_blockers_if_expanding.every(row => isRecord(row) && text(row.phase_id) && text(row.action))
      || !isRecord(value.candidate_decisions_per_idea)
      || !Object.entries(value.candidate_decisions_per_idea).every(([key, n]) => text(key) && count(n) && n > 0)
      || typeof value.finish_report_due !== 'boolean' || !strings(value.finish_reviews_due)
      || !Array.isArray(value.finish_pending_nodes) || !value.finish_pending_nodes.every(count)
      || !count(value.pending_checkpoint_count) || !Array.isArray(value.pending_checkpoints)
      || !value.pending_checkpoints.every(row => isRecord(row) && question(row.question))
      || value.pending_checkpoints.length !== Math.min(value.pending_checkpoint_count, 100)
      || typeof value.pending_checkpoints_truncated !== 'boolean'
      || value.pending_checkpoints_truncated !== (value.pending_checkpoint_count > value.pending_checkpoints.length)
      || !isRecord(value.history)) return false
  return ['decisions', 'reviews', 'checkpoints'].every(kind => {
    const page = value.history[kind]
    return isRecord(page) && count(page.total) && page.offset === offset && page.limit === 20
      && Array.isArray(page.items) && page.items.length === Math.min(20, Math.max(0, page.total - offset))
      && typeof page.has_more === 'boolean' && page.has_more === (offset + 20 < page.total)
      && page.items.every(row => {
        if (!isRecord(row) || !['status', 'validity', 'lifecycle'].every(key => optionalText(row[key]))) return false
        const receipt = kind === 'checkpoints' ? row.question : row
        return isRecord(receipt) && (kind !== 'checkpoints' || question(receipt))
          && ['phase_id', 'action_id', 'action_ref', 'stage', 'decision', 'reason', 'expectation', 'observation']
            .every(key => optionalText(receipt[key]))
          && (receipt.at_node == null || count(receipt.at_node))
          && (row.answer == null || isRecord(row.answer)
            && ['verdict', 'reason'].every(key => optionalText(row.answer[key])))
      })
  })
}
