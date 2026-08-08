// The byte ceiling is shared by the lazy comment UI and the eager recovery envelope. Keeping this
// dependency below both sides avoids a collaboration-chunk <-> recovery-storage cycle while still
// giving the server contract one browser-side spelling.
export const COMMENT_MAX_BYTES = 8 * 1024
