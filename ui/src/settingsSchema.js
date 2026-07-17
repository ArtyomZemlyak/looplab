import { getSettingsSchema } from './api.js'
import { deadlineRequest } from './requestDeadline.js'

export const SETTINGS_SCHEMA_VERSION = 1
const SETTINGS_SCHEMA_TIMEOUT_MS = 15_000
const FIELD_TYPES = new Set(['bool', 'enum', 'secret', 'int', 'float', 'list', 'text'])
const OPTIONAL_TEXT = ['help', 'placeholder', 'warning', 'warningTitle', 'warningTone']
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const schemaError = () => { throw new TypeError('Invalid settings schema') }
const boundedText = (value, maximum, allowEmpty = false) => {
  if (typeof value !== 'string' || (!allowEmpty && !value) || value.length > maximum) schemaError()
  return value
}
const ownKeys = value => Object.keys(value)

export function validateSettingsSchema(value) {
  if (!record(value) || value.schema !== SETTINGS_SCHEMA_VERSION
      || typeof value.revision !== 'string' || !/^[0-9a-f]{64}$/.test(value.revision)
      || !Array.isArray(value.groups) || !value.groups.length || value.groups.length > 32
      || !record(value.agent_role_pills)) schemaError()

  const roleNames = ownKeys(value.agent_role_pills)
  if (!roleNames.length || roleNames.length > 16) schemaError()
  const agentRolePills = value.agent_role_pills
  for (const name of roleNames) {
    const item = agentRolePills[name]
    if (!/^[a-z][a-z0-9_]{0,79}$/.test(name) || !record(item)) schemaError()
    boundedText(item.short, 12)
    boundedText(item.title, 500)
    Object.freeze(item)
  }

  const groupNames = new Set()
  const fieldByKey = Object.create(null)
  let fieldCount = 0
  value.groups.forEach(group => {
    if (!record(group)) schemaError()
    const title = boundedText(group.title, 200)
    boundedText(group.sub, 1_000, true)
    if (groupNames.has(title) || !Array.isArray(group.fields)
        || !group.fields.length || group.fields.length > 256) schemaError()
    groupNames.add(title)
    group.fields.forEach(field => {
      if (!record(field) || !/^[a-z][a-z0-9_]{0,119}$/.test(field.key)
          || Object.hasOwn(fieldByKey, field.key) || !FIELD_TYPES.has(field.type)) schemaError()
      boundedText(field.label, 500)
      for (const attribute of OPTIONAL_TEXT) {
        if (Object.hasOwn(field, attribute)) {
          boundedText(field[attribute], 16_000, true)
        }
      }
      if (Object.hasOwn(field, 'essential')) {
        if (typeof field.essential !== 'boolean') schemaError()
      }
      if (field.type === 'enum') {
        if (!Array.isArray(field.options) || !field.options.length || field.options.length > 64) {
          schemaError()
        }
        field.options.forEach(option => boundedText(option, 500, true))
        Object.freeze(field.options)
      } else if (Object.hasOwn(field, 'options')) {
        schemaError()
      }
      if (Object.hasOwn(field, 'agents')) {
        if (!Array.isArray(field.agents) || field.agents.length > roleNames.length
            || new Set(field.agents).size !== field.agents.length
            || field.agents.some(role => !Object.hasOwn(agentRolePills, role))) schemaError()
        Object.freeze(field.agents)
      }
      fieldByKey[field.key] = Object.freeze(field)
      fieldCount += 1
    })
    Object.freeze(group.fields)
    Object.freeze(group)
  })
  if (!fieldCount || fieldCount > 512) schemaError()
  value.groups = Object.freeze(value.groups)
  value.agentRolePills = Object.freeze(agentRolePills)
  value.fieldByKey = Object.freeze(fieldByKey)
  return Object.freeze(value)
}

export function createSettingsSchemaLoader(readSchema) {
  if (typeof readSchema !== 'function') throw new TypeError('readSchema must be a function')
  let cached = null
  let flight = null
  return Object.freeze({
    peek: () => cached,
    load: ({ reload = false } = {}) => {
      if (cached) return Promise.resolve(cached)
      if (flight) return flight
      let pending
      pending = Promise.resolve()
        .then(() => readSchema({ cache: reload ? 'reload' : 'default' }))
        .then(validateSettingsSchema)
        .then(value => { cached = value; return value })
        .finally(() => { if (flight === pending) flight = null })
      flight = pending
      return pending
    },
  })
}

const schemaLoader = createSettingsSchemaLoader(({ cache }) => {
  const timed = deadlineRequest(signal => getSettingsSchema({ signal, cache }),
    SETTINGS_SCHEMA_TIMEOUT_MS)
  return timed.promise
})

export const loadSettingsSchema = options => schemaLoader.load(options)
export const cachedSettingsSchema = () => schemaLoader.peek()

const fieldsFor = schema => {
  if (!record(schema?.fieldByKey)) throw new TypeError('Settings schema is unavailable')
  return schema.fieldByKey
}

// Invalid numeric input is deliberately a distinct non-serializable value. Blank is a valid explicit
// clear (`null`); junk, fractions in integer fields and non-finite numbers must never masquerade as
// that destructive operation while a controlled numeric text input is mid-edit.
export const INVALID_SETTING_VALUE = Symbol('invalid-setting-value')
const INTEGER_INPUT = /^[+-]?\d+(?:\.0+)?$/u
const DECIMAL_INPUT = /^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?$/u

export function parseSettingValue(field, raw) {
  if (field.type === 'bool') return { valid: true, value: !!raw, error: '' }
  if (raw == null || (typeof raw === 'string' && raw.trim() === '')) {
    return { valid: true, value: null, error: '' }
  }
  if (field.type === 'int') {
    const text = String(raw).trim()
    if (!INTEGER_INPUT.test(text)) {
      return { valid: false, value: INVALID_SETTING_VALUE,
        error: 'Enter a whole number using decimal digits (for example 2 or 2.0).' }
    }
    const value = Number(text)
    if (!Number.isFinite(value)) {
      return { valid: false, value: INVALID_SETTING_VALUE, error: 'Enter a finite whole number.' }
    }
    if (!Number.isInteger(value)) {
      return { valid: false, value: INVALID_SETTING_VALUE, error: 'Enter a whole number without decimals.' }
    }
    if (!Number.isSafeInteger(value)) {
      return { valid: false, value: INVALID_SETTING_VALUE, error: 'Enter a whole number within the safe range.' }
    }
    return { valid: true, value, error: '' }
  }
  if (field.type === 'float') {
    const text = String(raw).trim()
    if (!DECIMAL_INPUT.test(text)) {
      return { valid: false, value: INVALID_SETTING_VALUE,
        error: 'Enter a finite decimal number (scientific notation is allowed).' }
    }
    const value = Number(text)
    return Number.isFinite(value)
      ? { valid: true, value, error: '' }
      : { valid: false, value: INVALID_SETTING_VALUE, error: 'Enter a finite number.' }
  }
  if (field.type === 'list') {
    return { valid: true, value: String(raw).split(',').map(s => s.trim()).filter(Boolean), error: '' }
  }
  return { valid: true, value: raw, error: '' }   // text / enum
}

export function coerce(field, raw) {
  return parseSettingValue(field, raw).value
}

export function settingsValidationErrors(form, schema) {
  const errors = {}
  for (const [key, field] of Object.entries(fieldsFor(schema))) {
    if (field.type !== 'int' && field.type !== 'float') continue
    const result = parseSettingValue(field, form?.[key])
    if (!result.valid) errors[key] = result.error
  }
  return errors
}

// Turn a settings object into the form's editable shape (lists → comma string, null → '').
// `secret` fields are write-only: the API only ever returns the masked "***", never the value, so
// the input always starts BLANK (a non-empty edit means "set a new key" — see Settings.onSave).
export function toForm(settings, schema) {
  const out = {}
  for (const [k, f] of Object.entries(fieldsFor(schema))) {
    const v = settings?.[k]
    if (f.type === 'secret') out[k] = ''
    else if (f.type === 'bool') out[k] = !!v
    else if (f.type === 'list') out[k] = Array.isArray(v) ? v.join(', ') : (v ?? '')
    else out[k] = v == null ? '' : v
  }
  return out
}

// Turn the form shape back into a coerced settings object (for PUT /api/settings or run launch).
// `secret` fields are SKIPPED — they never travel in the settings payload (they go through the
// dedicated, owner-only secret endpoint instead), so a credential can't land in ui_settings.json.
export function fromForm(form, schema) {
  const out = {}
  for (const [k, f] of Object.entries(fieldsFor(schema))) {
    if (f.type === 'secret') continue
    out[k] = coerce(f, form[k])
  }
  return out
}

// The server applies this as a patch over its latest override document. With a baseline, send only
// fields edited since the last successful load/save: a stale tab must not replay old values over a
// newer tab's disjoint changes. `null` is deliberately retained as the explicit "clear override"
// operation. Omitting the baseline preserves the historical full-payload helper contract for compact
// consumers that construct a new run rather than patching the shared global defaults.
export function settingsSavePayload(form, agentControl, baselineForm, baselineAgentControl, schema) {
  const validationErrors = settingsValidationErrors(form || {}, schema)
  if (Object.keys(validationErrors).length) {
    const error = new Error('Fix invalid numeric settings before saving.')
    error.code = 'invalid_settings_form'
    error.validationErrors = validationErrors
    throw error
  }
  const current = fromForm(form || {}, schema)
  const out = {}
  if (baselineForm && typeof baselineForm === 'object' && !Array.isArray(baselineForm)) {
    const baseline = fromForm(baselineForm, schema)
    for (const [key, value] of Object.entries(current)) {
      if (JSON.stringify(value) !== JSON.stringify(baseline[key])) out[key] = value
    }
  } else {
    Object.assign(out, current)
  }

  const control = agentControl && typeof agentControl === 'object' && !Array.isArray(agentControl)
    ? agentControl
    : {}
  if (baselineAgentControl && typeof baselineAgentControl === 'object' && !Array.isArray(baselineAgentControl)) {
    const patch = {}
    const keys = new Set([...Object.keys(control), ...Object.keys(baselineAgentControl)])
    for (const key of keys) {
      const value = Object.hasOwn(control, key) ? control[key] : null
      if (JSON.stringify(value) !== JSON.stringify(baselineAgentControl[key] ?? null)) patch[key] = value
    }
    if (Object.keys(patch).length) out.agent_control = patch
  } else if (baselineForm == null) {
    out.agent_control = control
  } else if (Object.keys(control).length) {
    // A caller supplied a form baseline but no governance baseline: merge only the known current keys.
    out.agent_control = control
  }
  return out
}
