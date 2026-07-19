import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  INVALID_SETTING_VALUE, coerce, parseSettingValue,
  settingsValidationErrors,
} from '../src/settingsSchema.js'
import { FIELD_BY_KEY, SETTINGS_GROUPS, SETTINGS_SCHEMA } from './settingsSchemaFixture.js'
import {
  ESSENTIAL_SETTING_KEYS,
  filterSettingsGroups,
  normalizeSettingsQuery,
  reconcileAcceptedRecord,
  reconcileUnknownRecord,
  runConfigWriteDisposition,
  splitRunConfigPayload,
  settingsViewStats,
  validateRunConfigSaveAck,
  validateSecretSaveAck,
  validateSettingsResource,
  validateSettingsSaveAck,
} from '../src/settingsModel.js'

test('every curated essential key exists in the settings schema', () => {
  for (const key of ESSENTIAL_SETTING_KEYS) assert.ok(FIELD_BY_KEY[key], `unknown essential key: ${key}`)
})

test('essential mode keeps a compact cross-section of the catalogue', () => {
  const groups = filterSettingsGroups(SETTINGS_GROUPS, { mode: 'essential' })
  const stats = settingsViewStats(groups)
  assert.equal(stats.fields, ESSENTIAL_SETTING_KEYS.size)
  assert.ok(stats.groups > 1)
  assert.ok(stats.keys.has('max_nodes'))
  assert.ok(stats.keys.has('llm_model'))
  assert.ok(!stats.keys.has('proxy_kill_fraction'))
})

test('search is normalized and spans advanced settings from Essential mode', () => {
  assert.equal(normalizeSettingsQuery('  PATCH Gate  '), 'patch gate')
  const groups = filterSettingsGroups(SETTINGS_GROUPS, { mode: 'essential', query: 'patch gate' })
  const stats = settingsViewStats(groups)
  assert.ok(stats.keys.has('agent_patch_gate'))
  assert.equal(stats.groups, 1)
})

test('search covers labels, keys, help, and enum options', () => {
  const byKey = settingsViewStats(filterSettingsGroups(SETTINGS_GROUPS, { query: 'max_eval_seconds' })).keys
  const byOption = settingsViewStats(filterSettingsGroups(SETTINGS_GROUPS, { query: 'hostile' })).keys
  const byHelp = settingsViewStats(filterSettingsGroups(SETTINGS_GROUPS, { query: 'corporate proxy' })).keys
  assert.ok(byKey.has('max_eval_seconds'))
  assert.ok(byOption.has('trust_mode'))
  assert.ok(byHelp.has('llm_trust_env'))
})

test('group and secret restrictions remain available to compact consumers', () => {
  const groups = filterSettingsGroups(SETTINGS_GROUPS, {
    only: ['LLM'], hideSecret: true,
  })
  const stats = settingsViewStats(groups)
  assert.equal(groups.length, 1)
  assert.equal(groups[0].title, 'LLM')
  assert.ok(!stats.keys.has('llm_api_key'))
  assert.ok(stats.keys.has('llm_model'))
})

test('numeric settings distinguish invalid input from a deliberate blank clear', () => {
  assert.equal(coerce({ type: 'int' }, '2abc'), INVALID_SETTING_VALUE)
  assert.equal(coerce({ type: 'float' }, '1.5seconds'), INVALID_SETTING_VALUE)
  assert.equal(coerce({ type: 'int' }, '2.5'), INVALID_SETTING_VALUE)
  assert.equal(coerce({ type: 'int' }, '   '), null)
  assert.equal(coerce({ type: 'float' }, '   '), null)
  assert.equal(coerce({ type: 'int' }, '2.0'), 2)
  assert.equal(coerce({ type: 'float' }, '1.5'), 1.5)
  assert.equal(coerce({ type: 'float' }, '-2.5E-2'), -0.025)
  assert.equal(coerce({ type: 'float' }, '1e3'), 1000)
  for (const raw of ['-', '1e', '0x10', '0b10']) {
    assert.equal(coerce({ type: 'int' }, raw), INVALID_SETTING_VALUE)
    assert.equal(coerce({ type: 'float' }, raw), INVALID_SETTING_VALUE)
  }
  assert.deepEqual(parseSettingValue({ type: 'int' }, ''), { valid: true, value: null, error: '' })
  assert.match(parseSettingValue({ type: 'int' }, '2.5').error, /whole number/i)
  assert.match(parseSettingValue({ type: 'float' }, '1e309').error, /finite number/i)
  assert.match(settingsValidationErrors({ max_nodes: '2.5' }, SETTINGS_SCHEMA).max_nodes,
    /whole number/i)
  assert.match(parseSettingValue(FIELD_BY_KEY.max_nodes, '0').error, /at least 1/i)
  assert.match(parseSettingValue(FIELD_BY_KEY.max_nodes, '1000001').error, /at most 1000000/i)
  assert.match(parseSettingValue(FIELD_BY_KEY.timeout, '0').error, /greater than 0/i)
  assert.match(parseSettingValue(FIELD_BY_KEY.holdout_fraction, '0.91').error, /at most 0\.9/i)
  assert.deepEqual(parseSettingValue(FIELD_BY_KEY.select_verifier_samples, '32'),
    { valid: true, value: 32, error: '' })
  assert.match(parseSettingValue(FIELD_BY_KEY.max_nodes, '', { allowClear: false }).error,
    /required/i)
  assert.deepEqual(parseSettingValue(FIELD_BY_KEY.max_seconds, '', { allowClear: false }),
    { valid: true, value: null, error: '' })
})

test('validation catches a blanked REQUIRED non-numeric field, not just int/float', () => {
  // A blank required enum/text under a run-config edit (allowClear:false) must be flagged. Before the fix
  // settingsValidationErrors only checked int/float, so the field silently coerced to INVALID_SETTING_VALUE
  // and JSON.stringify dropped it from the payload — a silent data loss under a "saved" toast.
  assert.equal(FIELD_BY_KEY.policy.type, 'enum')
  assert.notEqual(FIELD_BY_KEY.policy.nullable, true)                        // policy is required
  // A valid value has no error; blanking it under allowClear:false does (checked per-key, since a partial
  // form leaves other required fields blank too — the real editor always carries the full field set).
  assert.equal(
    settingsValidationErrors({ policy: 'mcts' }, SETTINGS_SCHEMA, { allowClear: false }).policy, undefined)
  assert.match(
    settingsValidationErrors({ policy: '' }, SETTINGS_SCHEMA, { allowClear: false }).policy, /required/i)
  // fromForm would indeed have produced the payload-dropping symbol for that blank.
  assert.equal(coerce(FIELD_BY_KEY.policy, '', { allowClear: false }), INVALID_SETTING_VALUE)
  // Clearing a GLOBAL override (allowClear defaults true) stays valid — no false error on that path.
  assert.equal(settingsValidationErrors({ policy: '' }, SETTINGS_SCHEMA).policy, undefined)
  // A genuinely nullable field left blank is still allowed even under allowClear:false.
  assert.equal(
    settingsValidationErrors({ memory_dir: '' }, SETTINGS_SCHEMA, { allowClear: false }).memory_dir,
    undefined)
})

test('accepted save records rebase without erasing edits made after submit', () => {
  const submitted = { max_nodes: '08', policy: 'greedy', removed: 'old', roles: ['boss'] }
  const current = { ...submitted, policy: 'mcts', roles: ['boss', 'strategist'], localOnly: true }
  const accepted = { max_nodes: 8, policy: 'greedy', serverOnly: 'fresh', roles: ['boss'] }

  assert.deepEqual(reconcileAcceptedRecord(current, submitted, accepted), {
    max_nodes: 8,
    policy: 'mcts',
    serverOnly: 'fresh',
    roles: ['boss', 'strategist'],
    localOnly: true,
  })
})

test('accepted save records honor a local deletion made after submit', () => {
  assert.deepEqual(
    reconcileAcceptedRecord({}, { agent_control: { timeout: ['boss'] } }, {
      agent_control: { timeout: ['boss'] }, server_revision: 4,
    }),
    { server_revision: 4 },
  )
})

test('unknown save recovery keeps an unreflected draft and accepts it only when observed', () => {
  const submitted = { max_nodes: 8, policy: 'greedy', serverOnly: 'old' }
  const current = { ...submitted, policy: 'mcts' }

  assert.deepEqual(reconcileUnknownRecord(current, submitted, {
    max_nodes: 4, policy: 'greedy', serverOnly: 'fresh', concurrent: true,
  }, ['max_nodes']), {
    max_nodes: 8,
    policy: 'mcts',
    serverOnly: 'fresh',
    concurrent: true,
  })
  assert.deepEqual(reconcileUnknownRecord(current, submitted, {
    max_nodes: 8, policy: 'greedy', serverOnly: 'old', concurrent: true,
  }, ['max_nodes']), {
    max_nodes: 8,
    policy: 'mcts',
    serverOnly: 'old',
    concurrent: true,
  })
  assert.deepEqual(reconcileUnknownRecord({}, {}, { timeout: ['strategist'] }, ['timeout']), {},
    'an uncertain explicit deletion remains a visible local deletion when the server still has it')
})

test('run-config write disposition fails closed around partial-write server errors', () => {
  assert.equal(runConfigWriteDisposition({ status: 409, code: 'run_config_revision_conflict' }),
    'conflict')
  assert.equal(runConfigWriteDisposition({ status: 503, code: 'run_config_lock_unavailable' }),
    'rejected')
  for (const error of [new Error('offline'), { status: 409 }, { status: 500 },
    { status: 503, code: 'different_failure' }]) {
    assert.equal(runConfigWriteDisposition(error), 'unknown')
  }
  for (const error of [{ status: 400 }, { status: 404 }, { status: 422 }]) {
    assert.equal(runConfigWriteDisposition(error), 'rejected')
  }
})

test('per-run config metadata is separated from the flat config and exposes launch pins', () => {
  const parsed = splitRunConfigPayload({
    timeout: 40,
    holdout_fraction: 0.4,
    _looplab_config_meta: {
      config_revision: 'a'.repeat(64),
      run_start_pinned_fields: ['holdout_fraction', 'holdout_fraction', 'select_verifier'],
      snapshot_mismatch_fields: ['holdout_fraction'],
      run_read_only_fields: ['profile'],
    },
  })
  assert.deepEqual(parsed.config, { timeout: 40, holdout_fraction: 0.4 })
  assert.equal(parsed.configRevision, 'a'.repeat(64))
  assert.deepEqual([...parsed.pinnedFields], ['holdout_fraction', 'select_verifier'])
  assert.deepEqual([...parsed.readOnlyFields], ['holdout_fraction', 'select_verifier', 'profile'])
  assert.deepEqual(parsed.mismatchFields, ['holdout_fraction'])
})

const validSettingValue = field => {
  if (field.type === 'bool') return false
  if (field.type === 'enum') return field.options[0]
  if (field.type === 'secret') return null
  if (field.type === 'int') return field.minimum ?? (field.exclusiveMinimum ?? 0) + 1
  if (field.type === 'float') return field.minimum ?? (field.exclusiveMinimum ?? 0) + 0.5
  if (field.type === 'list') return []
  return field.nullable ? null : ''
}

const completeSettingsRecord = ({ includeSecret = true } = {}) => Object.fromEntries(
  Object.values(SETTINGS_SCHEMA.fieldByKey)
    .filter(field => includeSecret || field.type !== 'secret')
    .map(field => [field.key, validSettingValue(field)]),
)

test('settings and run-config resources reject malformed HTTP-200 envelopes', () => {
  const settings = completeSettingsRecord()
  const defaults = completeSettingsRecord({ includeSecret: false })
  const resource = { settings, defaults, overrides: {},
    settings_revision: 'settings-r1', secret_revision: 'secret-r1' }
  assert.equal(validateSettingsResource(resource, SETTINGS_SCHEMA), resource)
  assert.equal(validateSettingsSaveAck({ ok: true, settings, overrides: {},
    settings_revision: 'settings-r2' }, SETTINGS_SCHEMA).ok, true)
  assert.equal(validateSecretSaveAck({ ok: true, key: 'llm_api_key', set: true,
    secret_revision: 'secret-r2' },
    'llm_api_key').set, true)

  for (const malformed of [
    {}, { ...resource, settings: [] }, { ...resource, defaults: 'wrong' },
    { ...resource, settings: { ...settings, max_nodes: 'many' } },
    { ...resource, settings: Object.fromEntries(
      Object.entries(settings).filter(([key]) => key !== 'max_nodes')) },
  ]) assert.throws(() => validateSettingsResource(malformed, SETTINGS_SCHEMA),
    /Invalid settings resource/)
  assert.throws(() => validateSettingsSaveAck({ ok: true, settings: {}, overrides: {} },
    SETTINGS_SCHEMA), /Invalid settings resource/)
  assert.throws(() => validateSecretSaveAck({ ok: true, key: 'llm_api_key', set: 'yes' },
    'llm_api_key'), /Invalid settings resource/)

  const config = { ...settings, _looplab_config_meta: {
    config_revision: 'b'.repeat(64), run_start_pinned_fields: [],
    snapshot_mismatch_fields: [], run_read_only_fields: ['profile'],
  } }
  assert.equal(splitRunConfigPayload(config, SETTINGS_SCHEMA).readOnlyFields.has('profile'), true)
  const ack = { ok: true, config, changed: ['max_nodes'], normalized_pinned: [],
    trust_gate_event_appended: false, engine_running: false }
  assert.equal(validateRunConfigSaveAck(ack, SETTINGS_SCHEMA), ack)
  for (const malformed of [{}, { ...config, _looplab_config_meta: null },
    { ...config, max_nodes: [] },
    { ...config, _looplab_config_meta: {
      run_start_pinned_fields: [], snapshot_mismatch_fields: [], run_read_only_fields: ['profile'],
    } },
    { ...config, _looplab_config_meta: { ...config._looplab_config_meta, config_revision: 'short' } },
    { ...config, _looplab_config_meta: { ...config._looplab_config_meta, config_revision: 'C'.repeat(64) } },
    { ...config, _looplab_config_meta: { ...config._looplab_config_meta, config_revision: 7 } },
    { ...config, _looplab_config_meta: { ...config._looplab_config_meta, config_revision: 'd'.repeat(65) } },
    { ...config, _looplab_config_meta: { ...config._looplab_config_meta, unexpected: [] } }]) {
    assert.throws(() => splitRunConfigPayload(malformed, SETTINGS_SCHEMA),
      /Invalid settings resource/)
  }
  assert.throws(() => validateRunConfigSaveAck({ ...ack, changed: 'max_nodes' },
    SETTINGS_SCHEMA), /Invalid settings resource/)
  assert.throws(() => validateRunConfigSaveAck({ ...ack, config: {
    ...config, _looplab_config_meta: { ...config._looplab_config_meta, config_revision: null },
  } }, SETTINGS_SCHEMA), /Invalid settings resource/)
})

test('holdout and verifier run-start contracts are all represented in the settings catalogue', () => {
  for (const key of [
    'holdout_fraction', 'holdout_select', 'select_verifier',
    'verifier_ci_tie', 'select_verifier_samples',
  ]) assert.ok(FIELD_BY_KEY[key], key)
})

test('numeric controls preserve raw transitional text instead of native number-input sanitization', () => {
  const form = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  assert.match(form, /type="text" inputMode=\{f\.type === 'int' \? 'numeric' : f\.type === 'float' \? 'decimal'/)
  assert.match(form, /onChange=\{e => set\(e\.target\.value\)\}/)
  assert.doesNotMatch(form, /type=\{f\.type === 'int' \|\| f\.type === 'float' \? 'number'/)
})

// The agent-governance pills (`field.agents`) advertise which autonomous role may change a setting at
// runtime — that is exactly the backend `DEFAULT_AGENT_CONTROL` registry (looplab/core/config.py). A
// mismatch shows the operator a toggle the engine won't honor (or hides a real one). Guard the seam
// across the language boundary so the pills can't silently drift again (mega-review §UI).
test('agent-governance pills mirror the backend DEFAULT_AGENT_CONTROL registry', () => {
  const configPy = readFileSync(new URL('../../looplab/core/config.py', import.meta.url), 'utf8')
  const block = configPy.match(/DEFAULT_AGENT_CONTROL[^{]*\{([\s\S]*?)\n\}/)
  assert.ok(block, 'could not locate DEFAULT_AGENT_CONTROL in config.py')
  const registry = {}
  for (const m of block[1].matchAll(/"(\w+)":\s*\[([^\]]*)\]/g)) {
    registry[m[1]] = [...m[2].matchAll(/"(\w+)"/g)].map(x => x[1]).sort()
  }
  assert.ok(Object.keys(registry).length >= 10, 'registry parse looks empty — regex drifted')
  for (const [key, f] of Object.entries(FIELD_BY_KEY)) {
    if (!f.agents) continue
    assert.ok(registry[key],
      `settings field "${key}" shows governance pills ${JSON.stringify(f.agents)} but is not agent-governable in DEFAULT_AGENT_CONTROL`)
    assert.deepEqual([...f.agents].sort(), registry[key],
      `governance pills for "${key}" drifted from the backend DEFAULT_AGENT_CONTROL`)
  }
})
