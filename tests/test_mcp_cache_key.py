"""The MCP adapter cache answers for the CONFIGURATION, not for whoever asked first.

`McpTools.cached()` held one process-global instance, so it returned whatever server set the first
caller in the process happened to resolve — forever. Two consequences, both wrong:

* an operator who edits `.mcp.json` or re-points `LOOPLAB_MCP_CONFIG` keeps talking to the OLD
  servers with no way to tell, and an MCP tool is an arbitrary external side effect, so "which
  server am I actually calling" is not a detail;
* the day a per-principal configuration source exists, an unkeyed cache hands one principal the
  servers another principal's session connected — the cache would be the thing that broke the
  isolation, with no code change anywhere near it.

The key is a digest of the config `load_config` resolves, i.e. of the thing that actually determines
the server set. Deliberately not "the principal": no per-principal config source exists today, so
keying on an identity the config does not vary with would spawn N identical subprocess sets and buy
nothing.
"""
from __future__ import annotations

import json

import pytest

from looplab.tools import mcp_tools
from looplab.tools.mcp_tools import McpTools


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    monkeypatch.setattr(mcp_tools, "_CACHED", {})
    monkeypatch.delenv("LOOPLAB_MCP_SERVERS", raising=False)
    monkeypatch.delenv("LOOPLAB_MCP_CONFIG", raising=False)


@pytest.fixture
def _counting(monkeypatch):
    """Count `from_config` calls without connecting to anything."""
    calls: list[dict] = []

    def _fake(cls):
        calls.append(mcp_tools.load_config())
        return cls([])

    monkeypatch.setattr(McpTools, "from_config", classmethod(_fake))
    return calls


def _servers(monkeypatch, **spec):
    monkeypatch.setenv("LOOPLAB_MCP_SERVERS", json.dumps({"mcpServers": spec}))


def test_the_same_configuration_connects_ONCE(monkeypatch, _counting):
    """Unchanged and load-bearing: a live server owns a thread, an event loop and a subprocess, so
    this must not run per assistant turn."""
    _servers(monkeypatch, alpha={"command": "a"})
    first, second, third = McpTools.cached(), McpTools.cached(), McpTools.cached()
    assert first is second is third
    assert len(_counting) == 1


def test_a_CHANGED_configuration_gets_its_own_provider(monkeypatch, _counting):
    """THE DEFECT. MUTATION: restore the single `_CACHED` -> the second call returns servers from a
    configuration that is no longer in effect, for the life of the process."""
    _servers(monkeypatch, alpha={"command": "a"})
    first = McpTools.cached()
    _servers(monkeypatch, beta={"command": "b"})
    second = McpTools.cached()

    assert first is not second
    assert [sorted(cfg) for cfg in _counting] == [["alpha"], ["beta"]]


def test_switching_BACK_reuses_the_first_provider(monkeypatch, _counting):
    """It is a key, not an invalidation: returning to a configuration must not re-spawn its servers,
    which is the leak the cache exists to prevent (nothing here can be closed)."""
    _servers(monkeypatch, alpha={"command": "a"})
    first = McpTools.cached()
    _servers(monkeypatch, beta={"command": "b"})
    McpTools.cached()
    _servers(monkeypatch, alpha={"command": "a"})

    assert McpTools.cached() is first
    assert len(_counting) == 2


def test_a_reordered_but_identical_config_is_the_SAME_key(monkeypatch, _counting):
    """The digest is canonical, so key order in the JSON does not spawn a second server set."""
    monkeypatch.setenv("LOOPLAB_MCP_SERVERS", json.dumps(
        {"mcpServers": {"a": {"command": "x", "args": []}, "b": {"command": "y"}}}))
    first = McpTools.cached()
    monkeypatch.setenv("LOOPLAB_MCP_SERVERS", json.dumps(
        {"mcpServers": {"b": {"command": "y"}, "a": {"args": [], "command": "x"}}}))
    assert McpTools.cached() is first
    assert len(_counting) == 1


def test_no_configuration_at_all_is_still_cached(monkeypatch, _counting):
    """The overwhelmingly common case in this repo's own tests: no MCP config, one inert provider."""
    assert McpTools.cached() is McpTools.cached()
    assert len(_counting) == 1


def test_the_number_of_distinct_configurations_is_BOUNDED(monkeypatch, _counting):
    """NOTHING IS EVICTED, because a handle owns a thread, a loop and a subprocess and exposes no
    way to close them — dropping one from the map leaks all three. So the bound is on how many
    configurations a process will CONNECT for, and past it this answers with the inert provider
    rather than spawning more.

    MUTATION: make it an LRU -> every eviction leaks a thread, a loop and a subprocess per server.
    """
    for index in range(mcp_tools._MAX_CACHED_CONFIGS):
        _servers(monkeypatch, **{f"s{index}": {"command": str(index)}})
        McpTools.cached()
    assert len(_counting) == mcp_tools._MAX_CACHED_CONFIGS

    _servers(monkeypatch, overflow={"command": "z"})
    over = McpTools.cached()
    assert over.servers == [], "past the bound it is inert, not another set of subprocesses"
    assert len(_counting) == mcp_tools._MAX_CACHED_CONFIGS, "and it connected nothing"

    # ...and every configuration already connected still answers with its own provider.
    _servers(monkeypatch, s0={"command": "0"})
    assert McpTools.cached() is not None and len(_counting) == mcp_tools._MAX_CACHED_CONFIGS


def test_concurrent_first_calls_connect_once(monkeypatch):
    """The double-checked lock the cache already had, kept: two tabs arriving together would
    otherwise both miss, both connect, and the loser's server set would orphan for the process
    lifetime."""
    import threading

    _servers(monkeypatch, alpha={"command": "a"})
    started = threading.Barrier(4)
    connects = []

    def _slow(cls):
        connects.append(1)
        return cls([])

    monkeypatch.setattr(McpTools, "from_config", classmethod(_slow))
    got: list = []

    def _worker():
        started.wait()
        got.append(McpTools.cached())

    threads = [threading.Thread(target=_worker) for _ in range(3)]
    for t in threads:
        t.start()
    started.wait()
    for t in threads:
        t.join()

    assert len(connects) == 1, "one configuration, one connect"
    assert got[0] is got[1] is got[2]
