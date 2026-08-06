// The primitives every panel module shares. They live here, not in panels.jsx, so that ConfigPanel
// and CardBoard can be their own modules without importing back from the barrel — that import cycle
// is the failure mode this file exists to prevent — and so no panel gets a second, drifting copy of
// a guard. panels.jsx stays the barrel; this is a leaf.
export const invalidPanelPayload = () => { throw new Error('Invalid panel payload') }
export const isRecord = value => !!value && typeof value === 'object' && !Array.isArray(value)
export const nullableText = value => value === null || typeof value === 'string'
export const nullableNumber = value => value === null || (typeof value === 'number' && Number.isFinite(value))

// HTTP 200 proves transport success, not resource truth. Each panel validates its exact envelope
// before replacing last-good data or presenting an authoritative empty state.
export const PANEL_REQUEST_TIMEOUT_MS = 15_000
export const RUN_GENERATION_RE = /^[0-9a-f]{64}$/
