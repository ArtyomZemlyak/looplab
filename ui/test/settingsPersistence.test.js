import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
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
  // The guard list keeps growing and now wraps across lines, so assert each TERM instead of the
  // whole expression: every one of them is a state in which saving would write against a
  // baseline the server has not confirmed.
  const saveDisabled = source.slice(source.indexOf('<button ref={saveButtonRef}'),
    source.indexOf('onClick={onSave}'))
  for (const term of ['!unsaved', 'invalidCount > 0', '!!mutationBusy', '!!mutationUnknown',
                      'healthRecoveryBlocked']) {
    assert.ok(saveDisabled.includes(term), `Save no longer refuses while ${term}`)
  }
  const formSource = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  assert.match(formSource, /aria-invalid=\{error \? 'true' : undefined\}/)
  assert.match(formSource, /className="sf-error" role="alert"/)
})

test('global Settings commits the settings ACK before the independent secret ACK', () => {
  const source = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  const accepted = source.indexOf('setSaved(acceptedForm)')
  const secret = source.indexOf("signal => saveSecret('llm_api_key', apiKey, {")
  assert.ok(accepted >= 0 && secret > accepted, 'ordinary settings must become the baseline before secret I/O')
  assert.match(source, /Endpoint\/settings saved, but the replacement API key was not confirmed/)
  assert.match(source, /const formBeforeSecretAck = apiKey[\s\S]*?llm_api_key: submittedForm\.llm_api_key[\s\S]*?: acceptedForm/)
  assert.match(source, /reconcileAcceptedRecord\(current, submittedForm, acceptedForm\)/)
  assert.match(source, /mutationRef\.current \|\| \(mutationUnknown && kind !== 'reconciling'\)/)
  assert.match(source, /finally \{\s*finishMutation\(mutation\)/)
})

test('per-run Settings bounds reads, protects uncertain writes and rebases an ACK onto newer edits', () => {
  const source = readFileSync(new URL('../src/ConfigPanel.jsx', import.meta.url), 'utf8')
  assert.match(source, /const generation = \+\+loadGenerationRef\.current/)
  assert.match(source, /const configRequest = deadlineGet\(/)
  assert.match(source, /deadlineGet\(runApiPath\(runId, '\/config'\), PANEL_REQUEST_TIMEOUT_MS\)/)
  assert.match(source, /return \(\) => configRequest\.controller\.abort\(\)/)
  assert.match(source, /const submittedRevision = configMeta\.configRevision/)
  assert.match(source, /saveRunConfig\(submittedRunId, changed, \{[\s\S]*?expectedRevision: submittedRevision/)
  assert.match(source, /const disposition = e\?\.status === 409 && e\?\.code === 'run_generation_changed'\n\s*\? 'conflict' : runConfigWriteDisposition\(e\)/)
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
  // A read-only field is now ALSO disabled while an interaction is in flight; both reasons must
  // reach the control, so the assertion follows the composed predicate rather than the bare flag.
  assert.match(formSource, /disabled=\{readOnly \|\| interactionDisabled\}/)
  assert.match(formSource, /readOnly=\{readOnly \|\| interactionDisabled\}/)
  assert.match(formSource, /aria-readonly=\{readOnly \|\| interactionDisabled \|\| undefined\}/,
    'a read-only control must say so to assistive tech, not only look inert')
  assert.match(formSource, /launch-pinned/)
})

test('Settings loss guards and secret actions remain explicit and no recovery state retains a key', () => {
  const source = readFileSync(new URL('../src/Settings.jsx', import.meta.url), 'utf8')
  const panelSource = readFileSync(new URL('../src/ConfigPanel.jsx', import.meta.url), 'utf8')
  const formSource = readFileSync(new URL('../src/SettingsForm.jsx', import.meta.url), 'utf8')
  const guardSource = readFileSync(new URL('../src/navigationLossGuard.js', import.meta.url), 'utf8')
  assert.match(source, /installNavigationLossGuard\(/)
  assert.match(panelSource, /installNavigationLossGuard\(/)
  assert.match(guardSource, /addEventListener\('beforeunload'/)
  assert.match(guardSource, /addEventListener\('popstate', blockClientNavigation, true\)/)
  assert.match(guardSource, /addEventListener\('hashchange', blockClientNavigation, true\)/)
  assert.match(source, /Clear the stored API key and its endpoint binding now\? This is immediate, separate from Save/)
  assert.match(source, /publicSubmittedForm = form => \(\{ \.\.\.\(form \|\| \{\}\), llm_api_key: '' \}\)/)
  assert.match(source, /focusFirstInvalid\(\)/)
  // `publicConfigForm` grew a schema walk so it blanks EVERY declared secret, not only the one
  // hardcoded name — a strictly stronger guarantee that this regex could not express, so it simply
  // stopped matching and took the rest of this test's assertions with it. The property is now
  // checked for real below.
  assert.match(panelSource, /const publicConfigForm = \(form, settingsSchema = null\) =>/)
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
  // CODEX AGENT: settings copy must disclose live steering and paid/tool-loop effects, not freeze the
  // older audit-only story after the implementation began influencing generated proposals.
  assert.match(FIELD_BY_KEY.concept_pivot.help, /affect downstream selection/i)
  assert.match(FIELD_BY_KEY.concept_pivot.help, /does not itself rank candidates/i)
  assert.match(FIELD_BY_KEY.graded_novelty.help, /proposal admission/i)
  assert.match(FIELD_BY_KEY.capability_expansion.help, /Concept coverage pivot/i)
  assert.match(FIELD_BY_KEY.cross_run_concepts.help, /D8.*persist independently/i)
  assert.match(FIELD_BY_KEY.cross_run_read_tools.help, /model\/tool-loop turns.*inference cost/i)
  assert.match(FIELD_BY_KEY.cross_run_curation.warning, /finalization latency.*paid model cost/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.warning, /never applies/i)
  assert.match(FIELD_BY_KEY.cross_run_curation_auto.help, /explicit operator/i)
})

test('a per-run config draft carries no secret from any schema-declared secret field', async () => {
  const { createServer } = await import('vite')
  const vite = await createServer({
    root: fileURLToPath(new URL('..', import.meta.url)),
    configFile: false, appType: 'custom', logLevel: 'silent',
    server: { middlewareMode: true },
  })
  try {
    const { __testPublicConfigForm: redact } = await vite.ssrLoadModule('/src/panels.jsx')
    assert.ok(typeof redact === 'function', 'panels.jsx no longer exports the draft redactor')

    // The shipped schema declares exactly one secret today, which is the same field the function
    // blanks by name — so testing against it alone cannot distinguish the schema walk from the old
    // hardcoded line. A second declared secret is what makes this test about the WALK.
    const schema = { ...SETTINGS_SCHEMA, fieldByKey: {
      ...SETTINGS_SCHEMA.fieldByKey,
      provider_token: { key: 'provider_token', type: 'secret', label: 'Provider token' },
    } }
    const secrets = Object.values(schema.fieldByKey).filter(f => f.type === 'secret')
    assert.ok(secrets.length >= 2, 'this test needs more than one secret field to mean anything')

    const form = Object.fromEntries(Object.values(schema.fieldByKey)
      .map(field => [field.key, field.type === 'secret' ? `RAW_${field.key.toUpperCase()}` : 'kept']))
    const redacted = redact(form, schema)
    for (const field of secrets) {
      assert.equal(redacted[field.key], '',
        `${field.key} survived into a persisted config draft`)
    }
    assert.doesNotMatch(JSON.stringify(redacted), /RAW_/)
    assert.equal(redacted.max_nodes, 'kept', 'non-secret fields must survive the draft intact')

    // Without a schema the one historically-hardcoded key must still be blanked: a draft written
    // before the schema loaded would otherwise persist a typed key to browser storage.
    assert.equal(redact({ llm_api_key: 'RAW_KEY', max_nodes: '3' }).llm_api_key, '')
    assert.deepEqual(redact(null), { llm_api_key: '' })
  } finally {
    await vite.close()
  }
})
