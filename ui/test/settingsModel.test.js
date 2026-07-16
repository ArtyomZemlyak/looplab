import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  SETTINGS_GROUPS, FIELD_BY_KEY, INVALID_SETTING_VALUE, coerce, parseSettingValue,
  settingsValidationErrors,
} from '../src/settingsSchema.js'
import {
  ESSENTIAL_SETTING_KEYS,
  filterSettingsGroups,
  normalizeSettingsQuery,
  reconcileAcceptedRecord,
  splitRunConfigPayload,
  settingsViewStats,
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
  assert.match(settingsValidationErrors({ max_nodes: '2.5' }).max_nodes, /whole number/i)
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

test('per-run config metadata is separated from the flat config and exposes launch pins', () => {
  const parsed = splitRunConfigPayload({
    timeout: 40,
    holdout_fraction: 0.4,
    _looplab_config_meta: {
      run_start_pinned_fields: ['holdout_fraction', 'holdout_fraction', 'select_verifier'],
      snapshot_mismatch_fields: ['holdout_fraction'],
    },
  })
  assert.deepEqual(parsed.config, { timeout: 40, holdout_fraction: 0.4 })
  assert.deepEqual([...parsed.pinnedFields], ['holdout_fraction', 'select_verifier'])
  assert.deepEqual(parsed.mismatchFields, ['holdout_fraction'])
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
