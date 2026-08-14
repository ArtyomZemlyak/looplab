import test from 'node:test'
import assert from 'node:assert/strict'
import {
  SETTINGS_SCHEMA_VERSION, createSettingsSchemaLoader, validateSettingsSchema,
} from '../src/settingsSchema.js'
import { RAW_SETTINGS_SCHEMA } from './settingsSchemaFixture.js'

const payload = () => ({ ...structuredClone(RAW_SETTINGS_SCHEMA), revision: 'a'.repeat(64) })

test('packaged settings metadata validates as one bounded versioned contract without losing copy', () => {
  const schema = validateSettingsSchema(payload())
  assert.equal(schema.schema, SETTINGS_SCHEMA_VERSION)
  assert.equal(schema.groups.length, RAW_SETTINGS_SCHEMA.groups.length)
  assert.equal(Object.keys(schema.fieldByKey).length,
    RAW_SETTINGS_SCHEMA.groups.reduce((total, group) => total + group.fields.length, 0))
  // A LITERAL beside the derived equality above, so a change in the packaged catalogue's SIZE has to
  // be re-pinned deliberately rather than sliding through on "the two sides still agree".
  // Field-count history:
  //   158 -> 162 (2026-08-05): the live-watchdog knobs landed — `train_monitor`,
  //   `train_monitor_kill`, `train_monitor_kill_confidence` and `asha_live_kill_confidence`.
  //   Verified by diffing the catalogue's key set against the last 158-field revision, so this is
  //   real growth rather than a duplicated or renamed key.
  //   162 -> 163 (2026-08-06): `concept_tidy` joined the form — the switch that lets an agent's
  //   recorded merge become cross-run policy unattended. Same verification: diffing the packaged
  //   catalogue's key set across that commit reports exactly `['concept_tidy']` added and nothing
  //   removed. The Python side of this tripwire moved with it and this one did not, so the fixture
  //   (which reads the REAL `looplab/serve/settings_ui_schema.json`) reported 163 against a literal
  //   still pinned at 162 — see `tests/test_settings_ui_schema.py`, which pins the same growth.
  //   163 -> 164 (2026-08-09): `task_facets_finalize` split the paid-but-unconsumed facet steward
  //   from the useful concept/claim curation umbrella. Fresh configurations now keep it off unless
  //   explicitly requested; the paired Python key-set guard proves this is the only added row.
  //   164 -> 165 (2026-08-11): `systemic_failure_stop`, the run-level "nothing has ever worked"
  //   bound that stops a run whose every node fails for the same reason. The paired Python guards
  //   (`SETTINGS_UI_SCHEMA_KEYSET_REVISION` + the divergence table) prove it is the only added row —
  //   and this literal is exactly the tripwire the comment above records being missed once already.
  //   165 -> 167 (2026-08-12): `metric_salvage` and `metric_salvage_repair` joined the Resilience
  //   group when salvage shipped — the rung that decides whether a metric the eval already produced
  //   can be recovered from a node that failed for something else, and whether the broken
  //   DECLARATION is fixed in the same attempt. Two rows, one commit. And the tripwire was missed a
  //   SECOND time: `looplab/serve/settings_ui_schema.py` moved to 167 and this literal stayed at
  //   165, so the fixture — which reads the REAL packaged catalogue — has been failing ever since,
  //   which is precisely the failure mode the 162 -> 163 note above describes. Read that note before
  //   adding a catalogue row: the Python and JS halves of this count are ONE tripwire in two files.
  //   167 -> 168 (2026-08-13): `read_fence`. FOURTH occurrence of the same drift — that commit moved
  //   `settings_ui_schema.py` to 168 and left this literal behind, exactly as the 162 -> 163 and
  //   165 -> 167 notes above record. The pattern is now unmistakable: the Python guard is in the
  //   suite a contributor runs (`python -m pytest`) and this one is not, so it is ALWAYS the half
  //   that gets missed. If you are adding a row and reading this, the fix is to grep the repo for
  //   the OLD number rather than trusting either file to remind you.
  //   168 -> 175 (2026-08-14): FIFTH occurrence, and the largest — seven rows across five branches
  //   merged over four days while this literal stayed at 168, so the paragraph above predicted its
  //   own next failure exactly. Verified the way that paragraph prescribes rather than by bumping
  //   the number: the packaged catalogue was 175 keys, and removing precisely the seven the Python
  //   side's own history names — `assistant_time_budget_s`, `developer_probe`,
  //   `developer_probe_timeout_s`, `repair_critic_after`, `metric_subject`, `landlock`,
  //   `eval_deadline_grace_s` — gave back 168. So this was seven real additions with nothing
  //   renamed away underneath them, which a bare re-pin could not have told you.
  //   175 -> 176 (2026-08-14, same day): F1's `proposal_width`. SIXTH occurrence — and it was
  //   caught by the Python guard again, not by this one, exactly as the paragraph above says.
  //   176 -> 177 (2026-08-14, same day): `train_monitor_tools` — whether the two live-eval
  //   watchdog judges may QUERY the stage logs instead of being handed a fixed slice. SEVENTH
  //   occurrence, and the Python guard caught it first for the third consecutive time, which is
  //   now the pattern rather than an anecdote: this literal has never once been the one that
  //   noticed. Verified as the paragraph prescribes — the catalogue was 177 keys and removing
  //   exactly `train_monitor_tools` gave back 176, so this is one real addition.
  //   177 -> 178 (2026-08-14, same day): `auto_extra_metrics` — may an UNDECLARED numeric key on
  //   an experiment's own stdout be RECORDED as one of that node's metrics. EIGHTH occurrence, and
  //   the Python guard caught it first for the fourth consecutive time. Verified as the paragraph
  //   prescribes rather than by bumping the number: the catalogue was 178 keys and removing exactly
  //   `auto_extra_metrics` gave back 177, so this is one real addition with nothing renamed away
  //   underneath it.
  assert.equal(Object.keys(schema.fieldByKey).length, 178)
  assert.equal(schema.fieldByKey.speculation_depth.type, 'int')
  assert.equal(schema.fieldByKey.speculation_depth.minimum, 0)
  assert.equal(schema.fieldByKey.speculation_depth.maximum, 64)
  assert.equal(schema.fieldByKey.concept_pivot.type, 'bool')
  assert.equal(schema.fieldByKey.concept_run_base.type, 'bool')
  assert.equal(schema.fieldByKey.concept_retag_every.type, 'int')
  assert.equal(schema.fieldByKey.concept_retag_every.minimum, 1)
  assert.equal(schema.fieldByKey.graded_novelty.type, 'bool')
  assert.equal(schema.fieldByKey.capability_expansion.type, 'bool')
  assert.match(schema.fieldByKey.cross_run_concepts.help, /D8.*persist independently/i)
  assert.match(schema.fieldByKey.cross_run_curation_auto.warning, /never applies/i)
  assert.equal(schema.agentRolePills.researcher.title,
    RAW_SETTINGS_SCHEMA.agent_role_pills.researcher.title)
  assert.ok(Object.isFrozen(schema) && Object.isFrozen(schema.groups)
    && Object.isFrozen(schema.fieldByKey.max_nodes))
})

test('settings metadata fails closed on version, revision, identity and role drift', () => {
  for (const mutate of [
    value => { value.schema += 1 },
    value => { value.revision = 'not-a-digest' },
    value => { value.groups[1].fields[0].key = value.groups[0].fields[0].key },
    value => { value.groups[0].fields[0].agents = ['unknown-role'] },
    value => { value.groups[0].fields[2].options = ['not-an-enum'] },
  ]) {
    const value = payload()
    mutate(value)
    assert.throws(() => validateSettingsSchema(value), /Invalid settings schema/)
  }
})

test('settings metadata fails closed on malformed or contradictory numeric bounds', () => {
  for (const mutate of [
    value => { delete value.groups[0].fields[0].nullable },
    value => { value.groups[0].fields[0].nullable = 'sometimes' },
    value => { value.groups[0].fields[0].minimum = 1 },
    value => { value.groups[0].fields.find(field => field.key === 'max_nodes').minimum = NaN },
    value => { value.groups[0].fields.find(field => field.key === 'max_nodes').exclusiveMinimum = 1 },
    value => {
      const field = value.groups[0].fields.find(item => item.key === 'max_nodes')
      field.minimum = field.maximum + 1
    },
  ]) {
    const value = payload()
    mutate(value)
    assert.throws(() => validateSettingsSchema(value), /Invalid settings schema/)
  }
})

test('settings schema loader is single-flight and keeps one revalidated revision', async () => {
  let finish
  let reads = 0
  const seen = []
  const loader = createSettingsSchemaLoader(options => {
    reads += 1
    seen.push(options)
    return new Promise(resolve => { finish = resolve })
  })
  const first = loader.load()
  const joined = loader.load({ reload: true })
  assert.equal(first, joined)
  assert.equal(reads, 0, 'the read starts in a microtask so same-tick callers can join')
  await Promise.resolve()
  assert.equal(reads, 1)
  finish(payload())
  const schema = await first
  assert.equal(await loader.load(), schema)
  assert.equal(loader.peek(), schema)
  assert.equal(reads, 1)
  assert.deepEqual(seen, [{ cache: 'no-cache' }])
})

test('failed or malformed schema reads never populate cache and retry requests a reload', async () => {
  const seen = []
  let attempt = 0
  const loader = createSettingsSchemaLoader(options => {
    seen.push(options)
    attempt += 1
    if (attempt === 1) return Promise.reject(new Error('offline'))
    if (attempt === 2) return { ...payload(), revision: 'broken' }
    return payload()
  })
  await assert.rejects(loader.load(), /offline/)
  assert.equal(loader.peek(), null)
  await assert.rejects(loader.load({ reload: true }), /Invalid settings schema/)
  assert.equal(loader.peek(), null)
  const schema = await loader.load({ reload: true })
  assert.equal(schema.revision, 'a'.repeat(64))
  assert.deepEqual(seen.map(item => item.cache), ['no-cache', 'reload', 'reload'])
})

test('explicit schema reload revalidates a cached same-version semantic revision', async () => {
  const seen = []
  let revision = 'a'.repeat(64)
  const loader = createSettingsSchemaLoader(options => {
    seen.push(options.cache)
    return { ...payload(), revision }
  })
  const first = await loader.load()
  revision = 'b'.repeat(64)
  const refreshed = await loader.load({ reload: true })
  assert.equal(first.revision, 'a'.repeat(64))
  assert.equal(refreshed.revision, 'b'.repeat(64))
  assert.equal(loader.peek(), refreshed)
  assert.deepEqual(seen, ['no-cache', 'reload'])
})
