import { useEffect, useRef } from 'react'

import { workspaceFocusOwner, workspaceRefocusSlots } from './workspaceFocusModel.js'

// The React half of the workspace focus switchyard (doc 25 UI-03). The two truth tables live in
// `workspaceFocusModel.js`; what is left here is exactly the choreography a pure module cannot own:
// the `focusin` subscription, the ref that remembers the last owner across a layout swap, and the
// `requestAnimationFrame` that waits for the replacement layout to mount before focusing into it.

// The bag RunView hands in holds REFS, and is dereferenced here rather than by the caller: the
// elements it names are precisely the ones the breakpoint swap replaces, so anything captured before
// the swap commits is the layout that just unmounted.
const live = surfaces => Object.fromEntries(
  Object.entries(surfaces || {}).map(([name, ref]) => [name, ref?.current || null]))

/**
 * Remember which workspace surface has focus, and restore focus after a compact/desktop swap.
 *
 * `surfaces` is a stable ref holding `{name: elementRef}`; it is read at EVENT time and again at
 * frame time, never captured. `onCrossed` is called once per crossing with the layout that was
 * left — RunView uses it to drop the compact overlays, which are temporary by design and must never
 * be replayed across the breakpoint.
 */
export function useWorkspaceFocusOwner({ surfaces, compactWorkspace, overlayPanelOpen, sideC,
                                         timelineCollapsed, selectedGroup, selectedId,
                                         onCrossed }) {
  const focusOwnerRef = useRef(null)
  const previousCompactRef = useRef(compactWorkspace)
  useEffect(() => {
    const rememberWorkspaceFocus = event => {
      focusOwnerRef.current = workspaceFocusOwner(event.target, live(surfaces.current))
    }
    document.addEventListener('focusin', rememberWorkspaceFocus)
    return () => document.removeEventListener('focusin', rememberWorkspaceFocus)
  }, [surfaces])
  useEffect(() => {
    if (previousCompactRef.current === compactWorkspace) return
    const wasCompact = previousCompactRef.current
    previousCompactRef.current = compactWorkspace
    const focusOwner = focusOwnerRef.current
    requestAnimationFrame(() => {
      if (overlayPanelOpen || document.querySelector('[aria-modal="true"]')) return
      const selectedNode = [...(document.querySelectorAll('[data-node-select-id]') || [])]
        .find(element => element.dataset.nodeSelectId === String(selectedId))
      const mounted = live(surfaces.current)
      const elements = {
        'timeline-collapse': mounted.timelineCollapse,
        'side-rail': mounted.sideRail,
        'compact-inspector-close': mounted.compactInspectorClose,
        'compact-inspector': mounted.compactInspector,
        'compact-inspector-trigger': mounted.compactInspectorTrigger,
        'selected-node': selectedNode,
      }
      const slots = workspaceRefocusSlots({ focusOwner, wasCompact, sideC, timelineCollapsed })
      const target = slots.map(slot => elements[slot]).find(Boolean) || null
      target?.focus({ preventScroll: true })
    })
    // Compact surfaces are temporary. Never replay stale open state after crossing the breakpoint.
    onCrossed?.(wasCompact)
    // The dependency list is the one this effect had inline in RunView, unchanged: `selectedGroup`
    // is in it although nothing here reads it, because a group change re-renders the workspace and
    // this effect is what re-places focus afterwards.
  }, [compactWorkspace, overlayPanelOpen, selectedGroup, selectedId, sideC, timelineCollapsed])
  return focusOwnerRef
}
