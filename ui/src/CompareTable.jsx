import { uiText, uiMessage, uiPlural, useUILanguage } from './uiLanguage.js'
// Experiments × parameters × metrics, for any number of experiments (UI review 2026-10-14 section
// 7.7). Decisions live in `compareTableModel.js`; this half keeps the selection, the sort and the one
// read it needs — the attempts' effective configs from `GET /code`, fetched when the table opens.
import React, { useMemo, useState } from 'react'

import { get, runApiPath } from './api.js'
import { downloadBlob } from './accessibility.jsx'
import { useScopedResource } from './hooks.js'
import { compareColumns, compareCsv, compareRows, sortRows } from './compareTableModel.js'
import './compare-table.css'

const GROUP_LABEL = { params: 'idea', config: 'ran with', metrics: 'metric' }
const show = value => (value === undefined ? '' : typeof value === 'number'
  ? (Number.isInteger(value) ? String(value) : value.toPrecision(4)) : String(value))

export default function CompareTable({ state, runId, reviewMode = false }) {
  useUILanguage()
  const nodes = state?.nodes || {}
  const allIds = useMemo(() => Object.keys(nodes).map(Number).filter(Number.isSafeInteger)
    .sort((a, b) => a - b), [nodes])
  const evaluated = allIds.filter(id => typeof nodes[id]?.metric === 'number')
  const [picked, setPicked] = useState(() => new Set(evaluated.length ? evaluated : allIds))
  const [onlyDiffering, setOnlyDiffering] = useState(true)
  const [metricFilter, setMetricFilter] = useState('')
  const [sort, setSort] = useState({ column: { group: 'metric' }, descending: state?.direction !== 'min' })
  // The effective config of each CURRENT attempt (doc 79 §6) — owner reads only.
  const code = useScopedResource(signal => get(runApiPath(runId, '/code'), { signal }),
    { scope: reviewMode || !runId ? '' : `${runId}:compare-table`, gate: reviewMode || !runId ? 'idle' : null })
  const configs = useMemo(() => new Map((code.data?.attempts || [])
    .filter(row => row?.current && row.effective_config)
    .map(row => [`${row.node_id}:${row.generation}`, row.effective_config])), [code.data])
  const rows = useMemo(() => compareRows(state, { nodeIds: [...picked], effectiveConfigs: configs }),
    [state, picked, configs])
  const { columns, hidden } = useMemo(() => compareColumns(rows, { onlyDiffering, metricFilter }),
    [rows, onlyDiffering, metricFilter])
  const ordered = useMemo(() => sortRows(rows, sort.column, sort.descending), [rows, sort])
  const toggle = id => setPicked(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })
  const sortBy = column => setSort(prev => ({
    column, descending: prev.column?.group === column.group && prev.column?.name === column.name
      ? !prev.descending : false,
  }))
  const arrow = column => (sort.column?.group === column.group && sort.column?.name === column.name
    ? (sort.descending ? ' ↓' : ' ↑') : '')
  return <section className="compare-table" aria-label={uiText('Experiments, parameters and metrics')}>
    <div className="toolbar compare-table-tools">
      <details className="compare-table-pick">
        <summary>{uiPlural(picked.size, '{0} experiment selected', '{0} experiments selected')}</summary>
        <div className="compare-table-pick-list">
          <button type="button" className="btn xs" onClick={() => setPicked(new Set(allIds))}>{uiText('all')}</button>
          <button type="button" className="btn xs" onClick={() => setPicked(new Set(evaluated))}>{uiText('evaluated')}</button>
          <button type="button" className="btn xs" onClick={() => setPicked(new Set())}>{uiText('none')}</button>
          {allIds.map(id => <label key={id}><input type="checkbox" checked={picked.has(id)} onChange={() => toggle(id)} /> #{id}</label>)}
        </div>
      </details>
      <label><input type="checkbox" checked={onlyDiffering} onChange={e => setOnlyDiffering(e.target.checked)} /> {uiText('only columns that differ')}</label>
      <input type="search" className="text" value={metricFilter} placeholder={uiText('filter metrics…')}
        aria-label={uiText('Filter metric columns')} onChange={e => setMetricFilter(e.target.value.slice(0, 80))} />
      <button type="button" className="btn sm" disabled={!rows.length}
        onClick={() => downloadBlob(`${state?.run_id || runId}_experiments.csv`, [compareCsv(ordered, columns)], 'text/csv')}>
        {uiText('Download CSV')}</button>
    </div>
    {!rows.length ? <p className="muted">{uiText('Select experiments to compare.')}</p>
      : <div className="md-table-wrap compare-table-wrap" role="region" tabIndex={0}
          aria-label={uiText('Experiment comparison table')}>
        <table className="tbl md-table">
          <thead><tr>
            <th scope="col"><button type="button" className="linklike" onClick={() => sortBy({ group: 'id' })}>{uiText('experiment')}{arrow({ group: 'id' })}</button></th>
            <th scope="col">{uiText('status')}</th>
            <th scope="col"><button type="button" className="linklike" onClick={() => sortBy({ group: 'metric' })}>{uiText('metric')}{arrow({ group: 'metric' })}</button></th>
            {columns.map(column => <th scope="col" key={`${column.group}:${column.name}`} className={`ct-${column.group}`}>
              <button type="button" className="linklike" onClick={() => sortBy(column)}
                title={uiText(GROUP_LABEL[column.group])}>
                <span className="muted">{uiText(GROUP_LABEL[column.group])}</span> <code>{column.name}</code>{arrow(column)}</button></th>)}
          </tr></thead>
          <tbody>{ordered.map(row => <tr key={row.id}>
            <th scope="row">#{row.id}{row.attempt ? <span className="muted"> · {uiMessage('gen {0}', [row.attempt])}</span> : null}</th>
            <td>{row.status ? uiText(row.status) : ''}</td>
            <td className="num">{show(row.metric)}</td>
            {columns.map(column => <td key={`${column.group}:${column.name}`}
              className={typeof row[column.group][column.name] === 'number' ? 'num' : ''}>{show(row[column.group][column.name])}</td>)}
          </tr>)}</tbody>
        </table>
      </div>}
    <p className="muted">
      {uiText('Empty cell: the experiment does not have that value (never zero). "idea" is what the experiment asked for; "ran with" is the effective config its attempt recorded.')}
      {hidden > 0 && <> {uiPlural(hidden, '{0} more column is not shown; narrow the metric filter.', '{0} more columns are not shown; narrow the metric filter.')}</>}
      {!reviewMode && code.status === 'error' && <> {uiText('The effective configs could not be read; the "ran with" columns are missing.')}</>}
    </p>
  </section>
}
