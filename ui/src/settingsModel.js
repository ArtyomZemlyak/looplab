// Pure view-model helpers for the settings UI. Keeping filtering here makes the
// progressive-disclosure rules testable without React or a browser.

export const ESSENTIAL_SETTING_KEYS = new Set([
  'profile',
  'policy',
  'max_nodes',
  'n_seeds',
  'max_parallel',
  'max_seconds',
  'max_eval_seconds',
  'timeout',
  'backend',
  'llm_model',
  'llm_base_url',
  'llm_api_key',
  'unified_agent',
  'agent_max_turns',
  'trust_mode',
  'require_approval',
  'redact_output',
])

const searchableText = (group, field) => [
  group.title,
  group.sub,
  field.key,
  field.label,
  field.help,
  field.placeholder,
  ...(field.options || []),
].filter(Boolean).join(' ').toLowerCase()   // locale-INVARIANT: toLocaleLowerCase() folds "I"→"ı"
                                            // in tr/az, so "API key" would stop matching a typed "api"

export function normalizeSettingsQuery(query) {
  return String(query || '').trim().toLowerCase()   // match searchableText: locale-invariant fold
}

// Search intentionally spans the complete catalogue even while the Essential
// view is selected. A search box that silently hides advanced matches is much
// harder to trust; clearing the query returns to the selected disclosure mode.
export function filterSettingsGroups(groups, {
  mode = 'all', query = '', only, hideSecret = false,
} = {}) {
  const needle = normalizeSettingsQuery(query)
  const allowedGroups = only ? new Set(only) : null

  return groups
    .filter(group => !allowedGroups || allowedGroups.has(group.title))
    .map(group => ({
      ...group,
      fields: group.fields.filter(field => {
        if (hideSecret && field.type === 'secret') return false
        if (needle) return searchableText(group, field).includes(needle)
        return mode !== 'essential' || ESSENTIAL_SETTING_KEYS.has(field.key)
      }),
    }))
    .filter(group => group.fields.length > 0)
}

export function settingsViewStats(groups) {
  return {
    groups: groups.length,
    fields: groups.reduce((total, group) => total + group.fields.length, 0),
    keys: new Set(groups.flatMap(group => group.fields.map(field => field.key))),
  }
}

// Per-run config stays a flat Settings object for compatibility. The server adds one reserved
// metadata member so this client can render event-log-owned launch semantics truthfully without a
// second request or a duplicated JavaScript field list.
export function splitRunConfigPayload(payload) {
  const record = payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : {}
  const config = { ...record }
  const rawMeta = config._looplab_config_meta
  delete config._looplab_config_meta
  const meta = rawMeta && typeof rawMeta === 'object' && !Array.isArray(rawMeta) ? rawMeta : {}
  const cleanNames = value => Array.isArray(value)
    ? [...new Set(value.filter(name => typeof name === 'string' && name.length > 0))]
    : []
  return {
    config,
    pinnedFields: new Set(cleanNames(meta.run_start_pinned_fields)),
    mismatchFields: cleanNames(meta.snapshot_mismatch_fields),
  }
}

const sameSettingValue = (left, right) => JSON.stringify(left) === JSON.stringify(right)

// Rebase an authoritative save response onto the edits made while that request was in flight.
// Fields that still equal the submitted snapshot accept the server value (including deletion or
// canonicalisation); fields changed after submit keep the user's newer local value. The helper is
// deliberately record-agnostic so the same rule covers both settings and agent-control matrices.
export function reconcileAcceptedRecord(current, submitted, accepted) {
  const currentRecord = current && typeof current === 'object' && !Array.isArray(current) ? current : {}
  const submittedRecord = submitted && typeof submitted === 'object' && !Array.isArray(submitted) ? submitted : {}
  const acceptedRecord = accepted && typeof accepted === 'object' && !Array.isArray(accepted) ? accepted : {}
  const reconciled = { ...acceptedRecord }
  const localKeys = new Set([...Object.keys(currentRecord), ...Object.keys(submittedRecord)])

  for (const key of localKeys) {
    if (sameSettingValue(currentRecord[key], submittedRecord[key])) continue
    if (Object.hasOwn(currentRecord, key)) reconciled[key] = currentRecord[key]
    else delete reconciled[key]
  }
  return reconciled
}
