// How to show one artifact (doc 77 §3) — a pure decision, so the panel keeps only the fetching.
// Text the server already returned is shaped here (JSON pretty-printed, CSV/TSV as a bounded table);
// a binary raster image is fetched as bytes through `/artifact/raw`; any other binary gets its
// metadata (`/artifact/meta`: size, sha256, safetensors/zip/npy headers) — never executed.
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp'])
export const CSV_MAX_ROWS = 200
export const CSV_MAX_COLS = 32

export const extensionOf = path => {
  const name = String(path || '').split('/').pop() || ''
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : ''
}

export function previewKind(path, content) {
  const ext = extensionOf(path)
  // A PDF is shown by the browser's own viewer from bytes `/artifact/raw` verified (suffix + magic),
  // whether or not its body happened to contain a NUL (UI review 2026-10-14 section 7.4).
  if (ext === 'pdf' && content && (content.is_text === false || typeof content.content === 'string')) return 'pdf'
  if (content && content.is_text === false) return IMAGE_EXT.has(ext) ? 'image' : 'meta'
  if (!content || typeof content.content !== 'string') return 'none'
  if (ext === 'json' || ext === 'jsonl') return ext
  if (ext === 'csv' || ext === 'tsv') return 'table'
  return 'text'
}

// RFC 4180-ish: quoted fields, doubled quotes, embedded separators/newlines. Bounded rows and columns.
export function parseDelimited(text, separator = ',') {
  const rows = []
  let row = [], field = '', quoted = false, truncated = false
  const source = String(text || '')
  for (let i = 0; i < source.length; i++) {
    const ch = source[i]
    if (quoted) {
      if (ch === '"' && source[i + 1] === '"') { field += '"'; i++ }
      else if (ch === '"') quoted = false
      else field += ch
      continue
    }
    if (ch === '"' && field === '') quoted = true
    else if (ch === separator) { row.push(field); field = '' }
    else if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && source[i + 1] === '\n') i++
      row.push(field); field = ''
      if (row.length > 1 || row[0] !== '') rows.push(row)
      row = []
      if (rows.length > CSV_MAX_ROWS) { truncated = true; break }
    } else field += ch
  }
  if (!truncated && (field !== '' || row.length)) { row.push(field); rows.push(row) }
  const width = Math.min(CSV_MAX_COLS, Math.max(0, ...rows.map(r => r.length)))
  if (rows.some(r => r.length > CSV_MAX_COLS)) truncated = true
  const shaped = rows.slice(0, CSV_MAX_ROWS + 1).map(r => Array.from({ length: width }, (_, c) => r[c] ?? ''))
  return { header: shaped[0] || [], rows: shaped.slice(1, CSV_MAX_ROWS + 1), truncated: truncated || rows.length > CSV_MAX_ROWS + 1 }
}

export function prettyJson(text, jsonl = false) {
  try {
    if (!jsonl) return JSON.stringify(JSON.parse(text), null, 2)
    return String(text).split('\n').filter(Boolean).slice(0, 500)
      .map(line => JSON.stringify(JSON.parse(line), null, 2)).join('\n')
  } catch { return null }
}
