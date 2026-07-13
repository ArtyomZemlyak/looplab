import { useEffect, useRef } from 'react'

const dialogStack = []

export function useDialogFocus(ref, onClose, active = true) {
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    if (!active) return
    const previous = document.activeElement
    const layer = { ref }
    dialogStack.push(layer)
    const topmost = () => dialogStack[dialogStack.length - 1] === layer
    const root = ref.current
    const selector = 'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [contenteditable="true"], [tabindex]:not([tabindex="-1"])'
    // React's autoFocus runs during commit. Preserve it; otherwise focus the first real action, falling
    // back to the dialog container only when the dialog genuinely has no controls.
    if (root && !root.contains(document.activeElement)) {
      const initial = root.querySelector('[autofocus], [data-dialog-initial-focus]') || root.querySelector(selector)
      ;(initial || root).focus()
    }
    const onKey = (event) => {
      if (!topmost()) return
      if (event.defaultPrevented) return
      if (event.key === 'Escape') { event.preventDefault(); closeRef.current?.(); return }
      if (event.key !== 'Tab' || !ref.current) return
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
      if (wasTopmost && previous && document.contains(previous)) previous.focus({ preventScroll: true })
    }
  }, [active])
}
