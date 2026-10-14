import { uiMessage, uiPlural, uiText, useUILanguage } from './uiLanguage.js'
import React, { useState } from 'react'
import PanelShell from './PanelShell.jsx'
import { PanelResourceNotice } from './PanelResourceNotice.jsx'
import { DataTable } from './accessibility.jsx'
import { get } from './api.js'
import { useScopedResource } from './useScopedResource.js'
import { comparisonView, fmtWhen, protocolView } from './campaignModel.js'
import { ContrastTable, Registration } from './CampaignTables.jsx'

// The run's CAMPAIGN PROTOCOL (doc 81): the experiments pre-registered in this run, LoopLab's paired
// comparison for each, and the amendment audit trail — who changed the task, a base file or the
// budget, when, why, and the diff. Read-only: amendments are written by the external agent (or the
// operator) through `POST /api/runs/{id}/protocol/amendments`.
const TIMEOUT_MS = 20_000

function ExperimentComparison({ runId, experiment }) {
  useUILanguage()
  const [open, setOpen] = useState(false)
  const resource = useScopedResource(
    signal => get(`/api/runs/${encodeURIComponent(runId)}/experiments/${encodeURIComponent(experiment.id)}/comparison`, { signal }),
    { scope: `${runId}:${experiment.id}`, gate: open ? null : 'idle', timeout: TIMEOUT_MS })
  const view = resource.data ? comparisonView(resource.data) : null
  return <div className="protocol-experiment">
    <div className="protocol-experiment-head">
      <b>{experiment.title}</b> <code>{experiment.id}</code>
      {experiment.superseded && <span className="pill warn" style={{ marginLeft: 6 }}>{uiText('superseded')}</span>}
      {experiment.supersedes && <span className="muted">{uiMessage(' · supersedes {0}', [experiment.supersedes])}</span>}
      {' · '}<Registration experiment={experiment} />
    </div>
    {experiment.question && <div className="muted">{uiText('Question: ')}{experiment.question}</div>}
    <div>{uiText('Hypothesis: ')}{experiment.hypothesis}</div>
    <div className="muted">{uiText('Arms: ')}{experiment.arms.map(arm =>
      `${arm.label}${arm.runId ? ` (${arm.runId}#${arm.nodeId ?? '?'})` : arm.nodeId != null ? ` (#${arm.nodeId})` : ''}`).join(', ')}</div>
    <button type="button" className="btn sm" aria-expanded={open} onClick={() => setOpen(v => !v)}>
      {open ? uiText('Hide comparison') : uiPlural(experiment.contrasts, 'Compute {0} contrast', 'Compute {0} contrasts')}</button>
    {open && <>
      <PanelResourceNotice resource={resource} label={uiText('Paired comparison')} onRetry={() => resource.retry()} />
      {view && <>
        <ContrastTable rows={view.rows} caption={uiMessage('Paired contrasts of {0}', [experiment.id])}
          csvName={`contrasts-${experiment.id}`} />
        <div className="muted">{uiMessage('Computed by LoopLab ({0}; {1}; correction: {2}) at {3}.',
          [view.method, view.ci, view.correction, fmtWhen(view.computedAt)])}</div>
        {view.caveats.length > 0 && <ul className="muted">{view.caveats.map((c, i) => <li key={i}>{c}</li>)}</ul>}
      </>}
    </>}
  </div>
}

export default function ProtocolPanel({ runId, onClose }) {
  useUILanguage()
  const resource = useScopedResource(
    signal => get(`/api/runs/${encodeURIComponent(runId)}/protocol`, { signal }),
    { scope: runId, timeout: TIMEOUT_MS })
  const view = resource.data ? protocolView(resource.data) : null
  const amendmentColumns = [
    { key: 'version', label: uiText('#'), numeric: true, firstColumnHeader: true },
    { key: 'kind', label: uiText('Kind'), value: row => uiText(row.kind) },
    { key: 'author', label: uiText('Author'), value: row => uiText(row.author) },
    { key: 'at', label: uiText('When'), value: row => `${fmtWhen(row.at)}${row.atSeq != null ? ` · #${row.atSeq}` : ''}` },
    { key: 'summary', label: uiText('What'), value: row => row.summary },
    { key: 'reason', label: uiText('Reason'), value: row => row.reason },
    { key: 'diff', label: uiText('Diff'), render: (_v, row) => row.diffs.length === 0 ? '—'
      : row.diffs.map((d, i) => <details key={i}><summary>{d.path}{d.protected ? uiText(' (protected)') : ''}</summary>
        <pre className="protocol-diff">{d.diff}</pre></details>) },
  ]
  return <PanelShell title={uiText('Protocol & comparisons')} wide onClose={onClose}
    sub={view ? uiMessage('protocol version {0} · agent may amend: {1}{2}', [view.version,
      uiText(view.policy.amendments), view.policy.cap != null ? uiMessage(' · node cap {0}', [view.policy.cap]) : '']) : ''}>
    <PanelResourceNotice resource={resource} label={uiText('Run protocol')} onRetry={() => resource.retry()} />
    {view && view.experiments.length === 0 && view.amendments.length === 0 && <div className="notice" role="status">
      {uiText('No experiment is pre-registered in this run and nothing was amended. An external agent registers one with amend_protocol (kind=experiment) or submit_experiment.')}</div>}
    {view && view.experiments.length > 0 && <section>
      <h3>{uiText('Pre-registered experiments')}</h3>
      {view.experiments.map(exp => <ExperimentComparison key={exp.id} runId={runId} experiment={exp} />)}
    </section>}
    {view && view.amendments.length > 0 && <DataTable caption={uiText('Amendment audit trail')}
      columns={amendmentColumns} rows={view.amendments} rowKey={row => row.id}
      csvName={`amendments-${runId}`} />}
  </PanelShell>
}
