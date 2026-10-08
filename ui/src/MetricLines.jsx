import { uiMessage, uiPlural } from './uiLanguage.js'
import { uiText, useUILanguage } from './uiLanguage.js'
import React from 'react'
import { fmt } from './util.js'
import { ChartFrame } from './accessibility.jsx'
import { metricCurveProjection, metricCurvePosition } from './metricCurveProjection.js'

const AX = 'var(--fg-mut)', GRID = 'var(--line)'
function Empty({ children }) {
  useUILanguage()
 return <div className="muted" style={{ padding: 20 }}>{children}</div> }

// Online training/eval curves — a small line chart per logged metric tag (loss, every recall@k, lr,
// grad norms, …) from a node's TensorBoard series {tag: [{step, value}]}. ALL metrics, not just the
// objective — the "a la TensorBoard" per-node view.
export function MetricLines({ series, cols = 2 }) {
  useUILanguage()

  const tags = Object.keys(series || {}).filter(t => (series[t] || []).length > 0).sort()
  if (!tags.length) return <Empty>{uiText("no metric curves logged yet — they appear once training starts writing TensorBoard events")}</Empty>
  // Group by the tag prefix before the first '/' (TensorBoard convention: train/loss, val/recall@100,
  // …); a tag with no slash falls into "other". Each group is an independent COLLAPSIBLE section so a
  // run that logs dozens of scalars isn't one endless wall of charts.
  // Prefixes come from logged tags; even constructor or __proto__ is a real group name.
  const groups = Object.create(null)
  for (const t of tags) {
    const i = t.indexOf('/')
    const g = i > 0 ? t.slice(0, i) : 'other'
    ;(groups[g] || (groups[g] = [])).push(t)
  }
  const names = Object.keys(groups).sort()
  return <div>{names.map(g => <MetricGroup key={g} name={g} tags={groups[g]} series={series} cols={cols} />)}</div>
}

function MetricGroup({ name, tags, series, cols }) {
  useUILanguage()

  const [open, setOpen] = React.useState(false)   // groups COLLAPSED by default (expand one to see its curves)
  const groupId = `metric-group-${React.useId().replaceAll(':', '')}`
  return (
    <div style={{ marginBottom: 8 }}>
      <button type="button" className="metric-group-toggle" aria-expanded={open}
        aria-controls={groupId} onClick={() => setOpen(o => !o)}>
        <span style={{ opacity: 0.6, fontSize: 10, width: 10, display: 'inline-block' }}>{open ? '▾' : '▸'}</span>
        {name} <span className="muted" style={{ fontWeight: 400 }}>· {uiPlural(tags.length, '{0} metric', '{0} metrics')}</span>
      </button>
      {open && <div id={groupId} className="metric-group-grid"
        style={{ display: 'grid', gridTemplateColumns: `repeat(${cols}, minmax(0,1fr))`, gap: 10 }}>
        {tags.map(t => <MiniLine key={t} label={t} pts={series[t]} />)}
      </div>}
    </div>
  )
}

export function MiniLine({ label, pts, width = 340, height = 130 }) {
  useUILanguage()

  const [hi, setHi] = React.useState(null)   // hover belongs to this projection (tooltip + dot)
  const projection = React.useMemo(() => metricCurveProjection(pts), [pts])
  const plotted = projection.points
  const [minX, maxX, minY, maxY] = projection.bounds
  if (!pts.length) return <Empty>{uiText("no metric points recorded")}</Empty>
  const pad = 30, w = width, h = height
  const X = v => pad + metricCurvePosition(v, minX, maxX) * (w - pad - 8)
  const Y = v => h - pad - metricCurvePosition(v, minY, maxY) * (h - pad - 16)
  const d = plotted.map((p, i) => (i ? 'L' : 'M') + X(p.step).toFixed(1) + ' ' + Y(p.value).toFixed(1)).join(' ')
  const last = pts.at(-1).value
  const nearestIdx = (px) => {   // pixel x -> nearest point index (hover)
    let bi = 0, bd = 1e9
    plotted.forEach((p, i) => { const dd = Math.abs(X(p.step) - px); if (dd < bd) { bd = dd; bi = i } })
    return bi
  }
  const hp = hi?.projection === projection ? plotted[hi.index] : null
  const columns = [
    { key: 'step', label: 'Step', firstColumnHeader: true, numeric: true },
    { key: 'value', label: 'Value', numeric: true },
    { key: 'wall_time', label: 'Wall time', numeric: true },
  ]
  const csvName = `${String(label).replace(/[^a-z0-9._-]+/gi, '_').slice(0, 80) || 'metric'}.csv`
  return (
    <div style={{ border: `1px solid ${GRID}`, borderRadius: 6, padding: 6, background: 'var(--bg-1)' }}>
      <ChartFrame title={uiText(label)}
        description={uiPlural(pts.length, '{0} · {1} points{2}', '{0} · {1} points{2}', [hp ? uiMessage('Step {0}: {1}', [hp.step, fmt(hp.value)]) : uiMessage('Latest {0}', [fmt(last)]), pts.length, plotted.length < pts.length
          ? uiPlural(pts.length, ' · plot: {0} of {1} points, bucket extrema; exact data in table/CSV', ' · plot: {0} of {1} points, bucket extrema; exact data in table/CSV', [plotted.length, pts.length]) : ''])}
        columns={columns} rows={pts} pageSize={100} csvName={csvName} className="metric-mini-chart">
      {({ labelledBy }) => <svg width="100%" viewBox={`0 0 ${w} ${h}`}
           role="img" aria-labelledby={labelledBy}
           onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); setHi({ projection, index: nearestIdx((e.clientX - r.left) / r.width * w) }) }}
           onPointerLeave={() => setHi(null)}>
        {[0, .5, 1].map((t, i) => { const y = pad / 2 + t * (h - pad - 16); return <line key={i} x1={pad} x2={w - 8} y1={y} y2={y} stroke={GRID} /> })}
        <path d={d} fill="none" stroke="var(--fg)" strokeWidth="3.8" opacity=".78" />
        <path d={d} fill="none" stroke="var(--ok)" strokeWidth="1.8" />
        {pts.length === 1 && <circle cx={X(pts[0].step)} cy={Y(last)} r="3" fill="var(--ok)" />}
        {hp && <><line x1={X(hp.step)} x2={X(hp.step)} y1={pad / 2} y2={h - pad} stroke={AX} strokeDasharray="3 3" opacity=".6" />
          <circle cx={X(hp.step)} cy={Y(hp.value)} r="3.5" fill="none" stroke="var(--fg)" strokeWidth="1.4" /></>}
        <text x={2} y={pad / 2 + 4} fill={AX} fontSize="9">{fmt(maxY)}</text>
        <text x={2} y={h - pad + 4} fill={AX} fontSize="9">{fmt(minY)}</text>
        <text x={pad} y={h - 6} fill={AX} fontSize="9">{uiText("step ")}{minX}–{maxX}</text>
      </svg>}
      </ChartFrame>
    </div>
  )
}
