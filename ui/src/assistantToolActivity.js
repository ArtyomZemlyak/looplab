// The assistant's TOOL ACTIVITY strip — its window, its truncation, and its numbering.
//
// A pure model beside its React half (the house pattern), because these are DECISIONS rather than
// choreography. `assistantToolActivityPresentation.test.js` does mount the component through jsdom,
// so the rendered result was reachable — but only through a DOM, which is why the numbering defect
// below survived: the one topology that exposes it (an unlabelled payload inside the window) is
// awkward to reach that way and trivial to state here. The presentation test still owns the
// rendering; this owns the rules.

export const TOOL_ACTIVITY_ITEM_MAX = 40
export const TOOL_ACTIVITY_LABEL_MAX = 240

/** One tool label, bounded and whitespace-collapsed. Tool payloads carry arguments and results;
 *  this reads a label and nothing else, so a long payload cannot become unbounded DOM.
 *
 *  The trailing-high-surrogate trim is not cosmetic: slicing a JS string at a fixed length can cut
 *  an astral character in half, and a lone surrogate is not valid text to hand a renderer. */
export const boundedToolText = value => {
  const clipped = String(value ?? '').slice(0, TOOL_ACTIVITY_LABEL_MAX)
  const surrogateSafe = /[\uD800-\uDBFF]$/.test(clipped) ? clipped.slice(0, -1) : clipped
  return surrogateSafe.replace(/\s+/g, ' ').trim()
}

/** The display label for one activity item, or '' when it carries none. */
export const toolActivityLabel = item => {
  if (typeof item === 'string') return boundedToolText(item)
  if (!item || typeof item !== 'object' || Array.isArray(item)) return ''
  const label = typeof item.label === 'string' ? boundedToolText(item.label) : ''
  return label || (typeof item.tool === 'string' ? boundedToolText(item.tool) : '')
}

/**
 * `{steps, total, limited}` for a list of activity items.
 *
 * `steps` carries each shown label WITH ITS TRUE 1-BASED POSITION in the full list, and that pairing
 * is the point. The projection skips items whose label resolves to '' but still counts them in
 * `total`, so the number of labels is NOT the size of the window — and the old render derived its
 * first ordinal as `total - labels.length + 1`, which is only correct when every windowed item
 * produced a label. Any unlabelled payload in the window shifted every ordinal, and the
 * "Showing the latest N of M steps" line disagreed with the numbers underneath it.
 *
 * Carrying the position removes the assumption instead of patching the arithmetic: the list can set
 * an explicit `value` per item, which is exactly what HTML ordered lists provide for a
 * non-contiguous sequence.
 */
export const toolActivityProjection = items => {
  if (!Array.isArray(items) || items.length === 0) return { steps: [], total: 0, limited: false }
  const start = Math.max(0, items.length - TOOL_ACTIVITY_ITEM_MAX)
  const steps = []
  for (let index = start; index < items.length; index += 1) {
    const label = toolActivityLabel(items[index])
    if (label) steps.push({ position: index + 1, label })
  }
  return { steps, total: items.length, limited: start > 0 }
}
