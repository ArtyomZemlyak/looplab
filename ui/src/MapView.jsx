import React, { useEffect, useMemo } from 'react'
import {
  ReactFlow, Background, Controls, Handle, MiniMap, Panel, Position, useReactFlow,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { fmt } from './util.js'
import { regionGeometry, groupColor } from './grouping.js'
import { RegionShell, SuperShell } from './groupnodes.jsx'
import { OpIcon } from './icons.jsx'
import { packRunGrid, UNASSIGNED_CLUSTER } from './runMapModel.js'
import { effectiveRunStatus } from './runIndex.js'
import { followClientRoute } from './accessibility.jsx'

// Cross-run map: projects are regions and runs are readable cards inside them. Large clusters are
// represented by a super-node until expanded; expanded runs use bounded grid packing rather than an
// unbounded horizontal row (55 unassigned runs previously produced an ~11.7k px line).
const RUN_W = 190, RUN_H = 80, RUN_DX = 214, ROW_DY = 122, INDENT = 64

function RunNode({ data }) {
  const run = data.run
  const themes = Object.entries(run.themes || {})
  const status = effectiveRunStatus(run)
  const stalled = status === 'stalled'
  const open = () => data.onOpen(run.run_id)
  return (
    <a className="run-node nodrag nopan" href={`#/run/${encodeURIComponent(run.run_id)}`}
         onClick={event => followClientRoute(event, open)}
         aria-label={`Open ${run.label || run.run_id}, ${status}, ${run.task_id || 'unknown task'}`}
         title={run.goal}>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="row"><span className={'pill phase ' + status}>{status}</span>
        <b>{run.label || run.run_id}</b></div>
      <div className="muted">{run.label ? `${run.run_id} · ` : ''}{run.task_id} · best {fmt(run.best_confirmed ?? run.best_metric)} {run.direction || ''}</div>
      {themes.length > 0 && <div className="chips">{themes.slice(0, 4).map(([theme, info]) =>
        <span className="chip sm" key={theme} title={`best ${fmt(info.best_metric)}`}>{theme} <b>{info.count}</b></span>)}</div>}
    </a>
  )
}

function ProjRegion({ data }) {
  const toggle = () => data.onToggle(data.id)
  const tab = (
    <button type="button" className="grp-tab nodrag nopan"
         onClick={(event) => { event.stopPropagation(); toggle() }}
         title={`Collapse ${data.name}`}>
      <span className="grp-chev">▾</span><OpIcon name="folder" className="t-ic" /> {data.name}<span className="grp-n">{data.count}</span>
    </button>
  )
  return <RegionShell w={data.w} h={data.h} path={data.path} tint={data.tint} tab={tab} />
}

function ProjSuper({ data }) {
  return (
    <SuperShell tint={data.tint} onClick={() => data.onToggle(data.id)} title={`Expand ${data.name}`}>
      <div className="row">
        <span className="grp-chev btn-chev">▸</span>
        <b className="grp-name"><OpIcon name="folder" className="t-ic" /> {data.name}</b>
        <span className="spacer" style={{ flex: 1 }} /><span className="grp-n">{data.count}</span>
      </div>
      <div className="muted" style={{ marginTop: 3 }}>{data.runs} run{data.runs !== 1 ? 's' : ''} · expand to inspect</div>
    </SuperShell>
  )
}

const nodeTypes = { run: RunNode, projRegion: ProjRegion, projSuper: ProjSuper }

export function buildGraph(projects, runs, collapsed, onOpen, onToggle) {
  const byId = Object.fromEntries(projects.map(project => [project.id, project]))
  const childrenOf = { root: [] }
  projects.forEach(project => { (childrenOf[project.parent_id || 'root'] ||= []).push(project) })
  Object.values(childrenOf).forEach(items => items.sort((a, b) => a.name.localeCompare(b.name)))

  const runsByProject = {}
  runs.forEach(run => {
    const projectId = run.project_id && byId[run.project_id] ? run.project_id : UNASSIGNED_CLUSTER
    ;(runsByProject[projectId] ||= []).push(run)
  })
  const subtreeCache = new Map()
  const subtree = (id) => {
    if (subtreeCache.has(id)) return subtreeCache.get(id)
    const out = new Set([id]); const stack = [id]
    while (stack.length) {
      const current = stack.pop()
      ;(childrenOf[current] || []).forEach(child => { out.add(child.id); stack.push(child.id) })
    }
    subtreeCache.set(id, out); return out
  }
  const depthOf = (id) => { let depth = 0, current = byId[id]; while (current?.parent_id) { depth++; current = byId[current.parent_id] } return depth }
  const ancestorCollapsed = (id) => { let current = byId[id]?.parent_id; while (current) { if (collapsed.has(current)) return true; current = byId[current]?.parent_id } return false }
  const visibleCount = (ids) => runs.reduce((count, run) => count + (ids.has(run.project_id) ? 1 : 0), 0)
  const maxDepth = projects.length ? Math.max(...projects.map(project => depthOf(project.id))) : 0

  const graphNodes = [], runPositions = {}
  let y = 0
  const placeRuns = (items, x) => {
    const packed = packRunGrid(items, { x, y, dx: RUN_DX, dy: ROW_DY })
    items.forEach(run => {
      const position = packed.positions.get(run.run_id); runPositions[run.run_id] = position
      graphNodes.push({ id: `run:${run.run_id}`, type: 'run', position, zIndex: 5,
        width: RUN_W, height: RUN_H, data: { run, onOpen } })
    })
    if (items.length) y += packed.height
  }
  const visit = (project) => {
    if (ancestorCollapsed(project.id)) return
    const depth = depthOf(project.id)
    const count = visibleCount(subtree(project.id))
    if (collapsed.has(project.id) && count > 0) {
      graphNodes.push({ id: `ps:${project.id}`, type: 'projSuper', position: { x: depth * INDENT, y }, zIndex: 5,
        data: { id: project.id, name: project.name, count, runs: count, tint: groupColor(project.id), onToggle } })
      y += ROW_DY; return
    }
    placeRuns(runsByProject[project.id] || [], depth * INDENT)
    ;(childrenOf[project.id] || []).forEach(visit)
  }
  childrenOf.root.forEach(visit)

  const unassigned = runsByProject[UNASSIGNED_CLUSTER] || []
  if (unassigned.length) {
    if (collapsed.has(UNASSIGNED_CLUSTER)) {
      graphNodes.push({ id: `ps:${UNASSIGNED_CLUSTER}`, type: 'projSuper', position: { x: 0, y }, zIndex: 5,
        data: { id: UNASSIGNED_CLUSTER, name: 'Unassigned', count: unassigned.length, runs: unassigned.length,
          tint: groupColor(UNASSIGNED_CLUSTER), onToggle } })
      y += ROW_DY
    } else {
      placeRuns(unassigned, 0)
    }
  }

  const regions = []
  projects.forEach(project => {
    if (collapsed.has(project.id) || ancestorCollapsed(project.id)) return
    const ids = subtree(project.id)
    const rects = runs.filter(run => ids.has(run.project_id) && runPositions[run.run_id])
      .map(run => ({ ...runPositions[run.run_id], w: RUN_W, h: RUN_H }))
    if (!rects.length) return
    const depth = depthOf(project.id)
    const geometry = regionGeometry(rects, 18 + (maxDepth - depth) * 16)
    regions.push({ id: `pr:${project.id}`, type: 'projRegion', position: { x: geometry.x, y: geometry.y }, zIndex: depth,
      selectable: false, draggable: false, focusable: false,
      data: { id: project.id, name: project.name, count: rects.length, w: geometry.w, h: geometry.h,
        path: geometry.path, tint: groupColor(project.id), onToggle } })
  })
  if (unassigned.length && !collapsed.has(UNASSIGNED_CLUSTER)) {
    const rects = unassigned.map(run => ({ ...runPositions[run.run_id], w: RUN_W, h: RUN_H }))
    const geometry = regionGeometry(rects, 24)
    regions.push({ id: `pr:${UNASSIGNED_CLUSTER}`, type: 'projRegion', position: { x: geometry.x, y: geometry.y }, zIndex: 0,
      selectable: false, draggable: false, focusable: false,
      data: { id: UNASSIGNED_CLUSTER, name: 'Unassigned', count: rects.length, w: geometry.w, h: geometry.h,
        path: geometry.path, tint: groupColor(UNASSIGNED_CLUSTER), onToggle } })
  }

  const edges = []
  runs.forEach(run => (run.seeded_from || []).forEach(source => {
    if (runPositions[run.run_id] && runPositions[source]) edges.push({
      id: `seed:${source}->${run.run_id}`, source: `run:${source}`, target: `run:${run.run_id}`, className: 'seed-edge',
    })
  }))
  return { nodes: [...regions, ...graphNodes], edges }
}

function FitVisible({ signature }) {
  const { fitView } = useReactFlow()
  useEffect(() => {
    const frame = requestAnimationFrame(() => fitView({ padding: 0.16, maxZoom: 1 }))
    return () => cancelAnimationFrame(frame)
    // fitView changes the React Flow store; keying only on the visible node signature prevents a
    // fit→render→fit feedback loop while the run-list polling refreshes object identities.
  }, [signature])
  return null
}

export default function MapView({ onOpen, runs = [], projects = [], collapsed = new Set(), onToggle, scopeLabel = 'All runs' }) {
  const { nodes, edges } = useMemo(
    () => buildGraph(projects, runs, collapsed, onOpen, onToggle),
    [projects, runs, collapsed, onOpen, onToggle])
  // Reframe when the filtered run scope changes, but preserve zoom/pan when a user expands a cluster.
  // Auto-fitting all 57 newly revealed cards would immediately shrink their text back to ~40%.
  const signature = runs.map(run => run.run_id).sort().join('|')
  const runNodeCount = nodes.filter(node => node.type === 'run').length
  const collapsedIds = nodes.filter(node => node.type === 'projSuper').map(node => node.data.id)

  return (
    <div className="mapwrap">
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes}
                  minZoom={0.15} maxZoom={1.6} proOptions={{ hideAttribution: true }} nodesDraggable={false}
                  nodesFocusable={false} onlyRenderVisibleElements>
        <FitVisible signature={signature} />
        <Background color="var(--line)" gap={22} />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable className="run-minimap" nodeColor={node => node.type === 'run' ? 'var(--accent)' : 'var(--line-2)'} />
        <Panel position="top-left" className="map-summary">
          <b>{runs.length} runs</b><span>{scopeLabel}</span>
          <span>{runNodeCount} visible · {collapsedIds.length} collapsed cluster{collapsedIds.length === 1 ? '' : 's'}</span>
          {collapsedIds.length > 0 && <button className="btn sm" onClick={() => collapsedIds.forEach(onToggle)}>Expand clusters</button>}
        </Panel>
      </ReactFlow>
    </div>
  )
}
