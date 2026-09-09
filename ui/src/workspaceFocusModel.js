// Doc 25 UI-03's other named residue: the `workspaceFocusOwnerRef` SWITCHYARD in RunView.jsx — a
// `focusin` listener that classified whatever the operator last focused, and a breakpoint-crossing
// effect that chose where to put focus back when the compact/desktop layouts swap.
//
// It is a switchyard because both halves are truth tables wearing an if/else chain, and neither is
// reachable from a test without rendering the whole run route and resizing it. Focus is not cosmetic
// here: crossing the breakpoint UNMOUNTS the surface the operator was on (the compact inspector
// sheet, the desktop side rail, a splitter), and if nothing catches focus it falls to `document.body`
// — a keyboard operator loses their place mid-run, with no visible cause. Both tables are pure, so
// they belong here; the listener, the refs and the `requestAnimationFrame` stay in
// `useWorkspaceFocusOwner.js`.

// The workspace surfaces focus can belong to. `null` is a real answer and means "somewhere the
// swap does not disturb" — the canvas, a panel, the topbar — for which the effect does nothing.
export const WORKSPACE_FOCUS_OWNERS = Object.freeze([
  'compact-inspector-trigger',
  'desktop-side-rail',
  'inspector-surface',
  'side-splitter',
  'timeline-splitter',
  'timeline-body',
  'timeline-collapse',
])

/**
 * Which workspace surface the focused element belongs to.
 *
 * `surfaces` are the live DOM nodes (or null) for the four containers RunView holds refs to. The
 * order below is the original order and is load-bearing where the regions nest: the compact
 * inspector CONTAINS the scrim and the splitters visually, so `.workspace-scrim` / `.splitter.*` are
 * tested before `compactInspector.contains(target)`, and the trigger is tested before everything
 * because it sits inside the toolbar rather than inside the sheet it opens.
 */
export function workspaceFocusOwner(target, surfaces = {}) {
  const { compactInspectorTrigger, sideRail, compactInspector, timelineCollapse } = surfaces
  if (!target) return null
  if (compactInspectorTrigger?.contains(target)) return 'compact-inspector-trigger'
  if (sideRail?.contains(target)) return 'desktop-side-rail'
  if (target.closest?.('.workspace-scrim')) return 'inspector-surface'
  if (target.closest?.('.splitter.v')) return 'side-splitter'
  if (target.closest?.('.splitter.h')) return 'timeline-splitter'
  if (compactInspector?.contains(target)) return 'inspector-surface'
  if (target.closest?.('#run-events-timeline')) return 'timeline-body'
  if (timelineCollapse?.contains(target)) return 'timeline-collapse'
  return null
}

/**
 * Where focus goes when the workspace crosses the compact/desktop breakpoint, as an ORDERED list of
 * surface slots: the first one that exists on the new layout takes focus, and an empty list means
 * "leave focus alone".
 *
 * A list rather than one slot because two of the branches always had a fallback — the compact
 * inspector's close button may not be mounted yet (its sheet is), and the compact trigger may be
 * absent on a narrow layout that has a selected node instead. Naming the slots keeps the choice
 * testable without any DOM: the hook is the only thing that knows which ref holds which element.
 *
 * `wasCompact` is the layout being LEFT, which is what decides the direction. The timeline branch is
 * direction-independent on purpose: a splitter and a collapsed timeline body have no counterpart on
 * either side of the swap, so the collapse control is where focus belongs in both directions.
 */
export function workspaceRefocusSlots({ focusOwner, wasCompact, sideC, timelineCollapsed }) {
  if (focusOwner === 'timeline-splitter'
      || (focusOwner === 'timeline-body' && timelineCollapsed)) {
    return ['timeline-collapse']
  }
  if (wasCompact && ['compact-inspector-trigger', 'inspector-surface'].includes(focusOwner)) {
    return sideC ? ['side-rail'] : ['compact-inspector-close', 'compact-inspector']
  }
  if (!wasCompact
      && ['desktop-side-rail', 'inspector-surface', 'side-splitter'].includes(focusOwner)) {
    return ['compact-inspector-trigger', 'selected-node']
  }
  return []
}
