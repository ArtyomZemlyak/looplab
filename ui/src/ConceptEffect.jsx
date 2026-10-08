import React, { useId } from 'react'
import { uiText, uiMessage, uiPlural, useUILanguage } from './uiLanguage.js'
import { fmt } from './format.js'
import { conceptEffectValue, validConceptEffect } from './conceptEffect.js'

const REASONS = {
  no_without_concept: 'No evaluated control without this concept.',
  no_with_concept: 'No eligible evaluated experiment with this concept.',
  no_matching_context_or_phase: 'Other concepts or evaluation phases differ.',
  comparability_unknown_or_different: 'Evaluation conditions are unknown or incompatible.',
  frame_incomplete: 'The concept frame is incomplete; contribution is unknown.',
  membership_source_incomplete: 'Concept membership is incomplete; contribution is unknown.',
  analysis_limit: 'The comparison budget was reached; contribution is unknown.',
  mixed_evaluation_conditions: 'Matched pairs use different evaluation conditions; their effects cannot be pooled.',
}
export default function ConceptEffect({ effect }) {
  useUILanguage()
  const panelId = useId()
  const value = conceptEffectValue(effect)
  if (value === null) return <span className="muted" title={uiText(
    validConceptEffect(effect) && REASONS[effect.reason]
      || 'No compatible with/without comparison. Contribution is unknown.')}>{uiText('unknown')}</span>
  return <>
    <button type="button" className="btn xs" popovertarget={panelId}
      title={uiText('Matched with/without concept contrast. Positive means better.')}>
      <span style={{ color: value > 0 ? 'var(--ok)' : value < 0 ? 'var(--fail)' : 'inherit' }}>{value > 0 ? '+' : ''}{fmt(value)}</span>
      {' · '}{uiMessage('{0} pairs', [effect.n_pairs])}
      {effect.has_advisory_warnings && ' ⚠'}
    </button>
    <div id={panelId} popover="auto" role="dialog" aria-label={uiText('Concept contribution')}
      style={{ width: 440, maxWidth: 'calc(100vw - 40px)', maxHeight: '80vh', overflowY: 'auto',
        padding: 20, border: '1px solid var(--line)', borderRadius: 12,
        background: 'var(--bg)', color: 'var(--fg)', fontSize: 14, lineHeight: 1.6,
        whiteSpace: 'normal', textAlign: 'left' }}>
      <strong>{uiText('Concept contribution · with / without')}</strong>
      {effect.has_advisory_warnings && <p role="note">{uiText('Some paired measurements have Trust warnings. Review them before relying on this estimate.')}</p>}
      <p>{uiMessage('With concept: {0}; without: {1}; matched contexts: {2}.',
        [effect.n_with, effect.n_without, effect.n_contexts])}</p>
      <p>{uiMessage('Pair range: {0} to {1}. Better / worse / unchanged: {2} / {3} / {4}.',
        [fmt(effect.low), fmt(effect.high), effect.positive, effect.negative, effect.neutral])}</p>
      <p>{uiText('Median of context means. Same remaining concepts and compatible evaluation conditions. Observational evidence, not a proven ablation; code and parameters may also differ.')}</p>
      {effect.n_pairs === 1 && <p>{uiText('Only one comparison; repeat a controlled ablation before drawing a conclusion.')}</p>}
      <ul>{effect.pairs.map(pair => <li key={`${pair.with_node}:${pair.with_attempt}:${pair.without_node}:${pair.without_attempt}`}>
        {uiMessage('With #{0} (attempt {1}) / without #{2} (attempt {3}): {4}',
          [pair.with_node, pair.with_attempt, pair.without_node, pair.without_attempt, fmt(pair.delta)])}
      </li>)}</ul>
      {effect.pairs_omitted > 0 && <p>{uiPlural(effect.pairs_omitted, '{0} additional pairs included in the estimate.', '{0} additional pairs included in the estimate.')}</p>}
      <button type="button" className="btn sm" popovertarget={panelId} popovertargetaction="hide">{uiText('Close')}</button>
    </div>
  </>
}
