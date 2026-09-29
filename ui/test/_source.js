import { readFileSync as readFileSyncRaw } from 'node:fs'
import { readFile as readFileAsync } from 'node:fs/promises'

// Source assertions describe JavaScript lines, independent of the checkout's line endings.
// Git may write CRLF on Windows even when the committed file uses LF.
const lf = source => source.replace(/\r\n?/g, '\n')

export const readSource = async (path, encoding = 'utf8') =>
  lf(await readFileAsync(path, encoding))

export const readSourceSync = (path, encoding = 'utf8') =>
  lf(readFileSyncRaw(path, encoding))
