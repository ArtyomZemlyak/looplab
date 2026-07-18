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

const settingsRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const settingKey = value => typeof value === 'string' && /^[a-z][a-z0-9_]{0,119}$/.test(value)
const opaqueRevision = value => typeof value === 'string' && value.length > 0 && value.length <= 256
  && !/[\u0000-\u001f\u007f]/.test(value)
const configRevision = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value)
const resourceError = () => { throw new TypeError('Invalid settings resource') }

function boundedJson(value, budget = { nodes: 0 }, depth = 0) {
  budget.nodes += 1
  if (budget.nodes > 8192 || depth > 8) resourceError()
  if (value == null || typeof value === 'boolean') return
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value))) {
      resourceError()
    }
    return
  }
  if (typeof value === 'string') {
    if (value.length > 1_000_000 || /[\u0000]/.test(value)) resourceError()
    return
  }
  if (Array.isArray(value)) {
    if (value.length > 4096) resourceError()
    value.forEach(item => boundedJson(item, budget, depth + 1))
    return
  }
  if (!settingsRecord(value) || Object.keys(value).length > 512) resourceError()
  for (const [key, item] of Object.entries(value)) {
    if (key.length > 200) resourceError()
    boundedJson(item, budget, depth + 1)
  }
}

function validateAgentControl(value, schema) {
  if (!settingsRecord(value) || Object.keys(value).length > 512) resourceError()
  const roles = new Set(Object.keys(schema.agentRolePills || {}))
  for (const [key, granted] of Object.entries(value)) {
    if (!settingKey(key) || !Array.isArray(granted) || granted.length > roles.size
        || new Set(granted).size !== granted.length
        || granted.some(role => typeof role !== 'string' || !roles.has(role))) resourceError()
  }
}

function validateKnownSetting(field, value) {
  if (value == null) {
    if (!field.nullable && field.type !== 'secret') resourceError()
    return
  }
  if (field.type === 'bool') {
    if (typeof value !== 'boolean') resourceError()
  } else if (field.type === 'int') {
    if (!Number.isSafeInteger(value)) resourceError()
  } else if (field.type === 'float') {
    if (typeof value !== 'number' || !Number.isFinite(value)) resourceError()
  } else if (field.type === 'enum') {
    if (typeof value !== 'string' || !field.options.includes(value)) resourceError()
  } else if (field.type === 'list') {
    if (!Array.isArray(value) || value.length > 4096
        || value.some(item => typeof item !== 'string' || item.length > 16_000)) resourceError()
  } else if (field.type === 'secret') {
    if (value !== '***') resourceError()
  } else if (typeof value !== 'string' || value.length > 1_000_000 || /[\u0000]/.test(value)) {
    resourceError()
  }
}

export function validateSettingsRecord(value, schema, {
  complete = false, allowMissingSecret = false, allowEmpty = false,
} = {}) {
  if (!settingsRecord(value) || Object.keys(value).length > 512
      || (!allowEmpty && Object.keys(value).length === 0)) resourceError()
  let known = 0
  for (const [key, item] of Object.entries(value)) {
    if (!settingKey(key)) resourceError()
    if (key === 'agent_control') {
      validateAgentControl(item, schema)
    } else if (Object.hasOwn(schema.fieldByKey, key)) {
      known += 1
      validateKnownSetting(schema.fieldByKey[key], item)
    } else {
      boundedJson(item)
    }
  }
  if (!known && !allowEmpty) resourceError()
  if (complete) {
    for (const field of Object.values(schema.fieldByKey)) {
      if (allowMissingSecret && field.type === 'secret') continue
      if (!Object.hasOwn(value, field.key)) resourceError()
    }
  }
  return value
}

export function validateSettingsResource(value, schema) {
  if (!settingsRecord(value) || Object.keys(value).length > 16
      || !opaqueRevision(value.settings_revision) || !opaqueRevision(value.secret_revision)) {
    resourceError()
  }
  validateSettingsRecord(value.settings, schema, { complete: true })
  validateSettingsRecord(value.defaults, schema, { complete: true, allowMissingSecret: true })
  validateSettingsRecord(value.overrides, schema, { allowEmpty: true })
  return value
}

export function validateSettingsSaveAck(value, schema) {
  if (!settingsRecord(value) || value.ok !== true || Object.keys(value).length > 16
      || !opaqueRevision(value.settings_revision)) resourceError()
  validateSettingsRecord(value.settings, schema, { complete: true })
  validateSettingsRecord(value.overrides, schema, { allowEmpty: true })
  return value
}

export function validateSecretSaveAck(value, expectedKey) {
  if (!settingsRecord(value) || value.ok !== true || value.key !== expectedKey
      || typeof value.set !== 'boolean' || !opaqueRevision(value.secret_revision)
      || Object.keys(value).length > 8) resourceError()
  return value
}

// Per-run config stays a flat Settings object for compatibility. The server adds one reserved
// metadata member so this client can render event-log-owned launch semantics truthfully without a
// second request or a duplicated JavaScript field list.
export function splitRunConfigPayload(payload, schema = null) {
  if (schema && (!settingsRecord(payload) || !settingsRecord(payload._looplab_config_meta))) {
    resourceError()
  }
  const record = settingsRecord(payload) ? payload : {}
  const config = { ...record }
  const rawMeta = config._looplab_config_meta
  delete config._looplab_config_meta
  const meta = rawMeta && typeof rawMeta === 'object' && !Array.isArray(rawMeta) ? rawMeta : {}
  const metaKeys = new Set([
    'config_revision', 'run_start_pinned_fields', 'snapshot_mismatch_fields',
    'run_read_only_fields',
  ])
  if (schema && (Object.keys(meta).length > metaKeys.size
      || Object.keys(meta).some(key => !metaKeys.has(key))
      || !configRevision(meta.config_revision))) resourceError()
  const cleanNames = value => {
    if (schema && (!Array.isArray(value) || value.length > 512
        || value.some(name => !settingKey(name)) || new Set(value).size !== value.length)) {
      resourceError()
    }
    return Array.isArray(value)
      ? [...new Set(value.filter(name => typeof name === 'string' && name.length > 0))]
      : []
  }
  if (schema) validateSettingsRecord(config, schema)
  const pinnedFields = new Set(cleanNames(meta.run_start_pinned_fields))
  const readOnlyFields = new Set([
    ...pinnedFields, ...cleanNames(meta.run_read_only_fields),
  ])
  return {
    config,
    configRevision: configRevision(meta.config_revision) ? meta.config_revision : '',
    pinnedFields,
    readOnlyFields,
    mismatchFields: cleanNames(meta.snapshot_mismatch_fields),
  }
}

export function validateRunConfigSaveAck(value, schema) {
  if (!settingsRecord(value) || value.ok !== true || typeof value.engine_running !== 'boolean'
      || typeof value.trust_gate_event_appended !== 'boolean'
      || !Array.isArray(value.changed) || !Array.isArray(value.normalized_pinned)
      || Object.keys(value).length > 16) resourceError()
  for (const names of [value.changed, value.normalized_pinned]) {
    if (names.length > 512 || names.some(name => !settingKey(name))
        || new Set(names).size !== names.length) resourceError()
  }
  splitRunConfigPayload(value.config, schema)
  return value
}

// A config write may durably replace the snapshot before the server repairs its trust-gate event.
// Only the coded CAS conflict and fail-closed lock error prove that this request wrote nothing;
// unclassified 409/5xx outcomes require an authoritative read and must never be replayed blindly.
export function runConfigWriteDisposition(error) {
  if (error?.status === 409 && error?.code === 'run_config_revision_conflict') return 'conflict'
  if (error?.status === 503 && error?.code === 'run_config_lock_unavailable') return 'rejected'
  if (!Number.isInteger(error?.status) || error.status === 409 || error.status >= 500) return 'unknown'
  return 'rejected'
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

// Reconcile a read performed after a write whose outcome is unknown. Unlike an ACK, an
// authoritative read may still show the pre-write value while an already-delivered request is
// finishing. Keep a submitted value as a visible draft until the server actually reflects it;
// still preserve edits made after submit and accept unrelated authoritative server fields.
export function reconcileUnknownRecord(current, submitted, authoritative, uncertainKeys = []) {
  const currentRecord = current && typeof current === 'object' && !Array.isArray(current) ? current : {}
  const submittedRecord = submitted && typeof submitted === 'object' && !Array.isArray(submitted) ? submitted : {}
  const authoritativeRecord = authoritative && typeof authoritative === 'object'
    && !Array.isArray(authoritative) ? authoritative : {}
  const reconciled = { ...authoritativeRecord }
  const uncertain = new Set(uncertainKeys)
  const localKeys = new Set([
    ...Object.keys(currentRecord), ...Object.keys(submittedRecord), ...uncertain,
  ])

  for (const key of localKeys) {
    const editedAfterSubmit = !sameSettingValue(currentRecord[key], submittedRecord[key])
    const serverReflectsSubmit = sameSettingValue(authoritativeRecord[key], submittedRecord[key])
    if (!editedAfterSubmit && (serverReflectsSubmit || !uncertain.has(key))) continue
    if (Object.hasOwn(currentRecord, key)) reconciled[key] = currentRecord[key]
    else delete reconciled[key]
  }
  return reconciled
}
