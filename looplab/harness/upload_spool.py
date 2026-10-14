"""The MCP client's SPOOL of per-query upload bodies (doc 81 §8 leftover, 2026-10-14).

WHY. `upload_per_query` sends up to ~32 MB in one request, past the client request store's 1 MiB
records, so its only recovery advice was "keep the file and the action_id to retry exactly". An agent
whose turn died after the POST (or whose `/tmp` copy of the analysis `.npz` was cleaned) could not
tell whether LoopLab had the upload and could not resend it. The server is already idempotent on
`action_id` (an exact retry returns the original receipt, a different body under the same id is a
409); what was missing is the client half: the exact BYTES of the request, kept until a definite
answer, so a resume needs nothing but `(run_id, action_id)`.

WHAT. Before the POST the body is written to `<request_dir>/uploads/<server>/<key>.body` (0600,
atomically) with a small `<key>.json` meta; a definite answer (any HTTP status below 500) settles the
entry — the body is deleted and the meta keeps the status as a receipt; a transport error or a 5xx
leaves it pending for `resume`. Bounded: a body over `MAX_BODY_BYTES` or a spool over
`MAX_SPOOL_BYTES` is not spooled (the upload still proceeds, and the response says it was not).
Private to this client, like the request store: nothing here is a server receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

from looplab.core.atomicio import atomic_write_bytes

MAX_BODY_BYTES = 40 * 1024 * 1024
MAX_SPOOL_BYTES = 512 * 1024 * 1024
MAX_SETTLED_RECEIPTS = 500


def _key(run_id: str, action_id: str) -> str:
    return hashlib.sha256(f"{run_id}\0{action_id}".encode("utf-8")).hexdigest()[:40]


class UploadSpool:
    def __init__(self, root, server_url: str):
        scope = hashlib.sha256(server_url.rstrip("/").encode("utf-8")).hexdigest()[:16]
        self.dir = Path(root).expanduser().resolve() / "uploads" / scope

    def _paths(self, run_id: str, action_id: str) -> tuple[Path, Path]:
        key = _key(run_id, action_id)
        return self.dir / f"{key}.json", self.dir / f"{key}.body"

    def _spooled_bytes(self) -> int:
        try:
            return sum(p.stat().st_size for p in self.dir.glob("*.body"))
        except OSError:
            return 0

    def save(self, run_id: str, action_id: str, body: bytes, *, source: str = "") -> Optional[str]:
        """Spool `body` before it is sent; returns None, or why it was NOT spooled."""
        if len(body) > MAX_BODY_BYTES:
            return f"body over {MAX_BODY_BYTES} bytes"
        if self._spooled_bytes() + len(body) > MAX_SPOOL_BYTES:
            return f"spool over {MAX_SPOOL_BYTES} bytes; resume or discard pending uploads"
        meta, data = self._paths(run_id, action_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        atomic_write_bytes(data, body, mode=0o600)
        atomic_write_bytes(meta, json.dumps({
            "version": 1, "run_id": run_id, "action_id": action_id, "status": "pending",
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "source": source[:500], "created_at": time.time()}).encode(), mode=0o600)
        return None

    def load(self, run_id: str, action_id: str) -> Optional[bytes]:
        """The spooled body if it is still pending and intact, else None."""
        meta, data = self._paths(run_id, action_id)
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            body = data.read_bytes()
        except (OSError, ValueError):
            return None
        if info.get("status") != "pending" or hashlib.sha256(body).hexdigest() != info.get("sha256"):
            return None
        return body

    def entry(self, run_id: str, action_id: str) -> Optional[dict]:
        meta, _data = self._paths(run_id, action_id)
        try:
            return json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def settle(self, run_id: str, action_id: str, *, http_status) -> None:
        """A definite answer: drop the body, keep a small receipt."""
        meta, data = self._paths(run_id, action_id)
        info = self.entry(run_id, action_id) or {"run_id": run_id, "action_id": action_id}
        info.update(status="settled", http_status=http_status, settled_at=time.time())
        try:
            data.unlink()
        except OSError:
            pass
        try:
            atomic_write_bytes(meta, json.dumps(info).encode(), mode=0o600)
        except OSError:
            return
        self._prune_receipts()

    def _prune_receipts(self) -> None:
        try:
            metas = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        settled = []
        for path in metas:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("status") == "settled":
                    settled.append(path)
            except (OSError, ValueError):
                continue
        for path in settled[:-MAX_SETTLED_RECEIPTS]:
            try:
                path.unlink()
            except OSError:
                pass

    def pending(self, run_id: Optional[str] = None) -> list[dict]:
        out = []
        for path in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else ():
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if info.get("status") == "pending" and (run_id is None or info.get("run_id") == run_id):
                out.append({k: info.get(k) for k in ("run_id", "action_id", "bytes", "source",
                                                     "created_at")})
        return out

    def discard(self, run_id: str, action_id: str) -> bool:
        meta, data = self._paths(run_id, action_id)
        found = False
        for path in (data, meta):
            try:
                path.unlink()
                found = True
            except OSError:
                pass
        return found
