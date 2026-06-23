import React, { useMemo } from 'react'
import { ReactFlow, Background, Controls, MiniMap, Handle, Position } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { fmt, layout, nodeClass, delta, workingId } from './util.js'

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
  return (
    <div className={nodeClass(node, state, workId) + (node.id === selectedId ? ' sel' : '')}
         onClick={() => onSelect(node.id)} title={node.idea?.rationale || ''}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="row">
        <span className="nid">#{node.id}</span>
        <span className="op">{node.operator}</span>
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
        {state.policy_scores && state.policy_scores[node.id] != null &&
          <span className="badge" title="policy UCB1 — why the search would expand here"
                style={node.id === state.policy_chosen ? { color: 'var(--accent)', borderColor: 'var(--accent)' } : {}}>
            ucb {fmt(state.policy_scores[node.id], 2)}</span>}
      </div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

const nodeTypes = { exp: ExpNode }

export default function Dag({ state, selectedId, onSelect }) {
  const workId = workingId(state)
  const { nodes, edges } = useMemo(() => {
    const ns = state?.nodes || {}
    const pos = layout(ns)
    const rfNodes = Object.values(ns).map(n => ({
      id: String(n.id), type: 'exp', position: pos[n.id] || { x: 0, y: 0 },
      data: { node: n, state, workId, selectedId, onSelect },
    }))
    const rfEdges = []
    Object.values(ns).forEach(n => (n.parent_ids || []).forEach(p => {
      if (!(p in ns)) return
      const onPath = n.id === selectedId || p === selectedId
      rfEdges.push({
        id: `${p}-${n.id}`, source: String(p), target: String(n.id),
        className: n.operator === 'debug' ? 'debug' : (onPath ? 'lineage' : ''),
        animated: n.id === workId,
      })
    }))
    return { nodes: rfNodes, edges: rfEdges }
  }, [state, selectedId, workId, onSelect])

  return (
    <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.2} maxZoom={1.8}
               proOptions={{ hideAttribution: true }} nodesDraggable={false} onPaneClick={() => onSelect(null)}>
      <Background color="#20252f" gap={22} />
      <Controls showInteractive={false} />
      <MiniMap pannable zoomable nodeColor={(n) => {
        const nd = n.data?.node; if (!nd) return '#444'
        if (nd.id === state.best_node_id) return '#ffd54a'
        if (nd.status === 'failed') return '#ef4444'
        if (nd.status === 'evaluated') return '#2ecc71'
        return '#6b7686'
      }} style={{ background: '#12151c' }} />
    </ReactFlow>
  )
}
