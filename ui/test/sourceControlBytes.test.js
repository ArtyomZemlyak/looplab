// NO RAW CONTROL BYTES IN THE UI TREE (review 2026-09-22, UI-09).
//
// `crossRunRank.js` and `conceptForest.js` each spelled a NUL key separator as a LITERAL 0x00 byte
// inside a string. The byte is legal JavaScript and runs identically to `\u0000`, but it makes grep
// and ripgrep classify the whole file as BINARY: `grep -rn 'OPEN\['` — the repo's own recipe for
// "what is still open?" (CLAUDE.md, the open-item index) — printed "binary file matches" or nothing
// for both files, so any marker or citation in them was invisible to every text search. The escape is
// the same string at runtime; the raw byte is only ever a hazard.
//
// The sweep covers every file the tree ships or tests (src, test, scripts, index.html), not just the
// two that had it, because the failure is silent where it lands: nothing else notices a file that
// greps as binary. Tab, LF and CR are the three C0 bytes a text file legitimately carries.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readdir, readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { join, relative } from 'node:path'

const UI_ROOT = fileURLToPath(new URL('..', import.meta.url))
const ALLOWED = new Set([0x09, 0x0a, 0x0d])

// Every C0 control byte (0x00-0x1F) except tab/LF/CR, as `line:column (0xNN)` receipts.
function controlBytes(bytes) {
  const found = []
  let line = 1
  let lineStart = 0
  for (let i = 0; i < bytes.length; i += 1) {
    const byte = bytes[i]
    if (byte === 0x0a) { line += 1; lineStart = i + 1; continue }
    if (byte < 0x20 && !ALLOWED.has(byte)) {
      found.push(`${line}:${i - lineStart + 1} (0x${byte.toString(16).padStart(2, '0')})`)
    }
  }
  return found
}

const treeFiles = async () => {
  const files = [join(UI_ROOT, 'index.html')]
  for (const dir of ['src', 'test', 'scripts']) {
    const entries = await readdir(join(UI_ROOT, dir), { withFileTypes: true, recursive: true })
    for (const entry of entries) {
      if (entry.isFile()) files.push(join(entry.parentPath ?? entry.path, entry.name))
    }
  }
  return files.sort()
}

test('the scanner reports a raw NUL and passes the three text controls', () => {
  // Anti-vacuity: the sweep below is only evidence if the scanner can see the defect it guards.
  const sample = Buffer.from('const key = `a\u0000b`\n\tok\r\n\u001b[0m', 'utf8')
  assert.deepEqual(controlBytes(sample), ['1:15 (0x00)', '3:1 (0x1b)'])
  assert.deepEqual(controlBytes(Buffer.from('tab\tlf\ncr\r\n', 'utf8')), [])
})

test('no file in the UI tree carries a raw C0 control byte', async () => {
  const files = await treeFiles()
  assert.ok(files.length > 300, `the sweep found only ${files.length} files — it must see the tree`)
  const offenders = []
  for (const file of files) {
    const hits = controlBytes(await readFile(file))
    if (hits.length) offenders.push(`${relative(UI_ROOT, file)}: ${hits.join(', ')}`)
  }
  assert.deepEqual(offenders, [],
    'write the escape (`\\u0000`, `\\x1b`, …) instead of the byte — a raw control byte makes grep and '
    + `ripgrep treat the whole file as binary:\n${offenders.join('\n')}`)
})
