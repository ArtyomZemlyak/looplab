import React from 'react'
import { BrandMark } from './GlobalMenu.jsx'

// Doc 25 UI-03. The six non-workspace screens of the run route — two Start-over recovery states,
// the run-resource state, the two stale-generation-fence states and the loading snapshot — each
// re-declared this page shell. They were NOT identical, and the two axes that varied are kept as
// explicit props rather than defaulted, because each variation is a reachability statement:
//
//   * `reviewMode` adds the class that hides the head's pills and the live badge at ≤600px
//     (styles.css `.review-mode .run-head>.pill,.review-mode .run-head>.live`).
//   * `reviewPill` is the read-only stand-in shown where the "← runs" control cannot be: `onBack`
//     is null exactly when RunView is mounted for a review capability (App.jsx passes both).
//
// The three Start-over screens pass NEITHER, and that is correct rather than an oversight:
// `useStartOverRecovery` pins the recovery state to `{ kind: 'none' }` whenever `reviewMode` is
// set, so no Start-over screen can render for a reviewer. The history screen passes `reviewPill`
// but not `reviewMode` — an inconsistency preserved verbatim, because `sanitizeRunRouteState`
// drops `sequence` in review mode, so that screen cannot render for a reviewer either. Defaulting
// either prop would silently change markup on a path nobody can reach and quietly retire the
// invariant that makes it unreachable.
export default function RunScreen({
  reviewMode = false, reviewPill = false, onBack, onLeave, head = null, toast = null, children,
}) {
  return <div className={'app' + (reviewMode ? ' review-mode' : '')}>
    <div className="topbar run-head">
      {/* The INERT mark, not the menu trigger, and that is a reachability statement like the two
          props above: three of these six screens pass no `reviewMode` at all (the note above says
          why), so this shell cannot tell an owner from a reviewer. A trigger here would be a menu
          offered to a public review capability on exactly the screens that render when a run cannot.
          The mark comes from GlobalMenu.jsx so the workspace header and these six screens keep
          rendering the same LoopLab mark rather than two copies that drift. */}
      <BrandMark />
      {onBack ? <button className="btn sm ghost" onClick={onLeave}>← runs</button>
        : reviewPill ? <span className="pill">read-only review</span> : null}
      {head}
    </div>
    {children}
    {toast && <div className="toast" role="status" aria-live="polite" aria-atomic="true">{toast}</div>}
  </div>
}
