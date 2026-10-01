import React from 'react'
import './harness-next-step.css'

const codes = new Set(['inspect_sources', 'answer_checkpoint', 'inspect_lifecycle',
  'inspect_pending', 'choose_direction'])

// Older servers omit this optional field. A malformed new summary is a failed read,
// never a reason to replace a missing obligation with optimistic client advice.
export function validHarnessNextStep(value) {
  return value == null || (typeof value === 'object' && !Array.isArray(value)
    && codes.has(value.code) && value.owner === 'external_agent'
    && typeof value.title === 'string' && typeof value.detail === 'string'
    && Array.isArray(value.reads) && value.reads.every(ref => typeof ref === 'string')
    && (value.action === null || typeof value.action === 'string')
    && (value.phase_id === null || typeof value.phase_id === 'string'))
}

export default function HarnessNextStep({ step, runId, fresh }) {
  if (!step) return null
  if (!fresh) return <p role="status" className="report-inline-state">
    Next step unavailable until Agent cycle refresh succeeds. The journals below are the last read.
  </p>
  const ref = value => value.replaceAll('{run_id}', runId)
  return <section className="harness-next-step" aria-label="External agent next step">
    <h3>Next step · {step.title}</h3>
    <p>{step.detail}</p>
    <p className="muted">Responsible: external agent.</p>
    <details>
      <summary>Read and response references</summary>
      <ul>{step.reads.map((value, index) => <li key={index} style={{ overflowWrap: 'anywhere' }}>
        {ref(value)}</li>)}</ul>
      {step.phase_id && <p>Read MCP phase_info: {step.phase_id}</p>}
      {step.action && <p style={{ overflowWrap: 'anywhere' }}>Submit an explicit response via {ref(step.action)}.</p>}
      <p className="muted">Use the current run generation. Refresh after an event or response.</p>
    </details>
  </section>
}
