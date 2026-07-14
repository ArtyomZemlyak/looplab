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
import { useDialogFocus } from './useDialogFocus.js'
import './attention.css'

const dispatchOpenAttention = () => {
  if (typeof window !== 'undefined' && typeof window.Event === 'function') {
    window.dispatchEvent(new window.Event('ll:open-attention'))
  }
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

function AttentionItem({ item, unread, onAcknowledge, onDismiss, onOpenPermission }) {
  const timestamp = itemTime(item.created)
  // Reconstruct the destination from the normalized, generation-fenced fields. Never trust a URL
  // supplied by the feed (and permission cards never receive a link at all).
  const runHref = item.source === 'run' ? attentionHref(item) : null
  return <li className={`attention-item severity-${item.severity}${unread ? ' unread' : ''}`}>
    <div className="attention-item-heading">
      <span className="attention-severity-dot" aria-hidden="true" />
      <h4>{item.title}</h4>
      {item.stale && <span className="attention-stale-label">Last verified</span>}
      {unread && <span className="attention-new-label">New</span>}
    </div>
    <p>{item.detail}</p>
    {timestamp && <time dateTime={timestamp.iso}>{timestamp.label}</time>}
    <div className="attention-item-actions">
      {runHref && <a className="attention-button primary" href={runHref}
        onClick={() => onAcknowledge(item.id)}>{item.actionLabel}</a>}
      {item.source === 'permission' && <button type="button" className="attention-button primary"
        onClick={() => onOpenPermission(item)}>{item.actionLabel}</button>}
      <button type="button" className="attention-button subtle"
        aria-label={`Dismiss ${item.title}`} onClick={() => onDismiss(item.id)}>Dismiss</button>
    </div>
  </li>
}

export default function AttentionCenter() {
  const {
    items, currentItems, initialized, runStale, permissionsStale, partial, truncated,
    hasMore, loadingMore, loadMoreError, loadMore,
  } = useAttention()
  const [open, setOpen] = useState(false)
  const [preferences, setPreferences] = useState(() => loadAttentionState())
  const [capability, setCapability] = useState(() => notificationCapability())
  const [notificationBusy, setNotificationBusy] = useState(false)
  const [notificationFeedback, setNotificationFeedback] = useState('')
  const [liveMessage, setLiveMessage] = useState('')
  const dialogRef = useRef(null)
  const channelRef = useRef(null)
  const seenItemIdsRef = useRef(new Set())
  const baselinedSourcesRef = useRef({ run: false, permission: false })
  const titleId = useId()
  const descriptionId = useId()
  const drawerId = useId()

  const close = useCallback(() => setOpen(false), [])
  useDialogFocus(dialogRef, close, open)

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
    () => items.filter(item => !dismissedIds.has(item.id)), [items, dismissedIds],
  )
  const actionItems = useMemo(
    () => visibleItems.filter(item => item.needsAction && item.active), [visibleItems],
  )
  const recentItems = useMemo(
    () => visibleItems.filter(item => !(item.needsAction && item.active)), [visibleItems],
  )
  const unreadCount = useMemo(
    () => visibleItems.reduce((count, item) => count + (acknowledgedIds.has(item.id) ? 0 : 1), 0),
    [visibleItems, acknowledgedIds],
  )

  // Baseline each source's first successful snapshot silently. A source may recover after the other
  // one initialized the hook, so treating them separately prevents a delayed backlog avalanche.
  useEffect(() => {
    if (!initialized) return
    const fresh = []
    for (const source of ['run', 'permission']) {
      const sourceStale = source === 'run' ? runStale : permissionsStale
      const sourceItems = currentItems.filter(item => item.source === source)
      if (!baselinedSourcesRef.current[source]) {
        if (sourceStale) continue
        for (const item of sourceItems) seenItemIdsRef.current.add(item.id)
        baselinedSourcesRef.current[source] = true
        continue
      }
      for (const item of sourceItems) {
        if (!seenItemIdsRef.current.has(item.id)) {
          seenItemIdsRef.current.add(item.id)
          if (!dismissedIds.has(item.id) && !item.stale) fresh.push(item)
        }
      }
    }
    if (fresh.length) setLiveMessage(`${fresh.length} new attention ${fresh.length === 1 ? 'item' : 'items'}.`)
  }, [initialized, currentItems, dismissedIds, runStale, permissionsStale])

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
      setNotificationFeedback('This browser could not verify the saved attention preference.')
      return false
    }
    setPreferences({ state: result.state, available: true, valid: true })
    if (message) setLiveMessage(message)
    return true
  }, [broadcastInvalidation])

  const acknowledge = useCallback(async id => {
    await persistIds('acknowledged', [id], '')
    setOpen(false)
  }, [persistIds])

  const dismiss = useCallback(async id => {
    await persistIds('dismissed', [id], 'Attention item dismissed.', true)
  }, [persistIds])

  const markAllRead = useCallback(async () => {
    if (!unreadCount) return
    await persistIds('acknowledged', visibleItems.map(item => item.id),
      `${unreadCount} ${unreadCount === 1 ? 'item' : 'items'} marked as read.`)
  }, [persistIds, unreadCount, visibleItems])

  const openPermission = useCallback(async item => {
    if (item?.source !== 'permission' || !/^[0-9a-f]{16}$/.test(item.session || '')) return
    await persistIds('acknowledged', [item.id], '')
    setOpen(false)
    window.dispatchEvent(new CustomEvent('ll:open-assistant-session', {
      detail: { session: item.session },
    }))
  }, [persistIds])

  const enableNotifications = useCallback(async () => {
    if (notificationBusy) return
    setNotificationBusy(true)
    setNotificationFeedback('')
    try {
      const result = await enableAttentionNotifications(items, {
        broadcast: broadcastInvalidation,
      })
      if (result.state) setPreferences({ state: result.state, available: true, valid: true })
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
  const deliveryKey = deliveryItems.map(item => `${item.id}:${item.created}`).join('|')
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

  // If dismissing the focused row removes it from the DOM, keep keyboard focus inside the dialog.
  useEffect(() => {
    if (!open) return
    const frame = requestAnimationFrame(() => {
      const root = dialogRef.current
      if (root && !root.contains(document.activeElement)) {
        root.querySelector('[data-dialog-initial-focus]')?.focus({ preventScroll: true })
      }
    })
    return () => cancelAnimationFrame(frame)
  }, [open, visibleItems.length])

  const sourceMessages = []
  if (!initialized) sourceMessages.push('Updating attention items…')
  else {
    if (runStale && permissionsStale) sourceMessages.push('Both attention sources are temporarily stale; showing the last safe snapshot.')
    else if (runStale) sourceMessages.push('Run attention is temporarily stale; showing the last safe snapshot.')
    else if (permissionsStale) sourceMessages.push('Assistant approvals are temporarily stale; showing the last safe snapshot.')
    if (partial) sourceMessages.push('Some run logs could not be inspected, so this list may be incomplete.')
    if (truncated) sourceMessages.push('More older attention items are available below.')
  }
  const notificationsEnabled = preferences.valid && preferences.state.enabled
  const enableBlocked = notificationBusy || !preferences.available
    || capability === 'unsupported' || capability === 'denied' || capability === 'locks-unavailable'
  const badge = unreadCount > 99 ? '99+' : String(unreadCount)
  const triggerLabel = unreadCount
    ? `Open attention center, ${unreadCount} unread ${unreadCount === 1 ? 'item' : 'items'}`
    : 'Open attention center'

  return <>
    <button type="button" className={`attention-trigger${unreadCount ? ' has-unread' : ''}`}
      aria-label={triggerLabel} aria-haspopup="dialog" aria-expanded={open}
      aria-controls={drawerId} onClick={() => setOpen(value => !value)}>
      <OpIcon name="bell" size={22} className="attention-bell-icon" />
      {unreadCount > 0 && <span className="attention-badge" aria-hidden="true">{badge}</span>}
    </button>
    <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{liveMessage}</div>

    {open && <div className="attention-layer">
      <div className="attention-backdrop" aria-hidden="true" onMouseDown={close} />
      <section ref={dialogRef} id={drawerId} className="attention-drawer" role="dialog"
        aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1}>
        <header className="attention-header">
          <div className="attention-title-wrap">
            <h2 id={titleId}>Attention center</h2>
            <p id={descriptionId}>{unreadCount
              ? `${unreadCount} unread ${unreadCount === 1 ? 'item' : 'items'}`
              : 'You are caught up'}</p>
          </div>
          <button type="button" className="attention-header-action" disabled={!unreadCount}
            onClick={markAllRead}>Mark all read</button>
          <button type="button" className="attention-close" aria-label="Close attention center"
            data-dialog-initial-focus onClick={close}><OpIcon name="cross" size={20} /></button>
        </header>

        <div className="attention-scroll">
          {sourceMessages.length > 0 && <ul className="attention-source-status" role="status">
            {sourceMessages.map(message => <li key={message}>{message}</li>)}
          </ul>}

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

          <section className="attention-section" aria-labelledby={`${titleId}-action`}>
            <div className="attention-section-heading">
              <h3 id={`${titleId}-action`}>Needs action</h3>
              <span>{actionItems.length}</span>
            </div>
            {actionItems.length
              ? <ul className="attention-list">{actionItems.map(item => <AttentionItem key={item.id}
                  item={item} unread={!acknowledgedIds.has(item.id)} onAcknowledge={acknowledge}
                  onDismiss={dismiss} onOpenPermission={openPermission} />)}</ul>
              : <p className="attention-empty">{initialized
                  ? 'Nothing needs your action right now.' : 'Checking for items that need action…'}</p>}
          </section>

          <section className="attention-section" aria-labelledby={`${titleId}-recent`}>
            <div className="attention-section-heading">
              <h3 id={`${titleId}-recent`}>Recent</h3>
              <span>{recentItems.length}</span>
            </div>
            {recentItems.length
              ? <ul className="attention-list">{recentItems.map(item => <AttentionItem key={item.id}
                  item={item} unread={!acknowledgedIds.has(item.id)} onAcknowledge={acknowledge}
                  onDismiss={dismiss} onOpenPermission={openPermission} />)}</ul>
              : <p className="attention-empty">{initialized
                  ? 'No recent completion or budget notices.' : 'Checking recent run notices…'}</p>}
          </section>

          {hasMore && <div className="attention-load-more">
            <button type="button" className="attention-button" disabled={loadingMore}
              onClick={loadMore}>{loadingMore ? 'Loading…' : 'Load older items'}</button>
          </div>}
          {loadMoreError && <p className="attention-feedback" role="status">{loadMoreError}</p>}

          {initialized && items.length > 0 && visibleItems.length === 0
            && <p className="attention-all-dismissed">All current items are dismissed. New IDs will appear here normally.</p>}
        </div>
      </section>
    </div>}
  </>
}
