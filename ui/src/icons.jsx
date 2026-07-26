// Monochrome dev/git glyph geometry lives in a versioned SVG sprite. Vite emits it as a hashed local
// asset; the client fetches and injects it once, so OpIcon stays a tiny `<use>` while the XML remains
// cacheable and out of the executable JS parse path.
//
// WebKit/Safari does NOT resolve an EXTERNAL-document `<use href="sprite.svg#id">` (a long-standing
// security restriction), so the previous external reference rendered every icon BLANK on Safari/iOS.
// Fetching and injecting the symbols before referencing `<use href="#id">` keeps the final reference
// same-document and works in every engine, including WebKit.
import spriteUrl from './looplab-icons-v1.svg?url&no-inline'

const OP_ICON_NAMES = new Set(
  'flag trending bug confluence gitbranch target dot search doc alert gear user bot bolt star pause play stop replay sliders chevron-up chevron-down chat bell folder clip map compass bulb check cross pencil link download printer crown list'.split(' '),
)
const SPRITE_DOM_ID = 'looplab-icon-sprite'
let spriteLoad = null

// Inject the trusted bundled symbol defs exactly once. SSR never starts a browser request; production
// failures remain contained (buttons retain their accessible names) and a later call can retry.
function ensureSprite() {
  if (import.meta.env.SSR || typeof document === 'undefined' || !document.body
      || document.getElementById(SPRITE_DOM_ID) || spriteLoad) return
  if (typeof fetch !== 'function') return
  spriteLoad = fetch(spriteUrl, { credentials: 'same-origin' })
    .then(response => {
      if (!response.ok) throw new Error('icon sprite unavailable')
      return response.text()
    })
    .then(markup => {
      if (!/^<svg[\s>]/.test(markup)
          || /<(?:script|foreignObject|image|use)\b/i.test(markup)) {
        throw new Error('invalid icon sprite')
      }
      if (document.getElementById(SPRITE_DOM_ID)) return
      const host = document.createElement('div')
      host.id = SPRITE_DOM_ID
      host.setAttribute('aria-hidden', 'true')
      host.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden'
      host.innerHTML = markup
      document.body.prepend(host)
    })
    .catch(() => { spriteLoad = null })
}

if (typeof document !== 'undefined') {
  if (document.body) ensureSprite()
  // Module may evaluate before <body> exists (non-deferred include); catch up on DOM ready.
  else document.addEventListener('DOMContentLoaded', ensureSprite, { once: true })
}

export function OpIcon({ name, size = 14, className }) {
  ensureSprite()   // idempotent safety net: guarantees the defs exist before the first icon paints
  const glyph = OP_ICON_NAMES.has(name) ? name : 'dot'
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 16 16" fill="none"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true" focusable="false">
      <use href={`#${glyph}`} />
    </svg>
  )
}
