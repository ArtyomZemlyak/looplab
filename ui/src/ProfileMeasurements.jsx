import React, { useState } from 'react'
import { get, runApiPath } from './api.js'
import { uiText } from './uiLanguage.js'

function MeasurementPlot({ metric, nodes, index }) {
  const measured = nodes.filter(node => typeof node.values[index]?.value === 'number' && Number.isFinite(node.values[index].value))
  if (!measured.length) return null
  const values = measured.map(node => node.values[index].value)
  const lo = Math.min(...values), hi = Math.max(...values), span = hi - lo || 1
  return <figure>
    <figcaption>{metric.label || metric.key} · {uiText('Declared denominator')}: {metric.denominator || uiText('not recorded')}</figcaption>
    <svg width="360" height={30 + measured.length * 26} role="img" aria-label={`${metric.label || metric.key}: ${measured.map(n => `#${n.node_id} ${n.values[index].value}`).join(', ')}`}>
      {measured.map((node, i) => <g key={node.node_id}>
        <text x="0" y={22 + i * 26}>#{node.node_id}</text>
        <circle cx={75 + 250 * (node.values[index].value - lo) / span} cy={18 + i * 26} r="4" fill="currentColor" />
      </g>)}
    </svg>
    <p>{lo} – {hi} · {metric.unit || uiText('unit not recorded')}</p>
  </figure>
}

// Stack only author-declared additive components with identical, nonempty denominator and unit.
function StackedMeasurements({ metrics, nodes }) {
  const groups = new Map()
  metrics.forEach((metric, i) => {
    if (!metric.stack_group) return
    const rows = groups.get(metric.stack_group) || []
    rows.push({ metric, i }); groups.set(metric.stack_group, rows)
  })
  return [...groups.entries()].map(([group, rows]) => {
    const first = rows[0].metric
    if (rows.length < 2 || !first.denominator || rows.some(x => x.metric.denominator !== first.denominator || x.metric.unit !== first.unit)) return null
    const valid = nodes.filter(node => rows.every(({ i }) => typeof node.values[i]?.value === 'number' && Number.isFinite(node.values[i].value) && node.values[i].value >= 0))
    if (!valid.length) return null
    const total = node => rows.reduce((sum, { i }) => sum + node.values[i].value, 0)
    const max = Math.max(...valid.map(total)) || 1
    const colors = ['#286db0', '#e67e22', '#4a8d5b', '#9354ad', '#a34a50', '#737373']
    return <figure key={group}>
      <figcaption>{group} · {uiText('Declared denominator')}: {first.denominator} · {first.unit || uiText('unit not recorded')}</figcaption>
      <p>{uiText('Components are declared additive by the project; missing or negative values are excluded. Cross-node protocols are not verified.')}</p>
      <svg width="360" height={30 + valid.length * 26} role="img" aria-label={`${group}: ${valid.map(n => `#${n.node_id} ${total(n)}`).join(', ')}`}>
        {valid.map((node, k) => { let x = 70; return <g key={node.node_id}><text x="0" y={22 + k * 26}>#{node.node_id}</text>
          {rows.map(({ i, metric }, j) => { const width = 260 * node.values[i].value / max; const start = x; x += width
            return <rect key={metric.key} x={start} y={7 + k * 26} width={width} height="17" fill={colors[j % colors.length]}><title>{metric.label || metric.key}: {node.values[i].value}</title></rect> })}
        </g> })}
      </svg>
      <p>{rows.map(({ metric }) => metric.label || metric.key).join(' + ')}</p>
    </figure>
  })
}

// Values are native node observations, never inferred comparisons or generated metrics.
export default function ProfileMeasurements({ runId, generation }) {
  const [data, setData] = useState(null), [error, setError] = useState(false)
  const load = async offset => {
    setError(false)
    try {
      const p = new URLSearchParams({ expected_generation: generation, offset: String(offset), limit: '10' })
      setData(await get(`${runApiPath(runId, '/profile-measurements')}?${p}`, { cache: 'no-store' }))
    } catch { setError(true) }
  }
  const metrics = data?.profile?.profile?.metrics || []
  return <section>
    <button type="button" onClick={() => load(0)}>{uiText('Show recorded profile measurements')}</button>
    {error && <p role="status">{uiText('Measurements are unavailable. Refresh before acting.')}</p>}
    {data && <>
      <p>{uiText('Values are recorded per attempt. The declared profile does not verify cohort, policy or comparability.')}</p>
      {data.profile?.state !== 'configured' && <p>{uiText('No project metric profile is configured.')}</p>}
      <StackedMeasurements metrics={metrics} nodes={data.nodes} />
      {metrics.map((metric, i) => <section key={metric.key}>
        <h4>{metric.label || metric.key} · {metric.unit || uiText('unit not recorded')}</h4>
        <p>{uiText('Declared denominator')}: {metric.denominator || uiText('not recorded')}</p>
        <MeasurementPlot metric={metric} nodes={data.nodes} index={i} />
        <table><thead><tr><th>{uiText('Experiment')}</th><th>{uiText('Attempt')}</th><th>{uiText('Recorded value')}</th><th>{uiText('Provenance')}</th></tr></thead>
          <tbody>{data.nodes.map(node => { const v = node.values[i]; return <tr key={node.node_id}>
            <td>{node.name || `#${node.node_id}`} (#{node.node_id})</td><td>{node.attempt}</td>
            <td>{v.value == null ? 'N/A' : v.value}</td><td>{v.value == null ? 'N/A' : `${v.channel || uiText('unknown')}${v.reconstructed ? ` · ${uiText('reconstructed')}` : ''}`}</td>
          </tr> })}</tbody></table>
      </section>)}
      {data.offset > 0 && <button type="button" onClick={() => load(Math.max(0, data.offset - data.limit))}>{uiText('Previous')}</button>}
      {data.has_more && <button type="button" onClick={() => load(data.offset + data.limit)}>{uiText('Next')}</button>}
    </>}
  </section>
}
