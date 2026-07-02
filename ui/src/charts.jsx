import React from 'react'
import { fmt, operatorMeta } from './util.js'

const AX = '#6b7480', GRID = '#20252f'

// Grouping palette: one stable hue per operator so scatter points read as families ("группировка").
const OP_COLORS = {
  draft: '#4aa3ff', improve: '#2ecc71', debug: '#ef4444', merge: '#9a6bff', refine_block: '#f0b429',
  fork: '#22c5c5', random: '#e06fae', tune: '#7f8cff', sweep: '#f59e42',
}
const opColor = (op) => OP_COLORS[op] || '#6b8cc0'

// A compact colour legend rendered under a chart (operators present in the data).
function ChartLegend({ items }) {
  if (!items || items.length < 2) return null
  return <div className="chart-legend">{items.map((it, i) =>
    <span key={i} className="chart-leg"><span className="chart-leg-dot" style={{ background: it.color }} />{it.label}</span>)}</div>
}

// Best-metric-over-time + all-node scatter. Pass `steps` (from report.improvements) to annotate
// the nodes that moved the frontier with a marker + a "what changed" label — so the chart shows
// not just the metric curve but WHICH improvement caused each drop/rise.
// `onPick(id)` (optional) makes every point + frontier marker clickable to drill into that node.
// Points are coloured BY OPERATOR (grouping), with a legend; the frontier keeps its green line + a
// soft area fill under it.
export function Trajectory({ nodes, direction, width = 760, height = 220, steps = null, onPick = null }) {
  const evald = nodes.filter(n => (n.metric ?? null) !== null).sort((a, b) => a.id - b.id)
  if (!evald.length) return <Empty>no evaluated nodes yet</Empty>
  const xs = evald.map(n => n.id)
  const ys = evald.map(n => n.confirmed_mean ?? n.metric)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const pad = 34, w = width, h = height
  const x0 = Math.min(...xs), x1 = Math.max(...xs)
  const X = (id) => pad + (id - x0) / Math.max(1, x1 - x0) * (w - pad - 10)
  const Y = (v) => h - pad - (v - minY) / Math.max(1e-9, maxY - minY) * (h - pad - 12)
  // running best — exclude infeasible (constraint-violating) nodes, mirroring engine selection
  // (replay.fold ranks only feasible nodes), so the line never claims a best the engine rejected.
  let best = null; const bestPts = []
  evald.forEach(n => {
    const v = n.confirmed_mean ?? n.metric
    if (n.feasible !== false && (best === null || (direction === 'min' ? v < best : v > best))) best = v
    if (best !== null) bestPts.push([X(n.id), Y(best)])
  })
  const line = bestPts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ')
  const area = bestPts.length > 1
    ? `${line} L ${bestPts[bestPts.length - 1][0]} ${h - pad} L ${bestPts[0][0]} ${h - pad} Z` : ''
  const marks = steps || []
  const pick = onPick || null
  const opsPresent = [...new Set(evald.map(n => n.operator).filter(Boolean))]
  return (
    <div className="chart">
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} className={pick ? 'pickable' : ''}>
      <defs><linearGradient id="ll-traj-fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stopColor="#2ecc71" stopOpacity=".20" /><stop offset="100%" stopColor="#2ecc71" stopOpacity="0" />
      </linearGradient></defs>
      {[0, .25, .5, .75, 1].map((t, i) => {
        const y = pad / 2 + t * (h - pad - 12)
        return <line key={i} x1={pad} x2={w - 10} y1={y} y2={y} stroke={GRID} />
      })}
      {area && <path d={area} fill="url(#ll-traj-fill)" />}
      {evald.map(n => {
        const v = n.confirmed_mean ?? n.metric
        const c = n.feasible === false ? '#7a6b9a' : opColor(n.operator)
        return <circle key={n.id} className={'chart-pt' + (pick ? ' pick' : '')} cx={X(n.id)} cy={Y(v)}
          r={n.feasible === false ? 2.8 : 4} fill={c} opacity={.85}
          onClick={pick ? () => pick(n.id) : undefined}>
          <title>#{n.id} {n.operator}{n.idea?.theme ? ` (${n.idea.theme})` : ''} → {fmt(v)}{n.feasible === false ? ' · infeasible' : ''}</title>
        </circle>
      })}
      <path d={line} fill="none" stroke="#2ecc71" strokeWidth="2" />
      {marks.map((s, i) => {
        const v = s.to, x = X(s.id), y = Y(v)
        return <g key={i} className={pick ? 'chart-mark pick' : 'chart-mark'} onClick={pick ? () => pick(s.id) : undefined}>
          <circle cx={x} cy={y} r="5" fill="none" stroke="#2ecc71" strokeWidth="1.6" />
          <line x1={x} x2={x} y1={y - 6} y2={Math.max(14, y - 20)} stroke="#2ecc71" strokeWidth="1" opacity=".5" />
          <text x={x} y={Math.max(11, y - 22)} fill="#7fe0a3" fontSize="9.5" textAnchor="middle">#{s.id}</text>
          <title>#{s.id} {s.operator}{s.theme ? ` (${s.theme})` : ''} → {fmt(v)}{s.delta != null ? ` (Δ ${fmt(s.delta)})` : ' baseline'}</title>
        </g>
      })}
      <text x={pad} y={12} fill={AX} fontSize="11">best so far: {fmt(best)}</text>
      <text x={pad} y={h - 8} fill={AX} fontSize="11">node id →</text>
    </svg>
    <ChartLegend items={opsPresent.map(o => ({ label: operatorMeta(o).label, color: opColor(o) }))} />
    </div>
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
  return (
    <svg width="100%" viewBox={`0 0 ${width} ${h}`}>
      <line x1={pad} x2={width - pad} y1={base} y2={base} stroke={GRID} />
      {steps.map((s, i) => {
        const x = pad + i * (bw + gap)
        const yTo = Y(s.to), yFrom = s.from == null ? base : Y(s.from)
        const top = Math.min(yTo, yFrom), hgt = Math.max(3, Math.abs(yTo - yFrom))
        const improved = s.delta == null || (direction === 'min' ? s.delta < 0 : s.delta > 0)
        return <g key={i}>
          <rect x={x} y={s.from == null ? yTo : top} width={bw} height={s.from == null ? base - yTo : hgt} rx="3"
                fill={s.from == null ? '#4aa3ff' : (improved ? '#2ecc71' : '#ef4444')} opacity={s.from == null ? .55 : .9} />
          <text x={x + bw / 2} y={Math.max(11, top - 4)} fill="#9aa3b2" fontSize="9.5" textAnchor="middle">#{s.id}</text>
          <text x={x + bw / 2} y={base + 12} fill={AX} fontSize="9.5" textAnchor="middle">{fmt(s.to)}</text>
          <title>#{s.id} {s.operator} → {fmt(s.to)}{s.delta != null ? ` (Δ ${fmt(s.delta)})` : ' (baseline)'}</title>
        </g>
      })}
    </svg>
  )
}

export function Bars({ data, width = 760, height = 220, color = '#4aa3ff', fmtv = fmt }) {
  // data: [{label, value}]
  if (!data || !data.length) return <Empty>no data</Empty>
  const max = Math.max(...data.map(d => Math.abs(d.value)), 1e-9)
  const bh = 22, gap = 8, lab = 150, w = width
  const h = Math.max(height, data.length * (bh + gap) + 10)
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`}>
      {data.map((d, i) => {
        const y = i * (bh + gap) + 4
        const bw = Math.abs(d.value) / max * (w - lab - 60)
        return <g key={i}>
          <text x={lab - 8} y={y + bh / 2 + 4} fill="#e6e9ef" fontSize="12" textAnchor="end">{d.label}</text>
          <rect x={lab} y={y} width={bw} height={bh} rx="3" fill={color} opacity=".85" />
          <text x={lab + bw + 6} y={y + bh / 2 + 4} fill={AX} fontSize="11">{fmtv(d.value)}</text>
        </g>
      })}
    </svg>
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
  const rowH = 16, lab = 150, w = width, h = flat.length * rowH + 24
  const X = (t) => lab + (t - t0) / span * (w - lab - 20)
  const palette = { evaluate: '#2ecc71', implement: '#4aa3ff', propose: '#9a6bff', repair: '#ef4444', setup: '#f0b429', command: '#4aa3ff' }
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`}>
      {flat.map((s, i) => {
        const y = i * rowH + 4, x = X(s.start), bw = Math.max(2, (s.dur / span) * (w - lab - 20))
        return <g key={i} onClick={onPick ? () => onPick(s.nid) : undefined}
                  style={onPick ? { cursor: 'pointer' } : undefined}>
          <title>{s.nid}:{s.name} — {fmt(s.dur, 3)}s{s.err ? ' (ERROR)' : ''}</title>
          <text x={lab - 6} y={y + 11} fill="#9aa3b2" fontSize="10" textAnchor="end">{s.nid}:{s.name}</text>
          <rect x={x} y={y} width={bw} height={rowH - 5} rx="2" fill={s.err ? '#ef4444' : (palette[s.name] || '#4aa3ff')} opacity=".85" />
        </g>
      })}
      <text x={lab} y={h - 4} fill={AX} fontSize="11">{fmt(span)}s total span</text>
    </svg>
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
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`}>
      {axes.map((a, i) => <g key={a}>
        <line x1={AXX(i)} x2={AXX(i)} y1={pad} y2={h - pad} stroke={GRID} />
        <text x={AXX(i)} y={h - pad + 14} fill={AX} fontSize="11" textAnchor="middle">{a}</text>
      </g>)}
      {ev.map(n => {
        const pts = axes.map((a, i) => { const v = vals(n, a); return v == null ? null : [AXX(i), AXY(a, v)] }).filter(Boolean)
        const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0] + ' ' + p[1]).join(' ')
        return <path key={n.id} className={'pc-line' + (pick ? ' pick' : '')} d={d} fill="none"
          stroke={colorOf(n.confirmed_mean ?? n.metric)} strokeWidth="1.5" opacity=".7"
          onClick={pick ? () => pick(n.id) : undefined}>
          <title>#{n.id} {n.operator || ''} → {fmt(n.confirmed_mean ?? n.metric)}</title></path>
      })}
    </svg>
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
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} className={pick ? 'pickable' : ''}>
      {[0, .5, 1].map((t, i) => { const y = pad + t * (h - 2 * pad); return <line key={i} x1={pad} x2={w - pad} y1={y} y2={y} stroke={GRID} /> })}
      {data.map((d, i) => <circle key={i} className={'chart-pt' + (pick && d.id != null ? ' pick' : '')}
        cx={X(d.x)} cy={Y(d.y)} r="4.5" fill={d.feasible ? '#2ecc71' : '#7a6b9a'} opacity=".85"
        onClick={pick && d.id != null ? () => pick(d.id) : undefined}>
        <title>{d.id != null ? `#${d.id} · ` : ''}{xlab} {fmt(d.x)} · {ylab} {fmt(d.y)}{d.feasible ? '' : ' · infeasible'}</title>
      </circle>)}
      <text x={w / 2} y={h - 6} fill={AX} fontSize="11" textAnchor="middle">{xlab}</text>
      <text x={12} y={14} fill={AX} fontSize="11">{ylab}</text>
    </svg>
  )
}

// Tiny sparkline of a numeric series — used by collapsed-group super-cards, sweep node cards, and
// the inspector. Returns null for <2 points (nothing meaningful to draw).
export function Spark({ series, width = 120, height = 22 }) {
  if (!series || series.length < 2) return null
  const lo = Math.min(...series), hi = Math.max(...series), span = hi - lo || 1
  const W = width, H = height
  const pts = series.map((v, i) => `${(i / (series.length - 1) * W).toFixed(1)},${(H - (v - lo) / span * H).toFixed(1)}`).join(' ')
  return <svg className="grp-spark" width={W} height={H}><polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.5" /></svg>
}

function Empty({ children }) { return <div className="muted" style={{ padding: 20 }}>{children}</div> }

// Online training/eval curves — a small line chart per logged metric tag (loss, every recall@k, lr,
// grad norms, …) from a node's TensorBoard series {tag: [{step, value}]}. ALL metrics, not just the
// objective — the "a la TensorBoard" per-node view.
export function MetricLines({ series, cols = 2 }) {
  const tags = Object.keys(series || {}).filter(t => (series[t] || []).length > 0).sort()
  if (!tags.length) return <Empty>no metric curves logged yet — they appear once training starts writing TensorBoard events</Empty>
  return (
    <div style={{ display: 'grid', gridTemplateColumns: `repeat(${cols}, minmax(0,1fr))`, gap: 10 }}>
      {tags.map(t => <MiniLine key={t} label={t} pts={series[t]} />)}
    </div>
  )
}

function MiniLine({ label, pts, width = 340, height = 130 }) {
  const xs = pts.map(p => p.step), ys = pts.map(p => p.value)
  const minX = Math.min(...xs), maxX = Math.max(...xs)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const pad = 30, w = width, h = height
  const X = v => pad + (v - minX) / Math.max(1e-9, maxX - minX) * (w - pad - 8)
  const Y = v => h - pad - (v - minY) / Math.max(1e-9, maxY - minY) * (h - pad - 16)
  const d = pts.map((p, i) => (i ? 'L' : 'M') + X(p.step).toFixed(1) + ' ' + Y(p.value).toFixed(1)).join(' ')
  const last = ys[ys.length - 1]
  return (
    <div style={{ border: `1px solid ${GRID}`, borderRadius: 6, padding: 6, background: '#0d1017' }}>
      <div className="muted" style={{ fontSize: 11, marginBottom: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {label} · <span style={{ color: '#4aa3ff' }}>{fmt(last)}</span> <span style={{ opacity: .6 }}>({pts.length} pts)</span>
      </div>
      <svg width="100%" viewBox={`0 0 ${w} ${h}`}>
        {[0, .5, 1].map((t, i) => { const y = pad / 2 + t * (h - pad - 16); return <line key={i} x1={pad} x2={w - 8} y1={y} y2={y} stroke={GRID} /> })}
        <path d={d} fill="none" stroke="#2ecc71" strokeWidth="1.6" />
        <text x={2} y={pad / 2 + 4} fill={AX} fontSize="9">{fmt(maxY)}</text>
        <text x={2} y={h - pad + 4} fill={AX} fontSize="9">{fmt(minY)}</text>
        <text x={pad} y={h - 6} fill={AX} fontSize="9">step {minX}–{maxX}</text>
      </svg>
    </div>
  )
}
