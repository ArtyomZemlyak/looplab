// The resources ONE attempt used (doc 77 §4, UI review 2026-10-14 section 7.8) — the pure half of
// `ResourceSnapshotPanel.jsx`.
//
// Three kinds of number, never mixed: MEASURED (what the engine timed — stage wall clock, the eval's
// wall time — and what the trainer itself reported, under its own metric names and units), DERIVED
// (arithmetic over measured numbers and a declared count, with the assumption said beside it) and
// ABSENT (not recorded: shown as such, never estimated). The attempt is the node's CURRENT lifecycle:
// stage rows of an earlier generation (a reset) are not this attempt's time.

const finite = value => typeof value === 'number' && Number.isFinite(value)
const count = value => (Number.isSafeInteger(value) && value > 0 ? value : null)
const TRAIN_STAGE = /(^|[_\-. ])(train|training|sft|fit|finetune|fine_tune)([_\-. ]|$)/i
const TRAINER_RATE = /(samples|steps|tokens|examples|rows)_per_second$|^train_runtime$|throughput/i
const PEAK_MEMORY = /peak|max_memory|max_mem|memory_(allocated|reserved)|mem_(gb|mib|mb)$/i

function currentStageRows(node) {
  const attempt = Number.isSafeInteger(node?.attempt) ? node.attempt : 0
  const ledger = Array.isArray(node?.stage_attempts) ? node.stage_attempts : []
  const rows = ledger.filter(row => row && typeof row.name === 'string'
    && (row.generation == null || row.generation === attempt))
  if (rows.length) return rows
  // An older log carries only the per-name projection.
  return Array.isArray(node?.stages) ? node.stages.filter(row => row && typeof row.name === 'string') : []
}

export function resourceSnapshot(node, { effectiveConfig = null } = {}) {
  if (!node || typeof node !== 'object') return null
  const rows = currentStageRows(node)
  const byName = new Map()
  for (const row of rows) {
    const entry = byName.get(row.name) || { name: row.name, seconds: 0, runs: 0, status: '', timed: false }
    if (finite(row.seconds) && row.seconds >= 0) { entry.seconds += row.seconds; entry.timed = true }
    entry.runs += 1
    entry.status = typeof row.status === 'string' ? row.status : entry.status
    byName.set(row.name, entry)
  }
  const stages = [...byName.values()]
  const stageSeconds = stages.some(stage => stage.timed)
    ? stages.reduce((total, stage) => total + stage.seconds, 0) : null
  const footprint = node.idea?.footprint || node.footprint || null
  const gpus = count(footprint?.gpus)
  const effective = effectiveConfig?.effective && typeof effectiveConfig.effective === 'object'
    ? effectiveConfig.effective : {}
  const worldSize = count(effective.world_size)
  const maxSteps = count(effective.max_steps)
  const train = stages.find(stage => TRAIN_STAGE.test(stage.name) && stage.timed && stage.seconds > 0) || null
  const extras = node.extra_metrics && typeof node.extra_metrics === 'object' ? node.extra_metrics : {}
  const reported = (pattern) => Object.entries(extras)
    .filter(([key, value]) => pattern.test(key) && finite(value))
    .map(([key, value]) => ({ key, value }))
    .sort((a, b) => a.key.localeCompare(b.key))
  return {
    attempt: Number.isSafeInteger(node.attempt) ? node.attempt : 0,
    repairs: Number.isSafeInteger(node.repairs) ? node.repairs : 0,
    gpus, worldSize,
    stages,
    stageSeconds,
    wallSeconds: finite(node.eval_seconds) ? node.eval_seconds : null,
    // gpus × the time the stages ran: the GPUs the experiment DECLARED, assumed held for every stage.
    gpuHours: gpus != null && stageSeconds != null ? (gpus * stageSeconds) / 3600 : null,
    train: train ? { name: train.name, seconds: train.seconds } : null,
    // max_steps over the training stage's wall clock: includes start-up and checkpointing.
    stepsPerSecond: train && maxSteps != null ? maxSteps / train.seconds : null,
    maxSteps,
    trainerRates: reported(TRAINER_RATE),
    peakMemory: reported(PEAK_MEMORY),
  }
}

export const fmtSeconds = seconds => {
  if (!finite(seconds)) return '—'
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`
  if (seconds < 5400) return `${(seconds / 60).toFixed(1)} min`
  return `${(seconds / 3600).toFixed(2)} h`
}
