const TEXT_LIMIT = 500
const KNOWN_RISKS = new Set(['READ', 'REVERSIBLE', 'CONSEQUENTIAL', 'HIGH', 'UNKNOWN'])
const REMEMBERED_GRANT_MODES = new Set(['default', 'acceptEdits', 'auto'])
// Digests participate in the exact server identity but are redundant beside the card's one canonical
// scope ID. `verb` repeats the title/preview. Hiding both keeps the human review scope scannable.
const HIDDEN_SCOPE_KEY = /token|secret|credential|password|content|preview|verb|_digest$/i

const safeText = value => {
  const text = typeof value === 'string' ? value
    : typeof value === 'number' && Number.isFinite(value) ? String(value)
      : typeof value === 'boolean' ? String(value) : ''
  const cleaned = text.replace(/[\u0000-\u001f\u007f]/g, ' ').trim()
  return cleaned.length > TEXT_LIMIT ? cleaned.slice(0, TEXT_LIMIT - 1) + '…' : cleaned
}

const firstText = (...values) => {
  for (const value of values) {
    const direct = safeText(value)
    if (direct) return direct
    if (Array.isArray(value)) {
      const joined = value.map(safeText).filter(Boolean).slice(0, 8).join(', ')
      if (joined) return joined.slice(0, TEXT_LIMIT)
    }
  }
  return ''
}

const scopeText = value => {
  const plain = firstText(value)
  if (plain) return plain
  if (!value || typeof value !== 'object' || Array.isArray(value)) return ''
  return Object.entries(value).filter(([key]) => !HIDDEN_SCOPE_KEY.test(key)).slice(0, 10)
    .map(([key, item]) => {
      const rendered = firstText(item)
      const label = safeText(key).replaceAll('_', ' ')
      return rendered ? `${label}: ${rendered}` : ''
    }).filter(Boolean).join(' · ')
}

const expiryMillis = (req, action) => {
  const raw = req?.request_expires_at ?? req?.expires_at ?? req?.expiresAt
    ?? action?.request_expires_at ?? action?.expires_at ?? action?.expiresAt
  if (typeof raw === 'number' && Number.isFinite(raw) && raw > 0) {
    return raw < 1e12 ? raw * 1000 : raw
  }
  if (typeof raw === 'string' && raw.trim()) {
    const parsed = Date.parse(raw)
    if (Number.isFinite(parsed)) return parsed
  }
  const created = Number(req?.created)
  const ttl = Number(req?.ttl_seconds ?? req?.ttlSeconds)
  return Number.isFinite(created) && created > 0 && Number.isFinite(ttl) && ttl > 0
    ? (created + ttl) * 1000 : null
}

const inferredScope = action => {
  const path = safeText(action.path)
  const cwd = safeText(action.cwd)
  if (path) return `File: ${path}`
  if (cwd) return `Working directory: ${cwd}`
  if (action.tool_kind === 'run_control') return 'The run-control target shown in the preview'
  if (action.tool_kind) return `This ${safeText(action.tool_kind)} action only`
  return 'Not specified by the server — review the preview before deciding'
}

const inferredConsequence = action => {
  const tool = safeText(action.tool)
  if (tool === 'delete_file') return 'Deletes the named file from disk.'
  if (tool === 'revert_file') return 'Overwrites the named file with its previous snapshot.'
  if (tool === 'write_file') return 'Creates or overwrites the named file.'
  if (tool === 'edit_file' || tool === 'apply_patch') return 'Changes workspace file contents.'
  if (tool === 'kill_background') return 'Stops the named background process.'
  if (action.tool_kind === 'shell') return 'Runs a command that may change files, processes, or external systems.'
  if (action.tool_kind === 'run_control') return 'Changes the state of the named run.'
  return 'Not specified by the server — reject unless the action and preview are fully understood.'
}

const durationLabel = seconds => seconds % 60 === 0
  ? `${seconds / 60} min` : `${seconds} sec`

export function permissionPresentation(req, now = Date.now()) {
  const action = req?.action && typeof req.action === 'object' && !Array.isArray(req.action)
    ? req.action : {}
  const rawRisk = firstText(
    action.risk_level, action.riskLevel, action.risk,
    req?.risk_level, req?.riskLevel, req?.risk,
  ).toUpperCase()
  const risk = KNOWN_RISKS.has(rawRisk) ? rawRisk : 'UNKNOWN'
  const toolKind = safeText(action.tool_kind || action.toolKind)
  const candidateExpiry = expiryMillis(req, action)
  const expiryDate = candidateExpiry == null ? null : new Date(candidateExpiry)
  const expiresMs = expiryDate && Number.isFinite(expiryDate.getTime()) ? expiryDate.getTime() : null
  const rememberable = (action.rememberable ?? req?.rememberable) === true
  const scopeDigest = safeText(action.scope_digest || req?.scope_digest)
  const actionId = safeText(action.action_id || req?.action_id)
  const grantTtl = Number(req?.grant_ttl_seconds ?? action.grant_ttl_seconds)
  const grantTtlSeconds = Number.isSafeInteger(grantTtl) && grantTtl > 0 && grantTtl <= 600
    ? grantTtl : 0
  const expired = expiresMs != null && expiresMs <= now
  const hasDurableScopeIdentity = !!actionId && /^[0-9a-f]{64}$/.test(scopeDigest)
  const mode = firstText(req?.mode) || 'unknown'
  return {
    action,
    actionId,
    risk,
    mode,
    scope: safeText(scopeText(action.scope) || scopeText(req?.scope) || inferredScope(action)),
    scopeDigest: /^[0-9a-f]{64}$/.test(scopeDigest) ? scopeDigest : '',
    consequence: firstText(action.consequence, req?.consequence) || inferredConsequence(action),
    expiresMs,
    expiresIso: expiresMs == null ? '' : expiryDate.toISOString(),
    expiryLabel: expiresMs == null
      ? 'Exact expiry unavailable; the server timeout still applies.'
      : expiryDate.toLocaleString(),
    expired,
    canAlways: rememberable && risk !== 'HIGH' && risk !== 'UNKNOWN' && !!toolKind
      && REMEMBERED_GRANT_MODES.has(mode) && hasDurableScopeIdentity
      && expiresMs != null && !expired && grantTtlSeconds > 0,
    grantTtlSeconds,
    grantDurationLabel: grantTtlSeconds ? durationLabel(grantTtlSeconds) : '',
  }
}

// Reconcile a server permissions snapshot against ids the user just resolved LOCALLY, so a lagging
// /permissions poll can't resurrect a card the user already approved/rejected (which would re-enable its
// buttons and allow a contradictory second decision). Self-healing: an id the server has already dropped
// is pruned from `resolvedSet` (mutated in place), so it never grows unbounded and a legitimately
// re-issued id is not hidden forever. Pure aside from the intended set pruning.
export function reconcilePendingPermissions(serverPending, resolvedSet) {
  const list = Array.isArray(serverPending) ? serverPending : []
  const serverIds = new Set(list.map(req => req && req.id))
  for (const rid of [...resolvedSet]) if (!serverIds.has(rid)) resolvedSet.delete(rid)
  return list.filter(req => req && !resolvedSet.has(req.id))
}
