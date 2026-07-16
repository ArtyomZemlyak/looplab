export const SCOPE_CONTENT_SCHEMA = 5
export const SCOPE_VERDICT_AUTHORITY = 'server-derived-v3'
export const SCOPE_NARRATIVE_AUTHORITY = 'model-advisory'

// # CODEX AGENT: Freshness and authority are protocol booleans, not truthy hints. Missing, null,
// numeric, and legacy values must all fail closed before outcome-bearing report content is rendered.
export function scopeReportAuthority(value) {
  const content = value?.content
  // REVIEW(2026-07-16): `stale === false` here conflates FRESHNESS with AUTHORITY, and the effect is
  // that a paid report over any LIVE scope is unreadable: the server recomputes stale on every GET
  // (any event appended by a still-running scope run flips it), mid-run regeneration is structurally
  // impossible (_inputs_unchanged discards even a finished synthesis on any input change), and
  // ScopeReport.jsx gates ALL content — advisory narrative AND winner-free observation rows — on
  // authority.current, replacing them with "quarantined … Regenerate to inspect it". The pre-4225226
  // UI rendered stale content WITH a stale badge and only suppressed the winner. Staleness should
  // stay a per-section demotion (badge + withhold outcome claims), not a blanket content kill:
  // split `current` into `authoritative` (exists/authoritative/schema) and `fresh` (stale===false),
  // and let the narrative/observations render under `authoritative && !fresh` with the stale notice.
  const current = value?.exists === true
    && value?.authoritative === true
    && value?.stale === false
    && content?.schema === SCOPE_CONTENT_SCHEMA
  return {
    current,
    verdict: current && content?.verdict_authority === SCOPE_VERDICT_AUTHORITY,
    narrative: current && content?.narrative_authority === SCOPE_NARRATIVE_AUTHORITY,
  }
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
