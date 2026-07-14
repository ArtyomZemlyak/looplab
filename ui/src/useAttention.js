import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { assistantPermissions, attentionFeed } from './api.js'
import {
  normalizePermissionAttention, normalizeRunAttention, sortAttentionItems,
} from './attentionModel.js'

const CURSOR_RE = /^[0-9a-f]{64}$/

function normalizeRunPage(payload) {
  if (!payload || !Array.isArray(payload.items)) return null
  const items = payload.items.map(normalizeRunAttention)
  // A protocol-invalid item must not turn an actionable last-safe snapshot into an
  // authoritative empty page. Treat the whole source read as stale and retry it.
  if (items.some(item => item == null)) return null
  const truncated = payload.truncated === true
  const nextCursor = typeof payload.next_cursor === 'string'
    && CURSOR_RE.test(payload.next_cursor) ? payload.next_cursor : null
  if (truncated && !nextCursor) return null
  return { items, truncated, nextCursor, partial: payload.partial === true }
}

function normalizePermissionPage(payload, now) {
  if (!payload || !Array.isArray(payload.pending)) return null
  const items = []
  for (const raw of payload.pending) {
    const item = normalizePermissionAttention(raw, now)
    if (item) {
      items.push(item)
      continue
    }
    // Expiry is an authoritative removal, not a malformed response. Other invalid
    // records make the independent permission source stale so the prior safe list stays.
    const expired = raw && typeof raw === 'object' && !Array.isArray(raw)
      && typeof raw.expires_at === 'number' && Number.isFinite(raw.expires_at)
      && raw.expires_at > 0 && raw.expires_at * 1000 <= now
    if (!expired) return null
  }
  return items
}

const sameIds = (left, right) => left.length === right.length
  && left.every((item, index) => item.id === right[index]?.id)

export function useAttention({ intervalMs = 4000 } = {}) {
  const [state, setState] = useState({
    runPages: [], permissions: [], initialized: false,
    runStale: false, permissionsStale: false, partial: false, truncated: false,
    nextCursor: null, firstNextCursor: null, loadingMore: false, loadMoreError: '',
  })
  const [refreshToken, setRefreshToken] = useState(0)
  const loadingMoreRef = useRef(false)

  useEffect(() => {
    let active = true
    let running = false
    const poll = async () => {
      if (running) return
      running = true
      const now = Date.now()
      try {
        const [runResult, permissionResult] = await Promise.allSettled([
          attentionFeed(200), assistantPermissions(),
        ])
        if (!active) return
        const firstPage = runResult.status === 'fulfilled'
          ? normalizeRunPage(runResult.value) : null
        const permissionPage = permissionResult.status === 'fulfilled'
          ? normalizePermissionPage(permissionResult.value, now) : null
        setState(previous => {
          let runPages = previous.runPages
          let nextCursor = previous.nextCursor
          let firstNextCursor = previous.firstNextCursor
          let truncated = previous.truncated
          let loadMoreError = previous.loadMoreError
          if (firstPage) {
            const sameFirstPage = sameIds(previous.runPages[0] || [], firstPage.items)
            firstNextCursor = firstPage.nextCursor
            if (sameFirstPage && firstPage.truncated && previous.runPages.length > 1) {
              runPages = [firstPage.items, ...previous.runPages.slice(1)]
            } else {
              runPages = [firstPage.items]
              nextCursor = firstPage.nextCursor
              truncated = firstPage.truncated
              loadMoreError = ''
            }
          }
          // Drop raw action/scope/preview immediately; only opaque request/session ids and expiry
          // survive into React state. Assistant re-reads the authoritative card when it is opened.
          const permissions = permissionPage != null
            ? permissionPage
            : previous.permissions.filter(item => !item.expiresAt || item.expiresAt * 1000 > now)
          return {
            ...previous, runPages, permissions, nextCursor, firstNextCursor, truncated, loadMoreError,
            initialized: true,
            runStale: firstPage == null,
            permissionsStale: permissionPage == null,
            partial: firstPage ? firstPage.partial : previous.partial,
          }
        })
      } finally { running = false }
    }
    poll()
    const timer = setInterval(poll, intervalMs)
    return () => { active = false; clearInterval(timer) }
  }, [intervalMs, refreshToken])

  const loadMore = useCallback(async () => {
    const cursor = state.nextCursor
    if (!cursor || loadingMoreRef.current) return
    loadingMoreRef.current = true
    setState(previous => ({ ...previous, loadingMore: true, loadMoreError: '' }))
    try {
      const page = normalizeRunPage(await attentionFeed(200, cursor))
      if (!page) throw new Error('invalid attention page')
      setState(previous => {
        // A first-page poll may have invalidated this cursor while the request was in flight.
        if (previous.nextCursor !== cursor) return { ...previous, loadingMore: false }
        return {
          ...previous,
          runPages: [...previous.runPages, page.items],
          nextCursor: page.nextCursor,
          truncated: page.truncated,
          partial: previous.partial || page.partial,
          loadingMore: false,
          loadMoreError: '',
        }
      })
    } catch (error) {
      setState(previous => error?.status === 409 ? ({
        ...previous, runPages: previous.runPages.slice(0, 1),
        nextCursor: previous.firstNextCursor,
        truncated: !!previous.firstNextCursor, loadingMore: false,
        loadMoreError: 'The attention list changed before the next page loaded. Try again.',
      }) : ({
        ...previous, loadingMore: false,
        loadMoreError: 'Older attention items could not be loaded. Try again.',
      }))
    } finally { loadingMoreRef.current = false }
  }, [state.nextCursor])

  const runs = useMemo(() => state.runPages.flat(), [state.runPages])
  const currentItems = useMemo(() => sortAttentionItems([
    ...(state.runPages[0] || []), ...state.permissions,
  ]), [state.runPages, state.permissions])
  const items = useMemo(() => sortAttentionItems([...runs, ...state.permissions]),
    [runs, state.permissions])
  return {
    ...state, runs, items, currentItems,
    hasMore: state.truncated && !!state.nextCursor, loadMore,
    refresh: () => setRefreshToken(value => value + 1),
  }
}
