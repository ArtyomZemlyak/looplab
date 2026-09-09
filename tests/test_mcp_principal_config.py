"""MCP servers are a property of the PARTY, not of the process (doc 27).

The residue `McpTools.cached()`'s config keying could not supply: the three configuration sources
(`LOOPLAB_MCP_CONFIG`, `LOOPLAB_MCP_SERVERS`, `<repo>/.mcp.json`) are all process-wide, so every
session on a shared hub resolved the SAME server set whichever principal was driving it — and MCP
tools are arbitrary external side effects, which is why `GatedMcpTools` asks about every one of
them. `serve/principal.py::mcp_config_scope` now decides which configuration a party may connect
and `tools/mcp_tools.py::principal_mcp_config` resolves it.

The properties driven here, each against the thing it would have been easy to get wrong:

  * the decision is taken BEFORE the cache. A test that only asserted "two principals get different
    tools" would pass with the principal folded into the cache key, which is the shape the cache's
    own comment refuses: a cache key that grants access grants it by collision;
  * a declared per-principal root FAILS CLOSED for a scope with no file — it does not inherit the
    process-wide set, which would hand the one party the operator did not configure the servers
    they configured for someone else;
  * with NO root declared, every owner-plane party still gets the historical process-wide
    configuration, byte-identical: a single-user deployment must not change;
  * `review`/`anonymous`/no-principal connect nothing at all, and the scope never becomes a path.
"""
from __future__ import annotations

import json

import pytest

from looplab.serve.principal import (ANONYMOUS_PRINCIPAL, LOCAL_PRINCIPAL, OWNER_PRINCIPAL,
                                     mcp_config_scope, review_principal)
from looplab.tools import mcp_tools
from looplab.tools.mcp_tools import McpTools, principal_mcp_config


@pytest.fixture(autouse=True)
def _clean_mcp_env(monkeypatch):
    for name in ("LOOPLAB_MCP_CONFIG", "LOOPLAB_MCP_SERVERS", mcp_tools.MCP_CONFIG_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mcp_tools, "_CACHED", {})
    monkeypatch.setattr(mcp_tools, "REPO_ROOT", __import__("pathlib").Path("/nonexistent-repo-root"))


def _servers(name: str, url: str) -> str:
    return json.dumps({"mcpServers": {name: {"url": url}}})


def test_only_the_owner_plane_gets_a_scope_and_the_rest_get_nothing():
    assert mcp_config_scope(OWNER_PRINCIPAL)[0] == "owner"
    assert mcp_config_scope(LOCAL_PRINCIPAL)[0] == "local"
    # The two parties that may not: a review capability is one run and read-only, and a caller that
    # presented nothing is anonymous. Both refusals SAY which party they refused.
    for party in (review_principal({"id": "link-1"}), ANONYMOUS_PRINCIPAL, None, "nonsense"):
        scope, why = mcp_config_scope(party)
        assert scope is None and "may not connect MCP servers" in why


def test_a_declared_root_gives_each_party_its_own_servers_and_fails_closed_for_the_rest(
        tmp_path, monkeypatch):
    root = tmp_path / "mcp"
    root.mkdir()
    (root / "owner.json").write_text(_servers("owner_only", "https://owner.example/mcp"),
                                     encoding="utf-8")
    monkeypatch.setenv(mcp_tools.MCP_CONFIG_DIR_ENV, str(root))
    # A process-wide configuration exists AND is deliberately different: with a per-principal root
    # declared it must not leak into any scope, or the fallback is the isolation hole.
    monkeypatch.setenv("LOOPLAB_MCP_SERVERS", _servers("everyone", "https://shared.example/mcp"))

    assert principal_mcp_config("owner") == {"owner_only": {"url": "https://owner.example/mcp"}}
    # `local` has no file: no servers, NOT the process-wide set the operator wrote for nobody.
    assert principal_mcp_config("local") == {}
    assert principal_mcp_config(None) == {}


def test_without_a_declared_root_the_owner_plane_keeps_the_process_wide_configuration(monkeypatch):
    monkeypatch.setenv("LOOPLAB_MCP_SERVERS", _servers("shared", "https://shared.example/mcp"))
    historical = mcp_tools.load_config()
    assert historical == {"shared": {"url": "https://shared.example/mcp"}}
    assert principal_mcp_config("owner") == historical
    assert principal_mcp_config("local") == historical
    assert principal_mcp_config(None) == {}


def test_a_scope_is_a_name_and_never_a_path(tmp_path, monkeypatch):
    root = tmp_path / "mcp"
    (root / "nested").mkdir(parents=True)
    (root / "nested" / "owner.json").write_text(_servers("deep", "https://deep.example/mcp"),
                                                encoding="utf-8")
    monkeypatch.setenv(mcp_tools.MCP_CONFIG_DIR_ENV, str(root))
    for hostile in ("../nested/owner", "nested/owner", "/etc/passwd", "OWNER", "", 7):
        assert principal_mcp_config(hostile) == {}


def test_the_toolset_a_party_receives_is_decided_before_the_cache_is_consulted(
        tmp_path, monkeypatch):
    """The end-to-end property, driven through `build_tools` with a real connect accountant.

    The cache is left ON: what is asserted is that the review principal's turn never REACHES it
    (nothing is connected for it), while the owner's turn connects its own configuration — i.e.
    the authorization happened at `mcp_config_scope`, not by a key that happened not to collide.
    """
    pytest.importorskip("fastapi")
    from looplab.serve.assistant import build_tools

    root = tmp_path / "mcp"
    root.mkdir()
    (root / "owner.json").write_text(_servers("owner_only", "https://owner.example/mcp"),
                                     encoding="utf-8")
    (root / "local.json").write_text(_servers("local_only", "https://local.example/mcp"),
                                     encoding="utf-8")
    monkeypatch.setenv(mcp_tools.MCP_CONFIG_DIR_ENV, str(root))

    connected: list[dict] = []

    class _FakeServer:
        def __init__(self, name):
            self.name = name

        def tools(self):
            return [{"name": "echo", "description": "", "input_schema": {}}]

        def call(self, tool, args):
            return "echoed"

    def _from_config(cls, cfg=None):
        cfg = mcp_tools.load_config() if cfg is None else cfg
        connected.append(dict(cfg))
        return McpTools([_FakeServer(name) for name in cfg])

    monkeypatch.setattr(McpTools, "from_config", classmethod(_from_config))

    def _mcp_names(principal):
        tools = build_tools(tmp_path, mode="default", mcp=True, approver=lambda a: "allow_once",
                            principal=principal)
        return {spec["function"]["name"] for spec in tools.specs()
                if spec["function"]["name"].startswith("mcp__")}

    assert _mcp_names(OWNER_PRINCIPAL) == {"mcp__owner_only__echo"}
    assert _mcp_names(LOCAL_PRINCIPAL) == {"mcp__local_only__echo"}
    # The two parties that may not connect: no tools, and — the load-bearing half — no connection
    # attempt at all, so no stdio subprocess is started on their behalf.
    before = len(connected)
    assert _mcp_names(review_principal({"id": "link-1"})) == set()
    assert _mcp_names(ANONYMOUS_PRINCIPAL) == set()
    assert _mcp_names(None) == set()
    assert len(connected) == before, "a refused party must reach no connect at all"
    assert [sorted(cfg) for cfg in connected] == [["owner_only"], ["local_only"]]

    # And the cache still does its own job: the owner's second turn reuses the same handles.
    assert _mcp_names(OWNER_PRINCIPAL) == {"mcp__owner_only__echo"}
    assert len(connected) == 2, "an unchanged configuration must not re-connect"
