const DIRECT_MODE_DECISIONS = Object.freeze({
  plan: 'deny',
  default: 'ask',
  acceptEdits: 'ask',
  auto: 'inline',
})

const DIRECT_PRESENTATIONS = Object.freeze({
  stop: {
    title: 'Pause this run?',
    actionLabel: 'Pause run',
    consequence: 'Stops active work at a safe boundary. The run can be resumed later.',
  },
  pause: {
    title: 'Pause this run?',
    actionLabel: 'Pause run',
    consequence: 'Stops active work at a safe boundary. The run can be resumed later.',
  },
  finalize: {
    title: 'Finalize this run?',
    actionLabel: 'Finalize run',
    consequence: 'Stops active work and begins the durable report, lessons, and cost wrap-up.',
    danger: true,
  },
  abort: {
    title: 'Finalize this run?',
    actionLabel: 'Finalize run',
    consequence: 'Stops active work and begins the durable report, lessons, and cost wrap-up.',
    danger: true,
  },
  resume: {
    title: 'Resume this run?',
    actionLabel: 'Resume run',
    consequence: 'Restarts run execution and may consume compute time or paid model budget.',
  },
  ratify: {
    title: 'Ratify this evaluation spec?',
    actionLabel: 'Ratify spec',
    consequence: 'Accepts the current evaluation specification so the run can continue.',
  },
  approve: {
    title: 'Approve this experiment?',
    actionLabel: 'Approve experiment',
    consequence: 'Accepts the currently pending experiment result so the run can continue.',
  },
})

// Stop/finalize/resume mirror the server's CONSEQUENTIAL row in perm_modes.py. The direct human
// ratify/approve shortcuts deliberately use the same conservative matrix instead of becoming a
// second, weaker approval surface. Unknown or future modes fail closed.
export function assistantDirectDecision(mode) {
  return Object.hasOwn(DIRECT_MODE_DECISIONS, mode) ? DIRECT_MODE_DECISIONS[mode] : 'deny'
}

export function assistantDirectPresentation(name, arg, runId) {
  const commandName = typeof name === 'string' ? name : ''
  const commandArg = Number.isSafeInteger(arg) ? ` #${arg}` : ''
  const fallback = {
    title: 'Run this command?',
    actionLabel: 'Run command',
    consequence: 'Changes the state of the selected run.',
  }
  const presentation = Object.hasOwn(DIRECT_PRESENTATIONS, commandName)
    ? DIRECT_PRESENTATIONS[commandName] : fallback
  return {
    ...presentation,
    commandText: `/${commandName}${commandArg}`,
    runId: String(runId || ''),
  }
}
