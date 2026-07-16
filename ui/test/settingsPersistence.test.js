import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  FIELD_BY_KEY,
  settingsSavePayload,
  settingsValidationErrors,
  toForm,
} from '../src/settingsSchema.js'

const CROSS_RUN_FLAGS = [
  'fingerprint_universal',
  'cross_run_concepts',
  'cross_run_read_tools',
  'cross_run_advisory',
  'cross_run_structured_claims',
  'cross_run_curation',
  'cross_run_curation_auto',
]

test('global Settings sends only controlled fields and never replays stale hidden values or secrets', () => {
  const loaded = {
    profile: 'default',
    future_engine_override: { mode: 'newer-server-value', budget: 7 },
    llm_api_key: '***',
    agent_control: { timeout: ['strategist'] },
  }
  const baseline = toForm(loaded)
  const form = { ...baseline, max_nodes: '13' }
  const payload = settingsSavePayload(form, loaded.agent_control, baseline, loaded.agent_control)

  assert.equal(Object.hasOwn(payload, 'future_engine_override'), false)
  assert.deepEqual(payload, { max_nodes: 13 })
  assert.equal(Object.hasOwn(payload, 'llm_api_key'), false)
})

test('two stale tabs emit disjoint patches and preserve explicit clears and governance edits', () => {
  const loaded = {
    max_nodes: 8,
    policy: 'greedy',
    memory_dir: 'portfolio',
    agent_control: { timeout: ['researcher', 'strategist'], max_nodes: ['boss'] },
  }
  const baseline = toForm(loaded)
  const tabA = { ...baseline, max_nodes: '21', memory_dir: '' }
  const tabB = { ...baseline, policy: 'mcts' }
  const controlB = { ...loaded.agent_control, timeout: ['strategist'] }

  assert.deepEqual(
    settingsSavePayload(tabA, loaded.agent_control, baseline, loaded.agent_control),
    { max_nodes: 21, memory_dir: null },
  )
  assert.deepEqual(
    settingsSavePayload(tabB, controlB, baseline, loaded.agent_control),
    { policy: 'mcts', agent_control: { timeout: ['strategist'] } },
  )
})

test('Settings page imports coercion and saves against its last baseline', () => {
  const source = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  assert.match(source, /import \{[^}]*\bfromForm\b[^}]*\} from ['"]\.\/settingsSchema\.js['"]/)
  assert.match(source, /settingsSavePayload\(submittedForm, submittedControl, saved, savedAC\)/)
  assert.match(source, /disabled=\{!unsaved \|\| invalidCount > 0 \|\| !!mutationBusy\}/)
  const formSource = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  assert.match(formSource, /aria-invalid=\{error \? 'true' : undefined\}/)
  assert.match(formSource, /className="sf-error" role="alert"/)
})

test('global Settings commits the settings ACK before the independent secret ACK', () => {
  const source = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  const accepted = source.indexOf('setSaved(acceptedForm)')
  const secret = source.indexOf("await saveSecret('llm_api_key', apiKey)")
  assert.ok(accepted >= 0 && secret > accepted, 'ordinary settings must become the baseline before secret I/O')
  assert.match(source, /Settings saved, but the API key was not stored/)
  assert.match(source, /const formBeforeSecretAck = apiKey[\s\S]*?llm_api_key: submittedForm\.llm_api_key[\s\S]*?: acceptedForm/)
  assert.match(source, /reconcileAcceptedRecord\(current, submittedForm, acceptedForm\)/)
  assert.match(source, /if \(mutationRef\.current\) return null/)
  assert.match(source, /finally \{\s*finishMutation\(mutation\)/)
})

test('per-run Settings aborts stale loads and rebases a save ACK onto newer edits', () => {
  const source = readFileSync(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  assert.match(source, /const generation = \+\+loadGenerationRef\.current/)
  assert.match(source, /const controller = new AbortController\(\)/)
  assert.match(source, /get\(runApiPath\(runId, '\/config'\), \{ signal: controller\.signal \}\)/)
  assert.match(source, /return \(\) => controller\.abort\(\)/)
  assert.match(source, /if \(mutationRef\.current\) return null/)
  assert.match(source, /setForm\(current => reconcileAcceptedRecord\(current, submittedForm, acceptedForm\)\)/)
  assert.match(source, /mutation\.generation !== loadGenerationRef\.current/)
  assert.match(source, /splitRunConfigPayload\(c\)/)
  assert.match(source, /readOnlyKeys=\{configMeta\.pinnedFields\}/)
  assert.match(source, /legacy snapshot disagrees/)
  const formSource = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  assert.match(formSource, /disabled=\{readOnly\}/)
  assert.match(formSource, /readOnly=\{readOnly\}/)
  assert.match(formSource, /launch-pinned/)
})

test('an invalid numeric edit fails before transport and cannot clear the persisted override', () => {
  const baseline = toForm({ max_nodes: 17 })
  const form = { ...baseline, max_nodes: '2.5' }
  assert.match(settingsValidationErrors(form).max_nodes, /whole number/i)
  assert.throws(
    () => settingsSavePayload(form, {}, baseline, {}),
    error => error?.code === 'invalid_settings_form'
      && /max_nodes/.test(JSON.stringify(error.validationErrors)),
  )
  assert.equal(baseline.max_nodes, 17, 'the last persisted baseline remains intact')
  assert.deepEqual(settingsSavePayload({ ...baseline, max_nodes: '' }, {}, baseline, {}),
    { max_nodes: null }, 'a deliberate blank remains an explicit clear')
})

test('every Part IV/V flag is visible and round-trips its boolean value', () => {
  const baseline = Object.fromEntries(CROSS_RUN_FLAGS.map(key => [key, false]))
  baseline.future_engine_override = 'still-here'
  const form = toForm(baseline)
  for (const key of CROSS_RUN_FLAGS) form[key] = true

  const payload = settingsSavePayload(form, {})
  for (const key of CROSS_RUN_FLAGS) {
    assert.equal(FIELD_BY_KEY[key]?.type, 'bool', `${key} must be a visible boolean control`)
    assert.equal(payload[key], true, `${key} must round-trip through the form`)
  }
  assert.equal(Object.hasOwn(payload, 'future_engine_override'), false)
  assert.match(FIELD_BY_KEY.cross_run_concepts.help, /D8.*persist independently/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.warning, /never applies/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.help, /explicit operator/i)
})
