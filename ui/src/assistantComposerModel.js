// The assistant composer's pure rules (review 2026-09-22, UI-06): which `#N` tokens name an
// experiment, when a draft belongs to the open run, the run-context footer a turn carries, which
// files may be attached and how large, and the shape of one composer draft. Moved verbatim out of
// `AssistantBar.jsx`, where they were module-private; `test/assistantComposerModel.test.js` drives
// them. Pure apart from reading the shared mode table.
import { ASSISTANT_MODES as MODES } from './util.js'

// `#N` must start a token (not follow a word/# char) and end at a boundary — so `#3498db` (hex color),
// URL fragments (`page#12`), and `x#5` don't fabricate an experiment reference.
export const refNodes = (t) => [...new Set([...(t || '').matchAll(/(?<![\w#])#(?:node-)?(\d+)\b/gi)].map(m => Number(m[1])))]
export const NEW_RUN_DRAFT_RE = /^\/(?:new|genesis|run)\b/i
export const composerUsesRun = (input, files = [], pendingFileReads = 0) => {
  const text = String(input || '').trim()
  return files.length > 0 || pendingFileReads > 0
    || (!NEW_RUN_DRAFT_RE.test(text) && text.length > 0)
}
export const composerRunKey = runId => runId == null ? '' : String(runId)
export const uiRunContext = (runId, refs) => {
  if (!runId) return ''
  const safe = String(runId).replace(/[\]"\r\n]/g, ' ').slice(0, 200)
  const nodes = refs.length
    ? ` The user refers to ${refs.map(id => '#' + id).join(', ')}; read them with run tools.` : ''
  return `\n\n[UI context: run "${safe}" is open.${nodes} Use run tools if relevant.]`
}

// Attach only text-ish files we can read as plain text (no special parsing). Cap each file so a huge
// paste doesn't blow the context; the backend receives the content inline in the instruction.
export const TEXT_EXT = /\.(txt|md|markdown|csv|tsv|json|jsonl|ya?ml|toml|ini|cfg|conf|log|py|js|jsx|ts|tsx|sh|c|cpp|h|hpp|java|go|rs|rb|sql|html|css|xml|env)$/i
export const FILE_CHAR_CAP = 20000
export const MAX_FILE_BYTES = 2 * 1024 * 1024   // never readAsText a giant log/csv into the tab (OOM)
export const SECRET_RE = /(^|\/)\.env(\.|$)|\.pem$|\.key$|(^|\/)(id_rsa|id_ed25519)$|secret|credential/i
export const NEW_CHAT_COMPOSER_KEY = '__new__'
export const normalizeComposerMode = value => (
  MODES.some(candidate => candidate.id === value) ? value : 'plan'
)
export const newComposerDraft = (mode = 'plan') => ({
  input: '', files: [], pendingFileReads: 0, runScope: null, mode: normalizeComposerMode(mode),
})
