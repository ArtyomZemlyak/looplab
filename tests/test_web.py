"""SSRF guard for outbound tool fetches (blocks loopback / link-local / cloud-metadata targets).

Regression from the second whole-codebase review pass (security: SSRF)."""
from __future__ import annotations

from looplab.tools.web import _ssrf_blocked


def test_ssrf_blocks_internal_addresses():
    assert _ssrf_blocked("http://127.0.0.1/x")                          # loopback
    assert _ssrf_blocked("http://169.254.169.254/latest/meta-data/")   # cloud metadata (link-local)
    assert _ssrf_blocked("http://localhost:8765/")                     # resolves to loopback


def test_peer_verification_closes_the_dns_rebind_toctou():
    """`_ssrf_blocked` and the transport resolve DNS INDEPENDENTLY — a preflight cannot be the boundary.

    A short-TTL rebind hands the check a public address and the connect a loopback / RFC1918 /
    169.254.169.254 one, so the guard passed and the internal body was returned to the caller (and
    therefore the model). Verifying the address actually connected to closes it for the case that
    matters: nothing is read from an internal peer.
    """
    from looplab.tools.web import _peer_blocked

    class _Sock:
        def __init__(self, ip):
            self._ip = ip

        def getpeername(self):
            return (self._ip, 80)

    class _Resp:
        def __init__(self, ip):
            self.fp = type("f", (), {"raw": type("r", (), {"_sock": _Sock(ip)})()})()

    for internal in ("127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.9", "::1"):
        assert _peer_blocked(_Resp(internal)), internal
    assert _peer_blocked(_Resp("8.8.8.8")) is None
    # an unrecognized transport shape must not break fetching (prior behaviour preserved)
    assert _peer_blocked(object()) is None
