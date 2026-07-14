import React from 'react'
import { fmt, operatorMeta } from './util.js'
import { ChartFrame } from './accessibility.jsx'

const AX = 'var(--fg-mut)', GRID = 'var(--line)'
const MARK_SHAPES = ['circle', 'square', 'diamond', 'triangle', 'triangle-down', 'pentagon',
  'hexagon', 'star', 'plus', 'cross', 'bar-horizontal', 'bar-vertical']

const polygonPoints = (x, y, radius, sides, rotation = -Math.PI / 2) => Array.from({ length: sides }, (_, index) => {
  const angle = rotation + index * Math.PI * 2 / sides
  return `${x + Math.cos(angle) * radius},${y + Math.sin(angle) * radius}`
}).join(' ')
const starPoints = (x, y, radius) => Array.from({ length: 10 }, (_, index) => {
  const angle = -Math.PI / 2 + index * Math.PI / 5
  const r = index % 2 ? radius * .44 : radius
  return `${x + Math.cos(angle) * r},${y + Math.sin(angle) * r}`
}).join(' ')

function PointMark({ x, y, size = 4, color, shape = 'circle', className = '', opacity = 1,
  variant = 'solid', feasibility = 'feasible', onClick = null, title }) {
  const common = { className: 'chart-point-shape', fill: variant === 'outline' ? 'var(--bg-1)' : color,
    stroke: variant === 'outline' ? color : 'var(--fg)', strokeWidth: variant === 'outline' ? 1.5 : 0.65 }
  const mark = shape === 'square'
    ? <rect {...common} x={x - size} y={y - size} width={size * 2} height={size * 2} rx="1" />
    : shape === 'diamond'
      ? <rect {...common} x={x - size * .78} y={y - size * .78} width={size * 1.56} height={size * 1.56}
          transform={`rotate(45 ${x} ${y})`} />
      : shape === 'triangle'
        ? <path {...common} d={`M ${x} ${y - size - .5} L ${x + size} ${y + size} L ${x - size} ${y + size} Z`} />
        : shape === 'triangle-down'
          ? <path {...common} d={`M ${x - size} ${y - size} L ${x + size} ${y - size} L ${x} ${y + size + .5} Z`} />
          : shape === 'pentagon' ? <polygon {...common} points={polygonPoints(x, y, size + .3, 5)} />
            : shape === 'hexagon' ? <polygon {...common} points={polygonPoints(x, y, size + .3, 6)} />
              : shape === 'star' ? <polygon {...common} points={starPoints(x, y, size + 1)} />
                : shape === 'plus' || shape === 'cross'
                  ? <path {...common} d={`M ${x - size} ${y - size * .3} H ${x - size * .3} V ${y - size} H ${x + size * .3} V ${y - size * .3} H ${x + size} V ${y + size * .3} H ${x + size * .3} V ${y + size} H ${x - size * .3} V ${y + size * .3} H ${x - size} Z`}
                      transform={shape === 'cross' ? `rotate(45 ${x} ${y})` : undefined} />
                  : shape === 'bar-horizontal'
                    ? <rect {...common} x={x - size - 1} y={y - size * .35} width={(size + 1) * 2} height={size * .7} rx="1" />
                    : shape === 'bar-vertical'
                      ? <rect {...common} x={x - size * .35} y={y - size - 1} width={size * .7} height={(size + 1) * 2} rx="1" />
                      : <circle {...common} cx={x} cy={y} r={size} />
  return <g className={className} opacity={opacity} onClick={onClick}>
    {onClick && <circle className="chart-hit-area" cx={x} cy={y} r="15" fill="transparent" />}
    {mark}
    {variant === 'dot' && <circle cx={x} cy={y} r={Math.max(1.1, size * .28)} fill="var(--bg-1)" />}
    {feasibility === 'infeasible' && <circle className="chart-feasibility-ring" cx={x} cy={y}
      r={size + 2.2} fill="none" stroke="var(--fg)" strokeWidth="1.2" strokeDasharray="2 1.7" />}
    {feasibility === 'unknown' && <circle className="chart-feasibility-ring" cx={x} cy={y}
      r={size + 2.2} fill="none" stroke="var(--fg-dim)" strokeWidth="1.2" />}
    <title>{title}</title>
  </g>
}

// Grouping palette: one stable hue per operator so scatter points read as families ("группировка").
const OP_COLORS = {
  draft: '#4aa3ff', improve: '#2ecc71', debug: '#ef4444', merge: '#9a6bff', refine_block: '#f0b429',
  fork: '#22c5c5', random: '#e06fae', tune: '#7f8cff', sweep: '#f59e42',
}
const opColor = (op) => OP_COLORS[op] || '#6b8cc0'
// Stable hue per theme slug (for grouping BY theme) — hashed so a theme always gets the same colour.
function themeColor(t) {
  let h = 0; const s = String(t || 'untagged')
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360
  return `hsl(${h}, 60%, 58%)`
}

// A compact colour legend rendered under a chart (operators present in the data). When `onPick` is
// given the swatches are clickable to FOCUS one operator group (dim the rest) — interactive grouping.
function ChartLegend({ items, active = null, onPick = null }) {
  if (!items || items.length < 2) return null
  return <div className="chart-legend">{items.map((it, i) =>
    onPick ? <button type="button" key={i}
      className={'chart-leg pick' + (active && active !== it.key ? ' dim' : '')}
      aria-pressed={active === it.key}
      onClick={() => onPick(active === it.key ? null : it.key)}
      title={active === it.key ? 'show all' : `show only ${it.label}`}>
      <span className={`chart-leg-dot shape-${it.shape || 'circle'} variant-${it.variant || 'solid'}`}
        style={{ '--marker-color': it.color, background: it.color }} />{it.label}</button>
      : <span key={i} className={'chart-leg' + (active && active !== it.key ? ' dim' : '')}>
        <span className={`chart-leg-dot shape-${it.shape || 'circle'} variant-${it.variant || 'solid'}`}
          style={{ '--marker-color': it.color, background: it.color }} />{it.label}</span>)}</div>
}

// Best-metric-over-time + all-node scatter. Pass `steps` (from report.improvements) to annotate
// the nodes that moved the frontier with a marker + a "what changed" label — so the chart shows
// not just the metric curve but WHICH improvement caused each drop/rise.
// `onPick(id)` (optional) makes every point + frontier marker clickable to drill into that node.
// Points are coloured BY OPERATOR (grouping), with a legend; the frontier keeps its green line + a
// soft area fill under it.
export function Trajectory({ nodes, direction, width = 760, height = 220, steps = null, onPick = null, selected = null }) {
  const evald = nodes.filter(n => (n.metric ?? null) !== null).sort((a, b) => a.id - b.id)
  const [logY, setLogY] = React.useState(false)   // interactivity: log-scale toggle (e.g. loss curves)
  const [hoverId, setHoverId] = React.useState(null)   // interactivity: crosshair + tooltip on hover
  const [focusGrp, setFocusGrp] = React.useState(null)   // interactivity: click the legend to isolate one group
  const [groupBy, setGroupBy] = React.useState('operator')   // grouping dimension: operator | theme
  const grpKey = (n) => groupBy === 'theme' ? (n.idea?.theme || 'untagged') : (n.operator || '—')
  const grpColor = (n) => groupBy === 'theme' ? themeColor(grpKey(n)) : opColor(n.operator)
  const grpLabel = (g) => groupBy === 'theme' ? g : operatorMeta(g).label
  const grpSwatch = (g) => groupBy === 'theme' ? themeColor(g) : opColor(g)
  if (!evald.length) return <Empty>no evaluated nodes yet</Empty>
  const xs = evald.map(n => n.id)
  const ys = evald.map(n => n.confirmed_mean ?? n.metric)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const canLog = minY > 0                           // log scale only when every value is positive
  const useLog = logY && canLog
  const tf = (v) => useLog ? Math.log10(v) : v      // value transform for the axis
  const tMin = tf(minY), tMax = tf(maxY)
  const pad = 34, w = width, h = height
  const x0 = Math.min(...xs), x1 = Math.max(...xs)
  const X = (id) => pad + (id - x0) / Math.max(1, x1 - x0) * (w - pad - 10)
  const Y = (v) => h - pad - (tf(v) - tMin) / Math.max(1e-9, tMax - tMin) * (h - pad - 12)
  const nearest = (px) => {   // map a pixel x to the nearest evaluated node (for the hover crosshair)
    let best = null, bd = 1e9
    for (const n of evald) { const d = Math.abs(X(n.id) - px); if (d < bd) { bd = d; best = n } }
    return best
  }
  const hn = hoverId != null ? evald.find(n => n.id === hoverId) : null
  // running best — exclude infeasible (constraint-violating) nodes, mirroring engine selection
  // (replay.fold ranks only feasible nodes), so the line never claims a best the engine rejected.
  let best = null; const bestPts = []; const tableRows = []
  evald.forEach(n => {
    const v = n.confirmed_mean ?? n.metric
    if (n.feasible !== false && (best === null || (direction === 'min' ? v < best : v > best))) best = v
    if (best !== null) bestPts.push([X(n.id), Y(best)])
    tableRows.push({ node: n.id, operator: n.operator || '—', theme: n.idea?.theme || 'untagged',
      metric: v, best, feasible: n.feasible === false ? 'infeasible' : n.feasible === true ? 'feasible' : 'not reported' })
  })
  const line = bestPts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ')
  const area = bestPts.length > 1
    ? `${line} L ${bestPts[bestPts.length - 1][0]} ${h - pad} L ${bestPts[0][0]} ${h - pad} Z` : ''
  const marks = steps || []
  const pick = onPick || null
  const groupsPresent = [...new Set(evald.map(grpKey).filter(Boolean))]
  const groupMarker = group => {
    const index = Math.max(0, groupsPresent.indexOf(group))
    return { shape: MARK_SHAPES[index % MARK_SHAPES.length],
      variant: ['solid', 'outline', 'dot'][Math.floor(index / MARK_SHAPES.length) % 3] }
  }
  const hasThemes = evald.some(n => n.idea?.theme)
  const columns = [
    { key: 'node', label: 'Node', firstColumnHeader: true,
      render: (value) => pick ? <button type="button" className="btn xs ghost" onClick={() => pick(value)}>#{value}</button> : `#${value}` },
    { key: 'operator', label: 'Operator' }, { key: 'theme', label: 'Theme' },
    { key: 'metric', label: 'Metric', numeric: true },
    { key: 'best', label: 'Best so far', numeric: true }, { key: 'feasible', label: 'Constraint status' },
  ]
  return (
    <ChartFrame className="chart" title="Metric trajectory"
      description={`Evaluated experiments and running ${direction === 'min' ? 'minimum' : 'maximum'}; groups pair colour with marker shape and fill style, while rings and the data table expose constraint status.`}
      columns={columns} rows={tableRows} csvName="metric-trajectory.csv">
    {({ labelledBy }) => <>
    <div className="chart-tools">
      {hasThemes && <span className="chart-grp">group:
        {['operator', 'theme'].map(g => <button type="button" key={g} aria-pressed={groupBy === g}
          className={'btn xs ghost' + (groupBy === g ? ' primary' : '')}
          onClick={() => { setGroupBy(g); setFocusGrp(null) }} title={`colour points by ${g}`}>{g}</button>)}
      </span>}
      {canLog && <button type="button" aria-pressed={logY}
        className={'btn xs ghost' + (logY ? ' primary' : '')} onClick={() => setLogY(v => !v)}
        title="toggle a logarithmic Y axis">log Y</button>}
    </div>
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} className={pick ? 'pickable' : ''}
         role="img" aria-labelledby={labelledBy}
         onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect()
           const px = (e.clientX - r.left) / r.width * w; const n = nearest(px); setHoverId(n ? n.id : null) }}
         onPointerLeave={() => setHoverId(null)}>
      <defs><linearGradient id="ll-traj-fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor="#2ecc71" stopOpacity=".20" /><stop offset="100%" stopColor="#2ecc71" stopOpacity="0" />
      </linearGradient></defs>
      {[0, .25, .5, .75, 1].map((t, i) => {
        const y = pad / 2 + t * (h - pad - 12)
        return <line key={i} x1={pad} x2={w - 10} y1={y} y2={y} stroke={GRID} />
      })}
      {area && <path d={area} fill="url(#ll-traj-fill)" />}
      {selected != null && (() => {   // ring the currently-selected node so the chart tracks the Inspector
        const sn = evald.find(n => n.id === selected); if (!sn) return null
        const v = sn.confirmed_mean ?? sn.metric
        return <circle cx={X(sn.id)} cy={Y(v)} r="7.5" fill="none" stroke="var(--fg)" strokeWidth="2" opacity=".9" pointerEvents="none" />
      })()}
      {evald.map(n => {
        const v = n.confirmed_mean ?? n.metric
        const c = grpColor(n)
        const dim = focusGrp && grpKey(n) !== focusGrp   // legend focus: fade the other groups
        const status = n.feasible === false ? 'infeasible' : n.feasible === true ? 'feasible' : 'unknown'
        const marker = groupMarker(grpKey(n))
        return <PointMark key={n.id} className={'chart-pt' + (pick ? ' pick' : '')}
          x={X(n.id)} y={Y(v)} size={n.id === selected ? 5 : 4} color={c}
          shape={marker.shape} variant={marker.variant}
          feasibility={status} opacity={dim ? .12 : .88} onClick={pick ? () => pick(n.id) : null}
          title={`#${n.id} ${n.operator || ''}${n.idea?.theme ? ` (${n.idea.theme})` : ''} → ${fmt(v)} · ${status === 'unknown' ? 'constraint status not reported' : status}`} />
      })}
      <path d={line} fill="none" stroke="var(--fg)" strokeWidth="4" opacity=".78" />
      <path d={line} fill="none" stroke="var(--ok)" strokeWidth="2.2" />
      {marks.map((s, i) => {
        const v = s.to, x = X(s.id), y = Y(v)
        return <g key={i} className={pick ? 'chart-mark pick' : 'chart-mark'}
          onClick={pick ? () => pick(s.id) : undefined}>
          {pick && <circle className="chart-hit-area" cx={x} cy={y} r="15" fill="transparent" />}
          <circle cx={x} cy={y} r="5" fill="none" stroke="var(--fg)" strokeWidth="3.2" opacity=".78" />
          <circle cx={x} cy={y} r="5" fill="none" stroke="var(--ok)" strokeWidth="1.6" />
          <line x1={x} x2={x} y1={y - 6} y2={Math.max(14, y - 20)} stroke="var(--fg)" strokeWidth="2.4" opacity=".78" />
          <line x1={x} x2={x} y1={y - 6} y2={Math.max(14, y - 20)} stroke="var(--ok)" strokeWidth="1" />
          <text x={x} y={Math.max(11, y - 22)} fill="var(--ok)" fontSize="9.5" textAnchor="middle">#{s.id}</text>
          <title>{`#${s.id} ${s.operator || ''}${s.theme ? ` (${s.theme})` : ''} → ${fmt(v)}${s.delta != null ? ` (Δ ${fmt(s.delta)})` : ' baseline'}`}</title>
        </g>
      })}
      {hn && (() => {   // hover crosshair + tooltip tracking the nearest node
        const v = hn.confirmed_mean ?? hn.metric, hx = X(hn.id), hy = Y(v)
        const label = `#${hn.id} ${hn.operator || ''} → ${fmt(v)}`
        const tw = Math.max(64, label.length * 6.0), tx = Math.min(w - 10 - tw, Math.max(pad, hx - tw / 2))
        return <g pointerEvents="none">
          <line x1={hx} x2={hx} y1={pad / 2} y2={h - pad} stroke={AX} strokeDasharray="3 3" opacity=".6" />
          <circle cx={hx} cy={hy} r="5" fill="none" stroke="var(--fg)" strokeWidth="1.5" />
          <rect x={tx} y={2} width={tw} height={16} rx="3" fill="var(--bg-1)" stroke={GRID} />
          <text x={tx + tw / 2} y={13} fill="var(--fg)" fontSize="10.5" textAnchor="middle">{label}</text>
        </g>
      })()}
      <text x={pad} y={12} fill={AX} fontSize="11">best so far: {fmt(best)}{useLog ? ' · log Y' : ''}</text>
      <text x={pad} y={h - 8} fill={AX} fontSize="11">node id →</text>
    </svg>
    <ChartLegend items={groupsPresent.map(g => ({ key: g, label: grpLabel(g), color: grpSwatch(g), ...groupMarker(g) }))}
                 active={focusGrp} onPick={setFocusGrp} />
    {(evald.some(n => n.feasible === false) || evald.some(n => n.feasible == null)) &&
      <div className="chart-status-legend" aria-label="Constraint marker legend">
        {evald.some(n => n.feasible === false) && <span><span className="chart-status-ring dashed" /> dashed ring · infeasible</span>}
        {evald.some(n => n.feasible == null) && <span><span className="chart-status-ring" /> solid ring · status not reported</span>}
      </div>}
    </>}
    </ChartFrame>
  )
}

// Waterfall of the key improvements: each bar is the metric the frontier reached at that step;
// the baseline is the first best, and each subsequent bar's coloured segment is the gain it added.
export function ImprovementWaterfall({ steps, direction, width = 760 }) {
  if (!steps || !steps.length) return <Empty>no improvement steps yet</Empty>
  const vals = steps.map(s => s.to)
  const lo = Math.min(...vals), hi = Math.max(...vals)
  const pad = 40, bw = Math.max(18, Math.min(64, (width - 2 * pad) / steps.length - 10))
  const gap = ((width - 2 * pad) - steps.length * bw) / Math.max(1, steps.length - 1)
  const h = 200, base = h - 26
  const Y = (v) => 16 + (1 - (v - lo) / Math.max(1e-9, hi - lo)) * (base - 16)
  const rows = steps.map(step => ({ node: step.id, operator: step.operator || '—',
    from: step.from, to: step.to, delta: step.delta }))
  const columns = [
    { key: 'node', label: 'Node', firstColumnHeader: true, render: value => `#${value}` },
    { key: 'operator', label: 'Operator' }, { key: 'from', label: 'Previous', numeric: true },
    { key: 'to', label: 'Metric', numeric: true }, { key: 'delta', label: 'Delta', numeric: true },
  ]
  return (
    <ChartFrame title="Improvement waterfall"
      description={`Frontier changes for a ${direction === 'min' ? 'minimization' : 'maximization'} objective.`}
      columns={columns} rows={rows} csvName="improvement-waterfall.csv">
    {({ labelledBy }) => <svg width="100%" viewBox={`0 0 ${width} ${h}`}
      role="img" aria-labelledby={labelledBy}>
      <line x1={pad} x2={width - pad} y1={base} y2={base} stroke={GRID} />
      {steps.map((s, i) => {
        const x = pad + i * (bw + gap)
        const yTo = Y(s.to), yFrom = s.from == null ? base : Y(s.from)
        const top = Math.min(yTo, yFrom), hgt = Math.max(3, Math.abs(yTo - yFrom))
        const improved = s.delta == null || (direction === 'min' ? s.delta < 0 : s.delta > 0)
        return <g key={i}>
          <rect x={x} y={s.from == null ? yTo : top} width={bw} height={s.from == null ? base - yTo : hgt} rx="3"
                fill={s.from == null ? '#4aa3ff' : (improved ? '#2ecc71' : '#ef4444')}
                stroke="var(--fg)" strokeWidth=".75" opacity={s.from == null ? .72 : .9} />
          <text x={x + bw / 2} y={Math.max(11, top - 4)} fill="var(--fg-dim)" fontSize="9.5" textAnchor="middle">#{s.id}</text>
          <text x={x + bw / 2} y={base + 12} fill={AX} fontSize="9.5" textAnchor="middle">{fmt(s.to)}</text>
          <title>{`#${s.id} ${s.operator || ''} → ${fmt(s.to)}${s.delta != null ? ` (Δ ${fmt(s.delta)})` : ' (baseline)'}`}</title>
        </g>
      })}
    </svg>}
    </ChartFrame>
  )
}

export function Bars({ data, width = 760, height = 220, color = '#4aa3ff', fmtv = fmt }) {
  // data: [{label, value}]
  if (!data || !data.length) return <Empty>no data</Empty>
  const max = Math.max(...data.map(d => Math.abs(d.value)), 1e-9)
  const bh = 22, gap = 8, lab = 150, w = width
  const h = Math.max(height, data.length * (bh + gap) + 10)
  const columns = [
    { key: 'label', label: 'Label', firstColumnHeader: true },
    { key: 'value', label: 'Value', numeric: true },
  ]
  return (
    <ChartFrame title="Value comparison" description="Bar lengths and exact values compare each item."
      columns={columns} rows={data} csvName="bar-values.csv">
    {({ labelledBy }) => <svg width="100%" viewBox={`0 0 ${w} ${h}`}
      role="img" aria-labelledby={labelledBy}>
      {data.map((d, i) => {
        const y = i * (bh + gap) + 4
        const bw = Math.abs(d.value) / max * (w - lab - 60)
        return <g key={i}>
          <text x={lab - 8} y={y + bh / 2 + 4} fill="var(--fg)" fontSize="12" textAnchor="end">{d.label}</text>
          <rect x={lab} y={y} width={bw} height={bh} rx="3" fill={color}
            stroke="var(--fg)" strokeWidth=".75" opacity=".85" />
          <text x={lab + bw + 6} y={y + bh / 2 + 4} fill={AX} fontSize="11">{fmtv(d.value)}</text>
        </g>
      })}
    </svg>}
    </ChartFrame>
  )
}

// Gantt of span timing per node. `onPick(nid)` drills into the clicked span's node.
export function Gantt({ spans, width = 760, onPick }) {
  const flat = []
  const walk = (arr, nid) => arr.forEach(s => {
    flat.push({ nid, name: s.name, start: s.start, dur: s.duration_s || 0, err: s.status === 'ERROR' })
    if (s.children) walk(s.children, nid)
  })
  Object.entries(spans?.nodes || {}).forEach(([nid, arr]) => walk(arr, nid))
  if (!flat.length) return <Empty>no spans recorded</Empty>
  const t0 = Math.min(...flat.map(s => s.start))
  const t1 = Math.max(...flat.map(s => s.start + s.dur))
  const span = Math.max(1e-6, t1 - t0)
  const rowH = 28, lab = 150, w = width, h = flat.length * rowH + 24
  const X = (t) => lab + (t - t0) / span * (w - lab - 20)
  const palette = { evaluate: '#2ecc71', implement: '#4aa3ff', propose: '#9a6bff', repair: '#ef4444', setup: '#f0b429', command: '#4aa3ff' }
  const columns = [
    { key: 'nid', label: 'Node', firstColumnHeader: true,
      render: value => onPick
        ? <button type="button" className="btn xs ghost" onClick={() => onPick(value)}>#{value}</button>
        : `#${value}` },
    { key: 'name', label: 'Span' }, { key: 'start', label: 'Started', numeric: true },
    { key: 'dur', label: 'Duration (s)', numeric: true }, { key: 'err', label: 'Error' },
  ]
  return (
    <ChartFrame title="Execution span timeline" description="Start time and duration for each recorded node span; failed spans also use a dashed outline."
      columns={columns} rows={flat} csvName="execution-spans.csv">
    {({ labelledBy }) => <svg width="100%" viewBox={`0 0 ${w} ${h}`}
      role="img" aria-labelledby={labelledBy}>
      {flat.map((s, i) => {
        const y = i * rowH + 4, x = X(s.start), bw = Math.max(2, (s.dur / span) * (w - lab - 20))
        return <g key={i} onClick={onPick ? () => onPick(s.nid) : undefined}
                  style={onPick ? { cursor: 'pointer' } : undefined}>
          <title>{`${s.nid}:${s.name} — ${fmt(s.dur, 3)}s${s.err ? ' (ERROR)' : ''}`}</title>
          {onPick && <rect className="chart-hit-area" x="0" y={y - 2} width={w} height={rowH} fill="transparent" />}
          <text x={lab - 6} y={y + 15} fill="var(--fg-dim)" fontSize="10" textAnchor="end">{s.nid}:{s.name}</text>
          <rect x={x} y={y} width={bw} height={rowH - 5} rx="2" fill={s.err ? '#ef4444' : (palette[s.name] || '#4aa3ff')}
            stroke="var(--fg)" strokeWidth={s.err ? 1.2 : .65}
            strokeDasharray={s.err ? '3 2' : undefined} opacity=".85" />
        </g>
      })}
      <text x={lab} y={h - 4} fill={AX} fontSize="11">{fmt(span)}s total span</text>
    </svg>}
    </ChartFrame>
  )
}

// Parallel coordinates of params -> metric.
export function ParallelCoords({ nodes, direction, width = 760, height = 260, onPick = null }) {
  const ev = nodes.filter(n => (n.metric ?? null) !== null)
  if (!ev.length) return <Empty>no evaluated nodes</Empty>
  const pick = onPick || null
  const isNum = v => v != null && Number.isFinite(Number(v))
  // Only numeric params can be plotted on a value axis; a string param (optimizer=adam) would give
  // NaN coordinates and blank the whole chart, so drop non-numeric axes here.
  const params = Array.from(new Set(ev.flatMap(n => Object.keys(n.idea?.params || {}))))
    .filter(a => ev.some(n => isNum(n.idea?.params?.[a])))
  const axes = [...params, 'metric']
  if (axes.length < 2) return <Empty>not enough dimensions</Empty>
  const vals = (n, a) => { const v = a === 'metric' ? (n.confirmed_mean ?? n.metric) : n.idea?.params?.[a]; return isNum(v) ? Number(v) : null }
  const ranges = {}
  axes.forEach(a => { const xs = ev.map(n => vals(n, a)).filter(v => v != null); ranges[a] = [Math.min(...xs), Math.max(...xs)] })
  const pad = 40, w = width, h = height
  const AXX = (i) => pad + i / (axes.length - 1) * (w - 2 * pad)
  const AXY = (a, v) => { const [lo, hi] = ranges[a]; return h - pad - (v - lo) / Math.max(1e-9, hi - lo) * (h - 2 * pad) }
  const ms = ev.map(n => n.confirmed_mean ?? n.metric)
  const mlo = Math.min(...ms), mhi = Math.max(...ms)
  const colorOf = (m) => {
    let t = (m - mlo) / Math.max(1e-9, mhi - mlo); if (direction === 'min') t = 1 - t
    return `hsl(${120 * t}, 65%, 55%)`
  }
  const rows = ev.map(node => ({ source: node }))
  const columns = [
    { key: 'node', label: 'Node', firstColumnHeader: true, value: row => row.source.id,
      render: (value) => pick ? <button type="button" className="btn xs ghost" onClick={() => pick(value)}>#{value}</button> : `#${value}` },
    { key: 'operator', label: 'Operator', value: row => row.source.operator || '—' },
    ...axes.map((axis, index) => ({ key: `axis-${index}`, label: axis, numeric: true,
      value: row => vals(row.source, axis) })),
  ]
  return (
    <ChartFrame title="Parameter relationships"
      description="Parallel coordinates connect each experiment's numeric parameters to its metric; exact values are available below."
      columns={columns} rows={rows} csvName="parallel-coordinates.csv">
    {({ labelledBy }) => <svg width="100%" viewBox={`0 0 ${w} ${h}`}
      role="img" aria-labelledby={labelledBy}>
      {axes.map((a, i) => <g key={a}>
        <line x1={AXX(i)} x2={AXX(i)} y1={pad} y2={h - pad} stroke={GRID} />
        <text x={AXX(i)} y={h - pad + 14} fill={AX} fontSize="11" textAnchor="middle">{a}</text>
      </g>)}
      {ev.map(n => {
        const pts = axes.map((a, i) => { const v = vals(n, a); return v == null ? null : [AXX(i), AXY(a, v)] }).filter(Boolean)
        const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ')
        return <g key={n.id} className={pick ? 'pick' : undefined}
          onClick={pick ? () => pick(n.id) : undefined}>
          {pick && <path className="chart-hit-area" d={d} fill="none" stroke="transparent" strokeWidth="28" />}
          <path d={d} fill="none" stroke="var(--fg)" strokeWidth="3.5" opacity=".78" pointerEvents="none" />
          <path className={'pc-line' + (pick ? ' pick' : '')} d={d} fill="none"
            stroke={colorOf(n.confirmed_mean ?? n.metric)} strokeWidth="1.8" opacity=".86" pointerEvents="none" />
          <title>{`#${n.id} ${n.operator || ''} → ${fmt(n.confirmed_mean ?? n.metric)}`}</title>
        </g>
      })}
    </svg>}
    </ChartFrame>
  )
}

// metric vs a constraint value (Pareto-ish). data: [{x,y,feasible,id}]
// `onPick(id)` (optional) drills into a point's node (points carrying an `id`).
export function Scatter({ data, xlab, ylab, width = 720, height = 260, onPick = null }) {
  if (!data || !data.length) return <Empty>no constraint data</Empty>
  const xs = data.map(d => d.x), ys = data.map(d => d.y)
  const pad = 40, w = width, h = height
  const X = v => pad + (v - Math.min(...xs)) / Math.max(1e-9, Math.max(...xs) - Math.min(...xs)) * (w - 2 * pad)
  const Y = v => h - pad - (v - Math.min(...ys)) / Math.max(1e-9, Math.max(...ys) - Math.min(...ys)) * (h - 2 * pad)
  const pick = onPick || null
  const statusOf = value => value === true ? 'feasible' : value === false ? 'infeasible' : 'unknown'
  const tableRows = data.map(point => ({ ...point, feasible: statusOf(point.feasible) }))
  const columns = [
    { key: 'id', label: 'Node', firstColumnHeader: true,
      render: value => value == null ? '—' : pick
        ? <button type="button" className="btn xs ghost" onClick={() => pick(value)}>#{value}</button>
        : `#${value}` },
    { key: 'x', label: xlab, numeric: true }, { key: 'y', label: ylab, numeric: true },
    { key: 'feasible', label: 'Feasible' },
  ]
  return (
    <ChartFrame title={`${ylab} by ${xlab}`}
      description="Feasible, infeasible, and unknown points use different shapes as well as colours; every point also has an exact text row."
      columns={columns} rows={tableRows} csvName="scatter-data.csv">
    {({ labelledBy }) => <>
    <svg width="100%" viewBox={`0 0 ${w} ${h}`}
      className={pick ? 'pickable' : ''} role="img" aria-labelledby={labelledBy}>
      {[0, .5, 1].map((t, i) => { const y = pad + t * (h - 2 * pad); return <line key={i} x1={pad} x2={w - pad} y1={y} y2={y} stroke={GRID} /> })}
      {data.map((d, i) => {
        const status = statusOf(d.feasible)
        return <PointMark key={i} className={'chart-pt' + (pick && d.id != null ? ' pick' : '')}
          x={X(d.x)} y={Y(d.y)} size={4.5}
          shape={status === 'feasible' ? 'circle' : status === 'infeasible' ? 'diamond' : 'square'}
          color={status === 'feasible' ? '#2ecc71' : status === 'infeasible' ? '#9a6bff' : '#7f8998'}
          feasibility={status} opacity=".88" onClick={pick && d.id != null ? () => pick(d.id) : null}
          title={`${d.id != null ? `#${d.id} · ` : ''}${xlab} ${fmt(d.x)} · ${ylab} ${fmt(d.y)} · ${status === 'unknown' ? 'constraint status not reported' : status}`} />
      })}
      <text x={w / 2} y={h - 6} fill={AX} fontSize="11" textAnchor="middle">{xlab}</text>
      <text x={12} y={14} fill={AX} fontSize="11">{ylab}</text>
    </svg>
    <ChartLegend items={[
      { key: 'feasible', label: 'feasible', color: '#2ecc71', shape: 'circle' },
      { key: 'infeasible', label: 'infeasible', color: '#9a6bff', shape: 'diamond' },
      { key: 'unknown', label: 'status not reported', color: '#7f8998', shape: 'square' },
    ].filter(item => data.some(point => statusOf(point.feasible) === item.key))} />
    </>}
    </ChartFrame>
  )
}

// Tiny sparkline of a numeric series — used by collapsed-group super-cards, sweep node cards, and
// the inspector. Returns null for <2 points (nothing meaningful to draw).
export function Spark({ series, width = 120, height = 22, label = null }) {
  if (!series || series.length < 2) return null
  const lo = Math.min(...series), hi = Math.max(...series), span = hi - lo || 1
  const W = width, H = height
  const pts = series.map((v, i) => `${(i / (series.length - 1) * W).toFixed(1)},${(H - (v - lo) / span * H).toFixed(1)}`).join(' ')
  return <svg className="grp-spark" width={W} height={H} role="img"
    aria-label={label || `Trend across ${series.length} values, from ${fmt(series[0])} to ${fmt(series[series.length - 1])}`}>
    <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
  </svg>
}

function Empty({ children }) { return <div className="muted" style={{ padding: 20 }}>{children}</div> }

// U4 · overlay several runs' running-best trajectories on ONE axis, to compare convergence at a
// glance. `runs` = [{label, run_id, series:[running-best value per evaluated node]}]. x = experiment
// index (runs have different lengths — each line just stops at its own end); y = shared metric range.
const _RUN_COLORS = ['#4aa3ff', '#2ecc71', '#f0b429', '#e0559a', '#8b5cf6', '#22d3d3', '#ff7a45', '#9aa7b5']
const _RUN_DASHES = ['', '7 3', '2 3', '9 3 2 3', '5 2', '1 3', '10 3', '4 3 1 3']
export function MultiTrajectory({ runs, width = 760, height = 240 }) {
  const withData = (runs || []).filter(r => (r.series || []).length > 0)
  if (!withData.length) return <Empty>no comparable run trajectories yet</Empty>
  const allV = withData.flatMap(r => r.series)
  const lo = Math.min(...allV), hi = Math.max(...allV), span = (hi - lo) || 1
  const maxLen = Math.max(...withData.map(r => r.series.length))
  const pad = 34, w = width, h = height
  const X = i => pad + (maxLen <= 1 ? 0 : i / (maxLen - 1) * (w - pad - 10))
  const Y = v => h - pad - (v - lo) / span * (h - pad - 12)
  const rows = withData.flatMap(run => run.series.map((metric, experiment) => ({
    run: run.label || run.run_id, run_id: run.run_id, experiment, metric,
  })))
  const columns = [
    { key: 'run', label: 'Run', firstColumnHeader: true },
    { key: 'run_id', label: 'Run id' }, { key: 'experiment', label: 'Experiment', numeric: true },
    { key: 'metric', label: 'Running best', numeric: true },
  ]
  return (
    <ChartFrame title="Cross-run trajectories"
      description="Each run uses both a hue and a dash pattern; the table contains every exact point."
      columns={columns} rows={rows} csvName="run-trajectories.csv">
    {({ labelledBy }) => <>
      <svg width={w} height={h} role="img" aria-labelledby={labelledBy}>
        <line x1={pad} y1={h - pad} x2={w - 8} y2={h - pad} stroke="var(--border)" />
        <line x1={pad} y1={12} x2={pad} y2={h - pad} stroke="var(--border)" />
        <text x={pad - 6} y={16} textAnchor="end" fontSize="10" fill="var(--fg-mut)">{fmt(hi)}</text>
        <text x={pad - 6} y={h - pad} textAnchor="end" fontSize="10" fill="var(--fg-mut)">{fmt(lo)}</text>
        <text x={(w + pad) / 2} y={h - 6} textAnchor="middle" fontSize="10" fill="var(--fg-mut)">experiment #</text>
        {withData.map((r, k) => {
          const c = _RUN_COLORS[k % _RUN_COLORS.length]
          const dash = _RUN_DASHES[k % _RUN_DASHES.length]
          const pts = r.series.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')
          return <g key={r.run_id || k}>
            <polyline points={pts} fill="none" stroke="var(--fg)" strokeDasharray={dash || undefined}
              strokeWidth="4" opacity=".78" />
            <polyline points={pts} fill="none" stroke={c}
              strokeDasharray={dash || undefined} strokeWidth="2.1" opacity="0.95" />
          </g>
        })}
      </svg>
      <div className="row" style={{ flexWrap: 'wrap', gap: 10, marginTop: 4 }}>
        {withData.map((r, k) => <span key={r.run_id || k} className="muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <svg width="18" height="8" aria-hidden="true"><line x1="0" x2="18" y1="4" y2="4" stroke="var(--fg)"
            strokeDasharray={_RUN_DASHES[k % _RUN_DASHES.length] || undefined} strokeWidth="4" opacity=".78" />
            <line x1="0" x2="18" y1="4" y2="4" stroke={_RUN_COLORS[k % _RUN_COLORS.length]}
              strokeDasharray={_RUN_DASHES[k % _RUN_DASHES.length] || undefined} strokeWidth="2" /></svg>
          {r.label || r.run_id}</span>)}
      </div>
    </>}
    </ChartFrame>
  )
}

// Online training/eval curves — a small line chart per logged metric tag (loss, every recall@k, lr,
// grad norms, …) from a node's TensorBoard series {tag: [{step, value}]}. ALL metrics, not just the
// objective — the "a la TensorBoard" per-node view.
export function MetricLines({ series, cols = 2 }) {
  const tags = Object.keys(series || {}).filter(t => (series[t] || []).length > 0).sort()
  if (!tags.length) return <Empty>no metric curves logged yet — they appear once training starts writing TensorBoard events</Empty>
  // Group by the tag prefix before the first '/' (TensorBoard convention: train/loss, val/recall@100,
  // …); a tag with no slash falls into "other". Each group is an independent COLLAPSIBLE section so a
  // run that logs dozens of scalars isn't one endless wall of charts.
  const groups = {}
  for (const t of tags) {
    const i = t.indexOf('/')
    const g = i > 0 ? t.slice(0, i) : 'other'
    ;(groups[g] || (groups[g] = [])).push(t)
  }
  const names = Object.keys(groups).sort()
  return <div>{names.map(g => <MetricGroup key={g} name={g} tags={groups[g]} series={series} cols={cols} />)}</div>
}

function MetricGroup({ name, tags, series, cols }) {
  const [open, setOpen] = React.useState(false)   // groups COLLAPSED by default (expand one to see its curves)
  const groupId = `metric-group-${React.useId().replaceAll(':', '')}`
  return (
    <div style={{ marginBottom: 8 }}>
      <button type="button" className="metric-group-toggle" aria-expanded={open}
        aria-controls={groupId} onClick={() => setOpen(o => !o)}>
        <span style={{ opacity: 0.6, fontSize: 10, width: 10, display: 'inline-block' }}>{open ? '▾' : '▸'}</span>
        {name} <span className="muted" style={{ fontWeight: 400 }}>· {tags.length} metric{tags.length === 1 ? '' : 's'}</span>
      </button>
      {open && <div id={groupId} className="metric-group-grid"
        style={{ display: 'grid', gridTemplateColumns: `repeat(${cols}, minmax(0,1fr))`, gap: 10 }}>
        {tags.map(t => <MiniLine key={t} label={t} pts={series[t]} />)}
      </div>}
    </div>
  )
}

export function MiniLine({ label, pts, width = 340, height = 130 }) {
  const [hi, setHi] = React.useState(null)   // hovered point index (tooltip + dot)
  const xs = pts.map(p => p.step), ys = pts.map(p => p.value)
  const minX = Math.min(...xs), maxX = Math.max(...xs)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const pad = 30, w = width, h = height
  const X = v => pad + (v - minX) / Math.max(1e-9, maxX - minX) * (w - pad - 8)
  const Y = v => h - pad - (v - minY) / Math.max(1e-9, maxY - minY) * (h - pad - 16)
  const d = pts.map((p, i) => (i ? 'L' : 'M') + X(p.step).toFixed(1) + ' ' + Y(p.value).toFixed(1)).join(' ')
  const last = ys[ys.length - 1]
  const nearestIdx = (px) => {   // pixel x -> nearest point index (hover)
    let bi = 0, bd = 1e9
    pts.forEach((p, i) => { const dd = Math.abs(X(p.step) - px); if (dd < bd) { bd = dd; bi = i } })
    return bi
  }
  const hp = hi != null ? pts[hi] : null
  const columns = [
    { key: 'step', label: 'Step', firstColumnHeader: true, numeric: true },
    { key: 'value', label: 'Value', numeric: true },
    { key: 'wall_time', label: 'Wall time', numeric: true },
  ]
  const csvName = `${String(label).replace(/[^a-z0-9._-]+/gi, '_').slice(0, 80) || 'metric'}.csv`
  return (
    <div style={{ border: `1px solid ${GRID}`, borderRadius: 6, padding: 6, background: 'var(--bg-1)' }}>
      <ChartFrame title={label}
        description={`${hp ? `Step ${hp.step}: ${fmt(hp.value)}` : `Latest ${fmt(last)}`} · ${pts.length} points`}
        columns={columns} rows={pts} csvName={csvName} className="metric-mini-chart">
      {({ labelledBy }) => <svg width="100%" viewBox={`0 0 ${w} ${h}`}
           role="img" aria-labelledby={labelledBy}
           onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); setHi(nearestIdx((e.clientX - r.left) / r.width * w)) }}
           onPointerLeave={() => setHi(null)}>
        {[0, .5, 1].map((t, i) => { const y = pad / 2 + t * (h - pad - 16); return <line key={i} x1={pad} x2={w - 8} y1={y} y2={y} stroke={GRID} /> })}
        <path d={d} fill="none" stroke="var(--fg)" strokeWidth="3.8" opacity=".78" />
        <path d={d} fill="none" stroke="var(--ok)" strokeWidth="1.8" />
        {hp && <><line x1={X(hp.step)} x2={X(hp.step)} y1={pad / 2} y2={h - pad} stroke={AX} strokeDasharray="3 3" opacity=".6" />
          <circle cx={X(hp.step)} cy={Y(hp.value)} r="3.5" fill="none" stroke="var(--fg)" strokeWidth="1.4" /></>}
        <text x={2} y={pad / 2 + 4} fill={AX} fontSize="9">{fmt(maxY)}</text>
        <text x={2} y={h - pad + 4} fill={AX} fontSize="9">{fmt(minY)}</text>
        <text x={pad} y={h - 6} fill={AX} fontSize="9">step {minX}–{maxX}</text>
      </svg>}
      </ChartFrame>
    </div>
  )
}
