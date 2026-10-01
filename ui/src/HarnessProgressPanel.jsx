import React, { useState } from 'react'
import { get, runApiPath } from './util.js'
import { useScopedResource } from './useScopedResource.js'
import Panel from './PanelShell.jsx'
import HarnessNextStep, { validHarnessNextStep } from './HarnessNextStep.jsx'
import HarnessHandoff from './HarnessHandoff.jsx'
import AgentActivity from './AgentActivity.jsx'
import { invalidPanelPayload, isRecord, PANEL_REQUEST_TIMEOUT_MS, RUN_GENERATION_RE } from './panelPrimitives.js'
import { PanelResourceNotice } from './PanelResourceNotice.jsx'

// External decisions live in three durable sidecars, not in the folded event log. This read model
// keeps the intermediate reasoning receipts inspectable after a client/server restart and labels
// old evidence explicitly; it never pretends that a receipt for one Idea satisfies another Idea.
export function HarnessProgressPanel({ runId, expectedGeneration, seq, externalMode, configStatus,
  onOpenEvents, onClose, compact = false, engineRunning, onOpen }) {
  const [offset, setOffset] = useState(0)
  const validGeneration = RUN_GENERATION_RE.test(expectedGeneration || '')
  const scope = validGeneration && externalMode === true
    ? `${runId}:${expectedGeneration}:${compact ? `brief:${engineRunning}` : offset}` : ''
  const resource = useScopedResource(signal => get(
    runApiPath(runId, '/harness-progress')
      + `?expected_generation=${expectedGeneration}&`
      + (compact ? 'brief=true' : `offset=${offset}&limit=20`),
    { cache: 'no-store', signal }).then(value => {
    if (!isRecord(value) || value.generation !== expectedGeneration
        || !validHarnessNextStep(value.next_step)
        || (compact ? !value.next_step || !Number.isSafeInteger(value.event_seq) || value.event_seq < 0
          || ![true, false, null].includes(value.execution?.engine_running)
          : !isRecord(value.history) || !isRecord(value.source_health)
        || !isRecord(value.candidate_requirements)
        || !Array.isArray(value.candidate_blockers_if_expanding)
        || !Array.isArray(value.finish_pending_nodes)
        || !Array.isArray(value.pending_checkpoints)
        || ['decisions', 'reviews', 'checkpoints'].some(
          kind => !isRecord(value.history[kind]) || !Array.isArray(value.history[kind].items)
            || !Number.isSafeInteger(value.history[kind].total)
            || !isRecord(value.source_health[kind])
            || !Number.isSafeInteger(value.source_health[kind].accepted_rows)))) {
      invalidPanelPayload()
    }
    return value
  }), { scope, gate: scope ? null : 'idle', timeout: PANEL_REQUEST_TIMEOUT_MS,
    pollMs: 10_000, deps: [seq, engineRunning] })
  const progress = resource.data
  // Engine observations can change without an event. The periodic read owns this label;
  // an engine prop change fences the old scope but must not override a newer server probe.
  if (compact) {
    const fresh = resource.status === 'ready' && progress?.event_seq >= seq
    return <section className="topbar" aria-label="External agent status">
      <span className="muted">External agent</span>
      {fresh ? <details className="spacer"><summary><b>{progress.next_step.title}</b></summary>
        <p>{progress.next_step.detail}</p></details>
        : <span role="status">Next step unavailable</span>}
      {!fresh && <span className="spacer" />}
      <AgentActivity activity={progress?.agent_activity} fresh={fresh} />
      {!fresh && <button type="button" className="btn sm ghost"
        onClick={() => resource.retry({ supersede: true })}>Retry</button>}
      <button type="button" className="btn sm" onClick={onOpen}>Agent cycle</button>
    </section>
  }
  const history = progress?.history
  const blockers = progress?.candidate_blockers_if_expanding || []
  const perIdea = Object.entries(progress?.candidate_decisions_per_idea || {})
  const hasMore = ['decisions', 'reviews', 'checkpoints'].some(kind => history?.[kind]?.has_more)
  const historyRow = (kind, row, index) => {
    const receipt = kind === 'checkpoints' ? row.question : row
    const answer = kind === 'checkpoints' ? row.answer : null
    return <li key={`${kind}-${receipt.action_id || receipt.checkpoint_id || index}`} className="ov-row">
      <b>{receipt.phase_id || kind}</b>{' '}
      <span className="chip">{row.status || row.validity || 'recorded'}</span>{' '}
      {row.lifecycle === 'superseded' && <span className="chip">old evaluator attempt</span>}
      {receipt.at_node != null && <span className="muted">at node {receipt.at_node} · </span>}
      {receipt.stage && <span>{receipt.stage} · </span>}
      {receipt.decision || answer?.verdict || ''}
      {(receipt.reason || answer?.reason) && <div>{receipt.reason || answer.reason}</div>}
      {(receipt.expectation || receipt.observation) && <details>
        <summary>Observed checkpoint</summary>
        {receipt.expectation && <p>Expected: {receipt.expectation}</p>}
        {receipt.observation && <pre className="log-tail">{receipt.observation}</pre>}
      </details>}
      <div className="muted">{receipt.action_id || receipt.checkpoint_id}
        {receipt.action_ref ? ` · action ${receipt.action_ref}` : ''}</div>
    </li>
  }
  return <Panel title="External agent cycle" sub={progress ? `event #${progress.event_seq}` : runId}
    onClose={onClose} wide>
    {configStatus === 'ready' && externalMode !== true && <p role="status">
      This run uses LoopLab's built-in agent cycle. The external agent journals apply to runs
      launched with external_harness=true.</p>}
    {configStatus !== 'ready' && <p role="status">Waiting for run settings…</p>}
    {!validGeneration && <p className="muted" role="status">Waiting for a durable run generation…</p>}
    {scope && <PanelResourceNotice resource={resource} label="Agent cycle"
      onRetry={() => resource.retry()} />}
    {scope && <HarnessHandoff runId={runId} generation={expectedGeneration} seq={seq} />}
    {progress && <>
      {!progress.complete && <div className="report-inline-state error" role="alert">
        An event, decision, review or checkpoint journal has damaged rows. History below may be partial;
        inspect source health before treating a missing receipt as never written.
        <pre>{JSON.stringify(progress.source_health, null, 2)}</pre>
      </div>}
      <p className="muted">Measured prefix: {progress.at_node} nodes, event #{progress.event_seq}.
        This is a read of several durable journals; refresh after a new event or response.</p>
      <HarnessNextStep step={progress.next_step} runId={runId}
        fresh={resource.status === 'ready' && !(seq > progress.event_seq)} />
      <AgentActivity activity={progress.agent_activity}
        fresh={resource.status === 'ready' && !(seq > progress.event_seq)} />
      <p className="muted">Journal rows: decisions {progress.source_health.decisions.accepted_rows},
        reviews {progress.source_health.reviews.accepted_rows}, checkpoints
        {' '}{progress.source_health.checkpoints.accepted_rows}. The event timeline records
        node and command transitions separately.{' '}
        <button type="button" className="btn sm ghost" onClick={onOpenEvents}>Open events</button></p>
      <h3>Before another candidate</h3>
      {blockers.length ? <ul>{blockers.map((item, index) => <li key={`${item.phase_id}-${index}`}>
        <b>{item.phase_id}</b> · {item.action.replaceAll('{run_id}', runId)}
      </li>)}</ul> : <p>No current external-cycle gate. Candidate-specific checks, budgets and
        the task's edit surface still apply.</p>}
      <h3>For each proposed Idea</h3>
      {progress.candidate_requirements.effective_concepts && <p>Effective concept tags are
        required on every submitted candidate.</p>}
      {progress.candidate_requirements.hypothesis_statement && <p>A nonempty hypothesis statement
        is required on every submitted candidate; injection creates a new Card.</p>}
      {perIdea.length ? <ul>{perIdea.map(([name, count]) => <li key={name}>
        {name}: review {count} option{count === 1 ? '' : 's'} for the exact Idea
      </li>)}</ul> : <p>No configured idea-specific review at this node count.</p>}
      <h3>Evaluation questions</h3>
      <p>{progress.pending_checkpoint_count} pending. Answers remain in the checkpoint history below.</p>
      {progress.pending_checkpoints.map(row => <div key={row.question.checkpoint_id} className="ov-row">
        <b>{row.question.phase_id}</b> · node {row.question.node_id} · {row.question.stage || 'live observation'}
        {row.question.expectation && <div>{row.question.expectation}</div>}
      </div>)}
      {progress.pending_checkpoints_truncated && <p className="muted">More questions: use the
        harness-checkpoints API.</p>}
      <h3>Before finalizing</h3>
      {progress.finish_pending_nodes.length > 0 && <p>Wait for or explicitly abort pending nodes:
        {' '}{progress.finish_pending_nodes.join(', ')}.</p>}
      <p>{progress.finish_report_due ? 'Current run report required. ' : ''}
        {progress.finish_reviews_due.length
          ? `Reviews due: ${progress.finish_reviews_due.join(', ')}`
          : 'No knowledge reviews due at this measured prefix.'}</p>
      <h3>Recorded intermediate actions</h3>
      <p className="muted">A current decision is still bound to its exact Idea and implementation.
        Superseded receipts remain visible for audit.</p>
      {['decisions', 'reviews', 'checkpoints'].map(kind => <section key={kind}>
        <h4>{kind} · {history[kind].total}</h4>
        {history[kind].items.length ? <ul>{history[kind].items.map((row, index) =>
          historyRow(kind, row, index))}</ul> : <p className="muted">No entries on this page.</p>}
      </section>)}
      <div className="panel-actions">
        <button type="button" className="btn sm" disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - 20))}>Newer</button>
        <button type="button" className="btn sm" disabled={!hasMore}
          onClick={() => setOffset(offset + 20)}>Older</button>
        <span className="muted">Page {Math.floor(offset / 20) + 1} of each journal</span>
      </div>
    </>}
  </Panel>
}
