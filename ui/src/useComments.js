import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { runComments } from './api.js'
import {
  commentMatchesSubject, mergeCommentPages, normalizeCommentsPage,
} from './commentsModel.js'

const initialState = () => ({
  pages: [],
  initialized: false,
  loading: false,
  refreshing: false,
  stale: false,
  error: '',
  nextCursor: null,
  hasMore: false,
  loadingMore: false,
  loadMoreError: '',
})

export function useComments({
  runId,
  nodeId = null,
  nodeGeneration = null,
  expectedGeneration = null,
  includeResolved = true,
  limit = 100,
  enabled = true,
  refreshKey = null,
} = {}) {
  const [resource, setResource] = useState(initialState)
  const [refreshToken, setRefreshToken] = useState(0)
  // The event-log generation is part of the resource identity, not merely response validation.
  // A reset can keep the same run id and numeric node attempt; retaining generation-A pages while
  // generation B is loading (or fails to load) would disclose comments from the replaced run.
  const scopeKey = `${runId || ''}@${expectedGeneration || '?'}:${nodeId == null ? '*' : `${nodeId}@${nodeGeneration}`}:${includeResolved ? 1 : 0}`
  const scopeRef = useRef(scopeKey)
  const requestRef = useRef(0)
  const loadingMoreRef = useRef(false)
  const subjectValid = nodeId == null || (Number.isSafeInteger(nodeId) && nodeId >= 0
    && Number.isSafeInteger(nodeGeneration) && nodeGeneration >= 0)

  useEffect(() => {
    const scopeChanged = scopeRef.current !== scopeKey
    scopeRef.current = scopeKey
    if (!enabled || !runId || !/^[0-9a-f]{64}$/.test(expectedGeneration || '') || !subjectValid) {
      if (scopeChanged || !subjectValid) setResource(subjectValid ? initialState() : {
        ...initialState(), initialized: true,
        error: 'An exact experiment attempt is required before comments can be loaded.',
      })
      return undefined
    }
    const requestId = ++requestRef.current
    const controller = new AbortController()
    setResource(previous => scopeChanged ? {
      ...initialState(), loading: true,
    } : {
      ...previous,
      loading: !previous.initialized,
      refreshing: previous.initialized,
      error: '',
    })
    runComments(runId, { nodeId, nodeGeneration, includeResolved, limit })
      .then(payload => {
        if (controller.signal.aborted || requestRef.current !== requestId) return
        const page = normalizeCommentsPage(payload, expectedGeneration)
        if (!page || page.comments.some(comment => !commentMatchesSubject(
          comment, nodeId, nodeGeneration))) {
          throw new Error('The comments response was not valid for this exact run and experiment attempt.')
        }
        setResource({
          pages: [page.comments], initialized: true, loading: false, refreshing: false,
          stale: false, error: '', nextCursor: page.nextCursor, hasMore: page.hasMore,
          loadingMore: false, loadMoreError: '',
        })
      })
      .catch(error => {
        if (controller.signal.aborted || requestRef.current !== requestId) return
        setResource(previous => ({
          ...previous,
          initialized: true,
          loading: false,
          refreshing: false,
          stale: previous.pages.length > 0,
          error: error?.message || 'Comments could not be loaded.',
        }))
      })
    return () => controller.abort()
  }, [scopeKey, runId, nodeId, nodeGeneration, includeResolved, limit, enabled, expectedGeneration,
    subjectValid, refreshToken, refreshKey])

  const loadMore = useCallback(async () => {
    const cursor = resource.nextCursor
    if (!cursor || !resource.hasMore || loadingMoreRef.current || !enabled) return
    const requestedScope = scopeKey
    loadingMoreRef.current = true
    setResource(previous => ({ ...previous, loadingMore: true, loadMoreError: '' }))
    try {
      const page = normalizeCommentsPage(await runComments(runId, {
        nodeId, nodeGeneration, includeResolved, limit, cursor,
      }), expectedGeneration)
      if (!page || page.comments.some(comment => !commentMatchesSubject(
        comment, nodeId, nodeGeneration))) throw new Error('invalid comments page')
      if (scopeRef.current !== requestedScope) return
      setResource(previous => previous.nextCursor !== cursor ? {
        ...previous, loadingMore: false,
      } : {
        ...previous,
        pages: [...previous.pages, page.comments],
        nextCursor: page.nextCursor,
        hasMore: page.hasMore,
        loadingMore: false,
        loadMoreError: '',
      })
    } catch (error) {
      if (scopeRef.current !== requestedScope) return
      setResource(previous => ({
        ...previous,
        loadingMore: false,
        loadMoreError: error?.status === 409
          ? 'The comment list changed before the next page loaded. Refresh and try again.'
          : 'Older comments could not be loaded. Try again.',
      }))
    } finally { loadingMoreRef.current = false }
  }, [resource.nextCursor, resource.hasMore, enabled, runId, nodeId, nodeGeneration, includeResolved,
    limit, expectedGeneration, scopeKey])

  const comments = useMemo(() => mergeCommentPages(resource.pages), [resource.pages])
  return {
    ...resource,
    comments,
    loadMore,
    refresh: () => setRefreshToken(value => value + 1),
  }
}
