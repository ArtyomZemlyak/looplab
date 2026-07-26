import { rm } from 'node:fs/promises'
import { dirname, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const uiRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dist = resolve(uiRoot, 'dist')

// Keep the destructive target explicit and local even if this script is invoked from another cwd.
if (relative(uiRoot, dist) !== 'dist') {
  throw new Error(`refusing to clean an unexpected output directory: ${dist}`)
}

await rm(dist, { recursive: true, force: true })
