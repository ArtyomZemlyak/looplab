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


class _Resp:
    """Minimal urlopen-response stand-in: a final url, a peer socket and a body."""
    def __init__(self, url, body=b"", chunks=None):
        self.url = url
        self._body = body
        self._chunks = list(chunks or [])

    def read(self, n=-1):
        if self._chunks:
            return self._chunks.pop(0)
        out, self._body = self._body[:n], self._body[n:]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fetching(monkeypatch, resp):
    monkeypatch.setattr(web._SSRF_OPENER, "open", lambda req, timeout=None: resp)
    monkeypatch.setattr(web, "_peer_blocked", lambda r: "peer is loopback")


def test_the_proxy_question_is_asked_about_the_hop_actually_connected_to(monkeypatch):
    """`_proxied` was consulted with the ORIGINAL url while the peer socket belongs to the FINAL
    redirect hop. Proxy-ness flips mid-chain (a NO_PROXY host, an http->https redirect with only one
    of http_proxy/https_proxy set), so an original-proxied -> final-direct chain SKIPPED the peer
    check on the very direct connection it exists for, reopening the DNS-rebind window."""
    proxied = {"https://proxied.test/start": True, "https://direct.test/landed": False}
    monkeypatch.setattr(web, "_proxied", lambda u: proxied[u])
    _fetching(monkeypatch, _Resp("https://direct.test/landed", b"internal body"))

    out = web.WebTools()._get("https://proxied.test/start")
    assert out == "(blocked: peer is loopback)", out       # was: the internal body was returned

    # …and the inverse: a direct start that LANDS behind the proxy must not be false-blocked.
    proxied = {"https://direct.test/start": False, "https://proxied.test/landed": True}
    _fetching(monkeypatch, _Resp("https://proxied.test/landed", b"page text"))
    assert web.WebTools()._get("https://direct.test/start") == "page text"


def test_a_slow_drip_body_cannot_hold_the_tool_thread_forever(monkeypatch):
    """`read(n)` blocks until n bytes or EOF and urlopen's timeout is per-socket-op, so a server
    dripping a byte just inside it held this thread for up to ~2M reads. `drive_tool_loop` runs
    `tools.execute()` synchronously and its `time_budget_s` does not interrupt an in-flight turn,
    so one slow-drip web_fetch wedged the whole research phase."""
    clock = {"t": 0.0}
    monkeypatch.setattr(web.time, "monotonic", lambda: clock["t"])

    class _Drip:
        reads = 0

        def read(self, n=-1):
            _Drip.reads += 1
            clock["t"] += 0.9          # each recv lands just inside the per-op timeout
            return b"x"

    got = web._read_bounded(_Drip(), timeout=1.0)
    assert 0 < len(got) < web._MAX_DOWNLOAD_BYTES        # truncated, not endless
    assert _Drip.reads <= 10, _Drip.reads                # deadline = 1.0 * _READ_DEADLINE_FACTOR

    # A cooperative server still gets its whole body, and EOF still ends the read.
    clock["t"] = 0.0
    assert web._read_bounded(_Resp("u", b"hello world"), timeout=30.0) == b"hello world"


def test_untag_is_linear_on_unclosed_script_tags():
    """The old `<(script|style)\\b.*?</\\1>` re-scanned to end-of-document for each of n unclosed
    opens — ~n²/2 steps, minutes of CPU on a 2MB page, on the tool-loop thread nothing interrupts."""
    import time as _time
    assert web._untag("<p>hi<script>var x=1;</script>there</p>") == "hi there"
    assert web._untag("<STYLE>a{}</STYLE>x") == "x"
    assert web._untag("<script >a</script >b") == "b"
    assert web._untag("a<script>b") == "a b"             # unclosed: only the tag itself is stripped

    start = _time.perf_counter()
    web._untag("<script>" * 40_000)
    assert _time.perf_counter() - start < 2.0, "still quadratic on unclosed script tags"


def test_the_pip_child_gets_the_same_value_aware_secret_screen_as_every_other_spawn(monkeypatch):
    """A NAME-only filter diverged from `is_secret_env(k, v)`, which every other child spawn uses:
    an inline-credential URL (`postgres://user:pw@host`) under an innocent name reached the sdist's
    setup.py — arbitrary code — while being stripped everywhere else."""
    from looplab.runtime import deps

    monkeypatch.setattr(deps.os, "environ", {
        "PATH": "/usr/bin",
        "LLM_API_KEY": "sk-secret",                          # name screen
        "DATABASE_URL": "postgres://user:pw@db/app",         # VALUE screen — name matches nothing
        "PIP_INDEX_URL": "https://tok:tok@private/simple",    # credential BY DESIGN, must survive
        "LOOPLAB_LLM_BASE_URL": "https://api.test/v1",        # plain endpoint, must survive
    })
    seen = {}

    def _run(argv, **kw):
        seen.update(kw["env"])
        raise OSError("stop here — the env is what this asserts")

    monkeypatch.setattr(deps.subprocess, "run", _run)
    deps.install("numpy")

    assert "LLM_API_KEY" not in seen
    assert "DATABASE_URL" not in seen, "an inline-credential URL reached the pip child"
    assert seen["PIP_INDEX_URL"] == "https://tok:tok@private/simple"
    assert seen["LOOPLAB_LLM_BASE_URL"] == "https://api.test/v1" and seen["PATH"] == "/usr/bin"
