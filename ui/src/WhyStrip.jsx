import React from 'react'
import { OpIcon } from './icons.jsx'

// Compact, always-visible narration of the loop's latest autonomous decisions. Keeping this pure
// projection outside panels.jsx means the core run canvas does not download every optional panel.
export default function WhyStrip({ state, onSelect }) {
  const items = []
  const strategies = state.strategy_history || []
  const strat = strategies[strategies.length - 1]
  if (strat && (strat.strategy?.rationale || strat.strategy?.policy)) {
    items.push({
      icon: 'compass',
      label: 'strategy',
      text: strat.strategy.rationale || `policy -> ${strat.strategy.policy}`,
      at: strat.at_node,
    })
  }
  const decisions = state.agent_decisions || []
  const decision = decisions[decisions.length - 1]
  if (decision && (decision.rationale || decision.chosen)) {
    const chosen = decision.chosen
    const label = chosen && typeof chosen === 'object'
      ? `${chosen.kind || 'action'}${chosen.node_id != null
        ? ` #${chosen.node_id}`
        : chosen.parent_id != null ? ` from #${chosen.parent_id}` : ''}`
      : (chosen || 'action')
    items.push({
      icon: 'bolt', label, text: decision.rationale || '', at: decision.at_node,
    })
  }
  if (state.policy_reason) {
    items.push({
      icon: 'target',
      label: 'policy',
      node: state.policy_chosen,
      text: `${state.policy_reason}${state.policy_chosen != null ? ` -> #${state.policy_chosen}` : ''}`,
    })
  }
  if (!items.length) return null
  return <div className="why-strip" title="why the loop is doing what it's doing (live)">
    {items.slice(0, 3).map((item, index) => {
      const Item = item.node != null ? 'button' : 'span'
      return <Item key={index} type={item.node != null ? 'button' : undefined}
        className={'why-item' + (item.node != null ? ' disclosure-button' : '')}
        onClick={item.node != null ? () => onSelect?.(item.node) : undefined}>
        <OpIcon name={item.icon} size={12} className="why-ic" />
        <b>{item.label}</b> {item.text}
        {item.at != null ? <span className="muted"> @{item.at}</span> : null}
      </Item>
    })}
  </div>
}
