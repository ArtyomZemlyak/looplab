"""The same durable identity for command submission and lost-response observation."""
from __future__ import annotations

import hashlib


def command_identity(key: str) -> tuple[str, str]:
    key = str(key or "")
    if not key or len(key) > 512:
        from fastapi import HTTPException

        raise HTTPException(400, "Idempotency-Key is required and must be at most 512 characters")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return "cmd_" + digest[:32], digest
