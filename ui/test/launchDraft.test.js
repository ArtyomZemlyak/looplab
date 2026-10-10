import test from 'node:test'
import assert from 'node:assert/strict'

import {
  LAUNCH_RUNTIME_FIELDS, buildLaunchBody, createLaunchDraft, launchFingerprint, parseObjectJson,
  runtimeValue, summarizeLaunchDecision, summarizeLaunchTask, updateRuntimeValue,
  validateLaunchDraft,
} from '../src/launchDraft.js'

const proposal = {
  proposal_id: 'proposal-1',
  run_id: 'ranker-search',
  task: {
    goal: 'maximize recall', direction: 'max', repo: 'C:/repo',
    cmd: { command: ['python', 'score.py'], metric: { reader: 'stdout_json', key: 'recall' } },
  },
  settings: { max_nodes: 12, nested_future_setting: { keep: ['exactly'] } },
  rationale: 'Grounded in the scorer.',
  setup_steps: ['Protect score.py', 'Pin dependencies'],
}

test('launch drafts clone a proposal and retain lossless task/settings JSON', () => {
  const draft = createLaunchDraft(proposal)
  assert.equal(draft.source, 'task')
  assert.equal(draft.run_id, 'ranker-search')
  assert.deepEqual(JSON.parse(draft.task_json), proposal.task)
  assert.deepEqual(JSON.parse(draft.settings_json), proposal.settings)
  assert.deepEqual(draft.setup_steps, proposal.setup_steps)

  JSON.parse(draft.task_json).goal = 'cannot mutate proposal'
  assert.equal(proposal.task.goal, 'maximize recall')
})

test('inline task and task_file payloads are mutually exclusive and preserve clean chat', () => {
  const inline = buildLaunchBody(createLaunchDraft(proposal), [
    { role: 'user', content: '/new ranker' },
    { role: 'tool', content: 'secret tool trace' },
    { role: 'assistant', content: 'Here is the plan.' },
    { role: 'assistant', content: '' },
  ])
  assert.equal(inline.ok, true)
  assert.ok(inline.body.task)
  assert.ok(!('task_file' in inline.body))
  assert.deepEqual(inline.body.chat, [
    { role: 'user', content: '/new ranker' },
    { role: 'assistant', content: 'Here is the plan.' },
  ])

  const fileDraft = createLaunchDraft({ ...proposal, task: {}, task_file: 'C:/tasks/ranker.json' })
  const file = buildLaunchBody(fileDraft)
  assert.equal(file.ok, true)
  assert.equal(file.body.task_file, 'C:/tasks/ranker.json')
  assert.ok(!('task' in file.body))
})

test('malformed JSON and unsafe identity block validation without discarding raw text', () => {
  const draft = { ...createLaunchDraft(proposal), run_id: '../escape', task_json: '{"goal":' }
  const checked = validateLaunchDraft(draft)
  assert.equal(checked.ok, false)
  assert.match(checked.errors.run_id, /plain run name/)
  assert.match(checked.errors.task, /valid JSON/)
  assert.equal(draft.task_json, '{"goal":')
  assert.equal(launchFingerprint(draft), '')
  assert.equal(parseObjectJson('[]', 'Task').ok, false)
})

test('curated runtime edits update the same JSON and preserve unknown nested overrides', () => {
  const draft = createLaunchDraft(proposal)
  const field = LAUNCH_RUNTIME_FIELDS.find(item => item.key === 'max_nodes')
  const changed = updateRuntimeValue(draft, field, '25')
  assert.equal(changed.ok, true)
  assert.equal(runtimeValue(changed.draft, 'max_nodes'), 25)
  assert.deepEqual(JSON.parse(changed.draft.settings_json).nested_future_setting, { keep: ['exactly'] })

  const cleared = updateRuntimeValue(changed.draft, field, '')
  assert.equal(cleared.ok, true)
  assert.ok(!('max_nodes' in JSON.parse(cleared.draft.settings_json)))
  assert.deepEqual(JSON.parse(cleared.draft.settings_json).nested_future_setting, { keep: ['exactly'] })

  const fractional = updateRuntimeValue(draft, field, '3.5')
  assert.equal(runtimeValue(fractional.draft, 'max_nodes'), 3.5)
  assert.match(validateLaunchDraft(fractional.draft).errors['settings.max_nodes'], /integer/)
})

test('launch controls expose canonical independent concurrency with bounded AUTO zero', () => {
  const keys = new Set(LAUNCH_RUNTIME_FIELDS.map(field => field.key))
  assert.ok(keys.has('eval_parallel'))
  assert.ok(keys.has('llm_parallel'))
  assert.ok(!keys.has('max_parallel'))
  assert.ok(!keys.has('parallel_build'))
  const llmField = LAUNCH_RUNTIME_FIELDS.find(field => field.key === 'llm_parallel')
  assert.match(llmField.help, /total LLM-call budget/i)
  assert.match(llmField.help, /research.*novelty.*enrichment/i)

  const draft = createLaunchDraft({ ...proposal, settings: { eval_parallel: 0, llm_parallel: 0 } })
  assert.equal(validateLaunchDraft(draft).ok, true)
  const tooWide = createLaunchDraft({ ...proposal, settings: { eval_parallel: 1025, llm_parallel: 65 } })
  const errors = validateLaunchDraft(tooWide).errors
  assert.match(errors['settings.eval_parallel'], /between 0 and 1024/)
  assert.match(errors['settings.llm_parallel'], /between 0 and 64/)
})

test('launch controls expose the run-pinned Card selector as a strict boolean', () => {
  const field = LAUNCH_RUNTIME_FIELDS.find(item => item.key === 'card_driven_selection')
  assert.equal(field.type, 'bool')
  const enabled = updateRuntimeValue(createLaunchDraft(proposal), field, true)
  assert.equal(enabled.ok, true)
  assert.equal(runtimeValue(enabled.draft, field.key), true)
  assert.equal(validateLaunchDraft(enabled.draft).ok, true)

  const invalid = createLaunchDraft({ ...proposal, settings: { card_driven_selection: 'true' } })
  assert.match(validateLaunchDraft(invalid).errors['settings.card_driven_selection'], /boolean/)
})

test('the validation fingerprint is semantic, stable, and includes provenance chat', () => {
  const one = createLaunchDraft(proposal)
  const reordered = {
    ...one,
    settings_json: '{"nested_future_setting":{"keep":["exactly"]},"max_nodes":12}',
    task_json: JSON.stringify({ cmd: proposal.task.cmd, repo: 'C:/repo', direction: 'max', goal: 'maximize recall' }),
  }
  const chat = [{ role: 'user', content: '/new ranker' }]
  assert.equal(launchFingerprint(one, chat), launchFingerprint(reordered, chat))
  assert.notEqual(launchFingerprint(one, chat), launchFingerprint(one, [...chat, { role: 'assistant', content: 'planned' }]))
  assert.notEqual(launchFingerprint(one, chat), launchFingerprint({ ...one, run_id: 'ranker-search-2' }, chat))
})

test('task summary exposes the decision-critical contract instead of only a kind chip', () => {
  const rows = summarizeLaunchTask(createLaunchDraft(proposal))
  const labels = new Set(rows.map(row => row.label))
  assert.ok(labels.has('Type'))
  assert.ok(labels.has('Goal'))
  assert.ok(labels.has('Direction'))
  assert.ok(labels.has('Source'))
  assert.ok(labels.has('Evaluation'))
  assert.ok(labels.has('Metric'))
})

test('plain launch decision names measured score, edit bounds, and every repo/data source', () => {
  const task = {
    kind: 'repo', goal: 'Improve recall', direction: 'max', editable_path: '/srv/model',
    edit_surface: ['src/**/*.py'], protect: ['score.py'],
    editables: [{ name: 'pipeline', path: '/srv/pipeline', surface: ['config/*.json'],
      protect: ['eval.json'] }],
    data: { train: { path: '/srv/data/train.csv' } },
    eval: { command: ['python', 'score.py'], metric: { kind: 'stdout_json', key: 'recall' } },
  }
  const rows = summarizeLaunchDecision(createLaunchDraft({ task }))
  const byLabel = Object.fromEntries(rows.map(row => [row.label, row.value]))
  assert.equal(byLabel.Goal, 'Improve recall')
  assert.equal(byLabel.Score, 'recall · higher is better')
  assert.match(byLabel['Code or data'], /\/srv\/model.*\/srv\/pipeline.*\/srv\/data\/train\.csv/)
  assert.match(byLabel['May edit'], /root: src\/\*\*\/\*\.py; pipeline: config\/\*\.json/)
  assert.equal(byLabel.Protected, 'score.py, eval.json')
  assert.equal(byLabel['Original data'], 'No editable originals declared.')
  const writable = summarizeLaunchDecision(createLaunchDraft({ task: {
    ...task, data: { train: { path: '/srv/data/train.csv', edit: true } },
  } }))
  assert.match(writable.find(row => row.label === 'Original data').value, /^train editable;/)
})

test('unknown launch facts are called unknown before task-file validation', () => {
  const fileRows = summarizeLaunchDecision(createLaunchDraft({ task_file: '/srv/task.json' }))
  assert.match(fileRows[1].value, /Validate to inspect/)
  const inline = summarizeLaunchDecision(createLaunchDraft({ task: { kind: 'repo',
    editable_path: '/srv/repo', direction: 'min' } }))
  const byLabel = Object.fromEntries(inline.map(row => [row.label, row.value]))
  assert.equal(byLabel.Goal, 'Not stated in the task.')
  assert.match(byLabel.Score, /Metric not explicitly stated · lower is better/)
  assert.match(byLabel['May edit'], /not stated/)
  assert.equal(byLabel.Protected, 'No protected paths listed in the task.')
  const auto = summarizeLaunchDecision(createLaunchDraft({ task: {
    kind: 'mlebench_real', competition: 'example', direction: 'auto',
  } }))
  assert.match(auto.find(row => row.label === 'Score').value, /direction resolved by evaluator/)
})

test('the offline demo card names the metric its contract declares (doc 75 UX-31)', async () => {
  const { summarizeLaunchDecision } = await import('../src/launchDraft.js')
  const { readFile } = await import('node:fs/promises')
  const spec = JSON.parse(await readFile(new URL('../src/offlineDemo.json', import.meta.url), 'utf8'))
  const rows = summarizeLaunchDecision({ source: 'json', task_json: JSON.stringify(spec.task) })
  const score = rows.find(row => row.label === 'Score')
  assert.equal(score.value, 'quadratic_loss · lower is better')
})
