import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  settingsSavePayload,
  settingsValidationErrors,
  toForm,
} from '../src/settingsSchema.js'
import { saveRunConfig } from '../src/api.js'
import { FIELD_BY_KEY, SETTINGS_SCHEMA } from './settingsSchemaFixture.js'

const CROSS_RUN_FLAGS = [
  'concept_pivot',
  'graded_novelty',
  'capability_expansion',
  'fingerprint_universal',
  'cross_run_concepts',
  'cross_run_read_tools',
  'cross_run_advisory',
  'cross_run_structured_claims',
  'cross_run_curation',
  'cross_run_curation_auto',
]

test('per-run settings write carries its exact revision and cancellation signal', async () => {
  const previous = { fetch: globalThis.fetch, location: globalThis.location }
  let request
  globalThis.location = { pathname: '/proxy/8765/', hash: '' }
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return { ok: true, json: async () => ({ ok: true }) }
  }
  try {
    const controller = new AbortController()
    const revision = 'c'.repeat(64)
    await saveRunConfig('run/with space', { max_seconds: null }, {
      expectedRevision: revision, signal: controller.signal,
    })
    assert.match(request.url, /\/proxy\/8765\/api\/runs\/run%2Fwith%20space\/config$/)
    assert.equal(request.options.method, 'PUT')
    assert.equal(request.options.signal, controller.signal)
    assert.deepEqual(JSON.parse(request.options.body), {
      settings: { max_seconds: null }, expected_revision: revision,
    })
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[key]
      else globalThis[key] = value
    }
  }
})

test('global Settings sends only controlled fields and never replays stale hidden values or secrets', () => {
  const loaded = {
    profile: 'default',
    future_engine_override: { mode: 'newer-server-value', budget: 7 },
    llm_api_key: '***',
    agent_control: { timeout: ['strategist'] },
  }
  const baseline = toForm(loaded, SETTINGS_SCHEMA)
  const form = { ...baseline, max_nodes: '13' }
  const payload = settingsSavePayload(form, loaded.agent_control, baseline, loaded.agent_control,
    SETTINGS_SCHEMA)

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
  const baseline = toForm(loaded, SETTINGS_SCHEMA)
  const tabA = { ...baseline, max_nodes: '21', memory_dir: '' }
  const tabB = { ...baseline, policy: 'mcts' }
  const controlB = { ...loaded.agent_control, timeout: ['strategist'] }

  assert.deepEqual(
    settingsSavePayload(tabA, loaded.agent_control, baseline, loaded.agent_control, SETTINGS_SCHEMA),
    { max_nodes: 21, memory_dir: null },
  )
  assert.deepEqual(
    settingsSavePayload(tabB, controlB, baseline, loaded.agent_control, SETTINGS_SCHEMA),
    { policy: 'mcts', agent_control: { timeout: ['strategist'] } },
  )
})

test('Settings page imports coercion and saves against its last baseline', () => {
  const source = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  assert.match(source, /import \{[^}]*\bfromForm\b[^}]*\} from ['"]\.\/settingsSchema\.js['"]/)
  assert.match(source, /settingsSavePayload\(submittedForm, submittedControl, saved, savedAC, schema\)/)
  assert.match(source, /disabled=\{!unsaved \|\| invalidCount > 0 \|\| !!mutationBusy \|\| !!mutationUnknown\}/)
  const formSource = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  assert.match(formSource, /aria-invalid=\{error \? 'true' : undefined\}/)
  assert.match(formSource, /className="sf-error" role="alert"/)
})

test('global Settings commits the settings ACK before the independent secret ACK', () => {
  const source = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  const accepted = source.indexOf('setSaved(acceptedForm)')
  const secret = source.indexOf("signal => saveSecret('llm_api_key', apiKey, {")
  assert.ok(accepted >= 0 && secret > accepted, 'ordinary settings must become the baseline before secret I/O')
  assert.match(source, /Settings saved, but the API key was not stored/)
  assert.match(source, /const formBeforeSecretAck = apiKey[\s\S]*?llm_api_key: submittedForm\.llm_api_key[\s\S]*?: acceptedForm/)
  assert.match(source, /reconcileAcceptedRecord\(current, submittedForm, acceptedForm\)/)
  assert.match(source, /mutationRef\.current \|\| \(mutationUnknown && kind !== 'reconciling'\)/)
  assert.match(source, /finally \{\s*finishMutation\(mutation\)/)
})

test('per-run Settings bounds reads, protects uncertain writes and rebases an ACK onto newer edits', () => {
  const source = readFileSync(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  assert.match(source, /const generation = \+\+loadGenerationRef\.current/)
  assert.match(source, /const configRequest = deadlineRequest\(/)
  assert.match(source, /get\(runApiPath\(runId, '\/config'\), \{ signal \}\)/)
  assert.match(source, /return \(\) => configRequest\.controller\.abort\(\)/)
  assert.match(source, /const submittedRevision = configMeta\.configRevision/)
  assert.match(source, /saveRunConfig\(submittedRunId, changed, \{[\s\S]*?expectedRevision: submittedRevision/)
  assert.match(source, /const disposition = runConfigWriteDisposition\(e\)/)
  assert.match(source, /Run settings changed elsewhere/)
  assert.match(source, /Load current version/)
  assert.match(source, /configMutationUnknown && kind !== 'reconciling'/)
  assert.match(source, /this client will not replay the save automatically/i)
  assert.match(source, /Refresh server state/)
  assert.match(source, /setForm\(current => reconcileAcceptedRecord\(current, submittedForm, acceptedForm\)\)/)
  assert.match(source, /mutation\.generation !== loadGenerationRef\.current/)
  assert.match(source, /splitRunConfigPayload\(c, nextSchema\)/)
  assert.match(source, /readOnlyKeys=\{configMeta\.readOnlyFields\}/)
  assert.match(source, /legacy snapshot disagrees/)
  const formSource = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  assert.match(formSource, /disabled=\{readOnly\}/)
  assert.match(formSource, /readOnly=\{readOnly\}/)
  assert.match(formSource, /launch-pinned/)
})

test('Settings loss guards and secret actions remain explicit and no recovery state retains a key', () => {
  const source = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  const panelSource = readFileSync(new URL('../src/panels.jsx', import.meta.url), 'utf8')
  const formSource = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  const guardSource = readFileSync(new URL('../src/navigationLossGuard.js', import.meta.url), 'utf8')
  assert.match(source, /installNavigationLossGuard\(/)
  assert.match(panelSource, /installNavigationLossGuard\(/)
  assert.match(guardSource, /addEventListener\('beforeunload'/)
  assert.match(guardSource, /addEventListener\('popstate', blockClientNavigation, true\)/)
  assert.match(guardSource, /addEventListener\('hashchange', blockClientNavigation, true\)/)
  assert.match(source, /Clear the stored API key now\? This is immediate, separate from Save/)
  assert.match(source, /publicSubmittedForm = form => \(\{ \.\.\.\(form \|\| \{\}\), llm_api_key: '' \}\)/)
  assert.match(source, /focusFirstInvalid\(\)/)
  assert.match(panelSource, /publicConfigForm = form => \(\{ \.\.\.\(form \|\| \{\}\), llm_api_key: '' \}\)/)
  assert.match(panelSource, /window\.confirm\(`\$\{warning\} Close the run settings panel anyway\?`\)/)
  assert.match(formSource, />Clear now<\/button>/)
  assert.match(formSource, /Clear now is immediate and separate from Save/)
})

test('an invalid numeric edit fails before transport and cannot clear the persisted override', () => {
  const baseline = toForm({ max_nodes: 17 }, SETTINGS_SCHEMA)
  const form = { ...baseline, max_nodes: '2.5' }
  assert.match(settingsValidationErrors(form, SETTINGS_SCHEMA).max_nodes, /whole number/i)
  assert.throws(
    () => settingsSavePayload(form, {}, baseline, {}, SETTINGS_SCHEMA),
    error => error?.code === 'invalid_settings_form'
      && /max_nodes/.test(JSON.stringify(error.validationErrors)),
  )
  assert.equal(baseline.max_nodes, 17, 'the last persisted baseline remains intact')
  assert.deepEqual(settingsSavePayload({ ...baseline, max_nodes: '' }, {}, baseline, {}, SETTINGS_SCHEMA),
    { max_nodes: null }, 'a deliberate blank remains an explicit clear')
})

test('every Part IV/V flag is visible and round-trips its boolean value', () => {
  const baseline = Object.fromEntries(CROSS_RUN_FLAGS.map(key => [key, false]))
  baseline.future_engine_override = 'still-here'
  const form = toForm(baseline, SETTINGS_SCHEMA)
  for (const key of CROSS_RUN_FLAGS) form[key] = true

  const payload = settingsSavePayload(form, {}, undefined, undefined, SETTINGS_SCHEMA)
  for (const key of CROSS_RUN_FLAGS) {
    assert.equal(FIELD_BY_KEY[key]?.type, 'bool', `${key} must be a visible boolean control`)
    assert.equal(payload[key], true, `${key} must round-trip through the form`)
  }
  assert.equal(Object.hasOwn(payload, 'future_engine_override'), false)
  assert.match(FIELD_BY_KEY.concept_pivot.help, /never ranks or selects/i)
  assert.match(FIELD_BY_KEY.graded_novelty.help, /proposal admission/i)
  assert.match(FIELD_BY_KEY.capability_expansion.help, /Concept coverage pivot/i)
  assert.match(FIELD_BY_KEY.cross_run_concepts.help, /D8.*persist independently/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.warning, /never applies/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.help, /explicit operator/i)
})
