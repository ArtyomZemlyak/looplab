import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const hex = value => {
  const text = String(value).trim().replace('#', '')
  const full = text.length === 3 ? [...text].map(char => char + char).join('') : text
  return [0, 2, 4].map(index => Number.parseInt(full.slice(index, index + 2), 16))
}
const mix = (left, leftWeight, right) => left.map((channel, index) =>
  channel * leftWeight + right[index] * (1 - leftWeight))
const luminance = color => {
  const linear = color.map(channel => {
    const value = channel / 255
    return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4
  })
  return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]
}
const contrast = (a, b) => {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (light + .05) / (dark + .05)
}
const variables = block => Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-f]{3,6})\b/gi)]
  .map(match => [match[1], match[2]]))

test('semantic text and filled-control inks retain AA contrast in every selectable theme', async () => {
  const css = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8')
  const base = variables(css.match(/:root\s*\{([\s\S]*?)\}/)?.[1] || '')
  const themes = [{ name: 'current', vars: base }]
  for (const match of css.matchAll(/:root\[data-theme="([^"]+)"\]\s*\{([\s\S]*?)\}/g)) {
    themes.push({ name: match[1], vars: { ...base, ...variables(match[2]) } })
  }
  assert.equal(themes.length, 6)

  const textMixes = [
    ['working', .46], ['ok', .52], ['fail', .52], ['best', .52], ['accent', .62], ['alarm', .48],
  ]
  for (const theme of themes) {
    const foreground = hex(theme.vars.fg)
    for (const [semantic, weight] of textMixes) {
      const ink = mix(hex(theme.vars[semantic]), weight, foreground)
      for (const surface of ['bg', 'bg-1', 'bg-2', 'bg-3']) {
        const ratio = contrast(ink, hex(theme.vars[surface]))
        assert.ok(ratio >= 4.5, `${theme.name} ${semantic}-text on ${surface}: ${ratio.toFixed(2)}:1`)
      }
    }
    for (const [inkName, surfaceName] of [
      ['accent-ink', 'accent-dim'], ['accent-solid-ink', 'accent'], ['fail-solid-ink', 'fail'],
    ]) {
      const ratio = contrast(hex(theme.vars[inkName]), hex(theme.vars[surfaceName]))
      assert.ok(ratio >= 4.5, `${theme.name} ${inkName} on ${surfaceName}: ${ratio.toFixed(2)}:1`)
    }
    for (const [semantic, inkWeight, tintWeight] of [
      ['ok', .52, .10], ['working', .46, .09], ['alarm', .48, .09],
    ]) {
      const ink = mix(hex(theme.vars[semantic]), inkWeight, foreground)
      const tintedSurface = mix(hex(theme.vars[semantic]), tintWeight, hex(theme.vars['bg-1']))
      const ratio = contrast(ink, tintedSurface)
      assert.ok(ratio >= 4.5, `${theme.name} ${semantic}-text on report chip: ${ratio.toFixed(2)}:1`)
    }
  }
})
