import { uiMessage, uiText, useUILanguage } from './uiLanguage.js'
import React from 'react'
import { DataTable } from './accessibility.jsx'
import { fmtCI, fmtP, fmtStat, fmtWhen } from './campaignModel.js'

// The two tables the campaign surfaces share (doc 81): an experiment's paired contrasts and a
// shared-test leaderboard. Every cell is the SERVER's computation (`harness/per_query.py`), never
// re-derived here; the provenance flags ride in their own column beside the number they qualify.

export function Flags({ flags }) {
  useUILanguage()
  return <>{flags.map((flag, i) => <span key={i} title={uiText(flag.title || '')}
    className={`pill ${flag.tone === 'ok' ? 'ok' : 'warn'}`} style={{ marginRight: 4 }}>
    {uiText(flag.label)}</span>)}</>
}

export function ContrastTable({ rows, caption, csvName }) {
  useUILanguage()
  const columns = [
    { key: 'id', label: uiText('Contrast'), firstColumnHeader: true,
      render: (_v, row) => <span title={row.reason || ''}>{row.treatment} − {row.control}</span> },
    { key: 'metric', label: uiText('Metric @ test'), value: row => `${row.metric} @ ${row.test}` },
    { key: 'family', label: uiText('Family'),
      value: row => (row.familySize ? `${row.family} (${row.familySize})` : row.family) },
    { key: 'delta', label: uiText('Δ (paired)'), numeric: true, value: row => fmtStat(row.delta) },
    { key: 'ci', label: uiText('95% CI'), value: row => fmtCI(row.low, row.high) },
    { key: 'p', label: uiText('p'), numeric: true, value: row => fmtP(row.p) },
    { key: 'pAdj', label: uiText('p (adjusted)'), numeric: true,
      render: (_v, row) => <span className={row.significant ? 'ok' : undefined}>{fmtP(row.pAdj)}</span> },
    { key: 'arms', label: uiText('Treatment / control'),
      value: row => `${row.treatmentWhere} / ${row.controlWhere}` },
    { key: 'users', label: uiText('Users'), numeric: true, value: row => row.clusters ?? '—' },
    { key: 'flags', label: uiText('Provenance'), render: (_v, row) => <Flags flags={row.flags} /> },
  ]
  return <DataTable caption={uiText(caption)} columns={columns} rows={rows} csvName={csvName}
    rowKey={row => row.id} empty={uiText('No declared contrasts')} />
}

export function LeaderboardTable({ board }) {
  useUILanguage()
  const columns = [
    { key: 'where', label: uiText('Run · node'), firstColumnHeader: true,
      value: row => `${row.runId}#${row.nodeId ?? '?'}` },
    { key: 'label', label: uiText('Arm / idea'), value: row => row.label || '—' },
    { key: 'value', label: board.metric || uiText('value'), numeric: true, value: row => fmtStat(row.value) },
    { key: 'ci', label: uiText('95% CI (users)'), value: row => fmtCI(row.low, row.high) },
    { key: 'check', label: uiText('Measured check'),
      value: row => (row.stale ? uiText('stale') : uiText(row.consistency || 'unchecked')) },
    { key: 'author', label: uiText('Uploaded by'), value: row => uiText(row.author) },
  ]
  return <DataTable caption={uiMessage('Shared test {0}', [board.label || board.testId])}
    columns={columns} rows={board.rows} csvName={`leaderboard-${board.testId}`}
    rowKey={row => `${row.runId}:${row.nodeId}`} empty={uiText('No uploads for this test')} />
}

export function Registration({ experiment }) {
  useUILanguage()
  return <span className="muted">
    {uiMessage('registered {0} by {1}', [fmtWhen(experiment.registeredAt), uiText(experiment.author)])}
    {experiment.registeredSeq != null && uiMessage(' at event #{0}', [experiment.registeredSeq])}
  </span>
}
