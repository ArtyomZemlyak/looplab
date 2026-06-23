import React, { useMemo, useState } from 'react'
import { ReactFlow, Background, Controls, MiniMap, Handle, Position, Panel } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { fmt, layoutWithGroups, nodeClass, delta, workingId, operatorMeta, OPERATOR_LEGEND } from './util.js'
import { OpIcon } from './icons.jsx'
import { RegionShell, SuperShell } from './groupnodes.jsx'
import {
  computeGroups, nodeGroupMap, regionGeometry, rerouteForCollapse, groupColor,
  groupAggregate, superId, GROUP_MODES,
} from './grouping.js'

const NODE_W = 190, NODE_H = 84

function agentBadge(rep) {
  if (!rep) return null
  if (rep.ok && !rep.fell_back) return <span className="badge agent-ok">✓agent</span>
  if (rep.fell_back) return <span className="badge agent-fb">↩fallback</span>
  return <span className="badge agent-x">✗agent</span>
}

function ExpNode({ data }) {
  const { node, state, workId, selectedId, onSelect } = data
  const m = node.confirmed_mean ?? node.metric
  const d = delta(node, state)
  const op = operatorMeta(node.operator)
  return (
    <div className={nodeClass(node, state, workId) + (node.id === selectedId ? ' sel' : '')}
         onClick={() => onSelect(node.id)} title={op.label + (node.idea?.rationale ? ' — ' + node.idea.rationale : '')}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="row">
        <span className="nid">#{node.id}</span>
        <span className="op"><OpIcon name={op.icon} /> {node.operator}</span>
        <span className="spacer" style={{ flex: 1 }} />
        {node.id === state.best_node_id && <span className="crown" title="champion">♚</span>}
        {agentBadge(node.agent_report)}
      </div>
      <div className="metric">
        {fmt(m)}
        {d && <span className={'delta ' + (d.improved ? 'up' : 'down')}>{d.improved ? '▲' : '▼'}{fmt(Math.abs(d.d), 2)}</span>}
      </div>
      <div className="sub">
        {node.status === 'failed'
          ? <span className="badge reason">{node.error_reason || 'failed'}</span>
          : node.confirmed_mean != null
            ? <>robust {fmt(node.confirmed_mean, 3)} ±{fmt(node.confirmed_std, 2)} ({node.confirmed_seeds}×)</>
            : node.feasible === false ? <span className="badge reason">infeasible</span>
              : node.status}
      </div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

// Soft enclosing region behind an expanded group. Only the label tab is interactive (the hull is
// pointer-transparent) so it never steals clicks from the nodes drawn on top.
function GroupRegion({ data }) {
  const { w, h, path, label, count, tint, onToggle } = data
  const tab = (
    <div className="grp-tab" onClick={(e) => { e.stopPropagation(); onToggle(label) }} title="collapse group">
      <span className="grp-chev">▾</span>{label}<span className="grp-n">{count}</span>
    </div>
  )
  return <RegionShell w={w} h={h} path={path} tint={tint} tab={tab} />
}

function Spark({ series }) {
  if (!series || series.length < 2) return null
  const lo = Math.min(...series), hi = Math.max(...series), span = hi - lo || 1
  const W = 120, H = 22
  const pts = series.map((v, i) => `${(i / (series.length - 1) * W).toFixed(1)},${(H - (v - lo) / span * H).toFixed(1)}`).join(' ')
  return <svg className="grp-spark" width={W} height={H}><polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.5" /></svg>
}

// Collapsed group → one aggregate card (semantic zoom). Body selects (group summary); the ▸ expands.
function GroupSuper({ data }) {
  const { label, count, best, series, status, tint, selected, onExpand, onSelect } = data
  return (
    <SuperShell tint={tint} selected={selected} onClick={() => onSelect(label)}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="row">
        <button className="grp-chev btn-chev" title="expand group" onClick={(e) => { e.stopPropagation(); onExpand(label) }}>▸</button>
        <b className="grp-name">{label}</b>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="grp-n">{count}</span>
      </div>
      <div className="metric">best {fmt(best)}</div>
      <Spark series={series} />
      <div className="grp-dots">
        {status.evaluated ? <span className="dot ok" title={`${status.evaluated} evaluated`}>●{status.evaluated}</span> : null}
        {status.failed ? <span className="dot fail" title={`${status.failed} failed`}>●{status.failed}</span> : null}
        {status.pending ? <span className="dot pend" title={`${status.pending} pending`}>●{status.pending}</span> : null}
      </div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </SuperShell>
  )
}

const nodeTypes = { exp: ExpNode, groupRegion: GroupRegion, groupSuper: GroupSuper }

export default function Dag({ state, selectedId, onSelect, groupMode = 'none', collapsed = new Set(),
                             onToggleGroup, onSetMode, onCollapseAll, onExpandAll, selectedGroup, onSelectGroup }) {
  const workId = workingId(state)
  const [showMap, setShowMap] = useState(() => localStorage.getItem('ll.minimap') === '1')
  const [showLegend, setShowLegend] = useState(false)
  const toggleMap = () => setShowMap(v => { localStorage.setItem('ll.minimap', v ? '0' : '1'); return !v })

  const { nodes, edges, groupKeys } = useMemo(() => {
    const ns = state?.nodes || {}
    const groups = groupMode === 'none' ? new Map() : computeGroups(ns, groupMode)
    const ng = nodeGroupMap(groups)
    const pos = layoutWithGroups(ns, { collapsed, nodeGroup: ng })
    const { hidden, edges: reEdges } = rerouteForCollapse(ns, collapsed, ng)
    const rfNodes = []

    // 1) region hulls for EXPANDED groups (drawn behind everything)
    groups.forEach((ids, key) => {
      if (collapsed.has(key)) return
      const rects = ids.map(id => pos[`n:${id}`]).filter(Boolean).map(p => ({ x: p.x, y: p.y, w: NODE_W, h: NODE_H }))
      if (!rects.length) return
      const geo = regionGeometry(rects)
      rfNodes.push({
        id: `region:${key}`, type: 'groupRegion', position: { x: geo.x, y: geo.y }, zIndex: 0,
        selectable: false, draggable: false, focusable: false,
        data: { w: geo.w, h: geo.h, path: geo.path, label: key, count: ids.length, tint: groupColor(key), onToggle: onToggleGroup },
      })
    })

    // 2) collapsed groups → super-nodes
    groups.forEach((ids, key) => {
      if (!collapsed.has(key)) return
      const p = pos[superId(key)]; if (!p) return
      const agg = groupAggregate(ids, ns, state.direction)
      rfNodes.push({
        id: superId(key), type: 'groupSuper', position: p, zIndex: 1,
        data: {
          label: key, count: agg.count, best: agg.best, series: agg.series, status: agg.status,
          tint: groupColor(key), selected: key === selectedGroup, onExpand: onToggleGroup, onSelect: onSelectGroup,
        },
      })
    })

    // 3) visible experiment nodes
    Object.values(ns).forEach(n => {
      if (hidden.has(n.id)) return
      const p = pos[`n:${n.id}`]; if (!p) return
      rfNodes.push({ id: `n:${n.id}`, type: 'exp', position: p, zIndex: 1,
        data: { node: n, state, workId, selectedId, onSelect } })
    })

    // edges (already rerouted around collapsed groups)
    const rfEdges = reEdges.map(e => {
      const child = ns[e.dstId]
      // debug semantics attach to the child (the repair node). Keep the styling as long as the
      // child is visible — even if the parent was collapsed and the edge now starts at a super-node.
      const isDebug = child && child.operator === 'debug' && !hidden.has(e.dstId)
      const onPath = e.dstId === selectedId || e.srcId === selectedId
      return {
        id: `${e.source}->${e.target}`, source: e.source, target: e.target,
        className: isDebug ? 'debug' : (onPath ? 'lineage' : ''),
        animated: e.dstId === workId && !hidden.has(e.dstId),
      }
    })
    return { nodes: rfNodes, edges: rfEdges, groupKeys: [...groups.keys()] }
  }, [state, selectedId, workId, onSelect, groupMode, collapsed, selectedGroup, onToggleGroup, onSelectGroup])

  return (
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.15} maxZoom={1.8}
               proOptions={{ hideAttribution: true }} nodesDraggable={false} onPaneClick={() => { onSelect(null); onSelectGroup && onSelectGroup(null) }}>
      <Background color="#20252f" gap={22} />
      <Controls showInteractive={false} />
      <Panel position="top-right" className="grp-control">
        <span className="muted">group by</span>
        <select className="text" value={groupMode} onChange={e => onSetMode && onSetMode(e.target.value)}>
          {GROUP_MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select>
        {groupMode !== 'none' && groupKeys.length > 0 && <>
          <button className="btn sm ghost" title="collapse all groups" onClick={() => onCollapseAll && onCollapseAll(groupKeys)}>⊟ all</button>
          <button className="btn sm ghost" title="expand all groups" onClick={() => onExpandAll && onExpandAll()}>⊞ all</button>
        </>}
      </Panel>
      {/* lift the toggles above the overview map when it's open — otherwise the minimap (also
          bottom-right) covers this row and you can't click 🗺 again to hide it. */}
      <Panel position="bottom-right" className="map-toggles" style={{ marginBottom: showMap ? 152 : 0 }}>
        <button className={'btn sm ghost' + (showLegend ? ' primary' : '')} title="operator legend"
                onClick={() => setShowLegend(v => !v)}>ⓘ ops</button>
        <button className={'btn sm ghost' + (showMap ? ' primary' : '')} title={showMap ? 'hide overview map' : 'show overview map'}
                onClick={toggleMap}>🗺 map{showMap ? ' ✕' : ''}</button>
      </Panel>
      {showLegend && <Panel position="top-left" className="op-legend">
        <div className="legend-h">Operators</div>
        {OPERATOR_LEGEND.map(o => { const m = operatorMeta(o); return (
          <div className="legend-row" key={o}>
            <span className="op-icon"><OpIcon name={m.icon} /></span><span>{m.label}</span>
          </div>) })}
      </Panel>}
      {showMap && <MiniMap position="bottom-right" pannable zoomable nodeColor={(n) => {
        const nd = n.data?.node; if (!nd) return '#3a4250'
        if (nd.id === state.best_node_id) return '#ffd54a'
        if (nd.status === 'failed') return '#ef4444'
        if (nd.status === 'evaluated') return '#2ecc71'
        return '#6b7686'
      }} style={{ background: '#12151c', width: 180, height: 130 }} />}
    </ReactFlow>
  )
}
