import React from 'react'
import { fmt } from './util.js'

const AX = '#6b7480', GRID = '#20252f'

// Best-metric-over-time + all-node scatter.
export function Trajectory({ nodes, direction, width = 760, height = 220 }) {
  const evald = nodes.filter(n => (n.metric ?? null) !== null).sort((a, b) => a.id - b.id)
  if (!evald.length) return <Empty>no evaluated nodes yet</Empty>
  const xs = evald.map(n => n.id)
  const ys = evald.map(n => n.confirmed_mean ?? n.metric)
  const minY = Math.min(...ys), maxY = Math.max(...ys)
  const pad = 34, w = width, h = height
  const X = (id) => pad + (id - Math.min(...xs)) / Math.max(1, (Math.max(...xs) - Math.min(...xs))) * (w - pad - 10)
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
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`}>
      {[0, .25, .5, .75, 1].map((t, i) => {
        const y = pad / 2 + t * (h - pad - 12)
        return <line key={i} x1={pad} x2={w - 10} y1={y} y2={y} stroke={GRID} />
      })}
      {evald.map(n => {
        const v = n.confirmed_mean ?? n.metric
        return <circle key={n.id} cx={X(n.id)} cy={Y(v)} r={n.feasible === false ? 2.5 : 3.5}
          fill={n.feasible === false ? '#7a6b9a' : '#4aa3ff'} opacity={.8} />
      })}
      <path d={line} fill="none" stroke="#2ecc71" strokeWidth="2" />
      <text x={pad} y={12} fill={AX} fontSize="11">best so far: {fmt(best)}</text>
      <text x={pad} y={h - 8} fill={AX} fontSize="11">node id →</text>
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
export function ParallelCoords({ nodes, direction, width = 760, height = 260 }) {
  const ev = nodes.filter(n => (n.metric ?? null) !== null)
  if (!ev.length) return <Empty>no evaluated nodes</Empty>
  const params = Array.from(new Set(ev.flatMap(n => Object.keys(n.idea?.params || {}))))
  const axes = [...params, 'metric']
  if (axes.length < 2) return <Empty>not enough dimensions</Empty>
  const vals = (n, a) => a === 'metric' ? (n.confirmed_mean ?? n.metric) : n.idea?.params?.[a]
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
        return <path key={n.id} d={d} fill="none" stroke={colorOf(n.confirmed_mean ?? n.metric)} strokeWidth="1.5" opacity=".7" />
      })}
    </svg>
  )
}

// metric vs a constraint value (Pareto-ish). data: [{x,y,feasible,id}]
export function Scatter({ data, xlab, ylab, width = 720, height = 260 }) {
  if (!data || !data.length) return <Empty>no constraint data</Empty>
  const xs = data.map(d => d.x), ys = data.map(d => d.y)
  const pad = 40, w = width, h = height
  const X = v => pad + (v - Math.min(...xs)) / Math.max(1e-9, Math.max(...xs) - Math.min(...xs)) * (w - 2 * pad)
  const Y = v => h - pad - (v - Math.min(...ys)) / Math.max(1e-9, Math.max(...ys) - Math.min(...ys)) * (h - 2 * pad)
  return (
    <svg width="100%" viewBox={`0 0 ${w} ${h}`}>
      {data.map((d, i) => <circle key={i} cx={X(d.x)} cy={Y(d.y)} r="4" fill={d.feasible ? '#2ecc71' : '#7a6b9a'} opacity=".85" />)}
      <text x={w / 2} y={h - 6} fill={AX} fontSize="11" textAnchor="middle">{xlab}</text>
      <text x={12} y={14} fill={AX} fontSize="11">{ylab}</text>
    </svg>
  )
}

function Empty({ children }) { return <div className="muted" style={{ padding: 20 }}>{children}</div> }
