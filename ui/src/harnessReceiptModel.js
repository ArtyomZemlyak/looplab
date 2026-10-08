// Saved receipts are observations, not authority to recover a command. Keep the
// wire checks in a pure leaf; tests pin this event vocabulary to serve/protocol.py.
import { authoringTextRevision, COMMAND_ID_RE, COMMAND_PENDING, COMMAND_STATUSES, validRunGeneration } from './api.js'

export const RECEIPT_CONTROL_EVENTS = new Set([
  'annotation', 'approval_granted', 'budget_extend', 'card_dropped', 'card_edited', 'card_filed',
  'card_reopened', 'card_reprioritized', 'card_resource_pinned', 'comment_created',
  'comment_edited', 'comment_resolution_changed', 'concept_tag_edited', 'deep_research',
  'force_ablate', 'force_confirm', 'fork', 'hint', 'hypothesis_added', 'hypothesis_updated',
  'inject_node', 'metric_retarget', 'node_abort', 'node_reset', 'pause', 'promote',
  'report_generated', 'research_completed', 'restart', 'resume', 'run_abort', 'run_concepts',
  'run_reopened', 'set_strategy', 'spec_approved', 'track_requested', 'upstream_auto_set',
])

export function validReceipt(value, generation, commandId = '') {
  const row = value?.command
  return validRunGeneration(generation) && value?.version === 1 && value.generation === generation
    && typeof row?.id === 'string' && COMMAND_ID_RE.test(row.id) && (!commandId || row.id === commandId)
    && COMMAND_STATUSES.has(row.status) && value.terminal === !COMMAND_PENDING.has(row.status)
    && RECEIPT_CONTROL_EVENTS.has(row.event_type)
    && (row.event_seq === null || (Number.isSafeInteger(row.event_seq) && row.event_seq >= 0))
    && typeof row.error_code === 'string' && row.error_code.length <= 256 && typeof row.retryable === 'boolean'
}

// Same UTF-8 SHA-256/first 128 bits as serve/command_identity.py. Reuse the
// existing bounded hashing primitive; never display an unbound key lookup.
export async function receiptCommandId(kind, identity, source = globalThis.crypto) {
  if (kind === 'id') return identity
  try { return 'cmd_' + (await authoringTextRevision(identity, source)).slice(7, 39) }
  catch {
    throw Object.assign(new Error('Key verification unavailable'), { code: 'receipt_key_unavailable' })
  }
}
