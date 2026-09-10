// The cross-run claim-ledger reads and the SANITIZERS that bound what they may put into React state.
// A MEMBER of the api.js barrel (doc 25 UI-02); it never imports api.js back and every name below is
// re-exported from there, so no consumer changed.
//
// UI-02's resolution left these in api.js for a stated reason: they looked like they belonged in
// `researchAtlasModel.js`, but that module opened by importing the two of them FROM the barrel and
// immediately re-exporting them — so hosting them there while api.js kept re-exporting them is a
// barrel<->member cycle, the one shape that change exists to avoid. (The quoted import line is
// deliberately paraphrased here rather than transcribed: `apiBarrel.test.js` scans this file's text
// for barrel imports, and a member that merely QUOTES one reads to that scanner exactly like a
// member that has one.) That blocker is gone twice over: the F7 surface rename retired
// `researchAtlasModel.js` (its successor is `claimsCurationModel.js`, and the two names are now
// `boundedLedgerText` / `projectLedgerSource`), and the fix here does not host them in a consumer at
// all. They live in a module of their own that imports only the fetch client, so `claimsCurationModel.js`
// and `ClaimsCuration.jsx` keep taking them from the barrel exactly as they do today — no consumer is
// re-pointed, and no member imports a barrel.
//
// The allowlist below is the load-bearing part and its comment states the direction that bites; both
// moved verbatim.
import { get } from './apiClient.js'

// Experimental Claims & Curation reads: owner-only, read-only projections over the shared memory
// portfolio. The ROUTE names below are the server's and are unchanged by the F7 surface rename
// (doc 29) — `/api/cross-run/atlas` still serves the mixed-evidence claim records the screen reads.
// Bypass browser caches so Refresh observes newly finalized runs/governance without a stale intermediary.
const crossRunRead = (path, options = {}) => get(path, { ...options, cache: 'no-store' })
export function boundedLedgerText(value, max = 360) {
  if (!['string', 'number', 'boolean'].includes(typeof value)) return ''
  const limit = Number.isSafeInteger(max) ? Math.max(0, Math.min(2000, max)) : 360
  const text = String(value).slice(0, limit)
  return text.replace(/[\u0000-\u001f\u007f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, ' ')
    .replace(/\s+/gu, ' ').trim().slice(0, limit)
}
// An ALLOWLIST: a field absent here never reaches React state. F7 dropped the concepts section, so
// the concept sections of the atlas envelope (`explored`/`thin_coverage` — up to 24 rows carrying 6
// run references each) and the `concept_capsules.jsonl` read receipt beside them are no longer
// listed. They are still SERVED; nothing renders them, so nothing keeps them.
//
// THE OTHER DIRECTION IS THE ONE THAT BITES, and it had: a field this list omits that something
// DOES render is not a smaller payload, it is a render branch no server response can reach — and it
// is silent, because the field simply arrives `undefined`. Five of `CrossRunClaim`'s were in exactly
// that state (`decision`/`note`/`by`/`at`, `polarity`, `sources`, `verification`,
// `evidence_digest`), so `ClaimsCuration.jsx`'s Decision line, the polarity half of its metric line
// and its whole "Sources and verification" disclosure were dead markup while
// `claimsCurationModel.js::normalizeClaim` went on bounding and validating all five. On a Claims &
// Curation screen the steward's own verdict — who ratified a claim, when, and why — is the thing an
// operator came for. `ui/test/claimsCuration.test.js` now derives the model's wire reads and fails
// on any name that is not here, so the two halves cannot drift apart again.
const CROSS_RUN_STATE_FIELDS = `portfolio_id n_runs n_contested
  claim_source contradictions
  revisions claims n revision v status complete entries limit source_complete
  runs run_id metric polarity sources verification evidence_digest
  decision note by at
  claim_uid statement epistemic maturity decision_fresh n_support n_oppose n_unverified
  n_contradicts support oppose unverified contradicts scopes receipt_known read_complete
  research_source_complete lessons research snapshot_digest rows_total rows_retained
  rows_quarantined malformed_rows invalid_rows outcome proposals receipt merges splits purges
  decisions applied concept_governance`.split(/\s+/)
const CROSS_RUN_STATE_CAPS = {
  contradictions: 12, claims: 40, entries: 20,
  runs: 6, support: 6, oppose: 6, unverified: 6, contradicts: 6, scopes: 6,
}
const CROSS_RUN_COUNT_ARRAYS = new Set(['merges', 'splits', 'purges', 'decisions', 'applied'])
function projectCrossRunValue(value, key = '', depth = 0) {
  if (typeof value === 'string') return boundedLedgerText(value, 500)
  if (typeof value === 'number' || typeof value === 'boolean' || value == null) return value
  if (Array.isArray(value)) {
    if (CROSS_RUN_COUNT_ARRAYS.has(key)) return value.length
    return value.slice(0, CROSS_RUN_STATE_CAPS[key] || 6)
      .map(item => projectCrossRunValue(item, key, depth + 1))
  }
  if (typeof value !== 'object' || depth >= 7) return null
  const out = {}
  for (const field of CROSS_RUN_STATE_FIELDS) {
    if (Object.hasOwn(value, field)) {
      out[field] = projectCrossRunValue(value[field], field, depth + 1)
    }
  }
  return out
}
export function projectLedgerSource(key, value) {
  const projected = projectCrossRunValue(value)
  if (key === 'atlas') {
    const contested = Array.isArray(value?.contradictions) ? value.contradictions.length : 0
    projected.n_contested = Math.max(
      Number.isSafeInteger(projected.n_contested) && projected.n_contested >= 0
        ? projected.n_contested : 0,
      contested,
    )
  }
  return projected
}
const boundedCrossRunInt = (value, fallback, maximum, minimum = 0) => {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? Math.max(minimum, Math.min(maximum, Math.trunc(parsed))) : fallback
}
const crossRunLimitArgs = (limitOrOptions, fallback, maximum, options) =>
  limitOrOptions && typeof limitOrOptions === 'object'
    ? { limit: fallback, options: limitOrOptions }
    : { limit: boundedCrossRunInt(limitOrOptions, fallback, maximum, 1), options }
// Bounds exist on both sides of the wire. Client render caps prevent DOM amplification; these query
// caps also prevent a routine claim-ledger navigation from requesting an unbounded shared ledger.
export const getCrossRunAtlas = (limitOrOptions = 24, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 24, 50, options)
  return crossRunRead(`/api/cross-run/atlas?limit=${args.limit}`, args.options)
}
export const getCrossRunClaims = (limitOrOptions = 80, offset = 0, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 80, 200, options)
  const offsetIsOptions = offset && typeof offset === 'object'
  if (offsetIsOptions && args.options == null) args.options = offset
  return crossRunRead(
    `/api/cross-run/claims?limit=${args.limit}&offset=${boundedCrossRunInt(offsetIsOptions ? 0 : offset, 0, 1_000_000)}`,
    args.options)
}
export const getCrossRunCurationLog = (limitOrOptions = 20, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 20, 50, options)
  return crossRunRead(`/api/cross-run/curation-log?limit=${args.limit}`, args.options)
}
export const getCrossRunClaimCurationLog = (limitOrOptions = 20, options) => {
  const args = crossRunLimitArgs(limitOrOptions, 20, 50, options)
  return crossRunRead(`/api/cross-run/claim-curation-log?limit=${args.limit}`, args.options)
}
