import React, { lazy } from 'react'
import LazyBoundary from './LazyBoundary.jsx'

// Native module loading deduplicates the shared specifier; React.lazy retains each named view.
const lazyOwner = name => lazy(() => import('./OwnerChrome.jsx')
  .then(module => ({ default: module[name] })))
const AssistantBar = lazyOwner('AssistantBar')
const AttentionCenter = lazyOwner('AttentionCenter')

/**
 * Stable owner-plane shell. Route content changes in the first slot; Assistant and Attention keep
 * the same sibling identity, so an owner navigation cannot remount or erase an Assistant session.
 * Injectable components keep the persistence contract testable without starting their pollers.
 */
export default function OwnerWorkspace({ route, children,
  AssistantComponent = AssistantBar, AttentionComponent = AttentionCenter }) {
  return <div className="app-shell">
    <div className="app-shell-main">{children}</div>
    <LazyBoundary label="Attention Center">
      <AttentionComponent />
    </LazyBoundary>
    <LazyBoundary label="Assistant">
      <AssistantComponent runId={route.view === 'run' ? route.id : null} />
    </LazyBoundary>
  </div>
}
