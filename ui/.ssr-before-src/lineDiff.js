const MAX_LCS_CELLS = 2_000_000

function entry(line, kind, oldNo, newNo) {
  return { line, l: line, kind, cls: kind === 'add' ? 'diff-add' : kind === 'del' ? 'diff-del' : '', oldNo, newNo }
}

// Ordered, duplicate-preserving line diff. The prior Set-based implementation erased duplicates and
// appended every deletion at the end, visually relocating changes. LCS keeps both source positions.
export function diffLines(before = '', after = '') {
  const a = String(before).split('\n'), b = String(after).split('\n')
  const cells = (a.length + 1) * (b.length + 1)
  if (cells > MAX_LCS_CELLS) return coarseOrderedDiff(a, b)

  const dp = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1))
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out = []; let i = 0, j = 0
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      out.push(entry(a[i], 'same', i + 1, j + 1)); i++; j++
    } else if (j < b.length && (i >= a.length || dp[i][j + 1] > dp[i + 1][j])) {
      out.push(entry(b[j], 'add', null, j + 1)); j++
    } else {
      out.push(entry(a[i], 'del', i + 1, null)); i++
    }
  }
  return out
}

function coarseOrderedDiff(a, b) {
  let prefix = 0
  while (prefix < a.length && prefix < b.length && a[prefix] === b[prefix]) prefix++
  let suffix = 0
  while (suffix < a.length - prefix && suffix < b.length - prefix &&
         a[a.length - 1 - suffix] === b[b.length - 1 - suffix]) suffix++
  const out = []
  for (let i = 0; i < prefix; i++) out.push(entry(a[i], 'same', i + 1, i + 1))
  for (let i = prefix; i < a.length - suffix; i++) out.push(entry(a[i], 'del', i + 1, null))
  for (let j = prefix; j < b.length - suffix; j++) out.push(entry(b[j], 'add', null, j + 1))
  for (let offset = suffix; offset > 0; offset--) {
    const i = a.length - offset, j = b.length - offset
    out.push(entry(a[i], 'same', i + 1, j + 1))
  }
  return out
}
