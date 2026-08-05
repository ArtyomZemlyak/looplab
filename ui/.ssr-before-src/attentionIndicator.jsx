import React, { useSyncExternalStore } from 'react'

import { OpIcon } from './icons.jsx'

const INACTIVE_ATTENTION_INDICATOR = Object.freeze({
  active: false,
  ariaLabel: 'Open attention center. Checking for updates.',
  badge: '',
  showBadge: false,
  tone: 'neutral',
  verified: false,
})

let publisher = null
let snapshot = INACTIVE_ATTENTION_INDICATOR
const listeners = new Set()

const cleanText = (value, fallback = '') => {
  const text = typeof value === 'string' ? value.trim() : ''
  return text || fallback
}

const equalIndicator = (left, right) => left.active === right.active
  && left.ariaLabel === right.ariaLabel
  && left.badge === right.badge
  && left.showBadge === right.showBadge
  && left.tone === right.tone
  && left.verified === right.verified

const setSnapshot = next => {
  if (equalIndicator(snapshot, next)) return snapshot
  snapshot = next
  for (const listener of [...listeners]) listener()
  return snapshot
}

export function getAttentionIndicatorSnapshot() {
  return snapshot
}

export function subscribeAttentionIndicator(listener) {
  if (typeof listener !== 'function') throw new TypeError('Attention indicator listener must be a function')
  listeners.add(listener)
  return () => listeners.delete(listener)
}

// Only the mounted AttentionCenter may publish. The owner token prevents a late StrictMode/HMR
// cleanup from clearing a newer center's projection.
export function claimAttentionIndicatorPublisher(owner) {
  if (!owner) throw new TypeError('Attention indicator publisher requires an owner token')
  publisher = owner
}

export function publishAttentionIndicator(owner, value = {}) {
  if (publisher !== owner) return snapshot
  const tone = value?.tone === 'action' || value?.tone === 'unread' ? value.tone : 'neutral'
  const badge = cleanText(value?.badge).slice(0, 8)
  return setSnapshot(Object.freeze({
    active: true,
    ariaLabel: cleanText(value?.ariaLabel, INACTIVE_ATTENTION_INDICATOR.ariaLabel),
    badge,
    showBadge: value?.showBadge === true && !!badge,
    tone,
    verified: value?.verified === true,
  }))
}

export function releaseAttentionIndicatorPublisher(owner) {
  if (publisher !== owner) return snapshot
  publisher = null
  return setSnapshot(INACTIVE_ATTENTION_INDICATOR)
}

export function useAttentionIndicator() {
  return useSyncExternalStore(
    subscribeAttentionIndicator, getAttentionIndicatorSnapshot, getAttentionIndicatorSnapshot,
  )
}

export function openAttentionCenter() {
  if (typeof window !== 'undefined' && typeof window.Event === 'function') {
    window.dispatchEvent(new window.Event('ll:open-attention'))
  }
}

export function AttentionLauncher({ indicator, embedded = false, expanded, controls, onClick }) {
  const toneClass = indicator.tone === 'action'
    ? ' has-action' : indicator.tone === 'unread' ? ' has-unread' : ''
  const className = `attention-trigger${embedded ? ' in-assistant' : ''}${toneClass}`
    + (indicator.verified ? '' : ' is-unverified')
  return <button type="button" className={className}
    aria-label={indicator.ariaLabel} aria-haspopup="dialog"
    aria-expanded={expanded} aria-controls={controls} onClick={onClick}>
    <OpIcon name="bell" size={22} className="attention-bell-icon" />
    {indicator.showBadge && <span
      className={`attention-badge ${indicator.tone === 'action' ? 'is-action' : 'is-unread'}`}
      aria-hidden="true">{indicator.badge}</span>}
  </button>
}
