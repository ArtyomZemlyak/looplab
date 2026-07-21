// Pure view-model helpers for the editable Assistant launch card.  The raw JSON text is kept as the
// user's source of truth so a partially edited document is never silently replaced by a lossy form
// projection.  Curated runtime controls edit one key inside that same JSON object.

export const LAUNCH_RUNTIME_FIELDS = Object.freeze([
  { key: 'profile', label: 'Profile', type: 'enum', options: ['default', 'fast', 'thorough'] },
  { key: 'backend', label: 'Backend', type: 'enum', options: ['toy', 'llm'] },
  { key: 'llm_model', label: 'Model', type: 'text', placeholder: 'inherit configured model' },
  { key: 'policy', label: 'Policy', type: 'enum', options: ['greedy', 'evolutionary', 'mcts', 'asha', 'bohb'] },
  { key: 'max_nodes', label: 'Max nodes', type: 'int', min: 1 },
  { key: 'n_seeds', label: 'Seeds', type: 'int', min: 1 },
  { key: 'eval_parallel', label: 'Eval parallel', type: 'int', min: 0, max: 1024,
    help: 'Concurrent evaluations; 0 = AUTO (one per detected GPU).' },
  { key: 'llm_parallel', label: 'LLM parallel', type: 'int', min: 0, max: 64,
    help: 'Total LLM-call budget + build fan-out; 0 = launch AUTO. Positive values govern build, research, novelty, and enrichment lanes.' },
  { key: 'max_seconds', label: 'Wall-clock budget (s)', type: 'float', min: 0 },
  { key: 'max_eval_seconds', label: 'Eval budget (s)', type: 'float', min: 0 },
])

const RESERVED_RUN_IDS = new Set(['assistant', 'reports', '.reviews', '.command-locks'])
const WINDOWS_RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i

const clone = value => {
  if (typeof structuredClone === 'function') return structuredClone(value)
  return JSON.parse(JSON.stringify(value))
}

const pretty = value => JSON.stringify(value && typeof value === 'object' ? value : {}, null, 2)

export function createLaunchDraft(spec = {}) {
  const source = spec.task_file ? 'task_file' : 'task'
  return {
    proposal_id: String(spec.proposal_id || ''),
    run_id: String(spec.run_id || ''),
    source,
    task_file: String(spec.task_file || ''),
    task_json: pretty(clone(spec.task || {})),
    settings_json: pretty(clone(spec.settings || {})),
    rationale: String(spec.rationale || ''),
    setup_steps: Array.isArray(spec.setup_steps)
      ? spec.setup_steps.map(value => String(value || '').trim()).filter(Boolean)
      : [],
  }
}

export function parseObjectJson(text, label) {
  let value
  try { value = JSON.parse(String(text || '')) }
  catch (error) { return { ok: false, error: `${label} must be valid JSON: ${error.message}` } }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return { ok: false, error: `${label} must be a JSON object` }
  }
  return { ok: true, value }
}

function runIdError(value) {
  const runId = String(value || '')
  if (!runId) return 'Run name is required'
  if (runId !== runId.trim()) return 'Run name cannot begin or end with whitespace'
  if (runId.length > 255) return 'Run name is too long'
  if (/[\\/:\u0000-\u001f\u007f]/.test(runId) || runId === '.' || runId === '..') {
    return 'Use a plain run name, not a path'
  }
  if (/[. ]$/.test(runId) || WINDOWS_RESERVED.test(runId)) return 'This run name is reserved by the filesystem'
  if (RESERVED_RUN_IDS.has(runId.toLowerCase())) return 'This run name is reserved by LoopLab'
  return ''
}

export function validateLaunchDraft(draft) {
  const errors = {}
  const ridError = runIdError(draft?.run_id)
  if (ridError) errors.run_id = ridError

  let task = null
  if (draft?.source === 'task_file') {
    if (!String(draft.task_file || '').trim()) errors.task_file = 'Choose or enter a task file'
  } else if (draft?.source === 'task') {
    const parsed = parseObjectJson(draft.task_json, 'Task')
    if (!parsed.ok) errors.task = parsed.error
    else if (!Object.keys(parsed.value).length) errors.task = 'Task cannot be empty'
    else task = parsed.value
  } else {
    errors.source = 'Choose an inline task or task file'
  }

  const parsedSettings = parseObjectJson(draft?.settings_json, 'Settings')
  if (!parsedSettings.ok) errors.settings = parsedSettings.error
  const settings = parsedSettings.ok ? parsedSettings.value : null
  if (settings) {
    for (const field of LAUNCH_RUNTIME_FIELDS) {
      const value = settings[field.key]
      if (value == null || value === '') continue
      if (field.type === 'int' && (!Number.isInteger(value)
          || (field.min != null && value < field.min)
          || (field.max != null && value > field.max))) {
        const range = field.min != null && field.max != null
          ? ` between ${field.min} and ${field.max}`
          : field.min != null ? ` of at least ${field.min}` : ''
        errors[`settings.${field.key}`] = `${field.label} must be an integer${range}`
      }
      if (field.type === 'float' && (typeof value !== 'number' || !Number.isFinite(value)
          || (field.min != null && value <= field.min))) {
        errors[`settings.${field.key}`] = `${field.label} must be a number greater than ${field.min}`
      }
    }
  }
  return { ok: Object.keys(errors).length === 0, errors, task, settings }
}

const cleanChat = chat => (Array.isArray(chat) ? chat : [])
  .filter(turn => turn && (turn.role === 'user' || turn.role === 'assistant') && String(turn.content || '').trim())
  .map(turn => ({ role: turn.role, content: String(turn.content) }))

export function buildLaunchBody(draft, chat = []) {
  const checked = validateLaunchDraft(draft)
  if (!checked.ok) return { ok: false, errors: checked.errors }
  const body = {
    run_id: String(draft.run_id),
    settings: checked.settings,
  }
  if (draft.source === 'task_file') body.task_file = String(draft.task_file).trim()
  else body.task = checked.task
  const provenance = cleanChat(chat)
  if (provenance.length) body.chat = provenance
  return { ok: true, body }
}

const sortObject = value => {
  if (Array.isArray(value)) return value.map(sortObject)
  if (!value || typeof value !== 'object') return value
  return Object.fromEntries(Object.keys(value).sort().map(key => [key, sortObject(value[key])]))
}

// The token itself is server-issued.  This fingerprint only ensures that the controls still describe
// the exact payload which received that token; /api/start repeats the authoritative validation.
export function launchFingerprint(draft, chat = []) {
  const built = buildLaunchBody(draft, chat)
  return built.ok ? JSON.stringify(sortObject(built.body)) : ''
}

export function runtimeValue(draft, key) {
  const parsed = parseObjectJson(draft?.settings_json, 'Settings')
  return parsed.ok && parsed.value[key] != null ? parsed.value[key] : ''
}

export function updateRuntimeValue(draft, field, rawValue) {
  const parsed = parseObjectJson(draft?.settings_json, 'Settings')
  if (!parsed.ok) return { ok: false, error: parsed.error }
  const settings = clone(parsed.value)
  if (rawValue === '' || rawValue == null) delete settings[field.key]
  else if (field.type === 'int' || field.type === 'float') {
    // Number(), not parseInt(): "3.5" must remain 3.5 so validation rejects a non-integer instead of
    // silently changing the user's budget to 3.
    const value = Number(rawValue)
    if (!Number.isFinite(value)) return { ok: false, error: `${field.label} must be a number` }
    settings[field.key] = value
  } else settings[field.key] = rawValue
  return { ok: true, draft: { ...draft, settings_json: pretty(settings) } }
}

const short = (value, max = 120) => {
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > max ? text.slice(0, max - 1) + '…' : text
}

export function summarizeLaunchTask(draft) {
  if (draft?.source === 'task_file') {
    const path = String(draft.task_file || '')
    return [{ label: 'Source', value: path ? path.split(/[\\/]/).pop() : 'No task file selected' }]
  }
  const parsed = parseObjectJson(draft?.task_json, 'Task')
  if (!parsed.ok) return [{ label: 'Task', value: 'Fix the JSON to preview this task', invalid: true }]
  const task = parsed.value
  const kind = task.kind || (task.repo || task.editable_path || task.editables ? 'repo'
    : task.kaggle || task.competition ? 'Kaggle'
      : task.dataset || task.data || task.data_path ? 'dataset'
        : task.benchmark || 'composable')
  const rows = [{ label: 'Type', value: String(kind) }]
  if (task.goal) rows.push({ label: 'Goal', value: short(task.goal) })
  if (task.direction) rows.push({ label: 'Direction', value: String(task.direction) })
  const source = task.repo || task.editable_path || task.dataset || task.data || task.data_path || task.competition || task.kaggle
  if (source) rows.push({ label: 'Source', value: short(source) })
  const evaluation = task.cmd || task.eval
  if (evaluation) {
    const command = Array.isArray(evaluation) ? evaluation : evaluation.command || evaluation.stages || evaluation
    rows.push({ label: 'Evaluation', value: short(command) })
    const metric = !Array.isArray(evaluation) && evaluation.metric
    if (metric) rows.push({ label: 'Metric', value: short(metric) })
  }
  return rows
}
