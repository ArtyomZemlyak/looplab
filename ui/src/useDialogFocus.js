import { useEffect, useRef } from 'react'

const dialogStack = []

export function useDialogFocus(ref, onClose, active = true, { modal = true } = {}) {
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    if (!active) return
    const previous = document.activeElement
    const layer = { ref, modal }
    dialogStack.push(layer)
    // True modals always outrank coexisting nonmodal surfaces, even when a route update mounts the
    // latter after the modal. Among peers, the most recently mounted layer still wins.
    const topmost = () => {
      const modalLayers = dialogStack.filter(candidate => candidate.modal)
      const topLayer = modalLayers.length ? modalLayers[modalLayers.length - 1] : dialogStack[dialogStack.length - 1]
      return topLayer === layer
    }
    const root = ref.current
    let focusWasInside = !!root?.contains(document.activeElement)
    const selector = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [contenteditable="true"], [tabindex]:not([tabindex="-1"])'
    // React's autoFocus runs during commit. Preserve it; otherwise focus the first real action, falling
    // back to the dialog container only when the dialog genuinely has no controls.
    const blockingModal = !modal && [...document.querySelectorAll('[aria-modal="true"]')]
      .some(surface => surface !== root && !root?.contains(surface))
    if (root && !blockingModal && !root.contains(document.activeElement)) {
      const initial = root.querySelector('[autofocus], [data-dialog-initial-focus]') || root.querySelector(selector)
      ;(initial || root).focus()
      // The tracking listener is attached below, so record our own programmatic initial focus now.
      focusWasInside = root.contains(document.activeElement)
    }
    const onKey = (event) => {
      if (!topmost()) return
      if (event.defaultPrevented) return
      if (event.key === 'Escape') {
        // A nonmodal surface must not consume Escape from an unrelated header/timeline control.
        if (!modal && !ref.current?.contains(document.activeElement)) return
        event.preventDefault(); closeRef.current?.(); return
      }
      if (!modal || event.key !== 'Tab' || !ref.current) return
      const focusable = [...ref.current.querySelectorAll(selector)]
      if (!focusable.length) { event.preventDefault(); ref.current.focus(); return }
      const first = focusable[0], last = focusable[focusable.length - 1]
      if (!ref.current.contains(document.activeElement)) {
        event.preventDefault(); (event.shiftKey ? last : first).focus()
      }
      else if (document.activeElement === ref.current) {
        event.preventDefault(); (event.shiftKey ? last : first).focus()
      }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    const onFocusIn = event => {
      // Preserve the last meaningful location if removing the focused surface makes the browser
      // focus body after the node has already disconnected.
      if (root?.isConnected) focusWasInside = root.contains(event.target)
      if (!modal) return
      if (!topmost() || !ref.current || ref.current.contains(event.target)) return
      const initial = ref.current.querySelector(selector) || ref.current
      initial.focus({ preventScroll: true })
    }
    window.addEventListener('keydown', onKey)
    document.addEventListener('focusin', onFocusIn)
    return () => {
      window.removeEventListener('keydown', onKey)
      document.removeEventListener('focusin', onFocusIn)
      const wasTopmost = topmost()
      const index = dialogStack.indexOf(layer)
      if (index >= 0) dialogStack.splice(index, 1)
      if (wasTopmost && previous && document.contains(previous) && (modal || focusWasInside)) {
        previous.focus({ preventScroll: true })
      }
    }
  }, [active, modal])
}
