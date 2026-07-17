export const SCOPE_CONTENT_SCHEMA = 5
export const SCOPE_VERDICT_AUTHORITY = 'server-derived-v3'
export const SCOPE_NARRATIVE_AUTHORITY = 'model-advisory'

// # CODEX AGENT: Authority and freshness are independent protocol facts. A stale exact-schema report
// remains inspectable historical evidence, but only an exact `stale:false` receipt may expose its
// snapshot-bound verdict.
export function scopeReportAuthority(value) {
  const content = value?.content
  const authoritative = value?.exists === true
    && value?.authoritative === true
    && content?.schema === SCOPE_CONTENT_SCHEMA
  const freshness = !authoritative ? 'unknown'
    : value?.stale === false ? 'fresh'
      : value?.stale === true ? 'stale' : 'unknown'
  const fresh = freshness === 'fresh'
  const inspectable = authoritative && freshness !== 'unknown'
  return {
    authoritative, freshness, fresh, inspectable,
    verdict: fresh && content?.verdict_authority === SCOPE_VERDICT_AUTHORITY,
    narrative: inspectable && content?.narrative_authority === SCOPE_NARRATIVE_AUTHORITY,
  }
}

export function scopeReportGenerationError(error) {
  const code = typeof error?.code === 'string' ? error.code : ''
  if (code === 'SCOPE_REPORT_ACTION_STORAGE_UNAVAILABLE') {
    return 'Durable paid-action identity is unavailable. Generation stays locked until tab storage is restored.'
  }
  if (code === 'scope_report_action_indeterminate') {
    return 'The paid action cannot be proven complete after a server restart. Check again or explicitly abandon its recovery lock before starting another generation.'
  }
  if (code === 'scope_report_action_unknown') {
    return 'No durable claim exists for this action. Retry this same paid action or durably discard it; a fresh UUID stays disabled.'
  }
  if (code === 'scope_report_action_in_progress') {
    return 'Another paid generation is already unresolved for this scope. Wait for it or recover that action before starting a new one.'
  }
  if (code === 'scope_report_action_capacity') {
    return 'The permanent paid-action ledger is full. Start a new run root or migrate the complete ledger; never delete individual receipts.'
  }
  if (code === 'scope_report_publication_read_failed') {
    return 'Generation completed, but the current published report could not be read. Retry the report read.'
  }
  if (error?.ambiguous === true || error?.submissionMayHaveSucceeded === true) {
    return 'Generation outcome is unknown. Check the same paid action status; a new generation is disabled.'
  }
  if (error?.status === 400) return 'No runs in this scope yet.'
  if (code === 'scope_report_inputs_changed') {
    return 'Scope runs changed during generation. Retry from the current scope snapshot.'
  }
  if (error?.status === 413
      || code === 'scope_report_too_large'
      || code === 'scope_report_source_too_large') {
    // # CODEX AGENT: use client-owned remediation copy; never echo provider/server detail into UI.
    return 'Scope exceeds bounded report limits. Generate a narrower child scope or compact oversized run history.'
  }
  return 'Generation failed.'
}

// Schema 5 deliberately publishes observations only: no point estimate may become a winner in the
// browser even if a malformed intermediary payload retains otherwise-current authority markers.
export function scopeObservationRows(group) {
  const rows = Array.isArray(group?.measurements) ? group.measurements : null
  if (!rows || rows.length > 64
      || group?.contract_authority !== 'declared'
      || group?.outcome_policy !== 'observations-only-v1'
      || group?.winner !== null
      || !Array.isArray(group?.tied_winners) || group.tied_winners.length !== 0
      || !rows.every(row => row?.authority === 'declared')) return null
  return rows
}

// JSON tuple encoding avoids identity collisions between values containing separators.
export const scopeReportKey = scope => JSON.stringify([
  String(scope?.type ?? ''), String(scope?.id ?? ''),
])
