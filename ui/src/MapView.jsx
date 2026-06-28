import React, { useEffect, useMemo, useState } from 'react'
import { ReactFlow, Background, Controls, MarkerType, Handle, Position } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { get, fmt, listProjects } from './util.js'
import { regionGeometry, groupColor } from './grouping.js'
import { RegionShell, SuperShell } from './groupnodes.jsx'
import { OpIcon } from './icons.jsx'

// Cross-run map: the SAME hull / super-node visual language as the in-run canvas, lifted one level
// — projects are (nestable) region hulls, runs are nodes inside them with theme chips. Collapsing a
// project yields a project super-node; clicking a run drills into its in-run canvas. One continuum.
const RUN_W = 190, RUN_H = 80, RUN_DX = 214, ROW_DY = 122, INDENT = 64

function RunNode({ data }) {
  const r = data.run
  const themes = Object.entries(r.themes || {})
  return (
    <div className="run-node" onClick={() => data.onOpen(r.run_id)} title={r.goal}>
      {/* invisible handles so cross-run "derived-from" edges can attach (left=source, right=target) */}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="row"><span className="pill phase">{r.phase}</span><b>{r.run_id}</b></div>
      <div className="muted">{r.task_id} · best {fmt(r.best_confirmed ?? r.best_metric)} · {r.nodes} nodes</div>
      {themes.length > 0 && <div className="chips">{themes.slice(0, 5).map(([t, info]) =>
        <span className="chip sm" key={t} title={`best ${fmt(info.best_metric)}`}>{t} <b>{info.count}</b></span>)}</div>}
    </div>
  )
}

function ProjRegion({ data }) {
  const tab = (
    <div className="grp-tab" onClick={(e) => { e.stopPropagation(); data.onToggle(data.id) }} title="collapse project">
      <span className="grp-chev">▾</span><OpIcon name="folder" className="t-ic" /> {data.name}<span className="grp-n">{data.count}</span>
    </div>
  )
  return <RegionShell w={data.w} h={data.h} path={data.path} tint={data.tint} tab={tab} />
}

function ProjSuper({ data }) {
  return (
    <SuperShell tint={data.tint} onClick={() => data.onToggle(data.id)} title="expand project">
      <div className="row">
        <button className="grp-chev btn-chev">▸</button>
        <b className="grp-name"><OpIcon name="folder" className="t-ic" /> {data.name}</b>
        <span className="spacer" style={{ flex: 1 }} /><span className="grp-n">{data.count}</span>
      </div>
      <div className="muted" style={{ marginTop: 3 }}>{data.runs} run{data.runs !== 1 ? 's' : ''}</div>
    </SuperShell>
  )
}

const nodeTypes = { run: RunNode, projRegion: ProjRegion, projSuper: ProjSuper }

function buildGraph(projects, runs, collapsed, onOpen, onToggle) {
  const byId = Object.fromEntries(projects.map(p => [p.id, p]))
  const childrenOf = { root: [] }
  projects.forEach(p => { (childrenOf[p.parent_id || 'root'] ||= []).push(p) })
  Object.values(childrenOf).forEach(a => a.sort((x, y) => x.name.localeCompare(y.name)))
  // a run whose project_id is null OR points to a project that no longer exists falls into the
  // unassigned bucket — otherwise it would be keyed under a dangling id that `visit` never walks,
  // and vanish from the map entirely.
  const runsByProj = {}
  runs.forEach(r => { const pid = (r.project_id && byId[r.project_id]) ? r.project_id : '__un'; (runsByProj[pid] ||= []).push(r) })

  const depthOf = (id) => { let d = 0, c = byId[id]; while (c && c.parent_id) { d++; c = byId[c.parent_id] } return d }
  const subtree = (id) => { const out = [id], st = [id]; while (st.length) { const x = st.pop(); (childrenOf[x] || []).forEach(k => { out.push(k.id); st.push(k.id) }) } return out }
  const ancestorCollapsed = (id) => { let c = byId[id] ? byId[id].parent_id : null; while (c) { if (collapsed.has(c)) return true; c = byId[c] ? byId[c].parent_id : null } return false }
  const countRuns = (ids) => runs.filter(r => ids.includes(r.project_id)).length
  const maxD = projects.length ? Math.max(...projects.map(p => depthOf(p.id))) : 0

  const rfNodes = [], runPos = {}
  let y = 0
  const visit = (p) => {
    if (ancestorCollapsed(p.id)) return
    const d = depthOf(p.id)
    if (collapsed.has(p.id)) {
      const total = countRuns(subtree(p.id))
      rfNodes.push({ id: 'ps:' + p.id, type: 'projSuper', position: { x: d * INDENT, y }, zIndex: 5,
        data: { id: p.id, name: p.name, count: total, runs: total, tint: groupColor(p.id), onToggle } })
      y += ROW_DY
      return
    }
    const myRuns = runsByProj[p.id] || []
    myRuns.forEach((r, i) => { const pos = { x: d * INDENT + i * RUN_DX, y }; runPos[r.run_id] = pos
      rfNodes.push({ id: 'run:' + r.run_id, type: 'run', position: pos, zIndex: 5,
        width: RUN_W, height: RUN_H, data: { run: r, onOpen } }) })
    if (myRuns.length) y += ROW_DY
    ;(childrenOf[p.id] || []).forEach(visit)
  }
  childrenOf.root.forEach(visit)

  const un = runsByProj.__un || []
  un.forEach((r, i) => { const pos = { x: i * RUN_DX, y }; runPos[r.run_id] = pos
    rfNodes.push({ id: 'run:' + r.run_id, type: 'run', position: pos, zIndex: 5,
      width: RUN_W, height: RUN_H, data: { run: r, onOpen } }) })

  // project regions: subtree bbox over placed runs (parent encloses children → genuine nesting)
  projects.forEach(p => {
    if (collapsed.has(p.id) || ancestorCollapsed(p.id)) return
    const ids = subtree(p.id)
    const rects = runs.filter(r => ids.includes(r.project_id) && runPos[r.run_id]).map(r => ({ ...runPos[r.run_id], w: RUN_W, h: RUN_H }))
    if (!rects.length) return
    const d = depthOf(p.id)
    const geo = regionGeometry(rects, 18 + (maxD - d) * 16)
    rfNodes.unshift({ id: 'pr:' + p.id, type: 'projRegion', position: { x: geo.x, y: geo.y }, zIndex: d,
      selectable: false, draggable: false, focusable: false,
      data: { id: p.id, name: p.name, count: rects.length, w: geo.w, h: geo.h, path: geo.path, tint: groupColor(p.id), onToggle } })
  })
  // Cross-run lineage: a "derived-from" edge for each run that SEEDED an experiment from another run
  // (run.seeded_from, from the backend). Only drawn when BOTH runs are placed (not hidden inside a
  // collapsed project) — this is the genuine node→node-across-runs provenance the in-run canvas can't show.
  const edges = []
  runs.forEach(r => (r.seeded_from || []).forEach(src => {
    if (runPos[r.run_id] && runPos[src]) {
      // minimal edge object (styling via the .seed-edge CSS class) — mirrors the in-run canvas's
      // proven pattern; an inline style/label/markerEnd here prevents the path from rendering in v12.
      edges.push({ id: `seed:${src}->${r.run_id}`, source: 'run:' + src, target: 'run:' + r.run_id,
        className: 'seed-edge', markerEnd: { type: MarkerType.ArrowClosed } })
    }
  }))
  return { nodes: rfNodes, edges }
}

export default function MapView({ onOpen }) {
  const [projects, setProjects] = useState([])
  const [runs, setRuns] = useState([])
  const [collapsed, setCollapsed] = useState(() => new Set())
  useEffect(() => {
    listProjects().then(d => setProjects(d.projects || [])).catch(() => {})
    get('/api/runs').then(setRuns).catch(() => {})
  }, [])
  const toggle = (id) => setCollapsed(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n })
  const { nodes, edges } = useMemo(() => buildGraph(projects, runs, collapsed, onOpen, toggle), [projects, runs, collapsed, onOpen])

  return (
    <div className="mapwrap">
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.1} maxZoom={1.6}
                 proOptions={{ hideAttribution: true }} nodesDraggable={false}>
        <Background color="#20252f" gap={22} />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  )
}
