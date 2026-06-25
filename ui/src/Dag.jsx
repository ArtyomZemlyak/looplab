import React, { useMemo, useState } from 'react'
import { ReactFlow, Background, Controls, MiniMap, Handle, Position, Panel } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { fmt, layoutWithGroups, nodeClass, delta, workingId, operatorMeta, OPERATOR_LEGEND, isSweep, sweepInfo } from './util.js'
import { changeLabel } from './report.js'
import { OpIcon } from './icons.jsx'
import { Spark } from './charts.jsx'
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
  const { node, state, workId, selectedId, onSelect, onExplode, exploded, themeFilter } = data
  const m = node.confirmed_mean ?? node.metric
  const d = delta(node, state)
  const op = operatorMeta(node.operator)
  const sweep = isSweep(node)
  const sw = sweep ? sweepInfo(node) : null
  // E2/E3: a one-line "what changed vs parent" chip (git-commit style); merges show what they fuse.
  const parents = (node.parent_ids || []).map(p => state.nodes[p]).filter(Boolean)
  const isMerge = parents.length > 1
  const chg = node.idea?.change_summary || changeLabel(node, state.nodes)   // '' for merges (handled below)
  const mergeThemes = isMerge ? parents.map(p => p.idea?.theme || ('#' + p.id)) : null
  const dim = themeFilter && node.idea?.theme !== themeFilter
  return (
    <div className={nodeClass(node, state, workId) + (node.id === selectedId ? ' sel' : '') + (sweep ? ' sweep' : '') + (dim ? ' dim' : '')}
         onClick={() => onSelect(node.id)} title={op.label + (node.idea?.rationale ? ' — ' + node.idea.rationale : '')}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="row">
        <span className="nid">#{node.id}</span>
        <span className="op"><OpIcon name={op.icon} /> {node.operator}</span>
        <span className="spacer" style={{ flex: 1 }} />
        {sweep && <span className="badge sweep" title={`intra-node sweep · ${sw.count} trials`}>⊞ {sw.count}</span>}
        {node.id === state.best_node_id && <span className="crown" title="champion">♚</span>}
        {agentBadge(node.agent_report)}
        <span className="drag-h" draggable title="drag into the chat as context"
          onClick={(e) => e.stopPropagation()}
          onDragStart={(e) => { e.stopPropagation(); e.dataTransfer.setData('application/looplab-node', String(node.id)); e.dataTransfer.setData('text/plain', '#' + node.id); e.dataTransfer.effectAllowed = 'copy' }}>⠿</span>
      </div>
      <div className="metric">
        {fmt(m)}
        {d && <span className={'delta ' + (d.improved ? 'up' : 'down')}>{d.improved ? '▲' : '▼'}{fmt(Math.abs(d.d), 2)}</span>}
      </div>
      {isMerge
        ? <div className="merge-line" title={'combines: ' + mergeThemes.join(' + ')}>⊕ {mergeThemes.join(' + ')}</div>
        : chg ? <div className="change-chip" title={chg}>{chg}</div> : null}
      {sweep
        ? <div className="sub sweep-foot">
            <Spark series={sw.series} width={104} height={16} />
            <span className="spacer" style={{ flex: 1 }} />
            {sw.failed ? <span className="dot fail" title={`${sw.failed} failed trials`}>●{sw.failed}</span> : null}
            <button className="btn-chev" title={exploded ? 'collapse trials' : 'explode into trials'}
                    onClick={(e) => { e.stopPropagation(); onExplode && onExplode(node.id) }}>{exploded ? '⊟' : '⊞'}</button>
          </div>
        : <div className="sub">
            {node.status === 'failed'
              ? <span className="badge reason">{node.error_reason || 'failed'}</span>
              : node.confirmed_mean != null
                ? <>robust {fmt(node.confirmed_mean, 3)} ±{fmt(node.confirmed_std, 2)} ({node.confirmed_seeds}×)</>
                : node.feasible === false ? <span className="badge reason">infeasible</span>
                  : node.status}
          </div>}
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

// One trial of an exploded sweep — a tiny leaf card. Not a real DAG node (no backend id): it is a
// synthetic node placed under its sweep node's hull (see the explosion pass in Dag's useMemo).
function TrialNode({ data }) {
  const { trial, idx, isBest, selected, more, loading, onSelect, nodeId } = data
  const failed = !more && !loading && trial.metric == null
  const title = more ? 'more trials — open the Trials tab'
    : loading ? 'loading trials…'
    : Object.entries(trial.params || {}).map(([k, v]) => `${k}=${v}`).join(', ') + ` → ${fmt(trial.metric)}`
  return (
    <div className={'trial-card' + (failed ? ' fail' : '') + (selected ? ' sel' : '') + (more ? ' more' : '') + (isBest ? ' best' : '')}
         onClick={(e) => { e.stopPropagation(); onSelect && onSelect(nodeId, more ? null : idx) }} title={title}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="trial-top"><span className="trial-idx">{more ? idx : `#${idx}`}</span>{isBest && <span className="crown" title="best trial">♚</span>}</div>
      <div className="trial-metric">{more ? 'more' : loading ? '…' : failed ? '✗' : fmt(trial.metric)}</div>
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

// Deep-Research memo node (Phase 2). NOT a search node (no metric/code) — rendered from
// state.research so the "go think hard" stage is a visible step in the tree. Click opens the
// Research panel at this memo (conclusion + sources + reasoning-debug).
function ResearchNode({ data }) {
  const { memo, idx, selected, onSelect } = data
  const dirs = memo.recommended_directions || []
  return (
    <div className={'research-node' + (selected ? ' sel' : '')} onClick={() => onSelect && onSelect(idx)}
         title={memo.summary || 'deep research'}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="row"><span className="rn-ic">🔬</span><b>deep research</b>
        <span className="spacer" style={{ flex: 1 }} />
        {memo.trigger && <span className="badge">{memo.trigger}</span>}</div>
      <div className="rn-summary">{(memo.summary || '(no summary)').slice(0, 110)}</div>
      <div className="rn-foot">
        {dirs.length ? <span className="badge">{dirs.length} direction{dirs.length === 1 ? '' : 's'}</span> : null}
        {memo.sources?.length ? <span className="badge">{memo.sources.length} src</span> : null}
      </div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

const nodeTypes = { exp: ExpNode, groupRegion: GroupRegion, groupSuper: GroupSuper, trial: TrialNode, research: ResearchNode }

export default function Dag({ state, selectedId, onSelect, groupMode = 'none', collapsed = new Set(),
                             onToggleGroup, onSetMode, onCollapseAll, onExpandAll, selectedGroup, onSelectGroup,
                             exploded = new Set(), onExplode, trialCache = {}, selectedTrial, onSelectTrial,
                             selectedResearch = null, onSelectResearch, themeFilter = null }) {
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

    // 1) region hulls for EXPANDED groups (drawn behind everything). A 1-member group needs no
    //    enclosing block — it'd just be a box hugging one node (pure noise, e.g. niche mode where
    //    most param combos are unique). The inter-cluster layout gap still sets singletons apart.
    groups.forEach((ids, key) => {
      if (collapsed.has(key) || ids.length < 2) return
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
        data: { node: n, state, workId, selectedId, onSelect, themeFilter,
                onExplode, exploded: exploded.has(n.id) } })
    })

    // 4) exploded sweep nodes → trial mini-nodes under a soft hull (semantic zoom, one level down).
    //    Trials are NOT backend DAG nodes, so they're placed in this parallel pass (NOT via
    //    layoutWithGroups, which keys entities `n:<int>`) and the sweep→trial edges are appended
    //    after the rerouted DAG edges. Big sweeps render the top-K trials + a "+N more" leaf.
    const trialEdges = []
    const TRIAL_W = 56, TRIAL_H = 42, TX = 72, TY = 58, CAP = 60
    Object.values(ns).forEach(n => {
      if (!exploded.has(n.id) || hidden.has(n.id)) return
      const base = pos[`n:${n.id}`]; if (!base) return
      const trials = trialCache[n.id]
      if (!trials) {   // detail not fetched yet → a single placeholder leaf
        rfNodes.push({ id: `t:${n.id}:loading`, type: 'trial', zIndex: 2, selectable: false, draggable: false,
          position: { x: base.x + NODE_W / 2 - TRIAL_W / 2, y: base.y + NODE_H + 56 },
          data: { loading: true, idx: '…' } })
        return
      }
      const better = (a, b) => state.direction === 'min' ? a < b : a > b
      let bestIdx = -1, bestV = null
      trials.forEach((t, i) => { if (t.metric != null && (bestV == null || better(t.metric, bestV))) { bestV = t.metric; bestIdx = i } })
      // cap: keep the best CAP trials (by metric under direction), summarize the rest in a "+N more"
      let shown = trials.map((t, i) => ({ t, i })), overflow = 0
      if (shown.length > CAP) {
        const sorted = [...shown].sort((a, b) => {
          if (a.t.metric == null) return 1; if (b.t.metric == null) return -1
          return state.direction === 'min' ? a.t.metric - b.t.metric : b.t.metric - a.t.metric
        })
        overflow = shown.length - CAP
        shown = sorted.slice(0, CAP)
      }
      const cells = shown.length + (overflow ? 1 : 0)
      const cols = Math.max(1, Math.ceil(Math.sqrt(cells)))
      const startX = base.x + NODE_W / 2 - (cols * TX) / 2
      const startY = base.y + NODE_H + 56
      const rects = []
      const place = (k) => ({ x: startX + (k % cols) * TX, y: startY + Math.floor(k / cols) * TY })
      shown.forEach(({ t, i }, k) => {
        const xy = place(k); rects.push({ x: xy.x, y: xy.y, w: TRIAL_W, h: TRIAL_H })
        rfNodes.push({ id: `t:${n.id}:${i}`, type: 'trial', position: xy, zIndex: 2, selectable: true, draggable: false,
          data: { trial: t, idx: i, nodeId: n.id, isBest: i === bestIdx, onSelect: onSelectTrial,
                  selected: !!(selectedTrial && selectedTrial.nodeId === n.id && selectedTrial.idx === i) } })
        trialEdges.push({ id: `n:${n.id}->t:${n.id}:${i}`, source: `n:${n.id}`, target: `t:${n.id}:${i}`, className: 'trial' })
      })
      if (overflow) {
        const xy = place(shown.length); rects.push({ x: xy.x, y: xy.y, w: TRIAL_W, h: TRIAL_H })
        rfNodes.push({ id: `t:${n.id}:more`, type: 'trial', position: xy, zIndex: 2, selectable: true, draggable: false,
          data: { more: true, idx: `+${overflow}`, nodeId: n.id, onSelect: onSelectTrial } })
      }
      if (rects.length) {
        const geo = regionGeometry(rects, 22)
        rfNodes.push({ id: `region:sweep:${n.id}`, type: 'groupRegion', position: { x: geo.x, y: geo.y }, zIndex: 0,
          selectable: false, draggable: false, focusable: false,
          data: { w: geo.w, h: geo.h, path: geo.path, label: `sweep #${n.id}`, count: trials.length,
                  tint: '#6f8bb0', onToggle: () => onExplode && onExplode(n.id) } })
      }
    })

    // 5) Deep-Research memo nodes (Phase 2): rendered from state.research as a distinct node type
    //    in a lane below the forest, each linked (dashed) to the most recent experiment it reviewed
    //    (largest node id ≤ at_node). Kept out of grouping/layout so the search DAG is untouched.
    const memos = state?.research || []
    if (memos.length) {
      let maxY = 0
      Object.values(pos).forEach(p => { if (p.y > maxY) maxY = p.y })
      const RY = maxY + NODE_H + 70
      const ids = Object.keys(ns).map(Number)
      memos.forEach((mm, i) => {
        const cap = (mm && mm.at_node != null) ? mm.at_node : Infinity
        const cands = ids.filter(id => id <= cap)
        const anchorId = cands.length ? Math.max(...cands) : null
        const ax = anchorId != null ? pos[`n:${anchorId}`]?.x : null
        const x = (ax != null ? ax : 0) + i * 36
        rfNodes.push({ id: `research:${i}`, type: 'research', position: { x, y: RY }, zIndex: 1,
          selectable: true, draggable: false,
          data: { memo: mm, idx: i, selected: i === selectedResearch, onSelect: onSelectResearch } })
        if (anchorId != null && pos[`n:${anchorId}`]) {
          trialEdges.push({ id: `n:${anchorId}->research:${i}`, source: `n:${anchorId}`,
                            target: `research:${i}`, className: 'research-edge', animated: false })
        }
      })
    }

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
    return { nodes: rfNodes, edges: rfEdges.concat(trialEdges), groupKeys: [...groups.keys()] }
  }, [state, selectedId, workId, onSelect, groupMode, collapsed, selectedGroup, onToggleGroup, onSelectGroup,
      exploded, onExplode, trialCache, selectedTrial, onSelectTrial, selectedResearch, onSelectResearch, themeFilter])

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
