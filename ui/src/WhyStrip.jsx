import { uiText, uiMessage, useUILanguage } from './uiLanguage.js'
import React, { useId, useState } from 'react'
import { OpIcon } from './icons.jsx'
import { policyPlainLine, strategyPlainLine } from './whyStripModel.js'

// Compact, always-visible narration of the loop's latest autonomous decisions. Keeping this pure
// projection outside panels.jsx means the core run canvas does not download every optional panel.
export default function WhyStrip({ state, onSelect }) {
  useUILanguage()

  const detailBaseId = useId()
  const [expandedKey, setExpandedKey] = useState(null)
  const items = []
  const strategies = state.strategy_history || []
  const strat = strategies[strategies.length - 1]
  if (strat && (strat.strategy?.rationale || strat.strategy?.policy)) {
    items.push({
      icon: 'compass',
      label: 'strategy',
      text: (strat.strategy.rationale || uiMessage("policy -> {0}", [strat.strategy.policy])),
      // Shown; `text` (the decision's own words) stays whole in the title and the disclosure.
      short: strategyPlainLine(strat.strategy) && uiText(strategyPlainLine(strat.strategy)),
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
      icon: 'bolt', label: label, text: decision.rationale || '', at: decision.at_node,
    })
  }
  if (state.policy_reason) {
    items.push({
      icon: 'target',
      label: 'next',
      node: state.policy_chosen,
      text: `${state.policy_reason}${state.policy_chosen != null ? ` -> #${state.policy_chosen}` : ''}`,
      short: uiMessage(...policyPlainLine(state.policy_chosen)),
    })
  }
  if (!items.length) return null
  return <div className="why-strip" role="region" aria-label={uiText("Why the loop is doing what it is doing live")}>
    {items.slice(0, 3).map((item, index) => {
      const Item = item.node != null ? 'button' : 'span'
      const itemKey = `${index}:${item.label}:${item.node ?? ''}:${item.at ?? ''}`
      const expanded = expandedKey === itemKey
      const detailId = `${detailBaseId}-${index}`
      return <div key={itemKey} className={'why-entry' + (expanded ? ' open' : '')}>
        <div className="why-item-row">
          <Item type={item.node != null ? 'button' : undefined}
            className={'why-item' + (item.node != null ? ' disclosure-button' : '')}
            title={item.text || undefined}
            onClick={item.node != null ? () => onSelect?.(item.node) : undefined}>
            <OpIcon name={item.icon} size={12} className="why-ic" />
            <b>{uiText(item.label)}</b> {item.short || item.text}
            {item.at != null ? <span className="muted"> @{item.at}</span> : null}
          </Item>
          {item.text && <button type="button" className="why-disclosure disclosure-button"
            aria-expanded={expanded} aria-controls={detailId}
            aria-label={uiMessage("{0} full rationale for {1}", [expanded ? 'Hide' : 'Show', item.label])}
            onClick={() => setExpandedKey(current => current === itemKey ? null : itemKey)}>
            <OpIcon name={expanded ? 'chevron-up' : 'chevron-down'} size={12} />
          </button>}
        </div>
        {item.text && <div id={detailId} className="why-detail" role="region" hidden={!expanded}
          aria-label={uiMessage("Full rationale for {0}", [item.label])}>
          <b>{uiText(item.label)}</b> {item.text}
          {item.at != null ? <span className="muted"> @{item.at}</span> : null}
        </div>}
      </div>
    })}
  </div>
}
