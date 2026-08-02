import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'

import { attentionHref } from './attentionModel.js'
import {
  createAttentionChannel, deliverAttentionNotifications, disableAttentionNotifications,
  enableAttentionNotifications, mutateAttentionState, notificationCapability,
} from './attentionNotifications.js'
import {
  attentionIds, loadAttentionState, recordAttentionIds,
} from './attentionStorage.js'
import { OpIcon } from './icons.jsx'
import { useAttention } from './useAttention.js'
import { DIALOG_PRIORITY, useDialogFocus } from './useDialogFocus.js'
import './attention.css'

const dispatchOpenAttention = () => {
  if (typeof window !== 'undefined' && typeof window.Event === 'function') {
    window.dispatchEvent(new window.Event('ll:open-attention'))
  }
}

const ATTENTION_PREFERENCE_FAILURE = 'This browser could not verify the saved attention preference.'

function isPlainRunActivation(event) {
  if (!event || event.defaultPrevented || (event.button != null && event.button !== 0)
      || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false
  const target = String(event.currentTarget?.target || '').toLowerCase()
  return (!target || target === '_self') && !event.currentTarget?.hasAttribute?.('download')
}

function itemTime(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null
  const date = new Date(seconds * 1000)
  if (Number.isNaN(date.getTime())) return null
  let label
  try {
    label = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
  } catch { label = date.toLocaleString() }
  return { iso: date.toISOString(), label }
}

function snapshotAge(value, now = Date.now()) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric) || numeric <= 0) return ''
  const timestamp = numeric < 1_000_000_000_000 ? numeric * 1000 : numeric
  const elapsed = Math.max(0, now - timestamp)
  if (elapsed < 60_000) return 'just now'
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m ago`
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}h ago`
  return `${Math.floor(elapsed / 86_400_000)}d ago`
}

const itemWord = count => count === 1 ? 'item' : 'items'

function actionCountCopy(count, still = false) {
  if (count === 1) return `1 item${still ? ' still' : ''} needs action`
  return `${count} items${still ? ' still' : ''} need action`
}

function visualCount(count, incomplete = false) {
  if (count > 99) return '99+'
  return `${count}${incomplete ? '+' : ''}`
}

function capabilityCopy(capability, preferences) {
  if (!preferences.available) {
    return 'Desktop alerts are unavailable because this browser cannot safely persist notification state.'
  }
  if (capability === 'unsupported') return 'This browser does not support desktop notifications.'
  if (capability === 'denied') {
    return 'Desktop notifications are blocked in browser settings. In-app attention remains available.'
  }
  if (capability === 'locks-unavailable') {
    return 'Desktop alerts are unavailable because this browser cannot prevent duplicate delivery across tabs.'
  }
  if (!preferences.valid) {
    return 'Saved notification preferences could not be verified. Alerts stay off until you enable them again.'
  }
  if (preferences.state.enabled && capability === 'granted') {
    return 'Enabled for new items only. Current items were used as the baseline and will not create a backlog burst.'
  }
  return capability === 'default'
    ? 'Off. Enabling will ask for browser permission from this click.'
    : 'Off. You can enable alerts for new items.'
}

const feedbackCopy = Object.freeze({
  granted: 'Desktop notifications enabled for future items.',
  disabled: 'Desktop notifications disabled.',
  denied: 'The browser did not grant notification permission.',
  unsupported: 'Desktop notifications are not supported by this browser.',
  'locks-unavailable': 'Desktop notifications need cross-tab lock support to avoid duplicates.',
  'storage-unavailable': 'Desktop notifications need safe local storage to avoid duplicate delivery.',
  'request-failed': 'The browser could not complete the notification permission request.',
  'presentation-failed': 'The browser could not display a desktop notification.',
})

// Severity is shown visually by the coloured dot (aria-hidden). Carry the same meaning in TEXT so it is
// not conveyed by colour alone (WCAG 1.4.1) and is announced to screen readers (the dot is decorative).
const SEVERITY_LABEL = Object.freeze({
  action: 'Needs action', warning: 'Warning', danger: 'Urgent', success: 'Resolved',
})

function AttentionItem({ item, unread, sourceStale, onOpenRun, onMarkRead, onDismiss,
  onOpenPermission }) {
  const timestamp = itemTime(item.created)
  const stale = item.stale || sourceStale
  const activeAction = item.needsAction && item.active
  const actionLabel = stale ? 'Verify current state' : item.actionLabel
  // Reconstruct the destination from the normalized, generation-fenced fields. Never trust a URL
  // supplied by the feed (and permission cards never receive a link at all).
  const runHref = item.source === 'run' ? attentionHref(item) : null
  const permissionActionLabel = item.source === 'permission'
    ? `${actionLabel} for approval request ${item.requestId.slice(0, 6)}`
    : undefined
  return <li className={`attention-item severity-${item.severity}${unread ? ' unread' : ''}`}
    data-attention-item-id={item.id} tabIndex={-1}>
    <div className="attention-item-heading">
      <span className="attention-severity-dot" aria-hidden="true" />
      {SEVERITY_LABEL[item.severity] && <span className="sr-only">{SEVERITY_LABEL[item.severity]}: </span>}
      <h4>{item.title}</h4>
      {stale && <span className="attention-stale-label">Last verified</span>}
      {unread && <span className="attention-new-label">{stale ? 'Unread' : 'New'}</span>}
    </div>
    {item.source === 'run' && <p className="attention-run-context">
      <strong>{item.contextLabel || item.runId}</strong>
      {item.taskId && item.taskId !== item.contextLabel && <span> · task {item.taskId}</span>}
    </p>}
    <p>{item.detail}</p>
    {timestamp && <time dateTime={timestamp.iso}>{timestamp.label}</time>}
    <div className="attention-item-actions">
      {runHref && <a className="attention-button primary" href={runHref}
        aria-label={`${actionLabel} for ${item.contextLabel || item.runId}`}
        onClick={event => onOpenRun(event, item.id, runHref)}>{actionLabel}</a>}
      {item.source === 'permission' && <button type="button" className="attention-button primary"
        aria-label={permissionActionLabel}
        onClick={() => onOpenPermission(item)}>{actionLabel}</button>}
      {activeAction && unread && <button type="button" className="attention-button subtle"
        aria-label={`Mark ${item.title}${item.source === 'run' ? ` for ${item.contextLabel || item.runId}` : ''} as read`}
        onClick={() => onMarkRead(item.id)}>Mark read</button>}
      {!activeAction && <button type="button" className="attention-button subtle"
        aria-label={`Dismiss ${item.title}${item.source === 'run' ? ` for ${item.contextLabel || item.runId}` : ''}`}
        onClick={() => onDismiss(item.id)}>Dismiss</button>}
    </div>
  </li>
}

export default function AttentionCenter() {
  const {
    items, currentItems, initialized, runStale, permissionsStale, partial, truncated,
    hasMore, loadingMore, loadMoreError, loadMore, refresh,
    authoritative, verified, runVerifiedGeneratedAt, runActiveActionCount,
  } = useAttention()
  const [open, setOpen] = useState(false)
  const [preferences, setPreferences] = useState(() => loadAttentionState())
  const [capability, setCapability] = useState(() => notificationCapability())
  const [notificationBusy, setNotificationBusy] = useState(false)
  const [notificationFeedback, setNotificationFeedback] = useState('')
  const [focusRevision, setFocusRevision] = useState(0)
  const [liveMessage, setLiveMessageState] = useState({ text: '', revision: 0 })
  const setLiveMessage = useCallback(text => {
    setLiveMessageState(previous => ({ text, revision: previous.revision + 1 }))
  }, [])
  const dialogRef = useRef(null)
  const actionHeadingRef = useRef(null)
  const recentHeadingRef = useRef(null)
  const focusRequestRef = useRef(null)
  const pendingHandoffRef = useRef(null)
  const channelRef = useRef(null)
  const seenItemIdsRef = useRef(new Set())
  const baselinedSourcesRef = useRef({ run: false, permission: false })
  const titleId = useId()
  const descriptionId = useId()
  const drawerId = useId()

  const close = useCallback(() => {
    pendingHandoffRef.current = null
    focusRequestRef.current = null
    setOpen(false)
  }, [])
  const closeForHandoff = useCallback(handoff => {
    focusRequestRef.current = null
    pendingHandoffRef.current = handoff
    setOpen(false)
  }, [])
  useDialogFocus(dialogRef, close, open, { priority: DIALOG_PRIORITY.ATTENTION })
  // Run the destination action only after the dialog focus trap has torn down and restored its
  // opener. Route/Assistant focus can then take ownership without a late Attention cleanup winning.
  useEffect(() => {
    if (open || !pendingHandoffRef.current) return
    const handoff = pendingHandoffRef.current
    pendingHandoffRef.current = null
    handoff()
  }, [open])

  const jumpToSection = useCallback(section => {
    const heading = section === 'action' ? actionHeadingRef.current : recentHeadingRef.current
    if (!heading) return
    heading.focus({ preventScroll: true })
    heading.closest('.attention-section')?.scrollIntoView({ block: 'start' })
  }, [])

  const reloadPreferences = useCallback(() => {
    setPreferences(loadAttentionState())
    setCapability(notificationCapability())
  }, [])

  useEffect(() => {
    const channel = createAttentionChannel({ onInvalidate: reloadPreferences })
    channelRef.current = channel
    return () => {
      channel.close()
      if (channelRef.current === channel) channelRef.current = null
    }
  }, [reloadPreferences])

  useEffect(() => {
    const onFocus = () => reloadPreferences()
    const onOpen = () => setOpen(true)
    window.addEventListener('focus', onFocus)
    window.addEventListener('ll:open-attention', onOpen)
    return () => {
      window.removeEventListener('focus', onFocus)
      window.removeEventListener('ll:open-attention', onOpen)
    }
  }, [reloadPreferences])

  const acknowledgedIds = useMemo(
    () => attentionIds(preferences.state, 'acknowledged'), [preferences.state.acknowledged],
  )
  const dismissedIds = useMemo(
    () => attentionIds(preferences.state, 'dismissed'), [preferences.state.dismissed],
  )
  const visibleItems = useMemo(
    () => items.filter(item => (item.needsAction && item.active) || !dismissedIds.has(item.id)),
    [items, dismissedIds],
  )
  const actionItems = useMemo(
    () => visibleItems.filter(item => item.needsAction && item.active), [visibleItems],
  )
  const recentItems = useMemo(
    () => visibleItems.filter(item => !(item.needsAction && item.active)), [visibleItems],
  )
  const unreadItems = useMemo(
    () => visibleItems.filter(item => !acknowledgedIds.has(item.id)),
    [visibleItems, acknowledgedIds],
  )
  const unreadCount = unreadItems.length
  const permissionActiveActionCount = useMemo(
    () => currentItems.reduce((count, item) => count
      + (item.source === 'permission' && item.needsAction && item.active ? 1 : 0), 0),
    [currentItems],
  )
  const runActionCountKnown = Number.isSafeInteger(runActiveActionCount)
    && runActiveActionCount >= 0
  const loadedRunActionCount = actionItems.reduce((count, item) => count
    + (item.source === 'run' ? 1 : 0), 0)
  const feedAuthoritative = authoritative === true
  const displayedRunActionCount = feedAuthoritative && runActionCountKnown
    ? runActiveActionCount
    : Math.max(runActionCountKnown ? runActiveActionCount : 0, loadedRunActionCount)
  const activeActionCount = displayedRunActionCount
    + permissionActiveActionCount
  const actionCountExact = feedAuthoritative && runActionCountKnown
  const actionPhrase = actionCountCopy(activeActionCount)
  const stillActionPhrase = actionCountCopy(activeActionCount, true)
  const uncertainActionPhrase = activeActionCount === 1
    ? (verified
        ? '1 item is shown or was last verified as needing action'
        : '1 loaded item needs action')
    : (verified
        ? `${activeActionCount} items are shown or were last verified as needing action`
        : `${activeActionCount} loaded items need action`)
  const unreadComplete = feedAuthoritative && !truncated
  const unreadPaginationIncomplete = feedAuthoritative && truncated

  // Baseline each source's first successful snapshot silently. A source may recover after the other
  // one initialized the hook, so treating them separately prevents a delayed backlog avalanche.
  useEffect(() => {
    if (!initialized) return
    const fresh = []
    for (const source of ['run', 'permission']) {
      const sourceStale = source === 'run' ? runStale || partial : permissionsStale
      const sourceItems = currentItems.filter(item => item.source === source)
      if (!baselinedSourcesRef.current[source]) {
        if (sourceStale) continue
        for (const item of sourceItems) seenItemIdsRef.current.add(item.id)
        baselinedSourcesRef.current[source] = true
        continue
      }
      for (const item of sourceItems) {
        // Do not consume the in-tab occurrence identity from a stale/partial snapshot. The same
        // episode must still receive its one live announcement when this source is verified again.
        if (sourceStale || item.stale) continue
        if (!seenItemIdsRef.current.has(item.id)) {
          seenItemIdsRef.current.add(item.id)
          if (!dismissedIds.has(item.id)) fresh.push(item)
        }
      }
    }
    if (fresh.length) setLiveMessage(`${fresh.length} new attention ${fresh.length === 1 ? 'item' : 'items'}.`)
  }, [initialized, currentItems, dismissedIds, runStale, permissionsStale, partial, setLiveMessage])

  const broadcastInvalidation = useCallback(value => {
    // Cross-tab messages are deliberately payload-free. The receiving tab reloads its own bounded,
    // validated envelope from storage instead of trusting data sent by another document.
    channelRef.current?.broadcast(value)
  }, [])

  const persistIds = useCallback(async (field, ids, message, includeAcknowledged = false) => {
    const result = await mutateAttentionState(state => {
      let next = recordAttentionIds(state, field, ids)
      if (includeAcknowledged) next = recordAttentionIds(next, 'acknowledged', ids)
      return next
    }, { broadcast: broadcastInvalidation })
    if (!result.ok || !result.state) {
      setNotificationFeedback(ATTENTION_PREFERENCE_FAILURE)
      setLiveMessage(ATTENTION_PREFERENCE_FAILURE)
      return false
    }
    setPreferences({ state: result.state, available: true, valid: true })
    if (message) setLiveMessage(message)
    return true
  }, [broadcastInvalidation, setLiveMessage])

  const acknowledgeInBackground = useCallback(id => {
    void persistIds('acknowledged', [id], '').catch(() => {
      setNotificationFeedback(ATTENTION_PREFERENCE_FAILURE)
      setLiveMessage(ATTENTION_PREFERENCE_FAILURE)
    })
  }, [persistIds, setLiveMessage])

  const openRun = useCallback((event, id, href) => {
    if (!isPlainRunActivation(event) || typeof href !== 'string' || !href.startsWith('#/run/')) return
    event.preventDefault()
    closeForHandoff(() => {
      if (location.hash === href) document.querySelector('[data-route-main]')?.focus({ preventScroll: true })
      else location.hash = href
      acknowledgeInBackground(id)
    })
  }, [acknowledgeInBackground, closeForHandoff])

  const markRead = useCallback(async id => {
    focusRequestRef.current = { ids: [id], section: 'action' }
    const saved = await persistIds('acknowledged', [id], 'Attention item marked as read.')
    if (saved) setFocusRevision(value => value + 1)
    else focusRequestRef.current = null
  }, [persistIds])

  const dismiss = useCallback(async id => {
    const index = recentItems.findIndex(item => item.id === id)
    focusRequestRef.current = {
      ids: index < 0 ? [] : [recentItems[index + 1]?.id, recentItems[index - 1]?.id].filter(Boolean),
      section: 'recent',
    }
    const saved = await persistIds('dismissed', [id], 'Attention item dismissed.', true)
    if (saved) setFocusRevision(value => value + 1)
    else focusRequestRef.current = null
  }, [persistIds, recentItems])

  const markAllRead = useCallback(async () => {
    if (!unreadCount) return
    const loaded = unreadComplete ? '' : 'loaded '
    const unresolved = activeActionCount > 0
      ? actionCountExact
        ? ` ${stillActionPhrase}.`
        : ` ${uncertainActionPhrase}. Current action total is unavailable.`
      : ''
    await persistIds('acknowledged', unreadItems.map(item => item.id),
      `${unreadCount} ${loaded}${itemWord(unreadCount)} marked as read.${unresolved}`)
  }, [activeActionCount, actionCountExact, persistIds, stillActionPhrase,
    uncertainActionPhrase, unreadComplete, unreadCount, unreadItems])

  const openPermission = useCallback(item => {
    if (item?.source !== 'permission' || !/^[0-9a-f]{16}$/.test(item.session || '')) return
    closeForHandoff(() => {
      window.dispatchEvent(new CustomEvent('ll:open-assistant-session', {
        detail: { session: item.session },
      }))
      acknowledgeInBackground(item.id)
    })
  }, [acknowledgeInBackground, closeForHandoff])

  const enableNotifications = useCallback(async () => {
    if (notificationBusy) return
    setNotificationBusy(true)
    setNotificationFeedback('')
    try {
      const result = await enableAttentionNotifications(items, {
        broadcast: broadcastInvalidation,
      })
      // Only assert a verified-available preference envelope when the write actually committed —
      // mirror disableNotifications, which gates on `result.ok`. A failed persist (quota/blocked)
      // returns ok:false with the safe old state; reload rather than claim valid:true it never wrote.
      if (result.state && result.ok) setPreferences({ state: result.state, available: true, valid: true })
      else reloadPreferences()
      setCapability(notificationCapability())
      setNotificationFeedback(feedbackCopy[result.status] || 'Desktop notification settings were not changed.')
    } catch {
      reloadPreferences()
      setNotificationFeedback('The browser could not complete the notification permission request.')
    } finally { setNotificationBusy(false) }
  }, [broadcastInvalidation, items, notificationBusy, reloadPreferences])

  const disableNotifications = useCallback(async () => {
    if (notificationBusy) return
    setNotificationBusy(true)
    setNotificationFeedback('')
    try {
      const result = await disableAttentionNotifications({ broadcast: broadcastInvalidation })
      if (result.state && result.ok) {
        setPreferences({ state: result.state, available: true, valid: true })
      } else reloadPreferences()
      setCapability(notificationCapability())
      setNotificationFeedback(feedbackCopy[result.status]
        || 'Desktop notification settings were not changed.')
    } catch {
      reloadPreferences()
      setNotificationFeedback('The browser could not update desktop notification settings.')
    } finally { setNotificationBusy(false) }
  }, [broadcastInvalidation, notificationBusy, reloadPreferences])

  const deliveryItems = useMemo(
    () => currentItems.filter(item => !dismissedIds.has(item.id)
      && !acknowledgedIds.has(item.id)),
    [currentItems, dismissedIds, acknowledgedIds],
  )
  // Include notifyEligible in the key so the delivery effect re-runs when an item flips
  // stale→fresh (same id+created): otherwise a genuinely-new item that first arrived while its
  // source was momentarily stale (notifyEligible=false → filtered out, never added to `notified`)
  // never gets its desktop notification once the source recovers. Re-delivery is idempotent
  // (deliverAttentionNotifications de-dupes via the persisted `notified` set), so this only surfaces
  // the previously-skipped one, never a duplicate.
  const deliveryKey = deliveryItems
    .map(item => `${item.id}:${item.created}:${item.notifyEligible ? 1 : 0}`).join('|')
  useEffect(() => {
    if (!initialized || !preferences.state.enabled) return
    let active = true
    deliverAttentionNotifications(deliveryItems, {
      broadcast: broadcastInvalidation,
      // A Notification can outlive this React instance (for example after owner navigation). Route
      // the click through the payload-free global event so only the currently mounted owner center
      // handles it; a review route has no listener and remains isolated.
      onOpenCenter: dispatchOpenAttention,
    }).then(result => {
      if (!active) return
      if (result.status === 'storage-unavailable') reloadPreferences()
      if (feedbackCopy[result.status]) setNotificationFeedback(feedbackCopy[result.status])
    }).catch(() => {
      if (active) setNotificationFeedback('Desktop notification delivery could not be completed.')
    })
    return () => { active = false }
  }, [broadcastInvalidation, deliveryKey, initialized, preferences.state.enabled, reloadPreferences])

  // If a focused read/dismiss control disappears from the DOM, keep keyboard focus in the dialog.
  useEffect(() => {
    if (!open) return
    const frame = requestAnimationFrame(() => {
      const root = dialogRef.current
      const request = focusRequestRef.current
      if (root && request) {
        const cards = [...root.querySelectorAll('[data-attention-item-id]')]
        const target = request.ids
          .map(id => cards.find(card => card.dataset.attentionItemId === id))
          .find(Boolean)
          || (request.section === 'action' ? actionHeadingRef.current : recentHeadingRef.current)
        focusRequestRef.current = null
        if (target) {
          target.focus({ preventScroll: true })
          target.scrollIntoView({ block: 'nearest' })
          return
        }
      }
      if (root && !root.contains(document.activeElement)) {
        root.querySelector('[data-dialog-initial-focus]')?.focus({ preventScroll: true })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [focusRevision, open, unreadCount, visibleItems.length])

  const feedVerified = feedAuthoritative
  const verifiedAge = verified === true ? snapshotAge(runVerifiedGeneratedAt) : ''
  const sourceMessages = []
  if (!initialized) sourceMessages.push('Updating attention items…')
  else {
    if (runStale && permissionsStale) sourceMessages.push('Both attention sources are temporarily stale; showing the last safe snapshot.')
    else if (runStale) sourceMessages.push('Run attention is temporarily stale; showing the last safe snapshot.')
    else if (permissionsStale) sourceMessages.push('Assistant approvals are temporarily stale; showing the last safe snapshot.')
    if (partial) sourceMessages.push('Some run logs could not be inspected, so this list may be incomplete.')
    if (!feedVerified) sourceMessages.push(verifiedAge
      ? `Last verified snapshot was updated ${verifiedAge}.`
      : 'No complete verified snapshot is available yet.')
    if (hasMore && truncated) sourceMessages.push('More older attention items are available below.')
  }
  const notificationsEnabled = preferences.valid && preferences.state.enabled
  const enableBlocked = notificationBusy || !preferences.available
    || capability === 'unsupported' || capability === 'denied' || capability === 'locks-unavailable'
  const unreadPhrase = unreadCount === 1 ? '1 unread item' : `${unreadCount} unread items`
  const loadedUnreadPhrase = unreadCount > 0
    ? `${unreadCount} unread loaded ${itemWord(unreadCount)}` : 'unread count incomplete'
  const actionAria = actionCountExact
    ? (activeActionCount > 0 ? actionPhrase : 'no items need action')
    : activeActionCount > 0
      ? `${uncertainActionPhrase}; current action total is unavailable`
      : verified
        ? 'current action total is unavailable; no loaded or last verified items need action'
        : 'current action total is unavailable; no loaded items need action'
  const unreadAria = unreadComplete
    ? (unreadCount > 0 ? unreadPhrase : 'no unread items')
    : unreadCount > 0
      ? `at least ${unreadCount} loaded ${itemWord(unreadCount)} ${unreadCount === 1 ? 'is' : 'are'} unread`
      : 'no unread items are loaded; unread count is incomplete'
  const countAria = `${actionAria}; ${unreadAria}`
  const triggerLabel = !initialized
    ? 'Open attention center. Checking for updates.'
    : feedVerified
      ? `Open attention center, ${countAria}.`
      : verified
        ? `Open attention center. Current status unavailable. ${countAria}.`
        : `Open attention center. Current status unavailable. No complete verified snapshot. ${countAria}.`
  const badgeShowsActions = activeActionCount > 0
  const showBadge = badgeShowsActions || unreadCount > 0 || unreadPaginationIncomplete
  const badge = badgeShowsActions
    ? `!${visualCount(activeActionCount)}${actionCountExact ? '' : '?'}`
    : unreadCount > 0
      ? visualCount(unreadCount, unreadPaginationIncomplete)
      : '?'
  const triggerClass = `attention-trigger${badgeShowsActions ? ' has-action' : showBadge ? ' has-unread' : ''}${feedVerified ? '' : ' is-unverified'}`
  const headerStatus = !initialized
    ? 'Checking for updates'
    : !feedVerified
      ? (verifiedAge ? `Last verified ${verifiedAge}` : 'Status unavailable')
      : activeActionCount > 0
        ? unreadCount > 0
          ? `${actionPhrase} · ${unreadComplete ? unreadPhrase : loadedUnreadPhrase}`
          : unreadComplete
            ? stillActionPhrase
            : `${stillActionPhrase} · unread count incomplete`
        : unreadCount > 0
          ? (unreadComplete ? unreadPhrase : loadedUnreadPhrase)
          : unreadComplete
            ? 'You are caught up'
            : 'Unread count incomplete'
  const actionEmptyCopy = !initialized
    ? 'Checking for items that need action…'
    : feedVerified
      ? 'Nothing needs your action right now.'
      : 'Current action status is unavailable. Showing the last verified snapshot.'
  const recentEmptyCopy = !initialized
    ? 'Checking recent run notices…'
    : feedVerified
      ? 'No recent completion or budget notices.'
      : 'Recent notice status is unavailable. Showing the last verified snapshot.'

  return <>
    <button type="button" className={triggerClass}
      aria-label={triggerLabel} aria-haspopup="dialog" aria-expanded={open}
      aria-controls={drawerId} onClick={() => setOpen(value => !value)}>
      <OpIcon name="bell" size={22} className="attention-bell-icon" />
      {showBadge && <span className={`attention-badge ${badgeShowsActions ? 'is-action' : 'is-unread'}`}
        aria-hidden="true">{badge}</span>}
    </button>
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
      {liveMessage.text && <span key={liveMessage.revision}>{liveMessage.text}</span>}
    </div>

    {open && <div className="attention-layer">
      <div className="attention-backdrop" aria-hidden="true" onMouseDown={close} />
      <section ref={dialogRef} id={drawerId} className="attention-drawer" role="dialog"
        aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1}>
        <header className="attention-header">
          <div className="attention-title-wrap">
            <h2 id={titleId}>Attention center</h2>
            <p id={descriptionId}>{headerStatus}</p>
          </div>
          {unreadCount > 0 && <button type="button" className="attention-header-action"
            aria-label={`${unreadComplete ? 'Mark all' : 'Mark'} ${unreadCount} ${unreadComplete ? 'unread' : 'loaded unread'} ${itemWord(unreadCount)} as read${activeActionCount > 0 ? `; ${actionCountExact ? stillActionPhrase : `${uncertainActionPhrase}; current action total is unavailable`}` : ''}`}
            onClick={markAllRead}>{unreadComplete ? 'Mark all read' : 'Mark loaded read'}</button>}
          <button type="button" className="attention-close" aria-label="Close attention center"
            data-dialog-initial-focus onClick={close}><OpIcon name="cross" size={20} /></button>
        </header>

        <div className="attention-scroll">
          <nav className="attention-jump-nav" aria-label="Attention sections">
            <button type="button" className="attention-jump"
              aria-label={`Jump to Needs action, ${actionAria}`}
              onClick={() => jumpToSection('action')}>
              <span>Needs action</span>
              <span className="attention-jump-count" aria-hidden="true">{actionCountExact
                ? visualCount(activeActionCount)
                : activeActionCount > 99 ? '99+?' : `${activeActionCount}?`}</span>
            </button>
            <button type="button" className="attention-jump"
              aria-label={`Jump to Recent, ${recentItems.length} loaded`}
              onClick={() => jumpToSection('recent')}>
              <span>Recent</span>
              <span className="attention-jump-count" aria-hidden="true">
                {visualCount(recentItems.length)}
              </span>
            </button>
          </nav>
          {sourceMessages.length > 0 && <div className="attention-source-status">
            {/* The live region is the WRAPPER, not the list. `role="status"` on the <ul> replaces
                its implicit `list` role, which strips every <li> of its list semantics — a screen
                reader then reads the source warnings as loose text with no count and no boundaries.
                Same rule the Atlas concept list is already annotated with. */}
            <div role="status" aria-live="polite">
              <ul>
                {sourceMessages.map(message => <li key={message}>{message}</li>)}
              </ul>
            </div>
            {!feedVerified && <button type="button" className="attention-button subtle"
              onClick={() => refresh?.()}>Retry now</button>}
          </div>}

          <section className="attention-section" aria-labelledby={`${titleId}-action`}>
            <div className="attention-section-heading">
              <h3 ref={actionHeadingRef} id={`${titleId}-action`} tabIndex={-1}>Needs action</h3>
              <span className={`attention-section-count${activeActionCount > 0 ? ' has-action' : ''}`}>
                <span aria-hidden="true">{actionCountExact
                  ? activeActionCount
                  : activeActionCount > 99 ? '99+?' : `${activeActionCount}?`}</span>
                <span className="sr-only">{actionCountExact
                  ? `${actionPhrase} in total`
                  : `${uncertainActionPhrase}. Current total unavailable.`}</span>
              </span>
            </div>
            {actionItems.length
              ? <ul className="attention-list">{actionItems.map(item => <AttentionItem key={item.id}
                  item={item} unread={!acknowledgedIds.has(item.id)}
                  sourceStale={item.source === 'run' ? runStale : permissionsStale}
                  onOpenRun={openRun} onMarkRead={markRead}
                  onDismiss={dismiss} onOpenPermission={openPermission} />)}</ul>
              : <p className="attention-empty">{actionEmptyCopy}</p>}
          </section>

          <section className="attention-notifications" aria-labelledby={`${titleId}-notifications`}>
            <div>
              <h3 id={`${titleId}-notifications`}>Desktop notifications</h3>
              <p>{capabilityCopy(capability, preferences)}</p>
            </div>
            {notificationsEnabled
              ? <button type="button" className="attention-button subtle" disabled={notificationBusy}
                onClick={disableNotifications}>Disable</button>
              : <button type="button" className="attention-button" disabled={enableBlocked}
                onClick={enableNotifications}>{notificationBusy ? 'Enabling…' : 'Enable'}</button>}
          </section>
          {notificationFeedback && <p className="attention-feedback" role="status">{notificationFeedback}</p>}

          <section className="attention-section" aria-labelledby={`${titleId}-recent`}>
            <div className="attention-section-heading">
              <h3 ref={recentHeadingRef} id={`${titleId}-recent`} tabIndex={-1}>Recent</h3>
              <span className="attention-section-count">
                <span aria-hidden="true">{recentItems.length} loaded</span>
                <span className="sr-only">{recentItems.length} recent {itemWord(recentItems.length)} loaded
                </span>
              </span>
            </div>
            {recentItems.length
              ? <ul className="attention-list">{recentItems.map(item => <AttentionItem key={item.id}
                  item={item} unread={!acknowledgedIds.has(item.id)}
                  sourceStale={item.source === 'run' ? runStale : permissionsStale}
                  onOpenRun={openRun} onMarkRead={markRead}
                  onDismiss={dismiss} onOpenPermission={openPermission} />)}</ul>
              : <p className="attention-empty">{recentEmptyCopy}</p>}
          </section>

          {hasMore && <div className="attention-load-more">
            <button type="button" className="attention-button" disabled={loadingMore}
              onClick={loadMore}>{loadingMore ? 'Loading…' : 'Load older items'}</button>
          </div>}
          {loadMoreError && <p className="attention-feedback" role="status">{loadMoreError}</p>}

          {initialized && items.length > 0 && visibleItems.length === 0
            && <p className="attention-all-dismissed">{feedVerified
              ? 'All current items are dismissed. New IDs will appear here normally.'
              : 'All items in the last verified snapshot are dismissed. Retry to check the current state.'}</p>}
        </div>
      </section>
    </div>}
  </>
}
