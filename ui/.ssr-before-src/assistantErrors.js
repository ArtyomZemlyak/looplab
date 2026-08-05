const KNOWN_KINDS = new Set(['rate_limit', 'credentials', 'unavailable', 'provider_error'])

function infoFor(kind, status = null) {
  if (kind === 'rate_limit') return {
    kind, status: status || 429,
    title: 'Assistant is temporarily rate-limited',
    message: 'The model provider is busy. Retry shortly or choose another provider in Settings.',
    retryable: true, technical: `HTTP ${status || 429} · rate_limit`,
  }
  if (kind === 'credentials') return {
    kind, status,
    title: 'Assistant credentials need attention',
    message: 'Check the provider and API key in Settings, then retry.',
    retryable: false, technical: status ? `HTTP ${status} · credentials` : 'credentials',
  }
  if (kind === 'unavailable') return {
    kind, status,
    title: 'Assistant could not reach the model provider',
    message: 'Check the connection and retry. Your message is still available.',
    retryable: true, technical: status ? `HTTP ${status} · unavailable` : 'provider_unavailable',
  }
  return {
    kind: 'provider_error', status,
    title: 'Assistant request failed',
    message: 'The model provider returned an error. Retry or review the provider settings.',
    retryable: true, technical: status ? `HTTP ${status} · provider_error` : 'provider_error',
  }
}

export function assistantErrorInfo(value, hintedKind = null) {
  const raw = String(value || '')
  if (KNOWN_KINDS.has(hintedKind)) return infoFor(hintedKind)
  // Legacy persisted failures predate error_kind. Keep this STRICTLY start-anchored to exception-like
  // shapes so ordinary assistant prose is never replaced by a provider card. In particular do NOT match
  // a bare "Error:" prefix or unanchored substrings like "LLM request" / "provider returned error" /
  // "error code: NNN" / "temporarily rate-limited": those occur in normal ML/LLM answers and hid them.
  if (!/^\s*(?:\(?assistant error\s*:|couldn['’]t reach the model\s*\(|authenticationerror\b|\d{3}\s+client error\b|http\s+\d{3}\b)/i.test(raw)) return null
  const status = Number(raw.match(/(?:error code\s*:\s*|\bcode['"]?\s*:\s*|\bhttp\s+|^\s*)(\d{3})/i)?.[1] || 0) || null
  if (status === 429 || /rate[- _]limit/i.test(raw)) return infoFor('rate_limit', status)
  if (status === 401 || status === 403 || /authenticationerror|api key|unauthori[sz]ed|credential/i.test(raw)) return infoFor('credentials', status)
  if (/timeout|timed out|network|connection|unreachable|couldn['’]t reach/i.test(raw)) return infoFor('unavailable', status)
  return infoFor('provider_error', status)
}

export function assistantPreview(value) {
  return assistantErrorInfo(value)?.title || String(value || '')
}
