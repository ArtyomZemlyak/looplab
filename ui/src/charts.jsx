import React from 'react'
import { fmt, operatorMeta } from './util.js'

const AX = '#6b7480', GRID = '#20252f'

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
    <span key={i} className={'chart-leg' + (onPick ? ' pick' : '') + (active && active !== it.key ? ' dim' : '')}
      onClick={onPick ? () => onPick(active === it.key ? null : it.key) : undefined}
      title={onPick ? (active === it.key ? 'show all' : `show only ${it.label}`) : undefined}>
      <span className="chart-leg-dot" style={{ background: it.color }} />{it.label}</span>)}</div>
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
  const grpColor = (n) => n.feasible === false ? '#7a6b9a'
    : (groupBy === 'theme' ? themeColor(grpKey(n)) : opColor(n.operator))
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
  const groupsPresent = [...new Set(evald.map(grpKey).filter(Boolean))]
  const hasThemes = evald.some(n => n.idea?.theme)
  return (
    <div className="chart">
    <div className="chart-tools">
      {hasThemes && <span className="chart-grp">group:
        {['operator', 'theme'].map(g => <button key={g} className={'btn xs ghost' + (groupBy === g ? ' primary' : '')}
          onClick={() => { setGroupBy(g); setFocusGrp(null) }} title={`colour points by ${g}`}>{g}</button>)}
      </span>}
      {canLog && <button className={'btn xs ghost' + (logY ? ' primary' : '')} onClick={() => setLogY(v => !v)}
        title="toggle a logarithmic Y axis">log Y</button>}
    </div>
    <svg width="100%" viewBox={`0 0 ${w} ${h}`} className={pick ? 'pickable' : ''}
         onMouseMove={(e) => { const r = e.currentTarget.getBoundingClientRect()
           const px = (e.clientX - r.left) / r.width * w; const n = nearest(px); setHoverId(n ? n.id : null) }}
         onMouseLeave={() => setHoverId(null)}>
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
        return <circle cx={X(sn.id)} cy={Y(v)} r="7.5" fill="none" stroke="#eaf2ff" strokeWidth="2" opacity=".9" pointerEvents="none" />
      })()}
      {evald.map(n => {
        const v = n.confirmed_mean ?? n.metric
        const c = grpColor(n)
        const dim = focusGrp && grpKey(n) !== focusGrp   // legend focus: fade the other groups
        return <circle key={n.id} className={'chart-pt' + (pick ? ' pick' : '')} cx={X(n.id)} cy={Y(v)}
          r={n.id === selected ? 5 : (n.feasible === false ? 2.8 : 4)} fill={c} opacity={dim ? .12 : .85}
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
      {hn && (() => {   // hover crosshair + tooltip tracking the nearest node
        const v = hn.confirmed_mean ?? hn.metric, hx = X(hn.id), hy = Y(v)
        const label = `#${hn.id} ${hn.operator} → ${fmt(v)}`
        const tw = Math.max(64, label.length * 6.0), tx = Math.min(w - 10 - tw, Math.max(pad, hx - tw / 2))
        return <g pointerEvents="none">
          <line x1={hx} x2={hx} y1={pad / 2} y2={h - pad} stroke={AX} strokeDasharray="3 3" opacity=".6" />
          <circle cx={hx} cy={hy} r="5" fill="none" stroke="#eaf2ff" strokeWidth="1.5" />
          <rect x={tx} y={2} width={tw} height={16} rx="3" fill="#12151c" stroke={GRID} />
          <text x={tx + tw / 2} y={13} fill="#eaf2ff" fontSize="10.5" textAnchor="middle">{label}</text>
        </g>
      })()}
      <text x={pad} y={12} fill={AX} fontSize="11">best so far: {fmt(best)}{useLog ? ' · log Y' : ''}</text>
      <text x={pad} y={h - 8} fill={AX} fontSize="11">node id →</text>
    </svg>
    <ChartLegend items={groupsPresent.map(g => ({ key: g, label: grpLabel(g), color: grpSwatch(g) }))}
                 active={focusGrp} onPick={setFocusGrp} />
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

// U4 · overlay several runs' running-best trajectories on ONE axis, to compare convergence at a
// glance. `runs` = [{label, run_id, series:[running-best value per evaluated node]}]. x = experiment
// index (runs have different lengths — each line just stops at its own end); y = shared metric range.
const _RUN_COLORS = ['#4aa3ff', '#2ecc71', '#f0b429', '#e0559a', '#8b5cf6', '#22d3d3', '#ff7a45', '#9aa7b5']
export function MultiTrajectory({ runs, width = 760, height = 240 }) {
  const withData = (runs || []).filter(r => (r.series || []).length > 0)
  if (!withData.length) return <Empty>no comparable run trajectories yet</Empty>
  const allV = withData.flatMap(r => r.series)
  const lo = Math.min(...allV), hi = Math.max(...allV), span = (hi - lo) || 1
  const maxLen = Math.max(...withData.map(r => r.series.length))
  const pad = 34, w = width, h = height
  const X = i => pad + (maxLen <= 1 ? 0 : i / (maxLen - 1) * (w - pad - 10))
  const Y = v => h - pad - (v - lo) / span * (h - pad - 12)
  return (
    <div>
      <svg width={w} height={h} role="img">
        <line x1={pad} y1={h - pad} x2={w - 8} y2={h - pad} stroke="var(--border)" />
        <line x1={pad} y1={12} x2={pad} y2={h - pad} stroke="var(--border)" />
        <text x={pad - 6} y={16} textAnchor="end" fontSize="10" fill="var(--fg-mut)">{fmt(hi)}</text>
        <text x={pad - 6} y={h - pad} textAnchor="end" fontSize="10" fill="var(--fg-mut)">{fmt(lo)}</text>
        <text x={(w + pad) / 2} y={h - 6} textAnchor="middle" fontSize="10" fill="var(--fg-mut)">experiment #</text>
        {withData.map((r, k) => {
          const c = _RUN_COLORS[k % _RUN_COLORS.length]
          const pts = r.series.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')
          return <polyline key={r.run_id || k} points={pts} fill="none" stroke={c} strokeWidth="1.8" opacity="0.9" />
        })}
      </svg>
      <div className="row" style={{ flexWrap: 'wrap', gap: 10, marginTop: 4 }}>
        {withData.map((r, k) => <span key={r.run_id || k} className="muted" style={{ fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          <span style={{ width: 10, height: 3, background: _RUN_COLORS[k % _RUN_COLORS.length], display: 'inline-block' }} />
          {r.label || r.run_id}</span>)}
      </div>
    </div>
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
  const [open, setOpen] = React.useState(true)
  return (
    <div style={{ marginBottom: 8 }}>
      <div onClick={() => setOpen(o => !o)}
           style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6, padding: '4px 2px',
                    fontSize: 12, fontWeight: 600, userSelect: 'none' }}>
        <span style={{ opacity: 0.6, fontSize: 10, width: 10, display: 'inline-block' }}>{open ? '▾' : '▸'}</span>
        {name} <span className="muted" style={{ fontWeight: 400 }}>· {tags.length} metric{tags.length === 1 ? '' : 's'}</span>
      </div>
      {open && <div style={{ display: 'grid', gridTemplateColumns: `repeat(${cols}, minmax(0,1fr))`, gap: 10 }}>
        {tags.map(t => <MiniLine key={t} label={t} pts={series[t]} />)}
      </div>}
    </div>
  )
}

function MiniLine({ label, pts, width = 340, height = 130 }) {
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
  return (
    <div style={{ border: `1px solid ${GRID}`, borderRadius: 6, padding: 6, background: '#0d1017' }}>
      <div className="muted" style={{ fontSize: 11, marginBottom: 2, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
        {label} · <span style={{ color: '#4aa3ff' }}>{hp ? `step ${hp.step}: ${fmt(hp.value)}` : fmt(last)}</span> <span style={{ opacity: .6 }}>({pts.length} pts)</span>
      </div>
      <svg width="100%" viewBox={`0 0 ${w} ${h}`}
           onMouseMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); setHi(nearestIdx((e.clientX - r.left) / r.width * w)) }}
           onMouseLeave={() => setHi(null)}>
        {[0, .5, 1].map((t, i) => { const y = pad / 2 + t * (h - pad - 16); return <line key={i} x1={pad} x2={w - 8} y1={y} y2={y} stroke={GRID} /> })}
        <path d={d} fill="none" stroke="#2ecc71" strokeWidth="1.6" />
        {hp && <><line x1={X(hp.step)} x2={X(hp.step)} y1={pad / 2} y2={h - pad} stroke={AX} strokeDasharray="3 3" opacity=".6" />
          <circle cx={X(hp.step)} cy={Y(hp.value)} r="3.5" fill="none" stroke="#eaf2ff" strokeWidth="1.4" /></>}
        <text x={2} y={pad / 2 + 4} fill={AX} fontSize="9">{fmt(maxY)}</text>
        <text x={2} y={h - pad + 4} fill={AX} fontSize="9">{fmt(minY)}</text>
        <text x={pad} y={h - 6} fill={AX} fontSize="9">step {minX}–{maxX}</text>
      </svg>
    </div>
  )
}
