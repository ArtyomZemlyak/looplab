import { apiPrefix } from './api.js'
import { attentionIdValid } from './attentionModel.js'

export const ATTENTION_STATE_VERSION = 1
// Keep enough causal identities for the documented 5k-run owner workspace while remaining well
// below typical per-origin storage quotas even when all three bounded fields are populated.
export const ATTENTION_MAX_IDS = 8192
const MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000
const STATE_KEYS = new Set(['v', 'enabled', 'armedAt', 'acknowledged', 'dismissed', 'notified'])
const ID_FIELDS = ['acknowledged', 'dismissed', 'notified']

const defaultState = () => ({
  v: ATTENTION_STATE_VERSION,
  enabled: false,
  armedAt: 0,
  acknowledged: [],
  dismissed: [],
  notified: [],
})

export function attentionStorageKey(prefix = apiPrefix()) {
  return `ll.attention.v1:${encodeURIComponent(prefix || '/')}`
}

const validTimestamp = value => Number.isSafeInteger(value) && value >= 0
  && value <= Number.MAX_SAFE_INTEGER

const normalizePairs = (value, now) => {
  if (!Array.isArray(value) || value.length > ATTENTION_MAX_IDS) return null
  const byId = new Map()
  for (const row of value) {
    if (!Array.isArray(row) || row.length !== 2 || !attentionIdValid(row[0])
        || !validTimestamp(row[1])) return null
    if (row[1] >= now - MAX_AGE_MS && row[1] <= now + 60_000) byId.set(row[0], row[1])
  }
  return [...byId.entries()].sort((left, right) => right[1] - left[1])
    .slice(0, ATTENTION_MAX_IDS)
}

export function parseAttentionState(raw, now = Date.now()) {
  if (raw == null || raw === '') return { state: defaultState(), valid: true }
  let parsed
  try { parsed = JSON.parse(raw) } catch { return { state: defaultState(), valid: false } }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)
      || Object.keys(parsed).some(key => !STATE_KEYS.has(key))
      || parsed.v !== ATTENTION_STATE_VERSION || typeof parsed.enabled !== 'boolean'
      || !validTimestamp(parsed.armedAt) || (parsed.enabled && parsed.armedAt <= 0)) {
    return { state: defaultState(), valid: false }
  }
  const next = { v: ATTENTION_STATE_VERSION, enabled: parsed.enabled, armedAt: parsed.armedAt }
  for (const field of ID_FIELDS) {
    const rows = normalizePairs(parsed[field], now)
    if (!rows) return { state: defaultState(), valid: false }
    next[field] = rows
  }
  return { state: next, valid: true }
}

const storageTarget = storage => {
  if (storage !== undefined) return storage
  try { return typeof localStorage === 'undefined' ? null : localStorage } catch { return null }
}

export function loadAttentionState(storage = undefined, prefix = apiPrefix(), now = Date.now()) {
  const target = storageTarget(storage)
  if (!target) return { state: defaultState(), available: false, valid: false }
  try {
    const parsed = parseAttentionState(target.getItem(attentionStorageKey(prefix)), now)
    return { ...parsed, available: true }
  } catch { return { state: defaultState(), available: false, valid: false } }
}

export function saveAttentionState(state, storage = undefined, prefix = apiPrefix(), now = Date.now()) {
  const target = storageTarget(storage)
  if (!target) return false
  const normalized = parseAttentionState(JSON.stringify(state), now)
  if (!normalized.valid) return false
  const text = JSON.stringify(normalized.state)
  try {
    const key = attentionStorageKey(prefix)
    target.setItem(key, text)
    return target.getItem(key) === text
  } catch { return false }
}

export function recordAttentionIds(state, field, ids, now = Date.now()) {
  if (!ID_FIELDS.includes(field)) return state
  const next = new Map(Array.isArray(state?.[field]) ? state[field] : [])
  for (const id of ids || []) if (attentionIdValid(id)) next.set(id, now)
  const rows = [...next.entries()].filter(([, timestamp]) => timestamp >= now - MAX_AGE_MS)
    .sort((left, right) => right[1] - left[1]).slice(0, ATTENTION_MAX_IDS)
  return { ...defaultState(), ...state, [field]: rows }
}

export const attentionIds = (state, field) => new Set(
  Array.isArray(state?.[field]) ? state[field].map(row => row[0]).filter(attentionIdValid) : [],
)

export function notificationsDisabledState(state = defaultState()) {
  return { ...state, enabled: false, armedAt: 0 }
}
