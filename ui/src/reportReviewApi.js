// Optional paid review: persist one exact request before submitting, and preserve it on ambiguity.
import { _authHeaders, assertNotReviewMutation, get, runApiPath } from './apiClient.js'
import { createIdempotencyKey, validRunGeneration, UUID_V4_RE } from './commandModel.js'
import { commandJson, jobAwait } from './commandProtocol.js'
import { assertRunMutationAllowed } from './runMode.js'

const hex = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const integer = value => Number.isSafeInteger(value) && value >= 0
const object = value => value && typeof value === 'object' && !Array.isArray(value)
const keys = (value, expected) => object(value) && Object.keys(value).sort().join('|') === [...expected].sort().join('|')
const slot = (rid, generation) => `ll.report-review.${encodeURIComponent(rid)}.${generation}`
const validBody = (body, generation) => keys(body, ['expected_generation', 'report_seq', 'report_digest', 'input_digest'])
  && validRunGeneration(generation) && body.expected_generation === generation
  && integer(body.report_seq) && hex(body.report_digest) && hex(body.input_digest)

export function loadReportReviewIntent(rid, generation, storage = globalThis.sessionStorage) {
  if (!validRunGeneration(generation)) throw new Error('Verified run generation required.')
  const raw = storage.getItem(slot(rid, generation))
  if (raw == null) return null
  let value
  try { value = JSON.parse(raw) } catch { throw new Error('Saved review request is damaged; preserve it and reconcile the native receipt.') }
  if (!keys(value, ['version', 'key', 'body']) || value.version !== 1
      || typeof value.key !== 'string' || !UUID_V4_RE.test(value.key) || !validBody(value.body, generation)) {
    throw new Error('Saved review request is invalid; preserve it and reconcile the native receipt.')
  }
  return value
}

export function saveReportReviewIntent(rid, generation, input, storage = globalThis.sessionStorage) {
  const current = loadReportReviewIntent(rid, generation, storage)
  if (current) return current // A newer view never replaces an unresolved paid request.
  const body = { expected_generation: generation, report_seq: input?.report_seq,
    report_digest: input?.report_digest, input_digest: input?.input_digest }
  if (!validBody(body, generation)) throw new Error('Read the current published report review first.')
  const value = { version: 1, key: createIdempotencyKey(), body }
  storage.setItem(slot(rid, generation), JSON.stringify(value))
  const saved = loadReportReviewIntent(rid, generation, storage)
  if (JSON.stringify(saved) !== JSON.stringify(value)) throw new Error('Cannot verify saved paid review identity.')
  return saved
}

export function clearReportReviewIntent(rid, generation, key, storage = globalThis.sessionStorage) {
  const current = loadReportReviewIntent(rid, generation, storage)
  if (current?.key !== key) throw new Error('Paid review identity changed; preserve the saved request.')
  storage.removeItem(slot(rid, generation))
  if (storage.getItem(slot(rid, generation)) != null) throw new Error('Cannot clear the completed review identity.')
}

export async function getReportReview(rid, options = {}) {
  const view = await get(runApiPath(rid, '/report-review'), options)
  if (!object(view) || !validBody({ expected_generation: view.generation,
    report_seq: view.input?.report_seq, report_digest: view.input?.report_digest,
    input_digest: view.input?.input_digest }, view.generation)
    || view.input?.version !== 1 || view.input?.generation !== view.generation
    || typeof view.input?.run_uid !== 'string' || !/^[0-9a-f]{32}$/.test(view.input.run_uid)
    || typeof view.unresolved !== 'boolean' || typeof view.conflicted !== 'boolean'
    || new TextEncoder().encode(JSON.stringify(view.input)).length > 129 * 1024) throw protocolError()
  if (view.latest !== null) {
    const latest = view.latest
    if (!object(latest) || !integer(latest.seq) || typeof latest.is_current !== 'boolean'
        || typeof latest.unavailable !== 'boolean') throw protocolError()
    if (latest.unavailable) {
      if (latest.content !== null || latest.is_current) throw protocolError()
    } else {
      const content = latest.content
      if (!object(content) || !validBody({ expected_generation: view.generation,
        report_seq: content.report_seq, report_digest: content.report_digest,
        input_digest: content.input_digest }, view.generation)) throw protocolError()
      validateReportReviewResult({ ok: true, generation: view.generation, seq: latest.seq, content },
        { body: { expected_generation: view.generation, report_seq: content.report_seq,
          report_digest: content.report_digest, input_digest: content.input_digest } })
      if (content.run_uid !== view.input.run_uid
          || (latest.is_current && content.input_digest !== view.input.input_digest)) throw protocolError()
    }
  }
  return view
}

const protocolError = () => Object.assign(new Error('Review receipt is invalid. Resume the saved request; completion is unknown.'),
  { code: 'REPORT_REVIEW_PROTOCOL_ERROR', ambiguous: true, submissionMayHaveSucceeded: true })

export function validateReportReviewResult(result, intent) {
  if (!object(result) || result.generation !== intent.body.expected_generation) throw protocolError()
  if (result.ok === false) {
    if (result.code === 'report_review_failed' && result.error_kind === 'review_or_accounting_failure'
        && result.ambiguous !== true) return result
    // Unknown, timeout and failed-job results can never release a saved paid identity.
    throw Object.assign(protocolError(), { message: result.error || protocolError().message })
  }
  const content = result.content
  if (result.ok !== true || !integer(result.seq) || result.seq <= intent.body.report_seq
      || !object(content) || content.version !== 1
      || content.generation !== intent.body.expected_generation
      || typeof content.run_uid !== 'string' || !/^[0-9a-f]{32}$/.test(content.run_uid)
      || content.input_digest !== intent.body.input_digest
      || content.report_seq !== intent.body.report_seq || content.report_digest !== intent.body.report_digest
      || typeof content.summary !== 'string' || !content.summary.trim()
      || new TextEncoder().encode(JSON.stringify(content)).length > 64 * 1024
      || !Array.isArray(content.comments) || content.comments.length > 5
      || content.reviewer?.mutation_tools !== false || content.reviewer?.verdict_authority !== 'advisory_only') throw protocolError()
  for (const row of content.comments) {
    if (!object(row) || !['none', 'confirm', 'ablation', 'comparison'].includes(row.nomination)
        || typeof row.report_field !== 'string' || typeof row.quote !== 'string' || !row.quote.trim() || typeof row.comment !== 'string'
        || !Array.isArray(row.node_ids) || row.node_ids.length > 8 || !row.node_ids.every(integer)
        || new Set(row.node_ids).size !== row.node_ids.length
        || !Array.isArray(row.outcomes) || row.outcomes.length !== row.node_ids.length
        || row.outcomes.some((outcome, index) => !object(outcome) || outcome.node_id !== row.node_ids[index]
          || !integer(outcome.node_id) || !integer(outcome.attempt)
          || !(outcome.metric === null || (typeof outcome.metric === 'number' && Number.isFinite(outcome.metric))))) throw protocolError()
    if (row.nomination === 'none' ? row.nomination_id !== null || row.target_node_id !== null
      : !hex(row.nomination_id) || !integer(row.target_node_id) || !row.node_ids.includes(row.target_node_id)) throw protocolError()
  }
  return result
}

export async function submitReportReview(rid, generation, { signal, storage = globalThis.sessionStorage } = {}) {
  const intent = loadReportReviewIntent(rid, generation, storage)
  if (!intent) throw new Error('Save the exact paid review request before submitting.')
  const path = runApiPath(rid, '/report-review')
  assertNotReviewMutation(path)
  assertRunMutationAllowed(path)
  try {
    const response = await commandJson(path, { method: 'POST', signal,
      headers: _authHeaders({ 'Content-Type': 'application/json', 'Idempotency-Key': intent.key }),
      body: JSON.stringify(intent.body) }, 20000, { submission: true })
    return validateReportReviewResult(await jobAwait(response, { signal, maxTransientErrors: 3 }), intent)
  } catch (error) {
    // Preserve identity even for rejection: the caller can reconcile the original receipt explicitly.
    error.submissionMayHaveSucceeded = true
    throw error
  }
}
