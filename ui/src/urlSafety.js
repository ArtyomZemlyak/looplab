const CONTROL_RE = /[\u0000-\u001f\u007f]/

// Research/provider payloads are untrusted UI data. Source links are useful only when they are
// ordinary web URLs; credentials, scriptable schemes and oversized/control-bearing values render as
// inert text instead of becoming a navigation surface.
export function safeExternalHref(value) {
  if (typeof value !== 'string') return null
  const href = value.trim()
  if (!href || href.length > 4096 || CONTROL_RE.test(href)) return null
  try {
    const parsed = new URL(href)
    if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
        || parsed.username || parsed.password) return null
    return href
  } catch { return null }
}
