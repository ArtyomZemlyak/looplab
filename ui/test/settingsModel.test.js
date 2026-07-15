import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { SETTINGS_GROUPS, FIELD_BY_KEY } from '../src/settingsSchema.js'
import {
  ESSENTIAL_SETTING_KEYS,
  filterSettingsGroups,
  normalizeSettingsQuery,
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
