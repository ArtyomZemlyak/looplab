const CONTROL_RE = /[\u0000-\u001f\u007f]/
const ABSOLUTE_SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i

const normalizedHref = value => {
  if (typeof value !== 'string') return null
  const href = value.trim()
  return !href || href.length > 4096 || CONTROL_RE.test(href) ? null : href
}

// Research/provider payloads are untrusted UI data. Source links are useful only when they are
// ordinary web URLs; credentials, scriptable schemes and oversized/control-bearing values render as
// inert text instead of becoming a navigation surface.
export function safeExternalHref(value) {
  const href = normalizedHref(value)
  if (!href) return null
  try {
    const parsed = new URL(href)
    if ((parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
        || parsed.username || parsed.password) return null
    return href
  } catch { return null }
}

// Markdown may link within the app/document and open an email client in addition to ordinary web
// URLs. Keep that wider policy in this shared trust boundary so credential-bearing HTTP URLs,
// control characters, oversized values, and scriptable schemes cannot regain an active href through
// a second parser.
export function safeMarkdownHref(value) {
  const href = normalizedHref(value)
  if (!href || /^[/\\]{2}/.test(href) || href.includes('\\')) return null

  if (/^https?:/i.test(href)) return safeExternalHref(href)
  if (/^mailto:/i.test(href)) {
    if (/^mailto:\/\//i.test(href) || /%0[ad]/i.test(href)) return null
    try {
      return new URL(href).protocol === 'mailto:' ? href : null
    } catch { return null }
  }
  // Any other explicit scheme is inert. Values without a scheme are document/app-relative links.
  return ABSOLUTE_SCHEME_RE.test(href) ? null : href
}
