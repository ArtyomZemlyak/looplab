"""The web tool's SSRF defences: what each layer can actually prove, and when one of them is
answering a different question than the one it was written for."""
from __future__ import annotations

import urllib.request

import looplab.tools.web as web


def test_the_landed_peer_check_is_skipped_when_urllib_will_proxy_the_request(monkeypatch):
    """`_peer_blocked` inspects the socket we actually connected to. Behind an env-configured proxy
    that socket goes to the PROXY, so on the common loopback/RFC1918 proxy the check refused every
    single fetch — a total false positive — while proving nothing about the target either way."""
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    assert web._proxied("https://example.test/page") is True

    # NO_PROXY still wins: a bypassed host is connected to directly, so the peer check applies.
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)
    assert web._proxied("https://example.test/page") is False


def test_no_proxy_configured_leaves_the_peer_check_in_force(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    assert web._proxied("https://example.test/page") is False


def test_an_unreadable_proxy_environment_does_not_decide_the_fetch(monkeypatch):
    """A raising `getproxies` must not silently disable the peer check (nor block the fetch)."""
    def _boom():
        raise RuntimeError("unreadable proxy env")

    monkeypatch.setattr(urllib.request, "getproxies", _boom)
    assert web._proxied("https://example.test/page") is False


def test_the_preflight_still_refuses_a_private_target_regardless_of_proxying(monkeypatch):
    """Only the LANDED-peer check is proxy-conditional; the preflight resolve is not."""
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:8080"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    assert web._ssrf_blocked("https://localhost/secret")
