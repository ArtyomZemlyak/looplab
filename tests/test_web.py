"""SSRF guard for outbound tool fetches (blocks loopback / link-local / cloud-metadata targets).

Regression from the second whole-codebase review pass (security: SSRF)."""
from __future__ import annotations

from looplab.tools.web import _ssrf_blocked


def test_ssrf_blocks_internal_addresses():
    assert _ssrf_blocked("http://127.0.0.1/x")                          # loopback
    assert _ssrf_blocked("http://169.254.169.254/latest/meta-data/")   # cloud metadata (link-local)
    assert _ssrf_blocked("http://localhost:8765/")                     # resolves to loopback
