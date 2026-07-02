import React, { useContext, useEffect, useMemo, useState } from 'react'
import { ReactFlow, Background, Controls, MiniMap, Handle, Position, Panel, useViewport } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { fmt, layoutWithGroups, nodeClass, delta, workingId, operatorMeta, OPERATOR_LEGEND, isSweep, sweepInfo, chipFontSize } from './util.js'
import { nodeChip } from './report.js'
import { OpIcon } from './icons.jsx'
import { Spark } from './charts.jsx'
import { GroupRegion, SuperShell } from './groupnodes.jsx'
import EnergyEdge from './EnergyEdge.jsx'
import { useFx } from './fx.js'
import {
  computeGroups, nodeGroupMap, regionGeometry, rerouteForCollapse, groupColor,
  groupAggregate, superId, GROUP_MODES, isMergeEntryEdge,
} from './grouping.js'

const NODE_W = 188, NODE_H = 78

// Zoom level-of-detail: a single watcher (rendered INSIDE <ReactFlow> so it can read the live viewport)
// flips a context boolean when zoom crosses a dead-band; every ExpNode reads it via context. So changing
// zoom never re-runs the layout memo (its deps don't include zoom) — it just swaps full-card ↔ glyph.
const LodContext = React.createContext(false)
const LOD_ON = 0.42, LOD_OFF = 0.5   // hysteresis: a dead-band so sitting near the threshold can't flicker
function LodWatcher({ lod, onChange }) {
  const { zoom } = useViewport()
  useEffect(() => {
    if (!lod && zoom < LOD_ON) onChange(true)
    else if (lod && zoom > LOD_OFF) onChange(false)
  }, [zoom, lod, onChange])
  return null
}

// Exception-only + monochrome: the common success case gets NO badge (the card's colour budget is
// reserved for experiment STATUS, so a green ✓agent can't sit next to a red failure meaning two
// different things). Only a fallback or a failed validation shows one faint glyph.
function agentBadge(rep) {
  if (!rep) return null
  if (rep.fell_back) return <span className="badge agent-note" title="developer fell back to a simpler build">↩</span>
  if (rep.ok === false) return <span className="badge agent-note" title="agent validation failed">✗</span>
  return null
}

// Champion lightning (Reactor/Energy "full" only): jagged arcs crackling around the best node's card.
// Always rendered for the best node; CSS hides it unless [data-fx="full"]. There's exactly ONE champion,
// so the extra SVG is negligible. Coords are in the card's 188×78 space; bolts spill outside via overflow.
const BOLTS = [
  'M22 4 L12 -10 L24 -18 L8 -34',          // top-left strike
  'M166 4 L180 -10 L166 -20 L184 -34',     // top-right strike
  'M4 28 L-14 24 L-4 40 L-20 50',          // left arc
  'M184 30 L202 26 L190 42 L208 52',       // right arc
  'M76 1 L84 -16 L98 -7 L106 -24 L114 -9', // top-centre crown
  'M70 78 L62 92 L80 88 L70 104',          // bottom discharge
]
function Bolts() {
  return (
    <svg className="ll-bolts" viewBox="0 0 188 78" preserveAspectRatio="none" aria-hidden="true">
      {BOLTS.map((d, i) => (
        <path key={i} d={d} className={i % 3 === 1 ? 'b-accent' : ''} style={{ animationDelay: `${(i * 0.17).toFixed(2)}s` }} />
      ))}
    </svg>
  )
}

function ExpNode({ data }) {
  const { node, state, workId, selectedId, onSelect, themeFilter, groupTint } = data
  const lod = useContext(LodContext)   // overview zoom → render the compact glyph instead of the full card
  const m = node.confirmed_mean ?? node.metric
  const d = delta(node, state)
  const op = operatorMeta(node.operator)
  const sweep = isSweep(node)
  const sw = sweep ? sweepInfo(node) : null
  // A one-line "what this node did" chip (git-commit style): a sweep says what was searched, a draft
  // says baseline, everything else diffs vs parent. nodeChip owns those rules (merges handled below).
  const parents = (node.parent_ids || []).map(p => state.nodes[p]).filter(Boolean)
  const isMerge = parents.length > 1
  const chg = nodeChip(node, state.nodes)   // returns '' for a (resolved) merge — render guards isMerge
  const mergeThemes = isMerge ? parents.map(p => p.idea?.theme || ('#' + p.id)) : null
  const dim = data.dim   // precomputed in the canvas memo: lineage-focus dimming OR the theme filter
  const confirmed = node.confirmed_mean != null
  const cardCls = nodeClass(node, state, workId) + (node.id === selectedId ? ' sel' : '') + (sweep ? ' sweep' : '') + (groupTint ? ' grouped' : '') + (dim ? ' dim' : '')
  // FX "heat" (0..1) for the reactor-core glow — only read under [data-fx]; harmless when FX is off.
  // Champion brightest, then an improving node, then plain evaluated; failed/pending stay dim.
  const coreI = node.id === state.best_node_id ? 1 : (d && d.improved) ? 0.85
    : node.status === 'evaluated' ? 0.55 : node.status === 'failed' ? 0.45 : 0.3
  const cardStyle = { '--core': coreI, ...(groupTint ? { '--grp-tint': groupTint } : {}) }
  const cardTitle = op.label + (node.idea?.rationale ? ' — ' + node.idea.rationale : '')
  // Zoom LOD: below the threshold the full card is sub-pixel mush AND expensive (4 text rows + a Spark
  // SVG). Collapse to a glyph — the status-coloured body does the talking, plus the operator icon + id,
  // so the forest reads as a field of green/red blocks at overview (Blender/ELK level-of-detail pattern).
  if (lod) return (
    <div className={cardCls + ' lod'} style={cardStyle} onClick={() => onSelect(node.id)} title={cardTitle}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      {node.id === state.best_node_id && <><span className="ll-ring" aria-hidden="true" /><Bolts /></>}
      <span className="lod-ic"><OpIcon name={op.icon} size={22} /></span>
      <span className="lod-id">#{node.id}</span>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
  return (
    <div className={cardCls} style={cardStyle}
         onClick={() => onSelect(node.id)} title={cardTitle}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      {node.id === state.best_node_id && <><span className="ll-ring" aria-hidden="true" /><Bolts /></>}
      <div className="row">
        <span className="nid">#{node.id}</span>
        {/* operator = icon only (the word is in the title + legend); kept monochrome so colour stays status */}
        <span className="op" title={node.operator}><OpIcon name={op.icon} /></span>
        <span className="spacer" style={{ flex: 1 }} />
        {sweep && <span className="badge sweep" title={`intra-node sweep · ${sw.count} trials — open the node's Trials tab`}>⊞ {sw.count}</span>}
        {agentBadge(node.agent_report)}
      </div>
      <div className="metric">
        {fmt(m)}
        {/* delta only where it's meaningful — a merge has several parents, so a single ▲/▼ vs parent[0] would lie */}
        {!isMerge && d && <span className={'delta ' + (d.improved ? 'up' : 'down')}>{d.improved ? '▲' : '▼'}{fmt(Math.abs(d.d), 2)}</span>}
        {/* confirmed = a compact tick, not a restated 'robust …' line; the full ±std lives in the Inspector */}
        {confirmed && <span className="conf-chip" title={`robust ${fmt(node.confirmed_mean, 3)} ±${fmt(node.confirmed_std, 2)} over ${node.confirmed_seeds} seeds`}>✓{node.confirmed_seeds}×</span>}
      </div>
      {isMerge
        ? (() => { const ml = '⊕ ' + mergeThemes.join(' + ')
            return <div className="merge-line" style={{ fontSize: chipFontSize(ml) + 'px' }} title={'combines: ' + mergeThemes.join(' + ')}>{ml}</div> })()
        : chg ? <div className="change-chip" style={{ fontSize: chipFontSize(chg) + 'px' }} title={chg}>{chg}</div> : null}
      {/* cross-run provenance: this experiment was seeded from a sibling run — deep-link to it */}
      {node.origin?.run_id && <a className="origin-chip" href={`#/run/${node.origin.run_id}`}
        onClick={(e) => e.stopPropagation()}
        title={`seeded from run ${node.origin.run_id} #${node.origin.node_id}`
          + (node.origin.metric != null ? ` · source metric ${fmt(node.origin.metric)}` : '')
          + ' — click to open that run'}>⤴ from {node.origin.run_id} #{node.origin.node_id}</a>}
      {/* deep-research provenance: this experiment was proposed right after a research memo (its
          directions were the active steering) — the 💡 marks where research landed in the tree */}
      {node.research_origin && <span className="origin-chip rsch"
        title={`proposed just after deep research (${node.research_origin.trigger || 'auto'}) at node ${node.research_origin.at_node} — its directions were steering`}><OpIcon name="bulb" size={11} /> from research</span>}
      {/* one optional sub-line, reserved for the ONE thing shown nowhere else: why it failed / infeasible */}
      {sweep
        ? <div className="sub sweep-foot">
            <Spark series={sw.series} width={104} height={16} />
            <span className="spacer" style={{ flex: 1 }} />
            {sw.failed ? <span className="dot fail" title={`${sw.failed} failed trials`}>●{sw.failed}</span> : null}
          </div>
        : node.status === 'failed'
          ? <div className="sub"><span className="badge reason">{node.error_reason || 'failed'}</span></div>
          : node.feasible === false ? <div className="sub"><span className="badge reason">infeasible</span></div> : null}
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

// Group region behind an EXPANDED cluster (round-8): a faint band + a compact label pill (replaces
// the round-6 full-width lane bar that stretched across the cluster). Click the pill to collapse.
function GroupLane({ data }) {
  const { w, h, label, count, tint, onToggle } = data
  return <GroupRegion w={w} h={h} label={label} count={count} tint={tint} onToggle={onToggle} />
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

// Deep research is NOT a search node (no metric/code/lineage), so it no longer lives in the DAG. Its
// memos are surfaced where they belong: a timeline marker in the Dock feed (research_completed) and the
// interactive Research drawer (panels.jsx). (Per-node "spawned from research direction" provenance
// chips are a planned follow-up — they need the Researcher to attribute which direction it used.)

const nodeTypes = { exp: ExpNode, groupLane: GroupLane, groupSuper: GroupSuper }
const edgeTypes = { energy: EnergyEdge }   // used only in Reactor/Energy FX mode (edge.type === 'energy')

export default function Dag({ state, selectedId, onSelect, groupMode = 'none', collapsed = new Set(),
                             onToggleGroup, onSetMode, onCollapseAll, onExpandAll, onAutoCollapse, selectedGroup, onSelectGroup,
                             themeFilter = null, onNodeAction, mergeArm = null }) {
  const workId = workingId(state)
  const [menu, setMenu] = useState(null)   // U3: right-click node menu {x,y,nodeId}
  const act = (action) => { const id = menu?.nodeId; setMenu(null); if (id != null && onNodeAction) onNodeAction(action, id) }
  const fx = useFx()   // '' | 'subtle' | 'full' — Reactor/Energy FX: swaps edge rendering + the backdrop
  const [showMap, setShowMap] = useState(() => localStorage.getItem('ll.minimap') === '1')
  const [showLegend, setShowLegend] = useState(false)
  const [lod, setLod] = useState(false)   // zoom level-of-detail: full cards ↔ glyphs (set by LodWatcher)
  const toggleMap = () => setShowMap(v => { localStorage.setItem('ll.minimap', v ? '0' : '1'); return !v })

  const { nodes, edges, groupKeys } = useMemo(() => {
    const ns = state?.nodes || {}
    const groups = groupMode === 'none' ? new Map() : computeGroups(ns, groupMode)
    // Group ORDER → a curated tint per group, so adjacent groups stay visually distinct (vs a hash
    // that can collide two neighbours). One muted hue per group is the primary "same family" cue.
    const groupOrder = new Map([...groups.keys()].map((k, i) => [k, i]))
    const tintOf = (k) => groupColor(k, groupOrder.get(k))
    const ng = nodeGroupMap(groups)
    const banded = groupMode === 'theme' || groupMode === 'niche'
    const { pos, cells } = layoutWithGroups(ns, { collapsed, nodeGroup: ng, groupMode })
    const { hidden, edges: reEdges } = rerouteForCollapse(ns, collapsed, ng)

    // Focus+context (Prefect-style): the transitive ancestor+descendant set of the SELECTED node — the
    // rest of the forest dims so "how did we get to this experiment / where did it lead" reads on a
    // deep tree. Plus the champion's ancestor chain → a persistent faint-gold "winning spine" that
    // shows the root→best path even with nothing selected. Pure graph walks over parent_ids/children.
    const children = {}
    Object.values(ns).forEach(n => (n.parent_ids || []).forEach(p => { (children[p] ||= []).push(n.id) }))
    const reach = (start, nextOf) => {
      const seen = new Set(); const stack = [start]
      while (stack.length) { const x = stack.pop(); (nextOf(x) || []).forEach(y => { if (!seen.has(y)) { seen.add(y); stack.push(y) } }) }
      return seen
    }
    const focusSet = (selectedId != null && ns[selectedId])
      ? new Set([selectedId, ...reach(selectedId, x => ns[x]?.parent_ids), ...reach(selectedId, x => children[x])])
      : null
    const champSet = (state.best_node_id != null && ns[state.best_node_id])
      ? new Set([state.best_node_id, ...reach(state.best_node_id, x => ns[x]?.parent_ids)])
      : null

    const rfNodes = []

    // 1) a region behind each EXPANDED group CELL. A layered mode draws one cell per group; the banded
    //    grid-pack (theme/niche) draws one per (band, group) — each a tight compact block instead of a
    //    tall stripe. A 1-member cell needs no header — it'd just label a single node. regionGeometry
    //    gives the cell's bounding rect; the label pill sits in the 26px pad gap above the top row.
    cells.forEach(cell => {
      if (cell.ids.length < 2) return
      const rects = cell.ids.map(id => pos[`n:${id}`]).filter(Boolean).map(p => ({ x: p.x, y: p.y, w: NODE_W, h: NODE_H }))
      if (!rects.length) return
      const geo = regionGeometry(rects)
      rfNodes.push({
        id: `region:${cell.band ?? 'g'}:${cell.key}`, type: 'groupLane', position: { x: geo.x, y: geo.y }, zIndex: 0,
        selectable: false, draggable: false, focusable: false,
        data: { w: geo.w, h: geo.h, label: cell.key, count: cell.ids.length, tint: tintOf(cell.key), onToggle: onToggleGroup },
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
          tint: tintOf(key), selected: key === selectedGroup, onExpand: onToggleGroup, onSelect: onSelectGroup,
        },
      })
    })

    // 3) visible experiment nodes. A grouped node carries its group's tint so the card shows a faint
    //    top accent + wash (membership cue) without an enclosing box — but ONLY for nodes that sit in a
    //    CELL that actually drew a region (≥2 members), so a singleton never gets an unexplained tint.
    //    Keying off the drawn cells (not whole-group size) keeps tint⇔region in sync in BOTH the layered
    //    layout (one cell per group) and the banded pack (a group split across bands can have a lone-
    //    member cell that draws no box — its node must then stay untinted).
    const tintedIds = new Set()
    cells.forEach(cell => { if (cell.ids.length >= 2) cell.ids.forEach(id => tintedIds.add(id)) })
    Object.values(ns).forEach(n => {
      if (hidden.has(n.id)) return
      const p = pos[`n:${n.id}`]; if (!p) return
      const gkey = ng.get(n.id)
      const inLane = tintedIds.has(n.id)
      // dim a node that's outside the selected lineage, or off the active theme filter
      const dimmed = (focusSet ? !focusSet.has(n.id) : false) || (themeFilter && n.idea?.theme !== themeFilter)
      rfNodes.push({ id: `n:${n.id}`, type: 'exp', position: p, zIndex: 1, width: NODE_W, height: NODE_H,
        data: { node: n, state, workId, selectedId, onSelect, themeFilter, dim: dimmed,
                groupTint: inLane ? tintOf(gkey) : null } })
    })

    // (Deep-research memos are no longer drawn in the DAG — they live in the Dock timeline + the
    //  Research drawer; nodes spawned from a research direction carry a 💡 origin chip instead.)

    // edges (already rerouted around collapsed groups)
    const rfEdges = reEdges.map(e => {
      const child = ns[e.dstId]
      // debug semantics attach to the child (the repair node). Keep the styling as long as the
      // child is visible — even if the parent was collapsed and the edge now starts at a super-node.
      const isDebug = child && child.operator === 'debug' && !hidden.has(e.dstId)
      // Phase 2: an edge into a MERGE (≥2 parents) crosses between packed cells. In a banded layout,
      // route it orthogonally (a "leader" edge) so it hugs the band gaps instead of slicing a bezier
      // straight through a compact cell. Only in banded modes — layered modes keep today's beziers.
      const isLeader = banded && isMergeEntryEdge(child) && !hidden.has(e.dstId)
      const onLineage = focusSet && focusSet.has(e.srcId) && focusSet.has(e.dstId)   // selected node's path
      const onChamp = champSet && champSet.has(e.srcId) && champSet.has(e.dstId)      // root→best spine
      const cls = []
      if (isDebug) cls.push('debug')
      if (isLeader) cls.push('leader')
      if (onLineage) cls.push('lineage')                 // accent: the path through the selected node
      else if (onChamp) cls.push('champion')             // faint gold: the winning lineage (persistent)
      const faded = focusSet && !onLineage && !onChamp
      if (faded) cls.push('faded')   // everything off-path recedes
      const charging = e.dstId === workId && !hidden.has(e.dstId)   // this edge feeds the working node
      const flow = onLineage ? 'lineage' : onChamp ? 'champion' : null
      if (charging && !onLineage && !onChamp) cls.push('charge')   // plasma feed into the working node
      return {
        id: `${e.source}->${e.target}`, source: e.source, target: e.target,
        // FX mode routes every edge through EnergyEdge (it draws the matching path shape itself);
        // otherwise keep today's behaviour (smoothstep only for the merge "leader" bridge).
        type: fx ? 'energy' : (isLeader ? 'smoothstep' : undefined),
        className: cls.join(' '),
        // the built-in dashed flow only OUTSIDE FX mode — in FX mode the streaming particle is the cue
        animated: charging && !fx,
        data: { leader: isLeader, charging, flow, faded: !!faded, level: fx },
      }
    })
    return { nodes: rfNodes, edges: rfEdges, groupKeys: [...groups.keys()] }
  }, [state, selectedId, workId, onSelect, groupMode, collapsed, selectedGroup, onToggleGroup, onSelectGroup,
      themeFilter, fx])

  return (
    <LodContext.Provider value={lod}>
    <div className="dag-wrap">
    {/* Reactor/Energy backdrop + shared SVG defs (the edge gradient + the neon glow filter), mounted
        only while FX is on so there's zero cost otherwise. The defs ids are referenced from CSS. */}
    {fx && <div className="reactor-bg" aria-hidden="true" />}
    {fx && <svg className="ll-fx-defs" width="0" height="0" aria-hidden="true"><defs>
      <linearGradient id="ll-energy-grad" x1="0" y1="0" x2="1" y2="1">
        <stop className="g0" offset="0%" /><stop className="g1" offset="100%" />
      </linearGradient>
      {/* neon bloom for the streaming packets + the champion's lightning */}
      <filter id="ll-energy-glow" x="-60%" y="-60%" width="220%" height="220%">
        <feGaussianBlur stdDeviation="2.2" result="b" />
        <feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
      </filter>
      {/* "barely jittering" electric arc — animated fractal turbulence displaces the path a pixel or two,
          then a bloom is merged over it. Applied (full only) to the few SEMANTIC edge paths, so the
          per-frame filter recompute stays bounded to the spine + lineage + charge feed, not every edge. */}
      <filter id="ll-arc" x="-80%" y="-80%" width="260%" height="260%">
        <feTurbulence type="fractalNoise" baseFrequency="0.018" numOctaves="1" seed="7" result="noise">
          <animate attributeName="baseFrequency" dur="0.55s" values="0.012;0.03;0.012" repeatCount="indefinite" />
        </feTurbulence>
        <feDisplacementMap in="SourceGraphic" in2="noise" scale="2.4"
                           xChannelSelector="R" yChannelSelector="G" result="disp" />
        <feGaussianBlur in="disp" stdDeviation="2.4" result="b" />
        <feMerge><feMergeNode in="b" /><feMergeNode in="disp" /></feMerge>
      </filter>
    </defs></svg>}
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes} fitView fitViewOptions={{ padding: 0.12 }}
               minZoom={0.05} maxZoom={1.8}
               onlyRenderVisibleElements proOptions={{ hideAttribution: true }}
               nodesDraggable={!!onNodeAction}
               onNodeContextMenu={(e, rf) => {
                 const id = rf?.data?.node?.id
                 if (id == null || !onNodeAction) return
                 e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY, nodeId: id })
               }}
               onNodeDragStop={(e, rf) => {
                 // U3 drag-to-merge: dropped a node near another -> merge the two. Manual intersection
                 // over the laid-out positions (nodes are otherwise controlled, so the node snaps back).
                 const from = rf?.data?.node?.id
                 if (from == null || !onNodeAction) return
                 const p = rf.position || {}
                 let hit = null, bestD = 9999
                 for (const n of nodes) {
                   if (n.type !== 'exp' || n.id === rf.id) continue
                   const dx = (n.position.x - p.x), dy = (n.position.y - p.y)
                   const dd = Math.hypot(dx, dy)
                   if (dd < 90 && dd < bestD) { bestD = dd; hit = n.data?.node?.id }
                 }
                 if (hit != null) onNodeAction('merge', { from, to: hit })
               }}
               onPaneClick={() => { setMenu(null); onSelect(null); onSelectGroup && onSelectGroup(null) }}>
      <LodWatcher lod={lod} onChange={setLod} />
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
          {(groupMode === 'theme' || groupMode === 'niche') && onAutoCollapse &&
            <button className="btn sm ghost" title="auto-collapse settled groups (keeps the champion, selected, and active groups open)"
                    onClick={() => onAutoCollapse()}>⊟ settled</button>}
        </>}
      </Panel>
      {/* lift the toggles above the overview map when it's open — otherwise the minimap (also
          bottom-right) covers this row and you can't click 🗺 again to hide it. */}
      <Panel position="bottom-right" className="map-toggles" style={{ marginBottom: showMap ? 152 : 0 }}>
        <button className={'btn sm ghost' + (showLegend ? ' primary' : '')} title="operator legend"
                onClick={() => setShowLegend(v => !v)}>ⓘ ops</button>
        <button className={'btn sm ghost' + (showMap ? ' primary' : '')} title={showMap ? 'hide overview map' : 'show overview map'}
                onClick={toggleMap}><OpIcon name="map" className="t-ic" /> map{showMap ? ' ✕' : ''}</button>
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
    {mergeArm != null && <div className="merge-arm-hint">click a node to merge with #{mergeArm} · Esc to cancel</div>}
    {menu && <>
      <div className="menu-backdrop" onClick={() => setMenu(null)} onContextMenu={(e) => { e.preventDefault(); setMenu(null) }} />
      <div className="node-menu" style={{ left: menu.x, top: menu.y }} onClick={e => e.stopPropagation()}>
        <div className="nm-h">experiment #{menu.nodeId}</div>
        <button className="nm-item" onClick={() => act('explore')}><OpIcon name="gitbranch" size={13} /> Explore from here</button>
        <button className="nm-item" onClick={() => act('merge')}><OpIcon name="confluence" size={13} /> Merge with…</button>
        <button className="nm-item" onClick={() => act('ablate')}><OpIcon name="target" size={13} /> Ablate</button>
        <button className="nm-item" onClick={() => act('diff')}><OpIcon name="doc" size={13} /> Diff vs champion</button>
        <button className="nm-item" onClick={() => act('inspect')}><OpIcon name="search" size={13} /> Inspect</button>
        <button className="nm-item danger" onClick={() => act('kill')}><OpIcon name="cross" size={13} /> Kill branch</button>
      </div>
    </>}
    </div>
    </LodContext.Provider>
  )
}
