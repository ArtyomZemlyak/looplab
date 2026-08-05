// The ONE load / last-good / stale / retry resource machine (doc 25 UI-06). The same concept —
// read something, keep the last good copy, mark it stale rather than blank when a refresh fails,
// offer a retry — had been re-derived independently in RunList.useResource, panels.usePanelResource,
// panels.useNodeResource, TrustPanel's config effect, RunView's config effect, Inspector's
// detailResource and App's ReviewRoute. Each differed in abort handling, stale semantics and pending
// labels, so a fix landed in one of them — the out-of-order response fences several of them grew —
// never reached the others.
//
// This module is the PURE half: the transitions, with no React and no I/O, so the rules can be
// stated and driven directly by a test. `hooks.js::useScopedResource` is the React half — the
// scope-fenced flight, the abort, the optional poll — and it is the only thing that decides WHEN a
// transition happens.
//
// The state is `{ scope, status, data, error, pending }`:
//   scope   — the identity the data belongs to. A read whose scope no longer matches never commits,
//             and a render for a scope this state does not carry sees the caller's gate status
//             instead of the previous scope's payload (`resourceView`). That fence is the property
//             every variant needed and only some of them had.
//   status  — 'idle' | 'loading' | 'ready' | 'stale' | 'error', plus whatever a caller's gate or
//             failure classifier introduces ('waiting', 'restricted', 'gone').
//   data    — the LAST GOOD payload. It survives a failed refresh; that is the whole point.
//   error   — presentation text, and OPT-IN: it stays '' unless a caller classifies its own
//             failures, because the panels deliberately never render a server message (a 503 body
//             can carry anything, including a credential echoed back in a detail string).
//   pending — the intent of the in-flight read ('load' | 'refresh' | 'retry' | 'reconcile') or null.
//             Status says what the operator may TRUST; pending says what is happening to it. Keeping
//             the two apart is what lets an error stay on screen while its own Retry button reads
//             "Retrying…", instead of blanking the surface the operator is reading back to a spinner.

export const resourceInitial = (scope = null, status = 'idle') =>
  ({ scope, status, data: null, error: '', pending: null })

// A scope that must not be read at all: no node is selected, a summary-only review withholds the
// evidence, an identity is not settled yet. It is a STATE, not an absence — the resource still
// belongs to `scope`, so a late response from the previous scope still cannot commit over it.
export const resourceGated = (scope, status) => resourceInitial(scope, status)

// Begin a read. `intent` is what the operator would call it; `mapLastGood` lets a caller rewrite the
// retained payload as the read starts (Inspector drops a cleared trace so the surface stops showing
// spans the server no longer has, without waiting for the refresh to land).
export function resourceBegin(previous, { scope, intent = 'refresh', mapLastGood = null }) {
  const sameScope = previous.scope === scope
  const loaded = sameScope ? previous.data : null
  const lastGood = typeof mapLastGood === 'function' ? mapLastGood(loaded) : loaded
  // A background refresh over healthy data is invisible on purpose: announcing it would flicker a
  // busy label on every poll tick. An explicit retry, and any read over data already known to be
  // bad, DOES announce itself — those are the ones the operator is waiting on.
  if (lastGood != null && previous.status === 'ready' && intent === 'refresh') return previous
  return {
    scope,
    // Last-good data keeps its own verdict: 'stale' stays stale until a read actually succeeds, so a
    // refresh cannot quietly re-present data the operator was just told not to trust. With nothing
    // to show, a re-read of a scope that already failed keeps 'error' visible rather than blanking
    // to 'loading'; only a scope with no verdict yet is 'loading'.
    status: lastGood == null ? (sameScope && previous.status === 'error' ? 'error' : 'loading')
      : previous.status === 'stale' ? 'stale' : 'ready',
    data: lastGood,
    error: sameScope ? previous.error : '',
    pending: intent,
  }
}

// Settle a read. `classify` is the caller's failure vocabulary: it receives `{ lastGood, ... }` and
// may return `{ status, error, data }` to override any part of the default verdict. It must stay
// pure — this runs inside a React state updater.
export function resourceSettle(previous, { scope, ok, data = null, failure = null, classify = null }) {
  const lastGood = previous.scope === scope ? previous.data : null
  if (ok) return { scope, status: 'ready', data, error: '', pending: null }
  const verdict = (typeof classify === 'function' ? classify({ ...failure, lastGood }) : null) || {}
  return {
    scope,
    // The default verdict IS the finding's shared rule: something to fall back on means 'stale',
    // nothing means 'error'.
    status: verdict.status || (lastGood == null ? 'error' : 'stale'),
    // A classifier that resolves a failure as TERMINAL — a revoked capability, a deleted run —
    // returns its own `data` (usually null), because last-good is no longer something the operator
    // may act on. Everything else keeps it: that is what makes 'stale' different from 'error'.
    data: Object.hasOwn(verdict, 'data') ? verdict.data : lastGood,
    error: verdict.error || '',
    pending: null,
  }
}

// An abort is not a failure: the read was withdrawn, nothing was learned about the resource. Only
// the busy label goes — the status and the last-good payload are exactly as truthful as before.
export function resourceCancel(previous, scope) {
  if (previous.scope !== scope || previous.pending == null) return previous
  return { ...previous, pending: null }
}

// The synchronous scope fence every one of these components wrote by hand. Between a scope change
// and the effect that services it there is one render in which the committed state still carries the
// OLD scope; reading it directly is how a component shows the previous node's details under the new
// node's header.
export const resourceView = (value, scope, status = 'loading') =>
  value.scope === scope ? value : resourceInitial(scope, status)
