// Monochrome dev/git glyph geometry lives in a versioned, cacheable SVG sprite. Keeping paths out
// of JavaScript removes parse/execute work while preserving the existing OpIcon API and currentColor.
const OP_ICON_NAMES = new Set(
  'flag trending bug confluence gitbranch target dot search doc alert gear user bot bolt star pause play stop replay sliders chevron-up chevron-down chat bell folder clip map compass bulb check cross pencil link download printer crown list'.split(' '),
)
const SPRITE_URL = './looplab-icons-v1.svg'

export function OpIcon({ name, size = 14, className }) {
  const glyph = OP_ICON_NAMES.has(name) ? name : 'dot'
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 16 16" fill="none"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true" focusable="false">
      <use href={`${SPRITE_URL}#${glyph}`} />
    </svg>
  )
}
